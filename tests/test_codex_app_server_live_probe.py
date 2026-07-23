import ast
import json
import unittest
from pathlib import Path

from llm_collab.codex_app_server_live_probe import (
    CLIENT_CAPABILITIES,
    EXPECTED_SERVER,
    EXPECTED_SERVER_CAPABILITIES,
    PROTOCOL_VERSION,
    READ_ONLY_NOTIFICATION_METHODS,
    READ_ONLY_REQUEST_METHODS,
    CodexAppServerLiveProbeError,
    probe_live_codex_app_server,
)


MODULE = Path("llm_collab/codex_app_server_live_probe.py")


class FakeTransport:
    def __init__(self, *, version=PROTOCOL_VERSION, capabilities=None, server_name=EXPECTED_SERVER, raw=None):
        self.version = version
        self.capabilities = {"tools": {"listChanged": True}} if capabilities is None else dict(capabilities)
        self.server_name = server_name
        self.raw = raw
        self.requests = []
        self.notifications = []

    def exchange(self, frame):
        self.requests.append(frame)
        if self.raw is not None:
            return self.raw
        if frame["method"] == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {
                    "protocolVersion": self.version,
                    "serverInfo": {"name": self.server_name},
                    "capabilities": self.capabilities,
                },
            }
        if frame["method"] == "model/list":
            return {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"data": [{"id": "gpt-test", "isDefault": True}]},
            }
        raise AssertionError(f"unexpected method {frame['method']}")

    def notify(self, frame):
        self.notifications.append(frame)


class CodexAppServerLiveProbeTests(unittest.TestCase):
    def test_live_probe_uses_exact_read_only_lifecycle(self):
        fake = FakeTransport()
        result = probe_live_codex_app_server(transport=fake)

        self.assertEqual(PROTOCOL_VERSION, result.protocol_version)
        self.assertEqual(EXPECTED_SERVER, result.server_name)
        self.assertEqual(EXPECTED_SERVER_CAPABILITIES, result.capabilities)
        self.assertEqual("gpt-test", result.default_model)
        self.assertEqual(READ_ONLY_REQUEST_METHODS, result.methods)
        self.assertEqual(["initialize", "model/list"], [frame["method"] for frame in fake.requests])
        self.assertEqual(["initialized"], [frame["method"] for frame in fake.notifications])
        self.assertEqual(["llm-collab-1", "llm-collab-2"], [frame["id"] for frame in fake.requests])
        self.assertEqual({"experimentalApi": True}, fake.requests[0]["params"]["capabilities"])
        self.assertIs(CLIENT_CAPABILITIES, fake.requests[0]["params"]["capabilities"])

    def test_endpoint_is_explicit_and_default_tests_do_not_open_live_connection(self):
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "exactly one"):
            probe_live_codex_app_server()
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "exactly one"):
            probe_live_codex_app_server("ws://127.0.0.1:1", transport=FakeTransport())

    def test_handshake_mismatch_fails_before_initialized_and_model_list(self):
        fake = FakeTransport(version="2025-01-01")

        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "unsupported protocolVersion"):
            probe_live_codex_app_server(transport=fake)

        self.assertEqual(["initialize"], [frame["method"] for frame in fake.requests])
        self.assertEqual([], fake.notifications)

    def test_identity_capability_and_malformed_responses_fail_closed(self):
        cases = (
            (FakeTransport(server_name="other"), "inconsistent server identity"),
            (FakeTransport(capabilities={}), "missing capability"),
            (FakeTransport(capabilities={"tools": {}, "other": True}), "unknown capability"),
            (FakeTransport(raw='{"jsonrpc":"2.0","id":"llm-collab-1","result":{},"result":{}}'), "duplicate"),
            (FakeTransport(raw=json.dumps({"jsonrpc": "2.0", "id": "llm-collab-1", "result": {}, "extra": True})), "unknown"),
        )
        for fake, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(CodexAppServerLiveProbeError, pattern):
                probe_live_codex_app_server(transport=fake)

    def test_model_list_is_data_out_only(self):
        fake = FakeTransport()
        result = probe_live_codex_app_server(transport=fake)

        self.assertEqual("gpt-test", result.default_model)
        self.assertFalse(hasattr(result, "session_ref"))
        self.assertFalse(hasattr(result, "state_path"))

    def test_read_only_method_sets_are_closed(self):
        self.assertEqual(("initialize", "model/list"), READ_ONLY_REQUEST_METHODS)
        self.assertEqual(("initialized",), READ_ONLY_NOTIFICATION_METHODS)

    def test_new_module_has_no_forbidden_import_or_method_surface(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        forbidden_import_names = {
            "_session_autobridge",
            "session_autobridge",
            "deliver",
            "inbox",
            "project_issue_queue",
            "registry",
            "daemon",
            "canonical",
            "ledger",
            "subprocess",
        }
        forbidden_literals = {
            "turn/start",
            "thread/resume",
            "runtime" + "_" + "dispatch",
            "runtime" + " binding",
            "SessionRefV1",
        }
        literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = set(alias.name.split("."))
                    self.assertFalse(parts & forbidden_import_names, alias.name)
            if isinstance(node, ast.ImportFrom):
                parts = set((node.module or "").split("."))
                self.assertFalse(parts & forbidden_import_names, node.module)
                for alias in node.names:
                    self.assertFalse(set(alias.name.split(".")) & forbidden_import_names, alias.name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)

        for value in literals:
            for forbidden in forbidden_literals:
                self.assertNotIn(forbidden, value)
        self.assertLessEqual(
            {value for value in literals if value in {"initialize", "initialized", "model/list"}},
            {"initialize", "initialized", "model/list"},
        )


if __name__ == "__main__":
    unittest.main()
