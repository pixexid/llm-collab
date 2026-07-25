"""The digest may not label a live request moot.

The whole point of the hint is that a packet sitting unread forever is not the same as
a decision still needed. That only helps if the hint is conservative: a wrong "moot" on
a live request is worse than no hint, because it teaches the operator to skip the queue.
An earlier version claimed moot when ANY referenced PR had merged, which mislabelled a
packet carrying three decisions because one of the three had shipped.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import operator_digest  # noqa: E402


class ResolutionHintTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Tasks" / "done").mkdir(parents=True)
        patcher = mock.patch.object(operator_digest, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def packet(self, body: str) -> str:
        path = self.root / "packet.md"
        path.write_text(body, encoding="utf-8")
        return "packet.md"

    def fake_gh(self, states: dict[int, str]):
        def run(argv, **kwargs):
            number = int(argv[argv.index("view") + 1])
            if number not in states:
                raise RuntimeError("no such pr")
            return mock.Mock(stdout=json.dumps({"state": states[number]}))
        return mock.patch.object(operator_digest.subprocess, "run", side_effect=run)

    def test_one_merged_pr_among_open_ones_is_not_moot(self) -> None:
        relpath = self.packet("Three decisions. See #299 and #302.")
        with self.fake_gh({299: "MERGED", 302: "OPEN"}):
            hint = operator_digest.resolution_hint(relpath)
        self.assertIn("PARTIAL", hint)
        self.assertIn("#302", hint, "the still-open PR must be named")
        self.assertNotIn("already settled", hint,
                         "a partially settled packet must not read as fully settled")

    def test_all_merged_prs_are_reported_settled(self) -> None:
        relpath = self.packet("Held on PR #170 and #171 until ratified.")
        with self.fake_gh({170: "MERGED", 171: "CLOSED"}):
            hint = operator_digest.resolution_hint(relpath)
        self.assertIn("already settled", hint)
        self.assertIn("#170", hint)
        self.assertIn("#171", hint)

    def test_an_unreachable_pr_counts_against_moot_rather_than_for_it(self) -> None:
        relpath = self.packet("See #299 and #4242.")
        with self.fake_gh({299: "MERGED"}):  # #4242 lookup raises
            hint = operator_digest.resolution_hint(relpath)
        self.assertIn("PARTIAL", hint)
        self.assertIn("#4242", hint, "an unknown state must be treated as still open")

    def test_tasks_still_require_every_reference_done(self) -> None:
        (self.root / "Tasks" / "done" / "x__TASK-8CED1C.md").write_text("x", encoding="utf-8")
        relpath = self.packet("Ratify for TASK-8CED1C and TASK-999999.")
        with self.fake_gh({}):
            hint = operator_digest.resolution_hint(relpath)
        self.assertNotIn("task(s) completed", hint,
                         "one done task among two must not read as completed")

    def test_a_partial_hint_renders_as_awaiting_you_not_moot(self) -> None:
        """Assert the OPERATOR-FACING row, not the helper's return value.

        The helper was correct while the row built from it read
        "**likely moot** — PARTIAL — settled: #299; still open: #302": a status telling
        the reader to skip an item while naming the live work inside it. Testing the
        helper alone could never catch that.
        """
        relpath = self.packet("Three decisions. See #299 and #302.")
        with self.fake_gh({299: "MERGED", 302: "OPEN"}):
            status = operator_digest.decision_status(relpath)
        self.assertIn("awaiting you", status)
        self.assertNotIn("likely moot", status,
                         "a row naming still-open work must never read as moot")
        self.assertIn("#302", status)

    def test_a_fully_settled_hint_still_renders_as_moot(self) -> None:
        relpath = self.packet("Held on PR #170 and #171.")
        with self.fake_gh({170: "MERGED", 171: "MERGED"}):
            status = operator_digest.decision_status(relpath)
        self.assertIn("likely moot", status)

    def test_a_packet_with_no_references_renders_as_awaiting_you(self) -> None:
        relpath = self.packet("Please decide whether to park the sidecar work.")
        with self.fake_gh({}):
            self.assertEqual("awaiting you", operator_digest.decision_status(relpath))

    def test_a_foreign_project_packets_pr_numbers_are_not_checked_here(self) -> None:
        """Another project numbers its PRs in another repo."""
        relpath = self.packet("---\nproject_id: nuvyr\n---\nBlocked on #170.")
        called = []

        def run(argv, **kwargs):
            called.append(argv)
            raise AssertionError("must not reach gh for a foreign-project packet")

        with mock.patch.object(operator_digest.subprocess, "run", side_effect=run):
            hint = operator_digest.resolution_hint(relpath)
        self.assertEqual([], called)
        self.assertIn("not checked", hint)
        self.assertNotIn("already settled", hint)

    def test_an_amiga_packet_is_still_checked_against_this_repo(self) -> None:
        relpath = self.packet("---\nproject_id: amiga\n---\nHeld on #170.")
        with self.fake_gh({170: "MERGED"}):
            hint = operator_digest.resolution_hint(relpath)
        self.assertIn("already settled", hint)

    def test_no_references_yields_no_hint(self) -> None:
        relpath = self.packet("Please decide whether to park the sidecar work.")
        with self.fake_gh({}):
            self.assertEqual("", operator_digest.resolution_hint(relpath))


if __name__ == "__main__":
    unittest.main()
