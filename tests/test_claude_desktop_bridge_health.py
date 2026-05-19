from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import claude_desktop_bridge_health


class ClaudeDesktopBridgeHealthTest(unittest.TestCase):
    def test_collect_health_reports_visible_frontmost_claude_without_content(self) -> None:
        def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
            joined = " ".join(command)
            if command[:2] == ["pgrep", "-fl"] and "Claude.app/Contents/MacOS/Claude" in joined:
                return subprocess.CompletedProcess(command, 0, stdout="123 /Applications/Claude.app/Contents/MacOS/Claude\n", stderr="")
            if command[:3] == ["ps", "-o", "pid=,stat=,etime=,pcpu=,pmem="]:
                return subprocess.CompletedProcess(command, 0, stdout="123 R 01:02:03 42.5 0.6\n", stderr="")
            if command[:2] == ["pgrep", "-fl"] and "claude-code" in joined:
                return subprocess.CompletedProcess(command, 0, stdout="456 /Users/me/Library/Application Support/Claude/claude-code/x/claude\n", stderr="")
            if command[:2] == ["pmset", "-g"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='pid 123(Claude): NoIdleSleepAssertion named: "Electron"\nCodex Computer Use interaction\nPreventSystemSleep',
                    stderr="",
                )
            if command[:2] == ["osascript", "-e"] and "frontmost" in command[2]:
                return subprocess.CompletedProcess(command, 0, stdout="Claude\n", stderr="")
            if command[:2] == ["osascript", "-e"] and "visible" in command[2]:
                return subprocess.CompletedProcess(command, 0, stdout="Finder, Claude, Codex\n", stderr="")
            raise AssertionError(f"unexpected command: {command}")

        with patch.object(claude_desktop_bridge_health.subprocess, "run", side_effect=fake_run):
            health = claude_desktop_bridge_health.collect_health()

        self.assertTrue(health["claude_process_running"])
        self.assertTrue(health["claude_frontmost"])
        self.assertTrue(health["claude_visible"])
        self.assertEqual(health["claude_main_process_metrics"]["cpu_percent_total"], 42.5)
        self.assertTrue(health["claude_main_process_metrics"]["busy"])
        self.assertEqual(health["claude_local_agent_process_count"], 1)
        self.assertTrue(health["computer_use_required_for_bridge"])
        self.assertIn("not proof", health["diagnostic_scope"])

    def test_process_metrics_parse_ps_output(self) -> None:
        with patch.object(
            claude_desktop_bridge_health,
            "run_command",
            return_value=claude_desktop_bridge_health.CommandResult(ok=True, stdout="123 S 00:01 3.5 0.2\n456 R 00:02 7.0 0.4", stderr=""),
        ):
            metrics = claude_desktop_bridge_health.claude_main_process_metrics(["123", "456"])

        self.assertEqual(metrics["cpu_percent_total"], 10.5)
        self.assertTrue(metrics["busy"])
        self.assertEqual(len(metrics["processes"]), 2)

    def test_local_agent_count_is_zero_when_no_process_matches(self) -> None:
        with patch.object(
            claude_desktop_bridge_health,
            "run_command",
            return_value=claude_desktop_bridge_health.CommandResult(ok=False, stdout="", stderr=""),
        ):
            self.assertEqual(claude_desktop_bridge_health.claude_local_agent_count(), 0)

    def test_run_command_reports_missing_binary_without_raising(self) -> None:
        with patch.object(
            claude_desktop_bridge_health.subprocess,
            "run",
            side_effect=FileNotFoundError("missing-tool"),
        ):
            result = claude_desktop_bridge_health.run_command(["missing-tool", "--version"])

        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "")
        self.assertIn("command not found: missing-tool", result.stderr)


if __name__ == "__main__":
    unittest.main()
