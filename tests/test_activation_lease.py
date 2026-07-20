from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import sys
import tempfile
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

    def identity_args(self, worktree: Path | None = None) -> list[str]:
        selected_worktree = worktree or self.worktree
        return [
            "--project", "amiga",
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

    def claim(self, root: Path, session: str, *extra: str, worktree: Path | None = None) -> tuple[dict, int]:
        return self.run_cli(root, "lease-claim", *self.identity_args(worktree), "--session", session, *extra)

    def test_claim_requires_bound_claimant_identity(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        refused, code = self.claim(root, "SESSION-A")
        self.assertEqual(75, code)
        self.assertEqual("claimant_identity_required", refused["reason"])

        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        self.assertEqual("runtime-a", claimed["lease"]["owner_runtime_session_id"])

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

    def test_release_requires_current_owner_and_fence(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A", runtime_id="runtime-a")
        self.register_session(root, "SESSION-B", runtime_id="runtime-b")
        claimed, code = self.claim(root, "SESSION-A", "--claimant-runtime-id", "runtime-a")
        self.assertEqual(0, code, claimed)
        fence = str(claimed["lease"]["fence_token"])

        refused, code = self.run_cli(root, "lease-release", *self.identity_args(), "--session", "SESSION-B", "--fence-token", fence)
        self.assertEqual(75, code)
        self.assertEqual("release_requires_current_owner", refused["reason"])

        released, code = self.run_cli(root, "lease-release", *self.identity_args(), "--session", "SESSION-A", "--fence-token", fence)
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
