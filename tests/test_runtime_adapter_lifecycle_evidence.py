"""Tests for deterministic Runtime Adapter lifecycle evidence."""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from llm_collab import runtime_adapter_reference, runtime_adapter_state
from llm_collab.runtime_adapter_conformance import DirectionOutcome, ERROR_CODES
from llm_collab.runtime_adapter_admission_evidence import build_admission_evidence
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_deadline_evidence import build_deadline_evidence
from llm_collab.runtime_adapter_lifecycle import (
    ADAPTER_UNHEALTHY,
    HEALTH_FAILURE_THRESHOLD,
    HEALTH_INTERVAL_MS,
    HEALTH_TIMEOUT,
    SHUTDOWN_DRAIN_MS,
    SHUTDOWN_HARD_KILL_MS,
    SHUTDOWN_IN_PROGRESS,
    EndpointIdentity,
    LifecycleState,
)
from llm_collab.runtime_adapter_lifecycle_evidence import (
    ARTIFACT_LABEL,
    EVIDENCE_KIND,
    HOST_HARNESS_EVIDENCED,
    LifecycleEvidenceFailure,
    build_lifecycle_evidence,
)
from llm_collab.runtime_adapter_manifest import ManifestResolutionError
from llm_collab.runtime_adapter_manifest_evidence import build_manifest_evidence
from llm_collab.runtime_adapter_reference import ReferenceAdapter
from llm_collab.runtime_adapter_redaction import RedactionFailure
from llm_collab.runtime_adapter_request_policy_evidence import build_request_policy_cancellation_evidence
from llm_collab.runtime_adapter_requests import HEALTH_DEADLINE_MS, METHOD_SHUTDOWN
from llm_collab.runtime_adapter_transport_evidence import build_transport_evidence


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_lifecycle_evidence.py"
CLAIM_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
LIFECYCLE_KEYS = {
    "C2cd9421b9c86.1",
    "C358ebcd9608d.3",
    "C4696f988cd35.1",
    "C810ab2059e2a.1",
    "C947f9da5c155.1",
    "Cacd7574f8bbf.1",
    "Cd5e98b5f64fa.1",
}
RECOVERY_KEYS = {
    "C1be9d6c85a83.1",
    "C34441dafd7b4.1",
    "C4988d4d49cef.1",
    "C4988d4d49cef.2",
    "C5a32e1fc6c14.1",
    "C99c6e25a17cd.1",
    "Cea1af958d37a.1",
}
PROVENANCE_KEYS = {"C587906f36ba3.1", "Cd87ad3561bfc.1"}
P7_KEYS = {
    "C3db5b5acb8d7.1",
    "Cbfa7351a2ba5.1",
    "Cbfa7351a2ba5.2",
    "Ce4dfe2af8d8d.1",
    "C0ed26afcfb8a.1",
}
REDACTION_KEYS = {
    "C5474a371af4f.1",
    "C542339e0745f.1",
    "C11985fb69796.1",
    "C1daaef574b8a.1",
    "C1daaef574b8a.2",
    "C1daaef574b8a.3",
}
STRUCTURED_ERROR_KEYS = {"C5d3edf690fb2.1", "C5d3edf690fb2.2"}
SHUTDOWN_KEYS = {
    "Ce0c84af21a71.1",
    "Ce0c84af21a71.2",
    "C43b913cc99f1.1",
    "C78f267e558da.1",
    "C78f267e558da.2",
    "Cc90269b4844b.1",
    "C4f1f0f86f6df.1",
    "Cc41b106e96ee.1",
    "Cc41b106e96ee.2",
    "C27be44c9a8a8.1",
}
C01_LOCAL_FAULT_KEYS = {
    "C960a0d4410e2.1",
    "Cf38671c0af86.1",
    "C5366af19013d.1",
    "C5366af19013d.2",
}
WIRE_COVERED_C15_KEYS = {
    "Ce0c84af21a71.1",
    "Ce0c84af21a71.2",
    "C43b913cc99f1.1",
    "C78f267e558da.1",
    "C78f267e558da.2",
}
DEFERRED_RETRYABILITY_KEYS = {"C1ba88e813bab.1"}
DEFERRED_SHUTDOWN_KEYS = {"C377978e26502.1", "C94617a1d5cde.1"}
DEFERRED_C01_LIVE_KEYS = {"C241df3117a06.1", "Cde2847524a58.1", "Cde2847524a58.2"}
DEFERRED_RECOVERY_ADMISSION_KEYS = {"Cd830c5efc97b.1", "Cd830c5efc97b.2"}
DEFERRED_C16_KEYS = {"C1731a3e18c8e.1", "C5bb2ba77ec3b.1", "C9138fb78426f.1", "Cf70f7c633f57.1"}
DEFERRED_P6_KEYS = {
    "C01d5a7107389.1",
    "C05530aaf0297.1",
    "C44a06b005f56.1",
    "C468b7316502d.1",
    "C4d3e4e331f8e.1",
    "C507960193aaf.1",
    "C5203ae51498d.1",
    "C60fb22117077.1",
    "C8665d49fe212.1",
    "C8665d49fe212.2",
    "C8665d49fe212.3",
    "C991a6ee55456.1",
    "Ca7d929aaf1c6.1",
    "Ca7d929aaf1c6.2",
    "Cbc69b8dc81fc.1",
    "Cbc69b8dc81fc.2",
    "Cbc69b8dc81fc.3",
    "Cbc69b8dc81fc.4",
    "Cfb24d181976b.1",
    "C41a1a5829726.1",
    "C1731a3e18c8e.1",
    "C5bb2ba77ec3b.1",
    "Cf70f7c633f57.1",
    "C9138fb78426f.1",
    "Cddf6725ddfa4.1",
    "Ce45ac56f0f07.1",
    "Ce45ac56f0f07.2",
    "Cd849c64f4310.1",
}
HOST_HARNESS_KEYS = (
    LIFECYCLE_KEYS
    | RECOVERY_KEYS
    | PROVENANCE_KEYS
    | P7_KEYS
    | REDACTION_KEYS
    | STRUCTURED_ERROR_KEYS
    | SHUTDOWN_KEYS
    | C01_LOCAL_FAULT_KEYS
)


class RuntimeAdapterLifecycleEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_lifecycle_evidence_is_distinct_and_covers_c11_and_c12_rows(self) -> None:
        artifact = build_lifecycle_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], EVIDENCE_KIND)
        self.assertEqual(artifact["claim"], HOST_HARNESS_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        covered = {clause["clause_key"] for clause in artifact["clauses"]}
        self.assertEqual(covered, HOST_HARNESS_KEYS)
        self.assertLessEqual(LIFECYCLE_KEYS, covered)
        self.assertLessEqual(RECOVERY_KEYS, covered)
        self.assertLessEqual(PROVENANCE_KEYS, covered)
        self.assertLessEqual(P7_KEYS, covered)
        self.assertLessEqual(REDACTION_KEYS, covered)
        self.assertLessEqual(STRUCTURED_ERROR_KEYS, covered)
        self.assertLessEqual(SHUTDOWN_KEYS, covered)
        self.assertLessEqual(C01_LOCAL_FAULT_KEYS, covered)
        self.assertFalse(DEFERRED_RECOVERY_ADMISSION_KEYS & covered)
        self.assertFalse(DEFERRED_P6_KEYS & covered)
        self.assertFalse(DEFERRED_RETRYABILITY_KEYS & covered)
        self.assertFalse(DEFERRED_SHUTDOWN_KEYS & covered)
        self.assertFalse(DEFERRED_C01_LIVE_KEYS & covered)
        self.assertTrue(
            all(
                clause["state"] == HOST_HARNESS_EVIDENCED and clause["evidence"] == ARTIFACT_LABEL
                for clause in artifact["clauses"]
            )
        )

    def test_lifecycle_observation_uses_real_cadence_and_exact_health_identity(self) -> None:
        observation = build_lifecycle_evidence(self.protocol)["observation"]

        self.assertEqual(observation["first_health_due_ms"], 11_000)
        self.assertTrue(observation["first_health_not_due_before_interval"])
        self.assertTrue(observation["first_health_dispatch_at_interval"])
        self.assertTrue(observation["valid_health_completed_inside_deadline"])
        self.assertEqual(
            observation["identity_health_result"],
            {
                "status": "healthy",
                "protocol_version": 1,
                "adapter_id": "adapter_a",
                "adapter_revision": "adapter_rev_1",
                "manifest_id": "manifest_a",
                "manifest_revision": "manifest_rev_1",
                "profile_id": "profile_a",
                "endpoint_id": "endpoint_a",
                "workspace_id": "ws_alpha",
                "scope_identity": "workspace:ws_alpha|project:amiga",
                "capability_set_id": "caps_a",
                "capability_set_revision": "caps_rev_1",
                "project_id": "amiga",
            },
        )

    def test_lifecycle_observation_anchors_later_health_to_completion_not_dispatch(self) -> None:
        observation = build_lifecycle_evidence(self.protocol)["observation"]

        self.assertTrue(observation["later_health_scheduled_from_completion"])
        self.assertEqual(
            observation["later_health_due_from_completion_ms"],
            25_999,
        )
        self.assertNotEqual(
            observation["later_health_due_from_completion_ms"],
            observation["later_health_due_from_dispatch_ms"],
        )

    def test_lifecycle_observation_timeout_and_unhealthy_disposition_are_real(self) -> None:
        observation = build_lifecycle_evidence(self.protocol)["observation"]

        self.assertEqual(observation["timeout_fault"], HEALTH_TIMEOUT)
        self.assertEqual(observation["timeout_actions"], ("close_connection", "terminate_process"))
        self.assertTrue(observation["timeout_counted_once"])
        self.assertTrue(observation["timeout_no_replacement_initialized"])
        self.assertTrue(observation["old_process_terminated_and_exit_confirmed"])
        self.assertIn("live OS exit waiting remains outside this evidence", observation["deterministic_host_boundary"])

        self.assertEqual(observation["unhealthy_fault"], ADAPTER_UNHEALTHY)
        self.assertEqual(
            observation["unhealthy_actions"],
            ("close_connection", "terminate_process", "mark_unhealthy"),
        )
        self.assertEqual(
            observation["unhealthy_record"],
            {
                "adapter_id": "adapter_a",
                "adapter_revision": "adapter_rev_1",
                "manifest_id": "manifest_a",
                "manifest_revision": "manifest_rev_1",
                "profile_id": "profile_a",
                "endpoint_id": "endpoint_a",
                "workspace_id": "ws_alpha",
                "scope_identity": "workspace:ws_alpha|project:amiga",
                "project_id": "amiga",
                "reason": HEALTH_TIMEOUT,
                "decided_at_ms": HEALTH_INTERVAL_MS + HEALTH_DEADLINE_MS,
                "failure_count": HEALTH_FAILURE_THRESHOLD,
                "unresolved_attempts": ("attempt-a", "attempt-b"),
            },
        )
        self.assertTrue(observation["normal_work_refused_while_unhealthy"])
        self.assertTrue(observation["recovery_health_does_not_clear_unhealthy"])
        self.assertTrue(observation["replacement_deferred_while_unhealthy"])

    def test_recovery_state_observation_uses_real_state_redaction_and_handshake(self) -> None:
        recovery = build_lifecycle_evidence(self.protocol)["observation"]["recovery_state"]

        self.assertTrue(recovery["trusted_handshake_valid"])
        self.assertEqual(recovery["trusted_handshake_mismatch_fault"], "UNTRUSTED_MANIFEST_INPUT")
        self.assertEqual(recovery["quarantined_faults"], ("ADAPTER_UNHEALTHY", "INVALID_SESSION_REF"))
        self.assertRegex(recovery["quarantine_record_id"], r"^adapter_record_[0-9a-f]{64}$")
        self.assertTrue(recovery["quarantine_record_opened"])
        self.assertEqual(
            recovery["quarantine_record_identity"],
            {
                "adapter_id": "adapter_a",
                "adapter_revision": "adapter_rev_1",
                "manifest_id": "manifest_a",
                "manifest_revision": "manifest_rev_1",
                "profile_id": "profile_a",
                "endpoint_id": "endpoint_a",
                "workspace_id": "ws_alpha",
                "scope_identity": "workspace:ws_alpha|project:amiga",
                "project_id": "amiga",
                "request_id": "attempt-1",
            },
        )
        self.assertTrue(recovery["quarantine_record_redacted_before_state_append"])
        self.assertTrue(recovery["raw_state_write_rejected"])
        self.assertTrue(recovery["host_protocol_fault_recorded"])
        self.assertTrue(recovery["host_protocol_fault_not_quarantined"])
        self.assertTrue(recovery["host_protocol_fault_not_released"])
        self.assertTrue(recovery["no_auto_clear_on_recovery_sequence"])
        self.assertTrue(recovery["recovery_sequence_preserves_unresolved_attempt"])
        self.assertTrue(recovery["release_requires_explicit_release_event"])
        self.assertTrue(recovery["redaction_preserves_bounded_stderr_metadata"])
        self.assertEqual(set(recovery["deferred_recovery_admission_keys"]), DEFERRED_RECOVERY_ADMISSION_KEYS)

    def test_manifest_provenance_uses_one_resolve_and_ignores_caller_identity(self) -> None:
        provenance = build_lifecycle_evidence(self.protocol)["observation"]["manifest_provenance"]

        self.assertEqual(provenance["resolve_calls"], 1)
        self.assertTrue(provenance["caller_identity_ignored"])
        self.assertTrue(provenance["same_lookup_identity"])
        self.assertTrue(provenance["initialized_identity_valid"])
        self.assertTrue(provenance["initialize_notification_rejected"])
        self.assertEqual(set(provenance["deferred_c16_keys"]), DEFERRED_C16_KEYS)
        self.assertEqual(
            provenance["initialize_params"],
            {
                "requested_protocol_version": 1,
                "adapter_id": "adapter_a",
                "adapter_revision": "adapter_rev_1",
                "manifest_id": "manifest_a",
                "manifest_revision": "manifest_rev_1",
                "endpoint": {
                    "endpoint_id": "endpoint_a",
                    "adapter_name": "adapter_a",
                    "adapter_revision": "adapter_rev_1",
                },
            },
        )

    def test_p7_integrity_uses_real_adapter_digest_and_state_quarantine(self) -> None:
        p7 = build_lifecycle_evidence(self.protocol)["observation"]["p7_integrity"]

        self.assertTrue(p7["adapter_rejects_delivery_digest_mismatch"])
        self.assertTrue(p7["host_accepts_receipt_digest_match"])
        self.assertTrue(p7["host_rejects_receipt_digest_mismatch"])
        self.assertTrue(p7["both_delivery_and_receipt_recomputed"])
        self.assertTrue(p7["invalid_adapter_output_quarantined"])
        self.assertTrue(p7["digest_mismatch_quarantined"])
        self.assertEqual(set(p7["deferred_p6_keys"]), DEFERRED_P6_KEYS)
        self.assertIn("does not re-home P6 authority", p7["deferred_p6_reason"])

    def test_redaction_observation_uses_real_redactor_before_persistence(self) -> None:
        redaction = build_lifecycle_evidence(self.protocol)["observation"]["redaction"]

        self.assertRegex(redaction["redacted_record_id"], r"^adapter_record_[0-9a-f]{64}$")
        self.assertTrue(redaction["sensitive_fields_dropped"])
        self.assertTrue(redaction["schema_identifiers_preserved"])
        self.assertTrue(redaction["native_identifiers_hashed"])
        self.assertTrue(redaction["recorder_accepts_only_redacted_document"])
        self.assertEqual(
            redaction["persisted_payload"]["stderr"],
            {"total_bytes": 2048, "retained_bytes": 20, "truncated": True},
        )
        self.assertNotIn("prefix", redaction["persisted_payload"]["stderr"])
        self.assertNotIn("raw_payload", redaction["persisted_payload"])
        self.assertNotIn("environment", redaction["persisted_payload"])
        self.assertEqual(redaction["redaction_failure_response_name"], "REDACTION_FAILURE")
        self.assertEqual(redaction["redaction_failure_response_code"], ERROR_CODES["REDACTION_FAILURE"])
        self.assertTrue(redaction["redaction_failure_quarantines_adapter"])
        self.assertFalse(redaction["redaction_failure_wrote_state"])
        self.assertIn("actual stderr drain remains outside", redaction["evidence_kind_boundary"])

    def test_structured_error_observation_uses_closed_enum_and_no_response(self) -> None:
        structured = build_lifecycle_evidence(self.protocol)["observation"]["structured_errors"]

        self.assertEqual(structured["adapter_output_fault"], "INVALID_REQUEST")
        self.assertEqual(structured["adapter_output_code"], ERROR_CODES["INVALID_REQUEST"])
        self.assertTrue(structured["adapter_output_recorded_locally"])
        self.assertTrue(structured["adapter_output_quarantined"])
        self.assertIsNone(structured["adapter_output_response_to_adapter"])
        self.assertTrue(structured["closed_error_envelope_validates"])
        self.assertEqual(set(structured["retryability_deferred_keys"]), DEFERRED_RETRYABILITY_KEYS)
        self.assertEqual(structured["retryability_deferred_reason"], "no real retryability-classification surface")

    def test_shutdown_observation_uses_real_adapter_lifecycle_and_result_validator(self) -> None:
        shutdown = build_lifecycle_evidence(self.protocol)["observation"]["shutdown"]

        self.assertEqual(shutdown["shutdown_success_result"], {"status": "shutdown_started"})
        self.assertTrue(shutdown["shutdown_request_connection_scoped"])
        self.assertTrue(shutdown["shutdown_rejects_session_selector"])
        self.assertEqual(shutdown["invalid_params_fault"], "INVALID_PARAMS")
        self.assertTrue(shutdown["invalid_params_did_not_begin_shutdown"])
        self.assertTrue(shutdown["adapter_enters_shutdown_before_success"])
        self.assertEqual(shutdown["adapter_refuses_later_work_fault"], SHUTDOWN_IN_PROGRESS)
        self.assertEqual(shutdown["lifecycle_shutdown_actions"], ("stop_admitting_new_work",))
        self.assertEqual(shutdown["lifecycle_drain_deadline_ms"], 2_000 + SHUTDOWN_DRAIN_MS)
        self.assertEqual(shutdown["lifecycle_hard_kill_deadline_ms"], 2_000 + SHUTDOWN_HARD_KILL_MS)
        self.assertEqual(shutdown["lifecycle_refuses_later_work_fault"], SHUTDOWN_IN_PROGRESS)
        self.assertTrue(shutdown["second_shutdown_delegated_to_capacity_policy"])
        self.assertEqual(shutdown["drain_actions"], ("continue_drain_without_outcome",))
        self.assertEqual(shutdown["drain_unresolved_attempts"], ("attempt-1", "attempt-2"))
        self.assertFalse(shutdown["drain_authoritative_outcome"])
        self.assertEqual(shutdown["hard_kill_actions"], ("hard_kill_process", "continue_stderr_drain"))
        self.assertEqual(shutdown["hard_kill_unresolved_attempts"], ("attempt-1", "attempt-2"))
        self.assertFalse(shutdown["hard_kill_authoritative_outcome"])
        self.assertIn("live protocol-output flush", shutdown["covered_flush_boundary"])
        self.assertEqual(set(shutdown["deferred_shutdown_keys"]), DEFERRED_SHUTDOWN_KEYS)
        self.assertIn("stderr drain-to-EOF", shutdown["deferred_shutdown_reasons"]["C377978e26502.1"])
        self.assertIn(
            "no real production invalid-shutdown-result validator",
            shutdown["deferred_shutdown_reasons"]["C94617a1d5cde.1"],
        )

    def test_c01_local_fault_observation_uses_real_components_and_honest_live_deferrals(self) -> None:
        c01 = build_lifecycle_evidence(self.protocol)["observation"]["c01_local_fault"]

        self.assertEqual(c01["adapter_request_fault"], "INVALID_REQUEST")
        self.assertTrue(c01["adapter_request_no_response"])
        self.assertTrue(c01["adapter_request_quarantined"])
        self.assertTrue(c01["adapter_request_recorded_locally"])
        self.assertTrue(c01["adapter_request_no_state_advance"])
        self.assertTrue(c01["host_response_closed_without_response"])
        self.assertTrue(c01["host_response_subsequent_input_refused"])
        self.assertTrue(c01["host_response_recorded_locally"])
        self.assertTrue(c01["host_response_not_quarantined"])
        self.assertTrue(c01["host_response_no_operator_release"])
        self.assertEqual(c01["malformed_adapter_output_fault"], "PARSE_ERROR")
        self.assertTrue(c01["malformed_adapter_output_no_response"])
        self.assertTrue(c01["malformed_adapter_output_quarantined"])
        self.assertTrue(c01["malformed_adapter_output_recorded_locally"])
        self.assertEqual(set(c01["deferred_live_c01_keys"]), DEFERRED_C01_LIVE_KEYS)
        self.assertIn("live stream drain", c01["deferred_live_c01_reason"])

    def test_real_lifecycle_component_mutations_kill_evidence(self) -> None:
        original_initialized = LifecycleState.initialized.__func__
        original_begin_health = LifecycleState.begin_health
        original_complete_health = LifecycleState.complete_health
        original_expire_health = LifecycleState.expire_health
        original_classify_later_work = LifecycleState.classify_later_work
        original_health_result = EndpointIdentity.health_result

        def late_initialized(cls, **kwargs):
            state = original_initialized(cls, **kwargs)
            return state.__class__(
                identity=state.identity,
                next_health_due_ms=state.next_health_due_ms + 1,
                consecutive_health_failures=state.consecutive_health_failures,
                in_flight_health=state.in_flight_health,
                expired_health_requests=state.expired_health_requests,
                possibly_accepted_attempts=state.possibly_accepted_attempts,
                shutdown_started_at_ms=state.shutdown_started_at_ms,
                unhealthy=state.unhealthy,
            )

        def no_dispatch_begin_health(self, **kwargs):
            transition = original_begin_health(self, **kwargs)
            if transition.decision.kind == "dispatch_health":
                return self.begin_health(request_id=kwargs["request_id"], now_ms=kwargs["now_ms"] - 1)
            return transition

        def dispatch_anchored_complete_health(self, **kwargs):
            transition = original_complete_health(self, **kwargs)
            if transition.decision.kind != "health_ok" or self.in_flight_health is None:
                return transition
            due_ms = self.in_flight_health.dispatched_at_ms + HEALTH_INTERVAL_MS
            state = transition.state.__class__(
                identity=transition.state.identity,
                next_health_due_ms=due_ms,
                consecutive_health_failures=transition.state.consecutive_health_failures,
                in_flight_health=transition.state.in_flight_health,
                expired_health_requests=transition.state.expired_health_requests,
                possibly_accepted_attempts=transition.state.possibly_accepted_attempts,
                shutdown_started_at_ms=transition.state.shutdown_started_at_ms,
                unhealthy=transition.state.unhealthy,
            )
            return transition.__class__(
                state,
                transition.decision.__class__(
                    transition.decision.kind,
                    actions=transition.decision.actions,
                    fault=transition.decision.fault,
                    next_health_due_ms=due_ms,
                    drain_deadline_ms=transition.decision.drain_deadline_ms,
                    hard_kill_deadline_ms=transition.decision.hard_kill_deadline_ms,
                    unhealthy=transition.decision.unhealthy,
                    unresolved_attempts=transition.decision.unresolved_attempts,
                    authoritative_outcome=transition.decision.authoritative_outcome,
                ),
            )

        def incomplete_expire_health(self, **kwargs):
            transition = original_expire_health(self, **kwargs)
            if transition.decision.kind in {"health_failed", "adapter_unhealthy"}:
                return transition.__class__(
                    transition.state,
                    transition.decision.__class__(
                        transition.decision.kind,
                        actions=("close_connection",),
                        fault=transition.decision.fault,
                        next_health_due_ms=transition.decision.next_health_due_ms,
                        drain_deadline_ms=transition.decision.drain_deadline_ms,
                        hard_kill_deadline_ms=transition.decision.hard_kill_deadline_ms,
                        unhealthy=transition.decision.unhealthy,
                        unresolved_attempts=transition.decision.unresolved_attempts,
                        authoritative_outcome=transition.decision.authoritative_outcome,
                    ),
                )
            return transition

        def admit_later_work(self, **kwargs):
            decision = original_classify_later_work(self, **kwargs)
            if decision.kind == "refuse_new_work" and decision.fault == ADAPTER_UNHEALTHY:
                return decision.__class__("admission_open")
            return decision

        def mismatched_health_result(self):
            payload = dict(original_health_result(self))
            payload["adapter_id"] = "adapter_other"
            return payload

        mutations = (
            (mock.patch.object(LifecycleState, "initialized", classmethod(late_initialized)), "first health due time"),
            (mock.patch.object(LifecycleState, "begin_health", no_dispatch_begin_health), "first health dispatch"),
            (mock.patch.object(LifecycleState, "complete_health", dispatch_anchored_complete_health), "later health anchor"),
            (mock.patch.object(LifecycleState, "expire_health", incomplete_expire_health), "timeout termination action"),
            (mock.patch.object(LifecycleState, "classify_later_work", admit_later_work), "unhealthy refusal"),
            (mock.patch.object(EndpointIdentity, "health_result", mismatched_health_result), "health identity"),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_recovery_component_mutations_kill_evidence(self) -> None:
        original_record_quarantine_opened = runtime_adapter_state.record_quarantine_opened
        original_fold = runtime_adapter_state._fold

        def redaction_failure(document):
            return RedactionFailure("unexpected_redaction_exception")

        def raw_accepting_record(db_path, redacted):
            if isinstance(redacted, dict):
                return "adapter_record_" + ("0" * 64)
            return original_record_quarantine_opened(db_path, redacted)

        def auto_clear_fold(record_id, rows):
            current = original_fold(record_id, rows)
            if current.opened and not current.release_event_seen:
                return current.__class__(
                    current.record_id,
                    opened=False,
                    recovery_authorized=current.recovery_authorized,
                    unresolved_attempts=current.unresolved_attempts,
                    reconciled_attempts=current.reconciled_attempts,
                    fresh_handshake=current.fresh_handshake,
                    valid_health_count=current.valid_health_count,
                    release_event_seen=current.release_event_seen,
                    released=True,
                    event_count=current.event_count,
                )
            return current

        def reject_valid_handshake(resolved, initialized):
            raise ManifestResolutionError("valid identity rejected")

        mutations = (
            (mock.patch.object(_lifecycle_module(), "redact_document", redaction_failure), "redaction before append"),
            (mock.patch.object(runtime_adapter_state, "record_quarantine_opened", raw_accepting_record), "raw payload rejection"),
            (mock.patch.object(runtime_adapter_state, "_fold", auto_clear_fold), "no auto clear"),
            (
                mock.patch.object(_lifecycle_module(), "validate_initialized_identity", reject_valid_handshake),
                "trusted handshake",
            ),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_manifest_provenance_component_mutations_kill_evidence(self) -> None:
        original_resolve = _lifecycle_module()._CountingRegistry.resolve
        original_validate = _lifecycle_module().validate_initialized_identity
        original_initialize_params = _lifecycle_module()._initialize_params_from_resolved
        original_handle_text = ReferenceAdapter.handle_text

        def double_resolve(self, adapter_id):
            original_resolve(self, adapter_id)
            return original_resolve(self, adapter_id)

        def reject_valid_identity(resolved, initialized):
            raise ManifestResolutionError("valid identity rejected")

        def caller_sourced_params(resolved, caller_payload):
            params = dict(original_initialize_params(resolved, caller_payload))
            params["adapter_id"] = caller_payload["adapter_id"]
            return params

        def accept_initialize_notification(self, raw):
            if '"method":"initialize"' in raw and '"id"' not in raw:
                return "{}"
            return original_handle_text(self, raw)

        mutations = (
            (mock.patch.object(_lifecycle_module()._CountingRegistry, "resolve", double_resolve), "one resolve"),
            (
                mock.patch.object(_lifecycle_module(), "validate_initialized_identity", reject_valid_identity),
                "identity validation",
            ),
            (
                mock.patch.object(_lifecycle_module(), "_initialize_params_from_resolved", caller_sourced_params),
                "single-source provenance",
            ),
            (mock.patch.object(ReferenceAdapter, "handle_text", accept_initialize_notification), "notification reject"),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_p7_component_mutations_kill_evidence(self) -> None:
        original_digest = runtime_adapter_reference._canonical_digest
        original_record_quarantine_opened = runtime_adapter_state.record_quarantine_opened

        def trust_existing_integrity(value):
            if isinstance(value, dict) and isinstance(value.get("integrity"), str):
                return value["integrity"]
            return original_digest(value)

        def skip_quarantine_write(db_path, redacted):
            if not hasattr(redacted, "as_dict"):
                return original_record_quarantine_opened(db_path, redacted)
            if redacted.as_dict().get("request_id") not in {"invalid-output", "digest-mismatch"}:
                return original_record_quarantine_opened(db_path, redacted)
            return runtime_adapter_state.record_id_for(redacted)

        mutations = (
            (
                mock.patch.object(runtime_adapter_reference, "_canonical_digest", trust_existing_integrity),
                "real canonical digest recomputation",
            ),
            (
                mock.patch.object(runtime_adapter_state, "record_quarantine_opened", skip_quarantine_write),
                "real adapter-state quarantine append",
            ),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_redaction_component_mutations_kill_evidence(self) -> None:
        original_record_quarantine_opened = runtime_adapter_state.record_quarantine_opened
        original_error = ReferenceAdapter._error

        def redaction_failure(document):
            return RedactionFailure("unexpected_redaction_exception")

        def passthrough_redaction(document):
            return document

        def raw_accepting_record(db_path, redacted):
            if isinstance(redacted, dict):
                return "adapter_record_" + ("0" * 64)
            return original_record_quarantine_opened(db_path, redacted)

        def wrong_redaction_error(self, request_id, name):
            if name == "REDACTION_FAILURE":
                return original_error(self, request_id, "INVALID_REQUEST")
            return original_error(self, request_id, name)

        mutations = (
            (mock.patch.object(_lifecycle_module(), "redact_document", redaction_failure), "redaction success path"),
            (mock.patch.object(_lifecycle_module(), "redact_document", passthrough_redaction), "redacted wrapper gate"),
            (
                mock.patch.object(runtime_adapter_state, "record_quarantine_opened", raw_accepting_record),
                "redacted-only recorder",
            ),
            (mock.patch.object(ReferenceAdapter, "_error", wrong_redaction_error), "redaction failure response"),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_structured_error_component_mutations_kill_evidence(self) -> None:
        original_validate_response = _lifecycle_module().validate_response
        original_record_quarantine_opened = runtime_adapter_state.record_quarantine_opened
        original_error_codes = _lifecycle_module().ERROR_CODES
        original_error = ReferenceAdapter._error

        def accept_adapter_output(value, request_id):
            if request_id == "adapter-output-fault":
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
            return original_validate_response(value, request_id)

        def skip_structured_fault_write(db_path, redacted):
            if hasattr(redacted, "as_dict") and redacted.as_dict().get("request_id") == "adapter-output-fault":
                return runtime_adapter_state.record_id_for(redacted)
            return original_record_quarantine_opened(db_path, redacted)

        changed_error_codes = dict(original_error_codes)
        changed_error_codes["INVALID_REQUEST"] = -32099

        def response_for_adapter_output(self, request_id, name):
            if request_id == "adapter-output-fault":
                return original_error(self, request_id, "METHOD_NOT_FOUND")
            return original_error(self, request_id, name)

        mutations = (
            (mock.patch.object(_lifecycle_module(), "validate_response", accept_adapter_output), "output validation"),
            (
                mock.patch.object(runtime_adapter_state, "record_quarantine_opened", skip_structured_fault_write),
                "local state record",
            ),
            (mock.patch.object(_lifecycle_module(), "ERROR_CODES", MappingProxyType(changed_error_codes)), "closed catalog"),
            (mock.patch.object(ReferenceAdapter, "_error", response_for_adapter_output), "closed error envelope"),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_shutdown_component_mutations_kill_evidence(self) -> None:
        original_begin_shutdown = LifecycleState.begin_shutdown
        original_classify_later_work = LifecycleState.classify_later_work
        original_classify_shutdown_progress = LifecycleState.classify_shutdown_progress
        original_handle_shutdown = ReferenceAdapter._handle_shutdown

        def wrong_shutdown_result(self, request_id, params):
            if not params:
                self._shutdown = True
                return self._result(request_id, {"status": "stopped"})
            return original_handle_shutdown(self, request_id, params)

        def no_inert_shutdown_state(self, request_id, params):
            if not params:
                return self._result(request_id, {"status": "shutdown_started"})
            return original_handle_shutdown(self, request_id, params)

        def accept_session_selector(self, request_id, params):
            if params:
                self._shutdown = True
                return self._result(request_id, {"status": "shutdown_started"})
            return original_handle_shutdown(self, request_id, params)

        def late_shutdown_deadlines(self, **kwargs):
            transition = original_begin_shutdown(self, **kwargs)
            if transition.decision.kind != "shutdown_started":
                return transition
            return transition.__class__(
                transition.state,
                transition.decision.__class__(
                    transition.decision.kind,
                    actions=transition.decision.actions,
                    fault=transition.decision.fault,
                    next_health_due_ms=transition.decision.next_health_due_ms,
                    drain_deadline_ms=(transition.decision.drain_deadline_ms or 0) + 1,
                    hard_kill_deadline_ms=transition.decision.hard_kill_deadline_ms,
                    unhealthy=transition.decision.unhealthy,
                    unresolved_attempts=transition.decision.unresolved_attempts,
                    authoritative_outcome=transition.decision.authoritative_outcome,
                ),
            )

        def admit_later_shutdown_work(self, **kwargs):
            decision = original_classify_later_work(self, **kwargs)
            if decision.fault == SHUTDOWN_IN_PROGRESS:
                return decision.__class__("admission_open")
            return decision

        def resolved_hard_kill(self, **kwargs):
            decision = original_classify_shutdown_progress(self, **kwargs)
            if decision.kind == "hard_kill_due":
                return decision.__class__(
                    decision.kind,
                    actions=decision.actions,
                    fault=decision.fault,
                    next_health_due_ms=decision.next_health_due_ms,
                    drain_deadline_ms=decision.drain_deadline_ms,
                    hard_kill_deadline_ms=decision.hard_kill_deadline_ms,
                    unhealthy=decision.unhealthy,
                    unresolved_attempts=(),
                    authoritative_outcome=True,
                )
            return decision

        mutations = (
            (mock.patch.object(ReferenceAdapter, "_handle_shutdown", wrong_shutdown_result), "success result shape"),
            (mock.patch.object(ReferenceAdapter, "_handle_shutdown", no_inert_shutdown_state), "adapter inert state"),
            (mock.patch.object(ReferenceAdapter, "_handle_shutdown", accept_session_selector), "invalid params refusal"),
            (mock.patch.object(LifecycleState, "begin_shutdown", late_shutdown_deadlines), "deadline constants"),
            (mock.patch.object(LifecycleState, "classify_later_work", admit_later_shutdown_work), "later-work refusal"),
            (
                mock.patch.object(LifecycleState, "classify_shutdown_progress", resolved_hard_kill),
                "uncertain attempts stay unresolved",
            ),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_real_c01_local_fault_component_mutations_kill_evidence(self) -> None:
        original_classify_direction = _lifecycle_module().classify_direction
        original_record_quarantine_opened = runtime_adapter_state.record_quarantine_opened
        original_handle_text = ReferenceAdapter.handle_text
        original_load_json_frame = _lifecycle_module().load_json_frame

        def fail_open_adapter_request(sender, receiver, payload):
            outcome = original_classify_direction(sender, receiver, payload)
            if sender == "adapter" and receiver == "host" and outcome.form == "request":
                return DirectionOutcome(
                    direction_valid=True,
                    form=outcome.form,
                    fault=None,
                    should_close=False,
                    should_quarantine=False,
                    send_response=True,
                )
            return outcome

        def skip_c01_quarantine_write(db_path, redacted):
            if hasattr(redacted, "as_dict") and redacted.as_dict().get("request_id") in {
                "adapter-request",
                "malformed-output",
            }:
                return runtime_adapter_state.record_id_for(redacted)
            return original_record_quarantine_opened(db_path, redacted)

        def accept_host_response(self, raw):
            if '"result":{}' in raw and '"method"' not in raw:
                return self._result("host-response", {"status": "accepted"})
            return original_handle_text(self, raw)

        def accept_duplicate_adapter_output(raw):
            if '"result":{},"result":{}' in raw:
                return {"jsonrpc": "2.0", "id": "fault", "result": {}}
            return original_load_json_frame(raw)

        mutations = (
            (mock.patch.object(_lifecycle_module(), "classify_direction", fail_open_adapter_request), "direction classifier"),
            (
                mock.patch.object(runtime_adapter_state, "record_quarantine_opened", skip_c01_quarantine_write),
                "quarantine state append",
            ),
            (mock.patch.object(ReferenceAdapter, "handle_text", accept_host_response), "host response close"),
            (mock.patch.object(_lifecycle_module(), "load_json_frame", accept_duplicate_adapter_output), "malformed output parser"),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(LifecycleEvidenceFailure):
                    build_lifecycle_evidence(self.protocol)

    def test_build_claim_still_gaps_host_harness_rows_and_deferred_cd830(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertLessEqual(HOST_HARNESS_KEYS - WIRE_COVERED_C15_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_RECOVERY_ADMISSION_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_P6_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_RETRYABILITY_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_SHUTDOWN_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_C01_LIVE_KEYS, gap_keys)

    def test_lifecycle_ledger_is_scoped_disjoint_from_existing_ledgers(self) -> None:
        lifecycle = build_lifecycle_evidence(self.protocol)
        transport = build_transport_evidence(self.protocol)
        admission = build_admission_evidence(self.protocol)
        manifest = build_manifest_evidence(self.protocol)
        cancellation = build_request_policy_cancellation_evidence(self.protocol)
        deadline = build_deadline_evidence(self.protocol)

        lifecycle_keys = {clause["clause_key"] for clause in lifecycle["clauses"]}
        for name, artifact in (
            ("transport", transport),
            ("admission", admission),
            ("manifest", manifest),
            ("cancellation", cancellation),
            ("deadline", deadline),
        ):
            with self.subTest(name=name):
                other_keys = {clause["clause_key"] for clause in artifact["clauses"]}
                self.assertFalse(lifecycle_keys & other_keys)
                self.assertFalse(other_keys & lifecycle_keys)

    def test_clause_text_drift_fails_closed_for_health_and_recovery_rows(self) -> None:
        replacements = (
            (
                "first `runtime.health` call\n    `HEALTH_INTERVAL_MS`",
                "first `runtime.health` call\n    after `HEALTH_INTERVAL_MS`",
            ),
            (
                "MUST arrive inside `HEALTH_DEADLINE_MS`, fixed at\n    5,000 milliseconds",
                "MUST arrive within `HEALTH_DEADLINE_MS`, fixed at\n    5,000 milliseconds",
            ),
            (
                "MUST record exactly one health failure at expiry",
                "MUST record a health failure at expiry",
            ),
            (
                "MUST NOT automatically clear unhealthy\n    or quarantine state",
                "MUST not automatically clear unhealthy\n    or quarantine state",
            ),
            (
                "operator-authorized recovery connection under Clause 12 MUST perform this\n   same exact trusted handshake",
                "operator-authorized recovery connection under Clause 12 MUST perform this\n   trusted handshake",
            ),
            (
                "Quarantine MUST\n    create an operator-visible record",
                "Quarantine MUST\n    create a visible record",
            ),
            (
                "The host MUST record its own\n    outbound protocol fault",
                "The host MUST record an\n    outbound protocol fault",
            ),
            (
                "The host MUST\n   construct its params only after trusted manifest and exact registry lookup",
                "The host MUST\n   construct its params after trusted manifest and exact registry lookup",
            ),
            (
                "The `initialize` adapter and manifest identity/revision members MUST come from\n   that same lookup",
                "The `initialize` adapter and manifest identity/revision members MUST come from\n   a matching lookup",
            ),
            (
                "At P7 the adapter MUST recompute\n   and verify the exact frozen `StateEvidenceV1.integrity`",
                "At P7 the adapter MUST verify\n   the exact frozen `StateEvidenceV1.integrity`",
            ),
            (
                "At P6 the `runtime.deliver` action relation, session-binding\n   evidence profile",
                "At P6 the `runtime.deliver` action relation and session-binding\n   evidence profile",
            ),
            (
                "only\n    redacted bounded diagnostics, byte counts, and a truncation marker may enter\n    the quarantine record",
                "only\n    bounded diagnostics, byte counts, and a truncation marker may enter\n    the quarantine record",
            ),
            (
                "Every adapter output failure MUST be\n    recorded locally under the same enumeration",
                "Every adapter output failure MUST be\n    recorded locally under an enumeration",
            ),
            (
                "The host MUST\n    record it and quarantine the adapter rather than guessing retryability",
                "The host MUST\n    record it rather than guessing retryability",
            ),
            (
                "`runtime.shutdown` is connection-scoped, MUST use\n    the request id required by Clause 1",
                "`runtime.shutdown` is connection-scoped, MUST use\n    a request id required by Clause 1",
            ),
            (
                "MUST enter shutdown before returning exactly\n    `{\"status\":\"shutdown_started\"}`",
                "MUST enter shutdown before returning\n    `{\"status\":\"shutdown_started\"}`",
            ),
            (
                "At `SHUTDOWN_HARD_KILL_MS = 15,000` from shutdown start, the host\n    MUST terminate a still-running process",
                "At `SHUTDOWN_HARD_KILL_MS = 15,000` from shutdown start, the host\n    MUST stop a still-running process",
            ),
            (
                "A missing, additional, or\n    mistyped shutdown result member is host-local `INVALID_REQUEST` at P3",
                "A missing, additional, or\n    mistyped shutdown result member is `INVALID_REQUEST` at P3",
            ),
            (
                "The host MUST classify it only as host-local P2 `INVALID_REQUEST`,\n   send no JSON-RPC response",
                "The host MUST classify it as host-local P2 `INVALID_REQUEST`,\n   send no JSON-RPC response",
            ),
            (
                "The receiver MUST NOT reinterpret stderr, concatenate\n   adjacent lines, or answer malformed adapter output",
                "The receiver MUST NOT reinterpret stderr, join\n   adjacent lines, or answer malformed adapter output",
            ),
            (
                "The host MUST treat the resulting close as\n   its own outbound protocol fault and MUST NOT quarantine",
                "The host MUST treat the resulting close as\n   an outbound protocol fault and MUST NOT quarantine",
            ),
            (
                "The host MUST continuously drain stderr,\n   independently of stdout and request processing",
                "The host MUST drain stderr,\n   independently of stdout and request processing",
            ),
        )
        for old, new in replacements:
            with self.subTest(old=old):
                changed = self.protocol.replace(old, new)
                self.assertNotEqual(changed, self.protocol)
                with self.assertRaisesRegex(
                    LifecycleEvidenceFailure,
                    "missing lifecycle clause|stale lifecycle clause",
                ):
                    build_lifecycle_evidence(changed)

    def test_lifecycle_evidence_module_and_claim_module_remain_disjoint(self) -> None:
        lifecycle_tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        claim_tree = ast.parse(CLAIM_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(lifecycle_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")
        for node in ast.walk(claim_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(
                    any(alias.name == "llm_collab.runtime_adapter_lifecycle_evidence" for alias in node.names)
                )
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_lifecycle_evidence")

    def test_lifecycle_evidence_module_uses_no_live_runtime_surfaces(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        forbidden_modules = {
            "datetime",
            "os",
            "sqlite3",
            "subprocess",
            "threading",
            "time",
            "llm_collab.canonical",
            "llm_collab.daemon",
            "llm_collab.inbox",
            "llm_collab.project_issue_queue",
            "llm_collab.registry",
            "llm_collab.runtime_adapter_claim",
            "llm_collab.runtime_adapter_supervisor",
        }
        forbidden_calls = {"Popen", "run", "sleep", "Thread", "terminate", "kill"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_modules)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, forbidden_modules)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_calls)


def _lifecycle_module():
    return importlib.import_module("llm_collab.runtime_adapter_lifecycle_evidence")


if __name__ == "__main__":
    unittest.main()
