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
    """Selection and the endpoint, exercised against the REAL shared resolver.

    An earlier version of this suite stubbed `resolve_exact_dispatch_target`, which made every
    caller-owned boundary invisible: four regressed at once -- the family gate, which the shared
    resolver deliberately does not enforce; which refusals may be skipped during broad lookup;
    and where `runtime_home` and the `last` ordering are read from. A stub cannot show any of
    that, because the thing it replaces is what decides it.

    So these tests write real bindings and real sessions to disk and let the audited function
    run. It requires an exact binding whose project, chat and agent match its location; a
    session whose id, runtime family and runtime thread all match that binding; and that the
    session be dispatchable.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bindings = self.root / "bindings"
        self.sessions = self.root / "sessions"
        self.bindings.mkdir()
        self.sessions.mkdir()
        for patcher in (
            mock.patch.object(codex_stream, "BINDINGS_DIR", self.bindings),
            mock.patch.object(codex_stream.autobridge, "BINDINGS_DIR", self.bindings),
            mock.patch.object(codex_stream.autobridge, "SESSIONS_DIR", self.sessions),
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

    def bind(self, project="amiga", chat="CHAT-A", agent="codex", *, thread="t1",
             family="codex_app", home="/Users/someone/.codex", updated="2026-07-25T00:00:00Z",
             session_id=None, status="active", session_status="active", raw=None,
             session_project=None, session_chat=None, session_thread=None,
             session_family=None, lease=None, write_session=True,
             session_home="/Users/session-side/.codex", session_updated="2026-01-01T00:00:00Z"):
        """One coherent binding+session pair, with every field overridable to break it."""
        session_id = session_id or f"SESSION-{chat}"
        path = self.bindings / project / chat / f"{agent}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_text(json.dumps({
                "project_id": project, "chat_id": chat, "agent_id": agent,
                "session_id": session_id, "runtime_session_id": thread,
                "runtime_family": family, "runtime_home": home,
                "status": status, "updated_utc": updated,
            }), encoding="utf-8")
        if not write_session:
            return
        payload = {
            "session_id": session_id, "agent_id": agent,
            "project_id": session_project or project,
            "chat_id": session_chat or chat,
            "status": session_status,
            # deliberately DIFFERENT from the binding's home and timestamp, so that reading
            # the wrong record is detectable rather than coincidentally identical
            "runtime": {"family": session_family or family,
                        "session_id": session_thread or thread,
                        "home": session_home},
            "updated_utc": session_updated,
        }
        if lease:
            payload["lease_expires_utc"] = lease
        (self.sessions / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    # --- the happy path, through the audited function ------------------------------------

    def test_a_named_chat_resolves_through_the_real_resolver(self) -> None:
        self.bind()
        thread, provenance, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("t1", thread)
        self.assertEqual("amiga/CHAT-A", provenance)
        self.assertEqual("/Users/someone/.codex", home)

    def test_a_session_belonging_to_another_project_is_refused(self) -> None:
        # the audited function compares project and chat; my own check never did
        self.bind(session_project="nuvyr")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))

    def test_a_session_pointing_at_another_thread_is_refused(self) -> None:
        self.bind(session_thread="different")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))

    def test_a_deactivated_session_is_refused_when_named_exactly(self) -> None:
        self.bind(session_status="inactive")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertIn("not an exact live binding", str(caught.exception))

    def test_an_expired_lease_is_refused(self) -> None:
        self.bind(lease="2020-01-01T00:00:00Z")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))

    # --- one validated snapshot, not four reads ------------------------------------------

    def test_a_binding_swapped_after_validation_is_refused(self) -> None:
        """The reported TOCTOU, reproduced by swapping the real file mid-resolution.

        The resolver validates ONE snapshot. Re-reading the path afterwards for the family, the
        recency, then the thread id and home gave four later chances to see a different file --
        so a swap after validation returned a thread and home from a binding that had never been
        checked, while the session had been validated against the original.
        """
        self.bind(chat="CHAT-RACE", thread="good-thread", home="/tmp/good-home")
        path = self.bindings / "amiga" / "CHAT-RACE" / "codex.json"
        good = json.loads(path.read_text())
        real_resolver = codex_stream.autobridge.resolve_exact_dispatch_target

        def swap_after_validation(project_id, chat_id, agent_id):
            result = real_resolver(project_id, chat_id, agent_id)
            # the file changes the instant validation completes
            swapped = dict(good, runtime_session_id="wrong-thread",
                           runtime_home="/tmp/wrong-home")
            path.write_text(json.dumps(swapped), encoding="utf-8")
            return result

        with mock.patch.object(codex_stream.autobridge, "resolve_exact_dispatch_target",
                               side_effect=swap_after_validation):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(
                    self.args(agent="codex", project="amiga", chat="CHAT-RACE"))
        message = str(caught.exception)
        self.assertIn("changed between validation and use", message)
        self.assertIn("runtime_session_id", message,
                      "the disagreeing field must be named")

    def test_a_swapped_home_alone_is_also_refused(self) -> None:
        """Not every swap changes an id -- but every swap that changes what we USE must fail.

        Here only runtime_home moves, which no id comparison would catch, so the session_id is
        the field that betrays the substitution.
        """
        self.bind(chat="CHAT-RACE2", thread="good-thread", home="/tmp/good-home")
        path = self.bindings / "amiga" / "CHAT-RACE2" / "codex.json"
        good = json.loads(path.read_text())
        real_resolver = codex_stream.autobridge.resolve_exact_dispatch_target

        def swap(project_id, chat_id, agent_id):
            result = real_resolver(project_id, chat_id, agent_id)
            path.write_text(json.dumps(dict(good, session_id="SESSION-OTHER",
                                            runtime_home="/tmp/wrong-home")),
                            encoding="utf-8")
            return result

        with mock.patch.object(codex_stream.autobridge, "resolve_exact_dispatch_target",
                               side_effect=swap):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(
                    self.args(agent="codex", project="amiga", chat="CHAT-RACE2"))
        self.assertIn("session_id", str(caught.exception))

    def test_the_binding_is_read_once_per_chat(self) -> None:
        """Four reads were four chances to see a different file."""
        self.bind(chat="CHAT-A")
        self.bind(chat="CHAT-B")
        real_loader = codex_stream.autobridge.load_binding
        reads = []

        def counting(project_id, chat_id, agent_id):
            reads.append(chat_id)
            return real_loader(project_id, chat_id, agent_id)

        with mock.patch.object(codex_stream.autobridge, "load_binding", side_effect=counting):
            with self.assertRaises(SystemExit):
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        # Patching the module function catches BOTH readers, so two per chat is the floor: one
        # inside the audited resolver, one by us. Five per chat was the defect -- the resolver's,
        # then ours for the family, the recency, the thread id and the home.
        from collections import Counter
        per_chat = Counter(reads)
        self.assertEqual({"CHAT-A": 2, "CHAT-B": 2}, dict(per_chat),
                         f"one read of our own per chat, not four: {dict(per_chat)}")

    def test_a_successful_resolution_reads_the_binding_exactly_once(self) -> None:
        """Counted on the path that REACHES the end, which the ambiguity case never does.

        There is a second window after the cross-check: re-reading the path to pick the thread
        id and home would reopen it once more, and a swap in that window is invisible to a check
        that already ran. Two reads is the floor -- one inside the audited resolver, one ours.
        """
        self.bind(chat="CHAT-ONLY", thread="the-thread", home="/tmp/the-home")
        real_loader = codex_stream.autobridge.load_binding
        reads = []

        def counting(project_id, chat_id, agent_id):
            reads.append(chat_id)
            return real_loader(project_id, chat_id, agent_id)

        with mock.patch.object(codex_stream.autobridge, "load_binding", side_effect=counting):
            thread, _p, home = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-ONLY"))
        self.assertEqual("the-thread", thread)
        self.assertEqual("/tmp/the-home", home)
        self.assertEqual(2, len(reads),
                         f"the resolver's read plus exactly one of ours: {reads}")

    def test_a_swap_after_the_cross_check_cannot_change_what_is_used(self) -> None:
        """The second window: swap once the cross-check has already passed.

        With the metadata taken from the validated snapshot there is nothing left to reopen, so
        the swapped file cannot influence the thread id or the home.
        """
        self.bind(chat="CHAT-LATE", thread="the-thread", home="/tmp/the-home")
        path = self.bindings / "amiga" / "CHAT-LATE" / "codex.json"
        good = json.loads(path.read_text())
        real_loader = codex_stream.autobridge.load_binding
        calls = {"n": 0}

        def swap_on_third_read(project_id, chat_id, agent_id):
            calls["n"] += 1
            record = real_loader(project_id, chat_id, agent_id)
            if calls["n"] >= 2:
                # everything validated; now the file changes underneath us
                path.write_text(json.dumps(dict(good, runtime_session_id="wrong-thread",
                                                runtime_home="/tmp/wrong-home")),
                                encoding="utf-8")
            return record

        with mock.patch.object(codex_stream.autobridge, "load_binding",
                               side_effect=swap_on_third_read):
            thread, _p, home = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-LATE"))
        self.assertEqual("the-thread", thread, "a later swap must not reach the thread id")
        self.assertEqual("/tmp/the-home", home, "nor the home")

    def test_a_binding_that_vanishes_after_validation_is_refused(self) -> None:
        self.bind(chat="CHAT-GONE")
        path = self.bindings / "amiga" / "CHAT-GONE" / "codex.json"
        real_resolver = codex_stream.autobridge.resolve_exact_dispatch_target

        def unlink_after(project_id, chat_id, agent_id):
            result = real_resolver(project_id, chat_id, agent_id)
            path.unlink()
            return result

        with mock.patch.object(codex_stream.autobridge, "resolve_exact_dispatch_target",
                               side_effect=unlink_after):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(
                    self.args(agent="codex", project="amiga", chat="CHAT-GONE"))
        self.assertIn("unreadable", str(caught.exception))

    # --- the family gate, which the shared resolver does NOT enforce ---------------------

    def test_a_consistent_non_codex_family_is_still_refused(self) -> None:
        """The shared resolver only requires binding and session to AGREE on the family.

        A consistent claude_app pair therefore passes it -- correct for dispatch, which can
        reach several runtimes, and wrong here: that session id would go to a Codex App Server.
        """
        for family in ("claude_app", "gemini_cli"):
            with self.subTest(family=family):
                self.bind(chat=f"CHAT-{family}", family=family)
                with self.assertRaises(SystemExit) as caught:
                    codex_stream.resolve_thread(
                        self.args(agent="codex", project="amiga", chat=f"CHAT-{family}"))
                self.assertIn("codex_app", str(caught.exception))

    def test_the_family_gate_also_applies_during_broad_lookup(self) -> None:
        self.bind(chat="CHAT-CLAUDE", family="claude_app")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("codex_app", str(caught.exception))

    # --- which refusals may be skipped --------------------------------------------------

    def test_broad_lookup_skips_only_a_liveness_failure(self) -> None:
        self.bind(chat="CHAT-DEAD", session_status="inactive")
        self.bind(chat="CHAT-LIVE", thread="live-thread")
        thread, provenance, _home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("live-thread", thread)
        self.assertIn("CHAT-LIVE", provenance)

    def test_a_malformed_sibling_aborts_broad_lookup_instead_of_being_skipped(self) -> None:
        """An inconsistent workspace must not be stepped over.

        Treating every resolver refusal as "dead" meant a malformed binding beside a valid one
        silently handed the caller the valid thread -- which is only correct if the malformed
        one could not have been the intended target, and nothing here establishes that.
        """
        self.bind(chat="CHAT-TORN", raw='{"project_id": "amiga", "chat_id": ')
        self.bind(chat="CHAT-LIVE", thread="live-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="last"))
        self.assertIn("CHAT-TORN", str(caught.exception))

    def test_a_binding_that_lies_about_its_location_aborts_broad_lookup(self) -> None:
        self.bind(chat="CHAT-LIAR", session_chat="CHAT-OTHER")
        self.bind(chat="CHAT-LIVE", thread="live-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="last"))
        self.assertIn("CHAT-LIAR", str(caught.exception))

    def test_a_binding_whose_session_is_missing_aborts_broad_lookup(self) -> None:
        self.bind(chat="CHAT-ORPHAN", write_session=False)
        self.bind(chat="CHAT-LIVE", thread="live-thread")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="last"))

    # --- the binding is authoritative for home and recency ------------------------------

    def test_chat_last_orders_by_the_binding_timestamp(self) -> None:
        """`last` is advertised as the newest BINDING.

        Ordering by the session's own timestamp silently changed what the flag means; both
        sessions here carry the same session timestamp, so only the binding can decide.
        """
        # the session timestamps are INVERTED relative to the bindings, so ordering by the
        # wrong record picks the wrong chat rather than accidentally agreeing
        self.bind(chat="CHAT-OLD", thread="old", updated="2026-07-01T00:00:00Z",
                  session_updated="2026-07-30T00:00:00Z")
        self.bind(chat="CHAT-NEW", thread="new", updated="2026-07-25T00:00:00Z",
                  session_updated="2026-07-02T00:00:00Z")
        thread, _p, _h = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("new", thread)

    def test_the_binding_derived_runtime_home_is_used(self) -> None:
        """The binding carries runtime_home; the session's copy may differ or be absent.

        Reading the session's instead lost a binding-derived home entirely. The fixture gives
        the two different values so that reading the wrong one cannot pass by coincidence.
        """
        self.bind(home="/Users/binding-side/.codex-alt",
                  session_home="/Users/session-side/.codex")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("/Users/binding-side/.codex-alt", home)

    def test_a_binding_with_no_home_falls_back_to_the_session(self) -> None:
        self.bind(home=None, session_home="/Users/session-side/.codex")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("/Users/session-side/.codex", home)

    def test_an_explicit_home_overrides_the_binding(self) -> None:
        self.bind(home="/Users/elsewhere/.codex-alt")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A",
                      runtime_home="/tmp/override"))
        self.assertEqual("/tmp/override", home)

    def test_there_is_no_hardcoded_home_default_left(self) -> None:
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn('default="/Users/', source)

    # --- ambiguity ----------------------------------------------------------------------

    def test_two_live_bindings_refuse_and_name_both(self) -> None:
        self.bind(chat="CHAT-A", thread="a")
        self.bind(chat="CHAT-B", thread="b")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        message = str(caught.exception)
        self.assertIn("CHAT-A", message)
        self.assertIn("CHAT-B", message)

    def test_no_live_binding_is_reported_clearly(self) -> None:
        self.bind(session_status="inactive")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("no live exactly-bound", str(caught.exception))

    def test_only_this_agents_chats_are_considered(self) -> None:
        self.bind(chat="CHAT-MINE", agent="codex", thread="mine")
        self.bind(chat="CHAT-THEIRS", agent="claude", thread="theirs")
        thread, _p, _h = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("mine", thread)

    # --- direct-thread mode -------------------------------------------------------------

    def test_thread_mode_requires_a_runtime_home(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(thread="019f-abc"))
        self.assertIn("--runtime-home", str(caught.exception))

    def test_thread_mode_with_a_home_needs_no_binding(self) -> None:
        thread, provenance, home = codex_stream.resolve_thread(
            self.args(thread="019f-abc", runtime_home="/tmp/home"))
        self.assertEqual(("019f-abc", "--thread", "/tmp/home"), (thread, provenance, home))

    def test_the_documented_thread_example_carries_a_runtime_home(self) -> None:
        # the example could not work without it, since there is no binding to read one from
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        usage = source[source.index("Usage:"):source.index('"""', source.index("Usage:"))]
        for line in usage.splitlines():
            if "--thread" in line:
                block = usage[usage.index(line):]
                self.assertIn("--runtime-home", block,
                              "a documented invocation must be runnable as shown")
                break
        else:
            self.fail("no --thread example found in the usage block")

    # --- selectors ----------------------------------------------------------------------

    def test_omitting_project_is_refused(self) -> None:
        self.bind()
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex"))
        self.assertIn("--project is required", str(caught.exception))

    def test_an_unregistered_project_is_refused(self) -> None:
        self.bind(project="ghost")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="ghost"))
        self.assertIn("not registered", str(caught.exception))

    def test_an_empty_registry_fails_closed(self) -> None:
        self.bind(project="ghost")
        with mock.patch.object(codex_stream, "registered_project_ids", return_value=set()):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="ghost"))
        self.assertIn("cannot be verified", str(caught.exception))

    def test_traversal_and_glob_selectors_are_refused(self) -> None:
        self.bind()
        cases = [("project", "amiga/../nuvyr"), ("project", "*"), ("project", ""),
                 ("chat", "CHAT-[A]"), ("chat", "*"), ("chat", ""), ("chat", "."),
                 ("chat", ".."), ("chat", "CHAT-A/../CHAT-B")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                kw = {"agent": "codex", "project": "amiga"}
                kw[field] = value
                with self.assertRaises(SystemExit):
                    codex_stream.resolve_thread(self.args(**kw))

    def test_a_traversing_agent_selector_is_refused(self) -> None:
        self.bind()
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="../amiga/CHAT-A/codex", project="amiga", chat="CHAT-A"))

    # --- the enumeration budget ---------------------------------------------------------

    def test_every_entry_consumes_the_budget_before_filtering(self) -> None:
        (self.bindings / "amiga").mkdir(parents=True)
        for i in range(5):
            (self.bindings / "amiga" / f"junk-{i}.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SCANNED_CHATS", 1):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("more than", str(caught.exception))

    def test_a_named_chat_needs_no_enumeration(self) -> None:
        self.bind()
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
