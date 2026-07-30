"""Fixture-only proof for codex_delivery.deliver_next_turn_idle (#94).

Mutation-proven gates: idle delivers exactly one turn with an accepted native
receipt; busy/uncertain/stale-binding/gate-disabled/probe-failure each produce
zero turn frames, and the admissibility gate is load-bearing (neutering it
would let a frame through and fail the busy test).
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import llm_collab.ledger.store as store_module
from llm_collab.canonical.control import CANONICAL_CONTROL_ENABLED, CANONICAL_CONTROL_ENV
from llm_collab.canonical.legacy_packet_materialization import (
    LegacyPacketMaterializationRefused,
)
from llm_collab.codex_app_server_live_probe import (
    OBSERVATION_ADMISSIBLE,
    OBSERVATION_BUSY,
    OBSERVATION_UNCERTAIN,
    CodexAppServerExactThreadResult,
    CodexAppServerThreadObservation,
)
from llm_collab.canonical.codex_delivery import (
    OUTCOME_ACCEPTED,
    OUTCOME_DEFERRED_BUSY,
    OUTCOME_GATE_DISABLED,
    OUTCOME_UNCERTAIN,
    deliver_next_turn_idle,
)
from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.ledger import LedgerPaths, LedgerStore
from llm_collab.session_lifecycle import (
    CodexLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
    TrustedProjectRoot,
)

WORKSPACE = "ws_alpha"
PROJECT = "amiga"
CHAT = "CHAT-DELIVER1"
NOW = "2026-07-30T00:00:00+00:00"
EXPIRY = "2026-07-30T00:01:00+00:00"
NATIVE = "native_session_one"
SESSION_ID = "SESSION-PI-KIMI-DELIVER"


class FakeTurnTransport:
    def __init__(self, terminal=({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}},),
                 turn_start_result=None, recv_sleep=0.0):
        self.frames = []
        self.notifications = []
        self.recv_calls = 0
        self._terminal = list(terminal)
        self._turn_start_result = turn_start_result
        self._recv_sleep = recv_sleep

    def exchange(self, frame):
        self.frames.append(frame)
        method = frame["method"]
        if method == "initialize" or method == "thread/resume":
            return {"jsonrpc": "2.0", "id": frame["id"], "result": {}}
        if method == "turn/start":
            if self._turn_start_result is not None:
                return self._turn_start_result
            return {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
            }
        raise AssertionError(f"unexpected method {method}")

    def notify(self, frame):
        self.notifications.append(frame)

    def recv_json(self):
        self.recv_calls += 1
        if self._recv_sleep:
            import time as _time
            _time.sleep(self._recv_sleep)
        if not self._terminal:
            raise TimeoutError("no terminal frame")
        return self._terminal.pop(0)


class DeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir()
        self.repo = root / "repo"
        self.repo.mkdir()
        self.cwd = self.repo / "work"
        self.cwd.mkdir()
        self.runtime_home = bind_runtime_home(self.codex_home)
        self.paths = LedgerPaths.derive(root / "state", WORKSPACE)
        self.workspace_root = root
        patcher = mock.patch.object(
            store_module, "_linked_sqlite_version_info", return_value=(3, 51, 3)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.provider = CodexLifecycleProvider(
            exact_thread_probe=lambda thread_id: CodexAppServerExactThreadResult(
                thread_id=thread_id, methods=("initialize", "thread/read")
            )
        )
        self.core = SessionLifecycleCore(self.provider, token_factory=lambda: "token-alpha")
        self.subject = LifecycleSubject(
            workspace_id=WORKSPACE,
            scope_kind="project",
            scope_identity=PROJECT,
            conversation_id=CHAT,
            participant_id="participant_kimi",
            agent_id="agent_kimi",
            endpoint_id="endpoint_codex",
            native_session_id=NATIVE,
            runtime_instance_id="runtime_one",
        )
        self.trusted_root = TrustedProjectRoot(PROJECT, "repo_app", str(self.repo), str(self.cwd))
        with LedgerStore.open_writer(self.paths) as store:
            self._provision(store)
            challenge = self.core.reserve(
                store,
                self.subject,
                runtime_home=self.runtime_home,
                created_at_utc=NOW,
                expires_at_utc=EXPIRY,
                correlation_id="corr_reserve",
                trusted_project_root=self.trusted_root,
            )
            resolved = self.core.consume(
                store,
                self.subject,
                challenge,
                runtime_home=self.runtime_home,
                consumed_at_utc=NOW,
                correlation_id="corr_consume",
                trusted_project_root=self.trusted_root,
            )
            self.assertTrue(resolved["resolved"])
            self.binding_id = str(resolved["binding_id"])
            revision = "sha256:" + "ab" * 32
            store.record_registry_snapshot(
                workspace_id=WORKSPACE,
                registry_revision=revision,
                registry_source_sha256="ab" * 32,
                captured_at_utc=NOW,
                workspace_snapshot_json=json.dumps(
                    {"workspace_id": WORKSPACE, "projects": [PROJECT]}
                ),
                project_snapshots={PROJECT: json.dumps({"project_id": PROJECT, "canonical_writes": True})},
                source_snapshots={PROJECT: {}},
            )

    def _provision(self, store: LedgerStore) -> None:
        store._connection.execute(
            """
            INSERT INTO conversation_participants
            (workspace_id, scope_kind, scope_identity, conversation_id, participant_id, agent_id, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (WORKSPACE, "project", PROJECT, CHAT, "participant_kimi", "agent_kimi", NOW),
        )
        descriptor = self.provider.descriptor()
        store._connection.execute(
            """
            INSERT OR IGNORE INTO lifecycle_provider_registry
            (workspace_id, provider_id, provider_revision, trust_class,
             supported_operations_json, challenge_algorithm, challenge_ttl_seconds, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                WORKSPACE,
                descriptor["provider_id"],
                descriptor["provider_revision"],
                descriptor["trust_class"],
                descriptor["supported_operations_json"],
                descriptor["challenge_algorithm"],
                descriptor["challenge_ttl_seconds"],
                NOW,
            ),
        )

    def session(self) -> dict:
        return {
            "agent_id": "kimi",
            "project_id": PROJECT,
            "chat_id": CHAT,
            "session_id": SESSION_ID,
            "endpoint_id": "endpoint_codex",
            "binding_id": self.binding_id,
            "binding_generation": 1,
            "repo_targets": ["app"],
            "runtime": {"family": "pi", "session_id": NATIVE, "home": str(self.codex_home)},
        }

    def packet(self, relpath: str, **fm_overrides) -> dict:
        frontmatter = {
            "chat_id": CHAT,
            "from": "codex",
            "sender_agent_id": "codex",
            "to": "kimi",
            "title": "deliver me",
            "priority": "normal",
            "project_id": PROJECT,
            "repo_targets": ["app"],
            "sent_utc": NOW,
            "target_session_id": SESSION_ID,
            "target_binding_id": self.binding_id,
            "target_binding_generation": 1,
        }
        frontmatter.update(fm_overrides)
        lines = ["---"] + [f"{key}: {value}" for key, value in frontmatter.items()]
        # repo_targets must be a JSON list line like real packets
        lines = [
            line if not line.startswith("repo_targets:") else 'repo_targets: ["app"]'
            for line in lines
        ]
        path = self.workspace_root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
        return {"path": relpath, "frontmatter": frontmatter}

    def observation(self, classification: str, *, status_type: str, can_accept):
        return CodexAppServerThreadObservation(
            thread_id=NATIVE,
            codex_home=str(self.codex_home),
            user_agent="llm-collab-pm2-verify/0.146.0-alpha.3.1",
            status_type=status_type,
            can_accept_direct_input=can_accept,
            classification=classification,
        )

    def deliver(self, *, classification=OBSERVATION_ADMISSIBLE, status_type="idle",
                can_accept=True, transport=None, fm_overrides=None):
        transport = transport if transport is not None else FakeTurnTransport()
        message = self.packet("Chats/chat-dir/deliver.md", **(fm_overrides or {}))
        with mock.patch.dict(os.environ, {CANONICAL_CONTROL_ENV: CANONICAL_CONTROL_ENABLED}):
            with LedgerStore.open_writer(self.paths) as store:
                result = deliver_next_turn_idle(
                    store,
                    workspace_root=self.workspace_root,
                    session=self.session(),
                    message=message,
                    subject=self.subject,
                    provider=self.provider,
                    observe=lambda _tid: self.observation(
                        classification, status_type=status_type, can_accept=can_accept
                    ),
                    turn_transport=transport,
                    runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root,
                    observed_at_utc=NOW,
                    correlation_id="corr_deliver",
                    timeout_seconds=0.05,
                )
                receipts = store._connection.execute(
                    "SELECT state, quality FROM canonical_delivery_receipts"
                ).fetchall()
        return result, transport, receipts

    def turn_frames(self, transport) -> list[dict]:
        return [f for f in transport.frames if f["method"] == "turn/start"]

    def test_idle_delivers_exactly_one_turn_and_native_accepted_receipt(self) -> None:
        result, transport, receipts = self.deliver()
        self.assertEqual(result["outcome"], OUTCOME_ACCEPTED)
        self.assertEqual(len(self.turn_frames(transport)), 1)
        frame = self.turn_frames(transport)[0]
        self.assertEqual(frame["params"]["threadId"], NATIVE)
        self.assertEqual(
            [f["method"] for f in transport.frames],
            ["initialize", "thread/resume", "turn/start"],
        )
        self.assertIn(("accepted", "authoritative"), receipts)
        self.assertTrue(result["receipt_id"])

    def test_busy_defers_with_zero_turn_frames_and_deferred_receipt(self) -> None:
        # Load-bearing gate: neutering the admissibility check would let a
        # turn/start frame through and fail this test.
        result, transport, receipts = self.deliver(
            classification=OBSERVATION_BUSY, status_type="active", can_accept=True
        )
        self.assertEqual(result["outcome"], OUTCOME_DEFERRED_BUSY)
        self.assertEqual(self.turn_frames(transport), [])
        self.assertEqual(transport.frames, [])
        self.assertIn(("deferred_busy", "best_effort"), receipts)

    def test_uncertain_observation_injects_nothing(self) -> None:
        result, transport, receipts = self.deliver(
            classification=OBSERVATION_UNCERTAIN, status_type=None, can_accept=None
        )
        self.assertEqual(result["outcome"], OUTCOME_UNCERTAIN)
        self.assertEqual(self.turn_frames(transport), [])
        self.assertEqual(transport.frames, [])
        self.assertIn(("ambiguous", "best_effort"), receipts)

    def test_stale_binding_generation_refuses_before_any_frame_or_attempt(self) -> None:
        transport = FakeTurnTransport()
        with self.assertRaises(LegacyPacketMaterializationRefused):
            self.deliver(transport=transport, fm_overrides={"target_binding_generation": 2})
        self.assertEqual(transport.frames, [])
        with LedgerStore.open_reader(self.paths) as store:
            attempts = store._connection.execute(
                "SELECT count(*) FROM canonical_delivery_attempts"
            ).fetchone()[0]
        self.assertEqual(attempts, 0)

    def test_disabled_gate_dispatches_nothing(self) -> None:
        transport = FakeTurnTransport()
        message = self.packet("Chats/chat-dir/deliver.md")
        with LedgerStore.open_writer(self.paths) as store:
            result = deliver_next_turn_idle(
                store,
                workspace_root=self.workspace_root,
                session=self.session(),
                message=message,
                subject=self.subject,
                provider=self.provider,
                observe=lambda _tid: self.observation(
                    OBSERVATION_ADMISSIBLE, status_type="idle", can_accept=True
                ),
                turn_transport=transport,
                runtime_home=self.runtime_home,
                trusted_project_root=self.trusted_root,
                observed_at_utc=NOW,
                correlation_id="corr_deliver",
                timeout_seconds=0.05,
            )
        self.assertEqual(result["outcome"], OUTCOME_GATE_DISABLED)
        self.assertEqual(transport.frames, [])


    def test_subject_session_identity_split_refuses_before_anything(self) -> None:
        wrong = LifecycleSubject(
            workspace_id=self.subject.workspace_id,
            scope_kind=self.subject.scope_kind,
            scope_identity=self.subject.scope_identity,
            conversation_id=self.subject.conversation_id,
            participant_id=self.subject.participant_id,
            agent_id=self.subject.agent_id,
            endpoint_id=self.subject.endpoint_id,
            native_session_id="native_session_other",
            runtime_instance_id=self.subject.runtime_instance_id,
        )
        transport = FakeTurnTransport()
        message = self.packet("Chats/chat-dir/deliver.md")
        with mock.patch.dict(os.environ, {CANONICAL_CONTROL_ENV: CANONICAL_CONTROL_ENABLED}):
            with LedgerStore.open_writer(self.paths) as store:
                from llm_collab.canonical.codex_delivery import CodexDeliveryError
                with self.assertRaises(CodexDeliveryError):
                    deliver_next_turn_idle(
                        store,
                        workspace_root=self.workspace_root,
                        session=self.session(),
                        message=message,
                        subject=wrong,
                        provider=self.provider,
                        observe=lambda _tid: self.observation(
                            OBSERVATION_ADMISSIBLE, status_type="idle", can_accept=True
                        ),
                        turn_transport=transport,
                        runtime_home=self.runtime_home,
                        trusted_project_root=self.trusted_root,
                        observed_at_utc=NOW,
                        correlation_id="corr_deliver",
                        timeout_seconds=0.05,
                    )
        self.assertEqual(transport.frames, [])
        with LedgerStore.open_reader(self.paths) as store:
            attempts = store._connection.execute(
                "SELECT count(*) FROM canonical_delivery_attempts"
            ).fetchone()[0]
        self.assertEqual(attempts, 0)

    def test_terminal_for_a_different_turn_is_not_acceptance(self) -> None:
        transport = FakeTurnTransport(
            terminal=(
                {"method": "turn/completed", "params": {"turn": {"id": "turn-other", "status": "completed"}}},
            )
        )
        result, transport, receipts = self.deliver(transport=transport)
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertIn(("ambiguous", "best_effort"), receipts)
        self.assertNotIn(("accepted", "authoritative"), receipts)

    def test_null_turn_start_result_is_ambiguous_never_accepted(self) -> None:
        transport = FakeTurnTransport(turn_start_result={"jsonrpc": "2.0", "id": "llm-collab-delivery-3", "result": None})
        result, transport, receipts = self.deliver(transport=transport)
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertIsNone(result["turn_id"])
        self.assertIn(("ambiguous", "best_effort"), receipts)
        self.assertNotIn(("accepted", "authoritative"), receipts)

    def test_failed_turn_after_start_is_not_retry_safe(self) -> None:
        transport = FakeTurnTransport(
            terminal=(
                {"method": "turn/failed", "params": {"turn": {"id": "turn-1", "status": "failed"}}},
            )
        )
        result, transport, receipts = self.deliver(transport=transport)
        self.assertEqual(result["outcome"], "ambiguous")
        states = {state for state, _quality in receipts}
        self.assertNotIn("rejected_before_acceptance", states)

    def test_absolute_deadline_bounds_the_blocking_read(self) -> None:
        import time as _time
        transport = FakeTurnTransport(recv_sleep=0.3)
        started = _time.monotonic()
        result, transport, receipts = self.deliver(transport=transport)
        elapsed = _time.monotonic() - started
        self.assertEqual(result["outcome"], "ambiguous")
        self.assertEqual(transport.recv_calls, 1)
        self.assertLess(elapsed, 0.6)

    def test_attestation_failure_leaves_durable_intent_but_no_receipt(self) -> None:
        def failing_probe(_tid):
            raise RuntimeError("app server unreachable")

        provider = CodexLifecycleProvider(exact_thread_probe=failing_probe)
        transport = FakeTurnTransport()
        message = self.packet("Chats/chat-dir/deliver.md")
        with mock.patch.dict(os.environ, {CANONICAL_CONTROL_ENV: CANONICAL_CONTROL_ENABLED}):
            with LedgerStore.open_writer(self.paths) as store:
                with self.assertRaises(RuntimeError):
                    deliver_next_turn_idle(
                        store,
                        workspace_root=self.workspace_root,
                        session=self.session(),
                        message=message,
                        subject=self.subject,
                        provider=provider,
                        observe=lambda _tid: self.observation(
                            OBSERVATION_ADMISSIBLE, status_type="idle", can_accept=True
                        ),
                        turn_transport=transport,
                        runtime_home=self.runtime_home,
                        trusted_project_root=self.trusted_root,
                        observed_at_utc=NOW,
                        correlation_id="corr_deliver",
                        timeout_seconds=0.05,
                    )
        self.assertEqual(transport.frames, [])
        with LedgerStore.open_reader(self.paths) as store:
            messages = store._connection.execute(
                "SELECT count(*) FROM canonical_messages"
            ).fetchone()[0]
            receipts = store._connection.execute(
                "SELECT count(*) FROM canonical_delivery_receipts"
            ).fetchone()[0]
        # Durable intent (message/delivery/attempt) is the recovery record; no
        # receipt may exist without native evidence.
        self.assertEqual(messages, 1)
        self.assertEqual(receipts, 0)

    def test_probe_failure_fails_closed_before_any_frame(self) -> None:
        def failing_probe(_tid):
            raise RuntimeError("app server unreachable")

        provider = CodexLifecycleProvider(exact_thread_probe=failing_probe)
        transport = FakeTurnTransport()
        message = self.packet("Chats/chat-dir/deliver.md")
        with mock.patch.dict(os.environ, {CANONICAL_CONTROL_ENV: CANONICAL_CONTROL_ENABLED}):
            with LedgerStore.open_writer(self.paths) as store:
                with self.assertRaises(RuntimeError):
                    deliver_next_turn_idle(
                        store,
                        workspace_root=self.workspace_root,
                        session=self.session(),
                        message=message,
                        subject=self.subject,
                        provider=provider,
                        observe=lambda _tid: self.observation(
                            OBSERVATION_ADMISSIBLE, status_type="idle", can_accept=True
                        ),
                        turn_transport=transport,
                        runtime_home=self.runtime_home,
                        trusted_project_root=self.trusted_root,
                        observed_at_utc=NOW,
                        correlation_id="corr_deliver",
                        timeout_seconds=0.05,
                    )
        self.assertEqual(transport.frames, [])


if __name__ == "__main__":
    unittest.main()
