"""Regression guard for GH-122 / PR #123 review-loop convergence contract.

The PR #120 postmortem showed an unbounded review-fix loop: a fresh
zero-context reviewer per amendment always finds new findings, the finding
family circuit breaker was skippable via the judgment clause, and no cycle or
time cap existed. These assertions pin the shared contract wording in
docs/workflows/commit-push-prs.md and docs/standalone-agent-session-bus-plan.md
so a later edit cannot silently weaken the mechanical safeguards.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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
AGENTS_DOC = REPO_ROOT / "AGENTS.md"
QUICKSTART_DOC = REPO_ROOT / "docs" / "workflows" / "collab-thread-quickstart.md"
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
            "- a finding rejected or deferred without a code change",
        )
        cls.canonical_rereview = contract_section(
            workflow_text,
            "- a finding rejected or deferred without a code change",
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
            "**All three former no-terminal-artifact fallback variants are "
            "deleted, not",
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
            "For requested-review silence, follow the canonical",
            "If GitHub Codex comments on the PR",
        ).strip()

        required_phrases = (
            (
                precedence,
                (
                    "remains pending until its roughly 30–35-minute clock expires",
                    "never ages into a merge-eligible state, because no silence "
                    "fallback exists to age into",
                    "That clock decides only when to re-trigger once and when to "
                    "escalate -- never when to merge",
                    "Anchor each clock to the corresponding explicit request "
                    "artifact's GitHub `created_at`, never to the latest push or "
                    "the time the head became reviewable",
                    "A current-head `eyes` reaction alone is non-terminal: it "
                    "does not exit requested-review precedence",
                    "issue exactly one re-trigger",
                    "The re-trigger repeats the full request shape",
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
                    "It does not waive required local exact-head verification, green "
                    "required checks, mergeability, a full re-read of "
                    "[the reviewed artifact set](#reviewed-artifact-set), or "
                    "unresolved-feedback handling",
                    "the release-gate disposition does not masquerade as that "
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
                    "with no clock attached to any of them",
                    "gate violation to fix, not a delay to wait out",
                ),
            ),
            (
                review_policy,
                (
                    "no elapsed time is ever a terminal signal",
                    "There is no resettable settle that ripens a head for merge",
                    "it does not waive post-signal handling",
                    "the approximately five-minute post-clean settle and a full "
                    "re-read of [the reviewed artifact set](#reviewed-artifact-set) "
                    "remain mandatory before merge",
                    "only two connector-authored clean signal models",
                    "third terminal gate outcome",
                ),
            ),
            (
                handoff_wait,
                (
                    "[Explicit requested-review precedence]"
                    "(commit-push-prs.md#explicit-requested-review-precedence)",
                    "**No silence fallback exists.**",
                    "repeating the focus and exact head SHA of the original request",
                    "no further automatic retry is allowed",
                    "The canonical section is the sole authority for the request-anchored "
                    "clocks, current-head invalidation, the post-timeout disposition "
                    "choices, and every effect of an exact-head release-gate disposition; "
                    "this compact guidance defines no separate disposition effect",
                    "A terminal outcome stops waiting for further artifacts only; it does "
                    "not waive the handling below",
                    "approximately five-minute mandatory post-clean settle",
                    "connector completed an exact-head review",
                    "Each outcome is terminal for the bot wait",
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
                'For requested-review silence, follow the canonical [Explicit requested-review '
                'precedence](commit-push-prs.md#explicit-requested-review-precedence). Automation may '
                'issue exactly one re-trigger, repeating the focus and exact head SHA of the original '
                'request, and no further automatic retry is allowed. The canonical section is the sole '
                'authority for the request-anchored clocks, current-head invalidation, the post-timeout '
                'disposition choices, and every effect of an exact-head release-gate disposition; this '
                'compact guidance defines no separate disposition effect. **No silence fallback exists.** '
                'All three former no-terminal-artifact variants — no explicit review request, eyes-only '
                'current-head artifact, prior-head artifacts only — are deleted, not shortened. Each '
                'measured how long to wait for a review manual-only review never sends unrequested, so '
                'each always expired into a merge on nothing. They remain a classification of non-signals '
                'with no clock: at Tier A an absent request is a **gate violation to fix, not a delay to '
                'wait out**; at Tier B/C there is nothing to wait for. This compact handoff rule must not '
                'define a competing timer or disposition rule.'
            ),
        )

        fallback_variants = re.findall(r"- \*\*([^*]+)\.\*\*", fallback)
        # All three survive as a CLASSIFICATION with no clock. Deleting the timer for only
        # the unrequested-review variant left the other two ripening a head on silence,
        # which is the same defect under a narrower name.
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
                "A capped head with zero open actionable findings and a completed "
                "exact-head connector review follows the normal merge gate with no "
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
                "Reaching the applicable cap requires a durable, operator-visible "
                "disposition, but visibility is not an approval gate",
                sources["cap"],
            )
            # v5 (2026-07-28): the 2-hour escalation became the wall-clock lane
            # budget — 4 hours or a third amended head forces a worker decision.
            self.assertIn("4 hours in the review-fix state", sources["cap"])
            self.assertIn("third amended head", sources["cap"])
            self.assertIn("merge-with-followups or close", sources["cap"])

        self.assert_scenario_cases("review_loop_cap", check)

    def test_v5_lane_contract_defer_first_and_one_reviewer(self):
        """v5 gate: contract before branch, defer-first findings, one reviewer.

        PR #347 showed the old shape: no lane contract, every finding blocking,
        up to three reviewers per head, and a cap whose only used exits were
        descope/split. Pin the v5 wording so a later edit cannot silently
        restore it.
        """
        text = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("## Lane contract (Tier A, before the first branch)", text)
        self.assertIn(
            "### Per-finding disposition at arrival (defer-first)", text
        )
        self.assertIn(
            "accept each remaining contract violation as a named, bounded risk "
            "with a follow-up issue and merge that exact head",
            text,
        )
        self.assertIn(
            "it is never a silent default for a contract violation",
            text,
        )
        self.assertIn(
            "a second independent model review on the same head must not be run",
            text,
        )
        intake = normalized(
            (REPO_ROOT / "docs" / "workflows" / "task-intake-and-delegation.md")
            .read_text(encoding="utf-8")
        )
        self.assertIn("## Lane WIP limit", intake)
        template = REPO_ROOT / "docs" / "workflows" / "lane-contract.md"
        self.assertTrue(template.is_file())

    def test_exact_head_signal_models_remain_exclusive(self):
        def check(case, sources):
            self.assertIn(
                "a clean `chatgpt-codex-connector` review/comment that explicitly "
                "covers the exact current OID is terminal for that head",
                sources["review_policy"],
            )
            # Updated 2026-07-25 with the manual-only ruling on llm-collab#310. The second
            # terminal source used to be a watcher-observed eyes-to-`+1` LIFECYCLE on the head.
            # Under manual-only the `+1` must sit on the exact manual-review REQUEST comment, and a
            # bare `eyes` is explicitly not a verdict -- so pinning the old lifecycle phrasing would
            # hold the obsolete model in place. The count of terminal sources is unchanged, which is
            # still pinned below.
            # No line break in the pinned phrase: this source is whitespace-normalised, so a
            # substring spanning a markdown wrap can never match.
            self.assertIn(
                "on the exact manual-review request",
                sources["review_policy"],
            )
            self.assertIn("is terminal CLEAN", sources["review_policy"])
            self.assertIn(
                "a bare `eyes` reaction or the request comment itself is never a",
                sources["review_policy"],
            )
            self.assertIn(
                "only two connector-authored clean signal models",
                sources["review_policy"],
            )
            self.assertIn("third terminal gate outcome", sources["review_policy"])

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_clean_verdict_gets_post_clean_settle_and_reread(self):
        def check(case, sources):
            self.assertIn(
                "approximately five-minute post-clean settle",
                sources["canonical_clean_verdict"],
            )
            self.assertIn(
                "re-read [the reviewed artifact set](#reviewed-artifact-set) in full",
                sources["canonical_clean_verdict"],
            )
            self.assertIn(
                "Do not request another connector review on the unchanged head",
                sources["canonical_rereview"],
            )
            self.assertIn(
                "the exact-head review already happened",
                sources["canonical_rereview"],
            )

        self.assert_scenario_cases("canonical_wait_gate", check)

    THREAD_FIXTURE = (
        REPO_ROOT / "tests" / "fixtures" / "review_thread_binding" / "pr313_threads_raw.json"
    )

    @staticmethod
    def originating_commit_oid(thread):
        """The AUTHORITATIVE origin of a review thread.

        `comments.nodes[0].commit.oid` is mutable: GitHub advances it to the current
        head while the thread stays non-outdated. Reading it reports every live stale
        thread as a current-head finding -- twelve of them on this very PR. The review
        commit is stable, and originalCommit is the fallback when there is no review.
        """
        comment = (thread.get("comments") or {}).get("nodes", [{}])[0]
        review = (comment.get("pullRequestReview") or {}).get("commit") or {}
        if review.get("oid"):
            return review["oid"]
        return (comment.get("originalCommit") or {}).get("oid")

    @staticmethod
    def naive_comment_commit_oid(thread):
        """The obvious wrong choice, kept so a test can prove it is wrong."""
        comment = (thread.get("comments") or {}).get("nodes", [{}])[0]
        return (comment.get("commit") or {}).get("oid")

    def classify(self, threads, head_oid):
        """Two mechanical questions only.

        Whether a written disposition has closed an unresolved thread is a MANUAL,
        human-verified judgement -- see the runbook rule that a disposition must
        identify each thread it closes. It is deliberately not computed here. Four
        attempts to model it each relocated the authority decision somewhere the test
        could not see; the last would have let an authorized comment on a DIFFERENT PR
        reading "Still unresolved: <url>" close a thread. A mention is not a closure,
        and prose is not a protocol.
        """
        # `isResolved` is METADATA, not adjudication. It never removes a thread from an
        # output: a thread someone clicked Resolve on with nothing recorded is still an
        # arriving finding that owes a written outcome, and dropping it from both counts is
        # the one way to lose one silently. Origin and current-head are classified
        # mechanically; whether a written disposition closed anything stays a human
        # judgement and is deliberately not computed here.
        at_head = [t for t in threads if self.originating_commit_oid(t) == head_oid]
        return {
            # Every finding raised at this head, whatever its resolution state.
            "exact_head_findings": len(at_head),
            # Reported alongside rather than instead of: the gate needs to know which of
            # them are still open, and the human needs to know the total it must account
            # for in writing.
            "exact_head_unresolved": len([t for t in at_head if not t["isResolved"]]),
            "arriving_total": len(threads),
            "unresolved_total": len([t for t in threads if not t["isResolved"]]),
        }

    def load_threads(self):
        fixture = json.loads(self.THREAD_FIXTURE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "disposition_artifacts", fixture,
            "closure by written disposition is a manual gate; modelling it here has "
            "produced an authority bypass every time it was attempted",
        )
        for thread in fixture["threads"]:
            # The extraction decision is what is under test, so the fixture must carry
            # the raw nested shapes and never a pre-resolved answer of any kind.
            for banned in ("originating_commit_oid", "hasWrittenDisposition",
                           "disposes_thread_ids", "isOpen"):
                self.assertNotIn(banned, thread)
            self.assertIn("comments", thread)
        return fixture

    def test_the_mutable_comment_commit_field_is_not_the_thread_origin(self):
        """The crossed live shape: comment.commit is current, review/original are old.

        Twelve threads on this PR report comments.nodes[0].commit.oid as the current
        head while pullRequestReview.commit.oid and originalCommit.oid stay at the
        review that raised them. Picking the obvious field recreates exactly the
        misclassification the binding rule exists to prevent.
        """
        fixture = self.load_threads()
        head, threads = fixture["head_oid"], fixture["threads"]
        expected = fixture["expected"]

        naive = [
            t for t in threads
            if not t["isResolved"] and self.naive_comment_commit_oid(t) == head
        ]
        self.assertEqual(expected["naive_comment_commit_count"], len(naive))
        self.assertTrue(naive, "fixture must contain the crossed shape")

        actual = self.classify(threads, head)
        self.assertEqual(expected["exact_head_findings"], actual["exact_head_findings"])
        self.assertNotEqual(
            len(naive), actual["exact_head_findings"],
            "the two field choices must disagree here, or the fixture proves nothing",
        )
        for thread in naive:
            self.assertNotEqual(head, self.originating_commit_oid(thread))

    def test_the_review_commit_wins_when_the_two_stable_fields_disagree(self):
        """Primary and fallback must be distinguishable, or the order is unproven.

        Every other case supplies the same OID in `pullRequestReview.commit` and
        `originalCommit`, so swapping the documented primary and fallback leaves
        them all green while binding findings to the wrong head on the one shape
        where it matters: a thread carried forward into a later review.
        """
        def node(review_oid, original_oid):
            comment = {
                "commit": {"oid": "MUTABLE-MUST-NOT-BE-READ"},
                "originalCommit": {"oid": original_oid},
                "pullRequestReview": (
                    {"commit": {"oid": review_oid}} if review_oid else None
                ),
            }
            return {"isResolved": False, "isOutdated": False,
                    "comments": {"nodes": [comment]}}

        self.assertEqual(
            "head999", self.originating_commit_oid(node("head999", "old111")),
            "the backing review's commit is authoritative over originalCommit",
        )
        self.assertEqual(
            "old111", self.originating_commit_oid(node("old111", "head999")),
            "originalCommit must not override the backing review's commit",
        )
        self.assertEqual(
            "head999", self.originating_commit_oid(node(None, "head999")),
            "originalCommit is the fallback only when no review backs the thread",
        )

        crossed = [node("head999", "old111"), node("old111", "head999")]
        actual = self.classify(crossed, "head999")
        self.assertEqual(1, actual["exact_head_findings"])
        self.assertEqual(2, actual["unresolved_total"])

    def test_outdated_is_not_the_exclusion_criterion_on_real_graphql_shapes(self):
        fixture = self.load_threads()
        threads, expected = fixture["threads"], fixture["expected"]
        actual = self.classify(threads, fixture["head_oid"])
        self.assertEqual(expected["unresolved_total"], actual["unresolved_total"])
        dropped = [t for t in threads if not t["isResolved"] and t["isOutdated"]]
        self.assertEqual(expected["unresolved_outdated"], len(dropped))
        self.assertTrue(dropped, "fixture must contain an unresolved OUTDATED thread")
        self.assertLess(actual["exact_head_findings"], actual["unresolved_total"])

    def test_a_thread_from_an_older_review_is_not_an_exact_head_finding(self):
        def node(review_oid, original_oid, comment_oid, *, resolved, outdated):
            return {
                "isResolved": resolved,
                "isOutdated": outdated,
                "comments": {"nodes": [{
                    "commit": {"oid": comment_oid},
                    "originalCommit": {"oid": original_oid},
                    "pullRequestReview": (
                        {"commit": {"oid": review_oid}} if review_oid else None
                    ),
                }]},
            }

        threads = [
            # Crossed: comment.commit says head, the review says otherwise.
            node("old111", "old111", "head999", resolved=False, outdated=False),
            # Outdated but genuinely raised on this head -- still a finding.
            node("head999", "head999", "head999", resolved=False, outdated=True),
            # Resolved on this head -- closed by GitHub, no judgement needed.
            node("head999", "head999", "head999", resolved=True, outdated=False),
            # No backing review: originalCommit is the fallback authority.
            node(None, "head999", "head999", resolved=False, outdated=False),
        ]
        actual = self.classify(threads, "head999")
        # Three findings ARRIVED at this head; two are still open. The resolved one is
        # counted, because `isResolved` is metadata and a thread someone closed with
        # nothing recorded still owes a written outcome.
        self.assertEqual(3, actual["exact_head_findings"])
        self.assertEqual(2, actual["exact_head_unresolved"])
        self.assertEqual(3, actual["unresolved_total"])
        self.assertEqual("old111", self.originating_commit_oid(threads[0]))
        self.assertEqual("head999", self.originating_commit_oid(threads[3]))

    # Field names that LOOK like closure. Retained only as a readability aid for the
    # access guard below; the guard does not depend on this list being complete, and an
    # earlier version that did was defeated by a name absent from it.
    CLOSURE_LOOKING_FIELDS = {
        "hasWrittenDisposition": True,
        "manualDisposition": True,
        "dispositioned": True,
        "closed": True,
        "disposition": "superseded by the rewrite",
        "adjudicated": True,
        "resolvedByComment": "PRRC_kwDO_whatever",
        "state": "RESOLVED",
    }

    class StrictMapping(Mapping):
        """A read-only Mapping that refuses every key outside its allowlist.

        Five versions of this guard have been defeated, each by the same move at a new
        depth: banned helper names (beaten from inside the method), enumerated closure
        field names (beaten by an unlisted name), a dict subclass overriding two methods
        (beaten by inherited setdefault), iteration yielding allowed keys (inert in the
        harness, live in production), and a top-level-only guard (beaten by reading
        `comments.nodes[0]`, an ordinary nested dict), and a mappings-only guard
        (beaten by `len(t["comments"]["nodes"])`, an ordinary nested list).

        So the constraint is applied at every level the origin helper walks, not just
        the outermost one -- and `StrictSequence` covers the levels that are lists.
        Iteration and len raise rather than yielding permitted keys, because a scan that
        finds nothing here would still find the field against a real GraphQL dict in
        production.
        """

        def __init__(self, data, allowed, label):
            self._data = dict(data)
            self._allowed = frozenset(allowed)
            self._label = label

        def __getitem__(self, key):
            if key not in self._allowed:
                raise AssertionError(
                    f"read {key!r} on {self._label}; only {sorted(self._allowed)} allowed"
                )
            return self._data[key]

        def __iter__(self):
            raise AssertionError(
                f"iterated {self._label}; keys must be read directly, not enumerated"
            )

        def __len__(self):
            raise AssertionError(f"measured {self._label}")

        def __bool__(self):
            # Mapping falls back to __len__ for truthiness, and the origin helper's
            # `thread.get("comments") or {}` is a legitimate presence check rather than
            # an enumeration. Answering it directly keeps len() closed as a probe while
            # letting production-shaped code run unchanged.
            return True

        def hidden(self):
            return {k: v for k, v in self._data.items() if k not in self._allowed}

    class StrictSequence(Sequence):
        """A list that permits only the index the origin helper reads.

        `comments.nodes` was the last ordinary container. Every fixture thread holds
        exactly one node, so a closure filter like `len(t["comments"]["nodes"]) == 1`
        could be added to both counts without tripping any guard or changing any
        expected number -- while silently excluding every real thread that has replies.
        Length, iteration, slices and any other index raise here.
        """

        def __init__(self, items, label):
            self._items = list(items)
            self._label = label

        def __getitem__(self, index):
            if index != 0:
                raise AssertionError(
                    f"read index {index!r} of {self._label}; only [0] is permitted"
                )
            return self._items[0]

        def __iter__(self):
            raise AssertionError(f"iterated {self._label}; read [0] directly")

        def __len__(self):
            raise AssertionError(
                f"measured {self._label}; a node count is not an input to classification"
            )

        def __bool__(self):
            return True

    def strict_thread(self, oid, *, resolved, decorated, backed=True):
        """A thread guarded at EVERY level the origin helper walks.

        `commit` is deliberately forbidden on the comment node: it is the mutable field
        the origin rule must never consult, so reading it fails loudly here rather than
        silently returning the current head.

        `backed=False` drops the backing review so the fallback branch runs. With a
        review always present the helper returns before it ever touches
        `originalCommit`, which left that mapping guarded but never exercised.
        """
        def strict_commit(commit_oid, label):
            """The commit mappings are walked too, so they are guarded too.

            Left as plain dicts these were the last undecorated level: a closure
            read added inside the origin helper could probe a new field on either
            one and pass, because nothing here would object.
            """
            data = {"oid": commit_oid}
            if decorated:
                data.update(self.CLOSURE_LOOKING_FIELDS)
                data["someFieldNobodyHasThoughtOfYet"] = True
            return self.StrictMapping(data, {"oid"}, label)

        review = dict(self.CLOSURE_LOOKING_FIELDS) if decorated else {}
        review["commit"] = strict_commit(oid, "pullRequestReview.commit")
        comment = {
            "commit": strict_commit("MUTABLE-MUST-NOT-BE-READ", "comment.commit"),
            "originalCommit": strict_commit(oid, "originalCommit"),
            "pullRequestReview": (
                self.StrictMapping(review, {"commit"}, "pullRequestReview")
                if backed
                else None
            ),
        }
        if decorated:
            comment.update(self.CLOSURE_LOOKING_FIELDS)
            comment["someFieldNobodyHasThoughtOfYet"] = True
        comments = {"nodes": self.StrictSequence(
            [self.StrictMapping(comment, {"originalCommit", "pullRequestReview"}, "comment")],
            "comments.nodes",
        )}
        data = {
            "isResolved": resolved,
            "comments": self.StrictMapping(comments, {"nodes"}, "comments"),
        }
        if decorated:
            data.update(self.CLOSURE_LOOKING_FIELDS)
            data["someFieldNobodyHasThoughtOfYet"] = True
        return self.StrictMapping(data, {"isResolved", "comments"}, "thread")

    def test_classify_consults_only_isresolved_and_origin(self):
        """Proved by access, not by enumeration.

        classify() may read exactly two top-level keys. Anything else raises on the
        first lookup, so a closure exclusion cannot be smuggled in under a name the
        test did not anticipate -- which is precisely how the previous two versions of
        this guard were defeated.
        """
        plain = [
            self.strict_thread("head999", resolved=False, decorated=False),
            self.strict_thread("old111", resolved=False, decorated=False),
            self.strict_thread("head999", resolved=True, decorated=False),
            self.strict_thread("head999", resolved=False, decorated=False, backed=False),
        ]
        baseline = self.classify(plain, "head999")
        self.assertEqual(3, baseline["exact_head_findings"])
        self.assertEqual(2, baseline["exact_head_unresolved"])
        self.assertEqual(3, baseline["unresolved_total"])

        decorated = [
            self.strict_thread("head999", resolved=False, decorated=True),
            self.strict_thread("old111", resolved=False, decorated=True),
            self.strict_thread("head999", resolved=True, decorated=True),
            self.strict_thread("head999", resolved=False, decorated=True, backed=False),
        ]
        self.assertEqual(
            baseline, self.classify(decorated, "head999"),
            "closure-looking fields changed the counts",
        )

    def test_the_access_guard_actually_refuses(self):
        """A guard that never fires is decoration.

        If StrictThread silently permitted unknown keys, the contract above would pass
        against any implementation at all.
        """
        node = self.strict_thread("head999", resolved=False, decorated=True)
        for key in ("hasWrittenDisposition", "manualDisposition", "reviewDisposition",
                    "someFieldNobodyHasThoughtOfYet"):
            with self.subTest(key=key):
                if key != "reviewDisposition":
                    self.assertIn(key, node.hidden(), "must be present but unreadable")
                with self.assertRaises(AssertionError):
                    node[key]
                with self.assertRaises(AssertionError):
                    node.get(key)
                with self.assertRaises(AssertionError):
                    key in node
                # Mutating access paths must not exist at all, so reaching for one is
                # an error rather than a silent bypass.
                for method in ("setdefault", "pop", "update", "__setitem__"):
                    self.assertFalse(
                        hasattr(node, method),
                        f"{method} would bypass the allowed-key contract",
                    )
        # Enumeration is refused outright, so a scan cannot find a hidden key here and
        # then behave differently against a real dict in production.
        with self.assertRaises(AssertionError):
            set(node)
        with self.assertRaises(AssertionError):
            len(node)
        # And the two permitted keys must still work, or classify() could not run.
        self.assertIs(False, node["isResolved"])
        self.assertIn("nodes", node["comments"])

    def test_closure_by_written_disposition_is_not_automated(self):
        """Deleting this classifier was the fix, so the deletion needs a guard.

        A machine rule over free-form prose cannot establish that an authorized person
        closed a specific finding on THIS pull request. Every version tried let
        something through: an arbitrary boolean, an invented id mapping, a preselected
        artifact list, and finally a bare mention -- an authorized comment on a
        different PR reading "Still unresolved: <url>" would have closed the thread.
        The runbook carries the requirement and a human applies it.

        Asserted by introspection rather than by grepping this file, which would match
        the names written in this docstring.
        """
        import inspect

        for banned in ("dispositioned_thread_ids", "closed_ids", "is_open"):
            self.assertFalse(
                hasattr(type(self), banned),
                f"{banned} reintroduces automated closure over prose",
            )
        params = list(inspect.signature(type(self).classify).parameters)
        self.assertEqual(
            ["self", "threads", "head_oid"], params,
            "classify must take no closure input; a disposition argument is the "
            "authority decision re-entering through the caller",
        )
        fixture = self.load_threads()
        self.assertEqual(
            {"source", "why", "head_oid", "threads", "expected"}, set(fixture),
            "the fixture holds raw GraphQL evidence only",
        )
        # The requirement itself must survive in the runbook, unautomated.
        section = contract_section(
            WORKFLOW_DOC.read_text(encoding="utf-8"),
            "- **bind an exact-head finding through its initiating review commit",
            "- a head-named clean connector verdict is not merge-immediate.",
        )
        self.assertIn("that identifies the thread", section)

    def test_identification_is_necessary_but_not_sufficient_for_closure(self):
        """A mechanical consumer must not read "identifies the thread" as "closes it".

        `Still unresolved: <url>` identifies a thread perfectly. Without the human
        validation clause the rule reads as a recipe a consumer could implement, which
        is the same bypass the deleted classifier kept reinventing.
        """
        section = contract_section(
            WORKFLOW_DOC.read_text(encoding="utf-8"),
            "- **bind an exact-head finding through its initiating review commit",
            "- a head-named clean connector verdict is not merge-immediate.",
        )
        for phrase in (
            "necessary and not sufficient",
            "someone authorised on *this* pull request",
            "Still unresolved:",
            "Closure is never derived mechanically from a body",
            "not so a consumer can skip the human",
        ):
            self.assertIn(phrase, section)

    def test_the_request_limit_exempts_the_single_re_trigger(self):
        """The absolute reading forbids the only recovery the same file mandates.

        `Request once per candidate final head` and `issue exactly one re-trigger` are
        contradictory as written, leaving a worker to violate one of them.
        """
        # normalized(): the phrases wrap across lines in both documents.
        for doc in (WORKFLOW_DOC, AGENTS_DOC):
            text = normalized(doc.read_text(encoding="utf-8"))
            with self.subTest(doc=doc.name):
                self.assertIn("one initial request per candidate final head", text)
                self.assertNotIn("Request **once per candidate final head**", text)
        workflow = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("the single request-anchored re-trigger below is", workflow)
        self.assertIn("explicit exemption", workflow)
        self.assertIn(
            "the single request-anchored re-trigger in",
            normalized(AGENTS_DOC.read_text(encoding="utf-8")),
        )

    def test_a_reaction_is_bound_to_the_latest_unedited_request(self):
        """GitHub preserves reactions across an edit.

        A request comment edited to swap an old SHA for the current one carries the
        `+1` the connector left for the OLD head, and all four checks then pass on a
        review that never happened.
        """
        for doc in (WORKFLOW_DOC, HANDOFF_DOC):
            text = normalized(doc.read_text(encoding="utf-8"))
            with self.subTest(doc=doc.name):
                self.assertIn("latest, unedited request artifact", text)
                self.assertNotIn("same-head re-review request", text)

    def test_resolution_state_never_removes_a_thread_from_an_output(self):
        """`isResolved` is metadata, not adjudication.

        Filtering both outputs on it meant a thread someone clicked Resolve on with nothing
        recorded appeared in neither -- so the gate could not see it and the human had no
        total to account for. That is the silent-loss path the whole section exists to
        close, and the classifier modelling it was the last place it survived.
        """
        def node(oid, resolved):
            return {
                "isResolved": resolved,
                "isOutdated": False,
                "comments": {"nodes": [{
                    "commit": {"oid": "MUTABLE-MUST-NOT-BE-READ"},
                    "originalCommit": {"oid": oid},
                    "pullRequestReview": {"commit": {"oid": oid}},
                }]},
            }

        threads = [node("head999", True), node("head999", False), node("old111", True)]
        actual = self.classify(threads, "head999")
        self.assertEqual(
            3, actual["arriving_total"],
            "every arriving finding is counted, whatever its resolution state",
        )
        self.assertEqual(
            2, actual["exact_head_findings"],
            "a resolved thread raised at this head is still a finding at this head",
        )
        self.assertEqual(1, actual["exact_head_unresolved"])
        self.assertEqual(1, actual["unresolved_total"])

        # The load-bearing property, stated as a difference: resolving everything must not
        # empty the enumeration.
        all_resolved = self.classify([node("head999", True), node("head999", True)], "head999")
        self.assertEqual(2, all_resolved["exact_head_findings"])
        self.assertEqual(0, all_resolved["exact_head_unresolved"])

    def test_the_reviewed_artifact_set_is_written_exactly_once(self):
        """Twelve sites had drifted into five spellings; the first pin could only see two.

        A re-review request lives in a comment and a terminal `+1` lives in a reaction, so
        each omission was a live path to merging on a superseded signal.

        The first version of this pin read two documents and matched fixed spellings one
        line at a time, and three restatements walked straight through it -- including
        `commit-push-prs.md`'s own "top-level PR comments as well as reviews, threads and
        reactions", which escaped twice over: it wrapped across a line break, and it used
        the short forms. That is the same defect as the drift it was pinning, one level up:
        the rule was re-derived from what a restatement was assumed to look like instead of
        read from what restatements actually are.

        So: every markdown document, whitespace-normalized so a line break hides nothing,
        short and plural forms recognized, and the threshold is *naming three or more of
        the kinds* rather than matching any particular wording. Sites naming one or two are
        left alone -- prose has to be able to say "review threads" -- but three is an
        enumeration no matter how it is spelled.
        """
        import re

        # What makes a site a restatement is that it ENUMERATES -- three artifact heads in
        # a run joined by list separators. Merely counting mentions flagged prose that
        # explains one artifact in terms of another ("the connector posts its findings as
        # inline review threads, and the review body can be boilerplate"), which is not a
        # copy of the list and must stay sayable. Spelled-out phrases were tried first and
        # let `review/thread/reaction` through, so the heads are matched as word stems and
        # `review` is discounted where it modifies another head.
        HEAD = r"(?:comments?|bodies|body|threads?|reactions?|reviews?)"
        ITEM = rf"(?:(?:top-level|PR|inline|connector|full|review|the)[ -])*{HEAD}"
        SEP = r"(?:\s*/\s*|[,;]\s+(?:and\s+|or\s+)?|\s+(?:and|or|as well as)\s+)"
        RUN = re.compile(rf"{ITEM}(?:{SEP}{ITEM}){{2,}}", re.I)

        def kinds(sentence):
            run = RUN.search(sentence)
            if not run:
                return set()
            text = re.sub(r"review(?=[ -](?:thread|comment|bod))", "", run.group(0), flags=re.I)
            return {h for h in ("comment", "review", "bod", "thread", "reaction")
                    if re.search(rf"\b{h}\w*", text, re.I)}
        block = "<a id=\"reviewed-artifact-set\"></a>"
        workflow = WORKFLOW_DOC.read_text(encoding="utf-8")
        self.assertEqual(1, workflow.count(block), "the normative block must exist once")

        # Every INSTRUCTION-bearing tree, not just docs/ and the root. The contract
        # requires project notes and agent guidance to reference this contract rather
        # than restate it, so `projects/` and `agents/` are exactly where a copied
        # enumeration lands unseen.
        #
        # Scoped by an allowlist rather than by scanning everything: `Chats/`, `Evidence/`
        # and `Tasks/` are durable RECORDS. A message in which someone once listed the
        # five artifacts is history and must stay readable as written -- rewriting a
        # delivered packet to satisfy a lint would falsify the archive.
        INSTRUCTION_TREES = ("docs", "projects", "agents", "examples", "schemas")
        documents = sorted(REPO_ROOT.glob("*.md"))
        for tree in INSTRUCTION_TREES:
            documents.extend(sorted((REPO_ROOT / tree).rglob("*.md")))
        self.assertIn(WORKFLOW_DOC, documents, "the scan must cover the canonical document")
        # The SCOPE is asserted, not the contents. Requiring a markdown file under each
        # tree passed only because of an untracked local file: `agents/` holds nothing but
        # `.gitkeep` in a clean checkout, so that assertion failed unconditionally for
        # anyone else -- a test that depended on one machine's stray file.
        self.assertIn("agents", INSTRUCTION_TREES)
        self.assertIn("projects", INSTRUCTION_TREES)
        self.assertTrue(
            any(d.is_relative_to(REPO_ROOT / "projects") for d in documents),
            "projects/ has tracked markdown, so the scan must be reaching it",
        )

        # A link to the canonical set is stripped, not treated as absolution. Exempting
        # the whole sentence let a complete restatement pass by appending the link to it:
        # "re-read comments, review bodies, threads and reactions; see [the reviewed
        # artifact set](...)" named all five and skipped the check.
        LINK = re.compile(r"\[[^\]]*\]\([^)]*reviewed-artifact-set\)|`[^`]*reviewed-artifact-set[^`]*`")

        def units(text):
            """Blocks of prose, with a bulleted list kept WITH its lead-in.

            Splitting before every bullet made a real list -- "Re-read the following:"
            over four bullets -- read as four unrelated one-item blocks, none of which
            reached the threshold. Merging every adjacent bullet instead made two
            unrelated bullets look like one enumeration. The lead-in is what separates
            them: a line ending in a colon introduces the bullets under it; bullets
            without one are their own statements.
            """
            bullet = re.compile(r"\s*(?:[-*]|\d+\.)\s")

            def flatten(chunk):
                """Normalize, turning bullet markers into list separators.

                A bulleted enumeration is still an enumeration: joining the lines with
                spaces left `- comments - review bodies - threads`, which matches no
                separator, so the run never formed and the list read as prose.
                """
                joined = re.sub(r"\n\s*(?:[-*]|\d+\.)\s+", ", ", chunk)
                return re.sub(r"\s+", " ", joined)

            previous = ""
            for para in re.split(r"\n\s*\n", text):
                lines = [l for l in para.split("\n") if l.strip()]
                if not lines:
                    continue
                all_bullets = all(bullet.match(l) for l in lines)
                # A list introduced by a colon belongs to its lead-in, whether or not a
                # blank line sits between them -- markdown allows both, and the version
                # that only handled the no-blank-line case missed the ordinary spelling.
                if all_bullets and previous.rstrip().endswith(":"):
                    yield flatten(previous + "\n" + para)
                    previous = para
                    continue
                lead = lines[0].strip()
                if lead.endswith(":") and any(bullet.match(l) for l in lines[1:]):
                    yield flatten(para)
                else:
                    for chunk in re.split(r"\n(?=\s*(?:[-*]|\d+\.)\s)", para):
                        yield re.sub(r"\s+", " ", chunk)
                previous = para

        offenders = []
        for document in documents:
            text = document.read_text(encoding="utf-8")
            if document == WORKFLOW_DOC:
                start = text.index(block)
                end = text.index("Referenced, never restated.", start)
                text = text[:start] + text[end:]
            for unit in units(text):
                # Split on sentence ends only. A semicolon LINKS clauses -- "inspect
                # comments; review bodies; threads; reactions" is one list -- so splitting
                # there first cut a genuine enumeration into one-kind fragments that could
                # never reach the threshold.
                for sentence in re.split(r"(?<=[.]) ", LINK.sub(" ", unit)):
                    named = kinds(sentence)
                    if len(named) >= 3:
                        offenders.append(f"{document.name}: {sentence.strip()[:110]}")
        self.assertEqual(
            [], offenders,
            "these sites restate the artifact list instead of referencing the one place it "
            "is written",
        )

    def test_the_artifact_set_is_defined_for_a_lane_with_no_github(self):
        """Naming five GitHub artifacts unconditionally made Tier A unsatisfiable.

        The section directly above defines a registered project with no GitHub surface and
        says it owes the same review. If every read-review-state instruction then means
        five GitHub-only artifacts, that lane is required to read artifacts that cannot
        exist -- a gate nobody can pass reads as an exemption, which is the one thing the
        section says it is not.
        """
        workflow = WORKFLOW_DOC.read_text(encoding="utf-8")
        start = workflow.index("<a id=\"reviewed-artifact-set\"></a>")
        section = workflow[start:workflow.index("Referenced, never restated.", start)]
        self.assertIn("no GitHub surface", section)
        self.assertIn("review request", section)
        self.assertIn("verdict packet", section)
        self.assertRegex(
            section, r"means the set below for the lane",
            "the set must be selected by lane rather than stated unconditionally",
        )

    def test_the_mailbox_lane_has_an_equivalent_for_every_github_anchor(self):
        """A lane that may request a review must be able to finish waiting for one.

        Requested-review precedence anchored both clocks to GitHub `created_at`,
        re-triggered with a GitHub comment, and waited for two GitHub connector signals.
        A mailbox lane has none of those, so a Tier A non-GitHub head could request and
        then stall forever -- or invent its own timing, which is a second contract.
        """
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("every anchor in this section has a mailbox equivalent", text)
        for equivalent in (
            "request packet naming the exact commit OID",
            "recorded delivery timestamp",
            "marked as the re-trigger",
            "verdict or disposition packet naming that exact OID",
        ):
            self.assertIn(equivalent, text, f"no mailbox equivalent for {equivalent!r}")
        # The clocks and the single-re-trigger rule are properties of the request, so
        # they must NOT be redefined per lane -- that would be the competing contract.
        self.assertIn(
            "are unchanged — they are properties of the request, not of GitHub", text
        )

    def test_an_unrequested_tier_a_head_offers_no_waiver_alternative(self):
        """"Fix the missing request, OR go to the operator disposition" was a choice.

        Requested-review precedence is reachable only after a request and its single
        re-trigger have both existed and run out. Offering it beside the missing request
        reopened the closed path where a worker never requests review at all and asks for
        authorization on an unreviewed head instead -- the waiver is the end of the
        requested flow, not a substitute for entering it.
        """
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertNotIn("Fix the missing request, or follow", text)
        self.assertIn("the two cases are **not** alternatives", text)
        self.assertIn("issue it. That is the only move", text)
        self.assertRegex(
            text,
            r"reachable only after a request and its single re-trigger have both existed",
        )

    def test_a_finding_may_be_rejected_in_writing_without_a_fix(self):
        """Requiring a fix for every finding left an invalid one with no legal move.

        The amended-head sequence began by requiring the pointed issue to be fixed, so a
        wrong or out-of-scope finding forced an unwarranted change or a stall -- and there
        is no amended head in that case for the rest of the sequence to apply to. The
        contract asks for written adjudication, which includes rejecting.
        """
        import re

        text = re.sub(r"\s+", " ", HANDOFF_DOC.read_text(encoding="utf-8"))
        self.assertIn("adjudicated in writing — which is not the same as accepted", text)
        self.assertIn("**Reject it.**", text)
        self.assertIn("no code changes means no amended head".lower(),
                      text.lower())
        self.assertIn("no new request", text)
        self.assertIn(
            "completed exact-head review plus that disposition is sufficient",
            text,
        )

    def test_a_withdrawal_is_bound_to_the_head_it_was_granted_on(self):
        """An operator decision about one head was readable as a standing exemption.

        The fixture helper already required withdrawal evidence naming the current head,
        but that requirement lived only in the tests: the worker-facing rule said
        "explicit" and nothing more, so a withdrawal recorded before an amendment could
        be read as retiring the new head's obligation too.
        """
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("withdraws the review requirement **for that exact head**", text)
        self.assertIn("naming the commit OID it applies to", text)
        self.assertIn("a later push invalidates it", text)

    def test_the_rule_heading_covers_resolved_threads(self):
        """A worker who reads only the bold heading is the one who loses a finding.

        The heading said "every unresolved thread" while the requirement below it said
        resolved threads owe an outcome too -- so Resolve-with-nothing-recorded was
        sanctioned by the summary and forbidden by the detail.
        """
        text = WORKFLOW_DOC.read_text(encoding="utf-8")
        self.assertIn("every thread, resolved or not, regardless of `isOutdated`", text)
        self.assertNotIn("every unresolved thread regardless of `isOutdated`", text)

    def test_a_threadless_finding_still_needs_a_written_outcome(self):
        """A review body has no node ID, so the thread-linking rule cannot reach it.

        And "no longer unresolved" is not a standard prose can meet, because nobody
        resolves a comment -- so a finding raised in a body was discharged by whoever
        pushed next.
        """
        text = WORKFLOW_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "an actionable finding that arrived with no thread carries a written outcome",
            text,
        )
        self.assertIn("Quote or link the comment", text)

    def test_every_summary_states_the_resolved_finding_rule(self):
        """One stale summary is a working instruction path to a lost finding.

        The checklist was corrected to enumerate every thread, and the v4 contract
        summary, this runbook's rationale and the quickstart all still described the
        unresolved-only model -- three alternative paths a worker could follow to resolve a
        thread without recording its outcome and pass the gate.
        """
        for doc, phrases in (
            (AGENTS_DOC, ("**arriving finding**", "resolved or not")),
            (WORKFLOW_DOC, ("enumerates **every** thread, resolved or not",)),
            (QUICKSTART_DOC, ("enumerates every thread, resolved or not",)),
        ):
            text = doc.read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(doc=doc.name, phrase=phrase):
                    self.assertIn(phrase, text)

    def test_the_reaction_path_rereads_request_comments(self):
        """A re-review request posted during the settle supersedes the older `+1`.

        The mandatory re-read covered reviews, threads and reactions but not top-level
        comments -- which is where a request lives -- so it could merge on a reaction that
        no longer passed the latest-request check.
        """
        # Whitespace-normalized deliberately. Two earlier versions of this assertion
        # pinned the exact line wrapping and broke when the sentence was reflowed -- the
        # same mistake as the drift being pinned, since where a line happens to break
        # carries no meaning and a rule that depends on it is a rule about layout.
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn(
            "covers **all of [the reviewed artifact set](#reviewed-artifact-set)**", text
        )
        self.assertIn("revalidates all six reaction conditions", text)

    def test_the_quickstart_does_not_paraphrase_the_tier_lists(self):
        """Both paraphrases drifted, in opposite directions.

        The Tier C one dropped the canonical qualifiers; the Tier A one omitted provider
        and idempotency paths and the already-produced-a-finding family. A summary of a
        tier list is a second source that goes stale the moment the first one moves.
        """
        text = QUICKSTART_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "no short version, of the inclusions or the exclusions", text
        )
        self.assertNotIn("Short version:", text)

    def test_the_contract_version_advanced_with_the_gate_rewrite(self):
        """A cached copy of the old gate can produce a wrong merge.

        Workers bootstrapped on v3 get no signal that the fallback, reaction lifecycle,
        request shape and authority rules changed underneath them; workers on v4 get
        no signal that v5 added the lane contract, defer-first findings, one reviewer
        per head, and the merge-or-kill lane budget.
        """
        text = AGENTS_DOC.read_text(encoding="utf-8")
        self.assertIn("<!-- CONTRACT_VERSION: 5 -->", text)
        self.assertNotIn("<!-- CONTRACT_VERSION: 3 -->", text)
        entry = contract_section(text, "- **v4 (2026-07-26)**", "- **v3 (2026-07-26)**")
        for phrase in (
            "silence fallback is **deleted**",
            "one *initial* request per candidate final head",
            "body** listing no findings is not a clean verdict",
            "never** the mutable `comment.commit.oid`",
            "a push is not an adjudication",
            "latest unedited request artifact",
        ):
            self.assertIn(normalized(phrase), entry)
        v5_entry = contract_section(
            text, "- **v5 (2026-07-28)**", "- **v4 (2026-07-26)**"
        )
        for phrase in (
            "lane contract",
            "per-finding",
            "One external reviewer per head",
            "merge-with-followups",
            "merge-with-followups-or-close",
            "review_request.py",
            "two active implementation lanes",
        ):
            self.assertIn(normalized(phrase), v5_entry)

    def test_the_merge_checklist_does_not_inherit_the_origin_rule_narrowing(self):
        """The hole the origin rule opened in the checklist below it.

        Binding findings to the initiating commit answers "is this about this head".
        The final checklist asked only for no unresolved feedback "for the current
        head", so once the origin rule narrowed that phrase, a worker could satisfy the
        checklist with prior-head threads nobody had answered -- recreating the silent
        drop the same section forbids.

        Phrasing the bullet over *unresolved* threads left the second way to lose a
        finding: clicking Resolve without recording anything takes the thread out of
        that set for good. The checklist is phrased over every arriving finding now,
        whatever its resolution state.
        """
        section = contract_section(
            WORKFLOW_DOC.read_text(encoding="utf-8"),
            "Proceed only when all of these are true:",
            "Read [the reviewed artifact set](#reviewed-artifact-set) directly.",
        )
        for phrase in (
            "**every arriving finding has a thread-linked written outcome, whatever "
            "head it was initiated on and whatever its current resolution state.**",
            "it does not narrow this checklist",
            "a prior-head thread nobody answered is unadjudicated rather than closed",
            "is no longer *unresolved*",
            "Enumerate every thread, resolved or not",
        ):
            self.assertIn(phrase, section)

    def test_the_silence_disposition_satisfies_review_completion(self):
        section = contract_section(
            WORKFLOW_DOC.read_text(encoding="utf-8"),
            "Proceed only when all of these are true:",
            "Read [the reviewed artifact set](#reviewed-artifact-set) directly.",
        )
        self.assertIn(
            "release-gate disposition lifted the missing review-completion "
            "subgate without claiming the connector completed",
            normalized(section),
        )

    def test_the_compact_handoff_requires_a_new_request_after_a_fix_push(self):
        """With automatic review off, nothing replaces an invalidated signal.

        The compact handoff still told a worker to evaluate the amended head's
        "automatic artifacts". A fix push invalidates every prior-head signal and
        produces no replacement on its own, so that instruction waits forever.
        """
        section = contract_section(
            HANDOFF_DOC.read_text(encoding="utf-8"),
            "If GitHub Codex comments on the PR",
            "Do not substitute a resolved older thread",
        )
        for phrase in (
            "issue a new exact-head request for",
            "Automatic review is off, so nothing arrives unrequested",
            "waits forever",
        ):
            self.assertIn(phrase, section)
        self.assertNotIn("automatic artifacts from scratch", section)

    def test_the_doc_names_the_authoritative_field_and_rejects_the_mutable_one(self):
        text = WORKFLOW_DOC.read_text(encoding="utf-8")
        section = contract_section(
            text,
            "- **bind an exact-head finding through its initiating review commit",
            "- a head-named clean connector verdict is not merge-immediate.",
        )
        for phrase in (
            "`comments.nodes[0].pullRequestReview.commit.oid`",
            "`comments.nodes[0].originalCommit.oid`",
            "**Never `comments.nodes[0].commit.oid`**",
            "that field is mutable",
            "A push is not an adjudication**",
            "that identifies the thread",
            "node ID or its `#discussion_r...` comment URL",
            "closes nothing",
            "diff-position metadata",
            "wrong in both directions",
        ):
            self.assertIn(phrase, section)
        self.assertNotIn("both unresolved and not outdated", text)

    def test_an_empty_connector_review_body_is_not_a_clean_verdict(self):
        """The trap that nearly defeated this gate on 2026-07-26.

        The connector posts findings as inline review threads. Its review BODY can be
        pure boilerplate -- heading, reviewed commit, collapsed "About Codex" section --
        while unresolved P1 threads sit on the same head. On llm-collab#317 at 87e8e47
        the body listed nothing and six live threads existed, three of them P1. Reading
        the body as the verdict is a merge on unaddressed P1s.
        """
        section = contract_section(
            WORKFLOW_DOC.read_text(encoding="utf-8"),
            "- **a connector review body that lists no findings is not a clean verdict.**",
            "- a finding rejected or deferred without a code change",
        )
        for phrase in (
            "posts its findings as inline review threads",
            "the review body can be boilerplate",
            "`reviewThreads`, not the body, is the finding list",
        ):
            self.assertIn(phrase, section)

    def test_the_request_shape_itself_names_focus_and_the_exact_head_sha(self):
        """The source-of-truth request template, pinned.

        A reaction is terminal only on a comment that named the current head, so a
        template without a SHA makes the reaction path unsatisfiable -- the rule and the
        syntax that is supposed to satisfy it have to be pinned together or they drift
        apart again.
        """
        section = contract_section(
            AGENTS_DOC.read_text(encoding="utf-8"),
            "Request with `@codex review for <focus>`",
            '**"Untrusted" means input we do not control.**',
        )
        for phrase in (
            "naming **every** Tier A family the diff touches",
            "**stating the exact head SHA the request is for**",
            "a connector `+1` is terminal only while the head still equals the SHA "
            "that request named",
            "a request without one leaves the reaction path unsatisfiable",
            "Issue **one initial request per candidate final head**",
            "the single request-anchored re-trigger",
        ):
            self.assertIn(phrase, section)

    def test_silently_dropped_review_gets_one_request_anchored_retrigger(self):
        def check(case, sources):
            self.assertIn(
                "remains pending until its roughly 30–35-minute clock expires",
                sources["review_policy"],
            )
            self.assertIn(
                "request as silently dropped and issue exactly one re-trigger",
                sources["review_policy"],
            )
            # GH-313 finding 3: the retry shape is the contract, not prose. A bare
            # `@codex review` names no SHA, and a reaction on a comment that named no
            # SHA can never satisfy the terminal-signal rule.
            self.assertIn(
                "The re-trigger repeats the full request shape — focus and the exact "
                "head SHA — never a bare `@codex review`",
                sources["review_policy"],
            )
            self.assertIn(
                "starts its own 30–35-minute clock at its GitHub `created_at`",
                sources["review_policy"],
            )
            self.assertIn("do not re-trigger again", sources["review_policy"])

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_release_gate_disposition_is_exact_head_and_narrow(self):
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
                "`descope`, `split`, `backend-first`, or close",
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
                "three exact-head terminal outcomes", sources["plan"]
            )
            # Pinned by reference rather than by spelling: this sentence used to restate
            # the artifact list itself, and restating it is the defect the canonical block
            # exists to prevent. What must survive is that step 11 demands the FULL set.
            self.assertIn(
                "post-clean settle and full re-read of the reviewed",
                sources["plan"],
            )
            # Updated 2026-07-26: the plan pinned "resettable 15-minute fallback", which
            # under manual-only review is a clock for a review that never arrives. It now
            # pins the rule that replaced it.
            self.assertIn(
                "no silence fallback for an unrequested review", sources["plan"]
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
            # Same ruling as above: the handoff runbook now states the request-comment `+1` rule.
            self.assertIn(
                "a connector-authored `+1` (`thumbs-up`) sits on the exact",
                handoff,
            )
            self.assertIn(
                "A bare `eyes` reaction is accepted-and-in-progress, never a verdict.",
                handoff,
            )
            self.assertIn(
                "connector completed an exact-head review",
                handoff,
            )
            self.assertIn("Each outcome is terminal for the bot wait", handoff)

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_compact_clean_verdict_gets_settle_and_reread(self):
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "approximately five-minute mandatory post-clean settle", handoff
            )
            self.assertIn(
                "re-read of [the reviewed artifact set](commit-push-prs.md#reviewed-artifact-set)", handoff
            )
            self.assertNotIn("same-head re-review request", handoff)
            self.assertNotIn(
                "timestamps immediately and do not wait out the remainder", handoff
            )

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_compact_reaction_signal_takes_the_same_settle_as_a_verdict(self):
        """Inverted 2026-07-26. This pinned the DEFECT.

        It required the reaction path to "report it immediately and do not wait out the
        remainder of the 15-minute fallback" -- so a Tier A PR could merge the instant a
        `+1` appeared, with no settle and no re-read. The stated justification for
        accepting a reaction-only CLEAN at Tier A is settle plus adjudication, so
        exempting the reaction from the settle removed the very evidence the rule rests
        on. The reaction path now takes the same settle as a text verdict.
        """
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "the same approximately five-minute post-clean settle and full re-read "
                "as a text verdict",
                handoff,
            )
            self.assertNotIn(
                "report it immediately and do not wait out the remainder", handoff,
                "the reaction path must not be exempt from the settle again",
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
                "repeating the focus and exact head SHA of the original request",
                handoff,
                "a SHA-less retry cannot produce a usable reaction signal",
            )
            self.assertIn("**No silence fallback exists.**", handoff)

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
                "requested-review silence re-aged into a merge path",
                "workflow",
                "never ages into a merge-eligible state",
                "ages into a merge-eligible state",
            ),
            (
                "a deleted fallback variant reintroduced",
                "workflow",
                "- **Prior-head artifacts only.**",
                "- **Explicit requested-review silence.** Broadened case.\n"
                "- **Prior-head artifacts only.**",
            ),
            (
                "post-clean settle made waivable",
                "workflow",
                # The enumeration between "re-read" and "remain" is now a reference, so
                # the mutation targets the clause that carries the obligation.
                "remain mandatory before merge",
                "may be skipped after a terminal signal",
            ),
            (
                "terminal signal source altered",
                "workflow",
                "third terminal\n  gate outcome",
                "optional disposed-review\n  outcome",
            ),
            (
                "only the canonical document updated",
                "handoff",
                "[Explicit requested-review precedence]"
                "(commit-push-prs.md#explicit-requested-review-precedence)",
                "Explicit requested-review precedence",
            ),
            (
                "elapsed time reinstated as a terminal signal",
                "workflow",
                "no elapsed time is ever a terminal signal",
                "elapsed time is a terminal signal",
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
                "issue exactly one re-trigger",
                "repeatedly issue a re-trigger",
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
                "It does not waive required local exact-head\nverification, green "
                "required checks, mergeability, a full re-read of\n"
                "[the reviewed artifact set](#reviewed-artifact-set), or unresolved-feedback\n"
                "handling",
                "It waives independent review, checks, and reread",
            ),
            (
                "compact guidance defines a divergent disposition effect",
                "handoff",
                "this compact guidance defines no\nseparate disposition effect",
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
