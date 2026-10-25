"""End-to-end stress tests for the Harness² hook surface.

The suite exercises the real ``scripts/hook_runner.py`` entry point against a
throw-away plugin/workspace. It is intentionally separate from the unit tests:
unit tests prove individual functions, this script proves that concurrent hook
processes, large transcripts, and repeated rule updates still behave.

Usage::

    python scripts/stress_test.py
    python scripts/stress_test.py --quick
    python scripts/stress_test.py --json
    python scripts/stress_test.py --keep
    python scripts/stress_test.py --plugin-root C:\\temp\\harness-stress

Exit code 0 means every scenario passed. The deployed plugin is never touched
unless you explicitly pass ``--plugin-root``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

CHECKOUT = Path(__file__).resolve().parent.parent
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))

from harness.core.agents_md_manager import (
    ANTIGRAVITY_RULE_CAP,
    AgentsMdManager,
    RuleSet,
)

RUNNER = CHECKOUT / "scripts" / "hook_runner.py"


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    seconds: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class StressContext:
    root: Path
    plugin_dir: Path
    gemini_dir: Path
    app_data: Path
    workspace: Path
    runner: Path
    timeout: float
    env: dict[str, str]

    def child_env(self) -> dict[str, str]:
        env = dict(self.env)
        env["HARNESS_PLUGIN_DIR"] = str(self.plugin_dir)
        env["HARNESS_GEMINI_CONFIG_DIR"] = str(self.gemini_dir)
        env["ANTIGRAVITY_APP_DATA_DIR"] = str(self.app_data)
        env["PYTHONIOENCODING"] = "utf-8"
        for key in ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_PROJECT_ID"):
            env.pop(key, None)
        return env

    def run_hook(
        self,
        event: str,
        payload: object | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if stdin is None:
            stdin = json.dumps(payload if payload is not None else {}, ensure_ascii=False)
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(self.runner), event],
            input=stdin,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=str(self.workspace),
            env=self.child_env(),
            timeout=self.timeout,
        )

    def journal_events(self) -> list[dict[str, Any]]:
        path = self.plugin_dir / "state" / "events.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def pending_files(self) -> list[Path]:
        """All per-conversation queues plus the legacy fallback queue."""
        state = self.plugin_dir / "state"
        files: list[Path] = []
        pending_dir = state / "pending"
        if pending_dir.is_dir():
            files.extend(sorted(pending_dir.glob("*.jsonl")))
        legacy = state / "pending_events.jsonl"
        if legacy.exists():
            files.append(legacy)
        return files

    def pending_text(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in self.pending_files()
        )

    def pending_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for path in self.pending_files():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssertionError(
                        f"corrupt pending line in {path.name}: {exc}: {line[:120]}"
                    )
                if not isinstance(value, dict):
                    raise AssertionError(
                        f"pending line in {path.name} is not an object: {line[:120]}"
                    )
                events.append(value)
        return events

    def reset_pending(self) -> None:
        state = self.plugin_dir / "state"
        candidates = [
            state / "pending_events.jsonl",
            state / "pending_events.jsonl.lock",
        ]
        pending_dir = state / "pending"
        if pending_dir.is_dir():
            candidates.extend(sorted(pending_dir.glob("*.jsonl")))
            candidates.extend(sorted(pending_dir.glob("*.lock")))
        for path in candidates:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def hook_errors(self) -> str:
        path = self.plugin_dir / "state" / "hook_errors.log"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")


def _check_hook_json(
    completed: subprocess.CompletedProcess[str],
    *,
    event: str,
    require: Callable[[dict[str, Any]], bool] = lambda value: True,
    description: str = "",
) -> dict[str, Any]:
    if completed.returncode != 0:
        raise AssertionError(
            f"{event} exited {completed.returncode}: {completed.stderr.strip()[:400]}"
        )
    if not completed.stdout.strip():
        raise AssertionError(f"{event} wrote no JSON to stdout")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{event} wrote invalid JSON: {exc}: {completed.stdout[:200]}")
    if not isinstance(value, dict):
        raise AssertionError(f"{event} returned {type(value).__name__}, not an object")
    if not require(value):
        raise AssertionError(f"{event} returned an unexpected payload: {value}")
    return value


def scenario_contract(ctx: StressContext) -> tuple[dict[str, Any], str]:
    cases: list[tuple[str, object | None, str | None, str]] = [
        (
            "session-start",
            {"conversationId": "stress-contract", "workspacePaths": [str(ctx.workspace)]},
            None,
            "valid session start",
        ),
        ("post-tool-use", {"toolCall": {"name": "run_command", "arguments": {}}}, None, "noop post"),
        (
            "post-tool-use",
            {
                "conversationId": "stress-contract",
                "workspacePaths": [str(ctx.workspace)],
                "toolCall": {
                    "name": "run_command",
                    "arguments": {"CommandLine": "python missing.py"},
                    "error": "ModuleNotFoundError: No module named 'contractpkg'",
                },
            },
            None,
            "failing post",
        ),
        ("stop", {}, None, "empty stop"),
        ("pre-invocation", {}, None, "empty pre-invocation"),
        (
            "pre-invocation",
            {"invocationNum": "not-a-number", "workspacePaths": [123, None]},
            None,
            "malformed pre-invocation",
        ),
        ("session-start", None, "this is not json", "malformed JSON"),
        ("session-start", None, "[]", "wrong JSON type"),
        ("stop", None, "{\"conversationId\": 7}", "partially malformed payload"),
        (
            "session-start",
            {"conversationId": {"unexpected": "object"}, "workspacePaths": [123, None]},
            None,
            "wrong field types",
        ),
        ("unknown-event", {}, None, "unknown event"),
    ]
    failures: list[str] = []
    timings: list[float] = []
    for event, payload, stdin, label in cases:
        started = time.monotonic()
        completed = ctx.run_hook(event, payload, stdin)
        timings.append(time.monotonic() - started)
        if event == "stop":
            requirement = lambda value: value == {"decision": "stop"}
        elif event == "pre-invocation":
            requirement = lambda value: isinstance(value.get("injectSteps"), list)
        elif event == "session-start" and label == "valid session start":
            requirement = lambda value: isinstance(value.get("injectSteps"), list) and bool(
                value["injectSteps"]
            )
        elif event == "session-start":
            requirement = lambda value: isinstance(value.get("injectSteps"), list)
        elif event == "post-tool-use":
            requirement = lambda value: value == {}
        else:
            requirement = lambda value: value == {}
        try:
            _check_hook_json(
                completed, event=event, require=requirement, description=label
            )
        except Exception as exc:
            failures.append(f"{label}: {exc}")
    ctx.reset_pending()
    if ctx.hook_errors():
        failures.append("hook_errors.log was written during the contract checks")
    if failures:
        raise AssertionError("; ".join(failures))
    details = {
        "cases": len(cases),
        "slowest_seconds": round(max(timings), 3),
    }
    return details, ""


def scenario_post_concurrency(
    ctx: StressContext, count: int
) -> tuple[dict[str, Any], str]:
    ctx.reset_pending()
    payloads = [
        {
            "conversationId": f"stress-post-{index}",
            "workspacePaths": [str(ctx.workspace)],
            "toolCall": {
                "name": "run_command",
                "arguments": {"CommandLine": f"python stress_{index}.py"},
                "error": f"ModuleNotFoundError: No module named 'stresspostpkg{index}'",
            },
        }
        for index in range(count)
    ]

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(ctx.run_hook, "post-tool-use", payload) for payload in payloads
        ]
        completed = [future.result() for future in futures]
    elapsed = time.monotonic() - started

    for result in completed:
        _check_hook_json(
            result, event="post-tool-use", require=lambda value: value == {}
        )

    if not ctx.pending_files():
        raise AssertionError("no pending observation queue was created")
    events = ctx.pending_events()

    if len(events) != count:
        raise AssertionError(f"expected {count} events, found {len(events)}")
    ids = {str(event.get("id")) for event in events}
    if len(ids) != count:
        raise AssertionError(f"expected {count} unique ids, found {len(ids)}")
    errors = {str(event.get("error")) for event in events}
    expected = {
        f"ModuleNotFoundError: No module named 'stresspostpkg{index}'"
        for index in range(count)
    }
    if errors != expected:
        raise AssertionError("one or more concurrent PostToolUse events were lost")
    if ctx.hook_errors():
        raise AssertionError("hook_errors.log was written during PostToolUse stress")

    return (
        {
            "processes": count,
            "events": len(events),
            "unique_ids": len(ids),
            "seconds": round(elapsed, 2),
        },
        "",
    )


def scenario_stop_concurrency(
    ctx: StressContext, count: int
) -> tuple[dict[str, Any], str]:
    ctx.reset_pending()
    for index in range(count):
        completed = ctx.run_hook(
            "post-tool-use",
            {
                "conversationId": f"stress-stop-{index}",
                "workspacePaths": [str(ctx.workspace)],
                "toolCall": {
                    "name": "run_command",
                    "arguments": {"CommandLine": f"python stop_{index}.py"},
                    "error": f"ModuleNotFoundError: No module named 'stressstoppkg{index}'",
                },
            },
        )
        _check_hook_json(
            completed, event="post-tool-use", require=lambda value: value == {}
        )

    payloads = [
        {
            "conversationId": f"stress-stop-{index}",
            "workspacePaths": [str(ctx.workspace)],
        }
        for index in range(count)
    ]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(ctx.run_hook, "stop", payload) for payload in payloads]
        completed = [future.result() for future in futures]
    elapsed = time.monotonic() - started

    for result in completed:
        _check_hook_json(
            result,
            event="stop",
            require=lambda value: value == {"decision": "stop"},
        )

    if ctx.pending_text().strip():
        raise AssertionError("concurrent Stop cycles left observations behind")

    results = [
        event
        for event in ctx.journal_events()
        if event.get("event") == "Stop:result"
        and str(event.get("payload", {}).get("conversation_id", "")).startswith(
            "stress-stop-"
        )
    ]
    statuses: dict[str, int] = {}
    for event in results:
        status = str(event.get("payload", {}).get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    if statuses.get("processed", 0) != count:
        raise AssertionError(
            f"expected {count} processed Stop cycles, got {statuses}"
        )

    db_path = ctx.plugin_dir / "memory" / "harness_memory.db"
    if not db_path.exists():
        raise AssertionError("memory database was not created")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        stored = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE error_pattern LIKE ? AND verified = 0",
            ("%stressstoppkg%",),
        ).fetchone()[0]
    finally:
        conn.close()
    if stored != count:
        raise AssertionError(f"expected {count} stored candidates, found {stored}")
    if ctx.hook_errors():
        raise AssertionError("hook_errors.log was written during Stop stress")

    return (
        {
            "processes": count,
            "processed": statuses.get("processed", 0),
            "candidates": stored,
            "seconds": round(elapsed, 2),
        },
        "",
    )


def _learning_steps() -> list[dict[str, Any]]:
    return [
        {
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "content": "Install pytest and run the test suite",
        },
        {
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {"name": "run_command", "arguments": {"CommandLine": "pytest -q"}}
            ],
        },
        {
            "type": "CORTEX_STEP_TYPE_OUTPUT",
            "content": "ModuleNotFoundError: No module named 'pytest'",
            "status": "ERROR",
        },
        {
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {"name": "run_command", "arguments": {"CommandLine": "pip install pytest"}}
            ],
        },
        {
            "type": "CORTEX_STEP_TYPE_OUTPUT",
            "content": "Successfully installed pytest",
            "status": "DONE",
        },
        {
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {"name": "run_command", "arguments": {"CommandLine": "pytest -q"}}
            ],
        },
        {
            "type": "CORTEX_STEP_TYPE_OUTPUT",
            "content": "5 passed",
            "status": "DONE",
        },
    ]


def scenario_idempotence(ctx: StressContext) -> tuple[dict[str, Any], str]:
    workspace = ctx.root / "idempotent-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    transcript = ctx.root / "transcripts" / "idempotent.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "\n".join(json.dumps(step) for step in _learning_steps()) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    payload = {
        "conversationId": "stress-idempotent",
        "workspacePaths": [str(workspace)],
        "transcriptPath": str(transcript),
    }

    first = ctx.run_hook("stop", payload)
    second = ctx.run_hook("stop", payload)
    for result in (first, second):
        _check_hook_json(
            result,
            event="stop",
            require=lambda value: value == {"decision": "stop"},
        )

    posted = ctx.run_hook(
        "post-tool-use",
        {
            "conversationId": "stress-idempotent",
            "workspacePaths": [str(workspace)],
            "toolCall": {
                "name": "run_command",
                "error": "ModuleNotFoundError: No module named 'idempotencepkg'",
            },
        },
    )
    _check_hook_json(posted, event="post-tool-use", require=lambda value: value == {})
    third = ctx.run_hook("stop", payload)
    _check_hook_json(
        third, event="stop", require=lambda value: value == {"decision": "stop"}
    )

    statuses = [
        str(event.get("payload", {}).get("status", ""))
        for event in ctx.journal_events()
        if event.get("event") == "Stop:result"
        and event.get("payload", {}).get("conversation_id") == "stress-idempotent"
    ]
    if statuses != ["processed", "skipped", "skipped"]:
        raise AssertionError(f"unexpected idempotence statuses: {statuses}")

    if "idempotencepkg" not in ctx.pending_text():
        raise AssertionError("a skipped Stop discarded its pending observation")

    rules = workspace / "AGENTS.md"
    if not rules.exists():
        raise AssertionError("the verified session did not produce workspace rules")
    text = rules.read_text(encoding="utf-8")
    if text.count("HARNESS:AUTONOMOUS_RULES_START") != 1:
        raise AssertionError("rules were duplicated across Stop cycles")
    skill = workspace / ".agents" / "skills" / "run-python-tests" / "SKILL.md"
    if not skill.exists():
        raise AssertionError("the verified session did not produce a skill")
    if ctx.hook_errors():
        raise AssertionError("hook_errors.log was written during idempotence checks")

    return (
        {
            "statuses": statuses,
            "pending_retained": True,
            "rules_bytes": rules.stat().st_size,
            "skill": skill.parent.name,
        },
        "",
    )


def scenario_large_transcript(
    ctx: StressContext, loops: int, expect_truncation: bool
) -> tuple[dict[str, Any], str]:
    workspace = ctx.root / "large-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    transcript = ctx.root / "transcripts" / "large.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    with transcript.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "Run a long deterministic workload",
                }
            )
            + "\n"
        )
        for index in range(loops):
            handle.write(
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [
                            {
                                "name": "run_command",
                                "arguments": {
                                    "CommandLine": f'python -c "print({index})"'
                                },
                            }
                        ],
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "CORTEX_STEP_TYPE_OUTPUT",
                        "content": f"step {index} finished",
                        "status": "DONE",
                    }
                )
                + "\n"
            )
    generation_seconds = time.monotonic() - started

    completed = ctx.run_hook(
        "stop",
        {
            "conversationId": "stress-large",
            "workspacePaths": [str(workspace)],
            "transcriptPath": str(transcript),
        },
    )
    _check_hook_json(
        completed, event="stop", require=lambda value: value == {"decision": "stop"}
    )

    result = next(
        (
            event
            for event in reversed(ctx.journal_events())
            if event.get("event") == "Stop:result"
            and event.get("payload", {}).get("conversation_id") == "stress-large"
        ),
        None,
    )
    if not result:
        raise AssertionError("large transcript produced no Stop:result journal entry")
    payload = result.get("payload", {})
    if payload.get("status") != "processed":
        raise AssertionError(f"large transcript status was {payload.get('status')!r}")
    warnings = " | ".join(str(item) for item in payload.get("warnings", []))
    if expect_truncation and "analysis limit" not in warnings:
        raise AssertionError(f"expected a truncation warning, got: {warnings!r}")
    if not expect_truncation and warnings:
        raise AssertionError(f"unexpected warning for a bounded transcript: {warnings!r}")

    db_path = ctx.plugin_dir / "memory" / "harness_memory.db"
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        session = conn.execute(
            "SELECT transcript_hash FROM sessions WHERE conversation_id = ?",
            ("stress-large",),
        ).fetchone()
    finally:
        conn.close()
    if not session or not session[0]:
        raise AssertionError("large transcript session was not stored")
    if ctx.hook_errors():
        raise AssertionError("hook_errors.log was written during the large transcript")

    return (
        {
            "loops": loops,
            "bytes": transcript.stat().st_size,
            "lines": loops * 2 + 1,
            "generation_seconds": round(generation_seconds, 2),
            "truncated": expect_truncation,
        },
        "",
    )


def scenario_rule_budget(
    ctx: StressContext, attempts: int
) -> tuple[dict[str, Any], str]:
    workspace = ctx.root / "budget-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    rules = workspace / "AGENTS.md"
    rules.write_text("# User Rules\n\n- Keep this manual note.\n", encoding="utf-8")

    manager = AgentsMdManager(workspace)
    directives = [
        (
            f"Always document quux{index:04d}alpha quux{index:04d}bravo "
            f"quux{index:04d}charlie quux{index:04d}delta quux{index:04d}echo "
            "in the shared local runbook."
        )
        for index in range(attempts)
    ]
    ruleset = RuleSet(directives=directives)
    started = time.monotonic()
    # One apply is enough to exercise the same render/trim loop used by every
    # rule update; repeating update_rules 400 times only tests file I/O.
    results = manager.update_rules(
        contrastive_events=[],
        user_constraints=list(ruleset.directives),
        environment_quirks=[],
        target="workspace",
    )
    last = results["workspace"]
    elapsed = time.monotonic() - started

    size = rules.stat().st_size
    text = rules.read_text(encoding="utf-8")
    if size > manager.max_bytes:
        raise AssertionError(
            f"rules grew to {size} bytes, above the {manager.max_bytes}-byte managed budget"
        )
    if size > ANTIGRAVITY_RULE_CAP:
        raise AssertionError(
            f"rules grew to {size} bytes, above the {ANTIGRAVITY_RULE_CAP}-byte cap"
        )
    if text.count("HARNESS:AUTONOMOUS_RULES_START") != 1:
        raise AssertionError("managed rule block is missing or duplicated")
    if text.count("HARNESS:AUTONOMOUS_RULES_END") != 1:
        raise AssertionError("managed rule block has no unique end marker")
    if "Keep this manual note." not in text:
        raise AssertionError("user-authored rule content was lost")
    if last.get("kept", attempts) >= attempts:
        raise AssertionError("the budget did not trim any of the simulated rules")

    return (
        {
            "attempts": attempts,
            "kept": last.get("kept"),
            "bytes": size,
            "budget_bytes": manager.max_bytes,
            "cap_bytes": ANTIGRAVITY_RULE_CAP,
            "seconds": round(elapsed, 2),
        },
        "",
    )


def scenario_integrity(ctx: StressContext) -> tuple[dict[str, Any], str]:
    db_path = ctx.plugin_dir / "memory" / "harness_memory.db"
    if not db_path.exists():
        raise AssertionError("memory database is missing")
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise AssertionError(f"SQLite quick_check failed: {check!r}")
        lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        candidates = conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE verified = 0"
        ).fetchone()[0]
        fts_present = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lessons_fts'"
            ).fetchone()
        )
        fts_rows = None
        fts_hit = None
        if fts_present:
            fts_rows = conn.execute("SELECT COUNT(*) FROM lessons_fts").fetchone()[0]
            if fts_rows != lessons:
                raise AssertionError(
                    f"FTS index is out of sync: {fts_rows} rows for {lessons} lessons"
                )
            try:
                fts_hit = conn.execute(
                    "SELECT COUNT(*) FROM lessons_fts WHERE lessons_fts MATCH ?",
                    ("stressstoppkg0",),
                ).fetchone()[0]
            except sqlite3.DatabaseError:
                # MATCH syntax is only available when FTS5 is compiled in.
                fts_hit = None
    finally:
        conn.close()

    journal_path = ctx.plugin_dir / "state" / "events.jsonl"
    journal_lines = 0
    journal_bad = 0
    if journal_path.exists():
        for line in journal_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if not line.strip():
                continue
            journal_lines += 1
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    journal_bad += 1
            except json.JSONDecodeError:
                journal_bad += 1
    if journal_bad:
        raise AssertionError(f"{journal_bad} corrupt journal line(s)")
    if journal_lines == 0:
        raise AssertionError("event journal is empty")

    # Raises AssertionError on any corrupt pending line.
    ctx.pending_events()
    if ctx.hook_errors():
        raise AssertionError("hook_errors.log is not empty")

    return (
        {
            "sqlite": "ok",
            "lessons": lessons,
            "candidates": candidates,
            "fts": bool(fts_present),
            "fts_rows": fts_rows,
            "fts_hit": fts_hit,
            "journal_lines": journal_lines,
            "journal_corrupt": journal_bad,
        },
        "",
    )


def _sizes(args: argparse.Namespace) -> dict[str, int]:
    if args.quick:
        return {"post": 8, "stop": 4, "loops": 4_000, "budget": 240}
    if args.full:
        return {"post": 100, "stop": 20, "loops": 80_000, "budget": 280}
    return {"post": 32, "stop": 12, "loops": 52_000, "budget": 260}


def _prepare(
    runner: Path, plugin_root: Path | None, workspace_root: Path | None
) -> tuple[StressContext, Path, bool]:
    if plugin_root is not None:
        root = plugin_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        owned = False
    else:
        root = Path(tempfile.mkdtemp(prefix="harness-stress-"))
        owned = True

    plugin_dir = root / "plugin"
    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        raise SystemExit(
            f"refusing to reuse a non-empty stress data root: {plugin_dir}\n"
            "Choose an empty --plugin-root directory or remove that folder first."
        )
    gemini_dir = root / "gemini"
    app_data = root / "appdata"
    workspace = workspace_root.resolve() if workspace_root else root / "workspace"
    for path in (plugin_dir, gemini_dir, app_data, workspace):
        path.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    ctx = StressContext(
        root=root,
        plugin_dir=plugin_dir,
        gemini_dir=gemini_dir,
        app_data=app_data,
        workspace=workspace,
        runner=runner,
        timeout=240.0,
        env=env,
    )
    return ctx, root, owned


def _run(
    name: str, function: Callable[[], tuple[dict[str, Any], str]]
) -> ScenarioResult:
    started = time.monotonic()
    try:
        details, error = function()
        return ScenarioResult(
            name=name,
            passed=not error,
            seconds=round(time.monotonic() - started, 2),
            details=details,
            error=error,
        )
    except Exception:
        return ScenarioResult(
            name=name,
            passed=False,
            seconds=round(time.monotonic() - started, 2),
            error=traceback.format_exc(limit=3),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="small CI-friendly run")
    parser.add_argument("--full", action="store_true", help="larger concurrency and transcripts")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data root")
    parser.add_argument(
        "--plugin-root",
        type=Path,
        help="create <DIR>/plugin as the stress data root instead of a temporary one; data is kept",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="use this workspace instead of a temporary one (pollutes it)",
    )
    parser.add_argument(
        "--runner",
        type=Path,
        default=RUNNER,
        help="hook_runner.py to exercise (defaults to this checkout)",
    )
    args = parser.parse_args(argv)
    if args.quick and args.full:
        parser.error("--quick and --full are mutually exclusive")

    sizes = _sizes(args)
    runner = args.runner.expanduser().resolve()
    if not runner.exists():
        print(f"hook runner not found: {runner}", file=sys.stderr)
        return 2

    started = time.monotonic()
    ctx, root, owned = _prepare(runner, args.plugin_root, args.workspace)
    scenarios: list[ScenarioResult] = []
    try:
        scenarios.append(
            _run("hook contract and malformed payloads", lambda: scenario_contract(ctx))
        )
        scenarios.append(
            _run(
                "concurrent PostToolUse",
                lambda: scenario_post_concurrency(ctx, sizes["post"]),
            )
        )
        scenarios.append(
            _run(
                "concurrent Stop cycles",
                lambda: scenario_stop_concurrency(ctx, sizes["stop"]),
            )
        )
        scenarios.append(
            _run("idempotence and pending retention", lambda: scenario_idempotence(ctx))
        )
        scenarios.append(
            _run(
                "large transcript",
                lambda: scenario_large_transcript(
                    ctx, sizes["loops"], expect_truncation=sizes["loops"] * 2 + 1 > 100_000
                ),
            )
        )
        scenarios.append(
            _run(
                "rule size budget",
                lambda: scenario_rule_budget(ctx, sizes["budget"]),
            )
        )
        scenarios.append(
            _run("database and journal integrity", lambda: scenario_integrity(ctx))
        )
    finally:
        failed = any(not scenario.passed for scenario in scenarios)
        elapsed = round(time.monotonic() - started, 2)
        report = {
            "ok": bool(scenarios) and not failed,
            "quick": args.quick,
            "full": args.full,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "plugin_root": str(ctx.plugin_dir),
            "workspace": str(ctx.workspace),
            "artifacts_kept": bool(args.keep or args.plugin_root or args.workspace),
            "total_seconds": elapsed,
            "scenarios": [asdict(scenario) for scenario in scenarios],
        }

        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print("Harness² stress test")
            print(f"  plugin data: {report['plugin_root']}")
            print(f"  workspace:   {report['workspace']}")
            print("")
            for scenario in scenarios:
                state = "PASS" if scenario.passed else "FAIL"
                print(f"  [{state}] {scenario.name} ({scenario.seconds:.2f}s)")
                if scenario.details:
                    summary = ", ".join(
                        f"{key}={value}" for key, value in scenario.details.items()
                    )
                    print(f"         {summary}")
                if scenario.error:
                    for line in scenario.error.strip().splitlines():
                        print(f"         {line}")
            print("")
            print(
                f"  {sum(item.passed for item in scenarios)}/{len(scenarios)} passed "
                f"in {elapsed:.2f}s"
            )
            if report["artifacts_kept"]:
                print(f"  artifacts retained at {ctx.root}")
            else:
                print("  artifacts removed (use --keep to inspect them)")

        if owned and not args.keep and not args.plugin_root and not args.workspace:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if all(scenario.passed for scenario in scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
