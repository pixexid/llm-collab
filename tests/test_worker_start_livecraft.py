"""Fake-HTTP proof for the gated Livecraft first-start path (GH-94)."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import worker_rotate_pi as wr  # noqa: E402


NATIVE = "bfe59384-f808-4885-8aed-604774d728fc"


class FakeLivecraft:
    def __init__(self, chronology, *, drift=False):
        self.chronology = chronology
        self.drift = drift
        self.marker = None
        self.snapshot_calls = 0
        self.closed = []

    def create_session(self, cwd):
        self.chronology.append(("http", "create"))
        return {"id": NATIVE, "cwd": cwd}

    def set_model(self, session_id, provider, model):
        self.chronology.append(("http", "set_model"))

    def set_thinking(self, session_id, thinking):
        self.chronology.append(("http", "set_thinking"))

    def get_state(self, session_id):
        self.snapshot_calls += 1
        self.chronology.append(("http", "snapshot_state"))
        provider = "other" if self.drift and self.snapshot_calls > 1 else "zai"
        return {
            "native": NATIVE,
            "session_file": "/pi/sessions/livecraft.jsonl",
            "provider": provider,
            "model_id": "glm-5.2",
            "thinking": "max",
            "cwd": "/repo",
        }

    def prompt(self, session_id, message):
        self.marker = re.search(r"BOOTSTRAP_READY_\S+", message).group(0)
        self.chronology.append(("http", "prompt"))

    def last_assistant_text(self, session_id):
        self.chronology.append(("http", "snapshot_messages"))
        return self.marker

    def close_session(self, session_id):
        self.closed.append(session_id)
        self.chronology.append(("http", "abort"))


class FakeAutobridge:
    def __init__(self, chronology):
        self.chronology = chronology
        self.calls = []
        self.session = None

    def __call__(self, args):
        self.calls.append(args)
        self.chronology.append(("autobridge", args[0]))
        if args[0] == "register":
            self.session = args[args.index("--session") + 1]
            return 0, json.dumps({"ok": True})
        if args[0] == "show-binding":
            return 0, json.dumps({"session_id": self.session, "status": "active", "binding_generation": 4})
        raise AssertionError(args)


def _cfg(**over):
    values = dict(
        livecraft_backend_url=wr.DEFAULT_LIVECRAFT_BACKEND_URL,
        agent="glmpi", project="llm-collab", chat="CHAT-NEWPROJ", repo_target="app",
        provider="zai", model="glm-5.2", thinking="max", runtime_home="/pi",
        pilot_scope="llm-collab/glmpi", disposable=True, bootstrap_timeout=5.0,
        poll_interval=0.0, json_output=True,
    )
    values.update(over)
    return argparse.Namespace(**values)


class StartLivecraftTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        import _helpers

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "collab.config.json").write_text(json.dumps({
            "workspace_name": "ws", "projects_root": str(root), "workspace_id": "ws", "schema_version": 2,
        }))
        self.old = (_helpers.CONFIG_FILE, _helpers.CHATS_DIR, _helpers.AGENTS_FILE,
                    _helpers._config_cache, _helpers._agents_cache)
        _helpers.CONFIG_FILE = root / "collab.config.json"
        _helpers.CHATS_DIR = root / "Chats"
        _helpers.CHATS_DIR.mkdir()
        chat_dir = _helpers.CHATS_DIR / "2026-08-03_test__CHAT-NEWPROJ"
        chat_dir.mkdir()
        (chat_dir / "meta.json").write_text(json.dumps({"chat_id": "CHAT-NEWPROJ", "project_id": "llm-collab"}))
        _helpers.AGENTS_FILE = root / "agents.json"
        _helpers.AGENTS_FILE.write_text(json.dumps({"agents": [{
            "id": "glmpi", "activation": {"type": "cli_session", "watcher_enabled": True},
        }]}))
        _helpers._config_cache = None
        _helpers._agents_cache = None
        self.addCleanup(self.restore)

    def restore(self):
        import _helpers

        (_helpers.CONFIG_FILE, _helpers.CHATS_DIR, _helpers.AGENTS_FILE,
         _helpers._config_cache, _helpers._agents_cache) = self.old
        self.tmp.cleanup()

    def _run(self, cfg=None, *, livecraft=None, run=None):
        chronology = []
        livecraft = livecraft or FakeLivecraft(chronology)
        run = run or FakeAutobridge(chronology)
        result = wr.start_livecraft(
            cfg or _cfg(), livecraft=livecraft, run_autobridge=run,
            resolve_cwd=lambda project, repo: "/repo", gate_check=lambda _cfg: None,
            sleep=lambda _seconds: None,
            clock=(lambda counter=[0.0]: (counter.__setitem__(0, counter[0] + 1), counter[0])[1]),
        )
        return result, chronology, livecraft, run

    def test_client_uses_livecraft_rpc_and_snapshot_shapes(self):
        calls = []

        def request(method, path, body):
            calls.append((method, path, body))
            if path == "/api/sessions":
                return 201, {"id": NATIVE, "cwd": "/repo"}
            if path.endswith("/snapshot"):
                return 200, {
                    "state": {
                        "sessionId": NATIVE, "sessionFile": "/pi/livecraft.jsonl",
                        "model": {"provider": "zai", "id": "glm-5.2"},
                        "thinkingLevel": "max",
                    },
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "READY"}]}],
                }
            return 200, {"success": True}

        client = wr.Livecraft("http://127.0.0.1:43121", request=request)
        client.create_session("/repo")
        client.set_model(NATIVE, "zai", "glm-5.2")
        client.set_thinking(NATIVE, "max")
        self.assertEqual(client.get_state(NATIVE)["session_file"], "/pi/livecraft.jsonl")
        client.prompt(NATIVE, "bootstrap")
        self.assertEqual(client.last_assistant_text(NATIVE), "READY")
        self.assertEqual(calls[1][2], {"type": "set_model", "provider": "zai", "modelId": "glm-5.2"})
        self.assertEqual(calls[2][2], {"type": "set_thinking_level", "level": "max"})
        self.assertEqual(calls[4][2], {"type": "prompt", "message": "bootstrap"})

    def test_default_gate_refuses_without_network_mutation(self):
        cfg = _cfg(disposable=False)
        client = FakeLivecraft([])
        with self.assertRaisesRegex(wr.RotateError, "--disposable"):
            wr.start_livecraft(
                cfg, livecraft=client, run_autobridge=FakeAutobridge([]),
                resolve_cwd=lambda _project, _repo: "/repo",
            )
        self.assertEqual(client.snapshot_calls, 0)

    def test_happy_path_registers_after_marker_with_lowercase_native_suffix(self):
        result, chronology, _client, run = self._run()
        self.assertEqual(result["session"], "SESSION-LIVECRAFT-GLMPI-NEWPROJ-bfe59384")
        self.assertTrue(result["verified"])
        self.assertEqual([call[0] for call in run.calls], ["register", "show-binding"])
        marker_at = next(i for i, item in enumerate(chronology) if item == ("http", "snapshot_messages"))
        register_at = next(i for i, item in enumerate(chronology) if item == ("autobridge", "register"))
        self.assertLess(marker_at, register_at)
        register = run.calls[0]
        self.assertNotIn("--supersedes-session", register)
        self.assertEqual(register[register.index("--endpoint-id") + 1], wr.LIVECRAFT_ENDPOINT)

    def test_final_fingerprint_drift_aborts_and_never_registers(self):
        chronology = []
        client = FakeLivecraft(chronology, drift=True)
        run = FakeAutobridge(chronology)
        with self.assertRaisesRegex(wr.RotateError, "drifted"):
            self._run(livecraft=client, run=run)
        self.assertEqual(run.calls, [])
        self.assertEqual(client.closed, [NATIVE])

    def test_noncanonical_chat_is_refused_before_native_create(self):
        cfg = _cfg(chat="NEWPROJ")
        client = FakeLivecraft([])
        with self.assertRaisesRegex(wr.RotateError, "canonical --chat"):
            wr.start_livecraft(
                cfg, livecraft=client, run_autobridge=FakeAutobridge([]),
                resolve_cwd=lambda _project, _repo: "/repo", gate_check=lambda _cfg: None,
            )
        self.assertEqual(client.snapshot_calls, 0)


if __name__ == "__main__":
    unittest.main()
