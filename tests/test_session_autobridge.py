from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "bin" / "session_autobridge.py"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
    write(path, json.dumps(payload, indent=2))


class SessionAutobridgeTest(unittest.TestCase):
    def make_workspace(self) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix="llm-collab-autobridge-"))
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
            {
                "projects": [
                    {
                        "id": "amiga",
                        "display_name": "Amiga",
                        "repos": {"app": "."},
                    }
                ]
            },
        )
        return temp_root

    def add_agent(self, root: Path, agent: dict) -> None:
        agents_file = root / "agents.json"
        if agents_file.exists():
            payload = json.loads(agents_file.read_text())
        else:
            payload = {"agents": []}
        payload["agents"].append(agent)
        write_json(agents_file, payload)
        write(root / "agents" / agent["id"] / "identity.md", f"# Identity: {agent['id']}\n")
        write_json(root / "agents" / agent["id"] / "inbox.json", {"agent": agent["id"], "unread": [], "read": []})

    def add_message(self, root: Path, *, agent_id: str, chat_id: str, project_id: str, title: str) -> str:
        chat_dir = root / "Chats" / f"2026-04-22_autobridge-test__{chat_id}"
        write_json(chat_dir / "meta.json", {"chat_id": chat_id, "project_id": project_id})
        message_rel = f"Chats/{chat_dir.name}/2026-04-22T00-00-00_to-{agent_id}_test.md"
        message_path = root / message_rel
        write(
            message_path,
            "\n".join(
                [
                    "---",
                    f"chat_id: {chat_id}",
                    "from: codex",
                    f"to: {agent_id}",
                    f"title: {title}",
                    "priority: normal",
                    f"project_id: {project_id}",
                    "sent_utc: 2026-04-22T00:00:00+00:00",
                    "---",
                    "",
                    "Hello from the test harness.",
                ]
            ),
        )
        inbox_path = root / "agents" / agent_id / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)
        return message_rel

    def run_cli(self, root: Path, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_runtime_trigger_executes_once(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "api-bot",
                "display_name": "API Bot",
                "activation": {"type": "api_trigger", "watcher_enabled": False},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="api-bot",
            chat_id="CHAT-TEST1234",
            project_id="amiga",
            title="Runtime trigger",
        )
        worker_script = root / "runtime_worker.py"
        output_file = root / "runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps({",
                    "    'session_id': os.environ['LLM_COLLAB_SESSION_ID'],",
                    "    'message_path': os.environ['LLM_COLLAB_MESSAGE_PATH'],",
                    "    'title': payload['message']['title'],",
                    "}, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-RUNTIME",
            "--agent",
            "api-bot",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TEST1234",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )
        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RUNTIME")

        self.assertTrue(dispatch_result["dispatchable"])
        self.assertEqual(1, len(dispatch_result["actions"]))
        self.assertEqual("runtime_trigger", dispatch_result["actions"][0]["effective_action"])
        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("SESSION-RUNTIME", runtime_payload["session_id"])
        self.assertEqual(message_rel, runtime_payload["message_path"])

        dispatch_again = self.run_cli(root, "dispatch", "--session", "SESSION-RUNTIME")
        self.assertEqual([], dispatch_again["actions"])

    def test_human_relay_downgrades_to_prompt(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {
                    "type": "human_relay",
                    "watcher_enabled": False,
                    "identity_note": "You are CDX2 (cdx2). Read only messages addressed to 'cdx2'.",
                },
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-TEST5678",
            project_id="amiga",
            title="Relay fallback",
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-RELAY",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TEST5678",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
        )
        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RELAY")

        self.assertEqual(1, len(dispatch_result["actions"]))
        action = dispatch_result["actions"][0]
        self.assertEqual("relay_prompt", action["effective_action"])
        prompt_path = root / action["relay_result"]["prompt_path"]
        self.assertTrue(prompt_path.exists())
        prompt_text = prompt_path.read_text()
        self.assertIn("Please check your inbox now and execute the latest task.", prompt_text)
        self.assertIn("session_autobridge.py", str(SCRIPT_PATH))


if __name__ == "__main__":
    unittest.main()
