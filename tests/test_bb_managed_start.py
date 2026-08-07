"""GH-564 Slice 1B: the bb provider side of the managed-start saga.

AC3 and V5 live here: the mapping from a bb refusal to a saga shape is the part
that decides whether a lost response becomes a second real thread.
"""

from __future__ import annotations

import sys
import unittest

from llm_collab.bb_client import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_IDENTITY_MISMATCH,
    REFUSAL_MALFORMED_RESPONSE,
    SLICE_1A_PROFILE,
    BbProfile,
    BbRefusal,
    BbThread,
)
from llm_collab.bb_managed_start import (
    BB_START_SOURCE,
    BbStartEvidence,
    bb_start_evidence_digest,
    bb_start_native,
    validate_bb_start_evidence,
)
from llm_collab.managed_start_errors import (
    ManagedStartOrphaned,
    ManagedStartResponseLost,
    SessionLifecycleError,
)

PROJECT = "proj_vny6bi5p8e"
ENDPOINT = "endpoint_bb_one"
RUNTIME = "runtime_bb_one"
THREAD = BbThread(
    thread_id="thr_9xirgjgdis",
    project_id=PROJECT,
    environment_id="env_one",
    provider_id="pi",
    status="active",
)


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.spawns = 0

    def spawn(self, *, project_id, prompt, profile):
        self.spawns += 1
        return self.outcome


def start_native(outcome, *, profile: BbProfile = SLICE_1A_PROFILE):
    client = FakeClient(outcome)
    return client, bb_start_native(
        client,
        project_id=PROJECT,
        prompt="first packet",
        profile=profile,
        endpoint_id=ENDPOINT,
        runtime_instance_id=RUNTIME,
    )


class StartNativeSagaMappingTest(unittest.TestCase):
    def test_a_successful_spawn_produces_validatable_evidence(self):
        _client, start = start_native(THREAD)
        candidate = start("start_x")
        evidence = validate_bb_start_evidence(
            candidate,
            expected_project_id=PROJECT,
            expected_endpoint_id=ENDPOINT,
            expected_runtime_instance_id=RUNTIME,
            expected_profile=SLICE_1A_PROFILE,
        )
        self.assertEqual("thr_9xirgjgdis", evidence.native_thread_id)
        self.assertEqual(BB_START_SOURCE, evidence.source)

    def test_an_ambiguous_spawn_is_not_retryable(self):
        """V5/AC3: bb may have created the thread; a retry would create a second."""
        client, start = start_native(
            BbRefusal(REFUSAL_AMBIGUOUS, "spawn succeeded but its response was unreadable")
        )
        with self.assertRaises(ManagedStartResponseLost):
            start("start_x")
        self.assertEqual(1, client.spawns, "start_native must spawn at most once")

    def test_a_refusal_carrying_a_thread_id_becomes_an_orphan_with_that_id(self):
        """AC3: a created-but-unreturnable thread is an orphan, never a clean failure."""
        client, start = start_native(
            BbRefusal(
                REFUSAL_IDENTITY_MISMATCH,
                "bb reported another project",
                native_thread_id="thr_real",
            )
        )
        with self.assertRaises(ManagedStartOrphaned) as caught:
            start("start_x")
        self.assertEqual("thr_real", caught.exception.native_session_id)
        self.assertEqual(1, client.spawns)

    def test_orphan_wins_over_ambiguous_when_an_id_exists(self):
        """An id is strictly better evidence than 'we cannot tell'.

        A refusal that is BOTH ambiguous-reasoned and id-carrying must reconcile as
        an orphan: the saga can act on an id and cannot act on an ambiguity.
        """
        _client, start = start_native(
            BbRefusal(REFUSAL_AMBIGUOUS, "lost", native_thread_id="thr_real")
        )
        with self.assertRaises(ManagedStartOrphaned):
            start("start_x")

    def test_a_refusal_with_no_thread_is_a_plain_failure(self):
        _client, start = start_native(
            BbRefusal(REFUSAL_MALFORMED_RESPONSE, "not json")
        )
        with self.assertRaises(SessionLifecycleError) as caught:
            start("start_x")
        self.assertNotIsInstance(caught.exception, ManagedStartOrphaned)
        self.assertNotIsInstance(caught.exception, ManagedStartResponseLost)


class EvidenceValidationTest(unittest.TestCase):
    def _candidate(self, **overrides):
        _client, start = start_native(THREAD)
        candidate = dict(start("start_x"))
        candidate.update(overrides)
        return candidate

    def _validate(self, candidate, *, profile: BbProfile = SLICE_1A_PROFILE):
        return validate_bb_start_evidence(
            candidate,
            expected_project_id=PROJECT,
            expected_endpoint_id=ENDPOINT,
            expected_runtime_instance_id=RUNTIME,
            expected_profile=profile,
        )

    def test_a_thread_from_another_project_is_refused(self):
        with self.assertRaises(SessionLifecycleError):
            self._validate(self._candidate(project_id="proj_other"))

    def test_a_different_endpoint_is_refused(self):
        with self.assertRaises(SessionLifecycleError):
            self._validate(self._candidate(endpoint_id="endpoint_other"))

    def test_AC6_the_frozen_triple_is_attested_not_merely_requested(self):
        """Each member of the triple is compared, so none can drift silently."""
        for field, wrong in (
            ("provider", "claude-code"),
            ("model", "kimi-coding/k3-256k"),
            ("reasoning_level", "low"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(SessionLifecycleError):
                    self._validate(self._candidate(**{field: wrong}))

    def test_the_shipped_profile_passes_unchanged(self):
        """The value 1B must carry through is the one Slice 1A actually ships."""
        evidence = self._validate(self._candidate())
        self.assertEqual("pi", evidence.provider)
        self.assertEqual("kimi-coding/k3", evidence.model)
        self.assertEqual("high", evidence.reasoning_level)

    def test_a_foreign_source_is_refused(self):
        with self.assertRaises(SessionLifecycleError):
            self._validate(self._candidate(source="managed_thread_start"))


class DigestTest(unittest.TestCase):
    def _evidence(self) -> BbStartEvidence:
        return BbStartEvidence(
            native_thread_id="thr_9xirgjgdis",
            project_id=PROJECT,
            environment_id="env_one",
            provider_id="pi",
            status="active",
            endpoint_id=ENDPOINT,
            runtime_instance_id=RUNTIME,
            provider="pi",
            model="kimi-coding/k3",
            reasoning_level="high",
            source=BB_START_SOURCE,
        )

    def test_the_digest_is_stable_for_a_fixed_value(self):
        self.assertEqual(
            bb_start_evidence_digest(self._evidence()),
            bb_start_evidence_digest(self._evidence()),
        )
        self.assertEqual(64, len(bb_start_evidence_digest(self._evidence())))

    def test_the_digest_refuses_a_foreign_evidence_type(self):
        """A Codex value must not silently hash through the bb digest."""
        with self.assertRaises(SessionLifecycleError):
            bb_start_evidence_digest({"native_thread_id": "thr_x"})

    def test_a_changed_field_changes_the_digest(self):
        other = BbStartEvidence(**{**self._evidence().__dict__, "reasoning_level": "low"})
        self.assertNotEqual(
            bb_start_evidence_digest(self._evidence()), bb_start_evidence_digest(other)
        )



class DefaultOffEndToEndTest(unittest.TestCase):
    """V7/AC8: disabled means no process is spawned, proven against a real transport.

    The Slice 1A default-off test asserts an empty call log on a fake transport.
    That proves the client made no call; it does not prove no bb process would
    have been created, because a fake cannot create one. Here the transport is
    the production `subprocess_transport`, pointed at a command that writes a
    sentinel file. If disablement ever stops short of the process boundary, the
    sentinel appears and this fails.
    """

    def test_a_disabled_client_creates_no_process(self):
        import tempfile
        from pathlib import Path

        from llm_collab.bb_client import BbClient, subprocess_transport

        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "spawned"
            transport = subprocess_transport(
                [
                    sys.executable,
                    "-c",
                    f"open({str(sentinel)!r}, 'w').write('x')",
                ]
            )
            client = BbClient(transport)  # enabled defaults to False

            outcomes = [
                client.verify_version(),
                client.spawn(project_id=PROJECT, prompt="p", profile=SLICE_1A_PROFILE),
                client.thread_state("thr_x"),
                client.send(thread_id="thr_x", message="m"),
            ]

            for outcome in outcomes:
                self.assertIsInstance(outcome, BbRefusal)
            self.assertFalse(
                sentinel.exists(),
                "a disabled client reached the process boundary and spawned bb",
            )

    def test_an_enabled_client_does_reach_the_process(self):
        """The control. Without it, the test above passes on a broken transport."""
        import tempfile
        from pathlib import Path

        from llm_collab.bb_client import BbClient, subprocess_transport

        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "spawned"
            transport = subprocess_transport(
                [
                    sys.executable,
                    "-c",
                    f"open({str(sentinel)!r}, 'w').write('x'); print('{{}}')",
                ]
            )
            client = BbClient(transport, enabled=True)
            client.verify_version()
            self.assertTrue(
                sentinel.exists(),
                "the enabled control never reached the process, so the proof above is vacuous",
            )

if __name__ == "__main__":
    unittest.main()
