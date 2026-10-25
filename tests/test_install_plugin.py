"""Tests for the non-destructive deployment script."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

import install_plugin
from harness import __version__


class TestInstaller(IsolatedHarnessTestCase):
    def deploy(self, target: Path, **kwargs: object) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = install_plugin.install(target, **kwargs)
        return code, output.getvalue()

    def test_fresh_install_deploys_and_smoke_tests(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.assertIn("SUCCESS", output)
        self.assertTrue((target / "plugin.json").exists())
        self.assertTrue((target / "hooks.json").exists())
        self.assertTrue((target / "scripts" / "hook_runner.py").exists())
        self.assertTrue((target / "scripts" / "harness_cli.py").exists())
        self.assertTrue((target / "bin" / "harness.cmd").exists())
        self.assertTrue((target / "rules" / "AGENTS.md").exists())
        self.assertTrue((target / "skills" / "harness-control" / "SKILL.md").exists())
        self.assertTrue((target / "harness" / "core" / "engine.py").exists())
        self.assertTrue((target / "harness" / "core" / "settings.py").exists())
        manifest = json.loads((target / "plugin.json").read_text(encoding="utf-8"))
        self.assertLessEqual(set(manifest), install_plugin.PLUGIN_MANIFEST_FIELDS)
        self.assertEqual(manifest["name"], "harness")
        self.assertEqual(manifest["version"], __version__)
        self.assertIn(__version__, output)

    def test_manifest_matches_the_official_antigravity_schema(self) -> None:
        manifest = json.loads(
            (install_plugin.ROOT / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertLessEqual(set(manifest), install_plugin.PLUGIN_MANIFEST_FIELDS)
        self.assertEqual(manifest["name"], "harness")
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(
            manifest["$schema"], "https://antigravity.google/schemas/v1/plugin.json"
        )

    def test_upgrade_preserves_data_and_reset_removes_generated_data(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)

        memory_sentinel = target / "memory" / "sentinel.txt"
        state_sentinel = target / "state" / "sentinel.txt"
        generated_skill = target / "skills" / "junk-skill" / "SKILL.md"
        user_skill = target / "skills" / "team-conventions" / "SKILL.md"
        rules = target / "rules" / "AGENTS.md"
        self.write_text(memory_sentinel, "keep memory\n")
        self.write_text(state_sentinel, "keep state\n")
        self.write_text(
            generated_skill,
            "---\nname: junk-skill\ndescription: Generated noise from v1.\n---\n\n"
            "<!-- HARNESS:SKILL_START -->\n# junk\n<!-- HARNESS:SKILL_END -->\n",
        )
        self.write_text(
            user_skill,
            "---\nname: team-conventions\ndescription: Hand-authored team rules.\n---\n\n"
            "# Team conventions\n",
        )
        self.write_text(rules, "# User Global Rules\n\n- Keep this user rule.\n")

        connection = sqlite3.connect(str(target / "memory" / "harness_memory.db"))
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('keep')")
            connection.commit()
        finally:
            connection.close()

        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.assertEqual(memory_sentinel.read_text(encoding="utf-8"), "keep memory\n")
        self.assertEqual(state_sentinel.read_text(encoding="utf-8"), "keep state\n")
        self.assertTrue(generated_skill.exists())
        self.assertTrue(user_skill.exists())
        self.assertIn("Keep this user rule.", rules.read_text(encoding="utf-8"))

        backups = sorted((target.parent / ".harness-backups").iterdir())
        self.assertTrue(backups)
        latest = backups[-1]
        self.assertTrue((latest / "memory" / "harness_memory.db").exists())
        self.assertTrue((latest / "memory" / "sentinel.txt").exists())
        backup_db = sqlite3.connect(str(latest / "memory" / "harness_memory.db"))
        try:
            self.assertEqual(
                backup_db.execute("SELECT value FROM marker").fetchone()[0], "keep"
            )
        finally:
            backup_db.close()
        self.assertFalse((latest / "memory" / "harness_memory.db-wal").exists())

        code, output = self.deploy(target, reset=True)
        self.assertEqual(code, 0, output)
        self.assertFalse(memory_sentinel.exists())
        self.assertFalse(state_sentinel.exists())
        self.assertFalse(generated_skill.exists())
        # A manually authored skill survives --reset.
        self.assertTrue(user_skill.exists())
        self.assertTrue((target / "skills" / "harness-control" / "SKILL.md").exists())
        reset_text = rules.read_text(encoding="utf-8")
        self.assertIn("HARNESS:AUTONOMOUS_RULES_START", reset_text)
        self.assertNotIn("Keep this user rule.", reset_text)

        backups = sorted((target.parent / ".harness-backups").iterdir())
        self.assertGreaterEqual(len(backups), 2)
        self.assertTrue((backups[-1] / "memory" / "sentinel.txt").exists())
        self.assertTrue((backups[-1] / "skills" / "junk-skill" / "SKILL.md").exists())

    def test_dry_run_writes_nothing(self) -> None:
        target = self.root / "dry-run-target"
        code, output = self.deploy(target, dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("Dry run complete", output)
        self.assertFalse(target.exists())

    def _run(self, action, target: Path, **kwargs: object) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = action(target, **kwargs)
        return code, output.getvalue()

    def _deployed_hooks(self, target: Path) -> dict:
        return json.loads((target / "hooks.json").read_text(encoding="utf-8"))

    def test_deployed_hooks_use_an_existing_platform_launcher(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)

        hooks = self._deployed_hooks(target)["harness-lifecycle"]
        expected = {
            "session-start": (
                "cmd /c bin\\hook.cmd session-start"
                if os.name == "nt"
                else "sh bin/hook session-start"
            ),
            "post-tool-use": (
                "cmd /c bin\\hook.cmd post-tool-use"
                if os.name == "nt"
                else "sh bin/hook post-tool-use"
            ),
            "stop": (
                "cmd /c bin\\hook.cmd stop"
                if os.name == "nt"
                else "sh bin/hook stop"
            ),
            "pre-invocation": (
                "cmd /c bin\\hook.cmd pre-invocation"
                if os.name == "nt"
                else "sh bin/hook pre-invocation"
            ),
        }
        session = hooks["SessionStart"][0]
        self.assertEqual(session["type"], "command")
        self.assertEqual(session["timeout"], 20)
        self.assertEqual(session["command"], expected["session-start"])

        post_group = hooks["PostToolUse"][0]
        self.assertEqual(post_group["matcher"], "*")
        post = post_group["hooks"][0]
        self.assertEqual(post["type"], "command")
        self.assertEqual(post["timeout"], 5)
        self.assertEqual(post["command"], expected["post-tool-use"])

        stop = hooks["Stop"][0]
        self.assertEqual(stop["type"], "command")
        self.assertEqual(stop["timeout"], 90)
        self.assertEqual(stop["command"], expected["stop"])

        pre = hooks["PreInvocation"][0]
        self.assertEqual(pre["type"], "command")
        self.assertEqual(pre["timeout"], 5)
        self.assertEqual(pre["command"], expected["pre-invocation"])

        launcher = target / "bin" / ("hook.cmd" if os.name == "nt" else "hook")
        self.assertTrue(launcher.is_file(), launcher)
        if os.name != "nt":
            self.assertTrue(os.access(launcher, os.X_OK), launcher)
        # The deployed Stop command runs end to end in the temp target.
        self.assertEqual(install_plugin._smoke_test(target), [])

    def test_deployed_hook_timeouts_honor_settings(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.write_text(
            target / "harness.json",
            json.dumps(
                {"hooks": {"stop_timeout": 37, "post_tool_use_timeout": 8}}
            ),
        )

        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        hooks = self._deployed_hooks(target)["harness-lifecycle"]
        self.assertEqual(hooks["Stop"][0]["timeout"], 37)
        self.assertEqual(hooks["PostToolUse"][0]["hooks"][0]["timeout"], 8)
        # Settings without a hook timeout keep the deployed default.
        self.assertEqual(hooks["PreInvocation"][0]["timeout"], 5)
        self.assertEqual(hooks["SessionStart"][0]["timeout"], 20)

    def test_backup_retention_keeps_the_newest_and_current_backup(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        backup_root = target.parent / ".harness-backups"
        fake = [backup_root / f"2020010{index}-000000" for index in range(1, 5)]
        for path in fake:
            path.mkdir(parents=True, exist_ok=True)

        code, output = self.deploy(target, keep_backups=2)
        self.assertEqual(code, 0, output)
        remaining = sorted(path.name for path in backup_root.iterdir())
        self.assertEqual(len(remaining), 2, remaining)
        self.assertIn("20200104-000000", remaining)
        self.assertIn("prune old backup", output)
        self.assertTrue(
            any(
                (backup_root / name / "plugin.json").is_file()
                for name in remaining
            ),
            remaining,
        )

    def test_dry_run_logs_pruning_without_deleting_backups(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        backup_root = target.parent / ".harness-backups"
        fake = [backup_root / f"2020010{index}-000000" for index in range(1, 5)]
        for path in fake:
            path.mkdir(parents=True, exist_ok=True)

        code, output = self.deploy(target, dry_run=True, keep_backups=2)
        self.assertEqual(code, 0, output)
        self.assertIn("prune old backup", output)
        self.assertTrue(all(path.exists() for path in fake))

    def test_failed_backup_never_prunes_old_backups(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        sentinel = target / "memory" / "sentinel.txt"
        self.write_text(sentinel, "keep\n")
        backup_root = target.parent / ".harness-backups"
        fake = [backup_root / f"2020010{index}-000000" for index in range(1, 5)]
        for path in fake:
            path.mkdir(parents=True, exist_ok=True)

        with mock.patch.object(
            install_plugin.shutil, "copytree", side_effect=OSError("simulated")
        ):
            code, output = self.deploy(target, keep_backups=1)

        self.assertEqual(code, 2, output)
        self.assertIn("backup failed", output)
        self.assertEqual(len(list(backup_root.iterdir())), 4)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_uninstall_backs_up_memory_then_removes_the_plugin(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.write_text(target / "memory" / "sentinel.txt", "keep memory\n")

        code, output = self._run(install_plugin.uninstall, target)
        self.assertEqual(code, 0, output)
        self.assertIn("SUCCESS", output)
        self.assertFalse(target.exists())
        backups = sorted((target.parent / ".harness-backups").iterdir())
        self.assertTrue(backups)
        self.assertTrue((backups[-1] / "plugin.json").is_file())
        self.assertEqual(
            (backups[-1] / "memory" / "sentinel.txt").read_text(encoding="utf-8"),
            "keep memory\n",
        )
        self.assertIn(str(backups[-1]), output)

    def test_uninstall_refuses_non_harness_and_source_checkouts(self) -> None:
        unrelated = self.root / "unrelated"
        self.write_text(unrelated / "notes.txt", "do not remove\n")
        code, output = self._run(install_plugin.uninstall, unrelated)
        self.assertEqual(code, 2, output)
        self.assertTrue((unrelated / "notes.txt").exists())

        code, output = self._run(install_plugin.uninstall, install_plugin.ROOT)
        self.assertEqual(code, 2, output)
        self.assertIn("source checkout", output)
        self.assertTrue((install_plugin.ROOT / "install_plugin.py").exists())

    def test_uninstall_dry_run_keeps_the_plugin(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)

        code, output = self._run(install_plugin.uninstall, target, dry_run=True)
        self.assertEqual(code, 0, output)
        self.assertIn("Dry run complete", output)
        self.assertTrue(target.exists())
        self.assertFalse((target.parent / ".harness-backups").exists())

    def test_uninstall_aborts_when_the_final_backup_fails(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        sentinel = target / "memory" / "sentinel.txt"
        self.write_text(sentinel, "keep\n")

        with mock.patch.object(
            install_plugin.shutil, "copytree", side_effect=OSError("simulated")
        ):
            code, output = self._run(install_plugin.uninstall, target)

        self.assertEqual(code, 2, output)
        self.assertIn("nothing was removed", output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_rollback_restores_the_newest_valid_backup(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        sentinel = target / "memory" / "sentinel.txt"
        self.write_text(sentinel, "first\n")

        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.write_text(sentinel, "second\n")

        code, output = self._run(install_plugin.rollback, target)
        self.assertEqual(code, 0, output)
        self.assertIn("SUCCESS", output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "first\n")

        backups = sorted((target.parent / ".harness-backups").iterdir())
        self.assertEqual(
            (backups[-1] / "memory" / "sentinel.txt").read_text(encoding="utf-8"),
            "second\n",
        )
        leftovers = [
            path.name
            for path in target.parent.iterdir()
            if ".harness-restore-" in path.name or ".harness-previous-" in path.name
        ]
        self.assertEqual(leftovers, [])

    def test_rollback_skips_invalid_backups_and_refuses_without_one(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        sentinel = target / "memory" / "sentinel.txt"
        self.write_text(sentinel, "first\n")
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.write_text(sentinel, "second\n")

        # A lexicographically newest directory without plugin.json is ignored.
        junk = target.parent / ".harness-backups" / "99990101-000000"
        self.write_text(junk / "notes.txt", "not a backup\n")
        code, output = self._run(install_plugin.rollback, target)
        self.assertEqual(code, 0, output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "first\n")

        empty_target = self.root / "no-backup-parent" / "target"
        code, output = self._run(install_plugin.rollback, empty_target)
        self.assertEqual(code, 2, output)
        self.assertIn("no valid Harness", output)

    def test_rollback_dry_run_and_backup_failure_leave_the_target_alone(self) -> None:
        target = self.root / "deployed"
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        sentinel = target / "memory" / "sentinel.txt"
        self.write_text(sentinel, "old\n")
        code, output = self.deploy(target)
        self.assertEqual(code, 0, output)
        self.write_text(sentinel, "current\n")

        code, output = self._run(install_plugin.rollback, target, dry_run=True)
        self.assertEqual(code, 0, output)
        self.assertIn("Dry run complete", output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "current\n")

        with mock.patch.object(
            install_plugin.shutil, "copytree", side_effect=OSError("simulated")
        ):
            code, output = self._run(install_plugin.rollback, target)
        self.assertEqual(code, 2, output)
        self.assertIn("nothing was restored", output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "current\n")

    def test_cli_rejects_unsafe_mode_combinations(self) -> None:
        combinations = (
            ("--uninstall", "--rollback"),
            ("--uninstall", "--reset"),
            ("--uninstall", "--force"),
            ("--rollback", "--reset"),
            ("--rollback", "--force"),
        )
        for combination in combinations:
            stderr = io.StringIO()
            with mock.patch.object(
                sys, "argv", ["install_plugin.py", *combination]
            ), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                install_plugin.main()
            self.assertEqual(raised.exception.code, 2, combination)
            message = stderr.getvalue()
            self.assertTrue(
                "mutually exclusive" in message or "cannot be combined" in message,
                message,
            )

    def test_refuses_to_deploy_over_the_source_checkout(self) -> None:
        code, output = self.deploy(install_plugin.ROOT, dry_run=True)
        self.assertEqual(code, 2)
        self.assertIn("source checkout", output)

    def test_refuses_to_deploy_over_an_unrelated_non_empty_directory(self) -> None:
        target = self.root / "unrelated"
        self.write_text(target / "notes.txt", "do not replace me\n")
        code, output = self.deploy(target)
        self.assertEqual(code, 2)
        self.assertIn("does not look like a Harness", output)
        self.assertEqual((target / "notes.txt").read_text(encoding="utf-8"), "do not replace me\n")

    def test_refuses_a_file_as_target(self) -> None:
        target = self.root / "not-a-directory"
        self.write_text(target, "file\n")
        code, output = self.deploy(target)
        self.assertEqual(code, 2)
        self.assertIn("not a directory", output)

    def test_refuses_root_home_and_antigravity_config_targets(self) -> None:
        root = Path(self.root.anchor)
        self.assertIn("filesystem root", install_plugin._target_problem(root))
        self.assertIn("home directory", install_plugin._target_problem(Path.home()))
        self.assertIn(
            "config directory", install_plugin._target_problem(self.gemini_dir)
        )
        self.assertIn(
            "config directory",
            install_plugin._target_problem(self.gemini_dir / "plugins"),
        )

    def test_hook_smoke_test_uses_the_configured_command(self) -> None:
        hooks = {
            "harness-lifecycle": {
                "Stop": [{"type": "command", "command": "python -X utf8 scripts/hook_runner.py stop", "timeout": 45}]
            }
        }
        command, timeout = install_plugin._find_hook_command(hooks, "Stop")
        self.assertEqual(command, "python -X utf8 scripts/hook_runner.py stop")
        self.assertEqual(timeout, 45)
        self.assertEqual(install_plugin._find_hook_command(hooks, "SessionStart"), ("", None))


if __name__ == "__main__":
    unittest.main()
