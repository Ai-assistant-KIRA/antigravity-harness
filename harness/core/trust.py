"""Trust scanning and privacy auditing for learned artifacts.

Harness² injects learned rules, lessons, and skills into the agent context.
That makes them an instruction boundary: a skill is not just documentation, it
is executable advice that a future model may follow. Remote skills and
transcript-derived text therefore need a cheap, local check for obvious
prompt-injection, exfiltration, and destructive-command content.

The scanner is intentionally heuristic. It catches well-known shapes and keeps
false positives low enough that ordinary engineering documentation can be
stored. It is not a sandbox, an antivirus engine, or a proof of safety. High
severity findings block skill installation; medium findings are warnings and
low findings are informational.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import get_plugin_dir, get_workspace_skills_dir
from .redaction import redact

MAX_SCAN_FILES = 50
MAX_SCAN_BYTES = 1_000_000
SNIPPET_LIMIT = 180

TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".ps1",
        ".js",
        ".ts",
        ".json",
        ".yaml",
        ".yml",
    }
)

# Kept in one place so skills, generated rules, and future callers agree on the
# severity vocabulary.
SEVERITIES = ("high", "medium", "low")


@dataclass
class Finding:
    """One suspicious line or construct.

    ``snippet`` is redacted and truncated before it is stored so reports never
    become a secondary copy of a credential. ``source`` is optional because a
    direct ``scan_text`` call may not have a file behind it.
    """

    category: str
    severity: str
    message: str
    line: int
    snippet: str
    source: str = ""


# --------------------------------------------------------------------- patterns
_OVERRIDE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)\bignore\s+(?:all\s+|any\s+)?(?:the\s+)?"
            r"(?:previous|prior|above|earlier|foregoing)\s+"
            r"(?:system\s+)?"
            r"(?:instructions?|prompts?|rules?|guidance|directions?|context)\b"
        ),
        "asks the model to ignore earlier instructions",
    ),
    (
        re.compile(
            r"(?i)\bignore\s+(?:all\s+|any\s+)?(?:the\s+)?"
            r"(?:instructions?|prompts?|rules?)\s+"
            r"(?:above|before|previously)\b"
        ),
        "asks the model to ignore earlier instructions",
    ),
    (
        re.compile(
            r"(?i)\b(?:disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?"
            r"(?:above|previous|prior|earlier|foregoing)"
            r"(?:\s+(?:instructions?|prompts?|rules?|context))?\b"
        ),
        "asks the model to disregard earlier instructions",
    ),
    (
        re.compile(
            r"(?i)\b(?:disregard|ignore)\s+(?:the\s+)?system\s+"
            r"(?:prompt|message|instructions?)\b"
        ),
        "asks the model to disregard its system prompt",
    ),
    (
        re.compile(
            r"(?i)\bdisregard\s+(?:the\s+)?system\b(?!\s+[A-Za-z])"
        ),
        "asks the model to disregard the system",
    ),
    (
        re.compile(
            r"(?i)\byou\s+are\s+now\s+(?:(?:in|entering|operating\s+in)\s+)?"
            r"(?:developer|god|dan|jailbreak|unrestricted|uncensored|root|admin|system)\b"
        ),
        "tries to replace the model's role",
    ),
    (
        re.compile(
            r"(?i)\byou\s+are\s+now\s+(?:a|an)\s+"
            r"(?:unrestricted|uncensored|jailbroken|unfiltered|evil|different)\b"
        ),
        "tries to replace the model's role",
    ),
    (
        re.compile(r"(?i)\bdeveloper\s+mode\b"),
        "references developer mode as an instruction override",
    ),
    (
        re.compile(
            r"(?i)\b(?:reveal|show|print|output|repeat|display|leak)\s+"
            r"(?:me\s+)?(?:(?:your|the)\s+)?(?:system|initial|hidden)\s+"
            r"(?:prompt|instructions?)\b"
        ),
        "asks the model to reveal its system prompt",
    ),
    (
        re.compile(
            r"(?i)\b(?:do\s+not|don't|never)\s+"
            r"(?:tell|inform|notify|alert|warn)\s+(?:the\s+)?"
            r"(?:user|human|operator)\b"
        ),
        "asks the model to hide behaviour from the user",
    ),
    (
        re.compile(
            r"(?i)\bhidden\s+(?:instructions?|prompts?|directives?|commands?)\b"
        ),
        "references hidden instructions",
    ),
    (
        re.compile(
            r"(?i)\b(?:instructions?|prompts?)\s+"
            r"(?:hidden|invisible|secret)\s+(?:from|to)\s+(?:the\s+)?user\b"
        ),
        "references instructions hidden from the user",
    ),
]

_EXFIL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?"
            r"(?:sh|bash|zsh|dash|ksh|fish|python[0-9.]*|perl|ruby|node|php|"
            r"powershell|pwsh)\b"
        ),
        "pipes a network download directly into a shell",
    ),
    (
        re.compile(
            r"(?i)\b(?:Invoke-WebRequest|iwr|Invoke-RestMethod|irm)\b[^\n|]*"
            r"\|\s*(?:Invoke-Expression|iex)\b"
        ),
        "pipes a PowerShell download into Invoke-Expression",
    ),
    (
        re.compile(
            r"(?i)\b(?:base64|openssl\s+base64)\b[^\n|]*"
            r"(?:\s-d\b|--decode\b|\s-D\b)[^\n|]*\|\s*(?:sudo\s+)?"
            r"(?:sh|bash|zsh|dash|ksh|python[0-9.]*|perl|ruby|node)\b"
        ),
        "decodes a base64 payload directly into a shell",
    ),
]

_WEBHOOK_RE = re.compile(
    r"(?i)https?://(?:"
    r"discord(?:app)?\.com/api/webhooks/"
    r"|pastebin\.com/"
    r"|webhook\.site/"
    r"|requestbin\.(?:com|net)/"
    r"|pipedream\.net/"
    r")"
)

# A reference to a credential store is only high severity when the same logical
# line also uploads or posts data. Reading a project's own .env is normal.
_SECRET_PATH_RE = re.compile(
    r"(?i)(?:"
    r"(?:~|\$HOME|%USERPROFILE%)[\\/]+\.ssh(?:[\\/]|\b)"
    r"|[\\/]\.ssh[\\/](?:id_(?:rsa|dsa|ecdsa|ed25519)|config|authorized_keys|known_hosts)\b"
    r"|(?:~|\$HOME|%USERPROFILE%)[\\/]+\.aws[\\/]+credentials\b"
    r"|[\\/]\.aws[\\/]+credentials\b"
    r"|(?:~|\$HOME)[\\/]+\.config[\\/]+gcloud\b"
    r"|(?:~|\$HOME)[\\/]+\.azure[\\/]"
    r"|(?:~|\$HOME)[\\/]+\.netrc\b"
    r"|(?:~|\$HOME)[\\/]+\.docker[\\/]+config\.json\b"
    r"|(?:^|[\s=@'\"\/\\])(?:\.env|\.env\.local|\.env\.production)(?![\w.])"
    r"|(?:^|[\s=@'\"\/\\])(?:secrets?\.(?:json|ya?ml|toml)|credentials\.json)\b"
    r")"
)

_PRIVATE_STORE_RE = re.compile(
    r"(?i)(?:"
    r"(?:~|\$HOME|%USERPROFILE%)[\\/]+\.ssh(?:[\\/]|\b)"
    r"|[\\/]\.ssh[\\/](?:id_(?:rsa|dsa|ecdsa|ed25519)|config|authorized_keys|known_hosts)\b"
    r"|(?:~|\$HOME|%USERPROFILE%)[\\/]+\.aws[\\/]+credentials\b"
    r"|[\\/]\.aws[\\/]+credentials\b"
    r"|(?:~|\$HOME)[\\/]+\.config[\\/]+gcloud\b"
    r"|(?:~|\$HOME)[\\/]+\.azure[\\/]"
    r"|(?:~|\$HOME)[\\/]+\.netrc\b"
    r"|(?:~|\$HOME)[\\/]+\.docker[\\/]+config\.json\b"
    r")"
)

_UPLOAD_RE = re.compile(
    r"(?i)(?:"
    r"\bcurl\b[^\n]*(?:\s-d\b|--data(?:-binary|-raw)?\b|\s-F\b|"
    r"--form(?:-string)?\b|--upload-file\b|\s-T\b|"
    r"\|\s*(?:curl|nc|ncat|netcat)\b)"
    r"|\bwget\b[^\n]*(?:--post-data|--post-file|--body-file)"
    r"|\b(?:scp|sftp|rsync|ftp|nc|ncat|netcat|telnet)\b"
    r"|\bInvoke-(?:WebRequest|RestMethod)\b[^\n]*"
    r"(?:-Method\s+(?:Post|Put|Patch)|-Body\b|-InFile\b)"
    r"|\b(?:requests|httpx)\.(?:post|put|patch)\b"
    r"|\burlopen\b[^\n]*\bdata\s*="
    r"|\baws\s+s3\s+(?:cp|mv|sync)\b"
    r"|\bgsutil\s+cp\b"
    r"|\baz\s+storage\b"
    r")"
)

_READ_COMMAND_RE = re.compile(
    r"(?i)\b(?:cat|type|get-content|gc|more|head|tail|cp|copy|copy-item|"
    r"base64|tar|zip|compress-archive|scp|sftp)\b"
)

_SOLICITATION_RE = re.compile(
    r"(?i)\b(?:send|paste|provide|share|give|upload|export|reveal|post|"
    r"transmit|email|message|dm)\s+(?:me\s+)?"
    r"(?:your|the\s+user'?s?|their)\s+"
    r"(?:api[-\s]?key|access[-\s]?token|auth(?:entication)?[-\s]?token|"
    r"token|password|passphrase|secret|credentials?|private[-\s]?key|"
    r"ssh[-\s]?key|session[-\s]?cookie)\b"
)

_BIDI_RE = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200d\u2060\u180e\ufeff]")
_TAG_RE = re.compile(r"[\U000e0000-\U000e007f]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_RM_ROOT_RE = re.compile(
    r"(?i)\brm\s+((?:(?:-[a-z]+\b|--[a-z-]+\b)\s+)+)"
    r"((?:/|~|\$HOME|%USERPROFILE%|[A-Za-z]:[\\/])[^\s;&|]*)"
)
_FORMAT_RE = re.compile(r"(?i)\bformat\s+(?:/[a-z]\s+)*[a-z]:")
_DEL_ROOT_RE = re.compile(
    r"(?i)\bdel\b(?=[^\n]*\s/s\b)(?=[^\n]*\s/q\b)"
    r"[^\n]*[A-Za-z]:[\\/](?:\*|\s|$)"
)
_REMOVE_ITEM_ROOT_RE = re.compile(
    r"(?i)\bRemove-Item\b"
    r"(?=[^\n]*(?:-Recurse\b|-r\b))"
    r"(?=[^\n]*(?:-Force\b|-fo\b))"
    r"[^\n]*(?:[A-Za-z]:[\\/](?:\*|\s|$)|(?:^|\s)[\\/](?:\*|\s|$)|~)"
)
_GIT_FORCE_RE = re.compile(
    r"(?i)\bgit\s+push\b"
    r"(?=[^\n]*(?:--force(?![\w-])|(?<![\w-])-f\b))"
    r"(?=[^\n]*\b(?:main|master)\b)"
)
_DROP_TABLE_RE = re.compile(r"(?i)\bdrop\s+table\b(?!\s+if\s+exists\b)")

_HIDDEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_BIDI_RE, "Unicode bidi control characters can visually reorder text"),
    (_INVISIBLE_RE, "invisible zero-width or BOM characters can hide text"),
    (_TAG_RE, "Unicode tag characters can hide instructions"),
    (_CONTROL_RE, "ASCII control characters can hide terminal output"),
]


# ------------------------------------------------------------------- utilities
def _iter_logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield lines, joining shell-style backslash continuations.

    The reported line number is the first physical line of the logical line.
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    buffer = ""
    start = 1
    for index, raw_line in enumerate(raw.split("\n"), 1):
        if not buffer:
            start = index
        stripped = raw_line.rstrip()
        # Require whitespace before the backslash: a bare trailing ``C:\`` is a
        # Windows root, not a shell continuation.
        if (
            stripped.endswith("\\")
            and not stripped.endswith("\\\\")
            and len(stripped) > 1
            and stripped[-2].isspace()
        ):
            buffer += stripped[:-1] + " "
            continue
        yield start, buffer + raw_line
        buffer = ""
    if buffer:
        yield start, buffer


def _is_hidden_char(char: str) -> bool:
    return any(
        pattern.fullmatch(char) for pattern, _label in _HIDDEN_PATTERNS
    )


def _escape_hidden(value: str) -> str:
    escaped: list[str] = []
    for char in value:
        code = ord(char)
        if _is_hidden_char(char):
            escaped.append(f"<U+{code:04X}>")
        elif char in "\t\n\r" or code >= 0x20:
            escaped.append(char)
        else:
            escaped.append(f"<U+{code:04X}>")
    return "".join(escaped)


def _snippet(line: str) -> str:
    """Redact, flatten, and truncate a line before it is stored."""
    value = redact(line)
    value = _escape_hidden(value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > SNIPPET_LIMIT:
        value = value[: SNIPPET_LIMIT - 1].rstrip() + "…"
    return value


def _rm_targets_system_root(target: str) -> bool:
    normalized = target.replace("\\", "/").lower().strip()
    normalized = normalized.rstrip("*").rstrip("/")
    if normalized in {"", "~", "$home", "%userprofile%"}:
        return True
    return bool(re.fullmatch(r"[a-z]:", normalized))


def _destructive_reason(line: str) -> str:
    match = _RM_ROOT_RE.search(line)
    if match:
        flags = match.group(1).lower()
        recursive = "--recursive" in flags or bool(
            re.search(r"(?:^|\s)-[a-z]*r", flags)
        )
        force = "--force" in flags or bool(
            re.search(r"(?:^|\s)-[a-z]*f", flags)
        )
        if recursive and force and _rm_targets_system_root(match.group(2)):
            return "recursive forced delete of a system or home root"
    if _FORMAT_RE.search(line):
        return "formats a disk volume"
    if _DEL_ROOT_RE.search(line):
        return "recursively deletes a drive root"
    if _REMOVE_ITEM_ROOT_RE.search(line):
        return "recursively and forcibly removes a system root"
    if _GIT_FORCE_RE.search(line):
        return "force-pushes to main or master"
    if _DROP_TABLE_RE.search(line):
        return "drops a database table without IF EXISTS"
    return ""


def _add_finding(
    findings: list[Finding],
    seen: set[tuple[str, str, int, str]],
    *,
    source: str,
    line_number: int,
    line: str,
    category: str,
    severity: str,
    message: str,
) -> None:
    key = (source, category, severity, line_number)
    if key in seen:
        return
    seen.add(key)
    findings.append(
        Finding(
            category=category,
            severity=severity,
            message=message,
            line=line_number,
            snippet=_snippet(line),
            source=source,
        )
    )


# ---------------------------------------------------------------------- scanning
def scan_text(text: str, source: str = "") -> list[Finding]:
    """Scan a block of text for high-confidence untrusted instructions.

    Findings are per logical line. Snippets are redacted and truncated, so the
    returned report is safe to log or show in a CLI.
    """
    if not isinstance(text, str) or not text:
        return []

    findings: list[Finding] = []
    seen: set[tuple[str, str, int, str]] = set()

    for line_number, line in _iter_logical_lines(text):
        line_has_exfil = False
        line_has_override = False

        for pattern, label in _HIDDEN_PATTERNS:
            matches = pattern.findall(line)
            if matches:
                _add_finding(
                    findings,
                    seen,
                    source=source,
                    line_number=line_number,
                    line=line,
                    category="hidden_text",
                    severity="high",
                    message=f"{label} ({len(matches)} occurrence(s))",
                )
                break

        for pattern, label in _OVERRIDE_PATTERNS:
            if pattern.search(line):
                line_has_override = True
                _add_finding(
                    findings,
                    seen,
                    source=source,
                    line_number=line_number,
                    line=line,
                    category="instruction_override",
                    severity="high",
                    message=label,
                )

        for pattern, label in _EXFIL_PATTERNS:
            if pattern.search(line):
                line_has_exfil = True
                _add_finding(
                    findings,
                    seen,
                    source=source,
                    line_number=line_number,
                    line=line,
                    category="exfiltration",
                    severity="high",
                    message=label,
                )

        if _WEBHOOK_RE.search(line):
            line_has_exfil = True
            _add_finding(
                findings,
                seen,
                source=source,
                line_number=line_number,
                line=line,
                category="exfiltration",
                severity="medium",
                message="references a data-drop or webhook endpoint",
            )

        if _SECRET_PATH_RE.search(line) and _UPLOAD_RE.search(line):
            line_has_exfil = True
            _add_finding(
                findings,
                seen,
                source=source,
                line_number=line_number,
                line=line,
                category="exfiltration",
                severity="high",
                message="sends a sensitive credential file over the network",
            )
        elif _PRIVATE_STORE_RE.search(line) and _READ_COMMAND_RE.search(line):
            _add_finding(
                findings,
                seen,
                source=source,
                line_number=line_number,
                line=line,
                category="credential_access",
                severity="low",
                message="reads a private credential store; verify the context",
            )

        if _SOLICITATION_RE.search(line):
            _add_finding(
                findings,
                seen,
                source=source,
                line_number=line_number,
                line=line,
                category="credential_solicitation",
                severity="medium",
                message="asks for a credential instead of a pointer to it",
            )

        reason = _destructive_reason(line)
        if reason:
            severity = "high" if (line_has_exfil or line_has_override) else "medium"
            _add_finding(
                findings,
                seen,
                source=source,
                line_number=line_number,
                line=line,
                category="destructive_command",
                severity=severity,
                message=reason,
            )

    # A destructive command that shares a document with an override or a real
    # exfiltration chain is part of the same attempt, even if it sits on a
    # different line. Upgrade it so reviewers see the combined risk.
    if any(
        finding.severity == "high"
        and finding.category in ("instruction_override", "exfiltration")
        for finding in findings
    ):
        for finding in findings:
            if (
                finding.category == "destructive_command"
                and finding.severity == "medium"
            ):
                finding.severity = "high"

    return findings


def _decode_text(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_skill(folder: Path) -> list[Finding]:
    """Scan ``SKILL.md`` and text-like support files in a skill folder.

    The walk is bounded to :data:`MAX_SCAN_FILES` text files and
    :data:`MAX_SCAN_BYTES` per file. Hidden directories, symlinks, oversized
    files, and files that do not decode as text are skipped. A path to a
    ``SKILL.md`` file is accepted for convenience.
    """
    root = Path(folder).expanduser()
    try:
        if root.is_file():
            if root.name.lower() != "skill.md":
                return []
            candidates = [(root, Path(root.name))]
            root = root.parent
        elif root.is_dir():
            collected: list[tuple[Path, Path]] = []
            for item in root.rglob("*"):
                try:
                    if item.is_symlink() or not item.is_file():
                        continue
                    relative = item.relative_to(root)
                except (OSError, ValueError):
                    continue
                if any(part.startswith(".") for part in relative.parts):
                    continue
                if item.name.lower() != "skill.md" and item.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                collected.append((item, relative))
            collected.sort(
                key=lambda pair: (
                    0 if pair[1].as_posix().lower() == "skill.md" else 1,
                    pair[1].as_posix().lower(),
                )
            )
            candidates = collected[:MAX_SCAN_FILES]
        else:
            return []
    except OSError:
        return []

    findings: list[Finding] = []
    for path, relative in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            with path.open("rb") as handle:
                raw = handle.read(MAX_SCAN_BYTES + 1)
        except OSError:
            continue
        if len(raw) > MAX_SCAN_BYTES:
            continue
        text = _decode_text(raw)
        if not text:
            continue
        findings.extend(scan_text(text, source=relative.as_posix()))
    return findings


def is_safe_to_install(findings: Iterable[Finding]) -> bool:
    """Only high severity findings block installation."""
    for finding in findings or ():
        if getattr(finding, "severity", "") == "high":
            return False
    return True


# ------------------------------------------------------------------ privacy audit
def _path_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "bytes": 0,
        "size": 0,
    }
    try:
        if not path.exists():
            return info
        info["exists"] = True
        size = int(path.stat().st_size)
        info["bytes"] = size
        info["size"] = size
    except OSError:
        pass
    return info


def _skill_summary(directory: Path, limit: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(directory),
        "exists": False,
        "count": 0,
        "samples": [],
    }
    try:
        if not directory.is_dir():
            return summary
        summary["exists"] = True
        children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return summary
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
            if not (child / "SKILL.md").is_file():
                continue
        except OSError:
            continue
        summary["count"] += 1
        if len(summary["samples"]) < limit:
            summary["samples"].append(str(child))
    return summary


def _database_counts(db_path: Path) -> tuple[dict[str, int | None], str]:
    tables = {
        "sessions": "sessions",
        "lessons": "lessons",
        "skills": "skills_index",
        "rule_edits": "rule_edits",
    }
    counts: dict[str, int | None] = {key: None for key in tables}
    error = ""
    try:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return counts, str(exc)[:240]
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        for key, table in tables.items():
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[key] = int(row[0]) if row else 0
            except (sqlite3.Error, TypeError, ValueError):
                counts[key] = None
                if not error:
                    error = f"could not read table '{table}'"
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return counts, error


def audit_privacy(
    plugin_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Report what Harness² has retained without returning its content.

    The report contains file sizes, existence flags, database row counts, a
    bounded list of learned skill paths, and retention guidance. It never
    returns memory text, lesson bodies, event payloads, or secrets. Missing,
    unreadable, and corrupt files are reported instead of raising.
    """
    plugin = Path(plugin_dir).expanduser() if plugin_dir else get_plugin_dir()
    workspace_path = Path(workspace).expanduser() if workspace else Path.cwd()
    try:
        limit = max(0, min(int(sample_limit), 100))
    except (TypeError, ValueError):
        limit = 20

    memory_dir = plugin / "memory"
    state_dir = plugin / "state"
    db_path = memory_dir / "harness_memory.db"

    files = {
        "event_journal": _path_info(state_dir / "events.jsonl"),
        "event_journal_rotated": _path_info(state_dir / "events.jsonl.1"),
        "pending_events": _path_info(state_dir / "pending_events.jsonl"),
        "pending_events_rotated": _path_info(state_dir / "pending_events.jsonl.1"),
        "hook_errors": _path_info(state_dir / "hook_errors.log"),
        "hook_errors_rotated": _path_info(state_dir / "hook_errors.log.1"),
    }
    pending_dir = state_dir / "pending"
    pending_info = _path_info(pending_dir)
    try:
        queues = sorted(pending_dir.glob("*.jsonl")) if pending_dir.is_dir() else []
    except OSError:
        queues = []
    queue_bytes = 0
    for queue in queues:
        try:
            queue_bytes += int(queue.stat().st_size)
        except OSError:
            continue
    pending_info.update(
        {"exists": bool(queues) or pending_info["exists"], "files": len(queues)}
    )
    pending_info["bytes"] = pending_info["size"] = queue_bytes
    files["pending_queues"] = pending_info

    database = _path_info(db_path)
    database["read_only"] = True
    if database["exists"]:
        counts, error = _database_counts(db_path)
        database["counts"] = counts
        if error:
            database["error"] = error
    else:
        database["counts"] = {}

    workspace_skills = get_workspace_skills_dir(workspace_path)
    learned = {
        "workspace_rules": _path_info(workspace_path / "AGENTS.md"),
        "plugin_rules": _path_info(plugin / "rules" / "AGENTS.md"),
        "experience_markdown": _path_info(memory_dir / "EXPERIENCE.md"),
        "workspace_skills": _skill_summary(workspace_skills, limit),
        "plugin_skills": _skill_summary(plugin / "skills", limit),
    }

    state_files = [
        info
        for key, info in files.items()
        if info["exists"] and not key.endswith("_rotated")
    ]
    rotated_present = any(
        info["exists"] for key, info in files.items() if key.endswith("_rotated")
    )

    recommendations = [
        "Review learned rules with `harness rules show`; repair managed blocks "
        "with `harness rules sanitize --dry-run` before applying them.",
        "Validate installed and workspace skills with `harness skills validate`, "
        "and remove any skill whose instructions you no longer trust.",
        "State journals and hook logs are transcript-derived. Delete "
        "state/events.jsonl, state/pending/, state/pending_events.jsonl, "
        "state/hook_errors.log, and their .1 rotations when they are no longer "
        "needed (about 30 days).",
        "Run `harness memory prune` periodically. The defaults keep at most "
        "1000 sessions, 2000 lessons, and 500 rule edits; candidates age out "
        "after 30 days and verified lessons after 365 days.",
        "Remote skills are checked for structure and high-risk content, not "
        "sandboxed or proven safe. Review their source and scripts before use.",
    ]
    if rotated_present:
        recommendations.append(
            "Rotated state files are present; delete the .1 copies if you do "
            "not need historical diagnostics."
        )
    if any(info["bytes"] > 5_000_000 for info in state_files):
        recommendations.append(
            "At least one state journal or log is larger than 5 MB. Truncate or "
            "delete it if the history is no longer needed."
        )
    if database["exists"] and database["bytes"] > 50_000_000:
        recommendations.append(
            "The memory database is larger than 50 MB. Run `harness memory "
            "prune` and consider exporting only the lessons you need."
        )

    return {
        "plugin_dir": str(plugin),
        "workspace": str(workspace_path),
        "files": files,
        "database": database,
        "counts": dict(database.get("counts", {})),
        "learned": learned,
        "retention": {
            "journal_days": 30,
            "candidate_lesson_days": 30,
            "verified_lesson_days": 365,
            "max_sessions": 1000,
            "max_lessons": 2000,
            "max_rule_edits": 500,
            "hook_log_rotate_bytes": 500_000,
        },
        "recommendations": recommendations,
    }
