"""Explain why sessions did or did not produce skills.

Skill synthesis has several gates (repeatable steps, actionable goal, name,
segment success, quality). Without a diagnostic it looks like the harness simply
ignores a session. This module replays the recorded sessions through the same
analyzer and reports the first gate each task failed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .analyzer import TranscriptAnalyzer
from .config import resolve_transcript_path
from .memory_store import MemoryStore
from .skill_builder import QUALITY_THRESHOLD
from .util import one_line


def diagnose_skill_generation(
    workspace: str | Path | None = None,
    limit: int = 10,
    memory: MemoryStore | None = None,
) -> dict[str, Any]:
    """Replay recent sessions and report the skill gate outcome per task."""
    store = memory or MemoryStore(workspace=workspace)
    sessions = store.list_sessions(limit=limit, workspace=workspace)
    indexed = {
        str(row.get("name") or ""): row
        for row in store.get_all_skills(workspace=workspace)
    }

    entries: list[dict[str, Any]] = []
    for session in sessions:
        conversation_id = str(session.get("conversation_id") or "")
        transcript = resolve_transcript_path(conversation_id)
        entry: dict[str, Any] = {
            "conversation_id": conversation_id,
            "workspace": str(session.get("workspace") or ""),
            "task_summary": one_line(str(session.get("task_summary") or ""), 160),
            "outcome": str(session.get("outcome") or ""),
            "updated_at": str(session.get("updated_at") or ""),
            "transcript": str(transcript) if transcript else "",
            "tasks": 0,
            "workflows": [],
        }
        if transcript is None:
            entry["error"] = "transcript not found (Antigravity may have pruned it)"
            entries.append(entry)
            continue

        analyzer = TranscriptAnalyzer(transcript_path=transcript)
        try:
            analysis = analyzer.analyze(
                analyzer.parse_transcript(),
                conversation_id=conversation_id,
                workspace=session.get("workspace") or workspace,
            )
        except Exception as exc:  # a single bad transcript must not hide the rest
            entry["error"] = f"analysis failed: {exc}"
            entries.append(entry)
            continue

        entry["tasks"] = len(analysis.tasks)
        entry["outcome"] = analysis.final_outcome or entry["outcome"]
        for note in analysis.workflow_diagnostics:
            workflow = dict(note)
            if workflow.get("status") == "candidate":
                name = str(workflow.get("name") or "")
                quality = float(workflow.get("quality") or 0.0)
                if quality < QUALITY_THRESHOLD:
                    workflow["verdict"] = (
                        f"blocked: quality {quality:.2f} < {QUALITY_THRESHOLD:.2f}"
                    )
                elif name in indexed:
                    workflow["verdict"] = "already written to this workspace"
                else:
                    workflow["verdict"] = (
                        "eligible: run `harness reflect` (or wait for the Stop hook)"
                    )
            else:
                workflow["verdict"] = f"blocked: {workflow.get('reason', 'unknown')}"
            entry["workflows"].append(workflow)
        entries.append(entry)

    eligible = sum(
        1
        for entry in entries
        for workflow in entry["workflows"]
        if str(workflow.get("verdict", "")).startswith("eligible")
    )
    return {
        "workspace": str(workspace) if workspace else "",
        "min_quality": QUALITY_THRESHOLD,
        "sessions": entries,
        "eligible": eligible,
        "skills_indexed": len(indexed),
    }
