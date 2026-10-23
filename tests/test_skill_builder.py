"""
Unit tests for SkillBuilder.
"""

import tempfile
import unittest
from pathlib import Path
from harness.core.skill_builder import SkillBuilder
from harness.core.analyzer import WorkflowCandidate, ContrastiveEvent


class TestSkillBuilder(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name)
        self.builder = SkillBuilder(self.workspace_dir)
        self.test_skills_dir = self.workspace_dir / ".agents" / "skills"
        self.test_skills_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_build_fresh_skill(self):
        wf = WorkflowCandidate(
            name="vite-cloudflare-deploy",
            description="Builds and deploys Vite application to Cloudflare Pages",
            goal="Deploy Vite app to Cloudflare Pages",
            steps=[
                "Run `npm run build` to generate dist folder",
                "Execute `wrangler pages deploy dist --project-name my-app`",
                "Verify deployed URL status code is 200",
            ],
            tools_involved=["run_command"],
        )
        events = [
            ContrastiveEvent(
                trigger_tool="run_command",
                failed_args={},
                error_message="wrangler: command not found",
                successful_args={},
                resolution_summary="Install wrangler via `npm install -g wrangler` or use `npx wrangler`",
                category="command",
            )
        ]

        skill_path = self.builder.build_or_update_skill(wf, events, target_dir=self.test_skills_dir)
        self.assertTrue(skill_path.exists())
        self.assertEqual(skill_path.name, "SKILL.md")

        content = skill_path.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---"))
        self.assertIn("name: vite-cloudflare-deploy", content)
        self.assertIn("description:", content)
        self.assertIn("wrangler pages deploy dist", content)
        self.assertIn("Known Pitfalls & Verified Solutions", content)
        self.assertIn("npx wrangler", content)

    def test_sequential_refinement(self):
        wf1 = WorkflowCandidate(
            name="test-runner",
            description="Run unit tests",
            goal="Execute unit test suite",
            steps=["pytest tests/"],
            tools_involved=["run_command"],
        )
        skill_path = self.builder.build_or_update_skill(wf1, [], target_dir=self.test_skills_dir)
        first_content = skill_path.read_text(encoding="utf-8")

        # Refine with new steps in subsequent session
        wf2 = WorkflowCandidate(
            name="test-runner",
            description="Run unit tests with coverage",
            goal="Execute unit tests with coverage reporting",
            steps=["pytest --cov=src tests/"],
            tools_involved=["run_command"],
        )
        self.builder.build_or_update_skill(wf2, [], target_dir=self.test_skills_dir)
        second_content = skill_path.read_text(encoding="utf-8")

        self.assertIn("Sequential Refinement", second_content)
        self.assertIn("pytest --cov=src tests/", second_content)


if __name__ == "__main__":
    unittest.main()
