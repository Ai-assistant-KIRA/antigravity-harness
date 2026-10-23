"""
Persistent Memory Store and Experience Library for Harness².
Maintains an indexed SQLite database of cross-session memories, contrastive lessons,
and an automatically synced, human-readable EXPERIENCE.md log.
"""

from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator
from .config import get_memory_db_path, get_experience_md_path
from .analyzer import ContrastiveEvent, WorkflowCandidate, SessionAnalysis


class MemoryStore:
    """Manages persistent cross-session memory and the Harness Experience Library."""

    def __init__(self, db_path: Path | None = None, exp_md_path: Path | None = None):
        self.db_path = db_path or get_memory_db_path()
        self.exp_md_path = exp_md_path or get_experience_md_path()
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initializes tables for sessions, lessons, skills, and rule edits."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    conversation_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    task_summary TEXT,
                    tools_used_count INTEGER DEFAULT 0,
                    errors_count INTEGER DEFAULT 0,
                    outcome TEXT DEFAULT 'completed'
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    category TEXT NOT NULL,
                    trigger_tool TEXT NOT NULL,
                    error_pattern TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    occurrences INTEGER DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS skills_index (
                    name TEXT PRIMARY KEY,
                    description TEXT,
                    file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reused_count INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rule_edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    target_file TEXT NOT NULL,
                    change_summary TEXT NOT NULL
                )
            """)
            conn.commit()

    def record_session(self, analysis: SessionAnalysis) -> None:
        """Records a session analysis into the persistent store."""
        now_str = datetime.now(timezone.utc).isoformat()
        task_summary = "; ".join(analysis.tasks)[:500] if analysis.tasks else "General interactive session"

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO sessions
                (conversation_id, timestamp, task_summary, tools_used_count, errors_count, outcome)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                analysis.conversation_id or "unknown-session",
                now_str,
                task_summary,
                len(analysis.tool_calls),
                len(analysis.contrastive_events),
                analysis.final_outcome,
            ))

            # Record contrastive lessons
            for ev in analysis.contrastive_events:
                # Check for existing similar lesson
                cursor.execute("""
                    SELECT id, occurrences FROM lessons
                    WHERE trigger_tool = ? AND error_pattern = ?
                """, (ev.trigger_tool, ev.error_message[:150]))
                row = cursor.fetchone()

                if row:
                    lesson_id, count = row[0], row[1]
                    cursor.execute("""
                        UPDATE lessons
                        SET occurrences = ?, last_seen = ?, resolution = ?
                        WHERE id = ?
                    """, (count + 1, now_str, ev.resolution_summary, lesson_id))
                else:
                    cursor.execute("""
                        INSERT INTO lessons
                        (conversation_id, category, trigger_tool, error_pattern, resolution, occurrences, first_seen, last_seen)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                    """, (
                        analysis.conversation_id or "unknown",
                        ev.category,
                        ev.trigger_tool,
                        ev.error_message[:150],
                        ev.resolution_summary,
                        now_str,
                        now_str,
                    ))

            conn.commit()

        # Update human-readable EXPERIENCE.md
        self.sync_experience_md()

    def record_skill_saved(self, name: str, description: str, file_path: str) -> None:
        """Indexes an auto-synthesized skill."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT created_at, reused_count FROM skills_index WHERE name = ?
            """, (name,))
            row = cursor.fetchone()

            if row:
                created_at, count = row[0], row[1]
                cursor.execute("""
                    UPDATE skills_index
                    SET description = ?, file_path = ?, updated_at = ?, reused_count = ?
                    WHERE name = ?
                """, (description, file_path, now_str, count + 1, name))
            else:
                cursor.execute("""
                    INSERT INTO skills_index
                    (name, description, file_path, created_at, updated_at, reused_count)
                    VALUES (?, ?, ?, ?, ?, 0)
                """, (name, description, file_path, now_str, now_str))

            conn.commit()

        self.sync_experience_md()

    def record_rule_edit(self, target_file: str, change_summary: str) -> None:
        """Logs changes to AGENTS.md."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO rule_edits (timestamp, target_file, change_summary)
                VALUES (?, ?, ?)
            """, (now_str, target_file, change_summary))
            conn.commit()

    def get_recent_lessons(self, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieves most recent lessons."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT category, trigger_tool, error_pattern, resolution, occurrences, last_seen
                FROM lessons
                ORDER BY last_seen DESC LIMIT ?
            """, (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def get_all_skills(self) -> list[dict[str, Any]]:
        """Retrieves all indexed skills."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name, description, file_path, created_at, updated_at, reused_count
                FROM skills_index
                ORDER BY updated_at DESC
            """)
            return [dict(r) for r in cursor.fetchall()]

    def sync_experience_md(self) -> None:
        """Exports the SQLite database into a readable EXPERIENCE.md file."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sessions")
            session_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM lessons")
            lesson_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM skills_index")
            skill_count = cursor.fetchone()[0]

            cursor.execute("SELECT * FROM lessons ORDER BY occurrences DESC, last_seen DESC LIMIT 15")
            top_lessons = [dict(r) for r in cursor.fetchall()]

            cursor.execute("SELECT * FROM skills_index ORDER BY updated_at DESC")
            skills = [dict(r) for r in cursor.fetchall()]

        lines = [
            "# 🧠 Harness² Experience Library (`EXPERIENCE.md`)",
            "",
            "> *Persistent episodic memory and recursive learning repository.*",
            f"> **Last Synced**: {now_str} | **Sessions Tracked**: {session_count} | **Lessons**: {lesson_count} | **Skills**: {skill_count}",
            "",
            "---",
            "",
            "## ⚡ High-Frequency Contrastive Invariants (Top Learned Lessons)",
            "| Tool | Category | Occurrences | Resolution & Workaround |",
            "| :--- | :--- | :--- | :--- |",
        ]

        if not top_lessons:
            lines.append("| *None recorded yet* | - | - | Self-improvement cycle will populate as tasks execute. |")
        else:
            for l in top_lessons:
                tool = l["trigger_tool"]
                cat = l["category"]
                occ = l["occurrences"]
                res = l["resolution"].replace("|", "\\|")[:120]
                lines.append(f"| `{tool}` | {cat} | {occ} | {res} |")

        lines.extend([
            "",
            "## 📦 Autonomously Synthesized Skills Library",
            "| Skill Name | Purpose | Last Updated | Path |",
            "| :--- | :--- | :--- | :--- |",
        ])

        if not skills:
            lines.append("| *No skills built yet* | - | - | Skills are synthesized on task completion. |")
        else:
            for s in skills:
                name = s["name"]
                desc = s["description"][:80].replace("|", "\\|")
                updated = s["updated_at"][:10]
                fpath = s["file_path"].replace("\\", "/")
                lines.append(f"| **`{name}`** | {desc}... | {updated} | `{fpath}` |")

        lines.extend([
            "",
            "## 🔄 Loading in Next Sessions",
            "All skills above are automatically loaded by Antigravity's progressive disclosure.",
            "The `SessionStart` hook injects matching memory context at the beginning of each turn.",
        ])

        self.exp_md_path.parent.mkdir(parents=True, exist_ok=True)
        self.exp_md_path.write_text("\n".join(lines), encoding="utf-8")
