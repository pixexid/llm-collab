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

    def test_one_settled_pr_among_open_ones_is_not_fully_settled(self) -> None:
        relpath = self.packet("Three decisions. See pixexid/llm-collab#299 and "
                              "pixexid/llm-collab#302.")
        with self.fake_gh({299: "MERGED", 302: "OPEN"}):
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled)
        self.assertIn("#302", note, "the still-open PR must be named")

    def test_all_settled_qualified_prs_are_fully_settled(self) -> None:
        relpath = self.packet("Held on pixexid/llm-collab#170 and pixexid/llm-collab#171.")
        with self.fake_gh({170: "MERGED", 171: "CLOSED"}):
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertTrue(settled)
        self.assertIn("#170", note)

    def test_an_unreachable_pr_counts_against_settlement(self) -> None:
        relpath = self.packet("See pixexid/llm-collab#299 and pixexid/llm-collab#4242.")
        with self.fake_gh({299: "MERGED"}):  # #4242 lookup raises
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled)
        self.assertIn("#4242", note, "an unknown state must be treated as still open")

    def test_a_bare_pr_number_is_never_guessed_against_this_repo(self) -> None:
        """`#170` names no repository, and amiga's registered repo is pixexid/amiga."""
        relpath = self.packet("Blocked on #170 until ratified.")
        called = []

        def run(argv, **kwargs):
            called.append(argv)
            raise AssertionError("a bare reference must not be looked up anywhere")

        with mock.patch.object(operator_digest.subprocess, "run", side_effect=run):
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertEqual([], called)
        self.assertFalse(settled, "an unattributable reference cannot settle anything")
        self.assertIn("not checked", note)

    def test_a_done_task_with_a_bare_pr_is_not_moot(self) -> None:
        """The exact shape that rendered moot when authority came from the note's prefix."""
        (self.root / "Tasks" / "done" / "x__TASK-8CED1C.md").write_text("x", encoding="utf-8")
        relpath = self.packet("Ratify option A for TASK-8CED1C; #170 and #171 are held.")
        with mock.patch.object(operator_digest.subprocess, "run",
                               side_effect=AssertionError("no lookup for bare refs")):
            status = operator_digest.decision_status(relpath)
        self.assertIn("awaiting you", status)
        self.assertNotIn("likely moot", status)
        self.assertIn("TASK-8CED1C", status, "the settled part is still worth showing")

    def test_tasks_still_require_every_reference_done(self) -> None:
        (self.root / "Tasks" / "done" / "x__TASK-8CED1C.md").write_text("x", encoding="utf-8")
        relpath = self.packet("Ratify for TASK-8CED1C and TASK-999999.")
        with self.fake_gh({}):
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled, "one done task among two cannot settle the packet")
        self.assertIn("TASK-999999", note)

    def test_a_fully_settled_packet_renders_as_moot(self) -> None:
        (self.root / "Tasks" / "done" / "x__TASK-8CED1C.md").write_text("x", encoding="utf-8")
        relpath = self.packet("TASK-8CED1C, held on pixexid/llm-collab#170.")
        with self.fake_gh({170: "MERGED"}):
            status = operator_digest.decision_status(relpath)
        self.assertIn("likely moot", status)

    def test_a_packet_with_no_references_renders_as_awaiting_you(self) -> None:
        relpath = self.packet("Please decide whether to park the sidecar work.")
        with self.fake_gh({}):
            self.assertEqual("awaiting you", operator_digest.decision_status(relpath))

    def test_a_foreign_repo_reference_is_checked_against_that_repo(self) -> None:
        relpath = self.packet("Blocked on pixexid/amiga#170.")
        seen = []

        def run(argv, **kwargs):
            seen.append(argv[argv.index("--repo") + 1])
            return mock.Mock(stdout=json.dumps({"state": "MERGED"}))

        with mock.patch.object(operator_digest.subprocess, "run", side_effect=run):
            settled, _ = operator_digest.resolution_hint(relpath)
        self.assertEqual(["pixexid/amiga"], seen,
                         "the reference's own repo must be queried, not this one")
        self.assertTrue(settled)

    def test_no_references_yields_no_hint(self) -> None:
        relpath = self.packet("Please decide whether to park the sidecar work.")
        with self.fake_gh({}):
            self.assertEqual((False, ""), operator_digest.resolution_hint(relpath))


if __name__ == "__main__":
    unittest.main()
