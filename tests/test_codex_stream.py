"""codex_stream must resolve exactly one thread and never answer a server request.

Two behaviours carry real risk. Resolving `--agent codex` when several bindings match
would silently watch one of several threads, which is the wrong-thread failure the
exact-dispatch contract exists to prevent. Answering a server-initiated request --
an approval -- would vote on the operator's behalf on a turn this observer does not
own; the observer must answer nothing at all.
"""

from __future__ import annotations

import contextlib
import io
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

    def test_chat_without_project_still_narrows_to_that_chat(self) -> None:
        """A supplied --chat must be honoured even when --project is omitted.

        With exactly one active binding elsewhere, ignoring --chat returned that other
        thread and no ambiguity error fired, because there was nothing to be ambiguous
        about. Only the wrong-chat negative case exposes this; a two-binding fixture masks
        it behind the refusal.
        """
        binding(self.root, "amiga", "CHAT-OTHER", "codex", "other-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", chat="CHAT-WANTED"))
        self.assertIn("no binding", str(caught.exception).lower())

    def test_chat_without_project_resolves_its_own_binding(self) -> None:
        binding(self.root, "amiga", "CHAT-OTHER", "codex", "other-thread")
        binding(self.root, "nuvyr", "CHAT-WANTED", "codex", "wanted-thread")
        thread, provenance = codex_stream.resolve_thread(
            self.args(agent="codex", chat="CHAT-WANTED"))
        self.assertEqual("wanted-thread", thread)
        self.assertIn("CHAT-WANTED", provenance)

    def test_project_without_chat_still_narrows_to_that_project(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "amiga-thread")
        binding(self.root, "nuvyr", "CHAT-B", "codex", "nuvyr-thread")
        thread, _ = codex_stream.resolve_thread(self.args(agent="codex", project="nuvyr"))
        self.assertEqual("nuvyr-thread", thread)

    def test_explicit_thread_bypasses_binding_lookup(self) -> None:
        thread, provenance = codex_stream.resolve_thread(self.args(thread="thread-x"))
        self.assertEqual("thread-x", thread)
        self.assertEqual("--thread", provenance)


class ObserverAnswersNothingTest(unittest.TestCase):
    """The observer must send zero response frames for a server request.

    The base client answers any interleaved request with {"result": {}} -- invalid for all
    ten ServerRequest methods, since no Response schema in the bundle permits an empty
    object. An automatic JSON-RPC error is also wrong on this socket: a pending request can
    be resolved by the FIRST client to answer, so an observer's error could abort work the
    operator initiated in the desktop app. Right for a turn's owner, wrong for a watcher.
    """

    def client(self, incoming: list[dict]) -> codex_stream.ObserverClient:
        client = codex_stream.ObserverClient.__new__(codex_stream.ObserverClient)
        client.observed_requests = []
        client.sent: list[dict] = []
        client.queue = list(incoming)
        client.send_json = client.sent.append
        return client

    def drain(self, client, count: int) -> list[dict]:
        def base_recv(_self=None):
            return client.queue.pop(0)
        with mock.patch.object(codex_stream.autobridge.JsonRpcWebSocketClient,
                               "recv_json", base_recv):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                got = [client.recv_json() for _ in range(count)]
        self.stderr = captured.getvalue()
        return got

    def test_an_approval_request_produces_zero_response_frames(self) -> None:
        approval = {"id": "srv-1", "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "T1", "turnId": "U1", "command": "rm -rf /"}}
        event = {"method": "turn/completed", "params": {}}
        client = self.client([approval, event])
        delivered = self.drain(client, 1)

        self.assertEqual([event], delivered, "the request must not surface as an event")
        self.assertEqual([], client.sent,
                         "an observer must send NO frame at all -- not a result, not an error")
        self.assertEqual(["item/commandExecution/requestApproval"], client.observed_requests)
        self.assertIn("srv-1", self.stderr, "the request must be reported with its id")
        self.assertIn("T1", self.stderr, "and with the thread it belongs to")

    def test_a_request_interleaved_before_a_response_is_also_unanswered(self) -> None:
        # the initialize/resume window, where the base client would answer {}
        approval = {"id": "srv-2", "method": "item/fileChange/requestApproval", "params": {}}
        response = {"id": "llm-collab-1", "result": {"ok": True}}
        client = self.client([approval, response])
        self.assertEqual([response], self.drain(client, 1))
        self.assertEqual([], client.sent)

    def test_every_generated_server_request_method_is_left_unanswered(self) -> None:
        methods = [
            "account/chatgptAuthTokens/refresh", "applyPatchApproval",
            "attestation/generate", "execCommandApproval",
            "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
            "item/permissions/requestApproval", "item/tool/call",
            "item/tool/requestUserInput", "mcpServer/elicitation/request",
        ]
        incoming = [{"id": f"srv-{i}", "method": m, "params": {}}
                    for i, m in enumerate(methods)]
        incoming.append({"method": "turn/completed", "params": {}})
        client = self.client(incoming)
        self.drain(client, 1)
        self.assertEqual([], client.sent, "no member of the union may be answered")
        self.assertEqual(sorted(methods), sorted(client.observed_requests))

    def test_a_plain_notification_is_passed_through_untouched(self) -> None:
        note = {"method": "item/agentMessage/delta", "params": {"delta": "hi"}}
        client = self.client([note])
        self.assertEqual([note], self.drain(client, 1))
        self.assertEqual([], client.sent)

    def test_a_request_with_id_zero_is_still_treated_as_a_request(self) -> None:
        """0 is a legal JSON-RPC id, so the check must be is-not-None, not truthiness.

        Under truthiness, id 0 is falsy: the request falls through as if it were a
        notification and is handed to the caller as an event, which for a delivery client
        is the difference between refusing and silently ignoring an approval.
        """
        request = {"id": 0, "method": "item/tool/call", "params": {}}
        event = {"method": "turn/completed", "params": {}}
        client = self.client([request, event])
        delivered = self.drain(client, 1)
        self.assertEqual([event], delivered,
                         "a request with id 0 must not surface as an event")
        self.assertEqual(["item/tool/call"], client.observed_requests)
        self.assertEqual([], client.sent)

    def test_the_cli_entry_point_is_intact(self) -> None:
        # unit tests all passed once while main() raised NameError on a deleted helper
        import subprocess
        result = subprocess.run([sys.executable, str(ROOT / "bin" / "codex_stream.py"), "--help"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr[:300])
        self.assertIn("--seconds", result.stdout)


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
        self.assertIn("not a result, not an error", source)


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
