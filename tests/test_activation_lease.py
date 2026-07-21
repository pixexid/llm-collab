from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "bin" / "session_autobridge.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _activation_lease as lease_lib


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
    write(path, json.dumps(payload, indent=2))


class ActivationLeaseTest(unittest.TestCase):
    READER_ENV_VARS = (
        "LLM_COLLAB_READER_RUNTIME_ID",
        "LLM_COLLAB_READER_PID",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_SESSION_ID",
        "GEMINI_SESSION_ID",
    )

    def make_workspace(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="llm-collab-lease-"))
        write_json(
            root / "collab.config.json",
            {
                "workspace_name": "test-collab",
                "schema_version": 2,
                "projects_root": str(root),
                "poll_interval_seconds": 15,
                "notifications_enabled": False,
            },
        )
        write_json(
            root / "projects.json",
            {"projects": [{"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}}]},
        )
        write_json(root / "agents.json", {"agents": []})
        self.add_agent(root, "claude")
        self.worktree = root / "worktrees" / "lane"
        self.worktree.mkdir(parents=True)
        return root

    def add_agent(self, root: Path, agent_id: str) -> None:
        agents_file = root / "agents.json"
        agents = json.loads(agents_file.read_text())
        agents["agents"].append(
            {
                "id": agent_id,
                "display_name": agent_id.title(),
                "activation": {"type": "cli_session", "watcher_enabled": False},
            }
        )
        write_json(agents_file, agents)
        write(root / "agents" / agent_id / "identity.md", f"# Identity: {agent_id}\n")
        write_json(root / "agents" / agent_id / "inbox.json", {"agent": agent_id, "unread": [], "read": []})

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k not in self.READER_ENV_VARS}
        return {**base, "LLM_COLLAB_UI_REFRESH": "0", **(extra or {})}

    def run_cli(self, root: Path, *args: str, env: dict[str, str] | None = None) -> tuple[dict, int]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.env(env),
            check=False,
        )
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout), result.returncode

    def run_raw_cli(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.env(),
            check=False,
        )

    def identity_args(
        self,
        worktree: Path | None = None,
        *,
        project: str = "amiga",
    ) -> list[str]:
        selected_worktree = worktree or self.worktree
        return [
            "--project", project,
            "--chat", "CHAT-TEST0001",
            "--task", "TASK-TEST01",
            "--worktree", str(selected_worktree),
            "--branch", "codex/gh-1571-test",
            "--target-agent", "claude",
        ]

    def register_session(
        self,
        root: Path,
        session: str,
        *,
        agent: str = "claude",
        project: str | None = "amiga",
        chat: str | None = "CHAT-TEST0001",
        runtime_id: str | None = None,
    ) -> None:
        args = ["register", "--session", session, "--agent", agent, "--mode", "manual", "--status", "parked"]
        if project is not None:
            args += ["--project", project]
        if chat is not None:
            args += ["--chat", chat]
        if runtime_id:
            args += [
                "--runtime-family", "claude_app",
                "--runtime-session-id", runtime_id,
                "--runtime-session-source", "test_fixture",
            ]
        payload, code = self.run_cli(root, *args)
        self.assertEqual(0, code, payload)

    def update_session_status(self, root: Path, session: str, status: str) -> None:
        path = root / "State" / "session_autobridge" / "sessions" / f"{session}.json"
        payload = json.loads(path.read_text())
        payload["status"] = status
        write_json(path, payload)

    def expire_session_lease(self, root: Path, session: str) -> None:
        path = root / "State" / "session_autobridge" / "sessions" / f"{session}.json"
        payload = json.loads(path.read_text())
        payload["lease_expires_utc"] = "2000-01-01T00:00:00+00:00"
        write_json(path, payload)

    def claim(self, root: Path, session: str, *extra: str, worktree: Path | None = None) -> tuple[dict, int]:
        return self.run_cli(root, "lease-claim", *self.identity_args(worktree), "--session", session, *extra)

    def lease_records(self, root: Path) -> list[dict]:
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"
        return [
            json.loads(path.read_text())
            for path in sorted(lease_dir.glob("*.json"))
        ]

    def test_claim_requires_bound_claimant_identity(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        refused, code = self.claim(root, "SESSION-A")
        self.assertEqual(75, code)
        self.assertEqual("claimant_identity_required", refused["reason"])

        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        self.assertEqual("runtime-a", claimed["lease"]["owner_runtime_session_id"])

    def test_claim_rejects_registered_session_runtime_fallback(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        refused, code = self.claim(root, "SESSION-A")
        self.assertEqual(75, code)
        self.assertEqual("claimant_identity_required", refused["reason"])
        self.assertEqual([], self.lease_records(root))

    def test_identityless_processes_cannot_dual_grant_from_session_runtime(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        start_file = root / "start-identityless"
        output_a = root / "identityless-a.json"
        output_b = root / "identityless-b.json"
        worker_code = "\n".join(
            [
                "import json, os, subprocess, sys, time",
                "script, root, start, output = sys.argv[1:5]",
                "cmd = [sys.executable, script, 'lease-claim', *sys.argv[5:], '--json']",
                "while not os.path.exists(start):",
                "    time.sleep(0.005)",
                "result = subprocess.run(cmd, cwd=root, text=True, capture_output=True)",
                "payload = json.loads(result.stdout)",
                "payload['_returncode'] = result.returncode",
                "open(output, 'w').write(json.dumps(payload, sort_keys=True))",
            ]
        )
        common_env = self.env()
        proc_a = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_code,
                str(SCRIPT_PATH),
                str(root),
                str(start_file),
                str(output_a),
                *self.identity_args(),
                "--session",
                "SESSION-A",
            ],
            cwd=root,
            env=common_env,
        )
        proc_b = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_code,
                str(SCRIPT_PATH),
                str(root),
                str(start_file),
                str(output_b),
                *self.identity_args(),
                "--session",
                "SESSION-B",
            ],
            cwd=root,
            env=common_env,
        )
        start_file.write_text("go")
        self.assertEqual(0, proc_a.wait(timeout=10))
        self.assertEqual(0, proc_b.wait(timeout=10))
        results = [json.loads(output_a.read_text()), json.loads(output_b.read_text())]
        self.assertEqual(
            ["claimant_identity_required", "claimant_identity_required"],
            sorted(result["reason"] for result in results),
        )
        self.assertEqual([75, 75], sorted(result["_returncode"] for result in results))
        self.assertEqual([], self.lease_records(root))

    def test_pid_only_claim_requires_positive_process_pid_cli_and_library(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        for bad_pid in ("0", "-1"):
            refused, code = self.claim(root, "SESSION-A", "--owner-pid", bad_pid)
            self.assertEqual(75, code)
            self.assertEqual("invalid_owner_pid", refused["reason"])
        self.assertEqual([], self.lease_records(root))

        identity = lease_lib.lease_identity(
            {
                "project": "amiga",
                "chat": "CHAT-TEST0001",
                "task": "TASK-TEST01",
                "worktree": str(self.worktree),
                "branch": "codex/gh-1571-test",
                "target_agent": "claude",
            }
        )
        record = {
            "session_id": "SESSION-A",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-TEST0001",
            "status": "parked",
        }
        with patch.object(lease_lib, "owner_session_record", return_value=record):
            with self.assertRaises(lease_lib.LeaseRefused) as ctx:
                lease_lib.claim_lease(identity, owner_session_id="SESSION-A", owner_pid=0)
            self.assertEqual("invalid_owner_pid", ctx.exception.reason)
            with self.assertRaises(lease_lib.LeaseRefused) as ctx:
                lease_lib.claim_lease(identity, owner_session_id="SESSION-A", owner_pid=-7)
            self.assertEqual("invalid_owner_pid", ctx.exception.reason)

    def test_runtime_claim_refuses_dead_positive_owner_pid(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        sleeper = subprocess.Popen([sys.executable, "-c", "pass"])
        sleeper.wait(timeout=5)

        refused, code = self.claim(
            root,
            "SESSION-A",
            "--claimant-runtime-id",
            "runtime-a",
            "--owner-pid",
            str(sleeper.pid),
        )
        self.assertEqual(75, code)
        self.assertEqual("owner_pid_not_live", refused["reason"])
        self.assertEqual([], self.lease_records(root))

    def test_session_identity_must_match_activation_identity(self):
        root = self.make_workspace()
        self.add_agent(root, "codex")
        self.register_session(root, "SESSION-CODEX", agent="codex", runtime_id="runtime-c")
        refused, code = self.claim(root, "SESSION-CODEX", "--claimant-runtime-id", "runtime-c")
        self.assertEqual(75, code)
        self.assertEqual("owner_session_identity_mismatch", refused["reason"])
        self.assertEqual("agent_id", refused["owner"]["field"])

        self.register_session(root, "SESSION-NULLCHAT", chat=None, runtime_id="runtime-a")
        refused, code = self.claim(root, "SESSION-NULLCHAT", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(75, code)
        self.assertEqual("owner_session_identity_mismatch", refused["reason"])
        self.assertEqual("chat_id", refused["owner"]["field"])

    def test_refused_claim_leaves_record_byte_identical_and_names_owner(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        first, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, first)
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"
        before = {path.name: path.read_text() for path in lease_dir.glob("*.json")}

        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b")
        self.assertEqual(75, code)
        self.assertEqual("lease_held_by_active_owner", refused["reason"])
        self.assertEqual("SESSION-A", refused["owner"]["owner_session_id"])
        after = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        self.assertEqual(before, after)

    def test_different_session_same_runtime_cannot_reclaim_without_takeover(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-shared")
        self.register_session(root, "SESSION-B", runtime_id="runtime-shared")
        first, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-shared")
        self.assertEqual(0, code, first)
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"
        before = {path.name: path.read_text() for path in lease_dir.glob("*.json")}

        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-shared")
        self.assertEqual(75, code)
        self.assertEqual("lease_held_by_active_owner", refused["reason"])
        self.assertEqual("SESSION-A", refused["owner"]["owner_session_id"])
        after = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        self.assertEqual(before, after)

    def test_runtime_only_reclaim_is_idempotent_and_refreshes_ttl(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        first, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--ttl-seconds", "60")
        self.assertEqual(0, code, first)
        again, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--ttl-seconds", "3600")
        self.assertEqual(0, code, again)
        self.assertEqual(1, again["lease"]["fence_token"])
        self.assertIsNone(again["lease"]["owner_pid"])
        self.assertGreater(
            again["lease"]["lease_expires_utc"],
            first["lease"]["lease_expires_utc"],
        )

    def test_expired_same_identity_runtime_only_requires_takeover_and_increments_fence(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        first, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--ttl-seconds", "0")
        self.assertEqual(0, code, first)

        refused, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(75, code)
        self.assertEqual("lease_expired_requires_takeover", refused["reason"])

        taken, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertEqual(2, taken["lease"]["fence_token"])
        self.assertEqual("SESSION-A", taken["lease"]["previous_owner_session_id"])

    def test_expired_same_identity_same_pid_requires_takeover(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        pid = str(os.getpid())
        first, code = self.claim(
            root,
            "SESSION-A",
            "--claimant-runtime-id",
            "runtime-a",
            "--owner-pid",
            pid,
            "--ttl-seconds",
            "0",
        )
        self.assertEqual(0, code, first)

        refused, code = self.claim(
            root,
            "SESSION-A",
            "--claimant-runtime-id",
            "runtime-a",
            "--owner-pid",
            pid,
        )
        self.assertEqual(75, code)
        self.assertEqual("lease_expired_requires_takeover", refused["reason"])

    def test_dead_owner_same_identity_requires_takeover(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
        try:
            claimed, code = self.claim(
                root,
                "SESSION-A",
                "--claimant-runtime-id",
                "runtime-a",
                "--owner-pid",
                str(sleeper.pid),
            )
            self.assertEqual(0, code, claimed)
        finally:
            sleeper.wait(timeout=5)

        refused, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(75, code)
        self.assertEqual("dead_owner_requires_takeover", refused["reason"])

        taken, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertEqual(2, taken["lease"]["fence_token"])

    def test_stopped_or_superseded_owner_session_overrides_live_recorded_pid(self):
        for status in ("stopped", "superseded"):
            with self.subTest(status=status):
                root = self.make_workspace()
                self.register_session(root, "SESSION-A", runtime_id="runtime-a")
                self.register_session(root, "SESSION-B", runtime_id="runtime-b")
                claimed, code = self.claim(
                    root,
                    "SESSION-A",
                    "--claimant-runtime-id",
                    "runtime-a",
                    "--owner-pid",
                    str(os.getpid()),
                )
                self.assertEqual(0, code, claimed)
                self.update_session_status(root, "SESSION-A", status)
                lease_dir = root / "State" / "session_autobridge" / "activation_leases"
                before = {path.name: path.read_text() for path in lease_dir.glob("*.json")}

                refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b")
                self.assertEqual(75, code)
                self.assertEqual("dead_owner_requires_takeover", refused["reason"])
                after = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
                self.assertEqual(before, after)

                taken, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", "--takeover")
                self.assertEqual(0, code, taken)
                self.assertEqual(2, taken["lease"]["fence_token"])
                self.assertEqual("SESSION-A", taken["lease"]["previous_owner_session_id"])

    def test_expired_parked_owner_session_takeover_blocks_old_assert_and_release(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        fence = str(claimed["lease"]["fence_token"])
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"

        self.expire_session_lease(root, "SESSION-A")
        before = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b")
        self.assertEqual(75, code)
        self.assertEqual("dead_owner_requires_takeover", refused["reason"])
        self.assertEqual(before, {path.name: path.read_text() for path in lease_dir.glob("*.json")})

        taken, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertEqual(2, taken["lease"]["fence_token"])
        self.assertEqual("SESSION-A", taken["lease"]["previous_owner_session_id"])

        after_takeover = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            fence,
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("owner_session_not_live", refused["reason"])
        self.assertEqual(
            "2000-01-01T00:00:00+00:00",
            refused["owner"]["owner_session_lease_expires_utc"],
        )
        self.assertEqual(after_takeover, {path.name: path.read_text() for path in lease_dir.glob("*.json")})

        refused, code = self.run_cli(
            root,
            "lease-release",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            fence,
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("owner_session_not_live", refused["reason"])
        self.assertEqual(after_takeover, {path.name: path.read_text() for path in lease_dir.glob("*.json")})

    def test_expired_takeover_invalidates_old_fence_token(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        first, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--ttl-seconds", "0")
        self.assertEqual(0, code, first)
        old_fence = str(first["lease"]["fence_token"])

        taken, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertGreater(taken["lease"]["fence_token"], first["lease"]["fence_token"])

        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            old_fence,
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("stale_fence_token", refused["reason"])

    def test_same_session_different_runtime_assert_refuses(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        fence = str(claimed["lease"]["fence_token"])

        ok, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session", "SESSION-A",
            "--fence-token", fence,
            "--claimant-runtime-id", "runtime-a",
        )
        self.assertEqual(0, code, ok)

        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session", "SESSION-A",
            "--fence-token", fence,
            "--claimant-runtime-id", "runtime-b",
        )
        self.assertEqual(75, code)
        self.assertEqual("claimant_runtime_mismatch", refused["reason"])

    def test_assert_requires_asserting_runtime_not_session_record_fallback(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        self.assertEqual("runtime-a", claimed["lease"]["owner_runtime_session_id"])

        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            str(claimed["lease"]["fence_token"]),
        )
        self.assertEqual(75, code)
        self.assertEqual("claimant_runtime_identity_required", refused["reason"])

    def test_assert_refuses_stopped_and_superseded_owner_sessions(self):
        for status in ("stopped", "superseded"):
            with self.subTest(status=status):
                root = self.make_workspace()
                self.register_session(root, "SESSION-A", runtime_id="runtime-a")
                claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
                self.assertEqual(0, code, claimed)
                self.update_session_status(root, "SESSION-A", status)

                refused, code = self.run_cli(
                    root,
                    "lease-assert",
                    *self.identity_args(),
                    "--session",
                    "SESSION-A",
                    "--fence-token",
                    str(claimed["lease"]["fence_token"]),
                    "--claimant-runtime-id",
                    "runtime-a",
                )
                self.assertEqual(75, code)
                self.assertEqual("owner_session_not_live", refused["reason"])

    def test_assert_requires_the_recorded_owner_session(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)

        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session",
            "SESSION-B",
            "--fence-token",
            str(claimed["lease"]["fence_token"]),
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("lease_owned_by_other_session", refused["reason"])
        self.assertEqual("SESSION-A", refused["owner"]["owner_session_id"])

    def test_wrong_fence_assert_refuses_stale_fence_token(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")

        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            "99",
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("stale_fence_token", refused["reason"])

    def test_assert_requires_fence_token_at_cli(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "lease-assert",
                *self.identity_args(),
                "--session", "SESSION-A",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.env(),
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--fence-token", result.stderr)

    def test_lease_commands_reject_unknown_project_before_identity_access(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        unknown_identity = self.identity_args(project="unknown-project")
        cases = [
            ("lease-claim", [*unknown_identity, "--session", "SESSION-A", "--claimant-runtime-id", "runtime-a"]),
            ("lease-show", unknown_identity),
            (
                "lease-assert",
                [*unknown_identity, "--session", "SESSION-A", "--fence-token", "1", "--claimant-runtime-id", "runtime-a"],
            ),
            (
                "lease-release",
                [*unknown_identity, "--session", "SESSION-A", "--fence-token", "1", "--claimant-runtime-id", "runtime-a"],
            ),
        ]
        for command, args in cases:
            with self.subTest(command=command):
                result = self.run_raw_cli(root, command, *args)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn("Unknown project_id: 'unknown-project'", result.stderr)
        self.assertEqual([], self.lease_records(root))

    def test_lease_show_accepts_registered_project(self):
        root = self.make_workspace()
        shown, code = self.run_cli(root, "lease-show", *self.identity_args())
        self.assertEqual(0, code, shown)
        self.assertIsNone(shown["lease"])
        self.assertEqual("amiga", shown["identity"]["project"])

    def test_alias_collapse_refuses_symlink_claim_under_lock(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        alias = self.worktree.parent / "lane-alias"
        alias.symlink_to(self.worktree)
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)

        refused, code = self.claim(
            root,
            "SESSION-B",
            "--claimant-runtime-id",
            "runtime-b",
            worktree=alias,
        )
        self.assertEqual(75, code)
        self.assertEqual("worktree_alias_collision", refused["reason"])
        self.assertEqual("SESSION-A", refused["owner"]["owner_session_id"])

        other = self.worktree.parent / "other-lane"
        other.mkdir()
        granted, code = self.claim(
            root,
            "SESSION-B",
            "--claimant-runtime-id",
            "runtime-b",
            worktree=other,
        )
        self.assertEqual(0, code, granted)

    def test_concurrent_alias_claim_serializes_grant_across_identity_keys(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        alias = self.worktree.parent / "lane-alias"
        alias.symlink_to(self.worktree)
        start_file = root / "start-claims"
        output_a = root / "claim-a.json"
        output_b = root / "claim-b.json"
        worker_code = "\n".join(
            [
                "import json, os, subprocess, sys, time",
                "script, root, start, output = sys.argv[1:5]",
                "cmd = [sys.executable, script, 'lease-claim', *sys.argv[5:], '--json']",
                "while not os.path.exists(start):",
                "    time.sleep(0.005)",
                "result = subprocess.run(cmd, cwd=root, text=True, capture_output=True)",
                "payload = json.loads(result.stdout)",
                "payload['_returncode'] = result.returncode",
                "open(output, 'w').write(json.dumps(payload, sort_keys=True))",
            ]
        )
        common_env = self.env()
        proc_a = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_code,
                str(SCRIPT_PATH),
                str(root),
                str(start_file),
                str(output_a),
                *self.identity_args(self.worktree),
                "--session",
                "SESSION-A",
                "--claimant-runtime-id",
                "runtime-a",
            ],
            cwd=root,
            env=common_env,
        )
        proc_b = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_code,
                str(SCRIPT_PATH),
                str(root),
                str(start_file),
                str(output_b),
                *self.identity_args(alias),
                "--session",
                "SESSION-B",
                "--claimant-runtime-id",
                "runtime-b",
            ],
            cwd=root,
            env=common_env,
        )
        start_file.write_text("go")
        self.assertEqual(0, proc_a.wait(timeout=10))
        self.assertEqual(0, proc_b.wait(timeout=10))
        results = [json.loads(output_a.read_text()), json.loads(output_b.read_text())]
        winners = [result for result in results if result.get("_returncode") == 0]
        losers = [result for result in results if result.get("_returncode") == 75]
        self.assertEqual(1, len(winners), results)
        self.assertEqual(1, len(losers), results)
        self.assertTrue(winners[0]["claimed"])
        self.assertIn(losers[0]["reason"], {"worktree_alias_collision", "claim_in_progress"})
        if losers[0]["reason"] == "claim_in_progress":
            if winners[0]["lease"]["owner_session_id"] == "SESSION-A":
                retried, code = self.claim(
                    root,
                    "SESSION-B",
                    "--claimant-runtime-id",
                    "runtime-b",
                    worktree=alias,
                )
            else:
                retried, code = self.claim(
                    root,
                    "SESSION-A",
                    "--claimant-runtime-id",
                    "runtime-a",
                    worktree=self.worktree,
                )
            self.assertEqual(75, code)
            self.assertEqual("worktree_alias_collision", retried["reason"])
        records = self.lease_records(root)
        self.assertEqual(1, len(records), records)
        self.assertEqual(str(self.worktree.resolve()), records[0]["worktree_realpath"])

    def test_alias_collision_refuses_unknown_liveness_owner(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        alias = self.worktree.parent / "lane-alias"
        alias.symlink_to(self.worktree)
        identity = lease_lib.lease_identity(
            {
                "project": "amiga",
                "chat": "CHAT-TEST0001",
                "task": "TASK-TEST01",
                "worktree": str(self.worktree),
                "branch": "codex/gh-1571-test",
                "target_agent": "claude",
            }
        )
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"
        stale = {
            "identity": identity,
            "lease_key": lease_lib.lease_key(identity),
            "owner_session_id": "SESSION-MISSING",
            "owner_runtime_session_id": None,
            "owner_pid": None,
            "status": "active",
            "fence_token": 1,
            "claimed_utc": "2026-01-01T00:00:00+00:00",
            "lease_expires_utc": "2099-01-01T00:00:00+00:00",
            "previous_owner_session_id": None,
            "worktree_realpath": str(self.worktree.resolve()),
        }
        write_json(lease_dir / f"{stale['lease_key']}.json", stale)
        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", worktree=alias)
        self.assertEqual(75, code)
        self.assertEqual("worktree_alias_collision", refused["reason"])

    def test_corrupt_alias_lease_json_fails_closed_without_mutation(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"
        corrupt = lease_dir / "bad-alias-candidate.json"
        write(corrupt, "{not-json")
        before = {path.name: path.read_text() for path in lease_dir.glob("*.json")}

        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b")
        self.assertEqual(75, code)
        self.assertEqual("corrupt_lease_state", refused["reason"])
        self.assertEqual(
            {"lease_file": "bad-alias-candidate.json", "error": "JSONDecodeError"},
            refused["owner"],
        )
        after = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        self.assertEqual(before, after)

    def test_released_and_expired_alias_records_do_not_overblock_new_identity(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        alias = self.worktree.parent / "lane-alias"
        alias.symlink_to(self.worktree)
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        released, code = self.run_cli(
            root,
            "lease-release",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            str(claimed["lease"]["fence_token"]),
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(0, code, released)
        granted, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", worktree=alias)
        self.assertEqual(0, code, granted)

        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        alias = self.worktree.parent / "expired-alias"
        alias.symlink_to(self.worktree)
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--ttl-seconds", "0")
        self.assertEqual(0, code, claimed)
        granted, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", worktree=alias)
        self.assertEqual(0, code, granted)

    def test_expired_owner_requires_takeover_and_assert_refuses_expired(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a", "--ttl-seconds", "0")
        self.assertEqual(0, code, claimed)

        refused, code = self.run_cli(
            root,
            "lease-assert",
            *self.identity_args(),
            "--session", "SESSION-A",
            "--fence-token", "1",
            "--claimant-runtime-id", "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("lease_expired", refused["reason"])

        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b")
        self.assertEqual(75, code)
        self.assertEqual("lease_expired_requires_takeover", refused["reason"])

        taken, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertEqual(2, taken["lease"]["fence_token"])

    def test_live_positive_pid_owner_cannot_be_taken_over(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(
            root,
            "SESSION-A",
            "--claimant-runtime-id",
            "runtime-a",
            "--owner-pid",
            str(os.getpid()),
        )
        self.assertEqual(0, code, claimed)
        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", "--takeover")
        self.assertEqual(75, code)
        self.assertEqual("lease_held_by_active_owner", refused["reason"])

    def test_unknown_liveness_fails_closed_for_same_identity(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        identity = lease_lib.lease_identity(
            {
                "project": "amiga",
                "chat": "CHAT-TEST0001",
                "task": "TASK-TEST01",
                "worktree": str(self.worktree),
                "branch": "codex/gh-1571-test",
                "target_agent": "claude",
            }
        )
        stale = {
            "identity": identity,
            "lease_key": lease_lib.lease_key(identity),
            "owner_session_id": "SESSION-MISSING",
            "owner_runtime_session_id": "runtime-missing",
            "owner_pid": None,
            "status": "active",
            "fence_token": 1,
            "claimed_utc": "2026-01-01T00:00:00+00:00",
            "lease_expires_utc": "2099-01-01T00:00:00+00:00",
            "previous_owner_session_id": None,
            "worktree_realpath": str(self.worktree.resolve()),
        }
        write_json(
            root
            / "State"
            / "session_autobridge"
            / "activation_leases"
            / f"{stale['lease_key']}.json",
            stale,
        )
        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", "--takeover")
        self.assertEqual(75, code)
        self.assertEqual("owner_liveness_unknown", refused["reason"])

    def test_dead_owner_requires_explicit_takeover(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        self.update_session_status(root, "SESSION-A", "stopped")

        refused, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b")
        self.assertEqual(75, code)
        self.assertEqual("dead_owner_requires_takeover", refused["reason"])

        taken, code = self.claim(root, "SESSION-B", "--claimant-runtime-id", "runtime-b", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertEqual(2, taken["lease"]["fence_token"])
        self.assertEqual("SESSION-A", taken["lease"]["previous_owner_session_id"])

    def test_release_requires_current_owner_and_fence(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        fence = str(claimed["lease"]["fence_token"])
        lease_dir = root / "State" / "session_autobridge" / "activation_leases"

        refused, code = self.run_cli(root, "lease-release", *self.identity_args(), "--session", "SESSION-B", "--fence-token", fence)
        self.assertEqual(75, code)
        self.assertEqual("release_requires_current_owner", refused["reason"])

        before = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        unbound, code = self.run_cli(root, "lease-release", *self.identity_args(), "--session", "SESSION-A", "--fence-token", fence)
        self.assertEqual(75, code)
        self.assertEqual("claimant_runtime_identity_required", unbound["reason"])
        after_unbound = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        self.assertEqual(before, after_unbound)

        stale, code = self.run_cli(
            root,
            "lease-release",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            "99",
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(75, code)
        self.assertEqual("stale_fence_token", stale["reason"])
        after = {path.name: path.read_text() for path in lease_dir.glob("*.json")}
        self.assertEqual(before, after)

        released, code = self.run_cli(
            root,
            "lease-release",
            *self.identity_args(),
            "--session",
            "SESSION-A",
            "--fence-token",
            fence,
            "--claimant-runtime-id",
            "runtime-a",
        )
        self.assertEqual(0, code, released)
        self.assertTrue(released["released"])

    def test_flock_contention_maps_only_contention_errnos(self):
        root = self.make_workspace()
        identity = lease_lib.lease_identity(
            {
                "project": "amiga",
                "chat": "CHAT-TEST0001",
                "task": "TASK-TEST01",
                "worktree": str(self.worktree),
                "branch": "codex/gh-1571-test",
                "target_agent": "claude",
            }
        )
        lock_path = lease_lib.lease_path(identity).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(lease_lib.LeaseRefused) as ctx:
                with lease_lib._ClaimLock(identity):
                    pass
            self.assertEqual("claim_in_progress", ctx.exception.reason)

        with patch("fcntl.flock", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                with lease_lib._ClaimLock(identity):
                    pass

        grant_lock_path = lease_lib.ACTIVATION_GRANT_LOCK
        grant_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with grant_lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(lease_lib.LeaseRefused) as ctx:
                with lease_lib._claim_grant_lock():
                    pass
            self.assertEqual("claim_in_progress", ctx.exception.reason)

        with patch("fcntl.flock", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                with lease_lib._claim_grant_lock():
                    pass

    def test_global_claim_grant_lock_contention_returns_bounded_refusal(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        ready = root / "grant-lock-ready"
        locker_code = "\n".join(
            [
                "import fcntl, os, sys, time",
                "from pathlib import Path",
                "root, ready = sys.argv[1:3]",
                "path = Path(root) / 'State' / 'session_autobridge' / 'activation_leases' / '.claim-grant.lock'",
                "path.parent.mkdir(parents=True, exist_ok=True)",
                "fd = os.open(path, os.O_CREAT | os.O_RDWR)",
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                "Path(ready).write_text('locked')",
                "time.sleep(5)",
            ]
        )
        locker = subprocess.Popen([sys.executable, "-c", locker_code, str(root), str(ready)])
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "grant lock holder did not start")

            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "lease-claim",
                    *self.identity_args(),
                    "--session",
                    "SESSION-A",
                    "--claimant-runtime-id",
                    "runtime-a",
                    "--json",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                env=self.env(),
                timeout=2,
                check=False,
            )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
            self.assertEqual(75, result.returncode, result.stdout + result.stderr)
            self.assertEqual("claim_in_progress", json.loads(result.stdout)["reason"])
        finally:
            locker.terminate()
            try:
                locker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                locker.kill()
                locker.wait(timeout=5)

    def test_crashed_lock_holder_frees_kernel_flock(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        identity_payload = {
            "project": "amiga",
            "chat": "CHAT-TEST0001",
            "task": "TASK-TEST01",
            "worktree": str(self.worktree),
            "branch": "codex/gh-1571-test",
            "target_agent": "claude",
        }
        locker = (
            "import json, os, sys; "
            f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r}); "
            "import _activation_lease as l; "
            f"os.chdir({str(root)!r}); "
            f"identity=l.lease_identity({identity_payload!r}); "
            "lock=l._ClaimLock(identity); lock.__enter__(); os._exit(0)"
        )
        subprocess.run([sys.executable, "-c", locker], cwd=root, check=True)
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)


if __name__ == "__main__":
    unittest.main()
