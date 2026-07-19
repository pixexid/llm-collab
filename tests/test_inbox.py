from __future__ import annotations

import json
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_message(self, name: str, *, project_line: str | None) -> str:
        rel_path = f"Chats/2026-07-19_test__CHAT-{name}/{name}_to-codex.md"
        frontmatter = [
            "---",
            f"chat_id: CHAT-{name}",
            "from: claude",
            "to: codex",
            f"title: {name}",
        ]
        if project_line is not None:
            frontmatter.append(f"project_id: {project_line}")
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

    def run_inbox(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INBOX_SCRIPT), "--me", "codex", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
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


if __name__ == "__main__":
    unittest.main()
