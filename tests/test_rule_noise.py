"""Tests for the non-causal recovery filter on learned rules."""

from __future__ import annotations

import unittest

try:
    from .support import IsolatedHarnessTestCase, make_event
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event

from harness.core.agents_md_manager import (
    HARNESS_FOOTER,
    HARNESS_HEADER,
    AgentsMdManager,
    _is_non_causal_recovery,
)

JUNK_AGY = (
    "Environment: `agy --help` failed with `agy : The term 'agy' is not recognized "
    "as the name of a cmdlet, function, script file, or operable program. Check the`. "
    "Verified fix: use `curl.exe -s -X POST "
    "https://relume-library-mcp.relume.io/register ...`."
)
JUNK_AGY_SUMMARY = JUNK_AGY[len("Environment: ") :]
JUNK_WHISPER = (
    "`run_command` failed with `Error: No module named 'whisper'`; "
    "`search_web` then succeeded."
)
CURL_VERIFIED_FIX = (
    "`write_file` failed with `permission denied`. Verified fix: use "
    "`curl.exe -s -X POST https://example.test/register`."
)
LEGIT_INSTALL = (
    '`python -c "import whisper"` failed with `ModuleNotFoundError`; '
    "Installing `pip install openai-whisper` resolved the missing dependency."
)
SAME_TOOL_RETRY = (
    "`run_command` failed with a transient timeout; `run_command` then succeeded."
)
NORMAL_QUIRK = "PowerShell: quote paths containing spaces."


class TestNonCausalRecoveryFilter(IsolatedHarnessTestCase):
    def test_rejects_failure_paired_with_unrelated_success(self) -> None:
        for value in (JUNK_AGY, JUNK_WHISPER, CURL_VERIFIED_FIX):
            with self.subTest(value=value):
                self.assertTrue(_is_non_causal_recovery(value))

    def test_keeps_causal_and_generic_recoveries(self) -> None:
        kept = (
            LEGIT_INSTALL,
            SAME_TOOL_RETRY,
            NORMAL_QUIRK,
            "Verified fix: use `pip install openai-whisper`.",
        )
        for value in kept:
            with self.subTest(value=value):
                self.assertFalse(_is_non_causal_recovery(value))


class TestRuleBuilding(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = AgentsMdManager(self.workspace)

    def test_junk_quirks_are_not_persisted(self) -> None:
        rules = self.manager._build_rules([], [], [JUNK_AGY, JUNK_WHISPER], False)
        self.assertEqual(rules.quirks, [])
        self.assertEqual(rules.pitfalls, [])
        self.assertEqual(rules.directives, [])

    def test_agy_event_is_not_persisted(self) -> None:
        event = make_event(
            trigger_tool="run_command",
            error_message="agy is not recognized",
            resolution_summary=JUNK_AGY_SUMMARY,
            category="environment",
            verified=True,
        )
        rules = self.manager._build_rules([event], [], [], False)
        self.assertEqual(rules.quirks, [])
        self.assertEqual(rules.pitfalls, [])

    def test_whisper_event_is_not_persisted(self) -> None:
        event = make_event(
            trigger_tool="run_command",
            error_message="Error: No module named 'whisper'",
            resolution_summary=JUNK_WHISPER,
            category="dependency",
            verified=True,
        )
        rules = self.manager._build_rules([event], [], [], False)
        self.assertEqual(rules.quirks, [])
        self.assertEqual(rules.pitfalls, [])

    def test_legitimate_install_event_persists_one_quirk(self) -> None:
        event = make_event(
            trigger_tool="run_command",
            error_message="ModuleNotFoundError: No module named 'whisper'",
            resolution_summary=LEGIT_INSTALL,
            category="dependency",
            verified=True,
        )
        rules = self.manager._build_rules([event], [], [], False)
        self.assertEqual(len(rules.quirks), 1)
        self.assertIn("pip install openai-whisper", rules.quirks[0])
        self.assertEqual(rules.pitfalls, [])

    def test_normal_environment_quirk_still_persists(self) -> None:
        rules = self.manager._build_rules([], [], ["Windows PowerShell 5.1"], False)
        self.assertTrue(any("Windows PowerShell 5.1" in rule for rule in rules.quirks))


class TestRepairRemovesNonCausalRules(IsolatedHarnessTestCase):
    def test_repair_removes_junk_and_keeps_good_rules(self) -> None:
        user_text = (
            "# Workspace Notes\n\n"
            "Keep this handwritten paragraph exactly as written."
        )
        block = (
            f"{HARNESS_HEADER}\n"
            "## Harness Evolved Directives (Workspace)\n"
            "### Standing Project Directives\n"
            "- Never require us to use different models\n"
            "### Environment & Tooling Quirks\n"
            f"- {JUNK_AGY}\n"
            f"- {JUNK_WHISPER}\n"
            f"- {NORMAL_QUIRK}\n"
            f"{HARNESS_FOOTER}\n"
        )
        original = f"{user_text}\n\n{block}"
        self.write_text(self.workspace / "AGENTS.md", original)

        manager = AgentsMdManager(self.workspace)
        result = manager.repair(target="workspace")
        content = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")

        self.assertTrue(result["workspace"]["updated"])
        self.assertEqual(result["workspace"]["removed"], 2)
        self.assertGreaterEqual(result["workspace"]["kept"], 2)
        self.assertNotIn("agy --help", content)
        self.assertNotIn("search_web", content)
        self.assertIn("Never require us to use different models", content)
        self.assertIn(NORMAL_QUIRK, content)
        self.assertEqual(
            content.split(HARNESS_HEADER, 1)[0],
            original.split(HARNESS_HEADER, 1)[0],
        )


if __name__ == "__main__":
    unittest.main()
