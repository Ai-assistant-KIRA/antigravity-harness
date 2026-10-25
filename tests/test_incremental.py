"""Tests for incremental transcript cursors and their engine integration."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core.engine import HarnessEngine
from harness.core.incremental import (
    CONTEXT_LIMIT_BYTES,
    CONTEXT_LIMIT_STEPS,
    cursor_path,
    load_cursor,
    read_incremental,
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


def append_steps(path: Path, steps: list[dict]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for step in steps:
            handle.write(json.dumps(step, ensure_ascii=False) + "\n")


def failure_session() -> list[dict]:
    return [
        user("Run the test suite"),
        tool("run_command", CommandLine="pytest -q"),
        output("ModuleNotFoundError: No module named 'pytest'", "ERROR"),
    ]


def recovery_steps() -> list[dict]:
    return [
        tool("run_command", CommandLine="pip install pytest"),
        output("Successfully installed pytest"),
        tool("run_command", CommandLine="pytest -q"),
        output("5 passed"),
    ]


def learning_session() -> list[dict]:
    return failure_session() + recovery_steps()


class TestIncrementalReader(IsolatedHarnessTestCase):
    def test_full_parse_creates_cursor_and_idle_read_is_stable(self) -> None:
        transcript = self.make_transcript("basic.jsonl", learning_session())
        first = read_incremental(transcript, conversation_id="conv-basic")
        self.assertFalse(first.from_cursor)
        self.assertEqual(len(first.steps), len(learning_session()))
        self.assertEqual(first.new_steps, len(learning_session()))
        self.assertIsNotNone(first.cursor)
        self.assertTrue(first.save())

        state = cursor_path("conv-basic", transcript)
        self.assertTrue(state.exists())
        before = state.read_text(encoding="utf-8")
        self.assertTrue(first.save())
        self.assertEqual(before, state.read_text(encoding="utf-8"))
        stored = load_cursor("conv-basic", transcript)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.offset, transcript.stat().st_size)
        self.assertEqual(stored.line_count, len(learning_session()))

        second = read_incremental(transcript, conversation_id="conv-basic")
        self.assertTrue(second.from_cursor)
        self.assertEqual(second.new_steps, 0)
        self.assertEqual(second.steps, first.steps)

    def test_append_only_failure_then_recovery_pairs_across_boundary(self) -> None:
        transcript = self.make_transcript("append.jsonl", failure_session())
        engine = HarnessEngine(self.workspace)

        first = engine.run_cycle("conv-append", transcript_path=transcript)
        self.assertEqual(first.status, "processed")
        self.assertEqual(first.contrastive_events_found, 0)
        self.assertEqual(first.unresolved_observations, 1)

        append_steps(transcript, recovery_steps())
        second = engine.run_cycle("conv-append", transcript_path=transcript)
        self.assertEqual(second.status, "processed")
        self.assertEqual(second.contrastive_events_found, 1)
        self.assertEqual(second.verified_lessons, 1)
        self.assertTrue(second.rules_updated)
        self.assertNotEqual(first.transcript_hash, second.transcript_hash)

        # A repeated Stop with no new lines replays the same bounded context,
        # so the content hash is unchanged and the cycle is skipped.
        third = engine.run_cycle("conv-append", transcript_path=transcript)
        self.assertEqual(third.status, "skipped")
        self.assertEqual(third.transcript_hash, second.transcript_hash)

        stats = engine.memory_store.get_stats(workspace=self.workspace)
        self.assertEqual(stats["verified_lessons"], 1)
        # The candidate from Stop 1 was promoted, not counted a second time.
        lessons = engine.memory_store.search_lessons(
            "pytest", workspace=self.workspace
        )
        self.assertEqual(len(lessons), 1)
        self.assertTrue(lessons[0]["verified"])
        self.assertEqual(lessons[0]["occurrences"], 1)

    def test_partial_trailing_line_is_left_unconsumed(self) -> None:
        first_step = user("First complete step")
        second_step = tool("run_command", CommandLine="echo done")
        first_line = json.dumps(first_step) + "\n"
        second_line = json.dumps(second_step) + "\n"
        partial = second_line[:-4]
        transcript = self.root / "transcripts" / "partial.jsonl"
        self.write_text(transcript, first_line + partial)

        first = read_incremental(transcript, conversation_id="conv-partial")
        self.assertEqual(first.steps, [first_step])
        self.assertEqual(first.cursor.offset, len(first_line.encode("utf-8")))
        self.assertTrue(first.save())

        with transcript.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(second_line[-4:])
        second = read_incremental(transcript, conversation_id="conv-partial")
        self.assertTrue(second.from_cursor)
        self.assertEqual(second.new_steps, 1)
        self.assertEqual(second.steps, [first_step, second_step])
        self.assertEqual(second.cursor.offset, transcript.stat().st_size)

    def test_partial_valid_json_line_is_processed_only_once(self) -> None:
        first_step = user("First")
        second_step = user("Second")
        first_line = json.dumps(first_step) + "\n"
        second_line = json.dumps(second_step)
        transcript = self.root / "transcripts" / "partial-valid.jsonl"
        self.write_text(transcript, first_line + second_line)

        first = read_incremental(transcript, conversation_id="conv-partial-valid")
        self.assertEqual(first.steps, [first_step])
        first.save()

        with transcript.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n")
        second = read_incremental(transcript, conversation_id="conv-partial-valid")
        self.assertEqual(second.new_steps, 1)
        self.assertEqual(second.steps, [first_step, second_step])

    def test_rewrite_or_truncation_falls_back_to_full_parse(self) -> None:
        transcript = self.make_transcript(
            "rewrite.jsonl", [user("Alpha"), user("Beta")]
        )
        first = read_incremental(transcript, conversation_id="conv-rewrite")
        self.assertEqual(first.new_steps, 2)
        first.save()
        original_offset = first.cursor.offset

        # Truncation below the cursor offset.
        self.write_text(transcript, json.dumps(user("Alpha")) + "\n")
        truncated = read_incremental(transcript, conversation_id="conv-rewrite")
        self.assertFalse(truncated.from_cursor)
        self.assertEqual(truncated.steps, [user("Alpha")])
        self.assertLess(truncated.cursor.offset, original_offset)
        truncated.save()

        # Same conversation, rewritten head with a different first line.
        self.write_text(
            transcript,
            json.dumps(user("Rewritten")) + "\n" + json.dumps(user("Beta")) + "\n",
        )
        rewritten = read_incremental(transcript, conversation_id="conv-rewrite")
        self.assertFalse(rewritten.from_cursor)
        self.assertEqual(rewritten.steps[0], user("Rewritten"))
        rewritten.save()

        stable = read_incremental(transcript, conversation_id="conv-rewrite")
        self.assertTrue(stable.from_cursor)
        self.assertEqual(stable.new_steps, 0)

    def test_corrupt_cursor_falls_back_safely(self) -> None:
        transcript = self.make_transcript("corrupt.jsonl", learning_session())
        first = read_incremental(transcript, conversation_id="conv-corrupt")
        first.save()
        state = cursor_path("conv-corrupt", transcript)
        state.write_text("{ definitely not json", encoding="utf-8")

        recovered = read_incremental(transcript, conversation_id="conv-corrupt")
        self.assertFalse(recovered.from_cursor)
        self.assertEqual(len(recovered.steps), len(learning_session()))
        self.assertIsNotNone(recovered.cursor)
        self.assertTrue(recovered.save())
        self.assertIsNotNone(load_cursor("conv-corrupt", transcript))

    def test_context_step_cap_drops_old_steps_without_warning(self) -> None:
        total = CONTEXT_LIMIT_STEPS + 25
        transcript = self.make_transcript(
            "cap.jsonl", [user(f"Step {index}") for index in range(total)]
        )
        first = read_incremental(transcript, conversation_id="conv-cap")
        self.assertTrue(first.truncated)
        self.assertTrue(first.context_truncated)
        # The pairing window is an internal budget, not an analysis failure.
        self.assertEqual(first.warning, "")
        self.assertEqual(first.new_steps, total)
        # The full read must analyse every step; only the saved pairing
        # context is bounded.
        self.assertEqual(len(first.steps), total)
        self.assertEqual(len(first.cursor.context), CONTEXT_LIMIT_STEPS)
        self.assertEqual(first.steps[0], user("Step 0"))
        self.assertEqual(first.cursor.context[0], user(f"Step {total - CONTEXT_LIMIT_STEPS}"))
        first.save()

        append_steps(transcript, [user("Newest")])
        second = read_incremental(transcript, conversation_id="conv-cap")
        self.assertTrue(second.from_cursor)
        self.assertEqual(second.new_steps, 1)
        self.assertEqual(len(second.steps), CONTEXT_LIMIT_STEPS)
        self.assertEqual(second.steps[-1], user("Newest"))
        self.assertEqual(second.steps[0], user(f"Step {total - CONTEXT_LIMIT_STEPS + 1}"))
        self.assertTrue(second.truncated)

    def test_context_byte_cap_is_enforced(self) -> None:
        large = "x" * 600_000
        transcript = self.make_transcript(
            "bytes.jsonl", [user(f"Large {index} {large}") for index in range(6)]
        )
        read = read_incremental(transcript, conversation_id="conv-bytes")
        self.assertTrue(read.context_truncated)
        # The full read still analyses all six steps; only the pairing context
        # is byte-bounded.
        self.assertEqual(len(read.steps), 6)
        self.assertLess(len(read.cursor.context), 6)
        serialized = json.dumps(read.cursor.context, ensure_ascii=False)
        self.assertLessEqual(len(serialized.encode("utf-8")), CONTEXT_LIMIT_BYTES)
        self.assertEqual(read.steps[-1], read.cursor.context[-1])

    def test_cursor_is_conversation_and_transcript_scoped(self) -> None:
        first_transcript = self.make_transcript("scoped-a.jsonl", [user("Alpha")])
        second_transcript = self.make_transcript("scoped-b.jsonl", [user("Beta")])

        first = read_incremental(first_transcript, conversation_id="conv-scoped-a")
        first.save()
        self.assertTrue(cursor_path("conv-scoped-a", first_transcript).exists())
        self.assertFalse(cursor_path("conv-scoped-b", second_transcript).exists())

        other = read_incremental(second_transcript, conversation_id="conv-scoped-b")
        self.assertFalse(other.from_cursor)
        self.assertEqual(other.steps, [user("Beta")])

        # Reusing a conversation id with a different transcript must not reuse
        # the other transcript's cursor.
        mismatched = read_incremental(
            second_transcript, conversation_id="conv-scoped-a"
        )
        self.assertFalse(mismatched.from_cursor)
        self.assertEqual(mismatched.steps, [user("Beta")])

    def test_non_json_and_empty_lines_are_consumed(self) -> None:
        transcript = self.root / "transcripts" / "noise.jsonl"
        self.write_text(transcript, "not json\n\n" + json.dumps(user("Valid")) + "\n")
        read = read_incremental(transcript, conversation_id="conv-noise")
        self.assertEqual(read.steps, [user("Valid")])
        self.assertEqual(read.cursor.line_count, 3)
        self.assertEqual(read.cursor.offset, transcript.stat().st_size)


class TestEngineIncrementalIntegration(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine = HarnessEngine(self.workspace)

    def test_initial_stop_creates_cursor_and_idle_stop_is_skipped(self) -> None:
        transcript = self.make_transcript("engine-basic.jsonl", learning_session())
        first = self.engine.run_cycle("conv-engine-basic", transcript_path=transcript)
        self.assertEqual(first.status, "processed")
        state = cursor_path("conv-engine-basic", transcript)
        self.assertTrue(state.exists())

        second = self.engine.run_cycle("conv-engine-basic", transcript_path=transcript)
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.transcript_hash, first.transcript_hash)
        self.assertEqual(second.conversation_id, first.conversation_id)

    def test_append_is_analysed_as_context_plus_tail(self) -> None:
        transcript = self.make_transcript("engine-append.jsonl", failure_session())
        first = self.engine.run_cycle("conv-engine-append", transcript_path=transcript)
        self.assertEqual(first.status, "processed")
        self.assertEqual(first.unresolved_observations, 1)

        append_steps(transcript, recovery_steps())
        second = self.engine.run_cycle("conv-engine-append", transcript_path=transcript)
        self.assertEqual(second.status, "processed")
        self.assertEqual(second.contrastive_events_found, 1)
        self.assertEqual(second.verified_lessons, 1)

    def test_raw_steps_bypass_and_reset_the_cursor(self) -> None:
        transcript = self.make_transcript("engine-raw.jsonl", failure_session())
        first = self.engine.run_cycle("conv-engine-raw", transcript_path=transcript)
        self.assertEqual(first.status, "processed")

        append_steps(transcript, recovery_steps())
        raw = self.engine.run_cycle(
            "conv-engine-raw",
            transcript_path=transcript,
            raw_steps=[user("A raw test step")],
        )
        self.assertEqual(raw.status, "processed")
        self.assertEqual(raw.contrastive_events_found, 0)

        stored = load_cursor("conv-engine-raw", transcript)
        self.assertEqual(stored.offset, transcript.stat().st_size)
        follow_up = read_incremental(transcript, conversation_id="conv-engine-raw")
        self.assertTrue(follow_up.from_cursor)
        self.assertEqual(follow_up.new_steps, 0)

    def test_force_reparses_the_whole_transcript_and_resets_the_cursor(self) -> None:
        transcript = self.make_transcript("engine-force.jsonl", failure_session())
        self.engine.run_cycle("conv-engine-force", transcript_path=transcript)
        append_steps(transcript, recovery_steps())

        forced = self.engine.run_cycle(
            "conv-engine-force", transcript_path=transcript, force=True
        )
        self.assertEqual(forced.status, "processed")
        self.assertEqual(forced.contrastive_events_found, 1)
        self.assertEqual(forced.verified_lessons, 1)

        full = read_incremental(
            transcript, conversation_id="conv-engine-force", force_full=True
        )
        stored = load_cursor("conv-engine-force", transcript)
        self.assertEqual(stored.offset, transcript.stat().st_size)
        self.assertEqual(len(stored.context), len(full.steps))
        follow_up = read_incremental(
            transcript, conversation_id="conv-engine-force"
        )
        self.assertTrue(follow_up.from_cursor)
        self.assertEqual(follow_up.new_steps, 0)
        self.assertEqual(follow_up.steps, full.steps)

    def test_cursor_is_not_saved_when_memory_recording_fails(self) -> None:
        transcript = self.make_transcript("engine-failure.jsonl", learning_session())
        with mock.patch.object(
            self.engine.memory_store,
            "record_session",
            side_effect=RuntimeError("database unavailable"),
        ):
            result = self.engine.run_cycle(
                "conv-engine-failure", transcript_path=transcript
            )
        self.assertEqual(result.status, "processed")
        self.assertTrue(
            any("memory recording failed" in warning for warning in result.warnings)
        )
        self.assertFalse(cursor_path("conv-engine-failure", transcript).exists())

    def test_context_cap_is_not_surfaced_as_a_warning(self) -> None:
        transcript = self.make_transcript(
            "engine-cap.jsonl",
            [user(f"Step {index}") for index in range(CONTEXT_LIMIT_STEPS + 10)],
        )
        result = self.engine.run_cycle("conv-engine-cap", transcript_path=transcript)
        self.assertEqual(result.status, "processed")
        self.assertFalse(
            any("context" in warning.lower() for warning in result.warnings),
            result.warnings,
        )


if __name__ == "__main__":
    unittest.main()
