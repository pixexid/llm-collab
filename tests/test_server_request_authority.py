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
