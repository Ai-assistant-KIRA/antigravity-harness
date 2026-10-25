"""Tests for transcript parsing and evidence extraction."""

from __future__ import annotations

import json
import unittest
from unittest import mock

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core.analyzer import TranscriptAnalyzer


def tool(name: str, **arguments: object) -> dict:
    return {
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": name, "arguments": arguments}],
    }


def output(content: str, status: str = "DONE", **extra: object) -> dict:
    return {
        "type": "CORTEX_STEP_TYPE_OUTPUT",
        "content": content,
        "status": status,
        **extra,
    }


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


class TestParsing(IsolatedHarnessTestCase):
    def test_parse_transcript_skips_invalid_lines(self) -> None:
        path = self.root / "transcript.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "USER_INPUT", "content": "hello"}),
                    "not json",
                    "",
                    json.dumps(["not", "a", "dict"]),
                    json.dumps({"type": "PLANNER_RESPONSE", "content": "done"}),
                ]
            ),
            encoding="utf-8",
        )
        analyzer = TranscriptAnalyzer(path)
        steps = analyzer.parse_transcript()
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["content"], "hello")

    def test_parse_transcript_caps_line_count_and_reports_truncation(self) -> None:
        analyzer = TranscriptAnalyzer()
        with mock.patch("harness.core.analyzer.MAX_TRANSCRIPT_LINES", 2):
            steps = analyzer.parse_transcript(
                [
                    '{"type":"USER_INPUT","content":"old"}',
                    '{"type":"USER_INPUT","content":"middle"}',
                    '{"type":"USER_INPUT","content":"new"}',
                ]
            )
        self.assertEqual([step["content"] for step in steps], ["middle", "new"])
        self.assertTrue(analyzer.truncated)

    def test_parse_transcript_reads_inline_lines(self) -> None:
        analyzer = TranscriptAnalyzer()
        steps = analyzer.parse_transcript(
            ['{"type":"USER_INPUT","content":"one"}', "bad", '{"type":"X"}']
        )
        self.assertEqual([step["type"] for step in steps], ["USER_INPUT", "X"])


class TestAnalysis(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_basic_tasks_and_tool_calls(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Please set up uv for this project."),
                tool("run_command", CommandLine="uv init"),
                output("Initialized project"),
            ],
            conversation_id="conv-basic",
        )
        self.assertEqual(analysis.tasks, ["Please set up uv for this project."])
        self.assertEqual(len(analysis.tool_calls), 1)
        self.assertEqual(analysis.tool_calls[0]["name"], "run_command")
        self.assertEqual(analysis.final_outcome, "completed")
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.workflows, [])

    def test_verified_failure_and_recovery(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Build the Python package"),
                tool("run_command", CommandLine="python setup.py"),
                output("command exited with code 1: error: no setup.py found", "ERROR"),
                tool("run_command", CommandLine="python -m build"),
                output("Successfully built dist/package.whl"),
            ],
            conversation_id="conv-recovery",
        )
        self.assertEqual(len(analysis.contrastive_events), 1)
        event = analysis.contrastive_events[0]
        self.assertEqual(event.trigger_tool, "run_command")
        self.assertTrue(event.verified)
        self.assertIn("setup.py", event.resolution_summary)
        self.assertIn("python -m build", event.resolution_summary)
        self.assertEqual(analysis.unresolved_events, [])
        self.assertEqual(analysis.final_outcome, "completed")

    def test_unresolved_failure_is_a_candidate_not_a_lesson(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Run the tests"),
                tool("run_command", CommandLine="pytest"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
            ],
            conversation_id="conv-unresolved",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)
        event = analysis.unresolved_events[0]
        self.assertFalse(event.verified)
        self.assertIn("no verified recovery", event.resolution_summary)
        self.assertEqual(analysis.final_outcome, "partial")

    def test_user_turn_breaks_recovery_pairing(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Run the tests"),
                tool("run_command", CommandLine="pytest"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
                user("Try something else now"),
                tool("run_command", CommandLine="pytest"),
                output("12 passed"),
            ],
            conversation_id="conv-turn-break",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_transient_failure_confidence_is_penalized(self) -> None:
        command = "curl http://localhost:8000/health"
        analysis = self.analyzer.analyze(
            [
                user("Check the local service"),
                tool("run_command", CommandLine=command),
                output("curl: (7) Failed to connect: connection refused", "ERROR"),
                tool("run_command", CommandLine=command),
                output("ok"),
            ],
            conversation_id="conv-transient",
        )
        self.assertEqual(len(analysis.contrastive_events), 1)
        event = analysis.contrastive_events[0]
        self.assertEqual(event.category, "network")
        self.assertLess(event.confidence, 0.8)
        self.assertIn("retrying succeeded", event.resolution_summary)
        self.assertTrue(analysis.environment_quirks)

    def test_different_tool_can_resolve_dependency_failure(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Install the missing dependency"),
                tool("run_command", CommandLine="pytest -q"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
                tool("write_file", Path="requirements.txt", Content="pytest\n"),
                output("updated requirements.txt"),
            ],
            conversation_id="conv-different-tool",
        )
        self.assertEqual(len(analysis.contrastive_events), 1)
        self.assertEqual(analysis.contrastive_events[0].category, "dependency")

    def test_bare_exit_code_is_not_a_diagnosis(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Run a command"),
                tool("run_command", CommandLine="something"),
                output("Command exited with code 1.", "ERROR"),
            ],
            conversation_id="conv-exit-code",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.unresolved_events, [])
        self.assertEqual(self.analyzer._extract_error("Command exited with code 1.", "ERROR"), (None, 0.0))

    def test_source_text_from_read_file_is_not_a_failure(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Inspect the module"),
                tool("read_file", Path="app.py"),
                output(
                    "1: def failing():\n"
                    "2:     raise RuntimeError('expected')\n"
                    "# error: this comment explains a retry\n"
                    "error_code = 500\n"
                ),
            ],
            conversation_id="conv-source-read",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.unresolved_events, [])

    def test_explicit_file_read_error_is_still_a_candidate(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Read the configuration"),
                tool("read_file", Path="missing.json"),
                output("Error: file not found: missing.json"),
            ],
            conversation_id="conv-file-error",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)
        self.assertIn("file not found", analysis.unresolved_events[0].error_message)

    def test_secret_in_error_is_redacted(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Connect to the API"),
                tool("run_command", CommandLine="curl api"),
                output("Error: API_KEY=super-secret-12345 while connecting", "ERROR"),
            ],
            conversation_id="conv-secret",
        )
        self.assertEqual(len(analysis.unresolved_events), 1)
        error = analysis.unresolved_events[0].error_message
        self.assertNotIn("super-secret-12345", error)
        self.assertIn("[REDACTED]", error)

    def test_traceback_keeps_the_actionable_exception(self) -> None:
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 2, in <module>\n'
            "ValueError: invalid configuration value"
        )
        error, confidence = self.analyzer._extract_error(traceback, "ERROR")
        self.assertIsNotNone(error)
        self.assertIn("ValueError", error)
        self.assertGreaterEqual(confidence, 0.8)

    def test_user_constraints(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Please always use UTF-8 encoding when writing files."),
                user("Remember to run tests before committing changes."),
                user("Never commit secrets."),
                user("Do not worry about license for this private use."),
                user("From now on, prefer pnpm."),
            ],
            conversation_id="conv-constraints",
        )
        lowered = " | ".join(analysis.user_constraints).lower()
        self.assertIn("always use utf-8", lowered)
        self.assertNotIn("remember to run tests", lowered)
        self.assertIn("never commit secrets", lowered)
        self.assertIn("prefer pnpm", lowered)
        self.assertNotIn("worry about license", lowered)

    def test_metadata_is_stripped_from_user_text(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user(
                    "<USER_REQUEST>Fix the build</USER_REQUEST>\n"
                    "<ADDITIONAL_METADATA>hidden</ADDITIONAL_METADATA>\n"
                    "The current local time is: 2026-10-23T10:00:00Z"
                )
            ],
            conversation_id="conv-clean",
        )
        self.assertEqual(analysis.tasks, ["Fix the build"])

    def test_workflow_candidate_is_generated_from_a_verified_sequence(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Set up the Python project and run the tests"),
                tool("run_command", CommandLine="python -m venv .venv"),
                output("created virtual environment"),
                tool("run_command", CommandLine="pip install -r requirements.txt"),
                output("Successfully installed dependencies"),
                tool("run_command", CommandLine="pytest -q"),
                output("12 passed"),
            ],
            conversation_id="conv-workflow",
            workspace=self.workspace,
        )
        self.assertEqual(len(analysis.workflows), 1)
        workflow = analysis.workflows[0]
        self.assertEqual(workflow.name, "run-python-tests")
        self.assertEqual(workflow.scope, "workspace")
        self.assertEqual(len(workflow.steps), 3)
        self.assertTrue(workflow.verification_steps)
        self.assertGreaterEqual(workflow.quality_score, 0.55)
        self.assertTrue(all("`" in step for step in workflow.steps))

    def test_exploratory_commands_do_not_become_a_workflow(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Look around the project"),
                tool("run_command", CommandLine="ls"),
                output("src tests"),
                tool("run_command", CommandLine="git status"),
                output("clean"),
                tool("run_command", CommandLine="cat pyproject.toml"),
                output("[project]"),
            ],
            conversation_id="conv-explore",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.workflows, [])

    def test_two_commands_without_verification_are_not_a_workflow(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Bootstrap the project"),
                tool("run_command", CommandLine="python -m venv .venv"),
                output("ok"),
                tool("run_command", CommandLine="pip install -e ."),
                output("ok"),
            ],
            conversation_id="conv-two",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.workflows, [])

    def test_skill_usage_detection(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Use the control skill"),
                tool("view_file", Path=".agents/skills/harness-control/SKILL.md"),
                output("# Harness Control"),
            ],
            conversation_id="conv-skill-usage",
        )
        self.assertEqual(analysis.skill_usage, ["harness-control"])

    def test_assistant_failure_language_marks_outcome_failed(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Do the task"),
                tool("run_command", CommandLine="echo started"),
                output("started"),
                {"type": "PLANNER_RESPONSE", "content": "I could not complete the task."},
            ],
            conversation_id="conv-failed",
        )
        self.assertEqual(analysis.final_outcome, "failed")


if __name__ == "__main__":
    unittest.main()
