"""A connection must never send an unauthorized success envelope to a server request.

Two branches in bin/_session_autobridge.py answered ANY server-initiated request with
`{"result": {}}`: one in JsonRpcWebSocketClient.request() while waiting for a response, one in
execute_codex_app_server_trigger()'s turn loop. Every member of the generated ServerRequest
union is authority- or data-bearing -- command/file/permission approvals, tool calls, user
input, MCP elicitation, auth-token refresh, attestation, current time -- and not one of THEIR
response schemas can be satisfied by an empty object, so that reply was unauthorized on its
face. (Some CLIENT-request responses in the same bundle do permit an empty object, which is why
the claim is scoped to the server-request union rather than to the bundle as a whole.) Since the
production dispatch path uses this client for initialize / thread/resume / turn/start, every
automatic dispatch carried it.

The policy is per-ROLE, not global. App Server broadcasts one pending request to every
subscribed connection and the first response, result or error, can resolve it. A connection
that OWNS a turn must answer or the turn hangs; a connection that merely observes must not, or
it races the operator's own UI and can refuse work they started.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _session_autobridge as autobridge  # noqa: E402

# Every member of the ServerRequest union, from
# `codex app-server generate-json-schema --experimental`.
#
# The --experimental bundle is the right one BECAUSE this client sends
# capabilities.experimentalApi=true: without the flag the union has ten members, with it
# eleven. An earlier version of this matrix used the ten and claimed to cover every member,
# which was false for the connection we actually open. The response each one requires is
# recorded beside it: not one of them can be satisfied by an empty object, which is what makes
# `{"result": {}}` unauthorized rather than merely unhelpful.
SERVER_REQUEST_RESPONSE_REQUIREMENTS = {
    "account/chatgptAuthTokens/refresh": ("accessToken", "chatgptAccountId"),
    "applyPatchApproval": ("decision",),
    "attestation/generate": ("token",),
    "currentTime/read": ("currentTimeAt",),
    "execCommandApproval": ("decision",),
    "item/commandExecution/requestApproval": ("decision",),
    "item/fileChange/requestApproval": ("decision",),
    "item/permissions/requestApproval": ("permissions",),
    "item/tool/call": ("contentItems", "success"),
    "item/tool/requestUserInput": ("answers",),
    "mcpServer/elicitation/request": ("action",),
}
SERVER_REQUEST_METHODS = tuple(SERVER_REQUEST_RESPONSE_REQUIREMENTS)


def client(policy: str = autobridge.SERVER_REQUEST_REFUSE) -> autobridge.JsonRpcWebSocketClient:
    """A client with its transport replaced, so no socket is involved."""
    made = autobridge.JsonRpcWebSocketClient.__new__(autobridge.JsonRpcWebSocketClient)
    made.url = "ws://127.0.0.1:0"
    made.token = None
    made.timeout_seconds = 1
    made.server_request_policy = policy
    made.sock = None
    made.counter = 0
    made.server_requests = []
    made.read_deadline = None      # the shared client now carries an optional absolute deadline
    made.sent: list[dict] = []
    made.inbox: list[dict] = []
    made.send_json = made.sent.append
    return made


def feed(made, messages: list[dict]) -> None:
    made.inbox = list(messages)

    def recv_frame():
        if not made.inbox:
            raise ConnectionError("websocket closed")
        return 0x1, json.dumps(made.inbox.pop(0)).encode("utf-8")

    made._recv_frame = recv_frame


class RefusePolicyTest(unittest.TestCase):
    """Gate 1 and 3: a request in the response window is refused, id 0 included."""

    def test_a_request_interleaved_before_a_response_is_refused_not_answered(self) -> None:
        made = client()
        feed(made, [
            {"id": "srv-1", "method": "item/commandExecution/requestApproval",
             "params": {"command": "rm -rf /"}},
            {"id": "llm-collab-1", "result": {"ok": True}},
        ])
        made.counter = 0
        # drive the real request() correlation loop
        result = made.request("initialize", {})

        self.assertEqual({"ok": True}, result)
        replies = [m for m in made.sent if m.get("id") == "srv-1"]
        self.assertEqual(1, len(replies), "exactly one reply to the server request")
        self.assertIn("error", replies[0])
        self.assertNotIn("result", replies[0],
                         "a result is an authorization; dispatch may never send one")
        self.assertEqual(autobridge.JSONRPC_METHOD_NOT_FOUND, replies[0]["error"]["code"])

    def test_the_matrix_covers_the_experimental_union_this_client_opts_into(self) -> None:
        """Guards the count itself, since the claim is about completeness.

        This client sends capabilities.experimentalApi=true, so the eleven-member experimental
        union is the one that applies. Asserting ten of them and calling it every member was a
        false completeness claim, not a missing test.
        """
        self.assertEqual(11, len(SERVER_REQUEST_METHODS))
        self.assertIn("currentTime/read", SERVER_REQUEST_METHODS)
        for method, required in SERVER_REQUEST_RESPONSE_REQUIREMENTS.items():
            with self.subTest(method=method):
                self.assertTrue(required,
                                f"{method} must require fields, or an empty result would be valid")

    def test_the_client_actually_requests_the_experimental_api(self) -> None:
        # if this stopped being true, the ten-member union would be the correct matrix
        source = (ROOT / "bin" / "_session_autobridge.py").read_text(encoding="utf-8")
        self.assertIn("experimentalApi", source)

    def test_every_union_member_is_refused_with_a_correlated_error(self) -> None:
        made = client()
        feed(made, [{"id": index, "method": method, "params": {}}
                    for index, method in enumerate(SERVER_REQUEST_METHODS)]
                   + [{"method": "turn/completed", "params": {}}])
        self.assertEqual({"params": {}, "method": "turn/completed"},
                         {k: v for k, v in made.recv_json().items()})

        self.assertEqual(len(SERVER_REQUEST_METHODS), len(made.sent))
        for index, method in enumerate(SERVER_REQUEST_METHODS):
            with self.subTest(method=method):
                reply = made.sent[index]
                self.assertEqual(index, reply["id"], "the reply must correlate by id")
                self.assertIn("error", reply)
                self.assertNotIn("result", reply)
        self.assertEqual(list(SERVER_REQUEST_METHODS), made.server_requests)

    def test_a_request_with_id_zero_is_refused_not_treated_as_a_notification(self) -> None:
        # gate 3: truthiness would let 0 fall through as an event
        made = client()
        feed(made, [{"id": 0, "method": "item/tool/call", "params": {}},
                    {"method": "turn/completed", "params": {}}])
        delivered = made.recv_json()

        self.assertEqual("turn/completed", delivered["method"])
        self.assertEqual([0], [m["id"] for m in made.sent])
        self.assertIn("error", made.sent[0])


class PassThroughTest(unittest.TestCase):
    """Gate 4: notifications and method-without-id events are untouched."""

    def test_a_notification_is_returned_and_never_answered(self) -> None:
        made = client()
        note = {"method": "item/agentMessage/delta", "params": {"delta": "hi"}}
        feed(made, [note])
        self.assertEqual(note, made.recv_json())
        self.assertEqual([], made.sent)
        self.assertEqual([], made.server_requests)

    def test_a_cdp_style_event_with_no_id_passes_unchanged(self) -> None:
        made = client()
        event = {"method": "Runtime.consoleAPICalled", "params": {"type": "log"}}
        feed(made, [event])
        self.assertEqual(event, made.recv_json())
        self.assertEqual([], made.sent)

    def test_a_response_without_a_method_is_not_mistaken_for_a_request(self) -> None:
        made = client()
        response = {"id": 7, "result": {"threads": []}}
        feed(made, [response])
        self.assertEqual(response, made.recv_json())
        self.assertEqual([], made.sent)


class IgnorePolicyTest(unittest.TestCase):
    """The observer role: record the request, answer nothing at all."""

    def test_ignore_records_the_request_and_sends_no_frame(self) -> None:
        made = client(autobridge.SERVER_REQUEST_IGNORE)
        feed(made, [{"id": "srv-9", "method": "item/fileChange/requestApproval", "params": {}},
                    {"method": "turn/completed", "params": {}}])
        self.assertEqual("turn/completed", made.recv_json()["method"])
        self.assertEqual([], made.sent,
                         "an observer must not race the operator's own UI response")
        self.assertEqual(["item/fileChange/requestApproval"], made.server_requests)

    def test_an_unknown_policy_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            autobridge.JsonRpcWebSocketClient("ws://127.0.0.1:1", server_request_policy="approve")

    def test_the_default_policy_is_refuse(self) -> None:
        # the dispatch path owns its turns, so silence there would hang them
        made = autobridge.JsonRpcWebSocketClient("ws://127.0.0.1:1")
        self.assertEqual(autobridge.SERVER_REQUEST_REFUSE, made.server_request_policy)


class NoSuccessEnvelopeAnywhereTest(unittest.TestCase):
    """Gate 5, structurally: neither removed branch may come back."""

    def test_no_send_json_call_carries_a_result_key(self) -> None:
        """Parsed, not grepped: a docstring mentioning the old envelope must not fail this."""
        import ast

        source = (ROOT / "bin" / "_session_autobridge.py").read_text(encoding="utf-8")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name != "send_json":
                continue
            for argument in node.args:
                if not isinstance(argument, ast.Dict):
                    continue
                keys = [k.value for k in argument.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                if "result" in keys:
                    offenders.append(node.lineno)
        self.assertEqual([], offenders,
                         f"send_json must never carry a result key (lines {offenders})")

    def test_the_turn_loop_does_not_answer_server_requests_itself(self) -> None:
        source = (ROOT / "bin" / "_session_autobridge.py").read_text(encoding="utf-8")
        loop = source[source.index("def execute_codex_app_server_trigger"):]
        self.assertNotIn("send_json", loop,
                         "the turn loop must rely on the central policy, not its own reply")


if __name__ == "__main__":
    unittest.main()


class HandshakeClosesOnFailureTest(unittest.TestCase):
    """Every post-connect failure must close the socket, and the deadline must default off.

    The connected socket was leaked whenever sendall, recv or parsing raised before `self.sock`
    was assigned -- there was nothing for __exit__ to close, and the suites emitted unclosed-socket
    ResourceWarnings. The deadline lives here rather than in a subclass copy so there is one
    protocol authority instead of two that can drift.
    """

    def serve(self, chunks=(), gap=0.0, accept=True):
        import socket as _socket
        import threading
        import time as _time

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def run():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                for chunk in chunks:
                    _time.sleep(gap)
                    conn.sendall(chunk)
            except OSError:
                pass
            finally:
                conn.close()

        if accept:
            threading.Thread(target=run, daemon=True).start()
        return listener.getsockname()[1]

    def client(self, port, *, deadline=None, timeout=1):
        made = autobridge.JsonRpcWebSocketClient(f"ws://127.0.0.1:{port}", token=None,
                                                 timeout_seconds=timeout)
        if deadline is not None:
            made.set_deadline(deadline)
        return made

    def sockets_left_open(self, run):
        """Run `run()` and report any socket it leaves connected."""
        import socket as _socket
        created = []
        real_connect = _socket.create_connection

        def tracking(*args, **kwargs):
            sock = real_connect(*args, **kwargs)
            created.append(sock)
            return sock

        with mock.patch.object(_socket, "create_connection", tracking):
            with self.assertRaises(BaseException):
                run()
        for sock in created:
            self.addCleanup(sock.close)
        return [sock for sock in created if sock.fileno() != -1]

    def test_a_failing_sendall_closes_the_socket(self) -> None:
        port = self.serve()
        made = self.client(port)
        real_sendall = None

        def run():
            nonlocal real_sendall
            with mock.patch("socket.socket.sendall",
                            side_effect=OSError("simulated send failure")):
                with made:
                    pass

        self.assertEqual([], self.sockets_left_open(run),
                         "a connected socket must not survive a failed sendall")

    def test_a_rejected_handshake_closes_the_socket(self) -> None:
        port = self.serve([b"HTTP/1.1 400 Bad Request\r\n\r\n"])
        made = self.client(port)
        self.assertEqual([], self.sockets_left_open(lambda: made.__enter__()),
                         "a non-101 response must not leak the socket")

    def test_a_bad_accept_header_closes_the_socket(self) -> None:
        port = self.serve([b"HTTP/1.1 101 Switching Protocols\r\n"
                           b"Sec-WebSocket-Accept: wrong\r\n\r\n"])
        made = self.client(port)
        self.assertEqual([], self.sockets_left_open(lambda: made.__enter__()))

    def test_a_peer_that_hangs_up_closes_the_socket(self) -> None:
        port = self.serve([])          # accepts, reads, closes without replying
        made = self.client(port)
        self.assertEqual([], self.sockets_left_open(lambda: made.__enter__()))

    def test_a_trickled_handshake_cannot_outlast_an_absolute_deadline(self) -> None:
        import time as _time
        port = self.serve([b"HTTP/1.1 101 Switching Protocols\r\n",
                           b"Upgrade: websocket\r\n",
                           b"Connection: Upgrade\r\n\r\n"], gap=0.04)
        made = self.client(port, deadline=_time.monotonic() + 0.05, timeout=1)
        started = _time.monotonic()
        left = self.sockets_left_open(lambda: made.__enter__())
        self.assertLess(_time.monotonic() - started, 0.5)
        self.assertEqual([], left, "and the bounded-out socket must be closed too")

    def test_a_fast_trickle_is_bounded_by_the_check_not_the_clamp(self) -> None:
        """The case clamping alone cannot bound, and the reason the explicit check exists.

        Clamping each wait to whatever the deadline leaves shrinks it toward a 0.01s floor, so a
        peer sending a chunk every 40ms still times out on the read. A peer sending one every
        millisecond does not: each recv succeeds inside its own tiny timeout, forever. Only an
        explicit deadline check between reads stops that.

        My earlier trickle tests all used gaps far larger than the floor, so removing the check
        changed nothing they could see -- the mutation passed and the proof was hollow.
        """
        import time as _time
        header = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\nSec-WebSocket-Accept: x\r\n" + b"X-Pad: y\r\n" * 400
                  + b"\r\n")
        port = self.serve([header[i:i + 1] for i in range(len(header))], gap=0.001)
        made = self.client(port, deadline=_time.monotonic() + 0.05, timeout=5)
        started = _time.monotonic()
        left = self.sockets_left_open(lambda: made.__enter__())
        elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 0.5,
                        f"a 1ms trickle must still be bounded by the deadline: {elapsed:.3f}s")
        self.assertEqual([], left)

    def serve_ws_then_trickle_frame(self, payload: bytes, gap: float):
        """Complete a real handshake, then dribble ONE text frame a byte at a time."""
        import base64 as _b64
        import hashlib as _hashlib
        import re as _re
        import socket as _socket
        import threading
        import time as _time

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def run():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                request = conn.recv(4096).decode("iso-8859-1")
                key = _re.search(r"Sec-WebSocket-Key: (\S+)", request).group(1)
                accept = _b64.b64encode(_hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                    + accept.encode("ascii") + b"\r\n\r\n")
                # proper length encoding: a payload over 125 bytes needs the 126 marker plus a
                # 2-byte big-endian length. Writing bytes([0x81, len(payload)]) overflowed and
                # killed this thread, which made the client fail instantly -- a test passing
                # because its fixture crashed
                if len(payload) < 126:
                    header = bytes([0x81, len(payload)])
                else:
                    header = bytes([0x81, 126]) + len(payload).to_bytes(2, "big")
                frame = header + payload
                for i in range(len(frame)):
                    _time.sleep(gap)
                    conn.sendall(frame[i:i + 1])
                # linger: closing straight after the last byte races the client's read and made
                # this helper flaky rather than the code under test
                _time.sleep(1.0)
            except (OSError, AttributeError):
                pass
            finally:
                conn.close()

        threading.Thread(target=run, daemon=True).start()
        return listener.getsockname()[1]

    def test_a_byte_trickled_frame_cannot_outlast_the_deadline(self) -> None:
        """Your reproduction: one valid frame, payload a byte every 1ms, 50ms deadline.

        recv_json() checked the deadline once and then handed off to the exact-read loop, which
        called recv() as many times as the frame had bytes without ever looking at the clock. The
        frame was returned NORMALLY after 795ms. Checking before a loop is not bounding it.
        """
        import json as _json
        import time as _time

        # The payload must be long enough that the LOOP is what runs long, not a single read.
        # A short payload finishes inside the one timeout recv_json already set, so the inner
        # check makes no observable difference and the proof is hollow -- which is exactly what
        # my first version of this test did.
        payload = _json.dumps({"method": "turn/completed",
                               "params": {"pad": "x" * 500}}).encode()
        port = self.serve_ws_then_trickle_frame(payload, gap=0.001)
        made = autobridge.JsonRpcWebSocketClient(f"ws://127.0.0.1:{port}", token=None,
                                                 timeout_seconds=5)
        with made:
            made.set_deadline(_time.monotonic() + 0.05)
            started = _time.monotonic()
            with self.assertRaises((TimeoutError, ConnectionError, OSError)):
                made.recv_json()
            elapsed = _time.monotonic() - started
        # ~500 bytes at 1ms each is roughly 0.5s of trickle against a 0.05s budget, so an
        # unbounded loop is an order of magnitude over this bound
        self.assertLess(elapsed, 0.2,
                        f"a byte-trickled frame must be bounded: took {elapsed:.3f}s")

    def test_a_slowly_trickled_frame_HEADER_is_also_bounded(self) -> None:
        """The header read needs the check too, and my payload test could not see that.

        With a 1ms gap the two header bytes arrive well inside a 50ms budget, so removing the
        check from the header read changed nothing that test could observe. A gap large enough
        to exhaust the budget on the header itself is what distinguishes them.
        """
        import json as _json
        import time as _time

        payload = _json.dumps({"method": "turn/completed", "params": {}}).encode()
        port = self.serve_ws_then_trickle_frame(payload, gap=0.06)
        made = autobridge.JsonRpcWebSocketClient(f"ws://127.0.0.1:{port}", token=None,
                                                 timeout_seconds=5)
        with made:
            made.set_deadline(_time.monotonic() + 0.05)
            started = _time.monotonic()
            with self.assertRaises((TimeoutError, ConnectionError, OSError)):
                made.recv_json()
            elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 0.4,
                        f"the budget must expire on the header: took {elapsed:.3f}s")

    def test_a_frame_that_arrives_promptly_is_still_returned(self) -> None:
        # the bound must not break the ordinary case
        import json as _json
        import time as _time

        payload = _json.dumps({"method": "turn/started", "params": {}}).encode()
        port = self.serve_ws_then_trickle_frame(payload, gap=0.0005)
        made = autobridge.JsonRpcWebSocketClient(f"ws://127.0.0.1:{port}", token=None,
                                                 timeout_seconds=5)
        with made:
            # a generous deadline: the point is that bounding does not break the ordinary case
            made.set_deadline(_time.monotonic() + 5.0)
            message = made.recv_json()
        self.assertEqual("turn/started", message["method"])

    def test_a_trickled_frame_with_no_deadline_is_still_read(self) -> None:
        """The None default must keep the historical behaviour for existing callers."""
        import json as _json

        payload = _json.dumps({"method": "turn/started", "params": {}}).encode()
        port = self.serve_ws_then_trickle_frame(payload, gap=0.001)
        made = autobridge.JsonRpcWebSocketClient(f"ws://127.0.0.1:{port}", token=None,
                                                 timeout_seconds=5)
        with made:
            self.assertIsNone(made.read_deadline)
            self.assertEqual("turn/started", made.recv_json()["method"])

    def test_the_default_has_no_deadline_so_existing_callers_are_unchanged(self) -> None:
        made = autobridge.JsonRpcWebSocketClient("ws://127.0.0.1:1", timeout_seconds=30)
        self.assertIsNone(made.read_deadline)
        self.assertEqual(30, made.remaining_wait(),
                         "with no deadline every wait gets the full timeout, as before")
