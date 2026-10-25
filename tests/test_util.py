"""Tests for atomic I/O, hashing, duplicate detection, and locking."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.core.util import (
    append_jsonl,
    atomic_write_text,
    file_lock,
    hash_steps,
    jaccard,
    normalize_for_compare,
    one_line,
    read_json,
    read_text_lossless,
    sha256_text,
    truncate,
    write_json,
)


class TestUtil(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="harness-util-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_normalize_removes_volatile_values_but_keeps_code_when_asked(self) -> None:
        first = "Run `pytest -q` after 2026-10-23T10:00:00Z (task 42)"
        second = "Run `pytest -q` after 2026-10-24T11:00:00Z (task 99)"
        self.assertEqual(normalize_for_compare(first), normalize_for_compare(second))
        self.assertNotIn("pytest", normalize_for_compare(first))
        self.assertIn("pytest", normalize_for_compare(first, keep_code=True))

        left = "Run `pytest -q` now"
        right = "Run `pytest -x` now"
        self.assertEqual(jaccard(left, right), 1.0)
        self.assertLess(jaccard(left, right, keep_code=True), 1.0)

    def test_atomic_write_and_json_round_trip(self) -> None:
        path = self.root / "nested" / "value.txt"
        atomic_write_text(path, "hello\n")
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(read_json(path, default={"fallback": True}), {"fallback": True})

        json_path = self.root / "nested" / "value.json"
        write_json(json_path, {"ü": "✓"})
        self.assertEqual(read_json(json_path), {"ü": "✓"})

    def test_append_jsonl_rotates_at_limit(self) -> None:
        path = self.root / "events.jsonl"
        append_jsonl(path, {"n": 1}, max_bytes=10_000)
        self.assertTrue(path.exists())
        append_jsonl(path, {"n": 2}, max_bytes=1)
        self.assertTrue(path.with_suffix(".jsonl.1").exists())
        remaining = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(remaining, [{"n": 2}])

    def test_file_lock_excludes_a_second_holder(self) -> None:
        path = self.root / "cycle.lock"
        with file_lock(path) as first:
            self.assertTrue(first)
            with file_lock(path, timeout=0.05) as second:
                self.assertFalse(second)
        self.assertFalse(path.exists())
        with file_lock(path, timeout=0.05) as third:
            self.assertTrue(third)

    def test_lock_release_does_not_delete_a_successor_lock(self) -> None:
        path = self.root / "successor.lock"
        with file_lock(path) as acquired:
            self.assertTrue(acquired)
            path.write_text("successor-token\n", encoding="utf-8")
        self.assertTrue(path.exists())
        self.assertIn("successor-token", path.read_text(encoding="utf-8"))

    def test_file_lock_retries_a_transient_oserror(self) -> None:
        path = self.root / "transient.lock"
        real_open = os.open
        calls = {"count": 0}

        def flaky_open(*args: object, **kwargs: object) -> int:
            if calls["count"] == 0:
                calls["count"] += 1
                raise PermissionError("sharing violation")
            return real_open(*args, **kwargs)

        with mock.patch("harness.core.util.os.open", side_effect=flaky_open):
            with file_lock(path, timeout=1.0) as acquired:
                self.assertTrue(acquired)
        self.assertEqual(calls["count"], 1)
        self.assertFalse(path.exists())

    def test_lossless_reader_preserves_utf16_and_rejects_invalid_bytes(self) -> None:
        path = self.root / "utf16.txt"
        raw = "# User\r\n\r\n- Note.\r\n".encode("utf-16")
        path.write_bytes(raw)
        text, encoding, newline = read_text_lossless(path)
        self.assertEqual(text, "# User\n\n- Note.\n")
        self.assertEqual(encoding, "utf-16")
        self.assertEqual(newline, "\r\n")
        atomic_write_text(path, text, encoding=encoding, newline=newline)
        self.assertEqual(path.read_bytes(), raw)

        invalid = self.root / "invalid.txt"
        invalid.write_bytes(b"valid prefix\n\xff\xfe\n")
        with self.assertRaises(UnicodeDecodeError):
            read_text_lossless(invalid)

    def test_truncate_and_one_line(self) -> None:
        self.assertEqual(one_line("a\n\t b   c"), "a b c")
        self.assertEqual(truncate("abcdef", 5), "abcd…")
        self.assertEqual(truncate("abc", 5), "abc")
        self.assertEqual(sha256_text("a"), sha256_text("a"))
        self.assertNotEqual(sha256_text("a"), sha256_text("b"))
        self.assertEqual(hash_steps([{"b": 1, "a": 2}]), hash_steps([{"a": 2, "b": 1}]))


if __name__ == "__main__":
    unittest.main()
