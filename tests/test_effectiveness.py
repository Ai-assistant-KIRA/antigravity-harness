"""Tests for outcome-aware memory retrieval, utility, and recurrence signals."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    from .support import IsolatedHarnessTestCase, make_event
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event

from harness.core.analyzer import SessionAnalysis
from harness.core.loader import SessionLoader
from harness.core.memory_store import (
    MemoryStore,
    SCHEMA_VERSION,
    lesson_utility,
    skill_utility,
)
from harness.core.util import one_line


def make_analysis(
    conversation_id: str,
    workspace: str = "",
    *,
    events=None,
    unresolved=None,
    outcome: str = "completed",
) -> SessionAnalysis:
    return SessionAnalysis(
        conversation_id=conversation_id,
        workspace=workspace,
        tasks=["Exercise the effectiveness path"],
        tool_calls=[{"name": "run_command", "args": {}, "output": "", "status": "DONE"}],
        contrastive_events=list(events or []),
        unresolved_events=list(unresolved or []),
        final_outcome=outcome,
    )


def iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )


class EffectivenessTestCase(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = self.root / "memory" / "effectiveness.db"
        self.experience = self.root / "memory" / "EFFECTIVENESS.md"
        self.store = MemoryStore(self.db, self.experience, workspace=self.workspace)

    def sql(self, statement: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(str(self.db))
        try:
            connection.execute(statement, params)
            connection.commit()
        finally:
            connection.close()

    def add_lesson(
        self,
        pattern: str,
        resolution: str,
        *,
        verified: bool = True,
        confidence: float = 0.9,
        trigger_tool: str = "run_command",
    ) -> int:
        return self.store.add_lesson(
            trigger_tool=trigger_tool,
            error_pattern=pattern,
            resolution=resolution,
            category="command",
            verified=verified,
            workspace=self.workspace,
            confidence=confidence,
        )


class TestSchemaUpgrade(EffectivenessTestCase):
    def test_v3_to_v4_upgrade_preserves_rows_and_repeat_open_is_a_noop(self) -> None:
        lesson_id = self.add_lesson("pytest schema failure", "Use the schema fix.")
        skill_path = str(
            self.workspace / ".agents" / "skills" / "schema-skill" / "SKILL.md"
        )
        self.store.record_skill_saved(
            name="schema-skill",
            description="Schema compatibility skill",
            file_path=skill_path,
            workspace=self.workspace,
            quality=0.6,
        )

        connection = sqlite3.connect(str(self.db))
        try:
            for column in (
                "surfaced_count",
                "last_surfaced_at",
                "post_surface_occurrences",
            ):
                connection.execute(f"ALTER TABLE lessons DROP COLUMN {column}")
            for column in ("surfaced_count", "last_surfaced_at"):
                connection.execute(f"ALTER TABLE skills_index DROP COLUMN {column}")
            connection.execute(
                "UPDATE meta SET value = '3' WHERE key = 'schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        upgraded = MemoryStore(self.db, self.experience, workspace=self.workspace)
        lesson = upgraded.get_lesson(lesson_id)
        self.assertIsNotNone(lesson)
        assert lesson is not None
        self.assertEqual(lesson["resolution"], "Use the schema fix.")
        self.assertEqual(lesson["surfaced_count"], 0)
        self.assertEqual(lesson["post_surface_occurrences"], 0)
        self.assertIsNone(lesson["last_surfaced_at"])
        skill = upgraded.get_all_skills(workspace=self.workspace)[0]
        self.assertEqual(skill["name"], "schema-skill")
        self.assertEqual(skill["surfaced_count"], 0)
        self.assertIsNone(skill["last_surfaced_at"])

        connection = sqlite3.connect(str(self.db))
        try:
            version = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            lesson_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(lessons)").fetchall()
            }
            skill_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(skills_index)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertTrue(
            {"surfaced_count", "last_surfaced_at", "post_surface_occurrences"}
            .issubset(lesson_columns)
        )
        self.assertTrue(
            {"surfaced_count", "last_surfaced_at"}.issubset(skill_columns)
        )

        # A second open must take the fast path, not rebuild the schema.
        with mock.patch.object(
            MemoryStore, "_create_schema", side_effect=AssertionError("rebuilt")
        ), mock.patch.object(
            MemoryStore, "_ensure_columns", side_effect=AssertionError("altered")
        ):
            MemoryStore(self.db, self.experience, workspace=self.workspace)


class TestSurfacedTracking(EffectivenessTestCase):
    def test_record_lessons_surfaced_increments_and_dedupes(self) -> None:
        first = self.add_lesson("pytest surfaced one", "First surfaced fix.")
        second = self.add_lesson("pytest surfaced two", "Second surfaced fix.")

        self.store.record_lessons_surfaced([first, first, second])
        self.store.record_lessons_surfaced([])
        self.store.record_lessons_surfaced([first, 999999, "not-an-id"])

        first_lesson = self.store.get_lesson(first)
        second_lesson = self.store.get_lesson(second)
        assert first_lesson is not None and second_lesson is not None
        self.assertEqual(first_lesson["surfaced_count"], 2)
        self.assertEqual(second_lesson["surfaced_count"], 1)
        self.assertTrue(first_lesson["last_surfaced_at"])
        self.assertTrue(second_lesson["last_surfaced_at"])

    def test_record_skills_surfaced_accepts_dicts_tuples_and_names(self) -> None:
        global_store = MemoryStore(self.db, self.experience, workspace=None)
        global_store.record_skill_saved(
            name="shared-skill",
            description="Global copy",
            file_path=str(self.plugin_dir / "skills" / "shared-skill" / "SKILL.md"),
            scope="global",
            quality=0.5,
        )
        self.store.record_skill_saved(
            name="shared-skill",
            description="Workspace copy",
            file_path=str(
                self.workspace / ".agents" / "skills" / "shared-skill" / "SKILL.md"
            ),
            workspace=self.workspace,
            quality=0.7,
        )

        self.store.record_skills_surfaced(
            [
                {"name": "shared-skill", "scope": "workspace"},
                {"name": "shared-skill", "scope": "workspace"},
                ("shared-skill", "global"),
                "shared-skill",
            ]
        )

        skills = self.store.get_all_skills(workspace=self.workspace)
        workspace_copy = [
            skill for skill in skills
            if skill["scope"] == "workspace"
        ][0]
        global_copy = [
            skill for skill in skills
            if skill["scope"] == "global"
        ][0]
        # The dict and the plain name both resolve to the workspace copy.
        self.assertEqual(workspace_copy["surfaced_count"], 2)
        self.assertEqual(global_copy["surfaced_count"], 1)
        self.assertTrue(workspace_copy["last_surfaced_at"])
        self.assertTrue(global_copy["last_surfaced_at"])

    def test_export_import_round_trip_preserves_outcome_fields(self) -> None:
        lesson_id = self.add_lesson("pytest export outcome", "Export the outcome.")
        self.store.record_lessons_surfaced([lesson_id])
        self.store.record_skills_surfaced(["export-skill"])
        self.store.record_skill_saved(
            name="export-skill",
            description="Exportable skill",
            file_path=str(self.workspace / ".agents" / "skills" / "export-skill" / "SKILL.md"),
            workspace=self.workspace,
            quality=0.8,
        )
        self.store.record_skills_surfaced(["export-skill"])

        export_path = self.root / "effectiveness-export.json"
        self.store.export_data(export_path, workspace=self.workspace)
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], SCHEMA_VERSION)
        lesson_row = payload["lessons"][0]
        self.assertEqual(lesson_row["surfaced_count"], 1)
        skill_row = [row for row in payload["skills"] if row["name"] == "export-skill"][0]
        self.assertEqual(skill_row["surfaced_count"], 1)

        imported_store = MemoryStore(
            self.root / "memory" / "imported.db",
            self.root / "memory" / "IMPORTED.md",
            workspace=self.workspace,
        )
        imported_store.import_data(export_path)
        imported_lesson = imported_store.search_lessons(
            "pytest", workspace=self.workspace
        )[0]
        self.assertEqual(imported_lesson["surfaced_count"], 1)
        imported_skill = [
            row for row in imported_store.get_all_skills(workspace=self.workspace)
            if row["name"] == "export-skill"
        ][0]
        self.assertEqual(imported_skill["surfaced_count"], 1)


class TestUtilityRanking(EffectivenessTestCase):
    def test_high_quality_recent_lesson_outranks_stale_recurring_lesson(self) -> None:
        clean = self.add_lesson(
            "pytest clean signal", "Use the clean fix.", confidence=0.95
        )
        noisy = self.add_lesson(
            "pytest noisy signal", "Use the noisy fix.", confidence=0.95
        )
        stale = self.add_lesson(
            "pytest stale signal",
            "Use the stale fix.",
            verified=False,
            confidence=0.3,
        )
        self.sql(
            "UPDATE lessons SET surfaced_count = 10, post_surface_occurrences = 3, "
            "last_seen = ? WHERE id = ?",
            (iso_days_ago(120), noisy),
        )
        self.sql(
            "UPDATE lessons SET last_seen = ? WHERE id = ?",
            (iso_days_ago(90), stale),
        )

        clean_lesson = self.store.get_lesson(clean)
        noisy_lesson = self.store.get_lesson(noisy)
        stale_lesson = self.store.get_lesson(stale)
        assert clean_lesson and noisy_lesson and stale_lesson
        self.assertGreater(
            lesson_utility(clean_lesson), lesson_utility(noisy_lesson)
        )
        self.assertGreater(
            lesson_utility(clean_lesson), lesson_utility(stale_lesson)
        )

        searched = self.store.search_lessons("pytest", workspace=self.workspace)
        self.assertEqual(searched[0]["id"], clean)
        recent = self.store.get_recent_lessons(
            workspace=self.workspace, verified_only=False
        )
        self.assertEqual(recent[0]["id"], clean)

    def test_verified_filter_is_kept_under_utility_ranking(self) -> None:
        candidate = self.add_lesson(
            "pytest filter candidate",
            "Candidate with higher raw confidence.",
            verified=False,
            confidence=1.0,
        )
        verified = self.add_lesson(
            "pytest filter verified",
            "Verified but stale.",
            verified=True,
            confidence=0.4,
        )
        self.sql(
            "UPDATE lessons SET last_seen = ? WHERE id = ?",
            (iso_days_ago(120), verified),
        )

        all_results = self.store.search_lessons("pytest", workspace=self.workspace)
        self.assertEqual(
            {lesson["id"] for lesson in all_results}, {candidate, verified}
        )
        candidate_lesson = self.store.get_lesson(candidate)
        verified_lesson = self.store.get_lesson(verified)
        assert candidate_lesson and verified_lesson
        self.assertGreater(
            lesson_utility(candidate_lesson), lesson_utility(verified_lesson)
        )

        verified_only = self.store.search_lessons(
            "pytest", workspace=self.workspace, verified_only=True
        )
        self.assertEqual([lesson["id"] for lesson in verified_only], [verified])
        recent_verified = self.store.get_recent_lessons(workspace=self.workspace)
        self.assertEqual([lesson["id"] for lesson in recent_verified], [verified])

    def test_scope_filter_is_kept_under_utility_ranking(self) -> None:
        self.add_lesson("pytest scoped signal", "Scoped fix.")
        global_store = MemoryStore(self.db, self.experience, workspace=None)
        global_store.add_lesson(
            trigger_tool="run_command",
            error_pattern="pytest global signal",
            resolution="Global fix.",
            category="command",
            verified=True,
            confidence=1.0,
        )

        scoped = self.store.search_lessons(
            "pytest", workspace=self.workspace, include_global=False
        )
        self.assertEqual(len(scoped), 1)
        self.assertIn("Scoped fix", scoped[0]["resolution"])
        with_global = self.store.search_lessons(
            "pytest", workspace=self.workspace, include_global=True
        )
        self.assertEqual(len(with_global), 2)

    def test_skill_utility_rewards_reuse_and_recency(self) -> None:
        fresh = self.store.record_skill_saved(
            name="fresh-skill",
            description="Fresh",
            file_path="fresh/SKILL.md",
            workspace=self.workspace,
            quality=0.8,
        )
        self.assertEqual(fresh, "created")
        self.store.record_skill_saved(
            name="stale-skill",
            description="Stale",
            file_path="stale/SKILL.md",
            workspace=self.workspace,
            quality=0.2,
        )
        for _ in range(5):
            self.store.record_skill_reuse("fresh-skill", workspace=self.workspace)
        self.sql(
            "UPDATE skills_index SET updated_at = ? WHERE name = 'stale-skill'",
            (iso_days_ago(120),),
        )
        skills = {
            skill["name"]: skill
            for skill in self.store.get_all_skills(workspace=self.workspace)
        }
        self.assertGreater(
            skill_utility(skills["fresh-skill"]),
            skill_utility(skills["stale-skill"]),
        )
        self.assertGreater(
            MemoryStore.skill_utility(skills["fresh-skill"]),
            MemoryStore.skill_utility(skills["stale-skill"]),
        )


class TestRecurrenceSignal(EffectivenessTestCase):
    def test_recurrence_after_surfacing_lowers_confidence(self) -> None:
        event = make_event(error_signature="RECUR-ONE")
        self.store.record_session(
            make_analysis("conv-recur-1", str(self.workspace), events=[event])
        )
        lesson = self.store.get_recent_lessons(workspace=self.workspace)[0]
        lesson_id = int(lesson["id"])
        self.assertEqual(lesson["post_surface_occurrences"], 0)
        self.sql(
            "UPDATE lessons SET last_seen = ? WHERE id = ?",
            (iso_days_ago(1), lesson_id),
        )
        self.store.record_lessons_surfaced([lesson_id])
        before = self.store.get_lesson(lesson_id)
        assert before is not None
        before_confidence = float(before["confidence"])

        counts = self.store.record_session(
            make_analysis(
                "conv-recur-2",
                str(self.workspace),
                events=[make_event(error_signature="RECUR-ONE")],
            )
        )
        self.assertEqual(counts["updated"], 1)
        after = self.store.get_lesson(lesson_id)
        assert after is not None
        self.assertEqual(after["occurrences"], 2)
        self.assertEqual(after["post_surface_occurrences"], 1)
        self.assertLess(float(after["confidence"]), before_confidence)
        self.assertTrue(after["verified"])

        # A later occurrence that was *not* preceded by another surfacing does
        # not count as a second failed briefing.
        self.store.record_session(
            make_analysis(
                "conv-recur-3",
                str(self.workspace),
                events=[make_event(error_signature="RECUR-ONE")],
            )
        )
        later = self.store.get_lesson(lesson_id)
        assert later is not None
        self.assertEqual(later["occurrences"], 3)
        self.assertEqual(later["post_surface_occurrences"], 1)

    def test_surfacing_and_recurrence_in_the_same_second_are_ordered(self) -> None:
        event = make_event(error_signature="RECUR-SAME-SECOND")
        self.store.record_session(
            make_analysis("conv-same-1", str(self.workspace), events=[event])
        )
        lesson = self.store.get_recent_lessons(workspace=self.workspace)[0]
        lesson_id = int(lesson["id"])
        self.store.record_lessons_surfaced([lesson_id])
        self.store.record_session(
            make_analysis(
                "conv-same-2",
                str(self.workspace),
                events=[make_event(error_signature="RECUR-SAME-SECOND")],
            )
        )
        after = self.store.get_lesson(lesson_id)
        assert after is not None
        self.assertEqual(after["post_surface_occurrences"], 1)

    def test_reanalysis_and_unsurfaced_lessons_do_not_count(self) -> None:
        surfaced = make_event(
            error_signature="RECUR-SURFACED",
            error_message="ModuleNotFoundError: surfaced recurrence",
        )
        unsurfaced = make_event(
            error_signature="RECUR-UNSURFACED",
            error_message="ModuleNotFoundError: unsurfaced recurrence",
        )
        self.store.record_session(
            make_analysis(
                "conv-reanalysis-1",
                str(self.workspace),
                events=[surfaced, unsurfaced],
            )
        )
        lessons = {
            lesson["error_pattern"]: lesson
            for lesson in self.store.get_recent_lessons(workspace=self.workspace)
        }
        surfaced_id = int(lessons[surfaced.error_message]["id"])
        unsurfaced_id = int(lessons[unsurfaced.error_message]["id"])
        self.sql(
            "UPDATE lessons SET last_seen = ? WHERE id IN (?, ?)",
            (iso_days_ago(1), surfaced_id, unsurfaced_id),
        )
        self.store.record_lessons_surfaced([surfaced_id])

        # Re-analysing the same conversation is linked and must not count.
        counts = self.store.record_session(
            make_analysis(
                "conv-reanalysis-1",
                str(self.workspace),
                events=[surfaced, unsurfaced],
            )
        )
        self.assertEqual(counts["skipped"], 2)
        linked = self.store.get_lesson(surfaced_id)
        assert linked is not None
        self.assertEqual(linked["post_surface_occurrences"], 0)

        counts = self.store.record_session(
            make_analysis(
                "conv-reanalysis-2",
                str(self.workspace),
                events=[surfaced, unsurfaced],
            )
        )
        self.assertEqual(counts["updated"], 2)
        recurring = self.store.get_lesson(surfaced_id)
        untouched = self.store.get_lesson(unsurfaced_id)
        assert recurring is not None and untouched is not None
        self.assertEqual(recurring["post_surface_occurrences"], 1)
        self.assertEqual(untouched["post_surface_occurrences"], 0)
        self.assertGreater(
            float(untouched["confidence"]), float(recurring["confidence"])
        )


class TestLoaderSurfacing(EffectivenessTestCase):
    def setUp(self) -> None:
        super().setUp()
        # SessionLoader resolves the default database, so write through the
        # same store path the loader will open.
        self.store = MemoryStore(workspace=self.workspace)

    def make_skill(self, name: str, description: str) -> Path:
        path = self.workspace / ".agents" / "skills" / name / "SKILL.md"
        self.write_text(
            path,
            f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n",
        )
        self.store.record_skill_saved(
            name=name,
            description=description,
            file_path=str(path),
            workspace=self.workspace,
            quality=0.5,
        )
        return path

    def test_loader_records_exactly_the_shown_items(self) -> None:
        for index in range(9):
            self.make_skill(
                f"skill-{index:02d}",
                f"Use skill number {index:02d} for deterministic tests.",
            )
        lessons = []
        for index in range(6):
            lessons.append(
                self.add_lesson(
                    f"pytest loader signal {index}",
                    f"Apply loader fix number {index}.",
                )
            )

        briefing = SessionLoader(self.workspace).build_briefing(self.workspace)

        surfaced_skills = 0
        for index in range(9):
            name = f"skill-{index:02d}"
            description = f"Use skill number {index:02d} for deterministic tests."
            line = f"- `{name}` (workspace): {one_line(description, 150)}"
            shown = line in briefing
            skill = [
                item for item in self.store.get_all_skills(workspace=self.workspace)
                if item["name"] == name
            ][0]
            self.assertEqual(
                skill["surfaced_count"], 1 if shown else 0, name
            )
            surfaced_skills += 1 if shown else 0
        self.assertEqual(surfaced_skills, 8)

        surfaced_lessons = 0
        for index, lesson_id in enumerate(lessons):
            line = f"- `run_command`: Apply loader fix number {index}."
            shown = line in briefing
            lesson = self.store.get_lesson(lesson_id)
            assert lesson is not None
            self.assertEqual(
                lesson["surfaced_count"], 1 if shown else 0, str(lesson_id)
            )
            surfaced_lessons += 1 if shown else 0
        self.assertEqual(surfaced_lessons, 5)

    def test_truncation_does_not_record_dropped_items(self) -> None:
        for index in range(9):
            self.make_skill(
                f"skill-{index:02d}",
                f"Use skill number {index:02d} for deterministic tests.",
            )
        lessons = []
        for index in range(6):
            lessons.append(
                self.add_lesson(
                    f"pytest truncated signal {index}",
                    f"Apply truncated fix number {index}.",
                )
            )

        loader = SessionLoader(self.workspace)
        loader.max_briefing_chars = 900
        briefing = loader.build_briefing(self.workspace)
        self.assertIn("[Briefing truncated]", briefing)

        expected_lessons = 0
        for index, lesson_id in enumerate(lessons):
            line = f"- `run_command`: Apply truncated fix number {index}."
            shown = line in briefing
            lesson = self.store.get_lesson(lesson_id)
            assert lesson is not None
            self.assertEqual(
                lesson["surfaced_count"], 1 if shown else 0, str(lesson_id)
            )
            expected_lessons += 1 if shown else 0
        self.assertLess(expected_lessons, 5)

        for index in range(9):
            name = f"skill-{index:02d}"
            description = f"Use skill number {index:02d} for deterministic tests."
            line = f"- `{name}` (workspace): {one_line(description, 150)}"
            shown = line in briefing
            skill = [
                item for item in self.store.get_all_skills(workspace=self.workspace)
                if item["name"] == name
            ][0]
            self.assertEqual(
                skill["surfaced_count"], 1 if shown else 0, name
            )
        self.assertEqual(self.store.get_stats(workspace=self.workspace)["sessions"], 0)

    def test_reused_high_quality_skill_outranks_stale_low_quality_skill(self) -> None:
        self.make_skill("alpha-skill", "First deterministic skill.")
        self.make_skill("omega-skill", "Second deterministic skill.")
        with self.store._connect() as conn:
            conn.execute(
                "UPDATE skills_index SET quality = 0.95, reused_count = 7, "
                "last_used_at = ? WHERE name = 'alpha-skill'",
                (iso_days_ago(1),),
            )
            conn.execute(
                "UPDATE skills_index SET quality = 0.1, reused_count = 0, "
                "updated_at = ? WHERE name = 'omega-skill'",
                (iso_days_ago(120),),
            )
        ranked = SessionLoader(self.workspace)._rank_skills(self.workspace, [])
        self.assertEqual(ranked[0]["name"], "alpha-skill")


if __name__ == "__main__":
    unittest.main()
