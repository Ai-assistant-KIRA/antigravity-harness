"""Settings are applied by the engine, loader, and CLI defaults."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.cli import main
from harness.core.engine import HarnessEngine
from harness.core.loader import SessionLoader
from harness.core.memory_store import MemoryStore


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


def tool(name: str, **arguments: object) -> dict:
    return {
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": name, "arguments": arguments}],
    }


def output(content: str, status: str = "DONE") -> dict:
    return {
        "type": "CORTEX_STEP_TYPE_OUTPUT",
        "content": content,
        "status": status,
    }


def learning_session() -> list[dict]:
    return [
        user("Install pytest and run the test suite"),
        tool("run_command", CommandLine="pytest -q"),
        output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
        tool("run_command", CommandLine="pip install pytest"),
        output("Successfully installed pytest"),
        tool("run_command", CommandLine="pytest -q"),
        output("5 passed"),
    ]


class SettingsIntegrationTest(IsolatedHarnessTestCase):
    def write_settings(self, settings: dict) -> None:
        self.write_text(
            self.plugin_dir / "harness.json",
            json.dumps(settings, indent=2),
        )

    def test_rules_budget_is_applied_to_the_manager(self) -> None:
        self.write_settings({"rules": {"max_bytes": 1200}})
        engine = HarnessEngine(self.workspace)
        self.assertEqual(engine.agents_manager.max_bytes, 1200)

    def test_skill_cap_is_applied_to_the_cycle(self) -> None:
        self.write_settings({"skills": {"max_per_session": 0}})
        engine = HarnessEngine(self.workspace)
        result = engine.run_cycle("conv-settings-skills", raw_steps=learning_session())
        self.assertEqual(result.status, "processed")
        self.assertEqual(result.skills_synthesized, [])
        self.assertFalse((self.workspace / ".agents" / "skills").exists())

    def test_briefing_caps_are_applied(self) -> None:
        self.write_settings({"briefing": {"max_lessons": 0, "max_skills": 0}})
        MemoryStore(workspace=self.workspace).add_lesson(
            trigger_tool="run_command",
            error_pattern="ModuleNotFoundError: No module named 'pytest'",
            resolution="Install pytest before running the suite.",
            verified=True,
            workspace=self.workspace,
        )
        briefing = SessionLoader(self.workspace).build_briefing(self.workspace)
        self.assertNotIn("Verified lessons for this context", briefing)
        self.assertNotIn("Relevant learned skills", briefing)
        self.assertNotIn("Install pytest before running the suite", briefing)

    def test_invalid_settings_produce_a_cycle_warning(self) -> None:
        self.write_text(self.plugin_dir / "harness.json", "{not valid json")
        engine = HarnessEngine(self.workspace)
        result = engine.run_cycle("conv-settings-invalid", raw_steps=[user("hello")])
        self.assertTrue(
            any("settings:" in warning for warning in result.warnings),
            result.warnings,
        )

    def test_cli_recall_and_privacy_are_wired(self) -> None:
        MemoryStore(workspace=self.workspace).add_lesson(
            trigger_tool="run_command",
            error_pattern="ModuleNotFoundError: No module named 'pytest'",
            resolution="Install pytest with `pip install pytest`.",
            verified=True,
            workspace=self.workspace,
        )
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(
                [
                    "recall",
                    "ModuleNotFoundError pytest",
                    "--json",
                    "--workspace",
                    str(self.workspace),
                ]
            )
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["lessons"])
        self.assertIn("pip install pytest", payload["message"])

        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(
                [
                    "privacy",
                    "audit",
                    "--json",
                    "--workspace",
                    str(self.workspace),
                ]
            )
        report = json.loads(out.getvalue())
        self.assertEqual(report["workspace"], str(self.workspace))
        self.assertIn("files", report)
        self.assertIn("recommendations", report)


if __name__ == "__main__":
    unittest.main()
