"""Tests for project-local task routing and per-project memory isolation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core.agents_md_manager import SECTION_ROUTING
from harness.core.engine import HarnessEngine
from harness.core.memory_store import MemoryStore
from harness.core.retrieval import build_pre_invocation_payload
from harness.core.router import TaskRouter
from harness.core.skill_builder import SkillBuilder


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


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


class ProjectRoutingTestCase(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.builder = SkillBuilder(self.workspace)
        self.agents_md = self.workspace / "AGENTS.md"

    def install_skill(
        self,
        name: str,
        description: str,
        body: str = "# Patch\n\n## Steps\n\n1. Reproduce the failure.\n",
        scope: str = "workspace",
    ) -> Path:
        source = self.make_skill_source(name, description=description, body=body)
        self.builder.install_skill(
            str(source),
            scope=scope,
            workspace_path=self.workspace if scope == "workspace" else None,
        )
        return source


class TestTaskRouter(ProjectRoutingTestCase):
    def test_only_workspace_skills_are_eligible(self) -> None:
        self.install_skill(
            "pytest-fixes",
            "Fix failing pytest test suites and missing test dependencies.",
        )
        self.install_skill(
            "global-pytest-hints",
            "Global pytest helper that must not leak into a project.",
            scope="global",
        )

        router = TaskRouter(self.workspace)
        names = [skill["name"] for skill in router.project_skills()]
        self.assertEqual(names, ["pytest-fixes"])

        selection = router.select("pytest tests are failing, fix the suite")
        selected = [skill["name"] for skill in selection.skills]
        self.assertIn("pytest-fixes", selected)
        self.assertNotIn("global-pytest-hints", selected)

    def test_route_writes_managed_section_and_preserves_user_content(self) -> None:
        self.install_skill(
            "pytest-fixes",
            "Fix failing pytest test suites and missing test dependencies.",
        )
        self.write_text(
            self.agents_md, "# My Project\n\nKeep this hand-written note.\n"
        )

        result = TaskRouter(self.workspace).apply("fix the failing pytest suite")
        self.assertTrue(result.write["updated"])
        text = self.agents_md.read_text(encoding="utf-8")
        self.assertIn("Keep this hand-written note.", text)
        self.assertEqual(text.count(f"### {SECTION_ROUTING}"), 1)
        self.assertIn("pytest-fixes", text)
        self.assertIn("Tool: `", text)

        # A later task replaces the routing section instead of stacking it.
        TaskRouter(self.workspace).apply("write the changelog entry")
        text = self.agents_md.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"### {SECTION_ROUTING}"), 1)
        self.assertEqual(text.count("HARNESS:SKILL_START"), 0)
        self.assertIn("Keep this hand-written note.", text)
        self.assertNotIn("fix the failing pytest suite", text)

    def test_declared_project_tools_replace_the_builtin_catalog(self) -> None:
        tools_path = self.workspace / ".agents" / "tools.json"
        tools_path.parent.mkdir(parents=True, exist_ok=True)
        tools_path.write_text(
            json.dumps(
                {
                    "tools": [
                        {
                            "name": "project_runner",
                            "description": "Run this project's pytest matrix.",
                            "signals": ["pytest", "tests"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        selection = TaskRouter(self.workspace).select("run the pytest tests")
        self.assertEqual([tool["name"] for tool in selection.tools], ["project_runner"])
        self.assertTrue(selection.declared_tools)

    def test_unknown_paths_are_ignored(self) -> None:
        router = TaskRouter(self.workspace)
        selection = router.select("")
        self.assertEqual(selection.skills, [])
        self.assertEqual(selection.tools, [])
        self.assertIn("Project skill inventory", " ".join(selection.items()))


class TestPreInvocationRouting(ProjectRoutingTestCase):
    def test_prompt_persists_routing_and_announces_it(self) -> None:
        self.install_skill(
            "pytest-fixes",
            "Fix failing pytest test suites and missing test dependencies.",
        )
        transcript = self.make_transcript(
            "route.jsonl",
            [user("The pytest suite is failing, fix the tests")],
        )

        payload = build_pre_invocation_payload(
            conversation_id="conv-route",
            workspace=self.workspace,
            transcript_path=transcript,
            invocation_num=0,
        )
        message = payload["injectSteps"][0]["ephemeralMessage"]
        self.assertIn("Routed project context", message)

        text = self.agents_md.read_text(encoding="utf-8")
        self.assertIn("pytest-fixes", text)
        self.assertIn(f"### {SECTION_ROUTING}", text)


class TestEngineRoutingRefresh(ProjectRoutingTestCase):
    def test_engine_refreshes_routing_for_existing_project_skills(self) -> None:
        self.install_skill(
            "pytest-fixes",
            "Fix failing pytest test suites and missing test dependencies.",
        )
        engine = HarnessEngine(self.workspace)
        result = engine.run_cycle(
            conversation_id="conv-routing",
            raw_steps=[
                user("Run the tests"),
                tool("run_command", CommandLine="pytest -q"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
            ],
        )

        self.assertEqual(result.status, "processed")
        self.assertTrue(result.routing)
        text = self.agents_md.read_text(encoding="utf-8")
        self.assertIn(f"### {SECTION_ROUTING}", text)
        self.assertIn("pytest-fixes", text)

    def test_engine_does_not_create_agents_md_without_project_content(self) -> None:
        engine = HarnessEngine(self.workspace)
        result = engine.run_cycle(
            conversation_id="conv-routing-empty",
            raw_steps=[
                user("Run the tests"),
                tool("run_command", CommandLine="pytest -q"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
            ],
        )
        self.assertEqual(result.routing, {})
        self.assertFalse(self.agents_md.exists())


class TestProjectMemoryIsolation(IsolatedHarnessTestCase):
    def test_project_stores_use_separate_databases(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        for path in (first, second):
            path.mkdir()

        first_store = MemoryStore(workspace=first)
        second_store = MemoryStore(workspace=second)
        self.assertNotEqual(first_store.db_path, second_store.db_path)
        self.assertIn("projects", str(first_store.db_path))

        lesson_id = first_store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Only the first project fails",
            resolution="Apply the first project's fix.",
            workspace=first,
        )
        self.assertIsNotNone(first_store.get_lesson(lesson_id))
        self.assertIsNone(second_store.get_lesson(lesson_id))
        self.assertEqual(second_store.get_stats(workspace=second)["verified_lessons"], 0)

    def test_all_projects_search_spans_isolated_databases(self) -> None:
        project = self.root / "searchable"
        project.mkdir()
        store = MemoryStore(workspace=project)
        store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Cross project pytest quirk",
            resolution="Reinstall the pytest plugin.",
            workspace=project,
        )

        global_store = MemoryStore()
        self.assertEqual(global_store.search_lessons("pytest quirk"), [])
        results = global_store.search_lessons("pytest quirk", all_projects=True)
        self.assertEqual(len(results), 1)
        self.assertIn("Reinstall the pytest plugin.", results[0]["resolution"])

        listed = global_store.list_lessons(all_projects=True)
        self.assertTrue(any(item["id"] == results[0]["id"] for item in listed))

        stats = global_store.get_stats(all_projects=True)
        self.assertEqual(stats["candidate_lessons"], 1)
        self.assertEqual(stats["project_stores"], 1)

    def test_global_lessons_are_seeded_once_into_a_new_project(self) -> None:
        global_store = MemoryStore()
        global_id = global_store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Global environment quirk",
            resolution="Set the environment variable before running.",
        )

        project = self.root / "seeded"
        project.mkdir()
        store = MemoryStore(workspace=project)
        seeded = store.get_lesson(global_id)
        self.assertIsNotNone(seeded)
        self.assertEqual(seeded["project"], "global")

        # Seeding never imports another project's lessons.
        other = self.root / "other"
        other.mkdir()
        other_store = MemoryStore(workspace=other)
        local_id = other_store.add_lesson(
            trigger_tool="run_command",
            error_pattern="Other project only failure",
            resolution="Fix it in the other project.",
            workspace=other,
        )
        fresh = self.root / "fresh"
        fresh.mkdir()
        fresh_store = MemoryStore(workspace=fresh)
        self.assertIsNone(fresh_store.get_lesson(local_id))
        self.assertIsNotNone(fresh_store.get_lesson(global_id))


if __name__ == "__main__":
    unittest.main()
