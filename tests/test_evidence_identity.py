"""Tests for conservative evidence identity and goal gating."""

from __future__ import annotations

import unittest

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


class TestRecoveryIdentity(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_failure_on_one_file_is_not_recovered_by_another_file(self) -> None:
        experience = self.workspace / "memory" / "EXPERIENCE.md"
        skill = self.workspace / "skills" / "harness-control" / "SKILL.md"
        analysis = self.analyzer.analyze(
            [
                user("Inspect the memory notes"),
                tool("read_file", AbsolutePath=str(experience)),
                output(f"file does not exist: {experience}", "ERROR"),
                tool("read_file", AbsolutePath=str(skill)),
                output("# Harness Control"),
            ],
            conversation_id="conv-different-paths",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.verified_events, [])
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_failure_on_the_same_file_is_a_verified_recovery(self) -> None:
        experience = self.workspace / "memory" / "EXPERIENCE.md"
        analysis = self.analyzer.analyze(
            [
                user("Read the memory notes"),
                tool("read_file", AbsolutePath=str(experience)),
                output(f"FileNotFoundError: No such file or directory: {experience}", "ERROR"),
                tool("read_file", AbsolutePath=str(experience)),
                output("# Experience"),
            ],
            conversation_id="conv-same-path",
            workspace=self.workspace,
        )
        self.assertEqual(len(analysis.verified_events), 1)
        self.assertEqual(analysis.unresolved_events, [])

    def test_same_path_with_numeric_components_is_a_verified_recovery(self) -> None:
        target = "C:/proj/2024/report.md"
        analysis = self.analyzer.analyze(
            [
                user("Read the yearly report"),
                tool("read_file", AbsolutePath=target),
                output(f"FileNotFoundError: No such file or directory: {target}", "ERROR"),
                tool("read_file", AbsolutePath=target),
                output("# Report"),
            ],
            conversation_id="conv-same-numeric-path",
            workspace=self.workspace,
        )
        self.assertEqual(len(analysis.verified_events), 1)
        self.assertEqual(analysis.unresolved_events, [])

    def test_numeric_path_components_are_not_interchangeable(self) -> None:
        first = "C:/proj/2024/report.md"
        second = "C:/proj/2025/report.md"
        analysis = self.analyzer.analyze(
            [
                user("Read the yearly report"),
                tool("read_file", AbsolutePath=first),
                output(f"file does not exist: {first}", "ERROR"),
                tool("read_file", AbsolutePath=second),
                output("# Report"),
            ],
            conversation_id="conv-numeric-paths",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_repeated_separators_and_spaces_are_not_interchangeable(self) -> None:
        cases = [
            ("C:/proj/a/b/report.md", "C:/proj/a b/report.md"),
            ("C:/proj/a  b/report.md", "C:/proj/a b/report.md"),
        ]
        for index, (first, second) in enumerate(cases):
            with self.subTest(case=index):
                analysis = self.analyzer.analyze(
                    [
                        user("Read the project report"),
                        tool("read_file", AbsolutePath=first),
                        output(f"file does not exist: {first}", "ERROR"),
                        tool("read_file", AbsolutePath=second),
                        output("# Report"),
                    ],
                    conversation_id=f"conv-path-separators-{index}",
                    workspace=self.workspace,
                )
                self.assertEqual(analysis.contrastive_events, [])
                self.assertEqual(len(analysis.unresolved_events), 1)

    def test_unc_path_is_not_the_same_as_a_rooted_path(self) -> None:
        unc = "//server/share/report.md"
        rooted = "/server/share/report.md"
        analysis = self.analyzer.analyze(
            [
                user("Read the shared report"),
                tool("read_file", AbsolutePath=unc),
                output(f"file does not exist: {unc}", "ERROR"),
                tool("read_file", AbsolutePath=rooted),
                output("# Report"),
            ],
            conversation_id="conv-unc-path",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_internal_duplicate_separators_are_equivalent(self) -> None:
        first = "a//b/report.md"
        second = "a/b/report.md"
        analysis = self.analyzer.analyze(
            [
                user("Read the project report"),
                tool("read_file", AbsolutePath=first),
                output(f"FileNotFoundError: No such file or directory: {first}", "ERROR"),
                tool("read_file", AbsolutePath=second),
                output("# Report"),
            ],
            conversation_id="conv-duplicate-separators",
            workspace=self.workspace,
        )
        self.assertEqual(len(analysis.verified_events), 1)
        self.assertEqual(analysis.unresolved_events, [])

    def test_generic_tool_dict_arguments_must_match_exactly(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Look up the record"),
                tool("custom_lookup", timeout=30, query="alpha"),
                output("lookup failed for alpha", "ERROR"),
                tool("custom_lookup", timeout=60, query="beta"),
                output("record beta"),
            ],
            conversation_id="conv-generic-args",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_self_referential_failure_is_not_evidence(self) -> None:
        target = self.plugin_dir / "memory" / "EXPERIENCE.md"
        paired = self.analyzer.analyze(
            [
                user("Inspect the plugin memory"),
                tool("read_file", AbsolutePath=str(target)),
                output(f"FileNotFoundError: No such file or directory: {target}", "ERROR"),
                tool("read_file", AbsolutePath=str(target)),
                output("# Experience"),
            ],
            conversation_id="conv-self-paired",
            workspace=self.workspace,
        )
        self.assertEqual(paired.contrastive_events, [])
        self.assertEqual(paired.unresolved_events, [])

        unresolved = self.analyzer.analyze(
            [
                user("Inspect the plugin memory"),
                tool("read_file", AbsolutePath=str(target)),
                output(f"FileNotFoundError: No such file or directory: {target}", "ERROR"),
            ],
            conversation_id="conv-self-unresolved",
            workspace=self.workspace,
        )
        self.assertEqual(unresolved.contrastive_events, [])
        self.assertEqual(unresolved.unresolved_events, [])


class TestGoalAndDirectiveFiltering(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_question_shaped_task_does_not_become_a_workflow(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user(
                    "Deepseek v4.1 flash is a good model but it takes too long. "
                    "How can I make it faster?"
                ),
                tool("run_command", CommandLine="python -m venv .venv"),
                output("created virtual environment"),
                tool("run_command", CommandLine="pip install -r requirements.txt"),
                output("Successfully installed dependencies"),
                tool("run_command", CommandLine="pytest -q"),
                output("12 passed"),
            ],
            conversation_id="conv-question-goal",
            workspace=self.workspace,
        )
        self.assertEqual(analysis.workflows, [])

    def test_remember_and_make_sure_are_not_standing_directives(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Remember to run tests before committing changes."),
                user(
                    "Make sure to epidemic sound mcp is installed and generate a better "
                    "more human natural sounding voiceover."
                ),
                user(f"Always keep notes in {self.plugin_dir}."),
                user("Never commit secrets."),
            ],
            conversation_id="conv-directives",
        )
        lowered = " | ".join(analysis.user_constraints).lower()
        self.assertNotIn("remember to run tests", lowered)
        self.assertNotIn("make sure", lowered)
        self.assertNotIn("keep notes", lowered)
        self.assertIn("never commit secrets", lowered)


if __name__ == "__main__":
    unittest.main()
