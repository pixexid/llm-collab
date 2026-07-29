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
        (self.root / "projects.json").write_text(json.dumps({"projects": [
            {"id": "amiga", "repos": {"app": "amiga"}, "github": {"repo": "pixexid/amiga"}},
        ]}), encoding="utf-8")
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

    def _targets_for(self, projects: list[dict], origin: str | None = "pixexid/llm-collab"):
        (self.root / "projects.json").write_text(json.dumps({"projects": projects}),
                                                 encoding="utf-8")
        with mock.patch.object(operator_digest, "_repo_targets_cache", None):
            with mock.patch.object(operator_digest, "git_origin_slug", return_value=origin):
                return operator_digest.known_repo_targets()

    def test_a_project_id_colliding_with_another_repos_basename_fails_closed(self) -> None:
        """Last-write-wins silently retargeted the collision to the wrong repo.

        Project id `shared` names owner/first, while another repo's BASENAME is also
        `shared`. A packet scoped to project shared then queried owner/shared and could
        report a live request as moot on a same-numbered PR from the wrong repository.
        """
        targets = self._targets_for([
            {"id": "shared", "github": {"repo": "pixexid/first"}},
            {"id": "other", "github": {"repo": "pixexid/shared"}},
        ])
        self.assertIsNone(targets.get("shared"),
                          "an alias claimed by two repos must resolve to neither")
        # both repos remain enumerable, and both stay reachable by their full slug
        self.assertEqual({"pixexid/first", "pixexid/shared", "pixexid/llm-collab"},
                         set(targets.values()))
        self.assertEqual("pixexid/first", targets.get("pixexid/first"))
        self.assertEqual("pixexid/shared", targets.get("pixexid/shared"))
        self.assertEqual("other", "other")
        self.assertEqual("pixexid/shared", targets.get("other"),
                         "an unambiguous alias for the same repo still resolves")

    def test_two_projects_declaring_the_same_repo_keep_their_aliases(self) -> None:
        # the same slug claimed twice is not ambiguity -- it is one destination
        targets = self._targets_for([
            {"id": "app", "github": {"repo": "pixexid/nuvyr"}},
            {"id": "web", "github": {"repo": "pixexid/nuvyr"}},
        ])
        self.assertEqual("pixexid/nuvyr", targets.get("app"))
        self.assertEqual("pixexid/nuvyr", targets.get("web"))
        self.assertEqual("pixexid/nuvyr", targets.get("nuvyr"))

    def test_two_projects_sharing_one_id_across_different_repos_fails_closed(self) -> None:
        targets = self._targets_for([
            {"id": "app", "github": {"repo": "pixexid/one"}},
            {"id": "app", "github": {"repo": "pixexid/two"}},
        ])
        self.assertIsNone(targets.get("app"))
        self.assertEqual({"pixexid/one", "pixexid/two", "pixexid/llm-collab"},
                         set(targets.values()))

    def test_an_ambiguous_alias_cannot_resolve_a_bare_pr_reference(self) -> None:
        """The whole point: ambiguity must not settle anything."""
        packet = self.root / "packet.md"
        packet.write_text('---\nrepo_targets: ["shared"]\n---\nHeld on #170.',
                          encoding="utf-8")
        relpath = "packet.md"

        def no_pr_lookup(argv, **kwargs):
            self.assertNotIn("pr", argv, "an ambiguous alias must not be looked up")
            return mock.Mock(stdout="", returncode=0)

        targets = self._targets_for([
            {"id": "shared", "github": {"repo": "pixexid/first"}},
            {"id": "other", "github": {"repo": "pixexid/shared"}},
        ])
        with mock.patch.object(operator_digest, "known_repo_targets", return_value=targets):
            with mock.patch.object(operator_digest.subprocess, "run",
                                   side_effect=no_pr_lookup):
                settled, note = operator_digest.resolution_hint(relpath)
        self.assertFalse(settled)
        self.assertIn("not checked", note)

    def _scoped_projects(self) -> None:
        (self.root / "projects.json").write_text(json.dumps({"projects": [
            {"id": "amiga", "repos": {"app": "amiga", "docs": "amiga_docs"},
             "github": {"repo": "pixexid/amiga"}},
            {"id": "nuvyr", "repos": {"app": "nuvyr_app"},
             "github": {"repo": "pixexid/nuvyr"}},
        ]}), encoding="utf-8")

    def _checkouts(self, mapping: dict):
        """Stub the checkout->origin lookup; a separate test drives real git."""
        # mapping is keyed by (project_id, repo_key), matching the real signature
        return mock.patch.object(
            operator_digest, "checkout_origin_slug",
            side_effect=lambda project_id, repo_key: mapping.get((project_id, repo_key)))

    def test_the_logical_app_key_resolves_per_project(self) -> None:
        """`repo_targets: ["app"]` means a different repo in each project.

        And the value is a CHECKOUT name, not a repository name: the nuvyr_app checkout's
        origin is pixexid/nuvyr, and amiga_docs' is pixexid/amiga-docs. Concatenating an owner
        with the directory name invents a slug that matches neither.
        """
        self._scoped_projects()
        with self._checkouts({("amiga", "app"): "pixexid/amiga",
                              ("nuvyr", "app"): "pixexid/nuvyr",
                              ("amiga", "docs"): "pixexid/amiga-docs"}):
            self.assertEqual("pixexid/amiga",
                             operator_digest.resolve_repo_target("app", "amiga"))
            self.assertEqual("pixexid/nuvyr",
                             operator_digest.resolve_repo_target("app", "nuvyr"))
            self.assertEqual("pixexid/amiga-docs",
                             operator_digest.resolve_repo_target("docs", "amiga"))

    def test_a_checkout_whose_origin_cannot_be_read_resolves_nothing(self) -> None:
        self._scoped_projects()
        with self._checkouts({}):
            self.assertIsNone(operator_digest.resolve_repo_target("app", "amiga"),
                              "no origin means no slug, never a guessed one")

    def test_an_unknown_project_scope_never_falls_through_globally(self) -> None:
        """`ghost` + `llm-collab` resolved to this repo on the strength of a name.

        A packet scoped to a project we do not recognise must attribute nothing.
        """
        self._scoped_projects()
        self.assertIsNone(operator_digest.resolve_repo_target("llm-collab", "ghost"))
        self.assertIsNone(operator_digest.resolve_repo_target("app", "ghost"))

    def test_a_project_id_named_app_cannot_retarget_any_projects_app_key(self) -> None:
        """The retargeting hazard, stated as a test.

        Registering a project whose id happens to be `app` would, under a global alias table,
        silently redirect every project's `app` packet to that repo.
        """
        (self.root / "projects.json").write_text(json.dumps({"projects": [
            {"id": "amiga", "repos": {"app": "amiga"}, "github": {"repo": "pixexid/amiga"}},
            {"id": "nuvyr", "repos": {"app": "nuvyr_app"}, "github": {"repo": "pixexid/nuvyr"}},
            {"id": "app", "github": {"repo": "pixexid/hijack"}},
        ]}), encoding="utf-8")
        with self._checkouts({("amiga", "app"): "pixexid/amiga",
                              ("nuvyr", "app"): "pixexid/nuvyr"}):
            self.assertEqual("pixexid/amiga",
                             operator_digest.resolve_repo_target("app", "amiga"))
            self.assertEqual("pixexid/nuvyr",
                             operator_digest.resolve_repo_target("app", "nuvyr"))
            # the id alias remains reachable only where no project scope claims the key
            self.assertEqual("pixexid/hijack",
                             operator_digest.resolve_repo_target("app", None))

    def test_a_logical_key_absent_from_that_project_falls_back_to_global(self) -> None:
        self._scoped_projects()
        self.assertEqual("pixexid/llm-collab",
                         operator_digest.resolve_repo_target("llm-collab", "amiga"))

    def test_an_app_packet_resolves_its_own_projects_repo(self) -> None:
        self._scoped_projects()
        packet = self.root / "packet.md"
        packet.write_text('---\nproject_id: nuvyr\nrepo_targets: ["app"]\n---\nHeld on #170.',
                          encoding="utf-8")
        seen = []

        def run(argv, **kwargs):
            seen.append(argv[argv.index("--repo") + 1])
            return mock.Mock(stdout=json.dumps({"state": "MERGED"}))

        with self._checkouts({("nuvyr", "app"): "pixexid/nuvyr"}):
            with mock.patch.object(operator_digest.subprocess, "run", side_effect=run):
                settled, _ = operator_digest.resolution_hint("packet.md")
        self.assertEqual(["pixexid/nuvyr"], seen,
                         "the checkout's real origin decides the repo, not its directory name")
        self.assertTrue(settled)

    def test_a_project_with_no_github_owner_resolves_nothing(self) -> None:
        (self.root / "projects.json").write_text(json.dumps({"projects": [
            {"id": "orphan", "repos": {"app": "somewhere"}},
        ]}), encoding="utf-8")
        self.assertIsNone(operator_digest.resolve_repo_target("app", "orphan"),
                          "a logical key with no owner must not be half-resolved")

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


class CheckoutOriginTest(unittest.TestCase):
    """The slug comes from the checkout's own git origin, exercised against real git."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # stub the AUTHORITATIVE resolver, so this class still exercises the origin read
        patcher = mock.patch.object(
            operator_digest, "resolved_repo_path",
            side_effect=lambda project_id, repo_key: self.root / repo_key)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_checkout(self, name: str, remote: str | None) -> None:
        import subprocess as sp
        path = self.root / name
        path.mkdir()
        sp.run(["git", "init", "-q"], cwd=str(path), check=True,
               capture_output=True, timeout=30)
        if remote:
            sp.run(["git", "remote", "add", "origin", remote], cwd=str(path), check=True,
                   capture_output=True, timeout=30)

    def test_a_checkout_name_that_differs_from_its_repo_resolves_to_the_repo(self) -> None:
        # the live case: the nuvyr_app checkout is repository pixexid/nuvyr
        self.make_checkout("nuvyr_app", "https://github.com/pixexid/nuvyr.git")
        self.assertEqual("pixexid/nuvyr", operator_digest.checkout_origin_slug("nuvyr", "nuvyr_app"))

    def test_an_ssh_remote_resolves_too(self) -> None:
        self.make_checkout("docs", "git@github.com:pixexid/amiga-docs.git")
        self.assertEqual("pixexid/amiga-docs", operator_digest.checkout_origin_slug("amiga", "docs"))

    def test_a_checkout_with_no_remote_resolves_to_nothing(self) -> None:
        self.make_checkout("orphan", None)
        self.assertIsNone(operator_digest.checkout_origin_slug("amiga", "orphan"))

    def test_a_missing_checkout_resolves_to_nothing(self) -> None:
        self.assertIsNone(operator_digest.checkout_origin_slug("amiga", "not-cloned"))

    def test_a_local_path_origin_is_not_treated_as_a_slug(self) -> None:
        """`pixexid/amiga` and `../amiga` are relative PATHS, not repository references.

        Stripping an optional prefix and accepting whatever had one slash left admitted them,
        and querying that name hits an UNRELATED GitHub repo -- a false moot reached from a
        checkout that never touches GitHub at all.
        """
        for remote in ("pixexid/amiga", "../amiga", "/Users/pixexid/Projects/amiga",
                       "file:///tmp/amiga", "./sibling/repo"):
            with self.subTest(remote=remote):
                self.assertIsNone(operator_digest.slug_from_remote(remote))

    def test_every_github_transport_is_recognised(self) -> None:
        expected = "pixexid/amiga"
        for remote in ("https://github.com/pixexid/amiga.git",
                       "https://github.com/pixexid/amiga",
                       "https://token@github.com/pixexid/amiga.git",
                       "git@github.com:pixexid/amiga.git",
                       "ssh://git@github.com/pixexid/amiga",
                       "git://github.com/pixexid/amiga.git"):
            with self.subTest(remote=remote):
                self.assertEqual(expected, operator_digest.slug_from_remote(remote))

    def test_a_deeper_github_path_is_not_a_repository(self) -> None:
        self.assertIsNone(operator_digest.slug_from_remote("https://github.com/pixexid/a/b"))

    def test_a_non_github_remote_is_not_treated_as_a_slug(self) -> None:
        self.make_checkout("elsewhere", "https://gitlab.com/pixexid/thing.git")
        self.assertIsNone(operator_digest.checkout_origin_slug("amiga", "elsewhere"),
                          "gh pr view cannot query a non-GitHub host")


class PathHelperDelegationTest(unittest.TestCase):
    """Its own class: the real-git class stubs resolved_repo_path, which hides delegation."""

    def test_resolution_delegates_to_the_authoritative_path_helper(self) -> None:
        """A second implementation diverged from it on tilde and ../ forms."""
        seen = []

        def resolver(project_id, repo_key="app"):
            seen.append((project_id, repo_key))
            return Path("/nonexistent/nowhere")

        with mock.patch.object(operator_digest, "resolve_project_repo_path", side_effect=resolver):
            operator_digest.checkout_origin_slug("amiga", "docs")
        self.assertEqual([("amiga", "docs")], seen)

    def test_a_fatal_config_helper_does_not_kill_the_report(self) -> None:
        # resolve_project_repo_path reaches config_get, which exits when the ignored config
        # is absent; a read-only report must still render in a detached checkout
        with mock.patch.object(operator_digest, "resolve_project_repo_path",
                               side_effect=SystemExit(1)):
            self.assertIsNone(operator_digest.resolved_repo_path("amiga", "app"))


class ScopedDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "agents" / "operator").mkdir(parents=True)
        patcher = mock.patch.object(operator_digest, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def packet(self, name: str, *, project: str, chat: str, sender: str = "claude") -> str:
        relpath = f"Chats/{name}.md"
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"chat_id: {chat}\n"
            f"from: {sender}\n"
            f"sender_agent_id: {sender}\n"
            f"title: {name}\n"
            f"project_id: {project}\n"
            'repo_targets: ["llm-collab"]\n'
            "---\n\nbody\n",
            encoding="utf-8",
        )
        return relpath

    def test_project_and_chat_filter_unread_exactly(self) -> None:
        keep = self.packet("keep", project="llm-collab", chat="CHAT-A")
        other_chat = self.packet("other-chat", project="llm-collab", chat="CHAT-B")
        other_project = self.packet("other-project", project="amiga", chat="CHAT-A")
        inbox = {"unread": [keep, other_chat, other_project], "read": []}
        (self.root / "agents" / "operator" / "inbox.json").write_text(
            json.dumps(inbox), encoding="utf-8")

        self.assertEqual(
            [keep],
            operator_digest.filtered_unread("operator", "llm-collab", "CHAT-A"),
        )

    def test_project_and_chat_filter_sessions_exactly(self) -> None:
        records = [
            {"status": "active", "project_id": "llm-collab", "chat_id": "CHAT-A",
             "agent_id": "glim"},
            {"status": "active", "project_id": "llm-collab", "chat_id": "CHAT-B",
             "agent_id": "relay"},
            {"status": "active", "project_id": "amiga", "chat_id": "CHAT-A",
             "agent_id": "kimi"},
        ]
        with mock.patch("_session_autobridge.iter_sessions", return_value=records), \
             mock.patch("_session_autobridge.session_is_dispatchable",
                        return_value=(True, "")):
            live, stale = operator_digest.worker_sessions("llm-collab", "CHAT-A")
        self.assertEqual(["glim"], [record["agent_id"] for record in live])
        self.assertEqual(0, stale)

    def test_reply_delegates_to_deliver_without_reusing_sender_session(self) -> None:
        relpath = self.packet("decision", project="llm-collab", chat="CHAT-A")
        inbox_path = self.root / "agents" / "operator" / "inbox.json"
        inbox_path.write_text(json.dumps({"unread": [relpath], "read": []}), encoding="utf-8")
        marked = []

        with mock.patch.object(
            operator_digest.subprocess, "run", return_value=mock.Mock(returncode=0)
        ) as run, mock.patch.object(
            operator_digest, "mark_messages_read",
            side_effect=lambda agent, paths: marked.append((agent, paths)),
        ):
            code = operator_digest.reply_to_operator_packet(relpath, "/tmp/reply.md")

        command = run.call_args.args[0]
        self.assertEqual(0, code)
        self.assertEqual(["operator", [relpath]], [marked[0][0], marked[0][1]])
        self.assertNotIn("--target-session-id", command)
        self.assertEqual("claude", command[command.index("--to") + 1])
        self.assertEqual("CHAT-A", command[command.index("--chat") + 1])

    def test_nonzero_delivery_keeps_source_unread(self) -> None:
        relpath = self.packet("decision", project="llm-collab", chat="CHAT-A")
        (self.root / "agents" / "operator" / "inbox.json").write_text(
            json.dumps({"unread": [relpath], "read": []}), encoding="utf-8")

        with mock.patch.object(
            operator_digest.subprocess, "run", return_value=mock.Mock(returncode=2)
        ), mock.patch.object(operator_digest, "mark_messages_read") as mark:
            self.assertEqual(
                2, operator_digest.reply_to_operator_packet(relpath, "/tmp/reply.md"))
        mark.assert_not_called()

    def test_unread_without_matching_dispatchable_session_is_reported(self) -> None:
        relpath = self.packet("work", project="llm-collab", chat="CHAT-A", sender="glim")
        (self.root / "agents" / "operator" / "inbox.json").write_text(
            json.dumps({"unread": [], "read": []}), encoding="utf-8")
        (self.root / "agents" / "glim").mkdir(parents=True)
        (self.root / "agents" / "glim" / "inbox.json").write_text(
            json.dumps({"unread": [relpath], "read": []}), encoding="utf-8")

        with mock.patch.object(operator_digest, "agent_ids", return_value=["glim"]), \
             mock.patch.object(operator_digest, "worker_sessions", return_value=([], 0)), \
             mock.patch.object(operator_digest, "open_prs", return_value=([], [])), \
             mock.patch.object(operator_digest, "project_repo_slugs", return_value=set()):
            text = operator_digest.render("llm-collab", "CHAT-A")
        self.assertIn("work waiting; no dispatchable session", text)


class CanonicalWorkerJoinTest(unittest.TestCase):
    """#304: digest joins canonical worker binding state with unread outstanding."""

    WORKSPACE = "ws_alpha"
    NOW = "2026-07-29T00:00:00+00:00"
    EXPIRY = "2026-07-29T00:01:00+00:00"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "agents" / "operator").mkdir(parents=True)
        (self.root / "agents" / "operator" / "inbox.json").write_text(
            json.dumps({"unread": [], "read": []}), encoding="utf-8")
        for target, value in (("ROOT", self.root),):
            patcher = mock.patch.object(operator_digest, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        repo = self.root / "repo"
        repo.mkdir()
        self.cwd = repo / "work"
        self.cwd.mkdir()
        self.repo = repo
        from llm_collab.codex_runtime_home import bind_runtime_home
        self.runtime_home = bind_runtime_home(codex_home)
        from llm_collab.ledger import LedgerPaths
        self.paths = LedgerPaths.derive(self.root / "state", self.WORKSPACE)
        import llm_collab.ledger.store as store_module
        patcher = mock.patch.object(
            store_module, "_linked_sqlite_version_info", return_value=(3, 51, 3))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_worker(self, *, project: str, chat: str, native: str) -> str:
        from llm_collab.ledger import LedgerStore
        from llm_collab.session_lifecycle import (
            FakeLifecycleProvider,
            LifecycleSubject,
            SessionLifecycleCore,
            TrustedProjectRoot,
        )
        from llm_collab.worker import derive_worker_id

        core = SessionLifecycleCore(
            FakeLifecycleProvider(), token_factory=lambda: "token-alpha")
        subject = LifecycleSubject(
            workspace_id=self.WORKSPACE,
            scope_kind="project",
            scope_identity=project,
            conversation_id=chat,
            participant_id="participant_kimi",
            agent_id="agent_kimi",
            endpoint_id="endpoint_codex",
            native_session_id=native,
            runtime_instance_id="runtime_one",
        )
        with LedgerStore.open_writer(self.paths) as store:
            store._connection.execute(
                """
                INSERT INTO conversation_participants
                (workspace_id, scope_kind, scope_identity, conversation_id, participant_id, agent_id, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (subject.workspace_id, subject.scope_kind, subject.scope_identity,
                 subject.conversation_id, subject.participant_id, subject.agent_id,
                 self.NOW),
            )
            descriptor = core.provider.descriptor()
            store._connection.execute(
                """
                INSERT OR IGNORE INTO lifecycle_provider_registry
                (workspace_id, provider_id, provider_revision, trust_class,
                 supported_operations_json, challenge_algorithm, challenge_ttl_seconds, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (subject.workspace_id, descriptor["provider_id"],
                 descriptor["provider_revision"], descriptor["trust_class"],
                 descriptor["supported_operations_json"],
                 descriptor["challenge_algorithm"], descriptor["challenge_ttl_seconds"],
                 self.NOW),
            )
            trusted_root = TrustedProjectRoot(
                project, "repo_app", str(self.repo), str(self.cwd))
            challenge = core.reserve(
                store, subject, runtime_home=self.runtime_home,
                created_at_utc=self.NOW, expires_at_utc=self.EXPIRY,
                correlation_id="corr_reserve", trusted_project_root=trusted_root)
            resolved = core.consume(
                store, subject, challenge, runtime_home=self.runtime_home,
                consumed_at_utc=self.NOW, correlation_id="corr_consume",
                trusted_project_root=trusted_root)
            self.assertTrue(resolved["resolved"])
            self.binding_id = str(resolved["binding_id"])
        return derive_worker_id(
            workspace_id=self.WORKSPACE, scope_kind="project",
            scope_identity=project, conversation_id=chat,
            participant_id="participant_kimi")

    def packet(self, name: str, *, project: str, chat: str) -> str:
        relpath = f"Chats/{name}.md"
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\n"
            f"chat_id: {chat}\n"
            "from: codex\n"
            "sender_agent_id: codex\n"
            f"title: {name}\n"
            f"project_id: {project}\n"
            'repo_targets: ["llm-collab"]\n'
            "---\n\nbody\n",
            encoding="utf-8",
        )
        return relpath

    def render(self, project_id=None, chat_id=None) -> str:
        with mock.patch.object(
                operator_digest, "config_get", return_value=self.WORKSPACE), \
             mock.patch.object(
                operator_digest, "project_state_root",
                return_value=self.root / "state"), \
             mock.patch.object(operator_digest, "worker_sessions",
                               return_value=([], 0)), \
             mock.patch.object(operator_digest, "open_prs",
                               return_value=([], [])), \
             mock.patch.object(operator_digest, "project_repo_slugs",
                               return_value=set()), \
             mock.patch.object(operator_digest, "load_projects",
                               return_value=[{"id": "amiga"}, {"id": "nuvyr"}]):
            return operator_digest.render(project_id, chat_id)

    def test_join_counts_only_exact_session_unread(self) -> None:
        import _session_autobridge as autobridge

        worker_id = self.add_worker(project="amiga", chat="CHAT-WORKER1", native="native_session_one")
        binding_id = self.binding_id
        self.add_worker(project="amiga", chat="CHAT-OTHER9", native="native_session_two")

        bindings_dir = self.root / "State" / "session_autobridge" / "bindings"
        binding_path = bindings_dir / "amiga" / "CHAT-WORKER1" / "kimi.json"
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(json.dumps({
            "agent_id": "kimi",
            "project_id": "amiga",
            "chat_id": "CHAT-WORKER1",
            "session_id": "SESSION-PI-KIMI-WORKER1",
            "runtime_session_id": "native_session_one",
            "runtime_family": "pi",
            "runtime_home": str(self.root / "codex-home"),
            "binding_id": binding_id,
            "binding_generation": 1,
            "endpoint_id": "endpoint_codex",
            "repo_targets": ["app"],
            "status": "active",
        }), encoding="utf-8")
        session_record = {
            "agent_id": "kimi",
            "project_id": "amiga",
            "chat_id": "CHAT-WORKER1",
            "session_id": "SESSION-PI-KIMI-WORKER1",
            "status": "active",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "repo_targets": ["app"],
            "binding_id": binding_id,
            "binding_generation": 1,
            "runtime": {
                "family": "pi",
                "session_id": "native_session_one",
                "home": str(self.root / "codex-home"),
            },
        }

        def packet(name, **fm):
            relpath = f"Chats/{name}.md"
            path = self.root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = ["---", "chat_id: CHAT-WORKER1", "from: codex",
                     "sender_agent_id: codex", f"title: {name}",
                     "project_id: amiga", 'repo_targets: ["app"]']
            lines += [f"{key}: {value}" for key, value in fm.items()]
            path.write_text("\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
            return relpath

        exact = packet("exact", target_session_id="SESSION-PI-KIMI-WORKER1",
                       target_binding_id=binding_id, target_binding_generation=1)
        sibling = packet("sibling", target_session_id="SESSION-PI-KIMI-SIBLING9",
                         target_binding_id=binding_id, target_binding_generation=1)
        agent_scoped = packet("agent-scoped")
        (self.root / "agents" / "kimi").mkdir(parents=True)
        (self.root / "agents" / "kimi" / "inbox.json").write_text(
            json.dumps({"unread": [exact, sibling, agent_scoped], "read": []}),
            encoding="utf-8")

        with mock.patch.object(autobridge, "ROOT", self.root), \
             mock.patch.object(autobridge, "BINDINGS_DIR", bindings_dir), \
             mock.patch.object(autobridge, "iter_sessions", return_value=[session_record]), \
             mock.patch.object(autobridge, "agent_inbox_path",
                               lambda agent: self.root / "agents" / agent / "inbox.json"):
            text = self.render("amiga")

        # Only the packet targeted at this exact session/binding counts; the
        # sibling-targeted and agent-scoped packets do not.
        self.assertIn(f"| `{worker_id[:18]}` | kimi | amiga / CHAT-WORKER1 | active | 1 | 1 |", text)
        # No exact dispatch pair is provable for the second worker: unknown, not 0.
        self.assertIn("amiga / CHAT-OTHER9 | active | 1 | - |", text)

    def test_exact_join_failure_renders_note_not_partial_rows(self) -> None:
        import _session_autobridge as autobridge

        self.add_worker(project="amiga", chat="CHAT-WORKER1", native="native_session_one")
        with mock.patch.object(autobridge, "iter_sessions", return_value=[]), \
             mock.patch.object(autobridge, "resolve_exact_dispatch_pair",
                               side_effect=ValueError("binding unreadable")):
            text = self.render("amiga")
        self.assertIn("Canonical worker projection: canonical worker projection unavailable", text)
        self.assertNotIn("CHAT-WORKER1 |", text)

    def test_detached_checkout_renders_a_note_instead_of_crashing(self) -> None:
        with mock.patch.object(operator_digest, "config_get", return_value=None), \
             mock.patch.object(operator_digest, "worker_sessions",
                               return_value=([], 0)), \
             mock.patch.object(operator_digest, "open_prs",
                               return_value=([], [])), \
             mock.patch.object(operator_digest, "project_repo_slugs",
                               return_value=set()):
            text = operator_digest.render("amiga")
        self.assertIn("Canonical worker projection: no workspace_id", text)
