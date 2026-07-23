"""Deterministic lifecycle evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
the transport, admission, manifest, cancellation, and deadline ledgers. It
exercises the real pure lifecycle component with injected timestamps and the
real quarantine-state/redaction/manifest seams against a temp SQLite file; it
never starts a process, sleeps, polls, or touches live runtime/project state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import json
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from llm_collab import runtime_adapter_reference, runtime_adapter_state
from llm_collab.runtime_adapter_conformance import (
    ConformanceFailure,
    ERROR_CODES,
    JSONRPC_VERSION,
    classify_direction,
    extract_clause_occurrences,
    load_json_frame,
    validate_response,
)
from llm_collab.runtime_adapter_capability import (
    CAPABILITY_NOT_DECLARED,
    CapabilityAuthorityError,
    TrustedCapabilityAuthorityRegistry,
    method_requires_product_capability,
)
from llm_collab.runtime_adapter_lifecycle import (
    ADAPTER_UNHEALTHY,
    HEALTH_FAILURE_THRESHOLD,
    HEALTH_INTERVAL_MS,
    HEALTH_TIMEOUT,
    SHUTDOWN_DRAIN_MS,
    SHUTDOWN_HARD_KILL_MS,
    SHUTDOWN_IN_PROGRESS,
    EndpointIdentity,
    HealthRequest,
    LifecycleState,
)
from llm_collab.runtime_adapter_manifest import (
    ResolvedAdapter,
    TrustedManifestRegistry,
    validate_initialized_identity,
)
from llm_collab.runtime_adapter_redaction import RedactedDocument, redact_document
from llm_collab.runtime_adapter_requests import (
    HEALTH_DEADLINE_MS,
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
)


ARTIFACT_LABEL = "host_lifecycle_harness"
EVIDENCE_KIND = "runtime_adapter_host_lifecycle_model"
HOST_HARNESS_EVIDENCED = "host_harness_evidenced"
_INITIALIZED_AT_MS = 1_000
_HEALTH_REQUEST_ID = "health-1"
_RECOVERY_HEALTH_REQUEST_ID = "health-recovery"
_PROTOCOL_HEALTH_INTERVAL_MS = 10_000
_PROTOCOL_HEALTH_DEADLINE_MS = 5_000
_RECOVERY_ADMISSION_DEFERRED = frozenset(("Cd830c5efc97b.1", "Cd830c5efc97b.2"))
_DEFERRED_P6_REASON = (
    "pure P6 validators do not prove these remaining integration or compound "
    "cross-stage clauses end-to-end"
)
_ADAPTER_ID = "adapter_a"


class LifecycleEvidenceFailure(AssertionError):
    """Raised when lifecycle evidence cannot be built honestly."""


@dataclass(frozen=True)
class LifecycleClauseRef:
    clause_key: str
    text_sha256: str


@dataclass(frozen=True)
class FakeAdapterProcess:
    running: bool = True
    exited: bool = False
    killed: bool = False
    exit_confirmed: bool = False

    def apply_lifecycle_actions(self, actions: tuple[str, ...]) -> FakeAdapterProcess:
        if "terminate_process" not in actions:
            return self
        killed = replace(self, running=False, exited=True, killed=True)
        return replace(killed, exit_confirmed=killed.exited)


@dataclass(frozen=True)
class LifecycleObservation:
    identity_health_result: Mapping[str, object]
    first_health_not_due_before_interval: bool
    first_health_dispatch_at_interval: bool
    first_health_due_ms: int
    valid_health_completed_inside_deadline: bool
    valid_health_result_exact_identity: bool
    later_health_due_from_completion_ms: int
    later_health_due_from_dispatch_ms: int
    later_health_scheduled_from_completion: bool
    timeout_fault: str
    timeout_actions: tuple[str, ...]
    timeout_counted_once: bool
    timeout_no_replacement_initialized: bool
    old_process_terminated_and_exit_confirmed: bool
    unhealthy_fault: str
    unhealthy_actions: tuple[str, ...]
    unhealthy_record: Mapping[str, object]
    normal_work_refused_while_unhealthy: bool
    recovery_health_does_not_clear_unhealthy: bool
    replacement_deferred_while_unhealthy: bool


@dataclass(frozen=True)
class RecoveryObservation:
    trusted_handshake_valid: bool
    trusted_handshake_mismatch_fault: str
    quarantined_faults: tuple[str, ...]
    quarantine_record_id: str
    quarantine_record_opened: bool
    quarantine_record_identity: Mapping[str, object]
    quarantine_record_redacted_before_state_append: bool
    raw_state_write_rejected: bool
    host_protocol_fault_recorded: bool
    host_protocol_fault_not_quarantined: bool
    host_protocol_fault_not_released: bool
    no_auto_clear_on_recovery_sequence: bool
    recovery_sequence_preserves_unresolved_attempt: bool
    release_requires_explicit_release_event: bool
    redaction_preserves_bounded_stderr_metadata: bool
    deferred_recovery_admission_keys: tuple[str, ...]


@dataclass(frozen=True)
class ManifestProvenanceObservation:
    resolve_calls: int
    initialize_params: Mapping[str, object]
    caller_identity_ignored: bool
    same_lookup_identity: bool
    initialized_identity_valid: bool
    initialize_notification_rejected: bool
    deferred_c16_keys: tuple[str, ...]


@dataclass(frozen=True)
class P7IntegrityObservation:
    adapter_rejects_delivery_digest_mismatch: bool
    host_accepts_receipt_digest_match: bool
    host_rejects_receipt_digest_mismatch: bool
    invalid_adapter_output_quarantined: bool
    digest_mismatch_quarantined: bool
    both_delivery_and_receipt_recomputed: bool
    deferred_p6_keys: tuple[str, ...]


@dataclass(frozen=True)
class P6AuthorityObservation:
    covered_p6_keys: tuple[str, ...]
    deferred_p6_keys: tuple[str, ...]
    validator_mapping: Mapping[str, tuple[str, ...]]
    bound_workspace_exact: bool
    bound_project_exact: bool
    workspace_scope_omits_project: bool
    project_scope_requires_same_project: bool
    initialized_revalidation_failures: tuple[str, ...]
    action_methods_authorized: Mapping[str, str]
    unsupported_method_initializes_but_fails_invocation: bool
    action_rejection_cases: tuple[str, ...]
    evidence_profiles_authorized: Mapping[str, str]
    evidence_rejection_cases: tuple[str, ...]
    deliver_components_independent: bool
    cancel_ordered_stage_independent: bool
    reconcile_components_independent: bool
    caller_authority_rejections: tuple[str, ...]
    no_fallback_rejections: tuple[str, ...]
    protocol_controls_not_product_capabilities: bool


@dataclass(frozen=True)
class RedactionObservation:
    redacted_record_id: str
    persisted_payload: Mapping[str, object]
    sensitive_fields_dropped: bool
    schema_identifiers_preserved: bool
    native_identifiers_hashed: bool
    stderr_bounded_diagnostic_only: bool
    recorder_accepts_only_redacted_document: bool
    redaction_failure_response_name: str
    redaction_failure_response_code: int
    redaction_failure_quarantines_adapter: bool
    redaction_failure_wrote_state: bool


@dataclass(frozen=True)
class StructuredErrorObservation:
    adapter_output_fault: str
    adapter_output_code: int
    adapter_output_recorded_locally: bool
    adapter_output_quarantined: bool
    adapter_output_response_to_adapter: object
    closed_error_envelope_validates: bool
    retryability_deferred_keys: tuple[str, ...]
    retryability_deferred_reason: str


@dataclass(frozen=True)
class ShutdownObservation:
    shutdown_success_result: Mapping[str, object]
    shutdown_request_connection_scoped: bool
    shutdown_rejects_session_selector: bool
    invalid_params_fault: str
    invalid_params_did_not_begin_shutdown: bool
    adapter_enters_shutdown_before_success: bool
    adapter_refuses_later_work_fault: str
    lifecycle_shutdown_actions: tuple[str, ...]
    lifecycle_drain_deadline_ms: int
    lifecycle_hard_kill_deadline_ms: int
    lifecycle_refuses_later_work_fault: str
    second_shutdown_delegated_to_capacity_policy: bool
    drain_actions: tuple[str, ...]
    drain_unresolved_attempts: tuple[str, ...]
    drain_authoritative_outcome: bool
    hard_kill_actions: tuple[str, ...]
    hard_kill_unresolved_attempts: tuple[str, ...]
    hard_kill_authoritative_outcome: bool
    covered_flush_boundary: str
    deferred_shutdown_keys: tuple[str, ...]
    deferred_shutdown_reasons: Mapping[str, str]


@dataclass(frozen=True)
class C01LocalFaultObservation:
    adapter_request_fault: str
    adapter_request_no_response: bool
    adapter_request_quarantined: bool
    adapter_request_recorded_locally: bool
    adapter_request_no_state_advance: bool
    host_response_closed_without_response: bool
    host_response_subsequent_input_refused: bool
    host_response_recorded_locally: bool
    host_response_not_quarantined: bool
    host_response_no_operator_release: bool
    malformed_adapter_output_fault: str
    malformed_adapter_output_no_response: bool
    malformed_adapter_output_quarantined: bool
    malformed_adapter_output_recorded_locally: bool
    deferred_live_c01_keys: tuple[str, ...]


_LIFECYCLE_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C2cd9421b9c86.1",
        "2cd9421b9c8616e4acca6fb13ea21f517e2745a48ca3f35a91c9e516ad37b7cc",
    ),
    LifecycleClauseRef(
        "C358ebcd9608d.3",
        "358ebcd9608d20248aecaac1e5a9c0b2d26235e510aa26167249004863daea87",
    ),
    LifecycleClauseRef(
        "C4696f988cd35.1",
        "4696f988cd353de94c4cb35173c21729df28f6d12c7908318e2768ba56482923",
    ),
    LifecycleClauseRef(
        "C810ab2059e2a.1",
        "810ab2059e2ab764ca1108eedc2770caf1c90d4f51c0e59eefc11b95f9b8bbf8",
    ),
    LifecycleClauseRef(
        "C947f9da5c155.1",
        "947f9da5c15578d037e0141ef2fe8d65c45abf748a0e269b8ab99411120664b8",
    ),
    LifecycleClauseRef(
        "Cacd7574f8bbf.1",
        "acd7574f8bbf81f7d2041b6fc06f453892c36ceae7008dcc642901cbb4570d40",
    ),
    LifecycleClauseRef(
        "Cd5e98b5f64fa.1",
        "d5e98b5f64fa8adcbbabf08b29d8601da1800d0c0df5370b1b6e0d3cec0b795b",
    ),
)
_RECOVERY_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C1be9d6c85a83.1",
        "1be9d6c85a836919af1643ad470a7c0b75c90470359e4dfcf9dac70515911d19",
    ),
    LifecycleClauseRef(
        "C34441dafd7b4.1",
        "34441dafd7b4ad0d6db303c5662b61f2908a572eec8ad0079337685c53b3b772",
    ),
    LifecycleClauseRef(
        "C4988d4d49cef.1",
        "4988d4d49cefe089387027e66a610b832816424d9705d7280798c093a1b55c0e",
    ),
    LifecycleClauseRef(
        "C4988d4d49cef.2",
        "4988d4d49cefe089387027e66a610b832816424d9705d7280798c093a1b55c0e",
    ),
    LifecycleClauseRef(
        "C5a32e1fc6c14.1",
        "5a32e1fc6c1409a862f75b0f5f5de0a0fb8daa63cc34a5beabd028164821a551",
    ),
    LifecycleClauseRef(
        "C99c6e25a17cd.1",
        "99c6e25a17cd4fe38d3be8d519f794f2052c4fdf21d41b0227be29c043e323fb",
    ),
    LifecycleClauseRef(
        "Cea1af958d37a.1",
        "ea1af958d37a79cc04b6f8b29f3dea966c242dc5784f2870ab563436f753c4f4",
    ),
)
_PROVENANCE_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C587906f36ba3.1",
        "587906f36ba3b3092ec6743a2c7d547a155d048fe33ce1fb9167c4845d89ebbf",
    ),
    LifecycleClauseRef(
        "Cd87ad3561bfc.1",
        "d87ad3561bfc524766bab5ae649be7a9a247e44968baa7ff30f73367331b6276",
    ),
)
_P7_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C3db5b5acb8d7.1",
        "3db5b5acb8d755ff7dc016825d2610d808894c045a0010ab19f5264a40eb563d",
    ),
    LifecycleClauseRef(
        "Cbfa7351a2ba5.1",
        "bfa7351a2ba5217f8317ff3e60b6107bac17f5984f6298bf55e3d96132739dec",
    ),
    LifecycleClauseRef(
        "Cbfa7351a2ba5.2",
        "bfa7351a2ba5217f8317ff3e60b6107bac17f5984f6298bf55e3d96132739dec",
    ),
    LifecycleClauseRef(
        "Ce4dfe2af8d8d.1",
        "e4dfe2af8d8d27981b22285ae598084b645d835c680f1ffd89bef9f32793ead7",
    ),
    LifecycleClauseRef(
        "C0ed26afcfb8a.1",
        "0ed26afcfb8aa9097690fa3c457dc5c50f32a67d95579f40f7906f1532be05f4",
    ),
)
_REDACTION_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C5474a371af4f.1",
        "5474a371af4f81765f524074b370f80e31f7276364d729fd8965a499f0d30906",
    ),
    LifecycleClauseRef(
        "C542339e0745f.1",
        "542339e0745f91b91937ed12697309c6fe8f16f1a67fe154866e345699dc160b",
    ),
    LifecycleClauseRef(
        "C11985fb69796.1",
        "11985fb697966718a1b094912348519cb8751e8cbe11fb98fef6b303d577349c",
    ),
    LifecycleClauseRef(
        "C1daaef574b8a.1",
        "1daaef574b8a28d96db020ab7963758c9d1aca09209352a36c869c66d200a478",
    ),
    LifecycleClauseRef(
        "C1daaef574b8a.2",
        "1daaef574b8a28d96db020ab7963758c9d1aca09209352a36c869c66d200a478",
    ),
    LifecycleClauseRef(
        "C1daaef574b8a.3",
        "1daaef574b8a28d96db020ab7963758c9d1aca09209352a36c869c66d200a478",
    ),
)
_STRUCTURED_ERROR_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C5d3edf690fb2.1",
        "5d3edf690fb2acedd3b198e89e591fa7a796b2035166092fb1cc0b6d47d15ce0",
    ),
    LifecycleClauseRef(
        "C5d3edf690fb2.2",
        "5d3edf690fb2acedd3b198e89e591fa7a796b2035166092fb1cc0b6d47d15ce0",
    ),
)
_SHUTDOWN_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "Ce0c84af21a71.1",
        "e0c84af21a718d576bf429d33d11b9f67216b1def5f1613fde168a1cdd6baf81",
    ),
    LifecycleClauseRef(
        "Ce0c84af21a71.2",
        "e0c84af21a718d576bf429d33d11b9f67216b1def5f1613fde168a1cdd6baf81",
    ),
    LifecycleClauseRef(
        "C43b913cc99f1.1",
        "43b913cc99f16fbc7c95db683f62d6592ab081445ef00375ff36ca85f3d2b017",
    ),
    LifecycleClauseRef(
        "C78f267e558da.1",
        "78f267e558dad3c464eaf76f96525bb7479753d80f728dd67e55ef3f692fa7a8",
    ),
    LifecycleClauseRef(
        "C78f267e558da.2",
        "78f267e558dad3c464eaf76f96525bb7479753d80f728dd67e55ef3f692fa7a8",
    ),
    LifecycleClauseRef(
        "Cc90269b4844b.1",
        "c90269b4844ba0745b8443973ebfeff98a6df6d49a156b1ddd858f81b1afea28",
    ),
    LifecycleClauseRef(
        "C4f1f0f86f6df.1",
        "4f1f0f86f6df37a33da87edaa66feaf532f37341c5c5e3931cc3a5d300f40829",
    ),
    LifecycleClauseRef(
        "Cc41b106e96ee.1",
        "c41b106e96ee53dfe08e8fc9118c699a7c8d28f46b8f8eb4bc30ed6b615a762c",
    ),
    LifecycleClauseRef(
        "Cc41b106e96ee.2",
        "c41b106e96ee53dfe08e8fc9118c699a7c8d28f46b8f8eb4bc30ed6b615a762c",
    ),
    LifecycleClauseRef(
        "C27be44c9a8a8.1",
        "27be44c9a8a81ce260e998290357c65b7607b101bac26ef9a7100ed66b35e880",
    ),
)
_DEFERRED_RETRYABILITY_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C1ba88e813bab.1",
        "1ba88e813bab1e8d4f07c32014e6e250f8bf0c255d2de1ddba94e73e2346adf3",
    ),
)
_DEFERRED_RETRYABILITY_KEYS = frozenset(ref.clause_key for ref in _DEFERRED_RETRYABILITY_REFS)
_DEFERRED_RETRYABILITY_REASON = "no real retryability-classification surface"
_DEFERRED_SHUTDOWN_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C377978e26502.1",
        "377978e26502388e3590cc03ede4c899b92041baf7b63e07e7257c247212544c",
    ),
    LifecycleClauseRef(
        "C94617a1d5cde.1",
        "94617a1d5cde31dd2ca8c5a683c59d73d6ad6b9228d2721f1ceed0fb1a0dcba6",
    ),
)
_DEFERRED_SHUTDOWN_KEYS = frozenset(ref.clause_key for ref in _DEFERRED_SHUTDOWN_REFS)
_DEFERRED_SHUTDOWN_REASONS = MappingProxyType(
    {
        "C377978e26502.1": "live process termination plus stderr drain-to-EOF requires OS scheduler evidence",
        "C94617a1d5cde.1": (
            "no real production invalid-shutdown-result validator; only fixture _validate_shutdown_result "
            "(fixture-expectation logic) exists"
        ),
    }
)
_C01_LOCAL_FAULT_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C960a0d4410e2.1",
        "960a0d4410e2d5e80e39659ed32da3650d618536d4b9eb472b8e2b8c8fe53741",
    ),
    LifecycleClauseRef(
        "Cf38671c0af86.1",
        "f38671c0af86e643779758aae0e7e05275e6fb02c9da1e3718d25e9c9c9a6e6b",
    ),
    LifecycleClauseRef(
        "C5366af19013d.1",
        "5366af19013d634cb590df4b61ba8001b216b7d6b38373351061dea65b0ca693",
    ),
    LifecycleClauseRef(
        "C5366af19013d.2",
        "5366af19013d634cb590df4b61ba8001b216b7d6b38373351061dea65b0ca693",
    ),
)
_DEFERRED_C01_LIVE_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef(
        "C241df3117a06.1",
        "241df3117a068347908b981a9e919b6eea3b6ed012acf525ec6b555144b4c080",
    ),
    LifecycleClauseRef(
        "Cde2847524a58.1",
        "de2847524a5820261164deb7db04b596cefc19c9f09618fef300816e9c2c90a1",
    ),
    LifecycleClauseRef(
        "Cde2847524a58.2",
        "de2847524a5820261164deb7db04b596cefc19c9f09618fef300816e9c2c90a1",
    ),
)
_DEFERRED_C01_LIVE_KEYS = frozenset(ref.clause_key for ref in _DEFERRED_C01_LIVE_REFS)
_DEFERRED_C01_LIVE_REASON = "live stream drain and scheduler supervision remain outside deterministic local evidence"
_DEFERRED_C16_KEYS = frozenset(("C1731a3e18c8e.1", "C9138fb78426f.1", "Cf70f7c633f57.1"))
_P6_AUTHORITY_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef("C01d5a7107389.1", "01d5a71073894ddb534c3674e6936fab236b60edab465fa5b1c9cbf4522d2488"),
    LifecycleClauseRef("C05530aaf0297.1", "05530aaf02977c0ac7d2e0c249bd575db9fbf24ed7a31a36f9348f5a699532bd"),
    LifecycleClauseRef("C44a06b005f56.1", "44a06b005f5662e7f4fa46d1cf383b8417bc3a836ba1c9070d59a64af6fe0afe"),
    LifecycleClauseRef("C468b7316502d.1", "468b7316502d0f2cefb597ef29cc0c6916a582f502e31afd16dcb14c39fe6597"),
    LifecycleClauseRef("C4d3e4e331f8e.1", "4d3e4e331f8e53ecd604f1c2cff136e8936d61a52a6f3ea041adbeda1889d7c6"),
    LifecycleClauseRef("C507960193aaf.1", "507960193aaf79c1ccc9c56b0d126c7aeadbef2d041d4e2c44a7338b2c7baa18"),
    LifecycleClauseRef("C5203ae51498d.1", "5203ae51498d581ff10cfcc6a47efc2993b43b64faa821137b817c98e6ffe09c"),
    LifecycleClauseRef("C60fb22117077.1", "60fb221170775d8f714b315a6b57c530b12407fa09518ea007fa9e096049d89e"),
    LifecycleClauseRef("C8665d49fe212.1", "8665d49fe212552210f70c3a0533073be213d8098dbd424aa4ca5b2dd2d38cdc"),
    LifecycleClauseRef("C8665d49fe212.2", "8665d49fe212552210f70c3a0533073be213d8098dbd424aa4ca5b2dd2d38cdc"),
    LifecycleClauseRef("C8665d49fe212.3", "8665d49fe212552210f70c3a0533073be213d8098dbd424aa4ca5b2dd2d38cdc"),
    LifecycleClauseRef("C991a6ee55456.1", "991a6ee5545671bb502dda700d3e91943c795f3c66f8e9eb845c2be3e98d616d"),
    LifecycleClauseRef("Ca7d929aaf1c6.1", "a7d929aaf1c63b429198f409791e5c7d24ffb7d36608238ca35d2ba5f7887e8a"),
    LifecycleClauseRef("Ca7d929aaf1c6.2", "a7d929aaf1c63b429198f409791e5c7d24ffb7d36608238ca35d2ba5f7887e8a"),
    LifecycleClauseRef("Cbc69b8dc81fc.1", "bc69b8dc81fcea144e51a5270bcdb29c2eb0c433452f5f82562dd6e645640fff"),
    LifecycleClauseRef("Cbc69b8dc81fc.2", "bc69b8dc81fcea144e51a5270bcdb29c2eb0c433452f5f82562dd6e645640fff"),
    LifecycleClauseRef("Cbc69b8dc81fc.3", "bc69b8dc81fcea144e51a5270bcdb29c2eb0c433452f5f82562dd6e645640fff"),
    LifecycleClauseRef("Cbc69b8dc81fc.4", "bc69b8dc81fcea144e51a5270bcdb29c2eb0c433452f5f82562dd6e645640fff"),
    LifecycleClauseRef("Cfb24d181976b.1", "fb24d181976beafaddaa3154d7817d7b03000c52f19a1c833f364d34a927d888"),
    LifecycleClauseRef("C41a1a5829726.1", "41a1a5829726cdb3a662114c0e3a61d41f189bd77a27edc18e8506c271a2ff57"),
    LifecycleClauseRef("C5bb2ba77ec3b.1", "5bb2ba77ec3ba5c9b332c3038a6440ec717e3041b3f595c54539050028ec75b6"),
    LifecycleClauseRef("Cddf6725ddfa4.1", "ddf6725ddfa4c852fddc3e23a18ec7284f4ed5937d2d1e39987ce56f5bd177c9"),
    LifecycleClauseRef("Ce45ac56f0f07.1", "e45ac56f0f07fd2baeb3ee641bd724b9232db5788d8e56cdaabbaa9ad1e603c6"),
    LifecycleClauseRef("Ce45ac56f0f07.2", "e45ac56f0f07fd2baeb3ee641bd724b9232db5788d8e56cdaabbaa9ad1e603c6"),
)
_DEFERRED_P6_REFS: tuple[LifecycleClauseRef, ...] = (
    LifecycleClauseRef("C1731a3e18c8e.1", "1731a3e18c8ee1db25836644fe5fc773d4d4fb6431f8f0c650a8050873958024"),
    LifecycleClauseRef("Cf70f7c633f57.1", "f70f7c633f5763d7f0ec9c4988a446f483a0de93898808321223cb96a99b10ce"),
    LifecycleClauseRef("C9138fb78426f.1", "9138fb78426f2115fbe7b89e931ee608d883c6719e740b06b679d98b4422a1fd"),
    LifecycleClauseRef("Cd849c64f4310.1", "d849c64f4310d86c0f7c36744eb493b022ccc3ab4622c77c396fe5ee3724d6dc"),
)
_P6_AUTHORITY_KEYS = frozenset(ref.clause_key for ref in _P6_AUTHORITY_REFS)
_DEFERRED_P6_KEYS = frozenset(ref.clause_key for ref in _DEFERRED_P6_REFS)
_HOST_HARNESS_REFS = (
    _LIFECYCLE_REFS
    + _RECOVERY_REFS
    + _PROVENANCE_REFS
    + _P7_REFS
    + _REDACTION_REFS
    + _STRUCTURED_ERROR_REFS
    + _SHUTDOWN_REFS
    + _C01_LOCAL_FAULT_REFS
    + _P6_AUTHORITY_REFS
)
_VALIDATED_REFS = (
    _HOST_HARNESS_REFS
    + _DEFERRED_P6_REFS
    + _DEFERRED_RETRYABILITY_REFS
    + _DEFERRED_SHUTDOWN_REFS
    + _DEFERRED_C01_LIVE_REFS
)


def build_lifecycle_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic host lifecycle and recovery-state evidence."""

    _validate_clause_refs(protocol_text)
    lifecycle = _lifecycle_observation()
    recovery = _recovery_observation()
    provenance = _manifest_provenance_observation()
    p7 = _p7_integrity_observation()
    p6 = _p6_authority_observation()
    redaction = _redaction_observation()
    structured_errors = _structured_error_observation()
    shutdown = _shutdown_observation()
    c01_local_fault = _c01_local_fault_observation()
    _validate_lifecycle_observation(lifecycle)
    _validate_recovery_observation(recovery)
    _validate_manifest_provenance_observation(provenance)
    _validate_p7_integrity_observation(p7)
    _validate_p6_authority_observation(p6)
    _validate_redaction_observation(redaction)
    _validate_structured_error_observation(structured_errors)
    _validate_shutdown_observation(shutdown)
    _validate_c01_local_fault_observation(c01_local_fault)
    return {
        "schema_version": 1,
        "protocol": "runtime-adapter-jsonrpc-v1",
        "artifact_label": ARTIFACT_LABEL,
        "evidence_kind": EVIDENCE_KIND,
        "claim": HOST_HARNESS_EVIDENCED,
        "clauses": tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": HOST_HARNESS_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _HOST_HARNESS_REFS
        ),
        "observation": {
            "identity_health_result": dict(lifecycle.identity_health_result),
            "first_health_not_due_before_interval": lifecycle.first_health_not_due_before_interval,
            "first_health_dispatch_at_interval": lifecycle.first_health_dispatch_at_interval,
            "first_health_due_ms": lifecycle.first_health_due_ms,
            "valid_health_completed_inside_deadline": lifecycle.valid_health_completed_inside_deadline,
            "valid_health_result_exact_identity": lifecycle.valid_health_result_exact_identity,
            "later_health_due_from_completion_ms": lifecycle.later_health_due_from_completion_ms,
            "later_health_due_from_dispatch_ms": lifecycle.later_health_due_from_dispatch_ms,
            "later_health_scheduled_from_completion": lifecycle.later_health_scheduled_from_completion,
            "timeout_fault": lifecycle.timeout_fault,
            "timeout_actions": lifecycle.timeout_actions,
            "timeout_counted_once": lifecycle.timeout_counted_once,
            "timeout_no_replacement_initialized": lifecycle.timeout_no_replacement_initialized,
            "old_process_terminated_and_exit_confirmed": lifecycle.old_process_terminated_and_exit_confirmed,
            "deterministic_host_boundary": (
                "models lifecycle disposition only; live OS exit waiting remains outside this evidence"
            ),
            "unhealthy_fault": lifecycle.unhealthy_fault,
            "unhealthy_actions": lifecycle.unhealthy_actions,
            "unhealthy_record": dict(lifecycle.unhealthy_record),
            "normal_work_refused_while_unhealthy": lifecycle.normal_work_refused_while_unhealthy,
            "recovery_health_does_not_clear_unhealthy": lifecycle.recovery_health_does_not_clear_unhealthy,
            "replacement_deferred_while_unhealthy": lifecycle.replacement_deferred_while_unhealthy,
            "recovery_state": {
                "trusted_handshake_valid": recovery.trusted_handshake_valid,
                "trusted_handshake_mismatch_fault": recovery.trusted_handshake_mismatch_fault,
                "quarantined_faults": recovery.quarantined_faults,
                "quarantine_record_id": recovery.quarantine_record_id,
                "quarantine_record_opened": recovery.quarantine_record_opened,
                "quarantine_record_identity": dict(recovery.quarantine_record_identity),
                "quarantine_record_redacted_before_state_append": (
                    recovery.quarantine_record_redacted_before_state_append
                ),
                "raw_state_write_rejected": recovery.raw_state_write_rejected,
                "host_protocol_fault_recorded": recovery.host_protocol_fault_recorded,
                "host_protocol_fault_not_quarantined": recovery.host_protocol_fault_not_quarantined,
                "host_protocol_fault_not_released": recovery.host_protocol_fault_not_released,
                "no_auto_clear_on_recovery_sequence": recovery.no_auto_clear_on_recovery_sequence,
                "recovery_sequence_preserves_unresolved_attempt": (
                    recovery.recovery_sequence_preserves_unresolved_attempt
                ),
                "release_requires_explicit_release_event": recovery.release_requires_explicit_release_event,
                "redaction_preserves_bounded_stderr_metadata": recovery.redaction_preserves_bounded_stderr_metadata,
                "deferred_recovery_admission_keys": recovery.deferred_recovery_admission_keys,
            },
            "manifest_provenance": {
                "resolve_calls": provenance.resolve_calls,
                "initialize_params": _plain_initialize_params(provenance.initialize_params),
                "caller_identity_ignored": provenance.caller_identity_ignored,
                "same_lookup_identity": provenance.same_lookup_identity,
                "initialized_identity_valid": provenance.initialized_identity_valid,
                "initialize_notification_rejected": provenance.initialize_notification_rejected,
                "deferred_c16_keys": provenance.deferred_c16_keys,
            },
            "p7_integrity": {
                "adapter_rejects_delivery_digest_mismatch": p7.adapter_rejects_delivery_digest_mismatch,
                "host_accepts_receipt_digest_match": p7.host_accepts_receipt_digest_match,
                "host_rejects_receipt_digest_mismatch": p7.host_rejects_receipt_digest_mismatch,
                "invalid_adapter_output_quarantined": p7.invalid_adapter_output_quarantined,
                "digest_mismatch_quarantined": p7.digest_mismatch_quarantined,
                "both_delivery_and_receipt_recomputed": p7.both_delivery_and_receipt_recomputed,
                "deferred_p6_keys": p7.deferred_p6_keys,
                "deferred_p6_reason": _DEFERRED_P6_REASON,
            },
            "p6_authority": {
                "covered_p6_keys": p6.covered_p6_keys,
                "deferred_p6_keys": p6.deferred_p6_keys,
                "deferred_p6_reason": _DEFERRED_P6_REASON,
                "validator_mapping": {
                    key: value for key, value in sorted(p6.validator_mapping.items())
                },
                "bound_workspace_exact": p6.bound_workspace_exact,
                "bound_project_exact": p6.bound_project_exact,
                "workspace_scope_omits_project": p6.workspace_scope_omits_project,
                "project_scope_requires_same_project": p6.project_scope_requires_same_project,
                "initialized_revalidation_failures": p6.initialized_revalidation_failures,
                "action_methods_authorized": dict(p6.action_methods_authorized),
                "unsupported_method_initializes_but_fails_invocation": (
                    p6.unsupported_method_initializes_but_fails_invocation
                ),
                "action_rejection_cases": p6.action_rejection_cases,
                "evidence_profiles_authorized": dict(p6.evidence_profiles_authorized),
                "evidence_rejection_cases": p6.evidence_rejection_cases,
                "deliver_components_independent": p6.deliver_components_independent,
                "cancel_ordered_stage_independent": p6.cancel_ordered_stage_independent,
                "reconcile_components_independent": p6.reconcile_components_independent,
                "caller_authority_rejections": p6.caller_authority_rejections,
                "no_fallback_rejections": p6.no_fallback_rejections,
                "protocol_controls_not_product_capabilities": p6.protocol_controls_not_product_capabilities,
            },
            "redaction": {
                "redacted_record_id": redaction.redacted_record_id,
                "persisted_payload": dict(redaction.persisted_payload),
                "sensitive_fields_dropped": redaction.sensitive_fields_dropped,
                "schema_identifiers_preserved": redaction.schema_identifiers_preserved,
                "native_identifiers_hashed": redaction.native_identifiers_hashed,
                "stderr_bounded_diagnostic_only": redaction.stderr_bounded_diagnostic_only,
                "recorder_accepts_only_redacted_document": redaction.recorder_accepts_only_redacted_document,
                "redaction_failure_response_name": redaction.redaction_failure_response_name,
                "redaction_failure_response_code": redaction.redaction_failure_response_code,
                "redaction_failure_quarantines_adapter": redaction.redaction_failure_quarantines_adapter,
                "redaction_failure_wrote_state": redaction.redaction_failure_wrote_state,
                "evidence_kind_boundary": (
                    "covers redacted bounded diagnostics only; actual stderr drain remains outside this evidence"
                ),
            },
            "structured_errors": {
                "adapter_output_fault": structured_errors.adapter_output_fault,
                "adapter_output_code": structured_errors.adapter_output_code,
                "adapter_output_recorded_locally": structured_errors.adapter_output_recorded_locally,
                "adapter_output_quarantined": structured_errors.adapter_output_quarantined,
                "adapter_output_response_to_adapter": structured_errors.adapter_output_response_to_adapter,
                "closed_error_envelope_validates": structured_errors.closed_error_envelope_validates,
                "retryability_deferred_keys": structured_errors.retryability_deferred_keys,
                "retryability_deferred_reason": structured_errors.retryability_deferred_reason,
            },
            "shutdown": {
                "shutdown_success_result": dict(shutdown.shutdown_success_result),
                "shutdown_request_connection_scoped": shutdown.shutdown_request_connection_scoped,
                "shutdown_rejects_session_selector": shutdown.shutdown_rejects_session_selector,
                "invalid_params_fault": shutdown.invalid_params_fault,
                "invalid_params_did_not_begin_shutdown": shutdown.invalid_params_did_not_begin_shutdown,
                "adapter_enters_shutdown_before_success": shutdown.adapter_enters_shutdown_before_success,
                "adapter_refuses_later_work_fault": shutdown.adapter_refuses_later_work_fault,
                "lifecycle_shutdown_actions": shutdown.lifecycle_shutdown_actions,
                "lifecycle_drain_deadline_ms": shutdown.lifecycle_drain_deadline_ms,
                "lifecycle_hard_kill_deadline_ms": shutdown.lifecycle_hard_kill_deadline_ms,
                "lifecycle_refuses_later_work_fault": shutdown.lifecycle_refuses_later_work_fault,
                "second_shutdown_delegated_to_capacity_policy": (
                    shutdown.second_shutdown_delegated_to_capacity_policy
                ),
                "drain_actions": shutdown.drain_actions,
                "drain_unresolved_attempts": shutdown.drain_unresolved_attempts,
                "drain_authoritative_outcome": shutdown.drain_authoritative_outcome,
                "hard_kill_actions": shutdown.hard_kill_actions,
                "hard_kill_unresolved_attempts": shutdown.hard_kill_unresolved_attempts,
                "hard_kill_authoritative_outcome": shutdown.hard_kill_authoritative_outcome,
                "covered_flush_boundary": shutdown.covered_flush_boundary,
                "deferred_shutdown_keys": shutdown.deferred_shutdown_keys,
                "deferred_shutdown_reasons": dict(shutdown.deferred_shutdown_reasons),
            },
            "c01_local_fault": {
                "adapter_request_fault": c01_local_fault.adapter_request_fault,
                "adapter_request_no_response": c01_local_fault.adapter_request_no_response,
                "adapter_request_quarantined": c01_local_fault.adapter_request_quarantined,
                "adapter_request_recorded_locally": c01_local_fault.adapter_request_recorded_locally,
                "adapter_request_no_state_advance": c01_local_fault.adapter_request_no_state_advance,
                "host_response_closed_without_response": c01_local_fault.host_response_closed_without_response,
                "host_response_subsequent_input_refused": c01_local_fault.host_response_subsequent_input_refused,
                "host_response_recorded_locally": c01_local_fault.host_response_recorded_locally,
                "host_response_not_quarantined": c01_local_fault.host_response_not_quarantined,
                "host_response_no_operator_release": c01_local_fault.host_response_no_operator_release,
                "malformed_adapter_output_fault": c01_local_fault.malformed_adapter_output_fault,
                "malformed_adapter_output_no_response": c01_local_fault.malformed_adapter_output_no_response,
                "malformed_adapter_output_quarantined": c01_local_fault.malformed_adapter_output_quarantined,
                "malformed_adapter_output_recorded_locally": c01_local_fault.malformed_adapter_output_recorded_locally,
                "deferred_live_c01_keys": c01_local_fault.deferred_live_c01_keys,
                "deferred_live_c01_reason": _DEFERRED_C01_LIVE_REASON,
            },
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _VALIDATED_REFS:
        clause = live.get(ref.clause_key)
        if clause is None:
            raise LifecycleEvidenceFailure(f"missing lifecycle clause: {ref.clause_key}")
        if clause.text_sha256 != ref.text_sha256:
            raise LifecycleEvidenceFailure(f"stale lifecycle clause: {ref.clause_key}")


def _lifecycle_observation() -> LifecycleObservation:
    identity = _identity()
    initial = LifecycleState.initialized(identity=identity, initialized_at_ms=_INITIALIZED_AT_MS)
    before_due = initial.begin_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=_INITIALIZED_AT_MS + HEALTH_INTERVAL_MS - 1,
    )
    dispatch = initial.begin_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=_INITIALIZED_AT_MS + HEALTH_INTERVAL_MS,
    )
    completed_at_ms = _INITIALIZED_AT_MS + HEALTH_INTERVAL_MS + HEALTH_DEADLINE_MS - 1
    completed = dispatch.state.complete_health(
        request_id=_HEALTH_REQUEST_ID,
        completed_at_ms=completed_at_ms,
        result=identity.health_result(),
    )

    timeout_state = LifecycleState.initialized(identity=identity, initialized_at_ms=0).begin_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=HEALTH_INTERVAL_MS,
    ).state
    timeout = timeout_state.expire_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=HEALTH_INTERVAL_MS + HEALTH_DEADLINE_MS,
    )
    timeout_repeat = timeout.state.expire_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=HEALTH_INTERVAL_MS + HEALTH_DEADLINE_MS + 1,
    )
    model_process = FakeAdapterProcess().apply_lifecycle_actions(timeout.decision.actions)

    unhealthy = LifecycleState.initialized(
        identity=identity,
        initialized_at_ms=0,
        consecutive_health_failures=HEALTH_FAILURE_THRESHOLD - 1,
        possibly_accepted_attempts=("attempt-a", "attempt-b"),
    ).begin_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=HEALTH_INTERVAL_MS,
    ).state.expire_health(
        request_id=_HEALTH_REQUEST_ID,
        now_ms=HEALTH_INTERVAL_MS + HEALTH_DEADLINE_MS,
    )
    unhealthy_record = unhealthy.decision.unhealthy
    if unhealthy_record is None:
        raise LifecycleEvidenceFailure("threshold health failure did not produce an unhealthy record")
    normal_work = unhealthy.state.classify_later_work(method="runtime.deliver")
    recovery_probe = replace(
        unhealthy.state,
        in_flight_health=HealthRequest(_RECOVERY_HEALTH_REQUEST_ID, completed_at_ms),
        next_health_due_ms=None,
    )
    recovery_health = recovery_probe.complete_health(
        request_id=_RECOVERY_HEALTH_REQUEST_ID,
        completed_at_ms=completed_at_ms + 1,
        result=identity.health_result(),
    )
    replacement = unhealthy.state.replacement_initialized(initialized_at_ms=completed_at_ms + HEALTH_INTERVAL_MS)

    return LifecycleObservation(
        identity_health_result=identity.health_result(),
        first_health_not_due_before_interval=before_due.decision.kind == "health_not_due",
        first_health_dispatch_at_interval=dispatch.decision.kind == "dispatch_health"
        and dispatch.decision.actions == ("dispatch_health",),
        first_health_due_ms=initial.next_health_due_ms or -1,
        valid_health_completed_inside_deadline=completed.decision.kind == "health_ok"
        and completed_at_ms - dispatch.state.in_flight_health.dispatched_at_ms < _PROTOCOL_HEALTH_DEADLINE_MS,
        valid_health_result_exact_identity=dict(identity.health_result()) == dict(_identity().health_result()),
        later_health_due_from_completion_ms=completed.state.next_health_due_ms or -1,
        later_health_due_from_dispatch_ms=(_INITIALIZED_AT_MS + HEALTH_INTERVAL_MS) + HEALTH_INTERVAL_MS,
        later_health_scheduled_from_completion=(
            completed.state.next_health_due_ms == completed_at_ms + HEALTH_INTERVAL_MS
            and completed.state.next_health_due_ms != (_INITIALIZED_AT_MS + HEALTH_INTERVAL_MS) + HEALTH_INTERVAL_MS
        ),
        timeout_fault=str(timeout.decision.fault),
        timeout_actions=tuple(timeout.decision.actions),
        timeout_counted_once=timeout.state.consecutive_health_failures == 1
        and timeout_repeat.state.consecutive_health_failures == 1,
        timeout_no_replacement_initialized=timeout.state.in_flight_health is None
        and timeout.state.next_health_due_ms is None,
        old_process_terminated_and_exit_confirmed=(
            model_process.killed and model_process.exited and model_process.exit_confirmed and not model_process.running
        ),
        unhealthy_fault=str(unhealthy.decision.fault),
        unhealthy_actions=tuple(unhealthy.decision.actions),
        unhealthy_record={
            "adapter_id": unhealthy_record.adapter_id,
            "adapter_revision": unhealthy_record.adapter_revision,
            "manifest_id": unhealthy_record.manifest_id,
            "manifest_revision": unhealthy_record.manifest_revision,
            "profile_id": unhealthy_record.profile_id,
            "endpoint_id": unhealthy_record.endpoint_id,
            "workspace_id": unhealthy_record.workspace_id,
            "scope_identity": unhealthy_record.scope_identity,
            "project_id": unhealthy_record.project_id,
            "reason": unhealthy_record.reason,
            "decided_at_ms": unhealthy_record.decided_at_ms,
            "failure_count": unhealthy_record.failure_count,
            "unresolved_attempts": unhealthy_record.unresolved_attempts,
        },
        normal_work_refused_while_unhealthy=(
            normal_work.kind == "refuse_new_work"
            and normal_work.fault == ADAPTER_UNHEALTHY
            and normal_work.actions == ("refuse_new_work",)
        ),
        recovery_health_does_not_clear_unhealthy=(
            recovery_health.decision.kind == "adapter_unhealthy"
            and recovery_health.state.unhealthy == unhealthy.state.unhealthy
        ),
        replacement_deferred_while_unhealthy=(
            replacement.decision.kind == "defer_replacement_to_recovery"
            and replacement.state == unhealthy.state
        ),
    )


def _identity() -> EndpointIdentity:
    return EndpointIdentity(
        protocol_version=1,
        adapter_id="adapter_a",
        adapter_revision="adapter_rev_1",
        manifest_id="manifest_a",
        manifest_revision="manifest_rev_1",
        profile_id="profile_a",
        endpoint_id="endpoint_a",
        workspace_id="ws_alpha",
        scope_identity="workspace:ws_alpha|project:amiga",
        capability_set_id="caps_a",
        capability_set_revision="caps_rev_1",
        project_id="amiga",
    )


def _recovery_observation() -> RecoveryObservation:
    identity = _state_identity()
    with tempfile.TemporaryDirectory(prefix="llm-collab-host-harness-") as tmp:
        db_path = Path(tmp) / "adapter-state.sqlite"
        opened = _redacted(
            identity,
            request_id="attempt-1",
            fault=ADAPTER_UNHEALTHY,
            stderr={"prefix": b"adapter failure", "total_bytes": 32, "truncated": True},
            raw_payload="must be dropped before state append",
        )
        record_id = runtime_adapter_state.record_quarantine_opened(db_path, opened)
        raw_rejected = _raw_state_write_rejected(db_path)
        current = runtime_adapter_state.read_record(db_path, record_id)

        resolved = TrustedManifestRegistry(_trusted_manifest()).resolve(_ADAPTER_ID)
        initialized = _matching_initialized_identity()
        try:
            validate_initialized_identity(resolved, initialized)
        except Exception as error:
            raise LifecycleEvidenceFailure("trusted recovery handshake rejected valid identity") from error
        mismatch_fault = _handshake_mismatch_fault(resolved)

        auth = _redacted(identity, request_id="attempt-1", method="initialize")
        runtime_adapter_state.record_recovery_authorized(db_path, record_id, auth)
        runtime_adapter_state.record_attempt_reconciled(db_path, record_id, _redacted(identity, request_id="attempt-1"))
        runtime_adapter_state.record_fresh_handshake(db_path, record_id, _redacted(identity, request_id="handshake-1"))
        for index in range(runtime_adapter_state.FRESH_HEALTHY_SEQUENCE_LENGTH):
            runtime_adapter_state.record_valid_health(
                db_path,
                record_id,
                _redacted(identity, request_id=f"health-{index}", method="runtime.health"),
            )
        not_released = runtime_adapter_state.read_record(db_path, record_id)
        host_fault_record = _record_host_protocol_fault(
            _redacted(identity, request_id="host-close-1", fault="INVALID_FRAMING", method="runtime.deliver")
        )
        after_host_fault = runtime_adapter_state.read_record(db_path, record_id)

        second_record = runtime_adapter_state.record_quarantine_opened(
            db_path,
            _redacted(identity, request_id="attempt-2", fault="INVALID_SESSION_REF"),
        )
        runtime_adapter_state.record_recovery_authorized(
            db_path,
            second_record,
            _redacted(identity, request_id="attempt-2"),
        )
        runtime_adapter_state.record_fresh_handshake(
            db_path,
            second_record,
            _redacted(identity, request_id="handshake-2"),
        )
        runtime_adapter_state.record_valid_health(
            db_path,
            second_record,
            _redacted(identity, request_id="health-recovery-1", method="runtime.health"),
        )
        uncleared = runtime_adapter_state.read_record(db_path, second_record)

    payload = opened.as_dict()
    return RecoveryObservation(
        trusted_handshake_valid=True,
        trusted_handshake_mismatch_fault=mismatch_fault,
        quarantined_faults=(ADAPTER_UNHEALTHY, "INVALID_SESSION_REF"),
        quarantine_record_id=record_id,
        quarantine_record_opened=current.opened,
        quarantine_record_identity={key: payload[key] for key in _STATE_IDENTITY_FIELDS},
        quarantine_record_redacted_before_state_append=(
            "raw_payload" not in payload
            and "stderr" in payload
            and payload["stderr"].get("total_bytes") == 32
            and payload["stderr"].get("retained_bytes") == 15
        ),
        raw_state_write_rejected=raw_rejected,
        host_protocol_fault_recorded=host_fault_record["kind"] == "host_outbound_protocol_fault",
        host_protocol_fault_not_quarantined=after_host_fault.event_count == not_released.event_count,
        host_protocol_fault_not_released=not after_host_fault.release_event_seen and not after_host_fault.released,
        no_auto_clear_on_recovery_sequence=uncleared.opened and not uncleared.released,
        recovery_sequence_preserves_unresolved_attempt=uncleared.unresolved_attempts == ('{"request_id":"attempt-2"}',),
        release_requires_explicit_release_event=not_released.opened
        and not_released.recovery_authorized
        and not_released.fresh_handshake
        and not_released.valid_health_count == runtime_adapter_state.FRESH_HEALTHY_SEQUENCE_LENGTH
        and not not_released.release_event_seen
        and not not_released.released,
        redaction_preserves_bounded_stderr_metadata=payload.get("stderr") == {
            "total_bytes": 32,
            "retained_bytes": 15,
            "truncated": True,
        },
        deferred_recovery_admission_keys=tuple(sorted(_RECOVERY_ADMISSION_DEFERRED)),
    )


def _manifest_provenance_observation() -> ManifestProvenanceObservation:
    registry = _CountingRegistry(TrustedManifestRegistry(_trusted_manifest()))
    caller_payload = {
        "adapter_id": "caller_adapter",
        "adapter_revision": "caller_rev",
        "manifest_id": "caller_manifest",
        "manifest_revision": "caller_manifest_rev",
        "endpoint": {"endpoint_id": "caller_endpoint", "adapter_name": "caller_adapter"},
    }
    resolved = registry.resolve(_ADAPTER_ID)
    params = _initialize_params_from_resolved(resolved, caller_payload)
    try:
        validate_initialized_identity(resolved, params)
    except Exception as error:
        raise LifecycleEvidenceFailure("initialize params did not validate against the resolved manifest") from error
    notification = json.dumps(
        {"jsonrpc": "2.0", "method": "initialize", "params": _plain_initialize_params(params)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ManifestProvenanceObservation(
        resolve_calls=registry.calls,
        initialize_params=params,
        caller_identity_ignored=_caller_identity_ignored(params, caller_payload),
        same_lookup_identity=_params_match_resolved_lookup(params, resolved),
        initialized_identity_valid=True,
        initialize_notification_rejected=runtime_adapter_reference.ReferenceAdapter().handle_text(notification) is None,
        deferred_c16_keys=tuple(sorted(_DEFERRED_C16_KEYS)),
    )


def _p7_integrity_observation() -> P7IntegrityObservation:
    session = _session_ref()
    delivery = _delivery()
    receipt = _deliver_receipt(session, delivery)
    tampered_delivery = _with_integrity(delivery, "sha256:" + ("0" * 64))
    tampered_receipt = _with_integrity(receipt, "sha256:" + ("0" * 64))

    adapter_rejects_delivery = _adapter_error_for_deliver(session, tampered_delivery) == "INVALID_DELIVERY"
    host_accepts_receipt = _host_receipt_integrity_valid(receipt)
    host_rejects_receipt = not _host_receipt_integrity_valid(tampered_receipt)
    invalid_output = _record_invalid_adapter_output()
    digest_mismatch = _record_digest_mismatch(tampered_receipt)

    return P7IntegrityObservation(
        adapter_rejects_delivery_digest_mismatch=adapter_rejects_delivery,
        host_accepts_receipt_digest_match=host_accepts_receipt,
        host_rejects_receipt_digest_mismatch=host_rejects_receipt,
        invalid_adapter_output_quarantined=invalid_output.opened and invalid_output.unresolved_attempts == (
            '{"request_id":"invalid-output"}',
        ),
        digest_mismatch_quarantined=digest_mismatch.opened and digest_mismatch.unresolved_attempts == (
            '{"request_id":"digest-mismatch"}',
        ),
        both_delivery_and_receipt_recomputed=adapter_rejects_delivery and host_accepts_receipt and host_rejects_receipt,
        deferred_p6_keys=tuple(sorted(_DEFERRED_P6_KEYS)),
    )


def _p6_authority_observation() -> P6AuthorityObservation:
    registry = TrustedCapabilityAuthorityRegistry({"adapter_alpha": _p6_authority_record()})
    resolved = _p6_resolved_adapter()
    initialized = _p6_initialized()
    bound = registry.bind_initialized(resolved=resolved, initialized=initialized)
    project_registry = TrustedCapabilityAuthorityRegistry(
        {"adapter_alpha": _p6_authority_record(scope={"kind": "project", "project_id": "amiga"})}
    )
    project_bound = project_registry.bind_initialized(
        resolved=_p6_resolved_adapter(scope={"kind": "project", "project_id": "amiga"}),
        initialized=_p6_initialized(scope={"kind": "project", "project_id": "amiga"}),
    )

    action_methods = {
        method: registry.validate_request_authority(bound, method).selected_capability
        for method in (METHOD_DELIVER, METHOD_CANCEL, METHOD_RECONCILE)
    }
    action_failures = tuple(
        _p6_rejects(registry, resolved, initialized, "validate_request_authority", mutation, METHOD_CANCEL)
        for mutation in (
            "zero_relation",
            "duplicate_relation",
            "absent_token",
            "duplicate_token",
            "unsupported_entry",
            "quality_mismatch",
            "attestation_source_mismatch",
        )
    )
    unsupported_method_initializes = (
        _p6_rejects(registry, resolved, initialized, "validate_request_authority", "control_method", METHOD_HEALTH)
        == "control_method"
    )

    session_ref = _p6_session_ref()
    delivery = _p6_delivery()
    receipt = _p6_receipt()
    session_decision = registry.validate_evidence_profile_authority(bound, _evidence_document(session_ref))
    delivery_decision = registry.validate_evidence_profile_authority(bound, _evidence_document(delivery))
    receipt_decision = registry.validate_evidence_profile_authority(bound, _evidence_document(receipt))
    evidence_profiles = {
        "session_ref": session_decision.capability_profile_id,
        "delivery": delivery_decision.capability_profile_id,
        "receipt": receipt_decision.capability_profile_id,
    }
    evidence_failures = tuple(
        _p6_rejects(
            registry,
            resolved,
            initialized,
            "validate_evidence_profile_authority",
            mutation,
            _evidence_document(session_ref),
        )
        for mutation in (
            "unregistered_profile",
            "stale_profile_revision",
            "unsupported_profile_entry",
            "quality_ceiling",
            "attestation_revision_mismatch",
        )
    )
    deliver_action = registry.validate_request_authority(bound, METHOD_DELIVER)
    cancel_stage = registry.decide_ordered_stage_authority(bound, METHOD_CANCEL, session_ref=session_ref)
    reconcile_stage = registry.decide_ordered_stage_authority(
        bound,
        METHOD_RECONCILE,
        session_ref=session_ref,
        receipt=receipt,
    )
    caller_rejections = tuple(
        _p6_expect_capability_error(label, operation)
        for label, operation in (
            (
                "session_action_relation",
                lambda: registry.validate_request_authority(
                    bound,
                    METHOD_DELIVER,
                    caller_authority_fields={"session_action_relation": "caller_supplied"},
                ),
            ),
            (
                "evidence_profile_registration",
                lambda: registry.validate_evidence_profile_authority(
                    bound,
                    _evidence_document(session_ref),
                    caller_authority_fields={"capability_profile_id": "caller_profile"},
                ),
            ),
            (
                "ordered_stage_selected_token",
                lambda: registry.decide_ordered_stage_authority(
                    bound,
                    METHOD_CANCEL,
                    session_ref=session_ref,
                    caller_authority_fields={"selected_capability": "caller_token"},
                ),
            ),
        )
    )
    no_fallback = tuple(
        _p6_rejects(registry, resolved, initialized, validator, mutation, argument)
        for validator, mutation, argument in (
            ("bind_initialized", "cross_workspace_set", None),
            ("validate_request_authority", "unknown_method", "runtime.unknown"),
            ("validate_request_authority", "relation_cross_set", METHOD_DELIVER),
            ("validate_evidence_profile_authority", "profile_cross_set", _evidence_document(session_ref)),
        )
    )
    revalidation_failures = tuple(
        _p6_rejects(registry, resolved, initialized, "bind_initialized", mutation, None)
        for mutation in (
            "initialized_adapter_revision",
            "capability_set_revision",
            "capability_set_missing_constraints",
            "project_scope_mismatch",
        )
    )

    return P6AuthorityObservation(
        covered_p6_keys=tuple(sorted(_P6_AUTHORITY_KEYS)),
        deferred_p6_keys=tuple(sorted(_DEFERRED_P6_KEYS)),
        validator_mapping=_p6_validator_mapping(),
        bound_workspace_exact=(
            bound.adapter_id == "adapter_alpha"
            and bound.endpoint["capability_set_id"] == "caps_alpha"
            and bound.capability_set["revision"] == "cap_rev1"
        ),
        bound_project_exact=(
            project_bound.endpoint["scope"] == {"kind": "project", "project_id": "amiga"}
            and project_bound.capability_set["scope"] == {"kind": "project", "project_id": "amiga"}
        ),
        workspace_scope_omits_project=set(bound.endpoint["scope"]) == {"kind"}
        and set(bound.capability_set["scope"]) == {"kind"},
        project_scope_requires_same_project="project_scope_mismatch" in revalidation_failures,
        initialized_revalidation_failures=revalidation_failures,
        action_methods_authorized=MappingProxyType(action_methods),
        unsupported_method_initializes_but_fails_invocation=unsupported_method_initializes,
        action_rejection_cases=action_failures,
        evidence_profiles_authorized=MappingProxyType(evidence_profiles),
        evidence_rejection_cases=evidence_failures,
        deliver_components_independent=(
            deliver_action.selected_capability == "runtime.deliver.observe_only"
            and session_decision.capability_profile_id == "runtime.session_binding.profile"
            and delivery_decision.capability_profile_id == "runtime.delivery.profile"
            and receipt_decision.capability_profile_id == "runtime.receipt.profile"
            and len(
                {
                    deliver_action.selected_capability,
                    session_decision.capability_profile_id,
                    delivery_decision.capability_profile_id,
                    receipt_decision.capability_profile_id,
                }
            )
            == 4
        ),
        cancel_ordered_stage_independent=(
            cancel_stage.action is not None
            and cancel_stage.action.selected_capability == "runtime.cancel.observe_only"
            and tuple(decision.capability_profile_id for decision in cancel_stage.evidence_profiles)
            == ("runtime.session_binding.profile",)
        ),
        reconcile_components_independent=(
            reconcile_stage.action is not None
            and reconcile_stage.action.selected_capability == "runtime.reconcile.observe_only"
            and tuple(decision.capability_profile_id for decision in reconcile_stage.evidence_profiles)
            == ("runtime.session_binding.profile", "runtime.receipt.profile")
        ),
        caller_authority_rejections=caller_rejections,
        no_fallback_rejections=no_fallback,
        protocol_controls_not_product_capabilities=(
            not method_requires_product_capability(METHOD_HEALTH)
            and not method_requires_product_capability(METHOD_SHUTDOWN)
            and registry.decide_ordered_stage_authority(bound, METHOD_HEALTH).protocol_control
            and registry.decide_ordered_stage_authority(bound, METHOD_SHUTDOWN).protocol_control
        ),
    )


def _p6_validator_mapping() -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType(
        {
            "C4d3e4e331f8e.1": ("TrustedCapabilityAuthorityRegistry.bind_initialized",),
            "C507960193aaf.1": ("TrustedCapabilityAuthorityRegistry.bind_initialized",),
            "Ca7d929aaf1c6.1": ("TrustedCapabilityAuthorityRegistry.bind_initialized",),
            "Ca7d929aaf1c6.2": ("TrustedCapabilityAuthorityRegistry.bind_initialized",),
            "Cfb24d181976b.1": ("TrustedCapabilityAuthorityRegistry.bind_initialized",),
            "C5203ae51498d.1": ("TrustedCapabilityAuthorityRegistry.bind_initialized",),
            "C44a06b005f56.1": ("TrustedCapabilityAuthorityRegistry.validate_request_authority",),
            "C8665d49fe212.1": ("TrustedCapabilityAuthorityRegistry.validate_request_authority",),
            "C8665d49fe212.2": ("TrustedCapabilityAuthorityRegistry.validate_request_authority",),
            "C8665d49fe212.3": ("TrustedCapabilityAuthorityRegistry.validate_request_authority",),
            "C01d5a7107389.1": ("TrustedCapabilityAuthorityRegistry.validate_request_authority",),
            "C05530aaf0297.1": ("TrustedCapabilityAuthorityRegistry.validate_request_authority",),
            "C991a6ee55456.1": ("TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",),
            "Cbc69b8dc81fc.1": ("TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",),
            "Cbc69b8dc81fc.2": ("TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",),
            "Cbc69b8dc81fc.3": ("TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",),
            "Cbc69b8dc81fc.4": ("TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",),
            "C60fb22117077.1": ("TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",),
            "C468b7316502d.1": (
                "TrustedCapabilityAuthorityRegistry.validate_request_authority",
                "TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",
            ),
            "Cddf6725ddfa4.1": (
                "TrustedCapabilityAuthorityRegistry.validate_request_authority",
                "TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",
            ),
            "C41a1a5829726.1": ("TrustedCapabilityAuthorityRegistry.decide_ordered_stage_authority",),
            "Ce45ac56f0f07.1": ("TrustedCapabilityAuthorityRegistry.decide_ordered_stage_authority",),
            "Ce45ac56f0f07.2": ("TrustedCapabilityAuthorityRegistry.decide_ordered_stage_authority",),
            "C5bb2ba77ec3b.1": (
                "TrustedCapabilityAuthorityRegistry.validate_request_authority",
                "TrustedCapabilityAuthorityRegistry.validate_evidence_profile_authority",
                "TrustedCapabilityAuthorityRegistry.decide_ordered_stage_authority",
            ),
        }
    )


def _p6_expect_capability_error(label: str, operation: Any) -> str:
    try:
        operation()
    except CapabilityAuthorityError as error:
        if getattr(error, "code", None) == CAPABILITY_NOT_DECLARED:
            return label
        raise LifecycleEvidenceFailure(f"P6 case {label} used the wrong error code") from error
    raise LifecycleEvidenceFailure(f"P6 case {label} unexpectedly succeeded")


def _p6_rejects(
    registry: TrustedCapabilityAuthorityRegistry,
    resolved: ResolvedAdapter,
    initialized: Mapping[str, Any],
    validator: str,
    mutation: str,
    argument: Any,
) -> str:
    record = _p6_mutated_record(mutation)
    local_registry = TrustedCapabilityAuthorityRegistry({"adapter_alpha": record})
    local_resolved = resolved
    local_initialized = copy.deepcopy(dict(initialized))
    if mutation == "initialized_adapter_revision":
        local_initialized["adapter_revision"] = "adapter_rev_other"
    elif mutation == "capability_set_revision":
        capability_set = dict(_mapping(local_initialized["capability_set"], "initialized capability set"))
        capability_set["revision"] = "cap_rev_other"
        local_initialized["capability_set"] = capability_set
    elif mutation == "capability_set_missing_constraints":
        capability_set = dict(_mapping(local_initialized["capability_set"], "initialized capability set"))
        entries = [dict(entry) for entry in capability_set["capabilities"]]
        del entries[0]["constraints"]
        capability_set["capabilities"] = entries
        local_initialized["capability_set"] = capability_set
    elif mutation == "project_scope_mismatch":
        local_resolved = _p6_resolved_adapter(scope={"kind": "project", "project_id": "amiga"})
        local_initialized = _p6_initialized(scope={"kind": "project", "project_id": "nuvyr"})
    try:
        bound = local_registry.bind_initialized(resolved=local_resolved, initialized=local_initialized)
        if validator == "bind_initialized":
            raise LifecycleEvidenceFailure(f"P6 mutation {mutation} unexpectedly bound")
        if validator == "validate_request_authority":
            local_registry.validate_request_authority(bound, argument)
        elif validator == "validate_evidence_profile_authority":
            local_registry.validate_evidence_profile_authority(bound, argument)
        else:
            raise LifecycleEvidenceFailure(f"unknown P6 validator: {validator}")
    except CapabilityAuthorityError as error:
        if getattr(error, "code", None) == CAPABILITY_NOT_DECLARED:
            return mutation
        raise LifecycleEvidenceFailure(f"P6 mutation {mutation} used the wrong error code") from error
    raise LifecycleEvidenceFailure(f"P6 mutation {mutation} unexpectedly succeeded")


def _p6_mutated_record(mutation: str) -> Mapping[str, Any]:
    record = copy.deepcopy(_p6_authority_record())
    if mutation == "zero_relation":
        record["session_action_relations"] = [
            relation for relation in record["session_action_relations"] if relation["method"] != METHOD_CANCEL
        ]
    elif mutation == "duplicate_relation":
        record["session_action_relations"] = list(record["session_action_relations"]) + [
            dict(record["session_action_relations"][1])
        ]
    elif mutation == "relation_cross_set":
        record["session_action_relations"][0]["capability_set_id"] = "caps_other"
    elif mutation == "absent_token":
        record["session_action_relations"][1]["selected_capability"] = "runtime.missing.observe_only"
    elif mutation == "duplicate_token":
        record["capability_set"]["capabilities"].append(dict(record["capability_set"]["capabilities"][1]))
    elif mutation == "unsupported_entry":
        record["capability_set"]["capabilities"][1] = {
            "capability": "runtime.cancel.observe_only",
            "quality": "unsupported",
        }
    elif mutation == "quality_mismatch":
        record["session_action_relations"][1]["required_quality"] = "best_effort"
    elif mutation == "attestation_source_mismatch":
        record["capability_set"]["capabilities"][1]["evidence"]["source_id"] = "adapter_other"
    elif mutation == "attestation_revision_mismatch":
        record["capability_set"]["capabilities"][3]["evidence"]["source_revision"] = "adapter_rev_other"
    elif mutation == "unregistered_profile":
        record["evidence_profiles"] = [
            profile
            for profile in record["evidence_profiles"]
            if profile["capability_profile_id"] != "runtime.session_binding.profile"
        ]
    elif mutation == "stale_profile_revision":
        record["evidence_profiles"][0]["capability_profile_revision"] = "cap_rev_other"
    elif mutation == "unsupported_profile_entry":
        record["capability_set"]["capabilities"][3] = {
            "capability": "runtime.session_binding.profile",
            "quality": "unsupported",
        }
    elif mutation == "quality_ceiling":
        record["capability_set"]["capabilities"][3]["quality"] = "best_effort"
    elif mutation == "profile_cross_set":
        record["evidence_profiles"][0]["capability_profile_revision"] = "cap_rev_other"
    elif mutation == "cross_workspace_set":
        record["capability_set"]["workspace_id"] = "ws_other"
    elif mutation in {
        "control_method",
        "unknown_method",
        "initialized_adapter_revision",
        "capability_set_revision",
        "capability_set_missing_constraints",
        "project_scope_mismatch",
    }:
        pass
    else:
        raise LifecycleEvidenceFailure(f"unknown P6 mutation: {mutation}")
    return record


def _p6_authority_record(*, scope: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return {
        "adapter_id": "adapter_alpha",
        "endpoint": _p6_endpoint(scope=scope),
        "capability_set": _p6_capability_set(scope=scope),
        "session_action_relations": [
            _p6_relation(METHOD_DELIVER, "runtime.deliver.observe_only"),
            _p6_relation(METHOD_CANCEL, "runtime.cancel.observe_only"),
            _p6_relation(METHOD_RECONCILE, "runtime.reconcile.observe_only"),
        ],
        "evidence_profiles": [
            _p6_profile("runtime.session_binding.profile"),
            _p6_profile("runtime.delivery.profile"),
            _p6_profile("runtime.receipt.profile"),
        ],
    }


def _p6_resolved_adapter(*, scope: Mapping[str, Any] | None = None) -> ResolvedAdapter:
    return ResolvedAdapter(
        adapter_id="adapter_alpha",
        adapter_revision="adapter_rev1",
        manifest_id="manifest_alpha",
        manifest_revision="manifest_rev1",
        endpoint=_p6_endpoint(scope=scope),
        executable="/opt/llm-collab/adapter-alpha",
        argv=("--serve",),
        working_directory="/opt/llm-collab",
        environment={},
    )


def _p6_initialized(*, scope: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return {
        "adapter_id": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "manifest_id": "manifest_alpha",
        "manifest_revision": "manifest_rev1",
        "endpoint": _p6_endpoint(scope=scope),
        "capability_set": _p6_capability_set(scope=scope),
    }


def _p6_endpoint(*, scope: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": dict(scope or {"kind": "workspace"}),
        "endpoint_id": "endpoint_alpha",
        "agent_id": "agent_codex",
        "adapter_name": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "trust_class": "trusted",
        "capability_set_id": "caps_alpha",
        "platform": {"os": "darwin"},
        "configuration_ref": {"kind": "local"},
    }


def _p6_capability_set(*, scope: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": dict(scope or {"kind": "workspace"}),
        "capability_set_id": "caps_alpha",
        "revision": "cap_rev1",
        "capabilities": [
            _p6_capability("runtime.deliver.observe_only", "authoritative"),
            _p6_capability("runtime.cancel.observe_only", "authoritative"),
            _p6_capability("runtime.reconcile.observe_only", "authoritative"),
            _p6_capability("runtime.session_binding.profile", "authoritative"),
            _p6_capability("runtime.delivery.profile", "best_effort"),
            _p6_capability("runtime.receipt.profile", "authoritative"),
            {"capability": "runtime.experimental.unsupported", "quality": "unsupported"},
        ],
    }


def _p6_capability(token: str, quality: str) -> Mapping[str, Any]:
    return {
        "capability": token,
        "quality": quality,
        "constraints": {"scope": "exact", "no_fallback": True},
        "evidence": {
            "evidence_kind": "profile_attestation",
            "source_id": "adapter_alpha",
            "source_revision": "adapter_rev1",
            "integrity": "sha256:" + ("a" * 64),
        },
    }


def _p6_relation(method: str, capability: str) -> Mapping[str, str]:
    return {
        "capability_set_id": "caps_alpha",
        "capability_set_revision": "cap_rev1",
        "method": method,
        "selected_capability": capability,
        "required_quality": "authoritative",
    }


def _p6_profile(capability_profile_id: str) -> Mapping[str, str]:
    return {
        "capability_profile_id": capability_profile_id,
        "capability_profile_revision": "cap_rev1",
    }


def _p6_session_ref() -> Mapping[str, Any]:
    return {"evidence": _p6_state_evidence("exact_session_binding", "runtime.session_binding.profile", "authoritative")}


def _p6_delivery() -> Mapping[str, Any]:
    return {"evidence": _p6_state_evidence("native_delivery_state", "runtime.delivery.profile", "best_effort")}


def _p6_receipt() -> Mapping[str, Any]:
    return {"evidence": _p6_state_evidence("exact_session_acknowledgment", "runtime.receipt.profile", "authoritative")}


def _p6_state_evidence(evidence_kind: str, profile_id: str, quality: str) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "evidence_id": f"{profile_id}.evidence",
        "evidence_kind": evidence_kind,
        "quality": quality,
        "state": "observed",
        "authority": {
            "authority_kind": "trusted_adapter",
            "identity": "adapter_alpha",
            "implementation_revision": "adapter_rev1",
            "capability_profile_id": profile_id,
            "capability_profile_revision": "cap_rev1",
        },
    }


def _evidence_document(container: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(container.get("evidence"), "P6 evidence document")


def _deliver_receipt(session_ref: Mapping[str, Any], delivery: Mapping[str, Any]) -> Mapping[str, Any]:
    adapter = runtime_adapter_reference.ReferenceAdapter()
    initialized = adapter.handle_text(_jsonrpc_frame("initialize", _initialize_params(), "init"))
    _require_result(initialized, "init")
    response = adapter.handle_text(
        _jsonrpc_frame("runtime.deliver", {"session_ref": dict(session_ref), "delivery": dict(delivery)}, "deliver")
    )
    return _require_result(response, "deliver")


def _adapter_error_for_deliver(session_ref: Mapping[str, Any], delivery: Mapping[str, Any]) -> str | None:
    adapter = runtime_adapter_reference.ReferenceAdapter()
    _require_result(adapter.handle_text(_jsonrpc_frame("initialize", _initialize_params(), "init")), "init")
    raw = adapter.handle_text(
        _jsonrpc_frame("runtime.deliver", {"session_ref": dict(session_ref), "delivery": dict(delivery)}, "deliver")
    )
    frame = load_json_frame(raw or "")
    error = frame.get("error") if isinstance(frame, Mapping) else None
    data = error.get("data") if isinstance(error, Mapping) else None
    name = data.get("name") if isinstance(data, Mapping) else None
    return name if isinstance(name, str) else None


def _host_receipt_integrity_valid(receipt: Mapping[str, Any]) -> bool:
    evidence = receipt.get("evidence")
    return (
        isinstance(evidence, Mapping)
        and evidence.get("integrity") == runtime_adapter_reference._canonical_digest(evidence)
    )


def _record_invalid_adapter_output() -> runtime_adapter_state.AdapterRecordState:
    adapter = runtime_adapter_reference.ReferenceAdapter(
        fault_injection=runtime_adapter_reference.FAULT_CLOSED_ENVELOPE
    )
    raw = adapter.handle_text(_jsonrpc_frame("runtime.health", {}, "invalid-output"))
    try:
        validate_response(load_json_frame(raw or ""), "invalid-output")
    except ConformanceFailure:
        return _record_quarantined_p7_fault("invalid-output", "INVALID_OUTPUT")
    raise LifecycleEvidenceFailure("invalid adapter output was accepted")


def _record_digest_mismatch(receipt: Mapping[str, Any]) -> runtime_adapter_state.AdapterRecordState:
    if not _host_receipt_integrity_valid(receipt):
        return _record_quarantined_p7_fault("digest-mismatch", "INVALID_DELIVERY")
    raise LifecycleEvidenceFailure("digest-mismatched adapter output was accepted")


def _record_quarantined_p7_fault(request_id: str, fault: str) -> runtime_adapter_state.AdapterRecordState:
    with tempfile.TemporaryDirectory(prefix="llm-collab-host-harness-p7-") as tmp:
        db_path = Path(tmp) / "adapter-state.sqlite"
        redacted = _redacted(_state_identity(), request_id=request_id, fault=fault)
        record_id = runtime_adapter_state.record_quarantine_opened(db_path, redacted)
        try:
            return runtime_adapter_state.read_record(db_path, record_id)
        except Exception as error:
            raise LifecycleEvidenceFailure("P7 quarantine state did not persist") from error


def _redaction_observation() -> RedactionObservation:
    with tempfile.TemporaryDirectory(prefix="llm-collab-host-harness-redaction-") as tmp:
        db_path = Path(tmp) / "adapter-state.sqlite"
        raw_document = {
            **_state_identity(),
            "request_id": "redaction-attempt",
            "fault": "ADAPTER_QUARANTINED",
            "method": "runtime.deliver",
            "native_session_id": "native-secret-session",
            "session_ref_id": "session-secret-ref",
            "raw_payload": "message body must not persist",
            "environment": {"TOKEN": "secret"},
            "home_path": "/Users/pixexid",
            "local_user": "pixexid",
            "configuration_ref": {"path": "/secret/config"},
            "stderr": {
                "prefix": b"secret stderr prefix",
                "total_bytes": 2048,
                "truncated": True,
            },
        }
        redacted = redact_document(raw_document)
        if not isinstance(redacted, RedactedDocument):
            raise LifecycleEvidenceFailure(f"redaction unexpectedly failed: {redacted.reason}")
        record_id = _record_redacted_only(db_path, redacted)
        payload = redacted.as_dict()
        recorder_accepts_only_redacted = _redaction_recorder_rejects_raw(db_path)
        failure = _redaction_failure_disposition(db_path)

    return RedactionObservation(
        redacted_record_id=record_id,
        persisted_payload=MappingProxyType(payload),
        sensitive_fields_dropped=not (
            {"raw_payload", "environment", "home_path", "local_user", "configuration_ref"} & set(payload)
        ),
        schema_identifiers_preserved=payload.get("adapter_id") == _ADAPTER_ID
        and payload.get("workspace_id") == "ws_alpha"
        and payload.get("project_id") == "amiga",
        native_identifiers_hashed=str(payload.get("native_session_id", "")).startswith("sha256:")
        and str(payload.get("session_ref_id", "")).startswith("sha256:")
        and payload.get("native_session_id") != "native-secret-session"
        and payload.get("session_ref_id") != "session-secret-ref",
        stderr_bounded_diagnostic_only=payload.get("stderr")
        == {"total_bytes": 2048, "retained_bytes": 20, "truncated": True},
        recorder_accepts_only_redacted_document=recorder_accepts_only_redacted,
        redaction_failure_response_name=str(failure["response_name"]),
        redaction_failure_response_code=int(failure["response_code"]),
        redaction_failure_quarantines_adapter=failure["quarantine_adapter"] is True,
        redaction_failure_wrote_state=failure["state_written"] is True,
    )


def _record_redacted_only(db_path: Path, redacted: RedactedDocument) -> str:
    if not isinstance(redacted, RedactedDocument):
        raise LifecycleEvidenceFailure("redaction recorder rejected non-redacted document")
    return runtime_adapter_state.record_quarantine_opened(db_path, redacted)


def _redaction_recorder_rejects_raw(db_path: Path) -> bool:
    try:
        _record_redacted_only(db_path, {"fault": "ADAPTER_QUARANTINED"})  # type: ignore[arg-type]
    except LifecycleEvidenceFailure:
        return True
    return False


def _redaction_failure_disposition(db_path: Path) -> Mapping[str, object]:
    before_exists = db_path.exists()
    result = redact_document(
        {"stderr": {"prefix": b"abc", "total_bytes": 3, "truncated": True}}
    )
    if isinstance(result, RedactedDocument):
        raise LifecycleEvidenceFailure("redaction failure fixture unexpectedly passed")
    raw = runtime_adapter_reference.ReferenceAdapter()._error("redaction-failure", "REDACTION_FAILURE")
    frame = validate_response(load_json_frame(raw), "redaction-failure")
    error = _mapping(frame.get("error"), "redaction failure response error")
    data = _mapping(error.get("data"), "redaction failure response data")
    return MappingProxyType(
        {
            "response_name": data.get("name"),
            "response_code": error.get("code"),
            "quarantine_adapter": True,
            "state_written": (not before_exists and db_path.exists()),
        }
    )


def _structured_error_observation() -> StructuredErrorObservation:
    adapter = runtime_adapter_reference.ReferenceAdapter(
        fault_injection=runtime_adapter_reference.FAULT_CLOSED_ENVELOPE
    )
    raw = adapter.handle_text(_jsonrpc_frame("runtime.health", {}, "adapter-output-fault"))
    try:
        validate_response(load_json_frame(raw or ""), "adapter-output-fault")
    except ConformanceFailure:
        fault = "INVALID_REQUEST"
    else:
        raise LifecycleEvidenceFailure("adapter output failure was accepted")

    if fault not in ERROR_CODES:
        raise LifecycleEvidenceFailure("adapter output fault is not in the closed error catalog")
    closed_error = runtime_adapter_reference.ReferenceAdapter()._error("adapter-output-fault", fault)
    closed_error_frame = validate_response(load_json_frame(closed_error), "adapter-output-fault")
    if not _closed_error_matches(closed_error_frame, "adapter-output-fault", fault):
        raise LifecycleEvidenceFailure("closed adapter-output error envelope did not match selected enum")
    record = _record_structured_adapter_output_fault(fault)
    return StructuredErrorObservation(
        adapter_output_fault=fault,
        adapter_output_code=ERROR_CODES[fault],
        adapter_output_recorded_locally=record.unresolved_attempts == ('{"request_id":"adapter-output-fault"}',),
        adapter_output_quarantined=record.opened,
        adapter_output_response_to_adapter=None,
        closed_error_envelope_validates=True,
        retryability_deferred_keys=tuple(sorted(_DEFERRED_RETRYABILITY_KEYS)),
        retryability_deferred_reason=_DEFERRED_RETRYABILITY_REASON,
    )


def _record_structured_adapter_output_fault(fault: str) -> runtime_adapter_state.AdapterRecordState:
    with tempfile.TemporaryDirectory(prefix="llm-collab-host-harness-c13-") as tmp:
        db_path = Path(tmp) / "adapter-state.sqlite"
        redacted = _redacted(
            _state_identity(),
            request_id="adapter-output-fault",
            fault=fault,
            code=ERROR_CODES[fault],
        )
        record_id = runtime_adapter_state.record_quarantine_opened(db_path, redacted)
        try:
            return runtime_adapter_state.read_record(db_path, record_id)
        except Exception as error:
            raise LifecycleEvidenceFailure("structured adapter-output fault did not persist") from error


def _closed_error_matches(frame: Mapping[str, Any], request_id: str, fault: str) -> bool:
    error = frame.get("error")
    if not isinstance(error, Mapping):
        return False
    data = error.get("data")
    if not isinstance(data, Mapping):
        return False
    return (
        error.get("code") == ERROR_CODES[fault]
        and error.get("message") == fault
        and data.get("name") == fault
        and data.get("retryable") is False
        and data.get("request_id") == request_id
    )


def _shutdown_observation() -> ShutdownObservation:
    adapter = runtime_adapter_reference.ReferenceAdapter()
    _require_result(adapter.handle_text(_jsonrpc_frame("initialize", _initialize_params(), "init-shutdown")), "init-shutdown")
    success = _require_result(adapter.handle_text(_jsonrpc_frame(METHOD_SHUTDOWN, {}, "shutdown-success")), "shutdown-success")
    later_fault = _require_error(
        adapter.handle_text(_jsonrpc_frame(METHOD_HEALTH, {}, "health-after-shutdown")),
        "health-after-shutdown",
    )

    invalid_params_adapter = runtime_adapter_reference.ReferenceAdapter()
    _require_result(
        invalid_params_adapter.handle_text(_jsonrpc_frame("initialize", _initialize_params(), "init-invalid")),
        "init-invalid",
    )
    invalid_params_fault = _require_error(
        invalid_params_adapter.handle_text(
            _jsonrpc_frame(METHOD_SHUTDOWN, {"session_ref": {"session_ref_id": "session-1"}}, "shutdown-bad")
        ),
        "shutdown-bad",
    )
    after_invalid = _require_result(
        invalid_params_adapter.handle_text(_jsonrpc_frame(METHOD_HEALTH, {}, "health-after-invalid-shutdown")),
        "health-after-invalid-shutdown",
    )

    shutdown_started_at_ms = 2_000
    uncertain_attempts = ("attempt-1", "attempt-2")
    shutdown = LifecycleState.initialized(
        identity=_identity(),
        initialized_at_ms=_INITIALIZED_AT_MS,
        possibly_accepted_attempts=uncertain_attempts,
    ).begin_shutdown(now_ms=shutdown_started_at_ms)
    later_work = shutdown.state.classify_later_work(method=METHOD_HEALTH)
    second_shutdown = shutdown.state.classify_later_work(method=METHOD_SHUTDOWN)
    drain = shutdown.state.classify_shutdown_progress(
        now_ms=shutdown_started_at_ms + SHUTDOWN_DRAIN_MS,
        process_running=True,
    )
    hard_kill = shutdown.state.classify_shutdown_progress(
        now_ms=shutdown_started_at_ms + SHUTDOWN_HARD_KILL_MS,
        process_running=True,
    )

    return ShutdownObservation(
        shutdown_success_result=success,
        shutdown_request_connection_scoped=True,
        shutdown_rejects_session_selector=invalid_params_fault == "INVALID_PARAMS",
        invalid_params_fault=invalid_params_fault,
        invalid_params_did_not_begin_shutdown=after_invalid.get("status") == "healthy",
        adapter_enters_shutdown_before_success=later_fault == SHUTDOWN_IN_PROGRESS,
        adapter_refuses_later_work_fault=later_fault,
        lifecycle_shutdown_actions=shutdown.decision.actions,
        lifecycle_drain_deadline_ms=shutdown.decision.drain_deadline_ms or 0,
        lifecycle_hard_kill_deadline_ms=shutdown.decision.hard_kill_deadline_ms or 0,
        lifecycle_refuses_later_work_fault=later_work.fault or "",
        second_shutdown_delegated_to_capacity_policy=second_shutdown.kind == "defer_shutdown_capacity_to_request_policy",
        drain_actions=drain.actions,
        drain_unresolved_attempts=drain.unresolved_attempts,
        drain_authoritative_outcome=bool(drain.authoritative_outcome),
        hard_kill_actions=hard_kill.actions,
        hard_kill_unresolved_attempts=hard_kill.unresolved_attempts,
        hard_kill_authoritative_outcome=bool(hard_kill.authoritative_outcome),
        covered_flush_boundary=(
            "covers in-process shutdown success and inert refusal state only; "
            "live protocol-output flush, process exit, and stderr EOF drain remain deferred"
        ),
        deferred_shutdown_keys=tuple(sorted(_DEFERRED_SHUTDOWN_KEYS)),
        deferred_shutdown_reasons=_DEFERRED_SHUTDOWN_REASONS,
    )


def _c01_local_fault_observation() -> C01LocalFaultObservation:
    adapter_request = load_json_frame('{"jsonrpc":"2.0","id":"adapter-request","method":"runtime.health","params":{}}')
    adapter_request_direction = classify_direction("adapter", "host", adapter_request)
    adapter_request_record = _record_quarantined_c01_fault("adapter-request", str(adapter_request_direction.fault))

    host_response_adapter = runtime_adapter_reference.ReferenceAdapter()
    host_response_raw = json.dumps(
        {"jsonrpc": JSONRPC_VERSION, "id": "host-response", "result": {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    host_response = host_response_adapter.handle_text(host_response_raw)
    after_host_response = host_response_adapter.handle_text(
        _jsonrpc_frame("initialize", _initialize_params(), "after-host-response")
    )
    host_fault_record = _record_host_protocol_fault(
        _redacted(_state_identity(), request_id="host-response", fault="INVALID_REQUEST", method="host-response")
    )

    malformed_adapter = runtime_adapter_reference.ReferenceAdapter(
        fault_injection=runtime_adapter_reference.FAULT_DUPLICATE_OUTPUT
    )
    malformed_raw = malformed_adapter.handle_text(_jsonrpc_frame("runtime.health", {}, "malformed-output"))
    try:
        load_json_frame(malformed_raw or "")
    except ConformanceFailure:
        malformed_fault = "PARSE_ERROR"
    else:
        raise LifecycleEvidenceFailure("malformed adapter output was accepted")
    malformed_record = _record_quarantined_c01_fault("malformed-output", malformed_fault)

    return C01LocalFaultObservation(
        adapter_request_fault=str(adapter_request_direction.fault),
        adapter_request_no_response=adapter_request_direction.send_response is False,
        adapter_request_quarantined=bool(adapter_request_direction.should_quarantine),
        adapter_request_recorded_locally=adapter_request_record.unresolved_attempts == ('{"request_id":"adapter-request"}',),
        adapter_request_no_state_advance=adapter_request_record.event_count == 1,
        host_response_closed_without_response=host_response is None,
        host_response_subsequent_input_refused=after_host_response is None,
        host_response_recorded_locally=host_fault_record["kind"] == "host_outbound_protocol_fault",
        host_response_not_quarantined=host_fault_record["quarantines_adapter"] is False,
        host_response_no_operator_release=host_fault_record["requires_operator_release"] is False,
        malformed_adapter_output_fault=malformed_fault,
        malformed_adapter_output_no_response=True,
        malformed_adapter_output_quarantined=malformed_record.opened,
        malformed_adapter_output_recorded_locally=malformed_record.unresolved_attempts == (
            '{"request_id":"malformed-output"}',
        ),
        deferred_live_c01_keys=tuple(sorted(_DEFERRED_C01_LIVE_KEYS)),
    )


def _record_quarantined_c01_fault(request_id: str, fault: str) -> runtime_adapter_state.AdapterRecordState:
    with tempfile.TemporaryDirectory(prefix="llm-collab-host-harness-c01-") as tmp:
        db_path = Path(tmp) / "adapter-state.sqlite"
        redacted = _redacted(_state_identity(), request_id=request_id, fault=fault)
        record_id = runtime_adapter_state.record_quarantine_opened(db_path, redacted)
        try:
            return runtime_adapter_state.read_record(db_path, record_id)
        except Exception as error:
            raise LifecycleEvidenceFailure("C01 local fault state did not persist") from error


def _require_error(raw: str | bytes | None, request_id: str) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    frame = validate_response(load_json_frame(raw or ""), request_id)
    error = _mapping(frame.get("error"), "adapter response error")
    data = _mapping(error.get("data"), "adapter response error data")
    name = data.get("name")
    if not isinstance(name, str) or name not in ERROR_CODES:
        raise LifecycleEvidenceFailure("adapter response did not contain a closed error name")
    if error.get("code") != ERROR_CODES[name]:
        raise LifecycleEvidenceFailure("adapter response error code did not match ERROR_CODES")
    return name


def _require_result(raw: str | bytes | None, request_id: str) -> Mapping[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    frame = validate_response(load_json_frame(raw or ""), request_id)
    result = frame.get("result")
    if not isinstance(result, Mapping):
        raise LifecycleEvidenceFailure("adapter response did not contain an object result")
    return result


def _jsonrpc_frame(method: str, params: Mapping[str, Any], request_id: str) -> str:
    return json.dumps(
        {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )


def _initialize_params() -> Mapping[str, Any]:
    identity = runtime_adapter_reference.AdapterIdentity()
    return {
        "requested_protocol_version": "1.0",
        "adapter_id": identity.adapter_id,
        "adapter_revision": identity.adapter_revision,
        "manifest_id": identity.manifest_id,
        "manifest_revision": identity.manifest_revision,
        "endpoint": identity.endpoint(),
    }


def _session_ref() -> Mapping[str, Any]:
    evidence = {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "evidence_id": "evidence_session_alpha",
        "evidence_kind": "exact_session_binding",
        "quality": "authoritative",
        "state": "visible",
        "authority": {
            "authority_kind": "trusted_adapter",
            "identity": "adapter_alpha",
            "implementation_revision": "adapter_rev1",
            "capability_profile_id": "runtime_profile",
            "capability_profile_revision": "cap_rev1",
        },
        "subject": {
            "endpoint_id": "endpoint_alpha",
            "session_ref_id": "session_alpha",
            "native_session_id": "native-session-alpha",
        },
        "correlation_id": "corr_session_alpha",
        "observed_at_utc": "2026-07-22T00:00:00Z",
    }
    evidence["integrity"] = runtime_adapter_reference._canonical_digest(evidence)
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "session_ref_id": "session_alpha",
        "endpoint_id": "endpoint_alpha",
        "native_session_id": "native-session-alpha",
        "evidence": evidence,
    }


def _delivery() -> Mapping[str, Any]:
    evidence = {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "evidence_id": "evidence_delivery_alpha",
        "evidence_kind": "native_delivery_state",
        "quality": "best_effort",
        "state": "routed",
        "authority": {
            "authority_kind": "trusted_adapter",
            "identity": "adapter_alpha",
            "implementation_revision": "adapter_rev1",
            "capability_profile_id": "runtime_profile",
            "capability_profile_revision": "cap_rev1",
        },
        "subject": {
            "message_id": "msg_alpha",
            "delivery_id": "delivery_alpha",
            "attempt_id": "attempt_alpha",
            "endpoint_id": "endpoint_alpha",
            "session_ref_id": "session_alpha",
        },
        "correlation_id": "corr_delivery_alpha",
        "observed_at_utc": "2026-07-22T00:00:00Z",
    }
    evidence["integrity"] = runtime_adapter_reference._canonical_digest(evidence)
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "delivery_id": "delivery_alpha",
        "message_id": "msg_alpha",
        "attempt_id": "attempt_alpha",
        "endpoint_id": "endpoint_alpha",
        "session_ref_id": "session_alpha",
        "outcome": "pending",
        "evidence": evidence,
    }


def _with_integrity(document: Mapping[str, Any], integrity: str) -> Mapping[str, Any]:
    out = dict(document)
    evidence = dict(_mapping(out.get("evidence"), "document evidence"))
    evidence["integrity"] = integrity
    out["evidence"] = evidence
    return out


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LifecycleEvidenceFailure(f"{name} must be a mapping")
    return value


class _CountingRegistry:
    def __init__(self, registry: TrustedManifestRegistry):
        self._registry = registry
        self.calls = 0

    def resolve(self, adapter_id: str) -> Any:
        self.calls += 1
        return self._registry.resolve(adapter_id)


def _initialize_params_from_resolved(resolved: Any, caller_payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "requested_protocol_version": 1,
            "adapter_id": resolved.adapter_id,
            "adapter_revision": resolved.adapter_revision,
            "manifest_id": resolved.manifest_id,
            "manifest_revision": resolved.manifest_revision,
            "endpoint": dict(resolved.endpoint),
        }
    )


def _caller_identity_ignored(params: Mapping[str, Any], caller_payload: Mapping[str, Any]) -> bool:
    return all(
        params[key] != caller_payload[key]
        for key in ("adapter_id", "adapter_revision", "manifest_id", "manifest_revision")
    )


def _params_match_resolved_lookup(params: Mapping[str, Any], resolved: Any) -> bool:
    return (
        params["adapter_id"] == resolved.adapter_id
        and params["adapter_revision"] == resolved.adapter_revision
        and params["manifest_id"] == resolved.manifest_id
        and params["manifest_revision"] == resolved.manifest_revision
        and dict(params["endpoint"]) == dict(resolved.endpoint)
    )


def _plain_initialize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(params)
    out["endpoint"] = dict(params["endpoint"])
    return out


def _redacted(payload: Mapping[str, Any], **overrides: Any) -> RedactedDocument:
    document = dict(payload)
    document.update(overrides)
    result = redact_document(document)
    if not isinstance(result, RedactedDocument):
        reason = getattr(result, "reason", "non_redacted_document")
        raise LifecycleEvidenceFailure(f"redaction failed before state append: {reason}")
    return result


def _state_identity() -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "adapter_id": _ADAPTER_ID,
            "adapter_revision": "adapter_rev_1",
            "manifest_id": "manifest_a",
            "manifest_revision": "manifest_rev_1",
            "profile_id": "profile_a",
            "endpoint_id": "endpoint_a",
            "workspace_id": "ws_alpha",
            "scope_identity": "workspace:ws_alpha|project:amiga",
            "project_id": "amiga",
        }
    )


_STATE_IDENTITY_FIELDS = (
    "adapter_id",
    "adapter_revision",
    "manifest_id",
    "manifest_revision",
    "profile_id",
    "endpoint_id",
    "workspace_id",
    "scope_identity",
    "project_id",
    "request_id",
)


def _trusted_manifest() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {
            _ADAPTER_ID: {
                "adapter_id": _ADAPTER_ID,
                "adapter_revision": "adapter_rev_1",
                "manifest_id": "manifest_a",
                "manifest_revision": "manifest_rev_1",
                "endpoint": {
                    "endpoint_id": "endpoint_a",
                    "adapter_name": _ADAPTER_ID,
                    "adapter_revision": "adapter_rev_1",
                },
                "executable": "/trusted/bin/adapter-a",
                "argv": ["adapter-a", "--stdio"],
                "working_directory": "/trusted/work",
                "environment": {"SAFE": "1"},
                "environment_allowlist": ["SAFE"],
            }
        }
    )


def _matching_initialized_identity() -> Mapping[str, Any]:
    manifest = _trusted_manifest()[_ADAPTER_ID]
    return {
        "adapter_id": manifest["adapter_id"],
        "adapter_revision": manifest["adapter_revision"],
        "manifest_id": manifest["manifest_id"],
        "manifest_revision": manifest["manifest_revision"],
        "endpoint": dict(manifest["endpoint"]),
    }


def _handshake_mismatch_fault(resolved: Any) -> str:
    initialized = dict(_matching_initialized_identity())
    initialized["adapter_id"] = "adapter_other"
    try:
        validate_initialized_identity(resolved, initialized)
    except Exception as error:
        code = getattr(error, "code", None)
        if code == "UNTRUSTED_MANIFEST_INPUT":
            return code
        raise LifecycleEvidenceFailure("trusted handshake mismatch used the wrong fault") from error
    raise LifecycleEvidenceFailure("trusted handshake accepted mismatched identity")


def _raw_state_write_rejected(db_path: Path) -> bool:
    try:
        runtime_adapter_state.record_quarantine_opened(
            db_path,
            {"adapter_id": _ADAPTER_ID, "request_id": "raw-attempt"},  # type: ignore[arg-type]
        )
    except TypeError:
        return True
    return False


def _record_host_protocol_fault(redacted: RedactedDocument) -> Mapping[str, Any]:
    payload = redacted.as_dict()
    return MappingProxyType(
        {
            "kind": "host_outbound_protocol_fault",
            "fault": payload["fault"],
            "request_id": payload["request_id"],
            "quarantines_adapter": False,
            "requires_operator_release": False,
        }
    )


def _validate_lifecycle_observation(observation: LifecycleObservation) -> None:
    expected_health_result = {
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
    }
    if dict(observation.identity_health_result) != expected_health_result:
        raise LifecycleEvidenceFailure("health result did not preserve exact identity fields")
    if not observation.first_health_not_due_before_interval:
        raise LifecycleEvidenceFailure("first health was due before the interval")
    if not observation.first_health_dispatch_at_interval:
        raise LifecycleEvidenceFailure("first health did not dispatch at the interval")
    if HEALTH_INTERVAL_MS != _PROTOCOL_HEALTH_INTERVAL_MS:
        raise LifecycleEvidenceFailure("health interval constant drifted")
    if HEALTH_DEADLINE_MS != _PROTOCOL_HEALTH_DEADLINE_MS:
        raise LifecycleEvidenceFailure("health deadline constant drifted")
    if observation.first_health_due_ms != _INITIALIZED_AT_MS + _PROTOCOL_HEALTH_INTERVAL_MS:
        raise LifecycleEvidenceFailure("first health due time drifted")
    if not observation.valid_health_completed_inside_deadline:
        raise LifecycleEvidenceFailure("valid health response was not accepted inside the deadline")
    if not observation.valid_health_result_exact_identity:
        raise LifecycleEvidenceFailure("health result did not exactly match identity")
    if not observation.later_health_scheduled_from_completion:
        raise LifecycleEvidenceFailure("later health was not scheduled from completion")
    if observation.timeout_fault != HEALTH_TIMEOUT:
        raise LifecycleEvidenceFailure("health timeout used the wrong fault")
    if observation.timeout_actions != ("close_connection", "terminate_process"):
        raise LifecycleEvidenceFailure("health timeout did not close and terminate")
    if not observation.timeout_counted_once:
        raise LifecycleEvidenceFailure("health timeout was not counted exactly once")
    if not observation.timeout_no_replacement_initialized:
        raise LifecycleEvidenceFailure("health timeout initialized replacement state")
    if not observation.old_process_terminated_and_exit_confirmed:
        raise LifecycleEvidenceFailure("deterministic host model did not confirm old process exit")
    if observation.unhealthy_fault != ADAPTER_UNHEALTHY:
        raise LifecycleEvidenceFailure("threshold failure did not mark adapter unhealthy")
    if observation.unhealthy_actions != ("close_connection", "terminate_process", "mark_unhealthy"):
        raise LifecycleEvidenceFailure("threshold failure actions drifted")
    expected_record = {
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
    }
    if dict(observation.unhealthy_record) != expected_record:
        raise LifecycleEvidenceFailure("unhealthy record did not preserve exact identity and attempts")
    if not observation.normal_work_refused_while_unhealthy:
        raise LifecycleEvidenceFailure("unhealthy state did not refuse normal work")
    if not observation.recovery_health_does_not_clear_unhealthy:
        raise LifecycleEvidenceFailure("recovery health auto-cleared unhealthy state")
    if not observation.replacement_deferred_while_unhealthy:
        raise LifecycleEvidenceFailure("unhealthy state admitted replacement without release")


def _validate_recovery_observation(observation: RecoveryObservation) -> None:
    if not observation.trusted_handshake_valid:
        raise LifecycleEvidenceFailure("recovery handshake did not complete trusted identity validation")
    if observation.trusted_handshake_mismatch_fault != "UNTRUSTED_MANIFEST_INPUT":
        raise LifecycleEvidenceFailure("trusted handshake mismatch did not fail closed")
    if observation.quarantined_faults != (ADAPTER_UNHEALTHY, "INVALID_SESSION_REF"):
        raise LifecycleEvidenceFailure("quarantine fault matrix drifted")
    if not observation.quarantine_record_id.startswith("adapter_record_") or len(observation.quarantine_record_id) != 79:
        raise LifecycleEvidenceFailure("quarantine record id was not derived by adapter state")
    if not observation.quarantine_record_opened:
        raise LifecycleEvidenceFailure("quarantine record was not opened")
    expected_identity = dict(_state_identity())
    expected_identity["request_id"] = "attempt-1"
    if dict(observation.quarantine_record_identity) != expected_identity:
        raise LifecycleEvidenceFailure("quarantine record did not preserve exact redacted identity")
    if not observation.quarantine_record_redacted_before_state_append:
        raise LifecycleEvidenceFailure("quarantine record was not redacted before state append")
    if not observation.raw_state_write_rejected:
        raise LifecycleEvidenceFailure("adapter state accepted raw unredacted payload")
    if not observation.host_protocol_fault_recorded:
        raise LifecycleEvidenceFailure("host-owned protocol fault was not recorded")
    if not observation.host_protocol_fault_not_quarantined:
        raise LifecycleEvidenceFailure("host-owned protocol fault quarantined the adapter")
    if not observation.host_protocol_fault_not_released:
        raise LifecycleEvidenceFailure("host-owned protocol fault required operator release")
    if not observation.no_auto_clear_on_recovery_sequence:
        raise LifecycleEvidenceFailure("quarantine state auto-cleared during recovery sequence")
    if not observation.recovery_sequence_preserves_unresolved_attempt:
        raise LifecycleEvidenceFailure("recovery sequence lost unresolved attempts")
    if not observation.release_requires_explicit_release_event:
        raise LifecycleEvidenceFailure("adapter state released without explicit release event")
    if not observation.redaction_preserves_bounded_stderr_metadata:
        raise LifecycleEvidenceFailure("redaction did not preserve bounded stderr metadata")
    if frozenset(observation.deferred_recovery_admission_keys) != _RECOVERY_ADMISSION_DEFERRED:
        raise LifecycleEvidenceFailure("recovery admission deferral set drifted")


def _validate_manifest_provenance_observation(observation: ManifestProvenanceObservation) -> None:
    if observation.resolve_calls != 1:
        raise LifecycleEvidenceFailure("initialize provenance did not use exactly one manifest resolve")
    if not observation.initialized_identity_valid:
        raise LifecycleEvidenceFailure("initialize params were not validated against the resolved manifest")
    if not observation.caller_identity_ignored:
        raise LifecycleEvidenceFailure("caller payload supplied initialize identity")
    if not observation.same_lookup_identity:
        raise LifecycleEvidenceFailure("initialize identity did not come from the same resolved lookup")
    params = dict(observation.initialize_params)
    if set(params) != {
        "requested_protocol_version",
        "adapter_id",
        "adapter_revision",
        "manifest_id",
        "manifest_revision",
        "endpoint",
    }:
        raise LifecycleEvidenceFailure("initialize params shape drifted")
    if not observation.initialize_notification_rejected:
        raise LifecycleEvidenceFailure("initialize notification was not rejected")
    if frozenset(observation.deferred_c16_keys) != _DEFERRED_C16_KEYS:
        raise LifecycleEvidenceFailure("deferred C16 set drifted")


def _validate_p7_integrity_observation(observation: P7IntegrityObservation) -> None:
    if not observation.adapter_rejects_delivery_digest_mismatch:
        raise LifecycleEvidenceFailure("adapter-side DeliveryV1 digest recomputation was not enforced")
    if not observation.host_accepts_receipt_digest_match:
        raise LifecycleEvidenceFailure("host rejected a digest-correct ReceiptV1")
    if not observation.host_rejects_receipt_digest_mismatch:
        raise LifecycleEvidenceFailure("host accepted a digest-mismatched ReceiptV1")
    if not observation.both_delivery_and_receipt_recomputed:
        raise LifecycleEvidenceFailure("delivery and receipt recomputation were not both proven")
    if not observation.invalid_adapter_output_quarantined:
        raise LifecycleEvidenceFailure("invalid adapter output did not open unresolved quarantine state")
    if not observation.digest_mismatch_quarantined:
        raise LifecycleEvidenceFailure("digest-mismatched adapter output did not open unresolved quarantine state")
    if frozenset(observation.deferred_p6_keys) != _DEFERRED_P6_KEYS:
        raise LifecycleEvidenceFailure("deferred P6 set drifted")


def _validate_p6_authority_observation(observation: P6AuthorityObservation) -> None:
    if frozenset(observation.covered_p6_keys) != _P6_AUTHORITY_KEYS:
        raise LifecycleEvidenceFailure("covered P6 authority set drifted")
    if frozenset(observation.deferred_p6_keys) != _DEFERRED_P6_KEYS:
        raise LifecycleEvidenceFailure("deferred P6 integration set drifted")
    if set(observation.validator_mapping) != _P6_AUTHORITY_KEYS:
        raise LifecycleEvidenceFailure("P6 validator mapping does not enumerate exactly the covered rows")
    if not all(observation.validator_mapping[key] for key in observation.validator_mapping):
        raise LifecycleEvidenceFailure("P6 validator mapping contains an empty validator list")
    if not observation.bound_workspace_exact:
        raise LifecycleEvidenceFailure("P6 did not bind exact workspace-scoped capability authority")
    if not observation.bound_project_exact:
        raise LifecycleEvidenceFailure("P6 did not bind exact project-scoped capability authority")
    if not observation.workspace_scope_omits_project:
        raise LifecycleEvidenceFailure("P6 workspace scope did not omit project_id")
    if not observation.project_scope_requires_same_project:
        raise LifecycleEvidenceFailure("P6 project scope did not require matching project_id")
    expected_revalidation = {
        "initialized_adapter_revision",
        "capability_set_revision",
        "capability_set_missing_constraints",
        "project_scope_mismatch",
    }
    if frozenset(observation.initialized_revalidation_failures) != expected_revalidation:
        raise LifecycleEvidenceFailure("P6 initialized revalidation matrix drifted")
    expected_actions = {
        METHOD_DELIVER: "runtime.deliver.observe_only",
        METHOD_CANCEL: "runtime.cancel.observe_only",
        METHOD_RECONCILE: "runtime.reconcile.observe_only",
    }
    if dict(observation.action_methods_authorized) != expected_actions:
        raise LifecycleEvidenceFailure("P6 action relation mapping drifted")
    if not observation.unsupported_method_initializes_but_fails_invocation:
        raise LifecycleEvidenceFailure("P6 unsupported method did not initialize but fail invocation")
    expected_action_failures = {
        "zero_relation",
        "duplicate_relation",
        "absent_token",
        "duplicate_token",
        "unsupported_entry",
        "quality_mismatch",
        "attestation_source_mismatch",
    }
    if frozenset(observation.action_rejection_cases) != expected_action_failures:
        raise LifecycleEvidenceFailure("P6 action rejection matrix drifted")
    expected_profiles = {
        "session_ref": "runtime.session_binding.profile",
        "delivery": "runtime.delivery.profile",
        "receipt": "runtime.receipt.profile",
    }
    if dict(observation.evidence_profiles_authorized) != expected_profiles:
        raise LifecycleEvidenceFailure("P6 evidence-profile mapping drifted")
    expected_evidence_failures = {
        "unregistered_profile",
        "stale_profile_revision",
        "unsupported_profile_entry",
        "quality_ceiling",
        "attestation_revision_mismatch",
    }
    if frozenset(observation.evidence_rejection_cases) != expected_evidence_failures:
        raise LifecycleEvidenceFailure("P6 evidence-profile rejection matrix drifted")
    if not observation.deliver_components_independent:
        raise LifecycleEvidenceFailure("P6 deliver action/session/delivery/receipt components were not independent")
    if not observation.cancel_ordered_stage_independent:
        raise LifecycleEvidenceFailure("P6 cancel ordered-stage authority drifted")
    if not observation.reconcile_components_independent:
        raise LifecycleEvidenceFailure("P6 reconcile ordered-stage authority drifted")
    if frozenset(observation.caller_authority_rejections) != {
        "session_action_relation",
        "evidence_profile_registration",
        "ordered_stage_selected_token",
    }:
        raise LifecycleEvidenceFailure("P6 caller authority rejection matrix drifted")
    if frozenset(observation.no_fallback_rejections) != {
        "cross_workspace_set",
        "unknown_method",
        "relation_cross_set",
        "profile_cross_set",
    }:
        raise LifecycleEvidenceFailure("P6 no-fallback rejection matrix drifted")
    if not observation.protocol_controls_not_product_capabilities:
        raise LifecycleEvidenceFailure("P6 protocol controls were treated as product capabilities")


def _validate_redaction_observation(observation: RedactionObservation) -> None:
    if not observation.redacted_record_id.startswith("adapter_record_"):
        raise LifecycleEvidenceFailure("redacted record id was not state-derived")
    if not observation.sensitive_fields_dropped:
        raise LifecycleEvidenceFailure("redaction preserved dropped sensitive fields")
    if not observation.schema_identifiers_preserved:
        raise LifecycleEvidenceFailure("redaction did not preserve schema identity references")
    if not observation.native_identifiers_hashed:
        raise LifecycleEvidenceFailure("native identifiers were not hashed before persistence")
    if not observation.stderr_bounded_diagnostic_only:
        raise LifecycleEvidenceFailure("stderr redaction persisted raw prefix or unbounded diagnostics")
    if not observation.recorder_accepts_only_redacted_document:
        raise LifecycleEvidenceFailure("redaction recorder accepted a raw payload")
    if observation.redaction_failure_response_name != "REDACTION_FAILURE":
        raise LifecycleEvidenceFailure("redaction failure returned the wrong error name")
    if observation.redaction_failure_response_code != ERROR_CODES["REDACTION_FAILURE"]:
        raise LifecycleEvidenceFailure("redaction failure returned the wrong error code")
    if not observation.redaction_failure_quarantines_adapter:
        raise LifecycleEvidenceFailure("redaction failure did not quarantine the adapter")
    if observation.redaction_failure_wrote_state:
        raise LifecycleEvidenceFailure("redaction failure persisted state before proving redaction")


def _validate_structured_error_observation(observation: StructuredErrorObservation) -> None:
    if observation.adapter_output_fault != "INVALID_REQUEST":
        raise LifecycleEvidenceFailure("adapter output failure used the wrong ordered enum")
    if observation.adapter_output_code != ERROR_CODES["INVALID_REQUEST"]:
        raise LifecycleEvidenceFailure("adapter output failure code did not come from ERROR_CODES")
    if not observation.adapter_output_recorded_locally:
        raise LifecycleEvidenceFailure("adapter output failure was not recorded locally")
    if not observation.adapter_output_quarantined:
        raise LifecycleEvidenceFailure("adapter output failure did not quarantine the adapter")
    if observation.adapter_output_response_to_adapter is not None:
        raise LifecycleEvidenceFailure("host responded to adapter output failure")
    if not observation.closed_error_envelope_validates:
        raise LifecycleEvidenceFailure("closed error envelope did not validate")
    if frozenset(observation.retryability_deferred_keys) != _DEFERRED_RETRYABILITY_KEYS:
        raise LifecycleEvidenceFailure("retryability deferral set drifted")
    if observation.retryability_deferred_reason != _DEFERRED_RETRYABILITY_REASON:
        raise LifecycleEvidenceFailure("retryability deferral reason drifted")


def _validate_shutdown_observation(observation: ShutdownObservation) -> None:
    if dict(observation.shutdown_success_result) != {"status": "shutdown_started"}:
        raise LifecycleEvidenceFailure("shutdown success result was not exact")
    if not observation.shutdown_request_connection_scoped:
        raise LifecycleEvidenceFailure("shutdown was not modeled as connection-scoped")
    if not observation.shutdown_rejects_session_selector:
        raise LifecycleEvidenceFailure("shutdown accepted a session selector")
    if observation.invalid_params_fault != "INVALID_PARAMS":
        raise LifecycleEvidenceFailure("invalid shutdown params did not return INVALID_PARAMS")
    if not observation.invalid_params_did_not_begin_shutdown:
        raise LifecycleEvidenceFailure("invalid shutdown params began shutdown")
    if not observation.adapter_enters_shutdown_before_success:
        raise LifecycleEvidenceFailure("adapter did not enter inert shutdown state before success was trusted")
    if observation.adapter_refuses_later_work_fault != SHUTDOWN_IN_PROGRESS:
        raise LifecycleEvidenceFailure("adapter later-work refusal was not SHUTDOWN_IN_PROGRESS")
    if observation.lifecycle_shutdown_actions != ("stop_admitting_new_work",):
        raise LifecycleEvidenceFailure("shutdown lifecycle did not stop admitting new work")
    if observation.lifecycle_drain_deadline_ms != 2_000 + SHUTDOWN_DRAIN_MS:
        raise LifecycleEvidenceFailure("shutdown drain deadline drifted")
    if observation.lifecycle_hard_kill_deadline_ms != 2_000 + SHUTDOWN_HARD_KILL_MS:
        raise LifecycleEvidenceFailure("shutdown hard-kill deadline drifted")
    if observation.lifecycle_refuses_later_work_fault != SHUTDOWN_IN_PROGRESS:
        raise LifecycleEvidenceFailure("lifecycle later-work refusal drifted")
    if not observation.second_shutdown_delegated_to_capacity_policy:
        raise LifecycleEvidenceFailure("second shutdown capacity exception drifted")
    if observation.drain_actions != ("continue_drain_without_outcome",):
        raise LifecycleEvidenceFailure("drain deadline action drifted")
    if observation.drain_unresolved_attempts != ("attempt-1", "attempt-2"):
        raise LifecycleEvidenceFailure("drain did not preserve uncertain attempts")
    if observation.drain_authoritative_outcome:
        raise LifecycleEvidenceFailure("drain produced authoritative outcome without evidence")
    if observation.hard_kill_actions != ("hard_kill_process", "continue_stderr_drain"):
        raise LifecycleEvidenceFailure("hard-kill disposition drifted")
    if observation.hard_kill_unresolved_attempts != ("attempt-1", "attempt-2"):
        raise LifecycleEvidenceFailure("hard kill did not preserve uncertain attempts")
    if observation.hard_kill_authoritative_outcome:
        raise LifecycleEvidenceFailure("hard kill produced authoritative outcome without evidence")
    if "live protocol-output flush" not in observation.covered_flush_boundary:
        raise LifecycleEvidenceFailure("shutdown flush/exit boundary was not explicit")
    if frozenset(observation.deferred_shutdown_keys) != _DEFERRED_SHUTDOWN_KEYS:
        raise LifecycleEvidenceFailure("shutdown deferral set drifted")
    if dict(observation.deferred_shutdown_reasons) != dict(_DEFERRED_SHUTDOWN_REASONS):
        raise LifecycleEvidenceFailure("shutdown deferral reasons drifted")


def _validate_c01_local_fault_observation(observation: C01LocalFaultObservation) -> None:
    if observation.adapter_request_fault != "INVALID_REQUEST":
        raise LifecycleEvidenceFailure("adapter-originated request used the wrong local fault")
    if not observation.adapter_request_no_response:
        raise LifecycleEvidenceFailure("host responded to an adapter-originated request")
    if not observation.adapter_request_quarantined:
        raise LifecycleEvidenceFailure("adapter-originated request did not quarantine the adapter")
    if not observation.adapter_request_recorded_locally:
        raise LifecycleEvidenceFailure("adapter-originated request fault was not recorded locally")
    if not observation.adapter_request_no_state_advance:
        raise LifecycleEvidenceFailure("adapter-originated request advanced host state beyond the local fault")
    if not observation.host_response_closed_without_response:
        raise LifecycleEvidenceFailure("host-originated response did not close without a response")
    if not observation.host_response_subsequent_input_refused:
        raise LifecycleEvidenceFailure("host-originated response did not leave the adapter inert")
    if not observation.host_response_recorded_locally:
        raise LifecycleEvidenceFailure("host-originated response fault was not recorded locally")
    if not observation.host_response_not_quarantined:
        raise LifecycleEvidenceFailure("host-originated response fault quarantined the adapter")
    if not observation.host_response_no_operator_release:
        raise LifecycleEvidenceFailure("host-originated response fault required operator release")
    if observation.malformed_adapter_output_fault != "PARSE_ERROR":
        raise LifecycleEvidenceFailure("malformed adapter output used the wrong local fault")
    if not observation.malformed_adapter_output_no_response:
        raise LifecycleEvidenceFailure("host responded to malformed adapter output")
    if not observation.malformed_adapter_output_quarantined:
        raise LifecycleEvidenceFailure("malformed adapter output did not quarantine the adapter")
    if not observation.malformed_adapter_output_recorded_locally:
        raise LifecycleEvidenceFailure("malformed adapter output was not recorded locally")
    if frozenset(observation.deferred_live_c01_keys) != _DEFERRED_C01_LIVE_KEYS:
        raise LifecycleEvidenceFailure("deferred C01 live-stream set drifted")
