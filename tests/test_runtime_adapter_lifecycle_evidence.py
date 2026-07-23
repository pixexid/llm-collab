"""Tests for deterministic Runtime Adapter lifecycle evidence."""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path
from unittest import mock

from llm_collab import runtime_adapter_state
from llm_collab.runtime_adapter_admission_evidence import build_admission_evidence
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_deadline_evidence import build_deadline_evidence
from llm_collab.runtime_adapter_lifecycle import (
    ADAPTER_UNHEALTHY,
    HEALTH_FAILURE_THRESHOLD,
    HEALTH_INTERVAL_MS,
    HEALTH_TIMEOUT,
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
from llm_collab.runtime_adapter_requests import HEALTH_DEADLINE_MS
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
DEFERRED_RECOVERY_ADMISSION_KEYS = {"Cd830c5efc97b.1", "Cd830c5efc97b.2"}
DEFERRED_C16_KEYS = {"C1731a3e18c8e.1", "C5bb2ba77ec3b.1", "C9138fb78426f.1", "Cf70f7c633f57.1"}
HOST_HARNESS_KEYS = LIFECYCLE_KEYS | RECOVERY_KEYS | PROVENANCE_KEYS


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
        self.assertFalse(DEFERRED_RECOVERY_ADMISSION_KEYS & covered)
        self.assertFalse(DEFERRED_C16_KEYS & covered)
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

    def test_build_claim_still_gaps_host_harness_rows_and_deferred_cd830(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertLessEqual(HOST_HARNESS_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_RECOVERY_ADMISSION_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_C16_KEYS, gap_keys)

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
