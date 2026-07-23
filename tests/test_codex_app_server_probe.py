import ast
import json
import unittest
from pathlib import Path

from llm_collab.codex_app_server_probe import (
    CLIENT_CAPABILITIES,
    MCP_LIFECYCLE_SPEC,
    PROTOCOL_VERSION,
    AppServerProbeError,
    probe_app_server,
)


MODULE = Path("llm_collab/codex_app_server_probe.py")
AUTOBRIDGE = Path("bin/_session_autobridge.py")
EXPECTED_METHODS = ("initialize", "model/list")
EXPECTED_SERVER = "codex-app-server"
EXPECTED_SERVER_CAPABILITIES = frozenset(("tools",))


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


class CodexAppServerProbeTests(unittest.TestCase):
    def probe(self, fake):
        return probe_app_server(
            fake,
            expected_server_name=EXPECTED_SERVER,
            expected_server_capabilities=EXPECTED_SERVER_CAPABILITIES,
        )

    def test_positive_probe_uses_sourced_read_only_methods(self):
        fake = FakeTransport()
        result = self.probe(fake)

        self.assertEqual(PROTOCOL_VERSION, result.protocol_version)
        self.assertEqual(EXPECTED_SERVER, result.server_name)
        self.assertEqual(EXPECTED_SERVER_CAPABILITIES, result.capabilities)
        self.assertEqual("gpt-test", result.default_model)
        self.assertEqual(EXPECTED_METHODS, tuple(frame["method"] for frame in fake.requests))
        self.assertEqual(["initialized"], [frame["method"] for frame in fake.notifications])
        self.assertEqual(["2.0", "2.0"], [frame["jsonrpc"] for frame in fake.requests])
        self.assertEqual(["llm-collab-1", "llm-collab-2"], [frame["id"] for frame in fake.requests])
        self.assertEqual({"experimentalApi": True}, fake.requests[0]["params"]["capabilities"])

    def test_method_names_and_version_trace_to_existing_autobridge(self):
        source = AUTOBRIDGE.read_text(encoding="utf-8")
        for method in EXPECTED_METHODS + ("initialized",):
            self.assertIn(f'"{method}"', source)
        self.assertIn(f'"{PROTOCOL_VERSION}"', source)
        self.assertIn("2024-11-05/basic/lifecycle", MCP_LIFECYCLE_SPEC)

    def test_version_drift_fails_closed(self):
        with self.assertRaisesRegex(AppServerProbeError, "unsupported protocolVersion"):
            self.probe(FakeTransport(version="2025-01-01"))

    def test_capabilities_are_exact_not_renderer_visibility(self):
        with self.assertRaisesRegex(AppServerProbeError, "unknown capability"):
            self.probe(
                FakeTransport(capabilities={"rendererVisible": True}),
            )
        with self.assertRaisesRegex(AppServerProbeError, "missing capability"):
            self.probe(FakeTransport(capabilities={}))
        with self.assertRaisesRegex(AppServerProbeError, "unknown capability"):
            self.probe(FakeTransport(capabilities={"tools": {}, "other": True}))

    def test_server_capabilities_do_not_echo_client_capabilities(self):
        fake = FakeTransport(capabilities={"tools": {}, "resources": {}})
        result = probe_app_server(
            fake,
            expected_server_name=EXPECTED_SERVER,
            expected_server_capabilities=frozenset(("tools", "resources")),
        )
        self.assertEqual(frozenset(("tools", "resources")), result.capabilities)
        self.assertEqual({"experimentalApi": True}, fake.requests[0]["params"]["capabilities"])

    def test_malformed_duplicate_unknown_and_identity_drift_fail_closed(self):
        with self.assertRaisesRegex(AppServerProbeError, "duplicate response member"):
            self.probe(
                FakeTransport(raw='{"jsonrpc":"2.0","id":"llm-collab-1","result":{},"result":{}}'),
            )
        with self.assertRaisesRegex(AppServerProbeError, "unknown response member"):
            self.probe(
                FakeTransport(raw=json.dumps({"jsonrpc": "2.0", "id": "llm-collab-1", "result": {}, "extra": True})),
            )
        with self.assertRaisesRegex(AppServerProbeError, "inconsistent server identity"):
            self.probe(FakeTransport(server_name="other"))

    def test_module_has_no_live_or_send_surface(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        banned_imports = {"socket", "subprocess", "urllib", "requests", "websocket"}
        banned_modules = {"bin._session_autobridge", "_session_autobridge"}
        literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned_imports)
                    self.assertNotIn(alias.name, banned_modules)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn(module.split(".")[0], banned_imports)
                self.assertNotIn(module, banned_modules)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)

        self.assertFalse(any(value.startswith("turn/") for value in literals))
        self.assertNotIn("thread/resume", literals)
        self.assertNotIn("SessionRefV1", literals)


if __name__ == "__main__":
    unittest.main()
