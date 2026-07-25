"""`register --runtime-home` contract, across project classes.

Delivery resolves an endpoint by matching `CODEX_HOME=<value>` literally against the
running app-server process, so the spelling stored at registration is load-bearing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
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
    """End-to-end register, for an Amiga and a non-Amiga project.

    Runs against a TEMPORARY workspace. An earlier version invoked the production CLI
    with cwd=ROOT and then unlinked fixed session/binding paths from the real
    State/session_autobridge tree -- which live watchers read concurrently, and which a
    parallel suite process would race. find_workspace_root walks up for
    collab.config.json, so pointing cwd at a fixture root is enough to isolate it.
    """

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="lc-rh-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.workspace, True)
        (self.workspace / "collab.config.json").write_text(json.dumps({
            "workspace_name": "rh-fixture",
            "schema_version": 2,
            "workspace_id": "ws_rh",
            "projects_root": str(self.workspace),
            "project_state_root": str(self.workspace / "project-state"),
            "poll_interval_seconds": 15,
            "notifications_enabled": False,
        }), encoding="utf-8")
        (self.workspace / "projects.json").write_text(json.dumps({"projects": [
            {"id": "amiga", "display_name": "Amiga", "repos": {"llm-collab": "."}},
            {"id": "nuvyr", "display_name": "Nuvyr", "repos": {"llm-collab": "."}},
        ]}), encoding="utf-8")
        shutil.copy(ROOT / "agents.json", self.workspace / "agents.json")

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
            capture_output=True, text=True, timeout=60, cwd=str(self.workspace),
        )
        self.assertEqual(0, result.returncode, result.stderr[:400])
        return json.loads(result.stdout[result.stdout.index("{"):])

    def test_registration_never_touches_the_real_state_tree(self) -> None:
        """The isolation itself is the assertion, not a side effect of it."""
        self._register("SESSION-RH-ISOLATION", "amiga", "/tmp/rh-home")
        self.assertTrue(
            (self.workspace / "State" / "session_autobridge" / "sessions"
             / "SESSION-RH-ISOLATION.json").exists(),
            "the fixture workspace must be the one that received the record",
        )
        self.assertFalse(
            (ROOT / "State" / "session_autobridge" / "sessions"
             / "SESSION-RH-ISOLATION.json").exists(),
            "the live workspace must be untouched",
        )

    def test_registers_canonical_home_for_amiga_and_non_amiga_projects(self) -> None:
        for project, session in (("amiga", "SESSION-RH-AMIGA"), ("nuvyr", "SESSION-RH-OTHER")):
            with self.subTest(project=project):
                # trailing slash on input must not reach storage
                payload = self._register(session, project, "/tmp/rh-home/")
                self.assertEqual("/tmp/rh-home", payload["runtime"]["home"])
                self.assertEqual(project, payload["project_id"])
                self.assertEqual("thread-rh-test", payload["runtime"]["session_id"])


if __name__ == "__main__":
    unittest.main()
