from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = TESTS_ROOT.parent
sys.path.insert(0, str(TESTS_ROOT))
from _runtime_gate_testkit import gate_bypass_env  # noqa: E402


DELIVER = REPO_ROOT / "bin" / "deliver.py"


class DeliverEmptyBodyTest(unittest.TestCase):
    def make_workspace(self, root: Path) -> Path:
        (root / "Chats" / "2026-08-06_empty-body__CHAT-GH546").mkdir(parents=True)
        (root / "agents" / "codex").mkdir(parents=True)
        (root / "agents" / "claude").mkdir(parents=True)
        (root / "agents" / "codex" / "inbox.json").write_text(
            json.dumps({"agent": "codex", "unread": [], "read": []})
        )
        (root / "agents" / "claude" / "inbox.json").write_text(
            json.dumps({"agent": "claude", "unread": [], "read": []})
        )
        (root / "agents.json").write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "id": "codex",
                            "display_name": "Codex",
                            "activation": {"type": "cli_session", "watcher_enabled": True},
                        },
                        {
                            "id": "claude",
                            "display_name": "Claude",
                            # GH-554 refuses an unresolved route BEFORE the body is
                            # read, so a bound recipient would make this fixture fail
                            # on routing setup it never meant to exercise. This suite
                            # is about BODY handling, so the recipient uses GH-554's
                            # own documented escape: a watcherless human needs no
                            # exact binding. Do not "fix" this by weakening that
                            # refusal.
                            "activation": {"type": "human", "watcher_enabled": False},
                        },
                    ]
                }
            )
        )
        (root / "projects.json").write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "test-project",
                            "display_name": "Test project",
                            "repos": {"app": "."},
                        }
                    ]
                }
            )
        )
        (root / "collab.config.json").write_text(
            json.dumps(
                {
                    "workspace_name": "gh546-test",
                    "schema_version": 2,
                    "workspace_id": "ws_gh546",
                    "projects_root": str(root),
                    "project_state_root": str(root / "project-state"),
                    "poll_interval_seconds": 15,
                    "notifications_enabled": False,
                }
            )
        )
        (root / "Chats" / "2026-08-06_empty-body__CHAT-GH546" / "meta.json").write_text(
            json.dumps({"chat_id": "CHAT-GH546", "project_id": "test-project"})
        )
        return root / "Chats" / "2026-08-06_empty-body__CHAT-GH546"

    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def run_deliver(self, root: Path, body: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            **gate_bypass_env(),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return subprocess.run(
            [
                sys.executable,
                str(DELIVER),
                "--chat",
                "CHAT-GH546",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "test-project",
                "--repo-targets",
                "app",
                "--title",
                "Empty body regression",
                "--skip-awareness-instruction",
                "--body-file",
                "-",
            ],
            cwd=root,
            env=env,
            input=body,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_empty_stdin_refuses_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="gh546-") as raw:
            root = Path(raw)
            chat_dir = self.make_workspace(root)
            before = self.snapshot(root)

            result = self.run_deliver(root, " \n\t")

            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("refusing empty message body", result.stderr)
            self.assertIn("_to-claude_empty-body-regression.md", result.stderr)
            self.assertEqual(before, self.snapshot(root))
            self.assertEqual([], list(chat_dir.glob("*_to-claude_*.md")))

    def test_nonempty_stdin_still_delivers(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="gh546-") as raw:
            root = Path(raw)
            chat_dir = self.make_workspace(root)

            result = self.run_deliver(root, "A real packet body.\n")

            self.assertEqual(0, result.returncode, result.stderr)
            packets = list(chat_dir.glob("*_to-claude_*.md"))
            self.assertEqual(1, len(packets))
            self.assertIn("A real packet body.", packets[0].read_text())


if __name__ == "__main__":
    unittest.main()
