"""`register --runtime-home` contract, across project classes.

Delivery resolves an endpoint by matching `CODEX_HOME=<value>` literally against the
running app-server process, so the spelling stored at registration is load-bearing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import session_autobridge as cli  # noqa: E402


class CanonicalRuntimeHomeTest(unittest.TestCase):
    def test_trailing_separator_is_removed(self) -> None:
        # `/x/.codex/` would never match `CODEX_HOME=/x/.codex` in the process list
        self.assertEqual("/x/.codex", cli.canonical_runtime_home("/x/.codex/"))

    def test_tilde_is_expanded_for_non_shell_callers(self) -> None:
        expanded = cli.canonical_runtime_home("~/.codex")
        self.assertFalse(expanded.startswith("~"), expanded)
        self.assertTrue(expanded.endswith("/.codex"), expanded)

    def test_redundant_segments_are_collapsed(self) -> None:
        self.assertEqual("/x/.codex", cli.canonical_runtime_home("/x/./sub/../.codex"))

    def test_blank_and_none_stay_none(self) -> None:
        self.assertIsNone(cli.canonical_runtime_home(None))
        self.assertIsNone(cli.canonical_runtime_home("   "))

    def test_symlinks_are_not_resolved(self) -> None:
        # the comparison target is the spelling the runtime was LAUNCHED with, so
        # resolving symlinks here would create a mismatch rather than fix one
        import os
        import tempfile

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            real = Path(tmp) / "real-home"
            real.mkdir()
            link = Path(tmp) / "link-home"
            os.symlink(real, link)
            self.assertEqual(str(link), cli.canonical_runtime_home(str(link)))


class RegisterRuntimeHomeTest(unittest.TestCase):
    """End-to-end register, for an Amiga and a non-Amiga project."""

    def _register(self, session: str, project: str, home: str) -> dict:
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "bin" / "session_autobridge.py"), "register",
                "--session", session, "--agent", "codex",
                "--project", project, "--chat", "CHAT-RH-TEST",
                "--repo-target", "llm-collab",
                "--mode", "auto-read", "--status", "parked",
                "--wake-strategy", "runtime_trigger",
                "--runtime-family", "codex_app",
                "--runtime-session-id", "thread-rh-test",
                "--runtime-home", home,
                "--ttl-seconds", "60", "--json",
            ],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        self.assertEqual(0, result.returncode, result.stderr[:400])
        return json.loads(result.stdout[result.stdout.index("{"):])

    def _cleanup(self, session: str, project: str) -> None:
        (ROOT / "State" / "session_autobridge" / "sessions" / f"{session}.json").unlink(missing_ok=True)
        binding = ROOT / "State" / "session_autobridge" / "bindings" / project / "CHAT-RH-TEST" / "codex.json"
        binding.unlink(missing_ok=True)
        try:
            binding.parent.rmdir()
        except OSError:
            pass

    def test_registers_canonical_home_for_amiga_and_non_amiga_projects(self) -> None:
        for project, session in (("amiga", "SESSION-RH-AMIGA"), ("nuvyr", "SESSION-RH-OTHER")):
            with self.subTest(project=project):
                try:
                    # trailing slash on input must not reach storage
                    payload = self._register(session, project, "/tmp/rh-home/")
                    self.assertEqual("/tmp/rh-home", payload["runtime"]["home"])
                    self.assertEqual(project, payload["project_id"])
                    self.assertEqual("thread-rh-test", payload["runtime"]["session_id"])
                finally:
                    self._cleanup(session, project)


if __name__ == "__main__":
    unittest.main()
