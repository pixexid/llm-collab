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

    def test_repo_targets_frontmatter_attributes_bare_numbers(self) -> None:
        """`repo_targets: ["llm-collab"]` IS an explicit attribution.

        Ignoring it left the hint inert on every real packet, since our packets all write
        bare numbers and name the repo in frontmatter -- the field the delivery contract
        uses for exactly this.
        """
        relpath = self.packet('---\nproject_id: amiga\n'
                              'repo_targets: ["llm-collab"]\n---\nHeld on #170.')
        seen = []

        def run(argv, **kwargs):
            seen.append(argv[argv.index("--repo") + 1])
            return mock.Mock(stdout=json.dumps({"state": "MERGED"}))

        with mock.patch.object(operator_digest, "known_repo_targets",
                               return_value={"llm-collab": "pixexid/llm-collab"}):
            with mock.patch.object(operator_digest.subprocess, "run", side_effect=run):
                settled, note = operator_digest.resolution_hint(relpath)
        self.assertEqual(["pixexid/llm-collab"], seen)
        self.assertTrue(settled)
        self.assertNotIn("not checked", note)

    def test_two_repo_targets_stay_ambiguous(self) -> None:
        relpath = self.packet('---\nrepo_targets: ["llm-collab", "amiga"]\n---\nSee #170.')
        with mock.patch.object(operator_digest.subprocess, "run",
                               side_effect=AssertionError("must not look up an ambiguous ref")):
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled)
        self.assertIn("not checked", note)

    def test_an_unknown_repo_target_is_not_guessed(self) -> None:
        relpath = self.packet('---\nrepo_targets: ["some-other-repo"]\n---\nSee #170.')

        def no_pr_lookup(argv, **kwargs):
            # prohibit only the PR lookup; a blanket stub also blocked git origin discovery
            self.assertNotIn("pr", argv, "an unknown repo target must not be looked up")
            return mock.Mock(stdout="", returncode=0)

        with mock.patch.object(operator_digest, "known_repo_targets",
                               return_value={"llm-collab": "pixexid/llm-collab"}):
            with mock.patch.object(operator_digest.subprocess, "run", side_effect=no_pr_lookup):
                settled, _ = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled)

    def test_no_repo_targets_leaves_bare_numbers_unresolved(self) -> None:
        relpath = self.packet("---\nproject_id: amiga\n---\nSee #170.")
        with mock.patch.object(operator_digest.subprocess, "run",
                               side_effect=AssertionError("project_id alone is not attribution")):
            settled, note = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled)
        self.assertIn("not checked", note)

    def test_no_references_yields_no_hint(self) -> None:
        relpath = self.packet("Please decide whether to park the sidecar work.")
        with self.fake_gh({}):
            self.assertEqual((False, ""), operator_digest.resolution_hint(relpath))


if __name__ == "__main__":
    unittest.main()


class RepoEnumerationTest(unittest.TestCase):
    """The PR section must cover every registered repo, and name what it could not read.

    Fixture-only. Reading the live projects.json and the live git origin made these fail in
    a detached checkout, which is the third time tonight I let a unit test depend on ignored
    runtime state.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "projects.json").write_text(json.dumps({"projects": [
            {"id": "amiga", "github": {"repo": "pixexid/amiga"}},
            {"id": "nuvyr", "github": {"repo": "pixexid/nuvyr"}},
            {"id": "no-github"},
            # id differs from the repo basename: the ONLY case where the explicit id alias
            # matters, since register() already contributes the basename itself
            {"id": "web", "github": {"repo": "pixexid/nuvyr_app"}},
        ]}), encoding="utf-8")
        for patcher in (
            mock.patch.object(operator_digest, "ROOT", self.root),
            mock.patch.object(operator_digest, "_repo_targets_cache", None),
            mock.patch.object(operator_digest, "git_origin_slug",
                              return_value="pixexid/llm-collab"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_workspaces_own_repo_is_registered_from_the_origin(self) -> None:
        """A swallowed NameError once dropped it, hiding our own PRs from the operator.

        The digest reported other projects' PRs under the heading "Open pull requests"
        while omitting the five in front of the reader. The origin's basename supplies the
        alias, so no ignored config file needs to be readable for this to work.
        """
        targets = operator_digest.known_repo_targets()
        self.assertEqual("pixexid/llm-collab", targets.get("llm-collab"))
        self.assertIn("pixexid/llm-collab", set(targets.values()))

    def test_registered_project_repos_are_included(self) -> None:
        targets = operator_digest.known_repo_targets()
        self.assertEqual("pixexid/amiga", targets.get("amiga"))
        self.assertEqual("pixexid/nuvyr", targets.get("nuvyr"))
        self.assertNotIn("no-github", targets, "a project with no repo registers nothing")
        self.assertEqual("pixexid/nuvyr_app", targets.get("web"),
                         "a project id that differs from its repo basename must still map")

    def test_an_unreadable_origin_does_not_break_enumeration(self) -> None:
        with mock.patch.object(operator_digest, "git_origin_slug", return_value=None):
            with mock.patch.object(operator_digest, "_repo_targets_cache", None):
                targets = operator_digest.known_repo_targets()
        self.assertEqual({"pixexid/amiga", "pixexid/nuvyr", "pixexid/nuvyr_app"},
                         set(targets.values()))

    def test_open_prs_labels_each_row_and_reports_unreachable_repos(self) -> None:
        def run(argv, **kwargs):
            slug = argv[argv.index("--repo") + 1]
            if slug == "pixexid/nuvyr":
                raise RuntimeError("gh failed")
            return mock.Mock(stdout=json.dumps([{
                "number": 1, "title": "t", "isDraft": True,
                "headRefName": "b", "headRefOid": "a" * 40,
            }]))

        with mock.patch.object(operator_digest, "known_repo_targets",
                               return_value={"a": "pixexid/llm-collab", "b": "pixexid/nuvyr"}):
            with mock.patch.object(operator_digest.subprocess, "run", side_effect=run):
                rows, unreachable = operator_digest.open_prs()
        self.assertEqual(["pixexid/llm-collab"], [r["repo"] for r in rows],
                         "every row must carry the repo it came from")
        self.assertEqual(["pixexid/nuvyr"], unreachable,
                         "a repo we could not read must be named, not rendered as empty")
