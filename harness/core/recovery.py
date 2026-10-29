"""Shared judgement about whether a later success really fixed a failure.

The analyzer uses these helpers while pairing failures with recoveries, and the
AGENTS.md manager uses the same helpers to re-check rule text that was written
by an older, laxer version. Keeping one implementation means a rule that would
no longer be learned is also removed by ``harness rules repair``.

Three judgements live here:

* **Command identity.** ``python a.py`` and ``python b.py`` are different
  targets. ``cd x; npx hyperframes check`` and ``npx hyperframes render`` are
  different subcommands. A different target is only a fix when the failure was
  about the command form itself (unknown argument, missing file, usage error).
* **Error classes.** PowerShell quoting failures of inline ``python -c`` /
  ``node -e`` / ``powershell -Command`` code are one lesson, not one rule per
  throwaway command.
* **Benign output.** Status lines such as ``1/1 ready, 0 failed`` are not
  errors.
"""

from __future__ import annotations

import re

# Executables whose first argument is a launcher, so the real identity is the
# first *two* non-flag tokens (``npx hyperframes render``, ``git commit``).
_RUNNERS = frozenset(
    {
        "npx", "npm", "pnpm", "yarn", "bun", "bunx", "git", "docker", "uv",
        "uvx", "pip", "pip3", "cargo", "go", "dotnet", "kubectl", "gh",
        "wrangler", "hyperframes", "poetry", "pipenv", "winget", "choco",
        "scoop", "brew", "apt", "apt-get", "conda", "deno",
    }
)
# Flags that consume the following token as their value.
_VALUE_FLAGS = {
    "python": {"-x", "-w"},
    "py": {"-x", "-w"},
    "node": {"-r", "--require", "--import"},
    "blender": {"-o", "--render-output", "-f", "--render-frame"},
    "powershell": {"-executionpolicy", "-ep", "-windowstyle", "-inputformat", "-outputformat"},
    "pwsh": {"-executionpolicy", "-ep", "-windowstyle", "-inputformat", "-outputformat"},
}
_INLINE_FLAGS = {"-c", "-e", "--eval", "-command", "--command"}
_SHELLS = frozenset({"powershell", "pwsh", "cmd"})
_INTERPRETERS = frozenset({"python", "python3", "py", "node", "deno", "ruby", "perl"})

# ``cd x;``, ``cd "x" &&``, ``Set-Location x;``, ``pushd x &&`` and the
# PowerShell call operator ``&`` say nothing about which program ran.
_PREFIX_RE = re.compile(
    r"""(?ix)^\s*(?:
        (?:cd|chdir|set-location|sl|pushd)\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s*(?:;|&&|\|\|)\s*
      | \$env:[A-Za-z_][\w]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s*(?:;|&&)\s*
      | set\s+"?[A-Za-z_][\w]*=[^&]*?"?\s*&&\s*
      | (?:export\s+)?[A-Za-z_][\w]*=(?:"[^"]*"|'[^']*'|[^\s;&|]+)\s+(?:&&\s*)?
      | &\s+
      | \.\s+(?=["'])
    )"""
)
_TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s'\"]+")

# Parse and quoting failures produced when PowerShell mangles inline code.
INLINE_QUOTING_ERROR_RE = re.compile(
    r"(?i)FullyQualifiedErrorId\s*:\s*(?:UnexpectedToken|ExpectedExpression|MissingPropertyName|"
    r"RedirectionFailed|IncorrectValueForCommandParameter|MissingEndCurlyBrace|"
    r"TerminatorExpectedAtEndOfString|MissingArgument|AmpersandNotAllowed|ParserError)"
    r"|unterminated (?:triple-quoted )?string literal"
    r"|EOL while scanning string literal"
    r"|string is missing the terminator"
    r"|SyntaxError: (?:invalid syntax|unexpected EOF|unexpected character after line continuation)"
)
INLINE_QUOTING_SIGNATURE = "class:inline-interpreter-quoting"
INLINE_QUOTING_RULE = (
    "**run_command**: In PowerShell, multi-line or nested-quote inline code "
    "(`python -c`, `node -e`, `powershell -Command`) breaks quoting "
    "(`FullyQualifiedErrorId`, `unterminated string literal`). "
    "Write the code to a script file and run that file instead."
)

# The failure was about the command form or a missing target, so running the
# same program a different way is a genuine fix.
_USAGE_ERROR_RE = re.compile(
    r"(?i)unknown (?:argument|option|command|subcommand|flag)"
    r"|unrecognized (?:argument|option|arguments)"
    r"|invalid (?:choice|option|argument|command)"
    r"|did you mean"
    r"|\busage:"
    r"|no such (?:file|command|option|subcommand)"
    r"|can't open file"
    r"|cannot find (?:the )?(?:file|path|module)"
    r"|is not recognized"
    r"|\bno [\w.\-/\\]{1,60} (?:found|exists?)\b"
    r"|does not exist"
    r"|not a valid"
    r"|unexpected argument"
    r"|missing (?:required )?argument"
    r"|(?:file|path|module|command|script) not found"
)

# Lines that report success counts. ``0 failed`` is a summary, not a failure.
BENIGN_STATUS_RE = re.compile(
    r"(?i)(?:\b0\s+(?:failed|failures|errors?)\b"
    r"|\b\d+/\d+\s+ready\b"
    r"|\bno errors?\b"
    r"|\b(?:all|\d+)\s+(?:tests?\s+)?passed\b)"
)

TRANSIENT_TOKENS = (
    "timeout", "timed out", "temporar", "connection refused", "econnrefused",
    "econnreset", "connection reset", "try again", "rate limit", "429",
    "temporarily unavailable", "socket", "network is unreachable",
    "resource busy", "being used by another process", "sharing violation",
)


def strip_command_prefix(command: str) -> str:
    """Drop leading ``cd x;`` style prefixes and the PowerShell call operator."""
    text = (command or "").strip()
    for _ in range(4):
        stripped = _PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped.strip()
    return text


def _tokens(command: str) -> list[str]:
    return [token.strip("\"'") for token in _TOKEN_RE.findall(command or "")]


def _executable(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if re.fullmatch(r"python\d+(?:\.\d+)?", name):
        name = "python"
    return name


def command_identity(command: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(executable, target)`` for a shell command.

    ``target`` is the first meaningful argument (two for launchers such as
    ``npx`` or ``git``). Inline code flags (``-c``/``-e``/``-Command``) are the
    target themselves, so all inline invocations of one interpreter share an
    identity.
    """
    tokens = _tokens(strip_command_prefix(command))
    if not tokens:
        return "", ()
    executable = _executable(tokens[0])
    value_flags = _VALUE_FLAGS.get(executable, set())
    wanted = 2 if executable in _RUNNERS else 1
    target: list[str] = []
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        lowered = token.casefold()
        if not lowered:
            continue
        if lowered in _INLINE_FLAGS:
            target.append(lowered)
            break
        if lowered in ("-m", "-file", "--file"):
            target.append(lowered)
            continue
        if lowered.startswith("-"):
            if lowered in value_flags:
                skip_next = True
            continue
        target.append(lowered.replace("\\", "/"))
        if len(target) >= wanted + (1 if target[0] in ("-m", "-file", "--file") else 0):
            break
    return executable, tuple(target)


def is_inline_code(command: str) -> bool:
    executable, target = command_identity(command)
    if not target or target[0] not in _INLINE_FLAGS:
        return False
    return executable in _INTERPRETERS or executable in _SHELLS


def is_inline_quoting_failure(command: str, error: str) -> bool:
    """A PowerShell quoting failure of inline interpreter/shell code."""
    return is_inline_code(command) and bool(INLINE_QUOTING_ERROR_RE.search(error or ""))


def is_usage_error(error: str) -> bool:
    return bool(_USAGE_ERROR_RE.search(error or ""))


def is_transient_error(error: str) -> bool:
    lowered = (error or "").casefold()
    return any(token in lowered for token in TRANSIENT_TOKENS)


def is_benign_status(line: str) -> bool:
    """True for a summary line that reports success counts."""
    return bool(BENIGN_STATUS_RE.search(line or ""))


def commands_match(failed: str, recovered: str) -> bool:
    """Same program and same target, ignoring ``cd`` prefixes and flags."""
    left = command_identity(failed)
    right = command_identity(recovered)
    return bool(left[0]) and left == right


def is_causal_command_fix(failed: str, error: str, recovered: str) -> bool:
    """Whether running ``recovered`` plausibly fixed ``failed``.

    * Same identity: a retry or a flag change. Always plausible.
    * Inline code that PowerShell could not quote, then any run of the same
      interpreter: the "write a script file" recovery.
    * Same executable, different target: only when the failure was about the
      command form or a missing target (``no setup.py found`` ->
      ``python -m build``; ``Unknown argument: profile``).
    * A different executable or an inline-code "fix": never.
    """
    left = command_identity(failed)
    right = command_identity(recovered)
    if not left[0] or not right[0]:
        return False
    if is_inline_code(recovered) and not is_inline_code(failed):
        # Replacing a script with a one-liner trades a real failure for a
        # PowerShell quoting hazard; it is not a durable fix.
        return False
    if left == right:
        return True
    if left[0] != right[0]:
        return False
    if is_inline_quoting_failure(failed, error):
        return True
    return is_usage_error(error)
