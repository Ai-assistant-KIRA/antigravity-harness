"""Release hygiene and the agent-driven install contract.

An agent that installs Harness from a pasted repository link relies on three
things: the bootstrap scripts exist, every installer run ends with one
machine-readable status line, and the launchers survive any checkout's line
endings. Version numbers must also agree, or `doctor`, the manifest, and the
package report different releases.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install_plugin  # noqa: E402
from harness import __version__  # noqa: E402


class TestVersionsAgree(unittest.TestCase):
    def test_manifest_package_and_pyproject_share_one_version(self) -> None:
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
        self.assertIsNotNone(match)
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(match.group(1), __version__)

    def test_changelog_documents_the_current_version(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertRegex(changelog, rf"(?m)^## {re.escape(__version__)}\b")


class TestAgentInstallContract(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("HARNESS_PLUGIN_DIR", None)
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ROOT / "install_plugin.py"), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            env=env,
        )

    def test_install_ends_with_an_ok_status_line(self) -> None:
        with tempfile.TemporaryDirectory(prefix="harness-release-") as temp:
            target = Path(temp) / "plugins" / "harness"
            completed = self._run("--target", str(target))
            last = completed.stdout.strip().splitlines()[-1]
            self.assertEqual(completed.returncode, 0, completed.stdout[-2000:])
            self.assertTrue(last.startswith("HARNESS_INSTALL: ok action=install"), last)
            self.assertIn(f"version={__version__}", last)
            self.assertTrue((target / "bin" / "python.path").is_file())
            self.assertIn("checks passed", completed.stdout)

    def test_refusal_ends_with_a_failed_status_line(self) -> None:
        completed = self._run("--target", str(ROOT))
        last = completed.stdout.strip().splitlines()[-1]
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(last.startswith("HARNESS_INSTALL: failed"), last)
        self.assertIn("source checkout", last)
        self.assertIn("Clone the repository somewhere else", completed.stdout)

    def test_bootstrap_scripts_ship_with_the_repository(self) -> None:
        for name in ("install.ps1", "install.sh"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("install_plugin.py", text, name)
            self.assertIn("HARNESS_INSTALL: failed", text, name)
            self.assertIn("3.10", text, name)


class TestLauncherNewlines(unittest.TestCase):
    def test_gitattributes_pins_launcher_line_endings(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attributes, r"(?m)^\*\.cmd\s+text eol=crlf")
        self.assertRegex(attributes, r"(?m)^bin/hook\s+text eol=lf")

    def test_installer_repairs_launcher_line_endings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="harness-eol-") as temp:
            target = Path(temp)
            (target / "bin").mkdir()
            (target / "bin" / "hook.cmd").write_bytes(b"@echo off\n:label\ngoto :label\n")
            (target / "bin" / "hook").write_bytes(b"#!/usr/bin/env sh\r\necho ok\r\n")
            install_plugin._normalize_launcher_newlines(target, dry_run=False)
            self.assertEqual(
                (target / "bin" / "hook.cmd").read_bytes(),
                b"@echo off\r\n:label\r\ngoto :label\r\n",
            )
            self.assertEqual(
                (target / "bin" / "hook").read_bytes(), b"#!/usr/bin/env sh\necho ok\n"
            )


if __name__ == "__main__":
    unittest.main()
