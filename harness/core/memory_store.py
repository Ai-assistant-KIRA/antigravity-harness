"""
Persistent memory, contrastive lessons, and the experience library for Harness².

The store is deliberately boring: SQLite with WAL, explicit scoping by project,
idempotent session ingestion, and a human-readable markdown mirror. It never
records credentials (the analyzer redacts before data reaches this layer) and it
does not promote a lesson to a rule just because it happened once.

Two kinds of memory are kept apart:

* verified lessons - a failure followed by an observed, successful recovery;
* candidate observations - unresolved failures. These are searchable and useful
  for future debugging, but they are never turned into AGENTS.md rules.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Sequence

from .analyzer import ContrastiveEvent, SessionAnalysis
from .config import (
    get_effective_experience_md_path,
    get_effective_memory_db_path,
    get_lesson_id_counter_path,
    get_memory_db_path,
    project_fingerprint,
)
from .redaction import redact_mapping
from .util import (
    atomic_write_text,
    file_lock,
    normalize_for_compare,
    one_line,
    read_json,
    truncate,
    utc_iso,
    write_json,
)

SCHEMA_VERSION = 4

_RULE_SECTIONS = {"directive", "quirk", "pitfall"}
_SECTION_ALIASES = {
    "directive": "directive",
    "directives": "directive",
    "standing project directives": "directive",
    "quirk": "quirk",
    "quirks": "quirk",
    "environment & tooling quirks": "quirk",
    "pitfall": "pitfall",
    "pitfalls": "pitfall",
    "learned pitfalls & recovery invariants": "pitfall",
}


def rule_hash(text: str) -> str:
    """Stable identity for a rule, independent of case and punctuation noise."""
    normalized = normalize_for_compare(text, keep_code=True)
    return hashlib.sha256(
        normalized.encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def _normalize_rule_section(value: Any) -> str:
    key = one_line(str(value or ""), 80).lower()
    return _SECTION_ALIASES.get(key, key if key in _RULE_SECTIONS else "")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_datetime(value: Any) -> datetime | None:
    """Parse a stored ISO timestamp defensively."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _recency_bonus(value: Any) -> float:
    """+0.10 when seen within 7 days, +0.05 within 30 days, else 0."""
    seen = _as_datetime(value)
    if seen is None:
        return 0.0
    age_days = max(
        0.0,
        (datetime.now(timezone.utc) - seen).total_seconds() / 86400.0,
    )
    if age_days <= 7.0:
        return 0.10
    if age_days <= 30.0:
        return 0.05
    return 0.0


def lesson_utility(lesson: dict[str, Any]) -> float:
    """Value of a lesson as a briefing candidate, in the range 0.0-2.0.

    The score rewards confidence, repeated evidence, verification, and
    recency. It penalises lessons that keep recurring after being surfaced
    (they did not prevent the failure) and lessons that are surfaced too often
    without showing new value.
    """
    confidence = max(0.0, min(1.0, _as_float(lesson.get("confidence"), 0.0)))
    occurrences = max(0, _as_int(lesson.get("occurrences"), 0))
    surfaced = max(0, _as_int(lesson.get("surfaced_count"), 0))
    recurred = max(0, _as_int(lesson.get("post_surface_occurrences"), 0))
    score = confidence
    score += 0.05 * min(occurrences, 5)
    if lesson.get("verified"):
        score += 0.10
    score += _recency_bonus(lesson.get("last_seen"))
    score -= 0.12 * recurred
    score -= 0.01 * min(surfaced, 10)
    return round(max(0.0, min(2.0, score)), 6)


def skill_utility(skill: dict[str, Any]) -> float:
    """Value of an indexed skill as a briefing candidate, in 0.0-2.0.

    Quality is the base signal. Each observed reuse adds up to +0.50, recent
    use adds a small bonus, and repeated surfacing without reuse costs up to
    -0.10.
    """
    quality = max(0.0, _as_float(skill.get("quality"), 0.0))
    reused = max(0, _as_int(skill.get("reused_count"), 0))
    surfaced = max(0, _as_int(skill.get("surfaced_count"), 0))
    score = quality
    score += 0.05 * min(reused, 10)
    score -= 0.01 * min(surfaced, 10)
    score += _recency_bonus(skill.get("last_used_at") or skill.get("updated_at"))
    return round(max(0.0, min(2.0, score)), 6)


_SKILL_SCOPE_RANK = {"workspace": 0, "user": 1, "plugin": 2, "global": 3}


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement script without commits between statements.

    ``sqlite3.executescript`` commits the open transaction before running, which
    would make schema migration non-atomic. This helper keeps every statement in
    the caller's transaction.
    """
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def _lesson_fingerprint(project: str, event: ContrastiveEvent) -> str | None:
    from .util import normalize_for_compare

    signature = normalize_for_compare(event.error_signature or event.error_message, keep_code=True)
    if not signature:
        signature = normalize_for_compare(event.resolution_summary, keep_code=True)
    if not signature:
        return None
    base = f"{project}|{event.category}|{event.trigger_tool}|{signature}"
    return hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()[:32]


def _looks_like_noise(text: str) -> bool:
    """Cheap guard used only when importing legacy v1 rows."""
    import re

    lowered = (text or "").lower()
    if not lowered.strip():
        return True
    if "\ufffd" in text:
        return True
    if re.search(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}", lowered):
        return True
    if "command exited with code" in lowered or "completed at:" in lowered:
        return True
    if "finished with result" in lowered or "showing lines" in lowered:
        return True
    if re.match(r"^[0-9a-f-]{20,}", lowered):
        return True
    if len(lowered) < 12:
        return True
    return False


class MemoryStore:
    """Manages persistent cross-session memory and the experience library."""

    def __init__(
        self,
        db_path: Path | None = None,
        exp_md_path: Path | None = None,
        workspace: str | Path | None = None,
        initialize: bool = True,
        read_only: bool = False,
    ):
        self.workspace = str(workspace) if workspace else ""
        self.project = project_fingerprint(self.workspace) if self.workspace else "global"
        self.db_path = (
            Path(db_path)
            if db_path
            else get_effective_memory_db_path(self.workspace or None)
        )
        self.exp_md_path = (
            Path(exp_md_path)
            if exp_md_path
            else get_effective_experience_md_path(self.workspace or None)
        )
        self.isolated = self.db_path != get_memory_db_path()
        self.fts_enabled = False
        self.read_only = read_only
        created = not self.db_path.exists()
        if initialize:
            self._init_db()
            if created and self.isolated and not read_only:
                self._seed_from_global()
        elif self.db_path.exists():
            self.fts_enabled = self._detect_fts()

    # --------------------------------------------------------------- connection
    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        if self.read_only:
            if not self.db_path.exists():
                raise FileNotFoundError(str(self.db_path))
            uri = "file:" + self.db_path.as_posix() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if not self.read_only:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            if not self.read_only:
                conn.commit()
        except Exception:
            if not self.read_only:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _detect_fts(self) -> bool:
        if not self.db_path.exists():
            return False
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'lessons_fts'"
                ).fetchone()
                return row is not None
        except sqlite3.DatabaseError:
            return False

    # ------------------------------------------------------------------- schema
    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.DatabaseError:
            return set()
        return {str(row["name"]) for row in rows}

    def _init_db(self) -> None:
        with self._connect() as conn:
            # The common case is an already-current database. Avoid taking a
            # write lock and rebuilding the FTS index on every hook invocation.
            if self._schema_ready(conn):
                return
            conn.execute("BEGIN IMMEDIATE")
            lesson_columns = self._table_columns(conn, "lessons")
            session_columns = self._table_columns(conn, "sessions")
            skill_columns = self._table_columns(conn, "skills_index")
            legacy = bool(lesson_columns and "fingerprint" not in lesson_columns)
            legacy = legacy or bool(session_columns and "project" not in session_columns)
            legacy = legacy or bool(skill_columns and "scope" not in skill_columns)

            if legacy:
                self._migrate_v1(conn)
            else:
                self._create_schema(conn)
                self._ensure_columns(conn)

            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5("
                    "trigger_tool, error_pattern, resolution, "
                    "content='lessons', content_rowid='id', tokenize='unicode61')"
                )
                _execute_script(
                    conn,
                    """
                    CREATE TRIGGER IF NOT EXISTS lessons_ai AFTER INSERT ON lessons BEGIN
                        INSERT INTO lessons_fts(rowid, trigger_tool, error_pattern, resolution)
                        VALUES (new.id, new.trigger_tool, new.error_pattern, new.resolution);
                    END;
                    CREATE TRIGGER IF NOT EXISTS lessons_ad AFTER DELETE ON lessons BEGIN
                        INSERT INTO lessons_fts(lessons_fts, rowid, trigger_tool, error_pattern, resolution)
                        VALUES ('delete', old.id, old.trigger_tool, old.error_pattern, old.resolution);
                    END;
                    CREATE TRIGGER IF NOT EXISTS lessons_au AFTER UPDATE ON lessons BEGIN
                        INSERT INTO lessons_fts(lessons_fts, rowid, trigger_tool, error_pattern, resolution)
                        VALUES ('delete', old.id, old.trigger_tool, old.error_pattern, old.resolution);
                        INSERT INTO lessons_fts(rowid, trigger_tool, error_pattern, resolution)
                        VALUES (new.id, new.trigger_tool, new.error_pattern, new.resolution);
                    END;
                    """,
                )
                # This path runs only on first creation, migration, or repair.
                # Unconditionally rebuilding on every open made SessionStart and
                # Stop O(number of lessons) in write work.
                conn.execute("INSERT INTO lessons_fts(lessons_fts) VALUES('rebuild')")
                self.fts_enabled = True
            except sqlite3.DatabaseError:
                # FTS5 is optional. Search falls back to LIKE.
                self.fts_enabled = False

    def _advance_lesson_id_counter(self, minimum: int = 0) -> int:
        """Raise the shared "last issued id" counter to at least ``minimum``."""
        counter_path = get_lesson_id_counter_path()
        lock_path = counter_path.with_name(counter_path.name + ".lock")
        with file_lock(lock_path, timeout=5.0):
            try:
                reserved = int(counter_path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                reserved = 0
            value = max(reserved, int(minimum or 0))
            if value > reserved:
                try:
                    counter_path.write_text(str(value), encoding="utf-8")
                except OSError:
                    pass
            return value

    def _next_lesson_id(self, conn: sqlite3.Connection) -> int:
        """Reserve a lesson id that is unique across isolated databases."""
        try:
            row = conn.execute("SELECT MAX(id) FROM lessons").fetchone()
            highest = int(row[0] or 0) if row else 0
        except sqlite3.DatabaseError:
            highest = 0
        counter_path = get_lesson_id_counter_path()
        lock_path = counter_path.with_name(counter_path.name + ".lock")
        with file_lock(lock_path, timeout=5.0):
            try:
                reserved = int(counter_path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                reserved = 0
            next_id = max(reserved, highest) + 1
            try:
                counter_path.write_text(str(next_id), encoding="utf-8")
            except OSError:
                pass
            return next_id

    def _seed_from_global(self) -> None:
        """Copy pre-existing global environment lessons into a new project store.

        Projects are isolated, but environment lessons that were learned before
        isolation was enabled should not silently disappear. This runs once,
        when the project database is first created, and only copies rows whose
        project scope is ``global``; another project's lessons are never
        imported.
        """
        global_path = get_memory_db_path()
        if not self.workspace or not global_path.exists():
            return
        if self.db_path.resolve() == global_path.resolve():
            return
        try:
            source = sqlite3.connect(
                "file:" + global_path.as_posix() + "?mode=ro", uri=True, timeout=5
            )
            source.row_factory = sqlite3.Row
        except sqlite3.DatabaseError:
            return
        try:
            with self._connect() as conn:
                marked = conn.execute(
                    "SELECT value FROM meta WHERE key = 'seeded_from_global'"
                ).fetchone()
                if marked:
                    return
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('seeded_from_global', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                try:
                    rows = source.execute(
                        "SELECT id, fingerprint, conversation_id, project, workspace, "
                        "scope, category, trigger_tool, error_pattern, resolution, "
                        "occurrences, first_seen, last_seen, confidence, verified, "
                        "source, surfaced_count, last_surfaced_at, "
                        "post_surface_occurrences "
                        "FROM lessons WHERE project = 'global'"
                    ).fetchall()
                except sqlite3.DatabaseError:
                    return
                for row in rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO lessons "
                        "(id, fingerprint, conversation_id, project, workspace, scope, "
                        "category, trigger_tool, error_pattern, resolution, occurrences, "
                        "first_seen, last_seen, confidence, verified, source, "
                        "surfaced_count, last_surfaced_at, post_surface_occurrences) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(row),
                    )
                if rows:
                    # Seeded ids come from the shared sequence; keep the counter
                    # ahead of them so new project rows cannot reuse them.
                    highest = max(int(row["id"] or 0) for row in rows)
                    self._advance_lesson_id_counter(highest)
        except (sqlite3.DatabaseError, OSError):
            return
        finally:
            source.close()

    def _schema_ready(self, conn: sqlite3.Connection) -> bool:
        """Return True when the current schema and FTS triggers are usable."""
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if not row or str(row[0]) != str(SCHEMA_VERSION):
                return False
            names = {
                str(item[0])
                for item in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
                )
            }
            required = {
                "sessions",
                "lessons",
                "session_lessons",
                "skills_index",
                "rule_edits",
                "rules_index",
            }
            if not required.issubset(names):
                return False
            required_columns = {
                "lessons": {
                    "fingerprint",
                    "surfaced_count",
                    "last_surfaced_at",
                    "post_surface_occurrences",
                },
                "skills_index": {
                    "scope",
                    "surfaced_count",
                    "last_surfaced_at",
                },
            }
            for table, columns in required_columns.items():
                if not columns.issubset(self._table_columns(conn, table)):
                    return False
            if "lessons_fts" in names:
                self.fts_enabled = {
                    "lessons_ai",
                    "lessons_ad",
                    "lessons_au",
                }.issubset(names)
                return self.fts_enabled
            if self._fts5_available(conn):
                # The schema claims to be current but the index is missing.
                return False
            # SQLite without FTS5 is a supported fallback, not a broken schema.
            self.fts_enabled = False
            return True
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def _fts5_available(conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("CREATE VIRTUAL TABLE temp.harness_fts_probe USING fts5(value)")
            conn.execute("DROP TABLE temp.harness_fts_probe")
            return True
        except sqlite3.DatabaseError:
            return False

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        _execute_script(
            conn,
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                conversation_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                workspace TEXT,
                project TEXT NOT NULL DEFAULT 'global',
                task_summary TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT 'completed',
                tools_used_count INTEGER NOT NULL DEFAULT 0,
                errors_count INTEGER NOT NULL DEFAULT 0,
                lessons_count INTEGER NOT NULL DEFAULT 0,
                transcript_hash TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                conversation_id TEXT,
                project TEXT NOT NULL DEFAULT 'global',
                workspace TEXT,
                scope TEXT NOT NULL DEFAULT 'global',
                category TEXT NOT NULL DEFAULT 'command',
                trigger_tool TEXT NOT NULL DEFAULT 'unknown',
                error_pattern TEXT NOT NULL DEFAULT '',
                resolution TEXT NOT NULL DEFAULT '',
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                verified INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'transcript',
                surfaced_count INTEGER NOT NULL DEFAULT 0,
                last_surfaced_at TEXT,
                post_surface_occurrences INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS session_lessons (
                conversation_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                PRIMARY KEY (conversation_id, fingerprint)
            );

            CREATE TABLE IF NOT EXISTS skills_index (
                name TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                project TEXT NOT NULL DEFAULT 'global',
                description TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT,
                reused_count INTEGER NOT NULL DEFAULT 0,
                update_count INTEGER NOT NULL DEFAULT 0,
                quality REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'harness',
                surfaced_count INTEGER NOT NULL DEFAULT 0,
                last_surfaced_at TEXT,
                PRIMARY KEY (name, scope, project)
            );

            CREATE TABLE IF NOT EXISTS rule_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                target_file TEXT NOT NULL,
                change_summary TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rules_index (
                rule_hash TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                section TEXT NOT NULL,
                target_file TEXT NOT NULL,
                scope TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT 'global',
                conversation_id TEXT,
                evidence TEXT NOT NULL DEFAULT '',
                occurrences INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_surfaced_at TEXT,
                surfaced_count INTEGER NOT NULL DEFAULT 0,
                helpful_count INTEGER NOT NULL DEFAULT 0,
                harmful_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_lessons_project ON lessons(project, verified, last_seen);
            CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category);
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project, timestamp);
            CREATE INDEX IF NOT EXISTS idx_skills_project ON skills_index(project, scope);
            CREATE INDEX IF NOT EXISTS idx_rules_scope_project ON rules_index(scope, project);
            """
        )

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Add outcome-tracking columns to an older database in place.

        ``CREATE TABLE IF NOT EXISTS`` never changes an existing table, so a
        v3 store needs explicit ``ALTER TABLE`` upgrades. Existing rows keep
        their data and receive the declared defaults.
        """
        additions = {
            "lessons": (
                ("surfaced_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_surfaced_at", "TEXT"),
                ("post_surface_occurrences", "INTEGER NOT NULL DEFAULT 0"),
            ),
            "skills_index": (
                ("surfaced_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_surfaced_at", "TEXT"),
            ),
        }
        for table, columns in additions.items():
            existing = self._table_columns(conn, table)
            if not existing:
                continue
            for name, ddl in columns:
                if name in existing:
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """Rebuild the v1 schema, dropping rows that were pure transcript noise."""
        now = utc_iso()
        legacy_sessions = self._rows(conn, "sessions")
        legacy_lessons = self._rows(conn, "lessons")
        legacy_skills = self._rows(conn, "skills_index")
        legacy_edits = self._rows(conn, "rule_edits")

        for table in ("sessions", "lessons", "skills_index", "rule_edits", "meta"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

        self._create_schema(conn)

        for row in legacy_sessions:
            conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(conversation_id, timestamp, updated_at, workspace, project, task_summary, "
                "outcome, tools_used_count, errors_count, lessons_count, transcript_hash) "
                "VALUES (?, ?, ?, NULL, 'global', ?, ?, ?, ?, 0, '')",
                (
                    row.get("conversation_id") or "unknown-session",
                    row.get("timestamp") or now,
                    row.get("timestamp") or now,
                    row.get("task_summary") or "",
                    row.get("outcome") or "completed",
                    int(row.get("tools_used_count") or 0),
                    int(row.get("errors_count") or 0),
                ),
            )

        seen: set[str] = set()
        for row in legacy_lessons:
            resolution = one_line(str(row.get("resolution") or ""), 500)
            error = one_line(str(row.get("error_pattern") or ""), 300)
            if _looks_like_noise(resolution) or _looks_like_noise(error):
                continue
            event = ContrastiveEvent(
                trigger_tool=str(row.get("trigger_tool") or "unknown"),
                failed_args={},
                error_message=error,
                successful_args=None,
                resolution_summary=resolution,
                category=str(row.get("category") or "command"),
                error_signature=error,
            )
            fingerprint = _lesson_fingerprint("global", event)
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            try:
                conn.execute(
                    "INSERT INTO lessons "
                    "(fingerprint, conversation_id, project, workspace, scope, category, "
                    "trigger_tool, error_pattern, resolution, occurrences, first_seen, last_seen, "
                    "confidence, verified, source) "
                    "VALUES (?, ?, 'global', NULL, 'global', ?, ?, ?, ?, ?, ?, ?, 0.6, 1, 'migration')",
                    (
                        fingerprint,
                        row.get("conversation_id"),
                        event.category,
                        event.trigger_tool,
                        error,
                        resolution,
                        max(1, int(row.get("occurrences") or 1)),
                        row.get("first_seen") or now,
                        row.get("last_seen") or now,
                    ),
                )
            except sqlite3.IntegrityError:
                continue

        for row in legacy_skills:
            path = str(row.get("file_path") or "")
            if not path:
                continue
            name = str(row.get("name") or Path(path).parent.name or "skill")
            scope = "plugin" if "plugins" in path.replace("\\", "/").lower() else "external"
            conn.execute(
                "INSERT OR IGNORE INTO skills_index "
                "(name, scope, project, description, file_path, created_at, updated_at, "
                "reused_count, update_count, quality, source) "
                "VALUES (?, ?, 'global', ?, ?, ?, ?, ?, 0, 0, 'migration')",
                (
                    name,
                    scope,
                    str(row.get("description") or ""),
                    path,
                    row.get("created_at") or now,
                    row.get("updated_at") or now,
                    int(row.get("reused_count") or 0),
                ),
            )

        for row in legacy_edits:
            conn.execute(
                "INSERT INTO rule_edits (timestamp, target_file, change_summary) VALUES (?, ?, ?)",
                (
                    row.get("timestamp") or now,
                    str(row.get("target_file") or "AGENTS.md"),
                    str(row.get("change_summary") or "legacy rule edit"),
                ),
            )

    @staticmethod
    def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
        except sqlite3.DatabaseError:
            return []

    # ----------------------------------------------------------------- sessions
    def list_sessions(
        self,
        limit: int = 15,
        workspace: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """Return recently updated sessions, newest first."""
        where: list[str] = []
        params: list[Any] = []
        if workspace:
            where.append("workspace = ?")
            params.append(str(workspace))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sessions {clause} ORDER BY updated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_session_processed(self, conversation_id: str, transcript_hash: str = "") -> bool:
        if not conversation_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT transcript_hash, updated_at FROM sessions WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return False
        if transcript_hash:
            return str(row["transcript_hash"] or "") == transcript_hash
        return True

    def record_session(
        self,
        analysis: SessionAnalysis,
        transcript_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Record a session idempotently.

        Re-analysing the same transcript updates the session row but never
        double-counts a lesson that was already attributed to that conversation.
        """
        # Microsecond precision keeps surfacing-before-recurrence ordering
        # reliable even when a briefing and a session update happen together.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        workspace = str(analysis.workspace or self.workspace or "")
        project = project_fingerprint(workspace) if workspace else self.project
        scope = "workspace" if workspace else "global"
        task_summary = "; ".join(analysis.tasks)[:500] if analysis.tasks else "Interactive session"

        event_rows: list[tuple[ContrastiveEvent, str]] = []
        seen: set[str] = set()
        for event in list(analysis.contrastive_events) + list(analysis.unresolved_events):
            fingerprint = _lesson_fingerprint(project, event)
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            event_rows.append((event, fingerprint))

        created = updated = skipped = 0
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                (conversation_id, timestamp, updated_at, workspace, project, task_summary,
                 outcome, tools_used_count, errors_count, lessons_count, transcript_hash, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    workspace = COALESCE(NULLIF(excluded.workspace, ''), sessions.workspace),
                    project = excluded.project,
                    task_summary = excluded.task_summary,
                    outcome = excluded.outcome,
                    tools_used_count = excluded.tools_used_count,
                    errors_count = excluded.errors_count,
                    transcript_hash = excluded.transcript_hash,
                    metadata = excluded.metadata
                """,
                (
                    analysis.conversation_id or "unknown-session",
                    now,
                    now,
                    workspace or None,
                    project,
                    task_summary,
                    analysis.final_outcome,
                    len(analysis.tool_calls),
                    len(analysis.contrastive_events) + len(analysis.unresolved_events),
                    transcript_hash,
                    json.dumps(
                        redact_mapping(metadata or {}),
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            )

            for event, fingerprint in event_rows:
                linked = conn.execute(
                    "SELECT 1 FROM session_lessons WHERE conversation_id = ? AND fingerprint = ?",
                    (analysis.conversation_id or "unknown-session", fingerprint),
                ).fetchone() is not None

                verified = 1 if event.verified else 0
                confidence = max(0.0, min(1.0, float(event.confidence or (0.8 if verified else 0.2))))
                existing = conn.execute(
                    "SELECT id, occurrences, resolution, confidence, verified, "
                    "last_seen, last_surfaced_at, post_surface_occurrences "
                    "FROM lessons WHERE fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                if existing:
                    already_verified = bool(existing["verified"])
                    # Re-analysis of an unchanged session is a no-op. A later
                    # resolution in the same conversation may still promote a
                    # candidate, which is exactly the evidence we want.
                    if linked and (already_verified or not verified):
                        skipped += 1
                        continue
                    resolution = str(existing["resolution"] or "")
                    if verified and event.resolution_summary:
                        if (
                            not already_verified
                            or not resolution
                            or len(event.resolution_summary) >= len(resolution)
                            or confidence > float(existing["confidence"] or 0)
                        ):
                            resolution = event.resolution_summary
                    previous_seen = _as_datetime(existing["last_seen"])
                    surfaced_at = _as_datetime(existing["last_surfaced_at"])
                    # A later, unlinked occurrence *after* this lesson was
                    # surfaced means the briefing did not prevent the failure.
                    recurred = bool(
                        not linked
                        and previous_seen is not None
                        and surfaced_at is not None
                        and surfaced_at > previous_seen
                    )
                    if recurred:
                        conn.execute(
                            "UPDATE lessons SET occurrences = ?, last_seen = ?, "
                            "resolution = ?, confidence = MAX(0.1, confidence * 0.9), "
                            "verified = MAX(verified, ?), post_surface_occurrences = ?, "
                            "conversation_id = COALESCE(conversation_id, ?) WHERE id = ?",
                            (
                                int(existing["occurrences"] or 1) + 1,
                                now,
                                resolution,
                                verified,
                                int(existing["post_surface_occurrences"] or 0) + 1,
                                event.session_id or analysis.conversation_id,
                                int(existing["id"]),
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE lessons SET occurrences = ?, last_seen = ?, resolution = ?, "
                            "confidence = MAX(confidence, ?), verified = MAX(verified, ?), "
                            "conversation_id = COALESCE(conversation_id, ?) WHERE id = ?",
                            (
                                int(existing["occurrences"] or 1)
                                + (1 if not linked else 0),
                                now,
                                resolution,
                                confidence,
                                verified,
                                event.session_id or analysis.conversation_id,
                                int(existing["id"]),
                            ),
                        )
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO lessons "
                        "(id, fingerprint, conversation_id, project, workspace, scope, category, "
                        "trigger_tool, error_pattern, resolution, occurrences, first_seen, "
                        "last_seen, confidence, verified, source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'transcript')",
                        (
                            self._next_lesson_id(conn),
                            fingerprint,
                            analysis.conversation_id or event.session_id or None,
                            project,
                            workspace or None,
                            event.scope or scope,
                            event.category or "command",
                            event.trigger_tool or "unknown",
                            truncate(event.error_message, 300),
                            truncate(event.resolution_summary, 500),
                            now,
                            now,
                            confidence,
                            verified,
                        ),
                    )
                    created += 1

                conn.execute(
                    "INSERT OR IGNORE INTO session_lessons (conversation_id, fingerprint, seen_at) "
                    "VALUES (?, ?, ?)",
                    (analysis.conversation_id or "unknown-session", fingerprint, now),
                )

            conn.execute(
                "UPDATE sessions SET lessons_count = "
                "(SELECT COUNT(*) FROM session_lessons WHERE conversation_id = ?) "
                "WHERE conversation_id = ?",
                (
                    analysis.conversation_id or "unknown-session",
                    analysis.conversation_id or "unknown-session",
                ),
            )

        # Skill usage observed in the transcript is a real re-use signal.
        for name in analysis.skill_usage:
            self.record_skill_reuse(name, workspace=workspace or None)

        if created or updated or skipped:
            self.sync_experience_md()

        return {"created": created, "updated": updated, "skipped": skipped}

    def _sibling_stores(self) -> list["MemoryStore"]:
        """Open the other project databases for ``all_projects`` queries.

        Read-only peers are used, and a read-only store never fans out again, so
        this cannot recurse.
        """
        from .config import get_project_memory_db_paths, project_isolation_enabled

        if self.read_only or not project_isolation_enabled():
            return []
        try:
            current = self.db_path.resolve()
        except OSError:
            current = self.db_path
        seen = {current}
        stores: list[MemoryStore] = []
        for path in (get_memory_db_path(), *get_project_memory_db_paths()):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            try:
                stores.append(
                    MemoryStore(db_path=path, workspace=None, read_only=True)
                )
            except (sqlite3.DatabaseError, OSError):
                continue
        return stores

    def get_recent_lessons(
        self,
        limit: int = 10,
        workspace: str | Path | None = None,
        project: str | None = None,
        verified_only: bool = True,
        include_global: bool = True,
        all_projects: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        # Fetch a wider pool so utility (not raw recency) decides the final order.
        candidate_limit = min(200, max(limit, limit * 4))
        where, params = self._scope_filter(
            workspace, project, include_global, all_projects=all_projects
        )
        if verified_only:
            where.append("verified = 1")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM lessons {clause} "
                "ORDER BY last_seen DESC, id DESC LIMIT ?",
                (*params, candidate_limit),
            ).fetchall()
        lessons = [self._lesson_dict(row) for row in rows]
        if all_projects:
            lessons = self._merge_project_results(
                lessons,
                "get_recent_lessons",
                limit=limit,
                workspace=workspace,
                project=project,
                verified_only=verified_only,
                include_global=include_global,
            )
        lessons.sort(
            key=lambda item: (
                lesson_utility(item),
                str(item.get("last_seen") or ""),
            ),
            reverse=True,
        )
        return lessons[:limit]

    def _merge_project_results(
        self,
        results: list[dict[str, Any]],
        method: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Union local results with every other project database."""
        merged = list(results)
        seen = {item.get("id") for item in merged}
        for peer in self._sibling_stores():
            try:
                extra = getattr(peer, method)(**kwargs, all_projects=True)
            except (sqlite3.DatabaseError, OSError):
                continue
            for item in extra:
                lesson_id = item.get("id")
                if lesson_id in seen:
                    continue
                seen.add(lesson_id)
                merged.append(item)
        return merged

    def search_lessons(
        self,
        query: str,
        limit: int = 10,
        workspace: str | Path | None = None,
        include_global: bool = True,
        verified_only: bool = False,
        all_projects: bool = False,
    ) -> list[dict[str, Any]]:
        from .config import tokenize

        limit = max(1, min(int(limit), 50))
        candidate_limit = min(200, max(limit, limit * 4))
        tokens = tokenize(query)[:12]
        if not tokens:
            return self.get_recent_lessons(
                limit, workspace=workspace, verified_only=verified_only,
                include_global=include_global, all_projects=all_projects,
            )

        where, params = self._scope_filter(
            workspace, None, include_global, all_projects=all_projects
        )
        if verified_only:
            where.append("l.verified = 1")
        scope_clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows: list[sqlite3.Row] = []

        if self.fts_enabled:
            match = " OR ".join(f'"{token}"' for token in tokens)
            match_clause = (
                f"{scope_clause} AND f MATCH ?" if scope_clause else "WHERE f MATCH ?"
            )
            try:
                with self._connect() as conn:
                    rows = conn.execute(
                        "SELECT l.* FROM lessons_fts f JOIN lessons l ON l.id = f.rowid "
                        f"{match_clause} "
                        "ORDER BY bm25(f), l.verified DESC, l.last_seen DESC LIMIT ?",
                        (*params, match, candidate_limit),
                    ).fetchall()
            except sqlite3.DatabaseError:
                rows = []

        if not rows:
            like_terms = " OR ".join(
                "(l.trigger_tool LIKE ? OR l.error_pattern LIKE ? OR l.resolution LIKE ?)"
                for _ in tokens
            )
            like_params: list[Any] = []
            for token in tokens:
                pattern = f"%{token}%"
                like_params.extend([pattern, pattern, pattern])
            like_where = [*where, f"({like_terms})"]
            like_clause = "WHERE " + " AND ".join(like_where)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT l.* FROM lessons l {like_clause} "
                    "ORDER BY l.verified DESC, l.occurrences DESC, l.last_seen DESC LIMIT ?",
                    (*params, *like_params, candidate_limit),
                ).fetchall()

        lessons = [self._lesson_dict(row) for row in rows]
        if all_projects:
            lessons = self._merge_project_results(
                lessons,
                "search_lessons",
                query=query,
                limit=limit,
                workspace=workspace,
                include_global=include_global,
                verified_only=verified_only,
            )
        lessons.sort(
            key=lambda item: (
                lesson_utility(item),
                str(item.get("last_seen") or ""),
            ),
            reverse=True,
        )
        return lessons[:limit]

    @staticmethod
    def lesson_utility(lesson: dict[str, Any]) -> float:
        """Utility score used for briefing and search ordering."""
        return lesson_utility(lesson)

    @staticmethod
    def skill_utility(skill: dict[str, Any]) -> float:
        """Utility score used for skill ordering."""
        return skill_utility(skill)

    def record_lessons_surfaced(self, lesson_ids: Sequence[int]) -> None:
        """Record that these lessons were included in an injected briefing.

        This must stay cheap and non-fatal: a briefing is more important than
        an observability counter.
        """
        ids: list[int] = []
        seen: set[int] = set()
        for value in lesson_ids or ():
            lesson_id = _as_int(value, -1)
            if lesson_id > 0 and lesson_id not in seen:
                seen.add(lesson_id)
                ids.append(lesson_id)
        if not ids:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            with self._connect() as conn:
                for lesson_id in ids:
                    conn.execute(
                        "UPDATE lessons SET surfaced_count = surfaced_count + 1, "
                        "last_surfaced_at = ? WHERE id = ?",
                        (now, lesson_id),
                    )
        except (sqlite3.DatabaseError, OSError):
            return

    # --------------------------------------------------------------- editing
    @staticmethod
    def _manual_fingerprint(
        project: str,
        category: str,
        trigger_tool: str,
        error_pattern: str,
    ) -> str:
        """Stable fingerprint for a manually authored lesson.

        Digits are significant here, so this deliberately does not use
        ``normalize_for_compare`` (which collapses every number to ``n``).
        """
        signature = " ".join((error_pattern or "").casefold().split())
        base = f"{project}|{category}|{trigger_tool}|{signature}"
        return hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()[:32]

    @staticmethod
    def _lesson_in_scope(row: sqlite3.Row, workspace: str | Path) -> bool:
        project = project_fingerprint(workspace)
        return str(row["project"] or "global") in (project, "global")

    def get_lesson(
        self,
        lesson_id: int,
        workspace: str | Path | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lessons WHERE id = ?", (int(lesson_id),)
            ).fetchone()
        if not row:
            return None
        if workspace is not None and not self._lesson_in_scope(row, workspace):
            return None
        return self._lesson_dict(row)

    def list_lessons(
        self,
        limit: int = 50,
        workspace: str | Path | None = None,
        include_candidates: bool = True,
        all_projects: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        where, params = self._scope_filter(
            workspace, None, True, all_projects=all_projects
        )
        if not include_candidates:
            where.append("verified = 1")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM lessons {clause} "
                "ORDER BY verified DESC, last_seen DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        lessons = [self._lesson_dict(row) for row in rows]
        if all_projects:
            lessons = self._merge_project_results(
                lessons,
                "list_lessons",
                limit=limit,
                workspace=workspace,
                include_candidates=include_candidates,
            )
            lessons.sort(
                key=lambda item: (
                    int(bool(item.get("verified"))),
                    str(item.get("last_seen") or ""),
                    int(item.get("id") or 0),
                ),
                reverse=True,
            )
        return lessons[:limit]

    def add_lesson(
        self,
        *,
        trigger_tool: str,
        error_pattern: str,
        resolution: str,
        category: str = "command",
        verified: bool = False,
        workspace: str | Path | None = None,
        confidence: float = 0.9,
        source: str = "manual",
    ) -> int:
        trigger_tool = str(trigger_tool or "").strip()
        error_pattern = str(error_pattern or "").strip()
        resolution = str(resolution or "").strip()
        category = str(category or "").strip() or "command"
        for name, value in (
            ("trigger_tool", trigger_tool),
            ("error_pattern", error_pattern),
            ("resolution", resolution),
        ):
            if not value:
                raise ValueError(f"{name} must not be empty")
        confidence = max(0.0, min(1.0, float(confidence)))

        workspace_str = str(workspace or self.workspace or "")
        project = project_fingerprint(workspace_str) if workspace_str else self.project
        scope = "workspace" if workspace_str else "global"
        fingerprint = self._manual_fingerprint(
            project, category, trigger_tool, error_pattern
        )
        now = utc_iso()
        with self._connect() as conn:
            lesson_id = self._next_lesson_id(conn)
            conn.execute(
                "INSERT INTO lessons "
                "(id, fingerprint, conversation_id, project, workspace, scope, category, "
                "trigger_tool, error_pattern, resolution, occurrences, first_seen, "
                "last_seen, confidence, verified, source) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "resolution = excluded.resolution, "
                "confidence = MAX(lessons.confidence, excluded.confidence), "
                "verified = MAX(lessons.verified, excluded.verified), "
                "last_seen = excluded.last_seen",
                (
                    lesson_id,
                    fingerprint,
                    project,
                    workspace_str or None,
                    scope,
                    category,
                    trigger_tool,
                    error_pattern,
                    resolution,
                    now,
                    now,
                    confidence,
                    1 if verified else 0,
                    str(source or "manual"),
                ),
            )
            row = conn.execute(
                "SELECT id FROM lessons WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        self.sync_experience_md()
        return int(row["id"])

    def update_lesson(
        self,
        lesson_id: int,
        workspace: str | Path | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "trigger_tool",
            "error_pattern",
            "resolution",
            "category",
            "verified",
            "confidence",
            "scope",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(
                "Unknown lesson field(s): " + ", ".join(sorted(unknown))
            )
        text_fields = ("trigger_tool", "error_pattern", "resolution", "category", "scope")
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key in text_fields:
                text = str(value or "").strip()
                if not text:
                    raise ValueError(f"{key} must not be empty")
                updates[key] = text
            elif key == "verified":
                updates[key] = 1 if value else 0
            elif key == "confidence":
                try:
                    confidence = float(value)
                except (TypeError, ValueError):
                    raise ValueError("confidence must be a number between 0 and 1")
                updates[key] = max(0.0, min(1.0, confidence))

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lessons WHERE id = ?", (int(lesson_id),)
            ).fetchone()
            if not row:
                return None
            if workspace is not None and not self._lesson_in_scope(row, workspace):
                return None
            current = dict(row)
            old_fingerprint = str(current.get("fingerprint") or "")
            if any(
                key in updates for key in ("trigger_tool", "error_pattern", "category")
            ):
                new_fingerprint = self._manual_fingerprint(
                    str(current.get("project") or "global"),
                    str(updates.get("category", current.get("category") or "command")),
                    str(updates.get("trigger_tool", current.get("trigger_tool") or "unknown")),
                    str(updates.get("error_pattern", current.get("error_pattern") or "")),
                )
                if new_fingerprint != str(current.get("fingerprint") or ""):
                    collision = conn.execute(
                        "SELECT id FROM lessons WHERE fingerprint = ? AND id <> ?",
                        (new_fingerprint, int(lesson_id)),
                    ).fetchone()
                    if collision:
                        raise ValueError(
                            f"Lesson #{int(lesson_id)} would collide with "
                            f"lesson #{int(collision['id'])}: the same trigger, "
                            "category, and error pattern already exist."
                        )
                    updates["fingerprint"] = new_fingerprint
            updates["last_seen"] = utc_iso()
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE lessons SET {assignments} WHERE id = ?",
                (*updates.values(), int(lesson_id)),
            )
            new_fingerprint = str(updates.get("fingerprint") or old_fingerprint)
            if new_fingerprint != old_fingerprint:
                # Keep conversation links attached to the same lesson identity.
                conn.execute(
                    "UPDATE OR IGNORE session_lessons SET fingerprint = ? "
                    "WHERE fingerprint = ?",
                    (new_fingerprint, old_fingerprint),
                )
                conn.execute(
                    "DELETE FROM session_lessons WHERE fingerprint = ?",
                    (old_fingerprint,),
                )
            updated_row = conn.execute(
                "SELECT * FROM lessons WHERE id = ?", (int(lesson_id),)
            ).fetchone()
        self.sync_experience_md()
        return self._lesson_dict(updated_row) if updated_row else None

    def delete_lesson(
        self,
        lesson_id: int,
        workspace: str | Path | None = None,
    ) -> bool:
        deleted = False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lessons WHERE id = ?", (int(lesson_id),)
            ).fetchone()
            if not row:
                return False
            if workspace is not None and not self._lesson_in_scope(row, workspace):
                return False
            fingerprint = str(row["fingerprint"])
            cursor = conn.execute(
                "DELETE FROM lessons WHERE id = ?", (int(lesson_id),)
            )
            deleted = bool(cursor.rowcount)
            if deleted:
                try:
                    conn.execute(
                        "DELETE FROM session_lessons WHERE fingerprint = ?",
                        (fingerprint,),
                    )
                except sqlite3.DatabaseError:
                    pass
        if deleted:
            self.sync_experience_md()
        return deleted

    # ------------------------------------------------------------------- skills
    def record_skill_saved(
        self,
        name: str,
        description: str,
        file_path: str,
        workspace: str | Path | None = None,
        scope: str | None = None,
        quality: float = 0.0,
        source: str = "harness",
    ) -> str:
        """Index a synthesized skill. Returns ``created`` or ``updated``.

        Saving a skill is not reuse. ``record_skill_reuse`` owns that counter.
        """
        workspace_str = str(workspace or self.workspace or "")
        project = project_fingerprint(workspace_str) if workspace_str else "global"
        resolved_scope = scope or ("workspace" if workspace_str else "plugin")
        now = utc_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM skills_index WHERE name = ? AND scope = ? AND project = ?",
                (name, resolved_scope, project),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE skills_index SET description = ?, file_path = ?, updated_at = ?, "
                    "update_count = update_count + 1, quality = MAX(quality, ?), source = ? "
                    "WHERE name = ? AND scope = ? AND project = ?",
                    (
                        description,
                        file_path,
                        now,
                        float(quality),
                        source,
                        name,
                        resolved_scope,
                        project,
                    ),
                )
                status = "updated"
            else:
                conn.execute(
                    "INSERT INTO skills_index "
                    "(name, scope, project, description, file_path, created_at, updated_at, "
                    "reused_count, update_count, quality, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
                    (
                        name,
                        resolved_scope,
                        project,
                        description,
                        file_path,
                        now,
                        now,
                        float(quality),
                        source,
                    ),
                )
                status = "created"
        self.sync_experience_md()
        return status

    def record_skill_reuse(
        self,
        name: str,
        workspace: str | Path | None = None,
        scope: str | None = None,
    ) -> bool:
        """Increment the reuse counter for the most specific matching skill."""
        workspace_str = str(workspace or self.workspace or "")
        project = project_fingerprint(workspace_str) if workspace_str else "global"
        now = utc_iso()
        with self._connect() as conn:
            candidates = conn.execute(
                "SELECT name, scope, project FROM skills_index WHERE name = ?", (name,)
            ).fetchall()
            eligible = [
                candidate
                for candidate in candidates
                if candidate["project"] in (project, "global")
            ]
            if not eligible:
                return False
            scope_rank = {"workspace": 0, "user": 1, "plugin": 2}
            chosen = min(
                eligible,
                key=lambda candidate: (
                    0 if scope and candidate["scope"] == scope else 1,
                    0 if candidate["project"] == project else 1,
                    scope_rank.get(str(candidate["scope"]), 3),
                ),
            )
            conn.execute(
                "UPDATE skills_index SET reused_count = reused_count + 1, last_used_at = ? "
                "WHERE name = ? AND scope = ? AND project = ?",
                (now, name, chosen["scope"], chosen["project"]),
            )
        return True

    @staticmethod
    def _pick_skill_candidate(
        candidates: Sequence[sqlite3.Row],
        scope: str | None,
        project: str | None,
        project_id: str,
    ) -> sqlite3.Row | None:
        """Pick the most specific indexed skill for a name."""
        if not candidates:
            return None
        eligible = list(candidates)
        if project:
            exact_project = [
                row for row in eligible if str(row["project"]) == project
            ]
            if exact_project:
                eligible = exact_project
        else:
            scoped_project = [
                row
                for row in eligible
                if str(row["project"]) in (project_id, "global")
            ]
            if scoped_project:
                eligible = scoped_project
        if scope:
            exact_scope = [row for row in eligible if str(row["scope"]) == scope]
            if exact_scope:
                eligible = exact_scope
        return min(
            eligible,
            key=lambda row: (
                0 if str(row["project"]) == project_id else 1,
                _SKILL_SCOPE_RANK.get(str(row["scope"]), 4),
                str(row["project"]),
            ),
        )

    def record_skills_surfaced(
        self, skills: Sequence[dict[str, Any] | tuple | str]
    ) -> None:
        """Record that skills were included in an injected briefing.

        Accepts skill dicts (``name``/``scope``), ``(name, scope)`` tuples, or
        plain names. Plain names resolve to the most specific indexed copy.
        """
        resolved: list[tuple[str, str | None, str | None]] = []
        seen: set[tuple[str, str | None, str | None]] = set()
        for item in skills or ():
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                scope = str(item.get("scope") or "").strip() or None
                project = str(item.get("project") or "").strip() or None
            elif isinstance(item, (tuple, list)):
                name = str(item[0] or "").strip() if item else ""
                scope = (
                    str(item[1] or "").strip()
                    if len(item) > 1 and item[1]
                    else None
                )
                project = (
                    str(item[2] or "").strip()
                    if len(item) > 2 and item[2]
                    else None
                )
            else:
                name = str(item or "").strip()
                scope = None
                project = None
            key = (name, scope, project)
            if not name or key in seen:
                continue
            seen.add(key)
            resolved.append(key)
        if not resolved:
            return

        workspace_str = str(self.workspace or "")
        project_id = (
            project_fingerprint(workspace_str) if workspace_str else "global"
        )
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        try:
            with self._connect() as conn:
                for name, scope, project in resolved:
                    candidates = conn.execute(
                        "SELECT name, scope, project FROM skills_index WHERE name = ?",
                        (name,),
                    ).fetchall()
                    chosen = self._pick_skill_candidate(
                        candidates, scope, project, project_id
                    )
                    if not chosen:
                        continue
                    conn.execute(
                        "UPDATE skills_index SET surfaced_count = surfaced_count + 1, "
                        "last_surfaced_at = ? "
                        "WHERE name = ? AND scope = ? AND project = ?",
                        (
                            now,
                            name,
                            chosen["scope"],
                            chosen["project"],
                        ),
                    )
        except (sqlite3.DatabaseError, OSError):
            return

    def get_all_skills(
        self,
        workspace: str | Path | None = None,
        include_global: bool = True,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._scope_filter(workspace, None, include_global)
        if name:
            where.append("name = ?")
            params.append(name)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM skills_index {clause} "
                "ORDER BY updated_at DESC, reused_count DESC",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_skill_record(
        self,
        name: str,
        scope: str | None = None,
        project: str | None = None,
        file_path: str | None = None,
    ) -> int:
        where = ["name = ?"]
        params: list[Any] = [name]
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if project:
            where.append("project = ?")
            params.append(project)
        if file_path:
            where.append("file_path = ?")
            params.append(str(file_path))
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM skills_index WHERE {' AND '.join(where)}", tuple(params)
            )
            return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------- rules
    def record_rule_edit(self, target_file: str, change_summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rule_edits (timestamp, target_file, change_summary) VALUES (?, ?, ?)",
                (utc_iso(), str(target_file), str(change_summary)),
            )

    def get_recent_rule_edits(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_edits ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------- rule provenance
    def record_rule_application(
        self,
        rules: Sequence[dict],
        target_file: str,
        scope: str,
        conversation_id: str = "",
        evidence: str = "",
    ) -> int:
        """Record the rules the manager actually wrote to a rules file.

        ``rules`` items are ``{"text", "section", "new"}``. A rule seen for the
        first time starts at one occurrence; a rule already present before the
        update increments its occurrence counter. Returns the number of rules
        recorded.
        """
        target = str(target_file or "").strip() or "AGENTS.md"
        resolved_scope = str(scope or "").strip().lower()
        if resolved_scope not in ("workspace", "plugin"):
            resolved_scope = "workspace" if self.workspace else "plugin"
        project = self.project if resolved_scope == "workspace" else "global"
        evidence_text = one_line(str(evidence or ""), 1000)
        conversation = str(conversation_id or "").strip() or None
        now = utc_iso()
        applied = 0
        with self._connect() as conn:
            for rule in rules or ():
                if not isinstance(rule, dict):
                    continue
                text = str(rule.get("text") or "").strip()
                section = _normalize_rule_section(rule.get("section"))
                if not text or not section:
                    continue
                digest = rule_hash(text)
                is_new = bool(rule.get("new"))
                if is_new:
                    conn.execute(
                        "INSERT INTO rules_index "
                        "(rule_hash, text, section, target_file, scope, project, "
                        "conversation_id, evidence, occurrences, created_at, last_seen_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                        "ON CONFLICT(rule_hash) DO UPDATE SET "
                        "text = excluded.text, section = excluded.section, "
                        "target_file = excluded.target_file, scope = excluded.scope, "
                        "project = excluded.project, "
                        "conversation_id = COALESCE(excluded.conversation_id, rules_index.conversation_id), "
                        "evidence = CASE WHEN excluded.evidence <> '' "
                        "THEN excluded.evidence ELSE rules_index.evidence END, "
                        "occurrences = 1, last_seen_at = excluded.last_seen_at",
                        (
                            digest,
                            text,
                            section,
                            target,
                            resolved_scope,
                            project,
                            conversation,
                            evidence_text,
                            now,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO rules_index "
                        "(rule_hash, text, section, target_file, scope, project, "
                        "conversation_id, evidence, occurrences, created_at, last_seen_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                        "ON CONFLICT(rule_hash) DO UPDATE SET "
                        "text = excluded.text, section = excluded.section, "
                        "target_file = excluded.target_file, scope = excluded.scope, "
                        "project = excluded.project, "
                        "conversation_id = COALESCE(excluded.conversation_id, rules_index.conversation_id), "
                        "evidence = CASE WHEN excluded.evidence <> '' "
                        "THEN excluded.evidence ELSE rules_index.evidence END, "
                        "occurrences = rules_index.occurrences + 1, "
                        "last_seen_at = excluded.last_seen_at",
                        (
                            digest,
                            text,
                            section,
                            target,
                            resolved_scope,
                            project,
                            conversation,
                            evidence_text,
                            now,
                            now,
                        ),
                    )
                applied += 1
        return applied

    def record_rule_surfaced(self, rule_hashes: Sequence[str]) -> None:
        """Increment how often rules were included in an injected briefing."""
        hashes = sorted({
            str(value).strip()
            for value in rule_hashes or ()
            if str(value).strip()
        })
        if not hashes:
            return
        now = utc_iso()
        with self._connect() as conn:
            for digest in hashes:
                conn.execute(
                    "UPDATE rules_index SET surfaced_count = surfaced_count + 1, "
                    "last_surfaced_at = ? WHERE rule_hash = ?",
                    (now, digest),
                )

    def get_rule(
        self,
        text: str,
        target_file: str | None = None,
    ) -> dict[str, Any] | None:
        digest = rule_hash(text)
        query = "SELECT * FROM rules_index WHERE rule_hash = ?"
        params: list[Any] = [digest]
        if target_file:
            query += " AND target_file = ?"
            params.append(str(target_file))
        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return dict(row) if row else None

    def list_rules(
        self,
        target_file: str | None = None,
        scope: str | None = None,
        project: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if target_file:
            where.append("target_file = ?")
            params.append(str(target_file))
        if scope:
            where.append("scope = ?")
            params.append(str(scope))
        if project:
            where.append("project = ?")
            params.append(str(project))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM rules_index {clause} "
                "ORDER BY occurrences DESC, last_seen_at DESC, text ASC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def forget_rule(self, text: str, target_file: str | None = None) -> bool:
        query = "DELETE FROM rules_index WHERE rule_hash = ?"
        params: list[Any] = [rule_hash(text)]
        if target_file:
            query += " AND target_file = ?"
            params.append(str(target_file))
        with self._connect() as conn:
            cursor = conn.execute(query, tuple(params))
            return bool(cursor.rowcount)

    def record_rule_outcome(self, text: str, helpful: bool) -> None:
        column = "helpful_count" if helpful else "harmful_count"
        with self._connect() as conn:
            conn.execute(
                f"UPDATE rules_index SET {column} = {column} + 1 WHERE rule_hash = ?",
                (rule_hash(text),),
            )

    def get_rule_priority(self, text: str) -> float:
        """Higher is more valuable and should survive budget pressure."""
        rule = self.get_rule(text)
        if not rule:
            return 0.0
        occurrences = _as_int(rule.get("occurrences"), 1)
        helpful = _as_int(rule.get("helpful_count"), 0)
        harmful = _as_int(rule.get("harmful_count"), 0)
        surfaced = _as_int(rule.get("surfaced_count"), 0)
        score = float(occurrences) + 2.0 * float(helpful - harmful) - 0.15 * float(surfaced)
        last_seen = str(rule.get("last_seen_at") or "")
        if last_seen:
            try:
                seen = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                age_days = max(
                    0.0,
                    (datetime.now(timezone.utc) - seen).total_seconds() / 86400.0,
                )
                # Recency bonus: +3 for a rule seen now, fading to zero after 90 days.
                score += max(0.0, 3.0 - age_days / 30.0)
            except (TypeError, ValueError):
                pass
        return round(score, 6)

    # -------------------------------------------------------------------- stats
    def get_stats(
        self,
        workspace: str | Path | None = None,
        all_projects: bool = False,
    ) -> dict[str, Any]:
        stats = self._stats_local(workspace, all_projects)
        if all_projects and not self.read_only:
            for peer in self._sibling_stores():
                try:
                    extra = peer._stats_local(None, True, exclude_global_lessons=True)
                except (sqlite3.DatabaseError, OSError):
                    continue
                for key in (
                    "sessions",
                    "verified_lessons",
                    "candidate_lessons",
                    "skills",
                    "rule_edits",
                ):
                    stats[key] += int(extra.get(key) or 0)
                stats["last_lesson_at"] = max(
                    str(stats.get("last_lesson_at") or ""),
                    str(extra.get("last_lesson_at") or ""),
                )
                stats["project_stores"] = int(stats.get("project_stores") or 0) + 1
        return stats

    def _stats_local(
        self,
        workspace: str | Path | None,
        all_projects: bool,
        exclude_global_lessons: bool = False,
    ) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "db_path": str(self.db_path),
                "experience_path": str(self.exp_md_path),
                "sessions": 0,
                "verified_lessons": 0,
                "candidate_lessons": 0,
                "skills": 0,
                "rule_edits": 0,
                "last_lesson_at": "",
                "fts": False,
            }
        where, params = self._scope_filter(
            workspace, None, True, all_projects=all_projects
        )
        lesson_where = list(where)
        if exclude_global_lessons:
            lesson_where.append("project != 'global'")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        lesson_clause = (
            f"WHERE {' AND '.join(lesson_where)}" if lesson_where else ""
        )
        verified_clause = (
            f"{lesson_clause} AND verified = 1" if lesson_clause else "WHERE verified = 1"
        )
        candidate_clause = (
            f"{lesson_clause} AND verified = 0" if lesson_clause else "WHERE verified = 0"
        )
        with self._connect() as conn:
            sessions = conn.execute(
                f"SELECT COUNT(*) FROM sessions {clause}", tuple(params)
            ).fetchone()[0]
            verified = conn.execute(
                f"SELECT COUNT(*) FROM lessons {verified_clause}", tuple(params)
            ).fetchone()[0]
            candidates = conn.execute(
                f"SELECT COUNT(*) FROM lessons {candidate_clause}", tuple(params)
            ).fetchone()[0]
            skills = conn.execute(
                f"SELECT COUNT(*) FROM skills_index {clause}", tuple(params)
            ).fetchone()[0]
            rule_edits = conn.execute("SELECT COUNT(*) FROM rule_edits").fetchone()[0]
            last = conn.execute(
                f"SELECT MAX(last_seen) FROM lessons {lesson_clause}", tuple(params)
            ).fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "experience_path": str(self.exp_md_path),
            "sessions": int(sessions),
            "verified_lessons": int(verified),
            "candidate_lessons": int(candidates),
            "skills": int(skills),
            "rule_edits": int(rule_edits),
            "last_lesson_at": last or "",
            "fts": bool(self.fts_enabled),
        }

    def _scope_filter(
        self,
        workspace: str | Path | None,
        project: str | None,
        include_global: bool,
        all_projects: bool = False,
    ) -> tuple[list[str], list[Any]]:
        workspace_str = str(workspace or self.workspace or "")
        if not workspace_str and not project:
            if all_projects:
                # Explicit all-project operations: global status and export.
                return [], []
            # Without a workspace, only global knowledge is visible. This keeps
            # project lessons from leaking into another project's session.
            return ["project = 'global'"], []
        project_id = project or (project_fingerprint(workspace_str) if workspace_str else self.project)
        if project_id == "global":
            return ["project = 'global'"], []
        if include_global:
            return ["(project = ? OR project = 'global')"], [project_id]
        return ["project = ?"], [project_id]

    @staticmethod
    def _lesson_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["verified"] = bool(data.get("verified"))
        if data.get("workspace"):
            data["workspace_name"] = Path(str(data["workspace"])).name
        else:
            data["workspace_name"] = "global"
        data["utility"] = lesson_utility(data)
        return data

    # ------------------------------------------------------------- experience md
    def sync_experience_md(self) -> None:
        with self._connect() as conn:
            sessions = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 15"
            ).fetchall()
            verified = conn.execute(
                "SELECT * FROM lessons WHERE verified = 1 "
                "ORDER BY occurrences DESC, confidence DESC, last_seen DESC LIMIT 25"
            ).fetchall()
            candidates = conn.execute(
                "SELECT * FROM lessons WHERE verified = 0 "
                "ORDER BY last_seen DESC LIMIT 10"
            ).fetchall()
            skills = conn.execute(
                "SELECT * FROM skills_index ORDER BY updated_at DESC, reused_count DESC LIMIT 25"
            ).fetchall()
            counts = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "verified": conn.execute("SELECT COUNT(*) FROM lessons WHERE verified = 1").fetchone()[0],
                "candidates": conn.execute("SELECT COUNT(*) FROM lessons WHERE verified = 0").fetchone()[0],
                "skills": conn.execute("SELECT COUNT(*) FROM skills_index").fetchone()[0],
            }

        def cell(value: Any, limit: int = 160) -> str:
            return one_line(str(value or ""), limit).replace("|", "\\|")

        lines = [
            "# Harness² Experience Library",
            "",
            "> Persistent lessons and reusable procedures learned by the Harness² plugin.",
            f"> Last synced: {utc_iso()} | Sessions: {counts['sessions']} | "
            f"Verified lessons: {counts['verified']} | Candidates: {counts['candidates']} | "
            f"Skills: {counts['skills']}",
            "",
            "This file is generated from `harness_memory.db`. Edit memory through the CLI,",
            "not this file; the next sync will overwrite it:",
            "  harness memory list",
            "  harness memory show <id>",
            '  harness memory edit <id> --resolution "..."',
            "  harness memory add --trigger-tool ... --error-pattern ... --resolution ...",
            "  harness memory delete <id>",
            "",
            "## Verified Lessons",
            "",
            "A verified lesson is a failure followed by an observed successful recovery.",
            "",
            "| Tool | Category | Project | Seen | Resolution |",
            "| :--- | :--- | :--- | ---: | :--- |",
        ]

        if not verified:
            lines.append("| - | - | - | - | No verified lessons yet. |")
        else:
            for row in verified:
                lines.append(
                    f"| `{cell(row['trigger_tool'], 40)}` | {cell(row['category'], 30)} | "
                    f"{cell(_project_label(row), 40)} | "
                    f"{row['occurrences']} | {cell(row['resolution'], 220)} |"
                )

        lines.extend([
            "",
            "## Candidate Observations",
            "",
            "Unresolved failures. These are useful context but are never promoted to rules.",
            "",
        ])
        if not candidates:
            lines.append("- None recorded.")
        else:
            for row in candidates[:8]:
                label = _project_label(row)
                lines.append(
                    f"- [{label}] `{cell(row['trigger_tool'], 40)}`: "
                    f"{cell(row['error_pattern'], 180)}"
                )

        lines.extend([
            "",
            "## Skills",
            "",
            "| Skill | Scope | Reuses | Quality | Updated | Description |",
            "| :--- | :--- | ---: | ---: | :--- | :--- |",
        ])
        if not skills:
            lines.append("| - | - | - | - | - | No skills indexed yet. |")
        else:
            for row in skills:
                lines.append(
                    f"| `{cell(row['name'], 60)}` | {cell(row['scope'], 12)} | "
                    f"{row['reused_count']} | {float(row['quality']):.2f} | "
                    f"{cell(str(row['updated_at'])[:10], 12)} | {cell(row['description'], 180)} |"
                )

        lines.extend(["", "## Recent Sessions", ""])
        if not sessions:
            lines.append("- None recorded.")
        else:
            for row in sessions:
                label = _project_label(row)
                lines.append(
                    f"- {cell(str(row['updated_at'])[:19], 20)} | {cell(label, 40)} | "
                    f"{cell(row['outcome'], 16)} | {cell(row['task_summary'], 180)}"
                )

        lines.extend([
            "",
            "## How Memory Is Used",
            "",
            "The `SessionStart` hook searches this store for lessons relevant to the",
            "current workspace and injects a short briefing. `Stop` analyses the finished",
            "transcript and records new lessons and skill re-use. A lesson only becomes a",
            "rule after a verified recovery, and generated rules stay inside the managed",
            "block while the 20 KB per-file budget is respected.",
            "",
        ])
        atomic_write_text(self.exp_md_path, "\n".join(lines))

    # ---------------------------------------------------------------- export/import
    def export_data(self, path: Path | str, workspace: str | Path | None = None) -> Path:
        destination = Path(path).expanduser()
        scope_workspace = str(workspace or self.workspace or "")
        where, params = self._scope_filter(
            workspace, None, True, all_projects=not scope_workspace
        )
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            sessions = [dict(r) for r in conn.execute(f"SELECT * FROM sessions {clause}", tuple(params))]
            lessons = [dict(r) for r in conn.execute(f"SELECT * FROM lessons {clause}", tuple(params))]
            skills = [dict(r) for r in conn.execute(f"SELECT * FROM skills_index {clause}", tuple(params))]
            conversation_ids = [
                str(session["conversation_id"])
                for session in sessions
                if session.get("conversation_id")
            ]
            links: list[dict[str, Any]] = []
            if conversation_ids:
                placeholders = ", ".join("?" for _ in conversation_ids)
                links = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM session_lessons "
                        f"WHERE conversation_id IN ({placeholders})",
                        tuple(conversation_ids),
                    )
                ]
            payload = {
                "version": SCHEMA_VERSION,
                "exported_at": utc_iso(),
                "sessions": sessions,
                "lessons": lessons,
                "skills": skills,
                "session_lessons": links,
                "rule_edits": [dict(r) for r in conn.execute("SELECT * FROM rule_edits").fetchall()],
            }
        write_json(destination, payload)
        return destination

    def import_data(self, path: Path | str, merge: bool = True) -> dict[str, int]:
        payload = read_json(Path(path), default=None)
        if not isinstance(payload, dict) or "lessons" not in payload:
            raise ValueError("Not a Harness memory export file")
        imported = {"sessions": 0, "lessons": 0, "skills": 0, "rule_edits": 0, "links": 0}
        with self._connect() as conn:
            for row in payload.get("sessions", []):
                if not isinstance(row, dict) or not row.get("conversation_id"):
                    continue
                if merge:
                    existing = conn.execute(
                        "SELECT updated_at FROM sessions WHERE conversation_id = ?",
                        (row["conversation_id"],),
                    ).fetchone()
                    if existing and str(existing["updated_at"]) >= str(row.get("updated_at") or ""):
                        continue
                cursor = conn.execute(
                    "INSERT OR REPLACE INTO sessions "
                    "(conversation_id, timestamp, updated_at, workspace, project, task_summary, "
                    "outcome, tools_used_count, errors_count, lessons_count, transcript_hash, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.get("conversation_id"),
                        row.get("timestamp") or utc_iso(),
                        row.get("updated_at") or utc_iso(),
                        row.get("workspace"),
                        row.get("project") or "global",
                        row.get("task_summary") or "",
                        row.get("outcome") or "completed",
                        _as_int(row.get("tools_used_count"), 0),
                        _as_int(row.get("errors_count"), 0),
                        _as_int(row.get("lessons_count"), 0),
                        row.get("transcript_hash") or "",
                        row.get("metadata") or "{}",
                    ),
                )
                imported["sessions"] += int(cursor.rowcount or 0)

            for row in payload.get("lessons", []):
                if not isinstance(row, dict) or not row.get("fingerprint"):
                    continue
                fingerprint = str(row.get("fingerprint"))
                imported_verified = int(bool(row.get("verified")))
                imported_confidence = _as_float(row.get("confidence"), 0.5)
                imported_resolution = str(row.get("resolution") or "")
                imported_occurrences = max(1, _as_int(row.get("occurrences"), 1))
                imported_last_seen = str(row.get("last_seen") or utc_iso())
                imported_surfaced = max(0, _as_int(row.get("surfaced_count"), 0))
                imported_post_surface = max(
                    0, _as_int(row.get("post_surface_occurrences"), 0)
                )
                imported_last_surfaced = str(row.get("last_surfaced_at") or "")
                existing = conn.execute(
                    "SELECT * FROM lessons WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                if existing and merge:
                    existing_verified = int(existing["verified"] or 0)
                    resolution = str(existing["resolution"] or "")
                    if imported_verified and (
                        not existing_verified
                        or not resolution
                        or len(imported_resolution) >= len(resolution)
                    ):
                        resolution = imported_resolution
                    values = {
                        "resolution": resolution,
                        "confidence": max(
                            _as_float(existing["confidence"], 0.0),
                            imported_confidence,
                        ),
                        "verified": max(existing_verified, imported_verified),
                        "occurrences": max(
                            _as_int(existing["occurrences"], 1), imported_occurrences
                        ),
                        "last_seen": max(
                            str(existing["last_seen"] or ""), imported_last_seen
                        ),
                        "surfaced_count": max(
                            _as_int(existing["surfaced_count"], 0), imported_surfaced
                        ),
                        "post_surface_occurrences": max(
                            _as_int(existing["post_surface_occurrences"], 0),
                            imported_post_surface,
                        ),
                        "last_surfaced_at": max(
                            str(existing["last_surfaced_at"] or ""),
                            imported_last_surfaced,
                        ) or None,
                    }
                    unchanged = (
                        values["resolution"] == str(existing["resolution"] or "")
                        and values["confidence"] == _as_float(existing["confidence"], 0.0)
                        and values["verified"] == existing_verified
                        and values["occurrences"] == _as_int(existing["occurrences"], 1)
                        and values["last_seen"] == str(existing["last_seen"] or "")
                        and values["surfaced_count"]
                        == _as_int(existing["surfaced_count"], 0)
                        and values["post_surface_occurrences"]
                        == _as_int(existing["post_surface_occurrences"], 0)
                        and values["last_surfaced_at"]
                        == (str(existing["last_surfaced_at"] or "") or None)
                    )
                    if unchanged:
                        continue
                    conn.execute(
                        "UPDATE lessons SET resolution = ?, confidence = ?, verified = ?, "
                        "occurrences = ?, last_seen = ?, surfaced_count = ?, "
                        "post_surface_occurrences = ?, last_surfaced_at = ? "
                        "WHERE fingerprint = ?",
                        (
                            values["resolution"],
                            values["confidence"],
                            values["verified"],
                            values["occurrences"],
                            values["last_seen"],
                            values["surfaced_count"],
                            values["post_surface_occurrences"],
                            values["last_surfaced_at"],
                            fingerprint,
                        ),
                    )
                    imported["lessons"] += 1
                    continue

                values = (
                    self._next_lesson_id(conn),
                    fingerprint,
                    row.get("conversation_id"),
                    row.get("project") or "global",
                    row.get("workspace"),
                    row.get("scope") or "global",
                    row.get("category") or "command",
                    row.get("trigger_tool") or "unknown",
                    row.get("error_pattern") or "",
                    imported_resolution,
                    imported_occurrences,
                    row.get("first_seen") or utc_iso(),
                    imported_last_seen,
                    imported_confidence,
                    imported_verified,
                    row.get("source") or "import",
                    imported_surfaced,
                    imported_last_surfaced or None,
                    imported_post_surface,
                )
                statement = (
                    "INSERT INTO lessons "
                    "(id, fingerprint, conversation_id, project, workspace, scope, category, "
                    "trigger_tool, error_pattern, resolution, occurrences, first_seen, last_seen, "
                    "confidence, verified, source, surfaced_count, last_surfaced_at, "
                    "post_surface_occurrences) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(fingerprint) DO UPDATE SET "
                    "conversation_id=excluded.conversation_id, project=excluded.project, "
                    "workspace=excluded.workspace, scope=excluded.scope, "
                    "category=excluded.category, trigger_tool=excluded.trigger_tool, "
                    "error_pattern=excluded.error_pattern, resolution=excluded.resolution, "
                    "occurrences=excluded.occurrences, first_seen=excluded.first_seen, "
                    "last_seen=excluded.last_seen, confidence=excluded.confidence, "
                    "verified=excluded.verified, source=excluded.source, "
                    "surfaced_count=excluded.surfaced_count, "
                    "last_surfaced_at=excluded.last_surfaced_at, "
                    "post_surface_occurrences=excluded.post_surface_occurrences"
                )
                try:
                    cursor = conn.execute(statement, values)
                    imported["lessons"] += int(cursor.rowcount or 0)
                except sqlite3.IntegrityError:
                    continue

            for row in payload.get("skills", []):
                if not isinstance(row, dict) or not row.get("name") or not row.get("file_path"):
                    continue
                values = (
                    row.get("name"),
                    row.get("scope") or "global",
                    row.get("project") or "global",
                    row.get("description") or "",
                    row.get("file_path"),
                    row.get("created_at") or utc_iso(),
                    row.get("updated_at") or utc_iso(),
                    row.get("last_used_at"),
                    _as_int(row.get("reused_count"), 0),
                    _as_int(row.get("update_count"), 0),
                    _as_float(row.get("quality"), 0.0),
                    row.get("source") or "import",
                    max(0, _as_int(row.get("surfaced_count"), 0)),
                    str(row.get("last_surfaced_at") or "") or None,
                )
                statement = (
                    "INSERT OR IGNORE INTO skills_index "
                    "(name, scope, project, description, file_path, created_at, updated_at, "
                    "last_used_at, reused_count, update_count, quality, source, "
                    "surfaced_count, last_surfaced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                )
                if not merge:
                    statement = (
                        "INSERT INTO skills_index "
                        "(name, scope, project, description, file_path, created_at, updated_at, "
                        "last_used_at, reused_count, update_count, quality, source, "
                        "surfaced_count, last_surfaced_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(name, scope, project) DO UPDATE SET "
                        "description=excluded.description, file_path=excluded.file_path, "
                        "created_at=excluded.created_at, updated_at=excluded.updated_at, "
                        "last_used_at=excluded.last_used_at, reused_count=excluded.reused_count, "
                        "update_count=excluded.update_count, quality=excluded.quality, "
                        "source=excluded.source, surfaced_count=excluded.surfaced_count, "
                        "last_surfaced_at=excluded.last_surfaced_at"
                    )
                cursor = conn.execute(statement, values)
                imported["skills"] += int(cursor.rowcount or 0)

            touched_conversations: set[str] = set()
            for row in payload.get("session_lessons", []):
                if not isinstance(row, dict):
                    continue
                conversation_id = row.get("conversation_id")
                fingerprint = row.get("fingerprint")
                if not conversation_id or not fingerprint:
                    continue
                if not conn.execute(
                    "SELECT 1 FROM sessions WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone():
                    continue
                if not conn.execute(
                    "SELECT 1 FROM lessons WHERE fingerprint = ?", (fingerprint,)
                ).fetchone():
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO session_lessons "
                    "(conversation_id, fingerprint, seen_at) VALUES (?, ?, ?)",
                    (
                        conversation_id,
                        fingerprint,
                        row.get("seen_at") or utc_iso(),
                    ),
                )
                imported["links"] += int(cursor.rowcount or 0)
                touched_conversations.add(str(conversation_id))
            for conversation_id in touched_conversations:
                conn.execute(
                    "UPDATE sessions SET lessons_count = "
                    "(SELECT COUNT(*) FROM session_lessons WHERE conversation_id = ?) "
                    "WHERE conversation_id = ?",
                    (conversation_id, conversation_id),
                )

            for row in payload.get("rule_edits", []):
                if not isinstance(row, dict):
                    continue
                timestamp = row.get("timestamp") or utc_iso()
                target_file = row.get("target_file") or "AGENTS.md"
                change_summary = row.get("change_summary") or "imported rule edit"
                cursor = conn.execute(
                    "INSERT INTO rule_edits (timestamp, target_file, change_summary) "
                    "SELECT ?, ?, ? WHERE NOT EXISTS ("
                    "SELECT 1 FROM rule_edits WHERE timestamp = ? AND target_file = ? "
                    "AND change_summary = ?)",
                    (
                        timestamp,
                        target_file,
                        change_summary,
                        timestamp,
                        target_file,
                        change_summary,
                    ),
                )
                imported["rule_edits"] += int(cursor.rowcount or 0)
        self.sync_experience_md()
        return imported

    # -------------------------------------------------------------------- pruning
    def prune(
        self,
        max_lessons: int = 2000,
        max_sessions: int = 1000,
        max_rule_edits: int = 500,
        days: int = 365,
        candidate_days: int = 30,
    ) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        candidate_cutoff = (now - timedelta(days=candidate_days)).isoformat(timespec="seconds")
        cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
        deleted = {"lessons": 0, "sessions": 0, "rule_edits": 0, "links": 0}
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM lessons WHERE verified = 0 AND last_seen < ?", (candidate_cutoff,)
            )
            deleted["lessons"] += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM lessons WHERE verified = 1 AND last_seen < ? AND occurrences <= 1",
                (cutoff,),
            )
            deleted["lessons"] += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM lessons WHERE id NOT IN ("
                "SELECT id FROM lessons ORDER BY verified DESC, occurrences DESC, last_seen DESC LIMIT ?"
                ")",
                (max(50, int(max_lessons)),),
            )
            deleted["lessons"] += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM sessions WHERE conversation_id NOT IN ("
                "SELECT conversation_id FROM sessions ORDER BY updated_at DESC LIMIT ?"
                ")",
                (max(50, int(max_sessions)),),
            )
            deleted["sessions"] += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM rule_edits WHERE id NOT IN ("
                "SELECT id FROM rule_edits ORDER BY id DESC LIMIT ?"
                ")",
                (max(50, int(max_rule_edits)),),
            )
            deleted["rule_edits"] += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM session_lessons WHERE fingerprint NOT IN (SELECT fingerprint FROM lessons)"
            )
            deleted["links"] += int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM session_lessons "
                "WHERE conversation_id NOT IN (SELECT conversation_id FROM sessions)"
            )
            deleted["links"] += int(cursor.rowcount or 0)
        if any(deleted.values()):
            self.sync_experience_md()
        return deleted

    def reset_workspace(self, workspace: str | Path) -> dict[str, int]:
        project = project_fingerprint(workspace)
        deleted = {"sessions": 0, "lessons": 0, "skills": 0}
        with self._connect() as conn:
            deleted["lessons"] = int(
                conn.execute("DELETE FROM lessons WHERE project = ?", (project,)).rowcount or 0
            )
            deleted["sessions"] = int(
                conn.execute("DELETE FROM sessions WHERE project = ?", (project,)).rowcount or 0
            )
            deleted["skills"] = int(
                conn.execute("DELETE FROM skills_index WHERE project = ?", (project,)).rowcount or 0
            )
            conn.execute("DELETE FROM session_lessons WHERE conversation_id NOT IN (SELECT conversation_id FROM sessions)")
            conn.execute("DELETE FROM session_lessons WHERE fingerprint NOT IN (SELECT fingerprint FROM lessons)")
        self.sync_experience_md()
        return deleted

    def backup(self, destination: Path | str) -> Path:
        """Copy a consistent database snapshot (including WAL) and the library."""
        target = Path(destination).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            snapshot_path = target / self.db_path.name
            for suffix in ("-wal", "-shm"):
                stale = snapshot_path.with_name(snapshot_path.name + suffix)
                if stale.exists():
                    stale.unlink(missing_ok=True)
            source = sqlite3.connect(str(self.db_path), timeout=10)
            snapshot = sqlite3.connect(str(snapshot_path), timeout=10)
            try:
                source.backup(snapshot)
            finally:
                snapshot.close()
                source.close()
        if self.exp_md_path.exists():
            shutil.copy2(self.exp_md_path, target / self.exp_md_path.name)
        return target


def _project_label(row: sqlite3.Row | dict[str, Any]) -> str:
    try:
        workspace = row["workspace"]
    except (KeyError, IndexError):
        workspace = None
    if workspace:
        return Path(str(workspace)).name or str(workspace)
    try:
        project = row["project"]
    except (KeyError, IndexError):
        project = "global"
    return "global" if not project or project == "global" else str(project)[:12]
