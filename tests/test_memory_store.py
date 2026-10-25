"""Tests for SQLite memory, scoping, search, migration, and backup."""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

try:
    from .support import IsolatedHarnessTestCase, make_event
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event

from harness.core.analyzer import SessionAnalysis
from harness.core.memory_store import MemoryStore, SCHEMA_VERSION


def make_analysis(
    conversation_id: str,
    workspace: str = "",
    *,
    events=None,
    unresolved=None,
    tasks=None,
    outcome: str = "completed",
) -> SessionAnalysis:
    return SessionAnalysis(
        conversation_id=conversation_id,
        workspace=workspace,
        tasks=tasks or ["Set up the project test suite"],
        tool_calls=[{"name": "run_command", "args": {}, "output": "", "status": "DONE"}],
        contrastive_events=list(events or []),
        unresolved_events=list(unresolved or []),
        final_outcome=outcome,
    )


class TestMemoryStore(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.root / "memory" / "harness.db"
        self.experience = self.root / "memory" / "EXPERIENCE.md"
        self.store = MemoryStore(self.db, self.experience, workspace=self.workspace)
        self.other_workspace = self.root / "other-workspace"
        self.other_workspace.mkdir()

    def test_record_session_lesson_and_experience(self) -> None:
        analysis = make_analysis(
            "conv-1", str(self.workspace), events=[make_event()]
        )
        counts = self.store.record_session(analysis)
        self.assertEqual(counts, {"created": 1, "updated": 0, "skipped": 0})

        lessons = self.store.get_recent_lessons(workspace=self.workspace)
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["trigger_tool"], "run_command")
        self.assertTrue(lessons[0]["verified"])
        self.assertIn("pip install pytest", lessons[0]["resolution"])
        self.assertEqual(lessons[0]["surfaced_count"], 0)
        self.assertEqual(lessons[0]["post_surface_occurrences"], 0)
        self.assertIn("utility", lessons[0])

        self.assertTrue(self.experience.exists())
        text = self.experience.read_text(encoding="utf-8")
        self.assertIn("Harness² Experience Library", text)
        self.assertIn("pytest", text)
        self.assertIn("## Verified Lessons", text)

    def test_session_is_idempotent_and_occurrences_accumulate_across_sessions(self) -> None:
        analysis = make_analysis("conv-idem", str(self.workspace), events=[make_event()])
        self.store.record_session(analysis)
        second = self.store.record_session(analysis)
        self.assertEqual(second, {"created": 0, "updated": 0, "skipped": 1})
        lessons = self.store.get_recent_lessons(workspace=self.workspace)
        self.assertEqual(lessons[0]["occurrences"], 1)

        other = make_analysis("conv-idem-2", str(self.workspace), events=[make_event()])
        third = self.store.record_session(other)
        self.assertEqual(third, {"created": 0, "updated": 1, "skipped": 0})
        lessons = self.store.get_recent_lessons(workspace=self.workspace)
        self.assertEqual(lessons[0]["occurrences"], 2)

        stats = self.store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["verified_lessons"], 1)

    def test_unresolved_events_stay_candidates_and_are_searchable(self) -> None:
        candidate = make_event(
            verified=False,
            confidence=0.2,
            error_message="ModuleNotFoundError: No module named 'httpx'",
            resolution_summary="`pytest` failed with `No module named httpx`; no verified recovery was observed.",
        )
        self.store.record_session(
            make_analysis(
                "conv-candidate",
                str(self.workspace),
                unresolved=[candidate],
                outcome="partial",
            )
        )
        self.assertEqual(self.store.get_recent_lessons(workspace=self.workspace), [])
        candidates = self.store.search_lessons(
            "httpx", workspace=self.workspace, include_global=False
        )
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["verified"])
        stats = self.store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["candidate_lessons"], 1)
        self.assertEqual(stats["verified_lessons"], 0)

    def test_workspace_scoping_and_global_lessons(self) -> None:
        other_store = MemoryStore(self.db, self.experience, workspace=self.other_workspace)
        self.store.record_session(
            make_analysis(
                "conv-a",
                str(self.workspace),
                events=[make_event(error_message="pytest missing in A", error_signature="A")],
            )
        )
        other_store.record_session(
            make_analysis(
                "conv-b",
                str(self.other_workspace),
                events=[make_event(error_message="pytest missing in B", error_signature="B")],
            )
        )

        global_store = MemoryStore(self.db, self.experience, workspace=None)
        global_store.record_session(
            make_analysis(
                "conv-global",
                "",
                events=[make_event(error_message="global environment failure", error_signature="G")],
            )
        )

        scoped = self.store.get_recent_lessons(
            workspace=self.workspace, include_global=False
        )
        self.assertEqual(len(scoped), 1)
        self.assertIn("in A", scoped[0]["error_pattern"])

        with_global = self.store.get_recent_lessons(workspace=self.workspace)
        self.assertEqual(len(with_global), 2)
        self.assertTrue(any(item["project"] == "global" for item in with_global))

        searched = self.store.search_lessons("pytest", workspace=self.workspace)
        self.assertTrue(any("in A" in item["error_pattern"] for item in searched))
        self.assertFalse(any("in B" in item["error_pattern"] for item in searched))

    def test_unscoped_search_is_global_only_unless_all_projects(self) -> None:
        self.store.record_session(
            make_analysis("conv-unscoped", str(self.workspace), events=[make_event()])
        )
        # A no-workspace search must not leak project lessons into another project.
        self.assertEqual(
            MemoryStore(self.db, self.experience).search_lessons("pytest"), []
        )
        results = MemoryStore(self.db, self.experience).search_lessons(
            "pytest", all_projects=True
        )
        self.assertEqual(len(results), 1)

    def test_search_like_fallback(self) -> None:
        self.store.record_session(
            make_analysis("conv-fallback", str(self.workspace), events=[make_event()])
        )
        self.store.fts_enabled = False
        results = self.store.search_lessons("pytest", workspace=self.workspace)
        self.assertEqual(len(results), 1)

    def test_skill_save_and_reuse_are_separate_counters(self) -> None:
        status = self.store.record_skill_saved(
            name="demo-skill",
            description="A deterministic demo skill",
            file_path=str(self.workspace / ".agents" / "skills" / "demo-skill" / "SKILL.md"),
            workspace=self.workspace,
        )
        self.assertEqual(status, "created")
        status = self.store.record_skill_saved(
            name="demo-skill",
            description="An updated deterministic demo skill",
            file_path=str(self.workspace / ".agents" / "skills" / "demo-skill" / "SKILL.md"),
            workspace=self.workspace,
        )
        self.assertEqual(status, "updated")
        skill = self.store.get_all_skills(workspace=self.workspace)[0]
        self.assertEqual(skill["reused_count"], 0)
        self.assertEqual(skill["update_count"], 1)

        self.assertTrue(self.store.record_skill_reuse("demo-skill", workspace=self.workspace))
        skill = self.store.get_all_skills(workspace=self.workspace)[0]
        self.assertEqual(skill["reused_count"], 1)
        self.assertIsNotNone(skill["last_used_at"])

    def test_skill_reuse_prefers_the_workspace_copy(self) -> None:
        global_store = MemoryStore(self.db, self.experience, workspace=None)
        global_store.record_skill_saved(
            name="shared-skill",
            description="Global copy",
            file_path=str(self.plugin_dir / "skills" / "shared-skill" / "SKILL.md"),
            scope="global",
        )
        self.store.record_skill_saved(
            name="shared-skill",
            description="Workspace copy",
            file_path=str(self.workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md"),
            workspace=self.workspace,
        )
        self.store.record_skill_reuse("shared-skill", workspace=self.workspace)
        workspace_copy = [
            skill for skill in self.store.get_all_skills(workspace=self.workspace)
            if skill["name"] == "shared-skill" and skill["scope"] == "workspace"
        ][0]
        global_copy = [
            skill for skill in self.store.get_all_skills()
            if skill["name"] == "shared-skill" and skill["scope"] == "global"
        ][0]
        self.assertEqual(workspace_copy["reused_count"], 1)
        self.assertEqual(global_copy["reused_count"], 0)

    def test_delete_skill_record_by_path(self) -> None:
        path = str(self.workspace / ".agents" / "skills" / "gone" / "SKILL.md")
        self.store.record_skill_saved(
            name="gone", description="gone", file_path=path, workspace=self.workspace
        )
        self.assertEqual(self.store.delete_skill_record("gone", file_path=path), 1)
        self.assertEqual(self.store.get_all_skills(workspace=self.workspace), [])

    def test_rule_edits(self) -> None:
        self.store.record_rule_edit("/tmp/AGENTS.md", "added 2 rules")
        edits = self.store.get_recent_rule_edits()
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["change_summary"], "added 2 rules")

    def test_candidate_promotes_after_a_later_verified_recovery(self) -> None:
        candidate = make_event(
            verified=False,
            confidence=0.2,
            error_signature="SAME-ERROR",
            error_message="ModuleNotFoundError: No module named 'pytest'",
            resolution_summary="`pytest` failed; no verified recovery was observed.",
        )
        self.store.record_session(
            make_analysis(
                "conv-promote",
                str(self.workspace),
                unresolved=[candidate],
                outcome="partial",
            )
        )
        self.assertEqual(
            self.store.get_stats(workspace=self.workspace)["candidate_lessons"], 1
        )

        recovered = make_event(
            error_signature="SAME-ERROR",
            error_message="ModuleNotFoundError: No module named 'pytest'",
        )
        counts = self.store.record_session(
            make_analysis("conv-promote", str(self.workspace), events=[recovered])
        )
        self.assertEqual(counts["updated"], 1)
        lessons = self.store.get_recent_lessons(workspace=self.workspace)
        self.assertEqual(len(lessons), 1)
        self.assertTrue(lessons[0]["verified"])
        self.assertIn("pip install pytest", lessons[0]["resolution"])
        self.assertEqual(lessons[0]["occurrences"], 1)
        stats = self.store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["verified_lessons"], 1)
        self.assertEqual(stats["candidate_lessons"], 0)

    def test_export_import_merge_and_replace(self) -> None:
        self.store.record_session(
            make_analysis("conv-export", str(self.workspace), events=[make_event()])
        )
        self.store.record_rule_edit("AGENTS.md", "one edit")
        export_path = self.root / "export.json"
        self.store.export_data(export_path, workspace=self.workspace)
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], SCHEMA_VERSION)
        self.assertEqual(len(payload["lessons"]), 1)
        self.assertEqual(len(payload["session_lessons"]), 1)
        self.assertEqual(len(payload["rule_edits"]), 1)
        self.assertIn("surfaced_count", payload["lessons"][0])
        self.assertIn("post_surface_occurrences", payload["lessons"][0])

        db2 = self.root / "memory" / "second.db"
        exp2 = self.root / "memory" / "SECOND.md"
        imported = MemoryStore(db2, exp2).import_data(export_path)
        self.assertEqual(imported["sessions"], 1)
        self.assertEqual(imported["lessons"], 1)
        self.assertEqual(imported["links"], 1)
        self.assertEqual(imported["rule_edits"], 1)

        # A plain merge is idempotent for lessons, links, and rule edits.
        merge = MemoryStore(db2, exp2).import_data(export_path)
        self.assertEqual(merge["lessons"], 0)
        self.assertEqual(merge["links"], 0)
        self.assertEqual(merge["rule_edits"], 0)

        payload["lessons"][0]["resolution"] = "Replacement resolution from import."
        payload["lessons"][0]["confidence"] = 0.99
        export_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        replaced = MemoryStore(db2, exp2).import_data(export_path, merge=False)
        self.assertEqual(replaced["lessons"], 1)
        lesson = MemoryStore(db2, exp2).search_lessons("pytest", all_projects=True)[0]
        self.assertEqual(lesson["resolution"], "Replacement resolution from import.")
        self.assertAlmostEqual(lesson["confidence"], 0.99)

    def test_prune_deletes_candidates_and_links(self) -> None:
        candidate = make_event(
            verified=False,
            confidence=0.2,
            error_message="candidate failure",
            resolution_summary="`tool` failed; no verified recovery was observed.",
        )
        self.store.record_session(
            make_analysis(
                "conv-prune",
                str(self.workspace),
                unresolved=[candidate],
                outcome="partial",
            )
        )
        deleted = self.store.prune(candidate_days=-1, days=-1)
        self.assertGreaterEqual(deleted["lessons"], 1)
        self.assertGreaterEqual(deleted["links"], 1)
        self.assertEqual(self.store.get_stats(workspace=self.workspace)["candidate_lessons"], 0)

    def test_reset_workspace_is_scoped(self) -> None:
        other_store = MemoryStore(self.db, self.experience, workspace=self.other_workspace)
        self.store.record_session(
            make_analysis(
                "conv-a", str(self.workspace),
                events=[make_event(error_message="failure A", error_signature="A")],
            )
        )
        other_store.record_session(
            make_analysis(
                "conv-b", str(self.other_workspace),
                events=[make_event(error_message="failure B", error_signature="B")],
            )
        )
        self.store.record_skill_saved(
            name="a-skill", description="a", file_path="a/SKILL.md", workspace=self.workspace
        )
        counts = self.store.reset_workspace(self.workspace)
        self.assertEqual(counts["sessions"], 1)
        self.assertEqual(counts["lessons"], 1)
        self.assertEqual(counts["skills"], 1)
        remaining = MemoryStore(self.db, self.experience).get_recent_lessons(
            all_projects=True
        )
        self.assertEqual(len(remaining), 1)
        self.assertIn("failure B", remaining[0]["error_pattern"])
        connection = sqlite3.connect(str(self.db))
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM session_lessons").fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_backup_is_a_consistent_database_copy(self) -> None:
        self.store.record_session(
            make_analysis("conv-backup", str(self.workspace), events=[make_event()])
        )
        destination = self.store.backup(self.root / "backup")
        backup_db = destination / self.db.name
        self.assertTrue(backup_db.exists())
        self.assertTrue((destination / self.experience.name).exists())
        connection = sqlite3.connect(str(backup_db))
        try:
            count = connection.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)


class TestMigration(IsolatedHarnessTestCase):
    def _create_v1_database(self, path: Path) -> None:
        connection = sqlite3.connect(str(path))
        try:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    conversation_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    task_summary TEXT,
                    outcome TEXT,
                    tools_used_count INTEGER,
                    errors_count INTEGER,
                    lessons_count INTEGER
                );
                CREATE TABLE lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    category TEXT,
                    trigger_tool TEXT,
                    error_pattern TEXT,
                    resolution TEXT,
                    occurrences INTEGER,
                    first_seen TEXT,
                    last_seen TEXT,
                    confidence REAL,
                    verified INTEGER
                );
                CREATE TABLE skills_index (
                    name TEXT PRIMARY KEY,
                    description TEXT,
                    file_path TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    reused_count INTEGER
                );
                CREATE TABLE rule_edits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    target_file TEXT,
                    change_summary TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES ('legacy-1', '2026-01-01T00:00:00+00:00', 'legacy task', 'completed', 3, 1, 2)"
            )
            connection.execute(
                "INSERT INTO lessons (conversation_id, category, trigger_tool, error_pattern, resolution, occurrences, first_seen, last_seen, confidence, verified) "
                "VALUES ('legacy-1', 'dependency', 'run_command', "
                "'ModuleNotFoundError: No module named pytest', "
                "'Installed pytest with pip install pytest to fix the missing dependency.', "
                "1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0.8, 1)"
            )
            connection.execute(
                "INSERT INTO lessons (conversation_id, category, trigger_tool, error_pattern, resolution, occurrences, first_seen, last_seen, confidence, verified) "
                "VALUES ('legacy-1', 'command', 'run_command', "
                "'Command exited with code 1 at 2026-10-23T10:00:00Z', "
                "'Completed at: 2026-10-23T10:00:01Z', "
                "1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0.4, 1)"
            )
            connection.execute(
                "INSERT INTO skills_index VALUES ('legacy-skill', 'Legacy skill', "
                "'C:\\Users\\test\\.gemini\\config\\plugins\\harness\\skills\\legacy-skill\\SKILL.md', "
                "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0)"
            )
            connection.execute(
                "INSERT INTO rule_edits (timestamp, target_file, change_summary) "
                "VALUES ('2026-01-01T00:00:00+00:00', 'AGENTS.md', 'legacy edit')"
            )
            connection.commit()
        finally:
            connection.close()

    def test_v1_database_is_migrated_and_noise_is_dropped(self) -> None:
        db = self.root / "legacy.db"
        experience = self.root / "legacy-EXPERIENCE.md"
        self._create_v1_database(db)
        store = MemoryStore(db, experience)

        stats = store.get_stats()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["verified_lessons"], 1)
        self.assertEqual(stats["candidate_lessons"], 0)
        self.assertEqual(stats["skills"], 1)
        self.assertEqual(stats["rule_edits"], 1)

        lessons = store.get_recent_lessons()
        self.assertEqual(len(lessons), 1)
        self.assertIn("Installed pytest", lessons[0]["resolution"])
        self.assertNotIn("Completed at", lessons[0]["resolution"])

        skill = store.get_all_skills()[0]
        self.assertEqual(skill["scope"], "plugin")

        connection = sqlite3.connect(str(db))
        try:
            version = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(lessons)").fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertIn("fingerprint", columns)
        self.assertTrue(stats["fts"])


if __name__ == "__main__":
    unittest.main()
