from __future__ import annotations

import json
import os
import base64
import hashlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "bin" / "session_autobridge.py"
DELIVER_SCRIPT = REPO_ROOT / "bin" / "deliver.py"
INBOX_SCRIPT = REPO_ROOT / "bin" / "inbox.py"
WATCH_INBOX_SCRIPT = REPO_ROOT / "bin" / "watch_inbox.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _session_autobridge as session_autobridge_lib


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

    def add_message(
        self,
        root: Path,
        *,
        agent_id: str,
        chat_id: str,
        project_id: str,
        title: str,
        sender_session_id: str | None = None,
        target_session_id: str | None = None,
        sender_agent_id: str | None = None,
    ) -> str:
        chat_dir = root / "Chats" / f"2026-04-22_autobridge-test__{chat_id}"
        write_json(chat_dir / "meta.json", {"chat_id": chat_id, "project_id": project_id})
        message_rel = f"Chats/{chat_dir.name}/2026-04-22T00-00-00_to-{agent_id}_test.md"
        message_path = root / message_rel
        frontmatter_lines = [
            "---",
            f"chat_id: {chat_id}",
            f"from: {sender_agent_id or 'codex'}",
            f"to: {agent_id}",
            f"title: {title}",
            "priority: normal",
            f"project_id: {project_id}",
            "sent_utc: 2026-04-22T00:00:00+00:00",
        ]
        if sender_session_id:
            frontmatter_lines.append(f"sender_session_id: {sender_session_id}")
        if target_session_id:
            frontmatter_lines.append(f"target_session_id: {target_session_id}")
        frontmatter_lines.extend(
            [
                "---",
                "",
                "Hello from the test harness.",
            ]
        )
        write(
            message_path,
            "\n".join(frontmatter_lines),
        )
        inbox_path = root / "agents" / agent_id / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)
        return message_rel

    def run_cli(self, root: Path, *args: str) -> dict:
        return self.run_cli_with_env(root, None, *args)

    def run_cli_with_env(self, root: Path, env: dict[str, str] | None, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env={**os.environ, "LLM_COLLAB_UI_REFRESH": "0", **(env or {})},
            check=True,
        )
        return json.loads(result.stdout)

    def create_chat(self, root: Path, *, chat_dir_name: str, chat_id: str, project_id: str) -> Path:
        chat_dir = root / "Chats" / chat_dir_name
        write_json(chat_dir / "meta.json", {"chat_id": chat_id, "project_id": project_id})
        return chat_dir

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
            "--runtime-family",
            "api_trigger",
            "--runtime-session-id",
            "api-trigger-1",
            "--runtime-session-source",
            "test_fixture",
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

    def test_runtime_trigger_derives_resume_command_from_registered_session(self):
        fixtures = [
            ("codex_app", "LLM_COLLAB_CODEX_BIN", ["exec", "resume"], ["--json", "--skip-git-repo-check"]),
            ("claude_app", "LLM_COLLAB_CLAUDE_BIN", ["-p", "--output-format", "json", "--resume"], []),
            ("gemini_cli", "LLM_COLLAB_GEMINI_BIN", ["--prompt"], []),
        ]

        for runtime_family, env_var, expected_prefix, expected_suffix in fixtures:
            with self.subTest(runtime_family=runtime_family):
                root = self.make_workspace()
                self.add_agent(
                    root,
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "activation": {"type": "cli_session", "watcher_enabled": True},
                    },
                )
                self.add_message(
                    root,
                    agent_id="codex",
                    chat_id="CHAT-DERIVED123",
                    project_id="amiga",
                    title="Derived runtime wake",
                    sender_session_id="claude-session-2",
                    target_session_id=f"{runtime_family}-session-1",
                    sender_agent_id="claude",
                )

                output_file = root / f"{runtime_family}-runtime-result.json"
                runtime_script = root / f"{runtime_family}-runtime.py"
                write(
                    runtime_script,
                    "\n".join(
                        [
                            "#!/usr/bin/env python3",
                            "import json",
                            "import os",
                            "import sys",
                            "from pathlib import Path",
                            "payload = {",
                            "    'argv': sys.argv[1:],",
                            "    'stdin': json.load(sys.stdin),",
                            "    'env': {",
                            "        'session_id': os.environ.get('LLM_COLLAB_SESSION_ID'),",
                            "        'runtime_family': os.environ.get('LLM_COLLAB_RUNTIME_FAMILY'),",
                            "        'runtime_session_id': os.environ.get('LLM_COLLAB_RUNTIME_SESSION_ID'),",
                            "        'runtime_home': os.environ.get('LLM_COLLAB_RUNTIME_HOME'),",
                            "        'codex_home': os.environ.get('CODEX_HOME'),",
                            "        'claude_home': os.environ.get('CLAUDE_HOME'),",
                            "        'gemini_home': os.environ.get('GEMINI_HOME'),",
                            "        'target_session_id': os.environ.get('LLM_COLLAB_TARGET_SESSION_ID'),",
                            "        'sender_session_id': os.environ.get('LLM_COLLAB_SENDER_SESSION_ID'),",
                            "    },",
                            "}",
                            f"Path({json.dumps(str(output_file))}).write_text(json.dumps(payload, indent=2))",
                        ]
                    ),
                )
                runtime_script.chmod(0o755)

                self.run_cli(
                    root,
                    "register",
                    "--session",
                    "SESSION-DERIVED",
                    "--agent",
                    "codex",
                    "--project",
                    "amiga",
                    "--chat",
                    "CHAT-DERIVED123",
                    "--mode",
                    "auto-read",
                    "--wake-strategy",
                    "runtime_trigger",
                    "--runtime-family",
                    runtime_family,
                    "--runtime-session-id",
                    f"{runtime_family}-session-1",
                    "--runtime-session-source",
                    "first_read",
                )

                runtime_home = root / f"{runtime_family}-home"
                runtime_home.mkdir(parents=True, exist_ok=True)
                session_payload = self.run_cli(root, "show", "--session", "SESSION-DERIVED")
                session_payload["runtime"]["home"] = str(runtime_home)
                write_json(
                    root / "State" / "session_autobridge" / "sessions" / "SESSION-DERIVED.json",
                    session_payload,
                )

                dispatch_result = self.run_cli_with_env(
                    root,
                    {env_var: str(runtime_script)},
                    "dispatch",
                    "--session",
                    "SESSION-DERIVED",
                )

                self.assertEqual(1, len(dispatch_result["actions"]))
                action = dispatch_result["actions"][0]
                self.assertEqual("runtime_trigger", action["effective_action"])
                self.assertTrue(action["runtime_result"]["derived_command"])
                self.assertEqual(0, action["runtime_result"]["returncode"])

                runtime_payload = json.loads(output_file.read_text())
                argv = runtime_payload["argv"]
                self.assertEqual(expected_prefix, argv[: len(expected_prefix)])
                if expected_suffix:
                    self.assertEqual(expected_suffix, argv[-len(expected_suffix) :])
                self.assertIn(f"{runtime_family}-session-1", argv)
                if runtime_family == "gemini_cli":
                    resume_index = argv.index("--resume")
                    self.assertEqual(f"{runtime_family}-session-1", argv[resume_index + 1])
                    output_index = argv.index("--output-format")
                    self.assertEqual("json", argv[output_index + 1])
                self.assertIn("Derived runtime wake", json.dumps(runtime_payload["stdin"]))
                self.assertIn("claude-session-2", json.dumps(runtime_payload["stdin"]))
                self.assertEqual("SESSION-DERIVED", runtime_payload["env"]["session_id"])
                self.assertEqual(runtime_family, runtime_payload["env"]["runtime_family"])
                self.assertEqual(f"{runtime_family}-session-1", runtime_payload["env"]["runtime_session_id"])
                self.assertEqual(str(runtime_home), runtime_payload["env"]["runtime_home"])
                if runtime_family == "codex_app":
                    self.assertEqual(str(runtime_home), runtime_payload["env"]["codex_home"])
                if runtime_family == "claude_app":
                    self.assertEqual(str(runtime_home), runtime_payload["env"]["claude_home"])
                if runtime_family == "gemini_cli":
                    self.assertEqual(str(runtime_home), runtime_payload["env"]["gemini_home"])
                self.assertEqual(f"{runtime_family}-session-1", runtime_payload["env"]["target_session_id"])
                self.assertEqual("claude-session-2", runtime_payload["env"]["sender_session_id"])

    def test_codex_runtime_trigger_prefers_app_server_when_available(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-APPSERVER",
            project_id="amiga",
            title="App server visible refresh",
            target_session_id="codex-thread-appserver",
        )

        request_log: list[dict] = []
        ready = threading.Event()

        def read_exact(conn: socket.socket, count: int) -> bytes:
            chunks: list[bytes] = []
            remaining = count
            while remaining:
                chunk = conn.recv(remaining)
                if not chunk:
                    raise ConnectionError("closed")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def read_frame(conn: socket.socket) -> dict:
            first, second = read_exact(conn, 2)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(read_exact(conn, 2), "big")
            elif length == 127:
                length = int.from_bytes(read_exact(conn, 8), "big")
            mask = read_exact(conn, 4) if second & 0x80 else b""
            payload = read_exact(conn, length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self.assertEqual(0x1, first & 0x0F)
            return json.loads(payload.decode("utf-8"))

        def write_frame(conn: socket.socket, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            header = bytearray([0x81])
            if len(body) < 126:
                header.append(len(body))
            elif len(body) <= 0xFFFF:
                header.extend([126, (len(body) >> 8) & 0xFF, len(body) & 0xFF])
            else:
                header.append(127)
                header.extend(len(body).to_bytes(8, "big"))
            conn.sendall(bytes(header) + body)

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            ready.set()
            conn, _ = server.accept()
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                headers = request.decode("iso-8859-1")
                key_line = next(line for line in headers.splitlines() if line.lower().startswith("sec-websocket-key:"))
                key = key_line.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                while True:
                    frame = read_frame(conn)
                    method = frame.get("method")
                    request_log.append(frame)
                    if frame.get("id"):
                        if method == "initialize":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"serverInfo": {"name": "fake"}}})
                        elif method == "thread/resume":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"thread": {"id": "codex-thread-appserver"}}})
                        elif method == "model/list":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"data": [{"id": "gpt-test", "isDefault": True}]}})
                        elif method == "turn/start":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
                            write_frame(conn, {"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": "codex-thread-appserver", "turn": {"id": "turn-1"}}})
                            write_frame(conn, {"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "APP_SERVER_OK"}}})
                            write_frame(conn, {"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
                            break
                        else:
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {}})
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(timeout=2)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-APPSERVER",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-APPSERVER",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-appserver",
            "--runtime-session-source",
            "first_read",
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {"LLM_COLLAB_CODEX_APP_SERVER_URL": f"ws://127.0.0.1:{port}"},
            "dispatch",
            "--session",
            "SESSION-CODEX-APPSERVER",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_app_server", action["runtime_result"]["adapter"])
        self.assertEqual("APP_SERVER_OK", action["runtime_result"]["stdout"])
        self.assertIn("turn/started", action["runtime_result"]["notifications"])
        self.assertIn("turn/completed", action["runtime_result"]["notifications"])
        turn_start = next(frame for frame in request_log if frame.get("method") == "turn/start")
        self.assertEqual("gpt-test", turn_start["params"]["model"])

    def test_codex_app_server_discovery_matches_exact_codex_home(self):
        rows = [
            {
                "pid": 10,
                "command": (
                    "/Applications/Codex.app/Contents/Resources/codex app-server "
                    "--listen ws://127.0.0.1:8765 "
                    "CODEX_HOME=/Users/test/.codex-app-account2"
                ),
            },
            {
                "pid": 11,
                "command": (
                    "/Applications/Codex.app/Contents/Resources/codex app-server "
                    "--listen ws://127.0.0.1:8767 "
                    "--ws-token-file /tmp/main-token "
                    "CODEX_HOME=/Users/test/.codex"
                ),
            },
        ]

        with patch.object(session_autobridge_lib, "codex_app_server_process_rows", return_value=rows):
            result = session_autobridge_lib.discover_codex_app_server("/Users/test/.codex")

        self.assertIsNotNone(result)
        self.assertEqual(11, result["pid"])
        self.assertEqual("ws://127.0.0.1:8767", result["url"])
        self.assertEqual("/tmp/main-token", result["token_file"])

    def test_codex_shortcut_refresh_is_reported_unsupported(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-REFRESH",
            project_id="amiga",
            title="Refresh visible Codex UI",
            target_session_id="codex-thread-1",
        )

        worker_script = root / "codex_runtime.py"
        write(
            worker_script,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "import sys",
                    "json.load(sys.stdin)",
                    "print('ok')",
                ]
            ),
        )
        worker_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-REFRESH",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-REFRESH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "shortcut",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-REFRESH",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertTrue(action["ui_refresh_result"]["skipped"])
        self.assertEqual("codex_shortcut_refresh_unsupported", action["ui_refresh_result"]["reason"])

    def test_codex_cdp_refresh_reports_missing_debug_port(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-CDP",
            project_id="amiga",
            title="Refresh visible Codex UI through CDP",
            target_session_id="codex-thread-cdp",
        )

        worker_script = root / "codex_cdp_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-CDP",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-CDP",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-cdp",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "cdp",
                "LLM_COLLAB_CODEX_CDP_PORT": "9",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-CDP",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_cdp_refresh", action["ui_refresh_result"]["method"])
        self.assertEqual(1, action["ui_refresh_result"]["returncode"])
        self.assertIn("remote-debugging-port", action["ui_refresh_result"]["stderr"])

    def test_successful_codex_runtime_trigger_can_reopen_thread_deeplink(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        runtime_session_id = "019dbb4c-ac68-7f10-8332-77ea314a137f"
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-DEEPLINK",
            project_id="amiga",
            title="Refresh visible Codex account UI by deeplink",
            target_session_id=runtime_session_id,
        )

        worker_script = root / "codex_deeplink_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        fake_app_log = root / "fake_codex_deeplink_app.json"
        fake_app = root / "fake_codex_deeplink_app.py"
        write(
            fake_app,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    f"Path({json.dumps(str(fake_app_log))}).write_text(json.dumps({{'CODEX_HOME': os.environ.get('CODEX_HOME'), 'argv': sys.argv[1:]}}, indent=2))",
                ]
            ),
        )
        fake_app.chmod(0o755)

        runtime_home = root / ".codex-app-account2"
        runtime_home.mkdir()
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-DEEPLINK",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-DEEPLINK",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            runtime_session_id,
            "--runtime-session-source",
            str(runtime_home / "session_index.jsonl"),
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "deeplink",
                "LLM_COLLAB_CODEX_APP_BIN": str(fake_app),
                "LLM_COLLAB_CODEX_DEEPLINK_REQUIRE_PROCESS": "0",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-DEEPLINK",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_thread_deeplink", action["ui_refresh_result"]["method"])
        self.assertEqual(0, action["ui_refresh_result"]["returncode"])

        for _ in range(20):
            if fake_app_log.exists():
                break
            __import__("time").sleep(0.1)
        self.assertTrue(fake_app_log.exists())
        fake_payload = json.loads(fake_app_log.read_text())
        self.assertEqual(str(runtime_home), fake_payload["CODEX_HOME"])
        self.assertIn("codex://threads/019dbb4c-ac68-7f10-8332-77ea314a137f", fake_payload["argv"])

    def test_successful_codex_runtime_trigger_can_relaunch_account_ui(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-RELAUNCH",
            project_id="amiga",
            title="Relaunch visible Codex account UI",
            target_session_id="codex-thread-relaunch",
        )

        worker_script = root / "codex_relaunch_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        fake_app_log = root / "fake_codex_app.log"
        fake_app = root / "fake_codex_app.py"
        write(
            fake_app,
            "\n".join(
                [
                    "#!/bin/sh",
                    f"printf '%s' \"$CODEX_HOME\" > {json.dumps(str(fake_app_log))}",
                ]
            ),
        )
        fake_app.chmod(0o755)

        runtime_home = root / ".codex-worker"
        runtime_home.mkdir()
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-RELAUNCH",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-RELAUNCH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-relaunch",
            "--runtime-session-source",
            str(runtime_home / "session_index.jsonl"),
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "relaunch_account",
                "LLM_COLLAB_CODEX_APP_BIN": str(fake_app),
                "LLM_COLLAB_CODEX_REMOTE_DEBUGGING_PORT": "9224",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-RELAUNCH",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_relaunch_account", action["ui_refresh_result"]["method"])
        self.assertEqual(0, action["ui_refresh_result"]["returncode"])
        self.assertIsNone(action["ui_refresh_result"]["terminated_pid"])
        self.assertEqual("9224", action["ui_refresh_result"]["remote_debugging_port"])

        for _ in range(20):
            if fake_app_log.exists():
                break
            __import__("time").sleep(0.1)
        self.assertTrue(fake_app_log.exists())
        self.assertEqual(str(runtime_home), fake_app_log.read_text())

    def test_successful_claude_runtime_trigger_refreshes_app_ui(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="claude",
            chat_id="CHAT-CLAUDE-REFRESH",
            project_id="amiga",
            title="Refresh visible Claude UI",
            target_session_id="claude-thread-1",
        )

        worker_script = root / "claude_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        osascript_log = root / "claude_osascript.log"
        osascript_script = root / "fake_claude_osascript.py"
        write(
            osascript_script,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    "from pathlib import Path",
                    f"Path({json.dumps(str(osascript_log))}).write_text(sys.stdin.read())",
                ]
            ),
        )
        osascript_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-REFRESH",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CLAUDE-REFRESH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "claude-thread-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_OSASCRIPT_BIN": str(osascript_script),
            },
            "dispatch",
            "--session",
            "SESSION-CLAUDE-REFRESH",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("claude_reload_page", action["ui_refresh_result"]["method"])
        self.assertEqual(0, action["ui_refresh_result"]["returncode"])
        osascript_text = osascript_log.read_text()
        self.assertIn('tell application "Claude" to activate', osascript_text)
        self.assertIn('Reload This Page', osascript_text)

    def test_human_relay_downgrades_to_prompt_without_runtime_hook(self):
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
        worker_script = root / "human_relay_worker.py"
        output_file = root / "human_relay_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "from pathlib import Path",
                    "import sys",
                    "Path(sys.argv[1]).write_text('should-not-run')",
                ]
            ),
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
            "relay",
        )
        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RELAY")

        self.assertEqual(1, len(dispatch_result["actions"]))
        action = dispatch_result["actions"][0]
        self.assertEqual("relay_prompt", action["effective_action"])
        self.assertFalse(output_file.exists())
        prompt_path = root / action["relay_result"]["prompt_path"]
        self.assertTrue(prompt_path.exists())
        prompt_text = prompt_path.read_text()
        self.assertIn("Please check your inbox now and execute the latest task.", prompt_text)
        self.assertIn("session_autobridge.py", str(SCRIPT_PATH))

    def test_human_relay_uses_runtime_trigger_when_runtime_hook_exists(self):
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
            chat_id="CHAT-RELAYRUNTIME",
            project_id="amiga",
            title="Relay runtime hook",
            target_session_id="cdx2-runtime-1",
        )
        worker_script = root / "human_relay_runtime_worker.py"
        output_file = root / "human_relay_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-RELAY-RUNTIME",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-RELAYRUNTIME",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "cdx2-runtime-1",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RELAY-RUNTIME")
        self.assertEqual(1, len(dispatch_result["actions"]))
        action = dispatch_result["actions"][0]
        self.assertEqual("runtime_trigger", action["effective_action"])
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertTrue(output_file.exists())

    def test_explicit_target_session_id_routes_only_to_matching_session(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="codex",
            chat_id="CHAT-TARGET123",
            project_id="amiga",
            title="Targeted wake",
            sender_session_id="claude-session-a",
            target_session_id="codex-runtime-b",
            sender_agent_id="claude",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-A",
            "--agent",
            "codex",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TARGET123",
            "--mode",
            "notify",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-runtime-a",
            "--runtime-session-source",
            "first_read",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-B",
            "--agent",
            "codex",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TARGET123",
            "--mode",
            "notify",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-runtime-b",
            "--runtime-session-source",
            "first_read",
            "--supersedes-session",
            "SESSION-CODEX-A",
        )

        dispatch_a = self.run_cli(root, "dispatch", "--session", "SESSION-CODEX-A")
        dispatch_b = self.run_cli(root, "dispatch", "--session", "SESSION-CODEX-B")

        self.assertEqual([], dispatch_a["actions"])
        self.assertEqual(1, len(dispatch_b["actions"]))
        self.assertEqual("claude-session-a", dispatch_b["actions"][0]["sender_session_id"])
        self.assertEqual("codex-runtime-b", dispatch_b["actions"][0]["target_session_id"])

    def test_deliver_and_inbox_surface_session_protocol_fields(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-22_protocol-test__CHAT-PROTO1",
            chat_id="CHAT-PROTO1",
            project_id="amiga",
        )
        subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PROTO1",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Protocol message",
                "--sender-session-id",
                "codex-app-session-1",
                "--target-session-id",
                "claude-app-session-9",
                "--supersedes-session-id",
                "codex-app-session-0",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Session-aware protocol body.",
            capture_output=True,
            check=True,
        )

        delivered_file = chat_dir / "2026-04-22T00-00-00_to-claude_test.md"
        if not delivered_file.exists():
            delivered_candidates = sorted(chat_dir.glob("*_to-claude_*.md"))
            self.assertTrue(delivered_candidates)
            delivered_file = delivered_candidates[-1]

        delivered_text = delivered_file.read_text()
        self.assertIn("sender_session_id: codex-app-session-1", delivered_text)
        self.assertIn("target_session_id: claude-app-session-9", delivered_text)
        self.assertIn("supersedes_session_id: codex-app-session-0", delivered_text)

        inbox_result = subprocess.run(
            [
                sys.executable,
                str(INBOX_SCRIPT),
                "--me",
                "claude",
                "--peek",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Sender Session: codex-app-session-1", inbox_result.stdout)
        self.assertIn("Target Session: claude-app-session-9", inbox_result.stdout)
        self.assertIn("Supersedes: codex-app-session-0", inbox_result.stdout)

    def test_discover_runtime_for_codex_and_publish_from_inbox(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="codex",
            chat_id="CHAT-PUBLISH1",
            project_id="amiga",
            title="Publish runtime",
        )

        codex_home = root / ".codex"
        write(codex_home / "session_index.jsonl", json.dumps({
            "id": "codex-thread-123",
            "thread_name": "Autobridge runtime publish",
            "updated_at": "2026-04-22T20:00:00Z",
        }) + "\n")

        discovered = self.run_cli_with_env(
            root,
            {"CODEX_HOME": str(codex_home)},
            "discover-runtime",
            "--runtime-family",
            "codex_app",
        )
        self.assertEqual("codex-thread-123", discovered["session_id"])

        inbox_result = subprocess.run(
            [
                sys.executable,
                str(INBOX_SCRIPT),
                "--me",
                "codex",
                "--peek",
                "--project",
                "amiga",
                "--publish-session",
                "--session",
                "SESSION-CODEX-PUBLISH",
                "--runtime-family",
                "codex_app",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            check=True,
        )
        self.assertIn("[session] published codex_app codex-thread-123", inbox_result.stdout)

        published_session = self.run_cli_with_env(
            root,
            {"CODEX_HOME": str(codex_home)},
            "show",
            "--session",
            "SESSION-CODEX-PUBLISH",
        )
        self.assertEqual("codex-thread-123", published_session["runtime"]["session_id"])
        self.assertEqual("codex_app", published_session["runtime"]["family"])

        binding = self.run_cli_with_env(
            root,
            {"CODEX_HOME": str(codex_home)},
            "show-binding",
            "--project",
            "amiga",
            "--chat",
            "CHAT-PUBLISH1",
            "--agent",
            "codex",
        )
        self.assertEqual("codex-thread-123", binding["runtime_session_id"])
        self.assertEqual("SESSION-CODEX-PUBLISH", binding["session_id"])

    def test_discover_runtime_for_claude_project_index(self):
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        project_slug = str(project_path.resolve()).replace("/", "-")
        sessions_index = claude_home / "projects" / project_slug / "sessions-index.json"
        write_json(
            sessions_index,
            {
                "version": 1,
                "entries": [
                    {
                        "sessionId": "claude-session-456",
                        "fullPath": str(claude_home / "projects" / project_slug / "claude-session-456.jsonl"),
                        "fileMtime": 1771371735466,
                        "created": "2026-04-22T20:00:00Z",
                        "modified": "2026-04-22T21:00:00Z",
                        "projectPath": str(project_path.resolve()),
                    }
                ],
            },
        )

        discovered = self.run_cli_with_env(
            root,
            {"CLAUDE_HOME": str(claude_home)},
            "discover-runtime",
            "--runtime-family",
            "claude_app",
            "--project-path",
            str(project_path),
        )
        self.assertEqual("claude-session-456", discovered["session_id"])

    def test_discover_runtime_for_gemini_chat_file(self):
        root = self.make_workspace()
        gemini_home = root / ".gemini"
        chat_file = gemini_home / "tmp" / "pixexid" / "chats" / "session-2026-04-22T20-11-test.json"
        write_json(
            chat_file,
            {
                "sessionId": "gemini-session-789",
                "startTime": "2026-04-22T20:11:00Z",
                "lastUpdated": "2026-04-22T20:30:00Z",
            },
        )

        discovered = self.run_cli_with_env(
            root,
            {"GEMINI_HOME": str(gemini_home)},
            "discover-runtime",
            "--runtime-family",
            "gemini_cli",
        )
        self.assertEqual("gemini-session-789", discovered["session_id"])

    def test_deliver_uses_canonical_binding_for_target_session_id(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-23_binding-test__CHAT-BIND1",
            chat_id="CHAT-BIND1",
            project_id="amiga",
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-BOUND",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-BIND1",
            "--mode",
            "notify",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "claude-bound-session-42",
            "--runtime-session-source",
            "first_read",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-BIND1",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Binding targeted message",
                "--sender-session-id",
                "codex-session-5",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the canonical binding.",
            capture_output=True,
            check=True,
        )
        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertEqual("claude-bound-session-42", result_payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-claude_*.md"))
        self.assertTrue(delivered_candidates)
        delivered_text = delivered_candidates[-1].read_text()
        self.assertIn("target_session_id: claude-bound-session-42", delivered_text)

    def test_deliver_suppresses_manual_relay_when_autobridge_target_is_dispatchable(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": True},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-25_dispatchable-target__CHAT-DISPATCH1",
            chat_id="CHAT-DISPATCH1",
            project_id="amiga",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CDX2-DISPATCHABLE",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-DISPATCH1",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "cdx2-thread-1",
            "--runtime-session-source",
            "first_read",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-DISPATCH1",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "amiga",
                "--title",
                "Dispatchable receiver",
                "--sender-session-id",
                "codex-thread-1",
                "--target-session-id",
                "cdx2-thread-1",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use autobridge.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.strip())
        self.assertTrue(result_payload["autobridge_ready"])
        self.assertEqual("SESSION-CDX2-DISPATCHABLE", result_payload["autobridge_session_id"])
        self.assertFalse(result_payload["relay_required"])
        self.assertNotIn("RELAY REQUIRED FOR OPERATOR", deliver_result.stdout)

    def test_deliver_suppresses_manual_relay_for_untargeted_dispatchable_session(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": True},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-25_dispatchable-broadcast__CHAT-DISPATCH2",
            chat_id="CHAT-DISPATCH2",
            project_id="amiga",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CDX2-BROADCAST",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-DISPATCH2",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, "-c", "import sys; sys.exit(0)"]),
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-DISPATCH2",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "amiga",
                "--title",
                "Untargeted dispatchable receiver",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the chat-scoped autobridge session.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.strip())
        self.assertTrue(result_payload["autobridge_ready"])
        self.assertEqual("SESSION-CDX2-BROADCAST", result_payload["autobridge_session_id"])
        self.assertFalse(result_payload["relay_required"])
        self.assertIsNone(result_payload["resolved_target_session_id"])
        self.assertNotIn("RELAY REQUIRED FOR OPERATOR", deliver_result.stdout)

    def test_deliver_uses_thread_pair_for_reverse_reply_routing(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-24_pairing-test__CHAT-PAIR1",
            chat_id="CHAT-PAIR1",
            project_id="amiga",
        )

        subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PAIR1",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "amiga",
                "--title",
                "Seed receiver thread",
                "--sender-session-id",
                "codex-thread-1",
                "--target-session-id",
                "cdx2-thread-9",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Create the paired thread.",
            capture_output=True,
            check=True,
        )

        reverse_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PAIR1",
                "--from",
                "cdx2",
                "--to",
                "codex",
                "--project",
                "amiga",
                "--title",
                "Reply to sender thread",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Reply on the paired thread.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(reverse_result.stdout.split("\n\n", 1)[0])
        self.assertEqual("codex-thread-1", result_payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-codex_*.md"))
        self.assertTrue(delivered_candidates)
        delivered_text = delivered_candidates[-1].read_text()
        self.assertIn("sender_session_id: cdx2-thread-9", delivered_text)
        self.assertIn("target_session_id: codex-thread-1", delivered_text)

        pair_path = root / "State" / "session_autobridge" / "thread_pairs" / "amiga" / "CHAT-PAIR1" / "cdx2__codex.json"
        pair = json.loads(pair_path.read_text())
        self.assertEqual("codex-thread-1", pair["sessions"]["codex"])
        self.assertEqual("cdx2-thread-9", pair["sessions"]["cdx2"])

        note_candidates = sorted(chat_dir.glob("*_note-cdx2_*.md"))
        self.assertTrue(note_candidates)
        note_text = note_candidates[-1].read_text()
        self.assertIn("summary_event: sent", note_text)
        self.assertIn("sender_session_id: cdx2-thread-9", note_text)
        self.assertIn("target_session_id: codex-thread-1", note_text)
        self.assertIn("cdx2 sent `Reply to sender thread` to codex.", note_text)

    def test_deliver_updates_thread_pair_when_sender_session_changes(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-24_pairing-update__CHAT-PAIR2",
            chat_id="CHAT-PAIR2",
            project_id="amiga",
        )

        for sender_session_id in ("codex-thread-1", "codex-thread-2"):
            subprocess.run(
                [
                    sys.executable,
                    str(DELIVER_SCRIPT),
                    "--chat",
                    "CHAT-PAIR2",
                    "--from",
                    "codex",
                    "--to",
                    "cdx2",
                    "--project",
                    "amiga",
                    "--title",
                    f"Seed {sender_session_id}",
                    "--sender-session-id",
                    sender_session_id,
                    "--target-session-id",
                    "cdx2-thread-9",
                    "--body-file",
                    "-",
                ],
                cwd=root,
                text=True,
                input=f"Use {sender_session_id}.",
                capture_output=True,
                check=True,
            )

        reverse_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PAIR2",
                "--from",
                "cdx2",
                "--to",
                "codex",
                "--project",
                "amiga",
                "--title",
                "Reply after sender moved sessions",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Reply on the latest sender thread.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(reverse_result.stdout.split("\n\n", 1)[0])
        self.assertEqual("codex-thread-2", result_payload["resolved_target_session_id"])

        pair_path = root / "State" / "session_autobridge" / "thread_pairs" / "amiga" / "CHAT-PAIR2" / "cdx2__codex.json"
        pair = json.loads(pair_path.read_text())
        self.assertEqual("codex-thread-2", pair["sessions"]["codex"])
        self.assertEqual("cdx2-thread-9", pair["sessions"]["cdx2"])

    def test_dispatch_writes_operator_picked_up_note(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-24_operator-summary__CHAT-SUM1",
            chat_id="CHAT-SUM1",
            project_id="amiga",
        )
        worker_script = root / "operator_summary_worker.py"
        output_file = root / "operator_summary_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )
        self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-SUM1",
            project_id="amiga",
            title="Operator summary pickup",
            sender_session_id="codex-thread-1",
            target_session_id="gemini-thread-1",
            sender_agent_id="codex",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-SUMMARY",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-SUM1",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-thread-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        dispatch = self.run_cli(root, "dispatch", "--session", "SESSION-SUMMARY")
        self.assertEqual(1, dispatch["matched_messages"])

        note_candidates = sorted(chat_dir.glob("*_note-gemini_*.md"))
        self.assertTrue(note_candidates)
        note_text = note_candidates[-1].read_text()
        self.assertIn("summary_event: picked_up", note_text)
        self.assertIn("runtime_session_id: gemini-thread-1", note_text)
        self.assertIn("gemini picked up `Operator summary pickup`.", note_text)

    def test_watch_inbox_autobridges_runtime_trigger_and_marks_message_read(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCH123",
            project_id="amiga",
            title="Watcher autobridge",
            sender_session_id="codex-live-1",
            target_session_id="gemini-runtime-1",
            sender_agent_id="codex",
        )
        worker_script = root / "watcher_runtime_worker.py"
        output_file = root / "watcher_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-WATCHER",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-WATCH123",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        self.assertTrue(any(event["event"] == "new_message" for event in watcher_events))
        self.assertTrue(any(event["event"] == "autobridge_dispatch" for event in watcher_events))
        self.assertTrue(any(event["event"] == "autobridge_consumed" and event["message_path"] == message_rel for event in watcher_events))

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertIn(message_rel, inbox["read"])

        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("Watcher autobridge", runtime_payload["message"]["title"])
        session_payload = self.run_cli(root, "show", "--session", "SESSION-WATCHER")
        self.assertIn(message_rel, session_payload["processed_messages"])

    def test_watch_inbox_consumes_unread_in_only_one_overlapping_session(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCHOVERLAP",
            project_id="amiga",
            title="Watcher overlap",
            sender_session_id="codex-live-1",
            sender_agent_id="codex",
        )
        worker_script = root / "watcher_runtime_overlap.py"
        output_a = root / "watcher_runtime_overlap_a.json"
        output_b = root / "watcher_runtime_overlap_b.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        for session_id, output_file in (
            ("SESSION-WATCHER-A", output_a),
            ("SESSION-WATCHER-B", output_b),
        ):
            self.run_cli(
                root,
                "register",
                "--session",
                session_id,
                "--agent",
                "gemini",
                "--project",
                "amiga",
                "--chat",
                "CHAT-WATCHOVERLAP",
                "--mode",
                "auto-read",
                "--wake-strategy",
                "runtime_trigger",
                "--runtime-family",
                "gemini_cli",
                "--runtime-session-id",
                session_id.lower(),
                "--runtime-session-source",
                "first_read",
                "--runtime-command",
                json.dumps([sys.executable, str(worker_script), str(output_file)]),
            )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        consumed = [event for event in watcher_events if event["event"] == "autobridge_consumed"]
        self.assertEqual(1, len(consumed))
        self.assertEqual(message_rel, consumed[0]["message_path"])
        self.assertTrue(output_a.exists() ^ output_b.exists())

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertIn(message_rel, inbox["read"])

        session_a = self.run_cli(root, "show", "--session", "SESSION-WATCHER-A")
        session_b = self.run_cli(root, "show", "--session", "SESSION-WATCHER-B")
        processed_count = sum(
            message_rel in session["processed_messages"]
            for session in (session_a, session_b)
        )
        self.assertEqual(1, processed_count)

    def test_watch_inbox_keeps_message_unread_when_runtime_trigger_fails(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCHFAIL",
            project_id="amiga",
            title="Watcher failure",
            target_session_id="gemini-runtime-fail",
        )
        worker_script = root / "watcher_runtime_fail.py"
        write(
            worker_script,
            "\n".join(
                [
                    "import sys",
                    "sys.exit(7)",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-WATCHER-FAIL",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-WATCHFAIL",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-fail",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        self.assertTrue(any(event["event"] == "autobridge_failed" and event["message_path"] == message_rel for event in watcher_events))

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertIn(message_rel, inbox["unread"])
        self.assertEqual([], inbox["read"])

        session_payload = self.run_cli(root, "show", "--session", "SESSION-WATCHER-FAIL")
        self.assertEqual([], session_payload["processed_messages"])

    def test_watch_inbox_retries_deferred_message_without_new_message(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCHRETRY",
            project_id="amiga",
            title="Watcher retry",
            target_session_id="gemini-runtime-retry",
        )
        worker_script = root / "watcher_runtime_retry.py"
        output_file = root / "watcher_runtime_retry_result.json"
        marker_file = root / "watcher_runtime_retry_busy"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "output_file = Path(sys.argv[1])",
                    "marker_file = Path(sys.argv[2])",
                    "if not marker_file.exists():",
                    "    marker_file.write_text('busy')",
                    "    sys.exit(7)",
                    "output_file.write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-WATCHER-RETRY",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-WATCHRETRY",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-retry",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file), str(marker_file)]),
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "2",
                "--poll-seconds",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        new_message_events = [event for event in watcher_events if event["event"] == "new_message"]
        self.assertEqual(1, len(new_message_events))
        self.assertTrue(
            any(event["event"] == "autobridge_failed" and event["message_path"] == message_rel for event in watcher_events)
        )
        self.assertTrue(
            any(event["event"] == "autobridge_consumed" and event["message_path"] == message_rel for event in watcher_events)
        )

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertIn(message_rel, inbox["read"])

        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("Watcher retry", runtime_payload["message"]["title"])
        session_payload = self.run_cli(root, "show", "--session", "SESSION-WATCHER-RETRY")
        self.assertIn(message_rel, session_payload["processed_messages"])


if __name__ == "__main__":
    unittest.main()
