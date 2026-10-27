"""
Antigravity lifecycle hook handlers.

Contract (from the Antigravity hook documentation):

* commands run with the plugin directory as cwd;
* ``SessionStart`` returns ``{"injectSteps": [...]}``;
* ``PreInvocation`` returns ``{"injectSteps": [...]}`` or an empty list;
* ``PostToolUse`` returns ``{}``;
* ``Stop`` returns ``{"decision": "stop"}``;
* stdout must contain JSON only.

Everything here is defensive. A harness failure must never interrupt the agent,
so errors are written to ``state/hook_errors.log`` and a valid payload is still
returned.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .core.config import (
    bind_conversation_workspace,
    get_bound_workspace,
    get_event_journal_path,
    get_hook_error_log_path,
    get_state_dir,
    safe_conversation_id,
)
from .core.redaction import redact, redact_mapping
from .core.util import (
    append_jsonl,
    atomic_write_text,
    file_lock,
    one_line,
    sha256_text,
    utc_iso,
)

MAX_ERROR_LOG_BYTES = 500_000
# Stop hooks may arrive from several conversations at once. The cycle lock is
# intentionally held a little longer than a normal cycle so a second session is
# queued instead of silently dropped as "busy".
STOP_LOCK_TIMEOUT = 30.0
PENDING_LOCK_TIMEOUT = 10.0
PENDING_LOCK_STALE_AFTER = 30.0
# Each conversation has its own queue, so one session can no longer starve
# another. Reads stay bounded; only the newest slice is ever analysed.
PENDING_READ_LINES = 500


def _pending_key(conversation_id: str = "") -> str:
    return safe_conversation_id(conversation_id or "") or "global"


def _pending_path(conversation_id: str = "") -> Path:
    return get_state_dir() / "pending" / f"{_pending_key(conversation_id)}.jsonl"


def _pending_lock_path(conversation_id: str = "") -> Path:
    path = _pending_path(conversation_id)
    return path.with_name(path.name + ".lock")


def _legacy_pending_path() -> Path:
    """Old single-queue location, kept readable for mixed-version upgrades."""
    return get_state_dir() / "pending_events.jsonl"


def _legacy_pending_lock_path() -> Path:
    path = _legacy_pending_path()
    return path.with_name(path.name + ".lock")


def _payload_workspace(payload: dict[str, Any]) -> Path | None:
    raw = payload.get("workspacePaths") or payload.get("workspacePath") or payload.get("cwd")
    if isinstance(raw, str):
        choices = [raw]
    elif isinstance(raw, (list, tuple)):
        choices = [str(item) for item in raw if item]
    else:
        choices = []
    for choice in choices:
        path = Path(choice).expanduser()
        if path.exists() and path.is_dir():
            return path
    return None


def _resolve_workspace(payload: dict[str, Any]) -> Path | None:
    """Workspace for this hook call: payload first, then the conversation binding.

    The process working directory is never used. Falling back to it attributes a
    conversation to whatever folder the hook happened to run in, which is how
    project lessons leaked into another project's ``AGENTS.md``.
    """
    workspace = _payload_workspace(payload)
    conversation_id = _conversation_id(payload)
    if workspace:
        bind_conversation_workspace(conversation_id, workspace)
        return workspace
    return get_bound_workspace(conversation_id)


def _conversation_id(payload: dict[str, Any]) -> str:
    value = (
        payload.get("conversationId")
        or payload.get("conversation_id")
        or payload.get("sessionId")
        or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    )
    return safe_conversation_id(str(value or ""))


def _transcript_path(payload: dict[str, Any]) -> str:
    for key in ("transcriptPath", "transcript_path", "transcript"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _log_note(context: str, message: str) -> None:
    try:
        path = get_hook_error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_ERROR_LOG_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.replace(rotated)
            except OSError:
                pass
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[{utc_iso()}] {context}\n")
            handle.write(one_line(message, 1000) + "\n\n")
    except Exception:
        pass


def _log_error(context: str, exc: BaseException) -> None:
    try:
        path = get_hook_error_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_ERROR_LOG_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.replace(rotated)
            except OSError:
                pass
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[{utc_iso()}] {context}\n")
            handle.write(traceback.format_exc(limit=8))
            handle.write("\n")
    except Exception:
        pass


def _journal(event: str, payload: dict[str, Any]) -> None:
    try:
        append_jsonl(
            get_event_journal_path(),
            {
                "timestamp": utc_iso(),
                "event": event,
                "conversation_id": _conversation_id(payload),
                "payload": redact_mapping(payload),
            },
            max_bytes=2_000_000,
        )
    except Exception:
        pass


def _tool_call(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("toolCall", "tool_call", "tool"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            return {"name": value}
    data = payload.get("data")
    if isinstance(data, dict):
        return _tool_call(data)
    return {}


def _tool_error(payload: dict[str, Any], tool_call: dict[str, Any]) -> str:
    candidates: list[Any] = [
        payload.get("error"),
        payload.get("errorMessage"),
        payload.get("failure"),
        tool_call.get("error"),
        tool_call.get("errorMessage"),
    ]
    for key in ("toolResult", "result", "response"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.extend([value.get("error"), value.get("errorMessage")])
    for candidate in candidates:
        if candidate:
            return one_line(redact(str(candidate)), 500)
    status = str(payload.get("status") or tool_call.get("status") or "").lower()
    if status in ("error", "failed", "failure", "timeout", "cancelled", "canceled"):
        return one_line(redact(f"tool status: {status}"), 500)
    if payload.get("success") is False:
        return "tool reported success=false"
    return ""


def _matches_scope(
    event: dict[str, Any],
    conversation_id: str,
    workspace_str: str,
) -> bool:
    """Exact pending-event scope: never consume another session's failures."""
    event_conversation = str(event.get("conversation_id") or "")
    if conversation_id:
        if event_conversation not in ("", conversation_id):
            return False
    elif event_conversation:
        return False
    event_workspace = str(event.get("workspace") or "")
    if workspace_str:
        if event_workspace not in ("", workspace_str):
            return False
    elif event_workspace:
        return False
    return True


def _read_pending_lines(path: Path, lock_path: Path) -> list[str]:
    """Read the newest queue lines under the queue's own lock."""
    if not path.exists():
        return []
    try:
        with file_lock(
            lock_path,
            timeout=2.0,
            stale_after=PENDING_LOCK_STALE_AFTER,
        ) as acquired:
            if not acquired:
                # Leave the events in place for the next Stop instead of
                # consuming a partially written queue.
                _log_note(
                    "Stop:pending",
                    "lock timeout after 2.0s; pending queue not read",
                )
                return []
            return path.read_text(encoding="utf-8", errors="replace").splitlines()[
                -PENDING_READ_LINES:
            ]
    except OSError:
        return []


def _pending_events(conversation_id: str, workspace: Path | None, limit: int = 25) -> list[dict[str, Any]]:
    conversation_id = safe_conversation_id(conversation_id or "")
    workspace_str = str(workspace) if workspace else ""
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Read the legacy queue first so the limit favours the newer per-session
    # queue, and dedupe by event id across both.
    queues = (
        (_legacy_pending_path(), _legacy_pending_lock_path()),
        (_pending_path(conversation_id), _pending_lock_path(conversation_id)),
    )
    for path, lock_path in queues:
        for line in _read_pending_lines(path, lock_path):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if not _matches_scope(event, conversation_id, workspace_str):
                continue
            event_id = str(event.get("id") or "")
            if event_id:
                if event_id in seen:
                    continue
                seen.add(event_id)
            events.append(event)
    events.sort(key=lambda item: str(item.get("timestamp") or ""))
    return events[-limit:]


def _append_pending(event: dict[str, Any]) -> None:
    conversation_id = str(event.get("conversation_id") or "")
    path = _pending_path(conversation_id)
    try:
        with file_lock(
            _pending_lock_path(conversation_id),
            timeout=PENDING_LOCK_TIMEOUT,
            stale_after=PENDING_LOCK_STALE_AFTER,
        ) as acquired:
            if not acquired:
                # A dropped observation is not agent-fatal, but it must be
                # visible. Silent loss is what made this bug hard to find.
                _log_note(
                    "PostToolUse:pending",
                    f"lock timeout after {PENDING_LOCK_TIMEOUT}s; event dropped",
                )
                return
            if event.get("id") and path.exists():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-PENDING_READ_LINES:]:
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(existing, dict) and existing.get("id") == event.get("id"):
                        return
            append_jsonl(path, event, max_bytes=1_000_000, locked=False)
    except Exception as exc:
        _log_error("PostToolUse:pending", exc)


def _clear_pending_file(
    path: Path,
    lock_path: Path,
    conversation_id: str,
    workspace_str: str,
    consumed_ids: set[str] | None,
) -> None:
    if not path.exists():
        return
    retained: list[str] = []
    try:
        with file_lock(
            lock_path,
            timeout=PENDING_LOCK_TIMEOUT,
            stale_after=PENDING_LOCK_STALE_AFTER,
        ) as acquired:
            if not acquired:
                return
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if _matches_scope(event, conversation_id, workspace_str):
                    event_id = str(event.get("id") or "")
                    if consumed_ids is None or not event_id or event_id in consumed_ids:
                        continue
                retained.append(line)
            atomic_write_text(path, "\n".join(retained) + ("\n" if retained else ""))
    except OSError:
        pass


def _clear_pending(
    conversation_id: str,
    workspace: Path | None,
    consumed_ids: set[str] | None = None,
) -> None:
    """Drop only the observations consumed by a completed Stop cycle.

    Events appended while the cycle was running carry ids that are not in
    ``consumed_ids`` and must survive for the next Stop. The legacy queue is
    cleaned with the same rule so a mixed-version rollout loses nothing.
    """
    conversation_id = safe_conversation_id(conversation_id or "")
    workspace_str = str(workspace) if workspace else ""
    for path, lock_path in (
        (_pending_path(conversation_id), _pending_lock_path(conversation_id)),
        (_legacy_pending_path(), _legacy_pending_lock_path()),
    ):
        _clear_pending_file(
            path,
            lock_path,
            conversation_id,
            workspace_str,
            consumed_ids,
        )


# ------------------------------------------------------------------ entry points
def handle_session_start(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    _journal("SessionStart", payload)
    try:
        # Imported lazily: SessionStart is rare, and the PostToolUse path must
        # not pay for the loader, analyzer, and SQLite imports.
        from .core.loader import SessionLoader

        workspace = _resolve_workspace(payload)
        loader = SessionLoader(workspace)
        return loader.generate_session_start_payload(payload, workspace)
    except Exception as exc:
        _log_error("SessionStart", exc)
        return {"injectSteps": []}


def handle_post_tool_use(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    try:
        tool_call = _tool_call(payload)
        tool_name = str(
            tool_call.get("name")
            or tool_call.get("toolName")
            or payload.get("toolName")
            or ""
        ).strip()
        error = _tool_error(payload, tool_call) if tool_name else ""
        if error:
            conversation_id = _conversation_id(payload)
            workspace = _resolve_workspace(payload)
            args = tool_call.get("arguments") or tool_call.get("args") or {}
            command = ""
            if isinstance(args, dict):
                for key in ("CommandLine", "command", "cmd"):
                    value = args.get(key)
                    if isinstance(value, str) and value.strip():
                        command = one_line(redact(value), 300)
                        break
            event = {
                "id": sha256_text(
                    f"{conversation_id}|{workspace or ''}|{tool_name}|{command}|{error}"
                )[:24],
                "timestamp": utc_iso(),
                "conversation_id": conversation_id,
                "workspace": str(workspace) if workspace else "",
                "tool": tool_name[:80],
                "command": command,
                "error": error,
                "signature": one_line(error.lower(), 160),
            }
            _append_pending(event)
            _journal("PostToolUse:error", event)
        else:
            _journal("PostToolUse", {"tool": tool_name, "conversation_id": _conversation_id(payload)})
    except Exception as exc:
        _log_error("PostToolUse", exc)
    return {}


def handle_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    _journal("Stop", payload)
    try:
        from .core.engine import HarnessEngine

        conversation_id = _conversation_id(payload)
        workspace = _resolve_workspace(payload)
        transcript = _transcript_path(payload)
        pending = _pending_events(conversation_id, workspace)
        consumed_ids = {str(event.get("id")) for event in pending if event.get("id")}
        engine = HarnessEngine(workspace)
        result = engine.run_cycle(
            conversation_id=conversation_id,
            transcript_path=transcript or None,
            workspace_path=workspace,
            lock_timeout=STOP_LOCK_TIMEOUT,
            pending_events=pending,
        )
        if result.status == "processed":
            # A skipped session has not consumed its pending observations. Keep
            # them so a later transcript update can still record them.
            _clear_pending(conversation_id, workspace, consumed_ids)
        _journal(
            "Stop:result",
            {
                "conversation_id": result.conversation_id,
                "status": result.status,
                "summary": result.summary,
                "warnings": result.warnings,
            },
        )
    except Exception as exc:
        _log_error("Stop", exc)
    return {"decision": "stop"}


def handle_pre_invocation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    try:
        raw_invocation = payload.get("invocationNum", payload.get("invocation_num", 0))
        raw_steps = payload.get("initialNumSteps", payload.get("initial_num_steps", 0))
        try:
            invocation = int(raw_invocation)
        except (TypeError, ValueError):
            invocation = 0
        try:
            initial_steps = int(raw_steps)
        except (TypeError, ValueError):
            initial_steps = 0
        from .core.retrieval import build_pre_invocation_payload

        output = build_pre_invocation_payload(
            conversation_id=_conversation_id(payload),
            workspace=_resolve_workspace(payload),
            transcript_path=_transcript_path(payload),
            invocation_num=invocation,
            initial_num_steps=initial_steps,
        )
        steps = output.get("injectSteps") if isinstance(output, dict) else None
        if steps:
            _journal(
                "PreInvocation:recall",
                {
                    "conversation_id": _conversation_id(payload),
                    "invocation_num": invocation,
                    "injected_steps": len(steps),
                },
            )
        return output if isinstance(output, dict) else {"injectSteps": []}
    except Exception as exc:
        _log_error("PreInvocation", exc)
        return {"injectSteps": []}


def _fallback(event_name: str) -> dict[str, Any]:
    normalized = event_name.lower().replace("-", "").replace("_", "")
    if normalized in ("sessionstart", "preinvocation"):
        return {"injectSteps": []}
    if normalized == "stop":
        return {"decision": "stop"}
    return {}


def run_hook_cli(event_name: str) -> None:
    """Read a hook payload from stdin and write exactly one JSON object."""
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    payload: dict[str, Any] = {}
    if raw.strip():
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                payload = value
        except json.JSONDecodeError:
            payload = {}

    normalized = event_name.lower().replace("-", "").replace("_", "")
    try:
        if normalized == "sessionstart":
            output = handle_session_start(payload)
        elif normalized == "preinvocation":
            output = handle_pre_invocation(payload)
        elif normalized == "posttooluse":
            output = handle_post_tool_use(payload)
        elif normalized == "stop":
            output = handle_stop(payload)
        else:
            output = {}
    except BaseException as exc:  # the hook must always return valid JSON
        _log_error(f"hook:{event_name}", exc)
        output = _fallback(event_name)

    try:
        json.dump(output, sys.stdout, ensure_ascii=False)
        sys.stdout.flush()
    except Exception:
        pass
