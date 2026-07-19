from __future__ import annotations

import json
import os
import signal
import stat
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


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
    write(path, json.dumps(payload, indent=2))


def identity_args(worktree: str) -> list[str]:
    return [
        "--project", "amiga",
        "--chat", "CHAT-TEST0001",
        "--task", "TASK-TEST01",
        "--worktree", worktree,
        "--branch", "claude/gh-0000-test",
        "--target-agent", "claude",
    ]


def identity_dict(worktree: str) -> dict[str, str]:
    return {
        "project": "amiga",
        "chat": "CHAT-TEST0001",
        "task": "TASK-TEST01",
        "worktree": worktree,
        "branch": "claude/gh-0000-test",
        "target_agent": "claude",
    }


class WorkspaceTestCase(unittest.TestCase):
    def make_workspace(self) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix="llm-collab-lease-"))
        write_json(
            temp_root / "collab.config.json",
            {
                "workspace_name": "test-collab",
                "schema_version": 2,
                "projects_root": str(temp_root),
                "poll_interval_seconds": 15,
                "notifications_enabled": False,
            },
        )
        write_json(
            temp_root / "projects.json",
            {"projects": [{"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}}]},
        )
        write_json(temp_root / "agents.json", {"agents": []})
        self.add_agent(
            temp_root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": False},
            },
        )
        # Stub pm2 registry: present, healthy, empty.
        pm2_stub = temp_root / "pm2-stub.sh"
        write(pm2_stub, "#!/bin/sh\necho '[]'\n")
        pm2_stub.chmod(pm2_stub.stat().st_mode | stat.S_IEXEC)
        self.pm2_stub = str(pm2_stub)
        # Isolated process-table fixture: subprocesses must NEVER audit the
        # host's real ps output (a stubbed registry would misclassify real
        # registered watchers and kill them).
        ps_fixture = temp_root / "ps-fixture.txt"
        write(ps_fixture, "1 0 /sbin/launchd\n")
        self.ps_fixture = str(ps_fixture)
        # Canonical worktree path that actually exists.
        worktree = temp_root / "worktrees" / "t-test"
        worktree.mkdir(parents=True)
        self.worktree = str(worktree.resolve())
        return temp_root

    def add_agent(self, root: Path, agent: dict) -> None:
        agents_file = root / "agents.json"
        payload = json.loads(agents_file.read_text()) if agents_file.exists() else {"agents": []}
        payload["agents"].append(agent)
        write_json(agents_file, payload)
        write(root / "agents" / agent["id"] / "identity.md", f"# Identity: {agent['id']}\n")
        write_json(
            root / "agents" / agent["id"] / "inbox.json",
            {"agent": agent["id"], "unread": [], "read": []},
        )

    def cli_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        return {
            **os.environ,
            "LLM_COLLAB_UI_REFRESH": "0",
            "LLM_COLLAB_PM2_BIN": self.pm2_stub,
            "LLM_COLLAB_PS_FIXTURE": self.ps_fixture,
            **(env or {}),
        }

    def run_cli(self, root: Path, *args: str, env: dict[str, str] | None = None) -> tuple[dict, int]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.cli_env(env),
            check=False,
        )
        self.assertTrue(result.stdout.strip(), f"no stdout; stderr: {result.stderr}")
        return json.loads(result.stdout), result.returncode

    def register_session(self, root: Path, session: str, *, runtime_id: str | None = None) -> None:
        args = [
            "register",
            "--session", session,
            "--agent", "claude",
            "--project", "amiga",
            "--chat", "CHAT-TEST0001",
            "--mode", "manual",
            "--status", "parked",
        ]
        if runtime_id:
            args += [
                "--runtime-family", "claude_app",
                "--runtime-session-id", runtime_id,
                "--runtime-session-source", "test_fixture",
            ]
        payload, code = self.run_cli(root, *args)
        self.assertEqual(0, code, payload)

    def claim(self, root: Path, session: str, *extra: str, worktree: str | None = None) -> tuple[dict, int]:
        return self.run_cli(
            root,
            "lease-claim",
            *identity_args(worktree or self.worktree),
            "--session",
            session,
            "--skip-poller-cleanup",
            *extra,
        )


class ActivationLeaseCliTest(WorkspaceTestCase):
    def test_claim_requires_registered_live_owner_session(self):
        root = self.make_workspace()
        refused, code = self.claim(root, "SESSION-GHOST")
        self.assertEqual(75, code)
        self.assertEqual("owner_session_not_registered", refused["reason"])

    def test_second_session_fails_closed_and_names_owner(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.register_session(root, "SESSION-B")
        first, code = self.claim(root, "SESSION-A")
        self.assertEqual(0, code, first)
        self.assertTrue(first["claimed"])
        self.assertEqual(1, first["lease"]["fence_token"])

        lease_dir = Path(root) / "State" / "session_autobridge" / "activation_leases"
        before = {p.name: p.read_text() for p in lease_dir.glob("*.json")}

        second, code = self.claim(root, "SESSION-B")
        self.assertEqual(75, code)
        self.assertFalse(second["claimed"])
        self.assertEqual("lease_held_by_active_owner", second["reason"])
        self.assertEqual("SESSION-A", second["owner"]["owner_session_id"])

        after = {p.name: p.read_text() for p in lease_dir.glob("*.json")}
        self.assertEqual(before, after, "refused claim must not mutate the lease record")

    def test_equivalent_worktree_paths_hit_the_same_lease(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.register_session(root, "SESSION-B")
        self.claim(root, "SESSION-A")

        sneaky = str(Path(self.worktree).parent / ".." / Path(self.worktree).parent.name / "t-test")
        refused, code = self.claim(root, "SESSION-B", worktree=sneaky)
        self.assertEqual(75, code)
        self.assertEqual("lease_held_by_active_owner", refused["reason"])

    def test_reclaim_by_same_owner_is_idempotent(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.claim(root, "SESSION-A")
        again, code = self.claim(root, "SESSION-A")
        self.assertEqual(0, code, again)
        self.assertEqual(1, again["lease"]["fence_token"])

    def test_same_session_different_process_is_refused_while_owner_live(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        first, code = self.claim(root, "SESSION-A", "--owner-pid", str(os.getpid()))
        self.assertEqual(0, code, first)
        refused, code = self.claim(root, "SESSION-A", "--owner-pid", "1")
        self.assertEqual(75, code)
        self.assertEqual("same_session_different_process", refused["reason"])

    def test_dead_owner_requires_takeover_then_increments_fence(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.register_session(root, "SESSION-B")
        self.claim(root, "SESSION-A")
        # Deactivating the owner session ends its liveness AND auto-releases
        # its leases (seam integration) — so takeover is not even needed.
        deactivated, code = self.run_cli(
            root, "deactivate", "--session", "SESSION-A", "--status", "stopped"
        )
        self.assertEqual(0, code)
        self.assertEqual(1, len(deactivated["released_activation_leases"]))

        reclaimed, code = self.claim(root, "SESSION-B")
        self.assertEqual(0, code, reclaimed)
        self.assertEqual(2, reclaimed["lease"]["fence_token"])

    def test_dead_owner_without_release_needs_explicit_takeover(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.register_session(root, "SESSION-B")
        self.claim(root, "SESSION-A")
        # Kill the owner's session record liveness without releasing the lease
        # (simulates a crashed owner whose deactivate never ran).
        session_path = (
            Path(root) / "State" / "session_autobridge" / "sessions" / "SESSION-A.json"
        )
        record = json.loads(session_path.read_text())
        record["status"] = "stopped"
        session_path.write_text(json.dumps(record))

        refused, code = self.claim(root, "SESSION-B")
        self.assertEqual(75, code)
        self.assertEqual("dead_owner_requires_takeover", refused["reason"])

        taken, code = self.claim(root, "SESSION-B", "--takeover")
        self.assertEqual(0, code, taken)
        self.assertEqual(2, taken["lease"]["fence_token"])
        self.assertEqual("SESSION-A", taken["lease"]["previous_owner_session_id"])

    def test_release_requires_owner_and_current_fence(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.register_session(root, "SESSION-B")
        claimed, _ = self.claim(root, "SESSION-A")
        fence = str(claimed["lease"]["fence_token"])

        refused, code = self.run_cli(
            root, "lease-release", *identity_args(self.worktree),
            "--session", "SESSION-B", "--fence-token", fence,
        )
        self.assertEqual(75, code)
        self.assertEqual("release_requires_current_owner", refused["reason"])

        stale, code = self.run_cli(
            root, "lease-release", *identity_args(self.worktree),
            "--session", "SESSION-A", "--fence-token", "99",
        )
        self.assertEqual(75, code)
        self.assertEqual("stale_fence_token", stale["reason"])

        released, code = self.run_cli(
            root, "lease-release", *identity_args(self.worktree),
            "--session", "SESSION-A", "--fence-token", fence,
        )
        self.assertEqual(0, code, released)
        self.assertTrue(released["released"])

        reclaimed, code = self.claim(root, "SESSION-B")
        self.assertEqual(0, code)
        self.assertEqual(2, reclaimed["lease"]["fence_token"])

    def test_lease_assert_validates_owner_and_fence(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        claimed, _ = self.claim(root, "SESSION-A")
        fence = str(claimed["lease"]["fence_token"])

        ok, code = self.run_cli(
            root, "lease-assert", *identity_args(self.worktree),
            "--session", "SESSION-A", "--fence-token", fence,
        )
        self.assertEqual(0, code)
        self.assertTrue(ok["asserted"])

        stale, code = self.run_cli(
            root, "lease-assert", *identity_args(self.worktree),
            "--session", "SESSION-A", "--fence-token", "42",
        )
        self.assertEqual(75, code)
        self.assertEqual("stale_fence_token", stale["reason"])

        other, code = self.run_cli(
            root, "lease-assert", *identity_args(self.worktree),
            "--session", "SESSION-X",
        )
        self.assertEqual(75, code)
        self.assertEqual("lease_owned_by_other_session", other["reason"])

    def test_claim_fails_closed_when_registry_unavailable(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        refused, code = self.run_cli(
            root,
            "lease-claim", *identity_args(self.worktree),
            "--session", "SESSION-A", "--skip-poller-cleanup",
            env={"LLM_COLLAB_PM2_BIN": "/usr/bin/false"},
        )
        self.assertEqual(75, code)
        self.assertEqual("poller_audit_unavailable", refused["reason"])

    def test_stale_fence_cannot_assert_after_takeover(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        self.register_session(root, "SESSION-B")
        claimed, _ = self.claim(root, "SESSION-A")
        old_fence = str(claimed["lease"]["fence_token"])

        session_path = (
            Path(root) / "State" / "session_autobridge" / "sessions" / "SESSION-A.json"
        )
        record = json.loads(session_path.read_text())
        record["status"] = "stopped"
        session_path.write_text(json.dumps(record))
        taken, code = self.claim(root, "SESSION-B", "--takeover")
        self.assertEqual(0, code, taken)

        stale, code = self.run_cli(
            root, "lease-assert", *identity_args(self.worktree),
            "--session", "SESSION-A", "--fence-token", old_fence,
        )
        self.assertEqual(75, code)
        self.assertEqual("lease_owned_by_other_session", stale["reason"])

    def test_lease_show_reports_owner(self):
        root = self.make_workspace()
        self.register_session(root, "SESSION-A")
        empty, code = self.run_cli(root, "lease-show", *identity_args(self.worktree))
        self.assertEqual(0, code)
        self.assertIsNone(empty["lease"])

        self.claim(root, "SESSION-A")
        shown, code = self.run_cli(root, "lease-show", *identity_args(self.worktree))
        self.assertEqual(0, code)
        self.assertEqual("SESSION-A", shown["owner"]["owner_session_id"])


class DispatchActivationGateTest(WorkspaceTestCase):
    """The mandatory hook: dispatch cannot wake a second writer for one
    activation packet. Removing acquire_activation_lease_for_dispatch from
    dispatch_session makes these tests fail."""

    def add_activation_message(
        self, root: Path, *, path_stem: str, omit: tuple[str, ...] = ()
    ) -> str:
        message_rel = f"Chats/2026-01-01_test__CHAT-TEST0001/{path_stem}.md"
        fm_lines = [
            "chat_id: CHAT-TEST0001",
            "from: codex",
            "to: claude",
            "title: ACTIVATE test lane",
            "project_id: amiga",
            "activation: true",
            "related_task: TASK-TEST01",
            f"worktree: {self.worktree}",
            "branch: claude/gh-0000-test",
        ]
        fm_lines = [l for l in fm_lines if l.split(":")[0] not in omit]
        write(
            root / message_rel,
            "\n".join(["---", *fm_lines, "---", "", "ACTIVATE: one writer only."]),
        )
        inbox_path = root / "agents" / "claude" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)
        return message_rel

    def register_runtime_session(self, root: Path, session: str, output_file: Path) -> None:
        worker_script = root / f"worker-{session}.py"
        write(
            worker_script,
            "\n".join(
                [
                    "import sys, json, os",
                    "from pathlib import Path",
                    "json.load(sys.stdin)",
                    f"Path({json.dumps(str(output_file))}).write_text(os.environ['LLM_COLLAB_SESSION_ID'])",
                ]
            ),
        )
        payload, code = self.run_cli(
            root,
            "register",
            "--session", session,
            "--agent", "claude",
            "--project", "amiga",
            "--chat", "CHAT-TEST0001",
            "--mode", "auto-read",
            "--wake-strategy", "runtime_trigger",
            "--runtime-family", "claude_app",
            "--runtime-session-id", f"runtime-{session}",
            "--runtime-session-source", "test_fixture",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )
        self.assertEqual(0, code, payload)

    def test_one_packet_cannot_wake_two_writers(self):
        root = self.make_workspace()
        out_a = root / "out-a.txt"
        out_b = root / "out-b.txt"
        self.register_runtime_session(root, "SESSION-A", out_a)
        self.register_runtime_session(root, "SESSION-B", out_b)
        self.add_activation_message(root, path_stem="activate-1")

        result_a, code = self.run_cli(root, "dispatch", "--session", "SESSION-A")
        self.assertEqual(0, code, result_a)
        self.assertEqual(1, len(result_a["actions"]))
        self.assertEqual("message_dispatched", result_a["actions"][0]["event"])
        self.assertTrue(out_a.exists(), "winner session must be woken")

        result_b, code = self.run_cli(root, "dispatch", "--session", "SESSION-B")
        self.assertEqual(0, code, result_b)
        self.assertEqual(1, len(result_b["actions"]))
        refusal = result_b["actions"][0]
        self.assertEqual("activation_lease_refused", refusal["event"])
        self.assertEqual("held_read_only", refusal["effective_action"])
        self.assertEqual(
            "SESSION-A", refusal["activation_lease"]["owner"]["owner_session_id"]
        )
        self.assertFalse(out_b.exists(), "loser session must never be woken")

        # The refusal is terminal for that session: no retry loop.
        again, _ = self.run_cli(root, "dispatch", "--session", "SESSION-B")
        self.assertEqual([], again["actions"])

    def test_dispatch_refuses_malformed_activation_never_downgrades(self):
        root = self.make_workspace()
        out_a = root / "out-malformed.txt"
        self.register_runtime_session(root, "SESSION-A", out_a)
        self.add_activation_message(root, path_stem="activate-partial", omit=("branch",))

        result, code = self.run_cli(root, "dispatch", "--session", "SESSION-A")
        self.assertEqual(0, code, result)
        refusal = result["actions"][0]
        self.assertEqual("activation_lease_refused", refusal["event"])
        self.assertEqual(
            "malformed_activation_packet", refusal["activation_lease"]["reason"]
        )
        self.assertFalse(out_a.exists(), "malformed activation must never wake a writer")

    def test_winner_payload_carries_identity_and_fence(self):
        root = self.make_workspace()
        out_a = root / "out-payload.json"
        worker_script = root / "worker-payload.py"
        write(
            worker_script,
            "\n".join(
                [
                    "import sys, json",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    f"Path({json.dumps(str(out_a))}).write_text(json.dumps(payload.get('activation_lease')))",
                ]
            ),
        )
        payload, code = self.run_cli(
            root,
            "register",
            "--session", "SESSION-A",
            "--agent", "claude",
            "--project", "amiga",
            "--chat", "CHAT-TEST0001",
            "--mode", "auto-read",
            "--wake-strategy", "runtime_trigger",
            "--runtime-family", "claude_app",
            "--runtime-session-id", "runtime-SESSION-A",
            "--runtime-session-source", "test_fixture",
            "--runtime-command", json.dumps([sys.executable, str(worker_script)]),
        )
        self.assertEqual(0, code, payload)
        self.add_activation_message(root, path_stem="activate-payload")

        result, code = self.run_cli(root, "dispatch", "--session", "SESSION-A")
        self.assertEqual(0, code, result)
        lease_detail = json.loads(out_a.read_text())
        self.assertEqual(1, lease_detail["fence_token"])
        self.assertEqual(self.worktree, lease_detail["identity"]["worktree"])

    def test_non_activation_messages_are_not_gated(self):
        root = self.make_workspace()
        out_a = root / "out-plain.txt"
        self.register_runtime_session(root, "SESSION-A", out_a)
        message_rel = "Chats/2026-01-01_test__CHAT-TEST0001/plain-note.md"
        write(
            root / message_rel,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-TEST0001",
                    "from: codex",
                    "to: claude",
                    "title: plain note",
                    "project_id: amiga",
                    "---",
                    "",
                    "No activation identity here.",
                ]
            ),
        )
        inbox_path = root / "agents" / "claude" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)

        result, code = self.run_cli(root, "dispatch", "--session", "SESSION-A")
        self.assertEqual(0, code, result)
        self.assertEqual("message_dispatched", result["actions"][0]["event"])
        self.assertTrue(out_a.exists())


class InboxActivationGateTest(WorkspaceTestCase):
    """The mailbox boundary: the path every Desktop writer crosses.

    These tests follow the REAL one-packet lifecycle — a consumed packet
    stays read; later packets for the same identity (the incident's
    activation + review-fix pair) and later observers are covered without
    ever re-enqueueing a consumed path."""

    def add_activation_message(self, root: Path, *, path_stem: str = "activate-inbox") -> str:
        message_rel = f"Chats/2026-01-01_test__CHAT-TEST0001/{path_stem}.md"
        write(
            root / message_rel,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-TEST0001",
                    "from: codex",
                    "to: claude",
                    f"title: ACTIVATE inbox lane ({path_stem})",
                    "project_id: amiga",
                    "activation: true",
                    "related_task: TASK-TEST01",
                    f"worktree: {self.worktree}",
                    "branch: claude/gh-0000-test",
                    "---",
                    "",
                    "ACTIVATE via mailbox.",
                ]
            ),
        )
        inbox_path = root / "agents" / "claude" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)
        return message_rel

    def run_inbox(
        self,
        root: Path,
        *args: str,
        reader_pid: int | None = None,
        reader_runtime: str | None = None,
    ) -> tuple[dict, int]:
        env: dict[str, str] = {}
        if reader_pid:
            env["LLM_COLLAB_READER_PID"] = str(reader_pid)
        if reader_runtime:
            env["LLM_COLLAB_READER_RUNTIME_ID"] = reader_runtime
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "inbox.py"), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.cli_env(env),
            check=False,
        )
        self.assertTrue(result.stdout.strip(), f"no stdout; stderr: {result.stderr}")
        return json.loads(result.stdout), result.returncode

    def alive_pid(self) -> int:
        return os.getpid()

    def dead_pid(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_second_packet_same_identity_different_session_is_refused(self):
        """The incident shape: activation packet then review-fix packet for the
        same lane; a second Desktop session consuming the later packet must
        not become a second writer."""
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")

        first, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1", reader_pid=self.alive_pid()
        )
        self.assertEqual(0, code)
        gate1 = first["messages"][0]["activation_gate"]
        self.assertTrue(gate1["authorized"])
        self.assertEqual(1, gate1["fence_token"])

        self.add_activation_message(root, path_stem="review-fix-1")
        second, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R2", reader_pid=self.alive_pid()
        )
        self.assertEqual(75, code, "refused activation read must exit 75")
        gate2 = second["messages"][0]["activation_gate"]
        self.assertFalse(gate2["authorized"])
        self.assertEqual("lease_held_by_active_owner", gate2["reason"])
        self.assertEqual("SESSION-R1", gate2["owner"]["owner_session_id"])

    def test_same_session_different_reader_process_is_refused(self):
        """A second Desktop process reusing the winner's --session id must not
        inherit its authority (the 2ccfd88 bypass)."""
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        first, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-SAME", reader_pid=self.alive_pid()
        )
        self.assertEqual(0, code)
        self.assertTrue(first["messages"][0]["activation_gate"]["authorized"])

        self.add_activation_message(root, path_stem="review-fix-1")
        second, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-SAME", reader_pid=1
        )
        self.assertEqual(75, code)
        gate2 = second["messages"][0]["activation_gate"]
        self.assertFalse(gate2["authorized"])
        self.assertEqual("same_session_different_process", gate2["reason"])

    def test_same_session_same_reader_process_is_idempotent(self):
        root = self.make_workspace()
        pid = self.alive_pid()
        self.add_activation_message(root, path_stem="activate-1")
        self.run_inbox(root, "--me", "claude", "--session", "SESSION-R1", reader_pid=pid)

        self.add_activation_message(root, path_stem="review-fix-1")
        again, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1", reader_pid=pid
        )
        self.assertEqual(0, code)
        gate = again["messages"][0]["activation_gate"]
        self.assertTrue(gate["authorized"])
        self.assertEqual(1, gate["fence_token"])

    def test_crashed_reader_allows_takeover_by_next_reader(self):
        """A crashed ephemeral reader (dead bound pid, no release) must not
        block the lane forever; the next reader takes over with a new fence."""
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        first, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-CRASH", reader_pid=self.dead_pid()
        )
        self.assertEqual(0, code)
        self.assertTrue(first["messages"][0]["activation_gate"]["authorized"])

        self.add_activation_message(root, path_stem="review-fix-1")
        second, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-NEXT", reader_pid=self.alive_pid()
        )
        self.assertEqual(0, code, second)
        gate2 = second["messages"][0]["activation_gate"]
        self.assertTrue(gate2["authorized"])
        self.assertEqual(2, gate2["fence_token"])
        self.assertEqual("SESSION-CRASH", gate2["lease"]["previous_owner_session_id"])

    def test_late_observer_sees_held_read_only_without_reenqueue(self):
        """A later Desktop observer of the already-consumed packet (via --all)
        gets an explicit read-only refusal, exit 75 — the real lifecycle, no
        unread mutation."""
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1", reader_pid=self.alive_pid()
        )

        observer, code = self.run_inbox(
            root, "--me", "claude", "--all", "--session", "SESSION-R2"
        )
        self.assertEqual(75, code)
        gate = observer["messages"][0]["activation_gate"]
        self.assertEqual("held_read_only", gate["gate"])
        self.assertFalse(gate["authorized"])
        self.assertEqual("SESSION-R1", gate["owner"]["owner_session_id"])

        winner, code = self.run_inbox(
            root, "--me", "claude", "--all", "--session", "SESSION-R1"
        )
        self.assertEqual(0, code)
        self.assertEqual("peek_owner", winner["messages"][0]["activation_gate"]["gate"])

    def test_unbound_reader_is_refused(self):
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        refused, code = self.run_inbox(root, "--me", "claude", "--session", "SESSION-R1")
        self.assertEqual(75, code)
        gate = refused["messages"][0]["activation_gate"]
        self.assertEqual("reader_identity_unbound", gate["reason"])
        self.assertIn("LLM_COLLAB_READER_RUNTIME_ID", gate["hint"])

    def test_runtime_bound_same_session_different_runtime_is_refused(self):
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        first, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-SAME",
            reader_runtime="uuid-task-one",
        )
        self.assertEqual(0, code, first)
        self.assertTrue(first["messages"][0]["activation_gate"]["authorized"])

        self.add_activation_message(root, path_stem="review-fix-1")
        second, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-SAME",
            reader_runtime="uuid-task-two",
        )
        self.assertEqual(75, code)
        gate2 = second["messages"][0]["activation_gate"]
        self.assertEqual("same_session_different_process", gate2["reason"])

    def test_runtime_bound_reclaim_survives_transient_shells(self):
        """The real Desktop shape: every command runs in a new short-lived
        shell, but the runtime identity is constant — re-claims stay
        idempotent and the winner never looks crashed."""
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1",
            reader_runtime="uuid-task-one",
        )
        self.add_activation_message(root, path_stem="review-fix-1")
        again, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1",
            reader_runtime="uuid-task-one",
        )
        self.assertEqual(0, code, again)
        gate = again["messages"][0]["activation_gate"]
        self.assertTrue(gate["authorized"])
        self.assertEqual(1, gate["fence_token"])

    def test_runtime_bound_owner_not_taken_over_before_ttl(self):
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1",
            reader_runtime="uuid-task-one",
        )
        self.add_activation_message(root, path_stem="review-fix-1")
        second, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R2",
            reader_runtime="uuid-task-two",
        )
        self.assertEqual(75, code, "runtime-bound live owner must not be replaced")
        self.assertEqual(
            "lease_held_by_active_owner",
            second["messages"][0]["activation_gate"]["reason"],
        )

    def test_runtime_bound_owner_taken_over_after_ttl_expiry(self):
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-1")
        first, _ = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R1",
            reader_runtime="uuid-task-one",
        )
        owner_session = first["messages"][0]["activation_gate"]["reader_session_id"]
        session_path = (
            Path(root) / "State" / "session_autobridge" / "sessions" / f"{owner_session}.json"
        )
        record = json.loads(session_path.read_text())
        record["lease_expires_utc"] = "2020-01-01T00:00:00+00:00"
        session_path.write_text(json.dumps(record))

        self.add_activation_message(root, path_stem="review-fix-1")
        second, code = self.run_inbox(
            root, "--me", "claude", "--session", "SESSION-R2",
            reader_runtime="uuid-task-two",
        )
        self.assertEqual(0, code, second)
        gate2 = second["messages"][0]["activation_gate"]
        self.assertTrue(gate2["authorized"])
        self.assertEqual(2, gate2["fence_token"])

    def test_packet_selector_targets_exactly_one_message(self):
        root = self.make_workspace()
        self.add_activation_message(root, path_stem="activate-lane-a")
        other_rel = "Chats/2026-01-01_test__CHAT-TEST0001/plain-note.md"
        write(
            root / other_rel,
            "\n".join(
                [
                    "---", "chat_id: CHAT-TEST0001", "from: codex", "to: claude",
                    "title: plain", "project_id: amiga", "---", "", "note",
                ]
            ),
        )
        inbox_path = root / "agents" / "claude" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(other_rel)
        write_json(inbox_path, inbox)

        result, code = self.run_inbox(
            root, "--me", "claude", "--packet", "activate-lane-a.md",
            "--session", "SESSION-R1", reader_runtime="uuid-task-one",
        )
        self.assertEqual(0, code, result)
        self.assertEqual(1, len(result["messages"]))
        self.assertTrue(result["messages"][0]["path"].endswith("activate-lane-a.md"))
        self.assertTrue(result["messages"][0]["activation_gate"]["authorized"])

        remaining = json.loads(inbox_path.read_text())
        self.assertIn(other_rel, remaining["unread"], "--packet must not consume others")

    def test_peek_reports_unclaimed_without_claiming(self):
        root = self.make_workspace()
        self.add_activation_message(root)
        peeked, code = self.run_inbox(root, "--me", "claude", "--peek")
        self.assertEqual(0, code)
        gate = peeked["messages"][0]["activation_gate"]
        self.assertEqual("peek", gate["gate"])
        self.assertIsNone(gate["owner"])

        lease_dir = Path(root) / "State" / "session_autobridge" / "activation_leases"
        self.assertEqual([], list(lease_dir.glob("*.json")), "peek must not claim")

    def test_malformed_activation_fails_closed_at_inbox(self):
        root = self.make_workspace()
        message_rel = "Chats/2026-01-01_test__CHAT-TEST0001/activate-broken.md"
        write(
            root / message_rel,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-TEST0001",
                    "from: codex",
                    "to: claude",
                    "title: broken activation",
                    "project_id: amiga",
                    "activation: true",
                    "related_task: TASK-TEST01",
                    "---",
                    "",
                    "Missing worktree/branch.",
                ]
            ),
        )
        inbox_path = root / "agents" / "claude" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)

        shown, code = self.run_inbox(root, "--me", "claude")
        self.assertEqual(75, code)
        gate = shown["messages"][0]["activation_gate"]
        self.assertEqual("malformed_activation", gate["gate"])
        self.assertFalse(gate["authorized"])


class DeliverActivationValidationTest(WorkspaceTestCase):
    def run_deliver(self, root: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "deliver.py"), *args],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.cli_env(),
            check=False,
        )

    BASE = [
        "--chat", "CHAT-TEST0001",
        "--from", "codex",
        "--to", "claude",
        "--project", "amiga",
        "--title", "x",
    ]

    def test_activation_requires_full_identity(self):
        root = self.make_workspace()
        result = self.run_deliver(
            root, *self.BASE, "--activation", "--related-task", "TASK-TEST01"
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--worktree", result.stderr)
        self.assertIn("--branch", result.stderr)

    def test_partial_identity_without_activation_flag_is_rejected(self):
        root = self.make_workspace()
        result = self.run_deliver(root, *self.BASE, "--worktree", "/tmp/x")
        self.assertEqual(2, result.returncode)
        self.assertIn("--activation", result.stderr)

    def test_generated_activation_command_contract(self):
        """The claim command written into the packet body must be absolute,
        placeholder-free, and scoped to exactly this packet; the matching AX
        ring prompt must fit the doorbell budget."""
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": False},
            },
        )
        chat_dir = root / "Chats" / "2026-01-01_test__CHAT-TEST0001"
        write_json(chat_dir / "meta.json", {"chat_id": "CHAT-TEST0001", "project_id": "amiga"})
        body_file = root / "brief.md"
        write(body_file, "Do the lane work.")

        result = self.run_deliver(
            root, *self.BASE,
            "--activation",
            "--related-task", "TASK-TEST01",
            "--worktree", self.worktree,
            "--branch", "claude/gh-0000-test",
            "--body-file", str(body_file),
        )
        self.assertEqual(0, result.returncode, result.stderr)

        packets = sorted(chat_dir.glob("*_to-claude_*.md"))
        self.assertEqual(1, len(packets))
        packet = packets[0]
        body = packet.read_text()

        command_lines = [l for l in body.splitlines() if "inbox.py" in l]
        self.assertTrue(command_lines, "activation body must carry the claim command")
        command = command_lines[0]
        self.assertNotIn("<", command, "no placeholders in the claim command")
        self.assertIn(str(root / "bin" / "llm-collab"), command, "absolute launcher path")
        self.assertIn(f"--packet {packet.name}", command, "exact-packet scoped")
        self.assertIn("--me claude", command)
        self.assertNotIn("--session", command, "reader identity is not sender-invented")

        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, json; sys.path.insert(0, sys.argv[1]); import deliver; "
                "c = deliver.build_activation_consume_command('claude', 'amiga', sys.argv[2]); "
                "p = deliver.build_activation_ring_prompt('codex', 'TASK-TEST01', c); "
                "print(json.dumps({'command': c, 'prompt': p, 'max': deliver.AX_DOORBELL_MAX_CHARS}))",
                str(REPO_ROOT / "bin"),
                packet.name,
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.cli_env(),
            check=True,
        )
        generated = json.loads(probe.stdout)
        self.assertLessEqual(len(generated["prompt"]), generated["max"])
        self.assertIn(packet.name, generated["prompt"])
        self.assertNotIn("<", generated["prompt"])
        self.assertIn(generated["command"], body, "body and ring must carry the same command")


class TestIsolationGuard(unittest.TestCase):
    def test_ps_fixture_mode_never_signals_any_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "ps.txt"
            fixture.write_text(
                "1 0 /sbin/launchd\n"
                "555 1 /bin/zsh -c while true; do ls Chats/*CHAT-TEST0001*; done\n"
            )
            old_leases = lease_lib.ACTIVATION_LEASES_DIR
            lease_lib.ACTIVATION_LEASES_DIR = Path(tmp) / "leases"

            def forbidden_kill(pid: int, sig: int) -> None:
                raise AssertionError(f"test signaled real pid {pid}")

            old_kill = os.kill
            os.environ["LLM_COLLAB_PS_FIXTURE"] = str(fixture)
            os.kill = forbidden_kill  # any signal attempt fails the test
            try:
                findings = lease_lib.audit_activation_pollers(
                    identity_dict("/tmp/worktrees/claude/t-test"),
                    registered_pids=set(),
                    clean=True,
                    self_pid=99998,
                )
            finally:
                os.kill = old_kill
                del os.environ["LLM_COLLAB_PS_FIXTURE"]
                lease_lib.ACTIVATION_LEASES_DIR = old_leases
            actions = {f["pid"]: f for f in findings}
            self.assertEqual("terminated", actions[555]["action"])
            self.assertTrue(actions[555]["simulated"])


class PollerAuditTest(unittest.TestCase):
    IDENTITY = identity_dict("/tmp/worktrees/claude/t-test")

    ROWS = [
        # PM2-registered watcher pid (authoritative registry): must survive
        # even though the command carries no PM2 env marker.
        {"pid": 101, "ppid": 1, "command": "python3 bin/watch_inbox.py --me claude"},
        # Ad-hoc while-true poller referencing the activation chat: cleanup target.
        {
            "pid": 102,
            "ppid": 1,
            "command": "/bin/zsh -c while true; do ls Chats/*CHAT-TEST0001*/*_to-claude_*.md; sleep 60; done",
        },
        # Ad-hoc manual watch_inbox for the same agent: cleanup target.
        {"pid": 103, "ppid": 1, "command": "python3 bin/watch_inbox.py --me claude --poll-seconds 30"},
        # Unregistered process that merely inherited a PM2-looking env marker:
        # NOT in the registry, so it is NOT preserved.
        {
            "pid": 107,
            "ppid": 1,
            "command": "/bin/zsh -c while true; do cat Chats/*CHAT-TEST0001*; done PM2_HOME=/Users/op/.pm2",
        },
        # Unrelated agent: untouched, not even reported.
        {"pid": 104, "ppid": 1, "command": "python3 bin/watch_inbox.py --me codex"},
        # Purpose-scoped PR watcher for a different chat: not identity-matched.
        {"pid": 105, "ppid": 1, "command": "/bin/zsh -c while true; do gh pr checks 111; sleep 300; done"},
        # One-shot command mentioning the chat id: not poller-shaped, never a target.
        {"pid": 106, "ppid": 1, "command": "python3 bin/deliver.py --chat CHAT-TEST0001 --from codex --to claude"},
        # The claimer's own ancestor chain: excluded.
        {"pid": 200, "ppid": 1, "command": "/bin/zsh -c while true; do run-claim --chat CHAT-TEST0001; done"},
        {"pid": 99999, "ppid": 200, "command": "python3 bin/session_autobridge.py lease-claim"},
    ]

    REGISTERED = {101}

    def audit(self, *, clean: bool, kill=None, wait_for_exit=None):
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))

        findings = lease_lib.audit_activation_pollers(
            self.IDENTITY,
            rows=self.ROWS,
            registered_pids=self.REGISTERED,
            clean=clean,
            kill=kill or fake_kill,
            wait_for_exit=wait_for_exit or (lambda pid: True),
            self_pid=99999,
        )
        return findings, killed

    def test_report_only_audit_reports_matches_without_killing(self):
        findings, killed = self.audit(clean=False)
        self.assertEqual([], killed)
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual(
            {
                101: "preserved_registered_watch",
                102: "reported_only",
                103: "reported_only",
                107: "reported_only",
            },
            actions,
        )

    def test_cleanup_terminates_only_unregistered_identity_matches(self):
        findings, killed = self.audit(clean=True)
        self.assertEqual([102, 103, 107], sorted(pid for pid, _ in killed))
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual("preserved_registered_watch", actions[101])
        self.assertEqual("terminated", actions[102])
        self.assertEqual("terminated", actions[107], "inherited env marker must not protect")
        self.assertNotIn(104, actions)
        self.assertNotIn(105, actions)
        self.assertNotIn(106, actions, "one-shot chat-mentioning command must never match")
        self.assertNotIn(200, actions, "own ancestor chain must never be terminated")
        self.assertTrue(lease_lib.audit_proves_clean(findings))

    def test_unverified_termination_fails_the_audit(self):
        findings, _ = self.audit(clean=True, wait_for_exit=lambda pid: False)
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual("termination_unverified", actions[102])
        self.assertFalse(lease_lib.audit_proves_clean(findings))

    def test_terminate_denied_fails_the_audit(self):
        def denied(pid: int, sig: int) -> None:
            raise PermissionError

        findings, _ = self.audit(clean=True, kill=denied)
        self.assertFalse(lease_lib.audit_proves_clean(findings))

    def test_sigkill_escalation_counts_as_proven(self):
        attempts: dict[int, list[int]] = {}

        def stubborn_kill(pid: int, sig: int) -> None:
            attempts.setdefault(pid, []).append(sig)

        def exits_after_sigkill(pid: int) -> bool:
            return signal.SIGKILL in attempts.get(pid, [])

        findings, _ = self.audit(clean=True, kill=stubborn_kill, wait_for_exit=exits_after_sigkill)
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual("terminated_sigkill", actions[102])
        self.assertTrue(lease_lib.audit_proves_clean(findings))

    def test_already_exited_process_is_reported_not_fatal(self):
        def raising_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        findings, _ = self.audit(clean=True, kill=raising_kill)
        actions = {f["pid"]: f["action"] for f in findings}
        self.assertEqual("already_exited", actions[102])
        self.assertTrue(lease_lib.audit_proves_clean(findings))

    def test_ps_failure_raises_audit_unavailable(self):
        with self.assertRaises(lease_lib.PollerAuditUnavailable):
            lease_lib.poller_process_rows("")
        rows = lease_lib.poller_process_rows("  201 1 python3 x.py\n 202 201 /bin/zsh -c loop\n")
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
