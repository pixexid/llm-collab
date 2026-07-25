"""codex_stream must resolve exactly one thread and never answer a server request.

Two behaviours carry real risk. Resolving `--agent codex` when several bindings match
would silently watch one of several threads, which is the wrong-thread failure the
exact-dispatch contract exists to prevent. Answering a server-initiated request --
an approval -- would vote on the operator's behalf on a turn this observer does not
own; the observer must refuse it explicitly on the same socket.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import codex_stream  # noqa: E402


def binding(root: Path, project: str, chat: str, agent: str, thread: str,
            status: str = "active", updated: str = "2026-07-25T00:00:00+00:00") -> None:
    path = root / project / chat / f"{agent}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agent_id": agent, "project_id": project, "chat_id": chat,
        "runtime_session_id": thread, "status": status, "updated_utc": updated,
    }), encoding="utf-8")


class ResolveThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(codex_stream, "BINDINGS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, **kw) -> SimpleNamespace:
        base = {"agent": None, "project": None, "chat": None, "thread": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_exact_project_and_chat_resolves(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        thread, provenance = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-AAA"))
        self.assertEqual("thread-1", thread)
        self.assertIn("CHAT-AAA", provenance)

    def test_ambiguous_agent_lookup_refuses_instead_of_picking_one(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        binding(self.root, "nuvyr", "CHAT-BBB", "codex", "thread-2")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex"))
        message = str(caught.exception)
        self.assertIn("CHAT-AAA", message)
        self.assertIn("CHAT-BBB", message, "both candidates must be named")

    def test_chat_last_accepts_ambiguity_and_takes_the_newest(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1",
                updated="2026-07-01T00:00:00+00:00")
        binding(self.root, "amiga", "CHAT-BBB", "codex", "thread-2",
                updated="2026-07-25T00:00:00+00:00")
        thread, _ = codex_stream.resolve_thread(self.args(agent="codex", chat="last"))
        self.assertEqual("thread-2", thread)

    def test_parked_binding_loses_to_an_active_one(self) -> None:
        binding(self.root, "amiga", "CHAT-OLD", "codex", "thread-parked", status="parked")
        binding(self.root, "amiga", "CHAT-NEW", "codex", "thread-active", status="active")
        thread, _ = codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertEqual("thread-active", thread)

    def test_binding_without_a_runtime_session_id_is_not_a_candidate(self) -> None:
        path = self.root / "amiga" / "CHAT-AAA" / "codex.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"agent_id": "codex", "runtime_session_id": None}),
                        encoding="utf-8")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))

    def test_explicit_thread_bypasses_binding_lookup(self) -> None:
        thread, provenance = codex_stream.resolve_thread(self.args(thread="thread-x"))
        self.assertEqual("thread-x", thread)
        self.assertEqual("--thread", provenance)


class ObserverRefusesServerRequestsTest(unittest.TestCase):
    """The safety contract must hold on the wire, not only in the docstring.

    The base client answers any interleaved server request with {"result": {}}, so an
    approval arriving during initialize/resume would be APPROVED. And a request read in
    the steady-state loop cannot be deferred to another client: a JSON-RPC response belongs
    on the connection that received it, so refusing here is the only coherent answer.
    """

    def client(self, incoming: list[dict]) -> codex_stream.ObserverClient:
        client = codex_stream.ObserverClient.__new__(codex_stream.ObserverClient)
        client.refused = []
        client.sent: list[dict] = []
        client.queue = list(incoming)
        client.send_json = client.sent.append
        return client

    def drain(self, client, count: int) -> list[dict]:
        def base_recv(_self=None):
            return client.queue.pop(0)
        with mock.patch.object(codex_stream.autobridge.JsonRpcWebSocketClient,
                               "recv_json", base_recv):
            return [client.recv_json() for _ in range(count)]

    def test_an_approval_request_is_answered_with_an_error_never_a_result(self) -> None:
        approval = {"id": "srv-1", "method": "item/commandExecution/requestApproval",
                    "params": {"command": "rm -rf /"}}
        event = {"method": "turn/completed", "params": {}}
        got = self.client([approval, event])
        delivered = self.drain(got, 1)

        self.assertEqual([event], delivered, "the request must not surface as an event")
        self.assertEqual(1, len(got.sent), "exactly one response must go out")
        reply = got.sent[0]
        self.assertEqual("srv-1", reply["id"], "the reply must correlate to the request")
        self.assertIn("error", reply)
        self.assertNotIn("result", reply,
                         "a result is an approval; an observer must never send one")
        self.assertEqual(codex_stream.METHOD_NOT_FOUND, reply["error"]["code"])
        self.assertEqual(["item/commandExecution/requestApproval"], got.refused)

    def test_a_request_interleaved_before_a_response_is_still_refused(self) -> None:
        # this is the initialize/resume window, where the base client would answer {}
        approval = {"id": "srv-2", "method": "item/fileChange/requestApproval", "params": {}}
        response = {"id": "llm-collab-1", "result": {"ok": True}}
        got = self.client([approval, response])
        delivered = self.drain(got, 1)

        self.assertEqual([response], delivered)
        self.assertIn("error", got.sent[0])
        self.assertEqual("srv-2", got.sent[0]["id"])

    def test_a_plain_notification_is_passed_through_untouched(self) -> None:
        note = {"method": "item/agentMessage/delta", "params": {"delta": "hi"}}
        got = self.client([note])
        self.assertEqual([note], self.drain(got, 1))
        self.assertEqual([], got.sent, "notifications need no response at all")


class RecordIdentityTest(unittest.TestCase):
    """A record at the expected path is not proof of what the record is."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(codex_stream, "BINDINGS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, **kw):
        base = {"agent": None, "project": None, "chat": None, "thread": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def write(self, project: str, chat: str, agent: str, payload) -> None:
        path = self.root / project / chat / f"{agent}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                        encoding="utf-8")

    def test_a_record_claiming_another_project_and_chat_is_refused(self) -> None:
        self.write("amiga", "CHAT-A", "codex", {
            "agent_id": "claude", "project_id": "nuvyr", "chat_id": "CHAT-B",
            "runtime_session_id": "wrong-thread", "status": "active",
        })
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="CHAT-A"))

    def test_a_record_agreeing_with_its_path_is_admitted(self) -> None:
        self.write("amiga", "CHAT-A", "codex", {
            "agent_id": "codex", "project_id": "amiga", "chat_id": "CHAT-A",
            "runtime_session_id": "right-thread", "status": "active",
        })
        thread, _ = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("right-thread", thread)

    def test_malformed_json_fails_closed_rather_than_raising(self) -> None:
        self.write("amiga", "CHAT-A", "codex", "{not json")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="CHAT-A"))

    def test_a_json_list_is_not_mistaken_for_a_record(self) -> None:
        self.write("amiga", "CHAT-A", "codex", ["runtime_session_id"])
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="CHAT-A"))


class InterruptExitCodeTest(unittest.TestCase):
    """Ctrl-C during streaming must exit 130, not 0.

    The stream loop caught KeyboardInterrupt and broke, so main() returned normally and
    the outer 130 handler never ran -- supervision saw a clean exit for an interrupted
    view, which is the same ambiguity the transport-failure fix removed.
    """

    def test_keyboard_interrupt_is_not_swallowed_by_the_stream_loop(self) -> None:
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        loop = source[source.index("while deadline is None"):source.index("if pending_text")]
        self.assertNotIn("except KeyboardInterrupt", loop,
                         "the loop must let Ctrl-C reach the 130 handler")

    def test_the_module_promises_only_what_it_does(self) -> None:
        # prose drift is how a safety claim outlives the behaviour it described
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn("left for the turn owner", source)
        self.assertNotIn("never\nanswers a server-initiated request", source)
        self.assertIn("never a result", source)


class DescribeTest(unittest.TestCase):
    def test_agent_message_completion_stays_quiet_because_deltas_already_printed(self) -> None:
        self.assertIsNone(
            codex_stream.describe("item/completed", {"item": {"type": "agentMessage"}}))

    def test_command_execution_surfaces_the_command_and_exit_code(self) -> None:
        started = codex_stream.describe(
            "item/started", {"item": {"type": "commandExecution", "command": "pytest -q"}})
        self.assertIn("pytest -q", started)
        done = codex_stream.describe(
            "item/completed", {"item": {"type": "commandExecution", "exitCode": 1}})
        self.assertIn("1", done)


if __name__ == "__main__":
    unittest.main()
