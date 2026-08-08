import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_collab.codex_app_server_live_probe import (
    CLIENT_CAPABILITIES,
    _WebSocketJsonRpcTransport,
    _envelope,
    EXPECTED_SERVER,
    EXPECTED_SERVER_CAPABILITIES,
    OBSERVATION_ADMISSIBLE,
    OBSERVATION_BUSY,
    OBSERVATION_UNCERTAIN,
    PROTOCOL_VERSION,
    READ_ONLY_NOTIFICATION_METHODS,
    READ_ONLY_REQUEST_METHODS,
    CodexAppServerExactThreadResult,
    CodexAppServerLiveProbeError,
    THREAD_READ_METHOD,
    classify_thread_observation,
    observe_exact_thread,
    probe_exact_thread,
    probe_live_codex_app_server,
    probe_runtime_home_identity,
)


MODULE = Path("llm_collab/codex_app_server_live_probe.py")


class FakeTransport:
    def __init__(self, *, version=PROTOCOL_VERSION, capabilities=None, server_name=EXPECTED_SERVER, raw=None,
                 known_thread_ids=None, thread_read_error=False, thread_read_raw=None, thread_read_return_id=None,
                 init_result=None):
        self.version = version
        self.init_result = init_result
        self.capabilities = {"tools": {"listChanged": True}} if capabilities is None else dict(capabilities)
        self.server_name = server_name
        self.raw = raw
        self.known_thread_ids = known_thread_ids
        self.thread_read_error = thread_read_error
        self.thread_read_raw = thread_read_raw
        self.thread_read_return_id = thread_read_return_id
        self.requests = []
        self.notifications = []

    def exchange(self, frame):
        self.requests.append(frame)
        if self.raw is not None:
            return self.raw
        if frame["method"] == "initialize":
            if self.init_result is not None:
                return {"id": frame["id"], "result": self.init_result}
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
        if frame["method"] == "thread/read":
            if self.thread_read_raw is not None:
                return self.thread_read_raw
            thread_id = frame.get("params", {}).get("threadId")
            if self.thread_read_error or (
                self.known_thread_ids is not None and thread_id not in self.known_thread_ids
            ):
                return {"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32602, "message": "unknown thread"}}
            return_id = self.thread_read_return_id if self.thread_read_return_id is not None else thread_id
            return {"jsonrpc": "2.0", "id": frame["id"], "result": {"thread": {"id": return_id}}}
        raise AssertionError(f"unexpected method {frame['method']}")

    def notify(self, frame):
        self.notifications.append(frame)


def _ws_text_frame(obj):
    data = json.dumps(obj).encode("utf-8")
    if len(data) < 126:
        header = bytes([0x81, len(data)])
    elif len(data) <= 0xFFFF:
        header = bytes([0x81, 126]) + len(data).to_bytes(2, "big")
    else:
        header = bytes([0x81, 127]) + len(data).to_bytes(8, "big")
    return header + data


class _FakeSock:
    def __init__(self, frame_bytes):
        self._buf = b"".join(frame_bytes)
        self.sent = []

    def recv(self, n):
        if not self._buf:
            raise OSError("closed")
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, _timeout):
        pass

    def close(self):
        pass


def _transport_with_frames(frames, timeout_seconds=5):
    transport = _WebSocketJsonRpcTransport.__new__(_WebSocketJsonRpcTransport)
    transport._socket = _FakeSock(frames)
    transport._closed = False
    transport.timeout_seconds = timeout_seconds
    return transport


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

    def test_exact_thread_probe_succeeds_with_a_native_thread_read(self):
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        fake = FakeTransport(known_thread_ids={thread_id})
        result = probe_exact_thread(thread_id, transport=fake)

        self.assertEqual(thread_id, result.thread_id)
        self.assertEqual(("initialize", THREAD_READ_METHOD), result.methods)
        self.assertEqual(["initialize", "thread/read"], [f["method"] for f in fake.requests])
        self.assertEqual(["llm-collab-1", "llm-collab-2"], [f["id"] for f in fake.requests])
        self.assertEqual(["initialized"], [f["method"] for f in fake.notifications])
        # Load-bearing: thread/read carries exactly threadId + includeTurns false
        # -- no resume, no mutation, anything that would start a turn.
        self.assertEqual({"threadId": thread_id, "includeTurns": False}, fake.requests[1]["params"])

    def test_runtime_home_identity_uses_minimal_initialize_and_matches_exactly(self):
        fake = FakeTransport(init_result={"codexHome": "/Users/test/.codex"})

        self.assertEqual(
            "/Users/test/.codex",
            probe_runtime_home_identity("/Users/test/.codex", transport=fake),
        )
        self.assertEqual(["initialize"], [frame["method"] for frame in fake.requests])
        self.assertEqual(["initialized"], [frame["method"] for frame in fake.notifications])

    def test_runtime_home_identity_refuses_a_different_home(self):
        fake = FakeTransport(init_result={"codexHome": "/Users/other/.codex"})

        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "does not match"):
            probe_runtime_home_identity("/Users/test/.codex", transport=fake)

    def test_exact_thread_probe_fails_closed_on_an_unknown_thread_id(self):
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        fake = FakeTransport(known_thread_ids={"019f0000-0000-0000-0000-000000000000"})
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "thread/read failed"):
            probe_exact_thread(thread_id, transport=fake)
        # initialize + the native thread/read were attempted; nothing else.
        self.assertEqual(["initialize", "thread/read"], [f["method"] for f in fake.requests])

    def test_exact_thread_probe_fails_closed_on_a_read_error(self):
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        fake = FakeTransport(thread_read_error=True)
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "thread/read failed"):
            probe_exact_thread(thread_id, transport=fake)

    def test_exact_thread_probe_fails_closed_on_a_mismatched_returned_id(self):
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        fake = FakeTransport(known_thread_ids={thread_id}, thread_read_return_id="00000000-0000-0000-0000-000000000000")
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "returned a different thread id"):
            probe_exact_thread(thread_id, transport=fake)

    def test_exact_thread_probe_fails_closed_on_read_envelope_drift(self):
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        fake = FakeTransport(thread_read_raw=json.dumps(
            {"jsonrpc": "2.0", "id": "llm-collab-2", "result": {}, "error": {"code": -1}}
        ))
        with self.assertRaises(CodexAppServerLiveProbeError):
            probe_exact_thread(thread_id, transport=fake)

    def test_exact_thread_probe_rejects_empty_and_non_string_ids_before_any_request(self):
        fake = FakeTransport()
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "thread_id is required"):
            probe_exact_thread("", transport=fake)
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "thread_id is required"):
            probe_exact_thread(12345, transport=fake)
        # Empty/non-string ids fail closed before any App Server request is sent.
        self.assertEqual([], fake.requests)

    def test_exact_thread_probe_accepts_an_opaque_non_uuid_thread_id(self):
        # Native thread ids are opaque server-owned strings (not necessarily
        # UUIDs): a non-UUID id reaches thread/read and is proven by returned-id
        # equality, not by client-side UUID parsing.
        opaque = "an-opaque-server-thread-id"
        fake = FakeTransport(known_thread_ids={opaque})
        result = probe_exact_thread(opaque, transport=fake)
        self.assertEqual(opaque, result.thread_id)
        self.assertEqual({"threadId": opaque, "includeTurns": False}, fake.requests[1]["params"])

    def test_exact_thread_probe_requires_exactly_one_endpoint_or_transport(self):
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "exactly one"):
            probe_exact_thread(thread_id)
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "exactly one"):
            probe_exact_thread(thread_id, endpoint_url="ws://127.0.0.1:1", transport=FakeTransport())

    def test_envelope_jsonrpc_strictness_is_parameterized(self):
        # probe_live (default) requires jsonrpc present and 2.0; only the
        # jsonrpc-optional path (require_jsonrpc=False) accepts an absent member.
        with self.assertRaises(CodexAppServerLiveProbeError):
            _envelope({"id": "llm-collab-1", "result": {}})  # strict: absent rejected
        self.assertEqual({}, _envelope({"id": "llm-collab-1", "result": {}}, require_jsonrpc=False)["result"])
        with self.assertRaises(CodexAppServerLiveProbeError):
            _envelope({"jsonrpc": "1.0", "id": "llm-collab-1", "result": {}}, require_jsonrpc=False)

    def test_probe_live_rejects_an_absent_jsonrpc_member(self):
        # probe_live keeps the strict contract: a response missing jsonrpc is rejected.
        fake = FakeTransport(raw=json.dumps({"id": "llm-collab-1", "result": {
            "protocolVersion": PROTOCOL_VERSION, "serverInfo": {"name": EXPECTED_SERVER},
            "capabilities": {"tools": {"listChanged": True}}}}))
        with self.assertRaises(CodexAppServerLiveProbeError):
            probe_live_codex_app_server(transport=fake)

    def test_exact_thread_probe_accepts_an_absent_jsonrpc_response(self):
        # Only the exact-thread path is jsonrpc-optional: a thread/read response
        # missing jsonrpc is accepted (result.thread.id still proven).
        thread_id = "019f9452-6954-7301-bff9-db1c47432bc8"
        fake = FakeTransport(
            known_thread_ids={thread_id},
            thread_read_raw=json.dumps({"id": "llm-collab-2", "result": {"thread": {"id": thread_id}}}),
        )
        result = probe_exact_thread(thread_id, transport=fake)
        self.assertEqual(thread_id, result.thread_id)

    def test_close_sends_the_close_frame_before_clearing_state(self):
        class FakeSock:
            def __init__(self):
                self.sent: list[bytes] = []
                self.closed = False

            def sendall(self, data: bytes) -> None:
                self.sent.append(data)

            def close(self) -> None:
                self.closed = True

        sock = FakeSock()
        transport = _WebSocketJsonRpcTransport.__new__(_WebSocketJsonRpcTransport)
        transport._socket = sock
        transport._closed = False
        transport.close()  # must not raise

        # The close frame (opcode 0x8 -> first byte 0x88) must be sent before the
        # transport marks itself closed, otherwise _send_frame rejects it.
        self.assertTrue(sock.sent, "close frame was not sent")
        self.assertEqual(0x88, sock.sent[0][0])
        self.assertTrue(transport._closed)
        self.assertIsNone(transport._socket)

    def test_exchange_skips_a_notification_before_the_matched_response(self):
        transport = _transport_with_frames([
            _ws_text_frame({"jsonrpc": "2.0", "method": "thread/started", "params": {}}),
            _ws_text_frame({"id": "llm-collab-1", "result": {"thread": {"id": "T"}}}),
        ])
        msg = transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})
        self.assertEqual("llm-collab-1", msg["id"])
        self.assertEqual({"thread": {"id": "T"}}, msg["result"])

    def test_exchange_fails_closed_on_a_non_matching_response_id(self):
        transport = _transport_with_frames([_ws_text_frame({"id": "other", "result": {}})])
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "response id mismatch"):
            transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_fails_closed_on_a_server_request(self):
        transport = _transport_with_frames([
            _ws_text_frame({"jsonrpc": "2.0", "id": "srv-1", "method": "approval/requested", "params": {}}),
        ])
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "server request during exchange"):
            transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_fails_closed_when_notification_count_exceeds_the_bound(self):
        from llm_collab.codex_app_server_live_probe import MAX_EXCHANGE_NOTIFICATIONS
        transport = _transport_with_frames(
            [_ws_text_frame({"method": "n", "params": {}}) for _ in range(MAX_EXCHANGE_NOTIFICATIONS + 1)]
        )
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "too many notifications"):
            transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_fails_closed_when_a_single_frame_exceeds_the_byte_budget(self):
        from llm_collab.codex_app_server_live_probe import MAX_EXCHANGE_BYTES
        header = bytes([0x81, 127]) + (MAX_EXCHANGE_BYTES + 1).to_bytes(8, "big")
        transport = _transport_with_frames([header])
        with self.assertRaisesRegex(CodexAppServerLiveProbeError, "byte budget exceeded"):
            transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_fails_closed_when_cumulative_small_frames_exceed_the_budget(self):
        import llm_collab.codex_app_server_live_probe as module
        with patch.object(module, "MAX_EXCHANGE_BYTES", 256):
            small = _ws_text_frame({"method": "n", "params": {"x": "y" * 40}})
            # Consume valid small notification frames until little budget remains,
            # then provide ONLY the next frame HEADER declaring a payload above the
            # remainder (no payload bytes). A correct impl refuses at the header,
            # before reading/allocating the payload; a late-checking impl would
            # instead try to read the absent payload and fail differently.
            oversized_header = bytes([0x81, 100])  # fin+text, declared length 100, no payload bytes
            transport = _transport_with_frames([small, small, small, oversized_header])
            with self.assertRaisesRegex(CodexAppServerLiveProbeError, "byte budget exceeded"):
                transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_fails_closed_when_the_absolute_deadline_passes(self):
        import llm_collab.codex_app_server_live_probe as module
        transport = _transport_with_frames([_ws_text_frame({"method": "n", "params": {}})], timeout_seconds=1)
        ticks = {"v": 0.0}

        def advancing_monotonic():
            ticks["v"] += 100.0
            return ticks["v"]

        with patch.object(module.time, "monotonic", advancing_monotonic):
            with self.assertRaisesRegex(CodexAppServerLiveProbeError, "timed out"):
                transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_times_out_when_a_peer_drips_bytes_past_the_deadline(self):
        import llm_collab.codex_app_server_live_probe as module
        frame = _ws_text_frame({"method": "n", "params": {}})

        class _DripSock:
            def __init__(self, data):
                self._buf = data
                self.sent = []

            def recv(self, _n):
                if not self._buf:
                    return b""
                b, self._buf = self._buf[:1], self._buf[1:]
                return b  # one byte per recv

            def sendall(self, data):
                self.sent.append(data)

            def settimeout(self, _t):
                pass

            def close(self):
                pass

        transport = _WebSocketJsonRpcTransport.__new__(_WebSocketJsonRpcTransport)
        transport._socket = _DripSock(frame)
        transport._closed = False
        transport.timeout_seconds = 0.05
        ticks = {"v": 0.0}

        def advancing_monotonic():
            ticks["v"] += 0.02
            return ticks["v"]

        # The deadline is recomputed before EVERY recv, so dripping one byte at a
        # time cannot run past it; a single per-exchange settimeout could.
        with patch.object(module.time, "monotonic", advancing_monotonic):
            with self.assertRaisesRegex(CodexAppServerLiveProbeError, "timed out"):
                transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})

    def test_exchange_sets_the_send_timeout_before_the_first_sendall(self):
        import llm_collab.codex_app_server_live_probe as module
        response = _ws_text_frame({"id": "llm-collab-1", "result": {"thread": {"id": "T"}}})

        class _OrderSock:
            def __init__(self, data):
                self._buf = data
                self.calls = []

            def settimeout(self, _t):
                self.calls.append("settimeout")

            def sendall(self, _data):
                self.calls.append("sendall")

            def recv(self, n):
                self.calls.append("recv")
                if not self._buf:
                    return b""
                chunk, self._buf = self._buf[:n], self._buf[n:]
                return chunk

            def close(self):
                pass

        transport = _WebSocketJsonRpcTransport.__new__(_WebSocketJsonRpcTransport)
        transport._socket = _OrderSock(response)
        transport._closed = False
        transport.timeout_seconds = 5
        transport.exchange({"jsonrpc": "2.0", "id": "llm-collab-1", "method": "thread/read", "params": {}})
        # The send timeout must be set BEFORE the first sendall (deadline-before-send,
        # so socket backpressure is bounded). Moving the deadline after _send_json or
        # dropping the deadline arg breaks this ordering.
        self.assertLess(transport._socket.calls.index("settimeout"), transport._socket.calls.index("sendall"))

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
            # thread/read (native, threadId only) is the approved sole
            # thread-touching read-only request; thread/resume, turn/start, and
            # the rest stay forbidden.
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



INIT_RESULT = {
    "codexHome": "/Users/test/.codex",
    "userAgent": "llm-collab-pm2-verify/0.146.0-alpha.3.1 (Mac OS 27.0.0; arm64)",
    "platformFamily": "unix",
    "platformOs": "macos",
}
HOME = "/Users/test/.codex"
UA_PREFIXES = frozenset(("llm-collab-pm2-verify/0.146",))
THREAD_ID = "019faff9-e265-7e43-bee3-006d49e8e505"


def observe_transport(thread_result=None, *, thread_error=False, init_result=INIT_RESULT):
    raw = None
    if thread_result is not None:
        raw = {"id": "llm-collab-2", "result": {"thread": thread_result}}
    return FakeTransport(init_result=init_result, thread_read_raw=raw, thread_read_error=thread_error)


def observe(transport, **overrides):
    kwargs = {
        "expected_runtime_home": HOME,
        "supported_user_agent_prefixes": UA_PREFIXES,
        "transport": transport,
    }
    kwargs.update(overrides)
    return observe_exact_thread(THREAD_ID, **kwargs)


class CodexAppServerThreadObservationTests(unittest.TestCase):
    def test_classification_table(self):
        cases = [
            ({"id": THREAD_ID, "status": {"type": "idle"}, "canAcceptDirectInput": True}, OBSERVATION_ADMISSIBLE),
            # Load-bearing: direct input stays true during active turns, so it
            # can never be the busy discriminator — active + true is busy.
            ({"id": THREAD_ID, "status": {"type": "active", "activeFlags": []}, "canAcceptDirectInput": True}, OBSERVATION_BUSY),
            ({"id": THREAD_ID, "status": {"type": "notLoaded"}, "canAcceptDirectInput": None}, OBSERVATION_UNCERTAIN),
            ({"id": THREAD_ID, "status": {"type": "paused"}, "canAcceptDirectInput": True}, OBSERVATION_UNCERTAIN),
            ({"id": THREAD_ID, "canAcceptDirectInput": True}, OBSERVATION_UNCERTAIN),
            ({"id": THREAD_ID, "status": "idle", "canAcceptDirectInput": True}, OBSERVATION_UNCERTAIN),
            ({"id": THREAD_ID, "status": {"type": 7}, "canAcceptDirectInput": True}, OBSERVATION_UNCERTAIN),
            ({"id": THREAD_ID, "status": {"type": "idle"}}, OBSERVATION_UNCERTAIN),
            ({"id": THREAD_ID, "status": {"type": "idle"}, "canAcceptDirectInput": False}, OBSERVATION_UNCERTAIN),
        ]
        for thread_result, expected in cases:
            with self.subTest(thread_result=thread_result):
                self.assertEqual(observe(observe_transport(thread_result)).classification, expected)

    def test_admissible_observation_captures_identity_and_stays_read_only(self):
        transport = observe_transport({"id": THREAD_ID, "status": {"type": "idle"},
                                       "canAcceptDirectInput": True, "turns": []})
        observation = observe(transport)
        self.assertEqual(observation.classification, OBSERVATION_ADMISSIBLE)
        self.assertEqual(observation.status_type, "idle")
        self.assertIs(observation.can_accept_direct_input, True)
        self.assertEqual(observation.codex_home, HOME)
        self.assertTrue(observation.user_agent.startswith("llm-collab-pm2-verify/0.146"))
        self.assertEqual([f["method"] for f in transport.requests], ["initialize", "thread/read"])
        self.assertEqual(transport.requests[1]["params"],
                         {"threadId": THREAD_ID, "includeTurns": False})

    def test_read_failure_after_identity_proof_is_uncertain_not_a_crash(self):
        observation = observe(observe_transport(thread_error=True))
        self.assertEqual(observation.classification, OBSERVATION_UNCERTAIN)
        self.assertIsNone(observation.status_type)
        self.assertIsNone(observation.can_accept_direct_input)
        self.assertEqual(observation.codex_home, HOME)

    def test_runtime_home_drift_fails_closed(self):
        with self.assertRaises(CodexAppServerLiveProbeError):
            observe(observe_transport(), expected_runtime_home="/Users/other/.codex")

    def test_user_agent_drift_fails_closed(self):
        with self.assertRaises(CodexAppServerLiveProbeError):
            observe(observe_transport(), supported_user_agent_prefixes=frozenset(("other-server/1.",)))

    def test_returned_thread_id_mismatch_fails_closed(self):
        transport = observe_transport({"id": "019faffa-e76c-7800-811f-3f6716b8b753",
                                       "status": {"type": "idle"}, "canAcceptDirectInput": True})
        with self.assertRaises(CodexAppServerLiveProbeError):
            observe(transport)

    def test_runtime_home_must_be_an_absolute_normalized_realpath(self):
        for bad in ("/tmp/..", "relative/path", "", None, "/Users/test/.cod\x00ex"):
            with self.subTest(bad=bad):
                with self.assertRaises(CodexAppServerLiveProbeError):
                    observe(observe_transport(), expected_runtime_home=bad)


if __name__ == "__main__":
    unittest.main()
