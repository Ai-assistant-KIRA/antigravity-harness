"""
Skill synthesis, refinement, validation, and installation for Harness².

A skill is only generated when the observed procedure is substantial enough to
be worth remembering. Updates are bounded and idempotent: the generated body
lives inside a managed block, while anything the user adds outside that block is
preserved. A manually authored skill with the same name is never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .analyzer import MAX_GOAL_WORDS, ContrastiveEvent, WorkflowCandidate
from .config import (
    get_global_user_skills_dir,
    get_plugin_dir,
    get_primary_skills_dir,
    get_skills_dirs,
    get_workspace_skills_dir,
    slugify,
)
from .redaction import contains_secret, redact, redact_strict
from .trust import Finding, is_safe_to_install, scan_skill, scan_text
from .util import (
    atomic_write_text,
    jaccard,
    normalize_for_compare,
    one_line,
    read_text_lossless,
    short_goal,
    strip_paths,
    truncate,
    utc_iso,
)

SKILL_START = "<!-- HARNESS:SKILL_START -->"
SKILL_END = "<!-- HARNESS:SKILL_END -->"
SKILL_RE = re.compile(re.escape(SKILL_START) + r".*?" + re.escape(SKILL_END), re.DOTALL)
SKILL_START_LINE_RE = re.compile(
    r"(?m)^[ \t]*" + re.escape(SKILL_START) + r"[ \t]*$"
)
META_RE = re.compile(r"<!--\s*harness-meta:\s*(\{.*?\})\s*-->", re.DOTALL)

QUALITY_THRESHOLD = 0.55
MAX_STEPS = 15
MAX_PITFALLS = 8
MAX_SKILL_BYTES = 30_000
MAX_INSTALL_BYTES = 5_000_000
MAX_SKILL_FILES = 500
MAX_DOWNLOAD_BYTES = 50_000_000
MAX_EXTRACTED_BYTES = 100_000_000
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 10_000_000
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
JUNK_NAMES = {"workflow", "task", "project", "harness-workflow", "auto-skill", "procedure"}

_QUESTION_STARTS = (
    "is ",
    "are ",
    "why ",
    "how ",
    "what ",
    "when ",
    "where ",
    "who ",
    "can ",
    "could ",
    "should ",
    "does ",
    "do ",
    "would ",
)
_EXPLORATORY_PHRASES = ("research", "look up", "find out", "explain", "compare", "investigate")
_GOAL_PREFIX_RE = re.compile(r"^[\s#>*_`~\"'\-+=\[\(\{<|:;,.!]+")

_COPY_FILES = {"SKILL.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md", "README.md"}
_COPY_DIRS = {"references", "reference", "scripts", "assets", "examples", "templates"}


def _is_actionable_goal(text: str) -> bool:
    """A synthesized skill must encode something a user can *do*, not a prompt."""
    value = _GOAL_PREFIX_RE.sub("", (text or "").strip()).strip()
    if len(value) < 4:
        return False
    lowered = value.casefold()
    if "?" in value or lowered.startswith(_QUESTION_STARTS):
        return False
    if any(phrase in lowered for phrase in _EXPLORATORY_PHRASES):
        return False
    return len(value.split()) <= MAX_GOAL_WORDS


def _is_self_referential(*values: object) -> bool:
    """True when any text points at this plugin's own install tree."""
    plugin = re.sub(
        r"/{2,}", "/", str(get_plugin_dir()).replace("\\", "/").casefold()
    ).rstrip("/")
    needles = {".gemini/config/plugins/harness"}
    if plugin:
        needles.add(plugin)
    for value in values:
        text = re.sub(
            r"/{2,}", "/", str(value or "").replace("\\", "/").casefold()
        )
        if any(needle in text for needle in needles if needle):
            return True
    return False


class SkillError(RuntimeError):
    """Base class for skill builder failures."""


class SkillQualityError(SkillError):
    """The candidate workflow does not meet the synthesis bar."""


class SkillConflictError(SkillError):
    """A manually authored skill already owns this name."""


class SkillValidationError(SkillError):
    """An installed or generated SKILL.md is invalid."""


@dataclass
class SkillBuildResult:
    name: str
    path: Path
    status: str  # created, updated, unchanged, skipped
    quality: float = 0.0
    description: str = ""
    reason: str = ""


@dataclass
class SkillInstallResult:
    name: str
    path: Path
    status: str  # installed, unchanged, replaced
    source: str
    warnings: list[str] = field(default_factory=list)


class SkillBuilder:
    """Creates, refines, validates, and installs Antigravity skills."""

    def __init__(
        self,
        workspace_path: Path | str | None = None,
        skills_dir: Path | str | None = None,
    ):
        self.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
        self.skills_dirs = get_skills_dirs(self.workspace_path)
        self.primary_skills_dir = (
            Path(skills_dir).expanduser()
            if skills_dir
            else get_primary_skills_dir(self.workspace_path)
        )

    @staticmethod
    def _untrusted_findings(text: str, source: str = "") -> list[tuple[Finding, str]]:
        """High/medium findings with a consistent, user-facing prefix."""
        findings = []
        for finding in scan_text(text, source=source):
            if finding.severity not in ("high", "medium"):
                continue
            label = (
                f"untrusted content: {finding.category.replace('_', ' ')}: "
                f"{finding.message} (line {finding.line})"
            )
            findings.append((finding, label))
        return findings

    def _assert_generated_skill_safe(self, text: str, skill_file: Path) -> None:
        high = [
            finding
            for finding, _label in self._untrusted_findings(text, str(skill_file))
            if finding.severity == "high"
        ]
        if high:
            finding = high[0]
            raise SkillQualityError(
                "generated skill contains high-severity untrusted content: "
                f"{finding.category.replace('_', ' ')} at line {finding.line}"
            )

    # --------------------------------------------------------------- synthesis
    def build_skill(
        self,
        workflow: WorkflowCandidate,
        related_events: Sequence[ContrastiveEvent] = (),
        target_dir: Path | str | None = None,
        force: bool = False,
        source: str = "auto",
    ) -> SkillBuildResult:
        quality = self._quality(workflow, related_events)
        quality = self._validate_workflow(workflow, quality, force=force)
        name = self._skill_name(workflow.name)
        description = self._description(workflow)
        destination = Path(target_dir).expanduser() if target_dir else self.primary_skills_dir
        folder = destination / name
        skill_file = folder / "SKILL.md"

        existing_text = ""
        encoding = "utf-8"
        newline = "\n"
        if skill_file.exists():
            try:
                existing_text, encoding, newline = read_text_lossless(skill_file)
            except (OSError, UnicodeError) as exc:
                raise SkillError(
                    f"cannot read existing skill safely (non-UTF-8 or unreadable): {exc}"
                ) from exc
            if not self._is_harness_managed(existing_text):
                raise SkillConflictError(
                    f"'{name}' is a manually authored skill at {skill_file}; refusing to overwrite it"
                )

        pitfalls = self._event_pitfalls(related_events)
        if _is_self_referential(*pitfalls):
            raise SkillQualityError("skill pitfalls reference the Harness plugin install")

        if not existing_text:
            content = self._render(
                name=name,
                description=description,
                workflow=workflow,
                steps=self._dedupe_steps(workflow.steps)[:MAX_STEPS],
                pitfalls=pitfalls,
                quality=quality,
                version=1,
            )
            self._assert_generated_skill_safe(content, skill_file)
            folder.mkdir(parents=True, exist_ok=True)
            atomic_write_text(skill_file, content, encoding=encoding, newline=newline)
            return SkillBuildResult(name, skill_file, "created", quality, description)

        existing_meta = self._metadata(existing_text)
        existing_steps = self._parse_steps(existing_text)
        existing_pitfalls = [
            pitfall
            for pitfall in self._parse_pitfalls(existing_text)
            if not _is_self_referential(pitfall)
        ]
        steps = self._merge_bounded(
            existing_steps, workflow.steps, limit=MAX_STEPS, keep_head=3
        )
        merged_pitfalls = self._merge_pitfalls(existing_pitfalls, pitfalls)
        version = int(existing_meta.get("version") or 1) + 1
        content = self._render(
            name=name,
            description=description,
            workflow=workflow,
            steps=steps,
            pitfalls=merged_pitfalls,
            quality=max(quality, float(existing_meta.get("quality") or 0)),
            version=version,
            user_tail=self._user_tail(existing_text),
        )
        if self._semantic_normalize(content) == self._semantic_normalize(existing_text):
            return SkillBuildResult(
                name, skill_file, "unchanged", quality, description, "already up to date"
            )
        self._assert_generated_skill_safe(content, skill_file)
        atomic_write_text(skill_file, content, encoding=encoding, newline=newline)
        return SkillBuildResult(name, skill_file, "updated", quality, description)

    @staticmethod
    def _semantic_normalize(text: str) -> str:
        """Ignore volatile metadata when deciding whether a skill changed."""
        value = (text or "").replace("\r\n", "\n")
        value = re.sub(r'"updated":\s*"[^"]*"\s*,?', "", value)
        value = re.sub(r'"version":\s*\d+\s*,?', "", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def build_or_update_skill(
        self,
        workflow: WorkflowCandidate,
        related_events: Sequence[ContrastiveEvent] = (),
        target_dir: Path | str | None = None,
        force: bool = False,
    ) -> Path:
        """Backward-compatible wrapper returning the SKILL.md path."""
        return self.build_skill(workflow, related_events, target_dir, force).path

    def _validate_workflow(
        self,
        workflow: WorkflowCandidate,
        quality: float,
        force: bool = False,
    ) -> float:
        name = self._skill_name(workflow.name)
        if name in JUNK_NAMES or len(name) < 4:
            raise SkillQualityError(f"skill name '{name}' is too generic")
        if not workflow.goal or len(workflow.goal.strip()) < 8:
            raise SkillQualityError("workflow goal is too short")
        if not _is_actionable_goal(workflow.goal):
            raise SkillQualityError("workflow goal is not an actionable procedure")
        if _is_self_referential(
            workflow.goal,
            *workflow.steps,
            *workflow.verification_steps,
            *workflow.dead_ends,
        ):
            raise SkillQualityError("workflow describes Harness plugin self-inspection")
        steps = self._dedupe_steps(workflow.steps)
        if len(steps) < 2:
            raise SkillQualityError("workflow needs at least two distinct steps")
        if len(steps) < 3 and not workflow.verification_steps:
            quality = round(max(0.0, quality - 0.1), 2)
        if not force:
            if not workflow.success:
                raise SkillQualityError("workflow did not complete successfully")
            if quality < QUALITY_THRESHOLD:
                raise SkillQualityError(
                    f"workflow quality {quality:.2f} is below the {QUALITY_THRESHOLD:.2f} threshold"
                )
        return quality

    def _quality(
        self,
        workflow: WorkflowCandidate,
        related_events: Sequence[ContrastiveEvent] = (),
    ) -> float:
        if workflow.quality_score and workflow.quality_score > 0:
            base = float(workflow.quality_score)
        else:
            steps = self._dedupe_steps(workflow.steps)
            base = 0.30
            base += min(len(steps), 8) * 0.05
            if any("`" in str(step) for step in steps):
                base += 0.08
            if workflow.verification_steps:
                base += 0.10
            if len(set(workflow.tools_involved)) >= 2:
                base += 0.07
            if workflow.trigger_keywords:
                base += 0.03
            if not workflow.success:
                base -= 0.25
        if related_events:
            base += 0.04
        if not _is_actionable_goal(workflow.goal):
            base = min(base, 0.30)
        return max(0.0, min(1.0, round(base, 2)))

    def _skill_name(self, value: str) -> str:
        raw = (value or "").strip()
        if "?" in raw or len(raw.split()) > 8:
            raise SkillQualityError(
                f"skill name '{one_line(raw, 80)}' is a raw question, not an identifier"
            )
        name = slugify(value or "harness-skill", 64).strip("-")
        if not name:
            name = "harness-skill"
        if not NAME_RE.match(name):
            name = re.sub(r"[^a-z0-9-]", "-", name.lower())
            name = re.sub(r"-{2,}", "-", name).strip("-")
        return name or "harness-skill"

    _GOAL_FILLER_RE = re.compile(
        r"(?i)^(?:(?:okay|ok|so|hey|hi|alright|now|well|please|pls|also|and)[\s,]+)*"
        r"(?:(?:i|we)\s+(?:want|need|would like|wanna)\s+(?:you\s+)?to\s+|"
        r"(?:can|could|would)\s+you\s+|let'?s\s+|i\s+have\s+(?:made|added)\s+|"
        r"i\s+(?:just\s+)?(?:added|made|did)\s+)?"
    )

    _CHATTY_GOAL_RE = re.compile(
        r"(?i)\b(?:i|i'm|i've|we|my|our|me|abit|like|want|need|looks?|kinda|maybe|"
        r"please|okay|ok|now|so)\b"
    )
    _FAMILY_PREFIXES = (
        "edit-video-", "edit-audio-", "run-node-task-", "run-python-task-",
        "deploy-cloudflare-", "browser-test-", "run-python-tests-", "powershell-",
    )

    def _clean_goal(self, goal: str) -> str:
        """The request without filler, paths, or redaction markers."""
        text = re.sub(r"\S*\[REDACTED\]\S*", " ", redact_strict(goal or ""))
        text = self._GOAL_FILLER_RE.sub("", strip_paths(text)).strip(" ,;:-")
        return one_line(text, 240)

    def _goal_is_chatty(self, goal: str) -> bool:
        words = goal.split()
        return len(words) > 18 or len(self._CHATTY_GOAL_RE.findall(goal)) >= 2

    @staticmethod
    def _procedure_summary(steps: Sequence[str], limit: int = 3) -> str:
        """Join the agent's own step summaries: "Inspect palette; Compare backgrounds"."""
        summaries: list[str] = []
        for step in steps:
            match = re.search(r"\(([^()]{6,80})\)\s*$", str(step))
            if not match:
                continue
            summary = match.group(1).strip()
            if summary and summary.casefold() not in {item.casefold() for item in summaries}:
                summaries.append(summary)
            if len(summaries) >= limit:
                break
        return "; ".join(summaries)

    def _title(self, workflow: WorkflowCandidate, name: str) -> str:
        goal = self._clean_goal(workflow.goal)
        if goal and not self._goal_is_chatty(goal):
            title = short_goal(goal, 90)
        else:
            stem = name
            for prefix in self._FAMILY_PREFIXES:
                if stem.startswith(prefix):
                    stem = stem[len(prefix):]
                    break
            title = stem.replace("-", " ")
        return title[:1].upper() + title[1:]

    def _description(self, workflow: WorkflowCandidate) -> str:
        """Say what the skill does and when to load it, without the raw prompt."""
        goal = self._clean_goal(workflow.goal)
        summary = self._procedure_summary(workflow.steps)
        if summary and (not goal or self._goal_is_chatty(goal)):
            # A chatty prompt describes a mood; the agent's step summaries
            # describe the procedure.
            goal = summary
        goal = short_goal(goal, 150) if goal != summary else truncate(goal, 160)
        goal = goal or workflow.name.replace("-", " ")
        goal = goal[:1].upper() + goal[1:]
        keywords = ", ".join(workflow.trigger_keywords[:6])
        description = f"{goal}."
        if keywords:
            description += f" Use when the task involves {keywords}."
        steps = len(self._dedupe_steps(workflow.steps))
        if steps:
            description += f" {steps} verified step(s)."
        return truncate(redact_strict(description), 420)

    # ---------------------------------------------------------------- rendering
    def _render(
        self,
        name: str,
        description: str,
        workflow: WorkflowCandidate,
        steps: Sequence[str],
        pitfalls: Sequence[str],
        quality: float,
        version: int,
        user_tail: str = "",
    ) -> str:
        title = self._title(workflow, name) or name.replace("-", " ")
        trigger = workflow.trigger_keywords[:8]
        matches = short_goal(self._clean_goal(workflow.goal), 180)
        metadata = {
            "version": version,
            "updated": utc_iso(),
            "source": "harness-auto",
            "quality": round(float(quality), 2),
            "tags": workflow.tags[:8],
        }
        lines = [
            "---",
            f"name: {name}",
            "description: >-",
            f"  {description}",
            "---",
            "",
            f"# {title[:1].upper() + title[1:]}",
            "",
            SKILL_START,
            f"<!-- harness-meta: {json.dumps(metadata, ensure_ascii=False, sort_keys=True)} -->",
            "> Learned and maintained by Harness². Edit only the notes below the managed block.",
            "",
            "## When to Use",
        ]
        if matches:
            lines.append(f"- The task matches: {matches}")
        if trigger:
            lines.append(f"- Relevant signals: {', '.join(trigger)}")
        if workflow.tools_involved:
            lines.append(
                "- Tools normally involved: "
                + ", ".join(f"`{tool}`" for tool in workflow.tools_involved[:6])
            )
        lines.extend(["", "## Procedure"])
        for index, step in enumerate(steps, 1):
            lines.append(f"{index}. {one_line(str(step), 260)}")

        lines.extend(["", "## Verification"])
        verification = [
            one_line(step, 220) for step in (workflow.verification_steps or [])
        ]
        if not verification:
            verification = [
                "Re-run the commands above and require a successful exit status.",
                "Run the project's test, lint, or build commands when they exist.",
            ]
        for step in verification[:6]:
            lines.append(f"- {step}")

        if pitfalls:
            lines.extend(["", "## Known Pitfalls"])
            for pitfall in pitfalls[:MAX_PITFALLS]:
                lines.append(f"- {pitfall}")
        lines.append("")
        lines.append(SKILL_END)
        # Last line of defence: a generated skill is a project file that is
        # often committed. No credential may reach it, whatever its shape.
        lines = [redact_strict(line) for line in lines]
        body = "\n".join(lines).strip() + "\n"
        if user_tail.strip():
            body += "\n" + user_tail.strip() + "\n"
        if len(body.encode("utf-8")) > MAX_SKILL_BYTES:
            raw = body.encode("utf-8")[: MAX_SKILL_BYTES - 200].decode("utf-8", errors="ignore")
            body = raw.rstrip() + "\n\n" + SKILL_END + "\n"
        return body

    @staticmethod
    def _normalize(text: str) -> str:
        return (text or "").replace("\r\n", "\n").strip()

    def _dedupe_steps(self, steps: Iterable[str]) -> list[str]:
        result: list[str] = []
        for step in steps:
            clean = one_line(str(step), 260).strip(" .")
            if not clean:
                continue
            if any(self._similar(clean, previous) for previous in result):
                continue
            result.append(clean)
        return result

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        if normalize_for_compare(a, keep_code=True) == normalize_for_compare(b, keep_code=True):
            return True
        return jaccard(a, b, keep_code=True) >= 0.82

    def _merge_bounded(
        self,
        existing: Sequence[str],
        new_steps: Sequence[str],
        limit: int,
        keep_head: int = 3,
    ) -> list[str]:
        merged: list[str] = []
        for step in [*existing, *new_steps]:
            clean = one_line(str(step), 260).strip(" .")
            if not clean or any(self._similar(clean, previous) for previous in merged):
                continue
            merged.append(clean)
        if len(merged) > limit:
            merged = merged[:keep_head] + merged[-(limit - keep_head):]
        return merged

    def _event_pitfalls(self, events: Sequence[ContrastiveEvent]) -> list[str]:
        pitfalls: list[str] = []
        for event in events:
            if not getattr(event, "verified", True):
                continue
            error = one_line(redact(event.error_message), 140)
            fix = one_line(redact(event.resolution_summary), 220)
            if not error or not fix or len(fix) < 15:
                continue
            if "\ufffd" in error + fix:
                continue
            if re.search(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}", error + fix, re.I):
                continue
            if contains_secret(event.error_message + event.resolution_summary):
                continue
            pitfall = f"**{event.trigger_tool or 'tool'}**: {error} -> {fix}"
            if not any(self._similar(pitfall, previous) for previous in pitfalls):
                pitfalls.append(truncate(pitfall, 380))
        return pitfalls[:MAX_PITFALLS]

    def _merge_pitfalls(self, existing: Sequence[str], new_items: Sequence[str]) -> list[str]:
        merged = list(existing)
        for item in new_items:
            clean = one_line(item, 380)
            if any(self._similar(clean, previous) for previous in merged):
                continue
            merged.append(clean)
        return merged[-MAX_PITFALLS:]

    @staticmethod
    def _parse_steps(text: str) -> list[str]:
        block = SkillBuilder._managed_block(text)
        steps: list[str] = []
        in_procedure = False
        for line in block.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith("## "):
                in_procedure = "procedure" in lowered
                continue
            if in_procedure and re.match(r"^\d+[.)]\s+", line.strip()):
                steps.append(re.sub(r"^\d+[.)]\s+", "", line.strip()))
        return steps

    @staticmethod
    def _parse_pitfalls(text: str) -> list[str]:
        block = SkillBuilder._managed_block(text)
        pitfalls: list[str] = []
        in_pitfalls = False
        for line in block.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith("## "):
                in_pitfalls = "pitfall" in lowered
                continue
            if in_pitfalls and line.strip().startswith("- "):
                pitfalls.append(line.strip()[2:].strip())
        return pitfalls

    @staticmethod
    def _managed_block(text: str) -> str:
        match = SKILL_RE.search(text or "")
        return match.group(0) if match else (text or "")

    @staticmethod
    def _metadata(text: str) -> dict[str, Any]:
        match = META_RE.search(text or "")
        if not match:
            return {}
        try:
            value = json.loads(match.group(1))
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _is_harness_managed(text: str) -> bool:
        value = text or ""
        # A real managed skill has a block marker at the start of a line plus
        # Harness metadata. A prose mention of the marker must not make a
        # hand-authored skill overwritable.
        if SKILL_START_LINE_RE.search(value) and META_RE.search(value):
            return True
        head = value[:1500].lower()
        return (
            "synthesized autonomously by harness" in head
            or "autonomously learned by harness" in head
        )

    @staticmethod
    def _user_tail(text: str) -> str:
        match = SKILL_RE.search(text or "")
        if not match:
            return ""
        return (text[match.end():] or "").strip()

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
        raw = (text or "").replace("\r\n", "\n")
        if not raw.startswith("---"):
            return {}, raw
        end = raw.find("\n---", 3)
        if end == -1:
            return {}, raw
        frontmatter = raw[3:end].strip("\n")
        body = raw[end + 4:].lstrip("\n")
        data: dict[str, str] = {}
        key: str | None = None
        buffer: list[str] = []
        for line in frontmatter.splitlines():
            if not line.strip():
                continue
            if not line.startswith((" ", "\t")) and ":" in line:
                if key:
                    data[key] = " ".join(buffer).strip()
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if value in (">-", ">", "|", "|-"):
                    buffer = []
                else:
                    data[key] = value.strip("'\"")
                    key = None
                    buffer = []
            elif key:
                buffer.append(line.strip())
        if key:
            data[key] = " ".join(buffer).strip()
        return data, body

    def read_skill(self, path: Path | str) -> dict[str, Any]:
        skill_file = Path(path)
        if skill_file.is_dir():
            skill_file = skill_file / "SKILL.md"
        try:
            text, _, _ = read_text_lossless(skill_file)
        except (OSError, UnicodeError) as exc:
            raise SkillValidationError(f"cannot read {skill_file}: {exc}") from exc
        frontmatter, body = self.parse_frontmatter(text)
        return {
            "name": frontmatter.get("name") or skill_file.parent.name,
            "description": frontmatter.get("description") or "",
            "frontmatter": frontmatter,
            "body": body,
            "path": str(skill_file),
            "managed": self._is_harness_managed(text),
            "metadata": self._metadata(text),
        }

    def validate_skill(self, path: Path | str) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        skill_file = Path(path)
        if skill_file.is_dir():
            skill_file = skill_file / "SKILL.md"
        if not skill_file.exists():
            return {"valid": False, "errors": ["SKILL.md not found"], "warnings": [], "path": str(skill_file)}
        try:
            text, _, _ = read_text_lossless(skill_file)
        except (OSError, UnicodeError) as exc:
            return {
                "valid": False,
                "errors": [f"SKILL.md is not valid UTF-8/UTF-16 text: {exc}"],
                "warnings": [],
                "path": str(skill_file),
            }
        frontmatter, body = self.parse_frontmatter(text)
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        if not frontmatter:
            errors.append("missing YAML frontmatter")
        if not name:
            errors.append("missing name")
        elif not NAME_RE.match(name):
            errors.append("name must be kebab-case (a-z, 0-9, hyphens)")
        elif len(name) > 64:
            errors.append("name is longer than 64 characters")
        elif name != skill_file.parent.name:
            warnings.append(f"name '{name}' does not match folder '{skill_file.parent.name}'")
        if not description:
            errors.append("missing description")
        elif len(description) < 20:
            errors.append("description is too short to trigger reliably")
        elif len(description) > 1024:
            errors.append("description is longer than 1024 characters")
        if not body.strip():
            errors.append("body is empty")
        if "TODO" in text or "[TODO]" in text:
            errors.append("contains unresolved TODO placeholders")
        if contains_secret(text):
            errors.append("contains what appears to be a credential")
        for finding, label in self._untrusted_findings(text, str(skill_file)):
            if finding.severity == "high":
                errors.append(label)
            else:
                warnings.append(label)
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            errors.append(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes")
        if self._is_harness_managed(text) and not self._metadata(text):
            warnings.append("managed skill is missing harness metadata")
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "path": str(skill_file),
            "name": name or skill_file.parent.name,
            "description": description,
        }

    # ------------------------------------------------------------------ privacy
    def managed_skill_files(self, workspace_path: Path | str | None = None) -> list[Path]:
        """Harness-generated SKILL.md files in the workspace skills folder."""
        workspace = Path(workspace_path).expanduser() if workspace_path else self.workspace_path
        if not workspace:
            return []
        root = get_workspace_skills_dir(workspace)
        files: list[Path] = []
        if not root.is_dir():
            return files
        for skill_file in sorted(root.glob("*/SKILL.md")):
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if self._is_harness_managed(text):
                files.append(skill_file)
        return files

    def scrub_managed_skills(
        self,
        workspace_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Redact credentials from Harness-generated skills; return changed paths.

        Only the managed block is rewritten. Hand-written skills and the notes a
        user added below the block are never touched.
        """
        changed: list[str] = []
        for skill_file in self.managed_skill_files(workspace_path):
            try:
                text, encoding, newline = read_text_lossless(skill_file)
            except (OSError, UnicodeError):
                continue
            match = SKILL_RE.search(text)
            head = text[: match.start()] if match else text
            block = match.group(0) if match else ""
            tail = text[match.end():] if match else ""
            scrubbed = redact_strict(head) + redact_strict(block) + tail
            if scrubbed == text:
                continue
            changed.append(str(skill_file))
            if not dry_run:
                atomic_write_text(skill_file, scrubbed, encoding=encoding, newline=newline)
        return changed

    # ------------------------------------------------------------------ discovery
    def list_installed_skills(
        self,
        workspace_path: Path | str | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        workspace = Path(workspace_path).expanduser() if workspace_path else self.workspace_path
        dirs = get_skills_dirs(workspace)
        if not include_global:
            dirs = [path for path in dirs if path == get_workspace_skills_dir(workspace)] if workspace else []
        plugin_dir = get_plugin_dir() / "skills"
        user_dir = get_global_user_skills_dir()
        by_name: dict[str, dict[str, Any]] = {}

        for directory in dirs:
            if not directory.exists() or not directory.is_dir():
                continue
            if directory.resolve() == plugin_dir.resolve():
                scope = "plugin"
            elif directory.resolve() == user_dir.resolve():
                scope = "user"
            elif workspace and directory.resolve() == get_workspace_skills_dir(workspace).resolve():
                scope = "workspace"
            else:
                scope = "external"
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for item in children:
                if not item.is_dir() or item.name.startswith("."):
                    continue
                skill_file = item / "SKILL.md"
                if not skill_file.exists():
                    continue
                validation = self.validate_skill(skill_file)
                try:
                    info = self.read_skill(skill_file)
                except SkillValidationError:
                    info = {"name": item.name, "description": "", "managed": False, "metadata": {}}
                name = validation.get("name") or info["name"] or item.name
                record = {
                    "name": name,
                    "description": validation.get("description") or info.get("description", ""),
                    "path": str(skill_file),
                    "folder": str(item),
                    "scope": scope,
                    "managed": info.get("managed", False),
                    "valid": validation["valid"],
                    "errors": validation.get("errors", []),
                    "warnings": validation.get("warnings", []),
                    "quality": info.get("metadata", {}).get("quality", 0),
                    "source": info.get("metadata", {}).get("source", "manual"),
                }
                if name in by_name:
                    by_name[name]["duplicates"].append(str(skill_file))
                    continue
                record["duplicates"] = []
                by_name[name] = record
        return sorted(
            by_name.values(),
            key=lambda item: (0 if item["scope"] == "workspace" else 1, item["name"]),
        )

    # ------------------------------------------------------------------- removal
    def remove_skill(
        self,
        name: str,
        scope: str | None = None,
        workspace_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        removed: list[str] = []
        workspace = Path(workspace_path).expanduser() if workspace_path else self.workspace_path
        allowed_roots = get_skills_dirs(workspace)
        allowed_roots.extend([get_plugin_dir() / "skills", get_global_user_skills_dir()])
        for skill in self.list_installed_skills(workspace, include_global=True):
            if skill["name"] != name:
                continue
            candidates = [(skill["folder"], skill["scope"])]
            for duplicate in skill.get("duplicates", []):
                duplicate_folder = Path(duplicate).parent
                candidates.append(
                    (str(duplicate_folder), self._scope_for_path(duplicate_folder, workspace))
                )
            for folder_text, folder_scope in candidates:
                if scope and folder_scope != scope:
                    continue
                folder = Path(folder_text).resolve()
                if not any(
                    self._is_within(folder, root.resolve())
                    for root in allowed_roots
                    if root.exists()
                ):
                    continue
                if not dry_run:
                    self._remove_path(folder)
                removed.append(str(folder))
            break
        return removed

    def _scope_for_path(self, path: Path, workspace: Path | None) -> str:
        resolved = path.resolve()
        if workspace and self._is_within(resolved, get_workspace_skills_dir(workspace).resolve()):
            return "workspace"
        if self._is_within(resolved, (get_plugin_dir() / "skills").resolve()):
            return "plugin"
        if self._is_within(resolved, get_global_user_skills_dir().resolve()):
            return "user"
        return "external"

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    # ----------------------------------------------------------------- installation
    def install_skill(
        self,
        source: str,
        name: str | None = None,
        scope: str = "workspace",
        workspace_path: Path | str | None = None,
        force: bool = False,
        all_skills: bool = False,
        timeout: int = 30,
        target_dir: Path | str | None = None,
    ) -> list[SkillInstallResult]:
        workspace = Path(workspace_path).expanduser() if workspace_path else self.workspace_path
        if target_dir:
            destination = Path(target_dir).expanduser()
        elif scope in ("workspace", "project"):
            if not workspace:
                raise SkillError("workspace scope requires a workspace path")
            destination = get_workspace_skills_dir(workspace)
        elif scope == "user":
            destination = get_global_user_skills_dir()
        else:
            destination = get_plugin_dir() / "skills"
        destination.mkdir(parents=True, exist_ok=True)

        source = (source or "").strip()
        if not source:
            raise SkillError("empty skill source")

        local = Path(source).expanduser()
        if local.exists():
            roots = self._local_skill_roots(local, all_skills)
            if not roots:
                raise SkillError(f"no SKILL.md found under {local}")
            return [
                self._install_root(root, destination, name if len(roots) == 1 else None, force, source)
                for root in roots
            ]

        with tempfile.TemporaryDirectory(prefix="harness-skill-") as temp:
            roots, provenance = self._fetch_remote(source, Path(temp), timeout=timeout)
            if not roots:
                raise SkillError(f"no SKILL.md found at {source}")
            if not all_skills and len(roots) > 1:
                if not name:
                    names = ", ".join(root.name for root in roots[:12])
                    raise SkillError(
                        "repository contains multiple skills; pass a path or --all. "
                        f"Available: {names}"
                    )
                wanted = self._skill_name(name)
                roots = [root for root in roots if self._skill_name(root.name) == wanted]
                if not roots:
                    raise SkillError(f"skill '{name}' was not found in {source}")
            return [
                self._install_root(
                    root,
                    destination,
                    name if len(roots) == 1 else None,
                    force,
                    provenance,
                )
                for root in roots
            ]

    def _local_skill_roots(self, path: Path, all_skills: bool) -> list[Path]:
        if path.is_file():
            return [path.parent] if path.name == "SKILL.md" else []
        if (path / "SKILL.md").exists():
            if all_skills:
                nested = [
                    child for child in path.iterdir()
                    if child.is_dir() and (child / "SKILL.md").exists()
                ]
                return [path, *nested] if nested else [path]
            return [path]
        if all_skills:
            return [
                child for child in sorted(path.iterdir())
                if child.is_dir() and (child / "SKILL.md").exists()
            ]
        return []

    def _install_root(
        self,
        root: Path,
        destination: Path,
        name: str | None,
        force: bool,
        provenance: str,
    ) -> SkillInstallResult:
        skill_file = root / "SKILL.md"
        validation = self.validate_skill(skill_file)
        if not validation["valid"]:
            raise SkillValidationError(
                f"invalid skill at {skill_file}: {'; '.join(validation['errors'])}"
            )
        warnings = list(validation.get("warnings", []))
        folder_findings = scan_skill(root)
        high = [
            finding for finding in folder_findings if finding.severity == "high"
        ]
        if high:
            finding = high[0]
            raise SkillValidationError(
                "untrusted content in installed skill "
                f"{finding.source or root.name}: "
                f"{finding.category.replace('_', ' ')} at line {finding.line}: "
                f"{finding.message}"
            )
        for finding in folder_findings:
            if finding.severity != "medium":
                continue
            if finding.source.lower() == "skill.md":
                continue
            warnings.append(
                f"untrusted content: {finding.source}: "
                f"{finding.message} (line {finding.line})"
            )
        frontmatter, _ = self.parse_frontmatter(
            read_text_lossless(skill_file)[0]
        )
        skill_name = self._skill_name(name or frontmatter.get("name") or root.name)
        target = destination / skill_name
        target_file = target / "SKILL.md"

        source_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest()
        if target.exists():
            if not target_file.exists() and not force:
                raise SkillConflictError(
                    f"{target} already exists but is not a valid skill; use --force to replace it"
                )
            if target_file.exists():
                existing_hash = hashlib.sha256(target_file.read_bytes()).hexdigest()
                if existing_hash == source_hash and not force:
                    return SkillInstallResult(
                        skill_name, target_file, "unchanged", provenance, warnings
                    )
                if not force:
                    raise SkillConflictError(
                        f"'{skill_name}' already exists at {target_file}; use --force to replace it"
                    )
            status = "replaced"
        else:
            status = "installed"

        staging = destination / f".{skill_name}.harness-tmp-{os.getpid()}"
        displaced = destination / f".{skill_name}.harness-old-{os.getpid()}"
        for leftover in (staging, displaced):
            if leftover.exists():
                self._remove_path(leftover)
        try:
            staging.mkdir(parents=True, exist_ok=True)
            self._copy_skill_tree(root, staging)
            copied_skill = staging / "SKILL.md"
            if name and name != self._skill_name(frontmatter.get("name") or root.name):
                try:
                    copied, copied_encoding, copied_newline = read_text_lossless(copied_skill)
                except (OSError, UnicodeError) as exc:
                    raise SkillValidationError(
                        f"cannot safely rewrite {copied_skill}: {exc}"
                    ) from exc
                atomic_write_text(
                    copied_skill,
                    self._set_frontmatter_name(copied, skill_name),
                    encoding=copied_encoding,
                    newline=copied_newline,
                )
            atomic_write_text(
                staging / "provenance.json",
                json.dumps(
                    {
                        "source": provenance,
                        "installed_at": utc_iso(),
                        "sha256": source_hash,
                        "license": self._find_license(root),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )
            if target.exists():
                target.replace(displaced)
            try:
                staging.replace(target)
            except Exception:
                if displaced.exists() and not target.exists():
                    displaced.replace(target)
                raise
        finally:
            self._remove_path(staging)
            self._remove_path(displaced)
        return SkillInstallResult(
            skill_name, target / "SKILL.md", status, provenance, warnings
        )

    @staticmethod
    def _remove_path(path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def _copy_skill_tree(self, root: Path, target: Path) -> None:
        total = 0
        count = 0
        root_resolved = root.resolve()
        for item in sorted(root.rglob("*")):
            try:
                resolved = item.resolve()
                resolved.relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            if item.is_symlink():
                continue
            if item.is_dir():
                if item.name in _COPY_DIRS and not any(part.startswith(".") for part in item.parts):
                    (target / item.relative_to(root)).mkdir(parents=True, exist_ok=True)
                continue
            relative = item.relative_to(root)
            allowed = (
                item.name in _COPY_FILES
                or relative.parts[0] in _COPY_DIRS
            )
            if not allowed or any(part.startswith(".") for part in relative.parts):
                continue
            count += 1
            if count > MAX_SKILL_FILES:
                raise SkillError(f"skill payload exceeds {MAX_SKILL_FILES} files")
            if item.stat().st_size > 2_000_000:
                continue
            total += item.stat().st_size
            if total > MAX_INSTALL_BYTES:
                raise SkillError(f"skill payload exceeds {MAX_INSTALL_BYTES} bytes")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
        if not (target / "SKILL.md").exists():
            raise SkillError("SKILL.md was not copied")

    @staticmethod
    def _find_license(root: Path) -> str:
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE", "NOTICE.md"):
            path = root / name
            if path.exists():
                return name
        return ""

    @staticmethod
    def _set_frontmatter_name(text: str, name: str) -> str:
        if not text.startswith("---"):
            return text
        end = text.find("\n---", 3)
        if end == -1:
            return text
        frontmatter = text[3:end]
        if re.search(r"(?m)^name:", frontmatter):
            frontmatter = re.sub(r"(?m)^name:.*$", f"name: {name}", frontmatter, count=1)
        else:
            frontmatter = f"name: {name}\n" + frontmatter
        return "---" + frontmatter + text[end:]

    # ---------------------------------------------------------------- remote fetch
    def _fetch_remote(self, source: str, temp: Path, timeout: int = 30) -> tuple[list[Path], str]:
        parsed_source = urllib.parse.urlparse(source)
        if (
            parsed_source.scheme in ("http", "https")
            and (parsed_source.hostname or "").lower() == "raw.githubusercontent.com"
        ):
            return self._fetch_raw(source, temp, timeout), source

        parsed = self.parse_github_source(source)
        if not parsed:
            raise SkillError(
                "source must be a local skill directory, a GitHub repository, "
                "a GitHub tree URL, or a raw SKILL.md URL"
            )
        owner, repo, ref, subpath = parsed
        archive = self._download_repo(owner, repo, ref, temp, timeout)
        roots = self._find_roots(archive, subpath)
        return roots, f"https://github.com/{owner}/{repo}" + (f"/tree/{ref}/{subpath}" if subpath else "")

    @staticmethod
    def parse_github_source(source: str) -> tuple[str, str, str, str] | None:
        value = source.strip().rstrip("/")
        if value.startswith("git@github.com:"):
            value = "https://github.com/" + value[len("git@github.com:"):]
        value = re.sub(r"\.git$", "", value)
        url = urllib.parse.urlparse(value if "://" in value else f"https://github.com/{value}")
        if url.netloc.lower() not in ("github.com", "www.github.com"):
            return None
        parts = [part for part in url.path.split("/") if part]
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]
        if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
            return None
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            ref = parts[3]
            subpath = "/".join(parts[4:])
            if subpath.endswith("SKILL.md"):
                subpath = "/".join(parts[4:-1])
            return owner, repo, ref, subpath
        subpath = "/".join(parts[2:])
        return owner, repo, "", subpath

    def _download_repo(self, owner: str, repo: str, ref: str, temp: Path, timeout: int) -> Path:
        archive = temp / "repo.zip"
        branches = [ref] if ref else ["main", "master"]
        errors: list[str] = []
        for branch in branches:
            url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
            if self._download(url, archive, timeout):
                break
            errors.append(f"{branch}: download failed")
        else:
            api = f"https://api.github.com/repos/{owner}/{repo}/zipball"
            if ref:
                api += f"/{urllib.parse.quote(ref)}"
            if not self._download(api, archive, timeout):
                raise SkillError(
                    f"could not download {owner}/{repo}: " + "; ".join(errors or ["network error"])
                )
        extracted = temp / "extracted"
        extracted.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise SkillError(
                        f"archive contains {len(members)} entries; limit is {MAX_ARCHIVE_MEMBERS}"
                    )
                total = 0
                for member in members:
                    if not self._safe_member(member.filename):
                        continue
                    if member.is_dir():
                        continue
                    mode = (member.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode):
                        continue
                    if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                        continue
                    total += max(0, member.file_size)
                    if total > MAX_EXTRACTED_BYTES:
                        raise SkillError(
                            f"archive expands beyond {MAX_EXTRACTED_BYTES} bytes"
                        )
                    bundle.extract(member, extracted)
        except (zipfile.BadZipFile, OSError) as exc:
            raise SkillError(f"downloaded archive is not a valid zip: {exc}") from exc
        children = [child for child in extracted.iterdir() if child.is_dir()]
        if not children:
            raise SkillError("downloaded archive was empty")
        return children[0]

    @staticmethod
    def _safe_member(name: str) -> bool:
        normalized = name.replace("\\", "/")
        if not normalized or "\x00" in normalized:
            return False
        if normalized.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", normalized):
            return False
        parts = normalized.split("/")
        if ".." in parts:
            return False
        return not any(part.startswith(".") and part not in (".", "..") for part in parts)

    @staticmethod
    def _download(url: str, destination: Path, timeout: int) -> bool:
        request = urllib.request.Request(url, headers={"User-Agent": "Harness2-Antigravity/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
        if len(data) > MAX_DOWNLOAD_BYTES:
            return False
        destination.write_bytes(data)
        return True

    def _fetch_raw(self, source: str, temp: Path, timeout: int) -> list[Path]:
        request = urllib.request.Request(source, headers={"User-Agent": "Harness2-Antigravity/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(1_000_000 + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SkillError(f"could not download {source}: {exc}") from exc
        if len(data) > 1_000_000:
            raise SkillError("raw SKILL.md is unexpectedly large")
        folder = temp / "raw-skill"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_bytes(data)
        validation = self.validate_skill(folder / "SKILL.md")
        if not validation["valid"]:
            raise SkillValidationError("; ".join(validation["errors"]))
        return [folder]

    @staticmethod
    def _find_roots(extracted: Path, subpath: str) -> list[Path]:
        base = extracted
        if subpath:
            candidate = (extracted / subpath).resolve()
            try:
                candidate.relative_to(extracted.resolve())
            except ValueError:
                raise SkillValidationError("GitHub path escapes the repository root")
            if not candidate.exists():
                raise SkillError(f"path '{subpath}' does not exist in repository")
            base = candidate
        if (base / "SKILL.md").exists():
            return [base]
        roots = [
            child for child in sorted(base.iterdir())
            if child.is_dir() and (child / "SKILL.md").exists()
        ] if base.is_dir() else []
        if roots:
            return roots
        # Some repositories keep skills one level below a skills/ directory.
        nested: list[Path] = []
        for child in (base.rglob("SKILL.md") if base.is_dir() else []):
            if len(child.relative_to(base).parts) <= 4 and child.parent not in nested:
                nested.append(child.parent)
        return nested[:50]
