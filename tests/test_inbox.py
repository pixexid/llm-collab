from __future__ import annotations
import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)

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
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
INBOX_SCRIPT = REPO_ROOT / "bin" / "inbox.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))
import inbox as inbox_lib
import _helpers as helpers_lib
import _session_autobridge as session_autobridge_lib
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

    def test_exact_session_reports_and_skips_a_stale_self_target_packet_generation(self) -> None:
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
        self.assertEqual([], json.loads(result.stdout)["messages"])
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

    def test_exact_read_skips_a_wrong_binding_id_and_returns_the_routable_packets(
        self,
    ) -> None:
        # GH-631: a wrong target_binding_id remains excluded, but it must not suppress
        # the routable remainder of the batch.
        self.add_exact_session(binding_id="binding-current", binding_generation=3)
        routable = [
            self.add_exact_message(
                f"routable-{index}",
                repo_targets=["app"],
                target_binding_id="binding-current",
                target_binding_generation=3,
            )
            for index in range(2)
        ]
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

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(routable, [message["path"] for message in payload["messages"]])
        self.assertEqual(
            [{"path": wrong_binding, "reason": "route_ambiguous"}],
            payload["repo_scope_refused"],
        )

        human = self.run_inbox(
            "--project", "amiga", "--chat", "CHAT-EXACT",
            "--session", "SESSION-EXACT", "--repo-target", "app", "--peek",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )
        self.assertEqual(0, human.returncode, human.stderr)
        self.assertNotIn(wrong_binding, human.stdout)
        self.assertIn("[inbox] Exact-session refused", human.stderr)
        self.assertIn(wrong_binding, human.stderr)
        self.assertIn("route_ambiguous", human.stderr)

    def test_wrong_binding_packet_is_not_consumed_on_acknowledge(self) -> None:
        # GH-457 proof 2 (same-project isolation, real consume path): worker B
        # (binding-current) must not CONSUME worker A's binding-targeted packet
        # (binding-foreign). The exact-read fence must fail closed on --acknowledge
        # too, leaving the foreign-binding packet in B's unread — never moved to
        # read. --peek coverage is insufficient because it never marks state.
        self.add_exact_session(binding_id="binding-current", binding_generation=3)
        routable = [
            self.add_exact_message(
                f"routable-{index}",
                repo_targets=["app"],
                target_binding_id="binding-current",
                target_binding_generation=3,
            )
            for index in range(2)
        ]
        foreign = self.add_exact_message(
            "foreign-target",
            repo_targets=["app"],
            target_binding_id="binding-foreign",
            target_binding_generation=3,
        )
        before = self.load_inbox()
        self.assertIn(foreign, before["unread"])
        self.assertNotIn(foreign, before.get("read", []))

        result = self.run_inbox(
            "--project", "amiga", "--chat", "CHAT-EXACT",
            "--session", "SESSION-EXACT", "--repo-target", "app",
            "--acknowledge", "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(routable, payload["acknowledged"])
        self.assertEqual(routable, [message["path"] for message in payload["messages"]])
        self.assertEqual(
            [{"path": foreign, "reason": "route_ambiguous"}],
            payload["repo_scope_refused"],
        )
        after = self.load_inbox()
        self.assertIn(foreign, after["unread"], "foreign-binding packet must stay unread")
        self.assertNotIn(foreign, after.get("read", []), "must not be marked read")
        self.assertTrue(set(routable).issubset(after["read"]))

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

    def test_bounded_all_scan_refuses_over_cap_without_mutating(self) -> None:
        paths = [
            f"Chats/scan/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1)
        ]
        before = {"agent": "codex", "unread": [], "read": paths}
        write_json(self.root / "agents" / "codex" / "inbox.json", before)

        result = self.run_inbox(
            "--all", "--limit", str(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES), "--json"
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "inbox_scan_limit_exceeded", json.loads(result.stdout)["error"]
        )
        self.assertEqual(before, self.load_inbox())

    def test_default_unread_scan_refuses_over_cap_without_mutating(self) -> None:
        paths = [
            f"Chats/scan/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1)
        ]
        before = {"agent": "codex", "unread": paths, "read": []}
        write_json(self.root / "agents" / "codex" / "inbox.json", before)

        result = self.run_inbox("--project", "amiga", "--peek", "--json")

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "inbox_scan_limit_exceeded", json.loads(result.stdout)["error"]
        )
        self.assertEqual(before, self.load_inbox())

    def test_default_unread_scan_checks_the_index_before_packet_reads(self) -> None:
        paths = [
            f"Chats/{'amiga' if index % 2 == 0 else 'nuvyr'}/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1)
        ]
        with patch.object(
            helpers_lib,
            "load_agent_inbox",
            return_value={"agent": "codex", "unread": paths, "read": []},
        ), patch.object(Path, "exists", autospec=True, return_value=True) as exists, patch.object(
            Path,
            "read_text",
            autospec=True,
            side_effect=lambda path: path.parent.name,
        ), patch.object(
            helpers_lib,
            "parse_frontmatter",
            side_effect=lambda project: ({"project_id": project}, "body"),
        ):
            try:
                messages = helpers_lib.get_unread_messages("codex")
            except inbox_lib.InboxScanLimitExceeded:
                pass
            else:
                self.fail(
                    f"over-cap default read silently returned {len(messages)} messages"
                )

        exists.assert_not_called()

    def test_bootstrap_limit_counts_live_messages_not_pointers(self) -> None:
        dead_paths = [f"Chats/dead/{index}.md" for index in range(5)]
        live_paths = [f"Chats/live/{index}.md" for index in range(5)]
        with patch.object(
            helpers_lib,
            "load_agent_inbox",
            return_value={
                "agent": "codex",
                "unread": [*dead_paths, *live_paths],
                "read": [],
            },
        ), patch.object(
            Path,
            "exists",
            autospec=True,
            side_effect=lambda path: path.parent.name == "live",
        ), patch.object(
            helpers_lib,
            "read_regular_file_bounded",
            side_effect=lambda path, _limit: path.name.encode(),
        ), patch.object(
            helpers_lib,
            "parse_frontmatter",
            side_effect=lambda title: ({"title": title}, "body"),
        ):
            messages = helpers_lib.get_unread_messages("codex", limit=5)

        self.assertEqual(
            live_paths,
            [message["path"] for message in messages],
            "bootstrap would report no unread messages while live packets wait",
        )

    def test_ordinary_json_distinguishes_truncated_and_complete_populations(self) -> None:
        for name in ("ONE", "TWO", "THREE"):
            self.add_message(name, project_line="amiga")
        self.add_message("OTHER", project_line="nuvyr")

        partial_result = self.run_inbox(
            "--project", "amiga", "--peek", "--limit", "2", "--json"
        )
        complete_result = self.run_inbox(
            "--project", "amiga", "--peek", "--limit", "3", "--json"
        )

        self.assertEqual(0, partial_result.returncode, partial_result.stderr)
        self.assertEqual(0, complete_result.returncode, complete_result.stderr)
        partial = json.loads(partial_result.stdout)
        complete = json.loads(complete_result.stdout)
        self.assertEqual(
            {"total": 3, "limit": 2, "truncated": True},
            {key: partial[key] for key in ("total", "limit", "truncated")},
        )
        self.assertEqual(2, len(partial["messages"]))
        self.assertEqual(
            {"total": 3, "limit": 3, "truncated": False},
            {key: complete[key] for key in ("total", "limit", "truncated")},
        )
        self.assertEqual(complete["total"], len(complete["messages"]))
        self.assertNotEqual(partial["truncated"], complete["truncated"])

    def test_ordinary_text_names_scope_population_and_applied_cap(self) -> None:
        for name in ("ONE", "TWO", "THREE"):
            self.add_message(name, project_line="amiga")

        partial = self.run_inbox(
            "--project", "amiga", "--peek", "--limit", "2"
        )
        complete = self.run_inbox(
            "--project", "amiga", "--peek", "--limit", "3"
        )

        self.assertEqual(0, partial.returncode, partial.stderr)
        self.assertIn(
            "[inbox] showing 2 of 3 unread message(s) for codex "
            "(project amiga, --limit 2)",
            partial.stdout,
        )
        self.assertEqual(0, complete.returncode, complete.stderr)
        self.assertIn(
            "[inbox] 3 unread message(s) for codex (project amiga)",
            complete.stdout,
        )

    def test_empty_ordinary_scope_reports_metadata_and_named_scope(self) -> None:
        json_result = self.run_inbox(
            "--project", "amiga", "--peek", "--limit", "2", "--json"
        )
        text_result = self.run_inbox(
            "--project", "amiga", "--peek", "--limit", "2"
        )

        self.assertEqual(0, json_result.returncode, json_result.stderr)
        self.assertEqual(
            {"messages": [], "total": 0, "limit": 2, "truncated": False},
            json.loads(json_result.stdout),
        )
        self.assertEqual(0, text_result.returncode, text_result.stderr)
        self.assertIn(
            "[inbox] 0 unread message(s) for codex (project amiga).",
            text_result.stdout,
        )

    def test_limited_unread_scan_refuses_oversized_index(self) -> None:
        paths = [
            f"Chats/scan/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1)
        ]
        with patch.object(
            helpers_lib,
            "load_agent_inbox",
            return_value={"agent": "codex", "unread": paths, "read": []},
        ), patch.object(Path, "exists", autospec=True) as exists:
            with self.assertRaises(inbox_lib.InboxScanLimitExceeded):
                helpers_lib.get_unread_messages("codex", limit=5)

        exists.assert_not_called()

    def test_unread_scan_refuses_requested_limit_above_cap(self) -> None:
        with patch.object(
            helpers_lib,
            "load_agent_inbox",
            return_value={"agent": "codex", "unread": [], "read": []},
        ):
            with self.assertRaisesRegex(
                inbox_lib.InboxScanLimitExceeded,
                rf"requested inbox limit {inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1} "
                rf"exceeds {inbox_lib.MAX_MESSAGE_SCAN_ENTRIES} entries",
            ):
                helpers_lib.get_unread_messages(
                    "codex", limit=inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1
                )

    def test_default_unread_scan_returns_the_complete_at_cap_index(self) -> None:
        paths = [
            f"Chats/{'amiga' if index % 2 == 0 else 'nuvyr'}/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES)
        ]
        with patch.object(
            helpers_lib,
            "load_agent_inbox",
            return_value={"agent": "codex", "unread": paths, "read": []},
        ), patch.object(Path, "exists", autospec=True, return_value=True), patch.object(
            helpers_lib,
            "read_regular_file_bounded",
            side_effect=lambda path, _limit: path.parent.name.encode(),
        ), patch.object(
            helpers_lib,
            "parse_frontmatter",
            side_effect=lambda project: ({"project_id": project}, "body"),
        ):
            messages = helpers_lib.get_unread_messages("codex")

        self.assertEqual(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES, len(messages))
        self.assertEqual(
            {"amiga", "nuvyr"},
            {message["frontmatter"]["project_id"] for message in messages},
        )

    def test_default_unread_scan_refuses_oversized_index_before_parsing(self) -> None:
        inbox_path = self.root / "agents" / "codex" / "inbox.json"
        inbox_path.write_text(inbox_path.read_text() + " " * 128)
        with patch.object(helpers_lib, "ROOT", self.root), patch.object(
            helpers_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib,
            "MAX_DISPATCH_INBOX_BYTES",
            inbox_path.stat().st_size - 1,
        ), patch.object(helpers_lib.json, "loads", wraps=json.loads) as loads:
            with self.assertRaises(inbox_lib.InboxScanLimitExceeded):
                helpers_lib.get_unread_messages("codex")

        loads.assert_not_called()

    def test_default_unread_scan_refuses_oversized_single_packet(self) -> None:
        path = self.add_message("LARGE", project_line="amiga")
        packet_path = self.root / path
        packet_path.write_text(
            packet_path.read_text()
            + "x" * (session_autobridge_lib.MAX_DISPATCH_PACKET_BYTES + 1)
        )
        with patch.object(helpers_lib, "ROOT", self.root), patch.object(
            helpers_lib,
            "agent_inbox_path",
            return_value=self.root / "agents" / "codex" / "inbox.json",
        ):
            with self.assertRaises(inbox_lib.InboxScanLimitExceeded):
                helpers_lib.get_unread_messages("codex")

    def test_default_unread_scan_budget_is_cumulative_across_packets(self) -> None:
        paths = [
            self.add_message("A", project_line="amiga"),
            self.add_message("B", project_line="nuvyr"),
            self.add_message("C", project_line="amiga"),
        ]
        inbox_path = self.root / "agents" / "codex" / "inbox.json"
        packet_paths = [self.root / path for path in paths]
        budget_limit = (
            inbox_path.stat().st_size
            + sum(path.stat().st_size for path in packet_paths)
            - 1
        )
        self.assertTrue(
            all(path.stat().st_size < budget_limit for path in packet_paths)
        )
        with patch.object(helpers_lib, "ROOT", self.root), patch.object(
            helpers_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib,
            "MAX_DISPATCH_INBOX_BYTES",
            budget_limit,
        ):
            with self.assertRaises(inbox_lib.InboxScanLimitExceeded):
                helpers_lib.get_unread_messages("codex")

    def test_default_unread_scan_normal_amiga_and_non_amiga_inbox_is_unaffected(self) -> None:
        paths = [
            self.add_message("NORMAL-AMIGA", project_line="amiga"),
            self.add_message("NORMAL-NUVYR", project_line="nuvyr"),
        ]
        with patch.object(helpers_lib, "ROOT", self.root), patch.object(
            helpers_lib,
            "agent_inbox_path",
            return_value=self.root / "agents" / "codex" / "inbox.json",
        ):
            messages = helpers_lib.get_unread_messages("codex")

        self.assertEqual(paths, [message["path"] for message in messages])
        self.assertEqual(
            {"amiga", "nuvyr"},
            {message["frontmatter"]["project_id"] for message in messages},
        )

    def test_publish_scan_limit_refuses_before_registration(self) -> None:
        args = SimpleNamespace(
            session=None,
            publish_session=True,
            mark_all_read=False,
            peek=True,
            show_all=False,
            me="codex",
            json_output=True,
        )
        error = inbox_lib.InboxScanLimitExceeded("over cap")
        stdout = StringIO()
        with patch.object(inbox_lib, "parse_args", return_value=args), patch.object(
            inbox_lib, "agent_ids", return_value=["codex"]
        ), patch.object(
            inbox_lib, "publish_runtime_identity", side_effect=error
        ), patch.object(inbox_lib, "register_session") as register, redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                inbox_lib.main()

        self.assertEqual(75, raised.exception.code)
        self.assertEqual("inbox_scan_limit_exceeded", json.loads(stdout.getvalue())["error"])
        register.assert_not_called()

    def test_mark_all_read_scan_limit_refuses_without_mutation(self) -> None:
        args = SimpleNamespace(
            session=None,
            publish_session=False,
            mark_all_read=True,
            peek=False,
            show_all=False,
            me="codex",
            json_output=True,
        )
        error = inbox_lib.InboxScanLimitExceeded("over cap")
        stdout = StringIO()
        with patch.object(inbox_lib, "parse_args", return_value=args), patch.object(
            inbox_lib, "require_current_runtime"
        ), patch.object(inbox_lib, "agent_ids", return_value=["codex"]), patch.object(
            inbox_lib, "mark_all_read", side_effect=error
        ), patch.object(inbox_lib, "mark_messages_read") as mark_read, redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                inbox_lib.main()

        self.assertEqual(75, raised.exception.code)
        self.assertEqual("inbox_scan_limit_exceeded", json.loads(stdout.getvalue())["error"])
        mark_read.assert_not_called()

    def test_bounded_all_scan_accepts_exact_cap(self) -> None:
        paths = [
            f"Chats/scan/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES)
        ]
        before = {"agent": "codex", "unread": [], "read": paths}
        write_json(self.root / "agents" / "codex" / "inbox.json", before)

        result = self.run_inbox(
            "--all", "--limit", str(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES), "--json"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {
                "messages": [],
                "total": 0,
                "limit": inbox_lib.MAX_MESSAGE_SCAN_ENTRIES,
                "truncated": False,
            },
            json.loads(result.stdout),
        )
        self.assertEqual(before, self.load_inbox())

    def test_all_scan_missing_inbox_index_is_empty_without_mutation(self) -> None:
        inbox_path = self.root / "agents" / "codex" / "inbox.json"
        inbox_path.unlink()

        result = self.run_inbox("--all", "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"messages": [], "total": 0, "limit": 10, "truncated": False},
            json.loads(result.stdout),
        )
        self.assertFalse(inbox_path.exists())

    def test_bounded_packet_scan_refuses_over_cap_without_acknowledging(self) -> None:
        paths = [
            f"Chats/scan/{index}.md"
            for index in range(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES + 1)
        ]
        before = {"agent": "codex", "unread": paths, "read": []}
        write_json(self.root / "agents" / "codex" / "inbox.json", before)

        result = self.run_inbox(
            "--packet", paths[-1],
            "--limit", str(inbox_lib.MAX_MESSAGE_SCAN_ENTRIES),
            "--json",
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "inbox_scan_limit_exceeded", json.loads(result.stdout)["error"]
        )
        self.assertEqual(before, self.load_inbox())

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
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-a", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app",
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
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-a", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app"},
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
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-b", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app"},
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
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-a", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app",
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
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-b", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app",
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

    def test_general_read_skips_a_refused_activation_and_drains_valid_mail(self) -> None:
        # GH-502: one refused activation packet must not batch-abort the general
        # (non --session) read and hide the reader's other valid mail. The refused
        # packet is left unread and surfaced; the valid packet is shown and drained.
        valid = self.add_message("OK", project_line="amiga")
        refused = self.add_malformed_activation("BAD")

        result = self.run_inbox("--project", "amiga", "--json")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [refused],
            [entry["path"] for entry in payload.get("activation_refused", [])],
        )
        inbox = self.load_inbox()
        self.assertEqual([valid], inbox["read"])
        self.assertEqual([refused], inbox["unread"])

    def test_general_read_fails_closed_when_every_activation_packet_refused(self) -> None:
        # GH-502: an all-refused batch (no valid unread packet left to show or
        # drain) still exits 75 with nothing consumed.
        first = self.add_malformed_activation("BAD1")
        second = self.add_malformed_activation("BAD2")

        result = self.run_inbox("--project", "amiga", "--json")

        self.assertEqual(75, result.returncode, result.stdout + result.stderr)
        self.assertEqual({first, second}, set(self.load_inbox()["unread"]))
        self.assertEqual([], self.load_inbox()["read"])

    def test_refused_prefix_does_not_consume_the_limit_and_hide_valid_mail(self) -> None:
        # GH-502 P1: a refused activation packet must not consume the --limit
        # budget. With --limit 1 and a refused packet ahead of valid mail, the read
        # must scan past the refused entry to the valid packet (exit 0, valid
        # drained) instead of slicing to the refused prefix and exiting 75 — which,
        # since the refused packet stays unread, would hide the valid mail forever.
        refused = self.add_malformed_activation("BAD")
        valid = self.add_message("OK", project_line="amiga")

        result = self.run_inbox("--project", "amiga", "--limit", "1", "--json")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            [refused],
            [entry["path"] for entry in payload.get("activation_refused", [])],
        )
        self.assertEqual(2, payload["total"])
        self.assertEqual(1, payload["limit"])
        self.assertFalse(
            payload["truncated"],
            "a refused-and-skipped activation packet is not a cap cut",
        )
        inbox = self.load_inbox()
        self.assertEqual([valid], inbox["read"])
        self.assertEqual([refused], inbox["unread"])

    def test_unexamined_trailing_activation_counts_as_truncated(self) -> None:
        valid = self.add_message("OK", project_line="amiga")
        trailing = self.add_malformed_activation("BAD")

        result = self.run_inbox(
            "--project", "amiga", "--limit", "1", "--json"
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(
            payload["truncated"],
            "the unexamined trailing activation packet must make truncation visible",
        )
        self.assertEqual(2, payload["total"])
        self.assertEqual(1, payload["limit"])
        self.assertEqual(
            [],
            payload.get("activation_refused", []),
            "the trailing activation packet was never examined, so no refusal may be reported",
        )
        inbox = self.load_inbox()
        self.assertEqual([valid], inbox["read"])
        self.assertEqual([trailing], inbox["unread"])

    def test_zero_limit_selects_and_consumes_nothing(self):
        # GH-502: --limit 0 must select nothing and consume nothing (matching the
        # prior messages[:0] semantics), not display/consume the first valid packet.
        valid = self.add_message("OK", project_line="amiga")

        result = self.run_inbox("--project", "amiga", "--limit", "0", "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout)["messages"])
        self.assertEqual([valid], self.load_inbox()["unread"])  # not consumed
        self.assertEqual([], self.load_inbox()["read"])

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

    def _reader_sessions_dir_with_owner(self, *, status="parked", native="NAT-1",
                                        family="claude_app"):
        import _session_autobridge as sa_lib  # noqa: F401 (patched by caller)
        sessions = Path(tempfile.mkdtemp(prefix="gh468-reader-")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        write_json(sessions / "SESSION-OWNER.json", {
            "session_id": "SESSION-OWNER", "agent_id": "codex",
            "project_id": "amiga", "chat_id": "CHAT-A",
            "status": status, "lease_expires_utc": "2999-01-01T00:00:00+00:00",
            # An ORDINARY-family lease: the reader must resolve THIS family for the
            # native id (its runtime id IS the registered native), not a synthetic
            # "reader", or the (family, id) guard would miss the collision.
            "runtime": {"family": family, "session_id": native},
        })
        return sessions

    def test_gh468_reader_session_refuses_cross_scope_native_and_writes_nothing(self):
        # Finding 1 + reader-family P1: a reader whose native id is already owned
        # by an ORDINARY lease in another (project, chat) must resolve that lease's
        # family, refuse, AND leave no record (the refused path writes nothing).
        import _session_autobridge as sa_lib
        sessions = self._reader_sessions_dir_with_owner(family="claude_app")
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
        # SAME (project, chat) as the owner is disambiguated, not a collision, and
        # it adopts the owner's real family.
        import _session_autobridge as sa_lib
        sessions = self._reader_sessions_dir_with_owner(family="claude_app")
        identity = {"project": "amiga", "chat": "CHAT-A"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            inbox_lib.ensure_reader_session(
                "SESSION-READER", "codex", identity, runtime_id="NAT-1"
            )
            record = json.loads((sessions / "SESSION-READER.json").read_text())
            self.assertEqual("claude_app", record["runtime"]["family"])

    def test_gh468_reader_ambiguous_native_family_fails_closed(self):
        # Two live different-family leases share the native id: the reader cannot
        # resolve one, so ensure_reader_session must fail closed (raise) and write
        # no lease.
        import _session_autobridge as sa_lib
        sessions = Path(tempfile.mkdtemp(prefix="gh468-reader-amb-")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        for sid, fam, chat in (("A", "claude_app", "CHAT-A"), ("B", "gemini_cli", "CHAT-B")):
            write_json(sessions / f"SESSION-OWN-{sid}.json", {
                "session_id": f"SESSION-OWN-{sid}", "agent_id": "codex",
                "project_id": "amiga", "chat_id": chat, "status": "parked",
                "lease_expires_utc": "2999-01-01T00:00:00+00:00",
                "runtime": {"family": fam, "session_id": "NAT-1"},
            })
        identity = {"project": "amiga", "chat": "CHAT-C"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            with self.assertRaises(ValueError):
                inbox_lib.ensure_reader_session(
                    "SESSION-READER", "codex", identity, runtime_id="NAT-1"
                )
            self.assertFalse((sessions / "SESSION-READER.json").exists())

    def test_gh468_reader_prefers_carried_family_over_inference(self):
        # The reader's ACTUAL family carried from the environment is authoritative
        # even before any ordinary lease exists, closing the reader-first ordering:
        # the stored family is the carried one, not the synthetic "reader".
        import _session_autobridge as sa_lib
        sessions = self._reader_sessions_dir_with_owner(family="claude_app")
        identity = {"project": "nuvyr", "chat": "CHAT-B"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            inbox_lib.ensure_reader_session(
                "SESSION-READER", "codex", identity,
                runtime_id="NAT-UNOWNED", runtime_family="codex_app",
            )
            record = json.loads((sessions / "SESSION-READER.json").read_text())
            self.assertEqual("codex_app", record["runtime"]["family"])

    def test_gh468_activation_reader_runtime_family_from_env(self):
        # Explicit family var wins; a family-specific id var implies its family.
        with patch.dict(os.environ, {"LLM_COLLAB_READER_RUNTIME_FAMILY": "gemini_cli"}, clear=False):
            self.assertEqual("gemini_cli", inbox_lib.activation_reader_runtime_family())
        with patch.dict(os.environ, {"CODEX_SESSION_ID": "abc"}, clear=True):
            self.assertEqual("codex_app", inbox_lib.activation_reader_runtime_family())

    def test_gh468_cached_family_less_reader_is_upgraded_when_family_arrives(self):
        # A first family-less attempt persists an unresolved reader the gate
        # refuses; a retry that now carries the family must upgrade the cached
        # record in place (else the packet is refused forever).
        import _session_autobridge as sa_lib
        sessions = Path(tempfile.mkdtemp(prefix="gh468-upg-")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        write_json(sessions / "SESSION-READER.json", {
            "session_id": "SESSION-READER", "agent_id": "codex",
            "project_id": "amiga", "chat_id": "CHAT-B", "status": "parked",
            "ephemeral_reader": True, "lease_expires_utc": "2999-01-01T00:00:00+00:00",
            "runtime": {"family": "reader", "session_id": "NAT-1"},
        })
        identity = {"project": "amiga", "chat": "CHAT-B"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            rec = inbox_lib.ensure_reader_session(
                "SESSION-READER", "codex", identity,
                runtime_id="NAT-1", runtime_family="codex_app")
            self.assertEqual("codex_app", rec["runtime"]["family"])
            on_disk = json.loads((sessions / "SESSION-READER.json").read_text())
            self.assertEqual("codex_app", on_disk["runtime"]["family"])

    def test_gh468_cached_non_reader_session_is_not_mutated(self):
        # The upgrade path must never rewrite an ordinary (non-ephemeral) session.
        import _session_autobridge as sa_lib
        sessions = Path(tempfile.mkdtemp(prefix="gh468-upg2-")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        write_json(sessions / "SESSION-ORD.json", {
            "session_id": "SESSION-ORD", "agent_id": "codex",
            "project_id": "amiga", "chat_id": "CHAT-B", "status": "parked",
            "runtime": {"family": "claude_app", "session_id": "NAT-1"},
        })
        identity = {"project": "amiga", "chat": "CHAT-B"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            rec = inbox_lib.ensure_reader_session(
                "SESSION-ORD", "codex", identity,
                runtime_id="NAT-1", runtime_family="gemini_cli")
            self.assertEqual("claude_app", rec["runtime"]["family"])

    def test_gh468_reader_with_no_ordinary_owner_falls_back_to_reader_family(self):
        # No ordinary lease owns this native id, so there is nothing to collide
        # with: the reader is created and falls back to the synthetic "reader"
        # family.
        import _session_autobridge as sa_lib
        sessions = self._reader_sessions_dir_with_owner(family="claude_app")
        identity = {"project": "nuvyr", "chat": "CHAT-B"}
        with patch.object(sa_lib, "SESSIONS_DIR", sessions):
            inbox_lib.ensure_reader_session(
                "SESSION-READER", "codex", identity, runtime_id="NAT-UNOWNED"
            )
            record = json.loads((sessions / "SESSION-READER.json").read_text())
            self.assertEqual("reader", record["runtime"]["family"])

    def test_gh468_reader_collision_returns_structured_refusal_not_exception(self):
        # Finding A: an ownership refusal on the reader path must surface as a
        # structured gate refusal (like every other gate), not an uncaught
        # exception escaping the inbox flow.
        import session_autobridge as sa_cli

        def _boom(*a, **k):
            raise sa_cli.NativeSessionOwnedElsewhere(
                "native session already owns a dispatchable binding in x/y/z")

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

    def _gate_with_reader_raising(self, exc):
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

        def _raise(*a, **k):
            raise exc

        with (
            patch.object(inbox_lib, "load_lease", lambda identity: None),
            patch.object(inbox_lib, "ensure_reader_session", _raise),
        ):
            return inbox_lib.gate_activation_message(args, msg, consume=True)

    def _gate_with_reader_record(self, reader_record, *, runtime_id="NAT-1"):
        # Drive gate_activation_message with a reader record of a chosen family and
        # a mocked claim so we can assert refuse-before-claim vs proceed-to-claim.
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
        claim_mock = Mock(return_value={"lease": {}, "fence_token": 1, "poller_audit": {}})
        with (
            patch.object(inbox_lib, "load_lease", lambda identity: None),
            patch.object(inbox_lib, "activation_reader_runtime_id", lambda: runtime_id),
            patch.object(inbox_lib, "activation_reader_runtime_family", lambda: None),
            patch.object(inbox_lib, "ensure_reader_session", lambda *a, **k: reader_record),
            patch.object(inbox_lib, "claim_activation_lease", claim_mock),
        ):
            result = inbox_lib.gate_activation_message(args, msg, consume=True)
        return result, claim_mock

    def test_gh468_gate_refuses_family_less_reader_before_claim(self):
        # No family: refuse before claim, packet stays unread (claim not called).
        result, claim = self._gate_with_reader_record({"runtime": {"session_id": "NAT-1"}})
        self.assertFalse(result["authorized"])
        self.assertEqual("reader_runtime_family_unresolved", result["reason"])
        claim.assert_not_called()

    def test_gh468_gate_refuses_legacy_reader_family_before_claim(self):
        result, claim = self._gate_with_reader_record(
            {"runtime": {"session_id": "NAT-1", "family": "reader"}})
        self.assertFalse(result["authorized"])
        self.assertEqual("reader_runtime_family_unresolved", result["reason"])
        claim.assert_not_called()

    def test_gh468_gate_allows_resolved_family_reader_to_claim(self):
        result, claim = self._gate_with_reader_record(
            {"runtime": {"session_id": "NAT-1", "family": "claude_app"}})
        self.assertTrue(result["authorized"])
        claim.assert_called_once()

    def test_gh468_reader_family_ambiguity_has_its_own_gate_reason(self):
        # An ambiguity is not an ownership collision: the gate reports a distinct
        # reason so JSON clients can branch on the correct remediation.
        import session_autobridge as sa_cli
        result = self._gate_with_reader_raising(
            sa_cli.AmbiguousNativeFamily("two live families share NAT-1"))
        self.assertFalse(result["authorized"])
        self.assertEqual("native_family_ambiguous", result["reason"])

    def test_gh468_reader_corruption_is_not_mislabeled_as_owned_elsewhere(self):
        # A registry-corruption ValueError must NOT be caught as an ownership
        # collision: the gate lets it surface distinctly rather than reporting a
        # false native_session_owned_elsewhere.
        with self.assertRaises(ValueError):
            self._gate_with_reader_raising(ValueError("malformed session record SESSION-X"))

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
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-a", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app"},
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
            env={"LLM_COLLAB_READER_RUNTIME_ID": "runtime-b", "LLM_COLLAB_READER_RUNTIME_FAMILY": "codex_app"},
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
