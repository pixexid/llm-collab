from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INBOX_SCRIPT = REPO_ROOT / "bin" / "inbox.py"


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
            {"projects": [{"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}}]},
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
