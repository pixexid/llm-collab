from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
INBOX_SCRIPT = REPO_ROOT / "bin" / "inbox.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))
import _helpers as helpers_lib
import _session_autobridge as autobridge_lib
import inbox as inbox_lib


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
    ) -> None:
        session = {
            "session_id": "SESSION-EXACT",
            "agent_id": "codex",
            "project_id": project_id,
            "chat_id": chat_id,
            "status": "active",
            "lease_expires_utc": "2099-01-01T00:00:00+00:00",
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

    def test_exact_packet_uniqueness_is_checked_after_session_selection(self) -> None:
        self.add_exact_session()
        exact = "Chats/exact__CHAT-EXACT/packet.md"
        foreign = "Chats/foreign__CHAT-EXACT/packet.md"
        for path, chat, target in (
            (exact, "CHAT-EXACT", "SESSION-EXACT"),
            (foreign, "CHAT-EXACT", "SESSION-FOREIGN"),
        ):
            write(
                self.root / path,
                "\n".join(
                    [
                        "---",
                        f"chat_id: {chat}",
                        "project_id: amiga",
                        "from: claude",
                        "to: codex",
                        f"target_session_id: {target}",
                        "---",
                        "",
                        path,
                    ]
                ),
            )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [foreign, exact], "read": []},
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--packet",
            "packet.md",
            "--json",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [exact],
            [message["path"] for message in json.loads(result.stdout)["messages"]],
        )
        self.assertEqual([foreign], self.load_inbox()["unread"])
        self.assertEqual([exact], self.load_inbox()["read"])

    def test_one_budget_covers_authority_selection_and_consumption(self) -> None:
        session = {
            "session_id": "SESSION-EXACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-EXACT",
            "runtime": {"family": "pi", "session_id": "pi-exact"},
        }
        message = {
            "path": "Chats/CHAT-EXACT/exact.md",
            "frontmatter": {"project_id": "amiga", "chat_id": "CHAT-EXACT"},
            "body": "exact",
        }
        observed = []

        def record(value, expected=None):
            active = autobridge_lib._ACTIVE_READ_BUDGET[-1]
            if expected is not None:
                self.assertIs(active, expected)
            observed.append(active)
            return value

        stdout = StringIO()
        with patch.object(inbox_lib, "agent_ids", return_value=["codex"]), patch.object(
            inbox_lib, "load_session", side_effect=lambda _session: record(session)
        ), patch.object(
            inbox_lib,
            "resolve_exact_dispatch_pair",
            side_effect=lambda *_args: record(((session, {}), None, None)),
        ), patch.object(
            inbox_lib,
            "matching_unread_messages",
            side_effect=lambda *_args, **kwargs: record(
                [message], kwargs["read_budget"]
            ),
        ), patch.object(
            inbox_lib,
            "mark_exact_messages_read",
            side_effect=lambda *args, **kwargs: record(args[1], kwargs["budget"]),
        ), redirect_stdout(stdout), patch.object(
            sys,
            "argv",
            [
                "inbox.py",
                "--me",
                "codex",
                "--project",
                "amiga",
                "--chat",
                "CHAT-EXACT",
                "--session",
                "SESSION-EXACT",
                "--json",
            ],
        ):
            inbox_lib.main()

        self.assertEqual(4, len(observed))
        self.assertTrue(all(budget is observed[0] for budget in observed))

    def test_exact_session_honors_the_requested_limit(self) -> None:
        self.add_exact_session()
        paths = []
        for index in range(12):
            path = f"Chats/exact__CHAT-EXACT/{index}.md"
            paths.append(path)
            write(
                self.root / path,
                "\n".join(
                    [
                        "---",
                        "chat_id: CHAT-EXACT",
                        "project_id: amiga",
                        "from: claude",
                        "to: codex",
                        "target_session_id: SESSION-EXACT",
                        "---",
                        "",
                        str(index),
                    ]
                ),
            )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": paths, "read": []},
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--json",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(10, len(json.loads(result.stdout)["messages"]))
        self.assertEqual(paths[10:], self.load_inbox()["unread"])
        self.assertEqual(paths[:10], self.load_inbox()["read"])

    def test_exact_session_selection_is_project_independent(self) -> None:
        self.add_exact_session(project_id="nuvyr", chat_id="CHAT-NUVYR")
        path = "Chats/exact__CHAT-NUVYR/nuvyr.md"
        write(
            self.root / path,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-NUVYR",
                    "project_id: nuvyr",
                    "from: claude",
                    "to: codex",
                    "target_session_id: SESSION-EXACT",
                    "---",
                    "",
                    "nuvyr exact packet",
                ]
            ),
        )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [path], "read": []},
        )

        result = self.run_inbox(
            "--project",
            "nuvyr",
            "--chat",
            "CHAT-NUVYR",
            "--session",
            "SESSION-EXACT",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [path],
            [message["path"] for message in json.loads(result.stdout)["messages"]],
        )
        self.assertEqual([], self.load_inbox()["unread"])
        self.assertEqual([path], self.load_inbox()["read"])

    def test_exact_packet_selector_can_reinspect_read_history(self) -> None:
        self.add_exact_session()
        path = "Chats/exact__CHAT-EXACT/already-read.md"
        write(
            self.root / path,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-EXACT",
                    "project_id: amiga",
                    "from: claude",
                    "to: codex",
                    "target_session_id: SESSION-EXACT",
                    "---",
                    "",
                    "already read",
                ]
            ),
        )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [], "read": [path]},
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--packet",
            Path(path).name,
            "--peek",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        message = json.loads(result.stdout)["messages"][0]
        self.assertEqual(path, message["path"])
        self.assertTrue(message["read"])
        self.assertEqual([path], self.load_inbox()["read"])

    def test_exact_session_reports_an_unreadable_binding_as_a_refusal(self) -> None:
        self.add_exact_session()
        binding = (
            self.root
            / "State"
            / "session_autobridge"
            / "bindings"
            / "amiga"
            / "CHAT-EXACT"
            / "codex.json"
        )
        binding.write_bytes(b"x" * (autobridge_lib.MAX_BINDING_BYTES + 1))

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn("byte limit", json.loads(result.stdout)["exact_session_refused"])
        self.assertNotIn("Traceback", result.stderr)

    def test_exact_session_refuses_a_foreign_reader_runtime(self) -> None:
        self.add_exact_session()
        path = "Chats/exact__CHAT-EXACT/ordinary.md"
        write(
            self.root / path,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-EXACT",
                    "project_id: amiga",
                    "from: claude",
                    "to: codex",
                    "target_session_id: SESSION-EXACT",
                    "---",
                    "",
                    "ordinary packet",
                ]
            ),
        )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [path], "read": []},
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-foreign"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertEqual(
            "exact_session_runtime_mismatch",
            json.loads(result.stdout)["exact_session_refused"],
        )
        self.assertEqual([path], self.load_inbox()["unread"])

    def test_exact_session_refuses_duplicate_inbox_pointers_before_output(
        self,
    ) -> None:
        self.add_exact_session()
        path = "Chats/exact__CHAT-EXACT/duplicate.md"
        write(
            self.root / path,
            "\n".join(
                [
                    "---",
                    "chat_id: CHAT-EXACT",
                    "project_id: amiga",
                    "from: claude",
                    "to: codex",
                    "target_session_id: SESSION-EXACT",
                    "---",
                    "",
                    "must not be duplicated",
                ]
            ),
        )
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [path, path], "read": []},
        )

        result = self.run_inbox(
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--json",
            env={"LLM_COLLAB_READER_RUNTIME_ID": "pi-exact"},
        )

        self.assertEqual(75, result.returncode, result.stderr)
        self.assertIn(
            "duplicate pointers",
            json.loads(result.stdout)["exact_session_refused"],
        )
        self.assertNotIn("must not be duplicated", result.stdout)
        self.assertEqual([path, path], self.load_inbox()["unread"])

    def test_exact_reader_emits_only_pointers_it_claimed(self) -> None:
        session = {
            "session_id": "SESSION-EXACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-EXACT",
            "runtime": {"family": "pi", "session_id": "pi-exact"},
        }
        message = {
            "path": "Chats/CHAT-EXACT/exact.md",
            "frontmatter": {"project_id": "amiga", "chat_id": "CHAT-EXACT"},
            "body": "must not be emitted",
        }
        stdout = StringIO()
        with patch.object(inbox_lib, "agent_ids", return_value=["codex"]), patch.object(
            inbox_lib, "load_session", return_value=session
        ), patch.object(
            inbox_lib,
            "resolve_exact_dispatch_pair",
            return_value=((session, {}), None, None),
        ), patch.object(
            inbox_lib, "matching_unread_messages", return_value=[message]
        ), patch.object(
            inbox_lib, "gate_activation_message", return_value=None
        ) as gate, patch.object(
            inbox_lib,
            "mark_exact_messages_read",
            side_effect=lambda *_args, **kwargs: (
                kwargs["claim_paths"]([]),
                [],
            )[1],
        ) as claim, redirect_stdout(stdout), patch.object(
            sys,
            "argv",
            [
                "inbox.py",
                "--me",
                "codex",
                "--project",
                "amiga",
                "--chat",
                "CHAT-EXACT",
                "--session",
                "SESSION-EXACT",
                "--json",
            ],
        ):
            inbox_lib.main()

        claim.assert_called_once()
        gate.assert_not_called()
        self.assertEqual([], json.loads(stdout.getvalue())["messages"])
        self.assertNotIn("must not be emitted", stdout.getvalue())

    def test_exact_reader_reports_claimed_packets_before_a_later_gate_refusal(
        self,
    ) -> None:
        session = {
            "session_id": "SESSION-EXACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-EXACT",
            "runtime": {"family": "pi", "session_id": "pi-exact"},
        }
        messages = [
            {
                "path": "Chats/CHAT-EXACT/owned.md",
                "frontmatter": {"project_id": "amiga", "chat_id": "CHAT-EXACT"},
                "body": "owned",
            },
            {
                "path": "Chats/CHAT-EXACT/refused.md",
                "frontmatter": {"project_id": "amiga", "chat_id": "CHAT-EXACT"},
                "body": "refused",
            },
        ]
        gates = [
            {"authorized": True, "reason": "claimed"},
            {"authorized": False, "reason": "held"},
        ]
        stdout = StringIO()

        def claim(*_args, **kwargs):
            refused = kwargs["claim_paths"](
                [message["path"] for message in messages]
            )
            return [
                message["path"]
                for message in messages
                if message["path"] not in refused
            ]

        with patch.object(inbox_lib, "agent_ids", return_value=["codex"]), patch.object(
            inbox_lib, "load_session", return_value=session
        ), patch.object(
            inbox_lib,
            "resolve_exact_dispatch_pair",
            return_value=((session, {}), None, None),
        ), patch.object(
            inbox_lib, "activation_reader_runtime_id", return_value="pi-exact"
        ), patch.object(
            inbox_lib, "matching_unread_messages", return_value=messages
        ), patch.object(
            inbox_lib, "gate_activation_message", side_effect=gates
        ), patch.object(
            inbox_lib, "mark_exact_messages_read", side_effect=claim
        ), redirect_stdout(stdout), patch.object(
            sys,
            "argv",
            [
                "inbox.py",
                "--me",
                "codex",
                "--project",
                "amiga",
                "--chat",
                "CHAT-EXACT",
                "--session",
                "SESSION-EXACT",
                "--json",
            ],
        ):
            with self.assertRaises(SystemExit) as stopped:
                inbox_lib.main()

        self.assertEqual(75, stopped.exception.code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            ["Chats/CHAT-EXACT/owned.md", "Chats/CHAT-EXACT/refused.md"],
            [message["path"] for message in payload["messages"]],
        )
        self.assertEqual(
            ["Chats/CHAT-EXACT/refused.md"],
            [gate["path"] for gate in payload["activation_refused"]],
        )

    def test_exact_consume_serializes_with_delivery(self) -> None:
        old_path = "Chats/old.md"
        new_path = "Chats/new.md"
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {"agent": "codex", "unread": [old_path], "read": []},
        )
        read_started = threading.Event()
        release_read = threading.Event()
        writer_started = threading.Event()
        writer_done = threading.Event()
        original_read = inbox_lib.read_regular_file_bounded

        def blocked_read(path, limit):
            raw = original_read(path, limit)
            read_started.set()
            self.assertTrue(release_read.wait(2))
            return raw

        def consume():
            budget = autobridge_lib.ExactSessionReadBudget(1024 * 1024)
            with autobridge_lib.active_read_budget(budget), patch.object(
                inbox_lib, "read_regular_file_bounded", side_effect=blocked_read
            ):
                inbox_lib.mark_exact_messages_read(
                    "codex", [old_path], budget=budget
                )

        def deliver():
            writer_started.set()
            helpers_lib.add_to_inbox("codex", new_path)
            writer_done.set()

        with patch.object(helpers_lib, "ROOT", self.root), patch.object(
            helpers_lib, "AGENTS_DIR", self.root / "agents"
        ):
            consumer = threading.Thread(target=consume)
            writer = threading.Thread(target=deliver)
            consumer.start()
            self.assertTrue(read_started.wait(2))
            writer.start()
            self.assertTrue(writer_started.wait(2))
            try:
                self.assertFalse(writer_done.wait(0.05))
            finally:
                release_read.set()
            consumer.join(2)
            writer.join(2)

        self.assertFalse(consumer.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual([new_path], self.load_inbox()["unread"])
        self.assertEqual([old_path], self.load_inbox()["read"])

    def test_exact_inbox_bound_counts_read_history(self) -> None:
        write_json(
            self.root / "agents" / "codex" / "inbox.json",
            {
                "agent": "codex",
                "unread": [],
                "read": [f"Chats/{index}.md" for index in range(3)],
            },
        )
        session = {
            "agent_id": "codex",
            "session_id": "SESSION-EXACT",
            "project_id": "amiga",
            "chat_id": "CHAT-EXACT",
        }
        budget = autobridge_lib.ExactSessionReadBudget(1024 * 1024)

        with patch.object(helpers_lib, "ROOT", self.root), patch.object(
            helpers_lib, "AGENTS_DIR", self.root / "agents"
        ), patch.object(autobridge_lib, "ROOT", self.root), autobridge_lib.active_read_budget(
            budget
        ):
            with self.assertRaisesRegex(ValueError, "2 entry limit"):
                autobridge_lib.matching_unread_messages(
                    session,
                    max_entries=2,
                    read_budget=budget,
                )

    def test_exact_mark_indexes_read_history(self) -> None:
        class RefuseLinearMembership(list):
            def __contains__(self, _item):
                raise AssertionError("read history must be indexed once")

        inbox = {
            "unread": ["Chats/old.md"],
            "read": RefuseLinearMembership(["Chats/already.md"]),
        }

        def apply(_agent_id, update, *, load):
            update(inbox)

        with patch.object(inbox_lib, "update_agent_inbox", side_effect=apply):
            first = inbox_lib.mark_exact_messages_read(
                "codex",
                ["Chats/old.md"],
                budget=autobridge_lib.ExactSessionReadBudget(1),
            )
            second = inbox_lib.mark_exact_messages_read(
                "codex",
                ["Chats/old.md"],
                budget=autobridge_lib.ExactSessionReadBudget(1),
            )

        self.assertEqual(["Chats/old.md"], first)
        self.assertEqual([], second)
        self.assertEqual([], inbox["unread"])
        self.assertEqual(["Chats/already.md", "Chats/old.md"], inbox["read"])

    def test_exact_mark_rechecks_duplicate_pointers_under_lock(self) -> None:
        inbox = {
            "unread": ["Chats/duplicate.md"],
            "read": ["Chats/duplicate.md"],
        }

        def apply(_agent_id, update, *, load):
            update(inbox)

        with patch.object(inbox_lib, "update_agent_inbox", side_effect=apply):
            with self.assertRaisesRegex(ValueError, "duplicate pointers"):
                inbox_lib.mark_exact_messages_read(
                    "codex",
                    ["Chats/duplicate.md"],
                    budget=autobridge_lib.ExactSessionReadBudget(1),
                )

        self.assertEqual(
            {
                "unread": ["Chats/duplicate.md"],
                "read": ["Chats/duplicate.md"],
            },
            inbox,
        )

    def test_exact_mark_gates_only_owned_pointers_under_the_lock(self) -> None:
        inbox = {
            "unread": ["Chats/owned.md", "Chats/refused.md"],
            "read": [],
        }
        events: list[object] = []

        def apply(_agent_id, update, *, load):
            events.append("locked")
            update(inbox)
            events.append("saved")

        def claim(paths):
            events.append(("gate", paths))
            return {"Chats/refused.md"}

        with patch.object(inbox_lib, "update_agent_inbox", side_effect=apply):
            claimed = inbox_lib.mark_exact_messages_read(
                "codex",
                ["Chats/owned.md", "Chats/refused.md", "Chats/stale.md"],
                budget=autobridge_lib.ExactSessionReadBudget(1),
                claim_paths=claim,
            )

        self.assertEqual(["Chats/owned.md"], claimed)
        self.assertEqual(["Chats/refused.md"], inbox["unread"])
        self.assertEqual(["Chats/owned.md"], inbox["read"])
        self.assertEqual(
            [
                "locked",
                ("gate", ["Chats/owned.md", "Chats/refused.md"]),
                "saved",
            ],
            events,
        )

    def test_unscoped_session_scan_has_one_cumulative_byte_budget(self) -> None:
        sessions = self.root / "sessions"
        write_json(sessions / "one.json", {"session_id": "one", "pad": "x" * 40})
        write_json(sessions / "two.json", {"session_id": "two", "pad": "x" * 40})

        with patch.object(autobridge_lib, "SESSIONS_DIR", sessions), patch.object(
            autobridge_lib, "MAX_SESSION_SCAN_BYTES", 100
        ):
            with self.assertRaisesRegex(
                autobridge_lib.UnreadableFile, "100 byte limit"
            ):
                autobridge_lib.iter_sessions()

    def test_session_save_is_atomic_and_durable(self) -> None:
        sessions = self.root / "sessions"
        original_replace = os.replace
        with patch.object(autobridge_lib, "SESSIONS_DIR", sessions), patch.object(
            autobridge_lib.os, "replace", wraps=original_replace
        ) as replace, patch.object(
            autobridge_lib.os, "fsync", wraps=os.fsync
        ) as fsync:
            autobridge_lib.save_session({"session_id": "SESSION-DURABLE"})

        self.assertEqual(1, replace.call_count)
        self.assertEqual(2, fsync.call_count)
        self.assertEqual(
            "SESSION-DURABLE",
            json.loads((sessions / "SESSION-DURABLE.json").read_text())["session_id"],
        )

    def test_post_replace_directory_fsync_failure_reports_committed_state(self) -> None:
        sessions = self.root / "sessions"
        original_fsync = os.fsync
        calls = 0

        def fail_directory_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory fsync failed")
            return original_fsync(descriptor)

        with patch.object(autobridge_lib, "SESSIONS_DIR", sessions), patch.object(
            autobridge_lib.os, "fsync", side_effect=fail_directory_fsync
        ):
            with self.assertRaisesRegex(
                autobridge_lib.AtomicWriteCommitted, "was replaced"
            ):
                autobridge_lib.save_session({"session_id": "SESSION-COMMITTED"})

        self.assertEqual(
            "SESSION-COMMITTED",
            json.loads((sessions / "SESSION-COMMITTED.json").read_text())[
                "session_id"
            ],
        )

    def test_exact_mark_returns_claimed_paths_after_post_replace_failure(self) -> None:
        inbox = {"unread": ["Chats/committed.md"], "read": []}

        def apply(_agent_id, update, *, load):
            update(inbox)
            raise autobridge_lib.AtomicWriteCommitted("directory fsync failed")

        with patch.object(inbox_lib, "update_agent_inbox", side_effect=apply):
            claimed = inbox_lib.mark_exact_messages_read(
                "codex",
                ["Chats/committed.md"],
                budget=autobridge_lib.ExactSessionReadBudget(1),
            )

        self.assertEqual(["Chats/committed.md"], claimed)
        self.assertEqual([], inbox["unread"])
        self.assertEqual(["Chats/committed.md"], inbox["read"])

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

    def test_exact_activation_without_runtime_evidence_claims_as_the_live_reader_pid(
        self,
    ) -> None:
        args = SimpleNamespace(me="codex", session="SESSION-EXACT")
        message = {
            "frontmatter": {
                "activation": True,
                "to": "codex",
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT",
                "related_task": "TASK-EXACT",
                "worktree": str(self.worktree),
                "branch": "codex/exact",
            }
        }
        session = {
            "session_id": "SESSION-EXACT",
            "runtime": {"family": "pi", "session_id": "pi-exact"},
        }
        claim_result = {
            "lease": {},
            "fence_token": 1,
            "poller_audit": [],
        }

        with patch.object(
            inbox_lib, "activation_reader_runtime_id", return_value=None
        ), patch.object(
            inbox_lib, "activation_reader_pid", return_value=4321
        ), patch.object(
            inbox_lib, "load_lease", return_value=None
        ), patch.object(
            inbox_lib, "ensure_reader_session"
        ), patch.object(
            inbox_lib, "claim_activation_lease", return_value=claim_result
        ) as claim:
            result = inbox_lib.gate_activation_message(
                args,
                message,
                consume=True,
                exact_session=session,
            )

        self.assertTrue(result["authorized"])
        self.assertEqual(4321, claim.call_args.kwargs["owner_pid"])
        self.assertIsNone(claim.call_args.kwargs["claimant_runtime_id"])

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
