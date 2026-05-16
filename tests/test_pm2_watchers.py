from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import pm2_watchers


class PM2WatchersTest(unittest.TestCase):
    def test_pm2_run_exits_when_pm2_times_out(self) -> None:
        with patch.object(pm2_watchers, "resolve_pm2", return_value="/usr/local/bin/pm2"):
            with patch.object(
                pm2_watchers.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["pm2", "describe", "llm-collab-codex"], 15),
            ):
                with self.assertRaises(SystemExit) as context:
                    pm2_watchers.pm2_run(["describe", "llm-collab-codex"])

        self.assertEqual(context.exception.code, 124)

    def test_logs_command_requests_non_streaming_pm2_output(self) -> None:
        calls: list[list[str]] = []

        def fake_pm2_run(args_list: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess:
            calls.append(args_list)
            return subprocess.CompletedProcess(args=args_list, returncode=0)

        with patch.object(sys, "argv", ["pm2_watchers.py", "logs", "--agent", "codex", "--lines", "7"]):
            with patch.object(pm2_watchers, "agent_ids", return_value=["codex"]):
                with patch.object(pm2_watchers, "config_get", return_value="llm-collab"):
                    with patch.object(pm2_watchers, "pm2_run", side_effect=fake_pm2_run):
                        pm2_watchers.main()

        self.assertEqual(calls, [["logs", "llm-collab-codex", "--lines", "7", "--nostream"]])


if __name__ == "__main__":
    unittest.main()
