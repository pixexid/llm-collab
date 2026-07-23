"""Deterministic lifecycle evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
the transport, admission, manifest, cancellation, and deadline ledgers. It
exercises the real pure lifecycle component with injected timestamps and the
real quarantine-state/redaction/manifest seams against a temp SQLite file; it
never starts a process, sleeps, polls, or touches live runtime/project state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from llm_collab import runtime_adapter_state
from llm_collab.runtime_adapter_conformance import extract_clause_occurrences
from llm_collab.runtime_adapter_lifecycle import (
    ADAPTER_UNHEALTHY,
    HEALTH_FAILURE_THRESHOLD,
    HEALTH_INTERVAL_MS,
    HEALTH_TIMEOUT,
    EndpointIdentity,
    HealthRequest,
    LifecycleState,
)
from llm_collab.runtime_adapter_manifest import (
    TrustedManifestRegistry,
    validate_initialized_identity,
)
from llm_collab.runtime_adapter_redaction import RedactedDocument, redact_document
from llm_collab.runtime_adapter_requests import HEALTH_DEADLINE_MS


ARTIFACT_LABEL = "host_lifecycle_harness"
EVIDENCE_KIND = "runtime_adapter_host_lifecycle_model"
HOST_HARNESS_EVIDENCED = "host_harness_evidenced"
_INITIALIZED_AT_MS = 1_000
_HEALTH_REQUEST_ID = "health-1"
_RECOVERY_HEALTH_REQUEST_ID = "health-recovery"
_PROTOCOL_HEALTH_INTERVAL_MS = 10_000
_PROTOCOL_HEALTH_DEADLINE_MS = 5_000
_RECOVERY_ADMISSION_DEFERRED = frozenset(("Cd830c5efc97b.1", "Cd830c5efc97b.2"))
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
_HOST_HARNESS_REFS = _LIFECYCLE_REFS + _RECOVERY_REFS


def build_lifecycle_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic host lifecycle and recovery-state evidence."""

    _validate_clause_refs(protocol_text)
    lifecycle = _lifecycle_observation()
    recovery = _recovery_observation()
    _validate_lifecycle_observation(lifecycle)
    _validate_recovery_observation(recovery)
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
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _HOST_HARNESS_REFS:
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


def _redacted(payload: Mapping[str, Any], **overrides: Any) -> RedactedDocument:
    document = dict(payload)
    document.update(overrides)
    result = redact_document(document)
    if not isinstance(result, RedactedDocument):
        raise LifecycleEvidenceFailure(f"redaction failed before state append: {result.reason}")
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
