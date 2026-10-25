"""Tests for the user-facing CLI."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

try:
    from .support import IsolatedHarnessTestCase, make_event
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event

from harness import __version__
from harness.cli import main
from harness.core.agents_md_manager import (
    HARNESS_FOOTER,
    HARNESS_HEADER,
    SECTION_DIRECTIVES,
    AgentsMdManager,
)
from harness.core.analyzer import SessionAnalysis
from harness.core.config import get_experience_md_path, get_plugin_rules_path
from harness.core.memory_store import MemoryStore


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


def tool(name: str, **arguments: object) -> dict:
    return {
        "type": "PLANNER_RESPONSE",
        "tool_calls": [{"name": name, "arguments": arguments}],
    }


def output(content: str, status: str = "DONE") -> dict:
    return {"type": "CORTEX_STEP_TYPE_OUTPUT", "content": content, "status": status}


class TestCLI(IsolatedHarnessTestCase):
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

    def test_status_json(self) -> None:
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["version"], __version__)
        self.assertEqual(data["plugin_dir"], str(self.plugin_dir))
        self.assertEqual(data["skills"], [])
        self.assertIn("stats", data)

    def test_reflect_json_and_skip(self) -> None:
        transcript = self.make_transcript(
            "cli-reflect.jsonl",
            [
                user("Install pytest and run the test suite"),
                tool("run_command", CommandLine="pytest -q"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
                tool("run_command", CommandLine="pip install pytest"),
                output("Successfully installed pytest"),
                tool("run_command", CommandLine="pytest -q"),
                output("5 passed"),
            ],
        )
        code, out, err = self.run_cli(
            "reflect",
            "--session", "conv-cli",
            "--transcript", str(transcript),
            "--workspace", str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "processed")
        self.assertEqual(result["skills_synthesized"], ["run-python-tests"])
        self.assertTrue((self.workspace / "AGENTS.md").exists())

        code, out, err = self.run_cli(
            "reflect",
            "--session", "conv-cli",
            "--transcript", str(transcript),
            "--workspace", str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "skipped")

    def test_memory_search_without_workspace(self) -> None:
        store = MemoryStore(workspace=self.workspace)
        store.record_session(
            SessionAnalysis(
                conversation_id="conv-cli-memory",
                workspace=str(self.workspace),
                tasks=["Run tests"],
                contrastive_events=[make_event()],
            )
        )
        code, out, err = self.run_cli("memory", "search", "pytest", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), [])

        code, out, err = self.run_cli(
            "memory", "search", "pytest", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 0, err)
        results = json.loads(out)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["trigger_tool"], "run_command")

        code, out, err = self.run_cli(
            "memory", "search", "pytest", "--all-projects", "--json"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)), 1)

    def test_skills_install_list_show_and_remove(self) -> None:
        source = self.make_skill_source("cli-skill")
        code, out, err = self.run_cli(
            "skills", "install", str(source),
            "--scope", "workspace",
            "--workspace", str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        installed = json.loads(out)
        self.assertIsInstance(installed, list)
        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0]["status"], "installed")
        self.assertEqual(installed[0]["name"], "cli-skill")

        code, out, err = self.run_cli(
            "skills", "list", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 0, err)
        skills = json.loads(out)
        self.assertEqual([skill["name"] for skill in skills], ["cli-skill"])

        code, out, err = self.run_cli(
            "skills", "show", "cli-skill", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("name: cli-skill", out)

        code, out, err = self.run_cli(
            "skills", "remove", "cli-skill",
            "--workspace", str(self.workspace),
            "--yes",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("[REMOVED]", out)
        self.assertFalse(
            (self.workspace / ".agents" / "skills" / "cli-skill").exists()
        )
        store = MemoryStore(workspace=self.workspace)
        self.assertEqual(store.get_all_skills(workspace=self.workspace), [])

    def test_rules_sanitize_json(self) -> None:
        self.write_text(
            self.workspace / "AGENTS.md",
            "# User\n\nKeep me.\n\n"
            "<!-- HARNESS:AUTONOMOUS_RULES_START -->\n"
            "### Learned Pitfalls & Recovery Invariants\n"
            "- Completed at: 2026-10-23T10:00:00Z\n"
            "- Run `pytest` before merging changes.\n"
            "<!-- HARNESS:AUTONOMOUS_RULES_END -->\n",
        )
        self.write_text(
            get_plugin_rules_path(),
            "<!-- HARNESS:AUTONOMOUS_RULES_START -->\n"
            "### Environment & Tooling Quirks\n"
            "- Command exited with code 1\n"
            "- Capture stderr when a command exits with a non-zero status.\n"
            "<!-- HARNESS:AUTONOMOUS_RULES_END -->\n",
        )
        code, out, err = self.run_cli(
            "rules", "sanitize", "--target", "both",
            "--workspace", str(self.workspace), "--json",
        )
        self.assertEqual(code, 0, err)
        results = json.loads(out)
        self.assertTrue(results["workspace"]["updated"])
        workspace_text = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Keep me.", workspace_text)
        self.assertNotIn("Completed at", workspace_text)
        self.assertEqual(workspace_text.count("Run `pytest`"), 1)
        plugin_text = get_plugin_rules_path().read_text(encoding="utf-8")
        self.assertNotIn("Command exited", plugin_text)
        self.assertIn("Capture stderr", plugin_text)

    def test_skills_validate_reports_invalid(self) -> None:
        bad = self.workspace / ".agents" / "skills" / "bad-skill" / "SKILL.md"
        self.write_text(bad, "# Missing frontmatter\n")
        code, out, err = self.run_cli(
            "skills", "validate", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(len(report["invalid"]), 1)

    def test_doctor_json(self) -> None:
        self.make_minimal_plugin()
        code, out, err = self.run_cli(
            "doctor", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertFalse(report["critical"])
        checks = {check["check"]: check for check in report["checks"]}
        self.assertTrue(checks["plugin.json"]["ok"])
        self.assertTrue(checks["hooks.json"]["ok"])
        self.assertTrue(checks["hook events"]["ok"])
        self.assertTrue(checks["hook runner"]["ok"])
        self.assertTrue(checks["memory db"]["ok"])

    def test_doctor_reports_success_when_all_checks_pass(self) -> None:
        self.make_minimal_plugin()
        code, out, err = self.run_cli(
            "doctor", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("All checks passed.", out)
        self.assertNotIn("Review the checks marked FAIL", out)

    def test_doctor_does_not_create_runtime_files(self) -> None:
        self.make_minimal_plugin()
        code, out, err = self.run_cli(
            "doctor", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 0, err)
        self.assertFalse((self.plugin_dir / "memory").exists())
        self.assertFalse((self.plugin_dir / "state").exists())
        self.assertFalse((self.plugin_dir / "rules").exists())
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertFalse((self.workspace / ".agents").exists())

    def test_sync_regenerates_experience_library(self) -> None:
        code, out, err = self.run_cli("sync")
        self.assertEqual(code, 0, err)
        self.assertTrue(get_experience_md_path().exists())
        self.assertIn("Synchronized", out)

    def test_status_effectiveness_json_and_text(self) -> None:
        store = MemoryStore(workspace=self.workspace)
        lesson_id = store.add_lesson(
            trigger_tool="run_command",
            error_pattern="pytest effectiveness signal",
            resolution="Use the effectiveness fix.",
            category="command",
            verified=True,
            workspace=self.workspace,
            confidence=0.9,
        )
        store.record_lessons_surfaced([lesson_id])
        store.record_skill_saved(
            name="effectiveness-skill",
            description="A skill used by the effectiveness status test",
            file_path=str(
                self.workspace / ".agents" / "skills"
                / "effectiveness-skill" / "SKILL.md"
            ),
            workspace=self.workspace,
            quality=0.8,
        )
        store.record_skill_reuse(
            "effectiveness-skill", workspace=self.workspace
        )
        store.record_skills_surfaced(["effectiveness-skill"])

        code, out, err = self.run_cli(
            "status", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 0, err)
        default_payload = json.loads(out)
        self.assertNotIn("effectiveness", default_payload)

        code, out, err = self.run_cli(
            "status", "--workspace", str(self.workspace),
            "--effectiveness", "--json",
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertIn("effectiveness", payload)
        lessons = payload["effectiveness"]["lessons"]
        skills = payload["effectiveness"]["skills"]
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["id"], lesson_id)
        self.assertEqual(lessons[0]["surfaced_count"], 1)
        self.assertEqual(lessons[0]["post_surface_occurrences"], 0)
        self.assertIn("utility", lessons[0])
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "effectiveness-skill")
        self.assertEqual(skills[0]["reused_count"], 1)
        self.assertEqual(skills[0]["surfaced_count"], 1)
        self.assertIn("utility", skills[0])

        code, out, err = self.run_cli(
            "status", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, 0, err)
        self.assertNotIn("Lesson effectiveness", out)

        code, out, err = self.run_cli(
            "status", "--workspace", str(self.workspace), "--effectiveness"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Lesson effectiveness", out)
        self.assertIn("Skill effectiveness", out)
        self.assertIn("surfaced 1", out)
        self.assertIn("recurred 0", out)
        self.assertIn("effectiveness-skill", out)

        code, out, err = self.run_cli(
            "memory", "search", "pytest",
            "--workspace", str(self.workspace), "--json",
        )
        self.assertEqual(code, 0, err)
        searched = json.loads(out)
        self.assertEqual(searched[0]["surfaced_count"], 1)
        self.assertIn("post_surface_occurrences", searched[0])
        self.assertIn("utility", searched[0])

    def test_memory_export_and_import(self) -> None:
        store = MemoryStore(workspace=self.workspace)
        store.record_session(
            SessionAnalysis(
                conversation_id="conv-export-cli",
                workspace=str(self.workspace),
                tasks=["Task"],
                contrastive_events=[make_event()],
            )
        )
        export_path = self.root / "cli-export.json"
        code, out, err = self.run_cli(
            "memory", "export", "--path", str(export_path),
            "--workspace", str(self.workspace),
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(export_path.exists())

        code, out, err = self.run_cli(
            "memory", "import", "--path", str(export_path),
            "--workspace", str(self.workspace),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Imported", out)

    def test_rules_why_and_forget_use_provenance(self) -> None:
        rule = "Always use pnpm instead of npm for dependency installs."
        manager = AgentsMdManager(self.workspace)
        store = MemoryStore(workspace=self.workspace)

        def record(rules: list[dict]) -> None:
            store.record_rule_application(
                rules,
                rules[0]["target_file"] if rules else str(manager.workspace_agents_md),
                "workspace",
                conversation_id="conv-cli-rules",
                evidence="verified pnpm recovery",
            )

        manager.update_rules(
            [],
            [rule, "Always keep deployment notes current before releases."],
            [],
            target="workspace",
            on_apply=record,
        )

        code, out, err = self.run_cli(
            "rules", "why", rule,
            "--workspace", str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["found"])
        self.assertEqual(len(payload["matches"]), 1)
        match = payload["matches"][0]
        self.assertTrue(match["in_file"])
        self.assertEqual(match["section"], "directive")
        self.assertEqual(match["provenance"]["occurrences"], 1)
        self.assertEqual(
            match["provenance"]["conversation_id"], "conv-cli-rules"
        )

        code, out, err = self.run_cli(
            "rules", "forget", rule,
            "--workspace", str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        removed = json.loads(out)
        self.assertTrue(removed["removed"])
        self.assertTrue(removed["db_removed"])
        self.assertEqual(removed["targets"]["workspace"]["removed"], 1)
        content = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn(rule, content)
        self.assertIn("deployment notes", content)
        self.assertIsNone(store.get_rule(rule))

    def test_rules_why_reports_dry_run_forget_and_conflicts(self) -> None:
        self.write_text(
            self.workspace / "AGENTS.md",
            f"{HARNESS_HEADER}\n"
            "## Harness Evolved Directives (Workspace)\n"
            f"### {SECTION_DIRECTIVES}\n"
            "- Always use pnpm instead of npm for installing dependencies.\n"
            "- Always use npm for installing dependencies.\n"
            "- Avoid pnpm for installing dependencies.\n"
            f"{HARNESS_FOOTER}\n",
        )
        code, out, err = self.run_cli(
            "rules", "conflicts",
            "--workspace", str(self.workspace),
            "--json",
        )
        self.assertEqual(code, 0, err)
        conflicts = json.loads(out)["workspace"]
        self.assertTrue(conflicts)
        reasons = " ".join(conflict["reason"] for conflict in conflicts)
        self.assertIn("mutually exclusive tools", reasons)
        self.assertIn("opposing polarity", reasons)

        code, out, err = self.run_cli(
            "rules", "forget", "Always use pnpm",
            "--workspace", str(self.workspace),
            "--dry-run",
            "--json",
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["removed"])
        self.assertFalse(payload["db_removed"])
        content = (self.workspace / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Always use pnpm", content)

    def test_memory_reset_requires_yes_and_returns_valid_json(self) -> None:
        store = MemoryStore(workspace=self.workspace)
        store.record_session(
            SessionAnalysis(
                conversation_id="conv-reset-cli",
                workspace=str(self.workspace),
                tasks=["Task"],
                contrastive_events=[make_event()],
            )
        )
        code, out, err = self.run_cli(
            "memory", "reset", "--workspace", str(self.workspace), "--json"
        )
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(store.get_stats(workspace=self.workspace)["sessions"], 1)

        code, out, err = self.run_cli(
            "memory", "reset", "--workspace", str(self.workspace), "--yes", "--json"
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["reset"])
        self.assertEqual(payload["counts"]["sessions"], 1)
        self.assertEqual(store.get_stats(workspace=self.workspace)["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
