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

import _watcher_liveness  # noqa: E402
import session_gate  # noqa: E402
from _watcher_liveness import (  # noqa: E402
    WATCHER_MARKER_STALE_AFTER_SECONDS,
    WATCHER_NAMES,
    check_markers,
    foreign_fresh,
    not_fresh,
    write_marker,
)
from llm_collab.bb_client import PINNED_BB_VERSION  # noqa: E402


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

    def test_unreadable_marker_is_unreadable_never_fresh(self) -> None:
        with mock.patch.object(
            Path, "stat", side_effect=PermissionError("denied")
        ), tempfile.TemporaryDirectory() as temporary:
            report = self._check(temporary, now=time.time())
        self.assertEqual(
            ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
        )
        self.assertEqual(len(WATCHER_NAMES), len(not_fresh(report)))


class MarkerOwnershipTest(unittest.TestCase):
    """foreign_fresh: fresh markers owned by a DIFFERENT session, and only those."""

    @staticmethod
    def _report(owner: str) -> list[dict]:
        return [
            {"name": name, "status": "fresh", "age_seconds": 5.0, "session_id": owner}
            for name in WATCHER_NAMES
        ]

    def test_same_session_owner_is_not_foreign(self) -> None:
        self.assertEqual([], foreign_fresh(self._report("sess-mine"), "sess-mine"))

    def test_different_session_owner_is_foreign(self) -> None:
        foreign = foreign_fresh(self._report("sess-predecessor"), "sess-current")
        self.assertEqual(len(WATCHER_NAMES), len(foreign))
        self.assertEqual({"sess-predecessor"}, {e["session_id"] for e in foreign})

    def test_unknown_current_identity_compares_nothing(self) -> None:
        self.assertEqual([], foreign_fresh(self._report("sess-predecessor"), None))

    def test_stale_foreign_marker_is_not_a_fresh_foreign_alert(self) -> None:
        report = self._report("sess-predecessor")
        for entry in report:
            entry["status"] = "stale"
        self.assertEqual([], foreign_fresh(report, "sess-current"))


class MarkerWriterTest(unittest.TestCase):
    """write_marker is the single documented writing shape (GH-726 S2)."""

    def test_written_marker_reads_back_fresh_and_owned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(
                _watcher_liveness,
                "project_state_dir",
                side_effect=lambda project_id: root / project_id,
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
            ):
                with self.assertRaises(ValueError):
                    write_marker("project-a", "../escape", "sess-1")
                with self.assertRaises(ValueError):
                    write_marker("project-a", "heartbeat", "")


class _Completed:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


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
        self.assertIn(session_gate.HANDOFF_FILE, out)

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

    def test_real_bb_probe_shape(self) -> None:
        """The unpatched bb probe classifies a well-formed envelope."""
        envelope = _Completed(0, json.dumps({"currentVersion": PINNED_BB_VERSION}))
        with mock.patch.object(
            session_gate.subprocess, "run", return_value=envelope
        ):
            status, _detail = session_gate.bb_version_check()
        self.assertEqual(session_gate.PASS, status)

        with mock.patch.object(
            session_gate.subprocess, "run", side_effect=OSError("no bb")
        ):
            status, _detail = session_gate.bb_version_check()
        self.assertEqual(session_gate.UNKNOWN, status)


if __name__ == "__main__":
    unittest.main()
