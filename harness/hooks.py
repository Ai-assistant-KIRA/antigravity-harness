"""
Hook dispatchers for Antigravity lifecycle events.
Invoked by scripts/hook_runner.py during SessionStart, PostToolUse, and Stop.
"""

from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any

from .core.loader import SessionLoader
from .core.engine import HarnessEngine
from .core.memory_store import MemoryStore
from .core.analyzer import ContrastiveEvent


def handle_session_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Handles the SessionStart hook event."""
    workspace_paths = payload.get("workspacePaths", [])
    ws_path = Path(workspace_paths[0]) if workspace_paths else None
    loader = SessionLoader(ws_path)
    return loader.generate_session_start_payload()


def handle_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    """Handles the PostToolUse hook event to record real-time tool metrics or errors."""
    tool_call = payload.get("toolCall", {})
    tool_name = tool_call.get("name", "")
    error_msg = payload.get("error")
    conversation_id = payload.get("conversationId", "")

    if error_msg and tool_name:
        # Record immediate error into memory for contrastive pairing
        try:
            store = MemoryStore()
            args = tool_call.get("args", {})
            event = ContrastiveEvent(
                trigger_tool=tool_name,
                failed_args=args,
                error_message=str(error_msg),
                successful_args=None,
                resolution_summary=f"Encountered error during {tool_name}: {str(error_msg)[:120]}",
                category="command",
            )
            # Store partial lesson
            analysis = None  # Full synthesis happens on Stop
        except Exception:
            pass

    return {}


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    """Handles the Stop hook event by running the Harness² Propose-Probe-Compose cycle."""
    conversation_id = payload.get("conversationId", "")
    transcript_path = payload.get("transcriptPath", "")
    workspace_paths = payload.get("workspacePaths", [])
    ws_path = Path(workspace_paths[0]) if workspace_paths else None

    try:
        engine = HarnessEngine(ws_path)
        engine.run_cycle(
            conversation_id=conversation_id,
            transcript_path=transcript_path,
        )
    except Exception as exc:
        # Failsafe: never crash the agent stop
        pass

    return {
        "decision": "stop"
    }


def run_hook_cli(event_name: str) -> None:
    """Entry point for executing a hook from stdin/stdout."""
    raw_in = sys.stdin.read()
    payload = {}
    if raw_in.strip():
        try:
            payload = json.loads(raw_in)
        except Exception:
            payload = {}

    if event_name in ("SessionStart", "session_start", "session-start"):
        out = handle_session_start(payload)
    elif event_name in ("PostToolUse", "post_tool_use", "post-tool-use"):
        out = handle_post_tool_use(payload)
    elif event_name in ("Stop", "stop"):
        out = handle_stop(payload)
    else:
        out = {}

    json.dump(out, sys.stdout)
    sys.stdout.flush()
