"""
Session bootstrap loader for Harness².
Prepares dynamic briefings on SessionStart, indexes available skills,
and ensures that all synthesized skills and evolved rules are immediately active.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any
from .config import get_skills_dirs, get_plugin_dir
from .skill_builder import SkillBuilder
from .memory_store import MemoryStore


class SessionLoader:
    """Prepares and injects harness intelligence when a session starts."""

    def __init__(self, workspace_path: Path | None = None):
        self.workspace_path = workspace_path
        self.skill_builder = SkillBuilder(workspace_path)
        self.memory_store = MemoryStore()

    def generate_session_start_payload(self) -> dict[str, Any]:
        """Generates the SessionStart hook output payload with dynamic briefing."""
        skills = self.skill_builder.list_installed_skills()
        recent_lessons = self.memory_store.get_recent_lessons(limit=5)

        briefing_lines = [
            "⚡ **[Harness² Self-Improvement System Active]**",
            "> Autonomous agent harnessing enabled (Google Research Harness²).",
            "> Reusable skills and evolved AGENTS.md rules are loaded to prevent cold starts.",
            "",
        ]

        if skills:
            briefing_lines.append(f"**Available Harness Skills ({len(skills)} ready)**:")
            for s in skills[:8]:
                briefing_lines.append(f"- `{s['name']}`: {s['description'][:100]}")
            if len(skills) > 8:
                briefing_lines.append(f"- *...and {len(skills) - 8} more skills indexed.*")
            briefing_lines.append("")

        if recent_lessons:
            briefing_lines.append("**Key Learned Heuristics (Past Lessons)**:")
            for l in recent_lessons[:4]:
                tool = l.get("trigger_tool", "tool")
                res = l.get("resolution", "")[:110]
                briefing_lines.append(f"- `{tool}`: {res}")
            briefing_lines.append("")

        briefing_lines.append(
            "👉 *When executing tasks, leverage existing skills before writing from scratch. "
            "On completion, new procedures will automatically be synthesized into skills.*"
        )

        message_text = "\n".join(briefing_lines)

        return {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": message_text
                    }
                }
            ]
        }
