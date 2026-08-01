import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import current_runtime


def completed(*, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class CurrentRuntimeTest(unittest.TestCase):
    def test_current_tooling_reports_contract_and_heads(self):
        def run(*command, **_kwargs):
            if list(command) == ["fetch", "origin", "main", "--quiet"]:
                return completed()
            if list(command) == ["rev-parse", "origin/main"]:
                return completed(stdout="origin-sha\n")
            if list(command) == ["rev-parse", "HEAD"]:
                return completed(stdout="head-sha\n")
            if list(command) == ["show", "origin/main:AGENTS.md"]:
                return completed(stdout="<!-- CONTRACT_VERSION: 10 -->\n")
            raise AssertionError(command)

        with patch.object(current_runtime, "git", side_effect=run), patch.object(
            current_runtime.subprocess, "run", side_effect=[completed(), completed()]
        ), patch.object(
            current_runtime.Path, "read_text", return_value="<!-- CONTRACT_VERSION: 10 -->\n"
        ):
            evidence = current_runtime.current_tooling()

        self.assertEqual(
            {"head": "head-sha", "origin_main": "origin-sha", "contract_version": "10"},
            evidence,
        )

    def test_stale_checkout_is_refused_before_bootstrap(self):
        with patch.object(
            current_runtime,
            "current_tooling",
            side_effect=current_runtime.ToolingError("checkout is stale"),
        ), patch.object(current_runtime.subprocess, "run") as run:
            self.assertEqual(current_runtime.main(), 1)

        run.assert_not_called()

    def test_current_checkout_starts_repository_bootstrap(self):
        evidence = {"head": "head", "origin_main": "origin", "contract_version": "10"}
        with patch.object(current_runtime, "parse_args", return_value=(False, ["--agent", "codex"])), patch.object(
            current_runtime, "current_tooling", return_value=evidence
        ), patch.object(
            current_runtime.subprocess, "run", return_value=completed()
        ) as run:
            self.assertEqual(current_runtime.main(), 0)

        run.assert_called_once_with(
            [sys.executable, str(current_runtime.ROOT / "bin" / "session_bootstrap.py"), "--agent", "codex"],
            cwd=current_runtime.ROOT,
        )


if __name__ == "__main__":
    unittest.main()
