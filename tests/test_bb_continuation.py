from __future__ import annotations

import json
import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.bb_client import BbEvent, BbEventPage, BbQueued, BbRefusal, REFUSAL_TIMED_OUT
from llm_collab.bb_continuation import (
    BB_CONTINUATION_AMBIGUOUS,
    BB_CONTINUATION_COMPLETED,
    BB_CONTINUATION_DUPLICATE,
    BB_CONTINUATION_QUEUED,
    BB_CONTINUATION_REFUSED,
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

    def test_refusal_whose_state_write_fails_re_sends_on_a_later_poll(self):
        """GH-697: a refusal whose state write fails (after the receipt commits)
        must not strand the packet. Single-poll and two-poll tests have EACH
        already missed a defect on this seam, so this spans THREE polls with an
        injected failure BETWEEN the receipt commit and the observation state
        write.

        The receipt (rejected_before_acceptance) and the observation
        dispatch_state are two writes with no atomicity between them. Poll 1: the
        refusal receipt commits, then _advance(dispatch_state="failed") raises
        (injected). Before the fix the row stayed "queued" and the pending-row
        guard returned "ambiguous" forever -- three polls, one send, bb never
        called again. The receipt is now the single source of truth for whether a
        delivery occurred: a rejection receipt proves the send was refused before
        acceptance (nothing delivered), so it overrides the stale "queued" and the
        call re-sends on polls 2 and 3. accepted/completed/ambiguous receipts still
        short-circuit (a duplicate of those is a real delivery, or -- for ambiguous
        -- a send that may have landed and must not be re-sent).

        Shared contract (AGENTS.md Project Boundary): continue_bb_thread is scoped
        by project_id, so the fix is exercised under Amiga AND a registered
        non-Amiga project. The mutation proof fails on the non-Amiga path too.
        """
        import sqlite3
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
                    client = FailingBbClient()  # pre-acceptance refusal (bb_transport_failed)
                    real_advance = store.advance_bb_thread_observation
                    failed_once: list[bool] = []

                    def failing_advance(**kw):
                        # Inject the divergence: fail ONLY the first post-refusal
                        # state write (dispatch_state "failed"), i.e. the write that
                        # happens AFTER the rejection receipt commits. The pre-send
                        # "queued" advance and every later advance run normally, so
                        # polls 2 and 3 exercise the read-path reconciliation.
                        if (
                            kw.get("dispatch_state") == "failed"
                            and not failed_once
                        ):
                            failed_once.append(True)
                            raise sqlite3.OperationalError(
                                "injected: state write failed after receipt commit"
                            )
                        return real_advance(**kw)

                    store.advance_bb_thread_observation = failing_advance
                    with patch.dict(
                        os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}
                    ):
                        # Poll 1: the receipt commits, then the state write raises.
                        # The refusal surface is returned (the receipt is durable).
                        first = continue_bb_thread(
                            store,
                            client=client,
                            session=session,
                            materialized=materialized,
                            observed_at_utc=NOW,
                        )
                    self.assertEqual(
                        BB_CONTINUATION_REFUSED,
                        first.state,
                        f"{project}: poll 1 returns the refusal surface (the receipt "
                        "is durable) instead of raising on the failed state write",
                    )
                    self.assertEqual(
                        1,
                        len(client.sent),
                        f"{project}: poll 1 sent exactly once before the refusal",
                    )
                    delivery = store.read_canonical_delivery(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=project,
                        message_id=str(materialized["message_id"]),
                        delivery_id=str(materialized["delivery_id"]),
                    )
                    self.assertEqual(
                        "rejected_before_acceptance",
                        delivery["outcome"],
                        f"{project}: the rejection receipt committed before the "
                        "failed state write -- it is the durable proof nothing landed",
                    )
                    row = store.read_bb_thread_observation(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=project,
                        conversation_id=f"CHAT-{project}",
                        participant_id=PARTICIPANT,
                        binding_generation=1,
                    )
                    self.assertEqual(
                        "queued",
                        row["dispatch_state"],
                        f"{project}: the state write failed, so the row is stuck at "
                        "the pre-send 'queued' -- this is the divergence the receipt "
                        "must override on the next poll",
                    )

                    with patch.dict(
                        os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}
                    ):
                        # Poll 2: the rejection receipt overrides the stale 'queued';
                        # the packet RE-SENDS rather than stranding as 'ambiguous'.
                        second = continue_bb_thread(
                            store,
                            client=client,
                            session=session,
                            materialized=materialized,
                            observed_at_utc=NOW,
                        )
                        # Poll 3: still retryable across polls.
                        third = continue_bb_thread(
                            store,
                            client=client,
                            session=session,
                            materialized=materialized,
                            observed_at_utc=NOW,
                        )
                    self.assertEqual(
                        BB_CONTINUATION_REFUSED,
                        second.state,
                        f"{project}: a later poll must re-send (REFUSED), not strand "
                        "as 'ambiguous' forever -- the rejection receipt proves the "
                        "send was refused before acceptance",
                    )
                    self.assertEqual(
                        BB_CONTINUATION_REFUSED,
                        third.state,
                        f"{project}: the third poll is still retryable, not stranded",
                    )
                    self.assertEqual(
                        3,
                        len(client.sent),
                        f"{project}: polls 2 and 3 each re-send to bb (genuine "
                        "retries) -- the stranding 'one send across three polls' is "
                        "gone",
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


if __name__ == "__main__":
    unittest.main()
