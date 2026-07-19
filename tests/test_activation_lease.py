from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "bin" / "session_autobridge.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _activation_lease as lease_lib


IDENTITY_ARGS = [
    "--project", "amiga",
    "--chat", "CHAT-TEST0001",
    "--task", "TASK-TEST01",
    "--worktree", "/tmp/worktrees/claude/t-test",
    "--branch", "claude/gh-0000-test",
    "--target-agent", "claude",
]

IDENTITY = {
    "project": "amiga",
    "chat": "CHAT-TEST0001",
    "task": "TASK-TEST01",
    "worktree": "/tmp/worktrees/claude/t-test",
    "branch": "claude/gh-0000-test",
    "target_agent": "claude",
}


class ActivationLeaseCliTest(unittest.TestCase):
    def make_workspace(self) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix="llm-collab-lease-"))
        (temp_root / "collab.config.json").write_text(
            json.dumps(
                {
                    "workspace_name": "test-collab",
                    "schema_version": 2,
                    "projects_root": str(temp_root),
                    "poll_interval_seconds": 15,
                    "notifications_enabled": False,
                }
            )
        )
        (temp_root / "projects.json").write_text(
            json.dumps({"projects": [{"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}}]})
        )
        (temp_root / "agents.json").write_text(json.dumps({"agents": []}))
        return temp_root

    def run_cli(self, root: Path, *args: str) -> tuple[dict, int]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertTrue(result.stdout.strip(), f"no stdout; stderr: {result.stderr}")
        return json.loads(result.stdout), result.returncode

    def claim(self, root: Path, session: str, *extra: str) -> tuple[dict, int]:
        return self.run_cli(
            root,
            "lease-claim",
            *IDENTITY_ARGS,
            "--session",
            session,
            "--skip-poller-cleanup",
            *extra,
        )

    def test_second_session_fails_closed_and_names_owner(self):
        root = self.make_workspace()
        first, code = self.claim(root, "SESSION-A", "--owner-pid", str(os.getpid()))
        self.assertEqual(0, code)
        self.assertTrue(first["claimed"])
        self.assertEqual(1, first["lease"]["fence_token"])

        lease_file = Path(root) / "State" / "session_autobridge" / "activation_leases"
        before = {p.name: p.read_text() for p in lease_file.glob("*.json")}

        second, code = self.claim(root, "SESSION-B")
        self.assertEqual(75, code)
        self.assertFalse(second["claimed"])
        self.assertEqual("lease_held_by_active_owner", second["reason"])
        self.assertEqual("SESSION-A", second["owner"]["owner_session_id"])

        after = {p.name: p.read_text() for p in lease_file.glob("*.json")}
        self.assertEqual(before, after, "refused claim must not mutate the lease record")

    def test_reclaim_by_same_owner_is_idempotent(self):
        root = self.make_workspace()
        self.claim(root, "SESSION-A")
        again, code = self.claim(root, "SESSION-A")
        self.assertEqual(0, code)
        self.assertTrue(again["claimed"])
        self.assertEqual(1, again["lease"]["fence_token"])

    def test_expired_lease_requires_explicit_takeover(self):
        root = self.make_workspace()
        self.claim(root, "SESSION-A", "--ttl-seconds", "0")
        refused, code = self.claim(root, "SESSION-B")
        self.assertEqual(75, code)
        self.assertEqual("lease_expired_requires_takeover", refused["reason"])

    def test_takeover_refused_while_expired_owner_still_alive(self):
        root = self.make_workspace()
        self.claim(root, "SESSION-A", "--ttl-seconds", "0", "--owner-pid", str(os.getpid()))
        refused, code = self.claim(root, "SESSION-B", "--takeover")
        self.assertEqual(75, code)
        self.assertEqual("expired_owner_still_active", refused["reason"])

    def test_takeover_of_dead_owner_increments_fence_token(self):
        root = self.make_workspace()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.claim(root, "SESSION-A", "--ttl-seconds", "0", "--owner-pid", str(dead.pid))
        taken, code = self.claim(root, "SESSION-B", "--takeover")
        self.assertEqual(0, code)
        self.assertTrue(taken["claimed"])
        self.assertEqual(2, taken["lease"]["fence_token"])
        self.assertEqual("SESSION-A", taken["lease"]["previous_owner_session_id"])

    def test_release_requires_owner_then_frees_identity(self):
        root = self.make_workspace()
        self.claim(root, "SESSION-A")
        refused, code = self.run_cli(
            root, "lease-release", *IDENTITY_ARGS, "--session", "SESSION-B"
        )
        self.assertEqual(75, code)
        self.assertEqual("release_requires_current_owner", refused["reason"])

        released, code = self.run_cli(
            root, "lease-release", *IDENTITY_ARGS, "--session", "SESSION-A"
        )
        self.assertEqual(0, code)
        self.assertTrue(released["released"])

        reclaimed, code = self.claim(root, "SESSION-B")
        self.assertEqual(0, code)
        self.assertEqual(2, reclaimed["lease"]["fence_token"])

    def test_lease_show_reports_owner(self):
        root = self.make_workspace()
        empty, code = self.run_cli(root, "lease-show", *IDENTITY_ARGS)
        self.assertEqual(0, code)
        self.assertIsNone(empty["lease"])

        self.claim(root, "SESSION-A")
        shown, code = self.run_cli(root, "lease-show", *IDENTITY_ARGS)
        self.assertEqual(0, code)
        self.assertEqual("SESSION-A", shown["owner"]["owner_session_id"])


class StaleClaimLockTest(unittest.TestCase):
    def test_concurrent_claim_lock_refuses_second_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease_lib.ACTIVATION_LEASES_DIR = Path(tmp)
            lock_path = lease_lib.lease_path(IDENTITY).with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.touch()
            with self.assertRaises(lease_lib.LeaseRefused) as ctx:
                lease_lib.claim_lease(IDENTITY, owner_session_id="SESSION-B")
            self.assertEqual("claim_in_progress", ctx.exception.reason)


class PollerAuditTest(unittest.TestCase):
    ROWS = [
        # Registered PM2 watcher for the same agent: must survive.
        {
            "pid": 101,
            "ppid": 1,
            "command": "python3 bin/watch_inbox.py --me claude PM2_HOME=/Users/op/.pm2 PATH=/usr/bin",
        },
        # Ad-hoc while-true poller referencing the activation chat: cleanup target.
        {
            "pid": 102,
            "ppid": 1,
            "command": "/bin/zsh -c while true; do ls Chats/*CHAT-TEST0001*/*_to-claude_*.md; sleep 60; done",
        },
        # Ad-hoc manual watch_inbox for the same agent, not PM2: cleanup target.
        {"pid": 103, "ppid": 1, "command": "python3 bin/watch_inbox.py --me claude --poll-seconds 30"},
        # Unrelated process: untouched, not even reported.
        {"pid": 104, "ppid": 1, "command": "python3 bin/watch_inbox.py --me codex"},
        # Purpose-scoped PR watcher for a different chat: not identity-matched.
        {"pid": 105, "ppid": 1, "command": "/bin/zsh -c while true; do gh pr checks 111; sleep 300; done"},
        # One-shot command mentioning the chat id (e.g. deliver/lease-claim):
        # not poller-shaped, never a cleanup target.
        {
            "pid": 106,
            "ppid": 1,
            "command": "python3 bin/deliver.py --chat CHAT-TEST0001 --from codex --to claude",
        },
        # The claiming session's own ancestor shell mentions the chat id inside
        # a loop-shaped wrapper: excluded via the ancestor chain.
        {
            "pid": 200,
            "ppid": 1,
            "command": "/bin/zsh -c while true; do run-claim --chat CHAT-TEST0001; done",
        },
        {"pid": 99999, "ppid": 200, "command": "python3 bin/session_autobridge.py lease-claim"},
    ]

    def audit(self, *, clean: bool, terminate=None):
        killed: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            assert sig == signal.SIGTERM
            killed.append(pid)

        findings = lease_lib.audit_activation_pollers(
            IDENTITY,
            rows=self.ROWS,
            clean=clean,
            terminate=terminate or fake_kill,
            self_pid=99999,
        )
        return findings, killed

    def test_report_only_audit_reports_matches_without_killing(self):
        findings, killed = self.audit(clean=False)
        self.assertEqual([], killed)
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual(
            {101: "preserved_registered_watch", 102: "reported_only", 103: "reported_only"},
            actions,
        )

    def test_cleanup_terminates_only_unregistered_identity_matches(self):
        findings, killed = self.audit(clean=True)
        self.assertEqual([102, 103], sorted(killed))
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual("preserved_registered_watch", actions[101])
        self.assertEqual("terminated", actions[102])
        self.assertEqual("terminated", actions[103])
        self.assertNotIn(104, actions)
        self.assertNotIn(105, actions)
        self.assertNotIn(106, actions, "one-shot chat-mentioning command must never match")
        self.assertNotIn(200, actions, "own ancestor chain must never be terminated")
        self.assertNotIn(99999, actions)
        for finding in findings:
            self.assertIn("matched", finding)
            self.assertIn("command", finding)

    def test_already_exited_process_is_reported_not_fatal(self):
        def raising_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        findings, _ = self.audit(clean=True, terminate=raising_kill)
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual("already_exited", actions[102])

    def test_ps_output_parsing(self):
        rows = lease_lib.poller_process_rows(
            "  201 1 python3 x.py\n\nbadline\n 202 201 /bin/zsh -c loop\n"
        )
        self.assertEqual(
            [
                {"pid": 201, "ppid": 1, "command": "python3 x.py"},
                {"pid": 202, "ppid": 201, "command": "/bin/zsh -c loop"},
            ],
            rows,
        )

    def test_ancestor_chain_resolution(self):
        rows = [
            {"pid": 1, "ppid": 0, "command": "init"},
            {"pid": 10, "ppid": 1, "command": "zsh"},
            {"pid": 20, "ppid": 10, "command": "python claim"},
        ]
        self.assertEqual({20, 10, 1, 0}, lease_lib.ancestor_pids(rows, 20))


class ExpiryParsingTest(unittest.TestCase):
    def test_future_lease_not_expired(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        self.assertFalse(lease_lib.lease_is_expired({"lease_expires_utc": future}))

    def test_past_lease_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        self.assertTrue(lease_lib.lease_is_expired({"lease_expires_utc": past}))


if __name__ == "__main__":
    unittest.main()
