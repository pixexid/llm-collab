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
        """Assert the mandatory one-pass review gate against supplied docs."""
        review_policy = contract_section(
            workflow_text,
            "### GitHub Codex review policy",
            "## Autonomous Queue Runner State",
        )
        precedence = contract_section(
            workflow_text,
            "#### First-pass precedence",
            "If the PR is waiting only for the remote review state",
        )
        handoff_wait = contract_section(
            handoff_text,
            "For PR-review wait heartbeats",
            "If GitHub Codex comments on the PR",
        )
        for phrase in (
            "One automatic bot pass is mandatory for every PR",
            "Do not merge an open PR before that pass completes",
            "do not request a second bot pass",
            "No elapsed time, tier, or release-gate disposition substitutes",
        ):
            self.assertIn(phrase, review_policy)
        for phrase in (
            "The first bot pass is pending",
            "An `eyes` reaction is pickup only",
            "one manual fallback request",
            "do not replace a missing terminal review with a timer or disposition",
        ):
            self.assertIn(phrase, precedence)
        for phrase in (
            "every PR waits for that first pass",
            "the first connector pass remains the PR's only bot review",
            "Amended heads receive local exact-head verification",
        ):
            self.assertIn(phrase, handoff_wait)

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
                "A capped head with zero open actionable findings, a completed "
                "first connector pass, and local exact-head verification follows "
                "the normal merge gate with no convergence-disposition label",
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
        self.assertIn("A second model review must not be run", text)
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
                "on the exact manual fallback request",
                sources["review_policy"],
            )
            self.assertIn("is terminal CLEAN", sources["review_policy"])
            self.assertIn(
                "a bare `eyes` reaction or the request comment itself is never a",
                sources["review_policy"],
            )
            # GH-549: a third clean model was added for the automatic pass, so the
            # count moved 2 -> 3 and the adjudicated non-clean review moved 3 -> 4.
            # The set stays CLOSED at the new count -- that exclusivity, not the
            # number itself, is what this test defends.
            self.assertIn(
                "only three connector-authored clean signal models",
                sources["review_policy"],
            )
            self.assertIn("fourth terminal gate outcome", sources["review_policy"])
            # Every condition of the automatic model is pinned individually. A later
            # edit that drops one would leave a `+1` terminal on weaker evidence than
            # the model was adjudicated on, which is the failure this guards.
            self.assertIn(
                "at PR level, with no manual request comment in existence",
                sources["review_policy"],
            )
            self.assertIn("Verify all four", sources["review_policy"])
            for condition in (
                "the actor is the connector",
                "that the reaction post-dates the push of the current head",
                "that the head has not been amended since",
                "every other artifact class is empty",
            ):
                self.assertIn(condition, sources["review_policy"])
            # The model must NOT require separate proof that the automatic pass
            # ran: on a clean pass the reaction is the only artifact the connector
            # emits, so such a condition is circular and unsatisfiable. The
            # adjudicated resolution is to define the reaction AS that artifact.
            self.assertIn(
                "That reaction is itself the automatic pass's clean artifact",
                sources["review_policy"],
            )
            self.assertNotIn(
                "that the automatic pass ran for this", sources["review_policy"]
            )
            # The model has to be reachable from every wait-gate path, not only the
            # two enumerations that define it. A precedence rule that waits for a
            # review object holds every clean automatic PR forever.
            self.assertIn(
                "pending until a clean review, a valid automatic PR-level `+1`",
                sources["review_policy"],
            )
            self.assertIn(
                "hold until the bot returns a terminal review or a valid automatic "
                "PR-level `+1`",
                sources["review_policy"],
            )
            self.assertIn(
                "valid fallback-request `+1`, valid automatic PR-level `+1`",
                sources["review_policy"],
            )
            # The empty-findings clause is the whole safeguard against the #317
            # shape (clean-looking signal beside unresolved P1 threads), so pin the
            # consequence, not just the condition.
            self.assertIn(
                "a `+1` alongside any finding artifact is **not** terminal",
                sources["review_policy"],
            )
            self.assertIn(
                "A clean automatic `+1` never justifies a second review request",
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

    def test_contract_v7_retires_the_per_head_re_request_loop(self):
        """Contract v7 makes the external review one pass per PR."""
        for doc in (AGENTS_DOC,):
            text = normalized(doc.read_text(encoding="utf-8"))
            with self.subTest(doc=doc.name):
                self.assertIn("Contract v7", text)
                self.assertIn("review **once per PR**", text)
                self.assertIn("Do **not** re-request a review on the fixed head", text)
                self.assertNotIn("one initial request per candidate final head", text)
                self.assertNotIn("single request-anchored re-trigger", text)

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
        """A non-GitHub lane uses one exact-OID request and verdict."""
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("Non-GitHub lanes use an exact-OID mailbox request and verdict", text)
        self.assertIn("with the same no-silence rule", text)

    def test_an_unrequested_tier_a_head_offers_no_waiver_alternative(self):
        """A missing automatic pass may not be waived."""
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("Tier A issues the one manual fallback request", text)
        self.assertIn("every other tier reports the review-infrastructure blocker", text)
        self.assertIn("No elapsed time, tier, or release-gate disposition substitutes", text)

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
        """The first bot pass has no withdrawal path."""
        import re

        text = re.sub(r"\s+", " ", WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn("No elapsed time, tier, or release-gate disposition substitutes", text)
        self.assertNotIn("withdraws the review requirement", text)

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

    def test_the_quickstart_uses_the_generator_and_current_silence_owner(self):
        text = QUICKSTART_DOC.read_text(encoding="utf-8")
        self.assertIn("python bin/review_request.py --pr", text)
        self.assertNotIn("@codex review for <every Tier A family", text)
        self.assertNotIn("operator disposition", text)
        self.assertIn("review-infrastructure blocker", text)

    def test_the_contract_version_and_recent_summaries_stay_current(self):
        """A cached copy of the old gate can produce a wrong merge.

        Workers on older contracts get no signal that AX ownership, the mandatory
        bot pass, shared philosophy, or v7 review rules changed, so the version
        marker and recent summaries move together.

        The v4 and v3 changelog entries used to be pinned here too, in AGENTS.md.
        They were deleted with the rest of the v1-v4 narrative (GH-365): a changelog
        entry is a second normative copy of rules that `commit-push-prs.md` already
        owns, and every worker paid for it on every lane. What matters is that those
        rules still exist somewhere canonical, not that AGENTS.md retells them, so
        the assertions below moved to the document that owns them.
        """
        text = AGENTS_DOC.read_text(encoding="utf-8")
        self.assertIn("<!-- CONTRACT_VERSION: 12 -->", text)
        self.assertNotIn("<!-- CONTRACT_VERSION: 3 -->", text)

        # GH-556: pinning the marker alone let the marker and the body disagree.
        # A v12 block was added while the marker still read 11, the suite stayed
        # green at 2483, and startup/drift reported v11 against a v12 contract.
        # Derive the expected marker from the body instead of pinning both
        # independently, so the next version bump cannot repeat it.
        newest_in_body = max(
            int(match) for match in re.findall(r"^Contract v(\d+) \(", text, re.M)
        )
        marker = re.search(r"<!-- CONTRACT_VERSION: (\d+) -->", text)
        self.assertIsNotNone(marker, "AGENTS.md must carry a CONTRACT_VERSION marker")
        self.assertEqual(
            newest_in_body,
            int(marker.group(1)),
            "CONTRACT_VERSION marker must equal the newest `Contract vN` block in "
            "the body; a mismatch makes startup and drift checks report a stale "
            "contract version",
        )

        recent_entry = contract_section(
            text, "### Recent contract changes", "## Required Reading"
        )
        for phrase in (
            "Contract v11",
            "frozen, bounded work order",
            "must never share a packet",
            "Track what you actually sent",
            "Contract v10",
            "AX a Codex/ChatGPT-app doorbell only",
            "run only the exact command printed by `deliver.py`",
            "recipient's watcher owns pickup",
            "Contract v9",
            "mandatory",
            "Every PR waits",
            "Contract v8",
            "shared philosophy",
            "Contract v7",
            "one pass, not a loop",
            "once per PR",
            "Do **not** re-request a review on the fixed head",
            "lane contract",
            "per-finding",
            "One external bot review per PR",
            "merge-with-followups",
            "merge-with-followups-or-close",
            "review_request.py",
            "two active implementation lanes",
        ):
            self.assertIn(normalized(phrase), recent_entry)
        # The summary is only safe to keep short because it names where the full
        # rules live. Without this the section becomes a lossy paraphrase.
        self.assertIn("commit-push-prs.md", recent_entry)

        workflow = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        for phrase in (
            "silence fallback",
            "One automatic bot pass is mandatory for every PR",
            "a connector review body that lists no findings is not a clean verdict",
            "Never `comments.nodes[0].commit.oid`",
            "**A push is not an adjudication**",
            "latest, unedited request artifact",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(normalized(phrase), workflow)

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
        self.assertIn("connector completed the PR's first pass", section)
        self.assertNotIn("release-gate disposition lifted", section)

    def test_the_compact_handoff_uses_one_bot_pass_and_local_fix_proof(self):
        section = contract_section(
            HANDOFF_DOC.read_text(encoding="utf-8"),
            "If GitHub Codex comments on the PR",
            "Delete the heartbeat before post-merge cleanup.",
        )
        for phrase in (
            "Do not request a second bot or model review",
            "The first pass is the PR's only bot pass",
            "local exact-head verification",
        ):
            self.assertIn(phrase, section)
        self.assertNotIn("issue a new exact-head request", section)

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
            "When the Tier A fallback is needed, request with `@codex review for <focus>`",
            '**"Untrusted" means input we do not control.**',
        )
        for phrase in (
            "naming **every** Tier A family the diff touches",
            "**stating the exact head SHA the request is for**",
            "a connector `+1` is terminal only while the head still equals the SHA "
            "that request named",
            "a request without one leaves the reaction path unsatisfiable",
            "**Request at most ONCE per PR**",
            "do **not** re-request a review on the fixed head",
        ):
            self.assertIn(phrase, section)

    def test_silently_dropped_review_does_not_become_a_pass(self):
        def check(case, sources):
            self.assertIn(
                "The first bot pass is pending",
                sources["review_policy"],
            )
            self.assertIn(
                "one manual fallback request",
                sources["review_policy"],
            )
            self.assertIn(
                "do not replace a missing terminal review with a timer or disposition",
                sources["review_policy"],
            )
            self.assertNotIn("re-trigger", sources["review_policy"])

        self.assert_scenario_cases("canonical_wait_gate", check)

    def test_release_gate_disposition_cannot_replace_first_pass(self):
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

    def test_plan_doc_uses_the_one_external_reviewer_flow(self):
        plan = PLAN_DOC.read_text(encoding="utf-8")
        self.assertNotIn("reuse that reviewer", plan)
        self.assertNotIn("reusing the same reviewer", plan)
        self.assertNotIn("reused per the bounded amendment rules", plan)

        def check(case, sources):
            self.assertIn(
                "one-external-reviewer flow",
                sources["plan"],
            )
            self.assertNotIn(
                "reusing the same reviewer for in-contract amendments",
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

    def test_phase_completion_gate_uses_the_canonical_reviewer_flow(self):
        def check(case, sources):
            self.assertIn(
                "passes the one-external-reviewer flow in "
                "`docs/workflows/commit-push-prs.md`",
                sources["phase_completion"],
            )
            self.assertNotIn(
                "reused per the bounded amendment rules",
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
            # GH-549 P2: the automatic PR-level +1 outcome takes the same settle too.
            self.assertIn(
                "An automatic PR-level connector `+1` (no manual request comment in existence) "
                "receives the same approximately five-minute post-clean settle",
                handoff,
            )
            self.assertNotIn(
                "report it immediately and do not wait out the remainder", handoff,
                "the reaction path must not be exempt from the settle again",
            )

        self.assert_scenario_cases("compact_wait_gate", check)

    def test_compact_silently_dropped_review_is_blocked(self):
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "[First-pass precedence](commit-push-prs.md#first-pass-precedence)",
                handoff,
            )
            self.assertIn(
                "No retry or elapsed-time disposition replaces the mandatory first pass",
                handoff,
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
                "missing review becomes mergeable",
                "workflow",
                "Do not merge an open PR before that pass completes",
                "Merge an open PR before that pass completes",
            ),
            (
                "eyes becomes terminal",
                "workflow",
                "An `eyes` reaction is pickup only",
                "An `eyes` reaction is terminal",
            ),
            (
                "compact guidance permits second bot pass",
                "handoff",
                "the first connector pass remains the PR's only bot review",
                "a second bot review may replace local verification",
            ),
            (
                "elapsed time reinstated as a terminal signal",
                "workflow",
                "No elapsed time, tier, or release-gate disposition substitutes",
                "Elapsed time or a disposition substitutes",
            ),
        )
        for name, target, old, new in mutations:
            with self.subTest(mutation=name):
                original = (
                    self.workflow_text if target == "workflow" else self.handoff_text
                )
                original = normalized(original)
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

    def test_amended_heads_never_route_back_to_the_bot(self):
        text = normalized(self.workflow_text)
        self.assertIn(
            "later heads receive local exact-head verification",
            text,
        )
        self.assertIn(
            "Do not run another bot or independent model review on the amended head",
            text,
        )
        self.assertIn("completed the PR's first pass clean on a prior OID", text)
        self.assertIn("amended current head has complete local exact-head verification", text)
        self.assertNotIn("external reviewer for later exact heads", text)
        self.assertNotIn("connector as the external reviewer for the new exact head", text)

    def test_prior_oid_completion_path_recognizes_the_automatic_clean_model(self):
        """GH-549: the prior-OID completion path must recognize a first pass that
        was clean via the automatic PR-level `+1`, not only text verdict or
        request-comment `+1`. A clean automatic first pass followed by an amended
        head is the one-pass case the terminal list must still recognize, because
        the rule says local verification replaces a second bot pass."""
        text = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn(
            "completed the PR's first pass clean on a prior OID (by text verdict, "
            "request-comment `+1`, or automatic PR-level `+1`)",
            text,
        )

    def test_compact_wait_gate_names_the_automatic_pr_level_clean_model(self):
        """GH-549: the worker-facing GitHub Codex gate completion enumeration in
        review-and-handoff.md must name the automatic PR-level `+1` clean model.
        Without it a worker reading only this runbook holds a clean automatic PR,
        because the enumeration lists only the text verdict, request-comment `+1`,
        and disposed review."""
        def check(case, sources):
            handoff = sources["handoff_wait"]
            self.assertIn(
                "a connector-authored `+1` sits at PR level with no manual request "
                "comment in existence",
                handoff,
            )
            self.assertIn("the automatic first pass's clean model", handoff)
        self.assert_scenario_cases("compact_wait_gate", check)

    def test_automatic_plus_one_fails_closed_on_a_mid_pass_push(self):
        """GH-549 P1: a PR-level +1 carries no SHA, so the four checks cannot bind
        it to the reviewed head. A pass started on H1 that ends with a +1 after an
        H2 push passes all four for H2 though H1 was reviewed. The contract must
        fail closed in that case (route through the prior-OID local proof) unless a
        trustworthy connector pickup artifact proves the pass started after the
        current head's push with no push in between."""
        text = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn(
            "those four checks cannot by themselves bind the `+1` to the reviewed head",
            text,
        )
        self.assertIn("passes all four for H2 though H1 was reviewed", text)
        self.assertIn(
            "route it through the prior-OID path below and locally prove every amendment",
            text,
        )

    def test_canonical_terminal_list_binds_automatic_plus_one_to_the_reviewed_head(self):
        """GH-549 P1 (canonical list): the merge-driving automatic PR-level +1 bullet
        must carry the same head-binding as the review-policy enumeration -- a PR-level
        reaction has no SHA, so the four checks do not bind it to the reviewed head.
        Require a trustworthy connector pickup artifact (eyes) proving the pass started
        after the current head's push with no push in between, and route an
        ambiguous/prior-head +1 through the prior-OID local proof."""
        text = normalized(WORKFLOW_DOC.read_text(encoding="utf-8"))
        self.assertIn(
            "terminal on this path only when a trustworthy existing connector pickup "
            "artifact (its `eyes` reaction) proves the pass started after the current "
            "head's push with no push between pickup and `+1`",
            text,
        )
        self.assertIn(
            "an ambiguous or prior-head `+1` is not terminal here — fall back to the "
            "prior-OID clause below with local proof of every amendment",
            text,
        )


class AxIsNeverPrimaryDocTest(unittest.TestCase):
    """v12 says routine exact-session dispatch is the wake for every
    watcher-backed recipient, Codex included, and AX is the fallback
    `deliver.py` selects.

    This is a regression guard, not a general detector: each phrase below is one
    that actually shipped and had to be corrected. The class survived four review
    rounds on GH-556 because a hand sweep kept finding the named file and missing
    the next one — the last miss was a bullet beginning "primary for a Codex
    recipient only", which no grep pairing "AX" with "primary" on one line could
    see."""

    FORBIDDEN = (
        "no background event pickup",
        "no background pickup",
        "no native session watcher",
        "no native session event watcher",
        "no native watcher",
        "non-Codex watcher-backed",
        "primary for a Codex recipient",
        "avoid registering the matching dispatchable autobridge",
        "ax remains the routine doorbell",
        "it is the normal transport",
    )

    def test_no_doc_makes_ax_the_primary_wake(self):
        # Generated worker instructions count: bin/new_collab_session.py prints
        # the pickup guidance every new collaboration session is onboarded with,
        # and a docs-only scan stayed green while it still taught the retired
        # model (PR #559 r3725690813).
        # Worker-facing Markdown is not only under docs/: the root README and
        # tools/axbridge/README.md both taught the AX-primary model while a
        # docs-only scan stayed green (PR #559 r3725733643). Chats/, Tasks/ and
        # State/ are excluded because packets legitimately QUOTE these phrases
        # when reporting them.
        roots = [
            REPO_ROOT / "AGENTS.md",
            REPO_ROOT / "README.md",
            REPO_ROOT / "bin" / "new_collab_session.py",
            *(REPO_ROOT / "docs").rglob("*.md"),
            *(REPO_ROOT / "tools").rglob("*.md"),
        ]
        offenders = []
        for path in roots:
            text = path.read_text(encoding="utf-8")
            # Whitespace-normalized: these phrases live in wrapped prose and
            # docstrings, so a raw substring test silently misses any instance
            # that happens to straddle a line break — which is how the pm2.md
            # bullet survived three sweeps.
            lowered = " ".join(text.lower().split())
            for phrase in self.FORBIDDEN:
                # Case-insensitive: the sentence-initial "No native session
                # watcher" is the same claim as the mid-sentence one, and a
                # case-only miss is exactly how this class kept surviving.
                if phrase.lower() in lowered:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {phrase!r}")
        self.assertEqual(
            [],
            offenders,
            "these phrasings tell a worker that Codex lacks routine pickup, or that "
            "AX is its primary wake, both of which contract v12 retired:\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
