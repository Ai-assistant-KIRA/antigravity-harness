"""Tests for manual memory editing through the store and the CLI."""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.cli import main
from harness.core.loader import SessionLoader
from harness.core.memory_store import MemoryStore


class TestMemoryEditing(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.root / "memory" / "harness.db"
        self.experience = self.root / "memory" / "EXPERIENCE.md"
        self.store = MemoryStore(self.db, self.experience, workspace=self.workspace)
        self.other_workspace = self.root / "other"
        self.other_workspace.mkdir()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(list(arguments))
        except SystemExit as exc:
            code = int(exc.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    def add_demo_lesson(self, **overrides: object) -> int:
        values: dict[str, object] = {
            "trigger_tool": "run_command",
            "error_pattern": "ModuleNotFoundError: No module named 'requests'",
            "resolution": "Install the missing client with pip install requests.",
            "category": "dependency",
        }
        values.update(overrides)
        return self.store.add_lesson(**values)

    def test_add_get_search_update_delete_round_trip(self) -> None:
        lesson_id = self.add_demo_lesson()
        self.assertIsInstance(lesson_id, int)

        lesson = self.store.get_lesson(lesson_id)
        self.assertIsNotNone(lesson)
        assert lesson is not None
        self.assertFalse(lesson["verified"])
        self.assertEqual(lesson["source"], "manual")
        self.assertEqual(lesson["occurrences"], 1)

        found = self.store.search_lessons("requests", workspace=self.workspace)
        self.assertEqual([item["id"] for item in found], [lesson_id])

        updated = self.store.update_lesson(
            lesson_id,
            resolution="Install requests with pip install requests before retrying.",
            verified=True,
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertTrue(updated["verified"])
        self.assertIn("pip install requests", updated["resolution"])

        recent = self.store.get_recent_lessons(
            workspace=self.workspace, verified_only=True
        )
        self.assertEqual([item["id"] for item in recent], [lesson_id])

        self.assertTrue(self.store.delete_lesson(lesson_id))
        self.assertIsNone(self.store.get_lesson(lesson_id))
        self.assertEqual(
            self.store.search_lessons("requests", workspace=self.workspace), []
        )

    def test_workspace_scoping(self) -> None:
        lesson_id = self.add_demo_lesson(workspace=self.workspace)

        visible = self.store.list_lessons(workspace=self.workspace)
        self.assertEqual([item["id"] for item in visible], [lesson_id])
        self.assertEqual(visible[0]["workspace_name"], self.workspace.name)
        self.assertEqual(visible[0]["scope"], "workspace")

        other_store = MemoryStore(
            self.db, self.experience, workspace=self.other_workspace
        )
        hidden = other_store.list_lessons(workspace=self.other_workspace)
        self.assertNotIn(lesson_id, [item["id"] for item in hidden])

    def test_re_adding_as_candidate_keeps_verified_and_confidence(self) -> None:
        lesson_id = self.add_demo_lesson(
            verified=True, confidence=0.9, resolution="Verified fix."
        )
        again = self.add_demo_lesson(
            verified=False, confidence=0.2, resolution="Candidate restatement."
        )
        self.assertEqual(again, lesson_id)
        lesson = self.store.get_lesson(lesson_id)
        assert lesson is not None
        self.assertTrue(lesson["verified"])
        self.assertAlmostEqual(lesson["confidence"], 0.9)
        self.assertEqual(lesson["resolution"], "Candidate restatement.")

    def test_fingerprint_update_migrates_session_links(self) -> None:
        lesson_id = self.add_demo_lesson()
        original = self.store.get_lesson(lesson_id)
        assert original is not None
        old_fingerprint = original["fingerprint"]

        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute(
                "INSERT INTO session_lessons (conversation_id, fingerprint, seen_at) "
                "VALUES (?, ?, ?)",
                ("conv-link", old_fingerprint, "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()

        updated = self.store.update_lesson(
            lesson_id,
            error_pattern="ModuleNotFoundError: No module named 'httpx'",
        )
        assert updated is not None
        new_fingerprint = updated["fingerprint"]
        self.assertNotEqual(old_fingerprint, new_fingerprint)

        connection = sqlite3.connect(str(self.db))
        try:
            links = connection.execute(
                "SELECT fingerprint FROM session_lessons WHERE conversation_id = ?",
                ("conv-link",),
            ).fetchall()
            stale = connection.execute(
                "SELECT COUNT(*) FROM session_lessons WHERE fingerprint = ?",
                (old_fingerprint,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual([row[0] for row in links], [new_fingerprint])
        self.assertEqual(stale, 0)

    def test_manual_fingerprint_preserves_numeric_differences(self) -> None:
        first = self.add_demo_lesson(
            trigger_tool="run_command",
            error_pattern="failure number 0 alpha",
            resolution="Handle zero.",
        )
        second = self.add_demo_lesson(
            trigger_tool="run_command",
            error_pattern="failure number 1 alpha",
            resolution="Handle one.",
        )
        self.assertNotEqual(first, second)
        self.assertEqual(
            {item["id"] for item in self.store.list_lessons(workspace=self.workspace)},
            {first, second},
        )

    def test_id_operations_are_workspace_scoped(self) -> None:
        workspace_lesson = self.add_demo_lesson(workspace=self.workspace)
        global_store = MemoryStore(self.db, self.experience)
        global_lesson = global_store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Global environment quirk",
            resolution="Set the environment variable before running.",
        )

        self.assertIsNotNone(
            self.store.get_lesson(workspace_lesson, workspace=self.workspace)
        )
        self.assertIsNone(
            self.store.get_lesson(workspace_lesson, workspace=self.other_workspace)
        )
        self.assertIsNotNone(
            self.store.get_lesson(global_lesson, workspace=self.other_workspace)
        )

        self.assertIsNone(
            self.store.update_lesson(
                workspace_lesson,
                workspace=self.other_workspace,
                resolution="Hijacked resolution.",
            )
        )
        unchanged = self.store.get_lesson(workspace_lesson)
        assert unchanged is not None
        self.assertEqual(
            unchanged["resolution"],
            "Install the missing client with pip install requests.",
        )
        self.assertFalse(
            self.store.delete_lesson(workspace_lesson, workspace=self.other_workspace)
        )
        self.assertIsNotNone(self.store.get_lesson(workspace_lesson))

        owner_update = self.store.update_lesson(
            workspace_lesson,
            workspace=self.workspace,
            resolution="Owner update.",
        )
        self.assertIsNotNone(owner_update)
        assert owner_update is not None
        self.assertEqual(owner_update["resolution"], "Owner update.")
        self.assertTrue(
            self.store.delete_lesson(workspace_lesson, workspace=self.workspace)
        )
        self.assertIsNone(self.store.get_lesson(workspace_lesson))

    def test_fingerprint_collision_updates_and_rejects_duplicates(self) -> None:
        first = self.add_demo_lesson(resolution="First resolution.")
        second = self.add_demo_lesson(
            resolution="Updated resolution from the second add.", verified=True
        )
        self.assertEqual(first, second)
        self.assertEqual(
            len(self.store.list_lessons(workspace=self.workspace)), 1
        )
        lesson = self.store.get_lesson(first)
        assert lesson is not None
        self.assertEqual(lesson["resolution"], "Updated resolution from the second add.")
        self.assertTrue(lesson["verified"])

        other = self.add_demo_lesson(
            trigger_tool="run_command",
            error_pattern="ModuleNotFoundError: No module named 'httpx'",
            resolution="Install httpx.",
        )
        self.assertNotEqual(other, first)
        with self.assertRaises(ValueError):
            self.store.update_lesson(
                other,
                error_pattern="ModuleNotFoundError: No module named 'requests'",
            )

    def test_cli_add_list_show_edit_delete(self) -> None:
        code, out, err = self.run_cli(
            "memory",
            "add",
            "--trigger-tool",
            "run_command",
            "--error-pattern",
            "ModuleNotFoundError: No module named 'requests'",
            "--resolution",
            "Install requests with pip install requests.",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        lesson_id = json.loads(out)["id"]
        self.assertIsInstance(lesson_id, int)

        code, out, err = self.run_cli(
            "memory", "list", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, 0, err)
        self.assertIn(f"#{lesson_id}", out)
        self.assertIn("candidate", out)

        code, out, err = self.run_cli(
            "memory", "show", str(lesson_id), "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["id"], lesson_id)
        self.assertIn("pip install requests", shown["resolution"])

        code, out, err = self.run_cli(
            "memory",
            "edit",
            str(lesson_id),
            "--resolution",
            "Install requests, then re-run the failing command.",
            "--verified",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        edited = json.loads(out)
        self.assertTrue(edited["verified"])
        self.assertEqual(
            edited["resolution"], "Install requests, then re-run the failing command."
        )

        code, out, err = self.run_cli(
            "memory",
            "delete",
            str(lesson_id),
            "--yes",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["deleted"])
        self.assertIsNone(
            MemoryStore(workspace=self.workspace).get_lesson(lesson_id)
        )

    def test_cli_id_operations_respect_workspace_scope(self) -> None:
        store = MemoryStore(workspace=self.workspace)
        lesson_id = store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Workspace A only failure",
            resolution="Apply the workspace A fix.",
            workspace=self.workspace,
        )
        global_store = MemoryStore()
        global_id = global_store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Global environment quirk",
            resolution="Set the environment variable before running.",
        )

        code, out, err = self.run_cli(
            "memory", "show", str(lesson_id),
            "--workspace", str(self.other_workspace), "--json",
        )
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

        code, out, err = self.run_cli(
            "memory", "edit", str(lesson_id),
            "--resolution", "Cross-workspace edit.",
            "--workspace", str(self.other_workspace), "--json",
        )
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        unchanged = store.get_lesson(lesson_id)
        assert unchanged is not None
        self.assertEqual(unchanged["resolution"], "Apply the workspace A fix.")

        code, out, err = self.run_cli(
            "memory", "delete", str(lesson_id), "--yes",
            "--workspace", str(self.other_workspace), "--json",
        )
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        self.assertIsNotNone(store.get_lesson(lesson_id))

        code, out, err = self.run_cli(
            "memory", "show", str(global_id),
            "--workspace", str(self.other_workspace), "--json",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["id"], global_id)

        code, out, err = self.run_cli(
            "memory", "delete", str(lesson_id), "--yes",
            "--workspace", str(self.workspace), "--json",
        )
        self.assertEqual(code, 0, err)
        self.assertIsNone(store.get_lesson(lesson_id))

    def test_verified_manual_lesson_reaches_session_briefing(self) -> None:
        # Use the default store so SessionLoader reads the same database.
        store = MemoryStore(workspace=self.workspace)
        store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Interpreter mismatch when running tests",
            resolution="Use the pinned toolchain from .python-version.",
            verified=True,
            workspace=self.workspace,
            category="environment",
        )
        briefing = SessionLoader(self.workspace).build_briefing(self.workspace)
        self.assertIn("Use the pinned toolchain from .python-version.", briefing)

    def test_sync_experience_md_mentions_editing_commands(self) -> None:
        self.add_demo_lesson(verified=True)
        text = self.experience.read_text(encoding="utf-8")
        self.assertIn("harness memory edit <id>", text)
        self.assertIn("harness memory add", text)
        self.assertIn("harness memory delete <id>", text)

    def test_edit_without_fields_and_delete_missing_id_fail(self) -> None:
        store = MemoryStore(workspace=self.workspace)
        lesson_id = store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Interpreter mismatch when running tests",
            resolution="Use the pinned toolchain from .python-version.",
            workspace=self.workspace,
        )
        code, out, err = self.run_cli(
            "memory", "edit", str(lesson_id), "--workspace", str(self.workspace)
        )
        self.assertEqual(code, 1)
        self.assertIn("at least one field", err)

        code, out, err = self.run_cli(
            "memory", "delete", "999999", "--yes", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, 1)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
