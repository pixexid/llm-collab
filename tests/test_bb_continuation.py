from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unittest
from contextlib import closing
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.bb_client import BbEvent, BbEventPage, BbQueued, BbRefusal, REFUSAL_TIMED_OUT
from llm_collab.canonical.codex_delivery import _state_evidence
from llm_collab.canonical.control import append_dead_letter_receipt
from llm_collab.session_lifecycle import BbLifecycleProvider
from llm_collab.ledger import store as store_module
from llm_collab.ledger.store import MigrationError
from llm_collab.bb_continuation import (
    BB_CONTINUATION_AMBIGUOUS,
    BB_CONTINUATION_COMPLETED,
    BB_CONTINUATION_DUPLICATE,
    BB_CONTINUATION_QUEUED,
    BB_CONTINUATION_REFUSED,
    BbContinuationRefused,
    _context,
    _receipt_dispatch_seq,
    continue_bb_thread,
    observe_bb_thread,
)
from llm_collab.canonical.delivery import create_bound_attempt, create_deliveries
from llm_collab.canonical.messages import create_or_return_equivalent
from llm_collab.ledger import LedgerPaths, LedgerStore


WORKSPACE = "ws_alpha"
PROJECT = "llm-collab"
CHAT = "CHAT-BB-566"
AGENT = "glmpi"
PARTICIPANT = "participant_glmpi"
BINDING = "binding_bb_test"
ENDPOINT = "endpoint_bb_test"
SESSION_REF = "session_bb_test"
NATIVE_THREAD = "bb_thread_test"
RUNTIME_INSTANCE = "runtime_bb_test"
REVISION = "sha256:" + "a" * 64
NOW = "2026-08-07T06:00:00+00:00"
PACKET_BODY = "packet body"
PACKET = (
    b"---\n"
    b"project_id: llm-collab\n"
    b"chat_id: CHAT-BB-566\n"
    b"from: codex\n"
    b"to: glmpi\n"
    b"title: bb continuation\n"
    b"priority: normal\n"
    b"sent_utc: 2026-08-07T06:00:00+00:00\n"
    b"repo_targets: [app]\n"
    b"---\n"
    b"packet body\n"
)


class FakeBbClient:
    def __init__(self, pages: dict[int, BbEventPage] | None = None, on_send=None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.event_calls: list[tuple[str, int]] = []
        self.pages = pages or {}
        self.on_send = on_send

    def send(self, *, thread_id: str, message: str, mode: str = "queue-if-active"):
        if self.on_send is not None:
            self.on_send()
        self.sent.append((thread_id, message, mode))
        return BbQueued(thread_id=thread_id, mode="queue")

    def events_after(self, thread_id: str, after_seq: int):
        self.event_calls.append((thread_id, after_seq))
        return self.pages.get(
            after_seq,
            BbEventPage(events=(), truncated=False, next_after_seq=None),
        )


class FailingBbClient(FakeBbClient):
    def send(self, *, thread_id: str, message: str, mode: str = "queue-if-active"):
        self.sent.append((thread_id, message, mode))
        return BbRefusal("bb_transport_failed", "native rejected the queued message")


class RefuseThenAcceptClient(FakeBbClient):
    """Refuse the first send, accept every send after it.

    Every native send across every poll is recorded in ``sent`` so a
    no-double-send assertion can read the total directly (GH-700 item 5)."""

    def __init__(self) -> None:
        super().__init__()
        self._refused = False

    def send(self, *, thread_id: str, message: str, mode: str = "queue-if-active"):
        self.sent.append((thread_id, message, mode))
        if not self._refused:
            self._refused = True
            return BbRefusal("bb_transport_failed", "native rejected the queued message")
        return BbQueued(thread_id=thread_id, mode="queue")


def seed_binding(store: LedgerStore) -> None:
    store._connection.execute(
        """
        INSERT INTO lifecycle_provider_registry
        (workspace_id, provider_id, provider_revision, trust_class,
         supported_operations_json, challenge_algorithm, challenge_ttl_seconds, created_at_utc)
        VALUES (?, 'provider_bb', 'revision_1', 'managed', '["start"]', 'sha256', 60, ?)
        """,
        (WORKSPACE, NOW),
    )
    store._connection.execute(
        """
        INSERT INTO conversation_participants
        (workspace_id, scope_kind, scope_identity, conversation_id, participant_id,
         agent_id, created_at_utc)
        VALUES (?, 'project', ?, ?, ?, 'agent_glmpi', ?)
        """,
        (WORKSPACE, PROJECT, CHAT, PARTICIPANT, NOW),
    )
    store._connection.execute(
        """
        INSERT INTO conversation_bindings
        (workspace_id, scope_kind, scope_identity, conversation_id, participant_id,
         binding_id, generation, state, mutation_capable, provider_id, provider_revision,
         endpoint_id, session_ref_id, native_session_id, runtime_instance_id, registered_at_utc)
        VALUES (?, 'project', ?, ?, ?, ?, 1, 'active', 1, 'provider_bb', 'revision_1', ?, ?, ?, ?, ?)
        """,
        (
            WORKSPACE,
            PROJECT,
            CHAT,
            PARTICIPANT,
            BINDING,
            ENDPOINT,
            SESSION_REF,
            NATIVE_THREAD,
            RUNTIME_INSTANCE,
            NOW,
        ),
    )
    store.record_registry_snapshot(
        workspace_id=WORKSPACE,
        registry_revision=REVISION,
        registry_source_sha256="a" * 64,
        captured_at_utc=NOW,
        workspace_snapshot_json=json.dumps(
            {"workspace_id": WORKSPACE, "projects": [PROJECT]}
        ),
        project_snapshots={
            PROJECT: json.dumps({"project_id": PROJECT, "canonical_writes": True})
        },
        source_snapshots={PROJECT: {}},
    )


def make_session() -> dict[str, object]:
    return {
        "session_id": "bb-session-wrapper",
        "agent_id": AGENT,
        "project_id": PROJECT,
        "chat_id": CHAT,
        "status": "active",
        "binding_id": BINDING,
        "binding_generation": 1,
        "endpoint_id": ENDPOINT,
        "runtime": {"family": "bb", "session_id": NATIVE_THREAD},
    }


def make_delivery(store: LedgerStore) -> dict[str, object]:
    message_id, _ = create_or_return_equivalent(
        store,
        workspace_id=WORKSPACE,
        scope_kind="project",
        scope_identity=PROJECT,
        sender_agent_id="agent_codex",
        dedupe_key="bb-test-message",
        body=PACKET,
        recipients=("agent_glmpi",),
        registry_revision=REVISION,
        created_at_utc=NOW,
        title="bb continuation",
        ttl_seconds=0,
        ack_policy="none",
        priority="normal",
        tags=(),
        chat_link=CHAT,
        task_link=None,
        artifacts=(("chat", CHAT),),
    )
    ((delivery_id, _),) = create_deliveries(
        store,
        workspace_id=WORKSPACE,
        scope_kind="project",
        scope_identity=PROJECT,
        message_id=message_id,
        routes=(("agent_glmpi", ENDPOINT),),
        now_epoch_ms=1786082400000,
        created_at_utc=NOW,
    )
    attempt = create_bound_attempt(
        store,
        workspace_id=WORKSPACE,
        scope_kind="project",
        scope_identity=PROJECT,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_index=0,
        attempt_epoch_ms=1786082400000,
        created_at_utc=NOW,
        conversation_id=CHAT,
        participant_id=PARTICIPANT,
        expected_binding_id=BINDING,
        expected_generation=1,
    )
    return {
        "message_id": message_id,
        "delivery_id": delivery_id,
        "attempt_id": attempt["attempt_id"],
        "registry_revision": REVISION,
        "materialized": True,
    }


def _legacy_receipt_evidence(
    *,
    context: Mapping[str, object],
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    state: str = "rejected_before_acceptance",
) -> dict[str, object]:
    """Build a v14-era receipt evidence body that has NO ``x_note_dispatch_seq``.

    This is the exact shape a receipt predating the GH-700 column has: its
    extensions carry the bb refusal notes but not the per-send seq, so
    ``_receipt_dispatch_seq`` reads ``None`` for it -- which is never equal to
    the marker's seq, so the in-flight guard fails closed and strands rather
    than authorizing a retry of a send that may have landed.
    """
    correlation = "bb_" + hashlib.sha256(f"{attempt_id}|{state}".encode()).hexdigest()[:32]
    return _state_evidence(
        workspace_id=str(context["workspace_id"]),
        project_id=str(context["project_id"]),
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        endpoint_id=str(context["endpoint_id"]),
        session_ref_id=str(context["session_ref_id"]),
        native_session_id=str(context["native_thread_id"]),
        state=state,
        quality="authoritative",
        authority=BbLifecycleProvider().authority(),
        correlation_id=correlation,
        observed_at_utc=NOW,
        native_detail={"x_note_bb_refusal": "bb_transport_failed", "x_note_detail": "legacy"},
    )


def _receipt_evidence_with_seq(
    *,
    context: Mapping[str, object],
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    dispatch_seq: int,
    state: str = "rejected_before_acceptance",
) -> dict[str, object]:
    """Build a rejected_before_acceptance receipt stamped with a given dispatch_seq.

    Used to construct a delivery carrying several rejections from distinct sends
    so the P1 guard test can place the marker on whichever send the fold did NOT
    select (the lexicographic tie-break path the folded-selection guard got
    wrong).
    """
    correlation = "bb_" + hashlib.sha256(
        f"{attempt_id}|{state}|{dispatch_seq}".encode()
    ).hexdigest()[:32]
    return _state_evidence(
        workspace_id=str(context["workspace_id"]),
        project_id=str(context["project_id"]),
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        endpoint_id=str(context["endpoint_id"]),
        session_ref_id=str(context["session_ref_id"]),
        native_session_id=str(context["native_thread_id"]),
        state=state,
        quality="authoritative",
        authority=BbLifecycleProvider().authority(),
        correlation_id=correlation,
        observed_at_utc=NOW,
        native_detail={
            "x_note_bb_refusal": "bb_transport_failed",
            "x_note_detail": f"seq-{dispatch_seq}",
            "x_note_dispatch_seq": dispatch_seq,
        },
    )


def _append_legacy_receipt(
    store: LedgerStore,
    *,
    session: Mapping[str, object],
    materialized: Mapping[str, object],
) -> str:
    """Append a v14-era receipt (no dispatch_seq) through the real gate."""
    from llm_collab.bb_continuation import _context

    context = _context(store, session)
    message_id, delivery_id, attempt_id = (
        str(materialized["message_id"]),
        str(materialized["delivery_id"]),
        str(materialized["attempt_id"]),
    )
    evidence = _legacy_receipt_evidence(
        context=context,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
    )
    receipt_id, _ = append_dead_letter_receipt(
        store,
        workspace_id=str(context["workspace_id"]),
        scope_kind="project",
        scope_identity=str(context["project_id"]),
        registry_revision=str(materialized.get("registry_revision") or ""),
        allow_canonical_write=True,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        evidence=evidence,
        session_ref_id=str(context["session_ref_id"]),
        created_at_utc=NOW,
    )
    return receipt_id


def _run_stale_rejection_three_poll_scenario(
    store: LedgerStore,
    *,
    session: Mapping[str, object],
    materialized: Mapping[str, object],
) -> tuple[RefuseThenAcceptClient, object]:
    """GH-700 item 5: the three-poll stale-rejection double-send, in one place.

    Poll 1 refuses (rejection receipt stamped with this send's dispatch_seq 1).
    Poll 2 is accepted but its accepted-receipt write CRASHES after bb accepted
    the send, leaving the marker at dispatch_seq 2 with no accepted receipt.
    Poll 3 must NOT send: the only receipt is poll 1's rejection (dispatch_seq
    1), which does not speak for the current send (marker dispatch_seq 2), so the
    guard strands instead of authorizing a third send of a task that may have
    landed. Returns the shared client and the poll-3 result.
    """
    client = RefuseThenAcceptClient()
    first = continue_bb_thread(
        store, client=client, session=session,
        materialized=materialized, observed_at_utc=NOW,
    )
    assert first.state == BB_CONTINUATION_REFUSED, first.state
    # Crash only poll 2's accepted-receipt write; poll 1's refusal receipt is
    # already durable, and poll 2 attempts no other receipt.
    with patch(
        "llm_collab.bb_continuation._append_receipt",
        side_effect=RuntimeError("simulated accepted-receipt crash"),
    ):
        second = continue_bb_thread(
            store, client=client, session=session,
            materialized=materialized, observed_at_utc=NOW,
        )
    assert second.state == BB_CONTINUATION_AMBIGUOUS, second.state
    third = continue_bb_thread(
        store, client=client, session=session,
        materialized=materialized, observed_at_utc=NOW,
    )
    return client, third


def requested(seq: int, *, body: str = PACKET_BODY) -> BbEvent:
    return BbEvent(
        seq,
        f"event-request-{seq}",
        "client/turn/requested",
        {
            "requestId": f"request-{seq}",
            "source": "tell",
            "input": [{"type": "text", "text": body, "mentions": []}],
        },
    )


class BbContinuationTest(unittest.TestCase):
    def open_fixture(self):
        tmp = TemporaryDirectory(dir="/tmp")
        paths = LedgerPaths.derive(tmp.name, WORKSPACE)
        store = LedgerStore.open_writer(paths)
        seed_binding(store)
        return tmp, store, make_session(), make_delivery(store)

    def test_exact_binding_and_canonical_dedup_prevent_second_native_send(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FakeBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                first = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                second = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(BB_CONTINUATION_QUEUED, first.state)
            self.assertEqual(BB_CONTINUATION_DUPLICATE, second.state)
            self.assertEqual(first.receipt_id, second.receipt_id)
            self.assertEqual([(NATIVE_THREAD, PACKET_BODY, "queue-if-active")], client.sent)
            row = store.read_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_generation=1,
            )
            self.assertEqual("queued", row["dispatch_state"])
        finally:
            store.close()
            tmp.cleanup()

    def test_receipt_gap_is_ambiguous_and_never_retried(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FakeBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}), patch(
                "llm_collab.bb_continuation._append_receipt",
                side_effect=RuntimeError("simulated receipt crash"),
            ):
                first = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            second = continue_bb_thread(
                store,
                client=client,
                session=session,
                materialized=materialized,
                observed_at_utc=NOW,
            )
            self.assertEqual(BB_CONTINUATION_AMBIGUOUS, first.state)
            self.assertEqual(BB_CONTINUATION_AMBIGUOUS, second.state)
            self.assertEqual(1, len(client.sent))
            row = store.read_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_generation=1,
            )
            self.assertEqual("ambiguous", row["dispatch_state"])
            self.assertEqual(materialized["message_id"], row["last_message_id"])
            self.assertEqual(materialized["delivery_id"], row["last_delivery_id"])
            self.assertEqual(materialized["attempt_id"], row["last_attempt_id"])
        finally:
            store.close()
            tmp.cleanup()

    def test_replay_advances_monotonic_cursor_and_records_completion(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FakeBbClient(
                {
                    0: BbEventPage(
                        events=(
                            BbEvent(1, "event-start", "turn/started"),
                            BbEvent(2, "event-progress", "turn/progress"),
                        ),
                        truncated=False,
                        next_after_seq=None,
                    )
                }
            )
            first = observe_bb_thread(
                store,
                client=client,
                session=session,
                observed_at_utc=NOW,
                registry_revision=REVISION,
            )
            self.assertEqual(2, first.last_event_seq)
            self.assertEqual([(NATIVE_THREAD, 0)], client.event_calls)
            with self.assertRaisesRegex(ValueError, "cannot move backwards"):
                store.advance_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=1,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    event_seq=1,
                    dispatch_state="idle",
                    updated_at_utc=NOW,
                )
            self.assertEqual(
                2,
                store.read_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_generation=1,
                )["last_event_seq"],
            )

            terminal_client = FakeBbClient(
                {
                    2: BbEventPage(
                        events=(
                            requested(3),
                            BbEvent(4, "event-started", "turn/started", turn_id="turn-own"),
                            BbEvent(
                                5,
                                "event-completed",
                                "turn/completed",
                                turn_id="turn-own",
                            ),
                        ),
                        truncated=False,
                        next_after_seq=None,
                    )
                }
            )
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                # Seed the accepted receipt so the terminal event can be folded
                # into a completed receipt without a second native send.
                continue_bb_thread(
                    store,
                    client=FakeBbClient(),
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                completed = observe_bb_thread(
                    store,
                    client=terminal_client,
                    session=session,
                    observed_at_utc=NOW,
                    registry_revision=REVISION,
                )
            self.assertEqual(BB_CONTINUATION_COMPLETED, completed.state)
            self.assertEqual(5, completed.last_event_seq)
            delivery = store.read_canonical_delivery(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                message_id=str(materialized["message_id"]),
                delivery_id=str(materialized["delivery_id"]),
            )
            self.assertEqual("completed", delivery["outcome"])
            self.assertEqual([(NATIVE_THREAD, 2)], terminal_client.event_calls)
        finally:
            store.close()
            tmp.cleanup()

    def test_terminal_failure_records_ambiguous_receipt_and_advances_cursor(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                continue_bb_thread(
                    store,
                    client=FakeBbClient(),
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                result = observe_bb_thread(
                    store,
                    client=FakeBbClient(
                        {
                            0: BbEventPage(
                                events=(
                                    requested(1),
                                    BbEvent(
                                        2,
                                        "event-started",
                                        "turn/started",
                                        turn_id="turn-own",
                                    ),
                                    BbEvent(
                                        3,
                                        "event-failed",
                                        "turn/failed",
                                        turn_id="turn-own",
                                    ),
                                ),
                                truncated=False,
                                next_after_seq=None,
                            )
                        }
                    ),
                    session=session,
                    observed_at_utc=NOW,
                    registry_revision=REVISION,
                )
            self.assertEqual("failed", result.state)
            self.assertEqual(3, result.last_event_seq)
            delivery = store.read_canonical_delivery(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                message_id=str(materialized["message_id"]),
                delivery_id=str(materialized["delivery_id"]),
            )
            self.assertEqual("accepted", delivery["outcome"])
            self.assertEqual(
                {"accepted", "ambiguous"},
                {receipt["state"] for receipt in delivery["receipts"]},
            )
            row = store.read_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_generation=1,
            )
            self.assertEqual("failed", row["dispatch_state"])
            self.assertEqual(3, row["last_event_seq"])
        finally:
            store.close()
            tmp.cleanup()

    def test_clean_pre_acceptance_refusal_is_retried_across_polls_not_stranded(self):
        """GH-691 head 2: a clean pre-acceptance refusal must stay retryable on the
        SECOND poll, not just the first.

        Head 1 made the CURRENT poll return nonzero, but the refusal had already
        appended a rejected_before_acceptance receipt, so the NEXT poll hit the
        selected_receipt branch and returned "duplicate" -- which the seam treats
        as delivery_accepted -- and the packet was marked processed without ever
        calling bb again: stranded after one extra poll. This test INVERTS the
        assertion that the prior test (test_clean_native_refusal_is_durable_
        and_not_retried) used to pin that stranding: that test asserted
        second.state == DUPLICATE and len(sent) == 1, encoding the defect rather
        than catching it. A duplicate of a rejected_before_acceptance receipt is
        not a delivery (codex_delivery.py: that state "would authorize a blind
        retry"), so the duplicate branch now consults the receipt STATE and falls
        through to re-send.

        The receipt is still durable evidence (outcome stays
        rejected_before_acceptance); what changed is that the reader no longer
        treats it as proof of delivery.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FailingBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                first = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                second = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(BB_CONTINUATION_REFUSED, first.state)
            self.assertEqual(
                BB_CONTINUATION_REFUSED,
                second.state,
                "a pre-acceptance refusal must NOT strand as 'duplicate' on the "
                "second poll; the rejected_before_acceptance receipt authorizes a "
                "retry, so the call re-sends and stays retryable",
            )
            self.assertEqual(
                2,
                len(client.sent),
                "the second poll re-sends to bb (genuine retry) -- INVERTED from the "
                "prior test's len==1, which pinned the no-resend stranding",
            )
            delivery = store.read_canonical_delivery(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                message_id=str(materialized["message_id"]),
                delivery_id=str(materialized["delivery_id"]),
            )
            self.assertEqual(
                "rejected_before_acceptance",
                delivery["outcome"],
                "the refusal receipt is still durable evidence that nothing was "
                "delivered; the fix is that the reader no longer treats it as a delivery",
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_pre_acceptance_refusal_retry_holds_for_amiga_and_nuvyr_scopes(self):
        """GH-691 shared contract: the cross-poll retry must hold for Amiga AND a
        registered non-Amiga project.

        continue_bb_thread is scoped by project_id (it is the ledger
        scope_identity), so the duplicate-branch fix is exercised under each
        project's own scope. This is the project-aware mutation proof: a fix that
        strands the packet on the second poll would fail on BOTH projects, not
        just Amiga -- the exact clause missed on GH-689 and caught at review.

        Both directions, across polls, per project:
          * refusal  -- re-sent on poll 2 (retryable, NOT stranded).
          * accepted -- suppresses on poll 2 (a genuine duplicate of an ACCEPTED
            delivery still does not re-send; the fix narrows the short-circuit,
            it does not remove it).
        """
        import sys
        module = sys.modules[__name__]
        for project in ("amiga", "nuvyr"):
            with self.subTest(project=project, direction="refusal"):
                with patch.multiple(
                    module,
                    PROJECT=project,
                    CHAT=f"CHAT-{project}",
                    NATIVE_THREAD=f"thread-{project}",
                ):
                    tmp, store, session, materialized = self.open_fixture()
                try:
                    client = FailingBbClient()
                    with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                        first = continue_bb_thread(
                            store, client=client, session=session,
                            materialized=materialized, observed_at_utc=NOW,
                        )
                        second = continue_bb_thread(
                            store, client=client, session=session,
                            materialized=materialized, observed_at_utc=NOW,
                        )
                    self.assertEqual(BB_CONTINUATION_REFUSED, first.state)
                    self.assertEqual(
                        BB_CONTINUATION_REFUSED, second.state,
                        f"{project}: a pre-acceptance refusal must not strand as "
                        "'duplicate' on the second poll",
                    )
                    self.assertEqual(
                        2, len(client.sent),
                        f"{project}: the second poll re-sends to bb (genuine retry)",
                    )
                finally:
                    store.close()
                    tmp.cleanup()

            with self.subTest(project=project, direction="accepted"):
                with patch.multiple(
                    module,
                    PROJECT=project,
                    CHAT=f"CHAT-{project}",
                    NATIVE_THREAD=f"thread-{project}",
                ):
                    tmp, store, session, materialized = self.open_fixture()
                try:
                    client = FakeBbClient()  # BbQueued -> accepted
                    with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                        first = continue_bb_thread(
                            store, client=client, session=session,
                            materialized=materialized, observed_at_utc=NOW,
                        )
                        second = continue_bb_thread(
                            store, client=client, session=session,
                            materialized=materialized, observed_at_utc=NOW,
                        )
                    self.assertEqual(BB_CONTINUATION_QUEUED, first.state)
                    self.assertEqual(
                        BB_CONTINUATION_DUPLICATE, second.state,
                        f"{project}: a genuine duplicate of an ACCEPTED delivery "
                        "still suppresses on poll 2",
                    )
                    self.assertEqual(
                        1, len(client.sent),
                        f"{project}: an accepted duplicate does not re-send",
                    )
                finally:
                    store.close()
                    tmp.cleanup()

    def test_pre_acceptance_refusal_is_refused_but_timed_out_is_ambiguous(self):
        """GH-691: the two refusal outcomes must be distinct tokens, not one
        "failed" string. This is the module-level mirror of GH-688's two-direction
        rule: everything past the success-or-ambiguity boundary is
        retry-suppressing, and a CLEAN pre-acceptance refusal is the carve-out that
        must NOT be suppressed.

        A clean refusal (reason neither ambiguous nor timed out) means bb refused
        the message BEFORE accepting it -- nothing was delivered -- so it returns
        BB_CONTINUATION_REFUSED, never the bare "failed" that observe_bb_thread
        reuses for a post-delivery terminal. A timed-out refusal is ambiguous (the
        send may have landed) so it stays BB_CONTINUATION_AMBIGUOUS and
        retry-suppressing. One token cannot carry both contracts; a test asserting
        only the refused direction cannot tell this fix from one that suppresses
        every refusal.
        """
        # Direction 1: a clean pre-acceptance refusal returns the distinct
        # "refused" token, not "failed".
        tmp, store, session, materialized = self.open_fixture()
        try:
            clean = FailingBbClient()  # BbRefusal("bb_transport_failed", ...)
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                result = continue_bb_thread(
                    store,
                    client=clean,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(
                BB_CONTINUATION_REFUSED,
                result.state,
                "a clean pre-acceptance refusal is a distinct token from the "
                "post-delivery \"failed\" terminal; nothing was delivered",
            )
            self.assertNotEqual(
                "failed",
                result.state,
                "the bare \"failed\" string carries the post-delivery contract in "
                "observe_bb_thread and must not reach the dispatch seam here",
            )
            self.assertEqual(1, len(clean.sent), "the native send ran exactly once")
        finally:
            store.close()
            tmp.cleanup()

        # Direction 2: a timed-out refusal is ambiguous and retry-suppressing --
        # the opposite half of the same rule, unchanged by this fix.
        tmp, store, session, materialized = self.open_fixture()
        try:
            class TimedOutBbClient(FakeBbClient):
                def send(self, *, thread_id, message, mode="queue-if-active"):
                    self.sent.append((thread_id, message, mode))
                    return BbRefusal(REFUSAL_TIMED_OUT, "native send timed out")

            timed_out = TimedOutBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                result = continue_bb_thread(
                    store,
                    client=timed_out,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(
                BB_CONTINUATION_AMBIGUOUS,
                result.state,
                "a timed-out refusal is ambiguous (the send may have landed) and "
                "stays retry-suppressing, the opposite half of the rule",
            )
            self.assertEqual(
                1, len(timed_out.sent), "the native send ran exactly once and is not retried"
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_binding_generation_mismatch_refuses_before_native_io(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FakeBbClient()
            with self.assertRaises(BbContinuationRefused):
                continue_bb_thread(
                    store,
                    client=client,
                    session={**session, "binding_generation": 2},
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual([], client.sent)
        finally:
            store.close()
            tmp.cleanup()

    def test_queued_marker_is_committed_before_native_send(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            observed = []

            def capture_marker():
                observed.append(
                    store.read_bb_thread_observation(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        conversation_id=CHAT,
                        participant_id=PARTICIPANT,
                        binding_generation=1,
                    )
                )

            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                continue_bb_thread(
                    store,
                    client=FakeBbClient(on_send=capture_marker),
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(
                [
                    (
                        "queued",
                        materialized["message_id"],
                        materialized["delivery_id"],
                        materialized["attempt_id"],
                    )
                ],
                [
                    (
                        row["dispatch_state"],
                        row["last_message_id"],
                        row["last_delivery_id"],
                        row["last_attempt_id"],
                    )
                    for row in observed
                ],
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_terminal_receipt_waits_for_the_queued_delivery_turn(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FakeBbClient(
                {
                    0: BbEventPage(
                        events=(requested(1),),
                        truncated=False,
                        next_after_seq=None,
                    ),
                    1: BbEventPage(
                        events=(
                            BbEvent(2, "event-own-started", "turn/started", turn_id="turn-own"),
                            BbEvent(
                                3,
                                "event-existing-completed",
                                "turn/completed",
                                turn_id="turn-existing",
                            ),
                        ),
                        truncated=False,
                        next_after_seq=None,
                    ),
                    3: BbEventPage(
                        events=(
                            BbEvent(
                                4,
                                "event-own-completed",
                                "turn/completed",
                                turn_id="turn-own",
                            ),
                        ),
                        truncated=False,
                        next_after_seq=None,
                    ),
                }
            )
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                send_client = FakeBbClient()
                continue_bb_thread(
                    store,
                    client=send_client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                first = observe_bb_thread(
                    store,
                    client=client,
                    session=session,
                    observed_at_utc=NOW,
                    registry_revision=REVISION,
                )
                second = observe_bb_thread(
                    store,
                    client=client,
                    session=session,
                    observed_at_utc=NOW,
                    registry_revision=REVISION,
                )
                delivery_after_existing_turn = store.read_canonical_delivery(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=str(materialized["message_id"]),
                    delivery_id=str(materialized["delivery_id"]),
                )
                third = observe_bb_thread(
                    store,
                    client=client,
                    session=session,
                    observed_at_utc=NOW,
                    registry_revision=REVISION,
                )
            self.assertEqual("queued", first.state)
            self.assertEqual("queued", second.state)
            self.assertEqual(
                [(NATIVE_THREAD, PACKET_BODY, "queue-if-active")],
                send_client.sent,
            )
            self.assertEqual(
                {"accepted"},
                {receipt["state"] for receipt in delivery_after_existing_turn["receipts"]},
            )
            self.assertEqual(BB_CONTINUATION_COMPLETED, third.state)
            delivery = store.read_canonical_delivery(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                message_id=str(materialized["message_id"]),
                delivery_id=str(materialized["delivery_id"]),
            )
            self.assertEqual(
                "event-own-completed",
                delivery["evidence"]["extensions"]["x_note_bb_event_id"],
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_successor_generation_can_retain_native_thread_with_fresh_cursor(self):
        tmp = TemporaryDirectory(dir="/tmp")
        store = LedgerStore.open_writer(LedgerPaths.derive(tmp.name, WORKSPACE))
        seed_binding(store)
        try:
            first = store.ensure_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_id=BINDING,
                binding_generation=1,
                native_thread_id=NATIVE_THREAD,
                session_ref_id=SESSION_REF,
                updated_at_utc=NOW,
            )
            store.advance_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_id=BINDING,
                binding_generation=1,
                native_thread_id=NATIVE_THREAD,
                session_ref_id=SESSION_REF,
                event_seq=42,
                dispatch_state="completed",
                updated_at_utc=NOW,
            )
            store._connection.execute(
                "UPDATE conversation_bindings SET generation = 2, native_session_id = ? "
                "WHERE workspace_id = ? AND binding_id = ?",
                (NATIVE_THREAD, WORKSPACE, BINDING),
            )
            try:
                successor = store.ensure_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=2,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    updated_at_utc=NOW,
                )
                recorded = (
                    "created",
                    successor["last_event_seq"],
                    successor["dispatch_state"],
                    successor["native_thread_id"],
                )
            except Exception as error:
                recorded = ("refused", type(error).__name__, str(error))
            predecessor = store.read_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_generation=1,
            )
            self.assertEqual(0, first["last_event_seq"])
            self.assertEqual(
                ("created", 0, "idle", NATIVE_THREAD, 42),
                (*recorded, predecessor["last_event_seq"]),
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_v14_ledger_with_marker_migrates_to_v15_and_marker_reads_dispatch_seq_zero(self):
        """GH-700 migration/upgrade: a v14 ledger with a pre-column marker stays
        READABLE after the v15 ALTER, and the old marker reads dispatch_seq 0
        (the column's DEFAULT). A tightened validator colliding with a migration
        has made real rows unreadable in this repo before, so this exercises the
        new ``_validate_released_v14`` gate end to end against a real row, plus
        the new dispatch_seq SELECTs against a migrated row.

        The receipt-side half of "0 on the old marker and None in the old
        receipt" is covered by
        ``test_legacy_receipt_without_dispatch_seq_strands_fail_closed`` -- a
        v14-era receipt is built there through the exact code path v14 used.
        """
        tmp = TemporaryDirectory(dir="/tmp")
        paths = LedgerPaths.derive(tmp.name, WORKSPACE)
        try:
            # Build a fully-scaffolded v14 ledger (real backups + migration
            # rows) by letting the migration machinery run 1..14; the final
            # SCHEMA_VERSION validate refuses it, leaving the file committed at
            # v14 on disk with its backups intact.
            with self.assertRaisesRegex(MigrationError, "unsupported ledger schema version 14"):
                LedgerStore.open_writer(paths, migrations=store_module.MIGRATIONS[:14])
            # Insert a pre-column marker at v14 (no dispatch_seq column yet) plus
            # the parent rows its foreign keys require.
            with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    """
                    INSERT INTO lifecycle_provider_registry
                    (workspace_id, provider_id, provider_revision, trust_class,
                     supported_operations_json, challenge_algorithm,
                     challenge_ttl_seconds, created_at_utc)
                    VALUES (?, 'provider_bb', 'revision_1', 'managed', '["start"]',
                            'sha256', 60, ?)
                    """,
                    (WORKSPACE, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_participants
                    (workspace_id, scope_kind, scope_identity, conversation_id,
                     participant_id, agent_id, created_at_utc)
                    VALUES (?, 'project', ?, ?, ?, 'agent_glmpi', ?)
                    """,
                    (WORKSPACE, PROJECT, CHAT, PARTICIPANT, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO conversation_bindings
                    (workspace_id, scope_kind, scope_identity, conversation_id,
                     participant_id, binding_id, generation, state, mutation_capable,
                     provider_id, provider_revision, endpoint_id, session_ref_id,
                     native_session_id, runtime_instance_id, registered_at_utc)
                    VALUES (?, 'project', ?, ?, ?, ?, 1, 'active', 1, 'provider_bb',
                            'revision_1', ?, ?, ?, ?, ?)
                    """,
                    (WORKSPACE, PROJECT, CHAT, PARTICIPANT, BINDING, ENDPOINT,
                     SESSION_REF, NATIVE_THREAD, RUNTIME_INSTANCE, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO bb_thread_observations
                    (workspace_id, scope_kind, scope_identity, conversation_id,
                     participant_id, binding_id, binding_generation, native_thread_id,
                     session_ref_id, last_event_seq, dispatch_state, updated_at_utc)
                    VALUES (?, 'project', ?, ?, ?, ?, 1, ?, ?, 0, 'idle', ?)
                    """,
                    (WORKSPACE, PROJECT, CHAT, PARTICIPANT, BINDING, NATIVE_THREAD,
                     SESSION_REF, NOW),
                )
            # Migrate v14 -> v15: _validate_released_v14 accepts the real v14 row,
            # the V15 ALTER adds dispatch_seq DEFAULT 0, and the new SELECTs read it.
            with LedgerStore.open_writer(paths) as store:
                self.assertEqual(15, store.schema_version())
                row = store.read_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_generation=1,
                )
                self.assertIsNotNone(row)
                self.assertEqual(
                    0,
                    row["dispatch_seq"],
                    "a pre-column marker reads dispatch_seq 0 after the v15 ALTER "
                    "DEFAULT 0; a tightened validator that refused it would strand "
                    "every migrated binding",
                )
                self.assertEqual("idle", row["dispatch_state"])
                store.close()
        finally:
            tmp.cleanup()

    def test_legacy_receipt_without_dispatch_seq_strands_fail_closed(self):
        """GH-700 legacy fail-closed: a receipt predating the column has no
        ``x_note_dispatch_seq``, so ``_receipt_dispatch_seq`` reads None, which is
        never equal to the marker's seq. An in-flight send carrying such a receipt
        therefore strands as ambiguous rather than authorizing a retry -- old rows
        fail closed, exactly as the design requires.

        The legacy receipt is built through the real gate with the exact evidence
        shape v14 produced (extensions carry the refusal notes, not the per-send
        seq), so this also proves such a receipt stays readable post-migration.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                _append_legacy_receipt(store, session=session, materialized=materialized)
            # Park the marker in-flight (dispatch_seq stays 0) with the legacy
            # receipt as the one under inspection.
            store.ensure_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_id=BINDING,
                binding_generation=1,
                native_thread_id=NATIVE_THREAD,
                session_ref_id=SESSION_REF,
                updated_at_utc=NOW,
            )
            store.advance_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_id=BINDING,
                binding_generation=1,
                native_thread_id=NATIVE_THREAD,
                session_ref_id=SESSION_REF,
                event_seq=0,
                dispatch_state="ambiguous",
                last_message_id=str(materialized["message_id"]),
                last_delivery_id=str(materialized["delivery_id"]),
                last_attempt_id=str(materialized["attempt_id"]),
                updated_at_utc=NOW,
            )
            client = FakeBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                result = continue_bb_thread(
                    store, client=client, session=session,
                    materialized=materialized, observed_at_utc=NOW,
                )
            self.assertEqual(
                BB_CONTINUATION_AMBIGUOUS,
                result.state,
                "a legacy receipt (dispatch_seq None) must fail closed: None is "
                "never equal to the marker seq, so an in-flight send strands rather "
                "than authorizing a retry of a send that may have landed",
            )
            self.assertEqual(
                0, len(client.sent), "the guard strands before any native send"
            )
            delivery = store.read_canonical_delivery(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                message_id=str(materialized["message_id"]),
                delivery_id=str(materialized["delivery_id"]),
            )
            receipt = delivery["selected_receipt"]
            self.assertNotIn(
                "x_note_dispatch_seq",
                receipt["evidence"]["extensions"],
                "a v14-era receipt's extensions carry no per-send seq -- this is "
                "the None the guard reads and strands on",
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_inflight_send_whose_receipt_speaks_for_current_recovers_GH697(self):
        """GH-697 recovery (regression table): when a send is in flight AND the
        receipt under inspection DOES speak for the current send (dispatch_seq
        matches), the guard does NOT strand -- it proceeds and retries. This is
        the half GH-699 got wrong: it stranded every in-flight send that had a
        receipt, permanently. GH-700 strands only when the receipt is stale
        (S != S); an S == S receipt means THIS send was the one refused, so
        retrying is safe.

        Modelled as the post-receipt state-write failure that produced GH-697's
        live strand: poll 1 refuses and writes its rejection receipt
        (dispatch_seq 1), but the row is left in-flight (queued) -- the state
        write that would have recorded 'failed' did not land. Poll 2 sees
        marker_seq == receipt_seq == 1, so it does NOT strand; it re-sends.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FailingBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                first = continue_bb_thread(
                    store, client=client, session=session,
                    materialized=materialized, observed_at_utc=NOW,
                )
                self.assertEqual(BB_CONTINUATION_REFUSED, first.state)
                # Simulate GH-697's post-receipt state-write failure: the rejection
                # receipt (dispatch_seq 1) is durable, but the row is left in-flight.
                # dispatch_seq is intentionally NOT passed, so COALESCE preserves 1.
                store.advance_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=1,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    event_seq=0,
                    dispatch_state="queued",
                    last_message_id=str(materialized["message_id"]),
                    last_delivery_id=str(materialized["delivery_id"]),
                    last_attempt_id=str(materialized["attempt_id"]),
                    updated_at_utc=NOW,
                )
                second = continue_bb_thread(
                    store, client=client, session=session,
                    materialized=materialized, observed_at_utc=NOW,
                )
            self.assertEqual(
                BB_CONTINUATION_REFUSED,
                second.state,
                "an in-flight send whose receipt speaks for the current send "
                "(dispatch_seq 1 == 1) must NOT strand -- it retries. GH-699 "
                "stranded this permanently; only a STALE receipt (S != S) strands.",
            )
            self.assertEqual(
                2,
                len(client.sent),
                "poll 2 re-sends: the receipt speaks for the current send, so the "
                "guard falls through and retries rather than stranding",
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_marker_matching_rejection_among_many_receipts_authorizes_retry(self):
        """P1 (GH-700 review head 2): when one delivery accumulates several
        rejected_before_acceptance receipts, read_canonical_delivery's fold picks
        one by lexicographically smallest receipt_id -- which has no relationship
        to which send a receipt belongs to. The guard must consult EVERY receipt:
        if any rejected_before_acceptance receipt carries the marker's
        dispatch_seq, the current send was rejected before acceptance and a retry
        is authorized, even when the fold selected a different (earlier) send's
        receipt.

        The marker is parked on whichever send the fold did NOT select, so this
        test exercises the exact bug path (folded selection != marker) rather
        than the trivial one.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                context = _context(store, session)
                message_id = str(materialized["message_id"])
                delivery_id = str(materialized["delivery_id"])
                attempt_id = str(materialized["attempt_id"])
                # Two rejections from two distinct sends (dispatch_seq 10 and 20).
                for seq in (10, 20):
                    append_dead_letter_receipt(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        registry_revision=str(materialized.get("registry_revision") or ""),
                        allow_canonical_write=True,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        evidence=_receipt_evidence_with_seq(
                            context=context,
                            message_id=message_id,
                            delivery_id=delivery_id,
                            attempt_id=attempt_id,
                            dispatch_seq=seq,
                        ),
                        session_ref_id=str(context["session_ref_id"]),
                        created_at_utc=NOW,
                    )
                delivery = store.read_canonical_delivery(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                )
                selected_seq = _receipt_dispatch_seq(delivery["selected_receipt"])
                present_seqs = sorted(
                    _receipt_dispatch_seq(receipt) for receipt in delivery["receipts"]
                )
                # The fold selected one receipt; park the marker on the OTHER send,
                # which the folded-selection guard would strand on.
                marker_seq = next(seq for seq in present_seqs if seq != selected_seq)
                store.ensure_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=1,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    updated_at_utc=NOW,
                )
                store.advance_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=1,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    event_seq=0,
                    dispatch_state="ambiguous",
                    dispatch_seq=marker_seq,
                    last_message_id=message_id,
                    last_delivery_id=delivery_id,
                    last_attempt_id=attempt_id,
                    updated_at_utc=NOW,
                )
                client = FailingBbClient()
                result = continue_bb_thread(
                    store, client=client, session=session,
                    materialized=materialized, observed_at_utc=NOW,
                )
            self.assertEqual(
                BB_CONTINUATION_REFUSED,
                result.state,
                "a rejected_before_acceptance receipt carrying the marker seq proves "
                "the current send was rejected, so retry is authorized -- even though "
                "the folded selection is a different (earlier) send's receipt",
            )
            self.assertEqual(1, len(client.sent), "the guard authorizes exactly one retry send")
            # Prove the test exercises the bug path, not the trivial one: the fold
            # selected a receipt whose seq is NOT the marker, yet retry is
            # authorized by the other (present) receipt.
            self.assertNotEqual(selected_seq, marker_seq)
            self.assertIn(marker_seq, present_seqs)
        finally:
            store.close()
            tmp.cleanup()

    def test_no_marker_matching_rejection_stays_ambiguous(self):
        """P1 companion (both directions): with NO rejected_before_acceptance
        receipt carrying the marker's seq, the result is still ambiguous. The fix
        authorizes retry ONLY where a receipt proves the current send was
        rejected; a marker seq that no rejection matches is unproven, so the guard
        strands -- this is what tells the fix from an unconditional retry.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                context = _context(store, session)
                message_id = str(materialized["message_id"])
                delivery_id = str(materialized["delivery_id"])
                attempt_id = str(materialized["attempt_id"])
                for seq in (10, 20):
                    append_dead_letter_receipt(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        registry_revision=str(materialized.get("registry_revision") or ""),
                        allow_canonical_write=True,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        evidence=_receipt_evidence_with_seq(
                            context=context,
                            message_id=message_id,
                            delivery_id=delivery_id,
                            attempt_id=attempt_id,
                            dispatch_seq=seq,
                        ),
                        session_ref_id=str(context["session_ref_id"]),
                        created_at_utc=NOW,
                    )
                # Marker seq 99 matches neither receipt (10 nor 20): unproven.
                store.ensure_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=1,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    updated_at_utc=NOW,
                )
                store.advance_bb_thread_observation(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id=CHAT,
                    participant_id=PARTICIPANT,
                    binding_id=BINDING,
                    binding_generation=1,
                    native_thread_id=NATIVE_THREAD,
                    session_ref_id=SESSION_REF,
                    event_seq=0,
                    dispatch_state="ambiguous",
                    dispatch_seq=99,
                    last_message_id=message_id,
                    last_delivery_id=delivery_id,
                    last_attempt_id=attempt_id,
                    updated_at_utc=NOW,
                )
                client = FailingBbClient()
                result = continue_bb_thread(
                    store, client=client, session=session,
                    materialized=materialized, observed_at_utc=NOW,
                )
            self.assertEqual(
                BB_CONTINUATION_AMBIGUOUS,
                result.state,
                "no receipt proves the current send (dispatch_seq 99) was rejected, "
                "so the guard strands -- retry needs proof, not the absence of it",
            )
            self.assertEqual(0, len(client.sent), "an unproven in-flight send does not retry")
        finally:
            store.close()
            tmp.cleanup()

    def test_stale_rejection_from_earlier_send_does_not_authorize_a_resend(self):
        """GH-700 item 5 (three polls): the case both the one-poll and two-poll
        tests on this seam missed.

        Poll 1 refuses (rejection receipt, dispatch_seq 1). Poll 2 is accepted but
        its accepted-receipt write crashes after bb accepted the send, advancing
        the marker to dispatch_seq 2 with no accepted receipt. Poll 3 must NOT
        send: the only receipt is poll 1's rejection (dispatch_seq 1), which does
        not speak for the current send (marker dispatch_seq 2). A stale rejection
        from an earlier send cannot authorize a retry of a send that may have
        landed -- that is the four-round double-send this column closes.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                client, third = _run_stale_rejection_three_poll_scenario(
                    store, session=session, materialized=materialized,
                )
            self.assertEqual(
                BB_CONTINUATION_AMBIGUOUS,
                third.state,
                "poll 3 strands: the stale rejection does not speak for the current send",
            )
            self.assertEqual(
                2,
                len(client.sent),
                "poll 3 must NOT send a third time -- a stale rejection (dispatch_seq "
                "1) cannot authorize a retry of the current send (marker dispatch_seq 2)",
            )
            row = store.read_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_generation=1,
            )
            self.assertEqual("ambiguous", row["dispatch_state"])
            self.assertEqual(2, row["dispatch_seq"], "the marker represents send 2")
        finally:
            store.close()
            tmp.cleanup()

    def test_dispatch_seq_is_monotonic_per_send_and_stamped_on_each_receipt(self):
        """GH-700 monotonicity: each pre-send marker write bumps dispatch_seq by
        exactly one, and that send's receipt carries the same value, so the guard's
        ``receipt_seq == marker_seq`` test distinguishes sends. Because dispatch_seq
        is part of the SHA-protected evidence body, each refusal now materializes a
        DISTINCT receipt (different body -> different receipt_id), which is the
        durable per-send identity attempt_id could never provide.
        """
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FailingBbClient()  # every poll refuses -> every poll is a fresh send
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                for poll in (1, 2, 3):
                    result = continue_bb_thread(
                        store, client=client, session=session,
                        materialized=materialized, observed_at_utc=NOW,
                    )
                    self.assertEqual(BB_CONTINUATION_REFUSED, result.state)
                    row = store.read_bb_thread_observation(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        conversation_id=CHAT,
                        participant_id=PARTICIPANT,
                        binding_generation=1,
                    )
                    self.assertEqual(
                        poll,
                        row["dispatch_seq"],
                        f"after poll {poll} the marker dispatch_seq must equal the send count",
                    )
                delivery = store.read_canonical_delivery(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=str(materialized["message_id"]),
                    delivery_id=str(materialized["delivery_id"]),
                )
                seqs = [
                    receipt["evidence"]["extensions"].get("x_note_dispatch_seq")
                    for receipt in delivery["receipts"]
                ]
            self.assertEqual(
                [1, 2, 3],
                sorted(seqs),
                "each rejection receipt carries its own send's dispatch_seq -- the "
                "durable per-send identity (receipts return in receipt_id/hash order, "
                "so sort before comparing the values)",
            )
            self.assertEqual(3, len(client.sent))
        finally:
            store.close()
            tmp.cleanup()

    def test_stale_rejection_guard_holds_for_amiga_and_nuvyr_scopes(self):
        """GH-700 shared contract: the per-send guard must hold for Amiga AND a
        registered non-Amiga project, modelled on
        ``test_pre_acceptance_refusal_retry_holds_for_amiga_and_nuvyr_scopes``.

        ``continue_bb_thread`` is scoped by project_id (the ledger
        scope_identity), so the guard is exercised under each project's own scope.
        A fix that let a stale rejection authorize a resend would fail on BOTH
        projects, not just Amiga -- the exact clause missed on GH-689. The
        mutation proof (remove the seq comparison) must therefore fail on the
        NON-AMIGA path too.
        """
        import sys
        module = sys.modules[__name__]
        for project in ("amiga", "nuvyr"):
            with self.subTest(project=project):
                with patch.multiple(
                    module,
                    PROJECT=project,
                    CHAT=f"CHAT-{project}",
                    NATIVE_THREAD=f"thread-{project}",
                ):
                    tmp, store, session, materialized = self.open_fixture()
                try:
                    with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                        client, third = _run_stale_rejection_three_poll_scenario(
                            store, session=session, materialized=materialized,
                        )
                    self.assertEqual(
                        BB_CONTINUATION_AMBIGUOUS,
                        third.state,
                        f"{project}: poll 3 must strand on a stale rejection",
                    )
                    self.assertEqual(
                        2,
                        len(client.sent),
                        f"{project}: poll 3 must not send a third time",
                    )
                finally:
                    store.close()
                    tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
