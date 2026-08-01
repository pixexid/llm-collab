from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
INBOX_SCRIPT = REPO_ROOT / "bin" / "inbox.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))
import inbox as inbox_lib
from _activation_lease import RUNTIME_ID_ENV_VARS


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
    write(path, json.dumps(payload, indent=2))


class InboxMarkAllReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="llm-collab-inbox-")
        self.root = Path(self.temp_dir.name)
        write_json(
            self.root / "collab.config.json",
            {
                "workspace_name": "test-collab",
                "schema_version": 2,
                "projects_root": str(self.root),
                "notifications_enabled": False,
            },
        )
        write_json(
            self.root / "projects.json",
            {
                "projects": [
                    {"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}},
                    {"id": "nuvyr", "display_name": "Nuvyr", "repos": {"app": "."}},
                ]
            },
        )
        write_json(
            self.root / "agents.json",
            {
                "agents": [
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "activation": {
                            "type": "cli_session",
                            "watcher_enabled": False,
                        },
                    }
                ]
            },
        )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [], "read": []},
        )
        self.worktree = self.root / "worktrees" / "lane"
        self.worktree.mkdir(parents=True)
        self.pm2_bin = self.root / "pm2"
        self.pm2_bin.write_text("#!/bin/sh\nprintf '[]'\n")
        self.pm2_bin.chmod(0o755)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_message(
        self,
        name: str,
        *,
        project_line: str | None,
        activation: bool = False,
        repo_targets: list[str] | None = None,
        inbox_bucket: str = "unread",
    ) -> str:
        rel_path = f"Chats/2026-07-19_test__CHAT-{name}/{name}_to-codex.md"
        frontmatter = [
            "---",
            f"chat_id: CHAT-{name}",
            "from: claude",
            "to: codex",
            f"title: {name}",
            "related_task: TASK-TEST01",
        ]
        if project_line is not None:
            frontmatter.append(f"project_id: {project_line}")
        if repo_targets is not None:
            frontmatter.append("repo_targets: " + json.dumps(repo_targets))
        if activation:
            frontmatter.extend(
                [
                    "activation: true",
                    f"worktree: {self.worktree}",
                    "branch: codex/gh-1572-runtime-integration",
                ]
            )
        frontmatter.extend(
            [
                "sent_utc: 2026-07-19T00:00:00+00:00",
                "---",
                "",
                "Test message.",
            ]
        )
        write(self.root / rel_path, "\n".join(frontmatter))
        inbox = self.load_inbox()
        inbox[inbox_bucket].append(rel_path)
        write_json(self.root / "agents" / "codex" / "inbox.json", inbox)
        return rel_path

    def add_malformed_activation(self, name: str) -> str:
        rel_path = f"Chats/2026-07-19_test__CHAT-{name}/{name}_to-codex.md"
        write(
            self.root / rel_path,
            "\n".join(
                [
                    "---",
                    f"chat_id: CHAT-{name}",
                    "from: claude",
                    "to: codex",
                    f"title: {name}",
                    "project_id: amiga",
                    "related_task: TASK-TEST01",
                    "activation: true",
                    "---",
                    "",
                    "Malformed activation.",
                ]
            ),
        )
        inbox = self.load_inbox()
        inbox["unread"].append(rel_path)
        write_json(self.root / "agents" / "codex" / "inbox.json", inbox)
        return rel_path

    def add_missing_message_pointer(self, name: str) -> str:
        rel_path = f"Chats/2026-07-19_missing__CHAT-{name}/{name}_to-codex.md"
        inbox = self.load_inbox()
        inbox["unread"].append(rel_path)
        write_json(self.root / "agents" / "codex" / "inbox.json", inbox)
        return rel_path

    def load_inbox(self) -> dict:
        return json.loads(
            (self.root / "agents" / "codex" / "inbox.json").read_text()
        )

    def add_exact_session(
        self,
        *,
        project_id: str = "amiga",
        chat_id: str = "CHAT-EXACT",
        status: str = "active",
        binding_id: str | None = None,
        binding_generation: int | None = None,
    ) -> None:
        session = {
            "session_id": "SESSION-EXACT",
            "agent_id": "codex",
            "project_id": project_id,
            "chat_id": chat_id,
            "status": status,
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "pi", "session_id": "pi-exact"},
        }
        binding = {
            "project_id": project_id,
            "chat_id": chat_id,
            "agent_id": "codex",
            "session_id": "SESSION-EXACT",
            "runtime_family": "pi",
            "runtime_session_id": "pi-exact",
        }
        if binding_id is not None:
            session["binding_id"] = binding_id
            binding["binding_id"] = binding_id
        if binding_generation is not None:
            session["binding_generation"] = binding_generation
            binding["binding_generation"] = binding_generation
        write_json(
            self.root
            / "State"
            / "session_autobridge"
            / "sessions"
            / "SESSION-EXACT.json",
            session,
        )
        write_json(
            self.root
            / "State"
            / "session_autobridge"
            / "bindings"
            / project_id
            / chat_id
            / "codex.json",
            binding,
        )

    def add_exact_message(
        self,
        name: str,
        *,
        project_id: str = "amiga",
        chat_id: str = "CHAT-EXACT",
        target_session_id: str = "SESSION-EXACT",
        recipient: str = "codex",
        sender: str = "claude",
        activation: bool = False,
        path: str | None = None,
        repo_targets: list[object] | None = None,
        target_binding_id: str | None = None,
        target_binding_generation: int | None = None,
    ) -> str:
        path = path or f"Chats/exact__{chat_id}/{name}.md"
        lines = [
            "---",
            f"chat_id: {chat_id}",
            f"project_id: {project_id}",
            f"from: {sender}",
            f"to: {recipient}",
            f"target_session_id: {target_session_id}",
        ]
        if target_binding_id is not None:
            lines.append(f"target_binding_id: {target_binding_id}")
        if target_binding_generation is not None:
            lines.append(
                f"target_binding_generation: {target_binding_generation}"
            )
        if repo_targets is not None:
            lines.append("repo_targets: " + json.dumps(repo_targets))
        if activation:
            lines.extend(
                [
                    "activation: true",
                    f"worktree: {self.worktree}",
                    "branch: codex/exact",
                ]
            )
        lines.extend(["---", "", name])
        write(self.root / path, "\n".join(lines))
        inbox = self.load_inbox()
        inbox["unread"].append(path)
        write_json(self.root / "agents" / "codex" / "inbox.json", inbox)
        return path

    def run_inbox(
        self,
        *args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        ps_fixture = self.root / "ps-fixture.txt"
        if not ps_fixture.exists():
            ps_fixture.write_text("999 1 python test-harness\n")
        return subprocess.run(
            [sys.executable, str(INBOX_SCRIPT), "--me", "codex", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "LLM_COLLAB_PS_FIXTURE": str(ps_fixture),
                "LLM_COLLAB_PM2_BIN": str(self.pm2_bin),
                **(env or {}),
            },
            check=False,
        )

    def test_project_scope_marks_only_exact_project(self) -> None:
        amiga = self.add_message("AMIGA", project_line="amiga")
        nuvyr = self.add_message("NUVYR", project_line="nuvyr")
        missing = self.add_message("MISSING", project_line=None)
        empty = self.add_message("EMPTY", project_line="")
        null = self.add_message("NULL", project_line="null")

        result = self.run_inbox("--project", "amiga", "--mark-all-read")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {
                "marked_read": 1,
                "marked_read_by_project": {"amiga": 1},
            },
            json.loads(result.stdout),
        )
        inbox = self.load_inbox()
        self.assertEqual([amiga], inbox["read"])
        self.assertEqual([nuvyr, missing, empty, null], inbox["unread"])

    def test_empty_exact_session_refuses_before_ordinary_inbox_loading(self) -> None:
        self.add_message("SECRET", project_line="amiga")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "",
            "--peek",
            "--json",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("--session requires a non-empty session id", result.stderr)
        self.assertNotIn("Test message", result.stdout)

    def test_empty_exact_packet_selector_refuses(self) -> None:
        self.add_exact_session()
        self.add_exact_message("secret")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--packet",
            "",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("--packet requires a non-empty packet selector", result.stderr)
        self.assertNotIn("secret", result.stdout)

    def test_inbox_writes_serialize_so_a_delivery_and_ack_cannot_lose_a_packet(self) -> None:
        """GH-343: add_to_inbox (delivery) and mark_messages_read (ack drain) are
        load-modify-save on one index; unlocked, one overwrites the other. The lock
        makes each atomic. Proved deterministically: hold the lock, a concurrent
        mark_messages_read BLOCKS rather than clobbering; remove the lock and it does
        not block (which is how a pointer is lost).
        """
        import threading
        import time
        import tempfile
        from unittest.mock import patch
        import _helpers as helpers

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agents" / "pi").mkdir(parents=True)
            (root / "agents" / "pi" / "inbox.json").write_text(
                json.dumps({"unread": ["Chats/x.md"], "read": []})
            )
            with patch.object(helpers, "ROOT", root):
                done = threading.Event()

                def acker():
                    helpers.mark_messages_read("pi", ["Chats/x.md"])
                    done.set()

                with helpers.inbox_write_lock("pi"):
                    worker = threading.Thread(target=acker)
                    worker.start()
                    time.sleep(0.05)
                    blocked = not done.is_set()
                worker.join(timeout=2)

            self.assertTrue(blocked, "an inbox write ran while the lock was held")

    def test_acknowledge_drains_the_exact_set_and_leaves_a_wrong_session_unread(self) -> None:
        """GH-343: one coalesced wake, drain the durable queue. Two packets for the
        bound session are exposed and acknowledged (marked read) by a single
        --acknowledge; a packet for another session was never in the exact set and
        stays unread. The event count never mattered — the queue does.
        """
        self.add_exact_session()
        # Body markers deliberately distinct from the paths, so the assertions
        # discriminate BODY rendering from the always-printed Path line: deleting
        # the body render must fail this.
        a = self.add_exact_message("BODY-A", path="Chats/exact__CHAT-EXACT/mA.md")
        b = self.add_exact_message("BODY-B", path="Chats/exact__CHAT-EXACT/mB.md")
        other = self.add_exact_message(
            "BODY-OTHER", path="Chats/exact__CHAT-EXACT/mOther.md",
            target_session_id="SESSION-OTHER")

        # The documented human-path command (no --json): Pi must SEE the bodies it
        # drains, not just a count.
        result = self.run_inbox(
            "--project", "amiga", "--chat", "CHAT-EXACT",
            "--session", "SESSION-EXACT", "--acknowledge",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("BODY-A", result.stdout)
        self.assertIn("BODY-B", result.stdout)
        self.assertNotIn("BODY-OTHER", result.stdout, "the wrong-session body must not be exposed")
        self.assertIn("Acknowledged 2", result.stdout)
        inbox = self.load_inbox()
        self.assertEqual([other], inbox["unread"], "the wrong-session packet must remain unread")
        self.assertIn(a, inbox["read"])
        self.assertIn(b, inbox["read"])

    def test_exact_session_returns_every_match_without_the_display_limit(self) -> None:
        self.add_exact_session()
        paths = [self.add_exact_message(str(index)) for index in range(12)]

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--limit",
            "1",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            paths,
            [message["path"] for message in json.loads(result.stdout)["messages"]],
        )
        self.assertEqual(paths, self.load_inbox()["unread"])

    def test_exact_session_refuses_a_missing_packet_with_its_path(self) -> None:
        self.add_exact_session()
        missing = self.add_missing_message_pointer("EXACT")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn(
            missing,
            json.loads(result.stdout)["exact_session_refused"],
        )

    def test_exact_packet_selection_ignores_unrelated_missing_archive_entries(self) -> None:
        self.add_exact_session()
        selected = self.add_exact_message("selected")
        self.add_missing_message_pointer("UNRELATED")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--packet",
            Path(selected).name,
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [selected],
            [
                message["path"]
                for message in json.loads(result.stdout)["messages"]
            ],
        )

    def test_exact_session_refuses_foreign_scope_and_runtime(self) -> None:
        self.add_exact_session()
        secret = self.add_exact_message("secret")
        cases = (
            (
                ["--project", "nuvyr", "--chat", "CHAT-EXACT"],
                {"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
            ),
            (
                ["--project", "amiga", "--chat", "CHAT-FOREIGN"],
                {"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
            ),
            (
                ["--project", "amiga", "--chat", "CHAT-EXACT"],
                {"LLM_COLLAB_READER_RUNTIME_ID": "pi-foreign"},
            ),
        )
        for scope, env in cases:
            with self.subTest(scope=scope, env=env):
                result = self.run_inbox(
                    *scope,
                    "--session",
                    "SESSION-EXACT",
                    "--peek",
                    "--json",
                    env=env,
                )
                self.assertEqual(75, result.returncode, result.stderr)
                self.assertNotIn(secret, result.stdout)

    def test_exact_session_distinguishes_absent_from_foreign_runtime_identity(
        self,
    ) -> None:
        self.add_exact_session()
        secret = self.add_exact_message("secret")
        cleared = {name: "" for name in RUNTIME_ID_ENV_VARS}
        cases = (
            ("exact_session_runtime_missing", cleared),
            (
                "exact_session_runtime_mismatch",
                {**cleared, "LLM_COLLAB_READER_RUNTIME_ID": "pi-foreign"},
            ),
        )

        for expected, env in cases:
            with self.subTest(expected=expected):
                result = self.run_inbox(
                    "--project",
                    "amiga",
                    "--chat",
                    "CHAT-EXACT",
                    "--session",
                    "SESSION-EXACT",
                    "--peek",
                    "--json",
                    env=env,
                )

                self.assertEqual(75, result.returncode, result.stderr)
                self.assertEqual(
                    expected,
                    json.loads(result.stdout)["exact_session_refused"],
                )
                self.assertNotIn(secret, result.stdout)

    def test_exact_session_uses_frontmatter_not_the_packet_path_for_chat(self) -> None:
        self.add_exact_session()
        exact = self.add_exact_message(
            "exact",
            path="Chats/no-chat-id-here/exact.md",
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload, dict)
        self.assertEqual(
            [exact],
            [message["path"] for message in payload["messages"]],
        )

    def test_exact_session_refuses_an_unregistered_project(self) -> None:
        self.add_exact_session(project_id="ghost", chat_id="CHAT-GHOST")
        secret = self.add_exact_message(
            "secret",
            project_id="ghost",
            chat_id="CHAT-GHOST",
        )

        result = self.run_inbox(
            "--project",
            "ghost",
            "--chat",
            "CHAT-GHOST",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "unknown_project",
            json.loads(result.stdout)["exact_session_refused"],
        )
        self.assertNotIn(secret, result.stdout)

    def test_exact_session_charges_the_project_registry_to_its_budget(self) -> None:
        self.add_exact_session()
        self.add_exact_message("exact")
        write_json(
            self.root / "projects.json",
            {
                "projects": [
                    {
                        "id": "amiga",
                        "display_name": "Amiga",
                        "repos": {"app": "."},
                        "padding": "x" * inbox_lib.MAX_EXACT_SESSION_BYTES,
                    }
                ]
            },
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn(
            "projects.json",
            json.loads(result.stdout)["exact_session_refused"],
        )

    def test_exact_session_charges_the_agent_registry_to_its_budget(self) -> None:
        self.add_exact_session()
        self.add_exact_message("exact")
        write_json(
            self.root / "agents.json",
            {
                "agents": [
                    {
                        "id": "codex",
                        "padding": "x" * inbox_lib.MAX_EXACT_SESSION_BYTES,
                    }
                ]
            },
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn(
            "agents.json",
            json.loads(result.stdout)["exact_session_refused"],
        )

    def test_exact_session_refuses_an_inactive_record(self) -> None:
        self.add_exact_session(status="superseded")
        secret = self.add_exact_message("secret")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "exact_session_inactive",
            json.loads(result.stdout)["exact_session_refused"],
        )
        self.assertNotIn(secret, result.stdout)

    def test_exact_session_refuses_a_stale_binding_generation(self) -> None:
        self.add_exact_session(
            binding_id="binding-exact",
            binding_generation=3,
        )
        binding_path = (
            self.root
            / "State"
            / "session_autobridge"
            / "bindings"
            / "amiga"
            / "CHAT-EXACT"
            / "codex.json"
        )
        binding = json.loads(binding_path.read_text())
        binding["binding_generation"] = 4
        write_json(binding_path, binding)
        secret = self.add_exact_message("secret")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "exact_binding_generation_mismatch",
            json.loads(result.stdout)["exact_session_refused"],
        )
        self.assertNotIn(secret, result.stdout)

    def test_exact_session_refuses_a_stale_self_target_packet_generation(self) -> None:
        self.add_exact_session(
            binding_id="binding-exact",
            binding_generation=3,
        )
        stale = self.add_exact_message(
            "stale",
            sender="codex",
            target_binding_id="binding-exact",
            target_binding_generation=2,
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            [stale],
            [
                refusal["path"]
                for refusal in json.loads(result.stdout)["repo_scope_refused"]
            ],
        )

    def test_exact_session_reports_repo_scope_refusal(self) -> None:
        self.add_exact_session()
        refused = self.add_exact_message("refused", repo_targets=["amiga"])

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--repo-target",
            "llm-collab",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        # GH-417: a repo-scope (route_ambiguous) refusal is now NON-fatal (exit 0)
        # and reported, rather than aborting the batch and silencing the watcher.
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["messages"])
        self.assertEqual(
            [{"path": refused, "reason": "route_ambiguous"}],
            payload["repo_scope_refused"],
        )

    def test_exact_read_skips_a_repo_scope_refusal_and_returns_the_routable(self) -> None:
        # GH-417: one route_ambiguous packet must not suppress the routable ones.
        # The exact-read repo-scope contract is project-agnostic, so cover a
        # non-Amiga project (nuvyr) as well as Amiga.
        for project, chat in (("amiga", "CHAT-EXACT"), ("nuvyr", "CHAT-NUVYR")):
            with self.subTest(project=project):
                self.add_exact_session(project_id=project, chat_id=chat)
                routable = [
                    self.add_exact_message(
                        f"ok{index}", project_id=project, chat_id=chat, repo_targets=["app"]
                    )
                    for index in range(2)
                ]
                refused = self.add_exact_message(
                    "misscoped", project_id=project, chat_id=chat, repo_targets=["docs"]
                )

                result = self.run_inbox(
                    "--project", project, "--chat", chat,
                    "--session", "SESSION-EXACT", "--repo-target", "app",
                    "--peek", "--json",
                    env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
                )

                self.assertEqual(0, result.returncode, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(
                    routable, [message["path"] for message in payload["messages"]]
                )
                self.assertEqual(
                    [refused],
                    [entry["path"] for entry in payload.get("repo_scope_refused", [])],
                )

    def test_exact_read_fails_closed_on_a_wrong_binding_id_with_a_routable_packet(
        self,
    ) -> None:
        # GH-417 P1: a wrong target_binding_id also returns route_ambiguous, but it is
        # a BINDING-identity refusal, NOT repo-scope — it must still fail closed (exit
        # 75, messages suppressed), even alongside a routable packet. Classifying by
        # the reason string alone would wrongly skip it.
        self.add_exact_session(binding_id="binding-current", binding_generation=3)
        self.add_exact_message("routable", repo_targets=["app"])
        wrong_binding = self.add_exact_message(
            "wrongbinding",
            repo_targets=["app"],
            target_binding_id="binding-foreign",
            target_binding_generation=3,
        )

        result = self.run_inbox(
            "--project", "amiga", "--chat", "CHAT-EXACT",
            "--session", "SESSION-EXACT", "--repo-target", "app",
            "--peek", "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([], payload["messages"])
        self.assertIn(
            wrong_binding,
            [entry["path"] for entry in payload["repo_scope_refused"]],
        )

    def test_exact_session_refuses_malformed_repo_targets_before_rendering(self) -> None:
        self.add_exact_session()
        self.add_exact_message("TAIL-MARKER", repo_targets=[1])

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn("malformed exact-session packet repo_targets", result.stderr)
        self.assertNotIn("TAIL-MARKER", result.stdout)

    def test_exact_session_reuses_complete_repo_target_validation(self) -> None:
        self.add_exact_session()
        self.add_exact_message("TAIL-MARKER", repo_targets=[" app "])

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn(
            "malformed exact-session packet repo_targets",
            json.loads(result.stdout)["exact_session_refused"],
        )

    def test_exact_session_does_not_return_another_agents_packet(self) -> None:
        self.add_exact_session()
        foreign = self.add_exact_message("foreign", recipient="relay")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout))
        self.assertEqual([foreign], self.load_inbox()["unread"])

    def test_exact_session_is_project_independent_and_skips_foreign_target(
        self,
    ) -> None:
        self.add_exact_session(project_id="nuvyr", chat_id="CHAT-NUVYR")
        exact = self.add_exact_message(
            "exact",
            project_id="nuvyr",
            chat_id="CHAT-NUVYR",
        )
        self.add_exact_message(
            "foreign",
            project_id="nuvyr",
            chat_id="CHAT-NUVYR",
            target_session_id="SESSION-FOREIGN",
        )

        result = self.run_inbox(
            "--project",
            "nuvyr",
            "--chat",
            "CHAT-NUVYR",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [exact],
            [message["path"] for message in json.loads(result.stdout)["messages"]],
        )

    def test_exact_session_is_read_only_and_does_not_gate_activation(self) -> None:
        self.add_exact_session()
        path = self.add_exact_message("activation", activation=True)

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        message = json.loads(result.stdout)["messages"][0]
        self.assertNotIn("activation_gate", message)
        self.assertEqual([path], self.load_inbox()["unread"])

    def test_unscoped_mark_all_fails_without_mutating(self) -> None:
        amiga = self.add_message("AMIGA", project_line="amiga")

        result = self.run_inbox("--mark-all-read")

        self.assertEqual(2, result.returncode)
        self.assertIn(
            "--mark-all-read requires --project <id> or explicit --all-projects",
            result.stderr,
        )
        self.assertEqual([amiga], self.load_inbox()["unread"])

    def test_explicit_all_projects_reports_complete_blast_radius(self) -> None:
        paths = [
            self.add_message("AMIGA", project_line="amiga"),
            self.add_message("NUVYR", project_line="nuvyr"),
            self.add_message("MISSING", project_line=None),
            self.add_message("EMPTY", project_line=""),
            self.add_message("NULL", project_line="null"),
            self.add_missing_message_pointer("DANGLING"),
        ]

        result = self.run_inbox("--all-projects", "--mark-all-read")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {
                "marked_read": 6,
                "marked_read_by_project": {
                    "<missing-message>": 1,
                    "<unscoped-or-missing-project>": 3,
                    "amiga": 1,
                    "nuvyr": 1,
                },
            },
            json.loads(result.stdout),
        )
        inbox = self.load_inbox()
        self.assertEqual([], inbox["unread"])
        self.assertEqual(paths, inbox["read"])

    def test_repo_target_selector_filters_and_reports_ambiguous_packets(self) -> None:
        matched = self.add_message(
            "MATCHED", project_line="amiga", repo_targets=["llm-collab"]
        )
        partial = self.add_message(
            "PARTIAL",
            project_line="amiga",
            repo_targets=["llm-collab", "amiga"],
        )
        missing = self.add_message("MISSING-REPO", project_line="amiga")
        wrong = self.add_message(
            "WRONG-REPO", project_line="amiga", repo_targets=["amiga"]
        )

        result = self.run_inbox(
            "--project", "amiga", "--repo-target", "llm-collab", "--json"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([matched], [message["path"] for message in payload["messages"]])
        self.assertEqual(
            {
                partial: "route_ambiguous",
                missing: "route_ambiguous",
                wrong: "route_ambiguous",
            },
            {item["path"]: item["reason"] for item in payload["repo_scope_refused"]},
        )
        inbox = self.load_inbox()
        self.assertEqual([partial, missing, wrong], inbox["unread"])
        self.assertEqual([matched], inbox["read"])

    def test_repo_target_mark_all_read_only_mutates_matching_packets(self) -> None:
        matched = self.add_message(
            "MATCHED-ALL", project_line="amiga", repo_targets=["llm-collab"]
        )
        refused = self.add_message(
            "REFUSED-ALL",
            project_line="amiga",
            repo_targets=["llm-collab", "amiga"],
        )

        result = self.run_inbox(
            "--project", "amiga", "--repo-target", "llm-collab", "--mark-all-read"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(1, payload["marked_read"])
        self.assertEqual([matched], self.load_inbox()["read"])
        self.assertEqual([refused], self.load_inbox()["unread"])
        self.assertEqual(
            [{"path": refused, "reason": "route_ambiguous"}],
            payload["repo_scope_refused"],
        )

    def test_repo_scope_is_rechecked_before_read_mutation(self) -> None:
        message = {
            "path": "Chats/late/packet.md",
            "read": False,
            "frontmatter": {"project_id": "amiga", "repo_targets": ["llm-collab"]},
            "body": "packet",
        }
        changed = {
            **message,
            "frontmatter": {"project_id": "amiga", "repo_targets": ["amiga"]},
        }
        stdout = StringIO()
        with patch.object(inbox_lib, "agent_ids", return_value=["codex"]), patch.object(
            inbox_lib, "get_unread_messages", return_value=[message]
        ), patch.object(
            inbox_lib,
            "unread_messages_with_missing_files",
            return_value=[changed],
        ), patch.object(inbox_lib, "mark_messages_read") as mark_read, redirect_stdout(stdout):
            with patch.object(
                sys,
                "argv",
                [
                    "inbox.py",
                    "--me",
                    "codex",
                    "--project",
                    "amiga",
                    "--repo-target",
                    "llm-collab",
                    "--json",
                ],
            ):
                inbox_lib.main()

        mark_read.assert_called_once_with("codex", [])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            [{"path": message["path"], "reason": "route_ambiguous"}],
            payload["repo_scope_refused"],
        )

    def test_repo_target_requires_project_scope(self) -> None:
        result = self.run_inbox("--repo-target", "llm-collab", "--json")

        self.assertEqual(2, result.returncode)
        self.assertIn("--repo-target requires --project <id>", result.stderr)

    def test_mark_all_read_counts_only_paths_surviving_late_scope_recheck(self) -> None:
        message = {
            "path": "Chats/late/count.md",
            "frontmatter": {"project_id": "amiga", "repo_targets": ["llm-collab"]},
            "body": "packet",
        }
        changed = {
            **message,
            "frontmatter": {"project_id": "amiga", "repo_targets": ["amiga"]},
        }
        stdout = StringIO()
        with patch.object(inbox_lib, "agent_ids", return_value=["codex"]), patch.object(
            inbox_lib, "get_unread_messages", return_value=[message]
        ), patch.object(
            inbox_lib,
            "unread_messages_with_missing_files",
            return_value=[changed],
        ), patch.object(inbox_lib, "mark_messages_read") as mark_read, redirect_stdout(stdout):
            with patch.object(
                sys,
                "argv",
                [
                    "inbox.py",
                    "--me",
                    "codex",
                    "--project",
                    "amiga",
                    "--repo-target",
                    "llm-collab",
                    "--mark-all-read",
                ],
            ):
                inbox_lib.main()

        mark_read.assert_called_once_with("codex", [])
        self.assertEqual(
            {
                "marked_read": 0,
                "marked_read_by_project": {},
                "repo_scope_refused": [
                    {"path": message["path"], "reason": "route_ambiguous"}
                ],
            },
            json.loads(stdout.getvalue()),
        )

    def test_publish_session_carries_repository_subscription(self) -> None:
        args = SimpleNamespace(
            publish_session=True,
            me="codex",
            session="SESSION-PUBLISH-SCOPE",
            runtime_family="claude_app",
            project="amiga",
            chat=None,
            repo_target=["llm-collab"],
            project_path=None,
        )
        registered = {}

        def register(args):
            registered.update(vars(args))
            return {"session_id": args.session}

        with patch.object(
            inbox_lib,
            "discover_runtime_session",
            return_value={
                "family": "claude_app",
                "session_id": "claude-runtime",
                "session_source": "test",
            },
        ), patch.object(
            inbox_lib,
            "get_unread_messages",
            return_value=[
                {
                    "frontmatter": {
                        "project_id": "amiga",
                        "chat_id": "CHAT-PUBLISH",
                    }
                }
            ],
        ), patch.object(
            inbox_lib,
            "HEURISTIC_RUNTIME_DISCOVERY_FAMILIES",
            frozenset(),
        ), patch.object(inbox_lib, "register_session", side_effect=register):
            inbox_lib.publish_runtime_identity(args)

        self.assertEqual(["llm-collab"], registered["repo_targets"])

    def test_chat_filter_is_rejected_for_mutation(self) -> None:
        amiga = self.add_message("AMIGA", project_line="amiga")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-AMIGA",
            "--mark-all-read",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("--mark-all-read does not support --chat", result.stderr)
        self.assertEqual([amiga], self.load_inbox()["unread"])

    def test_session_publication_options_are_rejected_for_mutation(self) -> None:
        amiga = self.add_message("AMIGA", project_line="amiga")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--session",
            "SESSION-TEST",
            "--runtime-family",
            "codex_app",
            "--project-path",
            str(self.root),
            "--mark-all-read",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn(
            "--mark-all-read does not support --session, --runtime-family, --project-path",
            result.stderr,
        )
        self.assertEqual([amiga], self.load_inbox()["unread"])

    def test_project_and_all_projects_are_mutually_exclusive(self) -> None:
        amiga = self.add_message("AMIGA", project_line="amiga")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--all-projects",
            "--mark-all-read",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("not allowed with argument --project", result.stderr)
        self.assertEqual([amiga], self.load_inbox()["unread"])

    def test_all_projects_is_not_a_listing_filter(self) -> None:
        amiga = self.add_message("AMIGA", project_line="amiga")

        result = self.run_inbox("--all-projects")

        self.assertEqual(2, result.returncode)
        self.assertIn(
            "--all-projects is only valid with --mark-all-read",
            result.stderr,
        )
        self.assertEqual([amiga], self.load_inbox()["unread"])

    def test_packet_activation_claim_marks_exact_packet_read(self) -> None:
        path = self.add_message("CLAIM", project_line="amiga", activation=True)

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-CLAIM",
            "--packet",
            Path(path).name,
            "--json",
            env={
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-a",
                "LLM_COLLAB_READER_PID": str(os.getpid()),
            },
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        gate = payload["messages"][0]["activation_gate"]
        self.assertTrue(gate["authorized"])
        self.assertEqual(1, gate["fence_token"])
        inbox = self.load_inbox()
        self.assertEqual([], inbox["unread"])
        self.assertEqual([path], inbox["read"])

    def test_malformed_activation_packet_exits_75_without_consuming(self) -> None:
        path = self.add_malformed_activation("BAD")

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-BAD",
            "--packet",
            Path(path).name,
            "--json",
        )

        self.assertEqual(75, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual("malformed_activation", payload["activation_refused"][0]["reason"])
        self.assertEqual([path], self.load_inbox()["unread"])

    def test_late_observer_reports_held_owner_without_consuming(self) -> None:
        path = self.add_message("OBSERVE", project_line="amiga", activation=True)
        first = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-OBSERVE",
            "--packet",
            Path(path).name,
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-a"},
        )
        self.assertEqual(0, first.returncode, first.stderr)

        observed = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-OBSERVE",
            "--packet",
            Path(path).name,
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-b"},
        )

        self.assertEqual(0, observed.returncode, observed.stderr)
        gate = json.loads(observed.stdout)["messages"][0]["activation_gate"]
        self.assertEqual("peek_only", gate["reason"])
        self.assertEqual("runtime-a", gate["owner"]["owner_runtime_session_id"])
        self.assertEqual([path], self.load_inbox()["read"])

    def test_packet_activation_refusal_exits_75_without_consuming(self) -> None:
        path = self.add_message("HELD", project_line="amiga", activation=True)

        first = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-HELD",
            "--packet",
            Path(path).name,
            "--json",
            env={
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-a",
                "LLM_COLLAB_READER_PID": str(os.getpid()),
            },
        )
        self.assertEqual(0, first.returncode, first.stderr)
        inbox = self.load_inbox()
        inbox["read"].remove(path)
        inbox["unread"].append(path)
        write_json(self.root / "agents" / "codex" / "inbox.json", inbox)

        second = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-HELD",
            "--packet",
            Path(path).name,
            "--json",
            env={
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-b",
                "LLM_COLLAB_READER_PID": str(os.getpid()),
            },
        )

        self.assertEqual(75, second.returncode, second.stdout + second.stderr)
        payload = json.loads(second.stdout)
        self.assertEqual(
            "lease_held_by_active_owner",
            payload["activation_refused"][0]["reason"],
        )
        self.assertEqual([path], self.load_inbox()["unread"])

    def test_unregistered_activation_scope_is_terminal_before_reader_session(self):
        # GH-160: an invalid/unregistered project scope must be terminal at the gate,
        # before ensure_reader_session() or the claim's poller cleanup can mutate.
        import _activation_lease as lease_lib
        import _session_autobridge as sa_lib

        sessions_dir = self.root / "State" / "session_autobridge" / "sessions"
        msg = {
            "frontmatter": {
                "activation": True,
                "to": "codex",
                "project_id": "ghost-unregistered",
                "chat_id": "CHAT-X",
                "related_task": "TASK-1",
                "worktree": str(self.worktree),
                "branch": "codex/gh-1571-test",
            },
            "path": "Chats/x/packet.md",
        }
        args = SimpleNamespace(me="codex", session="SESSION-READER")
        with (
            patch.object(
                lease_lib, "get_project",
                lambda pid: {"id": pid} if pid in {"amiga", "nuvyr"} else None,
            ),
            patch.object(
                lease_lib, "project_state_dir",
                lambda _p, _r=self.root: _r / "State" / "session_autobridge" / "per-project" / _p,
            ),
            patch.object(sa_lib, "SESSIONS_DIR", sessions_dir),
        ):
            result = inbox_lib.gate_activation_message(args, msg, consume=True)
        self.assertFalse(result["authorized"])
        self.assertEqual("unregistered_project_scope", result["reason"])
        # Terminal before reader-session creation: nothing was written.
        self.assertFalse(sessions_dir.exists() and any(sessions_dir.glob("*.json")))

    def _reader_sessions_dir_with_owner(self, *, status="parked", native="NAT-1"):
        import _session_autobridge as sa_lib  # noqa: F401 (patched by caller)
        sessions = Path(tempfile.mkdtemp(prefix="gh468-reader-")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        write_json(sessions / "SESSION-OWNER.json", {
            "session_id": "SESSION-OWNER", "agent_id": "codex",
            "project_id": "amiga", "chat_id": "CHAT-A",
            "status": status, "lease_expires_utc": "2999-01-01T00:00:00+00:00",
            "runtime": {"session_id": native},
        })
        return sessions

    def test_gh468_reader_session_refuses_cross_scope_native_and_writes_nothing(self):
        # Finding 1: ensure_reader_session() is a second parked/dispatchable write
        # path. A reader for a native already dispatchable in another (project,
        # chat) must refuse AND leave no record (the refused path writes nothing).
        import _session_autobridge as sa_lib
        sessions = self._reader_sessions_dir_with_owner()
        identity = {"project": "nuvyr", "chat": "CHAT-B"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "already owns a dispatchable binding"):
                inbox_lib.ensure_reader_session(
                    "SESSION-READER", "codex", identity, runtime_id="NAT-1"
                )
            self.assertFalse(
                (sessions / "SESSION-READER.json").exists(),
                "a refused reader registration must not write a lease",
            )

    def test_gh468_reader_session_same_scope_native_is_created(self):
        # The guard must not over-block the normal reader path: a reader in the
        # SAME (project, chat) as the owner is disambiguated, not a collision.
        import _session_autobridge as sa_lib
        sessions = self._reader_sessions_dir_with_owner()
        identity = {"project": "amiga", "chat": "CHAT-A"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            inbox_lib.ensure_reader_session(
                "SESSION-READER", "codex", identity, runtime_id="NAT-1"
            )
            self.assertTrue((sessions / "SESSION-READER.json").exists())

    def test_gh468_reader_collision_returns_structured_refusal_not_exception(self):
        # Finding A: an ownership refusal on the reader path must surface as a
        # structured gate refusal (like every other gate), not an uncaught
        # ValueError escaping the inbox flow.
        def _boom(*a, **k):
            raise ValueError("native session already owns a dispatchable binding in x/y/z")

        msg = {
            "frontmatter": {
                "activation": True, "to": "codex",
                "project_id": "amiga", "chat_id": "CHAT-B",
                "related_task": "TASK-1", "worktree": str(self.worktree),
                "branch": "codex/gh-468-test",
            },
            "path": "Chats/x/packet.md",
        }
        args = SimpleNamespace(me="codex", session="SESSION-READER")
        with (
            patch.object(inbox_lib, "load_lease", lambda identity: None),
            patch.object(inbox_lib, "ensure_reader_session", _boom),
        ):
            result = inbox_lib.gate_activation_message(args, msg, consume=True)
        self.assertFalse(result["authorized"])
        self.assertEqual("native_session_owned_elsewhere", result["reason"])

    def test_released_activation_packet_reclaims_with_newer_fence(self) -> None:
        path = self.add_message("RECLAIM", project_line="amiga", activation=True)
        first = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-RECLAIM",
            "--packet",
            Path(path).name,
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-a"},
        )
        self.assertEqual(0, first.returncode, first.stderr)
        first_gate = json.loads(first.stdout)["messages"][0]["activation_gate"]
        identity = first_gate["identity"]
        session_id = first_gate["lease"]["owner_session_id"]
        release = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bin" / "session_autobridge.py"),
                "lease-release",
                "--project",
                identity["project"],
                "--chat",
                identity["chat"],
                "--task",
                identity["task"],
                "--worktree",
                identity["worktree"],
                "--branch",
                identity["branch"],
                "--target-agent",
                identity["target_agent"],
                "--session",
                session_id,
                "--fence-token",
                str(first_gate["fence_token"]),
                "--claimant-runtime-id",
                "runtime-a",
                "--json",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            env={**os.environ, "LLM_COLLAB_PM2_BIN": str(self.pm2_bin)},
            check=False,
        )
        self.assertEqual(0, release.returncode, release.stderr)
        inbox = self.load_inbox()
        inbox["read"].remove(path)
        inbox["unread"].append(path)
        write_json(self.root / "agents" / "codex" / "inbox.json", inbox)

        second = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-RECLAIM",
            "--packet",
            Path(path).name,
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-b"},
        )

        self.assertEqual(0, second.returncode, second.stderr)
        second_gate = json.loads(second.stdout)["messages"][0]["activation_gate"]
        self.assertEqual(2, second_gate["fence_token"])
        self.assertEqual("runtime-b", second_gate["lease"]["owner_runtime_session_id"])

    def test_packet_selector_ambiguous_across_read_and_unread_fails_before_mutation(self) -> None:
        unread = self.add_message("DUP", project_line="amiga")
        read = self.add_message("DUP", project_line="amiga", inbox_bucket="read")

        result = self.run_inbox("--project", "amiga", "--packet", Path(unread).name, "--json")

        self.assertEqual(75, result.returncode)
        payload = json.loads(result.stdout)
        self.assertEqual("packet_selection_not_unique", payload["error"])
        inbox = self.load_inbox()
        self.assertEqual([unread], inbox["unread"])
        self.assertEqual([read], inbox["read"])

    def test_mark_all_read_holds_activation_packets_and_consumes_missing(self) -> None:
        activation = self.add_message("ACT", project_line="amiga", activation=True)
        ordinary = self.add_message("ORD", project_line="amiga")
        missing = self.add_missing_message_pointer("DANGLING")

        result = self.run_inbox("--all-projects", "--mark-all-read")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(2, payload["marked_read"])
        self.assertEqual(1, payload["held_activation"])
        self.assertEqual([activation], payload["held_activation_paths"])
        inbox = self.load_inbox()
        self.assertEqual([activation], inbox["unread"])
        self.assertEqual([ordinary, missing], inbox["read"])


if __name__ == "__main__":
    unittest.main()
