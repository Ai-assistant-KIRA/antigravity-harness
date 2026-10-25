"""
Checkout/deployed CLI entry point.

Adds the plugin root to ``sys.path`` so ``python scripts/harness_cli.py`` works
from any working directory, then delegates to the real CLI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

os.environ.setdefault("HARNESS_PLUGIN_DIR", str(PLUGIN_ROOT))

from harness.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
