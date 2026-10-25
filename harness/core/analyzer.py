"""
Transcript analysis for Harness².

The analyzer turns a raw Antigravity transcript into evidence:

* verified failure -> recovery contrasts (the Propose-Probe-Compose unit),
* reusable multi-step procedures suitable for SKILL.md synthesis,
* explicit user directives, and
* skill re-use signals.

The design deliberately prefers precision over volume. A noisy rule is worse
than no rule at all, so generic "command exited with code 1" output, timestamps,
and unresolved failures are kept out of the promoted rule/skill path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import detect_project_signals, get_plugin_dir, tokenize
from .redaction import redact
from .util import jaccard, normalize_for_compare, one_line, truncate

# A session can grow for hours. Bound the Stop-hook work so a pathological
# transcript cannot exhaust memory or exceed the host's hook timeout.
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_LINES = 100_000


@dataclass
class ToolExecution:
    """A single tool call paired with the output that followed it."""

    name: str
    args: dict[str, Any] | str
    step_index: int = 0
    output: str = ""
    status: str = ""
    error: str | None = None
    error_signature: str = ""
    confidence: float = 1.0
    success: bool = True
    has_evidence: bool = False

    def command(self) -> str:
        if isinstance(self.args, dict):
            for key in ("CommandLine", "command", "cmd", "Command"):
                value = self.args.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("Path", "path", "Target", "target", "file", "File"):
                value = self.args.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if isinstance(self.args, str):
            return self.args.strip()
        return ""


@dataclass
class ContrastiveEvent:
    """A failure followed by a verified successful resolution."""

    trigger_tool: str
    failed_args: dict[str, Any] | str
    error_message: str
    successful_args: dict[str, Any] | str | None
    resolution_summary: str
    category: str = "command"  # environment, command, dependency, syntax, network, auth, convention
    verified: bool = True
    confidence: float = 0.8
    error_signature: str = ""
    recovery_tool: str = ""
    scope: str = "workspace"
    session_id: str = ""


@dataclass
class WorkflowCandidate:
    """A multi-step procedure that is a candidate for a reusable skill."""

    name: str
    description: str
    goal: str
    steps: list[str]
    tools_involved: list[str]
    code_snippets: list[dict[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    verification_steps: list[str] = field(default_factory=list)
    dead_ends: list[str] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)
    confidence: float = 0.5
    quality_score: float = 0.0
    scope: str = "workspace"
    session_id: str = ""
    success: bool = True


@dataclass
class SessionAnalysis:
    """Structured, evidence-backed extraction from one session transcript."""

    conversation_id: str
    workspace: str = ""
    tasks: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    contrastive_events: list[ContrastiveEvent] = field(default_factory=list)
    unresolved_events: list[ContrastiveEvent] = field(default_factory=list)
    workflows: list[WorkflowCandidate] = field(default_factory=list)
    user_constraints: list[str] = field(default_factory=list)
    environment_quirks: list[str] = field(default_factory=list)
    skill_usage: list[str] = field(default_factory=list)
    final_outcome: str = "completed"
    assistant_messages: list[str] = field(default_factory=list)

    @property
    def verified_events(self) -> list[ContrastiveEvent]:
        return [event for event in self.contrastive_events if event.verified]


class TranscriptAnalyzer:
    """Parses Antigravity transcript JSONL and extracts actionable learnings."""

    TRANSIENT_ERRORS = (
        "timeout", "timed out", "temporar", "connection refused", "econnrefused",
        "econnreset", "connection reset", "try again", "rate limit", "429",
        "temporarily unavailable", "socket", "network is unreachable",
    )

    # Diagnostics that always deserve to stay failures, even when the command
    # also looks like a probe or produced no output.
    ACTIONABLE_ERROR_TOKENS = (
        "no module named", "modulenotfounderror", "importerror", "not recognized",
        "traceback", "syntaxerror", "permission denied", "access is denied",
        "command not found", "cannot find module", "no such file", "not found",
        "connection refused", "econnrefused", "econnreset", "connection reset",
        "could not resolve", "timed out", "timeout", "npm err!",
    )

    EXPLORATORY_COMMANDS = (
        r"^\s*(cd|ls|dir|pwd|echo|cat|type|where|which|whoami|hostname)\b",
        r"^\s*git\s+(status|diff|log|show|branch)\b",
        r"^\s*(node|python|python3|npm|pnpm|yarn)\s+(--version|-v)\b",
        r"^\s*(Get-ChildItem|Get-Content|Get-Item|Select-Object)\b",
    )

    VERIFICATION_COMMAND = re.compile(
        r"(?i)\b(pytest|unittest|tox|nox|npm\s+(run\s+)?test|pnpm\s+test|yarn\s+test|"
        r"vitest|jest|mocha|playwright\s+test|go\s+test|cargo\s+test|dotnet\s+test|"
        r"mvn\s+test|gradle\s+test|ruff|flake8|mypy|pyright|eslint|tsc|typecheck|"
        r"npm\s+run\s+build|pnpm\s+build|yarn\s+build|vite\s+build|webpack|"
        r"curl\s+.*(health|ready)|wget\s+.*(health|ready)|alembic\s+current|"
        r"prisma\s+migrate\s+status|docker\s+compose\s+ps|kubectl\s+get)\b"
    )

    FILE_PATH_TOOLS = (
        "read_file", "view_file", "write_file", "edit_file", "apply_patch",
        "replace_file_content", "multi_replace_file_content", "list_dir",
        "find_by_name", "grep_search", "file_search", "create_file",
        "delete_file", "read_multiple_files",
    )

    FILE_WRITE_TOOLS = (
        "write_file", "edit_file", "apply_patch", "replace_file_content",
        "multi_replace_file_content", "create_file",
    )

    # Outputs from these tools are file contents, not diagnostics. Scanning
    # source code for words like "RuntimeError" creates false candidates.
    FILE_CONTENT_TOOLS = (
        "read_file", "view_file", "read_multiple_files", "list_dir",
        "find_by_name", "grep_search", "file_search",
    )

    _FILE_CONTENT_ERROR_RE = re.compile(
        r"(?i)^(?:"
        r"error\s*[:\-]|failed\s*[:\-]|cannot\s+|could not\s+|unable to\s+|"
        r"no such file|file does not exist|file not found|permission denied|"
        r"access is denied|encountered error"
        r")"
    )

    COMMAND_TOOLS = (
        "run_command", "run_terminal_command", "shell", "bash", "terminal",
    )

    def __init__(self, transcript_path: Path | str | None = None):
        self.transcript_path = Path(transcript_path) if transcript_path else None
        self.truncated = False

    # ------------------------------------------------------------------ parsing
    def parse_transcript(self, raw_lines: list[str] | None = None) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        self.truncated = False
        if raw_lines is None:
            if not self.transcript_path or not self.transcript_path.exists():
                return []
            try:
                size = self.transcript_path.stat().st_size
                if size > MAX_TRANSCRIPT_BYTES:
                    # Keep the newest evidence. Drop the first partial line
                    # produced by seeking into the middle of the file.
                    with self.transcript_path.open("rb") as handle:
                        handle.seek(size - MAX_TRANSCRIPT_BYTES)
                        handle.readline()
                        text = handle.read().decode("utf-8", errors="replace")
                    self.truncated = True
                else:
                    text = self.transcript_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                lines = text.splitlines()
            except Exception:
                return []
        else:
            lines = list(raw_lines)

        if len(lines) > MAX_TRANSCRIPT_LINES:
            lines = lines[-MAX_TRANSCRIPT_LINES:]
            self.truncated = True

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                steps.append(value)
        return steps

    # ----------------------------------------------------------------- analysis
    def analyze(
        self,
        steps: list[dict[str, Any]],
        conversation_id: str = "",
        workspace: str | Path | None = None,
    ) -> SessionAnalysis:
        workspace_str = str(workspace) if workspace else ""
        analysis = SessionAnalysis(conversation_id=conversation_id, workspace=workspace_str)

        executions: list[ToolExecution] = []
        pending: list[ToolExecution] = []
        final_assistant: list[str] = []
        failed_candidates: list[ToolExecution] = []

        for step in steps:
            step_type = str(step.get("type", "") or "")
            source = str(step.get("source", "") or "")
            status = str(step.get("status", "") or "")
            content = self._content_of(step)
            tool_calls = self._tool_calls_of(step)

            # A new user turn resets recovery pairing so we never pair a failure
            # from task A with a success from task B.
            if step_type == "USER_INPUT" or source == "USER_EXPLICIT":
                for failed in failed_candidates:
                    if not getattr(failed, "_recovered", False):
                        event = self._unresolved_event(failed, conversation_id)
                        if event:
                            analysis.unresolved_events.append(event)
                failed_candidates = []
                clean = self._clean_user_text(content)
                if clean:
                    analysis.tasks.append(clean)
                    self._extract_user_constraints(clean, analysis.user_constraints)
                continue

            if tool_calls:
                for index, tool_call in enumerate(tool_calls):
                    name, args = self._normalize_tool_call(tool_call)
                    execution = ToolExecution(
                        name=name,
                        args=args,
                        step_index=int(step.get("step_index", step.get("stepIndex", len(executions))) or 0) + index,
                        status=status or "DONE",
                    )
                    executions.append(execution)
                    pending.append(execution)
                    self._record_skill_usage(args, name, analysis.skill_usage)
                continue

            # Output / result steps are paired FIFO with the tool calls that
            # preceded them. This is what makes the analyzer work on both the
            # current `transcript.jsonl` and the richer `transcript_full.jsonl`.
            is_output = (
                bool(content)
                and (
                    "OUTPUT" in step_type.upper()
                    or "RESULT" in step_type.upper()
                    or "TOOL" in step_type.upper()
                    or status.upper() in ("ERROR", "FAILED", "DONE", "COMPLETED")
                    or bool(step.get("error"))
                )
            )
            if is_output and pending:
                execution = pending.pop(0)
                execution.output = content
                execution.has_evidence = True
                execution.status = status or "DONE"
                error_field = step.get("error") or step.get("errorMessage") or ""
                if error_field:
                    error = one_line(redact(str(error_field)), 500)
                    if self._is_expected_probe(execution, error, content):
                        execution.success = True
                        execution.error = None
                        execution.error_signature = ""
                        execution.confidence = 0.3
                    else:
                        execution.error = error
                        execution.error_signature = self._signature(error)
                        execution.success = False
                        execution.confidence = 0.95
                else:
                    if (
                        execution.name in self.FILE_CONTENT_TOOLS
                        and status.upper() not in ("ERROR", "FAILED")
                    ):
                        error, confidence = self._extract_file_content_error(content)
                    else:
                        error, confidence = self._extract_error(content, status)
                    if error:
                        if self._is_expected_probe(execution, error, content):
                            execution.success = True
                            execution.error = None
                            execution.error_signature = ""
                            execution.confidence = 0.3
                        else:
                            execution.error = error
                            execution.error_signature = self._signature(error)
                            execution.success = False
                            execution.confidence = confidence
                    else:
                        execution.success = True
                if not execution.success:
                    failed_candidates.append(execution)
                else:
                    self._pair_recovery(execution, failed_candidates, analysis, conversation_id)
                continue

            # A planner response with prose and no tool calls is the agent's
            # final answer (or an intermediate explanation).
            if content and ("PLANNER" in step_type.upper() or source == "MODEL"):
                clean = one_line(content, 2000)
                if clean:
                    final_assistant.append(clean)
                self._record_skill_usage({}, "", analysis.skill_usage, content=content)

        # Any tool call that never received an explicit output is assumed to have
        # completed, but with weaker evidence. This matters for commands whose
        # output is not echoed by the host.
        for execution in pending:
            execution.success = True
            execution.has_evidence = False
            execution.confidence = 0.5
            self._pair_recovery(execution, failed_candidates, analysis, conversation_id)

        for failed in failed_candidates:
            if not getattr(failed, "_recovered", False):
                event = self._unresolved_event(failed, conversation_id)
                if event:
                    analysis.unresolved_events.append(event)

        analysis.assistant_messages = final_assistant
        analysis.tool_calls = [
            {
                "name": execution.name,
                "args": execution.args,
                "step_index": execution.step_index,
                "output": truncate(execution.output, 1000),
                "status": execution.status,
                "error": execution.error,
                "success": execution.success,
            }
            for execution in executions
        ]
        analysis.tasks = self._dedupe_texts(analysis.tasks)
        analysis.user_constraints = self._dedupe_texts(analysis.user_constraints)
        analysis.environment_quirks = self._dedupe_texts(analysis.environment_quirks)
        analysis.final_outcome = self._determine_outcome(executions, final_assistant, analysis.unresolved_events)

        workflows = self._extract_procedural_workflows(
            analysis.tasks,
            executions,
            analysis.verified_events,
            workspace=workspace,
            conversation_id=conversation_id,
            outcome=analysis.final_outcome,
        )
        analysis.workflows.extend(workflows)
        return analysis

    # --------------------------------------------------------------- step helpers
    @staticmethod
    def _content_of(step: dict[str, Any]) -> str:
        for key in ("content", "output", "text", "result", "message"):
            value = step.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _tool_calls_of(step: dict[str, Any]) -> list[dict[str, Any]]:
        raw = step.get("tool_calls") or step.get("toolCalls")
        if not raw and isinstance(step.get("toolCall"), dict):
            raw = [step["toolCall"]]
        if isinstance(raw, dict):
            raw = [raw]
        return [item for item in (raw or []) if isinstance(item, dict)]

    @staticmethod
    def _normalize_tool_call(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any] | str]:
        name = (
            tool_call.get("name")
            or tool_call.get("toolName")
            or tool_call.get("tool")
            or "unknown"
        )
        name = str(name).strip()
        if name.lower().startswith("cortex_step_type_"):
            name = name[len("cortex_step_type_"):]
        name = name.lower()
        args = (
            tool_call.get("arguments")
            or tool_call.get("args")
            or tool_call.get("Arguments")
            or {}
        )
        if not isinstance(args, (dict, str)):
            args = str(args)
        return name, args

    def _record_skill_usage(
        self,
        args: dict[str, Any] | str,
        tool_name: str,
        usage: list[str],
        content: str = "",
    ) -> None:
        text = ""
        if isinstance(args, dict):
            text = " ".join(str(value) for value in args.values() if isinstance(value, (str, int, float)))
        elif isinstance(args, str):
            text = args
        text = f"{text} {content} {tool_name}"
        for match in re.finditer(r"skills[\\/]([a-zA-Z0-9][a-zA-Z0-9_-]{1,63})[\\/]SKILL\.md", text, re.IGNORECASE):
            name = match.group(1)
            if name not in usage:
                usage.append(name)

    # ------------------------------------------------------------ error handling
    def _extract_file_content_error(self, content: str) -> tuple[str | None, float]:
        """Extract only explicit file-operation errors from file contents.

        ``read_file`` results are normally source text. A source line such as
        ``raise RuntimeError(...)`` or ``error: retry`` is not evidence that the
        read failed. Numbered content lines are skipped, and only a narrow set
        of diagnostic prefixes is accepted.
        """
        if not isinstance(content, str) or not content.strip():
            return None, 0.0
        for raw_line in self._strip_noise(content).splitlines():
            line = raw_line.strip()
            if not line or self._is_noise_line(line):
                continue
            if re.match(r"^\d+:\s", line):
                continue
            if self._FILE_CONTENT_ERROR_RE.search(line):
                chosen = self._clean_error_line(line)
                if len(chosen) >= 6:
                    return truncate(redact(chosen), 300), 0.6
        return None, 0.0

    def _extract_error(self, content: str, status: str) -> tuple[str | None, float]:
        if not isinstance(content, str) or not content.strip():
            return None, 0.0
        text = self._strip_noise(content)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None, 0.0

        explicit_patterns = [
            r"Traceback \(most recent call last\):",
            r"(?i)\b(error|exception|fatal|panic)\s*[:\-]",
            r"(?i)\b(?:ModuleNotFoundError|ImportError|SyntaxError|TypeError|ValueError|KeyError|"
            r"AttributeError|NameError|FileNotFoundError|PermissionError|RuntimeError)\b",
            r"(?i)is not recognized as (?:the name of )?a",
            r"(?i)command not found",
            r"(?i)no such file or directory",
            r"(?i)access is denied",
            r"(?i)permission denied",
            r"(?i)cannot find module",
            r"(?i)could not (?:connect|resolve|find)",
            r"(?i)connection refused",
            r"(?i)econn(refused|reset)",
            r"(?i)npm ERR!",
            r"(?i)FAILED\s+\S+",
            r"(?i)exit code [1-9]\d*",
            r"(?i)exited with code [1-9]\d*",
        ]

        chosen: str | None = None
        confidence = 0.0
        for line in lines:
            if self._is_noise_line(line):
                continue
            if any(re.search(pattern, line) for pattern in explicit_patterns):
                chosen = line
                confidence = 0.9
                break

        # An exit code is a symptom, not a diagnosis. Keep a low-confidence
        # signal only when the output line also carries an actionable message.
        if not chosen and re.search(r"(?i)(exit|exited with) code [1-9]\d*", text):
            chosen = next((line for line in reversed(lines) if not self._is_noise_line(line)), "")
            confidence = 0.35

        if not chosen and status.upper() in ("ERROR", "FAILED"):
            chosen = next((line for line in reversed(lines) if not self._is_noise_line(line)), "")
            confidence = 0.5

        if not chosen:
            return None, 0.0
        if self._is_noise_line(chosen) or re.fullmatch(
            r"(?i)(?:command\s+)?(?:exited with|exit)\s+code\s+\d+\.?", chosen.strip()
        ):
            return None, 0.0

        # Include the final line of a Python traceback so the rule names the
        # actual exception rather than only "Traceback".
        if "traceback (most recent call last)" in text.lower():
            last = next((line for line in reversed(lines) if not self._is_noise_line(line)), chosen)
            if last and last != chosen:
                chosen = f"{chosen} {last}"

        chosen = self._clean_error_line(chosen)
        if len(chosen) < 6:
            return None, 0.0
        return truncate(redact(chosen), 300), confidence

    @staticmethod
    def _strip_noise(text: str) -> str:
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        text = re.sub(r"\r\n?", "\n", text)
        return text

    @staticmethod
    def _is_noise_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        if re.match(r"(?i)^(created at|completed at|started at|finished at|command output|output|stdout|stderr)\s*:", stripped):
            return True
        if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", stripped):
            return True
        if re.match(r"^Showing lines \d+", stripped):
            return True
        if re.match(r"^Command exited with code \d+\.?$", stripped):
            return True
        if re.match(r"^Command output:\s*$", stripped):
            return True
        return False

    @staticmethod
    def _clean_error_line(line: str) -> str:
        line = one_line(line, 400)
        line = re.sub(r"(?i)^command exited with code \d+:\s*", "", line)
        line = re.sub(r"(?i)^exited with code \d+:\s*", "", line)
        return line.strip(" :-")

    @staticmethod
    def _empty_failure_signal(error: str) -> bool:
        """Return True for an empty, no-output, or exit-code-only signal."""
        text = one_line(str(error or ""), 300).casefold().strip(" .")
        if not text:
            return True
        if text in ("no output from the command", "no output"):
            return True
        return bool(
            re.fullmatch(
                r"(?:command\s+|process\s+)?(?:exited with|exit(?:ed)?(?:\s+with)?)"
                r"\s+code\s*:?\s*\d+",
                text,
            )
        )

    def _has_actionable_output(self, output: str) -> bool:
        """Return True when output carries any diagnostic beyond a bare signal."""
        for line in self._strip_noise(str(output or "")).splitlines():
            stripped = line.strip()
            if not stripped or self._is_noise_line(stripped):
                continue
            if self._empty_failure_signal(stripped):
                continue
            return True
        return False

    def _is_expected_probe(self, execution: ToolExecution, error: str, output: str) -> bool:
        """Return True for a probe whose "failure" carries no diagnostic evidence.

        Probes such as ``Get-NetTCPConnection -ErrorAction SilentlyContinue``
        intentionally exit non-zero when nothing matches. A bare exit code or
        "No output from the command." is not learnable evidence, but a real
        diagnostic (missing module, unrecognized command, traceback, denied
        permission) always remains a failure.
        """
        error_text = str(error or "")
        output_text = str(output or "")
        # Inspect the diagnostic text as a whole. A probe can exit with a bare
        # exit code while the real error (denied access, missing module) lives
        # only in its output; that must stay a failure.
        combined = f"{error_text}\n{output_text}".casefold()
        if any(token in combined for token in self.ACTIONABLE_ERROR_TOKENS):
            return False
        if not self._empty_failure_signal(error_text):
            return False

        command = execution.command()
        if re.search(r"(?i)-ErrorAction\s+SilentlyContinue\b", command):
            return True
        if re.search(
            r"(?i)(?:^|[\s;&|'\"(])(?:Test-Path|Get-NetTCPConnection|Get-Process|"
            r"Get-Item|Get-Command|Get-Service|where\.exe|which|command\s+-v)\b",
            command,
        ):
            return True

        # A command that produced only the bare no-output signal, with no other
        # diagnostic line, is not evidence either.
        if "no output" in error_text.casefold():
            return not self._has_actionable_output(output)
        return False

    @staticmethod
    def _signature(error: str) -> str:
        return truncate(normalize_for_compare(error), 180)

    @staticmethod
    def _target_path(execution: ToolExecution) -> str:
        """Extract the raw path targeted by a file/path tool call."""
        if isinstance(execution.args, dict):
            for key in (
                "AbsolutePath", "TargetFile", "Target", "Path", "path",
                "file", "File", "DirectoryPath",
            ):
                value = execution.args.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        command = execution.command()
        return command.strip() if isinstance(command, str) else ""

    @staticmethod
    def _normalized_identity(value: str) -> str:
        """Normalize a path or argument without erasing meaningful values."""
        text = str(value or "").strip().casefold().replace("\\", "/")
        if not text:
            return ""
        is_unc = text.startswith("//")
        text = re.sub(r"/{2,}", "/", text)
        if is_unc and not text.startswith("//"):
            text = "/" + text
        if text not in ("/", "//"):
            text = text.rstrip("/")
        return text

    @staticmethod
    def _command_identity(command: str) -> str:
        """Return executable basename plus its first meaningful argument token."""
        tokens = re.findall(r"[^\s'\"]+|\"[^\"]*\"|'[^']*'", command or "")
        if not tokens:
            return ""
        executable = tokens[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if not executable:
            return ""
        for token in tokens[1:]:
            cleaned = token.strip("\"'")
            if not cleaned:
                continue
            if cleaned.startswith("-") and cleaned.casefold() not in ("-c", "-m", "-e"):
                continue
            return f"{executable} {cleaned.casefold()}"
        return executable

    @staticmethod
    def _primary_argument(execution: ToolExecution) -> str:
        """Return a deterministic normalized argument for generic tool matching."""
        if isinstance(execution.args, (dict, list)):
            serialized = json.dumps(
                execution.args, sort_keys=True, default=str, ensure_ascii=False
            )
            return TranscriptAnalyzer._normalized_identity(serialized)
        if isinstance(execution.args, str):
            return TranscriptAnalyzer._normalized_identity(execution.args)
        return ""

    @classmethod
    def _recovery_identity_matches(cls, failed: ToolExecution, success: ToolExecution) -> bool:
        """Return True only when a success plausibly recovers this failure."""
        if failed.name in cls.FILE_PATH_TOOLS:
            failed_path = cls._normalized_identity(cls._target_path(failed))
            success_path = cls._normalized_identity(cls._target_path(success))
            return bool(failed_path) and failed_path == success_path
        if failed.name in cls.COMMAND_TOOLS:
            failed_identity = cls._command_identity(failed.command())
            success_identity = cls._command_identity(success.command())
            if not failed_identity or not success_identity:
                return False
            failure_text = normalize_for_compare(failed.error_signature or failed.error or "")
            recovery_output = normalize_for_compare(success.output or "")
            if failure_text and failure_text in recovery_output:
                return False
            if failed_identity == success_identity:
                return True
            # Same executable with a different subcommand is a valid recovery
            # (e.g. `python setup.py` -> `python -m build`). File paths stay
            # strict above because a different file is a different target.
            return failed_identity.split(" ", 1)[0] == success_identity.split(" ", 1)[0]
        failed_argument = cls._primary_argument(failed)
        success_argument = cls._primary_argument(success)
        return bool(failed_argument) and failed_argument == success_argument

    @staticmethod
    def _is_self_referential(*values: object) -> bool:
        """Return True when any value references the Harness plugin install."""
        try:
            plugin_root = str(get_plugin_dir())
        except Exception:
            plugin_root = ""
        roots = [
            root for root in (
                plugin_root.replace("\\", "/").casefold().rstrip("/"),
                ".gemini/config/plugins/harness",
            )
            if root
        ]
        for value in values:
            if value is None:
                continue
            if isinstance(value, (dict, list, tuple)):
                text = json.dumps(value, default=str, ensure_ascii=False)
            else:
                text = str(value)
            normalized = re.sub(r"/{2,}", "/", text.replace("\\", "/").casefold())
            if any(root in normalized for root in roots):
                return True
        return False

    def _pair_recovery(
        self,
        success: ToolExecution,
        failed_candidates: list[ToolExecution],
        analysis: SessionAnalysis,
        conversation_id: str,
    ) -> None:
        if not failed_candidates:
            return
        failed_call: ToolExecution | None = None
        for failed in reversed(failed_candidates):
            if getattr(failed, "_recovered", False):
                continue
            if failed.step_index >= success.step_index:
                continue
            if success.step_index - failed.step_index > 8:
                continue
            if failed.name == success.name and self._recovery_identity_matches(failed, success):
                failed_call = failed
                break
        if failed_call is None:
            # A different call can fix an environment/dependency failure, but
            # only when the later success is causally related to the failure.
            # `_cross_tool_recovery` rejects unrelated successes (web searches,
            # HTTP requests, unrelated commands) and never pairs network
            # failures, which are already covered by same-tool retries.
            for failed in reversed(failed_candidates):
                if getattr(failed, "_recovered", False):
                    continue
                if self._cross_tool_recovery(failed, success):
                    failed_call = failed
                    break
        if failed_call is None:
            return

        event = self._formulate_contrastive_event(failed_call, success, conversation_id)
        if event:
            analysis.contrastive_events.append(event)
            if event.category in ("environment", "network") and not self._is_self_referential(
                event.resolution_summary
            ):
                analysis.environment_quirks.append(event.resolution_summary)
        setattr(failed_call, "_recovered", True)

    def _cross_tool_recovery(self, failed: ToolExecution, success: ToolExecution) -> bool:
        """Return True only when a later call causally recovers ``failed``.

        Temporal order alone is not causality: an unrelated web search, HTTP
        request, or command that happens to succeed after a failure proves
        nothing about that failure. Cross-tool pairing is therefore limited to
        dependency and environment failures, requires real output from the
        recovery, and requires the recovery to install/declare the missing
        module or install/invoke the missing executable.
        """
        if not success.has_evidence:
            return False
        if success.step_index <= failed.step_index:
            return False
        if success.step_index - failed.step_index > 8:
            return False

        error = failed.error or ""
        category = self._categorize(failed, error)
        if category not in ("dependency", "environment"):
            return False

        # A "success" that repeats the failure text is not a recovery. Check the
        # stored signature, the raw error, and the error without its exception
        # prefix so a repeated "No module named 'x'" line is still caught.
        recovery_output = normalize_for_compare(success.output or "")
        failure_texts = {
            normalize_for_compare(failed.error_signature or ""),
            normalize_for_compare(error),
            normalize_for_compare(
                re.sub(r"(?i)^(?:ModuleNotFoundError|ImportError)\s*:\s*", "", error)
            ),
        }
        failure_texts.discard("")
        if any(text in recovery_output for text in failure_texts):
            return False

        if category == "dependency":
            module = self._missing_module(error)
            if not module:
                return False
            return self._dependency_recovery_matches(module, success)

        executable = self._missing_executable(error)
        if not executable:
            return False
        return self._environment_recovery_matches(executable, success)

    @staticmethod
    def _missing_module(error: str) -> str:
        """Extract the missing module/package name from a dependency error."""
        text = str(error or "")
        patterns = (
            r"(?i)No module named\s+['\"`]([^'\"`]+)['\"`]",
            r"(?i)ModuleNotFoundError\s*:\s*(?:No module named\s+)?"
            r"['\"`]?([A-Za-z0-9_@][A-Za-z0-9_.\-/@]*)['\"`]?",
            r"(?i)Cannot find module\s+['\"`]([^'\"`]+)['\"`]",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            name = match.group(1).strip()
            if name and name.casefold() not in ("no", "module", "named"):
                return name
        return ""

    @staticmethod
    def _missing_executable(error: str) -> str:
        """Extract the missing executable/command from an environment error."""
        text = str(error or "")
        patterns = (
            r"(?i)['\"`]([^'\"`]+)['\"`]\s+is not recognized",
            r"(?i)^\s*([^\s'\":]+)\s+is not recognized",
            r"(?im)(?:^|[\s:])([^\s:]+):\s*command not found\b",
            r"(?im)(?:^|[\s:])([^\s:]+):\s*not found\b",
            r"(?i)command not found:\s*([^\s'\"`]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                name = match.group(1).strip()
                if name:
                    return name
        return ""

    @staticmethod
    def _normalize_package_token(value: str) -> str:
        """Casefold a package token and treat ``-``, ``_``, and ``.`` alike."""
        return re.sub(r"[-_.]+", "-", str(value or "").casefold()).strip("-/ ")

    @staticmethod
    def _tool_payload_text(execution: ToolExecution) -> str:
        if isinstance(execution.args, dict):
            return " ".join(
                str(value) for value in execution.args.values()
                if isinstance(value, (str, int, float))
            )
        return str(execution.args or "")

    @staticmethod
    def _is_dependency_manifest(execution: ToolExecution) -> bool:
        path = TranscriptAnalyzer._target_path(execution)
        name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if name.startswith("requirements") and name.endswith(".txt"):
            return True
        return name in {
            "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "package.json",
            "environment.yml", "environment.yaml",
        }

    def _dependency_recovery_matches(self, module: str, success: ToolExecution) -> bool:
        token = self._normalize_package_token(module)
        if not token:
            return False
        if success.name in self.COMMAND_TOOLS:
            command = success.command()
            if self._is_install_command(command) and token in self._normalize_package_token(command):
                return True
        # Declaring the dependency in a manifest is also a direct, causal fix
        # (e.g. write_file adding it to requirements.txt). Unrelated tools such
        # as search_web or curl never qualify.
        if success.name in self.FILE_WRITE_TOOLS and self._is_dependency_manifest(success):
            payload = self._normalize_package_token(self._tool_payload_text(success))
            return token in payload
        return False

    @staticmethod
    def _first_command_token(command: str) -> str:
        """Return the executable token after stripping known shell wrappers.

        Only leading wrappers and ``VAR=value`` assignments are removed. The
        first remaining token is the executable; arbitrary later tokens are
        never scanned, so ``echo agy`` or ``curl ... agy`` cannot look like an
        invocation of ``agy``.
        """
        tokens = re.findall(r"[^\s'\"]+|\"[^\"]*\"|'[^']*'", command or "")
        wrappers = {"sudo", "env", "nohup", "time", "command", "exec"}
        index = 0
        while index < len(tokens):
            token = tokens[index].strip("\"'")
            if not token:
                index += 1
                continue
            lowered = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if lowered.endswith(".exe"):
                lowered = lowered[:-4]
            # Leading environment assignments, e.g. `FOO=1 agy`.
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                index += 1
                continue
            if lowered in wrappers:
                index += 1
                continue
            if lowered in ("cmd", "powershell", "pwsh"):
                if index + 1 < len(tokens):
                    flag = tokens[index + 1].strip("\"'").casefold()
                    if (lowered == "cmd" and flag == "/c") or (
                        lowered in ("powershell", "pwsh") and flag in ("-command", "-c")
                    ):
                        index += 2
                        # A quoted payload such as `powershell -Command "agy --version"`
                        # still starts with the real executable inside the quotes.
                        if index < len(tokens) and tokens[index][:1] in ("'", '"'):
                            return TranscriptAnalyzer._first_command_token(
                                tokens[index].strip("\"'")
                            )
                        continue
                return lowered
            return token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        return ""

    def _environment_recovery_matches(self, executable: str, success: ToolExecution) -> bool:
        if success.name not in self.COMMAND_TOOLS:
            return False
        command = success.command()
        if not command:
            return False
        name = str(executable).replace("\\", "/").rsplit("/", 1)[-1].casefold()
        if not name:
            return False
        if self._is_install_command(command):
            token = self._normalize_package_token(name)
            if token and token in self._normalize_package_token(command):
                return True
        # Invoking exactly the missing executable is the other causal fix.
        return self._first_command_token(command) == name

    def _unresolved_event(self, failed: ToolExecution, conversation_id: str) -> ContrastiveEvent | None:
        if self._is_self_referential(failed.command(), failed.error or "", failed.args):
            return None
        error = failed.error or "tool failed without a recoverable output"
        return ContrastiveEvent(
            trigger_tool=failed.name,
            failed_args=failed.args,
            error_message=error,
            successful_args=None,
            resolution_summary=f"`{failed.name}` failed with `{truncate(error, 200)}`; no verified recovery was observed.",
            category=self._categorize(failed, error),
            verified=False,
            confidence=0.2,
            error_signature=failed.error_signature or self._signature(error),
            session_id=conversation_id,
            scope="workspace",
        )

    def _formulate_contrastive_event(
        self,
        failed: ToolExecution,
        recovery: ToolExecution,
        conversation_id: str,
    ) -> ContrastiveEvent | None:
        error = failed.error or "unknown error"
        failed_command = failed.command()
        recovery_command = recovery.command()
        if self._is_self_referential(
            failed_command, recovery_command, error, failed.args, recovery.args
        ):
            return None
        category = self._categorize(failed, error)
        unchanged = bool(failed_command) and failed_command == recovery_command

        if category == "dependency" and recovery_command:
            if self._is_install_command(recovery_command):
                summary = (
                    f"Installing `{truncate(recovery_command, 140)}` resolved "
                    f"`{truncate(error, 140)}`."
                )
            elif recovery.name in self.FILE_WRITE_TOOLS and self._is_dependency_manifest(recovery):
                summary = (
                    f"Declaring the missing dependency in `{truncate(recovery_command, 140)}` "
                    f"resolved `{truncate(error, 140)}`."
                )
            else:
                summary = (
                    f"Invoking `{truncate(recovery_command, 140)}` after the dependency "
                    f"failure resolved `{truncate(error, 140)}`."
                )
        elif failed_command and recovery_command and not unchanged:
            summary = (
                f"`{truncate(failed_command, 120)}` failed with `{truncate(error, 160)}`. "
                f"Verified fix: use `{truncate(recovery_command, 140)}`."
            )
        elif failed_command and unchanged:
            summary = (
                f"`{truncate(failed_command, 140)}` failed with `{truncate(error, 160)}`; "
                "retrying succeeded, which points to a transient or environment-dependent failure."
            )
        elif failed.name != recovery.name:
            summary = (
                f"`{failed.name}` failed with `{truncate(error, 160)}`; "
                f"`{recovery.name}` then succeeded."
            )
        else:
            summary = (
                f"`{failed.name}` failed with `{truncate(error, 160)}`; "
                "the next verified attempt succeeded."
            )

        summary = truncate(redact(one_line(summary, 400)), 320)
        if len(summary) < 20:
            return None
        confidence = 0.9
        if unchanged:
            confidence = 0.7 if self._is_transient(error) else 0.55
        if not recovery.has_evidence:
            confidence = min(confidence, 0.6)

        return ContrastiveEvent(
            trigger_tool=failed.name,
            failed_args=failed.args,
            error_message=error,
            successful_args=recovery.args,
            resolution_summary=summary,
            category=category,
            verified=True,
            confidence=confidence,
            error_signature=failed.error_signature or self._signature(error),
            recovery_tool=recovery.name,
            session_id=conversation_id,
            scope="workspace",
        )

    def _categorize(self, execution: ToolExecution, error: str) -> str:
        text = f"{execution.name} {error}".lower()
        if any(token in text for token in ("modulenotfound", "no module named", "cannot find module", "importerror")):
            return "dependency"
        if any(token in text for token in ("is not recognized", "command not found", "no such file", "access is denied", "permission denied", "eacces", "enoent", "not found")):
            return "environment"
        if any(token in text for token in ("syntaxerror", "unexpected token", "parse error", "invalid syntax")):
            return "syntax"
        if any(token in text for token in ("connection refused", "econnrefused", "econnreset", "timeout", "timed out", "could not resolve", "network is unreachable", "socket")):
            return "network"
        if any(token in text for token in ("401", "403", "unauthorized", "forbidden", "authentication", "invalid token")):
            return "auth"
        if any(token in text for token in ("permission", "convention", "style", "lint")):
            return "convention"
        return "command"

    def _is_transient(self, error: str) -> bool:
        lowered = error.lower()
        return any(token in lowered for token in self.TRANSIENT_ERRORS)

    # --------------------------------------------------------------- constraints
    def _clean_user_text(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        text = re.sub(r"<USER_REQUEST>|</USER_REQUEST>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"The current local time is:.*", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", redact(text)).strip()

    def _extract_user_constraints(self, text: str, constraints: list[str]) -> None:
        patterns = [
            r"(?:please\s+)?always\s+(.+?)(?:\.|$)",
            r"(?:please\s+)?never\s+(.+?)(?:\.|$)",
            r"(?:don't|do not)\s+(.+?)(?:\.|$)",
            r"from now on,?\s+(.+?)(?:\.|$)",
            r"prefer\s+(.+?)(?:\.|$)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = one_line(match.group(1), 200).strip(" .,-")
                if not candidate:
                    continue
                prefix = ""
                lowered = re.sub(r"^please\s+", "", match.group(0).lower().strip())
                if lowered.startswith("never") or "do not" in lowered or "don't" in lowered:
                    prefix = "Never "
                elif lowered.startswith("prefer"):
                    prefix = "Prefer "
                elif lowered.startswith("always"):
                    prefix = "Always "
                elif lowered.startswith("from now on"):
                    prefix = ""
                result = redact((prefix + candidate).strip())
                if (
                    10 <= len(result) <= 240
                    and not self._is_low_signal_constraint(result)
                    and not self._is_self_referential(result)
                ):
                    if result not in constraints:
                        constraints.append(result)

    @staticmethod
    def _is_low_signal_constraint(text: str) -> bool:
        lowered = text.lower()
        noise = (
            "worry about license", "private use", "be nice", "do your best",
            "keep going", "continue", "thanks", "thank you",
        )
        return any(item in lowered for item in noise)

    # ----------------------------------------------------------------- workflows
    def _extract_procedural_workflows(
        self,
        tasks: list[str],
        executions: list[ToolExecution],
        contrasts: list[ContrastiveEvent],
        workspace: str | Path | None = None,
        conversation_id: str = "",
        outcome: str = "completed",
    ) -> list[WorkflowCandidate]:
        successful = [
            execution for execution in executions
            if execution.success and not execution.error and execution.name
        ]
        if len(successful) < 2:
            return []

        actions: list[str] = []
        tools: set[str] = set()
        verification: list[str] = []
        seen_actions: set[str] = set()

        for execution in successful:
            tools.add(execution.name)
            action = self._action_description(execution)
            if not action:
                continue
            key = normalize_for_compare(execution.command() or action, keep_code=True)
            if key in seen_actions:
                continue
            seen_actions.add(key)
            actions.append(action)
            if self.VERIFICATION_COMMAND.search(execution.command()) and not self._is_install_command(execution.command()):
                verification.append(action)

        if not actions or not any("`" in action for action in actions):
            return []
        if len(actions) < 2:
            return []
        # A single exploratory pair is not a procedure. Require either a real
        # command sequence or a verified recovery that makes the sequence
        # meaningfully repeatable.
        command_actions = [action for action in actions if action.startswith("Run `")]
        if len(command_actions) < 1 and not contrasts:
            return []
        if len(actions) < 3 and not (contrasts or verification):
            return []

        task = tasks[0] if tasks else "repeatable development workflow"
        task = one_line(task, 240)
        if not self._is_actionable_goal(task):
            return []
        name = self._derive_skill_name(task, successful)
        if not name:
            return []
        dead_ends = [
            f"`{truncate(str(event.failed_args), 90)}` failed: {truncate(event.error_message, 120)}"
            for event in contrasts[:6]
        ]
        keywords = self._trigger_keywords(task, successful, workspace)
        description = self._describe_workflow(task, name, keywords)

        base_score = 0.35 + min(len(actions), 8) * 0.04 + min(len(tools), 4) * 0.04
        if verification:
            base_score += 0.1
        if contrasts:
            base_score += 0.12
        if outcome != "completed":
            base_score -= 0.2
        quality = round(max(0.0, min(1.0, base_score)), 2)

        return [
            WorkflowCandidate(
                name=name,
                description=description,
                goal=task,
                steps=actions[:12],
                tools_involved=sorted(tools),
                verification_steps=verification[:6],
                dead_ends=dead_ends,
                trigger_keywords=keywords,
                confidence=quality,
                quality_score=quality,
                scope="workspace" if workspace else "global",
                session_id=conversation_id,
                success=outcome == "completed",
                tags=["auto-synthesized", "verified", "harness"],
            )
        ]

    def _action_description(self, execution: ToolExecution) -> str:
        command = execution.command()
        if execution.name in ("run_command", "run_terminal_command", "shell", "bash"):
            if not command or self._is_exploratory_command(command):
                return ""
            summary = ""
            if isinstance(execution.args, dict):
                summary = str(execution.args.get("toolSummary") or "").strip()
            suffix = f" ({one_line(summary, 80)})" if summary else ""
            return f"Run `{truncate(redact(command), 180)}`{suffix}"
        if execution.name in ("write_file", "edit_file", "apply_patch", "replace_file_content"):
            path = Path(command).name if command else "the target file"
            return f"Create or update `{truncate(redact(path), 80)}`"
        if execution.name in ("browser_navigate", "browser_action", "read_url", "open_url"):
            return f"Open or interact with `{truncate(redact(command), 120)}`"
        if execution.name in ("view_file", "read_file", "list_dir", "grep_search", "search_web"):
            return ""
        if command:
            return f"Use `{execution.name}` with `{truncate(redact(command), 100)}`"
        return ""

    def _is_exploratory_command(self, command: str) -> bool:
        return any(re.search(pattern, command, re.IGNORECASE) for pattern in self.EXPLORATORY_COMMANDS)

    @staticmethod
    def _is_install_command(command: str) -> bool:
        return bool(
            re.search(
                r"(?i)(?:^|\s)(?:pip(?:3)?\s+install|python\s+-m\s+pip\s+install|"
                r"npm\s+(?:install|i|add)|pnpm\s+(?:install|add)|yarn\s+add|"
                r"uv\s+(?:add|pip\s+install)|poetry\s+add|pipenv\s+install|"
                r"apt(?:-get)?\s+install|brew\s+install|"
                r"winget\s+install|choco\s+install|scoop\s+install)\b",
                command or "",
            )
        )

    @staticmethod
    def _is_actionable_goal(text: str) -> bool:
        """Return True for a concrete task, not a question or exploration."""
        clean = one_line(text or "", 400)
        if not clean or "?" in clean:
            return False
        lowered = clean.lower()
        first_word = re.match(r"^[^a-z0-9]*([a-z]+)", lowered)
        question_words = {
            "is", "are", "why", "how", "what", "when", "where", "who",
            "can", "could", "should", "does", "do", "would",
        }
        if first_word and first_word.group(1) in question_words:
            return False
        if any(phrase in lowered for phrase in ("research", "look up", "find out", "explain", "compare")):
            return False
        return len(clean.split()) <= 16

    def _derive_skill_name(self, task: str, executions: list[ToolExecution]) -> str:
        text = f"{task} " + " ".join(execution.command() for execution in executions)
        lowered = text.lower()
        rules = [
            (("alembic", "migration", "prisma migrate", "flyway", "liquibase"), "database-migrations"),
            (("pytest", "unittest", "python -m test"), "run-python-tests"),
            (("playwright", "browser test", "e2e"), "browser-e2e-testing"),
            (("docker compose", "docker-compose", "docker build"), "docker-compose-workflow"),
            (("cloudflare", "wrangler"), "deploy-cloudflare-pages"),
            (("vercel", "netlify"), "deploy-web-app"),
            (("vite", "next build", "npm run build", "pnpm build", "yarn build"), "build-frontend-app"),
            (("fastapi", "flask", "uvicorn"), "python-web-service"),
            (("terraform", "pulumi"), "infrastructure-provisioning"),
            (("kubectl", "helm"), "kubernetes-deployment"),
            (("npm install", "pnpm add", "yarn add"), "install-node-dependencies"),
            (("uv ", "pip install", "poetry add"), "manage-python-dependencies"),
        ]
        for signals, name in rules:
            if any(signal in lowered for signal in signals):
                return name
        return ""

    def _trigger_keywords(
        self,
        task: str,
        executions: list[ToolExecution],
        workspace: str | Path | None,
    ) -> list[str]:
        keywords: list[str] = []
        for token in tokenize(task):
            if token not in keywords:
                keywords.append(token)
        for execution in executions:
            command = execution.command()
            for token in tokenize(command):
                if token not in keywords and token not in ("run", "command", "npx", "sudo"):
                    keywords.append(token)
        for signal in detect_project_signals(workspace):
            if signal not in keywords:
                keywords.append(signal)
        return keywords[:14]

    @staticmethod
    def _describe_workflow(task: str, name: str, keywords: list[str]) -> str:
        trigger = ", ".join(keywords[:8]) if keywords else name.replace("-", " ")
        goal = truncate(task, 160).rstrip(".")
        description = (
            f"Use when the task involves {trigger}. "
            f"Repeatable, verified procedure for: {goal}."
        )
        return truncate(redact(description), 500)

    # ------------------------------------------------------------------- outcome
    def _determine_outcome(
        self,
        executions: list[ToolExecution],
        assistant: list[str],
        unresolved: list[ContrastiveEvent],
    ) -> str:
        if not executions:
            return "completed"
        if unresolved:
            return "partial"
        last_text = (assistant[-1] if assistant else "").lower()
        if any(phrase in last_text for phrase in ("failed to", "could not complete", "unable to", "i couldn't", "not able to")):
            return "failed"
        last_error = max((index for index, execution in enumerate(executions) if execution.error), default=-1)
        last_success = max((index for index, execution in enumerate(executions) if execution.success), default=-1)
        if last_error > last_success:
            return "partial"
        return "completed"

    @staticmethod
    def _dedupe_texts(items: list[str]) -> list[str]:
        result: list[str] = []
        for item in items:
            clean = one_line(item, 400)
            if not clean:
                continue
            if any(jaccard(clean, previous) > 0.9 for previous in result):
                continue
            result.append(clean)
        return result
