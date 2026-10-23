"""
Core Harness² Engine implementing the Propose-Probe-Compose recursive self-improvement cycle.
Coordinates transcript analysis, AGENTS.md rule evolution, skill synthesis, and experience retention.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import resolve_transcript_path
from .analyzer import TranscriptAnalyzer, SessionAnalysis, ContrastiveEvent, WorkflowCandidate
from .agents_md_manager import AgentsMdManager
from .skill_builder import SkillBuilder
from .memory_store import MemoryStore


@dataclass
class EngineResult:
    """Summary of actions taken during the recursive harness improvement step."""
    conversation_id: str
    contrastive_events_found: int = 0
    rules_updated: bool = False
    skills_synthesized: list[str] = field(default_factory=list)
    skills_updated: list[str] = field(default_factory=list)
    memory_logged: bool = False
    summary: str = ""


class HarnessEngine:
    """Orchestrates recursive agent harnessing for Antigravity."""

    def __init__(self, workspace_path: Path | None = None):
        self.workspace_path = workspace_path
        self.agents_manager = AgentsMdManager(workspace_path)
        self.skill_builder = SkillBuilder(workspace_path)
        self.memory_store = MemoryStore()

    def run_cycle(
        self,
        conversation_id: str = "",
        transcript_path: Path | str | None = None,
        raw_steps: list[dict[str, Any]] | None = None,
    ) -> EngineResult:
        """Executes the Propose-Probe-Compose self-improvement loop for a session."""
        # 1. Resolve transcript file
        t_path = None
        if transcript_path:
            t_path = Path(transcript_path)
        elif not raw_steps:
            t_path = resolve_transcript_path(conversation_id)

        analyzer = TranscriptAnalyzer(t_path)
        steps = raw_steps or (analyzer.parse_transcript() if t_path else [])

        # Step 1: Diagnose & Analyze (Propose Phase)
        analysis: SessionAnalysis = analyzer.analyze(steps, conversation_id=conversation_id)

        result = EngineResult(
            conversation_id=conversation_id or analysis.conversation_id,
            contrastive_events_found=len(analysis.contrastive_events),
        )

        # Step 2: Probe & Evaluate Candidate Invariants
        valid_contrasts = self._probe_contrastive_events(analysis.contrastive_events)
        valid_workflows = self._probe_workflows(analysis.workflows)

        # Step 3: Compose - Evolve AGENTS.md Rules
        if valid_contrasts or analysis.user_constraints or analysis.environment_quirks:
            rule_res = self.agents_manager.update_rules(
                contrastive_events=valid_contrasts,
                user_constraints=analysis.user_constraints,
                environment_quirks=analysis.environment_quirks,
                target="both",
            )
            result.rules_updated = any(rule_res.values())
            self.memory_store.record_rule_edit(
                target_file="AGENTS.md",
                change_summary=f"Added {len(valid_contrasts)} heuristics, {len(analysis.user_constraints)} directives.",
            )

        # Step 4: Compose - Build or Update Skills
        for wf in valid_workflows:
            skill_path = self.skill_builder.build_or_update_skill(
                workflow=wf,
                related_events=valid_contrasts,
            )
            skill_name = wf.name
            if skill_path.exists():
                self.memory_store.record_skill_saved(
                    name=skill_name,
                    description=wf.description,
                    file_path=str(skill_path),
                )
                result.skills_synthesized.append(skill_name)

        # Step 5: Harnessing-Level Recursion - Retain in Experience Library
        self.memory_store.record_session(analysis)
        result.memory_logged = True

        # Generate summary
        result.summary = (
            f"Harness² Cycle completed for session '{result.conversation_id}': "
            f"{result.contrastive_events_found} contrastive lessons, "
            f"{len(result.skills_synthesized)} skills synthesized/updated, "
            f"rules evolved: {result.rules_updated}."
        )

        return result

    def _probe_contrastive_events(self, events: list[ContrastiveEvent]) -> list[ContrastiveEvent]:
        """Probes and filters contrastive events to retain only high-signal, actionable rules."""
        valid = []
        for ev in events:
            # Drop trivial or uninformative messages
            if len(ev.resolution_summary.strip()) < 15:
                continue
            if "unknown" in ev.trigger_tool.lower():
                continue
            valid.append(ev)
        return valid

    def _probe_workflows(self, workflows: list[WorkflowCandidate]) -> list[WorkflowCandidate]:
        """Probes workflow candidates to ensure they contain actionable, multi-step procedures."""
        valid = []
        for wf in workflows:
            if len(wf.steps) >= 2 and len(wf.goal.strip()) >= 5:
                valid.append(wf)
        return valid
