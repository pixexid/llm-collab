"""GH-507: the PR issue-link classifier (orphan-risk detection)."""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import issue_link_check as ilc


class ClosingKeywordTest(unittest.TestCase):
    def test_all_closing_keyword_variants(self):
        for kw in ("Close", "Closes", "closed", "Fix", "fixes", "fixed",
                   "Resolve", "Resolves", "resolved"):
            with self.subTest(kw=kw):
                self.assertEqual({42}, ilc.closing_refs(f"{kw} #42"))

    def test_colon_and_spacing_forms(self):
        self.assertEqual({7}, ilc.closing_refs("Fixes: #7"))
        self.assertEqual({7}, ilc.closing_refs("closes    #7"))

    def test_bare_reference_is_not_closing(self):
        self.assertEqual(set(), ilc.closing_refs("Related #9"))
        self.assertEqual(set(), ilc.closing_refs("see #9 for context"))

    def test_negated_prose_still_counts_as_closing(self):
        # GitHub closes it regardless of negation; the classifier must agree so the
        # author is warned rather than surprised (matches commit-push-prs.md).
        self.assertEqual({9}, ilc.closing_refs("This does not resolve #9"))

    def test_hyphen_prefixed_keyword_is_not_closing(self):
        # "auto-close #503" is prose describing behavior, not a closing directive —
        # GitHub won't auto-close on it, so neither should we (real #508 smoke bug).
        self.assertEqual(set(), ilc.closing_refs('output: "will auto-close #503"'))
        self.assertEqual(set(), ilc.closing_refs("preclose #7"))
        # a genuine standalone keyword still counts
        self.assertEqual({503}, ilc.closing_refs("Closes #503"))

    def test_closing_in_title_is_ignored_body_only(self):
        # GitHub's durable auto-close is from the body, not the title.
        c = ilc.classify_pr("Closes #5 in the title", "no closing keyword in body", "x")
        self.assertEqual([], c["closing"])
        self.assertEqual([5], c["referenced"])


class RefExtractionTest(unittest.TestCase):
    def test_hash_and_url_refs(self):
        self.assertEqual({1, 2}, ilc.any_refs("touches #1 and https://github.com/o/r/issues/2"))

    def test_cross_repo_refs_excluded_when_repo_given(self):
        REPO = "pixexid/llm-collab"
        # bare #N is always same-repo; a cross-repo qualified/URL ref must NOT match a
        # local issue N (else --sweep would suggest closing the wrong local issue).
        text = "#42 pixexid/llm-collab#43 other/repo#44 https://github.com/other/repo/issues/45"
        self.assertEqual({42, 43}, ilc.any_refs(text, REPO))
        # same text without a repo (pure classifier) includes all — repo filters
        self.assertEqual({42, 43, 44, 45}, ilc.any_refs(text, None))

    def test_qualified_and_url_closing_forms(self):
        REPO = "pixexid/llm-collab"
        self.assertEqual({42}, ilc.closing_refs("Closes pixexid/llm-collab#42", REPO))
        self.assertEqual({42}, ilc.closing_refs(
            "Fixes https://github.com/pixexid/llm-collab/issues/42", REPO))
        self.assertEqual(set(), ilc.closing_refs("Closes other/repo#42", REPO))

    def test_branch_issue(self):
        self.assertEqual(505, ilc.branch_issue("claude/gh505-freshness-gate"))
        self.assertEqual(505, ilc.branch_issue("codex/gh-505-x"))
        self.assertEqual(507, ilc.branch_issue("claude/gh507"))
        self.assertIsNone(ilc.branch_issue("claude/no-issue-here"))

    def test_branch_issue_requires_boundaries(self):
        # "gh" inside an ordinary word must NOT resolve to an issue.
        self.assertIsNone(ilc.branch_issue("feature/high500-throughput"))
        self.assertIsNone(ilc.branch_issue("feature/rough2-edge"))


class ClassifyTest(unittest.TestCase):
    def test_closing_keyword_on_branch_issue_no_orphan(self):
        c = ilc.classify_pr("Fix the gate", "Closes #503", "claude/gh503-x")
        self.assertEqual([503], c["closing"])
        self.assertEqual([], c["orphan_risk"])

    def test_prose_reference_without_branch_is_informational_not_risk(self):
        # A bare prose #99 with no gh<N> branch is too weak to call the PR's issue —
        # informational only, never an orphan-risk (auto-closing it would be wrong).
        c = ilc.classify_pr("Some work", "relates to #99", "feature/x")
        self.assertEqual([], c["closing"])
        self.assertEqual([], c["orphan_risk"])
        self.assertEqual([99], c["referenced"])

    def test_branch_issue_without_closing_is_orphan_risk(self):
        c = ilc.classify_pr("Doc tweak", "no issue keyword here", "claude/gh498-port")
        self.assertEqual([498], c["orphan_risk"])

    def test_no_reference_at_all_is_clean(self):
        c = ilc.classify_pr("Chore", "no refs", "chore/cleanup")
        self.assertEqual([], c["closing"])
        self.assertEqual([], c["orphan_risk"])
        self.assertEqual([], c["referenced"])

    def test_branch_issue_closed_with_extra_prose_ref(self):
        c = ilc.classify_pr("Big change", "Closes #10\nRelated #11", "claude/gh10-x")
        self.assertEqual([10], c["closing"])
        self.assertEqual([], c["orphan_risk"])       # branch issue 10 is closed
        self.assertEqual([11], c["referenced"])      # #11 surfaced as informational


class RepoLocalAutolinkTest(unittest.TestCase):
    # GH-510 / GH-522: this repo's `GH-N` autolink (AGENTS.md "GitHub Autolink
    # Safety") is a same-repo issue reference and is recognized like a bare `#N` —
    # but ONLY when the repo opts in (`gh_autolink=True`). The fail-closed cases
    # (no opt-in) live in GhAutolinkScopeTest below.
    REPO = "pixexid/llm-collab"

    def test_gh_autolink_is_a_same_repo_reference(self):
        self.assertEqual({123}, ilc.any_refs("see GH-123", self.REPO, gh_autolink=True))
        self.assertEqual({123}, ilc.any_refs("see gh-123", self.REPO, gh_autolink=True))  # case-insensitive
        self.assertEqual({123}, ilc.any_refs("see GH-123", None, gh_autolink=True))       # same-repo, no qualifier

    def test_gh_autolink_closing_and_bare_forms(self):
        self.assertEqual({42}, ilc.closing_refs("Closes GH-42", self.REPO, gh_autolink=True))
        self.assertEqual(set(), ilc.closing_refs("Related GH-42", self.REPO, gh_autolink=True))  # no keyword
        self.assertEqual({42}, ilc.any_refs("Related GH-42", self.REPO, gh_autolink=True))       # still a reference

    def test_gh_autolink_requires_a_boundary_and_hyphen(self):
        # Must not match inside a word (LIGH-5) or without the hyphen (GH5 / GHz).
        # gh_autolink=True so the boundary test is meaningful, not trivially empty.
        self.assertEqual(set(), ilc.any_refs("LIGH-5 and xGH-9", self.REPO, gh_autolink=True))
        self.assertEqual(set(), ilc.any_refs("GH5 GHz-3", self.REPO, gh_autolink=True))

    def test_closing_gh_autolink_on_matching_branch_is_not_orphan(self):
        # `Closes GH-3` on a gh3 branch must clear the orphan-risk (the bug: GH-N
        # was not recognized as closing, so the branch issue looked unclosed).
        c = ilc.classify_pr("Fix", "Closes GH-3", "claude/gh3-x", self.REPO, gh_autolink=True)
        self.assertEqual([3], c["closing"])
        self.assertEqual([], c["orphan_risk"])

    def test_bare_and_qualified_forms_need_no_opt_in(self):
        # Bare #N and owner/name#N forms are never gated by gh_autolink.
        self.assertEqual({7}, ilc.closing_refs("fixes #7", self.REPO))
        self.assertEqual({8}, ilc.closing_refs("Closes pixexid/llm-collab#8", self.REPO))
        self.assertEqual(set(), ilc.any_refs("other/repo#9", self.REPO))


class GhAutolinkScopeTest(unittest.TestCase):
    # GH-522: a bare `GH-N` is this workspace's autolink shorthand, NOT a property of
    # whatever repo --repo names, so it must NOT count unless the repo opts in. The
    # default is fail-closed. BRANCH_REF (branch_issue) is a separate branch
    # convention and stays ungated — its own regression is below.
    REPO = "pixexid/llm-collab"
    OTHER = "someone/another-repo"

    def test_gh_autolink_inert_without_opt_in_for_any_repo(self):
        # THE DEFECT (GH-522): with no opt-in, `GH-N` is inert text for the home repo
        # AND for a repo that never defined the autolink.
        # MUTATION PROOF for the `_refs` gh_autolink gate: delete the gate and the
        # first assertion fails, because `GH-N` would count for self.REPO again.
        self.assertEqual(set(), ilc.any_refs("see GH-123", self.REPO))
        self.assertEqual(set(), ilc.any_refs("see GH-123", self.OTHER))
        self.assertEqual(set(), ilc.any_refs("see GH-123", None))

    def test_gh_autolink_not_closing_without_opt_in(self):
        self.assertEqual(set(), ilc.closing_refs("Closes GH-42", self.REPO))

    def test_gh_autolink_opt_in_counts_for_resolved_repo(self):
        # Opting in restores recognition (the configured case the issue preserves).
        self.assertEqual({123}, ilc.any_refs("see GH-123", self.REPO, gh_autolink=True))
        self.assertEqual({42}, ilc.closing_refs("Closes GH-42", self.REPO, gh_autolink=True))

    def test_other_ref_forms_unaffected_by_opt_in_or_out(self):
        # #N and full URLs are never gated by gh_autolink.
        self.assertEqual({7}, ilc.any_refs("fixes #7", None))
        self.assertEqual({7}, ilc.any_refs("fixes #7", None, gh_autolink=True))
        self.assertEqual(
            {9},
            ilc.any_refs(
                "https://github.com/someone/another-repo/issues/9", self.OTHER
            ),
        )
        self.assertEqual(set(), ilc.any_refs("someone/another-repo#9", self.REPO))

    def test_classify_pr_gh_autolink_orphan_only_when_opted_in(self):
        # `Closes GH-3` on a gh3 branch: without opt-in the GH-3 close is invisible,
        # so branch issue 3 looks orphaned; with opt-in it is closed.
        no_opt = ilc.classify_pr("Fix", "Closes GH-3", "claude/gh3-x", self.REPO)
        self.assertEqual([], no_opt["closing"])
        self.assertEqual([3], no_opt["orphan_risk"])
        opt_in = ilc.classify_pr("Fix", "Closes GH-3", "claude/gh3-x", self.REPO, gh_autolink=True)
        self.assertEqual([3], opt_in["closing"])
        self.assertEqual([], opt_in["orphan_risk"])

    def test_branch_issue_regression_ungated(self):
        # SCOPE TRAP (GH-522): BRANCH_REF also matches `gh-` but encodes our branch
        # convention, not the autolink, so it must resolve with NO opt-in on any repo.
        self.assertEqual(524, ilc.branch_issue("codex/gh524-start-livecraft"))
        self.assertEqual(505, ilc.branch_issue("codex/gh-505-x"))
        self.assertEqual(507, ilc.branch_issue("claude/gh507"))
        self.assertIsNone(ilc.branch_issue("feature/no-gh-issue"))


class SweepBranchIssueTest(unittest.TestCase):
    # GH-510: the --sweep backstop must also apply the branch convention, so a merged
    # gh<N> branch that omitted #N from its body is still caught as a possible orphan.
    REPO = "pixexid/llm-collab"

    def _run_sweep(self, merged, open_numbers, *, gh_autolink=False):
        def fake_gh_json(args):
            if "pr" in args:
                return merged
            return [{"number": n} for n in open_numbers]
        with unittest.mock.patch.object(ilc, "_gh_json", side_effect=fake_gh_json):
            return ilc.sweep(self.REPO, gh_autolink=gh_autolink)

    def test_sweep_flags_branch_only_orphan(self):
        # Body has no #N; only the gh505 branch declares issue 505, which is open.
        merged = [{"number": 12, "title": "t", "body": "no issue ref", "headRefName": "claude/gh505-x"}]
        rc = self._run_sweep(merged, open_numbers=[505])
        self.assertEqual(1, rc)  # orphan found

    def test_sweep_ignores_closed_branch_issue(self):
        merged = [{"number": 12, "title": "t", "body": "no ref", "headRefName": "claude/gh505-x"}]
        rc = self._run_sweep(merged, open_numbers=[999])  # 505 not open
        self.assertEqual(0, rc)

    def test_sweep_gh_autolink_body_ref_only_counts_when_opted_in(self):
        # GH-522: a `GH-777` body ref is a possible orphan only when --gh-autolink
        # opts the repo in; without it the ref is inert (no spurious orphan for a
        # repo that never defined the autolink). Branch name carries no gh<N> issue.
        merged = [{"number": 30, "title": "t", "body": "Closes GH-777", "headRefName": "main"}]
        self.assertEqual(1, self._run_sweep(merged, open_numbers=[777], gh_autolink=True))
        self.assertEqual(0, self._run_sweep(merged, open_numbers=[777]))  # fail-closed


if __name__ == "__main__":
    unittest.main()
