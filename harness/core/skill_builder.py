"""
Skill Builder for Harness².
Autonomously synthesizes reusable Antigravity skills (SKILL.md) from task executions,
updates existing skills with new variations/edge-cases, and indexes them for progressive disclosure.
"""

from __future__ import annotations
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from .config import get_skills_dirs, get_plugin_dir
from .analyzer import WorkflowCandidate, ContrastiveEvent


class SkillBuilder:
    """Creates and refines Antigravity skills from observed workflows and contrastive lessons."""

    def __init__(self, workspace_path: Path | None = None):
        self.workspace_path = workspace_path
        self.skills_dirs = get_skills_dirs(workspace_path)
        # Primary directory where new skills are written
        self.primary_skills_dir = self.skills_dirs[0]

    def build_or_update_skill(
        self,
        workflow: WorkflowCandidate,
        related_events: Sequence[ContrastiveEvent] = (),
        target_dir: Path | None = None,
    ) -> Path:
        """Synthesizes a new SKILL.md or sequentially refines an existing one."""
        dest_dir = target_dir or self.primary_skills_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        skill_slug = self._slugify(workflow.name)
        skill_folder = dest_dir / skill_slug
        skill_folder.mkdir(parents=True, exist_ok=True)
        skill_file = skill_folder / "SKILL.md"

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if skill_file.exists():
            content = self._update_existing_skill(skill_file, workflow, related_events, now_str)
        else:
            content = self._generate_fresh_skill(workflow, related_events, now_str)

        skill_file.write_text(content, encoding="utf-8")
        return skill_file

    def _generate_fresh_skill(
        self,
        workflow: WorkflowCandidate,
        events: Sequence[ContrastiveEvent],
        timestamp: str,
    ) -> str:
        """Generates a complete, standard Antigravity SKILL.md."""
        slug = self._slugify(workflow.name)
        desc = (
            f"{workflow.description}. Use when working on tasks related to: "
            f"{workflow.goal[:100]}. Autonomously learned by Harness²."
        )
        # Clean description for YAML
        desc_clean = desc.replace("\n", " ").replace('"', '\\"')

        lines = [
            "---",
            f"name: {slug}",
            f"description: >-",
            f"  {desc_clean}",
            "---",
            "",
            f"# {workflow.goal.capitalize()}",
            "",
            f"> [!NOTE]",
            f"> Synthesized autonomously by Harness² on {timestamp}.",
            "> Re-used automatically in subsequent sessions to avoid starting from scratch.",
            "",
            "## When to Use",
            f"- When addressing: {workflow.goal}",
            f"- Tools involved: {', '.join(f'`{t}`' for t in workflow.tools_involved)}",
            "",
            "## Standard Execution Procedure",
        ]

        for idx, step in enumerate(workflow.steps, 1):
            lines.append(f"{idx}. {step}")

        if events:
            lines.append("")
            lines.append("## Known Pitfalls & Verified Solutions (Harness² Contrasts)")
            for ev in events:
                lines.append(f"> [!TIP]")
                lines.append(f"> **Issue encountered**: `{ev.error_message[:100]}`")
                lines.append(f"> **Verified fix**: {ev.resolution_summary}")
                lines.append("")

        lines.extend([
            "",
            "## Verification & Next Steps",
            "1. Verify execution outputs and exit codes.",
            "2. Ensure all artifacts and test suites pass.",
            "3. If new errors occur, Harness² will record the contrast and update this skill automatically.",
        ])

        return "\n".join(lines)

    def _update_existing_skill(
        self,
        skill_file: Path,
        workflow: WorkflowCandidate,
        events: Sequence[ContrastiveEvent],
        timestamp: str,
    ) -> str:
        """Performs sequential refinement on an existing skill, merging new steps and fixes."""
        try:
            existing = skill_file.read_text(encoding="utf-8")
        except Exception:
            return self._generate_fresh_skill(workflow, events, timestamp)

        # Append new procedure variations
        additions = [
            "",
            f"### Sequential Refinement ({timestamp})",
            f"- **Additional Goal Context**: {workflow.goal}",
            "- **Refined Steps**:",
        ]
        for s in workflow.steps:
            additions.append(f"  - {s}")

        if events:
            additions.append("- **New Pitfalls & Workarounds**:")
            for ev in events:
                additions.append(f"  - *{ev.trigger_tool}*: {ev.resolution_summary}")

        # Check if pitfall section exists, or append at the bottom
        return f"{existing.rstrip()}\n\n" + "\n".join(additions) + "\n"

    def list_installed_skills(self) -> list[dict[str, str]]:
        """Scans all registered skills directories and returns discovered skills metadata."""
        discovered = []
        seen = set()

        for sdir in self.skills_dirs:
            if not sdir.exists():
                continue
            for item in sdir.iterdir():
                if item.is_dir():
                    skill_md = item / "SKILL.md"
                    if skill_md.exists() and item.name not in seen:
                        seen.add(item.name)
                        meta = self._parse_skill_metadata(skill_md)
                        discovered.append(meta)

        return discovered

    def _parse_skill_metadata(self, skill_file: Path) -> dict[str, str]:
        """Extracts name and description from SKILL.md YAML frontmatter."""
        name = skill_file.parent.name
        desc = "No description provided."
        try:
            content = skill_file.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = parts[1]
                    lines = frontmatter.splitlines()
                    collecting_desc = False
                    desc_lines = []
                    for line in lines:
                        stripped = line.strip()
                        if stripped.startswith("name:"):
                            collecting_desc = False
                            name = stripped.split("name:", 1)[1].strip()
                        elif stripped.startswith("description:"):
                            val = stripped.split("description:", 1)[1].strip()
                            if val in (">-", ">", "|", "|-") or not val:
                                collecting_desc = True
                                desc_lines = []
                            else:
                                desc = val
                        elif collecting_desc:
                            if stripped and not stripped.startswith("-") and ":" in stripped and not line.startswith(" "):
                                collecting_desc = False
                            else:
                                if stripped:
                                    desc_lines.append(stripped)
                    if desc_lines:
                        desc = " ".join(desc_lines)
        except Exception:
            pass

        return {
            "name": name,
            "description": desc,
            "path": str(skill_file),
            "folder": str(skill_file.parent),
        }

    def _slugify(self, text: str) -> str:
        """Converts arbitrary text into a clean kebab-case skill identifier."""
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
        return cleaned[:35] if cleaned else "harness-procedure"
