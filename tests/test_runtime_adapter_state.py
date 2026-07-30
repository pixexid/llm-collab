import ast
import hashlib
import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_collab import runtime_adapter_state as state
from llm_collab import runtime_adapter_lifecycle
from llm_collab.runtime_adapter_redaction import RedactedDocument, redact_document


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_state.py"
_MISSING = object()


def _release_with_review(db_path, record_id, redacted):
    payload = redacted.as_dict()
    payload["review_references"] = [{
        "manifest_id": payload["manifest_id"],
        "manifest_revision": payload["manifest_revision"],
        "capability_set_id": payload["capability_set_id"],
        "capability_set_revision": payload["capability_set_revision"],
        "diagnostic_id": "diagnostic.test",
    }]
    reviewed = redact_document(payload)
    if not isinstance(reviewed, RedactedDocument):
        raise AssertionError(reviewed)
    state.record_release_reviewed(db_path, record_id, reviewed)
    return state._record_release(db_path, record_id, redacted)


state._record_release = state.record_release
state.record_release = _release_with_review


class RuntimeAdapterStateTests(unittest.TestCase):
    def test_quarantine_store_uses_redacted_document_and_folds_release_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            opened = _redacted(request_id="attempt-1", fault="ADAPTER_UNHEALTHY")
            record_id = state.record_quarantine_opened(db_path, opened)
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))

            current = state.read_record(db_path, record_id)

            self.assertTrue(current.opened)
            self.assertTrue(current.recovery_authorized)
            self.assertEqual(current.unresolved_attempts, ())
            self.assertEqual(current.valid_health_count, state.FRESH_HEALTHY_SEQUENCE_LENGTH)
            self.assertFalse(current.release_event_seen)
            self.assertFalse(current.released)

            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))
            released = state.read_record(db_path, record_id)

            self.assertTrue(released.release_event_seen)
            self.assertTrue(released.released)

    def test_read_record_exposes_the_exact_stored_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            opened = _redacted(request_id="attempt-1", fault="ADAPTER_UNHEALTHY")
            record_id = state.record_quarantine_opened(db_path, opened)
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            # A second recovery_authorized for the same attempt is stored as a
            # duplicate row that the fold dedupes (it must not double-advance);
            # the journal must still carry it exactly as recorded.
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))

            current = state.read_record(db_path, record_id)

            with sqlite3.connect(db_path) as conn:
                raw = conn.execute(
                    """
                    SELECT event_sequence, event_kind, payload_json
                    FROM runtime_adapter_events
                    WHERE record_id = ?
                    ORDER BY event_sequence
                    """,
                    (record_id,),
                ).fetchall()
            self.assertEqual(len(raw), 3)
            self.assertEqual(
                current.journal,
                tuple((int(seq), str(kind), json.loads(payload)["occurrence_at_utc"]) for seq, kind, payload in raw),
            )
            self.assertEqual(
                [kind for _, kind, _ in current.journal],
                ["quarantine_opened", "recovery_authorized", "recovery_authorized"],
            )
            # The fold still sees one recovery (no double-advance from the duplicate):
            # exactly two folded events, while the stored journal carries all three rows.
            self.assertTrue(current.recovery_authorized)
            self.assertEqual(current.event_count, 2)
            self.assertEqual(len(current.journal), 3)
            for sequence, kind, occurrence in current.journal:
                self.assertIsInstance(sequence, int)
                self.assertIn(kind, state.EVENT_KINDS)
                self.assertTrue(occurrence.endswith("Z"))

            self.assertEqual(
                current.unresolved_attempt_tuples,
                (("attempt-1", "delivery_attempt-1", "attempt_attempt-1"),),
            )
            self.assertEqual(current.incident_id, "incident.alpha")
            self.assertTrue({kind for _, kind, _ in current.journal} <= state.EVENT_KINDS)

    def test_journal_read_is_bounded_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(
                db_path, _redacted(request_id="attempt-1", fault="ADAPTER_UNHEALTHY"))
            payload_json, digest = _payload_and_digest(_redacted(request_id="health-1"))
            with sqlite3.connect(db_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO runtime_adapter_events
                        (record_id, event_kind, payload_json, payload_sha256)
                    VALUES (?, 'valid_health', ?, ?)
                    """,
                    [(record_id, payload_json, digest)]
                    * state.MAX_ADAPTER_STATE_EVENTS,
                )

            with self.assertRaises(state.AdapterStateIntegrityError):
                state.read_record(db_path, record_id)

    def test_journal_read_refuses_a_non_text_stored_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(
                db_path, _redacted(request_id="attempt-1", fault="ADAPTER_UNHEALTHY"))
            payload_json, digest = _payload_and_digest(_redacted(request_id="health-1"))
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO runtime_adapter_events
                        (record_id, event_kind, payload_json, payload_sha256, append_time_utc)
                    VALUES (?, 'valid_health', ?, ?, ?)
                    """,
                    (record_id, payload_json.replace('2026-07-30T00:00:00Z', 'not-a-timestamp'), digest, b"blob-timestamp"),
                )

            with self.assertRaises(state.AdapterStateIntegrityError):
                state.read_record(db_path, record_id)

    def test_release_is_not_derived_from_preconditions_or_partial_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            opened = _redacted(request_id="attempt-1")
            record_id = state.record_quarantine_opened(db_path, opened)
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH - 1):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))
            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))

            current = state.read_record(db_path, record_id)

            self.assertTrue(current.release_event_seen)
            self.assertFalse(current.released)

    def test_release_event_before_preconditions_does_not_become_retroactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            opened = _redacted(request_id="attempt-1")
            record_id = state.record_quarantine_opened(db_path, opened)
            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))

            current = state.read_record(db_path, record_id)

            self.assertTrue(current.release_event_seen)
            self.assertFalse(current.released)

    def test_later_distinct_release_event_can_release_after_early_invalid_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_release(db_path, record_id, _redacted(request_id="release-early"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))
            state.record_release(db_path, record_id, _redacted(request_id="release-late"))

            self.assertTrue(state.read_record(db_path, record_id).released)

    def test_repeated_authorization_requires_new_handshake(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="auth-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="handshake-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="auth-2"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))

            current = state.read_record(db_path, record_id)

            self.assertFalse(current.fresh_handshake)
            self.assertEqual(current.valid_health_count, 0)

    def test_repeated_handshake_starts_fresh_health_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="auth-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="handshake-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH - 1):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"old-health-{index}"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="handshake-2"))
            state.record_valid_health(db_path, record_id, _redacted(request_id="new-health-1"))
            state.record_release(db_path, record_id, _redacted(request_id="release-1"))

            current = state.read_record(db_path, record_id)

            self.assertEqual(current.valid_health_count, 1)
            self.assertFalse(current.released)

    def test_reconciliation_before_handshake_does_not_apply_retroactively(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_valid_health(db_path, record_id, _redacted(request_id="health-1"))

            current = state.read_record(db_path, record_id)

            self.assertTrue(current.opened)
            self.assertTrue(current.recovery_authorized)
            self.assertTrue(current.fresh_handshake)
            self.assertEqual(current.valid_health_count, 1)
            self.assertEqual(current.unresolved_attempts, ('{"request_id":"attempt-1"}',))

    def test_countable_health_events_use_sequence_identity_not_payload_equality(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            opened = _redacted(request_id="attempt-1")
            record_id = state.record_quarantine_opened(db_path, opened)
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))
            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))

            current = state.read_record(db_path, record_id)

            self.assertEqual(current.valid_health_count, state.FRESH_HEALTHY_SEQUENCE_LENGTH)
            self.assertTrue(current.released)

    def test_same_event_occurrence_does_not_double_advance_fold(self):
        payload_json, payload_sha256 = _payload_and_digest(_redacted(request_id="attempt-1"))
        health_json, health_sha256 = _payload_and_digest(_redacted(request_id="health-1"))
        record_id = state.record_id_for(_redacted(request_id="attempt-1"))

        current = state._fold(
            record_id,
            [
                (1, state.EVENT_QUARANTINE_OPENED, payload_json, payload_sha256),
                (2, state.EVENT_RECOVERY_AUTHORIZED, payload_json, payload_sha256),
                (3, state.EVENT_ATTEMPT_RECONCILED, payload_json, payload_sha256),
                (4, state.EVENT_FRESH_HANDSHAKE, payload_json, payload_sha256),
                (5, state.EVENT_VALID_HEALTH, health_json, health_sha256),
                (5, state.EVENT_VALID_HEALTH, health_json, health_sha256),
            ],
        )

        self.assertEqual(current.valid_health_count, 1)

    def test_replayed_health_occurrence_does_not_release_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            health = _redacted(request_id="health-1")
            for _ in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, health)
            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))

            current = state.read_record(db_path, record_id)

            self.assertEqual(current.valid_health_count, 1)
            self.assertTrue(current.release_event_seen)
            self.assertFalse(current.released)

    def test_health_without_request_id_is_uncountable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            for _ in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, record_id, _redacted())
            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))

            current = state.read_record(db_path, record_id)

            self.assertEqual(current.valid_health_count, 1)
            self.assertTrue(current.release_event_seen)
            self.assertFalse(current.released)

    def test_new_quarantine_occurrence_gets_new_record_without_resetting_old_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            first_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, first_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, first_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, first_id, _redacted(request_id="attempt-1"))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(db_path, first_id, _redacted(request_id=f"health-{index}"))
            state.record_release(db_path, first_id, _redacted(request_id="attempt-1"))

            second_id = state.record_quarantine_opened(
                db_path, _redacted(incident_id="incident.beta", request_id="attempt-2")
            )

            self.assertNotEqual(first_id, second_id)
            self.assertTrue(state.read_record(db_path, first_id).released)
            second = state.read_record(db_path, second_id)
            self.assertTrue(second.opened)
            self.assertFalse(second.released)
            self.assertEqual(second.unresolved_attempts, ('{"request_id":"attempt-2"}',))

    def test_record_identity_requires_clause_12_fields_and_scope_project_id_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            with self.assertRaises(ValueError):
                state.record_quarantine_opened(db_path, _redacted(adapter_revision=_MISSING, request_id="attempt-1"))
            with self.assertRaises(ValueError):
                state.record_quarantine_opened(
                    db_path,
                    _redacted(scope_identity="workspace:ws_alpha", project_id="amiga", request_id="attempt-1"),
                )
            with self.assertRaises(ValueError):
                state.record_quarantine_opened(
                    db_path,
                    _redacted(scope_identity="workspace:ws_alpha|project:other", request_id="attempt-1"),
                )
            project_id = state.record_quarantine_opened(
                db_path,
                _redacted(scope_identity="workspace:ws_alpha|project:amiga", request_id="attempt-1"),
            )
            self.assertTrue(project_id.startswith("adapter_record_"))
            workspace_id = state.record_quarantine_opened(
                db_path,
                _redacted(
                    incident_id="incident.workspace",
                    scope_identity="workspace:ws_alpha",
                    project_id=_MISSING,
                    request_id="attempt-1",
                ),
            )
            self.assertTrue(workspace_id.startswith("adapter_record_"))

    def test_follow_on_events_must_match_opening_record_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            with self.assertRaises(ValueError):
                state.record_recovery_authorized(
                    db_path,
                    record_id,
                    _redacted(adapter_id="adapter.other", request_id="attempt-1"),
                )

    def test_update_and_delete_are_refused_by_triggers_and_no_status_column_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            with sqlite3.connect(db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(runtime_adapter_events)")}
                self.assertNotIn("status", columns)
                self.assertIn("append_time_utc", columns)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE runtime_adapter_events SET event_kind = ? WHERE record_id = ?",
                        (state.EVENT_RELEASED, record_id),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "DELETE FROM runtime_adapter_events WHERE record_id = ?",
                        (record_id,),
                    )

    def test_direct_sql_shape_checks_reject_junk_record_id_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            state.initialize_store(db_path)
            valid_record_id = "adapter_record_" + "a" * 64
            valid_payload_json = json.dumps({"adapter_id": "adapter.alpha"})
            valid_sha = "b" * 64
            insert_sql = """
                INSERT INTO runtime_adapter_events
                    (record_id, event_kind, payload_json, payload_sha256)
                VALUES (?, ?, ?, ?)
            """
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    insert_sql,
                    (valid_record_id, state.EVENT_QUARANTINE_OPENED, valid_payload_json, valid_sha),
                )
                for bad_record_id, bad_sha in (
                    (valid_record_id, "a!!!not-a-digest"),
                    (valid_record_id, "a" + "\x00" + ("0" * 62)),
                    ("adapter_record_a../../etc", valid_sha),
                ):
                    with self.subTest(record_id=bad_record_id, sha=bad_sha):
                        with self.assertRaises(sqlite3.IntegrityError):
                            conn.execute(
                                insert_sql,
                                (bad_record_id, state.EVENT_QUARANTINE_OPENED, valid_payload_json, bad_sha),
                            )

    def test_read_fails_closed_for_missing_store_and_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            with self.assertRaises(state.AdapterStateStoreError):
                state.read_record(db_path, "adapter_record_" + "a" * 64)

            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO runtime_adapter_events
                        (record_id, event_kind, payload_json, payload_sha256)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        state.EVENT_RELEASED,
                        json.dumps({"request_id": "attempt-1"}),
                        "b" * 64,
                    ),
                )
            with self.assertRaises(state.AdapterStateIntegrityError):
                state.read_record(db_path, record_id)

    def test_release_sequence_length_is_independent_of_lifecycle_failure_threshold(self):
        with mock.patch.object(
            runtime_adapter_lifecycle,
            "HEALTH_FAILURE_THRESHOLD",
            state.FRESH_HEALTHY_SEQUENCE_LENGTH + 1,
        ):
            importlib.reload(state)
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "adapter-state.sqlite"
                    opened = _redacted(request_id="attempt-1")
                    record_id = state.record_quarantine_opened(db_path, opened)
                    state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
                    state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
                    state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
                    for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                        state.record_valid_health(db_path, record_id, _redacted(request_id=f"health-{index}"))
                    state.record_release_reviewed(db_path, record_id, _reviewed(_redacted(request_id="attempt-1")))
                    state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))

                    self.assertTrue(state.read_record(db_path, record_id).released)
            finally:
                importlib.reload(state)

    def test_raw_mapping_cannot_reach_durable_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TypeError):
                state.record_quarantine_opened(Path(tmp) / "state.sqlite", {"adapter_id": "adapter.alpha"})

    def test_canonical_database_bytes_are_untouched_by_every_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "adapter-state.sqlite"
            canonical = tmp_path / "canonical.sqlite"
            canonical.write_bytes(b"canonical-bytes")
            before = canonical.read_bytes()
            record_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-1"))
            state.record_recovery_authorized(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_fresh_handshake(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_attempt_reconciled(db_path, record_id, _redacted(request_id="attempt-1"))
            state.record_valid_health(db_path, record_id, _redacted(request_id="health-1"))
            state.record_release(db_path, record_id, _redacted(request_id="attempt-1"))
            state.read_record(db_path, record_id)

            self.assertEqual(canonical.read_bytes(), before)

    def test_import_direction_and_gate_non_binding_are_ast_proven(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        imports = _imported_modules(tree)
        forbidden = {
            "llm_collab.canonical",
            "llm_collab.compatibility",
            "llm_collab.daemon",
            "llm_collab.inbox",
            "llm_collab.ledger",
            "llm_collab.project_issue_queue",
            "llm_collab.runtime_adapter_supervisor",
            "llm_collab.task_contract",
        }
        self.assertTrue(forbidden.isdisjoint(imports))
        self.assertIn("llm_collab.runtime_adapter_redaction", imports)
        self.assertNotIn("llm_collab.runtime_adapter_lifecycle", imports)
        for relative in (
            "llm_collab/runtime_adapter_redaction.py",
            "llm_collab/runtime_adapter_lifecycle.py",
        ):
            imported = _imported_modules(ast.parse((ROOT / relative).read_text(encoding="utf-8")))
            self.assertNotIn("llm_collab.runtime_adapter_state", imported)

    def test_public_write_functions_accept_redacted_document_not_mapping_or_dict(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        public_writes = {
            "record_quarantine_opened",
            "record_recovery_authorized",
            "record_attempt_reconciled",
            "record_fresh_handshake",
            "record_valid_health",
            "record_release",
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in public_writes:
                found.add(node.name)
                annotations = {
                    arg.arg: ast.unparse(arg.annotation) if arg.annotation is not None else ""
                    for arg in node.args.args
                }
                self.assertEqual(annotations.get("redacted"), "RedactedDocument")
                self.assertNotIn("Mapping", annotations.values())
                self.assertNotIn("dict", annotations.values())
        self.assertEqual(found, public_writes)

    def test_open_v1_legacy_incident_identity_blocks_upgrade_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            legacy_document = _legacy_document("incident.legacy", with_attempt=False)
            legacy_record_id, legacy_rows = _legacy_rows(
                legacy_document, ["quarantine_opened"], legacy_incident="incident legacy"
            )
            _create_v1_database(db_path, legacy_record_id, legacy_rows)

            with self.assertRaisesRegex(
                state.AdapterStateStoreError,
                rf"migration blocked for open v1 record {legacy_record_id}",
            ):
                state.initialize_store(db_path)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runtime_adapter_events").fetchone()[0], 1
                )

    def test_open_v1_v2_valid_identity_migrates_and_can_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            legacy_document = _legacy_document("incident.valid", with_attempt=False)
            legacy_record_id, legacy_rows = _legacy_rows(legacy_document, ["quarantine_opened"])
            _create_v1_database(db_path, legacy_record_id, legacy_rows)

            state.initialize_store(db_path)
            state.record_recovery_authorized(
                db_path,
                legacy_record_id,
                _redacted(incident_id="incident.valid", attempts=[]),
            )
            self.assertTrue(state.read_record(db_path, legacy_record_id).recovery_authorized)

    def test_open_v1_legacy_incident_blocks_upgrade_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            legacy_document = _legacy_document("incident.open")
            legacy_record_id, legacy_rows = _legacy_rows(legacy_document, ["quarantine_opened"])
            _create_v1_database(db_path, legacy_record_id, legacy_rows)

            with self.assertRaisesRegex(
                state.AdapterStateStoreError,
                rf"migration blocked for open v1 record {legacy_record_id}",
            ):
                state.initialize_store(db_path)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runtime_adapter_events").fetchone()[0], 1
                )
                self.assertEqual(
                    conn.execute("PRAGMA table_info(runtime_adapter_events)").fetchall()[-1][1],
                    "append_time_utc",
                )

    def test_reconciled_v1_legacy_tuple_migrates_and_can_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            legacy_document = _legacy_document("incident.reconciled")
            kinds = [
                "quarantine_opened",
                "recovery_authorized",
                "fresh_handshake",
                "attempt_reconciled",
            ]
            legacy_record_id, legacy_rows = _legacy_rows(legacy_document, kinds)
            _create_v1_database(db_path, legacy_record_id, legacy_rows)

            state.initialize_store(db_path)
            migrated = state.read_record(db_path, legacy_record_id)
            self.assertEqual(migrated.unresolved_attempt_tuples, ())
            self.assertEqual(
                migrated.reconciled_attempt_tuples,
                (("orig-legacy", "delivery_a", "attempt_a"),),
            )
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(
                    db_path,
                    legacy_record_id,
                    _redacted(
                        incident_id="incident.reconciled",
                        attempts=[],
                        occurrence_at_utc=f"2026-07-30T00:01:0{index}Z",
                    ),
                )
            state.record_release_reviewed(
                db_path,
                legacy_record_id,
                _reviewed(_redacted(incident_id="incident.reconciled", attempts=[])),
            )
            state._record_release(
                db_path,
                legacy_record_id,
                _redacted(incident_id="incident.reconciled", attempts=[]),
            )
            self.assertTrue(state.read_record(db_path, legacy_record_id).released)

    def test_genuine_old_v1_payload_migrates_and_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            legacy_record_id, legacy_rows = _genuine_v1_rows("attempt-old")
            _create_v1_database(db_path, legacy_record_id, legacy_rows)

            state.initialize_store(db_path)
            migrated = state.read_record(db_path, legacy_record_id)

            self.assertTrue(migrated.released)
            self.assertEqual(migrated.incident_id, "")
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT DISTINCT event_schema_version FROM runtime_adapter_events"
                    ).fetchall(),
                    [(1,)],
                )

    def test_second_migration_after_v2_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            record_id = state.record_quarantine_opened(
                db_path, _redacted(incident_id="incident.v2", attempts=[])
            )
            with sqlite3.connect(db_path) as conn:
                before = conn.execute(
                    "SELECT event_schema_version FROM runtime_adapter_events WHERE record_id = ?",
                    (record_id,),
                ).fetchall()
                state._migrate_schema_v1_to_v2(conn)
                after = conn.execute(
                    "SELECT event_schema_version FROM runtime_adapter_events WHERE record_id = ?",
                    (record_id,),
                ).fetchall()

            self.assertEqual(before, [(2,)])
            self.assertEqual(after, before)

    def test_migration_budget_is_cumulative_across_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            first_id, first_rows = _genuine_v1_rows("attempt-first")
            second_id, second_rows = _genuine_v1_rows("attempt-second")
            _create_v1_database(db_path, first_id, first_rows + [])
            with sqlite3.connect(db_path) as conn:
                for kind, payload in second_rows:
                    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    conn.execute(
                        """
                        INSERT INTO runtime_adapter_events
                            (record_id, event_kind, payload_json, payload_sha256)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            second_id,
                            kind,
                            payload_json,
                            hashlib.sha256(payload_json.encode()).hexdigest(),
                        ),
                    )
                conn.commit()

            with mock.patch.object(state, "MAX_ADAPTER_STATE_EVENTS", len(first_rows)):
                with self.assertRaisesRegex(
                    state.AdapterStateStoreError,
                    r"more than .* total v1 events",
                ):
                    state.initialize_store(db_path)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_released_v1_legacy_incident_upgrades_and_remains_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            legacy_document = _legacy_document("incident.released")
            kinds = [
                "quarantine_opened",
                "recovery_authorized",
                "fresh_handshake",
                "attempt_reconciled",
                "valid_health",
                "valid_health",
                "valid_health",
                "released",
            ]
            legacy_record_id, legacy_rows = _legacy_rows(legacy_document, kinds)
            _create_v1_database(db_path, legacy_record_id, legacy_rows)

            state.initialize_store(db_path)
            legacy_state = state.read_record(db_path, legacy_record_id)
            self.assertTrue(legacy_state.released)
            self.assertEqual(
                legacy_state.unresolved_attempt_tuples,
                (),
            )
            self.assertEqual(
                legacy_state.reconciled_attempt_tuples,
                (("orig-legacy", "delivery_a", "attempt_a"),),
            )

            base = {"incident_id": "incident.upgrade", "attempts": []}
            record_id = state.record_quarantine_opened(db_path, "incident.upgrade", _redacted(**base))
            state.record_health_failed(
                db_path,
                record_id,
                _redacted(**{**base, "health_failure_reason": "HEALTH_TIMEOUT"}),
            )
            state.record_release_reviewed(db_path, record_id, _reviewed(_redacted(**base)))
            with sqlite3.connect(db_path) as conn:
                kinds = [row[0] for row in conn.execute(
                    "SELECT event_kind FROM runtime_adapter_events WHERE record_id = ? ORDER BY event_sequence",
                    (record_id,),
                )]
            self.assertEqual(kinds, ["quarantine_opened", "health_failed", "release_reviewed"])

    def test_released_incident_rejects_later_disqualifying_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            base = {"incident_id": "incident.latch", "attempts": []}
            record_id = state.record_quarantine_opened(db_path, "incident.latch", _redacted(**base))
            state.record_recovery_authorized(db_path, record_id, _redacted(**base))
            state.record_fresh_handshake(db_path, record_id, _redacted(**base))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(
                    db_path,
                    record_id,
                    _redacted(**{**base, "occurrence_at_utc": f"2026-07-30T00:00:0{index}Z"}),
                )
            state.record_release_reviewed(db_path, record_id, _reviewed(_redacted(**base)))
            state._record_release(db_path, record_id, _redacted(**base))
            with self.assertRaisesRegex(ValueError, "released adapter incident"):
                state.record_health_failed(
                    db_path,
                    record_id,
                    _redacted(**{**base, "health_failure_reason": "HEALTH_TIMEOUT"}),
                )
            current = state.read_record(db_path, record_id)
            self.assertTrue(current.released)
            self.assertEqual(current.health_failure_count, 0)

    def test_contract_incident_has_two_attempts_and_release_requires_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            attempts = [
                {"original_request_id": "orig-a", "delivery_id": "delivery_aaa", "attempt_id": "attempt_aaa"},
                {"original_request_id": "orig-b", "delivery_id": "delivery_bbb", "attempt_id": "attempt_bbb"},
            ]
            opened = _redacted(incident_id="incident.contract", attempts=attempts, request_id="orig-a")
            record_id = state.record_quarantine_opened(db_path, "incident.contract", opened)
            state.record_recovery_authorized(db_path, record_id, _redacted(incident_id="incident.contract", attempts=[]))
            state.record_fresh_handshake(db_path, record_id, _redacted(incident_id="incident.contract", attempts=[]))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(
                    db_path, record_id,
                    _redacted(
                        incident_id="incident.contract",
                        attempts=[],
                        request_id=f"health-{index}",
                        occurrence_at_utc=f"2026-07-30T00:00:0{index}Z",
                    ),
                )
            state.record_attempt_reconciled(
                db_path, record_id,
                _redacted(incident_id="incident.contract", attempts=[attempts[0]]),
            )
            state.record_release_reviewed(
                db_path, record_id,
                _reviewed(_redacted(incident_id="incident.contract", attempts=[])),
            )
            state._record_release(db_path, record_id, _redacted(incident_id="incident.contract", attempts=[]))
            blocked = state.read_record(db_path, record_id)
            self.assertFalse(blocked.released)
            self.assertEqual(blocked.unresolved_attempt_tuples, (('orig-b', 'delivery_bbb', 'attempt_bbb'),))

            state.record_attempt_reconciled(
                db_path, record_id,
                _redacted(incident_id="incident.contract", attempts=[attempts[1]]),
            )
            state._record_release(
                db_path, record_id,
                _redacted(incident_id="incident.contract", attempts=[], occurrence_at_utc="2026-07-30T00:00:01Z"),
            )
            self.assertTrue(state.read_record(db_path, record_id).released)

    def test_contract_incident_id_is_stable_when_attempt_set_changes(self):
        first = _redacted(incident_id="incident.stable", attempts=[])
        second = _redacted(
            incident_id="incident.stable",
            attempts=[{"original_request_id": "orig", "delivery_id": "delivery_xxx", "attempt_id": "attempt_xxx"}],
        )
        self.assertEqual(state.record_id_for(first), state.record_id_for(second))

    def test_contract_health_failed_resets_valid_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            base = {"incident_id": "incident.health", "attempts": []}
            record_id = state.record_quarantine_opened(db_path, "incident.health", _redacted(**base))
            state.record_recovery_authorized(db_path, record_id, _redacted(**base))
            state.record_fresh_handshake(db_path, record_id, _redacted(**base))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(
                    db_path, record_id,
                    _redacted(**{**base, "occurrence_at_utc": f"2026-07-30T00:00:0{index}Z"}),
                )
            state.record_health_failed(
                db_path, record_id,
                _redacted(**{**base, "health_failure_reason": "HEALTH_TIMEOUT", "occurrence_at_utc": "2026-07-30T00:00:10Z"}),
            )
            current = state.read_record(db_path, record_id)
            self.assertEqual(current.valid_health_count, 0)
            self.assertFalse(current.fresh_handshake)
            self.assertEqual(current.health_failure_count, 1)

    def test_contract_mismatched_review_reference_cannot_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            base = {"incident_id": "incident.review", "attempts": []}
            record_id = state.record_quarantine_opened(db_path, "incident.review", _redacted(**base))
            state.record_recovery_authorized(db_path, record_id, _redacted(**base))
            state.record_fresh_handshake(db_path, record_id, _redacted(**base))
            for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                state.record_valid_health(
                    db_path, record_id,
                    _redacted(**{**base, "occurrence_at_utc": f"2026-07-30T00:00:1{index}Z"}),
                )
            bad = _redacted(**base)
            bad_payload = bad.as_dict()
            bad_payload["review_references"] = [{
                "manifest_id": "wrong-manifest",
                "manifest_revision": bad_payload["manifest_revision"],
                "capability_set_id": bad_payload["capability_set_id"],
                "capability_set_revision": bad_payload["capability_set_revision"],
                "diagnostic_id": "diagnostic.bad",
            }]
            bad = redact_document(bad_payload)
            self.assertIsInstance(bad, RedactedDocument)
            with self.assertRaises(ValueError):
                state.record_release_reviewed(db_path, record_id, bad)
            state._record_release(db_path, record_id, _redacted(**base))
            self.assertFalse(state.read_record(db_path, record_id).released)

    def test_contract_release_prerequisite_removal_is_mutation_proven(self):
        for missing in ("review", "reconcile", "handshake", "health"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "adapter-state.sqlite"
                attempt = {"original_request_id": "orig", "delivery_id": "delivery_orig", "attempt_id": "attempt_orig"}
                base = {"incident_id": "incident-{0}".format(missing), "attempts": [attempt]}
                record_id = state.record_quarantine_opened(db_path, base["incident_id"], _redacted(**base))
                state.record_recovery_authorized(db_path, record_id, _redacted(**{**base, "attempts": []}))
                if missing != "handshake":
                    state.record_fresh_handshake(db_path, record_id, _redacted(**{**base, "attempts": []}))
                if missing != "reconcile":
                    state.record_attempt_reconciled(db_path, record_id, _redacted(**{**base, "attempts": [attempt]}))
                if missing != "health":
                    for index in range(state.FRESH_HEALTHY_SEQUENCE_LENGTH):
                        state.record_valid_health(
                            db_path, record_id,
                            _redacted(**{**base, "attempts": [], "occurrence_at_utc": f"2026-07-30T00:00:2{index}Z"}),
                        )
                if missing != "review":
                    state.record_release_reviewed(db_path, record_id, _reviewed(_redacted(**{**base, "attempts": []})))
                state._record_release(db_path, record_id, _redacted(**{**base, "attempts": []}))
                self.assertFalse(state.read_record(db_path, record_id).released)

    def test_contract_secret_input_never_reaches_persisted_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "adapter-state.sqlite"
            payload = _redacted(incident_id="incident.secret", attempts=[], raw_payload="Bearer secret-token", authorization="secret")
            record_id = state.record_quarantine_opened(db_path, "incident.secret", payload)
            with sqlite3.connect(db_path) as conn:
                stored = conn.execute("SELECT payload_json FROM runtime_adapter_events WHERE record_id = ?", (record_id,)).fetchone()[0]
            self.assertNotIn("secret-token", stored)
            self.assertNotIn("authorization", stored)


def _reviewed(redacted, diagnostic_id="diagnostic.contract"):
    payload = redacted.as_dict()
    payload["review_references"] = [{
        "manifest_id": payload["manifest_id"],
        "manifest_revision": payload["manifest_revision"],
        "capability_set_id": payload["capability_set_id"],
        "capability_set_revision": payload["capability_set_revision"],
        "diagnostic_id": diagnostic_id,
    }]
    result = redact_document(payload)
    if not isinstance(result, RedactedDocument):
        raise AssertionError(result)
    return result


def _redacted(**overrides):
    payload = {
        "adapter_id": "adapter.alpha",
        "adapter_revision": "rev1",
        "manifest_id": "manifest.alpha",
        "manifest_revision": "mrev1",
        "profile_id": "profile.alpha",
        "capability_set_id": "caps.alpha",
        "capability_set_revision": "caps.rev1",
        "endpoint_id": "endpoint.alpha",
        "workspace_id": "ws_alpha",
        "scope_identity": "workspace:ws_alpha|project:amiga",
        "project_id": "amiga",
    }
    request_id = overrides.get("request_id", payload.get("request_id"))
    payload.setdefault("incident_id", "incident.alpha")
    payload.setdefault("occurrence_at_utc", "2026-07-30T00:00:00Z")
    if request_id is not None and "attempts" not in payload:
        safe = str(request_id).replace(" ", "-")
        payload.setdefault(
            "attempts",
            [{
                "original_request_id": request_id,
                "delivery_id": f"delivery_{safe}",
                "attempt_id": f"attempt_{safe}",
            }],
        )
    payload.setdefault("attempts", [])
    payload.update(overrides)
    for key, value in list(payload.items()):
        if value is _MISSING:
            del payload[key]
    result = redact_document(payload)
    if not isinstance(result, RedactedDocument):
        raise AssertionError(result)
    return result


def _genuine_v1_rows(request_id):
    document = _redacted(request_id=request_id)
    payload = document.as_dict()
    for field in ("incident_id", "occurrence_at_utc", "attempts", "capability_set_id", "capability_set_revision", "review_references"):
        payload.pop(field, None)
    identity = {
        field: payload[field]
        for field in (
            "adapter_id", "adapter_revision", "manifest_id", "manifest_revision",
            "profile_id", "endpoint_id", "workspace_id", "scope_identity", "project_id",
        )
    }
    identity["request_id"] = request_id
    record_id = "adapter_record_" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    requests = [
        ("quarantine_opened", request_id),
        ("recovery_authorized", request_id),
        ("fresh_handshake", request_id),
        ("attempt_reconciled", request_id),
        ("valid_health", "health-1"),
        ("valid_health", "health-2"),
        ("valid_health", "health-3"),
        ("released", request_id),
    ]
    rows = []
    for kind, occurrence in requests:
        row_payload = dict(payload)
        if occurrence is None:
            row_payload.pop("request_id", None)
        else:
            row_payload["request_id"] = occurrence
        rows.append((kind, row_payload))
    return record_id, rows


def _legacy_document(incident_id, *, with_attempt=True):
    attempts = [{
        "original_request_id": "orig-legacy",
        "delivery_id": "delivery_aaa",
        "attempt_id": "attempt_aaa",
    }] if with_attempt else []
    return _redacted(incident_id=incident_id, attempts=attempts)


def _legacy_rows(document, kinds, legacy_incident=None):
    base = document.as_dict()
    if legacy_incident is not None:
        base["incident_id"] = legacy_incident
    record_id = "adapter_record_" + hashlib.sha256(base["incident_id"].encode()).hexdigest()
    if base["attempts"]:
        base["attempts"][0]["delivery_id"] = "delivery_a"
        base["attempts"][0]["attempt_id"] = "attempt_a"
    rows = []
    for index, kind in enumerate(kinds):
        payload = json.loads(json.dumps(base))
        payload["occurrence_at_utc"] = f"2026-07-30T00:00:{index:02d}Z"
        rows.append((kind, payload))
    return record_id, rows


def _create_v1_database(db_path, record_id, rows):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE runtime_adapter_events (
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL,
                event_kind TEXT NOT NULL CHECK(event_kind IN (
                    'quarantine_opened', 'recovery_authorized',
                    'attempt_reconciled', 'fresh_handshake',
                    'valid_health', 'released'
                )),
                payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                payload_sha256 TEXT NOT NULL,
                append_time_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for kind, payload in rows:
            payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            conn.execute(
                """
                INSERT INTO runtime_adapter_events
                    (record_id, event_kind, payload_json, payload_sha256)
                VALUES (?, ?, ?, ?)
                """,
                (record_id, kind, payload_json, hashlib.sha256(payload_json.encode()).hexdigest()),
            )
        conn.execute("PRAGMA user_version=1")


def _payload_and_digest(redacted):
    payload_json = json.dumps(redacted.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _imported_modules(tree):
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


if __name__ == "__main__":
    unittest.main()
