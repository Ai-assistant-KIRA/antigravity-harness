"""
SessionStart briefing for Harness².

The briefing is short by design. It surfaces the skills and verified lessons most
likely to matter for the current workspace, then gets out of the agent's way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .agents_md_manager import AgentsMdManager
from .config import (
    detect_project_signals,
    get_plugin_rules_path,
    get_workspace_rules_path,
    project_skills_only,
)
from .memory_store import MemoryStore
from .settings import get_setting, load_settings
from .skill_builder import SkillBuilder
from .util import one_line, truncate

MAX_BRIEFING_CHARS = 7_000


class SessionLoader:
    """Builds the dynamic system message injected at session start."""

    def __init__(self, workspace_path: Path | str | None = None):
        self.workspace_path = Path(workspace_path).expanduser() if workspace_path else None
        self.settings, self.settings_warnings = load_settings()
        self.max_briefing_chars = max(
            1, int(get_setting(self.settings, "briefing.max_chars", MAX_BRIEFING_CHARS))
        )
        self.max_skills = max(
            0, int(get_setting(self.settings, "briefing.max_skills", 8))
        )
        self.max_lessons = max(
            0, int(get_setting(self.settings, "briefing.max_lessons", 5))
        )
        self.skill_builder = SkillBuilder(self.workspace_path)
        self.memory_store = MemoryStore(workspace=self.workspace_path)

    def generate_session_start_payload(
        self,
        payload: dict[str, Any] | None = None,
        workspace_path: Path | str | None = None,
    ) -> dict[str, Any]:
        workspace = self._workspace_from(payload, workspace_path)
        if workspace and str(workspace) != str(self.workspace_path or ""):
            # The hook payload is authoritative; rebuild scoped helpers.
            self.workspace_path = workspace
            self.skill_builder = SkillBuilder(workspace)
            self.memory_store = MemoryStore(workspace=workspace)

        message = self.build_briefing(workspace)
        return {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": message
                    }
                }
            ]
        }

    def build_briefing(self, workspace: Path | str | None = None) -> str:
        workspace_path = Path(workspace).expanduser() if workspace else self.workspace_path
        signals = detect_project_signals(workspace_path)
        skills = self._rank_skills(workspace_path, signals)
        lessons = self._relevant_lessons(workspace_path, signals)
        shown_skills = skills[: self.max_skills] if self.max_skills else []
        shown_lessons = lessons[: self.max_lessons] if self.max_lessons else []
        rendered: list[tuple[str, dict[str, Any], str]] = []

        lines = [
            "⚡ **Harness² Self-Improvement System Active**",
            "> Learned skills, verified lessons, and evolved AGENTS.md rules are available.",
            "> Prefer an existing skill when its trigger matches; do not re-derive a known procedure.",
            "",
        ]

        if workspace_path:
            lines.append(f"**Workspace**: `{workspace_path}`")
            if signals:
                lines.append(f"**Detected stack**: {', '.join(signals[:10])}")
            rules_path = get_workspace_rules_path(workspace_path)
            if rules_path.exists():
                size = rules_path.stat().st_size
                lines.append(f"**Workspace rules**: `{rules_path.name}` ({size} bytes)")
            routing = AgentsMdManager(workspace_path).routing("workspace")
            if routing:
                selected = [item for item in routing if not item.lower().startswith("task:")]
                if selected:
                    lines.append(
                        "**Task routing (AGENTS.md)**: "
                        + "; ".join(one_line(item, 140) for item in selected[:3])
                    )
            lines.append("")

        if shown_skills:
            lines.append(f"**Relevant learned skills** ({len(shown_skills)} shown):")
            for skill in shown_skills:
                scope = skill.get("scope", "global")
                desc = one_line(skill.get("description", ""), 150)
                line = f"- `{skill['name']}` ({scope}): {desc}"
                rendered.append(("skill", skill, line))
                lines.append(line)
            lines.append("")
        elif not skills:
            lines.append(
                "**Learned skills**: none yet. Completed multi-step tasks will be distilled "
                "into reusable skills automatically."
            )
            lines.append("")

        if shown_lessons:
            lines.append("**Verified lessons for this context**:")
            for lesson in shown_lessons:
                tool = lesson.get("trigger_tool") or "tool"
                resolution = one_line(lesson.get("resolution", ""), 180)
                line = f"- `{tool}`: {resolution}"
                rendered.append(("lesson", lesson, line))
                lines.append(line)
            lines.append("")

        rules = get_plugin_rules_path()
        if rules.exists():
            try:
                size = rules.stat().st_size
                if size > 20_000:
                    lines.append(
                        f"⚠️ Global rules file is {size} bytes; run `harness rules sanitize` "
                        "to stay under Antigravity's 24 KB cap."
                    )
                    lines.append("")

            except OSError:
                pass

        lines.append(
            "At the end of a session, Harness² records verified failure/recovery lessons, "
            "updates workspace rules, and synthesises or refines skills. It never promotes "
            "an unresolved error into a rule."
        )
        text = "\n".join(lines).strip()
        if len(text) > self.max_briefing_chars:
            text = truncate(text, self.max_briefing_chars, "\n\n[Briefing truncated]")
            visible = text[: -len("\n\n[Briefing truncated]")].rstrip()
        else:
            visible = text

        # Record only the entries that survived the final truncation. Sequential
        # lookup keeps duplicate lines distinct and preserves display order.
        included: list[tuple[str, dict[str, Any]]] = []
        cursor = 0
        for kind, item, line in rendered:
            position = visible.find(line, cursor)
            if position < 0:
                continue
            cursor = position + len(line)
            included.append((kind, item))

        lesson_ids = [
            int(item["id"])
            for kind, item in included
            if kind == "lesson" and item.get("id") is not None
        ]
        surfaced_skills = [
            {"name": item.get("name"), "scope": item.get("scope")}
            for kind, item in included
            if kind == "skill" and item.get("name")
        ]
        try:
            self.memory_store.record_lessons_surfaced(lesson_ids)
            self.memory_store.record_skills_surfaced(surfaced_skills)
        except Exception:
            pass
        return text

    def _workspace_from(
        self,
        payload: dict[str, Any] | None,
        workspace_path: Path | str | None,
    ) -> Path | None:
        if workspace_path:
            return Path(workspace_path).expanduser()
        choices: list[str] = []
        if payload:
            raw = payload.get("workspacePaths") or payload.get("workspacePath") or []
            if isinstance(raw, str):
                choices = [raw]
            elif isinstance(raw, (list, tuple)):
                choices = [str(item) for item in raw if item]
            if not choices:
                cwd = payload.get("cwd") or payload.get("workingDirectory")
                if cwd:
                    choices = [str(cwd)]
        if not choices and self.workspace_path:
            return self.workspace_path
        for choice in choices:
            path = Path(choice).expanduser()
            if path.exists() and path.is_dir():
                return path
        return Path(choices[0]).expanduser() if choices else self.workspace_path

    def _rank_skills(
        self,
        workspace: Path | None,
        signals: Iterable[str],
    ) -> list[dict[str, Any]]:
        installed = self.skill_builder.list_installed_skills(
            workspace, include_global=not project_skills_only()
        )
        by_name: dict[str, list[dict[str, Any]]] = {}
        for record in self.memory_store.get_all_skills(
            workspace, include_global=True
        ):
            by_name.setdefault(str(record.get("name") or ""), []).append(record)

        skills: list[dict[str, Any]] = []
        for skill in installed:
            if project_skills_only() and skill.get("scope") != "workspace":
                continue
            enriched = dict(skill)
            record = MemoryStore._pick_skill_candidate(
                by_name.get(str(skill.get("name") or ""), []),
                str(skill.get("scope") or "") or None,
                None,
                self.memory_store.project,
            )
            try:
                quality = float(skill.get("quality") or 0)
            except (TypeError, ValueError):
                quality = 0.0
            if record:
                try:
                    indexed_quality = float(record.get("quality") or 0)
                except (TypeError, ValueError):
                    indexed_quality = 0.0
                quality = max(quality, indexed_quality)
                enriched["reused_count"] = int(record.get("reused_count") or 0)
                enriched["surfaced_count"] = int(record.get("surfaced_count") or 0)
                enriched["last_used_at"] = record.get("last_used_at")
                enriched["updated_at"] = record.get("updated_at")
            enriched["quality"] = quality
            skills.append(enriched)

        signal_tokens = {signal.lower() for signal in signals}

        def score(skill: dict[str, Any]) -> tuple[float, int, str]:
            value = 0.0
            if skill.get("scope") == "workspace":
                value += 4
            elif skill.get("scope") == "plugin":
                value += 1
            if skill.get("managed"):
                value += 1
            text = f"{skill.get('name', '')} {skill.get('description', '')}".lower()
            value += sum(2 for signal in signal_tokens if signal and signal in text)
            value += float(self.memory_store.skill_utility(skill))
            return (-value, 0 if skill.get("valid") else 1, skill.get("name", ""))

        return sorted(skills, key=score)

    def _relevant_lessons(
        self,
        workspace: Path | None,
        signals: Iterable[str],
    ) -> list[dict[str, Any]]:
        query = " ".join(signals)
        if self.max_lessons <= 0:
            return []
        if query:
            lessons = self.memory_store.search_lessons(
                query,
                limit=self.max_lessons,
                workspace=workspace,
                verified_only=True,
            )
            if lessons:
                return lessons
        return self.memory_store.get_recent_lessons(
            limit=self.max_lessons,
            workspace=workspace,
            verified_only=True,
        )
