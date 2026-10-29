"""Small, dependency-free utilities shared by Harness² modules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


_CHAT_PRONOMINAL_RE = re.compile(
    r"\b(i|i'm|i've|i'd|my|mine|we|our|ours|him|her|his|hers|they|them|their)\b",
    re.IGNORECASE,
)
_CHAT_VERBS = (
    "want", "wanted", "need", "needed", "like", "liked", "love", "loved",
    "feel", "feels", "felt", "sounds", "kinda", "maybe", "abit", "okay",
)
_CHAT_TRAILING = {
    "and", "or", "because", "with", "to", "for", "the", "a", "an", "of",
    "when", "also", "but", "so", "that", "it", "them",
}


def is_chatty_directive(text: str) -> bool:
    """True for a chat sentence that merely contains "never" or "always".

    Users write requirements in passing ("never want scenes to popping up out
    of no where ... i need them to be high quality"). Turning those into
    permanent rules filled AGENTS.md with first-person noise, so they are
    rejected unless they read like an instruction.
    """
    clean = str(text or "").strip()
    if not clean:
        return True
    if len(clean) > 180:
        return True
    if "\u2026" in clean or "..." in clean:
        return True
    lowered = clean.lower()
    if "http://" in lowered or "https://" in lowered:
        return True
    if _CHAT_PRONOMINAL_RE.search(clean):
        return True
    words = lowered.rstrip(" .,-").split()
    if words and words[-1] in _CHAT_TRAILING:
        return True
    padded = f" {lowered} "
    return any(f" {verb} " in padded for verb in _CHAT_VERBS)



def utc_date() -> str:
    return utc_now().strftime("%Y-%m-%d")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def hash_steps(steps: list[dict[str, Any]]) -> str:
    return sha256_text(json.dumps(steps, sort_keys=True, ensure_ascii=False, default=str))


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def one_line(text: str, limit: int = 400) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    return truncate(cleaned, limit)


def short_goal(text: str, limit: int = 100) -> str:
    """First sentence or clause of a request, without a dangling ellipsis."""
    cleaned = one_line(text, 300).strip()
    if not cleaned:
        return ""
    first = re.split(r"(?<=[.!?])\s+|;\s+", cleaned)[0].strip()
    if len(first) > limit:
        window = first[: limit + 20]
        cut = max(window.rfind(", "), window.rfind(" and "), window.rfind(" so "))
        if cut >= limit // 2:
            first = window[:cut]
        else:
            first = first[:limit].rsplit(" ", 1)[0]
    return first.rstrip(".,;:-")


def strip_paths(text: str) -> str:
    """Remove filesystem paths and URLs so titles and names stay readable."""
    cleaned = text or ""
    # Drop quoted paths as a whole: leaving the quotes behind reads like `" "`.
    cleaned = re.sub(r'"[^"]*[\\/:][^"]*"', " ", cleaned)
    cleaned = re.sub(r"'[^']*[\\/:][^']*'", " ", cleaned)
    cleaned = re.sub(r"(?i)\b[a-z]:\\[^\s\"'`]+", " ", cleaned)
    cleaned = re.sub(r"\\\\[^\s\"'`]+", " ", cleaned)
    cleaned = re.sub(r"(?i)\bhttps?://\S+", " ", cleaned)
    cleaned = re.sub(r"(?<![\w.])/(?:[\w.-]+/)+[\w.-]*", " ", cleaned)
    cleaned = re.sub(r'["\']{2,}', " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_for_compare(text: str, keep_code: bool = False) -> str:
    """Normalize a rule/step for duplicate detection.

    ``keep_code`` preserves backticked commands, which is essential when two
    rules share wording but prescribe different fixes.
    """
    text = (text or "").lower()
    if not keep_code:
        text = re.sub(r"`[^`]*`", " code ", text)
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", " uuid ", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[t\s]\d{2}:\d{2}.*?\b", " timestamp ", text)
    text = re.sub(r"\b\d+\b", " n ", text)
    text = re.sub(r"[^a-z0-9_+.#-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Sentence punctuation is not meaningful for comparisons. Strip it only at
    # the edges so extensions such as ``pnpm-lock.yaml`` remain intact.
    return text.strip(" .-+_")


def jaccard(a: str, b: str, keep_code: bool = False) -> float:
    words_a = set(normalize_for_compare(a, keep_code=keep_code).split())
    words_b = set(normalize_for_compare(b, keep_code=keep_code).split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def atomic_write_text(
    path: Path,
    text: str,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> None:
    """Write text atomically, creating parent directories as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_text_lossless(path: Path) -> tuple[str, str, str]:
    """Read a text file without replacing invalid bytes.

    Returns ``(text, encoding, newline)``. The text uses ``\n`` line endings;
    writing it back with the returned encoding and newline reproduces the
    original representation. Undecodable files raise ``UnicodeDecodeError`` so
    callers can skip the update instead of corrupting user content.
    """
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    text = raw.decode(encoding)
    newline = "\r\n" if "\r\n" in text[:8192] else "\n"
    return text.replace("\r\n", "\n"), encoding, newline


def append_jsonl(
    path: Path,
    payload: dict[str, Any],
    max_bytes: int = 2_000_000,
    locked: bool = True,
) -> None:
    """Append one JSON object and rotate the journal when it grows too large.

    Concurrent hook processes share these files. A per-file lock keeps whole
    lines intact; callers that already hold that lock may pass ``locked=False``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not locked:
        _append_jsonl_unlocked(path, payload, max_bytes)
        return
    try:
        lock_path = path.with_name(path.name + ".lock")
        with file_lock(lock_path, timeout=10.0, stale_after=30.0) as acquired:
            if not acquired:
                return
            _append_jsonl_unlocked(path, payload, max_bytes)
    except Exception:
        # Journaling must never break the agent loop.
        pass


def _append_jsonl_unlocked(path: Path, payload: dict[str, Any], max_bytes: int) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.replace(rotated)
            except OSError:
                pass
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # Journaling must never break the agent loop.
        pass


@contextmanager
def file_lock(path: Path, timeout: float = 8.0, stale_after: float = 120.0) -> Iterator[bool]:
    """Portable best-effort exclusive lock using an atomically-created file.

    Yields True when the lock was acquired, False on timeout. Callers should
    treat a lock miss as "another process is already doing this work".

    The lock records an ownership token. Release only removes the file when the
    token still matches, so a process whose stale lock was taken over cannot
    delete its successor's lock.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    deadline = time.monotonic() + timeout
    acquired = False
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{token}\n{time.time()}\n")
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_after:
                    # Re-check immediately before deleting; another waiter may
                    # have already replaced the stale lock.
                    if time.time() - lock_path.stat().st_mtime > stale_after:
                        lock_path.unlink()
                        continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        except OSError:
            # Windows can report a sharing violation while another process is
            # creating or deleting the lock. Treat that as contention and keep
            # trying until the deadline instead of dropping the operation.
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if lock_path.exists():
                    contents = lock_path.read_text(encoding="utf-8", errors="replace")
                    if contents.splitlines() and contents.splitlines()[0].strip() == token:
                        lock_path.unlink()
            except OSError:
                pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    atomic_write_text(Path(path), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- path scoping
# A Windows absolute path. The look-behind keeps escape sequences such as the
# ``k:\n`` inside ``"task:\n"`` from looking like a drive letter. The match
# deliberately stops at whitespace; callers that need to know whether a path
# lives under a root that itself contains spaces ("video editor") must use
# :func:`path_starts_with_root` at the match position instead of comparing the
# truncated match.
WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_\\])[A-Za-z]:[\\/](?![nrt]\b)[^\s`\"'<>|)]*")


def iter_strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string inside a JSON-like value (keys excluded)."""
    if depth > 12:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from iter_strings(item, depth + 1)


def normalize_path_text(text: str) -> str:
    """Lowercase and collapse any run of slashes to one backslash."""
    return re.sub(r"[\\/]+", r"\\", text or "").lower()


def path_root_key(root: Path | str) -> str:
    """Comparable form of a workspace root (resolved, lowercase, no trailing slash)."""
    try:
        resolved = str(Path(root).resolve())
    except OSError:
        resolved = str(root)
    return normalize_path_text(resolved).rstrip("\\")


def path_starts_with_root(text: str, start: int, root_key: str) -> bool:
    """True when ``text[start:]`` begins with the workspace root.

    The comparison happens on the original text, so spaces and apostrophes in
    the root are honoured. The character after the root must end the folder
    name, so ``video editor-old`` is not inside ``video editor``.
    """
    if not root_key:
        return False
    window = normalize_path_text(text[start : start + 2 * len(root_key) + 16])
    if not window.startswith(root_key):
        return False
    following = window[len(root_key) : len(root_key) + 1]
    return not following or not (following.isalnum() or following in "_-.")


def text_mentions_root(text: str, root_key: str) -> bool:
    """True when any absolute path in ``text`` points inside ``root_key``."""
    if not text or not root_key:
        return False
    if root_key not in normalize_path_text(text):
        return False
    return any(
        path_starts_with_root(text, match.start(), root_key)
        for match in WINDOWS_PATH_RE.finditer(text)
    ) or _posix_mentions_root(text, root_key)


def _posix_mentions_root(text: str, root_key: str) -> bool:
    # Roots on macOS/Linux have no drive letter; a substring test is enough
    # once the folder boundary is checked.
    if ":" in root_key[:3]:
        return False
    normalized = normalize_path_text(text)
    index = normalized.find(root_key)
    while index >= 0:
        following = normalized[index + len(root_key) : index + len(root_key) + 1]
        if not following or not (following.isalnum() or following in "_-."):
            return True
        index = normalized.find(root_key, index + 1)
    return False


# Installed tools are not "another project". A lesson about the Blender or
# Python executable a project uses must survive the foreign-path filter.
_TOOLING_PATH_RE = re.compile(
    r"(?i)^[a-z]:[\\/]+(?:program files(?: \(x86\))?|programdata|windows)(?:[\\/]|$)"
    r"|^[a-z]:[\\/]+users[\\/]+[^\\/]+[\\/]+(?:appdata|\.gemini|\.kiro|\.cursor|\.vscode|"
    r"\.codex|\.claude|\.config|\.local|\.cache)(?:[\\/]|$)"
)


def is_tooling_path(path: str) -> bool:
    """True for system/tool install locations such as Program Files."""
    return bool(_TOOLING_PATH_RE.search(path or ""))


def foreign_windows_paths(
    text: str,
    root_key: str,
    ignore_tooling: bool = False,
) -> list[str]:
    """Absolute Windows paths in ``text`` that do not live under ``root_key``.

    With ``ignore_tooling``, install locations such as ``C:\\Program Files``
    are not reported. Like the root check, that test reads the original text
    at the match position, because the match itself stops at the space in
    "Program Files".
    """
    foreign: list[str] = []
    for match in WINDOWS_PATH_RE.finditer(text or ""):
        if len(match.group(0)) < 4:
            # A bare drive such as ``C:\`` carries no project identity.
            continue
        if root_key and path_starts_with_root(text, match.start(), root_key):
            continue
        if root_key and _truncated_prefix_of_root(text, match, root_key):
            continue
        if ignore_tooling and is_tooling_path(text[match.start() : match.start() + 160]):
            continue
        foreign.append(match.group(0))
    return foreign


def _truncated_prefix_of_root(text: str, match: re.Match[str], root_key: str) -> bool:
    """``C:\\Users\\me\\Desktop\\vid…`` was cut by a length limit, not foreign."""
    end = match.end()
    if not text[end : end + 1] == "\u2026" and not match.group(0).endswith("\u2026"):
        return False
    stem = normalize_path_text(match.group(0).rstrip("\u2026"))
    return len(stem) >= 4 and root_key.startswith(stem)
