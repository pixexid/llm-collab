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
        for patcher in (
            mock.patch.object(codex_stream, "BINDINGS_DIR", self.root),
            # projects.json lives outside these fixtures; the registry check is
            # exercised by its own tests rather than gating every other case
            mock.patch.object(codex_stream, "registered_project_ids",
                              return_value={"amiga", "nuvyr"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, **kw) -> SimpleNamespace:
        base = {"agent": None, "project": None, "chat": None, "thread": None,
                "runtime_home": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_exact_project_and_chat_resolves(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        thread, provenance, _home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-AAA"))
        self.assertEqual("thread-1", thread)
        self.assertIn("CHAT-AAA", provenance)

    def test_omitting_project_is_refused_outright(self) -> None:
        """Cross-project selection is not an opt-in ambiguity mode.

        Enumerating every project meant `--chat last` could select a worker thread belonging
        to a project the caller never named.
        """
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        binding(self.root, "nuvyr", "CHAT-BBB", "codex", "thread-2")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex"))
        self.assertIn("--project is required", str(caught.exception))

    def test_an_unregistered_project_is_refused(self) -> None:
        """Existing as a directory is not the same as being a registered project."""
        binding(self.root, "ghost", "CHAT-AAA", "codex", "thread-1")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="ghost"))
        self.assertIn("not registered", str(caught.exception))

    def test_ambiguous_lookup_within_one_project_refuses_and_names_both(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        binding(self.root, "amiga", "CHAT-BBB", "codex", "thread-2")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        message = str(caught.exception)
        self.assertIn("CHAT-AAA", message)
        self.assertIn("CHAT-BBB", message, "both candidates must be named")

    def test_chat_last_accepts_ambiguity_and_takes_the_newest(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1",
                updated="2026-07-01T00:00:00+00:00")
        binding(self.root, "amiga", "CHAT-BBB", "codex", "thread-2",
                updated="2026-07-25T00:00:00+00:00")
        thread, _, _home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("thread-2", thread)

    def test_parked_binding_loses_to_an_active_one(self) -> None:
        binding(self.root, "amiga", "CHAT-OLD", "codex", "thread-parked", status="parked")
        binding(self.root, "amiga", "CHAT-NEW", "codex", "thread-active", status="active")
        thread, _, _home = codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertEqual("thread-active", thread)

    def test_binding_without_a_runtime_session_id_is_not_a_candidate(self) -> None:
        path = self.root / "amiga" / "CHAT-AAA" / "codex.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"agent_id": "codex", "runtime_session_id": None}),
                        encoding="utf-8")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))

    def test_a_named_chat_that_does_not_exist_resolves_nothing(self) -> None:
        """Naming a chat must never fall back to a different one in the same project."""
        binding(self.root, "amiga", "CHAT-OTHER", "codex", "other-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-WANTED"))
        self.assertIn("no binding", str(caught.exception).lower())

    def test_project_without_chat_still_narrows_to_that_project(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "amiga-thread")
        binding(self.root, "nuvyr", "CHAT-B", "codex", "nuvyr-thread")
        thread, _, _home = codex_stream.resolve_thread(self.args(agent="codex", project="nuvyr"))
        self.assertEqual("nuvyr-thread", thread)

    def test_a_glob_metacharacter_in_a_chat_selector_matches_nothing(self) -> None:
        """A selector is a name, not a pattern.

        Interpolating selectors into Path.glob made `CHAT-[A]` a character class that
        resolved CHAT-A -- a caller could reach a thread it did not name.
        """
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-[A]"))

    def test_a_wildcard_project_selector_matches_nothing(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="*"))

    def test_a_wildcard_chat_selector_matches_nothing(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="*"))

    def test_a_question_mark_in_an_agent_selector_matches_nothing(self) -> None:
        # the agent lands in the FILENAME, which was globbed too
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="code?", project="amiga", chat="CHAT-A"))

    def test_a_traversing_project_selector_cannot_reach_another_project(self) -> None:
        """The intermediate directory must exist, or the OS never traverses `..`.

        `--project 'amiga/../nuvyr'` resolved the nuvyr thread, and record_matches_path
        could not catch it: it compares the record against the LEXICAL destination
        component, nuvyr, not against what the caller named.
        """
        (self.root / "amiga").mkdir(parents=True)          # intermediate must be present
        binding(self.root, "nuvyr", "CHAT-A", "codex", "other-project-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga/../nuvyr", chat="CHAT-A"))
        self.assertIn("one literal name", str(caught.exception))

    def test_a_traversing_chat_selector_cannot_reach_another_chat(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        binding(self.root, "amiga", "CHAT-B", "codex", "thread-b")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A/../CHAT-B"))
        self.assertIn("one literal name", str(caught.exception))

    def test_a_traversing_agent_selector_is_refused(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="../amiga/CHAT-A/codex", project="amiga", chat="CHAT-A"))

    def test_dot_and_dotdot_and_empty_selectors_are_refused(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        for bad in (".", "..", ""):
            with self.subTest(selector=bad):
                with self.assertRaises(SystemExit):
                    codex_stream.resolve_thread(
                        self.args(agent="codex", project="amiga", chat=bad))

    def test_an_empty_project_selector_is_refused_too(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project=""))

    def test_last_remains_the_one_reserved_control_value(self) -> None:
        binding(self.root, "amiga", "CHAT-A", "codex", "thread-a",
                updated="2026-07-01T00:00:00+00:00")
        binding(self.root, "amiga", "CHAT-B", "codex", "thread-b",
                updated="2026-07-25T00:00:00+00:00")
        thread, _, _home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("thread-b", thread)

    def test_explicit_thread_bypasses_binding_lookup(self) -> None:
        thread, provenance, _home = codex_stream.resolve_thread(self.args(thread="thread-x"))
        self.assertEqual("thread-x", thread)
        self.assertEqual("--thread", provenance)


class EndpointFromBindingTest(unittest.TestCase):
    """The CODEX_HOME to discover comes from the selected binding, not from a default.

    The flag defaulted to one author's home, and discovery matches CODEX_HOME exactly, so any
    binding under a custom or secondary home either found no endpoint or connected to the
    wrong server and failed thread/resume.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for patcher in (
            mock.patch.object(codex_stream, "BINDINGS_DIR", self.root),
            mock.patch.object(codex_stream, "registered_project_ids",
                              return_value={"amiga", "nuvyr"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, **kw):
        base = {"agent": None, "project": None, "chat": None, "thread": None,
                "runtime_home": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def write(self, project, chat, agent, thread, home) -> None:
        path = self.root / project / chat / f"{agent}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"agent_id": agent, "project_id": project, "chat_id": chat,
                   "runtime_session_id": thread, "status": "active"}
        if home is not None:
            payload["runtime_home"] = home
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_the_bindings_own_runtime_home_is_returned(self) -> None:
        self.write("amiga", "CHAT-A", "codex", "t1", "/Users/someone-else/.codex-alt")
        _thread, _prov, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("/Users/someone-else/.codex-alt", home,
                         "the binding's home must win over any built-in default")

    def test_an_explicit_override_wins_over_the_binding(self) -> None:
        self.write("amiga", "CHAT-A", "codex", "t1", "/Users/someone-else/.codex-alt")
        _thread, _prov, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A",
                      runtime_home="/tmp/override-home"))
        self.assertEqual("/tmp/override-home", home)

    def test_a_binding_without_a_home_returns_none_rather_than_a_guess(self) -> None:
        self.write("amiga", "CHAT-A", "codex", "t1", None)
        _thread, _prov, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertIsNone(home, "no home is better than the wrong machine's home")

    def test_there_is_no_hardcoded_home_default_left(self) -> None:
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn('default="/Users/', source,
                         "a hardcoded home default only works on one machine")


class FailClosedCandidateTest(unittest.TestCase):
    """An unreadable candidate is a lookup failure, never a silent skip.

    Discarding one can leave a partial set that looks unambiguous, so a concurrent
    non-atomic write to a SIBLING binding could suppress the ambiguity refusal and get the
    remaining thread watched.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for patcher in (
            mock.patch.object(codex_stream, "BINDINGS_DIR", self.root),
            mock.patch.object(codex_stream, "registered_project_ids", return_value={"amiga"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, **kw):
        base = {"agent": None, "project": None, "chat": None, "thread": None,
                "runtime_home": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_a_corrupt_sibling_cannot_make_the_set_look_unambiguous(self) -> None:
        binding(self.root, "amiga", "CHAT-GOOD", "codex", "good-thread")
        broken = self.root / "amiga" / "CHAT-TORN" / "codex.json"
        broken.parent.mkdir(parents=True)
        broken.write_text('{"agent_id": "codex", "runtime_sess', encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("unreadable", str(caught.exception))

    def test_an_oversized_binding_is_refused_before_parsing(self) -> None:
        big = self.root / "amiga" / "CHAT-BIG" / "codex.json"
        big.parent.mkdir(parents=True)
        big.write_text("[" + "0," * (codex_stream.MAX_BINDING_BYTES) + "0]", encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("limit", str(caught.exception))

    def test_too_many_chat_directories_fails_closed(self) -> None:
        with mock.patch.object(codex_stream, "MAX_SCANNED_CHATS", 3):
            for i in range(4):
                binding(self.root, "amiga", f"CHAT-{i}", "codex", f"t{i}")
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("scan budget", str(caught.exception))

    def test_a_named_chat_skips_the_scan_budget_entirely(self) -> None:
        with mock.patch.object(codex_stream, "MAX_SCANNED_CHATS", 1):
            for i in range(4):
                binding(self.root, "amiga", f"CHAT-{i}", "codex", f"t{i}")
            thread, _, _home = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-2"))
        self.assertEqual("t2", thread, "naming a chat needs no enumeration at all")


class ElideTest(unittest.TestCase):
    def test_a_shortened_command_is_marked_as_truncated(self) -> None:
        long_command = "rm -rf " + "a" * 400
        line = codex_stream.describe("item/started",
                                     {"item": {"type": "commandExecution",
                                               "command": long_command}})
        self.assertIn("truncated", line,
                      "a cut command must not read as the whole command")
        self.assertIn("+247 chars", line)

    def test_a_short_command_is_printed_whole_without_a_marker(self) -> None:
        line = codex_stream.describe("item/started",
                                     {"item": {"type": "commandExecution",
                                               "command": "pytest -q"}})
        self.assertIn("pytest -q", line)
        self.assertNotIn("truncated", line)


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
        for patcher in (
            mock.patch.object(codex_stream, "BINDINGS_DIR", self.root),
            # projects.json lives outside these fixtures; the registry check is
            # exercised by its own tests rather than gating every other case
            mock.patch.object(codex_stream, "registered_project_ids",
                              return_value={"amiga", "nuvyr"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, **kw):
        base = {"agent": None, "project": None, "chat": None, "thread": None,
                "runtime_home": None}
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
        thread, _, _home = codex_stream.resolve_thread(
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
