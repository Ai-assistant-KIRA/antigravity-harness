"""
Unit tests for TranscriptAnalyzer and contrastive event extraction.
"""

import json
import unittest
from harness.core.analyzer import TranscriptAnalyzer, ContrastiveEvent


class TestTranscriptAnalyzer(unittest.TestCase):

    def setUp(self):
        self.analyzer = TranscriptAnalyzer()

    def test_parse_transcript_lines(self):
        raw_lines = [
            json.dumps({"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Please setup uv for this project."}),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "uv init"}}]
            }),
            json.dumps({"type": "CORTEX_STEP_TYPE_OUTPUT", "content": "Initialized project", "status": "DONE"}),
        ]
        steps = self.analyzer.parse_transcript(raw_lines)
        self.assertEqual(len(steps), 3)

        analysis = self.analyzer.analyze(steps, conversation_id="test-conv-1")
        self.assertEqual(len(analysis.tasks), 1)
        self.assertEqual(analysis.tasks[0], "Please setup uv for this project.")
        self.assertEqual(len(analysis.tool_calls), 1)
        self.assertEqual(analysis.tool_calls[0]["name"], "run_command")

    def test_contrastive_failure_and_recovery(self):
        steps = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Build python package"},
            {
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "python setup.py"}}]
            },
            {
                "type": "CORTEX_STEP_TYPE_OUTPUT",
                "content": "command exited with code 1: error: no setup.py found",
                "status": "ERROR"
            },
            {
                "type": "PLANNER_RESPONSE",
                "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "python -m build"}}]
            },
            {
                "type": "CORTEX_STEP_TYPE_OUTPUT",
                "content": "Successfully built dist/package.whl",
                "status": "DONE"
            }
        ]

        analysis = self.analyzer.analyze(steps, conversation_id="test-contrast-1")
        self.assertEqual(len(analysis.contrastive_events), 1)
        ev = analysis.contrastive_events[0]
        self.assertEqual(ev.trigger_tool, "run_command")
        self.assertIn("setup.py", ev.resolution_summary)
        self.assertIn("python -m build", ev.resolution_summary)

    def test_user_constraints_extraction(self):
        steps = [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Please always use UTF-8 encoding when writing files."},
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Remember to run tests before committing changes."},
        ]
        analysis = self.analyzer.analyze(steps, conversation_id="test-constraints")
        self.assertTrue(any("always use utf-8" in c.lower() for c in analysis.user_constraints))
        self.assertTrue(any("remember to run tests" in c.lower() for c in analysis.user_constraints))


if __name__ == "__main__":
    unittest.main()
