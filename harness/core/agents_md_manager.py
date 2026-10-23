"""
Manages the self-improving evolution of AGENTS.md rules files.
Preserves user customizations while continuously incorporating verified heuristics,
pitfalls avoided, environment quirks, and standing user preferences.
"""

from __future__ import annotations
import re
from pathlib import Path
from typing import Sequence
from .config import get_workspace_rules_path, get_plugin_rules_path
from .analyzer import ContrastiveEvent


HARNESS_HEADER = "<!-- HARNESS:AUTONOMOUS_RULES_START -->"
HARNESS_FOOTER = "<!-- HARNESS:AUTONOMOUS_RULES_END -->"
MAX_RULE_FILE_BYTES = 20_000  # Safe threshold well within Antigravity's 24KB cap


class AgentsMdManager:
    """Safely and iteratively evolves AGENTS.md files based on session learnings and memory."""

    def __init__(self, workspace_path: Path | None = None):
        self.workspace_path = workspace_path
        self.workspace_agents_md = get_workspace_rules_path(workspace_path)
        self.plugin_agents_md = get_plugin_rules_path()

    def update_rules(
        self,
        contrastive_events: Sequence[ContrastiveEvent],
        user_constraints: Sequence[str],
        environment_quirks: Sequence[str],
        target: str = "both",  # "workspace", "plugin", or "both"
    ) -> dict[str, Any]:
        """Evolves AGENTS.md with new contrastive lessons, quirks, and constraints."""
        new_heuristics = []
        new_pitfalls = []
        new_constraints = list(user_constraints)

        for event in contrastive_events:
            rule_entry = f"**{event.trigger_tool}**: {event.resolution_summary}"
            if event.category in ("environment", "command"):
                new_heuristics.append(rule_entry)
            else:
                new_pitfalls.append(rule_entry)

        for quirk in environment_quirks:
            new_heuristics.append(f"**Environment**: {quirk}")

        results = {}

        if target in ("workspace", "both"):
            results["workspace"] = self._apply_to_file(
                self.workspace_agents_md,
                new_heuristics,
                new_pitfalls,
                new_constraints,
                is_workspace=True,
            )

        if target in ("plugin", "both"):
            results["plugin"] = self._apply_to_file(
                self.plugin_agents_md,
                new_heuristics,
                new_pitfalls,
                new_constraints,
                is_workspace=False,
            )

        return results

    def _apply_to_file(
        self,
        file_path: Path,
        heuristics: list[str],
        pitfalls: list[str],
        constraints: list[str],
        is_workspace: bool,
    ) -> bool:
        """Injects or updates the Harness managed section in the given AGENTS.md file."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        existing_content = ""
        if file_path.exists():
            try:
                existing_content = file_path.read_text(encoding="utf-8")
            except Exception:
                existing_content = ""

        # Separate user content from previous Harness section
        user_content, prev_section = self._split_content(existing_content)

        # Parse previously recorded rules from the previous section
        prev_rules = self._parse_section(prev_section)

        # Merge new rules into previous rules with deduplication
        merged_heuristics = self._merge_rule_list(prev_rules.get("heuristics", []), heuristics)
        merged_pitfalls = self._merge_rule_list(prev_rules.get("pitfalls", []), pitfalls)
        merged_constraints = self._merge_rule_list(prev_rules.get("constraints", []), constraints)

        # Build fresh managed block
        managed_block = self._format_managed_block(
            merged_heuristics, merged_pitfalls, merged_constraints, is_workspace
        )

        # Combine preserving user content
        if user_content.strip():
            final_content = f"{user_content.rstrip()}\n\n{managed_block}\n"
        else:
            final_content = f"{managed_block}\n"

        # Check budget limit and prune if necessary
        if len(final_content.encode("utf-8")) > MAX_RULE_FILE_BYTES:
            final_content = self._prune_rules(user_content, merged_heuristics, merged_pitfalls, merged_constraints, is_workspace)

        file_path.write_text(final_content, encoding="utf-8")
        return True

    def _split_content(self, text: str) -> tuple[str, str]:
        """Splits file into user content and the Harness managed block."""
        if HARNESS_HEADER in text and HARNESS_FOOTER in text:
            start_idx = text.find(HARNESS_HEADER)
            end_idx = text.find(HARNESS_FOOTER) + len(HARNESS_FOOTER)
            user_part = text[:start_idx] + text[end_idx:]
            managed_part = text[start_idx:end_idx]
            return user_part.strip(), managed_part
        return text.strip(), ""

    def _parse_section(self, section_text: str) -> dict[str, list[str]]:
        """Parses rules out of a previous Harness managed block."""
        data: dict[str, list[str]] = {"heuristics": [], "pitfalls": [], "constraints": []}
        if not section_text:
            return data

        current_key: str | None = None
        for line in section_text.splitlines():
            line_str = line.strip()
            if "Environment & Tooling Quirks" in line_str:
                current_key = "heuristics"
            elif "Learned Pitfalls & Recovery Invariants" in line_str:
                current_key = "pitfalls"
            elif "Standing Project Directives" in line_str:
                current_key = "constraints"
            elif line_str.startswith("- ") and current_key:
                rule = line_str[2:].strip()
                if rule and rule not in data[current_key]:
                    data[current_key].append(rule)

        return data

    def _merge_rule_list(self, existing: list[str], new_items: list[str]) -> list[str]:
        """Merges rules, avoiding exact duplicates or near-duplicates."""
        merged = list(existing)
        for item in new_items:
            item_clean = item.strip()
            if not item_clean:
                continue
            # Check for high similarity
            duplicate = False
            for prev in merged:
                if item_clean == prev or self._similarity(item_clean, prev) > 0.85:
                    duplicate = True
                    break
            if not duplicate:
                merged.append(item_clean)
        return merged

    def _similarity(self, s1: str, s2: str) -> float:
        """Simple token-based Jaccard similarity for rule deduplication."""
        words1 = set(re.findall(r"\w+", s1.lower()))
        words2 = set(re.findall(r"\w+", s2.lower()))
        if not words1 or not words2:
            return 0.0
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        return len(intersection) / len(union)

    def _format_managed_block(
        self,
        heuristics: list[str],
        pitfalls: list[str],
        constraints: list[str],
        is_workspace: bool,
    ) -> str:
        """Formats the structured Harness rules block."""
        title = "Workspace Rules" if is_workspace else "Global Plugin Rules"
        lines = [
            HARNESS_HEADER,
            f"## ⚡ Harness² Evolved Directives ({title})",
            "> *These rules are autonomously maintained by the Harness² self-improvement engine.",
            "> They record verified platform heuristics, failure preventions, and standing directives.*",
            "",
        ]

        if constraints:
            lines.append("### Standing Project Directives")
            for c in constraints:
                lines.append(f"- {c}")
            lines.append("")

        if heuristics:
            lines.append("### Environment & Tooling Quirks")
            for h in heuristics:
                lines.append(f"- {h}")
            lines.append("")

        if pitfalls:
            lines.append("### Learned Pitfalls & Recovery Invariants")
            for p in pitfalls:
                lines.append(f"- {p}")
            lines.append("")

        lines.append(HARNESS_FOOTER)
        return "\n".join(lines)

    def _prune_rules(
        self,
        user_content: str,
        heuristics: list[str],
        pitfalls: list[str],
        constraints: list[str],
        is_workspace: bool,
    ) -> str:
        """Prunes the oldest rules if the file approaches the size limit."""
        # Retain most recent items
        pruned_heuristics = heuristics[-15:]
        pruned_pitfalls = pitfalls[-15:]
        pruned_constraints = constraints[-15:]
        managed = self._format_managed_block(
            pruned_heuristics, pruned_pitfalls, pruned_constraints, is_workspace
        )
        return f"{user_content.strip()}\n\n{managed}\n"
