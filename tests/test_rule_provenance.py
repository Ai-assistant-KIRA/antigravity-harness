"""Tests for rule provenance, conflicts, supersede, and value-based eviction."""

from __future__ import annotations

import sqlite3
import unittest

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core.agents_md_manager import (
    HARNESS_FOOTER,
    HARNESS_HEADER,
    SECTION_DIRECTIVES,
    AgentsMdManager,
)
from harness.core.memory_store import MemoryStore, SCHEMA_VERSION, rule_hash


PNPM_RULE = "Always use pnpm instead of npm for dependency installs."
NPM_RULE = "Always use npm for installing dependencies."
AVOID_PNPM_RULE = "Avoid pnpm for installing dependencies."


class TestRuleSchema(IsolatedHarnessTestCase):
    def test_v2_database_is_upgraded_with_rules_index(self) -> None:
        db = self.root / "memory" / "upgrade.db"
        experience = self.root / "memory" / "UPGRADE.md"
        MemoryStore(db, experience, workspace=self.workspace)

        connection = sqlite3.connect(str(db))
        try:
            connection.execute("DROP TABLE rules_index")
            connection.execute(
                "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        store = MemoryStore(db, experience, workspace=self.workspace)
        connection = sqlite3.connect(str(db))
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            version = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(rules_index)"
                ).fetchall()
            }
        finally:
            connection.close()

        self.assertIn("rules_index", names)
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertIn("surfaced_count", columns)
        self.assertIn("helpful_count", columns)
        self.assertEqual(
            store.record_rule_application(
                [{"text": PNPM_RULE, "section": "directive", "new": True}],
                str(self.workspace / "AGENTS.md"),
                "workspace",
            ),
            1,
        )

    def test_rule_hash_is_stable_and_bounded(self) -> None:
        first = rule_hash("Always use pnpm.")
        second = rule_hash("  always   USE pnpm.  ")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 24)
        int(first, 16)
        self.assertNotEqual(first, rule_hash("Always use npm."))


class TestRuleProvenance(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.root / "memory" / "rules.db"
        self.experience = self.root / "memory" / "EXPERIENCE.md"
        self.store = MemoryStore(self.db, self.experience, workspace=self.workspace)
        self.target = str(self.workspace / "AGENTS.md")

    def test_record_query_surface_and_outcome(self) -> None:
        applied = self.store.record_rule_application(
            [
                {"text": PNPM_RULE, "section": "directive", "new": True},
                {"text": AVOID_PNPM_RULE, "section": "pitfall", "new": True},
            ],
            self.target,
            "workspace",
            conversation_id="conv-provenance",
            evidence="verified npm failure and pnpm recovery",
        )
        self.assertEqual(applied, 2)

        rule = self.store.get_rule(PNPM_RULE)
        self.assertIsNotNone(rule)
        self.assertEqual(rule["occurrences"], 1)
        self.assertEqual(rule["section"], "directive")
        self.assertEqual(rule["scope"], "workspace")
        self.assertEqual(rule["project"], self.store.project)
        self.assertEqual(rule["conversation_id"], "conv-provenance")
        self.assertIn("verified", rule["evidence"])
        self.assertTrue(rule["created_at"])
        self.assertTrue(rule["last_seen_at"])

        self.store.record_rule_application(
            [{"text": PNPM_RULE, "section": "directive", "new": False}],
            self.target,
            "workspace",
            conversation_id="conv-second",
            evidence="reapplied",
        )
        rule = self.store.get_rule(PNPM_RULE)
        self.assertEqual(rule["occurrences"], 2)
        self.assertEqual(rule["conversation_id"], "conv-second")
        self.assertEqual(rule["evidence"], "reapplied")

        digest = rule_hash(PNPM_RULE)
        self.store.record_rule_surfaced([digest, digest, "unknown-hash"])
        rule = self.store.get_rule(PNPM_RULE)
        self.assertEqual(rule["surfaced_count"], 1)
        self.assertTrue(rule["last_surfaced_at"])

        self.store.record_rule_outcome(PNPM_RULE, helpful=True)
        self.store.record_rule_outcome(PNPM_RULE, helpful=False)
        rule = self.store.get_rule(PNPM_RULE)
        self.assertEqual(rule["helpful_count"], 1)
        self.assertEqual(rule["harmful_count"], 1)

        self.assertGreater(self.store.get_rule_priority(PNPM_RULE), 0.0)
        self.assertEqual(self.store.get_rule_priority("Never seen before."), 0.0)

        listed = self.store.list_rules(target_file=self.target, scope="workspace")
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(item["target_file"] == self.target for item in listed))
        self.assertTrue(
            all(item["project"] == self.store.project for item in listed)
        )

        self.assertTrue(self.store.forget_rule(PNPM_RULE))
        self.assertIsNone(self.store.get_rule(PNPM_RULE))
        self.assertFalse(self.store.forget_rule(PNPM_RULE))

    def test_plugin_scope_uses_global_project(self) -> None:
        self.store.record_rule_application(
            [{"text": PNPM_RULE, "section": "quirk", "new": True}],
            str(self.plugin_dir / "rules" / "AGENTS.md"),
            "plugin",
        )
        rule = self.store.get_rule(PNPM_RULE)
        self.assertEqual(rule["scope"], "plugin")
        self.assertEqual(rule["project"], "global")


class TestAgentsMdProvenance(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = AgentsMdManager(self.workspace)
        self.rules_path = self.workspace / "AGENTS.md"

    def test_callback_receives_fitted_rules_with_new_flags(self) -> None:
        first_payloads: list[list[dict]] = []
        constraints = [
            PNPM_RULE,
            "Always keep the deployment checklist current before releases.",
        ]
        self.manager.update_rules(
            [],
            constraints,
            ["PowerShell requires quoting paths that contain spaces."],
            target="workspace",
            on_apply=first_payloads.append,
        )
        self.assertEqual(len(first_payloads), 1)
        payload = first_payloads[0]
        self.assertEqual(len(payload), len(self.manager.managed_rules("workspace")))
        self.assertEqual(
            {item["section"] for item in payload},
            {"directive", "quirk"},
        )
        self.assertTrue(all(item["new"] for item in payload))
        self.assertTrue(all(item["scope"] == "workspace" for item in payload))
        self.assertTrue(
            all(item["target_file"] == str(self.rules_path) for item in payload)
        )
        for item in payload:
            self.assertEqual(
                set(item), {"text", "section", "target_file", "scope", "new"}
            )

        second_payloads: list[list[dict]] = []
        self.manager.update_rules(
            [],
            constraints,
            ["PowerShell requires quoting paths that contain spaces."],
            target="workspace",
            on_apply=second_payloads.append,
        )
        self.assertEqual(len(second_payloads), 1)
        self.assertTrue(second_payloads[0])
        self.assertFalse(any(item["new"] for item in second_payloads[0]))

    def test_managed_rules_reads_sections_without_a_database(self) -> None:
        self.manager.update_rules(
            [],
            [PNPM_RULE],
            ["PowerShell requires quoting paths that contain spaces."],
            target="workspace",
        )
        rules = self.manager.managed_rules("workspace")
        by_section = {item["section"]: item for item in rules}
        self.assertIn("directive", by_section)
        self.assertIn("quirk", by_section)
        self.assertEqual(by_section["directive"]["text"], PNPM_RULE)
        self.assertEqual(by_section["directive"]["scope"], "workspace")
        self.assertEqual(
            by_section["directive"]["target_file"], str(self.rules_path)
        )

    def test_remove_rule_preserves_user_content_and_encoding(self) -> None:
        self.rules_path.write_bytes(
            "# User Rules\r\n\r\n- Keep this project note.\r\n".encode("utf-16")
        )
        self.manager.update_rules(
            [],
            [PNPM_RULE, "Always keep the deployment checklist current before releases."],
            [],
            target="workspace",
        )

        dry = self.manager.remove_rule(PNPM_RULE, target="workspace", dry_run=True)
        self.assertTrue(dry["updated"])
        self.assertEqual(dry["removed"], 1)
        self.assertIn(PNPM_RULE, self.manager.show("workspace"))

        removed = self.manager.remove_rule(
            PNPM_RULE.rstrip(".") + " now",
            target="workspace",
        )
        self.assertTrue(removed["updated"])
        self.assertEqual(removed["removed"], 1)
        self.assertNotIn(PNPM_RULE, self.manager.show("workspace"))
        self.assertIn("deployment checklist", self.manager.show("workspace"))
        raw = self.rules_path.read_bytes()
        self.assertTrue(raw.startswith((b"\xff\xfe", b"\xfe\xff")))
        text = raw.decode("utf-16")
        self.assertIn("Keep this project note.", text)
        self.assertIn("\r\n", text)

    def test_find_conflicts_detects_tool_groups_and_polarity(self) -> None:
        self.write_text(
            self.rules_path,
            f"{HARNESS_HEADER}\n"
            "## Harness Evolved Directives (Workspace)\n"
            f"### {SECTION_DIRECTIVES}\n"
            f"- {PNPM_RULE}\n"
            f"- {NPM_RULE}\n"
            f"- {AVOID_PNPM_RULE}\n"
            f"{HARNESS_FOOTER}\n",
        )
        conflicts = self.manager.find_conflicts("workspace")
        reasons = [conflict["reason"] for conflict in conflicts]
        self.assertTrue(any("mutually exclusive tools" in reason for reason in reasons))
        self.assertTrue(any("opposing polarity" in reason for reason in reasons))
        for conflict in conflicts:
            self.assertEqual(
                set(conflict), {"first", "second", "section", "reason"}
            )
            self.assertIsInstance(conflict["first"], str)
            self.assertIsInstance(conflict["second"], str)
            self.assertEqual(conflict["section"], "directive")

    def test_new_rule_supersedes_a_conflicting_managed_rule(self) -> None:
        self.write_text(
            self.rules_path,
            "# User Rules\n\n- Keep this user-authored note.\n",
        )
        first = self.manager.update_rules([], [NPM_RULE], [], target="workspace")
        self.assertTrue(first["workspace"]["updated"])

        second = self.manager.update_rules([], [PNPM_RULE], [], target="workspace")
        result = second["workspace"]
        self.assertTrue(result["updated"])
        self.assertEqual(result["superseded"], [NPM_RULE])
        self.assertTrue(result["conflicts"])
        self.assertTrue(
            any("superseded" in warning for warning in result["warnings"])
        )
        content = self.rules_path.read_text(encoding="utf-8")
        self.assertNotIn(NPM_RULE, content)
        self.assertIn(PNPM_RULE, content)
        self.assertIn("Keep this user-authored note.", content)

    def test_priority_eviction_keeps_high_value_rules(self) -> None:
        manager = AgentsMdManager(
            self.workspace,
            max_bytes=2_000,
            priority=lambda text: 100.0 if "alpha deployment" in text else 0.0,
        )
        constraints = [
            "Always preserve the alpha deployment checklist before releases."
        ] + [
            (
                f"Always apply deterministic workspace convention "
                f"quux{index:04d}alpha when editing source files."
            )
            for index in range(80)
        ]
        result = manager.update_rules([], constraints, [], target="workspace")
        content = self.rules_path.read_text(encoding="utf-8")
        self.assertIn("alpha deployment checklist", content)
        self.assertNotIn("workspace convention 79", content)
        self.assertGreater(result["workspace"]["kept"], 0)
        self.assertTrue(
            any(
                "removed" in warning and "first removed" in warning
                for warning in result["workspace"]["warnings"]
            )
        )

        self.rules_path.unlink()
        default_manager = AgentsMdManager(self.workspace, max_bytes=2_000)
        default_manager.update_rules([], constraints, [], target="workspace")
        default_content = self.rules_path.read_text(encoding="utf-8")
        self.assertNotIn("alpha deployment checklist", default_content)


if __name__ == "__main__":
    unittest.main()
