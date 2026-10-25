"""Incremental transcript reads for the Stop hook.

A long session appends to the same transcript on every turn. Re-parsing the
whole growing file on each Stop is O(n²) in aggregate, so this module keeps a
small per-conversation cursor and only analyses the new tail.

The cursor is deliberately cheap to validate: the first :data:`HEAD_BYTES` and
the last :data:`TAIL_BYTES` before the consumed offset are hashed. If the
transcript was rewritten, compacted, or truncated, either hash changes (or the
file is shorter than the offset) and the caller falls back to a full parse.

Raw steps from the tail of the previous read are kept as a bounded context.
That is what allows a failure at the end of one Stop to be paired with a
recovery at the start of the next Stop.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyzer import TranscriptAnalyzer
from .config import get_state_dir, safe_conversation_id
from .util import atomic_write_text, file_lock, utc_iso

CURSOR_VERSION = 1
CONTEXT_LIMIT_STEPS = 400
CONTEXT_LIMIT_BYTES = 2_000_000
HEAD_BYTES = 65536
TAIL_BYTES = 4096

# The pairing context is an internal cap, not an analysis failure. Dropping
# older context must not surface a warning on every Stop of a long session.
CONTEXT_TRUNCATION_WARNING = ""

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_CHUNK_BYTES = 64 * 1024


@dataclass
class TranscriptCursor:
    """Persisted progress through one conversation transcript."""

    transcript_path: str = ""
    offset: int = 0
    size: int = 0
    line_count: int = 0
    head_sha256: str = _EMPTY_SHA256
    tail_sha256: str = _EMPTY_SHA256
    context: list[dict[str, Any]] = field(default_factory=list)
    updated_at: str = ""
    version: int = CURSOR_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "TranscriptCursor | None":
        """Build a cursor from untrusted JSON, or return None when corrupt."""
        if not isinstance(value, dict):
            return None
        try:
            version = int(value.get("version", 0))
            if version != CURSOR_VERSION:
                return None
            transcript_path = value.get("transcript_path")
            if not isinstance(transcript_path, str) or not transcript_path.strip():
                return None
            offset = int(value.get("offset", -1))
            size = int(value.get("size", -1))
            line_count = int(value.get("line_count", -1))
            if offset < 0 or size < 0 or line_count < 0 or offset > size:
                return None
            head = value.get("head_sha256")
            tail = value.get("tail_sha256")
            if not isinstance(head, str) or not isinstance(tail, str):
                return None
            raw_context = value.get("context", [])
            if not isinstance(raw_context, list):
                return None
            context = [step for step in raw_context if isinstance(step, dict)]
            return cls(
                transcript_path=transcript_path,
                offset=offset,
                size=size,
                line_count=line_count,
                head_sha256=head,
                tail_sha256=tail,
                context=list(context),
                updated_at=str(value.get("updated_at") or ""),
                version=version,
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "transcript_path": self.transcript_path,
            "offset": self.offset,
            "size": self.size,
            "line_count": self.line_count,
            "head_sha256": self.head_sha256,
            "tail_sha256": self.tail_sha256,
            "context": self.context,
            "updated_at": self.updated_at,
        }


@dataclass
class IncrementalRead:
    """Result of one incremental (or full fallback) transcript read."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    new_steps: int = 0
    truncated: bool = False
    cursor: TranscriptCursor | None = None
    from_cursor: bool = False
    warning: str = ""
    analysis_truncated: bool = False
    context_truncated: bool = False
    state_path: Path | None = None

    def save(self) -> bool:
        """Persist the cursor; safe to call on every successful Stop."""
        return save_cursor(self)


# --------------------------------------------------------------------- cursor io
def _conversation_key(
    conversation_id: str | None,
    transcript_path: Path | str | None = None,
) -> str:
    key = safe_conversation_id(conversation_id)
    if key:
        return key
    # Tests and direct CLI calls sometimes omit the conversation id. Derive a
    # stable key from the transcript path so appends still become incremental.
    path_text = ""
    if transcript_path is not None:
        try:
            path_text = str(Path(transcript_path).expanduser().resolve())
        except OSError:
            path_text = str(transcript_path)
    digest = hashlib.sha256(path_text.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"transcript-{digest}"


def cursor_path(
    conversation_id: str | None,
    transcript_path: Path | str | None = None,
) -> Path:
    """Return the state file for a conversation cursor."""
    key = _conversation_key(conversation_id, transcript_path)
    return get_state_dir() / "transcript_cursors" / f"{key}.json"


def _lock_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".lock")


def load_cursor(
    conversation_id: str | None,
    transcript_path: Path | str | None = None,
) -> TranscriptCursor | None:
    """Read a cursor defensively. Missing, locked, or corrupt state is None."""
    try:
        state_path = cursor_path(conversation_id, transcript_path)
        with file_lock(_lock_path(state_path), timeout=2.0, stale_after=30.0) as acquired:
            if not acquired:
                return None
            raw = state_path.read_text(encoding="utf-8")
        return TranscriptCursor.from_dict(json.loads(raw))
    except Exception:
        return None


def save_cursor(read: IncrementalRead) -> bool:
    """Atomically store ``read.cursor``. Never raises and is idempotent."""
    cursor = read.cursor
    state_path = read.state_path
    if cursor is None or state_path is None:
        return False
    previous_updated_at = cursor.updated_at
    cursor.updated_at = utc_iso()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            cursor.to_dict(), ensure_ascii=False, indent=2, default=str
        ) + "\n"
        with file_lock(_lock_path(state_path), timeout=5.0, stale_after=30.0) as acquired:
            if not acquired:
                return False
            try:
                existing = json.loads(state_path.read_text(encoding="utf-8"))
                current = json.loads(payload)
                existing.pop("updated_at", None)
                current.pop("updated_at", None)
                if existing == current:
                    cursor.updated_at = previous_updated_at
                    return True
            except Exception:
                pass
            atomic_write_text(state_path, payload)
        return True
    except Exception:
        # A cursor failure must never break the Stop hook; the next run simply
        # performs a full parse.
        return False


# ------------------------------------------------------------------------ reading
def read_incremental(
    transcript_path: Path | str,
    conversation_id: str | None = "",
    force_full: bool = False,
) -> IncrementalRead:
    """Read the transcript tail, falling back to a full parse when unsafe.

    ``force_full`` ignores and replaces any existing cursor.
    """
    try:
        path = Path(transcript_path).expanduser().resolve()
    except OSError:
        path = Path(transcript_path).expanduser()

    try:
        state_path: Path | None = cursor_path(conversation_id, path)
    except Exception:
        state_path = None

    if force_full or not path.is_file():
        return _full_read(path, state_path)

    try:
        size = path.stat().st_size
    except OSError:
        return IncrementalRead(state_path=state_path)

    cursor = load_cursor(conversation_id, path)
    if cursor is None or not _cursor_matches(cursor, path, size):
        return _full_read(path, state_path, cursor=cursor)

    try:
        with path.open("rb") as handle:
            handle.seek(cursor.offset)
            data = handle.read()
    except Exception:
        return _full_read(path, state_path)

    raw_lines, consumed = _complete_lines(data)
    if not raw_lines:
        # A partial trailing line is intentionally left unconsumed. Returning
        # the saved context keeps the content hash stable, so the engine's
        # idempotence check can skip this Stop.
        cursor.size = size
        return IncrementalRead(
            steps=list(cursor.context),
            new_steps=0,
            truncated=False,
            cursor=cursor,
            from_cursor=True,
            state_path=state_path,
        )

    analyzer = TranscriptAnalyzer(path)
    try:
        parsed = analyzer.parse_transcript(raw_lines=raw_lines)
    except Exception:
        return _full_read(path, state_path)

    combined = list(cursor.context) + list(parsed)
    bounded, dropped = _bounded_context(combined)
    cursor.offset += consumed
    cursor.line_count += len(raw_lines)
    cursor.size = size
    cursor.context = bounded
    cursor.head_sha256 = _hash_prefix(path, cursor.offset)
    cursor.tail_sha256 = _hash_tail(path, cursor.offset)
    cursor.updated_at = utc_iso()

    context_truncated = dropped
    return IncrementalRead(
        steps=bounded,
        new_steps=len(parsed),
        truncated=bool(analyzer.truncated or context_truncated),
        cursor=cursor,
        from_cursor=True,
        warning=CONTEXT_TRUNCATION_WARNING if context_truncated else "",
        analysis_truncated=bool(analyzer.truncated),
        context_truncated=context_truncated,
        state_path=state_path,
    )


def reset_cursor(
    transcript_path: Path | str,
    conversation_id: str | None = "",
) -> IncrementalRead:
    """Full-parse the transcript and return a cursor positioned at EOF."""
    return read_incremental(transcript_path, conversation_id=conversation_id, force_full=True)


def _full_read(
    path: Path,
    state_path: Path | None,
    cursor: TranscriptCursor | None = None,
) -> IncrementalRead:
    if not path.is_file():
        return IncrementalRead(cursor=None, state_path=state_path)

    try:
        size = path.stat().st_size
    except OSError:
        return IncrementalRead(state_path=state_path)

    analyzer = TranscriptAnalyzer(path)
    try:
        steps = analyzer.parse_transcript()
    except Exception:
        steps = []
        analyzer.truncated = False

    offset = _last_complete_offset(path, size)
    analysis_truncated = bool(analyzer.truncated)

    # The analyzer reads the whole file, including an unterminated trailing
    # line. If that partial line happens to already be valid JSON, drop it from
    # this read so the newline-terminated version is processed exactly once.
    if offset < size and not analysis_truncated:
        try:
            with path.open("rb") as handle:
                complete = handle.read(offset)
            raw_lines = complete.decode("utf-8", errors="replace").splitlines(keepends=True)
            reparsed = TranscriptAnalyzer(path)
            steps = reparsed.parse_transcript(raw_lines=raw_lines)
            analysis_truncated = bool(reparsed.truncated)
        except Exception:
            pass

    bounded, dropped = _bounded_context(steps)
    new_cursor = TranscriptCursor(
        transcript_path=str(path),
        offset=offset,
        size=size,
        line_count=_count_newlines(path, offset),
        head_sha256=_hash_prefix(path, offset),
        tail_sha256=_hash_tail(path, offset),
        context=bounded,
        updated_at=utc_iso(),
    )
    return IncrementalRead(
        # A full read is the only chance to analyse the whole transcript. The
        # 400-step cap is a pairing-context budget for incremental reads, not
        # an analysis window, so do not drop older evidence here.
        steps=list(steps),
        new_steps=len(steps),
        truncated=bool(analysis_truncated or dropped),
        cursor=new_cursor,
        from_cursor=False,
        warning=CONTEXT_TRUNCATION_WARNING if dropped else "",
        analysis_truncated=analysis_truncated,
        context_truncated=dropped,
        state_path=state_path,
    )


# ----------------------------------------------------------------- cursor checks
def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


def _cursor_matches(cursor: TranscriptCursor, path: Path, size: int) -> bool:
    """Append-mode validation using cheap head/tail integrity hashes."""
    try:
        if cursor.version != CURSOR_VERSION:
            return False
        if _normalized_path(cursor.transcript_path) != _normalized_path(path):
            return False
        if not path.is_file():
            return False
        if cursor.offset < 0 or cursor.size < cursor.offset:
            return False
        if size < cursor.offset:
            return False
        if _hash_prefix(path, cursor.offset) != cursor.head_sha256:
            return False
        if _hash_tail(path, cursor.offset) != cursor.tail_sha256:
            return False
        return True
    except Exception:
        return False


def _hash_prefix(path: Path, offset: int) -> str:
    length = max(0, min(HEAD_BYTES, int(offset)))
    if length == 0:
        return _EMPTY_SHA256
    with path.open("rb") as handle:
        return hashlib.sha256(handle.read(length)).hexdigest()


def _hash_tail(path: Path, offset: int) -> str:
    offset = max(0, int(offset))
    start = max(0, offset - TAIL_BYTES)
    if offset == start:
        return _EMPTY_SHA256
    with path.open("rb") as handle:
        handle.seek(start)
        return hashlib.sha256(handle.read(offset - start)).hexdigest()


def _complete_lines(data: bytes) -> tuple[list[str], int]:
    """Split bytes into newline-terminated lines and bytes consumed.

    Any trailing fragment without a newline is not returned and therefore not
    consumed; ``consumed`` never advances into it.
    """
    lines: list[str] = []
    start = 0
    while True:
        index = data.find(b"\n", start)
        if index < 0:
            break
        lines.append(data[start:index + 1].decode("utf-8", errors="replace"))
        start = index + 1
    return lines, start


def _last_complete_offset(path: Path, size: int) -> int:
    """Return the byte offset just past the final newline."""
    if size <= 0:
        return 0
    try:
        with path.open("rb") as handle:
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return size
            end = size
            while end > 0:
                start = max(0, end - _CHUNK_BYTES)
                handle.seek(start)
                chunk = handle.read(end - start)
                index = chunk.rfind(b"\n")
                if index >= 0:
                    return start + index + 1
                end = start
    except OSError:
        return 0
    return 0


def _count_newlines(path: Path, offset: int) -> int:
    total = 0
    try:
        with path.open("rb") as handle:
            remaining = max(0, offset)
            while remaining > 0:
                chunk = handle.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                total += chunk.count(b"\n")
                remaining -= len(chunk)
    except OSError:
        return total
    return total


def _bounded_context(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Keep the newest steps within the step and byte budgets."""
    bounded = list(steps[-CONTEXT_LIMIT_STEPS:])
    dropped = len(bounded) < len(steps)
    total = 0
    kept: list[dict[str, Any]] = []
    for step in reversed(bounded):
        try:
            size = len(
                json.dumps(step, ensure_ascii=False, default=str).encode("utf-8")
            ) + 1
        except Exception:
            size = 0
        # Always retain at least the newest step, even if a single transcript
        # step is larger than the context byte budget.
        if kept and total + size > CONTEXT_LIMIT_BYTES:
            dropped = True
            break
        kept.append(step)
        total += size
    if len(kept) < len(bounded):
        dropped = True
    return list(reversed(kept)), dropped
