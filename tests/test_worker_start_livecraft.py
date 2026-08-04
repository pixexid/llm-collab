"""Fake-HTTP proof for the gated Livecraft first-start path (GH-94)."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import worker_livecraft_pi as wr  # noqa: E402


NATIVE = "bfe59384-f808-4885-8aed-604774d728fc"


class FakeLivecraft:
    def __init__(self, chronology, *, drift=False, session_file="/pi/sessions/livecraft.jsonl",
                 abort_error=None):
        self.chronology = chronology
        self.drift = drift
        self.session_file = session_file
        self.abort_error = abort_error
        self.marker = None
        self.snapshot_calls = 0
        self.closed = []
        self.prompt_message = None

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
            "session_file": self.session_file,
            "provider": provider,
            "model_id": "glm-5.2",
            "thinking": "max",
            "cwd": "/repo",
        }

    def prompt(self, session_id, message):
        self.prompt_message = message
        self.marker = re.search(r"BOOTSTRAP_READY(?:_\S+)?", message).group(0)
        self.chronology.append(("http", "prompt"))

    def last_assistant_text(self, session_id):
        self.chronology.append(("http", "snapshot_messages"))
        return self.marker

    def close_session(self, session_id):
        self.closed.append(session_id)
        self.chronology.append(("http", "abort"))
        if self.abort_error:
            raise self.abort_error


class FakeAutobridge:
    def __init__(self, chronology, *, register_rc=0, show_binding_rc=0):
        self.chronology = chronology
        self.calls = []
        self.session = None
        self.register_rc = register_rc
        self.show_binding_rc = show_binding_rc

    def __call__(self, args):
        self.calls.append(args)
        self.chronology.append(("autobridge", args[0]))
        if args[0] == "register":
            self.session = args[args.index("--session") + 1]
            return self.register_rc, json.dumps({"ok": self.register_rc == 0})
        if args[0] == "show-binding":
            if self.show_binding_rc:
                return self.show_binding_rc, ""
            return 0, json.dumps({"session_id": self.session, "status": "active", "binding_generation": 4})
        if args[0] == "deactivate-pi":
            return 0, json.dumps({"deactivated_sessions": [self.session]})
        raise AssertionError(args)


def _cfg(**over):
    values = dict(
        livecraft_backend_url=wr.DEFAULT_LIVECRAFT_BACKEND_URL,
        agent="glmpi", project="llm-collab", chat="CHAT-NEWPROJ", repo_target="app",
        provider="zai", model="glm-5.2", thinking="max", runtime_home="/pi",
        starter_agent="codex", starter_session_id="codex-native",
        supersedes_session=None,
        pilot_scope="llm-collab/glmpi", disposable=True, production=False, bootstrap_timeout=5.0,
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
                    _helpers.PROJECTS_FILE, _helpers._config_cache, _helpers._agents_cache,
                    _helpers._projects_cache)
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
        _helpers.PROJECTS_FILE = root / "projects.json"
        _helpers.PROJECTS_FILE.write_text(json.dumps({
            "projects": [{"id": "llm-collab", "repos": {"app": "."}}],
        }))
        _helpers._config_cache = None
        _helpers._agents_cache = None
        _helpers._projects_cache = None
        self.addCleanup(self.restore)

    def restore(self):
        import _helpers

        (_helpers.CONFIG_FILE, _helpers.CHATS_DIR, _helpers.AGENTS_FILE,
         _helpers.PROJECTS_FILE, _helpers._config_cache, _helpers._agents_cache,
         _helpers._projects_cache) = self.old
        self.tmp.cleanup()

    def _run(self, cfg=None, *, livecraft=None, run=None, health_check=None,
             resolve_cwd=None):
        chronology = []
        livecraft = livecraft or FakeLivecraft(chronology)
        run = run or FakeAutobridge(chronology)
        health_check = health_check or (
            lambda _url, **_kwargs: chronology.append(("health", "ready"))
        )
        with mock.patch.object(
            wr, "_await_bootstrap_handshake",
            return_value={"path": "Chats/handshake.md", "body": {}},
        ):
            result = wr.start_livecraft(
                cfg or _cfg(), livecraft=livecraft, run_autobridge=run,
                resolve_cwd=resolve_cwd or (lambda project, repo: "/repo"),
                gate_check=lambda _cfg: None,
                sleep=lambda _seconds: None,
                clock=(lambda counter=[0.0]: (counter.__setitem__(0, counter[0] + 1), counter[0])[1]),
                health_check=health_check,
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

    def test_livecraft_close_session_propagates_abort_failure(self):
        def request(method, path, body):
            if path == "/api/sessions":
                return 201, {"id": NATIVE, "cwd": "/repo"}
            return 503, {"error": "backend unavailable"}

        client = wr.Livecraft("http://127.0.0.1:43121", request=request)
        client.create_session("/repo")

        with self.assertRaisesRegex(wr.RotateError, "Livecraft RPC abort failed \(HTTP 503\)"):
            client.close_session(NATIVE)

    def test_http_body_is_bounded_before_json_parse(self):
        class Body:
            def __init__(self):
                self.remaining = b"x" * (wr.HTTP_RESPONSE_LIMIT + 1)

            def read(self, size):
                chunk, self.remaining = self.remaining[:size], self.remaining[size:]
                return chunk

        with self.assertRaisesRegex(wr.RotateError, "byte limit"):
            wr._read_http_body(Body(), wr.time.monotonic() + 1)

    def test_http_body_deadline_is_checked_before_read(self):
        class Body:
            def read(self, _size):
                raise AssertionError("read must not start after the deadline")

        with self.assertRaisesRegex(wr.RotateError, "deadline exceeded"):
            wr._read_http_body(Body(), wr.time.monotonic() - 1)

    def test_default_gate_refuses_without_network_mutation(self):
        cfg = _cfg(disposable=False)
        client = FakeLivecraft([])
        with self.assertRaisesRegex(wr.RotateError, "--disposable"):
            wr.start_livecraft(
                cfg, livecraft=client, run_autobridge=FakeAutobridge([]),
                resolve_cwd=lambda _project, _repo: "/repo",
            )
        self.assertEqual(client.snapshot_calls, 0)

    def test_production_gate_requires_current_project_authority(self):
        cfg = _cfg(production=True, disposable=False)
        with mock.patch.object(wr, "_require_current_project_authority") as authority:
            wr.require_livecraft_gate(cfg, environ={})
        authority.assert_called_once_with("llm-collab", mode="Livecraft production")

    def test_single_repo_is_defaulted_when_repo_target_is_omitted(self):
        seen = []
        result, _chronology, _client, _run = self._run(
            _cfg(repo_target=None),
            resolve_cwd=lambda _project, repo: seen.append(repo) or "/repo",
        )
        self.assertTrue(result["verified"])
        self.assertEqual(["app"], seen)

    def test_amiga_multi_repo_requires_target_and_lists_valid_keys(self):
        import _helpers

        _helpers.PROJECTS_FILE.write_text(json.dumps({
            "projects": [{"id": "amiga", "repos": {"app": ".", "docs": "docs"}}],
        }))
        _helpers._projects_cache = None
        with self.assertRaisesRegex(wr.RotateError, r"valid keys: app, docs"):
            wr._resolve_repo_target("amiga", None)

    def test_missing_repo_target_on_multi_repo_project_lists_valid_keys(self):
        import _helpers

        _helpers.PROJECTS_FILE.write_text(json.dumps({
            "projects": [{"id": "llm-collab", "repos": {"app": ".", "docs": "docs"}}],
        }))
        _helpers._projects_cache = None
        with self.assertRaisesRegex(wr.RotateError, r"valid keys: app, docs"):
            wr.start_livecraft(
                _cfg(repo_target=None), livecraft=FakeLivecraft([]),
                run_autobridge=FakeAutobridge([]), resolve_cwd=lambda _project, _repo: "/repo",
                gate_check=lambda _cfg: None,
            )

    def test_invalid_repo_target_lists_valid_keys(self):
        with self.assertRaisesRegex(wr.RotateError, r"valid keys: app"):
            wr.start_livecraft(
                _cfg(repo_target="main"), livecraft=FakeLivecraft([]),
                run_autobridge=FakeAutobridge([]), resolve_cwd=lambda _project, _repo: "/repo",
                gate_check=lambda _cfg: None,
            )

    def test_missing_starter_binding_prints_registration_command(self):
        with mock.patch.object(wr, "_load_optional_binding", return_value=None), \
             self.assertRaisesRegex(wr.RotateError, "session_autobridge.py register") as raised:
            wr._resolve_starter(
                starter_agent="claude", starter_session_id=None, project="llm-collab",
                chat="CHAT-NEWPROJ", repo_target="app", require_active_binding=True,
            )
        message = str(raised.exception)
        self.assertIn("--agent claude", message)
        self.assertIn("--project llm-collab", message)
        self.assertIn("--chat CHAT-NEWPROJ", message)
        self.assertIn("--repo-target app", message)
        self.assertIn("--runtime-session-id YOUR_RUNTIME_SESSION_ID", message)

    def test_starter_registration_command_parses_as_minimal_manual_binding(self):
        import session_autobridge

        argv = shlex.split(wr._starter_registration_command(
            starter_agent="claude", project="llm-collab", chat="CHAT-NEWPROJ",
            repo_target="app",
        ))
        parser_argv = [argv[0], *argv[argv.index("register"):]]
        with mock.patch.object(sys, "argv", parser_argv):
            parsed = session_autobridge.parse_args()
        self.assertEqual("register", parsed.command)
        self.assertEqual("manual", parsed.mode)
        self.assertEqual("active", parsed.status)
        self.assertEqual("none", parsed.wake_strategy)
        self.assertEqual("claude_app", parsed.runtime_family)
        self.assertEqual("runtime_dir", parsed.runtime_session_source)
        self.assertIsNone(parsed.runtime_home)
        self.assertNotIn("--runtime-home", argv)

    def test_happy_path_registers_after_marker_with_lowercase_native_suffix(self):
        result, chronology, _client, run = self._run()
        self.assertEqual(result["session"], "SESSION-LIVECRAFT-GLMPI-NEWPROJ-bfe59384")
        self.assertTrue(result["verified"])
        self.assertEqual(result["bootstrap_handshake"]["path"], "Chats/handshake.md")
        self.assertEqual([call[0] for call in run.calls], ["register", "show-binding"])
        marker_at = next(i for i, item in enumerate(chronology) if item == ("http", "snapshot_messages"))
        register_at = next(i for i, item in enumerate(chronology) if item == ("autobridge", "register"))
        health_at = chronology.index(("health", "ready"))
        create_at = chronology.index(("http", "create"))
        self.assertLess(health_at, create_at)
        self.assertLess(marker_at, register_at)
        register = run.calls[0]
        self.assertNotIn("--supersedes-session", register)
        self.assertEqual(register[register.index("--endpoint-id") + 1], wr.LIVECRAFT_ENDPOINT)
        runtime_command = json.loads(register[register.index("--runtime-command") + 1])
        self.assertIn("livecraft_wake.py", runtime_command[1])
        self.assertEqual(
            runtime_command[runtime_command.index("--backend-url") + 1],
            wr.DEFAULT_LIVECRAFT_BACKEND_URL,
        )

    def test_bootstrap_prompt_identifies_starter_and_reader_identity(self):
        _result, _chronology, client, _run = self._run(_cfg(starter_session_id="starter-native"))
        self.assertIn("The worker who started this session is codex", client.prompt_message)
        self.assertIn("--from glmpi --to codex", client.prompt_message)
        self.assertIn("--sender-session-id bfe59384-f808-4885-8aed-604774d728fc", client.prompt_message)
        self.assertIn("--target-session-id starter-native", client.prompt_message)
        self.assertIn("starter will arm the background wake path", client.prompt_message)
        self.assertIn('"kind":"llm_collab.pi.bootstrap.v1"', client.prompt_message)

    def test_bootstrap_command_shell_quotes_paths_and_json_values(self):
        import _helpers

        client = FakeLivecraft([], session_file="/pi/sessions/worker's file.jsonl")
        with mock.patch.object(_helpers, "RUNTIME_ROOT", Path("/runtime root")):
            _result, _chronology, client, _run = self._run(
                _cfg(starter_agent="codex's", starter_session_id="starter's native"),
                livecraft=client,
            )

        command = next(line for line in client.prompt_message.splitlines() if "deliver.py" in line)
        parts = shlex.split(command)
        self.assertEqual("/runtime root/bin/llm-collab", parts[4])
        self.assertEqual("deliver.py", parts[5])
        self.assertEqual("codex's", parts[parts.index("--to") + 1])
        self.assertEqual("starter's native", parts[parts.index("--target-session-id") + 1])
        handshake = json.loads(parts[2])
        self.assertEqual("/pi/sessions/worker's file.jsonl", handshake["worker_runtime_session_source"])

    def test_marker_accepts_successful_trailing_bootstrap_marker(self):
        client = mock.Mock()
        client.last_assistant_text.return_value = "Delivery succeeded.\n\nBOOTSTRAP_READY"
        wr._await_marker(
            client, "native", "BOOTSTRAP_READY", timeout=1, interval=0,
            sleep=lambda _seconds: None,
            clock=(lambda counter=[0.0]: (counter.__setitem__(0, counter[0] + 0.1), counter[0])[1]),
        )

    def test_cli_defaults_profile_and_starter_from_bindings(self):
        cfg = _cfg(
            provider=None, model=None, thinking=None, runtime_home=None,
            starter_agent="claude", starter_session_id=None, production=True, disposable=False,
        )
        profile = {
            "provider": "zai", "model": "glm-5.2", "thinking": "max",
            "runtime_home": "/pi", "endpoint_id": wr.LIVECRAFT_ENDPOINT,
        }
        bindings = {
            "claude": {"status": "active", "runtime_session_id": "starter-native"},
            "glmpi": {"status": "active", "session_id": "SESSION-OLD"},
        }
        with mock.patch.object(wr, "resolve_livecraft_profile", return_value=profile), \
             mock.patch.object(wr, "_load_optional_binding", side_effect=lambda _project, _chat, agent: bindings[agent]):
            result, _chronology, _client, run = self._run(cfg)
        self.assertEqual(result["profile"], {
            "provider": "zai", "model": "glm-5.2", "thinking": "max", "runtime_home": "/pi",
        })
        register = run.calls[0]
        self.assertEqual(register[register.index("--supersedes-session") + 1], "SESSION-OLD")
        runtime_command = json.loads(register[register.index("--runtime-command") + 1])
        self.assertIn("livecraft_wake.py", runtime_command[1])

    def _assert_profile_uses_latest_livecraft_record_only(self, project):
        sessions = Path(self.tmp.name) / "sessions"
        sessions.mkdir()

        def write_record(name, endpoint, generation, model):
            (sessions / f"{name}.json").write_text(json.dumps({
                "session_id": name,
                "agent_id": "glmpi",
                "project_id": project,
                "endpoint_id": endpoint,
                "binding_generation": generation,
                "runtime": {"family": "pi", "home": "/pi"},
                "pi_fingerprint": {
                    "provider": "zai", "model_id": model, "thinking_level": "max",
                },
            }))

        write_record("SESSION-LEGACY", "endpoint_legacy_pi", 99, "wrong-model")
        write_record("SESSION-LIVECRAFT-OLD", wr.LIVECRAFT_ENDPOINT, 3, "glm-5.1")
        write_record("SESSION-LIVECRAFT-NEW", wr.LIVECRAFT_ENDPOINT, 4, "glm-5.2")

        self.assertEqual(
            {
                "endpoint_id": wr.LIVECRAFT_ENDPOINT,
                "runtime_home": "/pi",
                "wake_strategy": "runtime_trigger",
                "provider": "zai",
                "model": "glm-5.2",
                "thinking": "max",
            },
            wr.resolve_livecraft_profile("glmpi", project, sessions_dir=sessions),
        )

    def test_profile_uses_latest_livecraft_record_only(self):
        self._assert_profile_uses_latest_livecraft_record_only("llm-collab")

    def test_profile_uses_latest_livecraft_record_only_for_amiga(self):
        self._assert_profile_uses_latest_livecraft_record_only("amiga")

    def test_rebind_passes_explicit_predecessor(self):
        _result, _chronology, _client, run = self._run(_cfg(supersedes_session="SESSION-OLD"))
        register = run.calls[0]
        self.assertEqual(
            register[register.index("--supersedes-session") + 1],
            "SESSION-OLD",
        )

    def test_bootstrap_handshake_is_exact_and_acknowledged(self):
        expected = {
            "kind": wr.BOOTSTRAP_HANDSHAKE_KIND,
            "handshake_id": "handshake-1",
            "starter_agent": "codex",
            "starter_runtime_session_id": "starter-native",
            "worker_agent": "glmpi",
            "worker_runtime_family": "pi",
            "worker_native_session_id": NATIVE,
            "worker_runtime_session_source": "/pi/sessions/livecraft.jsonl",
            "project_id": "llm-collab",
            "chat_id": "CHAT-NEWPROJ",
            "repo_target": "app",
        }
        message = {
            "path": "Chats/handshake.md",
            "frontmatter": {
                "title": "Pi worker bootstrap handshake handshake-1",
                "from": "glmpi",
                "to": "codex",
                "sender_agent_id": "glmpi",
                "sender_session_id": NATIVE,
                "project_id": "llm-collab",
                "chat_id": "CHAT-NEWPROJ",
                "repo_targets": ["app"],
                "target_session_id": "starter-native",
            },
            "body": json.dumps(expected, sort_keys=True),
        }
        with mock.patch.object(wr, "_starter_handshake_messages", return_value=[message]), \
             mock.patch.object(wr, "_ack_starter_handshake") as acknowledge:
            result = wr._await_bootstrap_handshake(
                starter_agent="codex", starter_session_id="starter-native", agent="glmpi",
                project="llm-collab", chat="CHAT-NEWPROJ", repo_target="app",
                native=NATIVE,
                session_source=expected["worker_runtime_session_source"],
                handshake_id="handshake-1", timeout=1, interval=0, sleep=lambda _seconds: None,
                clock=(lambda counter=[0.0]: (counter.__setitem__(0, counter[0] + 0.1), counter[0])[1]),
            )
        self.assertEqual(result["path"], "Chats/handshake.md")
        acknowledge.assert_called_once_with(
            starter_agent="codex", project="llm-collab", chat="CHAT-NEWPROJ",
            repo_target="app", packet="Chats/handshake.md",
        )

    def test_final_fingerprint_drift_aborts_and_never_registers(self):
        chronology = []
        client = FakeLivecraft(chronology, drift=True)
        run = FakeAutobridge(chronology)
        with self.assertRaisesRegex(wr.RotateError, "drifted"):
            self._run(livecraft=client, run=run)
        self.assertEqual([call[0] for call in run.calls], ["deactivate-pi"])
        self.assertEqual(client.closed, [NATIVE])

    def test_register_failure_reports_livecraft_abort_failure(self):
        client = FakeLivecraft([], abort_error=RuntimeError("abort unavailable"))
        run = FakeAutobridge([], register_rc=1)

        with self.assertRaisesRegex(wr.RotateError, "Livecraft abort failed: abort unavailable"):
            self._run(livecraft=client, run=run)

    def test_postcondition_failure_reports_livecraft_abort_failure(self):
        client = FakeLivecraft([], abort_error=RuntimeError("abort unavailable"))
        run = FakeAutobridge([], show_binding_rc=1)

        with self.assertRaisesRegex(wr.RotateError, "Livecraft abort failed: abort unavailable"):
            self._run(livecraft=client, run=run)

    def test_noncanonical_chat_is_refused_before_native_create(self):
        cfg = _cfg(chat="NEWPROJ")
        client = FakeLivecraft([])
        with self.assertRaisesRegex(wr.RotateError, "canonical --chat"):
            wr.start_livecraft(
                cfg, livecraft=client, run_autobridge=FakeAutobridge([]),
                resolve_cwd=lambda _project, _repo: "/repo", gate_check=lambda _cfg: None,
            )
        self.assertEqual(client.snapshot_calls, 0)

    def test_health_failure_is_wrapped_before_native_create(self):
        client = FakeLivecraft([])

        def fail(_url, **_kwargs):
            raise wr.LivecraftHealthError("backend health failed")

        with self.assertRaisesRegex(wr.RotateError, "backend health failed"):
            self._run(livecraft=client, health_check=fail)
        self.assertEqual(client.snapshot_calls, 0)


if __name__ == "__main__":
    unittest.main()
