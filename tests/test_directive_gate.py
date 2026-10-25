"""Tests for the standing-directive gate and self-reference filtering."""

from __future__ import annotations

import unittest

try:
    from .support import IsolatedHarnessTestCase, make_event
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event

from harness.core.agents_md_manager import (
    HARNESS_FOOTER,
    HARNESS_HEADER,
    AgentsMdManager,
    _is_self_referential,
    _is_standing_directive,
)


class TestStandingDirectiveGate(IsolatedHarnessTestCase):
    def test_build_rules_keeps_only_the_standing_directive(self) -> None:
        manager = AgentsMdManager(self.workspace)
        rules = manager._build_rules(
            [],
            [
                "Make sure to epidemic sound mcp is installed and generate a better "
                "more human natural sounding voiceover",
                "Never require us to use different models",
            ],
            [],
            False,
        )
        self.assertEqual(rules.directives, ["Never require us to use different models"])

    def test_standing_directive_classification(self) -> None:
        accepted = [
            "Never require us to use different models",
            "Always run pytest before committing",
        ]
        rejected = [
            "Make sure to epidemic sound mcp is installed and generate a better "
            "more human natural sounding voiceover",
            "Remember to run tests before committing",
            "Please update the landing page",
        ]
        for value in accepted:
            with self.subTest(value=value):
                self.assertTrue(_is_standing_directive(value))
        for value in rejected:
            with self.subTest(value=value):
                self.assertFalse(_is_standing_directive(value))

    def test_repair_drops_task_instructions_and_plugin_self_reference(self) -> None:
        user_text = (
            "# Landing Page Notes\n\n"
            "Keep this handwritten paragraph exactly as written."
        )
        block = (
            f"{HARNESS_HEADER}\n"
            "## Harness Evolved Directives (Workspace)\n"
            "### Standing Project Directives\n"
            "- Make sure to epidemic sound mcp is installed and generate a better "
            "more human natural sounding voiceover\n"
            "- Never require us to use different models\n"
            "### Learned Pitfalls & Recovery Invariants\n"
            "- read_file: "
            "`C:\\Users\\Lenovo\\.gemini\\config\\plugins\\harness\\memory\\EXPERIENCE.md` "
            "failed with `file does not exist`.\n"
            f"{HARNESS_FOOTER}\n"
        )
        original = f"{user_text}\n\n{block}"
        self.write_text(self.workspace / "AGENTS.md", original)

        manager = AgentsMdManager(self.workspace)
        result = manager.repair(target="workspace")
        content = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")

        self.assertTrue(result["workspace"]["updated"])
        self.assertEqual(result["workspace"]["removed"], 2)
        self.assertEqual(result["workspace"]["kept"], 1)
        self.assertNotIn("epidemic sound", content)
        self.assertNotIn("EXPERIENCE.md", content)
        self.assertIn("Never require us to use different models", content)
        self.assertEqual(
            content.split(HARNESS_HEADER, 1)[0],
            original.split(HARNESS_HEADER, 1)[0],
        )

    def test_system_message_fragments_are_not_persisted(self) -> None:
        event = make_event(
            trigger_tool="write_file",
            error_message="Traceback (most recent call last): </SYSTEM_MESSAGE>",
            resolution_summary=(
                "`write_file` failed with `Traceback (most recent call last): "
                "</SYSTEM_MESSAGE>`; retrying succeeded."
            ),
            category="command",
            verified=True,
        )
        manager = AgentsMdManager(self.workspace)
        rules = manager._build_rules([event], [], [], False)
        self.assertEqual(rules.pitfalls, [])

    def test_plugin_path_quirk_is_not_persisted(self) -> None:
        plugin_path = str(self.plugin_dir / "memory" / "EXPERIENCE.md")
        manager = AgentsMdManager(self.workspace)
        rules = manager._build_rules([], [], [plugin_path], False)
        self.assertEqual(rules.quirks, [])

    def test_doubled_separators_are_still_self_referential(self) -> None:
        self.assertTrue(
            _is_self_referential(
                "C:\\Users\\Lenovo\\.gemini//config//plugins//harness\\memory\\EXPERIENCE.md"
            )
        )

    def test_normal_environment_quirk_still_persists(self) -> None:
        manager = AgentsMdManager(self.workspace)
        rules = manager._build_rules([], [], ["Windows PowerShell 5.1"], False)
        self.assertTrue(any("Windows PowerShell 5.1" in rule for rule in rules.quirks))

    def test_verified_event_referencing_plugin_root_is_not_persisted(self) -> None:
        event = make_event(
            trigger_tool="read_file",
            error_message="file does not exist",
            resolution_summary=(
                f"Use the hook runner at {self.plugin_dir}\\scripts\\hook_runner.py "
                "after installation."
            ),
            category="command",
            verified=True,
        )
        manager = AgentsMdManager(self.workspace)
        result = manager.update_rules([event], [], [], target="workspace")

        self.assertFalse(result["workspace"]["updated"])
        self.assertFalse((self.workspace / "AGENTS.md").exists())


if __name__ == "__main__":
    unittest.main()
