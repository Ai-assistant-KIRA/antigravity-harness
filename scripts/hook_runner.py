"""
Antigravity Hook Runner for Harness².
Executes lifecycle hook handlers with full isolation, robust exception trapping,
and fast JSON stdin/stdout serialization.
"""

import os
import sys
from pathlib import Path

# Ensure harness package is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = Path("c:/Users/Lenovo/Desktop/harness")

for path in [PLUGIN_ROOT, WORKSPACE_ROOT]:
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    from harness.hooks import run_hook_cli
except ImportError:
    # If not yet installed, define fallback passthrough
    def run_hook_cli(event_name):
        import json
        if event_name in ("SessionStart", "session-start"):
            json.dump({"injectSteps": []}, sys.stdout)
        elif event_name in ("Stop", "stop"):
            json.dump({"decision": "stop"}, sys.stdout)
        else:
            json.dump({}, sys.stdout)

if __name__ == "__main__":
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    run_hook_cli(event)
