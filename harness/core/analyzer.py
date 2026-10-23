"""
Transcript and session analyzer for Harness².
Extracts contrastive failure/recovery pairs, procedural workflows, and user constraints.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ContrastiveEvent:
    """Represents a failure followed by a successful resolution (Harness² contrastive unit)."""
    trigger_tool: str
    failed_args: dict[str, Any] | str
    error_message: str
    successful_args: dict[str, Any] | str | None
    resolution_summary: str
    category: str  # 'environment', 'command', 'dependency', 'syntax', 'convention'


@dataclass
class WorkflowCandidate:
    """A multi-step procedural workflow suitable for synthesis into a SKILL.md."""
    name: str
    description: str
    goal: str
    steps: list[str]
    tools_involved: list[str]
    code_snippets: list[dict[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SessionAnalysis:
    """Structured extraction of insights from a session transcript."""
    conversation_id: str
    tasks: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    contrastive_events: list[ContrastiveEvent] = field(default_factory=list)
    workflows: list[WorkflowCandidate] = field(default_factory=list)
    user_constraints: list[str] = field(default_factory=list)
    environment_quirks: list[str] = field(default_factory=list)
    final_outcome: str = "completed"


class TranscriptAnalyzer:
    """Parses Antigravity transcript files to extract actionable harness learning."""

    def __init__(self, transcript_path: Path | str | None = None):
        self.transcript_path = Path(transcript_path) if transcript_path else None

    def parse_transcript(self, raw_lines: list[str] | None = None) -> list[dict[str, Any]]:
        """Parses lines of JSONL into structured step objects."""
        steps = []
        if raw_lines is None:
            if not self.transcript_path or not self.transcript_path.exists():
                return []
            with open(self.transcript_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        else:
            lines = raw_lines

        for idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                steps.append(data)
            except json.JSONDecodeError:
                continue
        return steps

    def analyze(self, steps: list[dict[str, Any]], conversation_id: str = "") -> SessionAnalysis:
        """Performs Harness² diagnosis on the sequence of steps."""
        analysis = SessionAnalysis(conversation_id=conversation_id)

        user_prompts: list[str] = []
        tool_records: list[dict[str, Any]] = []
        recent_failed_tool: dict[str, Any] | None = None

        for step in steps:
            step_type = step.get("type", "")
            source = step.get("source", "")
            content = step.get("content", "")

            # 1. Capture User Requests & Standing Constraints
            if step_type == "USER_INPUT" or source == "USER_EXPLICIT":
                if content:
                    user_prompts.append(content)
                    self._extract_user_constraints(content, analysis.user_constraints)

            # 2. Capture Tool Calls & Results
            tool_calls = step.get("tool_calls", [])
            status = step.get("status", "")

            if tool_calls:
                for tc in tool_calls:
                    name = tc.get("name") or tc.get("toolName", "unknown")
                    args = tc.get("arguments") or tc.get("Arguments", {})
                    record = {
                        "name": name,
                        "args": args,
                        "step_index": step.get("step_index", len(tool_records)),
                        "content": content,
                        "status": status,
                    }
                    tool_records.append(record)

            # Check for tool errors in content or status
            error_indicator = self._find_error_in_step(step)
            if error_indicator:
                if tool_records:
                    recent_failed_tool = {
                        "record": tool_records[-1],
                        "error": error_indicator,
                    }
            elif recent_failed_tool and tool_records:
                # Potential recovery detected! (Contrastive Propose-Probe pair)
                last_call = tool_records[-1]
                if last_call != recent_failed_tool["record"]:
                    event = self._formulate_contrastive_event(recent_failed_tool, last_call)
                    if event:
                        analysis.contrastive_events.append(event)
                        # Also check if it indicates an environment quirk
                        if event.category in ("environment", "command"):
                            analysis.environment_quirks.append(event.resolution_summary)
                    recent_failed_tool = None

        analysis.tasks = user_prompts
        analysis.tool_calls = tool_records

        # 3. Detect Reusable Multi-Step Workflows
        workflows = self._extract_procedural_workflows(user_prompts, tool_records)
        analysis.workflows.extend(workflows)

        return analysis

    def _find_error_in_step(self, step: dict[str, Any]) -> str | None:
        """Detects if a step contains a failure, command error, or traceback."""
        if step.get("status") in ("ERROR", "FAILED"):
            return step.get("content", "Unknown error")

        content = step.get("content", "")
        if not isinstance(content, str):
            return None

        patterns = [
            r"command exited with code [1-9]\d*",
            r"Traceback \(most recent call last\):",
            r"error: command .* failed",
            r"SyntaxError:",
            r"ModuleNotFoundError: No module named",
            r"FileNotFoundError:",
            r"The term '.*' is not recognized",
            r"fatal: not a git repository",
            r"Access is denied",
        ]
        for pat in patterns:
            match = re.search(pat, content, re.IGNORECASE)
            if match:
                # Return snippet around error
                start = max(0, match.start() - 50)
                end = min(len(content), match.end() + 150)
                return content[start:end].strip()

        return None

    def _formulate_contrastive_event(
        self, failed_info: dict[str, Any], recovery_info: dict[str, Any]
    ) -> ContrastiveEvent | None:
        """Forms a contrastive event comparing failed attempt to successful recovery."""
        failed_rec = failed_info["record"]
        error_msg = failed_info["error"]
        tool_name = failed_rec["name"]

        category = "command"
        if "is not recognized" in error_msg or "not found" in error_msg:
            category = "environment"
        elif "ModuleNotFoundError" in error_msg or "cannot import" in error_msg:
            category = "dependency"
        elif "SyntaxError" in error_msg:
            category = "syntax"
        elif "Access is denied" in error_msg or "permission" in error_msg:
            category = "convention"

        # Generate rule-oriented resolution summary
        cmd_prev = failed_rec.get("args", {})
        cmd_curr = recovery_info.get("args", {})

        summary = f"When using '{tool_name}' and encountering '{error_msg[:80]}...', succeeded by adjusting parameters/context."
        if isinstance(cmd_prev, dict) and isinstance(cmd_curr, dict):
            prev_line = cmd_prev.get("CommandLine") or str(cmd_prev)
            curr_line = cmd_curr.get("CommandLine") or str(cmd_curr)
            if prev_line != curr_line:
                summary = f"Initial command `{prev_line[:60]}` failed with `{error_msg[:60]}`. Resolved by using `{curr_line[:60]}`."

        return ContrastiveEvent(
            trigger_tool=tool_name,
            failed_args=failed_rec.get("args", {}),
            error_message=error_msg,
            successful_args=recovery_info.get("args", {}),
            resolution_summary=summary,
            category=category,
        )

    def _extract_user_constraints(self, text: str, constraints: list[str]) -> None:
        """Detects explicit standing directives or user corrections."""
        patterns = [
            r"(?:please\s+)?always\s+(.+?)(?:\.|$)",
            r"(?:please\s+)?never\s+(.+?)(?:\.|$)",
            r"(?:don't|do not)\s+(.+?)(?:\.|$)",
            r"make sure to\s+(.+?)(?:\.|$)",
            r"remember to\s+(.+?)(?:\.|$)",
            r"from now on,?\s+(.+?)(?:\.|$)",
        ]
        for pat in patterns:
            for match in re.finditer(pat, text, re.IGNORECASE):
                candidate = match.group(0).strip().rstrip(".")
                if len(candidate) > 10 and len(candidate) < 200:
                    if candidate not in constraints:
                        constraints.append(candidate)

    def _extract_procedural_workflows(
        self, tasks: list[str], tools: list[dict[str, Any]]
    ) -> list[WorkflowCandidate]:
        """Identifies cohesive sequences of commands/file edits that represent a reusable skill."""
        workflows = []
        if len(tools) < 2:
            return workflows

        # Check for command chains or build/test/install procedures
        command_steps: list[str] = []
        tools_involved: set[str] = set()

        for t in tools:
            name = t.get("name", "")
            args = t.get("args", {})
            tools_involved.add(name)

            if name == "run_command" and isinstance(args, dict):
                cmd = args.get("CommandLine", "").strip()
                summary = args.get("toolSummary", "")
                if cmd:
                    desc = f"Execute: `{cmd}`" + (f" ({summary})" if summary else "")
                    command_steps.append(desc)
            elif name in ("write_file", "edit_file") and isinstance(args, dict):
                path = args.get("Path", "")
                if path:
                    pname = Path(path).name
                    command_steps.append(f"Configure/Update `{pname}`")

        # If a coherent task prompt exists and there were meaningful steps
        if command_steps and tasks:
            main_task = tasks[0].strip().replace("\n", " ")
            # Shorten task into a skill name
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", main_task[:40].lower()).strip("-")
            if not slug or len(slug) < 3:
                slug = "task-procedure"

            workflows.append(
                WorkflowCandidate(
                    name=slug,
                    description=f"Automated workflow synthesized from: {main_task[:120]}",
                    goal=main_task,
                    steps=command_steps[:10],  # Cap steps to key procedure
                    tools_involved=list(tools_involved),
                    tags=["auto-synthesized", "harness-learned"],
                )
            )

        return workflows
