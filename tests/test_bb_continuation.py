from __future__ import annotations

import json
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.bb_client import BbEvent, BbEventPage, BbQueued, BbRefusal
from llm_collab.bb_continuation import (
    BB_CONTINUATION_AMBIGUOUS,
    BB_CONTINUATION_COMPLETED,
    BB_CONTINUATION_DUPLICATE,
    BB_CONTINUATION_QUEUED,
    BbContinuationRefused,
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
        body=b"packet body",
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


def requested(seq: int, *, body: str = "packet body") -> BbEvent:
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
            message = {"body": "hello bb"}
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                first = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    message=message,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                second = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    message=message,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(BB_CONTINUATION_QUEUED, first.state)
            self.assertEqual(BB_CONTINUATION_DUPLICATE, second.state)
            self.assertEqual(first.receipt_id, second.receipt_id)
            self.assertEqual([(NATIVE_THREAD, "hello bb", "queue-if-active")], client.sent)
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
                    message={"body": "hello once"},
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            second = continue_bb_thread(
                store,
                client=client,
                session=session,
                message={"body": "hello once"},
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
                    message={"body": "hello once"},
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
                    message={"body": "hello once"},
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

    def test_clean_native_refusal_is_durable_and_not_retried(self):
        tmp, store, session, materialized = self.open_fixture()
        try:
            client = FailingBbClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                first = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    message={"body": "refused once"},
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                second = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    message={"body": "refused once"},
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual("failed", first.state)
            self.assertEqual(BB_CONTINUATION_DUPLICATE, second.state)
            self.assertEqual(1, len(client.sent))
            delivery = store.read_canonical_delivery(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                message_id=str(materialized["message_id"]),
                delivery_id=str(materialized["delivery_id"]),
            )
            self.assertEqual("rejected_before_acceptance", delivery["outcome"])
            self.assertEqual(
                "rejected_before_acceptance",
                delivery["selected_receipt"]["state"],
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
                    message={"body": "must not send"},
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
                    message={"body": "packet body"},
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
                continue_bb_thread(
                    store,
                    client=FakeBbClient(),
                    session=session,
                    message={"body": "packet body"},
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

    def test_successor_binding_generation_starts_with_a_fresh_cursor(self):
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
                ("bb_thread_successor", WORKSPACE, BINDING),
            )
            successor = store.ensure_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_id=BINDING,
                binding_generation=2,
                native_thread_id="bb_thread_successor",
                session_ref_id=SESSION_REF,
                updated_at_utc=NOW,
            )
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
                (0, "idle", "bb_thread_successor", 42),
                (
                    successor["last_event_seq"],
                    successor["dispatch_state"],
                    successor["native_thread_id"],
                    predecessor["last_event_seq"],
                ),
            )
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
