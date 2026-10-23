"""
Unit tests for end-to-end HarnessEngine and lifecycle hooks.
"""

import tempfile
import unittest
from pathlib import Path
from harness.core.engine import HarnessEngine
from harness.hooks import handle_session_start, handle_stop


class TestEngineAndHooks(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.temp_dir.name)
        self.engine = HarnessEngine(self.workspace_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_cycle_with_raw_steps(self):
        raw_steps = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Deploy database migrations"},
            {
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "alembic upgrade head"}}]
            },
            {
                "type": "CORTEX_STEP_TYPE_OUTPUT",
                "content": "command exited with code 1: Can't connect to local MySQL server through socket",
                "status": "ERROR"
            },
            {
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "docker compose up -d db && alembic upgrade head"}}]
            },
            {
                "type": "CORTEX_STEP_TYPE_OUTPUT",
                "content": "Running migrations... done",
                "status": "DONE"
            }
        ]

        result = self.engine.run_cycle(
            conversation_id="conv-engine-test",
            raw_steps=raw_steps,
        )

        self.assertEqual(result.contrastive_events_found, 1)
        self.assertTrue(result.rules_updated)
        self.assertTrue(len(result.skills_synthesized) >= 1)
        self.assertTrue(result.memory_logged)

        # Verify AGENTS.md in workspace has been updated
        ws_agents = self.workspace_dir / "AGENTS.md"
        self.assertTrue(ws_agents.exists())
        text = ws_agents.read_text(encoding="utf-8")
        self.assertIn("alembic", text)
        self.assertIn("docker compose up", text)

    def test_session_start_hook_payload(self):
        payload = {
            "conversationId": "conv-test",
            "workspacePaths": [str(self.workspace_dir)],
        }
        res = handle_session_start(payload)
        self.assertIn("injectSteps", res)
        self.assertTrue(len(res["injectSteps"]) > 0)
        system_msg = res["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Harness² Self-Improvement System Active", system_msg)


if __name__ == "__main__":
    unittest.main()
