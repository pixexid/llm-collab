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
    {
        "project_id": "amiga",
        "scenario": "wait_gate_precedence",
        "expected_outcome": "adjudicated_wait_precedence",
    },
    {
        "project_id": "nuvyr",
        "scenario": "wait_gate_precedence",
        "expected_outcome": "adjudicated_wait_precedence",
    },
    {
        "project_id": "amiga",
        "scenario": "operator_head_authorization",
        "expected_outcome": "adjudicated_wait_precedence",
    },
    {
        "project_id": "nuvyr",
        "scenario": "operator_head_authorization",
        "expected_outcome": "adjudicated_wait_precedence",
    },
)


class ReviewLoopCapContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow_text = WORKFLOW_DOC.read_text(encoding="utf-8")
        cls.workflow_text = workflow_text
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
            "- report the exact verdict",
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
        cls.handoff_text = handoff_text
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
            "adjudicated_wait_precedence": {
                "workflow_text": cls.workflow_text,
                "handoff_text": cls.handoff_text,
                "handoff_wait": cls.handoff_wait,
                "universal_contract": " ".join(
                    (cls.review_policy, cls.handoff_wait)
                ),
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

    def assert_wait_gate_residual_contract(self, workflow_text, handoff_text):
        """Assert the GH-133/GH-140 wait residuals against supplied doc text."""
        review_policy = contract_section(
            workflow_text,
            "### GitHub Codex review policy",
            "## Autonomous Queue Runner State",
        )
        fallback = contract_section(
            workflow_text,
            "The resettable fallback above handles three named "
            "no-terminal-artifact variants",
            "#### Explicit requested-review precedence",
        )
        precedence = contract_section(
            workflow_text,
            "#### Explicit requested-review precedence",
            "If the PR is waiting only for remote checks or remote review state",
        )
        handoff_wait = contract_section(
            handoff_text,
            "For PR-review wait heartbeats",
            "If GitHub Codex comments on the PR",
        )
        compact_precedence = contract_section(
            handoff_text,
            "For requested-review silence versus the fallback",
            "If GitHub Codex comments on the PR",
        ).strip()

        required_phrases = (
            (
                precedence,
                (
                    "remains pending until its roughly 30–35-minute clock expires",
                    "never ages into the 15-minute fallback",
                    "Anchor each clock to the corresponding explicit request "
                    "artifact's GitHub `created_at`, never to the latest push or "
                    "the time the head became reviewable",
                    "A current-head `eyes` reaction alone is non-terminal: it "
                    "does not exit requested-review precedence",
                    "issue exactly one `@codex review` re-trigger",
                    "The re-trigger is the sole automatic retry",
                    "do not re-trigger again",
                    "explicit disposition bound to the exact current head",
                    "The disposition must state exactly one of these outcomes",
                    "merge of that exact head is authorized despite the absent "
                    "connector terminal signal",
                    "that exact head must not merge and remains blocked or is "
                    "closed",
                    "An ambiguous note, a disposition not bound to the current "
                    "head, or an older-head disposition does not lift the merge "
                    "block",
                    "Any later push invalidates the disposition and restarts "
                    "exact-head evaluation",
                    "lifts only the missing connector-signal subgate",
                    "is not a third automated terminal-signal model",
                    "It does not waive independent exact-head review, green "
                    "required checks, mergeability, the full comment/review/"
                    "thread/reaction reread, unresolved-feedback handling, or "
                    "project/operator auto-merge authority",
                    "the operator authorization does not masquerade as that "
                    "signal or inherit its handling",
                    "a dropped request is indistinguishable from a review that "
                    "is still processing",
                    "unlike the absent-request variant, where there is nothing "
                    "to drop",
                ),
            ),
            (
                fallback,
                (
                    "Eyes-only current-head artifact",
                    "This fallback variant applies only when no explicit review "
                    "request is outstanding",
                ),
            ),
            (
                review_policy,
                (
                    "An explicitly requested review does not enter this ageing "
                    "rule",
                    "it does not waive post-signal handling",
                    "the approximately five-minute post-clean settle and full "
                    "review/thread/reaction re-read remain mandatory before merge",
                    "these are the only two exact-head terminal signal models",
                ),
            ),
            (
                handoff_wait,
                (
                    "[Explicit requested-review precedence]"
                    "(commit-push-prs.md#explicit-requested-review-precedence)",
                    "Do not apply the 15-minute fallback to an explicitly "
                    "requested review",
                    "Automation may issue exactly one re-trigger, and no further "
                    "automatic retry is allowed",
                    "The canonical section is the sole authority for both "
                    "request-anchored clocks, current-head invalidation, the "
                    "post-timeout disposition choices, and every effect of an "
                    "exact-head operator authorization; this compact guidance "
                    "defines no separate disposition effect",
                    "eyes-only current-head artifact (which applies only when no "
                    "explicit review request is outstanding",
                    "it does not reset an explicit request's request-anchored "
                    "clock",
                    "A terminal signal stops waiting for further artifacts or "
                    "the fallback timeout only; it does not waive the handling "
                    "below",
                    "approximately five-minute mandatory post-clean settle",
                    "these remain the only two exact-head terminal signal sources",
                ),
            ),
        )
        for source, phrases in required_phrases:
            for phrase in phrases:
                self.assertIn(phrase, source)
        self.assertNotIn(
            "remains unmergeable until a terminal human/operator disposition "
            "is recorded",
            handoff_wait,
        )
        self.assertEqual(
            compact_precedence,
            normalized(
                "For requested-review silence versus the fallback, follow the "
                "canonical [Explicit requested-review precedence]"
                "(commit-push-prs.md#explicit-requested-review-precedence). "
                "Do not apply the 15-minute fallback to an explicitly requested "
                "review. Automation may issue exactly one re-trigger, and no "
                "further automatic retry is allowed. The canonical section is "
                "the sole authority for both request-anchored clocks, "
                "current-head invalidation, the post-timeout disposition "
                "choices, and every effect of an exact-head operator "
                "authorization; this compact guidance defines no separate "
                "disposition effect. The fallback is limited to exactly three "
                "named no-terminal-artifact variants: no explicit review "
                "request (the reviewability clock starts at the later of the "
                "final push and the head becoming reviewable), eyes-only "
                "current-head artifact (which applies only when no explicit "
                "review request is outstanding and is non-blocking once no "
                "review is pending), and prior-head artifacts only (a stale-head "
                "`Codex Review:` body or reaction is not head-attributable and "
                "is ignored for terminal-signal purposes). For those fallback "
                "variants, any push invalidates the prior signal and restarts "
                "the fallback clock for the new head; it does not reset an "
                "explicit request's request-anchored clock. This compact handoff "
                "rule must not define a competing timer or disposition rule."
            ),
        )

        fallback_variants = re.findall(r"- \*\*([^*]+)\.\*\*", fallback)
        self.assertEqual(
            fallback_variants,
            [
                "No explicit review request",
                "Eyes-only current-head artifact",
                "Prior-head artifacts only",
            ],
        )

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

    def test_silently_dropped_review_gets_one_request_anchored_retrigger(self):
        def check(case, sources):
            self.assertIn(
                "remains pending until its roughly 30–35-minute clock expires",
                sources["review_policy"],
            )
            self.assertIn(
                "request as silently dropped and issue exactly one `@codex "
                "review` re-trigger",
                sources["review_policy"],
            )
            self.assertIn(
                "starts its own 30–35-minute clock at its GitHub `created_at`",
                sources["review_policy"],
            )
            self.assertIn("do not re-trigger again", sources["review_policy"])

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_operator_authorization_is_exact_head_and_narrow(self):
        def check(case, sources):
            self.assertEqual(
                case["expected_outcome"],
                "adjudicated_wait_precedence",
            )
            self.assert_wait_gate_residual_contract(
                sources["workflow_text"],
                sources["handoff_text"],
            )

        self.assert_scenario_cases("operator_head_authorization", check)

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
                "approximately five-minute mandatory post-clean settle", handoff
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
                "[Explicit requested-review precedence]"
                "(commit-push-prs.md#explicit-requested-review-precedence)",
                handoff,
            )
            self.assertIn(
                "Do not apply the 15-minute fallback to an explicitly requested "
                "review",
                handoff,
            )

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_wait_gate_precedence_is_adjudicated_and_synced(self):
        def check(case, sources):
            self.assertEqual(
                case["expected_outcome"],
                "adjudicated_wait_precedence",
            )
            self.assert_wait_gate_residual_contract(
                sources["workflow_text"],
                sources["handoff_text"],
            )

        self.assert_scenario_cases("wait_gate_precedence", check)

    def test_wait_gate_guards_reject_frozen_mutations(self):
        self.assert_wait_gate_residual_contract(
            self.workflow_text,
            self.handoff_text,
        )
        mutations = (
            (
                "requested-review silence re-aged to 15 minutes",
                "workflow",
                "never ages into the 15-minute fallback",
                "ages into the 15-minute fallback",
            ),
            (
                "fallback broadened beyond the three named variants",
                "workflow",
                "- **Prior-head artifacts only.**",
                "- **Explicit requested-review silence.** Broadened case.\n"
                "- **Prior-head artifacts only.**",
            ),
            (
                "post-clean settle made waivable",
                "workflow",
                "re-read remain mandatory before merge",
                "re-read may be skipped after a terminal signal",
            ),
            (
                "terminal signal source altered",
                "workflow",
                "these are the only two exact-head terminal signal models",
                "these are two common exact-head terminal signal models",
            ),
            (
                "only the canonical document updated",
                "handoff",
                "[Explicit requested-review precedence]"
                "(commit-push-prs.md#explicit-requested-review-precedence)",
                "Explicit requested-review precedence",
            ),
            (
                "explicit request also enters 15-minute ageing",
                "workflow",
                "does not enter this ageing",
                "also enters this ageing",
            ),
            (
                "generic terminal signal waives post-signal handling",
                "workflow",
                "it does not waive post-signal handling",
                "it waives post-signal handling",
            ),
            (
                "automatic re-trigger repeats indefinitely",
                "workflow",
                "issue exactly one `@codex review` re-trigger",
                "repeatedly issue an `@codex review` re-trigger",
            ),
            (
                "request clock re-anchored to latest push",
                "workflow",
                "GitHub `created_at`, never to the latest push",
                "the latest push, never to GitHub `created_at`",
            ),
            (
                "eyes exits requested-review precedence",
                "workflow",
                "does not exit requested-review precedence",
                "exits requested-review precedence",
            ),
            (
                "any recorded note lifts the block",
                "workflow",
                "An ambiguous note,\na disposition not bound to the current "
                "head, or an older-head disposition does\nnot lift the merge "
                "block",
                "Any recorded note lifts the merge block",
            ),
            (
                "older-head authorization survives a push",
                "workflow",
                "Any later push invalidates the disposition and\nrestarts "
                "exact-head evaluation",
                "A later push preserves the disposition",
            ),
            (
                "authorization becomes a third connector signal",
                "workflow",
                "is not a third automated terminal-signal\nmodel",
                "is a third automated terminal-signal model",
            ),
            (
                "authorization waives independent gates",
                "workflow",
                "It does not waive independent exact-head\nreview, green "
                "required checks, mergeability, the full\ncomment/review/thread/"
                "reaction reread, unresolved-feedback handling, or\nproject/"
                "operator auto-merge authority",
                "It waives independent review, checks, and reread",
            ),
            (
                "compact guidance defines a divergent disposition effect",
                "handoff",
                "this compact guidance\ndefines no separate disposition effect",
                "this compact guidance says any disposition ends the wait",
            ),
            (
                "canonical outcomes permit either or both",
                "workflow",
                "must state exactly one",
                "may state either or both",
            ),
            (
                "compact guidance adds a contradictory disposition effect",
                "handoff",
                "timer or disposition rule.\n\nIf GitHub Codex comments",
                "timer or disposition rule.\n"
                "Nevertheless, any recorded disposition ends the "
                "requested-review wait.\n\n"
                "If GitHub Codex comments",
            ),
            (
                "compact guidance adds a synonymous human-decision effect",
                "handoff",
                "timer or disposition rule.\n\nIf GitHub Codex comments",
                "timer or disposition rule.\n"
                "Nevertheless, any recorded human decision ends the "
                "requested-review wait.\n\n"
                "If GitHub Codex comments",
            ),
        )
        for name, target, old, new in mutations:
            with self.subTest(mutation=name):
                original = (
                    self.workflow_text if target == "workflow" else self.handoff_text
                )
                self.assertEqual(original.count(old), 1)
                mutated = original.replace(old, new, 1)
                workflow_text = (
                    mutated if target == "workflow" else self.workflow_text
                )
                handoff_text = (
                    mutated if target == "handoff" else self.handoff_text
                )
                with self.assertRaises(AssertionError):
                    self.assert_wait_gate_residual_contract(
                        workflow_text,
                        handoff_text,
                    )

    def test_guard_has_no_live_project_registry_dependency(self):
        test_source = Path(__file__).read_text(encoding="utf-8")
        self.assertNotIn("projects" + ".json", test_source)


if __name__ == "__main__":
    unittest.main()
