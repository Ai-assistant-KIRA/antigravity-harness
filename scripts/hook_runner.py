"""
Antigravity hook entry point for Harness².

This file deliberately has no third-party imports and derives the plugin root
from its own location. Antigravity runs hook commands with the plugin directory
as cwd, but relying on ``__file__`` also works when the script is invoked from a
terminal, an IDE task, or a test.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent

# Make the bundled package win over any globally installed copy.
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# When this runner is executed from the deployed plugin, use that plugin as the
# data root. An explicit environment variable (tests, CI) always wins.
os.environ.setdefault("HARNESS_PLUGIN_DIR", str(PLUGIN_ROOT))


def _normalized(event: str) -> str:
    return (event or "").lower().replace("-", "").replace("_", "")


def _fallback(event: str) -> dict:
    normalized = _normalized(event)
    if normalized == "sessionstart":
        return {"injectSteps": []}
    if normalized == "preinvocation":
        return {"injectSteps": []}
    if normalized == "stop":
        return {"decision": "stop"}
    return {}


def _log_error(context: str) -> None:
    try:
        path = PLUGIN_ROOT / "state" / "hook_errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[hook_runner] {context}\n")
            handle.write(traceback.format_exc(limit=8))
            handle.write("\n")
    except Exception:
        pass


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        from harness.hooks import run_hook_cli
    except BaseException:
        _log_error("import harness.hooks")
        try:
            json.dump(_fallback(event), sys.stdout, ensure_ascii=False)
            sys.stdout.flush()
        except Exception:
            pass
        return

    try:
        run_hook_cli(event)
    except BaseException:
        # run_hook_cli is already defensive; this is the last line of defence.
        _log_error(f"dispatch {event}")
        try:
            json.dump(_fallback(event), sys.stdout, ensure_ascii=False)
            sys.stdout.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
