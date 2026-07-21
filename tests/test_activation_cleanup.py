from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _activation_cleanup as cleanup


class ActivationCleanupTest(unittest.TestCase):
    def identity(self) -> dict[str, str]:
        return {
            "project": "amiga",
            "chat": "CHAT-1572",
            "task": "TASK-97402D",
            "worktree": "/tmp/llm-collab-test-worktree",
            "branch": "codex/gh-1572-runtime-integration",
            "target_agent": "claude",
        }

    def test_chat_match_requires_target_agent_binding(self) -> None:
        identity = self.identity()
        wrong_agent = (
            "zsh -lc 'while true; do bin/inbox.py --me gemini "
            "--project amiga --chat CHAT-1572 --peek; done'"
        )
        right_agent = (
            "zsh -lc 'while true; do bin/inbox.py --me claude "
            "--project amiga --chat CHAT-1572 --peek; done'"
        )

        self.assertIsNone(cleanup.matches_activation_identity(wrong_agent, identity))
        self.assertEqual(
            "chat:CHAT-1572:agent:claude",
            cleanup.matches_activation_identity(right_agent, identity),
        )

    def test_registered_pm2_pid_is_preserved(self) -> None:
        rows = [
            {
                "pid": 101,
                "ppid": 1,
                "command": "while true; do bin/inbox.py --me claude --chat CHAT-1572; done",
            }
        ]

        findings = cleanup.audit_activation_pollers(
            self.identity(),
            rows=rows,
            registered_pids={101},
            clean=True,
            self_pid=999,
        )

        self.assertEqual("preserved_registered_watch", findings[0]["action"])
        self.assertTrue(cleanup.audit_proves_clean(findings))

    def test_fixture_cleanup_is_simulated_and_never_calls_kill(self) -> None:
        calls: list[tuple[int, int]] = []
        old_fixture = os.environ.get("LLM_COLLAB_PS_FIXTURE")
        with tempfile.TemporaryDirectory(prefix="llm-collab-ps-fixture-") as tmp:
            fixture = Path(tmp) / "ps.txt"
            fixture.write_text(
                "202 1 while true; do bin/inbox.py --me claude --chat CHAT-1572; done\n"
            )
            os.environ["LLM_COLLAB_PS_FIXTURE"] = str(fixture)
            try:
                findings = cleanup.audit_activation_pollers(
                    self.identity(),
                    registered_pids=set(),
                    clean=True,
                    kill=lambda pid, sig: calls.append((pid, sig)),
                    self_pid=999,
                )
            finally:
                if old_fixture is None:
                    os.environ.pop("LLM_COLLAB_PS_FIXTURE", None)
                else:
                    os.environ["LLM_COLLAB_PS_FIXTURE"] = old_fixture

        self.assertEqual([], calls)
        self.assertEqual("terminated", findings[0]["action"])
        self.assertTrue(findings[0]["simulated"])
        self.assertTrue(cleanup.audit_proves_clean(findings))

    def test_pm2_binary_missing_makes_audit_unavailable(self) -> None:
        with patch.dict(os.environ, {"LLM_COLLAB_PM2_BIN": ""}, clear=False):
            with patch.object(cleanup, "which", return_value=None):
                with self.assertRaises(cleanup.PollerAuditUnavailable) as ctx:
                    cleanup.pm2_registered_pids()
        self.assertIn("pm2 binary unavailable", str(ctx.exception))

    def test_pm2_jlist_failure_makes_audit_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="llm-collab-pm2-fixture-") as tmp:
            pm2 = Path(tmp) / "pm2"
            pm2.write_text("#!/bin/sh\nexit 17\n")
            pm2.chmod(0o755)
            with patch.dict(os.environ, {"LLM_COLLAB_PM2_BIN": str(pm2)}, clear=False):
                with self.assertRaises(cleanup.PollerAuditUnavailable) as ctx:
                    cleanup.pm2_registered_pids()
        self.assertIn("pm2 jlist failed", str(ctx.exception))

    def test_pm2_invalid_json_makes_audit_unavailable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="llm-collab-pm2-fixture-") as tmp:
            pm2 = Path(tmp) / "pm2"
            pm2.write_text("#!/bin/sh\nprintf 'not-json'\n")
            pm2.chmod(0o755)
            with patch.dict(os.environ, {"LLM_COLLAB_PM2_BIN": str(pm2)}, clear=False):
                with self.assertRaises(cleanup.PollerAuditUnavailable) as ctx:
                    cleanup.pm2_registered_pids()
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_terminate_verified_sigterm_then_sigkill_and_refusals(self) -> None:
        calls: list[tuple[int, int]] = []

        def kill(pid: int, sig: int) -> None:
            calls.append((pid, sig))

        waits = iter([False, True])
        action = cleanup.terminate_verified(55, kill=kill, wait_for_exit=lambda _pid: next(waits))
        self.assertEqual("terminated_sigkill", action)
        self.assertEqual([(55, 15), (55, 9)], calls)

        self.assertEqual(
            "terminate_denied",
            cleanup.terminate_verified(
                56,
                kill=lambda _pid, _sig: (_ for _ in ()).throw(PermissionError()),
                wait_for_exit=lambda _pid: False,
            ),
        )
        self.assertEqual(
            "termination_unverified",
            cleanup.terminate_verified(
                57,
                kill=lambda _pid, _sig: None,
                wait_for_exit=lambda _pid: False,
            ),
        )

    def test_report_only_cleanup_is_not_proven_clean(self) -> None:
        rows = [
            {
                "pid": 303,
                "ppid": 1,
                "command": "while true; do bin/inbox.py --me claude --chat CHAT-1572; done",
            }
        ]

        findings = cleanup.audit_activation_pollers(
            self.identity(),
            rows=rows,
            registered_pids=set(),
            clean=False,
            self_pid=999,
        )

        self.assertEqual("reported_only", findings[0]["action"])
        self.assertFalse(cleanup.audit_proves_clean(findings))


if __name__ == "__main__":
    unittest.main()
