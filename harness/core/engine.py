"""
The Harness² reflection cycle.

The engine is intentionally conservative:

1. resolve and analyse a transcript;
2. keep only verified, high-confidence failure/recovery contrasts;
3. update *workspace* rules (never the global file when a workspace exists);
4. synthesise at most a small number of substantial skills;
5. retain the session, lessons, and skill re-use in the memory store.

Re-running the same transcript is a no-op unless ``force=True`` is passed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .agents_md_manager import AgentsMdManager
from .analyzer import ContrastiveEvent, SessionAnalysis, ToolExecution, TranscriptAnalyzer, WorkflowCandidate
from .config import (
    get_state_dir,
    project_fingerprint,
    resolve_transcript_path,
    safe_conversation_id,
    tokenize,
)
from .incremental import IncrementalRead, read_incremental
from .memory_store import MemoryStore
from .redaction import redact
from .router import TaskRouter
from .settings import get_setting, load_settings
from .skill_builder import (
    SkillBuildResult,
    SkillBuilder,
    SkillConflictError,
    SkillError,
    SkillQualityError,
)
from .util import (
    file_lock,
    foreign_windows_paths,
    hash_steps,
    iter_strings,
    one_line,
    path_root_keys,
    text_mentions_root,
    truncate,
)

MAX_SKILLS_PER_SESSION = 2
MIN_WORKFLOW_QUALITY = 0.55
MAX_DIRECTIVES_PER_CYCLE = 5



def cycle_lock_path(workspace: Path | str | None) -> Path:
    """Return the reflection-cycle lock for a workspace.

    Conversations in the same workspace share rules and memory, so they must
    serialize. Unrelated projects get independent locks and can reflect
    concurrently.
    """
    project = project_fingerprint(workspace) if workspace else "global"
    return get_state_dir() / "locks" / f"cycle-{project}.lock"


@dataclass
class EngineResult:
    conversation_id: str = ""
    status: str = "processed"  # processed, skipped, no_transcript, busy, error
    workspace: str = ""
    contrastive_events_found: int = 0
    verified_lessons: int = 0
    unresolved_observations: int = 0
    rules_updated: bool = False
    rule_details: dict[str, Any] = field(default_factory=dict)
    skills_synthesized: list[str] = field(default_factory=list)
    skills_updated: list[str] = field(default_factory=list)
    skills_skipped: list[dict[str, str]] = field(default_factory=list)
    skill_reuse: list[str] = field(default_factory=list)
    memory_logged: bool = False
    memory_counts: dict[str, int] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    transcript_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    summary: str = ""


class HarnessEngine:
    """Orchestrates the Propose-Probe-Compose reflection cycle."""

    def __init__(self, workspace_path: Path | str | None = None):
        self.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
        self.settings, self.settings_warnings = load_settings()
        self.memory_store = MemoryStore(workspace=self.workspace_path)
        self.agents_manager = AgentsMdManager(
            self.workspace_path,
            max_bytes=int(
                get_setting(self.settings, "rules.max_bytes", 20_000)
            ),
            priority=self.memory_store.get_rule_priority,
        )
        self.skill_builder = SkillBuilder(self.workspace_path)

    # ------------------------------------------------------------------- public
    def run_cycle(
        self,
        conversation_id: str = "",
        transcript_path: Path | str | None = None,
        raw_steps: list[dict[str, Any]] | None = None,
        workspace_path: Path | str | None = None,
        force: bool = False,
        lock_timeout: float = 1.5,
        pending_events: Sequence[dict[str, Any]] | None = None,
    ) -> EngineResult:
        workspace = self._workspace(workspace_path)
        self._bind_workspace(workspace)
        result = EngineResult(workspace=str(workspace) if workspace else "")
        for warning in self.settings_warnings:
            self._add_warning(result, f"settings: {warning}")

        with file_lock(cycle_lock_path(workspace), timeout=lock_timeout) as acquired:
            if not acquired:
                result.status = "busy"
                result.summary = "Another Harness reflection cycle is already running."
                return result
            return self._run_locked(
                result,
                workspace=workspace,
                conversation_id=conversation_id,
                transcript_path=transcript_path,
                raw_steps=raw_steps,
                force=force,
                pending_events=pending_events,
            )

    def record_observations(
        self,
        events: Sequence[dict[str, Any]],
        conversation_id: str = "",
        workspace_path: Path | str | None = None,
    ) -> EngineResult:
        """Persist PostToolUse observations when no transcript is available.

        These are stored as unverified candidates and never become rules.
        """
        workspace = self._workspace(workspace_path)
        self._bind_workspace(workspace)
        result = EngineResult(conversation_id=conversation_id, workspace=str(workspace) if workspace else "")
        analysis = SessionAnalysis(
            conversation_id=conversation_id or "observed-session",
            workspace=str(workspace) if workspace else "",
            final_outcome="partial",
        )
        for raw in events[-25:]:
            tool = str(raw.get("tool") or "tool")[:80]
            error = one_line(redact(str(raw.get("error") or "")), 300)
            if not error:
                continue
            analysis.unresolved_events.append(
                ContrastiveEvent(
                    trigger_tool=tool,
                    failed_args={},
                    error_message=error,
                    successful_args=None,
                    resolution_summary=f"`{tool}` reported an error; no recovery has been verified yet.",
                    category="command",
                    verified=False,
                    confidence=0.2,
                    error_signature=one_line(error, 160),
                    session_id=conversation_id,
                )
            )
        analysis.tasks = ["Observed tool failures (no transcript available)"]
        counts = self.memory_store.record_session(
            analysis,
            transcript_hash="observations:" + hash_steps(list(events)),
        )
        result.memory_logged = True
        result.memory_counts = counts
        result.unresolved_observations = len(analysis.unresolved_events)
        result.status = "processed" if analysis.unresolved_events else "no_transcript"
        result.summary = (
            f"Recorded {len(analysis.unresolved_events)} unresolved observation(s); "
            "no rules were changed without a verified recovery."
        )
        return result

    # ------------------------------------------------------------------ internals
    def _run_locked(
        self,
        result: EngineResult,
        workspace: Path | None,
        conversation_id: str,
        transcript_path: Path | str | None,
        raw_steps: list[dict[str, Any]] | None,
        force: bool,
        pending_events: Sequence[dict[str, Any]] | None,
    ) -> EngineResult:
        transcript = self._resolve_transcript(conversation_id, transcript_path)
        # Known up front so skipped and empty cycles stay attributable in the
        # event journal and in `harness status`.
        result.conversation_id = safe_conversation_id(
            conversation_id or self._conversation_from_path(transcript)
        )
        analyzer = TranscriptAnalyzer(transcript)
        incremental: IncrementalRead | None = None
        used_transcript_steps = raw_steps is None and transcript is not None

        if used_transcript_steps:
            incremental = read_incremental(
                transcript,
                conversation_id=conversation_id
                or self._conversation_from_path(transcript),
                force_full=force,
            )
            steps = incremental.steps
            if incremental.analysis_truncated:
                self._add_warning(
                    result,
                    "Transcript exceeded the analysis limit; only the most recent "
                    "steps were analyzed.",
                )
            # ``context_truncated`` only means the saved pairing window was
            # capped. Every parsed step was still analysed, so it is not a
            # user-visible warning.
        else:
            steps = raw_steps if raw_steps is not None else []
            if transcript is not None:
                # Raw steps and forced cycles bypass the cursor, but a full
                # parse still resets it so the next Stop does not re-ingest
                # lines that this cycle already handled.
                incremental = read_incremental(
                    transcript,
                    conversation_id=conversation_id
                    or self._conversation_from_path(transcript),
                    force_full=True,
                )

        if not steps:
            if pending_events:
                observed = self.record_observations(
                    pending_events, conversation_id=conversation_id, workspace_path=workspace
                )
                observed.warnings.extend(result.warnings)
                return observed
            result.status = "no_transcript"
            result.summary = "No transcript steps were available; nothing was recorded."
            return result

        if workspace and not self._steps_belong_to_workspace(steps, workspace):
            # A conversation is analysed against one workspace. If the
            # transcript is clearly from a different project, writing its
            # lessons here would leak rules into the wrong AGENTS.md.
            if incremental is not None:
                incremental.save()
            result.status = "skipped"
            result.summary = (
                "Transcript belongs to a different workspace; nothing was written."
            )
            self._add_warning(
                result,
                f"transcript/workspace mismatch: no lessons written for '{workspace}'",
            )
            return result

        content_hash = hash_steps(steps)
        resolved_id = safe_conversation_id(
            conversation_id
            or self._conversation_from_path(transcript)
            or f"session-{content_hash[:12]}"
        )
        result.conversation_id = resolved_id
        result.transcript_hash = content_hash

        if not force and self.memory_store.is_session_processed(resolved_id, content_hash):
            if incremental is not None:
                incremental.save()
            result.status = "skipped"
            result.summary = (
                f"Session '{resolved_id}' was already processed; use --force to re-analyse it."
            )
            return result

        analysis = analyzer.analyze(
            steps,
            conversation_id=resolved_id,
            workspace=workspace,
        )
        if not analysis.workspace and workspace:
            analysis.workspace = str(workspace)

        result.contrastive_events_found = len(analysis.contrastive_events)
        result.unresolved_observations = len(analysis.unresolved_events)

        verified = self._probe_contrastive_events(analysis.contrastive_events)
        if workspace and verified:
            verified = self._drop_foreign_path_events(verified, workspace, result)
        workflows = self._probe_workflows(analysis.workflows)
        result.verified_lessons = len(verified)

        # 1. Rules. A workspace owns its project-specific rules. Only a session
        # without a project may influence the global environment rules, and the
        # manager strips anything workspace-specific before writing.
        if verified or analysis.user_constraints or analysis.environment_quirks:
            target = "workspace" if workspace else "plugin"
            evidence = "; ".join(
                one_line(event.error_message or event.resolution_summary, 160)
                for event in list(verified)[:3]
            )

            def _record_rule_application(rules: list[dict[str, Any]]) -> None:
                if not rules:
                    return
                try:
                    self.memory_store.record_rule_application(
                        rules,
                        target_file=str(rules[0].get("target_file") or ""),
                        scope=str(rules[0].get("scope") or target),
                        conversation_id=result.conversation_id,
                        evidence=evidence,
                    )
                except Exception as exc:  # provenance must not break the Stop hook
                    self._add_warning(result, f"rule provenance recording failed: {exc}")

            try:
                details = self.agents_manager.update_rules(
                    contrastive_events=verified,
                    user_constraints=analysis.user_constraints[:MAX_DIRECTIVES_PER_CYCLE],
                    environment_quirks=analysis.environment_quirks,
                    target=target,
                    on_apply=_record_rule_application,
                )
                result.rule_details = details
                result.rules_updated = any(
                    bool(value.get("updated")) for value in details.values() if isinstance(value, dict)
                )
                for value in details.values():
                    if isinstance(value, dict):
                        result.warnings.extend(value.get("warnings", []))
                if result.rules_updated:
                    added = sum(
                        int(value.get("added", 0))
                        for value in details.values()
                        if isinstance(value, dict)
                    )
                    self.memory_store.record_rule_edit(
                        target_file=str(next(iter(details.values())).get("path", "AGENTS.md")),
                        change_summary=(
                            f"{target} rules: {added} new rule(s) from {len(verified)} verified lesson(s), "
                            f"{len(analysis.user_constraints)} directive(s)."
                        ),
                    )
            except Exception as exc:  # never let rule evolution break the Stop hook
                result.warnings.append(f"rule update failed: {exc}")

        # 2. Skills.
        existing_skills = {skill["name"] for skill in self.skill_builder.list_installed_skills(workspace)}
        max_skills = max(
            0,
            int(get_setting(self.settings, "skills.max_per_session", MAX_SKILLS_PER_SESSION)),
        )
        for workflow in workflows[:max_skills]:
            related = self._related_events(workflow, verified)
            try:
                build = self.skill_builder.build_skill(
                    workflow,
                    related_events=related,
                    target_dir=None,
                    force=force,
                )
            except (SkillQualityError, SkillConflictError, SkillError) as exc:
                result.skills_skipped.append({"name": workflow.name, "reason": str(exc)})
                continue
            except Exception as exc:
                result.skills_skipped.append(
                    {"name": workflow.name, "reason": f"unexpected error: {exc}"}
                )
                continue

            if build.status in ("created", "updated"):
                self.memory_store.record_skill_saved(
                    name=build.name,
                    description=build.description,
                    file_path=str(build.path),
                    workspace=workspace,
                    scope="workspace" if workspace else "plugin",
                    quality=build.quality,
                    source="harness-auto",
                )
                if build.status == "created":
                    result.skills_synthesized.append(build.name)
                else:
                    result.skills_updated.append(build.name)
            elif build.status == "unchanged":
                result.skills_skipped.append(
                    {"name": build.name, "reason": "already up to date"}
                )

        result.skill_reuse = list(analysis.skill_usage)

        # 3. Task routing. New rules and generated skills must be reachable from
        # this project's AGENTS.md, otherwise the next session cannot load them.
        if workspace and get_setting(self.settings, "routing.enabled", True):
            router = TaskRouter(workspace)
            # Do not create an AGENTS.md just to record that nothing was learned.
            worth_writing = bool(
                result.rules_updated
                or result.skills_synthesized
                or result.skills_updated
                or router.project_skills()
                or (Path(workspace) / "AGENTS.md").exists()
            )
            if worth_writing:
                try:
                    # The router cleans and bounds the text; cutting it here
                    # first split quoted paths in half.
                    # Route for the newest request: that is what the next turn
                    # continues, not whatever the session opened with.
                    task_text = (analysis.tasks[-1] if analysis.tasks else "")[:2000] or "Interactive session"
                    routed = router.apply(task_text)
                    result.routing = routed.as_dict()
                    result.warnings.extend(result.routing.get("warnings", []))
                except Exception as exc:
                    self._add_warning(result, f"task routing failed: {exc}")

        # 4. Memory. This is also where unverified observations are retained.
        try:
            counts = self.memory_store.record_session(
                analysis,
                transcript_hash=content_hash,
                metadata={"workflows": len(workflows), "verified_lessons": len(verified)},
            )
            result.memory_logged = True
            result.memory_counts = counts
        except Exception as exc:
            result.warnings.append(f"memory recording failed: {exc}")

        # Only advance the cursor once the analysis and memory write succeeded.
        if result.memory_logged and incremental is not None:
            incremental.save()

        result.status = "processed"
        result.summary = self._summary(result, workspace, existing_skills)
        return result

    @staticmethod
    def _add_warning(result: EngineResult, text: str) -> None:
        if text and text not in result.warnings:
            result.warnings.append(text)

    def _workspace(self, override: Path | str | None) -> Path | None:
        value = override if override is not None else self.workspace_path
        if value is None:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    @staticmethod
    def _root_prefix(workspace: Path | str) -> tuple[str, ...]:
        # Long and 8.3 short spellings of the same folder both count.
        return path_root_keys(workspace)

    def _steps_belong_to_workspace(
        self,
        steps: Sequence[Any],
        workspace: Path | str,
    ) -> bool:
        """Cheap affinity check: does this transcript talk about this project?

        Transcripts with no absolute paths are allowed. Once an absolute path
        appears, at least one must live under the workspace; otherwise the
        transcript belongs to another project.

        Strings are scanned as they appear in the transcript, not as a JSON
        dump: escaping turned ``"task:\\n"`` into a fake ``k:\\n`` drive path.
        The root is matched at each path's position, so workspace folders with
        spaces or apostrophes ("video editor", "instagram dm's") are found.
        """
        root = self._root_prefix(workspace)
        outside = False
        for step in list(steps)[:250]:
            for text in iter_strings(step):
                if text_mentions_root(text, root):
                    return True
                # Agent and tool install folders (~/.gemini, AppData, Program
                # Files) appear in every transcript and belong to no project.
                if not outside and foreign_windows_paths(text, root, ignore_tooling=True):
                    outside = True
        return not outside

    def _drop_foreign_path_events(
        self,
        events: Sequence[ContrastiveEvent],
        workspace: Path | str,
        result: EngineResult,
    ) -> list[ContrastiveEvent]:
        """Reject lessons whose evidence points at another project's files."""
        root = self._root_prefix(workspace)
        kept: list[ContrastiveEvent] = []
        for event in events:
            foreign = [
                path
                for text in iter_strings(
                    [
                        event.error_message,
                        event.resolution_summary,
                        event.failed_args,
                        event.successful_args,
                    ]
                )
                for path in foreign_windows_paths(text, root, ignore_tooling=True)
            ]
            if foreign:
                self._add_warning(
                    result,
                    "dropped lesson that references another project's path: "
                    + one_line(foreign[0], 80),
                )
                continue
            kept.append(event)
        return kept

    def _bind_workspace(self, workspace: Path | None) -> None:
        """Keep every helper consistent with a per-call workspace override."""
        current = self.workspace_path.resolve() if self.workspace_path else None
        if current == workspace:
            return
        self.workspace_path = workspace
        self.memory_store = MemoryStore(workspace=workspace)
        self.agents_manager = AgentsMdManager(
            workspace,
            max_bytes=int(
                get_setting(self.settings, "rules.max_bytes", 20_000)
            ),
            priority=self.memory_store.get_rule_priority,
        )
        self.skill_builder = SkillBuilder(workspace)

    @staticmethod
    def _resolve_transcript(conversation_id: str, transcript_path: Path | str | None) -> Path | None:
        if transcript_path:
            candidate = Path(transcript_path).expanduser()
            if candidate.exists() and candidate.is_file():
                return candidate
        return resolve_transcript_path(conversation_id)

    @staticmethod
    def _conversation_from_path(path: Path | None) -> str:
        if not path:
            return ""
        parts = list(path.parts)
        if "brain" in parts:
            index = parts.index("brain")
            if index + 1 < len(parts):
                return parts[index + 1]
        return ""

    def _probe_contrastive_events(self, events: list[ContrastiveEvent]) -> list[ContrastiveEvent]:
        valid: list[ContrastiveEvent] = []
        seen: set[str] = set()
        for event in events:
            if not getattr(event, "verified", False):
                continue
            if event.confidence < 0.5:
                continue
            if not event.trigger_tool or event.trigger_tool.lower() == "unknown":
                continue
            summary = (event.resolution_summary or "").strip()
            if len(summary) < 20:
                continue
            if "finished with result" in summary.lower() or "command exited with code" in summary.lower():
                continue
            key = (event.error_signature or summary).lower()
            if key in seen:
                continue
            seen.add(key)
            valid.append(event)
        return valid[:12]

    def _probe_workflows(self, workflows: list[WorkflowCandidate]) -> list[WorkflowCandidate]:
        valid: list[WorkflowCandidate] = []
        seen: set[str] = set()
        min_quality = float(
            get_setting(self.settings, "skills.min_quality", MIN_WORKFLOW_QUALITY)
        )
        for workflow in workflows:
            if not workflow.success:
                continue
            if workflow.quality_score < min_quality:
                continue
            steps = [one_line(step, 260) for step in workflow.steps if str(step).strip()]
            if len(steps) < 2:
                continue
            if workflow.name in seen:
                continue
            seen.add(workflow.name)
            valid.append(workflow)
        return sorted(valid, key=lambda item: (-item.quality_score, -len(item.steps)))

    def _related_events(
        self,
        workflow: WorkflowCandidate,
        events: Sequence[ContrastiveEvent],
    ) -> list[ContrastiveEvent]:
        keywords = set(tokenize(workflow.goal))
        for keyword in workflow.trigger_keywords:
            keywords.update(tokenize(keyword))
        related: list[ContrastiveEvent] = []
        for event in events:
            text = f"{event.error_message} {event.resolution_summary}"
            text_tokens = set(tokenize(text))
            tool_match = event.trigger_tool in workflow.tools_involved
            overlap = len(keywords & text_tokens)
            if tool_match or overlap >= 2:
                related.append(event)
        return related[:4]

    def _summary(
        self,
        result: EngineResult,
        workspace: Path | None,
        existing_skills: set[str],
    ) -> str:
        scope = workspace.name if workspace else "global"
        skills = len(result.skills_synthesized) + len(result.skills_updated)
        return (
            f"Harness cycle for '{result.conversation_id}' ({scope}): "
            f"{result.contrastive_events_found} contrast(s), {result.verified_lessons} verified rule candidate(s), "
            f"{skills} skill(s) written, rules updated: {result.rules_updated}, "
            f"memory recorded: {result.memory_logged}."
        )

    # ------------------------------------------------------------- repair helpers
    def repair(self, target: str = "both", dry_run: bool = False) -> dict[str, Any]:
        return self.agents_manager.repair(target=target, dry_run=dry_run)

    def memory_stats(self) -> dict[str, Any]:
        return self.memory_store.get_stats(workspace=self.workspace_path)
