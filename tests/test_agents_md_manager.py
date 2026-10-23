"""
Unit tests for AgentsMdManager.
"""

import tempfile
import unittest
from pathlib import Path
from harness.core.agents_md_manager import AgentsMdManager, HARNESS_HEADER, HARNESS_FOOTER
from harness.core.analyzer import ContrastiveEvent


class TestAgentsMdManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name)
        self.manager = AgentsMdManager(self.workspace_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_evolve_workspace_agents_md(self):
        events = [
            ContrastiveEvent(
                trigger_tool="run_command",
                failed_args={"CommandLine": "npm test"},
                error_message="exit code 1",
                successful_args={"CommandLine": "npm test -- --passWithNoTests"},
                resolution_summary="npm test failed with exit code 1. Resolved by passing --passWithNoTests.",
                category="command",
            )
        ]
        constraints = ["Always use pnpm instead of npm when pnpm-lock.yaml exists."]
        quirks = ["PowerShell requires escaping $ in script arguments."]

        res = self.manager.update_rules(
            contrastive_events=events,
            user_constraints=constraints,
            environment_quirks=quirks,
            target="workspace",
        )
        self.assertTrue(res.get("workspace"))

        agents_file = self.workspace_dir / "AGENTS.md"
        self.assertTrue(agents_file.exists())
        content = agents_file.read_text(encoding="utf-8")

        self.assertIn(HARNESS_HEADER, content)
        self.assertIn(HARNESS_FOOTER, content)
        self.assertIn("Always use pnpm instead of npm", content)
        self.assertIn("PowerShell requires escaping", content)
        self.assertIn("--passWithNoTests", content)

    def test_preserves_user_content_and_deduplicates(self):
        agents_file = self.workspace_dir / "AGENTS.md"
        user_header = "# Custom Project Guidelines\n\n- Do not break existing API routes.\n"
        agents_file.write_text(user_header, encoding="utf-8")

        events = [
            ContrastiveEvent(
                trigger_tool="run_command",
                failed_args={},
                error_message="err",
                successful_args={},
                resolution_summary="Rule A: Use python3 -m venv",
                category="environment",
            )
        ]

        # Update once
        self.manager.update_rules(events, [], [], target="workspace")
        # Update again with identical rule
        self.manager.update_rules(events, [], [], target="workspace")

        content = agents_file.read_text(encoding="utf-8")
        # Ensure user content is preserved
        self.assertIn("# Custom Project Guidelines", content)
        self.assertIn("- Do not break existing API routes.", content)
        # Ensure rule appears only once
        self.assertEqual(content.count("Rule A: Use python3 -m venv"), 1)


if __name__ == "__main__":
    unittest.main()
