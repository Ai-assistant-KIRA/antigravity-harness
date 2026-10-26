"""Tests for per-task skill extraction and `harness skills why` diagnostics."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness import cli as cli_module
from harness.core.analyzer import MAX_GOAL_WORDS, TranscriptAnalyzer
from harness.core.memory_store import MemoryStore
from harness.core.skill_diagnostics import diagnose_skill_generation


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


def tool(name: str, **arguments: object) -> dict:
    return {
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": name, "arguments": arguments}],
    }


def output(content: str, status: str = "DONE") -> dict:
    return {"type": "CORTEX_STEP_TYPE_OUTPUT", "content": content, "status": status}


class TestPerTaskWorkflows(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_question_prompt_does_not_veto_later_actionable_task(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("what is the best proxy provider with a free trial?"),
                tool("run_command", CommandLine="curl.exe -s https://api.ipify.org"),
                output('{"ip":"1.2.3.4"}'),
                user("fix the audio hook so the voiceover finishes"),
                tool("run_command", CommandLine="ffmpeg -i hook.wav -af areverse out.wav"),
                output("ok"),
                tool("run_command", CommandLine="ffmpeg -i out.wav -af silenceremove out2.wav"),
                output("ok"),
                tool("run_command", CommandLine="python scratch/check_duration.py"),
                output("14.2s"),
            ],
            conversation_id="conv-audio",
            workspace=self.workspace,
        )
        self.assertEqual(len(analysis.workflows), 1)
        workflow = analysis.workflows[0]
        self.assertIn("voiceover", workflow.goal)
        self.assertTrue(workflow.name.startswith("edit-audio"))
        self.assertTrue(workflow.success)

    def test_partial_session_success_is_decided_per_task(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("run the landing page dev server"),
                tool("run_command", CommandLine="npm run build"),
                output("built"),
                tool("run_command", CommandLine="npx serve -s dist -p 3000"),
                output("serving"),
                user("then deploy it"),
                tool("run_command", CommandLine="wrangler pages deploy dist"),
                output("Error: not authenticated", status="ERROR"),
            ],
            conversation_id="conv-partial",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.final_outcome, "partial")
        goals = [workflow.goal for workflow in analysis.workflows]
        self.assertIn("run the landing page dev server", goals)
        self.assertTrue(all(workflow.success for workflow in analysis.workflows))
        self.assertFalse(any("then deploy it" in goal for goal in goals))

    def test_long_goal_passes_but_question_and_research_do_not(self) -> None:
        long_task = " ".join(["edit"] + ["premium"] * (MAX_GOAL_WORDS - 1))
        self.assertTrue(TranscriptAnalyzer._is_actionable_goal(long_task))
        self.assertFalse(TranscriptAnalyzer._is_actionable_goal("how do I fix this?"))
        self.assertFalse(
            TranscriptAnalyzer._is_actionable_goal("research the best video codec")
        )

    def test_fallback_name_is_derived_for_non_code_work(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("the voiceover cuts off before the end"),
                tool("run_command", CommandLine="ffmpeg -i vo.mp3 -af volume=1.2 vo2.mp3"),
                output("ok"),
                tool("run_command", CommandLine="ffprobe -v error -show_format vo2.mp3"),
                output("ok"),
                tool("run_command", CommandLine="python scratch/check_loudness.py"),
                output("ok"),
            ],
            conversation_id="conv-name",
            workspace=self.workspace,
        )
        self.assertEqual(len(analysis.workflows), 1)
        name = analysis.workflows[0].name
        self.assertTrue(name.startswith("edit-audio-"), name)
        self.assertGreater(len(name), 12)

    def test_duplicate_names_in_one_session_are_disambiguated(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("run the python tests"),
                tool("run_command", CommandLine="pytest -q"),
                output("3 passed"),
                tool("run_command", CommandLine="python -m compileall src"),
                output("ok"),
                user("now run the python tests again"),
                tool("run_command", CommandLine="pytest -q tests/unit"),
                output("3 passed"),
                tool("run_command", CommandLine="python -m compileall tests"),
                output("ok"),
            ],
            conversation_id="conv-dupe",
            workspace=self.workspace,
        )
        names = [workflow.name for workflow in analysis.workflows]
        self.assertGreaterEqual(len(names), 2)
        self.assertEqual(len(names), len(set(names)))


class TestSkillWhyDiagnostics(IsolatedHarnessTestCase):
    def _record(self, conversation_id: str, steps: list[dict]) -> None:
        transcript = (
            self.app_data
            / "brain"
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            "\n".join(json.dumps(step) for step in steps) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        analyzer = TranscriptAnalyzer(transcript_path=transcript)
        analysis = analyzer.analyze(
            analyzer.parse_transcript(),
            conversation_id=conversation_id,
            workspace=self.workspace,
        )
        MemoryStore(workspace=self.workspace).record_session(analysis)

    def test_eligible_workflow_is_reported_with_a_reason(self) -> None:
        self._record(
            "conv-why-eligible",
            [
                user("fix the audio hook so the voiceover finishes"),
                tool("run_command", CommandLine="ffmpeg -i hook.wav -af areverse out.wav"),
                output("ok"),
                tool("run_command", CommandLine="ffmpeg -i out.wav -af silenceremove out2.wav"),
                output("ok"),
                tool("run_command", CommandLine="ffmpeg -i hook.mp3 -c:a copy hook-copy.mp3"),
                output("ok"),
                tool("run_command", CommandLine="python scratch/render_hook.py"),
                output("ok"),
                tool("run_command", CommandLine="python scratch/check_duration.py"),
                output("14.2s"),
            ],
        )
        report = diagnose_skill_generation(workspace=self.workspace)
        self.assertEqual(len(report["sessions"]), 1)
        entry = report["sessions"][0]
        self.assertIn("edit-audio", entry["workflows"][0]["name"])
        self.assertTrue(entry["workflows"][0]["verdict"].startswith("eligible"))
        self.assertEqual(report["eligible"], 1)

    def test_question_only_session_reports_the_failing_gate(self) -> None:
        self._record(
            "conv-why-question",
            [
                user("what is the best proxy provider with a free trial?"),
                tool("run_command", CommandLine="curl.exe -s https://api.ipify.org"),
                output('{"ip":"1.2.3.4"}'),
                tool("run_command", CommandLine="curl.exe -s https://ipinfo.io"),
                output('{"city":"Sofia"}'),
            ],
        )
        report = diagnose_skill_generation(workspace=self.workspace)
        workflows = report["sessions"][0]["workflows"]
        self.assertTrue(workflows)
        self.assertTrue(
            all(workflow["verdict"].startswith("blocked") for workflow in workflows)
        )
        self.assertEqual(report["eligible"], 0)

    def test_cli_why_returns_json(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli_module.main(["skills", "why", "--json"])
        payload = json.loads(buffer.getvalue())
        self.assertIn("sessions", payload)
        self.assertIn("min_quality", payload)


if __name__ == "__main__":
    unittest.main()
