"""Concurrency and isolation tests for cycle locks and pending queues."""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core.config import get_state_dir, project_fingerprint
from harness.core.engine import HarnessEngine, cycle_lock_path
from harness.core.memory_store import MemoryStore
from harness.core.util import file_lock
from harness.hooks import (
    _append_pending,
    _clear_pending,
    _pending_events,
    _pending_lock_path,
    _pending_path,
    handle_post_tool_use,
    handle_stop,
    run_hook_cli,
)


def event(
    event_id: str,
    conversation_id: str,
    workspace: Path | None,
    error: str,
) -> dict:
    return {
        "id": event_id,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "conversation_id": conversation_id,
        "workspace": str(workspace) if workspace else "",
        "tool": "run_command",
        "command": "",
        "error": error,
        "signature": error.lower(),
    }


@contextmanager
def denied_lock(*args: object, **kwargs: object):
    yield False


class TestCycleLocks(IsolatedHarnessTestCase):
    def test_cycle_lock_is_per_workspace(self) -> None:
        first = self.make_dir("project-a")
        second = self.make_dir("project-b")

        first_lock = cycle_lock_path(first)
        self.assertEqual(
            first_lock,
            get_state_dir() / "locks" / f"cycle-{project_fingerprint(first)}.lock",
        )
        self.assertEqual(first_lock, cycle_lock_path(str(first)))
        self.assertNotEqual(first_lock, cycle_lock_path(second))
        self.assertEqual(
            cycle_lock_path(None),
            get_state_dir() / "locks" / "cycle-global.lock",
        )

    def test_cycles_in_different_workspaces_both_process(self) -> None:
        first = self.make_dir("project-a")
        second = self.make_dir("project-b")
        barrier = threading.Barrier(2)

        def fake_run_locked(self, result, **kwargs):  # noqa: ANN001 - test double
            # Both cycles must hold their own lock at the same time. With the
            # old global lock the second thread would time out before reaching
            # this barrier.
            barrier.wait(timeout=10)
            result.status = "processed"
            result.summary = "fake cycle"
            return result

        engines = {
            first: HarnessEngine(first),
            second: HarnessEngine(second),
        }

        def run(workspace: Path, conversation_id: str):
            return engines[workspace].run_cycle(
                conversation_id=conversation_id,
                lock_timeout=5.0,
            )

        with mock.patch.object(HarnessEngine, "_run_locked", fake_run_locked):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(run, first, "conv-a"),
                    pool.submit(run, second, "conv-b"),
                ]
                results = [future.result(timeout=20) for future in futures]

        self.assertEqual([result.status for result in results], ["processed", "processed"])

    def test_same_workspace_conversations_share_the_lock(self) -> None:
        self.assertEqual(
            cycle_lock_path(self.workspace),
            cycle_lock_path(self.workspace.resolve()),
        )
        with file_lock(cycle_lock_path(self.workspace)):
            result = HarnessEngine(self.workspace).run_cycle(
                conversation_id="conv-blocked",
                raw_steps=[],
                lock_timeout=0.0,
            )
        self.assertEqual(result.status, "busy")


class TestPendingQueues(IsolatedHarnessTestCase):
    def payload(self, conversation_id: str, workspace: Path | None = None) -> dict:
        payload: dict = {
            "workspacePaths": [str(workspace or self.workspace)],
        }
        if conversation_id:
            payload["conversationId"] = conversation_id
        return payload

    def post_failure(
        self,
        conversation_id: str,
        error: str,
        workspace: Path | None = None,
    ) -> None:
        handle_post_tool_use(
            {
                **self.payload(conversation_id, workspace),
                "toolCall": {"name": "run_command", "error": error},
            }
        )

    def test_pending_paths_and_locks_are_per_conversation(self) -> None:
        first = _pending_path("conv-a")
        second = _pending_path("conv-b")
        self.assertEqual(first, get_state_dir() / "pending" / "conv-a.jsonl")
        self.assertNotEqual(first, second)
        self.assertEqual(_pending_path(""), get_state_dir() / "pending" / "global.jsonl")
        self.assertNotEqual(_pending_lock_path("conv-a"), _pending_lock_path("conv-b"))

    def test_stop_consumes_only_its_own_conversation_queue(self) -> None:
        self.post_failure("conv-a", "ModuleNotFoundError: No module named 'alphapkg'")
        self.post_failure("conv-b", "ModuleNotFoundError: No module named 'betapkg'")
        self.assertTrue(_pending_path("conv-a").exists())
        self.assertTrue(_pending_path("conv-b").exists())

        result = handle_stop(self.payload("conv-a"))
        self.assertEqual(result, {"decision": "stop"})

        store = MemoryStore(workspace=self.workspace)
        self.assertEqual(
            len(store.search_lessons("alphapkg", workspace=self.workspace)), 1
        )
        self.assertEqual(
            store.search_lessons("betapkg", workspace=self.workspace), []
        )
        self.assertIn("betapkg", _pending_path("conv-b").read_text(encoding="utf-8"))

        # B is still consumable afterwards.
        handle_stop(self.payload("conv-b"))
        self.assertEqual(
            len(store.search_lessons("betapkg", workspace=self.workspace)), 1
        )

    def test_event_appended_during_cycle_survives_clear(self) -> None:
        old = event("old-event", "conv-cycle", self.workspace, "old failure")
        new = event("new-event", "conv-cycle", self.workspace, "new failure")
        _append_pending(old)
        consumed = {str(item["id"]) for item in _pending_events("conv-cycle", self.workspace)}
        self.assertEqual(consumed, {"old-event"})

        # This append happens after the cycle read its snapshot.
        _append_pending(new)
        _clear_pending("conv-cycle", self.workspace, consumed)

        remaining = _pending_events("conv-cycle", self.workspace)
        self.assertEqual([item["id"] for item in remaining], ["new-event"])
        self.assertIn("new failure", remaining[0]["error"])

    def test_legacy_queue_is_read_cleared_and_deduped(self) -> None:
        legacy = get_state_dir() / "pending_events.jsonl"
        matching = event(
            "legacy-match", "conv-legacy", self.workspace, "legacymatchpkg failure"
        )
        other = event(
            "legacy-other", "conv-other", self.workspace, "legacyotherpkg failure"
        )
        legacy.write_text(
            json.dumps(matching) + "\n" + json.dumps(other) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        # The same id also exists in the new queue; it must be consumed once.
        _append_pending(matching)

        pending = _pending_events("conv-legacy", self.workspace)
        self.assertEqual([item["id"] for item in pending], ["legacy-match"])

        result = handle_stop(self.payload("conv-legacy"))
        self.assertEqual(result, {"decision": "stop"})

        store = MemoryStore(workspace=self.workspace)
        self.assertEqual(
            len(store.search_lessons("legacymatchpkg", workspace=self.workspace)), 1
        )
        remaining = legacy.read_text(encoding="utf-8")
        self.assertNotIn("legacy-match", remaining)
        self.assertIn("legacy-other", remaining)

    def test_empty_conversation_uses_global_queue_but_keeps_workspace_scope(self) -> None:
        other = self.make_dir("other-workspace")
        self.post_failure("", "ModuleNotFoundError: No module named 'globalapkg'")
        self.post_failure(
            "", "ModuleNotFoundError: No module named 'globalbpkg'", workspace=other
        )

        global_queue = _pending_path("")
        self.assertEqual(global_queue, get_state_dir() / "pending" / "global.jsonl")
        self.assertTrue(global_queue.exists())
        own = _pending_events("", self.workspace)
        self.assertEqual(len(own), 1)
        self.assertIn("globalapkg", own[0]["error"])

        handle_stop(self.payload(""))
        remaining = global_queue.read_text(encoding="utf-8")
        self.assertNotIn("globalapkg", remaining)
        self.assertIn("globalbpkg", remaining)

        handle_stop(self.payload("", workspace=other))
        store = MemoryStore(workspace=other)
        self.assertEqual(len(store.search_lessons("globalbpkg", workspace=other)), 1)

    def test_lock_timeout_paths_still_return_valid_json(self) -> None:
        queued = event("queued-event", "conv-timeout", self.workspace, "queued failure")
        _append_pending(queued)

        with mock.patch("harness.hooks.file_lock", side_effect=denied_lock):
            self.assertEqual(
                handle_post_tool_use(
                    {
                        **self.payload("conv-timeout"),
                        "toolCall": {"name": "run_command", "error": "dropped failure"},
                    }
                ),
                {},
            )
            self.assertEqual(handle_stop(self.payload("conv-timeout")), {"decision": "stop"})

        errors = (get_state_dir() / "hook_errors.log").read_text(encoding="utf-8")
        self.assertIn("lock timeout", errors)
        # A denied read must not consume the queued observation.
        self.assertIn("queued-event", _pending_path("conv-timeout").read_text(encoding="utf-8"))

    def test_lock_errors_still_return_valid_json_through_the_cli(self) -> None:
        def broken_lock(*args: object, **kwargs: object):
            raise OSError("lock unavailable")

        post_stdout = io.StringIO()
        stop_stdout = io.StringIO()
        with mock.patch("harness.hooks.file_lock", side_effect=broken_lock):
            with mock.patch(
                "sys.stdin",
                io.StringIO(
                    json.dumps(
                        {
                            **self.payload("conv-cli-error"),
                            "toolCall": {
                                "name": "run_command",
                                "error": "cli failure",
                            },
                        }
                    )
                ),
            ):
                with redirect_stdout(post_stdout):
                    run_hook_cli("post-tool-use")
            with mock.patch(
                "sys.stdin",
                io.StringIO(json.dumps(self.payload("conv-cli-error"))),
            ):
                with redirect_stdout(stop_stdout):
                    run_hook_cli("stop")

        self.assertEqual(json.loads(post_stdout.getvalue()), {})
        self.assertEqual(json.loads(stop_stdout.getvalue()), {"decision": "stop"})
        self.assertTrue((get_state_dir() / "hook_errors.log").exists())


if __name__ == "__main__":
    unittest.main()
