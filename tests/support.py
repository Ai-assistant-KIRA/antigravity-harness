"""Shared isolation helpers for the Harness² test suite.

Every test runs with its own plugin directory, Antigravity config directory,
transcript root, and workspace. No test is allowed to read or write the real
deployed plugin data.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterable

from harness.core.analyzer import ContrastiveEvent, WorkflowCandidate

_ENV_KEYS = (
    "HARNESS_PLUGIN_DIR",
    "HARNESS_GEMINI_CONFIG_DIR",
    "ANTIGRAVITY_APP_DATA_DIR",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_PROJECT_ID",
)


class IsolatedHarnessTestCase(unittest.TestCase):
    """Temporary paths plus a private cwd for one test."""

    maxDiff = None

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="harness-test-")
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.plugin_dir = self.root / "plugin"
        self.gemini_dir = self.root / "gemini"
        self.app_data = self.root / "appdata"
        self.workspace = self.root / "workspace"
        for path in (self.plugin_dir, self.gemini_dir, self.app_data, self.workspace):
            path.mkdir(parents=True, exist_ok=True)

        self._previous_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        os.environ["HARNESS_PLUGIN_DIR"] = str(self.plugin_dir)
        os.environ["HARNESS_GEMINI_CONFIG_DIR"] = str(self.gemini_dir)
        os.environ["ANTIGRAVITY_APP_DATA_DIR"] = str(self.app_data)
        for key in ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_PROJECT_ID"):
            os.environ.pop(key, None)

        self._previous_cwd = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(self._restore_process_state)

    def _restore_process_state(self) -> None:
        os.chdir(self._previous_cwd)
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------------------------ helpers
    def make_dir(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def make_transcript(
        self,
        name: str,
        steps: Iterable[dict[str, Any]],
    ) -> Path:
        path = self.root / "transcripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(step, ensure_ascii=False) for step in steps]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return path

    def make_skill_source(
        self,
        name: str = "demo-skill",
        description: str = "Use this skill when testing a deterministic local workflow.",
        body: str = "# Demo\n\n## Steps\n\n1. Do the deterministic thing.\n",
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        folder = self.root / "skill-sources" / name
        folder.mkdir(parents=True, exist_ok=True)
        self.write_text(
            folder / "SKILL.md",
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        )
        for relative, content in (extra_files or {}).items():
            self.write_text(folder / relative, content)
        return folder

    def make_minimal_plugin(self) -> None:
        """Enough deployment shape for `harness doctor` and listing tests."""
        self.write_text(
            self.plugin_dir / "plugin.json",
            json.dumps(
                {
                    "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                    "name": "harness",
                    "description": "Harness² test plugin",
                }
            ),
        )
        self.write_text(
            self.plugin_dir / "hooks.json",
            json.dumps(
                {
                    "harness-lifecycle": {
                        "SessionStart": [{"type": "command", "command": "python x.py"}],
                        "PostToolUse": [
                            {
                                "matcher": "*",
                                "hooks": [{"type": "command", "command": "python x.py"}],
                            }
                        ],
                        "Stop": [{"type": "command", "command": "python x.py"}],
                    }
                }
            ),
        )
        self.write_text(
            self.plugin_dir / "scripts" / "hook_runner.py",
            "print('{}')\n",
        )
        self.write_text(
            self.plugin_dir / "bin" / "harness.cmd",
            "@echo off\n",
        )


def make_event(
    *,
    trigger_tool: str = "run_command",
    error_message: str = "ModuleNotFoundError: No module named 'pytest'",
    resolution_summary: str = (
        "Installing `pytest` with `pip install pytest` resolved the missing dependency."
    ),
    category: str = "dependency",
    verified: bool = True,
    confidence: float = 0.9,
    error_signature: str = "",
) -> ContrastiveEvent:
    return ContrastiveEvent(
        trigger_tool=trigger_tool,
        failed_args={"CommandLine": "pytest"},
        error_message=error_message,
        successful_args={"CommandLine": "pip install pytest"},
        resolution_summary=resolution_summary,
        category=category,
        verified=verified,
        confidence=confidence,
        error_signature=error_signature,
    )


def make_workflow(
    *,
    name: str = "run-python-tests",
    goal: str = "Install dependencies and run the Python test suite",
    steps: list[str] | None = None,
    verification_steps: list[str] | None = None,
    quality: float = 0.7,
    success: bool = True,
) -> WorkflowCandidate:
    return WorkflowCandidate(
        name=name,
        description="Run the Python test suite",
        goal=goal,
        steps=steps
        if steps is not None
        else [
            "Run `pip install -e .`",
            "Run `pytest -q`",
            "Run `ruff check .`",
        ],
        tools_involved=["run_command"],
        verification_steps=verification_steps
        if verification_steps is not None
        else ["Run `pytest -q` and require exit code 0"],
        trigger_keywords=["python", "pytest", "tests"],
        quality_score=quality,
        confidence=quality,
        success=success,
    )
