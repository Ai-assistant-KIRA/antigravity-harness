"""End-to-end tests for the reflection engine, loader, and hook contract."""

from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

try:
    from .support import IsolatedHarnessTestCase, make_event
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase, make_event

from harness.core.analyzer import SessionAnalysis
from harness.core.config import get_event_journal_path, get_state_dir
from harness.core.engine import HarnessEngine, cycle_lock_path
from harness.core.loader import SessionLoader
from harness.core.memory_store import MemoryStore
from harness.core.util import file_lock
from harness.hooks import (
    handle_post_tool_use,
    handle_session_start,
    handle_stop,
)


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


def learning_session() -> list[dict]:
    return [
        user("Install pytest and run the test suite"),
        tool("run_command", CommandLine="pytest -q"),
        output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
        tool("run_command", CommandLine="pip install pytest"),
        output("Successfully installed pytest"),
        tool("run_command", CommandLine="pytest -q"),
        output("5 passed"),
    ]


class TestEngine(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine = HarnessEngine(self.workspace)

    def test_full_cycle_creates_rules_skill_and_memory(self) -> None:
        result = self.engine.run_cycle(
            conversation_id="conv-e2e",
            raw_steps=learning_session(),
        )
        self.assertEqual(result.status, "processed")
        self.assertEqual(result.contrastive_events_found, 1)
        self.assertEqual(result.verified_lessons, 1)
        self.assertTrue(result.rules_updated)
        self.assertEqual(result.skills_synthesized, ["run-python-tests"])
        self.assertTrue(result.memory_logged)
        self.assertEqual(result.memory_counts["created"], 1)

        workspace_rules = self.workspace / "AGENTS.md"
        self.assertTrue(workspace_rules.exists())
        rules_text = workspace_rules.read_text(encoding="utf-8")
        self.assertIn("pip install pytest", rules_text)
        self.assertIn("HARNESS:AUTONOMOUS_RULES_START", rules_text)
        self.assertFalse((self.plugin_dir / "rules" / "AGENTS.md").exists())

        skill = self.workspace / ".agents" / "skills" / "run-python-tests" / "SKILL.md"
        self.assertTrue(skill.exists())
        skill_text = skill.read_text(encoding="utf-8")
        self.assertIn("pip install pytest", skill_text)
        self.assertIn("pytest -q", skill_text)

        stats = self.engine.memory_store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["verified_lessons"], 1)
        self.assertEqual(stats["skills"], 1)

    def test_unauthorized_reprocessing_is_skipped(self) -> None:
        first = self.engine.run_cycle(
            conversation_id="conv-idempotent", raw_steps=learning_session()
        )
        self.assertEqual(first.status, "processed")
        second = self.engine.run_cycle(
            conversation_id="conv-idempotent", raw_steps=learning_session()
        )
        self.assertEqual(second.status, "skipped")
        stats = self.engine.memory_store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["verified_lessons"], 1)

    def test_forced_reprocessing_is_idempotent(self) -> None:
        self.engine.run_cycle(conversation_id="conv-force", raw_steps=learning_session())
        forced = self.engine.run_cycle(
            conversation_id="conv-force", raw_steps=learning_session(), force=True
        )
        self.assertEqual(forced.status, "processed")
        self.assertEqual(forced.skills_synthesized, [])
        self.assertTrue(
            any(item["name"] == "run-python-tests" for item in forced.skills_skipped)
        )
        self.assertEqual(forced.memory_counts, {"created": 0, "updated": 0, "skipped": 1})
        skill = self.workspace / ".agents" / "skills" / "run-python-tests" / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertEqual(text.count("HARNESS:SKILL_START"), 1)

    def test_unresolved_failure_only_records_a_candidate(self) -> None:
        result = self.engine.run_cycle(
            conversation_id="conv-candidate",
            raw_steps=[
                user("Run the tests"),
                tool("run_command", CommandLine="pytest -q"),
                output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
            ],
        )
        self.assertEqual(result.contrastive_events_found, 0)
        self.assertEqual(result.unresolved_observations, 1)
        self.assertFalse(result.rules_updated)
        self.assertEqual(result.skills_synthesized, [])
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertFalse((self.workspace / ".agents" / "skills").exists())
        stats = self.engine.memory_store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["candidate_lessons"], 1)
        self.assertEqual(stats["verified_lessons"], 0)

    def test_global_cycle_touches_only_plugin_rules(self) -> None:
        global_engine = HarnessEngine(None)
        result = global_engine.run_cycle(
            conversation_id="conv-global",
            raw_steps=learning_session(),
        )
        self.assertEqual(result.status, "processed")
        plugin_rules = self.plugin_dir / "rules" / "AGENTS.md"
        self.assertTrue(plugin_rules.exists())
        self.assertIn("pip install pytest", plugin_rules.read_text(encoding="utf-8"))
        self.assertFalse((self.workspace / "AGENTS.md").exists())

    def test_observations_are_candidates_and_never_rules(self) -> None:
        result = self.engine.record_observations(
            [
                {
                    "tool": "run_command",
                    "error": "ModuleNotFoundError: No module named 'httpx'",
                }
            ],
            conversation_id="conv-observe",
        )
        self.assertEqual(result.status, "processed")
        self.assertEqual(result.unresolved_observations, 1)
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        stats = self.engine.memory_store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["candidate_lessons"], 1)

    def test_missing_transcript_is_reported(self) -> None:
        result = self.engine.run_cycle(conversation_id="conv-missing")
        self.assertEqual(result.status, "no_transcript")
        self.assertFalse(result.memory_logged)

    def test_lock_contention_returns_busy(self) -> None:
        lock_path = cycle_lock_path(self.workspace)
        with file_lock(lock_path):
            result = self.engine.run_cycle(
                conversation_id="conv-lock",
                raw_steps=learning_session(),
                lock_timeout=0.0,
            )
        self.assertEqual(result.status, "busy")


class TestLoader(IsolatedHarnessTestCase):
    def test_briefing_lists_workspace_skills_and_relevant_lessons(self) -> None:
        skill = self.workspace / ".agents" / "skills" / "demo-skill" / "SKILL.md"
        self.write_text(
            skill,
            "---\nname: demo-skill\ndescription: Use for pytest projects with Python workflows.\n---\n\nBody.\n",
        )
        self.write_text(
            self.workspace / "pyproject.toml",
            "[project]\ndependencies = ['pytest']\n",
        )
        store = MemoryStore(workspace=self.workspace)
        store.record_session(
            SessionAnalysis(
                conversation_id="conv-loader",
                workspace=str(self.workspace),
                tasks=["Run pytest"],
                contrastive_events=[make_event()],
            )
        )
        message = SessionLoader(self.workspace).build_briefing()
        self.assertIn("Harness² Self-Improvement System Active", message)
        self.assertIn("demo-skill", message)
        self.assertIn("Detected stack", message)
        self.assertIn("pytest", message)
        self.assertIn(str(self.workspace), message)
        self.assertIn("Verified lessons", message)

    def test_large_global_rules_produce_a_warning(self) -> None:
        rules = self.plugin_dir / "rules" / "AGENTS.md"
        self.write_text(rules, "x" * 20_500)
        message = SessionLoader(self.workspace).build_briefing()
        self.assertIn("Global rules file is", message)
        self.assertIn("24 KB", message)

    def test_session_start_payload_shape(self) -> None:
        payload = SessionLoader(self.workspace).generate_session_start_payload(
            {"conversationId": "conv-start", "workspacePaths": [str(self.workspace)]}
        )
        self.assertEqual(len(payload["injectSteps"]), 1)
        message = payload["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Harness²", message)


class TestHooks(IsolatedHarnessTestCase):
    def payload(self, **extra: object) -> dict:
        payload = {
            "conversationId": "conv-hook",
            "workspacePaths": [str(self.workspace)],
        }
        payload.update(extra)
        return payload

    def pending_path(self, conversation_id: str = "conv-hook") -> Path:
        return get_state_dir() / "pending" / f"{conversation_id or 'global'}.jsonl"

    def test_session_start_returns_inject_steps(self) -> None:
        skill = self.workspace / ".agents" / "skills" / "hooked-skill" / "SKILL.md"
        self.write_text(
            skill,
            "---\nname: hooked-skill\ndescription: A long enough description for the hook test.\n---\n\nBody.\n",
        )
        result = handle_session_start(self.payload())
        self.assertIn("injectSteps", result)
        self.assertTrue(result["injectSteps"])
        message = result["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("hooked-skill", message)
        self.assertTrue(get_event_journal_path().exists())

    def test_post_tool_use_records_redacted_pending_event(self) -> None:
        result = handle_post_tool_use(
            self.payload(
                toolCall={
                    "name": "run_command",
                    "arguments": {"CommandLine": "curl api"},
                    "error": "Error: API_KEY=super-secret-value-123",
                }
            )
        )
        self.assertEqual(result, {})
        pending = self.pending_path()
        text = pending.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-value-123", text)
        self.assertIn("[REDACTED]", text)
        event = json.loads(text.splitlines()[0])
        self.assertEqual(event["tool"], "run_command")
        self.assertEqual(event["conversation_id"], "conv-hook")

    def test_post_tool_use_without_error_is_a_noop(self) -> None:
        result = handle_post_tool_use(
            self.payload(toolCall={"name": "run_command", "arguments": {}})
        )
        self.assertEqual(result, {})
        self.assertFalse(self.pending_path().exists())

    def test_pending_events_are_isolated_by_conversation_and_workspace(self) -> None:
        other = self.root / "other-workspace"
        other.mkdir()
        handle_post_tool_use(
            self.payload(
                conversationId="conv-other",
                toolCall={
                    "name": "run_command",
                    "error": "ModuleNotFoundError: No module named 'otherdep'",
                },
            )
        )
        handle_post_tool_use(
            {
                "conversationId": "conv-hook",
                "workspacePaths": [str(other)],
                "toolCall": {
                    "name": "run_command",
                    "error": "ModuleNotFoundError: No module named 'workspacedep'",
                },
            }
        )

        # Neither event belongs to this conversation + workspace.
        handle_stop(self.payload())
        self.assertEqual(
            MemoryStore(workspace=self.workspace).search_lessons(
                "otherdep workspacedep", workspace=self.workspace
            ),
            [],
        )
        self.assertIn(
            "otherdep", self.pending_path("conv-other").read_text(encoding="utf-8")
        )
        self.assertIn(
            "workspacedep", self.pending_path("conv-hook").read_text(encoding="utf-8")
        )

        # The owning conversation may consume its own observation.
        handle_stop(self.payload(conversationId="conv-other"))
        owned = MemoryStore(workspace=self.workspace).search_lessons(
            "otherdep", workspace=self.workspace
        )
        self.assertEqual(len(owned), 1)
        self.assertFalse(owned[0]["verified"])
        self.assertNotIn(
            "otherdep", self.pending_path("conv-other").read_text(encoding="utf-8")
        )
        self.assertIn(
            "workspacedep", self.pending_path("conv-hook").read_text(encoding="utf-8")
        )

    def test_duplicate_pending_events_are_recorded_once(self) -> None:
        payload = self.payload(
            toolCall={
                "name": "run_command",
                "error": "ModuleNotFoundError: No module named 'duplicatepkg'",
            }
        )
        handle_post_tool_use(payload)
        handle_post_tool_use(payload)
        pending = self.pending_path().read_text(encoding="utf-8")
        self.assertEqual(len(pending.splitlines()), 1)
        handle_stop(self.payload())
        candidates = MemoryStore(workspace=self.workspace).search_lessons(
            "duplicatepkg", workspace=self.workspace
        )
        self.assertEqual(len(candidates), 1)

    def test_stop_analyses_the_transcript_and_clears_pending(self) -> None:
        handle_post_tool_use(
            self.payload(
                toolCall={
                    "name": "run_command",
                    "error": "temporary failure observation",
                }
            )
        )
        transcript = self.make_transcript("stop.jsonl", learning_session())
        result = handle_stop(self.payload(transcriptPath=str(transcript)))
        self.assertEqual(result, {"decision": "stop"})
        self.assertTrue((self.workspace / "AGENTS.md").exists())
        self.assertTrue(
            (self.workspace / ".agents" / "skills" / "run-python-tests" / "SKILL.md").exists()
        )
        pending = self.pending_path()
        self.assertTrue(not pending.exists() or pending.read_text(encoding="utf-8") == "")

    def test_stop_without_transcript_records_observations(self) -> None:
        handle_post_tool_use(
            self.payload(
                toolCall={
                    "name": "run_command",
                    "error": "ModuleNotFoundError: No module named 'httpx'",
                }
            )
        )
        result = handle_stop(self.payload())
        self.assertEqual(result, {"decision": "stop"})
        store = MemoryStore(workspace=self.workspace)
        candidates = store.search_lessons("httpx", workspace=self.workspace)
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["verified"])
        pending = self.pending_path()
        self.assertTrue(not pending.exists() or pending.read_text(encoding="utf-8") == "")

    def test_skipped_stop_retains_pending_observations(self) -> None:
        transcript = self.make_transcript("skip.jsonl", learning_session())
        first = handle_stop(self.payload(transcriptPath=str(transcript)))
        self.assertEqual(first, {"decision": "stop"})

        handle_post_tool_use(
            self.payload(
                toolCall={
                    "name": "run_command",
                    "error": "ModuleNotFoundError: No module named 'retainedpkg'",
                }
            )
        )
        # The transcript hash is unchanged, so this cycle is skipped. Skipping
        # must not discard an observation that the next turn still needs.
        handle_stop(self.payload(transcriptPath=str(transcript)))
        pending = self.pending_path().read_text(encoding="utf-8")
        self.assertIn("retainedpkg", pending)

    def test_concurrent_stop_cycles_are_serialized_not_dropped(self) -> None:
        count = 6
        for index in range(count):
            handle_post_tool_use(
                self.payload(
                    conversationId=f"conv-concurrent-{index}",
                    toolCall={
                        "name": "run_command",
                        "error": f"ModuleNotFoundError: No module named 'parallelpkg{index}'",
                    },
                )
            )

        payloads = [
            self.payload(conversationId=f"conv-concurrent-{index}")
            for index in range(count)
        ]
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(handle_stop, payloads))
        self.assertEqual(results, [{"decision": "stop"}] * count)

        store = MemoryStore(workspace=self.workspace)
        for index in range(count):
            pending = self.pending_path(f"conv-concurrent-{index}")
            self.assertTrue(
                not pending.exists() or pending.read_text(encoding="utf-8") == "",
                f"pending queue {index} was not cleared",
            )
            candidates = store.search_lessons(
                f"parallelpkg{index}", workspace=self.workspace
            )
            self.assertEqual(len(candidates), 1, f"missing candidate {index}")
            self.assertFalse(candidates[0]["verified"])

    def test_stop_is_fail_safe(self) -> None:
        with mock.patch(
            "harness.core.engine.HarnessEngine.run_cycle",
            side_effect=RuntimeError("boom"),
        ):
            result = handle_stop(self.payload())
        self.assertEqual(result, {"decision": "stop"})
        self.assertTrue((get_state_dir() / "hook_errors.log").exists())

    def test_session_start_is_fail_safe(self) -> None:
        with mock.patch(
            "harness.core.loader.SessionLoader.generate_session_start_payload",
            side_effect=RuntimeError("boom"),
        ):
            result = handle_session_start(self.payload())
        self.assertEqual(result, {"injectSteps": []})
        self.assertTrue((get_state_dir() / "hook_errors.log").exists())


if __name__ == "__main__":
    unittest.main()
