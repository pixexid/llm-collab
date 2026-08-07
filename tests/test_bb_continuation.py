from __future__ import annotations

import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))
import _session_autobridge as session_autobridge  # noqa: E402

from llm_collab.bb_client import (
    PINNED_BB_VERSION,
    REFUSAL_LAUNCH_UNAVAILABLE,
    BbClient,
    BbEvent,
    BbEventPage,
    BbQueued,
    BbRefusal,
    BbTransportTimeout,
    subprocess_transport,
)
from llm_collab.bb_continuation import (
    BB_CONTINUATION_AMBIGUOUS,
    BB_CONTINUATION_COMPLETED,
    BB_CONTINUATION_DUPLICATE,
    BB_CONTINUATION_QUEUED,
    BB_CONTINUATION_UNATTEMPTED,
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

    def reserve_launch(self):
        return None

    def cancel_launch_reservation(self):
        pass

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

    def test_stalled_launch_cap_leaves_delivery_unattempted_and_retryable(self):
        import llm_collab.bb_client as bb

        tmp, store, session, materialized = self.open_fixture()
        paths = LedgerPaths.derive(tmp.name, WORKSPACE)
        release_launch = threading.Event()
        entered_lock = threading.Lock()
        entered = 0
        launch_threads = []
        message_path = "Chats/bb/cap-retry.md"
        message = {
            "path": message_path,
            "frontmatter": {
                "from": "codex",
                "sender_agent_id": "codex",
                "title": "bb cap retry",
                "target_session_id": NATIVE_THREAD,
            },
        }
        session.update(
            {
                "mode": "auto-read",
                "wake_strategy": "runtime_trigger",
                "processed_messages": [],
            }
        )

        class NeverReturningLaunch:
            def __init__(self, *_args, **_kwargs):
                nonlocal entered
                with entered_lock:
                    entered += 1
                    launch_number = entered
                if launch_number <= bb.MAX_STALLED_LAUNCHES:
                    release_launch.wait()
                self.stdout = io.StringIO(
                    json.dumps({"threadId": NATIVE_THREAD, "ok": True, "mode": "queue"})
                )
                self.stderr = io.StringIO("")

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        transport = subprocess_transport(["irrelevant"])
        client = BbClient(transport, enabled=True, timeout_seconds=0.05)
        client._verified_version = PINNED_BB_VERSION
        before_threads = set(threading.enumerate())
        store.close()

        def mark_processed(target_session, path, *, prepared=None):
            target_session.setdefault("processed_messages", []).append(path)

        dispatch_patches = {
            "load_session": Mock(return_value=session),
            "session_is_dispatchable": Mock(return_value=(True, "ok")),
            "matching_unread_messages": Mock(return_value=[message]),
            "processed_messages": Mock(
                side_effect=lambda target: set(target.get("processed_messages", []))
            ),
            "message_targets_session": Mock(return_value=(True, "test")),
            "claim_message_activation": Mock(return_value=(True, None)),
            "should_skip_for_loop_protection": Mock(return_value=(False, "ok")),
            "resolve_effective_action": Mock(return_value=("runtime_trigger", "test")),
            "reserve_message_result": Mock(return_value=(dict(session), "{}")),
            "materialize_selected_runtime_packet": Mock(
                return_value={
                    **materialized,
                    "resolved": True,
                    "created": True,
                    "canonical_write_started": True,
                }
            ),
            "mark_canonical_settlement_complete": Mock(),
            "append_event": Mock(),
            "write_operator_turn_summary": Mock(return_value={}),
            "refresh_runtime_ui": Mock(return_value={}),
            "mark_message_processed": Mock(side_effect=mark_processed),
            "save_session": Mock(),
            "config_get": Mock(
                side_effect=lambda key: WORKSPACE if key == "workspace_id" else None
            ),
            "project_state_root": Mock(return_value=tmp.name),
            "get_project": Mock(return_value={"bb": {"enabled": True}}),
            "bb_bootstrap_enabled": Mock(return_value=True),
        }
        try:
            with patch.object(bb.subprocess, "Popen", NeverReturningLaunch), patch.dict(
                os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}
            ), patch.multiple(session_autobridge, **dispatch_patches), patch(
                "llm_collab.bb_continuation.client_from_project", return_value=client
            ):
                for _ in range(bb.MAX_STALLED_LAUNCHES):
                    with self.assertRaises(BbTransportTimeout):
                        transport([], 0.05)

                refused = session_autobridge.dispatch_session(str(session["session_id"]))
                with LedgerStore.open_reader(paths) as reader:
                    row = reader.read_bb_thread_observation(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        conversation_id=CHAT,
                        participant_id=PARTICIPANT,
                        binding_generation=1,
                    )
                self.assertEqual(
                    ("idle", None, None, None),
                    (
                        row["dispatch_state"],
                        row["last_message_id"],
                        row["last_delivery_id"],
                        row["last_attempt_id"],
                    ),
                    "cap refusal left durable queued delivery state",
                )
                self.assertNotIn(
                    message_path,
                    session["processed_messages"],
                    "cap refusal marked the unread packet processed",
                )
                self.assertEqual(
                    BB_CONTINUATION_UNATTEMPTED,
                    refused["actions"][0]["runtime_result"]["status"],
                )

                launch_threads = [
                    thread
                    for thread in threading.enumerate()
                    if thread not in before_threads and thread.name == "bb-subprocess-launch"
                ]
                release_launch.set()
                for thread in launch_threads:
                    thread.join(timeout=1.0)

                retried = session_autobridge.dispatch_session(str(session["session_id"]))
                self.assertEqual(
                    BB_CONTINUATION_QUEUED,
                    retried["actions"][0]["runtime_result"]["status"],
                )
                self.assertIn(message_path, session["processed_messages"])
        finally:
            release_launch.set()
            for thread in launch_threads:
                thread.join(timeout=1.0)
            tmp.cleanup()

    def test_send_time_launch_cap_refusal_is_unattempted_and_retryable(self):
        class RefuseOnceClient(FakeBbClient):
            def send(self, *, thread_id: str, message: str, mode: str = "queue-if-active"):
                if not self.sent:
                    self.sent.append((thread_id, message, mode))
                    return BbRefusal(
                        REFUSAL_LAUNCH_UNAVAILABLE,
                        "stalled launch cap reached",
                    )
                return super().send(thread_id=thread_id, message=message, mode=mode)

        tmp, store, session, materialized = self.open_fixture()
        try:
            client = RefuseOnceClient()
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                refused = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
                retried = continue_bb_thread(
                    store,
                    client=client,
                    session=session,
                    materialized=materialized,
                    observed_at_utc=NOW,
                )
            self.assertEqual(BB_CONTINUATION_UNATTEMPTED, refused.state)
            self.assertFalse(refused.native_called)
            self.assertEqual(BB_CONTINUATION_QUEUED, retried.state)
            self.assertTrue(retried.native_called)
        finally:
            store.close()
            tmp.cleanup()

    def test_interrupted_send_time_reset_is_ambiguous_with_retryable_state(self):
        import llm_collab.bb_continuation as continuation

        class LaunchUnavailableClient(FakeBbClient):
            def send(self, *, thread_id: str, message: str, mode: str = "queue-if-active"):
                self.sent.append((thread_id, message, mode))
                return BbRefusal(
                    REFUSAL_LAUNCH_UNAVAILABLE,
                    "stalled launch cap reached",
                )

        tmp, store, session, materialized = self.open_fixture()
        real_advance = continuation._advance
        interrupted = False

        def interrupt_first_idle_reset(*args, **kwargs):
            nonlocal interrupted
            if kwargs.get("dispatch_state") == "idle" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("simulated interrupted idle reset")
            return real_advance(*args, **kwargs)

        result = None
        try:
            with patch.dict(
                os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}
            ), patch.object(continuation, "_advance", interrupt_first_idle_reset):
                try:
                    result = continue_bb_thread(
                        store,
                        client=LaunchUnavailableClient(),
                        session=session,
                        materialized=materialized,
                        observed_at_utc=NOW,
                    )
                except BaseException:
                    pass
            row = store.read_bb_thread_observation(
                workspace_id=WORKSPACE,
                scope_kind="project",
                scope_identity=PROJECT,
                conversation_id=CHAT,
                participant_id=PARTICIPANT,
                binding_generation=1,
            )
            self.assertEqual(
                (BB_CONTINUATION_AMBIGUOUS, "idle"),
                (result.state if result is not None else None, row["dispatch_state"]),
                "interrupted launch reset did not return ambiguity with retryable state",
            )
        finally:
            store.close()
            tmp.cleanup()

    def test_send_baseexception_cancels_unconsumed_launch_reservation(self):
        class InterruptingReservedClient(FakeBbClient):
            def __init__(self):
                super().__init__()
                self.reserved = False

            def reserve_launch(self):
                self.reserved = True
                return None

            def cancel_launch_reservation(self):
                self.reserved = False

            def send(self, *, thread_id: str, message: str, mode: str = "queue-if-active"):
                raise KeyboardInterrupt("simulated pre-transport interruption")

        tmp, store, session, materialized = self.open_fixture()
        client = InterruptingReservedClient()
        try:
            with patch.dict(os.environ, {"LLM_COLLAB_CANONICAL_CONTROL": "enabled"}):
                try:
                    continue_bb_thread(
                        store,
                        client=client,
                        session=session,
                        materialized=materialized,
                        observed_at_utc=NOW,
                    )
                except BaseException:
                    pass
            self.assertFalse(
                client.reserved,
                "send BaseException leaked its unconsumed launch reservation",
            )
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

    def test_clean_native_refusal_is_durable_and_not_retried(self):
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
