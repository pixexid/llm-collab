"""Focused proof for the SessionStart hook and watcher liveness markers (GH-722)."""

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
    not_fresh,
)
from llm_collab.bb_client import PINNED_BB_VERSION  # noqa: E402


class MarkerFreshnessTest(unittest.TestCase):
    """Marker classification in both directions; a broken probe is never fresh."""

    def _check(self, directory: str, *, now: float) -> list[dict]:
        with mock.patch.object(
            _watcher_liveness, "markers_dir", return_value=Path(directory)
        ):
            return check_markers("proj-under-test", now=now)

    def _write_markers(self, directory: str, *, mtime: float) -> None:
        for name in WATCHER_NAMES:
            marker = Path(directory) / f"{name}.alive"
            marker.write_text("alive\n", encoding="utf-8")
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
                marker.write_text("alive\n", encoding="utf-8")
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

    def test_fresh_markers_classify_fresh(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_markers(temporary, mtime=now - 30)
            report = self._check(temporary, now=now)
        self.assertEqual(["fresh"] * len(WATCHER_NAMES), [e["status"] for e in report])
        self.assertEqual([], not_fresh(report))

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

    def test_unreadable_marker_is_unreadable_never_fresh(self) -> None:
        with mock.patch.object(
            Path, "stat", side_effect=PermissionError("denied")
        ), tempfile.TemporaryDirectory() as temporary:
            report = self._check(temporary, now=time.time())
        self.assertEqual(
            ["unreadable"] * len(WATCHER_NAMES), [e["status"] for e in report]
        )
        self.assertEqual(len(WATCHER_NAMES), len(not_fresh(report)))


class _Completed:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class SessionGateTest(unittest.TestCase):
    """The hook prints check results and pointers, and never fails the session."""

    def _run(self, **patches) -> tuple[int, str]:
        tooling = patches.pop(
            "tooling_currency",
            {"state": "current", "head": "abcdef0", "fetched": True},
        )
        markers = patches.pop(
            "check_markers",
            [
                {"name": name, "status": "fresh", "age_seconds": 5.0}
                for name in WATCHER_NAMES
            ],
        )
        with mock.patch.object(
            session_gate.session_bootstrap, "tooling_currency", return_value=tooling
        ), mock.patch.object(session_gate, "check_markers", return_value=markers):
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
        self.assertIn("watcher worker-lifecycle: FAIL", out)
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
            session_gate, "run_checks", side_effect=RuntimeError("boom")
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = session_gate.main()
        out = output.getvalue()
        self.assertEqual(0, code)
        self.assertIn("session-gate itself: UNKNOWN", out)
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn(": PASS", out)

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
