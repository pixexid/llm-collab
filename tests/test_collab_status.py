from __future__ import annotations

import hashlib
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import collab_status
import llm_collab.ledger.store as store_module
from llm_collab.canonical import (
    append_receipt,
    create_bound_attempt,
    create_deliveries,
    create_or_return_equivalent,
)
from llm_collab.ledger import LedgerPaths, LedgerStore


NOW = "2026-07-22T00:00:00+00:00"
PROJECT = "amiga"
REVISION_HASH = "a" * 64
REVISION = "sha256:" + REVISION_HASH
SAFE_VERSION = (3, 51, 3)


def record_registry(store: LedgerStore) -> None:
    store.record_registry_snapshot(
        workspace_id="ws_alpha",
        registry_revision=REVISION,
        registry_source_sha256=REVISION_HASH,
        captured_at_utc=NOW,
        workspace_snapshot_json=json.dumps(
            {"workspace_id": "ws_alpha", "projects": [PROJECT, "nuvyr"]}
        ),
        project_snapshots={
            PROJECT: json.dumps({"project_id": PROJECT}),
            "nuvyr": json.dumps({"project_id": "nuvyr"}),
        },
        source_snapshots={PROJECT: {}, "nuvyr": {}},
    )


def seed_binding(store: LedgerStore) -> None:
    store._connection.execute(
        """
        INSERT INTO lifecycle_provider_registry
        (workspace_id, provider_id, provider_revision, trust_class,
         supported_operations_json, challenge_algorithm, challenge_ttl_seconds, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ws_alpha", "provider_codex", "revision_1", "managed", '["attach"]', "sha256", 60, NOW),
    )
    store._connection.execute(
        """
        INSERT INTO conversation_participants
        (workspace_id, scope_kind, scope_identity, conversation_id, participant_id, agent_id, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("ws_alpha", "project", PROJECT, "CHAT-SAMEID", "participant_claude", "agent_claude", NOW),
    )
    store._connection.execute(
        """
        INSERT INTO conversation_bindings
        (workspace_id, scope_kind, scope_identity, conversation_id, participant_id,
         binding_id, generation, state, mutation_capable, provider_id, provider_revision,
         endpoint_id, session_ref_id, native_session_id, runtime_instance_id, registered_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ws_alpha", "project", PROJECT, "CHAT-SAMEID", "participant_claude",
            "binding_one", 1, "active", 1, "provider_codex", "revision_1",
            "endpoint_claude_desktop", "session_ref_one", "native_session_one", "runtime_one", NOW,
        ),
    )


def delivery_fixture(store: LedgerStore) -> tuple[str, str]:
    message_id, _created = create_or_return_equivalent(
        store,
        workspace_id="ws_alpha",
        scope_kind="project",
        scope_identity=PROJECT,
        sender_agent_id="agent_codex",
        dedupe_key="status-test",
        body=b"hello",
        recipients=["agent_claude"],
        registry_revision=REVISION,
        created_at_utc=NOW,
        title="Status test",
        ttl_seconds=0,
        ack_policy="required",
        artifacts=[],
        priority="normal",
        tags=[],
        chat_link="CHAT-SAMEID",
        task_link=None,
    )
    ((delivery_id, _created),) = create_deliveries(
        store,
        workspace_id="ws_alpha",
        scope_kind="project",
        scope_identity=PROJECT,
        message_id=message_id,
        routes=[("agent_claude", "endpoint_claude_desktop")],
        now_epoch_ms=1_000,
        created_at_utc=NOW,
    )
    return message_id, delivery_id


def state_evidence(*, message_id: str, delivery_id: str, attempt_id: str) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "project", "project_id": PROJECT},
        "evidence_id": "evidence_status_test",
        "evidence_kind": "native_delivery_state",
        "quality": "best_effort",
        "state": "pull_pending",
        "authority": {
            "authority_kind": "native_runtime",
            "identity": "agent_claude",
            "implementation_revision": "rev_v1",
            "capability_profile_id": "profile_claude",
            "capability_profile_revision": "profile_rev_v1",
        },
        "subject": {
            "message_id": message_id,
            "delivery_id": delivery_id,
            "attempt_id": attempt_id,
            "endpoint_id": "endpoint_claude_desktop",
            "session_ref_id": "session_ref_one",
        },
        "correlation_id": "status_test",
        "observed_at_utc": NOW,
    }
    evidence["integrity"] = "sha256:" + hashlib.sha256(
        store_module._canonical_json_bytes(evidence)
    ).hexdigest()
    return evidence


class CollabStatusTest(unittest.TestCase):
    def test_renders_seeded_ledger_without_mutating_reader(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp, patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        ):
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as writer:
                record_registry(writer)
                seed_binding(writer)
                message_id, delivery_id = delivery_fixture(writer)
                bound = create_bound_attempt(
                    writer,
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_000,
                    created_at_utc=NOW,
                    conversation_id="CHAT-SAMEID",
                    participant_id="participant_claude",
                    expected_binding_id="binding_one",
                    expected_generation=1,
                )
                self.assertTrue(bound["created"])
                attempt_id = str(bound["attempt_id"])
                append_receipt(
                    writer,
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    evidence=state_evidence(
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                    ),
                    session_ref_id="session_ref_one",
                    created_at_utc=NOW,
                )

            with LedgerStore.open_reader(paths) as reader:
                writes: list[str] = []

                def trace(statement: str) -> None:
                    operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
                    if operation in {"INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN", "COMMIT", "ROLLBACK"}:
                        writes.append(statement)

                reader._connection.set_trace_callback(trace)
                try:
                    status = collab_status.render_status(
                        reader,
                        PROJECT,
                        now=datetime(2026, 7, 22, 0, 0, 10, tzinfo=timezone.utc),
                    )
                finally:
                    reader._connection.set_trace_callback(None)

            self.assertEqual(writes, [])
            self.assertEqual(status["project"], PROJECT)
            self.assertEqual(status["active_bindings"][0]["agent"], "claude")
            self.assertEqual(status["active_bindings"][0]["binding"], "binding_one")
            self.assertEqual(status["pending_delivery_attempts"], {"binding_one": 1})
            self.assertEqual(status["recent_events"][0]["kind"], "dead_letter")
            self.assertEqual(status["recent_events"][0]["state"], "pull_pending")


if __name__ == "__main__":
    unittest.main()
