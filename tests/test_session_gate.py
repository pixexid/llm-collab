"""Focused proof for the SessionStart hook and watcher liveness markers (GH-722).

Ownership coverage (GH-726 T2): fresh-and-mine passes, fresh-and-foreign alerts
naming the foreign session, malformed content is UNREADABLE never fresh, and
project A's markers never satisfy project B — by directory AND by content.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _bounded_io  # noqa: E402
import _watcher_liveness  # noqa: E402
import session_bootstrap  # noqa: E402
import session_gate  # noqa: E402
from _bounded_io import UnreadableFile  # noqa: E402
from _watcher_liveness import (  # noqa: E402
    WATCHER_MARKER_STALE_AFTER_SECONDS,
    WATCHER_NAMES,
    check_markers,
    evaluate_coverage,
    not_fresh,
    write_marker,
)
from llm_collab.bb_client import (  # noqa: E402
    PINNED_BB_VERSION,
    BbTransportResult,
    subprocess_transport,
)


def marker_content(project_id: str = "proj-under-test", session_id: str = "sess-writer") -> str:
    return (
        json.dumps(
            {
                "session_id": session_id,
                "project_id": project_id,
                "started_at": "2026-08-09T00:00:00+00:00",
            }
        )
        + "\n"
    )


class MarkerFreshnessTest(unittest.TestCase):
    """Marker classification in both directions; a broken probe is never fresh."""

    def _check(self, directory: str, *, now: float) -> list[dict]:
        with mock.patch.object(
            _watcher_liveness, "markers_dir", return_value=Path(directory)
        ):
            return check_markers("proj-under-test", now=now)

    def _write_markers(self, directory: str, *, mtime: float, content: str | None = None) -> None:
        for name in WATCHER_NAMES:
            marker = Path(directory) / f"{name}.alive"
            marker.write_text(content if content is not None else marker_content(), encoding="utf-8")
            os.utime(marker, (mtime, mtime))

    def test_fresh_marker_under_project_a_does_not_make_project_b_fresh(self) -> None:
        """The discriminating scoping test: markers are per-project.

        project_state_dir is mocked onto one temporary root, so the result does
        not depend on whether this tree has a collab.config.json. If markers_dir
        ever ignores its project argument again (any hardcode), project A's own
        check reads the wrong directory and the fresh assertion fails.
        """
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markers_a = root / "project-a" / "watchers"
            markers_a.mkdir(parents=True)
            for name in WATCHER_NAMES:
                marker = markers_a / f"{name}.alive"
                marker.write_text(marker_content(project_id="project-a"), encoding="utf-8")
                os.utime(marker, (now - 30, now - 30))
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
            ):
                report_a = check_markers("project-a", now=now)
                report_b = check_markers("project-b", now=now)
        self.assertEqual(["fresh"] * len(WATCHER_NAMES), [e["status"] for e in report_a])
        self.assertEqual(["absent"] * len(WATCHER_NAMES), [e["status"] for e in report_b])
        self.assertEqual(len(WATCHER_NAMES), len(not_fresh(report_b)))

    def test_marker_content_naming_another_project_never_satisfies(self) -> None:
        """The content half of cross-project scoping: a marker that sits in
        project A's directory but NAMES project B in its content does not
        satisfy project A's check (a cross-project overwrite reads UNREADABLE,
        not fresh), and does not satisfy project B's check either (it is not in
        B's directory)."""
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markers_a = root / "project-a" / "watchers"
            markers_a.mkdir(parents=True)
            for name in WATCHER_NAMES:
                marker = markers_a / f"{name}.alive"
                marker.write_text(marker_content(project_id="project-b"), encoding="utf-8")
                os.utime(marker, (now - 30, now - 30))
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
            ):
                report_a = check_markers("project-a", now=now)
                report_b = check_markers("project-b", now=now)
        self.assertEqual(["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report_a])
        self.assertEqual([], [e for e in report_a if e["status"] == "fresh"])
        self.assertEqual(["absent"] * len(WATCHER_NAMES), [e["status"] for e in report_b])

    def test_fresh_markers_classify_fresh(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_markers(temporary, mtime=now - 30)
            report = self._check(temporary, now=now)
        self.assertEqual(["fresh"] * len(WATCHER_NAMES), [e["status"] for e in report])
        self.assertEqual([], not_fresh(report))
        self.assertEqual(["sess-writer"] * len(WATCHER_NAMES), [e["session_id"] for e in report])

    def test_stale_markers_classify_stale(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_markers(
                temporary, mtime=now - 2 * WATCHER_MARKER_STALE_AFTER_SECONDS
            )
            report = self._check(temporary, now=now)
        self.assertEqual(["stale"] * len(WATCHER_NAMES), [e["status"] for e in report])
        self.assertEqual(len(WATCHER_NAMES), len(not_fresh(report)))

    def test_absent_markers_classify_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = self._check(temporary, now=time.time())
        self.assertEqual(["absent"] * len(WATCHER_NAMES), [e["status"] for e in report])
        self.assertEqual(len(WATCHER_NAMES), len(not_fresh(report)))

    def test_malformed_or_empty_marker_content_is_unreadable_never_fresh(self) -> None:
        now = time.time()
        for content in ("alive\n", "", "[]", '{"project_id": 1}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                self._write_markers(temporary, mtime=now - 30, content=content)
                report = self._check(temporary, now=now)
            self.assertEqual(
                ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
            )
            self.assertEqual([], [e for e in report if e["status"] == "fresh"])

    def test_future_dated_marker_is_never_fresh(self) -> None:
        """A backward clock or future mtime must not keep a dead watcher
        satisfying the gate: negative age classifies UNREADABLE, never FRESH."""
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_markers(
                temporary, mtime=now + 2 * WATCHER_MARKER_STALE_AFTER_SECONDS
            )
            report = self._check(temporary, now=now)
        self.assertEqual(
            ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
        )
        self.assertEqual([], [e for e in report if e["status"] == "fresh"])
        self.assertIn("future", report[0]["detail"])

    def test_non_regular_marker_is_unreadable_without_hanging(self) -> None:
        """A FIFO (or any non-regular path) at the marker location fails closed:
        the bounded primitive opens non-blocking and refuses on fstat, so the
        hook and the writing-spawn gate report UNREADABLE instead of wedging
        inside open() before any byte cap could apply."""
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            for name in WATCHER_NAMES:
                os.mkfifo(Path(temporary) / f"{name}.alive")
            # Completing at all is the no-hang assertion; the status is the
            # fail-closed one.
            report = self._check(temporary, now=now)
        self.assertEqual(
            ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
        )
        self.assertIn("not a regular file", report[0]["detail"])

    def test_oversized_marker_is_unreadable_never_fresh(self) -> None:
        # The oversized fixture must be VALID JSON: with the size bound removed,
        # unparseable bytes would still classify UNREADABLE via the parse error
        # and the test would pass while never measuring the bound. Valid-but-
        # oversized content reads FRESH if the bound is gone, so this fails.
        now = time.time()
        oversized = (
            marker_content()[:-2]
            + ', "pad": "'
            + "x" * _watcher_liveness.MAX_MARKER_BYTES
            + '"}\n'
        )
        self.assertGreater(
            len(oversized.encode()), _watcher_liveness.MAX_MARKER_BYTES
        )
        with tempfile.TemporaryDirectory() as temporary:
            self._write_markers(temporary, mtime=now - 30, content=oversized)
            report = self._check(temporary, now=now)
        self.assertEqual(
            ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
        )
        self.assertIn("exceeds", report[0]["detail"])
        self.assertEqual([], [e for e in report if e["status"] == "fresh"])

    def test_marker_size_bound_is_a_protective_order_of_magnitude(self) -> None:
        # The bound tests scale with the constant by design; this pins the
        # policy itself. A real marker is ~100 bytes of JSON: a bound below
        # that rejects every marker (the gate can never pass), and a bound in
        # the megabytes stops protecting a hook that runs at every session
        # start. Both directions of drift are worth one cheap assertion.
        self.assertGreaterEqual(_watcher_liveness.MAX_MARKER_BYTES, 256)
        self.assertLessEqual(_watcher_liveness.MAX_MARKER_BYTES, 1_000_000)

    def test_benign_concurrent_rewrite_negative_age_is_still_fresh(self) -> None:
        """A watcher that atomically rewrites its marker between our descriptor
        read and our clock sample yields a small negative age — a LIVE watcher,
        not a future-dated marker. The line is FUTURE_TOLERANCE_SECONDS: beyond
        it the timestamp is genuinely future and never fresh (the far-future
        test above keeps that side)."""
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_markers(temporary, mtime=now + 2)
            report = self._check(temporary, now=now)
        self.assertEqual(["fresh"] * len(WATCHER_NAMES), [e["status"] for e in report])

    def test_unreadable_marker_is_unreadable_never_fresh(self) -> None:
        with mock.patch.object(
            _watcher_liveness,
            "read_regular_file_bounded_with_identity",
            side_effect=UnreadableFile("denied"),
        ), tempfile.TemporaryDirectory() as temporary:
            report = self._check(temporary, now=time.time())
        self.assertEqual(
            ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
        )
        self.assertEqual(len(WATCHER_NAMES), len(not_fresh(report)))


class MarkerOwnershipTest(unittest.TestCase):
    """evaluate_coverage: the ONE coverage verdict — freshness AND ownership."""

    @staticmethod
    def _report(owner: str) -> list[dict]:
        return [
            {"name": name, "status": "fresh", "age_seconds": 5.0, "session_id": owner}
            for name in WATCHER_NAMES
        ]

    def test_same_session_owner_is_covered(self) -> None:
        verdicts = evaluate_coverage(self._report("sess-mine"), "sess-mine")
        self.assertEqual(["covered"] * len(WATCHER_NAMES), [v["reason"] for v in verdicts])
        self.assertTrue(all(v["acceptable"] for v in verdicts))

    def test_different_session_owner_is_foreign_and_not_coverage(self) -> None:
        verdicts = evaluate_coverage(self._report("sess-predecessor"), "sess-current")
        self.assertEqual(["foreign"] * len(WATCHER_NAMES), [v["reason"] for v in verdicts])
        self.assertEqual([], [v for v in verdicts if v["acceptable"]])
        self.assertEqual({"sess-predecessor"}, {v["session_id"] for v in verdicts})

    def test_unknown_current_identity_is_never_a_pass(self) -> None:
        verdicts = evaluate_coverage(self._report("sess-predecessor"), None)
        self.assertEqual(
            ["owner_unknown"] * len(WATCHER_NAMES), [v["reason"] for v in verdicts]
        )
        self.assertEqual([], [v for v in verdicts if v["acceptable"]])

    def test_stale_foreign_marker_is_not_a_fresh_foreign_alert(self) -> None:
        report = self._report("sess-predecessor")
        for entry in report:
            entry["status"] = "stale"
        verdicts = evaluate_coverage(report, "sess-current")
        self.assertEqual(["stale"] * len(WATCHER_NAMES), [v["reason"] for v in verdicts])


class MarkerWriterTest(unittest.TestCase):
    """write_marker is the single documented writing shape (GH-726 S2)."""

    @staticmethod
    def _registered(project_id: str):
        return {"id": project_id} if project_id in {"project-a", "project-b"} else None

    def test_written_marker_reads_back_fresh_and_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
            ), mock.patch.object(
                _watcher_liveness, "get_project", side_effect=self._registered
            ):
                marker = write_marker("project-a", "worker-lifecycle", "sess-1")
                first = json.loads(marker.read_text(encoding="utf-8"))
                again = write_marker("project-a", "worker-lifecycle", "sess-1")
                second = json.loads(again.read_text(encoding="utf-8"))
                report = check_markers("project-a")
        self.assertEqual("sess-1", first["session_id"])
        self.assertEqual("project-a", first["project_id"])
        self.assertEqual(first["started_at"], second["started_at"])
        own = next(e for e in report if e["name"] == "worker-lifecycle")
        self.assertEqual("fresh", own["status"])
        self.assertEqual("sess-1", own["session_id"])

    def test_writer_refuses_unknown_name_or_empty_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: Path(temporary) / project_id,
            ), mock.patch.object(
                _watcher_liveness, "get_project", side_effect=self._registered
            ):
                with self.assertRaises(ValueError):
                    write_marker("project-a", "../escape", "sess-1")
                with self.assertRaises(ValueError):
                    write_marker("project-a", "heartbeat", "")

    def test_unregistered_or_path_bearing_project_refuses_and_writes_nothing(self) -> None:
        """Project Boundary guard: a project-aware mutator demands an exact
        registered project. The proof is the absence of the write, not only
        the refusal."""
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: Path(temporary) / project_id,
            ), mock.patch.object(
                _watcher_liveness, "get_project", side_effect=self._registered
            ), mock.patch.object(_watcher_liveness, "write_file_durably") as writer:
                for bad in ("no-such-project", "../escape", "project-a/../project-a"):
                    with self.subTest(project=bad), self.assertRaises(ValueError):
                        write_marker(bad, "heartbeat", "sess-1")
            writer.assert_not_called()

    def test_oversized_existing_marker_is_not_preserved(self) -> None:
        """Bounded work fails closed: an oversized existing marker is discarded,
        not parsed unboundedly and not preserved into the next marker."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
            ), mock.patch.object(
                _watcher_liveness, "get_project", side_effect=self._registered
            ):
                oversized = (
                    json.dumps(
                        {
                            "session_id": "sess-1",
                            "project_id": "project-a",
                            "started_at": "1999-01-01T00:00:00+00:00",
                        }
                    )[:-1]
                    + ', "pad": "'
                    + "x" * _watcher_liveness.MAX_MARKER_BYTES
                    + '"}\n'
                )
                marker = root / "project-a" / "watchers" / "heartbeat.alive"
                marker.parent.mkdir(parents=True)
                marker.write_text(oversized, encoding="utf-8")
                write_marker("project-a", "heartbeat", "sess-1")
                new = json.loads(marker.read_text(encoding="utf-8"))
                size = marker.stat().st_size
                report = check_markers("project-a")
        self.assertLess(size, _watcher_liveness.MAX_MARKER_BYTES)
        self.assertNotEqual("1999-01-01T00:00:00+00:00", new["started_at"])
        self.assertNotIn("pad", new)
        own = next(e for e in report if e["name"] == "heartbeat")
        self.assertEqual("fresh", own["status"])

    def test_non_regular_existing_marker_does_not_wedge_the_writer(self) -> None:
        """The writer's read-before-preserve uses the same bounded primitive: a
        FIFO at the marker path is discarded (UnreadableFile), not blocked on,
        and the rewrite replaces it with a valid marker."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "project-a" / "watchers" / "heartbeat.alive"
            marker.parent.mkdir(parents=True)
            os.mkfifo(marker)
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
            ), mock.patch.object(
                _watcher_liveness, "get_project", side_effect=self._registered
            ):
                write_marker("project-a", "heartbeat", "sess-1")
                report = check_markers("project-a")
        own = next(e for e in report if e["name"] == "heartbeat")
        self.assertEqual("fresh", own["status"])
        self.assertEqual("sess-1", own["session_id"])


class ContractHeaderReadTest(unittest.TestCase):
    """The contract marker read is bounded: the hook makes it automatic."""

    def test_contract_header_read_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "AGENTS.md"
            path.write_text(
                "<!-- CONTRACT_VERSION: 99 -->\n" + "x" * 1_000_000, encoding="utf-8"
            )
            # Spy at the raw read: every os.read issued for this parse must
            # request a POSITIVE size at most the bound. An unbounded read
            # requests the whole file (or -1 through a buffered wrapper) and
            # fails here — the bound is asserted, not the presence of a read.
            with mock.patch.object(_bounded_io.os, "read", wraps=os.read) as spy:
                version = session_bootstrap.contract_version(path=path)
        sizes = [call.args[1] for call in spy.call_args_list]
        self.assertEqual("99", version)
        self.assertTrue(sizes)
        self.assertTrue(
            all(0 < n <= session_bootstrap.CONTRACT_HEADER_READ_BYTES for n in sizes),
            sizes,
        )

    def test_contract_header_read_does_not_block_on_a_special_file(self) -> None:
        """A FIFO at AGENTS.md must not stall every SessionStart: the prefix
        reader opens non-blocking and refuses non-regular files. Completing at
        all is the no-hang assertion."""
        with tempfile.TemporaryDirectory() as temporary:
            fifo = Path(temporary) / "AGENTS.md"
            os.mkfifo(fifo)
            version = session_bootstrap.contract_version(path=fifo)
        self.assertEqual("unknown", version)


class HookCommandTest(unittest.TestCase):
    """The tracked hook resolves its interpreter through bin/llm-collab, so it
    runs on any supported installation instead of silently doing nothing where
    a hardcoded interpreter name is absent. Configured command, not a live run."""

    def test_hook_command_resolves_through_the_launcher(self) -> None:
        settings = json.loads(
            (ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("bin/llm-collab", command)
        self.assertIn("session_gate", command)
        self.assertNotIn("python3.11", command)


class SessionGateTest(unittest.TestCase):
    """The hook prints check results and pointers, and never fails the session."""

    def _run(self, own_session_id: str | None = "sess-own", **patches) -> tuple[int, str]:
        tooling = patches.pop(
            "tooling_currency",
            {"state": "current", "head": "abcdef0", "fetched": True},
        )
        markers = patches.pop(
            "check_markers",
            [
                {
                    "name": name,
                    "status": "fresh",
                    "age_seconds": 5.0,
                    "session_id": "sess-own",
                }
                for name in WATCHER_NAMES
            ],
        )
        with mock.patch.object(
            session_gate.session_bootstrap, "tooling_currency", return_value=tooling
        ), mock.patch.object(session_gate, "check_markers", return_value=markers), mock.patch.object(
            session_gate, "current_session_id", return_value=own_session_id
        ):
            for target, replacement in patches.items():
                mock.patch.object(session_gate, target, replacement).start()
            self.addCleanup(mock.patch.stopall)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = session_gate.main()
        return code, output.getvalue()

    def test_broken_bb_probe_is_unknown_not_a_pass(self) -> None:
        code, out = self._run(
            bb_version_check=lambda: (session_gate.UNKNOWN, "probe could not run")
        )
        self.assertEqual(0, code)
        self.assertIn("bb version: UNKNOWN", out)
        self.assertNotIn("bb version: PASS", out)
        self.assertIn("SESSION SETUP INCOMPLETE", out)

    def test_bb_version_mismatch_is_a_visible_fail(self) -> None:
        code, out = self._run(
            bb_version_check=lambda: (
                session_gate.FAIL,
                f"bb 0.0.1 != pinned {PINNED_BB_VERSION}",
            )
        )
        self.assertEqual(0, code)
        self.assertIn("bb version: FAIL", out)
        self.assertIn("SESSION SETUP INCOMPLETE", out)

    def test_stale_watcher_marker_is_a_visible_fail(self) -> None:
        code, out = self._run(
            check_markers=[
                {"name": name, "status": "absent", "marker": f"/state/{name}.alive"}
                for name in WATCHER_NAMES
            ]
        )
        self.assertEqual(0, code)
        self.assertIn("watcher worker-lifecycle [llm-collab]: FAIL", out)
        self.assertIn("SESSION SETUP INCOMPLETE", out)

    def test_output_names_the_project_it_checked(self) -> None:
        """I5: coverage is observable — the output answers 'which project did
        this check?' without reading the source (GH-726 S3's stated property;
        per-checkout dispatch itself is S3's own lane)."""
        code, out = self._run(
            bb_version_check=lambda: (
                session_gate.PASS,
                f"bb {PINNED_BB_VERSION} == pinned {PINNED_BB_VERSION}",
            )
        )
        self.assertEqual(0, code)
        self.assertIn(f"project: {session_gate.HOOK_PROJECT_ID}", out)
        self.assertIn(f"[{session_gate.HOOK_PROJECT_ID}]", out)

    def test_fresh_marker_owned_by_the_same_session_passes_silently(self) -> None:
        code, out = self._run(
            own_session_id="sess-own",
            bb_version_check=lambda: (
                session_gate.PASS,
                f"bb {PINNED_BB_VERSION} == pinned {PINNED_BB_VERSION}",
            ),
        )
        self.assertEqual(0, code)
        self.assertIn("watcher worker-lifecycle [llm-collab]: PASS", out)
        self.assertIn("owned by this session", out)
        self.assertNotIn("FOREIGN", out)
        self.assertNotIn("SESSION SETUP INCOMPLETE", out)

    def test_fresh_marker_owned_by_a_different_session_alerts_naming_it(self) -> None:
        code, out = self._run(
            own_session_id="sess-current",
            check_markers=[
                {
                    "name": name,
                    "status": "fresh",
                    "age_seconds": 5.0,
                    "session_id": "sess-predecessor",
                }
                for name in WATCHER_NAMES
            ],
            bb_version_check=lambda: (
                session_gate.PASS,
                f"bb {PINNED_BB_VERSION} == pinned {PINNED_BB_VERSION}",
            ),
        )
        self.assertEqual(0, code)
        self.assertIn("watcher worker-lifecycle [llm-collab]: FAIL", out)
        self.assertIn("FOREIGN session sess-predecessor", out)
        self.assertIn("TaskStop", out)
        self.assertIn("SESSION SETUP INCOMPLETE", out)

    def test_unknown_current_identity_is_loud_not_a_pass(self) -> None:
        code, out = self._run(
            own_session_id=None,
            bb_version_check=lambda: (
                session_gate.PASS,
                f"bb {PINNED_BB_VERSION} == pinned {PINNED_BB_VERSION}",
            ),
        )
        self.assertEqual(0, code)
        self.assertIn("watcher ownership: UNKNOWN", out)
        self.assertIn("ownership unverified", out)
        self.assertIn("SESSION SETUP INCOMPLETE", out)

    def test_all_checks_passing_is_clean_and_points_at_the_docs(self) -> None:
        code, out = self._run(
            bb_version_check=lambda: (
                session_gate.PASS,
                f"bb {PINNED_BB_VERSION} == pinned {PINNED_BB_VERSION}",
            )
        )
        self.assertEqual(0, code)
        self.assertNotIn("SESSION SETUP INCOMPLETE", out)
        self.assertIn(session_gate.ORCHESTRATOR_DOC, out)
        self.assertIn("handoff:", out)

    def test_handoff_pointer_is_project_scoped_and_reports_absence(self) -> None:
        """The handoff signpost resolves through the project state root (GH-726
        S6 amended: runtime state, not a checkout document), and an absent file
        is stated on the line rather than implied."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = root / "llm-collab" / "orchestrator-handoff.md"
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
            ):
                absent_line = session_gate.handoff_line()
                expected.parent.mkdir(parents=True)
                expected.write_text("# handoff\n", encoding="utf-8")
                present_line = session_gate.handoff_line()
        self.assertIn(str(expected), absent_line)
        self.assertIn("ABSENT", absent_line)
        self.assertIn(str(expected), present_line)
        self.assertNotIn("ABSENT", present_line)

    def test_a_broken_gate_probe_is_loud_and_never_fails_the_session(self) -> None:
        with mock.patch.object(
            session_gate, "current_session_id", return_value="sess-own"
        ), mock.patch.object(session_gate, "run_checks", side_effect=RuntimeError("boom")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = session_gate.main()
        out = output.getvalue()
        self.assertEqual(0, code)
        self.assertIn("session-gate itself: UNKNOWN", out)
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn(": PASS", out)

    def test_current_session_id_comes_from_the_hook_payload_only(self) -> None:
        payload = io.StringIO(json.dumps({"session_id": "sess-hook", "hook_event_name": "SessionStart"}))
        self.assertEqual("sess-hook", session_gate.current_session_id(payload))
        self.assertIsNone(session_gate.current_session_id(io.StringIO("not json")))
        self.assertIsNone(session_gate.current_session_id(io.StringIO('{"session_id": 7}')))
        self.assertIsNone(session_gate.current_session_id(io.StringIO("")))
        tty = mock.Mock()
        tty.isatty.return_value = True
        self.assertIsNone(session_gate.current_session_id(tty))
        tty.read.assert_not_called()

    def test_over_cap_bb_probe_is_unknown_and_does_not_raise(self) -> None:
        """An unbounded bb (corrupt or regressed) cannot grow the hook's memory:
        the shared bounded transport raises on overflow and the probe reports
        UNKNOWN through the same path as any other broken probe."""
        script = (
            "import sys; sys.stdout.write('x' * "
            f"{session_gate.BB_PROBE_MAX_RESPONSE_CHARS + 1})"
        )
        bounded = subprocess_transport(
            [sys.executable, "-c", script],
            max_response_chars=session_gate.BB_PROBE_MAX_RESPONSE_CHARS,
        )
        # The probe resolves its executable through the project registry (GH-728),
        # so the registry read is mocked too: no test may depend on the live tree's
        # own collab.config.json being present.
        configured = {"bb": {"enabled": True, "executable": ["configured-bb"]}}
        with mock.patch.object(session_gate, "get_project", return_value=configured):
            with mock.patch.object(session_gate, "subprocess_transport", return_value=bounded):
                status, detail = session_gate.bb_version_check()
        self.assertEqual(session_gate.UNKNOWN, status)
        self.assertIn("exceeded", detail)

    def test_unconfigured_project_probe_is_unknown_not_a_path_fallback(self) -> None:
        """GH-728: no configured bb.executable is UNKNOWN, never a bare PATH probe."""
        with mock.patch.object(session_gate, "get_project", return_value=None):
            with mock.patch.object(session_gate, "subprocess_transport") as transport_factory:
                status, detail = session_gate.bb_version_check()
        self.assertEqual(session_gate.UNKNOWN, status)
        transport_factory.assert_not_called()

    def test_real_bb_probe_shape(self) -> None:
        """The unpatched bb probe classifies a well-formed envelope."""
        configured = {"bb": {"enabled": True, "executable": ["configured-bb"]}}
        envelope = BbTransportResult(0, json.dumps({"currentVersion": PINNED_BB_VERSION}), "")
        with mock.patch.object(session_gate, "get_project", return_value=configured):
            with mock.patch.object(
                session_gate, "subprocess_transport", return_value=mock.Mock(return_value=envelope)
            ) as transport_factory:
                status, _detail = session_gate.bb_version_check()
        self.assertEqual(session_gate.PASS, status)
        # The transport is built from the configured argv, not a bare PATH bb.
        self.assertEqual(["configured-bb"], transport_factory.call_args.args[0])

        with mock.patch.object(session_gate, "get_project", return_value=configured):
            with mock.patch.object(
                session_gate,
                "subprocess_transport",
                return_value=mock.Mock(side_effect=OSError("no bb")),
            ):
                status, _detail = session_gate.bb_version_check()
        self.assertEqual(session_gate.UNKNOWN, status)


if __name__ == "__main__":
    unittest.main()
