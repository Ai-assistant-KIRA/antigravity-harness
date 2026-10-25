"""Precision tests for cross-tool recovery pairing and expected probes."""

from __future__ import annotations

import unittest

try:
    from .support import IsolatedHarnessTestCase
except ImportError:  # unittest discover -s tests
    from support import IsolatedHarnessTestCase

from harness.core.analyzer import SessionAnalysis, ToolExecution, TranscriptAnalyzer


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


def user(content: str) -> dict:
    return {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": content}


class TestCrossToolRecoveryPrecision(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_unrelated_curl_success_does_not_verify_missing_executable(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Check the agy CLI"),
                tool("run_command", CommandLine="agy --help"),
                output(
                    "The term 'agy' is not recognized as the name of a cmdlet, "
                    "function, script file, or operable program.",
                    "ERROR",
                ),
                tool(
                    "run_command",
                    CommandLine=(
                        "curl.exe -s -X POST "
                        "https://relume-library-mcp.relume.io/register"
                    ),
                ),
                output('{"status":"ok"}'),
            ],
            conversation_id="conv-agy-curl",
        )
        self.assertEqual(analysis.verified_events, [])
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.environment_quirks, [])
        self.assertEqual(len(analysis.unresolved_events), 1)
        self.assertFalse(analysis.unresolved_events[0].verified)

    def test_unrelated_search_success_does_not_verify_missing_module(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Check whether whisper is installed"),
                tool("run_command", CommandLine='python -c "import whisper"'),
                output("ModuleNotFoundError: No module named 'whisper'", "ERROR"),
                tool("search_web", query="openai whisper python package"),
                output("openai-whisper is a Python package for speech recognition."),
            ],
            conversation_id="conv-whisper-search",
        )
        self.assertEqual(analysis.verified_events, [])
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.environment_quirks, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_install_of_missing_module_is_a_verified_dependency_recovery(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Install whisper"),
                tool("run_command", CommandLine='python -c "import whisper"'),
                output("ModuleNotFoundError: No module named 'whisper'", "ERROR"),
                tool("run_command", CommandLine="pip install openai-whisper"),
                output("Successfully installed openai-whisper-20240930"),
            ],
            conversation_id="conv-whisper-install",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        event = analysis.verified_events[0]
        self.assertEqual(event.category, "dependency")
        self.assertIn("install", event.resolution_summary.lower())
        self.assertIn("openai-whisper", event.resolution_summary)
        self.assertEqual(analysis.unresolved_events, [])

    def test_install_of_missing_executable_is_a_verified_environment_recovery(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Install the agy CLI"),
                tool("run_command", CommandLine="agy --help"),
                output("The term 'agy' is not recognized as the name of a cmdlet.", "ERROR"),
                tool("run_command", CommandLine="pip install agy"),
                output("Successfully installed agy-1.0.0"),
            ],
            conversation_id="conv-agy-install",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        event = analysis.verified_events[0]
        self.assertEqual(event.category, "environment")
        self.assertIn("pip install agy", event.resolution_summary)
        self.assertTrue(analysis.environment_quirks)
        self.assertEqual(analysis.unresolved_events, [])

    def test_winget_install_of_missing_executable_verifies(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Install the agy CLI"),
                tool("run_command", CommandLine="agy --help"),
                output("The term 'agy' is not recognized as the name of a cmdlet.", "ERROR"),
                tool("run_command", CommandLine="winget install agy"),
                output("Successfully installed agy"),
            ],
            conversation_id="conv-agy-winget",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        event = analysis.verified_events[0]
        self.assertEqual(event.category, "environment")
        self.assertIn("winget install agy", event.resolution_summary)
        self.assertEqual(analysis.unresolved_events, [])

    def test_choco_install_of_missing_executable_verifies(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Install the agy CLI"),
                tool("run_command", CommandLine="agy --help"),
                output("The term 'agy' is not recognized as the name of a cmdlet.", "ERROR"),
                tool("run_command", CommandLine="choco install agy -y"),
                output("Successfully installed agy"),
            ],
            conversation_id="conv-agy-choco",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        event = analysis.verified_events[0]
        self.assertEqual(event.category, "environment")
        self.assertIn("choco install agy -y", event.resolution_summary)
        self.assertEqual(analysis.unresolved_events, [])

    def test_sudo_invocation_of_missing_executable_verifies(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Run the agy CLI"),
                tool("run_command", CommandLine="agy --help"),
                output("The term 'agy' is not recognized as the name of a cmdlet.", "ERROR"),
                tool("run_command", CommandLine="sudo agy --version"),
                output("agy 1.0.0"),
            ],
            conversation_id="conv-agy-sudo",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        event = analysis.verified_events[0]
        self.assertEqual(event.category, "environment")
        self.assertIn("sudo agy --version", event.resolution_summary)
        self.assertEqual(analysis.unresolved_events, [])

    def test_env_wrapped_invocation_of_missing_executable_verifies(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Run the agy CLI"),
                tool("run_command", CommandLine="agy --help"),
                output("The term 'agy' is not recognized as the name of a cmdlet.", "ERROR"),
                tool("run_command", CommandLine="env FOO=1 agy --version"),
                output("agy 1.0.0"),
            ],
            conversation_id="conv-agy-env",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        event = analysis.verified_events[0]
        self.assertEqual(event.category, "environment")
        self.assertIn("env FOO=1 agy --version", event.resolution_summary)
        self.assertEqual(analysis.unresolved_events, [])

    def test_wrapped_command_forms_resolve_to_the_real_executable(self) -> None:
        cases = (
            ("nohup agy --version", "agy"),
            ("time agy --version", "agy"),
            ("command agy --version", "agy"),
            ("exec agy --version", "agy"),
            ("cmd /c agy --version", "agy"),
            ('cmd /c "agy --version"', "agy"),
            ("powershell -Command agy --version", "agy"),
            ('powershell -Command "agy --version"', "agy"),
            ('pwsh -Command "agy --version"', "agy"),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.analyzer._first_command_token(command), expected)

    def test_first_command_token_does_not_scan_later_tokens(self) -> None:
        self.assertEqual(self.analyzer._first_command_token("echo agy"), "echo")
        self.assertEqual(
            self.analyzer._first_command_token("curl.exe -s https://example.test agy"),
            "curl.exe",
        )

    def test_network_failure_is_not_verified_by_unrelated_search(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Check the local service"),
                tool("run_command", CommandLine="curl http://localhost:8000/health"),
                output("curl: (7) Failed to connect: connection refused", "ERROR"),
                tool("search_web", query="local health check"),
                output("A health check reports whether a service is reachable."),
            ],
            conversation_id="conv-network-search",
        )
        self.assertEqual(analysis.verified_events, [])
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(analysis.environment_quirks, [])
        self.assertEqual(len(analysis.unresolved_events), 1)

    def test_cross_tool_pair_outside_window_does_not_verify(self) -> None:
        error = "ModuleNotFoundError: No module named 'whisper'"
        failed = ToolExecution(
            name="run_command",
            args={"CommandLine": 'python -c "import whisper"'},
            step_index=1,
            error=error,
            error_signature=self.analyzer._signature(error),
            success=False,
            has_evidence=True,
        )
        success = ToolExecution(
            name="run_command",
            args={"CommandLine": "pip install openai-whisper"},
            step_index=10,
            output="Successfully installed openai-whisper",
            success=True,
            has_evidence=True,
        )
        analysis = SessionAnalysis(conversation_id="conv-window")
        self.analyzer._pair_recovery(success, [failed], analysis, "conv-window")
        self.assertEqual(analysis.contrastive_events, [])
        self.assertFalse(getattr(failed, "_recovered", False))

    def test_cross_tool_success_without_evidence_does_not_verify(self) -> None:
        error = "ModuleNotFoundError: No module named 'whisper'"
        failed = ToolExecution(
            name="run_command",
            args={"CommandLine": 'python -c "import whisper"'},
            step_index=1,
            error=error,
            error_signature=self.analyzer._signature(error),
            success=False,
            has_evidence=True,
        )
        success = ToolExecution(
            name="run_command",
            args={"CommandLine": "pip install openai-whisper"},
            step_index=2,
            output="Successfully installed openai-whisper",
            success=True,
            has_evidence=False,
        )
        analysis = SessionAnalysis(conversation_id="conv-no-evidence")
        self.analyzer._pair_recovery(success, [failed], analysis, "conv-no-evidence")
        self.assertEqual(analysis.contrastive_events, [])
        self.assertFalse(getattr(failed, "_recovered", False))


class TestExpectedProbes(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_silent_probe_with_no_output_is_not_a_failure(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Check whether port 8765 is free"),
                tool(
                    "run_command",
                    CommandLine=(
                        "Get-NetTCPConnection -LocalPort 8765 "
                        "-ErrorAction SilentlyContinue"
                    ),
                ),
                output("No output from the command.", "ERROR"),
            ],
            conversation_id="conv-probe",
        )
        self.assertEqual(analysis.unresolved_events, [])
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.tool_calls), 1)
        self.assertTrue(analysis.tool_calls[0]["success"])
        self.assertIsNone(analysis.tool_calls[0]["error"])

    def test_probe_output_access_denied_is_not_filtered(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Inspect the secret process"),
                tool("run_command", CommandLine="Get-Process secret"),
                output("Access is denied.", "ERROR", error="Command exited with code 1"),
            ],
            conversation_id="conv-probe-access-denied",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)
        self.assertFalse(analysis.tool_calls[0]["success"])
        self.assertIsNotNone(analysis.tool_calls[0]["error"])

    def test_probe_output_missing_module_is_not_filtered(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Inspect the secret process"),
                tool("run_command", CommandLine="Get-Process secret"),
                output(
                    "ModuleNotFoundError: No module named 'x'",
                    "ERROR",
                    error="Command exited with code 1",
                ),
            ],
            conversation_id="conv-probe-output-module",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)
        self.assertFalse(analysis.tool_calls[0]["success"])
        self.assertIsNotNone(analysis.tool_calls[0]["error"])

    def test_genuine_missing_module_is_not_filtered(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Import the module"),
                tool("run_command", CommandLine='python -c "import x"'),
                output("ModuleNotFoundError: No module named 'x'", "ERROR"),
            ],
            conversation_id="conv-genuine-module",
        )
        self.assertEqual(analysis.contrastive_events, [])
        self.assertEqual(len(analysis.unresolved_events), 1)
        self.assertFalse(analysis.unresolved_events[0].verified)
        self.assertFalse(analysis.tool_calls[0]["success"])
        self.assertIsNotNone(analysis.tool_calls[0]["error"])


class TestSameToolRecoveryRegression(IsolatedHarnessTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.analyzer = TranscriptAnalyzer()

    def test_same_tool_subcommand_recovery_still_verifies(self) -> None:
        analysis = self.analyzer.analyze(
            [
                user("Build the Python package"),
                tool("run_command", CommandLine="python setup.py"),
                output("command exited with code 1: error: no setup.py found", "ERROR"),
                tool("run_command", CommandLine="python -m build"),
                output("Successfully built dist/package.whl"),
            ],
            conversation_id="conv-same-tool",
        )
        self.assertEqual(len(analysis.verified_events), 1)
        self.assertEqual(analysis.unresolved_events, [])
        self.assertIn("python -m build", analysis.verified_events[0].resolution_summary)


if __name__ == "__main__":
    unittest.main()
