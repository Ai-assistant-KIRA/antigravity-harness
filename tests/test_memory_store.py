"""
Unit tests for MemoryStore and EXPERIENCE.md generation.
"""

import tempfile
import unittest
from pathlib import Path
from harness.core.memory_store import MemoryStore
from harness.core.analyzer import SessionAnalysis, ContrastiveEvent


class TestMemoryStore(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.db_path = base / "test_memory.db"
        self.exp_md_path = base / "EXPERIENCE.md"
        self.store = MemoryStore(self.db_path, self.exp_md_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_record_session_and_lessons(self):
        analysis = SessionAnalysis(
            conversation_id="conv-123",
            tasks=["Set up testing suite"],
            contrastive_events=[
                ContrastiveEvent(
                    trigger_tool="run_command",
                    failed_args={"CommandLine": "pytest"},
                    error_message="ModuleNotFoundError: No module named 'pytest'",
                    successful_args={"CommandLine": "pip install pytest && pytest"},
                    resolution_summary="Installed missing dependency pytest before executing tests.",
                    category="dependency",
                )
            ],
            environment_quirks=["Windows uses backslashes in paths."],
        )

        self.store.record_session(analysis)

        lessons = self.store.get_recent_lessons()
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["trigger_tool"], "run_command")
        self.assertIn("dependency pytest", lessons[0]["resolution"])

        # Check that EXPERIENCE.md was generated
        self.assertTrue(self.exp_md_path.exists())
        exp_text = self.exp_md_path.read_text(encoding="utf-8")
        self.assertIn("Harness² Experience Library", exp_text)
        self.assertIn("Installed missing dependency", exp_text)

    def test_record_skill_and_reuse_counter(self):
        self.store.record_skill_saved(
            name="test-skill",
            description="Testing skill description",
            file_path="/path/to/skill/SKILL.md",
        )
        skills = self.store.get_all_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["reused_count"], 0)

        # Update / reuse skill
        self.store.record_skill_saved(
            name="test-skill",
            description="Updated testing skill description",
            file_path="/path/to/skill/SKILL.md",
        )
        skills = self.store.get_all_skills()
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["reused_count"], 1)


if __name__ == "__main__":
    unittest.main()
