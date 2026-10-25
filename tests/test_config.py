"""Tests for path discovery, workspace identity, and small config helpers."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core import config


class TestConfigHelpers(IsolatedHarnessTestCase):
    def test_plugin_paths_follow_environment(self) -> None:
        self.assertEqual(config.get_plugin_dir(), self.plugin_dir)
        self.assertEqual(config.get_memory_dir(), self.plugin_dir / "memory")
        self.assertEqual(config.get_global_user_skills_dir(), self.gemini_dir / "skills")
        self.assertEqual(config.get_workspace_rules_path(self.workspace), self.workspace / "AGENTS.md")
        self.assertEqual(
            config.get_workspace_skills_dir(self.workspace),
            self.workspace / ".agents" / "skills",
        )

        other = self.root / "other-plugin"
        os.environ["HARNESS_PLUGIN_DIR"] = str(other)
        self.assertEqual(config.get_plugin_dir(), other)

    def test_skill_directory_precedence(self) -> None:
        workspace_skills = self.workspace / ".agents" / "skills"
        user_skills = self.gemini_dir / "skills"
        plugin_skills = self.plugin_dir / "skills"
        for path in (workspace_skills, user_skills, plugin_skills):
            path.mkdir(parents=True, exist_ok=True)
        dirs = config.get_skills_dirs(self.workspace)
        self.assertEqual(dirs[0], workspace_skills)
        self.assertEqual(dirs[1], plugin_skills)
        self.assertEqual(dirs[2], user_skills)

    def test_find_workspace_root_uses_markers(self) -> None:
        nested = self.workspace / "src" / "package"
        nested.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        self.assertEqual(config.find_workspace_root(nested / "module.py"), self.workspace.resolve())
        outside = self.root / "no-markers"
        outside.mkdir()
        self.assertEqual(config.find_workspace_root(outside), outside.resolve())

    def test_project_fingerprint_is_stable_and_hides_the_path(self) -> None:
        first = config.project_fingerprint(self.workspace)
        second = config.project_fingerprint(self.workspace)
        other = config.project_fingerprint(self.root / "another-workspace")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(config.project_fingerprint(None), "global")
        self.assertEqual(len(first), 16)
        self.assertNotIn(str(self.workspace).lower(), first)

    def test_detect_project_signals(self) -> None:
        (self.workspace / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {"react": "^19.0.0", "@scope/tool": "^1.0.0"},
                    "devDependencies": {"vite": "^7.0.0", "playwright": "^1.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (self.workspace / "pyproject.toml").write_text(
            "[project]\ndependencies = ['pytest', 'fastapi']\n",
            encoding="utf-8",
        )
        (self.workspace / "Dockerfile").write_text("FROM python\n", encoding="utf-8")
        signals = config.detect_project_signals(self.workspace)
        for expected in ("javascript", "node", "react", "vite", "playwright", "python", "pytest", "fastapi", "docker"):
            self.assertIn(expected, signals)
        self.assertEqual(config.detect_project_signals(None), [])

    def test_slugify_and_tokenize(self) -> None:
        self.assertEqual(config.slugify("Fix: pytest -q / CI!"), "fix-pytest-q-ci")
        self.assertEqual(config.slugify("***"), "harness-skill")
        tokens = config.tokenize("Please run pytest with UTF-8 and retry after 500 errors")
        self.assertIn("pytest", tokens)
        self.assertIn("utf-8", tokens)
        self.assertIn("retry", tokens)
        self.assertNotIn("please", tokens)
        self.assertNotIn("500", tokens)

    def test_resolve_transcript_path_prefers_newest_known_location(self) -> None:
        conversation = "conv-123"
        older = self.app_data / "brain" / conversation / ".system_generated" / "logs" / "transcript.jsonl"
        newer = self.app_data / "brain" / conversation / "transcript_full.jsonl"
        self.write_text(older, "{}\n")
        self.write_text(newer, "{}\n")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        resolved = config.resolve_transcript_path(conversation)
        self.assertEqual(resolved, newer)

        explicit = self.write_text(self.root / "explicit.jsonl", "{}\n")
        self.assertEqual(config.resolve_transcript_path("", explicit), explicit)
        self.assertIsNone(config.resolve_transcript_path("missing-conversation"))

    def test_missing_transcript_is_none(self) -> None:
        self.assertIsNone(config.resolve_transcript_path("not-there"))

    def test_safe_conversation_id_blocks_path_traversal(self) -> None:
        self.assertEqual(config.safe_conversation_id("conv-123.test"), "conv-123.test")
        for unsafe in ("../escape", "..\\escape", "a/b", "a" * 200, ""):
            with self.subTest(value=unsafe):
                safe = config.safe_conversation_id(unsafe)
                if unsafe:
                    self.assertNotIn("/", safe)
                    self.assertNotIn("\\", safe)
                    self.assertNotIn("..", safe)
                    self.assertLessEqual(len(safe), 128)
                else:
                    self.assertEqual(safe, "")
        self.assertEqual(
            config.safe_conversation_id("../escape"),
            config.safe_conversation_id("../escape"),
        )


if __name__ == "__main__":
    unittest.main()
