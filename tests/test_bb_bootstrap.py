"""GH-564 Slice 1B AC0/AC8: the bb bootstrap entry condition.

Every test drives the pure decision function. There is no watcher, no bb process
and no ledger here on purpose: AC8 is a claim about calls NOT made, and the only
way to prove a decision never reaches a native call is to make the decision
itself observable.
"""

from __future__ import annotations

import unittest

from llm_collab.bb_bootstrap import (
    BOOTSTRAP_DISABLED,
    BOOTSTRAP_NOT_ENABLED_FOR_PROJECT,
    BOOTSTRAP_NO_PACKET,
    BOOTSTRAP_SESSION_EXISTS,
    BOOTSTRAP_TERMINAL_BINDING,
    BOOTSTRAP_REPO_TARGET_AMBIGUOUS,
    BOOTSTRAP_REPO_TARGET_REQUIRED,
    BOOTSTRAP_PROFILE_UNAVAILABLE,
    BOOTSTRAP_MALFORMED_ACTIVATION,
    BootstrapPlan,
    BootstrapRefusal,
    plan_bootstrap,
    project_enables_bb,
    resolve_bootstrap_repo_id,
    execute_bootstrap,
    classify_first_delivery_assignment,
    ASSIGNMENT_READ_ONLY,
    ASSIGNMENT_AUTHORING,
    ASSIGNMENT_MALFORMED_ACTIVATION,
    BOOTSTRAP_STARTED,
    BOOTSTRAP_DUPLICATE,
    BOOTSTRAP_AMBIGUOUS,
    BOOTSTRAP_ORPHANED,
    BOOTSTRAP_FAILED,
)
from llm_collab.managed_start_errors import (
    ManagedStartOrphaned,
    ManagedStartResponseLost,
)

PROJECT = {"id": "llm-collab", "bb": {"enabled": True}}
PACKET = {
    "canonical_message_id": "cmid_0001",
    "path": "Chats/x/2026-08-06T00-00-00_to-worker_first.md",
}


def plan(**overrides):
    kwargs = dict(
        enabled=True,
        project=PROJECT,
        project_id="llm-collab",
        conversation_id="CHAT-ONE",
        participant_id="worker",
        agent_id="worker",
        existing_session_ids=[],
        binding_state=None,
        first_packet=PACKET,
    )
    kwargs.update(overrides)
    return plan_bootstrap(**kwargs)


class DefaultOffTest(unittest.TestCase):
    def test_disabled_adapter_refuses_before_anything_else(self):
        """AC8: disabled must not depend on project config being readable."""
        outcome = plan(enabled=False, project=None)
        self.assertIsInstance(outcome, BootstrapRefusal)
        self.assertEqual(BOOTSTRAP_DISABLED, outcome.reason)

    def test_disabled_refuses_even_when_everything_else_would_permit(self):
        outcome = plan(enabled=False)
        self.assertEqual(BOOTSTRAP_DISABLED, outcome.reason)


class ProjectEnablementTest(unittest.TestCase):
    def test_project_without_bb_block_is_disabled(self):
        outcome = plan(project={"id": "llm-collab"})
        self.assertEqual(BOOTSTRAP_NOT_ENABLED_FOR_PROJECT, outcome.reason)

    def test_missing_project_is_disabled(self):
        outcome = plan(project=None)
        self.assertEqual(BOOTSTRAP_NOT_ENABLED_FOR_PROJECT, outcome.reason)

    def test_truthy_non_boolean_does_not_enable(self):
        """Default-off means ambiguous is off, not 'probably meant on'."""
        for value in ("true", "yes", 1, [1]):
            with self.subTest(value=value):
                outcome = plan(project={"id": "p", "bb": {"enabled": value}})
                self.assertEqual(BOOTSTRAP_NOT_ENABLED_FOR_PROJECT, outcome.reason)

    def test_explicit_false_is_disabled(self):
        outcome = plan(project={"id": "p", "bb": {"enabled": False}})
        self.assertEqual(BOOTSTRAP_NOT_ENABLED_FOR_PROJECT, outcome.reason)

    def test_project_enables_bb_requires_exact_true(self):
        self.assertTrue(project_enables_bb({"bb": {"enabled": True}}))
        self.assertFalse(project_enables_bb({"bb": {"enabled": "true"}}))
        self.assertFalse(project_enables_bb({"bb": "enabled"}))
        self.assertFalse(project_enables_bb({}))
        self.assertFalse(project_enables_bb(None))


class RepoResolutionTest(unittest.TestCase):
    def test_explicit_project_repo_wins_over_packet(self):
        self.assertEqual(
            "app",
            resolve_bootstrap_repo_id(
                {"bb": {"repo_id": "app"}}, ["docs"]
            ),
        )

    def test_single_packet_repo_is_used_without_project_override(self):
        self.assertEqual("docs", resolve_bootstrap_repo_id({"bb": {}}, ["docs"]))

    def test_missing_or_multiple_packet_repos_refuse(self):
        self.assertEqual(
            BOOTSTRAP_REPO_TARGET_REQUIRED,
            resolve_bootstrap_repo_id({"bb": {}}, None).reason,
        )
        self.assertEqual(
            BOOTSTRAP_REPO_TARGET_AMBIGUOUS,
            resolve_bootstrap_repo_id({"bb": {}}, ["app", "docs"]).reason,
        )


class ExactAbsenceTest(unittest.TestCase):
    def test_an_existing_session_is_not_a_first_delivery(self):
        outcome = plan(existing_session_ids=["SESSION-ONE"])
        self.assertEqual(BOOTSTRAP_SESSION_EXISTS, outcome.reason)

    def test_absence_with_everything_else_satisfied_permits_bootstrap(self):
        outcome = plan()
        self.assertIsInstance(outcome, BootstrapPlan)
        self.assertEqual("cmid_0001", outcome.canonical_message_id)
        self.assertEqual("llm-collab", outcome.project_id)


class TerminalBindingTest(unittest.TestCase):
    def test_each_terminal_state_refuses_rather_than_bootstrapping(self):
        """AC0: a contested or unreadable binding is a repair, not a first delivery."""
        for state in ("unreadable", "mismatch", "ambiguous", "scope_refused"):
            with self.subTest(state=state):
                outcome = plan(binding_state=state)
                self.assertIsInstance(outcome, BootstrapRefusal)
                self.assertEqual(BOOTSTRAP_TERMINAL_BINDING, outcome.reason)

    def test_terminal_beats_absence(self):
        """A terminal binding with no session must NOT read as a clean first delivery.

        This is the ordering that matters: checking absence first would let a
        scope-refused participant with no session row bootstrap a second owner.
        """
        outcome = plan(binding_state="scope_refused", existing_session_ids=[])
        self.assertEqual(BOOTSTRAP_TERMINAL_BINDING, outcome.reason)

    def test_a_non_terminal_state_does_not_block(self):
        outcome = plan(binding_state="active")
        self.assertIsInstance(outcome, BootstrapPlan)


class FirstPacketTest(unittest.TestCase):
    def test_no_packet_refuses(self):
        outcome = plan(first_packet=None)
        self.assertEqual(BOOTSTRAP_NO_PACKET, outcome.reason)

    def test_a_packet_without_canonical_message_id_refuses(self):
        """AC4 dedups on this id, so bootstrapping without one cannot be safe."""
        outcome = plan(first_packet={"path": "Chats/x/p.md"})
        self.assertEqual(BOOTSTRAP_NO_PACKET, outcome.reason)

    def test_a_packet_without_a_path_refuses(self):
        outcome = plan(first_packet={"canonical_message_id": "cmid_1"})
        self.assertEqual(BOOTSTRAP_NO_PACKET, outcome.reason)

    def test_the_plan_carries_the_dedup_identity_forward(self):
        outcome = plan()
        self.assertEqual("cmid_0001", outcome.canonical_message_id)
        self.assertEqual(PACKET["path"], outcome.packet_path)



class ExecuteBootstrapTest(unittest.TestCase):
    """AC2/AC3/AC4 + V3/V5: dedup before start, and the three saga shapes."""

    def setUp(self) -> None:
        self.started: list[str] = []
        self.seen: set[str] = set()

    def _start_ok(self, plan):
        self.started.append(plan.canonical_message_id)
        return "thr_new"

    def _run(self, start=None, seen=None):
        return execute_bootstrap(
            plan(),
            already_started=lambda cmid: cmid in (self.seen if seen is None else seen),
            start=start or self._start_ok,
            on_ambiguous=ManagedStartResponseLost,
            on_orphaned=ManagedStartOrphaned,
        )

    def test_one_first_delivery_starts_exactly_one_thread(self):
        outcome = self._run()
        self.assertEqual(BOOTSTRAP_STARTED, outcome.state)
        self.assertEqual("thr_new", outcome.native_thread_id)
        self.assertEqual(1, len(self.started))

    def test_a_duplicate_first_packet_never_reaches_the_start(self):
        """V3/AC4: bb has no idempotency, so the guard must be BEFORE the spawn."""
        outcome = self._run(seen={"cmid_0001"})
        self.assertEqual(BOOTSTRAP_DUPLICATE, outcome.state)
        self.assertEqual(
            [], self.started, "a deduped delivery must not reach the start at all"
        )

    def test_an_ambiguous_start_is_recorded_and_not_retried(self):
        """V5: a lost response after thread creation must not produce a second."""
        calls: list[int] = []

        def start(_plan):
            calls.append(1)
            raise ManagedStartResponseLost("response lost after creation")

        outcome = self._run(start=start)
        self.assertEqual(BOOTSTRAP_AMBIGUOUS, outcome.state)
        self.assertEqual(1, len(calls), "ambiguous must never be retried")

    def test_an_orphan_carries_its_native_id_forward(self):
        def start(_plan):
            raise ManagedStartOrphaned("bound failed", native_session_id="thr_orphan")

        outcome = self._run(start=start)
        self.assertEqual(BOOTSTRAP_ORPHANED, outcome.state)
        self.assertEqual("thr_orphan", outcome.native_thread_id)

    def test_a_plain_failure_is_not_dressed_as_a_saga_state(self):
        def start(_plan):
            raise RuntimeError("connection refused")

        outcome = self._run(start=start)
        self.assertEqual(BOOTSTRAP_FAILED, outcome.state)
        self.assertIsNone(outcome.native_thread_id)

    def test_orphan_is_classified_before_ambiguous(self):
        """An id is actionable; an ambiguity is not. Precedence must not invert."""

        class BothShapes(ManagedStartOrphaned, ManagedStartResponseLost):
            pass

        def start(_plan):
            raise BothShapes("both", native_session_id="thr_both")

        outcome = self._run(start=start)
        self.assertEqual(BOOTSTRAP_ORPHANED, outcome.state)
        self.assertEqual("thr_both", outcome.native_thread_id)


class FirstDeliveryAssignmentTest(unittest.TestCase):
    """GH-596: the bb bootstrap must fail closed on ANY activation marker before
    the read-only launch, because a partial/malformed activation is never an
    ordinary message (docs/schema-reference.md). The work type is read from the
    markers the packet carries, never from its body."""

    def test_a_lane_less_analysis_packet_is_read_only(self):
        self.assertEqual(
            ASSIGNMENT_READ_ONLY,
            classify_first_delivery_assignment(
                {"canonical_message_id": "cmid_1", "body": "audit the source"}
            ),
        )

    def test_the_body_text_is_not_the_signal(self):
        # A guard bound to how the caller phrased the prompt is the wrong proxy:
        # an analysis packet whose body happens to say "implement" is still
        # read-only because it carries no activation marker.
        self.assertEqual(
            ASSIGNMENT_READ_ONLY,
            classify_first_delivery_assignment({"body": "implement feature X now"}),
        )

    def test_a_complete_well_formed_activation_is_authoring(self):
        self.assertEqual(
            ASSIGNMENT_AUTHORING,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "/repo/wt", "branch": "bb/feat-x"}
            ),
        )

    def test_a_non_mapping_is_read_only(self):
        self.assertEqual(ASSIGNMENT_READ_ONLY, classify_first_delivery_assignment(None))
        self.assertEqual(
            ASSIGNMENT_READ_ONLY, classify_first_delivery_assignment("not a packet")
        )

    # --- every malformed shape derived from docs/schema-reference.md ----------

    def test_activation_only_is_malformed(self):
        # activation: true with no worktree/branch.
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment({"activation": True}),
        )

    def test_blank_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "   ", "branch": "x"}
            ),
        )

    def test_non_string_branch_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "/repo/wt", "branch": 123}
            ),
        )

    def test_non_string_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": ["/repo/wt"], "branch": "x"}
            ),
        )

    def test_worktree_without_branch_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment({"worktree": "/repo/wt"}),
        )

    def test_branch_without_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment({"branch": "bb/feat-x"}),
        )

    def test_lane_fields_without_activation_true_is_malformed(self):
        # "any activation marker without activation: true present".
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"worktree": "/repo/wt", "branch": "bb/feat-x"}
            ),
        )

    def test_coerced_activation_marker_is_malformed(self):
        # activation must be exactly boolean true, not "true" / 1.
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": "true", "worktree": "/repo/wt", "branch": "x"}
            ),
        )
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": 1, "worktree": "/repo/wt", "branch": "x"}
            ),
        )

    def test_relative_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "relative/path", "branch": "x"}
            ),
        )

    def test_home_relative_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "~/repo/wt", "branch": "x"}
            ),
        )

    def test_dotdot_segment_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "/repo/../wt", "branch": "x"}
            ),
        )

    def test_duplicate_separator_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "/repo//wt", "branch": "x"}
            ),
        )

    def test_trailing_separator_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "/repo/wt/", "branch": "x"}
            ),
        )

    def test_double_leading_slash_worktree_is_malformed(self):
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": "//repo/wt", "branch": "x"}
            ),
        )

    def test_explicit_null_worktree_is_malformed(self):
        # Key present with null is still a marker (presence), and null is not a
        # canonical worktree.
        self.assertEqual(
            ASSIGNMENT_MALFORMED_ACTIVATION,
            classify_first_delivery_assignment(
                {"activation": True, "worktree": None, "branch": "x"}
            ),
        )

    def test_the_two_refusal_reasons_are_distinct(self):
        self.assertEqual("bb_bootstrap_profile_unavailable", BOOTSTRAP_PROFILE_UNAVAILABLE)
        self.assertEqual(
            "bb_bootstrap_malformed_activation", BOOTSTRAP_MALFORMED_ACTIVATION
        )
        self.assertNotEqual(
            BOOTSTRAP_PROFILE_UNAVAILABLE, BOOTSTRAP_MALFORMED_ACTIVATION
        )


if __name__ == "__main__":
    unittest.main()
