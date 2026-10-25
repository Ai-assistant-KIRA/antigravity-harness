"""Tests for the zero-dependency settings loader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.core import settings as harness_settings

EXPECTED_DEFAULTS = {
    "rules": {"max_bytes": 20000},
    "briefing": {"max_chars": 7000, "max_skills": 8, "max_lessons": 5},
    "skills": {"max_per_session": 2, "min_quality": 0.55},
    "memory": {"prune_days": 365, "candidate_days": 30, "max_lessons": 2000},
    "hooks": {"stop_timeout": 90, "post_tool_use_timeout": 5},
}

TOML_AVAILABLE = harness_settings._tomllib is not None


class TestSettings(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="harness-settings-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)

    def write_json(self, value: object, name: str = "harness.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def write_toml(self, text: str) -> Path:
        path = self.root / "harness.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_returns_defaults_without_warnings(self) -> None:
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings, EXPECTED_DEFAULTS)
        self.assertEqual(warnings, [])

    def test_json_fallback_overrides_only_valid_keys(self) -> None:
        self.write_json(
            {
                "rules": {"max_bytes": 1234},
                "briefing": {"max_chars": 4000, "max_skills": 3},
                "skills": {"min_quality": 0.8},
                "memory": {"prune_days": 30},
                "hooks": {"stop_timeout": 45},
            }
        )
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual(settings["rules"]["max_bytes"], 1234)
        self.assertEqual(settings["briefing"]["max_chars"], 4000)
        self.assertEqual(settings["briefing"]["max_skills"], 3)
        self.assertEqual(settings["briefing"]["max_lessons"], 5)
        self.assertEqual(settings["skills"]["min_quality"], 0.8)
        self.assertEqual(settings["memory"]["prune_days"], 30)
        self.assertEqual(settings["hooks"]["stop_timeout"], 45)
        self.assertEqual(settings["hooks"]["post_tool_use_timeout"], 5)

    @unittest.skipUnless(TOML_AVAILABLE, "tomllib requires Python 3.11+")
    def test_toml_is_preferred_over_json(self) -> None:
        self.write_toml(
            "[rules]\n"
            "max_bytes = 4321\n"
            "\n"
            "[hooks]\n"
            "post_tool_use_timeout = 9\n"
        )
        self.write_json({"rules": {"max_bytes": 1}})
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual(settings["rules"]["max_bytes"], 4321)
        self.assertEqual(settings["hooks"]["post_tool_use_timeout"], 9)
        self.assertEqual(settings["hooks"]["stop_timeout"], 90)

    def test_json_is_used_when_toml_is_absent(self) -> None:
        self.write_json({"memory": {"candidate_days": 7}})
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual(settings["memory"]["candidate_days"], 7)

    def test_invalid_types_and_ranges_warn_and_keep_defaults(self) -> None:
        self.write_json(
            {
                "rules": {
                    "max_bytes": "large",
                    "mystery": 1,
                },
                "briefing": {
                    "max_chars": True,
                    "max_skills": 1.5,
                    "max_lessons": None,
                },
                "skills": {"max_per_session": -2, "min_quality": 2.0},
                "memory": {
                    "prune_days": -1,
                    "candidate_days": "30",
                    "max_lessons": False,
                },
                "hooks": {"stop_timeout": 0, "post_tool_use_timeout": "5"},
                "unknown_section": {"value": 1},
            }
        )
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings, EXPECTED_DEFAULTS)
        text = "\n".join(warnings)
        for key in (
            "rules.max_bytes",
            "rules.mystery",
            "briefing.max_chars",
            "briefing.max_skills",
            "briefing.max_lessons",
            "skills.max_per_session",
            "skills.min_quality",
            "memory.prune_days",
            "memory.candidate_days",
            "memory.max_lessons",
            "hooks.stop_timeout",
            "hooks.post_tool_use_timeout",
            "unknown_section",
        ):
            self.assertIn(key, text)

    def test_number_coercion_accepts_integers_for_float_settings(self) -> None:
        self.write_json({"skills": {"min_quality": 1}})
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(warnings, [])
        self.assertEqual(settings["skills"]["min_quality"], 1.0)
        self.assertIsInstance(settings["skills"]["min_quality"], float)

    def test_wrong_section_type_warns_and_keeps_section_defaults(self) -> None:
        self.write_json(
            {
                "rules": {"max_bytes": 99},
                "hooks": [],
            }
        )
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings["rules"]["max_bytes"], 99)
        self.assertEqual(settings["hooks"], EXPECTED_DEFAULTS["hooks"])
        self.assertTrue(any("hooks" in warning for warning in warnings))

    def test_malformed_json_never_raises(self) -> None:
        (self.root / "harness.json").write_text("{not json", encoding="utf-8")
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings, EXPECTED_DEFAULTS)
        self.assertTrue(any("harness.json" in warning for warning in warnings))

    @unittest.skipUnless(TOML_AVAILABLE, "tomllib requires Python 3.11+")
    def test_malformed_toml_never_raises(self) -> None:
        self.write_toml("[rules\nmax_bytes = 1\n")
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings, EXPECTED_DEFAULTS)
        self.assertTrue(any("harness.toml" in warning for warning in warnings))

    def test_tomllib_unavailable_falls_back_to_json(self) -> None:
        self.write_toml("[rules]\nmax_bytes = 777\n")
        self.write_json({"rules": {"max_bytes": 888}})
        with mock.patch.object(harness_settings, "_tomllib", None):
            settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings["rules"]["max_bytes"], 888)
        self.assertTrue(any("harness.toml" in warning for warning in warnings))

    @unittest.skipIf(TOML_AVAILABLE, "Python 3.10 fallback is not active")
    def test_python_310_uses_json_and_warns_about_toml(self) -> None:
        self.write_toml("[rules]\nmax_bytes = 777\n")
        self.write_json({"rules": {"max_bytes": 888}})
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings["rules"]["max_bytes"], 888)
        self.assertTrue(any("harness.toml" in warning for warning in warnings))

    @unittest.skipIf(TOML_AVAILABLE, "Python 3.10 fallback is not active")
    def test_python_310_toml_only_warns_and_uses_defaults(self) -> None:
        self.write_toml("[rules]\nmax_bytes = 777\n")
        settings, warnings = harness_settings.load_settings(self.root)
        self.assertEqual(settings, EXPECTED_DEFAULTS)
        self.assertTrue(any("Python 3.11" in warning for warning in warnings))

    def test_get_setting_supports_dotted_paths_and_defaults(self) -> None:
        settings, _ = harness_settings.load_settings(self.root)
        self.assertEqual(
            harness_settings.get_setting(settings, "rules.max_bytes"), 20000
        )
        self.assertEqual(
            harness_settings.get_setting(settings, "hooks.stop_timeout", 1), 90
        )
        self.assertEqual(
            harness_settings.get_setting(settings, "rules.missing", 123), 123
        )
        self.assertEqual(
            harness_settings.get_setting({"rules": "bad"}, "rules.max_bytes", 7), 7
        )

    def test_returned_settings_do_not_mutate_shared_defaults(self) -> None:
        first, _ = harness_settings.load_settings(self.root)
        first["rules"]["max_bytes"] = 1
        second, _ = harness_settings.load_settings(self.root)
        self.assertEqual(second["rules"]["max_bytes"], 20000)
        self.assertEqual(
            harness_settings.DEFAULT_SETTINGS["rules"]["max_bytes"], 20000
        )


if __name__ == "__main__":
    unittest.main()
