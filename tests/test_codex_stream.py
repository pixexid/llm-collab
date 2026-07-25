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
import time
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
        "runtime_family": "codex_app", "session_id": f"SESSION-{chat}",
    }), encoding="utf-8")


class ResolveThreadTest(unittest.TestCase):
    """Selection, delegation, and the endpoint — nothing else.

    Seven test classes and forty-seven tests used to live here, exercising a
    reimplementation of exact-binding resolution: record-versus-location identity, a
    backing-session check, a runtime-family gate, per-binding byte budgets. Six review rounds
    found holes in all of it, each fix exposing the next adjacent one.

    That machinery is gone. resolve_thread() now delegates to
    autobridge.resolve_exact_dispatch_target(), which production dispatch uses and which
    already enforces every one of those invariants -- and enforces them in one place with its
    own suite. So these tests assert what remains ours: validating the selectors, choosing
    WHICH chat when the caller did not name one, and reading the endpoint. Crucially they also
    assert THAT we delegate, and with which arguments, which the previous forty-seven could
    not do because there was nothing to delegate to.
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

    def binding_file(self, project: str, chat: str, agent: str = "codex") -> None:
        """Only the FILE needs to exist; its contents are the audited path's business."""
        path = self.root / project / chat / f"{agent}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")

    def session(self, thread="t1", home="/Users/someone/.codex", updated="2026-07-25T00:00:00Z"):
        return {"runtime": {"session_id": thread, "home": home}, "updated_utc": updated}

    def delegate(self, table: dict):
        """Stub the audited resolver. table maps chat -> session, or chat -> None for dead."""
        calls = []

        def resolver(project_id, chat_id, agent_id):
            calls.append((project_id, chat_id, agent_id))
            found = table.get(chat_id)
            return (found, None) if found else (None, "exact_binding_not_dispatchable")

        patcher = mock.patch.object(codex_stream.autobridge, "resolve_exact_dispatch_target",
                                   side_effect=resolver)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    # --- delegation -----------------------------------------------------------------

    def test_a_named_chat_delegates_with_exactly_the_named_triple(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        calls = self.delegate({"CHAT-A": self.session()})
        thread, provenance, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual([("amiga", "CHAT-A", "codex")], calls)
        self.assertEqual("t1", thread)
        self.assertEqual("amiga/CHAT-A", provenance)
        self.assertEqual("/Users/someone/.codex", home)

    def test_a_named_chat_that_is_not_live_is_fatal_with_the_reason(self) -> None:
        """Named exactly, so substituting another chat would be worse than failing."""
        self.binding_file("amiga", "CHAT-A")
        self.delegate({})
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))
        message = str(caught.exception)
        self.assertIn("CHAT-A", message)
        self.assertIn("exact_binding_not_dispatchable", message,
                      "the audited path's own reason must reach the operator")

    def test_broad_selection_excludes_dead_bindings_instead_of_failing(self) -> None:
        """deactivate_session() leaves the binding behind deliberately.

        So one ordinary deactivation must not break `--chat last` for that agent forever --
        which an earlier revision of mine did, by treating any dead candidate as fatal.
        """
        for chat in ("CHAT-DEAD", "CHAT-LIVE"):
            self.binding_file("amiga", chat)
        self.delegate({"CHAT-LIVE": self.session(thread="live-thread")})
        thread, provenance, _home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("live-thread", thread)
        self.assertIn("CHAT-LIVE", provenance)

    def test_broad_selection_refuses_when_several_are_live(self) -> None:
        for chat in ("CHAT-A", "CHAT-B"):
            self.binding_file("amiga", chat)
        self.delegate({"CHAT-A": self.session(thread="a"), "CHAT-B": self.session(thread="b")})
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        message = str(caught.exception)
        self.assertIn("CHAT-A", message)
        self.assertIn("CHAT-B", message)

    def test_chat_last_takes_the_newest_of_several_live_bindings(self) -> None:
        for chat in ("CHAT-OLD", "CHAT-NEW"):
            self.binding_file("amiga", chat)
        self.delegate({
            "CHAT-OLD": self.session(thread="old", updated="2026-07-01T00:00:00Z"),
            "CHAT-NEW": self.session(thread="new", updated="2026-07-25T00:00:00Z"),
        })
        thread, _prov, _home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("new", thread)

    def test_no_live_binding_at_all_is_reported_clearly(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        self.delegate({})
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("no live exactly-bound", str(caught.exception))

    def test_only_chats_with_a_binding_for_this_agent_are_considered(self) -> None:
        self.binding_file("amiga", "CHAT-MINE", agent="codex")
        self.binding_file("amiga", "CHAT-THEIRS", agent="claude")
        calls = self.delegate({"CHAT-MINE": self.session()})
        codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual([("amiga", "CHAT-MINE", "codex")], calls,
                         "another agent's chat must not even be offered to the resolver")

    # --- the endpoint ---------------------------------------------------------------

    def test_the_runtime_home_comes_from_the_selected_session(self) -> None:
        # a hardcoded default made this work on exactly one machine
        self.binding_file("amiga", "CHAT-A")
        self.delegate({"CHAT-A": self.session(home="/Users/elsewhere/.codex-alt")})
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("/Users/elsewhere/.codex-alt", home)

    def test_an_explicit_runtime_home_overrides_the_session(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        self.delegate({"CHAT-A": self.session(home="/Users/elsewhere/.codex-alt")})
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A",
                      runtime_home="/tmp/override"))
        self.assertEqual("/tmp/override", home)

    def test_a_session_without_a_home_yields_none_rather_than_a_guess(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        self.delegate({"CHAT-A": self.session(home=None)})
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertIsNone(home)

    def test_a_session_without_a_runtime_thread_is_refused(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        self.delegate({"CHAT-A": self.session(thread=None)})
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertIn("no runtime thread id", str(caught.exception))

    def test_there_is_no_hardcoded_home_default_left(self) -> None:
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn('default="/Users/', source)

    # --- direct-thread mode ---------------------------------------------------------

    def test_thread_mode_requires_a_runtime_home(self) -> None:
        """The documented `--thread ... --raw` invocation could never work without it.

        There is no binding to read a home from, and discovery matches CODEX_HOME exactly, so
        the old code reached the endpoint lookup with None and always exited.
        """
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(thread="019f-abc"))
        self.assertIn("--runtime-home", str(caught.exception))

    def test_thread_mode_with_a_home_bypasses_binding_lookup_entirely(self) -> None:
        calls = self.delegate({})
        thread, provenance, home = codex_stream.resolve_thread(
            self.args(thread="019f-abc", runtime_home="/tmp/home"))
        self.assertEqual(("019f-abc", "--thread", "/tmp/home"), (thread, provenance, home))
        self.assertEqual([], calls, "no resolution is needed when the thread is named")

    # --- selectors ------------------------------------------------------------------

    def test_omitting_project_is_refused_outright(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        self.binding_file("nuvyr", "CHAT-B")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex"))
        self.assertIn("--project is required", str(caught.exception))

    def test_an_unregistered_project_is_refused(self) -> None:
        self.binding_file("ghost", "CHAT-A")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="ghost"))
        self.assertIn("not registered", str(caught.exception))

    def test_an_unreadable_or_empty_registry_fails_closed(self) -> None:
        self.binding_file("ghost", "CHAT-A")
        with mock.patch.object(codex_stream, "registered_project_ids", return_value=set()):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="ghost"))
        self.assertIn("cannot be verified", str(caught.exception))

    def test_a_traversing_selector_cannot_reach_another_project(self) -> None:
        (self.root / "amiga").mkdir(parents=True)
        self.binding_file("nuvyr", "CHAT-A")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga/../nuvyr", chat="CHAT-A"))
        self.assertIn("one literal name", str(caught.exception))

    def test_glob_metacharacters_and_empty_selectors_are_refused(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        for field, value in (("project", "*"), ("project", ""), ("chat", "CHAT-[A]"),
                             ("chat", "*"), ("chat", ""), ("chat", "."), ("chat", "..")):
            with self.subTest(field=field, value=value):
                kw = {"agent": "codex", "project": "amiga"}
                kw[field] = value
                with self.assertRaises(SystemExit):
                    codex_stream.resolve_thread(self.args(**kw))

    def test_a_traversing_agent_selector_is_refused(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="../amiga/CHAT-A/codex", project="amiga", chat="CHAT-A"))

    # --- the enumeration budget -----------------------------------------------------

    def test_every_entry_consumes_the_scan_budget_before_filtering(self) -> None:
        """Charging only directories let an untrusted tree spend unbounded work on entries
        that were then discarded."""
        (self.root / "amiga").mkdir(parents=True)
        for i in range(5):
            (self.root / "amiga" / f"junk-{i}.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SCANNED_CHATS", 1):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("more than", str(caught.exception))

    def test_a_named_chat_needs_no_enumeration_at_all(self) -> None:
        self.binding_file("amiga", "CHAT-A")
        self.delegate({"CHAT-A": self.session()})
        with mock.patch.object(codex_stream, "MAX_SCANNED_CHATS", 0):
            thread, _p, _h = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("t1", thread)


class FileChangePathsTest(unittest.TestCase):
    """A fileChange item has NO top-level path: the paths live in changes[]."""

    def line(self, item: dict) -> str:
        return codex_stream.describe("item/started", {"item": item})

    def test_a_single_file_edit_names_the_file(self) -> None:
        line = self.line({"type": "fileChange",
                          "changes": [{"path": "bin/deliver.py", "kind": "update"}]})
        self.assertIn("bin/deliver.py", line)

    def test_a_multi_file_edit_names_every_file(self) -> None:
        line = self.line({"type": "fileChange", "changes": [
            {"path": "bin/a.py"}, {"path": "bin/b.py"}, {"path": "tests/c.py"}]})
        self.assertIn("3 files", line)
        for path in ("bin/a.py", "bin/b.py", "tests/c.py"):
            self.assertIn(path, line)

    def test_a_protocol_valid_item_never_prints_an_empty_edit_line(self) -> None:
        """The defect: reading item["path"] printed `edit ` for every valid item.

        The tool reported that an edit happened while withholding the one fact that matters.
        """
        line = self.line({"type": "fileChange",
                          "changes": [{"path": "bin/deliver.py"}]})
        self.assertNotEqual("  edit ", line.rstrip())
        self.assertNotIn("edit \n", line)

    def test_an_item_with_no_usable_paths_says_so_instead_of_nothing(self) -> None:
        for item in ({"type": "fileChange", "changes": []},
                     {"type": "fileChange", "changes": "not-a-list"},
                     {"type": "fileChange"}):
            with self.subTest(item=item):
                self.assertIn("unspecified", self.line(item))


class MessageStartDetectionTest(unittest.TestCase):
    """Only item/started proves we were there from the beginning."""

    def test_an_agent_message_start_is_reported(self) -> None:
        self.assertEqual("msg-1", codex_stream.message_started_id(
            "item/started", {"item": {"type": "agentMessage", "id": "msg-1"}}))

    def test_a_delta_is_not_a_start(self) -> None:
        """The defect: the first delta SEEN was treated as the first delta SENT."""
        self.assertIsNone(codex_stream.message_started_id(
            "item/agentMessage/delta", {"itemId": "msg-1", "delta": "tail only"}))

    def test_a_non_message_item_start_is_not_reported(self) -> None:
        for kind in ("commandExecution", "fileChange", "reasoning"):
            with self.subTest(kind=kind):
                self.assertIsNone(codex_stream.message_started_id(
                    "item/started", {"item": {"type": kind, "id": "x"}}))

    def test_a_start_without_an_id_is_not_reported(self) -> None:
        self.assertIsNone(codex_stream.message_started_id(
            "item/started", {"item": {"type": "agentMessage"}}))

    def test_the_loop_populates_the_set_only_from_starts(self) -> None:
        # structural: the delta branch must not add to streamed_from_start
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        delta_branch = source[source.index('if method == "item/agentMessage/delta":'):
                              source.index('if method == "item/completed"')]
        self.assertNotIn("streamed_from_start", delta_branch)
        self.assertIn("started_id = message_started_id(method, params)", source)


class MessageReconciliationTest(unittest.TestCase):
    """A message that began before subscription must be recovered, once.

    Subscribing mid-message delivers only the later deltas while the completion payload
    carries the whole text. Always discarding that completion showed a suffix -- or nothing,
    when every delta preceded subscription. Always printing it would duplicate every message.
    """

    def test_a_message_followed_from_its_first_delta_is_not_reprinted(self) -> None:
        seen = {"msg-1"}
        self.assertIsNone(codex_stream.unstreamed_message_text(
            {"id": "msg-1", "text": "hello world"}, seen))
        self.assertEqual(set(), seen, "the id is consumed so it cannot suppress a later item")

    def test_a_message_that_began_before_subscription_is_recovered(self) -> None:
        self.assertEqual("the whole message", codex_stream.unstreamed_message_text(
            {"id": "msg-2", "text": "the whole message"}, set()))

    def test_a_message_whose_every_delta_preceded_subscription_is_recovered(self) -> None:
        # the case where the default view showed NOTHING at all
        self.assertEqual("entirely missed", codex_stream.unstreamed_message_text(
            {"id": "msg-3", "text": "entirely missed"}, {"other-msg"}))

    def test_an_empty_completion_reports_nothing(self) -> None:
        self.assertIsNone(codex_stream.unstreamed_message_text({"id": "m", "text": ""}, set()))

    def test_a_completion_with_no_id_still_recovers_its_text(self) -> None:
        self.assertEqual("no id", codex_stream.unstreamed_message_text({"text": "no id"}, set()))

    def test_two_messages_are_tracked_independently(self) -> None:
        seen = {"msg-a"}
        self.assertIsNone(codex_stream.unstreamed_message_text({"id": "msg-a", "text": "A"}, seen))
        self.assertEqual("B", codex_stream.unstreamed_message_text({"id": "msg-b", "text": "B"}, seen))


class DeadlineTest(unittest.TestCase):
    """The deadline is absolute, so nothing on the wire can extend it."""

    def client(self, frames=()):
        made = codex_stream.ObserverClient.__new__(codex_stream.ObserverClient)
        made.observed_requests = []
        made.read_deadline = None
        made.timeout_seconds = 5
        made.sock = mock.Mock()
        made.sent = []
        made.send_json = made.sent.append
        made._send_frame = lambda payload, opcode=0x1: None
        made.queue = list(frames)

        def recv_frame():
            if not made.queue:
                raise ConnectionError("closed")
            return made.queue.pop(0)

        made._recv_frame = recv_frame
        return made

    def test_a_near_deadline_shortens_the_socket_wait(self) -> None:
        made = self.client()
        made.set_deadline(time.monotonic() + 0.1)
        made._clamp_socket()
        self.assertLessEqual(made.sock.settimeout.call_args[0][0], 0.1)

    def test_no_deadline_uses_the_idle_cap(self) -> None:
        made = self.client()
        made.set_deadline(None)
        made._clamp_socket()
        self.assertEqual(5, made.sock.settimeout.call_args[0][0])

    def test_a_distant_deadline_still_respects_the_idle_cap(self) -> None:
        made = self.client()
        made.set_deadline(time.monotonic() + 600)
        made._clamp_socket()
        self.assertEqual(5, made.sock.settimeout.call_args[0][0])

    def test_an_exhausted_window_never_becomes_a_blocking_wait(self) -> None:
        # settimeout(0) makes the socket non-blocking, which is a different failure
        made = self.client()
        made.set_deadline(time.monotonic() - 3)
        made._clamp_socket()
        self.assertGreater(made.sock.settimeout.call_args[0][0], 0)

    def test_a_ping_storm_cannot_extend_the_deadline(self) -> None:
        """The reported repro: pings are consumed INSIDE the frame loop.

        With the base client's loop, a peer sending them steadily reset the wait each time and
        a 0.1s budget returned after roughly 0.21s. The deadline is absolute now, so a ping
        costs time against it.
        """
        pings = [(0x9, b"") for _ in range(500)]
        made = self.client(pings)
        made.set_deadline(time.monotonic() - 0.001)  # already expired
        with self.assertRaises(TimeoutError):
            made.recv_json()

    def test_an_expired_deadline_raises_before_reading_any_frame(self) -> None:
        made = self.client([(0x1, json.dumps({"method": "turn/completed"}).encode())])
        made.set_deadline(time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            made.recv_json()
        self.assertEqual(1, len(made.queue), "the frame must not have been consumed")

    def test_a_ping_is_answered_with_a_pong_and_the_loop_continues(self) -> None:
        frames = [(0x9, b"hb"), (0x1, json.dumps({"method": "turn/completed"}).encode())]
        made = self.client(frames)
        sent_opcodes = []
        made._send_frame = lambda payload, opcode=0x1: sent_opcodes.append(opcode)
        self.assertEqual("turn/completed", made.recv_json()["method"])
        self.assertEqual([0xA], sent_opcodes, "a ping must be ponged")


class SetupBoundaryTest(unittest.TestCase):
    """Setup is inside the deadline, and nothing emitted during it is lost."""

    def client(self, frames):
        made = codex_stream.ObserverClient.__new__(codex_stream.ObserverClient)
        made.observed_requests = []
        made.read_deadline = None
        made.pending_events = []
        made.timeout_seconds = 5
        made.counter = 0
        made.sock = mock.Mock()
        made.sent = []
        made.send_json = made.sent.append
        made._send_frame = lambda payload, opcode=0x1: None
        made.queue = [(0x1, json.dumps(m).encode()) for m in frames]

        def recv_frame():
            if not made.queue:
                raise ConnectionError("closed")
            return made.queue.pop(0)

        made._recv_frame = recv_frame
        return made

    def test_a_notification_arriving_before_the_response_is_buffered_not_dropped(self) -> None:
        """The subscription boundary: turn/started and the first items live exactly here.

        The inherited request() loop discards non-matching messages, so an event emitted after
        this socket was registered for the thread and before thread/resume answered vanished.
        """
        early = {"method": "turn/started", "params": {"turn": {"id": "u1"}}}
        response = {"id": "llm-collab-1", "result": {}}
        client = self.client([early, response])
        client.request("thread/resume", {"threadId": "T1"})

        self.assertEqual([early], client.pending_events)
        self.assertEqual([early], client.take_pending_events())
        self.assertEqual([], client.pending_events, "draining must not duplicate")

    def test_several_early_notifications_keep_their_order(self) -> None:
        first = {"method": "turn/started", "params": {}}
        second = {"method": "item/started", "params": {"item": {"type": "reasoning"}}}
        client = self.client([first, second, {"id": "llm-collab-1", "result": {}}])
        client.request("thread/resume", {"threadId": "T1"})
        self.assertEqual([first, second], client.take_pending_events())

    def test_a_request_error_still_raises(self) -> None:
        client = self.client([{"id": "llm-collab-1",
                               "error": {"code": -1, "message": "no rollout"}}])
        with self.assertRaises(RuntimeError) as caught:
            client.request("thread/resume", {"threadId": "T1"})
        self.assertIn("no rollout", str(caught.exception))

    def test_a_refused_server_request_during_setup_is_not_buffered_as_an_event(self) -> None:
        approval = {"id": "srv-1", "method": "item/commandExecution/requestApproval",
                    "params": {}}
        client = self.client([approval, {"id": "llm-collab-1", "result": {}}])
        import contextlib as _c, io as _io
        with _c.redirect_stderr(_io.StringIO()):
            client.request("thread/resume", {"threadId": "T1"})
        self.assertEqual([], client.pending_events,
                         "a server request is refused by policy, not replayed as an event")
        self.assertEqual(["item/commandExecution/requestApproval"], client.observed_requests)

    def test_the_deadline_is_installed_before_initialize(self) -> None:
        """A server that stalls answering initialize must not get the full idle timeout."""
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        set_at = source.index("client.set_deadline(deadline)")
        initialize_at = source.index('client.request(\n            "initialize"')
        self.assertLess(set_at, initialize_at,
                        "the deadline must be set before the first blocking request")

    def test_the_loop_replays_buffered_events_before_reading_new_ones(self) -> None:
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertIn("replay = client.take_pending_events()", source)
        self.assertIn("replay.pop(0) if replay else client.recv_json()", source)


class StreamLoopContractTest(unittest.TestCase):
    """Structural assertions about the loop the connector flagged."""

    def source(self) -> str:
        return (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")

    def test_the_whole_response_is_no_longer_accumulated(self) -> None:
        # appending every delta retained the full response to read only its truthiness
        self.assertNotIn("pending_text", self.source())

    def test_the_deadline_is_monotonic(self) -> None:
        source = self.source()
        self.assertIn("time.monotonic()", source)
        self.assertNotIn("time.time() + args.seconds", source)

    def test_the_receive_wait_is_bounded_by_an_absolute_deadline(self) -> None:
        source = self.source()
        self.assertNotIn("set_read_timeout", source,
                         "the per-iteration timeout was replaced by an absolute deadline")
        self.assertIn("client.set_deadline(deadline)", source)

    def test_the_recovery_hint_names_no_command_that_does_not_exist(self) -> None:
        """The old hint printed a command that exits `Unknown agent` in this repo."""
        source = self.source()
        self.assertNotIn("pm2_watchers.py start --agent codex-appserver", source)


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

    The base client refuses with a correlated JSON-RPC error, which is right for a connection
    that OWNS a turn -- silence there would hang it. (Before #308 it sent {"result": {}}, an
    unauthorized success envelope invalid for all ELEVEN members of the experimental
    ServerRequest union, none of whose response schemas can be satisfied by an empty object.
    Some CLIENT-request responses in the same bundle can be, which is why the claim is about
    the server-request union rather than the bundle.)

    An automatic error is wrong on THIS socket: a pending request can be resolved by the first
    client to answer, so an observer's error could abort work the operator initiated in the
    desktop app. Right for a turn's owner, wrong for a watcher.
    """

    def client(self, incoming: list[dict]) -> codex_stream.ObserverClient:
        """A client whose frame source is a queue, since it owns its own frame loop now."""
        client = codex_stream.ObserverClient.__new__(codex_stream.ObserverClient)
        client.observed_requests = []
        client.read_deadline = None
        client.timeout_seconds = 5
        client.sock = mock.Mock()
        client.sent: list[dict] = []
        client.queue = [json.dumps(m).encode("utf-8") for m in incoming]
        client.send_json = client.sent.append
        client._send_frame = lambda payload, opcode=0x1: None

        def recv_frame():
            if not client.queue:
                raise ConnectionError("websocket closed")
            return 0x1, client.queue.pop(0)

        client._recv_frame = recv_frame
        return client

    def drain(self, client, count: int) -> list[dict]:
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
        # the initialize/resume window: silence must hold here too, not only in the event
        # loop. (The pre-#308 base client answered {} in this window; it refuses now, and an
        # observer must do neither.)
        approval = {"id": "srv-2", "method": "item/fileChange/requestApproval", "params": {}}
        response = {"id": "llm-collab-1", "result": {"ok": True}}
        client = self.client([approval, response])
        self.assertEqual([response], self.drain(client, 1))
        self.assertEqual([], client.sent)

    def test_every_generated_server_request_method_is_left_unanswered(self) -> None:
        # All ELEVEN members of the union this client opts into. Without --experimental the
        # generated union has ten; this client initializes with experimentalApi:true, so
        # currentTime/read belongs here. Asserting ten and calling it every member was the same
        # false completeness claim I had to correct in #308.
        methods = [
            "account/chatgptAuthTokens/refresh", "applyPatchApproval",
            "attestation/generate", "currentTime/read", "execCommandApproval",
            "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
            "item/permissions/requestApproval", "item/tool/call",
            "item/tool/requestUserInput", "mcpServer/elicitation/request",
        ]
        self.assertEqual(11, len(methods), "the experimental union has eleven members")
        incoming = [{"id": f"srv-{i}", "method": m, "params": {}}
                    for i, m in enumerate(methods)]
        incoming.append({"method": "turn/completed", "params": {}})
        client = self.client(incoming)
        self.drain(client, 1)
        self.assertEqual([], client.sent, "no member of the union may be answered")
        self.assertEqual(sorted(methods), sorted(client.observed_requests))

    def test_this_client_opts_into_the_experimental_api(self) -> None:
        # if it stopped doing so, the ten-member union would become the correct matrix
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertIn("experimentalApi", source)

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


class InterruptExitCodeTest(unittest.TestCase):
    """Ctrl-C during streaming must exit 130, not 0.

    The stream loop caught KeyboardInterrupt and broke, so main() returned normally and
    the outer 130 handler never ran -- supervision saw a clean exit for an interrupted
    view, which is the same ambiguity the transport-failure fix removed.
    """

    def test_keyboard_interrupt_is_not_swallowed_by_the_stream_loop(self) -> None:
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        loop = source[source.index("        while True:"):source.index("    if text_line_open:")]
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
