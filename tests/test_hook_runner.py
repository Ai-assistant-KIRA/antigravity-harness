"""Subprocess tests for the actual Antigravity hook entry point."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "hook_runner.py"


class TestHookRunner(IsolatedHarnessTestCase):
    def run_hook(
        self,
        event: str,
        payload: object = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["HARNESS_PLUGIN_DIR"] = str(self.plugin_dir)
        env["HARNESS_GEMINI_CONFIG_DIR"] = str(self.gemini_dir)
        env["ANTIGRAVITY_APP_DATA_DIR"] = str(self.app_data)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("ANTIGRAVITY_CONVERSATION_ID", None)
        if stdin is None:
            stdin = json.dumps(payload if payload is not None else {})
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(RUNNER), event],
            input=stdin,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=str(self.root),
            env=env,
            timeout=60,
        )

    def test_session_start_returns_valid_json(self) -> None:
        completed = self.run_hook(
            "session-start",
            {
                "conversationId": "conv-runner",
                "workspacePaths": [str(self.workspace)],
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIn("injectSteps", payload)
        self.assertTrue(payload["injectSteps"])
        message = payload["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Harness²", message)

    def test_stop_returns_decision(self) -> None:
        completed = self.run_hook("stop", {})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"decision": "stop"})

    def test_malformed_input_still_returns_valid_json(self) -> None:
        completed = self.run_hook("stop", stdin="this is not json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"decision": "stop"})

    def test_unknown_event_returns_empty_object(self) -> None:
        completed = self.run_hook("unknown-event", {})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {})

    def test_post_tool_use_journals_a_failure(self) -> None:
        completed = self.run_hook(
            "post-tool-use",
            {
                "conversationId": "conv-runner",
                "workspacePaths": [str(self.workspace)],
                "toolCall": {
                    "name": "run_command",
                    "arguments": {"CommandLine": "pytest"},
                    "error": "ModuleNotFoundError: No module named 'pytest'",
                },
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {})
        pending = self.plugin_dir / "state" / "pending" / "conv-runner.jsonl"
        self.assertTrue(pending.exists())
        event = json.loads(pending.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["tool"], "run_command")
        self.assertIn("ModuleNotFoundError", event["error"])

    def test_concurrent_post_tool_use_loses_no_events(self) -> None:
        count = 12
        payloads = [
            {
                "conversationId": "conv-concurrent",
                "workspacePaths": [str(self.workspace)],
                "toolCall": {
                    "name": "run_command",
                    "arguments": {"CommandLine": f"python failing_{index}.py"},
                    "error": f"ModuleNotFoundError: No module named 'concurrentpkg{index}'",
                },
            }
            for index in range(count)
        ]
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(
                pool.map(lambda item: self.run_hook("post-tool-use", item), payloads)
            )
        for completed in results:
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {})

        pending = self.plugin_dir / "state" / "pending" / "conv-concurrent.jsonl"
        lines = pending.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        self.assertEqual(len(events), count)
        self.assertEqual(len({event["id"] for event in events}), count)
        self.assertEqual(
            {event["error"] for event in events},
            {
                f"ModuleNotFoundError: No module named 'concurrentpkg{index}'"
                for index in range(count)
            },
        )

    def test_runner_uses_its_own_plugin_root_by_default(self) -> None:
        # Build a disposable plugin tree so removing HARNESS_PLUGIN_DIR proves
        # that hook_runner derives its data root from __file__, not from cwd.
        import shutil

        source_root = RUNNER.parent.parent
        source_db = source_root / "memory" / "harness_memory.db"
        source_mtime = source_db.stat().st_mtime_ns if source_db.exists() else None
        fake_root = self.root / "fake-plugin"
        shutil.copytree(
            source_root / "harness",
            fake_root / "harness",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (fake_root / "scripts").mkdir(parents=True)
        shutil.copy2(RUNNER, fake_root / "scripts" / "hook_runner.py")

        env = dict(os.environ)
        env.pop("HARNESS_PLUGIN_DIR", None)
        env["HARNESS_GEMINI_CONFIG_DIR"] = str(self.gemini_dir)
        env["ANTIGRAVITY_APP_DATA_DIR"] = str(self.app_data)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(fake_root / "scripts" / "hook_runner.py"), "stop"],
            input="{}",
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=str(self.root),
            env=env,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"decision": "stop"})
        self.assertTrue((fake_root / "memory" / "harness_memory.db").exists())
        # Do not require the source tree to be pristine (a developer may have
        # run the hook locally); require that this subprocess did not touch it.
        source_db = source_root / "memory" / "harness_memory.db"
        if source_mtime is None:
            self.assertFalse(source_db.exists())
        else:
            self.assertEqual(source_db.stat().st_mtime_ns, source_mtime)


if __name__ == "__main__":
    unittest.main()
