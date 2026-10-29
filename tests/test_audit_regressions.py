"""Regressions from the October 2026 performance audit of real sessions.

Each test pins one failure observed in the "video editor" and "instagram dm's"
workspaces: learning that silently stopped, credentials written into project
files, "fixes" that were not fixes, and routing noise.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from harness.core.agents_md_manager import (
    HARNESS_FOOTER,
    HARNESS_HEADER,
    AgentsMdManager,
    _is_standing_directive,
)
from harness.core.analyzer import TranscriptAnalyzer
from harness.core.engine import EngineResult, HarnessEngine
from harness.core.memory_store import RETIRE_AFTER_SURFACINGS, is_retired_skill
from harness.core.recovery import INLINE_QUOTING_RULE, INLINE_QUOTING_SIGNATURE
from harness.core.redaction import redact, redact_strict
from harness.core.router import TaskRouter, _safe_task_text
from harness.core.skill_builder import SkillBuilder
from harness.core.util import foreign_windows_paths, path_root_key, text_mentions_root
from support import IsolatedHarnessTestCase, make_event, make_workflow

FAKE_HEX_KEY = "0123456789abcdef0123456789abcdef"
FAKE_SK_KEY = "sk_" + "f00dfacecafe1234" * 3


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


def tool(name: str, **arguments: object) -> dict:
    return {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": name, "arguments": arguments}]}


def output(content: str, status: str = "DONE") -> dict:
    return {"type": "CORTEX_STEP_TYPE_OUTPUT", "content": content, "status": status}


def managed_block(*sections: tuple[str, list[str]]) -> str:
    lines = [HARNESS_HEADER, "## Harness Evolved Directives (Workspace)", ""]
    for title, items in sections:
        lines.append(f"### {title}")
        lines.extend(f"- {item}" for item in items)
        lines.append("")
    lines.append(HARNESS_FOOTER)
    return "\n".join(lines) + "\n"


class TestWorkspaceWithSpaces(IsolatedHarnessTestCase):
    """The Stop hook skipped every session in a folder whose name has a space."""

    def setUp(self) -> None:
        super().setUp()
        self.space_ws = self.make_dir("video editor")

    def _steps(self, inside: str) -> list[dict]:
        return [
            user(f"render the reel in {inside}"),
            tool("run_command", CommandLine=f'python -X utf8 "{inside}"'),
            output("ModuleNotFoundError: No module named 'mediapipe'", "ERROR"),
            tool("run_command", CommandLine="pip install mediapipe"),
            output("Successfully installed mediapipe"),
            tool("run_command", CommandLine=f'python -X utf8 "{inside}"'),
            # Other absolute paths are always present in real transcripts.
            output(r"Loaded C:\Users\Someone\.gemini\antigravity\mcp\blender\instructions.md"),
        ]

    def test_session_in_spaced_workspace_is_processed(self) -> None:
        script = str(self.space_ws / "work" / "edit" / "render.py")
        result = HarnessEngine(self.space_ws).run_cycle(
            conversation_id="conv-space", raw_steps=self._steps(script)
        )
        self.assertEqual(result.status, "processed", result.summary)
        self.assertEqual(result.conversation_id, "conv-space")

    def test_root_with_space_is_matched_at_the_path_position(self) -> None:
        root = path_root_key(self.space_ws)
        inside = str(self.space_ws / "scratch" / "x.py")
        self.assertTrue(text_mentions_root(f'run "{inside}" now', root))
        sibling = str(self.space_ws) + "-old" + os.sep + "x.py"
        self.assertFalse(text_mentions_root(sibling, root))

    @unittest.skipUnless(os.name == "nt", "Windows drive paths")
    def test_json_escapes_are_not_drive_letters(self) -> None:
        root = path_root_key(self.space_ws)
        self.assertEqual(foreign_windows_paths("task:\\n- step", root), [])
        self.assertEqual(foreign_windows_paths("task:\n- step", root), [])

    def test_skipped_cycle_keeps_conversation_id(self) -> None:
        foreign = r"C:\Users\Someone\Desktop\other project\build.py"
        result = HarnessEngine(self.space_ws).run_cycle(
            conversation_id="conv-foreign-space",
            raw_steps=[
                user(f"build {foreign}"),
                tool("run_command", CommandLine=f'python "{foreign}"'),
                output("ok"),
            ],
        )
        if os.name == "nt":
            self.assertEqual(result.status, "skipped")
        self.assertEqual(result.conversation_id, "conv-foreign-space")

    def test_lessons_citing_own_spaced_paths_are_kept(self) -> None:
        engine = HarnessEngine(self.space_ws)
        own = str(self.space_ws / "work" / "tools" / "sketchfab_blender.py")
        event = make_event(
            trigger_tool="edit_file",
            error_message="path is not absolute",
            resolution_summary=f"`work\\tools\\sketchfab_blender.py` failed. Verified fix: use `{own}`.",
            category="convention",
        )
        event.failed_args = {"TargetFile": "work\\tools\\sketchfab_blender.py"}
        event.successful_args = {"TargetFile": own}
        kept = engine._drop_foreign_path_events([event], self.space_ws, EngineResult())
        self.assertEqual(len(kept), 1)

    @unittest.skipUnless(os.name == "nt", "Windows tool install paths")
    def test_tool_install_paths_are_not_foreign_projects(self) -> None:
        engine = HarnessEngine(self.space_ws)
        event = make_event(
            resolution_summary=(
                r"`C:\Program Files\Blender\blender.exe -b` timed out; retrying succeeded, "
                "which points to a transient or environment-dependent failure."
            ),
        )
        event.failed_args = {"CommandLine": r'"C:\Program Files\Blender\blender.exe" -b'}
        kept = engine._drop_foreign_path_events([event], self.space_ws, EngineResult())
        self.assertEqual(len(kept), 1)


class TestRedactionGaps(unittest.TestCase):
    def test_env_setters_and_flags_are_redacted(self) -> None:
        for text in (
            f'[System.Environment]::SetEnvironmentVariable("SKETCHFAB_API_KEY", "{FAKE_HEX_KEY}", "User")',
            f"setx OPENAI_API_KEY {FAKE_HEX_KEY}",
            f"python tool.py --key {FAKE_HEX_KEY}",
            f'os.environ.setdefault("SERVICE_TOKEN", "{FAKE_HEX_KEY}")',
        ):
            self.assertNotIn(FAKE_HEX_KEY, redact(text), text)

    def test_underscore_sk_keys_are_redacted(self) -> None:
        self.assertNotIn(FAKE_SK_KEY, redact(f"{FAKE_SK_KEY} install elevenlabs mcp"))

    def test_strict_redaction_catches_bare_keys_but_not_slugs(self) -> None:
        self.assertNotIn(FAKE_HEX_KEY, redact_strict(f"api key is {FAKE_HEX_KEY}"))
        prose = "Use edit-video-sketchfab-blender and run_python_task_mirror_whole"
        self.assertEqual(redact_strict(prose), prose)
        self.assertEqual(redact_strict("pass the --key flag"), "pass the --key flag")


class TestRecoveryPrecision(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = TranscriptAnalyzer()

    def test_other_script_after_runtime_failure_is_not_a_fix(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Download the premium fonts"),
                tool("run_command", CommandLine="python -X utf8 scratch/download_premium_fonts.py"),
                output("Failed to download Fraunces.ttf: HTTP Error 404: Not Found", "ERROR"),
                tool("run_command", CommandLine="python -X utf8 scratch/download_more_fonts.py"),
                output("Downloaded 4 fonts"),
            ],
            conversation_id="conv-fonts",
        )
        self.assertEqual(analysis.verified_events, [])

    def test_status_summary_is_not_an_error(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Validate the composition"),
                tool("run_command", CommandLine='cd "hyper_project"; npx hyperframes check --json'),
                output("[hyperframes] media proxy pre-resolve: 1/1 ready, 0 failed (16ms)"),
                tool("run_command", CommandLine='cd "hyper_project"; npx hyperframes render -o out.mp4'),
                output("Rendered out.mp4"),
            ],
            conversation_id="conv-check",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.unresolved_events, [])

    def test_inline_quoting_failures_collapse_into_one_class_lesson(self) -> None:
        steps = [user("Inspect the timing and the crops")]
        for index in range(3):
            steps += [
                tool("run_command", CommandLine=f'python -c " import json\n print({index})"'),
                output("+ FullyQualifiedErrorId : UnexpectedToken", "ERROR"),
                tool("run_command", CommandLine=f"python -X utf8 scratch/inspect_{index}.py"),
                output("ok"),
            ]
        analysis = self.analyzer.analyze(steps, conversation_id="conv-inline")
        verified = analysis.verified_events
        self.assertEqual(len(verified), 3)
        self.assertEqual({event.error_signature for event in verified}, {INLINE_QUOTING_SIGNATURE})
        self.assertEqual({event.resolution_summary for event in verified}, {INLINE_QUOTING_RULE})

    def test_rerun_after_an_edit_is_not_a_transient_lesson(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Fix the contrast findings"),
                tool("run_command", CommandLine="python -X utf8 scratch/check_findings.py"),
                output("ERROR: contrast_aa_failure #hud-section Contrast is 1.14:1", "ERROR"),
                tool("edit_file", TargetFile="hyper_project/index.html"),
                output("edited"),
                tool("run_command", CommandLine="python -X utf8 scratch/check_findings.py"),
                output("0 findings"),
            ],
            conversation_id="conv-edit",
        )
        self.assertEqual(analysis.verified_events, [])

    def test_inline_one_liner_is_never_the_recommended_fix(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("List the Arabic voices"),
                tool("run_command", CommandLine="python -X utf8 scratch/find_ar_voices.py"),
                output("Traceback (most recent call last):\nUnicodeEncodeError: 'charmap'", "ERROR"),
                tool("run_command", CommandLine='python -X utf8 -c "import edge_tts; print(1)"'),
                output("ar-MA-JamalNeural Male"),
            ],
            conversation_id="conv-voices",
        )
        self.assertEqual(analysis.verified_events, [])

    def test_usage_error_with_other_subcommand_is_still_a_fix(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Read my Canva profile"),
                tool("run_command", CommandLine="npx -y @canva/cli api users me profile"),
                output("Unknown argument: profile", "ERROR"),
                tool("run_command", CommandLine="npx -y @canva/cli api users me"),
                output('{"id": "u1"}'),
            ],
            conversation_id="conv-canva",
        )
        self.assertEqual(len(analysis.verified_events), 1)


class TestRuleCleanup(IsolatedHarnessTestCase):
    LEGACY_RULES = [
        "**run_command**: `python -c \" import json meta = json.load(open('a.json'))…` failed with "
        "`+ FullyQualifiedErrorId : UnexpectedToken`. Verified fix: use `python -X utf8 work\\edit\\inspect_timing.py`.",
        "**run_command**: `python -c \" import urllib.request, re req = 1…` failed with "
        "`+ FullyQualifiedErrorId : UnexpectedToken`. Verified fix: use `python -X utf8 scratch/get_links.py`.",
        "**run_command**: `cd \"hyper_project\"; npx hyperframes check --json` failed with "
        "`[hyperframes] media proxy pre-resolve: 1/1 ready, 0 failed (16ms)`. Verified fix: use "
        "`cd \"hyper_project\"; npx hyperframes render -o \"../exports/a.mp4\"`.",
        "**run_command**: `python -X utf8 scratch/check_findings.py` failed with "
        "`ERROR: contrast_aa_failure #hud-section Contrast is 1.14:1.`; retrying succeeded, which "
        "points to a transient or environment-dependent failure.",
        "**run_command**: `npx -y @canva/cli api users me profile` failed with `Unknown argument: profile`. "
        "Verified fix: use `npx -y @canva/cli api users me`.",
    ]
    LEGACY_QUIRKS = [
        "Environment: `python scratch/download_fontshare.py` failed with `Failed Satoshi-Black.woff2: "
        "HTTP Error 404: Not Found`. Verified fix: use `python scratch/render_font_samples.py`.",
        "**run_command**: Invoking `python scratch/download_clash_otf.py` after the dependency failure "
        "resolved `ModuleNotFoundError: No module named 'fontTools'`.",
        "**run_command**: Installing `pip install mediapipe` resolved `ModuleNotFoundError: No module named 'mediapipe'`.",
    ]
    LEGACY_DIRECTIVES = [
        "Never re render it from scratch",
        "Never make local edits to it because right now you made it sound noisy",
        "Always use Epidemic MCP to generate the voiceover for videos",
    ]

    def _write_agents(self, user_text: str = "# Notes\n") -> Path:
        path = self.workspace / "AGENTS.md"
        path.write_text(
            user_text
            + "\n"
            + managed_block(
                ("Standing Project Directives", self.LEGACY_DIRECTIVES),
                ("Environment & Tooling Quirks", self.LEGACY_QUIRKS),
                ("Learned Pitfalls & Recovery Invariants", self.LEGACY_RULES),
            ),
            encoding="utf-8",
        )
        return path

    def test_repair_removes_unverified_fixes_and_merges_the_quoting_class(self) -> None:
        path = self._write_agents()
        AgentsMdManager(self.workspace).repair(target="workspace")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(INLINE_QUOTING_RULE), 1)
        self.assertNotIn("hyperframes render", text)
        self.assertNotIn("render_font_samples", text)
        self.assertNotIn("download_clash_otf", text)
        self.assertNotIn("check_findings", text)
        self.assertNotIn("re render it", text)
        self.assertNotIn("right now", text)
        self.assertIn("api users me`", text)
        self.assertIn("pip install mediapipe", text)
        self.assertIn("Epidemic MCP", text)

    def test_next_update_cleans_the_block_without_a_manual_repair(self) -> None:
        path = self._write_agents()
        AgentsMdManager(self.workspace).update_rules(
            contrastive_events=[make_event()], user_constraints=[], environment_quirks=[]
        )
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("hyperframes render", text)
        self.assertEqual(text.count(INLINE_QUOTING_RULE), 1)

    def test_class_rule_is_skipped_when_the_user_already_wrote_it(self) -> None:
        path = self._write_agents(
            "# Notes\n\nNo inline `python -c` with nested quotes in PowerShell.\n"
        )
        AgentsMdManager(self.workspace).repair(target="workspace")
        self.assertNotIn(INLINE_QUOTING_RULE, path.read_text(encoding="utf-8"))

    def test_privacy_scrub_reports_the_rules_it_removed(self) -> None:
        from argparse import Namespace

        from harness.cli import _privacy_scrub

        self._write_agents()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _privacy_scrub(self.workspace, dry_run=True, as_json=True)
        report = json.loads(buffer.getvalue())
        self.assertTrue(report["rules"]["workspace"]["updated"])
        self.assertGreater(report["rules"]["workspace"]["removed"], 0)
        del Namespace

    def test_one_off_directives_are_rejected(self) -> None:
        self.assertFalse(_is_standing_directive("Never re render it from scratch"))
        self.assertTrue(_is_standing_directive("Always use Epidemic MCP to generate the voiceover for videos"))


class TestArtifactsStayClean(IsolatedHarnessTestCase):
    def test_generated_skill_never_contains_a_credential(self) -> None:
        workflow = make_workflow(
            name="sketchfab-model-import",
            goal="Import Sketchfab models into Blender",
            steps=[
                f'Run `[System.Environment]::SetEnvironmentVariable("SKETCHFAB_API_KEY", "{FAKE_HEX_KEY}", "User")`',
                "Run `python -X utf8 work/tools/sketchfab_blender.py robot`",
                f"Run `python fetch.py {FAKE_HEX_KEY}`",
            ],
        )
        result = SkillBuilder(self.workspace).build_skill(workflow)
        text = result.path.read_text(encoding="utf-8")
        self.assertNotIn(FAKE_HEX_KEY, text)
        self.assertNotIn("Repeatable procedure for:", text)

    def test_scrub_redacts_an_already_leaked_managed_skill(self) -> None:
        builder = SkillBuilder(self.workspace)
        result = builder.build_skill(make_workflow(name="sketchfab-model-import"))
        leaked = result.path.read_text(encoding="utf-8").replace(
            "Run `pytest -q`", f"Run `setx SKETCHFAB_API_KEY {FAKE_HEX_KEY}`"
        )
        result.path.write_text(leaked, encoding="utf-8")
        changed = builder.scrub_managed_skills(self.workspace)
        self.assertEqual(changed, [str(result.path)])
        self.assertNotIn(FAKE_HEX_KEY, result.path.read_text(encoding="utf-8"))

    def test_procedure_drops_probes_and_inline_one_liners(self) -> None:
        analyzer = TranscriptAnalyzer()
        analysis = analyzer.analyze(
            [
                user("Import robot models from Sketchfab into Blender"),
                tool("run_command", CommandLine='Get-Process -Name "*blender*" -ErrorAction SilentlyContinue'),
                output("blender 1234"),
                tool("run_command", CommandLine='Test-Path "C:\\Blender\\blender.exe"'),
                output("True"),
                tool("write_file", TargetFile="work/tools/sketchfab_blender.py"),
                output("written"),
                tool("run_command", CommandLine='python -X utf8 work/tools/sketchfab_blender.py "robot"'),
                output("3 models"),
                tool("run_command", CommandLine='python -X utf8 -c "import sys; print(1)"'),
                output("1"),
                tool("run_command", CommandLine="python -X utf8 work/tools/sketchfab_blender.py --download abc"),
                output("downloaded"),
            ],
            conversation_id="conv-skill",
        )
        self.assertTrue(analysis.workflows)
        workflow = analysis.workflows[0]
        joined = " ".join(workflow.steps)
        self.assertNotIn("Get-Process", joined)
        self.assertNotIn("Test-Path", joined)
        self.assertNotIn("-c \"import", joined)
        self.assertIn("sketchfab", workflow.name)

    def test_routing_task_line_has_no_secret_or_path(self) -> None:
        task = _safe_task_text(
            f"{FAKE_SK_KEY} C:\\Users\\Someone\\.kiro\\settings\\mcp.json properly install elevenlabs mcp"
        )
        self.assertNotIn("sk_", task)
        self.assertNotIn("mcp.json", task)
        self.assertIn("elevenlabs", task)


class TestRouting(IsolatedHarnessTestCase):
    def _skill(self, name: str, description: str) -> None:
        folder = self.workspace / ".agents" / "skills" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
            encoding="utf-8",
        )

    def test_unrelated_skills_are_not_routed(self) -> None:
        self._skill("sketchfab-model-import", "Import Sketchfab models into Blender for a video.")
        self._skill("gdrive-upload", "Upload exported files to Google Drive.")
        selection = TaskRouter(self.workspace).select("upload both exports to google drive")
        names = [skill["name"] for skill in selection.skills]
        self.assertEqual(names, ["gdrive-upload"])

    def test_run_extension_needs_an_explicit_extension(self) -> None:
        selection = TaskRouter(self.workspace).select("audit the harness plugin performance in the editor")
        self.assertNotIn("run_extension", [tool["name"] for tool in selection.tools])

    def test_unused_generated_skill_is_retired(self) -> None:
        skill = {"source": "harness-auto", "reused_count": 0, "surfaced_count": RETIRE_AFTER_SURFACINGS}
        self.assertTrue(is_retired_skill(skill))
        self.assertFalse(is_retired_skill({**skill, "reused_count": 1}))
        self.assertFalse(is_retired_skill({**skill, "source": "manual"}))


class TestHookFastPath(unittest.TestCase):
    def setUp(self) -> None:
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        sys.path.insert(0, str(scripts))
        self.addCleanup(sys.path.remove, str(scripts))
        import hook_runner  # noqa: E402

        self.runner = hook_runner

    def test_success_payloads_skip_the_full_handler(self) -> None:
        self.assertFalse(self.runner._reports_failure({"toolCall": {"name": "run_command"}}))
        for payload in (
            {"error": "boom"},
            {"toolCall": {"name": "x", "status": "failed"}},
            {"toolResult": {"errorMessage": "denied"}},
            {"success": False},
        ):
            self.assertTrue(self.runner._reports_failure(payload), payload)

    def test_fast_path_answers_with_empty_json(self) -> None:
        previous = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"toolCall": {"name": "read_file"}}))
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                handled = self.runner._fast_post_tool_use()
        finally:
            sys.stdin = previous
        self.assertTrue(handled)
        self.assertEqual(buffer.getvalue(), "{}")


class TestDoctorLearningHealth(IsolatedHarnessTestCase):
    def test_all_cycles_skipped_by_mismatch_is_reported(self) -> None:
        from harness.cli import _learning_health
        from harness.core.config import get_event_journal_path

        journal = get_event_journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("w", encoding="utf-8") as handle:
            for index in range(4):
                handle.write(
                    json.dumps(
                        {
                            "timestamp": f"2026-10-28T0{index}:00:00+00:00",
                            "event": "Stop:result",
                            "payload": {
                                "status": "skipped",
                                "warnings": ["transcript/workspace mismatch: no lessons written for 'x'"],
                            },
                        }
                    )
                    + "\n"
                )
        ok, detail = _learning_health()
        self.assertFalse(ok)
        self.assertIn("workspace mismatch", detail)


if __name__ == "__main__":
    unittest.main()
