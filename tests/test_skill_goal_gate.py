"""Semantic goal gates for synthesized skills.

Regression coverage for junk skills whose "goal" is the user's prompt and whose
"procedure" is Harness inspecting its own plugin state.
"""

from __future__ import annotations

import unittest

try:
    from .support import IsolatedHarnessTestCase, make_event, make_workflow
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event, make_workflow

from harness.core.config import get_plugin_dir
from harness.core.skill_builder import (
    SKILL_END,
    SKILL_START,
    SkillBuilder,
    SkillQualityError,
    _is_self_referential,
)


JUNK_GOAL = (
    "deepseek v4.1 flash is a good model but it takes to long for tasks for no "
    "reason research github repos that fix this issue?"
)


class TestSkillGoalGate(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.builder = SkillBuilder(self.workspace)

    def test_prompt_shaped_goal_is_rejected_despite_high_score(self) -> None:
        workflow = make_workflow(
            name="deepseek-flash-good-model-but",
            goal=JUNK_GOAL,
            steps=[
                "Run `python -m harness doctor`",
                "Read `memory/EXPERIENCE.md`",
                "Read `state/events.jsonl`",
                "Summarize the recent hook errors",
                "Compare the rules with the observed failures",
                "Inspect the analyzer configuration",
                "Report the findings",
            ],
            quality=0.91,
            success=True,
        )
        self.assertEqual(self.builder._quality(workflow), 0.30)
        with self.assertRaisesRegex(SkillQualityError, "not an actionable procedure"):
            self.builder.build_skill(workflow)

    def test_actionable_goal_builds(self) -> None:
        workflow = make_workflow(
            name="add-upload-retries",
            goal="Add retries to the upload command and verify with pytest",
            steps=[
                "Edit `uploader.py` to retry failed uploads",
                "Run `pytest -q tests/test_uploader.py`",
                "Run `ruff check uploader.py`",
            ],
        )
        result = self.builder.build_skill(workflow)
        self.assertEqual(result.status, "created")
        self.assertEqual(result.name, "add-upload-retries")
        self.assertTrue(result.path.exists())

    def test_steps_referencing_the_plugin_are_rejected(self) -> None:
        hook = get_plugin_dir() / "scripts" / "hook_runner.py"
        workflow = make_workflow(
            name="inspect-harness-state",
            goal="Summarize the latest hook events for this workspace",
            steps=[
                f"Run `python {hook}`",
                "Read the generated event report",
                "Run `pytest -q`",
            ],
        )
        with self.assertRaisesRegex(SkillQualityError, "self-inspection"):
            self.builder.build_skill(workflow)

    def test_verification_steps_referencing_the_plugin_are_rejected(self) -> None:
        hook = get_plugin_dir() / "scripts" / "hook_runner.py"
        workflow = make_workflow(
            name="summarize-hook-events",
            goal="Summarize the latest hook events for this workspace",
            verification_steps=[f"Run `python {hook}` and require exit code 0"],
        )
        with self.assertRaisesRegex(SkillQualityError, "self-inspection"):
            self.builder.build_skill(workflow)

    def test_pitfalls_referencing_the_plugin_are_rejected(self) -> None:
        event = make_event(
            resolution_summary=(
                f"Reinstalled the hook from {get_plugin_dir() / 'scripts'} and verified it."
            )
        )
        workflow = make_workflow(
            name="summarize-hook-events",
            goal="Summarize the latest hook events for this workspace",
            steps=[
                "Read the generated event report",
                "Group events by failure category",
                "Run `pytest -q`",
            ],
        )
        with self.assertRaisesRegex(SkillQualityError, "pitfalls reference"):
            self.builder.build_skill(workflow, related_events=[event])

    def test_existing_managed_pitfalls_with_plugin_paths_are_dropped(self) -> None:
        name = "summarize-hook-events"
        skill_file = self.workspace / ".agents" / "skills" / name / "SKILL.md"
        polluted = get_plugin_dir() / "memory" / "EXPERIENCE.md"
        self.write_text(
            skill_file,
            "---\n"
            f"name: {name}\n"
            "description: >-\n"
            "  Repeatable procedure for: Summarize the latest hook events for this workspace.\n"
            "---\n\n"
            "# Summarize hook events\n\n"
            f"{SKILL_START}\n"
            '<!-- harness-meta: {"version": 1, "quality": 0.8, "source": "harness-auto"} -->\n'
            "> Learned and maintained by Harness².\n\n"
            "## Procedure\n"
            "1. Read the generated event report\n"
            "2. Group events by failure category\n"
            "3. Run `pytest -q`\n\n"
            "## Verification\n"
            "- Run `pytest -q` and require exit code 0\n\n"
            "## Known Pitfalls\n"
            f"- **run_command**: stale failure -> Reinstalled from {polluted}\n"
            "- **run_command**: timeout -> Increase the timeout and retry\n"
            f"{SKILL_END}\n\n"
            "## My Notes\n\n"
            "Keep this user note.\n",
        )
        workflow = make_workflow(
            name=name,
            goal="Summarize the latest hook events for this workspace",
            steps=[
                "Read the generated event report",
                "Group events by failure category",
                "Run `pytest -q`",
            ],
        )
        result = self.builder.build_skill(workflow, related_events=[make_event()])
        self.assertEqual(result.status, "updated")
        content = result.path.read_text(encoding="utf-8")
        self.assertNotIn(str(polluted), content)
        self.assertIn("Increase the timeout and retry", content)
        self.assertIn("Keep this user note.", content)

    def test_doubled_separators_in_plugin_path_are_detected(self) -> None:
        doubled = str(get_plugin_dir()).replace("/", "//").replace("\\", "//")
        self.assertTrue(_is_self_referential(doubled))

    def test_skill_name_rejects_questions_but_keeps_valid_slugs(self) -> None:
        with self.assertRaises(SkillQualityError):
            self.builder._skill_name("is deepseek flash a good model?")
        for value in ("research-notes", "compare-datasets", "abc"):
            self.assertEqual(self.builder._skill_name(value), value)
        self.assertEqual(self.builder._skill_name("run-python-tests"), "run-python-tests")


if __name__ == "__main__":
    unittest.main()
