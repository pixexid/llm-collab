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
                    "**No silence fallback exists.**",
                    "repeating the focus and exact head SHA of the original request",
                    "no further automatic retry is allowed",
                    "The canonical section is the sole authority for the request-anchored "
                    "clocks, current-head invalidation, the post-timeout disposition "
                    "choices, and every effect of an exact-head operator authorization; "
                    "this compact guidance defines no separate disposition effect",
                    "A terminal signal stops waiting for further artifacts only; it does "
                    "not waive the handling below",
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
                'For requested-review silence, follow the canonical [Explicit requested-review '
                'precedence](commit-push-prs.md#explicit-requested-review-precedence). Automation may '
                'issue exactly one re-trigger, repeating the focus and exact head SHA of the original '
                'request, and no further automatic retry is allowed. The canonical section is the sole '
                'authority for the request-anchored clocks, current-head invalidation, the post-timeout '
                'disposition choices, and every effect of an exact-head operator authorization; this '
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
        return {
            "exact_head_findings": len([
                t for t in threads
                if not t["isResolved"] and self.originating_commit_oid(t) == head_oid
            ]),
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
        self.assertEqual(2, actual["exact_head_findings"])
        self.assertEqual(3, actual["unresolved_total"])
        self.assertEqual("old111", self.originating_commit_oid(threads[0]))
        self.assertEqual("head999", self.originating_commit_oid(threads[3]))

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

    def test_the_merge_checklist_does_not_inherit_the_origin_rule_narrowing(self):
        """The hole the origin rule opened in the checklist below it.

        Binding findings to the initiating commit answers "is this about this head".
        The final checklist asked only for no unresolved feedback "for the current
        head", so once the origin rule narrowed that phrase, a worker could satisfy the
        checklist with prior-head threads nobody had answered -- recreating the silent
        drop the same section forbids.
        """
        section = contract_section(
            WORKFLOW_DOC.read_text(encoding="utf-8"),
            "Proceed only when all of these are true:",
            "Read current review bodies and reactions directly.",
        )
        for phrase in (
            "**every** unresolved actionable thread is resolved or explicitly "
            "dispositioned in writing, whatever head it was initiated on",
            "it does not narrow this checklist",
            "A prior-head thread nobody answered is unadjudicated, not closed",
        ):
            self.assertIn(phrase, section)

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
            "- when a re-review was explicitly requested",
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
            "Request **once per candidate final head**",
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
                "It does not waive independent exact-head\nreview, green "
                "required checks, mergeability, the full\ncomment/review/thread/"
                "reaction reread, unresolved-feedback handling, or\nproject/"
                "operator auto-merge authority",
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
