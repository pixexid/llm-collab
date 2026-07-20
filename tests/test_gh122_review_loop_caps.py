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


def contract_section(text, start, end):
    """Return one uniquely anchored contract section, including its start."""
    if text.count(start) != 1:
        raise AssertionError(f"expected exactly one section start: {start!r}")
    remainder = text.split(start, 1)[1]
    if remainder.count(end) != 1:
        raise AssertionError(f"expected exactly one section end: {end!r}")
    return normalized(start + remainder.split(end, 1)[0])


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DOC = REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
PLAN_DOC = REPO_ROOT / "docs" / "standalone-agent-session-bus-plan.md"
HANDOFF_DOC = REPO_ROOT / "docs" / "workflows" / "review-and-handoff.md"
REQUIRED_PROJECTS = ("amiga", "nuvyr")
PROJECT_CASES = (
    {
        "project_id": "amiga",
        "scenario": "review_loop_cap",
        "expected_outcome": "bounded_review_loop",
    },
    {
        "project_id": "nuvyr",
        "scenario": "review_loop_cap",
        "expected_outcome": "bounded_review_loop",
    },
    {
        "project_id": "amiga",
        "scenario": "canonical_wait_gate",
        "expected_outcome": "guarded_two_signal_wait",
    },
    {
        "project_id": "nuvyr",
        "scenario": "canonical_wait_gate",
        "expected_outcome": "guarded_two_signal_wait",
    },
    {
        "project_id": "amiga",
        "scenario": "standalone_publication",
        "expected_outcome": "wait_gated_publication",
    },
    {
        "project_id": "nuvyr",
        "scenario": "standalone_publication",
        "expected_outcome": "wait_gated_publication",
    },
    {
        "project_id": "amiga",
        "scenario": "compact_wait_gate",
        "expected_outcome": "synced_compact_wait",
    },
    {
        "project_id": "nuvyr",
        "scenario": "compact_wait_gate",
        "expected_outcome": "synced_compact_wait",
    },
)


class ReviewLoopCapContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow_text = WORKFLOW_DOC.read_text(encoding="utf-8")
        cls.workflow = normalized(workflow_text)
        cls.cap = contract_section(
            workflow_text,
            "Hard cycle cap, independent of family counting:",
            "When a project supports structured review notes",
        )
        cls.review_policy = contract_section(
            workflow_text,
            "### GitHub Codex review policy",
            "## Autonomous Queue Runner State",
        )
        cls.canonical_clean_verdict = contract_section(
            workflow_text,
            "- a head-named clean connector verdict is not merge-immediate",
            "- when a re-review was explicitly requested",
        )
        cls.canonical_rereview = contract_section(
            workflow_text,
            "- when a re-review was explicitly requested",
            "- if no head-named review and no eyes reaction appears",
        )
        plan_text = PLAN_DOC.read_text(encoding="utf-8")
        cls.plan = contract_section(
            plan_text,
            "## Worker, review, and publication discipline",
            "## Definition of phase and program completion",
        )
        phase_completion_start = "## Definition of phase and program completion"
        if plan_text.count(phase_completion_start) != 1:
            raise AssertionError(
                f"expected exactly one section start: {phase_completion_start!r}"
            )
        cls.phase_completion = normalized(
            phase_completion_start
            + plan_text.split(phase_completion_start, 1)[1]
        )
        handoff_text = HANDOFF_DOC.read_text(encoding="utf-8")
        cls.handoff_wait = contract_section(
            handoff_text,
            "For PR-review wait heartbeats",
            "If GitHub Codex comments on the PR",
        )
        cls.sources_by_outcome = {
            "bounded_review_loop": {
                "workflow": cls.workflow,
                "cap": cls.cap,
                "universal_contract": cls.cap,
            },
            "guarded_two_signal_wait": {
                "review_policy": cls.review_policy,
                "canonical_clean_verdict": cls.canonical_clean_verdict,
                "canonical_rereview": cls.canonical_rereview,
                "universal_contract": cls.review_policy,
            },
            "wait_gated_publication": {
                "plan": cls.plan,
                "phase_completion": cls.phase_completion,
                "universal_contract": cls.plan,
            },
            "synced_compact_wait": {
                "handoff_wait": cls.handoff_wait,
                "universal_contract": cls.handoff_wait,
            },
        }

    def assert_scenario_cases(self, scenario, check):
        """Run a scenario's case-selected assertions for both projects."""
        cases = [case for case in PROJECT_CASES if case["scenario"] == scenario]
        self.assertTrue(cases, f"missing concrete cases for scenario {scenario!r}")
        for case in cases:
            with self.subTest(
                project_id=case["project_id"],
                scenario=case["scenario"],
            ):
                outcome = case["expected_outcome"]
                self.assertIn(
                    outcome,
                    self.sources_by_outcome,
                    f"unknown expected_outcome for concrete case: {case}",
                )
                check(case, self.sources_by_outcome[outcome])

    def test_project_cases_are_concrete(self):
        required_keys = {"project_id", "scenario", "expected_outcome"}
        for case in PROJECT_CASES:
            with self.subTest(
                project_id=case.get("project_id"),
                scenario=case.get("scenario"),
            ):
                self.assertEqual(set(case), required_keys)
                for key in required_keys:
                    self.assertIsInstance(case[key], str)
                    self.assertTrue(case[key].strip())

    def test_each_scenario_has_paired_amiga_and_nuvyr_cases(self):
        self.assertEqual(REQUIRED_PROJECTS, ("amiga", "nuvyr"))
        self.assertEqual(len(REQUIRED_PROJECTS), len(set(REQUIRED_PROJECTS)))
        scenarios = {case["scenario"] for case in PROJECT_CASES}
        for scenario in scenarios:
            declared = {
                case["project_id"]
                for case in PROJECT_CASES
                if case["scenario"] == scenario
            }
            self.assertEqual(
                declared,
                set(REQUIRED_PROJECTS),
                f"{scenario} must have concrete amiga and nuvyr cases",
            )

    def test_each_scenario_has_universal_expected_outcome(self):
        scenarios = {case["scenario"] for case in PROJECT_CASES}
        for scenario in scenarios:
            outcomes = {
                case.get("expected_outcome")
                for case in PROJECT_CASES
                if case["scenario"] == scenario
            }
            self.assertEqual(
                len(outcomes),
                1,
                f"{scenario} expected_outcome diverges by project: {outcomes}",
            )

    def test_universal_contract_sections_name_no_representative_project(self):
        def check(case, sources):
            contract = sources["universal_contract"].casefold()
            self.assertNotIn(case["project_id"].casefold(), contract)
            for project_id in REQUIRED_PROJECTS:
                self.assertNotRegex(
                    contract,
                    rf"\b{re.escape(project_id.casefold())}\b",
                    f"{case['expected_outcome']} contract is not universal",
                )

        for scenario in {case["scenario"] for case in PROJECT_CASES}:
            self.assert_scenario_cases(scenario, check)

    def test_same_file_family_counting_is_mechanical(self):
        def check(case, sources):
            self.assertEqual(case["expected_outcome"], "bounded_review_loop")
            self.assertIn(
                "Same-file anchoring counts mechanically", sources["workflow"]
            )
            self.assertNotIn(
                "Do not mechanically auto-count finding families",
                sources["workflow"],
            )

        self.assert_scenario_cases("review_loop_cap", check)

    def test_contract_clarified_limited_per_family(self):
        self.assert_scenario_cases(
            "review_loop_cap",
            lambda case, sources: self.assertIn(
                "at most once per family per PR", sources["workflow"]
            ),
        )

    def test_hard_cycle_cap_present_and_bounded(self):
        def check(case, sources):
            self.assertIn(
                "Hard cycle cap, independent of family counting",
                sources["workflow"],
            )
            self.assertIn(
                "at most 2 review-fix cycles are permitted per lane",
                sources["workflow"],
            )
            self.assertIn(
                "Starting another review cycle past the cap is a process violation",
                sources["workflow"],
            )

        self.assert_scenario_cases("review_loop_cap", check)

    def test_cycle_definition_ignores_reviewer_freshness(self):
        self.assert_scenario_cases(
            "review_loop_cap",
            lambda case, sources: self.assertIn(
                "regardless of reviewer freshness", sources["workflow"]
            ),
        )

    def test_counter_is_per_lane_and_covers_pre_pr_loop(self):
        def check(case, sources):
            self.assertIn("per task/lane, not per PR", sources["workflow"])
            self.assertIn(
                "pre-PR collab/doorbell review loop", sources["workflow"]
            )
            self.assertIn(
                "Opening the PR never resets the count", sources["workflow"]
            )

        self.assert_scenario_cases("review_loop_cap", check)

    def test_cap_requires_terminal_disposition_only_with_open_findings(self):
        def check(case, sources):
            self.assertIn(
                "Only when actionable findings remain open at the capped head is "
                "exactly one terminal action required",
                sources["cap"],
            )
            self.assertIn(
                "A capped head with zero open actionable findings and a clean "
                "exact-head re-review follows the normal merge gate with no "
                "convergence-disposition label",
                sources["cap"],
            )

        self.assert_scenario_cases("review_loop_cap", check)

    def test_cap_terminal_actions_include_backend_first(self):
        def check(case, sources):
            for value in (
                "risk-accepted-followup",
                "descope",
                "split",
                "backend-first",
            ):
                self.assertIn(value, sources["cap"])

        self.assert_scenario_cases("review_loop_cap", check)

    def test_cap_never_waives_pr_review_wait_gate(self):
        def check(case, sources):
            self.assertIn(
                "A cap disposition never waives the PR Review Wait Gate",
                sources["cap"],
            )
            self.assertIn(
                "The cap bars another fix cycle, not waiting", sources["cap"]
            )

        self.assert_scenario_cases("review_loop_cap", check)

    def test_cap_escalation_is_independent_of_terminal_disposition(self):
        def check(case, sources):
            self.assertIn(
                "Reaching the applicable cap requires an operator-visible "
                "escalation message recorded independently, whether or not open "
                "findings require a terminal disposition",
                sources["cap"],
            )
            self.assertIn(
                "When open findings do require a terminal disposition, record "
                "the escalation alongside it",
                sources["cap"],
            )
            self.assertIn("2 hours of wall-clock", sources["cap"])

        self.assert_scenario_cases("review_loop_cap", check)

    def test_exact_head_signal_models_remain_exclusive(self):
        def check(case, sources):
            self.assertIn(
                "a clean `chatgpt-codex-connector` review/comment that explicitly "
                "covers the exact current OID is terminal for that head",
                sources["review_policy"],
            )
            self.assertIn(
                "the watcher observed the connector's eyes-to-`+1` lifecycle on "
                "that head",
                sources["review_policy"],
            )
            self.assertIn(
                "these are the only two exact-head terminal signal models",
                sources["review_policy"],
            )

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_clean_verdict_gets_post_clean_settle_and_reread(self):
        def check(case, sources):
            self.assertIn(
                "approximately five-minute post-clean settle",
                sources["canonical_clean_verdict"],
            )
            self.assertIn(
                "full re-read of reviews, review threads, and reactions",
                sources["canonical_clean_verdict"],
            )
            self.assertIn(
                "that re-review supersedes older same-head clean artifacts for "
                "the clean-verdict path",
                sources["canonical_rereview"],
            )
            self.assertIn(
                "Only the explicit re-review verdict can satisfy that path",
                sources["canonical_rereview"],
            )

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_silently_dropped_review_is_retriggered(self):
        def check(case, sources):
            self.assertIn(
                "no head-named review and no eyes reaction appears within roughly "
                "30–35 minutes after the latest push",
                sources["review_policy"],
            )
            self.assertIn(
                "treat the request as silently dropped", sources["review_policy"]
            )
            self.assertIn(
                "re-trigger it with an `@codex review` comment",
                sources["review_policy"],
            )

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_plan_doc_reuses_reviewer_for_in_contract_amendments(self):
        def check(case, sources):
            self.assertIn(
                "reusing the same reviewer for in-contract amendments",
                sources["plan"],
            )
            self.assertNotIn(
                "Every implementation worker and every exact-head reviewer receives "
                "a separate fresh task/thread",
                sources["plan"],
            )

        self.assert_scenario_cases("standalone_publication", check)

    def test_capped_pre_pr_lane_can_still_publish(self):
        def check(case, sources):
            self.assertIn("before any further amendment", sources["workflow"])
            self.assertIn(
                "caps during the pre-PR loop can still land", sources["workflow"]
            )

        self.assert_scenario_cases("review_loop_cap", check)

    def test_phase_completion_gate_permits_reviewer_reuse(self):
        def check(case, sources):
            self.assertIn(
                "reused per the bounded amendment rules in "
                "`docs/workflows/commit-push-prs.md` for in-contract amended heads",
                sources["phase_completion"],
            )
            self.assertNotIn(
                "a fresh independent reviewer accepts the exact head",
                sources["phase_completion"],
            )

        self.assert_scenario_cases("standalone_publication", check)

    def test_plan_doc_caps_review_fix_cycles(self):
        def check(case, sources):
            self.assertIn(
                "most 2 review-fix cycles follow the initial review",
                sources["plan"],
            )
            self.assertIn(
                "a terminal disposition is mandatory only when actionable findings "
                "remain open at the capped head",
                sources["plan"],
            )
            self.assertIn(
                "A clean capped head follows the normal merge gate with no "
                "disposition label",
                sources["plan"],
            )

        self.assert_scenario_cases("standalone_publication", check)

    def test_plan_cap_terminal_actions_include_backend_first(self):
        def check(case, sources):
            self.assertIn(
                "`descope`, `split`, `backend-first`, or a durable operator escalation",
                sources["plan"],
            )

        self.assert_scenario_cases("standalone_publication", check)

    def test_plan_step_11_requires_full_pr_review_wait_gate(self):
        def check(case, sources):
            self.assertIn(
                "merge only the reviewed exact head after the full PR Review Wait Gate",
                sources["plan"],
            )
            self.assertIn(
                "two exact-head terminal-signal models", sources["plan"]
            )
            self.assertIn(
                "post-clean settle and full review/thread/reaction re-read",
                sources["plan"],
            )
            self.assertIn(
                "resettable 15-minute fallback", sources["plan"]
            )

        self.assert_scenario_cases("standalone_publication", check)

    def test_compact_wait_gate_preserves_the_two_signal_sources(self):
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "`chatgpt-codex-connector` review/comment explicitly covers that "
                "exact OID with no actionable issues",
                handoff,
            )
            self.assertIn(
                "watcher observed the connector's eyes-to-`+1` (`thumbs-up`) "
                "transition on the latest head",
                handoff,
            )
            self.assertIn(
                "these remain the only two exact-head terminal signal sources",
                handoff,
            )

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_compact_clean_verdict_gets_settle_and_reread(self):
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "approximately five-minute post-clean settle", handoff
            )
            self.assertIn(
                "full re-read of reviews, review threads, and reactions", handoff
            )
            self.assertIn(
                "that re-review supersedes older same-head clean artifacts for "
                "the clean-verdict path",
                handoff,
            )
            self.assertIn("it receives the same settle and full re-read", handoff)
            self.assertNotIn(
                "timestamps immediately and do not wait out the remainder", handoff
            )

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_compact_eyes_signal_avoids_only_the_fallback_remainder(self):
        def check(case, sources):
            self.assertIn(
                "report it immediately and do not wait out the remainder of the "
                "15-minute fallback itself",
                sources["handoff_wait"],
            )

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_compact_silently_dropped_review_is_retriggered(self):
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "no head-named review and no eyes reaction appears within roughly "
                "30–35 minutes after the latest push",
                handoff,
            )
            self.assertIn("treat the request as silently dropped", handoff)
            self.assertIn(
                "re-trigger it with an `@codex review` comment", handoff
            )

        self.assert_scenario_cases("compact_wait_gate", check)


if __name__ == "__main__":
    unittest.main()
