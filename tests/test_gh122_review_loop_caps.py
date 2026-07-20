"""Regression guard for GH-122 / PR #123 review-loop convergence contract.

The PR #120 postmortem showed an unbounded review-fix loop: a fresh
zero-context reviewer per amendment always finds new findings, the finding
family circuit breaker was skippable via the judgment clause, and no cycle or
time cap existed. These assertions pin the shared contract wording in
docs/workflows/commit-push-prs.md and docs/standalone-agent-session-bus-plan.md
so a later edit cannot silently weaken the mechanical safeguards.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


def normalized(text):
    """Collapse whitespace so assertions survive prose re-wrapping."""
    return re.sub(r"\s+", " ", text)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DOC = REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
PLAN_DOC = REPO_ROOT / "docs" / "standalone-agent-session-bus-plan.md"


class ReviewLoopCapContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        cls.plan = normalized(PLAN_DOC.read_text(encoding="utf-8"))

    def test_same_file_family_counting_is_mechanical(self):
        self.assertIn("Same-file anchoring counts mechanically", self.workflow)
        self.assertNotIn(
            "Do not mechanically auto-count finding families", self.workflow
        )

    def test_contract_clarified_limited_per_family(self):
        self.assertIn("at most once per family per PR", self.workflow)

    def test_hard_cycle_cap_present_and_bounded(self):
        self.assertIn("Hard cycle cap, independent of family counting", self.workflow)
        self.assertIn("at most 2 review-fix cycles are permitted per lane", self.workflow)
        self.assertIn("Starting another review cycle past the cap is a process violation", self.workflow)

    def test_cycle_definition_ignores_reviewer_freshness(self):
        self.assertIn("regardless of reviewer freshness", self.workflow)

    def test_counter_is_per_lane_and_covers_pre_pr_loop(self):
        self.assertIn("per task/lane, not per PR", self.workflow)
        self.assertIn("pre-PR collab/doorbell review loop", self.workflow)
        self.assertIn("Opening the PR never resets the count", self.workflow)

    def test_cap_forces_exactly_one_terminal_disposition(self):
        for value in ("risk-accepted-followup", "descope", "split"):
            self.assertIn(value, self.workflow)

    def test_escalation_fires_at_cap_and_wall_clock(self):
        self.assertIn("Reaching the applicable cap", self.workflow)
        self.assertIn("2 hours of wall-clock", self.workflow)
        self.assertIn("operator-visible escalation", self.workflow)

    def test_plan_doc_reuses_reviewer_for_in_contract_amendments(self):
        self.assertIn("reusing the same reviewer for in-contract amendments", self.plan)
        self.assertNotIn(
            "Every implementation worker and every exact-head reviewer receives "
            "a separate fresh task/thread",
            self.plan,
        )

    def test_capped_pre_pr_lane_can_still_publish(self):
        self.assertIn("before any further amendment", self.workflow)
        self.assertIn("caps during the pre-PR loop can still land", self.workflow)

    def test_phase_completion_gate_permits_reviewer_reuse(self):
        self.assertIn(
            "reused per the bounded amendment rules in "
            "`docs/workflows/commit-push-prs.md` for in-contract amended heads",
            self.plan,
        )
        self.assertNotIn("a fresh independent reviewer accepts the exact head", self.plan)

    def test_plan_doc_caps_review_fix_cycles(self):
        self.assertIn("most 2 review-fix cycles follow the initial review", self.plan)
        self.assertIn("exactly one terminal disposition is mandatory", self.plan)


if __name__ == "__main__":
    unittest.main()
