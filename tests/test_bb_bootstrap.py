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
    BootstrapPlan,
    BootstrapRefusal,
    plan_bootstrap,
    project_enables_bb,
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


if __name__ == "__main__":
    unittest.main()
