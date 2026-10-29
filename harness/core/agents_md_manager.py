"""
Safe, bounded evolution of ``AGENTS.md`` files.

Design rules:

* User-authored text is never rewritten. Harness only owns the block between
  ``HARNESS_HEADER`` and ``HARNESS_FOOTER``.
* Workspace rules may contain project-specific facts. Global plugin rules may
  only contain environment and tooling guidance that is safe in every project.
* Transcript noise (timestamps, exit codes, UUID fragments, truncated output)
  is rejected before it can become a persistent rule.
* The file is kept comfortably below Antigravity's 24 KB per-file cap; the
  default managed budget is 20 KB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .analyzer import ContrastiveEvent
from .config import get_plugin_dir, get_plugin_rules_path, get_workspace_rules_path
from .recovery import (
    INLINE_QUOTING_RULE,
    is_benign_status,
    is_causal_command_fix,
    is_inline_code,
    is_inline_quoting_failure,
    is_transient_error,
)
from .redaction import contains_secret, contains_secret_strict, redact, redact_strict
from .util import (
    atomic_write_text,
    foreign_windows_paths,
    is_chatty_directive,
    jaccard,
    normalize_for_compare,
    one_line,
    path_root_keys,
    read_text_lossless,
    truncate,
)

HARNESS_HEADER = "<!-- HARNESS:AUTONOMOUS_RULES_START -->"
HARNESS_FOOTER = "<!-- HARNESS:AUTONOMOUS_RULES_END -->"
MANAGED_RE = re.compile(
    r"(?m)^[ \t]*"
    + re.escape(HARNESS_HEADER)
    + r".*?^[ \t]*"
    + re.escape(HARNESS_FOOTER)
    + r"[ \t]*$",
    re.DOTALL,
)
UNCLOSED_HEADER_RE = re.compile(
    r"(?m)^[ \t]*" + re.escape(HARNESS_HEADER) + r"[ \t]*$"
)
MAX_RULE_FILE_BYTES = 20_000
ANTIGRAVITY_RULE_CAP = 24 * 1024

SECTION_DIRECTIVES = "Standing Project Directives"
SECTION_QUIRKS = "Environment & Tooling Quirks"
SECTION_PITFALLS = "Learned Pitfalls & Recovery Invariants"
SECTION_ROUTING = "Task Routing"
MAX_ROUTING_ITEMS = 16

_NOISE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}", re.IGNORECASE),
    re.compile(r"\b(?:completed|created|started|finished)\s+at\b", re.IGNORECASE),
    re.compile(r"command exited with code", re.IGNORECASE),
    re.compile(r"^exit(?:ed with)? code \d+\.?$", re.IGNORECASE),
    re.compile(r"^(?:command )?(?:output|stdout|stderr)\s*:", re.IGNORECASE),
    re.compile(r"showing lines \d+", re.IGNORECASE),
    re.compile(r"finished with result", re.IGNORECASE),
    re.compile(r"\btask-\d+\b", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", re.IGNORECASE),
    re.compile(r"^[0-9a-f-]{16,}", re.IGNORECASE),
    re.compile(r"succeeded by adjusting parameters/context", re.IGNORECASE),
    re.compile(r"\bunknown (?:error|command|failure)\b", re.IGNORECASE),
    re.compile(r"</?SYSTEM_MESSAGE>", re.IGNORECASE),
    re.compile(r"when using '.*' and encountering", re.IGNORECASE),
    re.compile(r"worry about (?:the )?license|private use|do your best|thank you", re.IGNORECASE),
)

_LOW_SIGNAL = (
    "try again",
    "something went wrong",
    "an error occurred",
    "failed without",
    "no verified recovery",
    "unknown error",
    "command failed",
)

_GLOBAL_UNSAFE = (
    re.compile(r"[a-zA-Z]:[\\/]"),
    re.compile(r"(?i)/(?:users|home|var|tmp|opt|workspace)/"),
    re.compile(r"(?i)\b(this|the current)\s+(?:workspace|project|repository|repo)\b"),
    re.compile(r"(?i)\.agents[\\/]"),
    re.compile(r"(?i)\b[a-zA-Z0-9_.-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|cs|php|rb)\b"),
)

_GLOBAL_UNSAFE_WORDS = (
    "alembic",
    "wrangler pages deploy",
    "docker compose up",
    "project-name",
    "localhost:",
)

# Phrases that can make a sentence a standing project rule rather than a
# one-off task instruction.
_INVARIANT_MARKERS = (
    "always",
    "never",
    "do not",
    "don't",
    "avoid",
    "prefer",
    "must",
    "from now on",
    "going forward",
    "in this project",
    "in this repo",
    "for this project",
)
_STRONG_MARKERS = (
    "always",
    "never",
    "do not",
    "don't",
    "avoid",
    "must",
    "from now on",
    "going forward",
)
_TASK_VERBS = (
    "install",
    "generate",
    "create",
    "build",
    "fix",
    "update",
    "add",
    "write",
    "research",
    "run",
    "set up",
    "implement",
    "refactor",
    "make sure",
    "remember to",
)
_TASK_PREFIXES = (
    "please",
    "can you",
    "could you",
    "i want",
    "i need",
    "help me",
    "make sure",
    "remember to",
)
# A directive that points at "it" or "right now" only made sense inside the
# conversation that produced it.
_ONE_OFF_RE = re.compile(
    r"(?i)\b(?:it|this one|that one|right now|for now|this time|today|tonight|"
    r"yesterday|just now|so far)\b"
)
_PLUGIN_ROOT_LITERAL = ".gemini/config/plugins/harness"
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_WINDOWS_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\")
_POSIX_ABS_PATH_RE = re.compile(r"(?<![\w:/])/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+")


def _text_fragments(value: Any) -> list[str]:
    """Flatten strings from plain values, mappings, and sequences."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        fragments: list[str] = []
        for key, item in value.items():
            fragments.extend(_text_fragments(key))
            fragments.extend(_text_fragments(item))
        return fragments
    if isinstance(value, (list, tuple, set)):
        fragments = []
        for item in value:
            fragments.extend(_text_fragments(item))
        return fragments
    return [str(value)]


def _is_self_referential(text: Any, *extra: Any) -> bool:
    """True when text mentions Harness' own plugin installation."""
    fragments = _text_fragments(text)
    for value in extra:
        fragments.extend(_text_fragments(value))
    haystack = re.sub(r"/{2,}", "/", " ".join(fragments).replace("\\", "/").lower())
    if not haystack.strip():
        return False
    if _PLUGIN_ROOT_LITERAL in haystack:
        return True
    plugin_root = re.sub(
        r"/{2,}", "/", str(get_plugin_dir()).replace("\\", "/").lower()
    ).strip("/")
    return bool(plugin_root) and plugin_root in haystack


# Success tools that cannot causally repair a failure in a different tool.
# A transcript line pairing one of them with an unrelated failure is a
# non-causal recovery note, not a durable environment lesson.
_UNRELATED_SUCCESS_TOOLS = (
    "search_web",
    "read_file",
    "write_file",
    "list_dir",
    "find_by_name",
    "grep",
    "curl",
    "wget",
    "Invoke-WebRequest",
    "web_search",
    "browser",
)
_UNRELATED_SUCCESS_RE = re.compile(
    r"(?i)`?\b("
    + "|".join(re.escape(tool) for tool in _UNRELATED_SUCCESS_TOOLS)
    + r")\b`?\s+then\s+succeeded\b"
)
_FAILURE_MARKER_RE = re.compile(
    r"(?i)\b(?:failed|failing|failure|error|exception|traceback)\b"
)
_FAILED_TOOL_RE = re.compile(
    r"(?i)(?:`([^`\n]{1,120})`|\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+))\s+"
    r"(?:failed|failing|failure)\b"
)

# A "Verified fix" is only causal when it installs the missing dependency.
# These mirror the package-manager forms the analyzer recognizes.
_INSTALL_COMMAND_RE = re.compile(
    r"(?i)(?:"
    r"\bpip3?\s+install\b"
    r"|\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+pip\s+install\b"
    r"|\bnpm\s+(?:install|i|add)\b"
    r"|\bpnpm\s+(?:install|add)\b"
    r"|\byarn\s+add\b"
    r"|\buv\s+(?:add|pip\s+install)\b"
    r"|\bpoetry\s+add\b"
    r"|\bpipenv\s+install\b"
    r"|\bapt\s+install\b"
    r"|\bbrew\s+install\b"
    r"|\bwinget\s+install\b"
    r"|\bchoco\s+install\b"
    r"|\bscoop\s+install\b"
    r")"
)
_REMOTE_OR_READ_COMMAND_RE = re.compile(
    r"(?i)(?:^|[\s;&|])(?:"
    r"curl(?:\.exe)?"
    r"|wget(?:\.exe)?"
    r"|invoke-webrequest"
    r"|web_search"
    r"|search_web"
    r"|read_file"
    r")\b"
)


def _is_non_causal_recovery(text: str) -> bool:
    """True for transcript summaries that pair a failure with an unrelated success.

    Two narrow shapes are rejected:

    * ``<tool> failed ...; <other tool> then succeeded`` where the successful
      tool is read-only, a web search, or an HTTP client rather than the tool
      that failed. Same-tool retries and transient-timeout retries are kept.
    * ``Verified fix: use <curl/wget/read/web command>`` where the "fix"
      does not install anything. Legitimate package-manager recoveries are
      kept.
    """
    if not isinstance(text, str):
        return False
    clean = text.strip()
    if not clean:
        return False
    if _is_unverified_fix(clean):
        return True

    fix_match = re.search(r"(?i)\bverified fix:\s*use\s+`([^`\n]+)`", clean)
    if fix_match:
        command = fix_match.group(1).strip()
        if not _INSTALL_COMMAND_RE.search(command) and _REMOTE_OR_READ_COMMAND_RE.search(
            command
        ):
            return True

    success_match = _UNRELATED_SUCCESS_RE.search(clean)
    if success_match and _FAILURE_MARKER_RE.search(clean):
        failed_match = _FAILED_TOOL_RE.search(clean[: success_match.start()])
        if failed_match:
            raw = (failed_match.group(1) or failed_match.group(2) or "").strip()
            failed_tool = re.split(r"\s+", raw, maxsplit=1)[0].strip("`'\"")
            if failed_tool:
                succeeded_tool = success_match.group(1).strip("` ")
                if failed_tool.lower() != succeeded_tool.lower():
                    return True
    return False


_VERIFIED_FIX_RULE_RE = re.compile(
    r"`(?P<failed>[^`\n]+)`\s+failed with\s+`(?P<error>[^`\n]*)`\.?\s+"
    r"Verified fix:\s*use\s+`(?P<fix>[^`\n]+)`",
    re.IGNORECASE,
)
_RETRY_RULE_RE = re.compile(
    r"`(?P<failed>[^`\n]+)`\s+failed with\s+`(?P<error>[^`\n]*)`;\s*retrying succeeded",
    re.IGNORECASE,
)
_INVOKING_RULE_RE = re.compile(
    r"\bInvoking\s+`[^`\n]+`\s+after the dependency failure resolved\b", re.IGNORECASE
)
_FAILED_WITH_RE = re.compile(r"failed with\s+`(?P<error>[^`\n]*)`", re.IGNORECASE)


def _looks_like_path(text: str) -> bool:
    value = (text or "").strip().strip("\"'")
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    return " " not in value and bool(re.search(r"[\\/]", value)) and bool(
        re.search(r"\.[A-Za-z0-9]{1,6}$", value)
    )


def _basename(text: str) -> str:
    return re.split(r"[\\/]", (text or "").strip().strip("\"'"))[-1].casefold()


def canonical_rule(text: str) -> str | None:
    """Map a legacy per-command rule onto its error-class rule, if any."""
    match = _VERIFIED_FIX_RULE_RE.search(text or "")
    if match and is_inline_quoting_failure(match.group("failed"), match.group("error")):
        return INLINE_QUOTING_RULE
    return None


def _is_unverified_fix(text: str) -> bool:
    """True when rule text records a "fix" the recovery rules would reject.

    Older versions paired any failure with the next success of the same
    program, which produced rules such as "download_fonts.py failed (404);
    use render_font_samples.py" or "hyperframes check printed `0 failed`;
    use hyperframes render". These checks mirror the analyzer so stale rules
    are removed instead of lingering in AGENTS.md.
    """
    clean = (text or "").strip()
    if not clean:
        return False
    if canonical_rule(clean):
        return False
    error_match = _FAILED_WITH_RE.search(clean)
    if error_match and is_benign_status(error_match.group("error")) and not re.search(
        r"Traceback|Error\b|Exception\b", error_match.group("error")
    ):
        return True
    if _INVOKING_RULE_RE.search(clean):
        return True
    retry = _RETRY_RULE_RE.search(clean)
    if retry and not is_transient_error(retry.group("error")):
        return True
    fix = _VERIFIED_FIX_RULE_RE.search(clean)
    if fix:
        failed, error, command = fix.group("failed"), fix.group("error"), fix.group("fix")
        if _looks_like_path(failed) and _looks_like_path(command):
            if failed.endswith("…") or command.endswith("…"):
                # Cut by the summary length limit; the basenames are unknown.
                return False
            return _basename(failed) != _basename(command)
        if _looks_like_path(failed) != _looks_like_path(command):
            # A file operation "fixed" by running a script, or the reverse.
            return True
        truncated = failed.endswith("…") or command.endswith("…")
        if is_inline_code(command) and not is_inline_code(failed):
            return True
        if is_inline_code(failed):
            # Throwaway one-liner that failed for a non-quoting reason.
            return True
        if not truncated and not is_causal_command_fix(failed, error, command):
            return True
        if truncated and not is_causal_command_fix(
            failed.rstrip("…"), error, command.rstrip("…")
        ):
            return True
    return False


def is_surfaceable_lesson(lesson: dict[str, Any]) -> bool:
    """Whether a stored lesson still passes the current learning gates.

    Memory written by older versions holds "verified" lessons that the current
    analyzer would reject (unrelated "fixes", status lines read as errors,
    host markup as the error). Recall and briefings must not keep surfacing
    them.
    """
    resolution = str(lesson.get("resolution") or "")
    error = str(lesson.get("error_pattern") or lesson.get("error_message") or "")
    if not resolution.strip():
        return False
    if re.fullmatch(r"\s*</?[A-Z][A-Z0-9_]{2,}>\s*", error):
        return False
    if is_benign_status(error) and not re.search(r"Traceback|Error\b|Exception\b", error):
        return False
    if _is_non_causal_recovery(resolution):
        return False
    return not contains_secret_strict(resolution + " " + error)


def _is_standing_directive(text: str) -> bool:
    """Only durable, scoped rules may become standing project directives.

    One-off task requests and plugin-internal recovery notes are deliberately
    rejected so they cannot pollute a project's AGENTS.md.
    """
    if not isinstance(text, str):
        return False
    clean = text.strip()
    if not clean or len(clean) < 10 or len(clean) > 240:
        return False
    if "?" in clean:
        return False
    if is_chatty_directive(clean):
        return False
    if _is_self_referential(clean):
        return False
    lowered = clean.lower()
    if lowered.startswith(_TASK_PREFIXES):
        return False
    if not any(marker in lowered for marker in _INVARIANT_MARKERS):
        return False
    if _ONE_OFF_RE.search(clean):
        # "Never re render it from scratch" refers to one artifact in one
        # conversation; out of context the rule is meaningless or wrong.
        return False
    has_task_verb = any(verb in lowered for verb in _TASK_VERBS)
    has_strong_marker = any(marker in lowered for marker in _STRONG_MARKERS)
    if has_task_verb and not has_strong_marker:
        return False
    if _WINDOWS_ABS_PATH_RE.search(clean):
        return False
    outside_fences = _CODE_FENCE_RE.sub("", clean)
    if _POSIX_ABS_PATH_RE.search(outside_fences):
        return False
    return True


@dataclass
class RuleSet:
    directives: list[str] = field(default_factory=list)
    quirks: list[str] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    routing: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.directives or self.quirks or self.pitfalls or self.routing)

    def all(self) -> list[str]:
        return [*self.directives, *self.quirks, *self.pitfalls, *self.routing]

    def compact(self) -> "RuleSet":
        return RuleSet(
            directives=_dedupe(self.directives),
            quirks=_dedupe(self.quirks),
            pitfalls=_dedupe(self.pitfalls),
            routing=_dedupe(self.routing),
        )


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        clean = item.strip()
        if not clean:
            continue
        normalized = normalize_for_compare(clean, keep_code=True)
        if not normalized:
            continue
        duplicate = False
        for previous in result:
            if normalized == normalize_for_compare(previous, keep_code=True):
                duplicate = True
                break
            if jaccard(clean, previous, keep_code=True) >= 0.84:
                duplicate = True
                break
            if len(normalized) > 30 and normalized in normalize_for_compare(previous, keep_code=True):
                duplicate = True
                break
        if not duplicate:
            result.append(clean)
    return result


# Rules that mention different members of the same group are mutually exclusive.
_TOOL_GROUPS: tuple[tuple[str, ...], ...] = (
    ("npm", "pnpm", "yarn", "bun"),
    ("pip", "poetry", "uv", "pipenv"),
    ("pytest", "unittest"),
    ("black", "ruff-format"),
    ("eslint", "biome"),
)

_SUBJECT_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "from", "with", "without",
    "when", "whenever", "while", "if", "then", "than", "that", "this", "these",
    "those", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "in", "on", "at", "to", "of", "by", "as", "into", "over", "after", "before",
    "all", "any", "only", "also", "just", "very", "should", "would", "could",
    "can", "may", "might", "will", "project", "workspace", "repo", "repository",
    "file", "files", "code", "change", "changes", "command", "commands", "run",
    "runs", "running", "install", "installs", "installing", "installation",
    "dependency", "dependencies", "package", "packages", "test", "tests",
    "testing", "existing", "currently", "present", "already", "exists",
    "instead", "rather", "using", "usage", "need", "needs", "make", "sure",
    "avoid", "always", "never", "prefer", "preferred", "use", "uses", "must",
    "require", "required", "ensure", "recommended", "n",
})
_NEGATIVE_POLARITY_RE = re.compile(
    r"(?i)\b(?:avoid|never|do not|does not|don'?t|must not|should not|"
    r"no longer|stop using)\b"
)
_POSITIVE_POLARITY_RE = re.compile(
    r"(?i)\b(?:always|prefer|use|uses|must|should|require|required|ensure|"
    r"recommended)\b"
)
_POLARITY_FILLER_RE = re.compile(
    r"(?i)\b(?:do not|does not|don'?t|never|always|avoid|prefer|use|uses|"
    r"using|must|should|require|required|ensure|recommended)\b"
)
_ALTERNATIVE_RE = re.compile(
    r"(?i)\b(?:instead of|rather than|over)\s+`?([a-z0-9_.+/-]+)`?"
)


def _rule_polarity(text: str) -> int:
    lowered = (text or "").lower()
    if _NEGATIVE_POLARITY_RE.search(lowered):
        return -1
    if _POSITIVE_POLARITY_RE.search(lowered):
        return 1
    return 0


def _excluded_tools(text: str) -> set[str]:
    """Tools named only as the rejected alternative ("over npm", "instead of npm")."""
    return {
        match.group(1).strip("`").lower()
        for match in _ALTERNATIVE_RE.finditer(text or "")
    }


def _subject_tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    excluded = _excluded_tools(lowered)
    cleaned = _POLARITY_FILLER_RE.sub(" ", lowered)
    normalized = normalize_for_compare(cleaned, keep_code=True)
    tokens: set[str] = set()
    for token in normalized.split():
        token = token.strip("._+-")
        if not token or token in _SUBJECT_STOPWORDS:
            continue
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        if token in excluded:
            continue
        tokens.add(token)
    return tokens


def _subject_overlap(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    intersection = first & second
    if not intersection:
        return 0.0
    jaccard_score = len(intersection) / len(first | second)
    containment = len(intersection) / min(len(first), len(second))
    # A short subject (usually the tool name) should still conflict with a
    # longer sentence about the same tool.
    return max(jaccard_score, containment) if min(len(first), len(second)) <= 3 else jaccard_score


def _tool_members(text: str) -> dict[int, set[str]]:
    lowered = (text or "").lower()
    excluded = _excluded_tools(lowered)
    found: dict[int, set[str]] = {}
    for index, group in enumerate(_TOOL_GROUPS):
        for member in group:
            pattern = re.escape(member)
            if member == "ruff-format":
                pattern = r"ruff[\s-]format"
            if re.search(
                rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", lowered, re.IGNORECASE
            ):
                if member in excluded:
                    continue
                found.setdefault(index, set()).add(member)
    return found


def _conflict_between(first: str, second: str, section: str) -> dict[str, Any] | None:
    """Return a clear conflict between two rule texts, or ``None``."""
    first_polarity = _rule_polarity(first)
    second_polarity = _rule_polarity(second)
    first_members = _tool_members(first)
    second_members = _tool_members(second)
    # Two non-negative rules ("use X" / "format with X") recommend different
    # members. A rule that explicitly rejects a tool is handled below instead.
    if first_polarity >= 0 and second_polarity >= 0:
        for group in first_members.keys() & second_members.keys():
            left = first_members[group]
            right = second_members[group]
            if left and right and left.isdisjoint(right):
                reason = (
                    "mutually exclusive tools: "
                    + ", ".join(sorted(left))
                    + " vs "
                    + ", ".join(sorted(right))
                )
                return {
                    "first": first,
                    "second": second,
                    "section": section,
                    "reason": reason,
                }

    if first_polarity and second_polarity and first_polarity != second_polarity:
        overlap = _subject_overlap(_subject_tokens(first), _subject_tokens(second))
        if overlap >= 0.8:
            return {
                "first": first,
                "second": second,
                "section": section,
                "reason": "opposing polarity on the same subject",
            }
    return None


def _conflicts_in_rules(rules: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for index, first in enumerate(rules):
        for second in rules[index + 1:]:
            conflict = _conflict_between(
                str(first.get("text") or ""),
                str(second.get("text") or ""),
                str(first.get("section") or "pitfall"),
            )
            if conflict:
                conflicts.append(conflict)
    return conflicts


def _clean_routing_item(text: str) -> str | None:
    """Keep a routing line readable and safe to persist in AGENTS.md.

    Routing entries are not rules: they contain file names, skill names, and a
    short task summary, so the stricter rule sanitizer would discard them. They
    still must never inject managed-block markers, credentials, or newlines.
    """
    if not isinstance(text, str):
        return None
    clean = one_line(text.strip(), 300)
    if not clean:
        return None
    if "\ufffd" in clean or "�" in clean:
        return None
    if "<!-- HARNESS:" in clean or "HARNESS:AUTONOMOUS_RULES" in clean:
        return None
    if contains_secret_strict(clean):
        return None
    if clean.startswith(("- ", "* ", "+ ")):
        clean = clean[2:].strip()
    return redact_strict(clean)


def sanitize_rule(text: str, global_scope: bool = False) -> str | None:
    """Return a clean, actionable one-line rule, or ``None`` when noisy."""
    if not isinstance(text, str):
        return None
    original = text
    clean = text.strip()
    clean = re.sub(r"^[-*+]\s+", "", clean)
    clean = re.sub(r"^\d+[.)]\s+", "", clean)
    clean = one_line(clean, 360).strip()
    if not clean:
        return None
    if "\ufffd" in clean or "�" in clean:
        return None
    if contains_secret_strict(original):
        return None
    if clean.lower().startswith(("http://", "https://")):
        return None
    for pattern in _NOISE_PATTERNS:
        if pattern.search(clean):
            return None
    lowered = clean.lower()
    if any(phrase in lowered for phrase in _LOW_SIGNAL):
        return None
    if len(clean) < 18 or len(clean) > 320:
        return None
    if not re.search(r"[a-zA-Z]{3}", clean):
        return None
    if clean.count("`") % 2:
        # Unbalanced inline code usually means the transcript line was cut off.
        return None
    if "<!-- HARNESS:" in clean or "HARNESS:AUTONOMOUS_RULES" in clean:
        # A learned rule must never inject a managed-block marker.
        return None
    if re.search(r"(?:\b\w{1,3}\.\.\.|\.\.\.\s*$)", clean) and not clean.endswith("..."):
        return None
    if global_scope:
        for pattern in _GLOBAL_UNSAFE:
            if pattern.search(clean):
                return None
        if any(word in lowered for word in _GLOBAL_UNSAFE_WORDS):
            return None
    return redact_strict(clean)


def is_global_safe(text: str) -> bool:
    return sanitize_rule(text, global_scope=True) is not None


class AgentsMdManager:
    """Safely evolves AGENTS.md while preserving user content and scope."""

    def __init__(
        self,
        workspace_path: Path | str | None = None,
        max_bytes: int = MAX_RULE_FILE_BYTES,
        priority: Callable[[str], float] | None = None,
    ):
        self.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
        self.workspace_agents_md = get_workspace_rules_path(self.workspace_path)
        self.plugin_agents_md = get_plugin_rules_path()
        self.max_bytes = max(1_000, min(int(max_bytes), ANTIGRAVITY_RULE_CAP))
        self.priority = priority

    # ------------------------------------------------------------------ public
    def update_rules(
        self,
        contrastive_events: Sequence[ContrastiveEvent],
        user_constraints: Sequence[str],
        environment_quirks: Sequence[str],
        target: str = "workspace",
        on_apply: Callable[[list[dict]], None] | None = None,
    ) -> dict[str, Any]:
        """Evolve one or both rule files.

        ``target`` is ``workspace``, ``plugin``, or ``both``. Passing ``both``
        does not copy project rules into the global file: each target receives
        only the rules that are safe for its scope.

        ``on_apply`` receives the final fitted rules for each target as a list
        of ``{"text", "section", "target_file", "scope", "new"}`` dictionaries.
        ``new`` is True only for rules that were not in the managed block before
        this update. The callback runs after the file has been written (or
        confirmed unchanged) and never receives rules dropped by the budget.
        """
        if target not in ("workspace", "plugin", "both"):
            raise ValueError("target must be 'workspace', 'plugin', or 'both'")

        workspace_rules = self._build_rules(
            contrastive_events,
            user_constraints,
            environment_quirks,
            global_scope=False,
        )
        results: dict[str, Any] = {}
        if target in ("workspace", "both"):
            results["workspace"] = self._apply_to_file(
                self.workspace_agents_md,
                workspace_rules,
                is_workspace=True,
                on_apply=on_apply,
            )
        if target in ("plugin", "both"):
            plugin_rules = self._build_rules(
                contrastive_events,
                (),  # project directives never belong in the global file
                environment_quirks,
                global_scope=True,
            )
            results["plugin"] = self._apply_to_file(
                self.plugin_agents_md,
                plugin_rules,
                is_workspace=False,
                on_apply=on_apply,
            )
        return results

    def repair(self, target: str = "both", dry_run: bool = False) -> dict[str, Any]:
        """Sanitize existing managed blocks, removing legacy transcript noise."""
        if target not in ("workspace", "plugin", "both"):
            raise ValueError("target must be 'workspace', 'plugin', or 'both'")
        results: dict[str, Any] = {}
        if target in ("workspace", "both"):
            results["workspace"] = self._repair_file(self.workspace_agents_md, True, dry_run)
        if target in ("plugin", "both"):
            results["plugin"] = self._repair_file(self.plugin_agents_md, False, dry_run)
        return results

    def read_rules(self, target: str = "workspace") -> RuleSet:
        path = self._target_path(target)
        if not path.exists():
            return RuleSet()
        try:
            text, _, _ = read_text_lossless(path)
        except (OSError, UnicodeError):
            return RuleSet()
        _, blocks = self._split_content(text)
        rules = RuleSet()
        for block in blocks:
            parsed = self._parse_section(block)
            rules.directives.extend(parsed.directives)
            rules.quirks.extend(parsed.quirks)
            rules.pitfalls.extend(parsed.pitfalls)
            rules.routing.extend(parsed.routing)
        return rules.compact()

    def managed_rules(self, target: str = "workspace") -> list[dict[str, Any]]:
        """Return the rules currently inside the managed block (no DB required)."""
        path = self._target_path(target)
        if not path.exists():
            return []
        try:
            text, _, _ = read_text_lossless(path)
        except (OSError, UnicodeError):
            return []
        _, blocks = self._split_content(text)
        rules: list[dict[str, Any]] = []
        scope = "workspace" if target == "workspace" else "plugin"
        for block in blocks:
            parsed = self._parse_section(block, global_scope=False)
            for section, items in self._rule_items(parsed):
                for item in items:
                    rules.append(
                        {
                            "text": item,
                            "section": section,
                            "target_file": str(path),
                            "scope": scope,
                        }
                    )
        return rules

    def find_conflicts(self, target: str = "workspace") -> list[dict[str, Any]]:
        """Return clear conflicts among the rules in one managed block."""
        return _conflicts_in_rules(self.managed_rules(target))

    def routing(self, target: str = "workspace") -> list[str]:
        """Return the auto-selected skill/tool routing currently in the block."""
        path = self._target_path(target)
        if not path.exists():
            return []
        try:
            text, _, _ = read_text_lossless(path)
        except (OSError, UnicodeError):
            return []
        _, blocks = self._split_content(text)
        items = RuleSet()
        for block in blocks:
            parsed = self._parse_section(block, global_scope=target != "workspace")
            items.routing.extend(parsed.routing)
        return [item for item in _dedupe(items.routing)][:MAX_ROUTING_ITEMS]

    def set_routing(
        self,
        task_summary: str,
        items: Sequence[str],
        target: str = "workspace",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Replace the managed routing section, preserving everything else.

        Routing is task-shaped, so it is replaced rather than accumulated. A
        stale ``Task Routing`` block would tell the agent to load skills that
        no longer match the work in front of it.
        """
        path = self._target_path(target)
        result: dict[str, Any] = {
            "updated": False,
            "path": str(path),
            "items": 0,
            "task": one_line(task_summary, 160),
            "bytes": path.stat().st_size if path.exists() else 0,
            "warnings": [],
        }
        cleaned: list[str] = []
        task_line = _clean_routing_item(f"Task: {task_summary}") if task_summary else ""
        if task_line:
            cleaned.append(task_line)
        for item in items or ():
            value = _clean_routing_item(item)
            if value:
                cleaned.append(value)
        cleaned = _dedupe(cleaned)[:MAX_ROUTING_ITEMS]
        if not cleaned and not path.exists():
            return result

        original = ""
        encoding = "utf-8"
        newline = "\n"
        if path.exists():
            try:
                original, encoding, newline = read_text_lossless(path)
            except (OSError, UnicodeError) as exc:
                result["warnings"].append(
                    f"not modified: unreadable or non-UTF-8 file ({exc})"
                )
                return result

        user_content, blocks = self._split_content(original)
        existing = RuleSet()
        for block in blocks:
            parsed = self._parse_section(block, global_scope=target != "workspace")
            existing.directives.extend(parsed.directives)
            existing.quirks.extend(parsed.quirks)
            existing.pitfalls.extend(parsed.pitfalls)
            existing.routing.extend(parsed.routing)
        existing = existing.compact()
        existing.routing = cleaned
        final, fitted, warnings = self._render_with_budget(
            user_content,
            existing,
            target == "workspace",
            newline=newline,
            encoding=encoding,
        )
        result["warnings"].extend(warnings)
        changed = self._normalize_file(final) != self._normalize_file(original)
        if changed and not dry_run:
            atomic_write_text(path, final, encoding=encoding, newline=newline)
        result.update(
            {
                "updated": changed,
                "items": len(fitted.routing),
                "bytes": self._encoded_size(final, newline, encoding),
            }
        )
        return result

    def remove_rule(
        self,
        text: str,
        target: str = "workspace",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove one managed rule, preserving all user content and encoding."""
        path = self._target_path(target)
        result: dict[str, Any] = {
            "updated": False,
            "path": str(path),
            "removed": 0,
            "bytes": path.stat().st_size if path.exists() else 0,
            "warnings": [],
        }
        if not path.exists():
            return result
        try:
            original, encoding, newline = read_text_lossless(path)
        except (OSError, UnicodeError) as exc:
            result["warnings"].append(
                f"not modified: unreadable or non-UTF-8 file ({exc})"
            )
            return result

        user_content, blocks = self._split_content(original)
        parsed = RuleSet()
        for block in blocks:
            block_rules = self._parse_section(block, global_scope=False)
            parsed.directives.extend(block_rules.directives)
            parsed.quirks.extend(block_rules.quirks)
            parsed.pitfalls.extend(block_rules.pitfalls)
            parsed.routing.extend(block_rules.routing)
        cleaned = RuleSet(
            directives=_dedupe(parsed.directives),
            quirks=_dedupe(parsed.quirks),
            pitfalls=_dedupe(parsed.pitfalls),
            routing=_dedupe(parsed.routing),
        )

        wanted = normalize_for_compare(text, keep_code=True)
        sections = self._rule_items(cleaned)
        best: tuple[str, int] | None = None
        best_score = 0.0
        for section, items in sections:
            for index, item in enumerate(items):
                normalized = normalize_for_compare(item, keep_code=True)
                if wanted and normalized == wanted:
                    best = (section, index)
                    best_score = 1.0
                    break
                score = jaccard(text, item, keep_code=True)
                if score >= 0.9 and score > best_score:
                    best = (section, index)
                    best_score = score
            if best_score >= 1.0:
                break
        if best is None:
            result["bytes"] = self._encoded_size(original, newline, encoding)
            return result

        section, index = best
        for current_section, items in sections:
            if current_section == section:
                del items[index]
                break
        final, fitted, warnings = self._render_with_budget(
            user_content, cleaned, target == "workspace", newline=newline, encoding=encoding
        )
        changed = self._normalize_file(final) != self._normalize_file(original)
        if changed and not dry_run:
            atomic_write_text(path, final, encoding=encoding, newline=newline)
        result.update(
            {
                "updated": changed,
                "removed": 1,
                "bytes": self._encoded_size(final, newline, encoding),
                "warnings": warnings,
            }
        )
        return result

    def show(self, target: str = "workspace") -> str:
        path = self._target_path(target)
        if not path.exists():
            return ""
        try:
            text, _, _ = read_text_lossless(path)
            return text
        except (OSError, UnicodeError):
            return ""

    # -------------------------------------------------------------- rule building
    def _build_rules(
        self,
        events: Sequence[ContrastiveEvent],
        constraints: Sequence[str],
        quirks: Sequence[str],
        global_scope: bool,
    ) -> RuleSet:
        rules = RuleSet()
        for constraint in constraints:
            rule = sanitize_rule(constraint, global_scope=global_scope)
            if rule and _is_standing_directive(rule):
                rules.directives.append(rule)

        for quirk in quirks:
            if _is_non_causal_recovery(quirk):
                continue
            rule = sanitize_rule(f"Environment: {quirk}", global_scope=global_scope)
            if (
                rule
                and not _is_self_referential(quirk)
                and not _is_self_referential(rule)
                and not _is_non_causal_recovery(rule)
            ):
                rules.quirks.append(rule)

        for event in events:
            if not getattr(event, "verified", True):
                continue
            if _is_self_referential(
                event.resolution_summary,
                event.failed_args,
                event.successful_args,
            ):
                continue
            summary = sanitize_rule(event.resolution_summary, global_scope=global_scope)
            if not summary:
                continue
            tool = (event.trigger_tool or "tool").strip()
            if tool.lower() in summary.lower()[:40]:
                rule = summary
            else:
                rule = f"**{tool}**: {summary}"
            rule = sanitize_rule(rule, global_scope=global_scope)
            if not rule or _is_self_referential(rule):
                continue
            if _is_non_causal_recovery(summary) or _is_non_causal_recovery(rule):
                continue
            if event.category in ("environment", "network", "dependency"):
                rules.quirks.append(rule)
            else:
                rules.pitfalls.append(rule)
        return rules.compact()

    # ------------------------------------------------------------------ file ops
    def _target_path(self, target: str) -> Path:
        if target == "workspace":
            return self.workspace_agents_md
        if target == "plugin":
            return self.plugin_agents_md
        raise ValueError("target must be 'workspace' or 'plugin'")

    def _apply_to_file(
        self,
        file_path: Path,
        new_rules: RuleSet,
        is_workspace: bool,
        on_apply: Callable[[list[dict]], None] | None = None,
    ) -> dict[str, Any]:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        original = ""
        encoding = "utf-8"
        newline = "\n"
        if file_path.exists():
            try:
                original, encoding, newline = read_text_lossless(file_path)
            except (OSError, UnicodeError) as exc:
                return {
                    "updated": False,
                    "path": str(file_path),
                    "added": 0,
                    "kept": 0,
                    "bytes": file_path.stat().st_size if file_path.exists() else 0,
                    "warnings": [f"not modified: unreadable or non-UTF-8 file ({exc})"],
                }

        user_content, blocks = self._split_content(original)
        existing = RuleSet()
        for block in blocks:
            parsed = self._parse_section(block, global_scope=not is_workspace)
            existing.directives.extend(parsed.directives)
            existing.quirks.extend(parsed.quirks)
            existing.pitfalls.extend(parsed.pitfalls)
            existing.routing.extend(parsed.routing)
        existing = existing.compact()
        previously_managed = existing.compact()
        # Rules written by an older, laxer version are re-checked on every
        # update, so one Stop is enough to clean a polluted block.
        existing = RuleSet(
            directives=[
                rule for rule in existing.directives
                if self._still_valid(rule, "directive", is_workspace)
            ],
            quirks=[
                rule for rule in existing.quirks
                if self._still_valid(rule, "quirk", is_workspace)
            ],
            pitfalls=[
                rule for rule in existing.pitfalls
                if self._still_valid(rule, "pitfall", is_workspace)
            ],
            routing=list(existing.routing),
        )

        warnings: list[str] = []
        superseded: list[str] = []
        conflicts: list[dict[str, Any]] = []
        new_items = [
            {"text": text, "section": section}
            for section, items in self._rule_items(new_rules)
            for text in items
        ]
        if new_items:
            for section, items in self._rule_items(existing):
                for old in list(items):
                    for new_item in new_items:
                        conflict = _conflict_between(
                            old, str(new_item["text"]), section
                        )
                        if not conflict:
                            continue
                        items.remove(old)
                        superseded.append(old)
                        conflicts.append(conflict)
                        warnings.append(
                            "superseded conflicting rule: " + one_line(old, 120)
                        )
                        break

        user_rules = self._bullet_lines(user_content)
        candidate = RuleSet(
            directives=self._merge(existing.directives, new_rules.directives, user_rules, user_content),
            quirks=self._merge(existing.quirks, new_rules.quirks, user_rules, user_content),
            pitfalls=self._merge(existing.pitfalls, new_rules.pitfalls, user_rules, user_content),
            routing=self._merge(existing.routing, new_rules.routing, user_rules),
        ).compact()

        final, fitted, budget_warnings = self._render_with_budget(
            user_content, candidate, is_workspace, newline=newline, encoding=encoding
        )
        warnings.extend(budget_warnings)
        changed = self._normalize_file(final) != self._normalize_file(original)
        if changed:
            atomic_write_text(file_path, final, encoding=encoding, newline=newline)

        payload = self._fitted_payload(
            fitted, file_path, is_workspace, previously_managed
        )
        if on_apply is not None:
            try:
                on_apply(payload)
            except Exception as exc:  # provenance must never break rule evolution
                warnings.append(f"rule provenance callback failed: {exc}")

        result: dict[str, Any] = {
            "updated": changed,
            "path": str(file_path),
            "added": sum(1 for item in payload if item["new"]),
            "kept": len(fitted.all()),
            "bytes": self._encoded_size(final, newline, encoding),
            "warnings": warnings,
        }
        if superseded:
            result["superseded"] = superseded
        if conflicts:
            result["conflicts"] = conflicts
        return result

    @classmethod
    def _rule_items(cls, rules: RuleSet) -> list[tuple[str, list[str]]]:
        return [
            ("directive", rules.directives),
            ("quirk", rules.quirks),
            ("pitfall", rules.pitfalls),
        ]

    @classmethod
    def _all_items(cls, rules: RuleSet) -> list[tuple[str, list[str]]]:
        return [*cls._rule_items(rules), ("routing", rules.routing)]

    def _fitted_payload(
        self,
        fitted: RuleSet,
        file_path: Path,
        is_workspace: bool,
        previous: RuleSet,
    ) -> list[dict[str, Any]]:
        previous_normalized = {
            normalize_for_compare(text, keep_code=True)
            for text in previous.all()
        }
        scope = "workspace" if is_workspace else "plugin"
        payload: list[dict[str, Any]] = []
        for section, items in self._rule_items(fitted):
            for text in items:
                payload.append(
                    {
                        "text": text,
                        "section": section,
                        "target_file": str(file_path),
                        "scope": scope,
                        "new": normalize_for_compare(text, keep_code=True)
                        not in previous_normalized,
                    }
                )
        return payload

    def _repair_file(self, file_path: Path, is_workspace: bool, dry_run: bool) -> dict[str, Any]:
        if not file_path.exists():
            return {
                "updated": False, "path": str(file_path), "removed": 0,
                "kept": 0, "bytes": 0, "warnings": [],
            }
        try:
            original, encoding, newline = read_text_lossless(file_path)
        except (OSError, UnicodeError) as exc:
            return {
                "updated": False, "path": str(file_path), "removed": 0,
                "kept": 0, "bytes": file_path.stat().st_size if file_path.exists() else 0,
                "warnings": [f"not modified: unreadable or non-UTF-8 file ({exc})"],
            }
        user_content, blocks = self._split_content(original)
        parsed = RuleSet()
        before = 0
        for block in blocks:
            block_rules = self._parse_section(
                block, global_scope=not is_workspace, compact=False
            )
            before += len(block_rules.all())
            for section, source, sink in (
                ("directive", block_rules.directives, parsed.directives),
                ("quirk", block_rules.quirks, parsed.quirks),
                ("pitfall", block_rules.pitfalls, parsed.pitfalls),
            ):
                sink.extend(
                    rule
                    for rule in source
                    if self._still_valid(rule, section, is_workspace)
                    and not self._covered_by_user(rule, user_content)
                )
            parsed.routing.extend(
                item for item in block_rules.routing if _clean_routing_item(item)
            )
        cleaned = RuleSet(
            directives=_dedupe(parsed.directives),
            quirks=_dedupe(parsed.quirks),
            pitfalls=_dedupe(parsed.pitfalls),
            routing=_dedupe(parsed.routing),
        )
        final, fitted, warnings = self._render_with_budget(
            user_content, cleaned, is_workspace, newline=newline, encoding=encoding
        )
        changed = self._normalize_file(final) != self._normalize_file(original)
        if changed and not dry_run:
            atomic_write_text(file_path, final, encoding=encoding, newline=newline)
        return {
            "updated": changed,
            "dry_run": dry_run,
            "path": str(file_path),
            "removed": max(0, before - len(fitted.all())),
            "kept": len(fitted.all()),
            "bytes": self._encoded_size(final, newline, encoding),
            "warnings": warnings,
        }

    def _still_valid(self, rule: str, section: str, is_workspace: bool) -> bool:
        """Apply the current learning gates to a rule that is already stored."""
        if _is_self_referential(rule) or _is_non_causal_recovery(rule):
            return False
        if section == "directive":
            # The global file ships curated directives ("Use the harness CLI
            # ...") that are deliberately not phrased as always/never rules.
            # Only workspace directives were learned from chat.
            return not is_workspace or _is_standing_directive(rule)
        if section == "pitfall" and is_workspace and self._mentions_foreign_path(rule):
            return False
        return True

    @staticmethod
    def _covered_by_user(item: str, user_text: str) -> bool:
        """The user's own section already states this lesson."""
        if not user_text:
            return False
        if item == INLINE_QUOTING_RULE:
            return bool(
                re.search(r"(?i)(?:python|py)\s+-c\b|inline\s+`?python|node\s+-e\b", user_text)
            )
        return False

    def _merge(
        self,
        existing: Sequence[str],
        new_items: Sequence[str],
        user_rules: Sequence[str],
        user_text: str = "",
    ) -> list[str]:
        merged = [item for item in existing if not self._covered_by_user(item, user_text)]
        for item in new_items:
            if any(jaccard(item, user_rule, keep_code=True) >= 0.82 for user_rule in user_rules):
                continue
            if self._covered_by_user(item, user_text):
                continue
            merged.append(item)
        # Deduplicate once at the end. Deduplicating after every append is
        # equivalent (both only compare against already-kept items) but O(n^3)
        # when a session reintroduces many rules at once.
        return _dedupe(merged)

    @staticmethod
    def _bullet_lines(text: str) -> list[str]:
        rules: list[str] = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ", "+ ")):
                rules.append(stripped[2:].strip())
        return rules

    # --------------------------------------------------------------- managed block
    def _split_content(self, text: str) -> tuple[str, list[str]]:
        blocks = [match.group(0) for match in MANAGED_RE.finditer(text or "")]
        user_content = MANAGED_RE.sub("", text or "").strip()
        unclosed = UNCLOSED_HEADER_RE.search(user_content)
        if unclosed:
            # Unclosed legacy block: keep the text before it, treat the rest as
            # managed so repair can remove noise.
            index = unclosed.start()
            broken = user_content[index:]
            user_content = user_content[:index].strip()
            if broken.strip():
                blocks.append(broken)
        return user_content, blocks

    def _parse_section(
        self,
        section_text: str,
        global_scope: bool = False,
        compact: bool = True,
    ) -> RuleSet:
        data = RuleSet()
        if not section_text:
            return data
        current: list[str] | None = None
        for raw_line in section_text.splitlines():
            line = raw_line.strip()
            if not line or line == HARNESS_HEADER or line == HARNESS_FOOTER:
                continue
            lowered = line.lower()
            if lowered.startswith("#"):
                if SECTION_DIRECTIVES.lower() in lowered:
                    current = data.directives
                elif SECTION_QUIRKS.lower() in lowered:
                    current = data.quirks
                elif SECTION_PITFALLS.lower() in lowered:
                    current = data.pitfalls
                elif SECTION_ROUTING.lower() in lowered:
                    current = data.routing
                else:
                    current = None
                continue
            if line.startswith(("- ", "* ", "+ ")):
                if current is data.routing:
                    item = _clean_routing_item(line[2:].strip())
                    if item:
                        current.append(item)
                    continue
                rule = sanitize_rule(line[2:].strip(), global_scope=global_scope)
                if rule and current is not None:
                    canonical = canonical_rule(rule)
                    if canonical:
                        # Collapse per-command quoting rules into one.
                        data.pitfalls.append(canonical)
                    else:
                        current.append(rule)
                continue
            if current is data.routing:
                continue
            if current is not None and line != ">":
                # Continuation of a previous bullet (usually a wrapped noise
                # line). The sanitizer will decide whether it is actionable.
                continuation = sanitize_rule(line, global_scope=global_scope)
                if continuation and not continuation.startswith((">", "|")):
                    current.append(continuation)
        return data.compact() if compact else data

    def _format_managed_block(self, rules: RuleSet, is_workspace: bool) -> str:
        title = "Workspace" if is_workspace else "Global Environment"
        lines = [
            HARNESS_HEADER,
            f"## Harness Evolved Directives ({title})",
            "> Maintained automatically by Harness². User content outside this block is preserved.",
            "> Verified failure/recovery lessons only; no transcript timestamps or exit-code noise.",
            "",
        ]
        if rules.directives:
            lines.append(f"### {SECTION_DIRECTIVES}")
            lines.extend(f"- {rule}" for rule in rules.directives)
            lines.append("")
        if rules.quirks:
            lines.append(f"### {SECTION_QUIRKS}")
            lines.extend(f"- {rule}" for rule in rules.quirks)
            lines.append("")
        if rules.pitfalls:
            lines.append(f"### {SECTION_PITFALLS}")
            lines.extend(f"- {rule}" for rule in rules.pitfalls)
            lines.append("")
        if rules.routing:
            lines.append(f"### {SECTION_ROUTING} (auto-selected)")
            lines.extend(f"- {item}" for item in rules.routing)
            lines.append("")
        lines.append(HARNESS_FOOTER)
        return "\n".join(lines).strip()

    @staticmethod
    def _encoded_size(
        text: str, newline: str = "\n", encoding: str = "utf-8"
    ) -> int:
        """Return the bytes written, including BOM, UTF-16, and CRLF effects."""
        try:
            size = len(text.encode(encoding))
        except (LookupError, UnicodeEncodeError):
            size = len(text.encode("utf-8"))
            encoding = "utf-8"
        if newline == "\r\n":
            normalized = encoding.lower().replace("_", "-")
            size += text.count("\n") * (
                2 if normalized in ("utf-16", "utf-16-le", "utf-16-be") else 1
            )
        return size

    def _render_with_budget(
        self,
        user_content: str,
        rules: RuleSet,
        is_workspace: bool,
        newline: str = "\n",
        encoding: str = "utf-8",
    ) -> tuple[str, RuleSet, list[str]]:
        warnings: list[str] = []
        fitted = RuleSet(
            rules.directives[:],
            rules.quirks[:],
            rules.pitfalls[:],
            rules.routing[:],
        )
        user = user_content.strip()
        removed: list[str] = []
        section_weights = {
            "pitfall": 0.05,
            "quirk": 0.03,
            "routing": 0.01,
            "directive": 0.01,
        }
        section_rank = {"pitfall": 0, "quirk": 1, "routing": 2, "directive": 3}

        def render(current: RuleSet) -> str:
            managed = self._format_managed_block(current, is_workspace) if current else ""
            if user and managed:
                return f"{user}\n\n{managed}\n"
            if user:
                return f"{user}\n"
            return f"{managed}\n" if managed else ""

        final = render(fitted)
        while self._encoded_size(final, newline, encoding) > self.max_bytes and fitted:
            if self.priority is None:
                # Deterministic default: drop the oldest pitfall, then quirk,
                # then directive, exactly as before priority callbacks existed.
                if fitted.pitfalls:
                    removed.append(fitted.pitfalls.pop(0))
                elif fitted.quirks:
                    removed.append(fitted.quirks.pop(0))
                elif fitted.directives:
                    removed.append(fitted.directives.pop(0))
                elif fitted.routing:
                    removed.append(fitted.routing.pop(0))
            else:
                choices: list[tuple[float, int, int, str, list[str]]] = []
                for section, items in self._all_items(fitted):
                    for index, text in enumerate(items):
                        try:
                            score = float(self.priority(text))
                        except Exception:
                            score = 0.0
                        score += section_weights[section]
                        choices.append(
                            (score, section_rank[section], index, section, items)
                        )
                if not choices:
                    break
                score, _, index, _, items = min(choices, key=lambda item: item[:3])
                removed.append(items.pop(index))
            final = render(fitted)

        if removed:
            detail = (
                f"removed {len(removed)} rule(s) to respect the size budget; "
                f"first removed: {one_line(removed[0], 100)}"
            )
            warnings.append(detail)

        user_bytes = self._encoded_size(user, newline, encoding)
        if user_bytes > ANTIGRAVITY_RULE_CAP:
            warnings.append(
                f"user content is {user_bytes} bytes, above Antigravity's 24 KB per-file cap"
            )
        if self._encoded_size(final, newline, encoding) > ANTIGRAVITY_RULE_CAP:
            warnings.append("combined AGENTS.md exceeds Antigravity's 24 KB per-file cap")
        if user and not fitted:
            warnings.append("managed rule block removed to respect the size budget")
        return final, fitted, warnings

    @staticmethod
    def _normalize_file(text: str) -> str:
        return (text or "").replace("\r\n", "\n").strip()

    def _mentions_foreign_path(self, text: str) -> bool:
        """True when a rule cites an absolute path outside this workspace."""
        if not self.workspace_path:
            return False
        roots = path_root_keys(self.workspace_path)
        return bool(foreign_windows_paths(text or "", roots, ignore_tooling=True))
