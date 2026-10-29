"""Secret redaction for learned rules, skills, and memory exports.

Harness² writes derived knowledge into files that are often committed to git.
The safe default is therefore: never persist a credential value, only a pointer
to where the credential lives.

The patterns intentionally prefer a small number of false positives over
leaking a credential. The one place where precision matters is prose: a
sentence such as "use a token: refresh it often" must survive unchanged. The
assignment rules therefore look at the key name, the separator, and the shape
of the value instead of redacting every word that follows a sensitive noun.
"""

from __future__ import annotations

import re
from typing import Any, Callable

REDACTED = "[REDACTED]"

_SENSITIVE_WORD = (
    r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|"
    r"CLIENT[_-]?SECRET|PRIVATE[_-]?KEY|CREDENTIALS?|AUTHORIZATION|COOKIE)"
)

# Keys that *contain* a sensitive word but describe metadata rather than a
# value. ``TOKEN_BUDGET=20000`` and ``password_hint=...`` are documentation, not
# credentials.
_NON_SECRET_SUFFIXES = (
    "count",
    "budget",
    "limit",
    "maximum",
    "minimum",
    "max",
    "min",
    "size",
    "ttl",
    "prefix",
    "suffix",
    "url",
    "uri",
    "name",
    "type",
    "length",
    "expiry",
    "expires",
    "field",
    "header",
    "path",
    "file",
    "env",
    "policy",
    "format",
    "hint",
    "label",
    "audience",
    "issuer",
    "scope",
    "algorithm",
    "version",
    "enabled",
    "required",
    "timeout",
)

_SENSITIVE_KEY = re.compile(r"(?i)(" + _SENSITIVE_WORD + r")")


def _is_sensitive_key(key: Any) -> bool:
    """Whether a mapping key names a credential rather than its metadata."""
    text = str(key)
    if not _SENSITIVE_KEY.search(text):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", text.lower())
    return not any(normalized.endswith(suffix) for suffix in _NON_SECRET_SUFFIXES)


_FLAG_PLACEHOLDERS = {
    "argument",
    "flag",
    "option",
    "parameter",
    "password",
    "secret",
    "token",
    "value",
}


def _looks_like_secret_value(value: str, *, separator: str, key: str = "") -> bool:
    """Heuristic for unquoted values, which can also be ordinary prose.

    Explicit assignments (``password=hunter2``) are treated as secrets. For
    colon-separated values (``token: refresh``), require either an unmistakably
    key-like name (``API_KEY``), a digit/symbol, or a long passphrase.
    """
    if len(value) < 4 or value.startswith("-"):
        return False

    key_like = key.isupper() or "_" in key
    if separator == "=":
        return True
    if key_like and len(value) >= 6:
        return True
    if len(value) >= 16 and value.isalpha():
        return True
    if len(value) < 6:
        return False
    return any(not char.isalnum() for char in value) or any(
        char.isdigit() for char in value
    )


def _quoted_assignment(match: re.Match[str]) -> str:
    key, separator, quote, _value = match.groups()
    if not _is_sensitive_key(key):
        return match.group(0)
    return f"{key}{separator}{quote}{REDACTED}{quote}"


def _unquoted_assignment(match: re.Match[str]) -> str:
    key, separator, quote, value = match.groups()
    if not _is_sensitive_key(key):
        return match.group(0)
    if not _looks_like_secret_value(
        value, separator="=" if "=" in separator else ":", key=key
    ):
        return match.group(0)
    return f"{key}{separator}{quote}{REDACTED}{quote}"


def _json_assignment(match: re.Match[str]) -> str:
    key_quote, key, value_quote, _value = match.groups()
    if not _is_sensitive_key(key):
        return match.group(0)
    return f"{key_quote}{key}{key_quote}: {value_quote}{REDACTED}{value_quote}"


def _call_assignment(match: re.Match[str]) -> str:
    key_quote, key, separator, value_quote, _value = match.groups()
    if not _is_sensitive_key(key):
        return match.group(0)
    return f"{key_quote}{key}{key_quote}{separator}{value_quote}{REDACTED}{value_quote}"


def _setx_assignment(match: re.Match[str]) -> str:
    command, key, separator, quote, value = match.groups()
    if not _is_sensitive_key(key) or value.startswith("/"):
        return match.group(0)
    return f"{command}{key}{separator}{quote}{REDACTED}{quote}"


def _flag_assignment(match: re.Match[str]) -> str:
    flag, separator, quote, value = match.groups()
    candidate = value.strip()
    if "=" in separator:
        # ``--token=...`` is an unambiguous assignment, even for short values.
        accepts = len(candidate) >= 4
    else:
        # ``--token ...`` can also be prose ("pass the --token flag").
        accepts = len(candidate) >= 6 and candidate.lower() not in _FLAG_PLACEHOLDERS
    if not accepts:
        return match.group(0)
    return f"{flag}{separator}{quote}{REDACTED}{quote}"



_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # Authorization headers and bearer-style credentials. ``Basic`` is covered
    # by the header rule; matching it standalone would redact ordinary prose
    # such as "basic authentication".
    (re.compile(r"(?i)\b(authorization|proxy-authorization)\s*:\s*([^\r\n]+)"), r"\1: " + REDACTED),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{10,}"), "Bearer " + REDACTED),
    # Command-line flags: --token=value, --password value, --api-key "value".
    # Values that look like prose ("pass the --token flag") are left alone.
    (
        re.compile(
            r"(?i)(?<![\w-])(--?(?:API[_-]?KEY|ACCESS[_-]?(?:KEY|TOKEN)|AUTH[_-]?TOKEN|TOKEN|PASSWORD|"
            r"PASSWD|SECRET|CLIENT[_-]?SECRET|PRIVATE[_-]?KEY)|--key)(=|[ \t]+)([\"']?)"
            r"([^\s\"']{4,})\3"
        ),
        _flag_assignment,
    ),
    # Function-call forms where the name and the value are separate arguments:
    # [Environment]::SetEnvironmentVariable("X_API_KEY", "value", "User"),
    # os.environ.setdefault("TOKEN", "value"), setenv("SECRET", "value").
    (
        re.compile(
            r"(?i)([\"'])([A-Z0-9_]*(?:" + _SENSITIVE_WORD + r")[A-Z0-9_]*)\1"
            r"(\s*,\s*)([\"'])([^\"'\r\n]{4,})\4"
        ),
        _call_assignment,
    ),
    # Windows ``setx NAME value`` and POSIX ``export NAME value``-less forms.
    (
        re.compile(
            r"(?i)\b(setx(?:\.exe)?\s+)([A-Z0-9_]*(?:" + _SENSITIVE_WORD + r")[A-Z0-9_]*)"
            r"(\s+)([\"']?)([^\s\"']{4,})\4"
        ),
        _setx_assignment,
    ),
    # Well-known token prefixes.
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}\b"), "sk-" + REDACTED),
    # ``sk_`` / ``rk_`` keys (ElevenLabs, Stripe live/test secret keys).
    (re.compile(r"\b(?:sk|rk)_(?:live_|test_)?[A-Za-z0-9]{20,}\b"), "sk_" + REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "gh_" + REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_" + REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"), "glpat-" + REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "xox-" + REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA" + REDACTED),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "ASIA" + REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "AIza" + REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "jwt." + REDACTED),
    # JSON-style quoted keys, e.g. {"password": "hunter2"}. The generic
    # assignment pattern below cannot see past the closing quote of the key.
    (
        re.compile(
            r"(?i)([\"'])([A-Z0-9_]*(?:" + _SENSITIVE_WORD + r")[A-Z0-9_]*)\1"
            r"\s*:\s*([\"'])([^\"'\r\n]{4,})\3"
        ),
        _json_assignment,
    ),
    # KEY="value with spaces" / key: 'quoted passphrase' where the key sounds
    # sensitive. This runs before the unquoted assignment rule below.
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:" + _SENSITIVE_WORD + r")[A-Z0-9_]*)(\s*[:=]\s*)"
            r"([\"'])([^\"'\r\n]{4,}?)\3"
        ),
        _quoted_assignment,
    ),
    # KEY=value / key: value where the key sounds sensitive. A callback keeps
    # ordinary prose such as "use a token: refresh it often" intact.
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:" + _SENSITIVE_WORD + r")[A-Z0-9_]*)(\s*[:=]\s*)"
            r"([\"']?)([^\s\"',;]{4,})\3"
        ),
        _unquoted_assignment,
    ),
    # Connection strings with embedded credentials. Password characters may
    # include ``/``, so the password class only excludes whitespace and ``@``.
    (
        re.compile(r"(?i)\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|kafka|mssql)://)[^\s:@/]+:[^\s@]+@"),
        r"\1" + REDACTED + "@",
    ),
    (
        re.compile(r"(?i)\b(https?://)[^\s:@/]+:[^\s@]+@"),
        r"\1" + REDACTED + "@",
    ),
    # Private key blocks.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "-----BEGIN PRIVATE KEY-----" + REDACTED + "-----END PRIVATE KEY-----",
    ),
]


def redact(text: str) -> str:
    """Return text with credential values replaced by a redaction marker."""
    if not isinstance(text, str) or not text:
        return text
    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_mapping(value: Any, _sensitive: bool = False) -> Any:
    """Recursively redact strings in JSON-like values.

    A value stored under a sensitive key (for example ``password``) is
    redacted even when it does not contain a recognisable token format.
    """
    if isinstance(value, str):
        if _sensitive and value:
            return REDACTED
        return redact(value)
    if isinstance(value, list):
        return [redact_mapping(item, _sensitive) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item, _sensitive) for item in value)
    if isinstance(value, dict):
        return {
            key: redact_mapping(
                item,
                _sensitive or _is_sensitive_key(key),
            )
            for key, item in value.items()
        }
    return value


def contains_secret(text: str) -> bool:
    """Whether redaction would materially change the text."""
    if not isinstance(text, str) or not text:
        return False
    return redact(text) != text


# A bare credential pasted without a recognisable name or prefix, such as a
# 32-character hex API key. Too aggressive for memory (content hashes are
# legitimate there), so it is only applied to text that is written into
# project files: SKILL.md, AGENTS.md rules, and routing lines.
_HIGH_ENTROPY_RE = re.compile(r"(?<![A-Za-z0-9_/\\.-])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_/\\.-])")


def _looks_high_entropy(token: str) -> bool:
    if REDACTED.strip("[]") in token:
        return False
    digits = sum(char.isdigit() for char in token)
    letters = sum(char.isalpha() for char in token)
    if digits < 6 or letters < 6:
        return False
    # Slugs and identifiers made of words ("edit-video-2026-10-27-export") are
    # not secrets: secrets have long runs without separators.
    longest_run = max((len(part) for part in re.split(r"[-_]", token)), default=0)
    return longest_run >= 20


def redact_strict(text: str) -> str:
    """:func:`redact` plus bare high-entropy tokens, for project artifacts."""
    if not isinstance(text, str) or not text:
        return text
    result = redact(text)
    return _HIGH_ENTROPY_RE.sub(
        lambda match: REDACTED if _looks_high_entropy(match.group(0)) else match.group(0),
        result,
    )


def contains_secret_strict(text: str) -> bool:
    """Whether :func:`redact_strict` would change the text."""
    if not isinstance(text, str) or not text:
        return False
    return redact_strict(text) != text
