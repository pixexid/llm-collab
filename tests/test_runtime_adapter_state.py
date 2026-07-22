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

            self.assertEqual(current.valid_health_count, 0)
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

            second_id = state.record_quarantine_opened(db_path, _redacted(request_id="attempt-2"))

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
                _redacted(scope_identity="workspace:ws_alpha", project_id=_MISSING, request_id="attempt-1"),
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


def _redacted(**overrides):
    payload = {
        "adapter_id": "adapter.alpha",
        "adapter_revision": "rev1",
        "manifest_id": "manifest.alpha",
        "manifest_revision": "mrev1",
        "profile_id": "profile.alpha",
        "endpoint_id": "endpoint.alpha",
        "workspace_id": "ws_alpha",
        "scope_identity": "workspace:ws_alpha|project:amiga",
        "project_id": "amiga",
    }
    payload.update(overrides)
    for key, value in list(payload.items()):
        if value is _MISSING:
            del payload[key]
    result = redact_document(payload)
    if not isinstance(result, RedactedDocument):
        raise AssertionError(result)
    return result


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
