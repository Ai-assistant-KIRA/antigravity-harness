"""Regression tests for workspace scoping, rule quality, and hook import cost."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from harness import hooks
from harness.core.agents_md_manager import _is_standing_directive
from harness.core.config import bind_conversation_workspace, get_bound_workspace
from harness.core.engine import HarnessEngine
from harness.core.util import is_chatty_directive
from support import IsolatedHarnessTestCase, make_event


def session_steps(path: str) -> list[dict]:
    return [
        {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": f"Fix the failing test in {path}"},
        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": f'python -X utf8 "{path}"'}}]},
        {"type": "CORTEX_STEP_TYPE_OUTPUT", "content": "ModuleNotFoundError: No module named 'pytest'", "status": "ERROR"},
        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "pip install pytest"}}]},
        {"type": "CORTEX_STEP_TYPE_OUTPUT", "content": "Successfully installed pytest", "status": "DONE"},
        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": f'python -X utf8 "{path}"'}}]},
        {"type": "CORTEX_STEP_TYPE_OUTPUT", "content": "5 passed", "status": "DONE"},
    ]


class TestWorkspaceScope(IsolatedHarnessTestCase):
    def test_conversation_binding_survives_missing_workspace_paths(self) -> None:
        bind_conversation_workspace("conv-bound", self.workspace)
        # Compare resolved paths: Windows runners hand out 8.3 temp paths
        # (RUNNER~1) while the binding stores the long form.
        self.assertEqual(get_bound_workspace("conv-bound").resolve(), self.workspace.resolve())

        resolved = hooks._resolve_workspace({"conversationId": "conv-bound"})
        self.assertEqual(resolved.resolve(), self.workspace.resolve())

    def test_unbound_conversation_never_falls_back_to_cwd(self) -> None:
        self.assertEqual(hooks._resolve_workspace({"conversationId": "conv-unbound"}), None)
        self.assertEqual(hooks._resolve_workspace({}), None)

    def test_payload_workspace_is_remembered(self) -> None:
        hooks._resolve_workspace(
            {"conversationId": "conv-payload", "workspacePaths": [str(self.workspace)]}
        )
        self.assertEqual(get_bound_workspace("conv-payload").resolve(), self.workspace.resolve())

    def test_foreign_transcript_writes_nothing(self) -> None:
        engine = HarnessEngine(self.workspace)
        foreign = r"C:\Users\Someone\Desktop\other project\scripts\build.py"
        result = engine.run_cycle(
            conversation_id="conv-foreign",
            raw_steps=session_steps(foreign),
        )
        self.assertEqual(result.status, "skipped")
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertFalse((self.workspace / ".agents" / "skills").exists())

    def test_matching_transcript_still_processes(self) -> None:
        engine = HarnessEngine(self.workspace)
        local = str(self.workspace / "scripts" / "build.py")
        result = engine.run_cycle(
            conversation_id="conv-local",
            raw_steps=session_steps(local),
        )
        self.assertEqual(result.status, "processed")
        self.assertTrue((self.workspace / "AGENTS.md").exists())

    def test_foreign_path_lesson_is_dropped(self) -> None:
        engine = HarnessEngine(self.workspace)
        event = make_event(
            error_message=r"python C:\Users\Someone\other project\run.py failed",
            resolution_summary=(
                r"Use `C:\Users\Someone\other project\run.py` instead of the relative path."
            ),
        )
        from harness.core.engine import EngineResult

        kept = engine._drop_foreign_path_events([event], self.workspace, EngineResult())
        self.assertEqual(kept, [])

    def test_cycle_without_workspace_writes_no_project_files(self) -> None:
        engine = HarnessEngine(None)
        local = str(Path.cwd() / "scripts" / "build.py")
        result = engine.run_cycle(conversation_id="conv-nows", raw_steps=session_steps(local))
        self.assertEqual(result.status, "processed")
        self.assertFalse((Path.cwd() / "AGENTS.md").exists())
        self.assertFalse((Path.cwd() / ".agents" / "skills").exists())


class TestRuleQuality(IsolatedHarnessTestCase):
    def test_repair_drops_pitfalls_from_another_project(self) -> None:
        from harness.core.agents_md_manager import (
            HARNESS_FOOTER,
            HARNESS_HEADER,
            AgentsMdManager,
        )

        foreign = r"C:\Users\Someone\Desktop\other project\run.py"
        agents = self.workspace / "AGENTS.md"
        agents.write_text(
            "# Project\n\n"
            f"{HARNESS_HEADER}\n"
            "## Harness Evolved Directives (Workspace)\n\n"
            "### Learned Pitfalls & Recovery Invariants\n"
            f"- **run_command**: `python {foreign}` failed; use the absolute path instead.\n"
            "- **run_command**: `pytest -q` failed; install pytest first.\n\n"
            f"{HARNESS_FOOTER}\n",
            encoding="utf-8",
            newline="\n",
        )
        result = AgentsMdManager(self.workspace).repair(target="workspace")
        text = agents.read_text(encoding="utf-8")
        self.assertNotIn("other project", text)
        self.assertIn("install pytest first", text)
        self.assertEqual(result["workspace"]["removed"], 1)

    NOISE = [
        "Never want scenes to popping up out of no where i want motion graphics",
        "Never needed from canva to include in our videos to make them look better",
        "Always use them when editing and making videos it's the core of our workflow",
        "Never like the text fonts use better looking ones",
        "Never make the background based off him",
    ]
    GOOD = [
        "Always run `python -X utf8` when invoking scripts on Windows.",
        "Never commit secrets or tokens into the repository.",
        "Prefer `pnpm` over `npm` in this project.",
    ]

    def test_chatty_directives_are_rejected(self) -> None:
        for text in self.NOISE:
            with self.subTest(text=text):
                self.assertTrue(is_chatty_directive(text))
                self.assertFalse(_is_standing_directive(text))

    def test_actionable_directives_survive(self) -> None:
        for text in self.GOOD:
            with self.subTest(text=text):
                self.assertFalse(is_chatty_directive(text))
                self.assertTrue(_is_standing_directive(text))


class TestHookImportBudget(IsolatedHarnessTestCase):
    def test_post_tool_use_does_not_import_the_engine_at_module_load(self) -> None:
        code = (
            "import sys; import harness.hooks; "
            "print('harness.core.engine' in sys.modules); "
            "print('harness.core.retrieval' in sys.modules)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        output = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout.split(), ["False", "False"])


if __name__ == "__main__":
    unittest.main()
