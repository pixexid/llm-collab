"""GH-507: the PR issue-link classifier (orphan-risk detection)."""
from __future__ import annotations

import sys
import unittest
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


class RefExtractionTest(unittest.TestCase):
    def test_hash_and_url_refs(self):
        self.assertEqual({1, 2}, ilc.any_refs("touches #1 and github.com/o/r/issues/2"))

    def test_branch_issue(self):
        self.assertEqual(505, ilc.branch_issue("claude/gh505-freshness-gate"))
        self.assertEqual(505, ilc.branch_issue("codex/gh-505-x"))
        self.assertIsNone(ilc.branch_issue("claude/no-issue-here"))


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


if __name__ == "__main__":
    unittest.main()
