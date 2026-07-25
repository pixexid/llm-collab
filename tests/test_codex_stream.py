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
             session_home=None, session_updated="2026-01-01T00:00:00Z"):
        """One coherent binding+session pair, with every field overridable to break it.

        session_home defaults to the BINDING's home because that is what real records do: across the
        44 live bindings in this workspace, 43 agree with their session and none disagree (the 44th
        has one side absent). The old default hard-coded a different value, which made every fixture
        pair permanently inconsistent about which App Server owns the thread -- and that arbitrary
        disagreement is what made a real mismatch look like normal fixture noise.
        """
        if session_home is None:
            session_home = home
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

    # --- the snapshot comes from the validation, so there is nothing to race -------------

    def swap_binding_after(self, chat: str, **changes):
        """Mutate the real binding file the instant the resolver returns.

        Wraps resolve_exact_dispatch_pair rather than the file, so the swap lands in exactly the
        window that used to matter: after validation, before use.
        """
        path = self.bindings / "amiga" / chat / "codex.json"
        good = json.loads(path.read_text())
        real = codex_stream.autobridge.resolve_exact_dispatch_pair

        def wrapper(project_id, chat_id, agent_id, sessions=None):
            result = real(project_id, chat_id, agent_id, sessions=sessions)
            path.write_text(json.dumps(dict(good, **changes)), encoding="utf-8")
            return result

        return mock.patch.object(codex_stream.autobridge, "resolve_exact_dispatch_pair",
                                 side_effect=wrapper)

    def test_a_home_only_swap_after_validation_cannot_redirect_the_endpoint(self) -> None:
        """The finding that forced this design, reproduced exactly.

        Changing ONLY runtime_home preserves project, chat, agent, session, runtime and family --
        so every cross-check I had written passed, and the endpoint was still redirected to
        /tmp/wrong-home. No local comparison can catch it, because the session deliberately does
        not mirror the binding's home. The snapshot has to come from the validation itself.
        """
        self.bind(chat="CHAT-HOME-RACE", thread="good-thread", home="/tmp/good-home")
        with self.swap_binding_after("CHAT-HOME-RACE", runtime_home="/tmp/wrong-home"):
            thread, _p, home = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-HOME-RACE"))
        self.assertEqual("good-thread", thread)
        self.assertEqual("/tmp/good-home", home,
                         "the validated snapshot's home must win over the swapped file")

    def test_a_thread_swap_after_validation_cannot_redirect_the_thread(self) -> None:
        self.bind(chat="CHAT-RACE", thread="good-thread", home="/tmp/good-home")
        with self.swap_binding_after("CHAT-RACE", runtime_session_id="wrong-thread",
                                     runtime_home="/tmp/wrong-home"):
            thread, _p, home = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-RACE"))
        self.assertEqual("good-thread", thread)
        self.assertEqual("/tmp/good-home", home)

    def test_a_family_swap_after_validation_cannot_slip_past_the_gate(self) -> None:
        """The gate reads the snapshot, so flipping the file afterwards changes nothing."""
        self.bind(chat="CHAT-FAM", thread="good-thread")
        with self.swap_binding_after("CHAT-FAM", runtime_family="claude_app"):
            thread, _p, _h = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-FAM"))
        self.assertEqual("good-thread", thread)

    def test_a_binding_deleted_after_validation_does_not_break_the_lookup(self) -> None:
        """Previously this had to be refused, because the metadata was still to be read.

        Now the snapshot is already in hand, so the deletion is irrelevant -- a strictly better
        outcome than failing an otherwise valid resolution.
        """
        self.bind(chat="CHAT-GONE", thread="good-thread", home="/tmp/good-home")
        path = self.bindings / "amiga" / "CHAT-GONE" / "codex.json"
        real = codex_stream.autobridge.resolve_exact_dispatch_pair

        def unlink_after(project_id, chat_id, agent_id, sessions=None):
            result = real(project_id, chat_id, agent_id, sessions=sessions)
            path.unlink()
            return result

        with mock.patch.object(codex_stream.autobridge, "resolve_exact_dispatch_pair",
                               side_effect=unlink_after):
            thread, _p, home = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-GONE"))
        self.assertEqual("good-thread", thread)
        self.assertEqual("/tmp/good-home", home)

    def test_this_module_never_reads_a_binding_itself(self) -> None:
        """Zero reads, not one: every field comes from the validated snapshot.

        Asserted structurally as well as by count, because "one read" was the previous
        contract and a regression to it would be invisible to a caller-side count alone.
        """
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn("load_binding", source)

    def test_a_successful_resolution_performs_one_binding_read_in_total(self) -> None:
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
        self.assertEqual(1, len(reads),
                         f"the resolver's own read and nothing else: {reads}")

    def test_broad_lookup_reads_each_binding_once_in_total(self) -> None:
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
        from collections import Counter
        self.assertEqual({"CHAT-A": 1, "CHAT-B": 1}, dict(Counter(reads)),
                         f"one read per chat, all of it the resolver's: {reads}")

    # --- one bounded session scan for the whole lookup ------------------------------------

    def test_broad_lookup_scans_the_session_directory_once(self) -> None:
        """Delegating per chat made the shared resolver rescan every session file per candidate.

        With up to MAX_SCANNED_CHATS candidates that is up to 2,000 full passes over an untrusted
        directory -- the delegation amplified the very cost the split had moved away. One scan now
        serves the whole lookup.
        """
        for chat in ("CHAT-A", "CHAT-B", "CHAT-C"):
            self.bind(chat=chat, thread=f"t-{chat}")
        real_iter = codex_stream.autobridge.iter_sessions
        scans = []

        def counting(agent_id=None):
            scans.append(agent_id)
            return real_iter(agent_id=agent_id)

        with mock.patch.object(codex_stream.autobridge, "iter_sessions", side_effect=counting):
            with self.assertRaises(SystemExit):   # three live bindings is ambiguous
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertEqual([], scans,
                         "the resolver must not scan at all when the caller supplies sessions")

    # --- binding and session must agree on which App Server owns the thread -----------------

    def test_a_binding_and_session_disagreeing_on_home_are_refused(self) -> None:
        """A torn re-registration under a different CODEX_HOME leaves a pair the resolver accepts.

        resolve_exact_dispatch_pair validates the thread id and the runtime family but NOT the home,
        so the two can point at different App Servers: resume fails there, or an unrelated matching
        thread is observed. Real records always agree, so a disagreement is a conflict rather than a
        preference to resolve.
        """
        self.bind(chat="CHAT-TORN", thread="t-torn", home="/tmp/new-home",
                  session_home="/tmp/old-home")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-TORN"))
        message = str(caught.exception)
        self.assertIn("/tmp/new-home", message)
        self.assertIn("/tmp/old-home", message)
        self.assertIn("re-register", message)

    def test_agreeing_homes_resolve_normally(self) -> None:
        self.bind(chat="CHAT-AGREE", thread="t-agree", home="/tmp/same")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-AGREE"))
        self.assertEqual("/tmp/same", home)

    def test_a_session_with_no_home_still_resolves_from_the_binding(self) -> None:
        """Only a real disagreement refuses; an absent session home is not a conflict."""
        self.bind(chat="CHAT-NOSESSHOME", thread="t-nsh", home="/tmp/binding-home",
                  session_home="")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-NOSESSHOME"))
        self.assertEqual("/tmp/binding-home", home)

    # --- the authoritative binding is bounded before it is parsed --------------------------
    #
    # Codex's proof: an ordinary named-chat lookup accepted and resolved a valid 4,194,591-byte
    # binding, because load_binding used read_text(). My earlier "whole family" audit missed this by
    # grepping the MODULE rather than the call graph -- the reader delegates resolution to the
    # shared autobridge, so the shared reads are on this reader's path too.

    def test_an_oversized_binding_is_refused_before_it_is_parsed(self) -> None:
        self.bind(chat="CHAT-BIG", thread="t-big")
        path = self.bindings / "amiga" / "CHAT-BIG" / "codex.json"
        good = json.loads(path.read_text())
        good["pad"] = "z" * (codex_stream.autobridge.MAX_BINDING_BYTES + 2048)
        path.write_text(json.dumps(good), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-BIG"))
        message = str(caught.exception)
        self.assertIn(str(codex_stream.autobridge.MAX_BINDING_BYTES), message)
        self.assertNotIn("exact_binding_required", message,
                         "an oversized binding is present-but-unreadable, not absent")
        self.assertNotIn("no live exactly-bound", message)

    def test_the_binding_read_ITSELF_is_bounded_not_just_the_verdict(self) -> None:
        """Asserted on the read() argument: read-all-then-measure gives the same verdict."""
        self.bind(chat="CHAT-ARG", thread="t-arg")
        seen = []
        real_open = Path.open

        def recording(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            if self_path.name == "codex.json":
                real_read = handle.read

                def read(*read_args):
                    seen.append(read_args)
                    return real_read(*read_args)

                handle.read = read
            return handle

        with mock.patch.object(Path, "open", recording):
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-ARG"))
        self.assertTrue(seen, "the binding was not read through a bounded read at all")
        self.assertEqual((codex_stream.autobridge.MAX_BINDING_BYTES + 1,), seen[0],
                         f"the binding read must be capped, got {seen[0]}")

    def test_an_unreadable_binding_is_reported_not_collapsed_to_absent(self) -> None:
        self.bind(chat="CHAT-DENY", thread="t-deny")
        real_open = Path.open

        def denied(self_path, *args, **kwargs):
            if self_path.name == "codex.json":
                raise PermissionError(13, "Permission denied")
            return real_open(self_path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(
                    self.args(agent="codex", project="amiga", chat="CHAT-DENY"))
        message = str(caught.exception)
        self.assertIn("Permission denied", message)
        self.assertNotIn("exact_binding_required", message)

    def test_a_binding_at_the_limit_still_resolves(self) -> None:
        """The boundary from the other side, so the cap is not merely 'refuses everything'."""
        self.bind(chat="CHAT-FITS", thread="t-fits")
        path = self.bindings / "amiga" / "CHAT-FITS" / "codex.json"
        good = json.loads(path.read_text())
        headroom = codex_stream.autobridge.MAX_BINDING_BYTES - len(json.dumps(good)) - 16
        good["pad"] = "q" * max(headroom, 0)
        path.write_text(json.dumps(good), encoding="utf-8")
        self.assertLessEqual(len(path.read_bytes()),
                             codex_stream.autobridge.MAX_BINDING_BYTES)
        thread, _p, _h = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-FITS"))
        self.assertEqual("t-fits", thread)

    def test_a_missing_binding_is_still_plain_absent(self) -> None:
        """BindingUnreadable must not swallow the ordinary 'no such binding' path."""
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-NOPE"))
        self.assertIn("exact_binding_required", str(caught.exception))

    # --- an unreadable directory is not an empty one ---------------------------------------

    def test_an_unreadable_bindings_dir_is_reported_not_reported_as_no_session(self) -> None:
        """Returning [] made the caller announce "no live binding" while bindings sat unreadable.

        That hides the real fault and breaks --chat last for a reason it cannot report.
        """
        self.bind(chat="CHAT-A")
        real_scandir = codex_stream.os.scandir

        def denied(path, *args, **kwargs):
            if str(path).endswith("amiga"):
                raise PermissionError(13, "Permission denied")
            return real_scandir(path, *args, **kwargs)

        with mock.patch.object(codex_stream.os, "scandir", denied):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        message = str(caught.exception)
        self.assertIn("cannot scan bindings", message)
        self.assertIn("Permission denied", message)
        self.assertNotIn("no live exactly-bound", message,
                         "an I/O failure must not be reported as an absent session")

    def test_a_genuinely_absent_bindings_dir_still_means_no_candidates(self) -> None:
        """Only FileNotFoundError may mean "nothing here" -- the narrow case must stay narrow."""
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex", project="nuvyr"))
        self.assertIn("no live exactly-bound", str(caught.exception))

    # --- the endpoint override carries no home, so this reader refuses it -------------------

    def test_the_unscoped_env_override_cannot_redirect_this_reader(self) -> None:
        """A binding under a secondary CODEX_HOME must not connect to the primary server.

        LLM_COLLAB_CODEX_APP_SERVER_URL has no home or project in it, so honouring it here observed
        either nothing or an unrelated thread that happened to match.
        """
        seen = {}
        real = codex_stream.autobridge.discover_codex_app_server

        def recording(home, **kwargs):
            seen.update(home=home, kwargs=kwargs)
            return real(home, **kwargs)

        with mock.patch.object(codex_stream.autobridge, "discover_codex_app_server", recording):
            with mock.patch.dict(codex_stream.os.environ,
                                 {"LLM_COLLAB_CODEX_APP_SERVER_URL": "ws://127.0.0.1:9/primary"}):
                endpoint = codex_stream.autobridge.discover_codex_app_server(
                    "/tmp/secondary-home", allow_unscoped_env=False)
        self.assertIsNone(endpoint,
                          "with the override refused and no matching process, discovery must fail "
                          "closed rather than fall back to the workspace-wide URL")
        self.assertFalse(seen["kwargs"].get("allow_unscoped_env", True))

    def test_dispatch_still_honours_the_override_by_default(self) -> None:
        """The default must stay True or this change reaches every other caller."""
        with mock.patch.dict(codex_stream.os.environ,
                             {"LLM_COLLAB_CODEX_APP_SERVER_URL": "ws://127.0.0.1:9/primary"}):
            endpoint = codex_stream.autobridge.discover_codex_app_server("/tmp/whatever")
        self.assertIsNotNone(endpoint)
        self.assertEqual("env", endpoint["source"])

    def test_this_module_asks_for_home_scoped_discovery(self) -> None:
        """Structural, asserted on the CALL EXPRESSION rather than a bare substring.

        My first version asserted only that "allow_unscoped_env=False" appeared somewhere in the
        file -- which the explanatory COMMENT above the call also satisfies. Removing the keyword
        from the actual call left the test green. An assertion a comment can satisfy is not an
        assertion.
        """
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertIn(
            "autobridge.discover_codex_app_server(runtime_home, allow_unscoped_env=False)",
            source,
            "the discovery call itself must opt out of the workspace-wide override",
        )
        self.assertNotIn("autobridge.discover_codex_app_server(runtime_home)\n", source,
                         "no call may fall back to the unscoped default")

    def test_a_named_chat_uses_the_BOUNDED_scan_not_the_resolver_s_own(self) -> None:
        """Inverted. This asserted "one chat, one lookup: no reason for the caller to pre-scan".

        That reasoning was about call COUNT and ignored that the resolver's own scan is
        iter_sessions(), which sorts and reads every session file with no count or byte limit. So the
        named-chat path -- the ordinary one -- was the only unbounded lookup left, while the broad
        path that looked riskier was bounded. The second test on this PR whose assertion was quietly
        holding an unsafe path in place.
        """
        self.bind()
        scans = []
        real_iter = codex_stream.autobridge.iter_sessions

        def counting(agent_id=None):
            scans.append(agent_id)
            return real_iter(agent_id=agent_id)

        with mock.patch.object(codex_stream.autobridge, "iter_sessions", side_effect=counting):
            thread, _p, _h = codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("t1", thread)
        self.assertEqual([], scans,
                         "the unbounded resolver scan must not be reached at all; the caller "
                         f"supplies a bounded snapshot instead, got {scans}")

    def test_a_named_chat_is_subject_to_the_session_count_budget(self) -> None:
        """The budget must bite on the named-chat path, not only on broad selection."""
        self.bind(chat="CHAT-A")
        for i in range(6):
            (self.sessions / f"filler-{i}.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SCANNED_SESSIONS", 3):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(
                    self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertIn("refusing to scan further", str(caught.exception))

    def test_a_named_chat_is_subject_to_the_session_byte_budget(self) -> None:
        self.bind(chat="CHAT-A")
        (self.sessions / "huge.json").write_text("{" + '"pad":"' + "z" * 4096 + '"}',
                                                 encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SESSION_BYTES", 512):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(
                    self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertIn("byte limit", str(caught.exception))

    def test_too_many_session_records_fails_closed(self) -> None:
        for chat in ("CHAT-A", "CHAT-B"):
            self.bind(chat=chat)
        for i in range(6):
            (self.sessions / f"filler-{i}.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SCANNED_SESSIONS", 3):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("refusing to scan further", str(caught.exception))

    def test_an_oversized_session_record_is_refused_before_parsing(self) -> None:
        self.bind(chat="CHAT-A")
        (self.sessions / "huge.json").write_text("[" + "0," * 4000 + "0]", encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SESSION_BYTES", 512):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("byte limit", str(caught.exception))

    def test_session_entries_consume_the_budget_before_filtering(self) -> None:
        self.bind(chat="CHAT-A")
        for i in range(6):
            (self.sessions / f"not-json-{i}.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(codex_stream, "MAX_SCANNED_SESSIONS", 3):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertIn("refusing to scan further", str(caught.exception),
                      "non-json entries must be charged too")

    def test_session_files_are_read_with_a_bounded_read(self) -> None:
        """Asserts the READ SIZE, because the verdict alone cannot distinguish this.

        Reading a whole file and then measuring it rejects an oversized record just the same --
        only the allocation differs, so a test that checks the refusal passes against the
        unbounded version. The only observable difference is the argument passed to read().
        """
        self.bind(chat="CHAT-A")
        real_open = Path.open
        sizes = []

        def recording_open(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            if self_path.suffix == ".json" and "sessions" in str(self_path):
                real_read = handle.read

                def read(size=-1):
                    sizes.append(size)
                    return real_read(size)

                handle.read = read
            return handle

        with mock.patch.object(Path, "open", recording_open):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga", chat="last"))
        self.assertTrue(sizes, "a session file should have been read")
        self.assertTrue(all(size == codex_stream.MAX_SESSION_BYTES + 1 for size in sizes),
                        f"every session read must be bounded, got {sizes}")

    def test_a_malformed_session_is_skipped_not_fatal(self) -> None:
        # matches the behaviour of the resolver's own scan, which this replaces
        self.bind(chat="CHAT-A", thread="good")
        (self.sessions / "broken.json").write_text("{not json", encoding="utf-8")
        thread, _p, _h = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="last"))
        self.assertEqual("good", thread)

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
        """The binding carries runtime_home and is the source; the session's copy is a fallback.

        This used to prove it by giving the two DIFFERENT values, so that reading the wrong one
        could not pass by coincidence. A disagreement is now refused outright -- the two homes point
        at different App Servers and picking one is guessing -- so the distinguishing case is a
        session with no home at all. Same guarantee, expressed through a state that can still occur.
        """
        self.bind(home="/Users/binding-side/.codex-alt", session_home="")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("/Users/binding-side/.codex-alt", home)

    def test_a_binding_with_no_home_falls_back_to_the_session(self) -> None:
        self.bind(home=None, session_home="/Users/session-side/.codex")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A"))
        self.assertEqual("/Users/session-side/.codex", home)

    def test_an_explicit_home_does_NOT_override_the_binding(self) -> None:
        """Inverted deliberately. This test used to assert the override wins.

        It was written when --runtime-home looked like a harmless convenience, and it is what
        pinned the endpoint-redirect in place: an assertion that the caller's home beats the
        validated one reads as a contract, so nobody questioned it. The home decides which App
        Server this connects to, which makes it an identity input, not a convenience.
        """
        self.bind(home="/Users/elsewhere/.codex-alt")
        _t, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-A",
                      runtime_home="/tmp/override"))
        self.assertEqual("/Users/elsewhere/.codex-alt", home)

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

    # --- --thread asserts the resolution, it does not bypass it ---------------------------
    #
    # It used to return before --project was required or validated, so a thread from another or
    # unregistered project could be observed whenever projects share a CODEX_HOME and App Server.
    # AGENTS.md:25-27 requires a project-aware reader to demand an exact project match.

    def test_thread_alone_is_refused_because_it_names_no_project(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(thread="019f-abc", runtime_home="/tmp/home"))
        message = str(caught.exception)
        self.assertIn("--project", message)
        self.assertNotIn("019f-abc", message,
                         "an unvalidated thread id must not be echoed as if it were accepted")

    def test_a_thread_matching_the_binding_is_accepted(self) -> None:
        self.bind(chat="CHAT-ASSERT", thread="the-real-thread", home="/tmp/real-home")
        thread, provenance, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-ASSERT",
                      thread="the-real-thread"))
        self.assertEqual("the-real-thread", thread)
        self.assertEqual("/tmp/real-home", home,
                         "the home still comes from the validated binding, not from the flag")
        self.assertIn("CHAT-ASSERT", provenance)

    def test_a_thread_the_project_does_not_own_is_refused(self) -> None:
        """The finding itself: a foreign thread id must not be observable through this project."""
        self.bind(chat="CHAT-OURS", thread="our-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-OURS",
                          thread="someone-elses-thread"))
        message = str(caught.exception)
        self.assertIn("someone-elses-thread", message)
        self.assertIn("our-thread", message,
                      "the refusal must say which thread this project actually owns")

    # My previous version of this test was MASKED and proved nothing: it paired a foreign thread
    # with a foreign home, so it failed at the thread-mismatch check and never reached the home
    # substitution at all. The substitution was still live -- a thread that passed every identity
    # check plus a home from another project connected to that project's App Server. These tests
    # reach the substitution deliberately, by making the thread match.

    def test_a_supplied_home_cannot_redirect_the_endpoint_when_the_thread_MATCHES(self) -> None:
        """The reproduction Codex gave: matching thread + mismatched home."""
        self.bind(chat="CHAT-OURS", thread="the-real-thread", home="/tmp/validated-home")
        _thread, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-OURS",
                      thread="the-real-thread", runtime_home="/tmp/other-project-home"))
        self.assertEqual("/tmp/validated-home", home,
                         "the home must come from the validated binding, never from the caller")

    def test_a_supplied_home_is_ignored_in_ordinary_agent_mode_too(self) -> None:
        """No --thread involved, so nothing else could be doing the refusing."""
        self.bind(chat="CHAT-PLAIN", thread="t-plain", home="/tmp/validated-home")
        _thread, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-PLAIN",
                      runtime_home="/tmp/other-project-home"))
        self.assertEqual("/tmp/validated-home", home)

    def test_a_supplied_home_cannot_substitute_when_the_binding_has_none(self) -> None:
        """The empty-home case must fall back to the SESSION, not to the caller."""
        self.bind(chat="CHAT-NOHOME", thread="t-nohome", home=None,
                  session_home="/tmp/session-home")
        _thread, _p, home = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-NOHOME",
                      runtime_home="/tmp/other-project-home"))
        self.assertEqual("/tmp/session-home", home)

    def test_the_stated_home_contract_matches_what_the_code_does(self) -> None:
        """The P2: prose claimed the binding was the ONLY source while the session is a fallback.

        Pinned as a test because an inaccurate contract is what makes the next reader trust the
        wrong invariant -- the same way an assertion that the caller's home wins read as a contract
        and kept the endpoint redirect alive.
        """
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn("is the only source for the home", source,
                         "the binding is preferred, not the only source")
        fallback_error = source[source.index("neither the selected binding"):]
        fallback_error = fallback_error[:fallback_error.index('"""') if '"""' in
                                       fallback_error[:600] else 400]
        self.assertIn("session", fallback_error,
                      "the error must name both validated sources it consulted")
        self.assertIn("never from caller input", fallback_error)

    def test_the_module_never_reads_a_caller_supplied_home(self) -> None:
        """Structural, because the flag being gone is the actual guarantee."""
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        self.assertNotIn("args.runtime_home", source)
        self.assertNotIn('add_argument("--runtime-home"', source)

    def test_a_foreign_thread_is_still_refused(self) -> None:
        self.bind(chat="CHAT-OURS", thread="our-thread", home="/tmp/ours")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(
                self.args(agent="codex", project="amiga", chat="CHAT-OURS",
                          thread="foreign-thread"))

    def test_an_unregistered_project_is_still_refused_with_a_thread(self) -> None:
        self.bind(chat="CHAT-OURS", thread="our-thread")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(
                self.args(agent="codex", project="not-registered", chat="CHAT-OURS",
                          thread="our-thread"))
        self.assertIn("not registered", str(caught.exception))

    # --- a non-finite duration is not a duration -----------------------------------------

    def test_nan_and_inf_durations_are_rejected(self) -> None:
        """argparse's float() accepts both, and both defeat the advertised stopping limit."""
        import argparse as _argparse

        for value in ("nan", "inf", "-inf", "NaN", "Infinity"):
            with self.subTest(value):
                with self.assertRaises(_argparse.ArgumentTypeError):
                    codex_stream.finite_seconds(value)

    def test_zero_and_negative_durations_are_rejected(self) -> None:
        import argparse as _argparse

        for value in ("0", "-1", "-0.5"):
            with self.subTest(value):
                with self.assertRaises(_argparse.ArgumentTypeError):
                    codex_stream.finite_seconds(value)

    def test_a_real_duration_is_accepted(self) -> None:
        self.assertEqual(60.0, codex_stream.finite_seconds("60"))
        self.assertEqual(0.5, codex_stream.finite_seconds("0.5"))

    def test_a_non_numeric_duration_is_rejected(self) -> None:
        import argparse as _argparse

        with self.assertRaises(_argparse.ArgumentTypeError):
            codex_stream.finite_seconds("soon")

    def test_the_parser_ITSELF_rejects_a_non_finite_duration(self) -> None:
        """End-to-end through argparse, because testing the validator alone proves nothing.

        A mutation reverting the option to `type=float` left every direct finite_seconds test
        green -- the validator was still correct, just no longer wired to anything.
        """
        for value in ("nan", "inf"):
            with self.subTest(value):
                with mock.patch.object(
                    codex_stream.sys, "argv",
                    ["codex_stream.py", "--agent", "codex", "--project", "amiga",
                     "--seconds", value],
                ):
                    with self.assertRaises(SystemExit) as caught:
                        codex_stream.parse_args()
                    self.assertNotEqual(0, caught.exception.code)

    def test_the_parser_accepts_a_real_duration(self) -> None:
        with mock.patch.object(
            codex_stream.sys, "argv",
            ["codex_stream.py", "--agent", "codex", "--project", "amiga", "--seconds", "45"],
        ):
            self.assertEqual(45.0, codex_stream.parse_args().seconds)

    def test_the_documented_thread_example_names_a_project(self) -> None:
        """A documented invocation must be runnable, and must not show the old bypass."""
        source = (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")
        usage = source[source.index("Usage:"):source.index('"""', source.index("Usage:"))]
        for line in usage.splitlines():
            if "--thread" in line:
                block = usage[usage.index(line) - 200:]
                self.assertIn("--project", block,
                              "the documented --thread invocation must name a project")
                self.assertIn("--agent", block)
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
        self.assertLessEqual(made.remaining_wait(), 0.1)

    def test_no_deadline_uses_the_idle_cap(self) -> None:
        made = self.client()
        made.set_deadline(None)
        self.assertEqual(5, made.remaining_wait())

    def test_a_distant_deadline_still_respects_the_idle_cap(self) -> None:
        made = self.client()
        made.set_deadline(time.monotonic() + 600)
        self.assertEqual(5, made.remaining_wait())

    def test_an_exhausted_window_never_becomes_a_blocking_wait(self) -> None:
        # settimeout(0) makes the socket non-blocking, which is a different failure
        made = self.client()
        made.set_deadline(time.monotonic() - 3)
        self.assertGreater(made.remaining_wait(), 0)

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


class ConnectDeadlineTest(unittest.TestCase):
    """`--seconds` must bound the CONNECT and the upgrade handshake, not only what follows.

    The inherited __enter__ uses timeout_seconds for socket.create_connection() and for every
    handshake recv(), and it runs before any deadline could be installed -- so a stalled connect,
    or a handshake trickling bytes, ran for seconds despite `--seconds 0.1`.
    """

    def source(self) -> str:
        return (ROOT / "bin" / "codex_stream.py").read_text(encoding="utf-8")

    def test_the_socket_timeout_is_folded_down_to_the_requested_duration(self) -> None:
        source = self.source()
        self.assertIn("connect_timeout = max(0.05, min(DEFAULT_IDLE_TIMEOUT_SECONDS, args.seconds))",
                      source,
                      "the requested duration must reach the connect timeout itself")

    def test_the_deadline_is_installed_before_the_context_is_entered(self) -> None:
        source = self.source()
        set_at = source.index("client.set_deadline(deadline)")
        enter_at = source.index("with client:")
        self.assertLess(set_at, enter_at,
                        "the very first blocking read must already be bounded")

    def test_the_client_is_no_longer_constructed_inside_the_with(self) -> None:
        # constructing it in the `with` header made __enter__ run before any deadline existed
        self.assertNotIn("with ObserverClient(", self.source())

    def test_a_small_seconds_value_does_not_become_a_nonblocking_socket(self) -> None:
        source = self.source()
        self.assertIn("max(0.05,", source, "a floor keeps the socket blocking")


class TricklingHandshakeTest(unittest.TestCase):
    """A REAL socket, because no structural test can detect this.

    The inherited handshake loops on recv() with a fixed socket timeout, so a peer sending the
    response in small pieces resets that timeout on every piece and the total is unbounded.
    Clamping the per-call timeout bounds each read; only an absolute deadline bounds the sum.
    """

    def serve(self, chunks, gap):
        """A listener that dribbles `chunks` with `gap` seconds between them."""
        import socket as _socket
        import threading

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        def run():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.recv(4096)          # the client's request line and headers
                for chunk in chunks:
                    time.sleep(gap)
                    try:
                        conn.sendall(chunk)
                    except OSError:
                        return
            finally:
                conn.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return port

    def client(self, port, deadline_seconds):
        made = codex_stream.ObserverClient(f"ws://127.0.0.1:{port}", token=None,
                                           timeout_seconds=deadline_seconds)
        made.set_deadline(time.monotonic() + deadline_seconds)
        return made

    def test_a_trickled_handshake_cannot_outlast_the_budget(self) -> None:
        """Codex's reproduction: three chunks 40ms apart against a 50ms budget.

        Each recv succeeded inside its own timeout, so the handshake ran ~144ms and the expired
        absolute deadline was never consulted.
        """
        port = self.serve([b"HTTP/1.1 101 Switching Protocols\r\n",
                           b"Upgrade: websocket\r\n",
                           b"Connection: Upgrade\r\n\r\n"], gap=0.04)
        made = self.client(port, 0.05)
        started = time.monotonic()
        with self.assertRaises((TimeoutError, ConnectionError, OSError)):
            with made:
                pass
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5,
                        f"the handshake must not outlast the budget: took {elapsed:.3f}s")

    def test_a_silent_peer_cannot_hold_the_handshake_open(self) -> None:
        port = self.serve([], gap=0)      # accepts, then says nothing at all
        made = self.client(port, 0.1)
        started = time.monotonic()
        with self.assertRaises((TimeoutError, ConnectionError, OSError)):
            with made:
                pass
        self.assertLess(time.monotonic() - started, 0.6)

    def test_many_tiny_chunks_cannot_extend_the_handshake(self) -> None:
        """The pathological case: each chunk arrives well inside the per-call timeout."""
        header = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                  b"Connection: Upgrade\r\n\r\n")
        port = self.serve([header[i:i + 1] for i in range(len(header))], gap=0.01)
        made = self.client(port, 0.08)
        started = time.monotonic()
        with self.assertRaises((TimeoutError, ConnectionError, OSError)):
            with made:
                pass
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.6, f"took {elapsed:.3f}s for a 0.08s budget")

    def test_an_expired_deadline_refuses_before_connecting(self) -> None:
        port = self.serve([], gap=0)
        made = codex_stream.ObserverClient(f"ws://127.0.0.1:{port}", token=None,
                                           timeout_seconds=1)
        made.set_deadline(time.monotonic() - 1)
        with self.assertRaises(TimeoutError) as caught:
            with made:
                pass
        self.assertIn("before connecting", str(caught.exception))


class ProjectRegistryBudgetTest(unittest.TestCase):
    """projects.json is workspace-local, so it is untrusted like the trees around it.

    read_text() allocated and parsed the whole file before any lookup limit existed, which made the
    earliest parse boundary in the run the only unbounded one. Tested against a REAL oversized file,
    because the defect is in what read does, and a mocked reader would prove nothing about it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(codex_stream, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_registry(self, text: str) -> Path:
        path = self.root / "projects.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_an_oversized_registry_is_refused_before_it_is_parsed(self) -> None:
        padding = "z" * (codex_stream.MAX_REGISTRY_BYTES + 1024)
        self.write_registry(json.dumps({"projects": [{"id": "amiga", "pad": padding}]}))
        with self.assertRaises(SystemExit) as caught:
            codex_stream.registered_project_ids()
        self.assertIn(str(codex_stream.MAX_REGISTRY_BYTES), str(caught.exception))

    def test_the_read_itself_is_bounded_not_just_the_verdict(self) -> None:
        """A read-all-then-measure implementation gives the same verdict and the same exhaustion.

        Asserted on the argument passed to read(), because that is the difference between refusing
        an oversized file and allocating it first and then complaining.
        """
        self.write_registry(json.dumps({"projects": [{"id": "amiga"}]}))
        seen = []
        real_open = Path.open

        def recording_open(self_path, *args, **kwargs):
            handle = real_open(self_path, *args, **kwargs)
            if self_path.name == "projects.json":
                real_read = handle.read

                def read(*read_args):
                    seen.append(read_args)
                    return real_read(*read_args)

                handle.read = read
            return handle

        with mock.patch.object(Path, "open", recording_open):
            codex_stream.registered_project_ids()
        self.assertTrue(seen, "projects.json was not read through a bounded read at all")
        self.assertEqual((codex_stream.MAX_REGISTRY_BYTES + 1,), seen[0],
                         f"the read must be capped, got {seen[0]}")

    def test_a_registry_at_the_limit_still_parses(self) -> None:
        entry = {"projects": [{"id": "amiga"}]}
        body = json.dumps(entry)
        pad = codex_stream.MAX_REGISTRY_BYTES - len(body) - len('", "pad": ""')
        self.write_registry(json.dumps({"projects": [{"id": "amiga", "pad": "q" * max(pad, 0)}]})
                            if pad > 0 else body)
        self.assertEqual({"amiga"}, codex_stream.registered_project_ids())

    def test_an_unreadable_registry_is_reported_not_treated_as_unregistered(self) -> None:
        """Same distinction as the binding scan, found by auditing the family, not by it being filed.

        Lives in THIS class because ResolveThreadTest.setUp patches registered_project_ids, so the
        version of this test I first wrote there was asserting against a mock and told me nothing.
        """
        self.write_registry('{"projects": [{"id": "amiga"}]}')
        real_open = Path.open

        def denied(self_path, *args, **kwargs):
            if self_path.name == "projects.json":
                raise PermissionError(13, "Permission denied")
            return real_open(self_path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied):
            with self.assertRaises(SystemExit) as caught:
                codex_stream.registered_project_ids()
        self.assertIn("Permission denied", str(caught.exception))

    def test_a_missing_registry_still_returns_empty_rather_than_raising(self) -> None:
        """Unchanged: callers turn an empty set into an explicit refusal to verify the project."""
        self.assertEqual(set(), codex_stream.registered_project_ids())

    def test_malformed_json_still_returns_empty(self) -> None:
        self.write_registry("{not json")
        self.assertEqual(set(), codex_stream.registered_project_ids())

    def test_non_utf8_bytes_return_empty_rather_than_crashing(self) -> None:
        (self.root / "projects.json").write_bytes(b'{"projects": [{"id": "\xff\xfe"}]}')
        self.assertEqual(set(), codex_stream.registered_project_ids())

    def test_a_normal_registry_lists_its_projects(self) -> None:
        self.write_registry(json.dumps({"projects": [{"id": "amiga"}, {"id": "nuvyr"}]}))
        self.assertEqual({"amiga", "nuvyr"}, codex_stream.registered_project_ids())


class SetupBoundaryTest(unittest.TestCase):
    """Setup is inside the deadline, and nothing emitted during it is lost."""

    def client(self, frames):
        # The REAL constructor, not __new__ plus hand-set attributes. The old version listed the
        # fields it thought mattered, so adding pending_event_bytes to __init__ broke two tests
        # with AttributeError -- the stub had quietly become a second, divergent definition of the
        # object. __init__ does not touch the network (only __enter__ connects), so there was never
        # a reason to skip it.
        made = codex_stream.ObserverClient("ws://127.0.0.1:1", token=None, timeout_seconds=5)
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

    # --- the setup buffer is bounded on BOTH axes ------------------------------------------
    #
    # Either axis alone is evadable: many tiny notifications exhaust the count while staying far
    # under the byte cap, and one enormous notification exhausts memory while the count stays at 1.

    def test_too_many_setup_notifications_abort_instead_of_buffering(self) -> None:
        flood = [{"method": "item/updated", "params": {"n": i}}
                 for i in range(codex_stream.MAX_PENDING_EVENTS + 5)]
        client = self.client(flood + [{"id": "llm-collab-1", "result": {}}])
        with self.assertRaises(SystemExit) as caught:
            client.request("thread/resume", {"threadId": "T1"})
        self.assertIn(str(codex_stream.MAX_PENDING_EVENTS), str(caught.exception))
        self.assertLessEqual(len(client.pending_events), codex_stream.MAX_PENDING_EVENTS)

    def test_one_enormous_setup_notification_aborts_on_the_byte_budget(self) -> None:
        """The count stays at 1 here, so only the byte axis can catch this."""
        huge = {"method": "item/updated",
                "params": {"blob": "x" * (codex_stream.MAX_PENDING_EVENT_BYTES + 1024)}}
        client = self.client([huge, {"id": "llm-collab-1", "result": {}}])
        with self.assertRaises(SystemExit) as caught:
            client.request("thread/resume", {"threadId": "T1"})
        self.assertIn(str(codex_stream.MAX_PENDING_EVENT_BYTES), str(caught.exception))
        self.assertEqual([], client.pending_events,
                         "nothing may be retained once the budget is blown")

    def test_many_medium_notifications_are_charged_cumulatively(self) -> None:
        """Each is well under the cap; together they exceed it. Per-message checks miss this."""
        one_eighth = codex_stream.MAX_PENDING_EVENT_BYTES // 8
        stream = [{"method": "item/updated", "params": {"blob": "y" * one_eighth}}
                  for _ in range(12)]
        client = self.client(stream + [{"id": "llm-collab-1", "result": {}}])
        with self.assertRaises(SystemExit):
            client.request("thread/resume", {"threadId": "T1"})

    def test_a_normal_setup_window_is_unaffected(self) -> None:
        events = [{"method": "turn/started", "params": {"turn": {"id": f"u{i}"}}}
                  for i in range(20)]
        client = self.client(events + [{"id": "llm-collab-1", "result": {}}])
        client.request("thread/resume", {"threadId": "T1"})
        self.assertEqual(20, len(client.pending_events))

    def test_draining_resets_the_byte_charge(self) -> None:
        """Otherwise a long-lived client would accumulate toward the cap across setups."""
        client = self.client([{"method": "turn/started", "params": {}},
                              {"id": "llm-collab-1", "result": {}}])
        client.request("thread/resume", {"threadId": "T1"})
        self.assertGreater(client.pending_event_bytes, 0)
        client.take_pending_events()
        self.assertEqual(0, client.pending_event_bytes)

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
        self.assertEqual({"item/commandExecution/requestApproval"},
                         client.observed_request_methods)
        self.assertEqual(1, client.observed_request_count)

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
        client.observed_request_methods = set()
        client.observed_request_count = 0
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

    def test_the_observed_method_set_is_capped_while_the_count_keeps_rising(self) -> None:
        """A noisy or hostile peer must not grow this without bound.

        The count is what shutdown reports, so it must stay exact; the SET is what can grow, so it
        is capped. An earlier version stored one string per request, which a peer with an unlimited
        streaming duration could grow indefinitely.
        """
        invented = [{"id": f"srv-{i}", "method": f"made/up/method-{i}", "params": {}}
                    for i in range(codex_stream.MAX_OBSERVED_REQUEST_METHODS + 40)]
        client = self.client(invented + [{"method": "turn/completed", "params": {}}])
        self.drain(client, 1)

        self.assertEqual(len(invented), client.observed_request_count,
                         "every request must still be counted")
        self.assertLessEqual(len(client.observed_request_methods),
                             codex_stream.MAX_OBSERVED_REQUEST_METHODS,
                             "the distinct-method set must be capped")

    def test_a_hostile_method_NAME_cannot_evade_the_cap_by_being_huge(self) -> None:
        """Capping the count of names is useless if one name can be arbitrarily long."""
        monster = {"id": "srv-x", "method": "m" * 100_000, "params": {}}
        client = self.client([monster, {"method": "turn/completed", "params": {}}])
        self.drain(client, 1)

        self.assertEqual(1, client.observed_request_count)
        stored = next(iter(client.observed_request_methods))
        self.assertLessEqual(len(stored), codex_stream.MAX_METHOD_NAME_CHARS,
                             f"stored method name must be truncated, got {len(stored)} chars")

    def test_an_approval_request_produces_zero_response_frames(self) -> None:
        approval = {"id": "srv-1", "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "T1", "turnId": "U1", "command": "rm -rf /"}}
        event = {"method": "turn/completed", "params": {}}
        client = self.client([approval, event])
        delivered = self.drain(client, 1)

        self.assertEqual([event], delivered, "the request must not surface as an event")
        self.assertEqual([], client.sent,
                         "an observer must send NO frame at all -- not a result, not an error")
        self.assertEqual({"item/commandExecution/requestApproval"},
                         client.observed_request_methods)
        self.assertEqual(1, client.observed_request_count)
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
        self.assertEqual(set(methods), client.observed_request_methods)
        self.assertEqual(len(methods), client.observed_request_count)

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
        self.assertEqual({"item/tool/call"}, client.observed_request_methods)
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
