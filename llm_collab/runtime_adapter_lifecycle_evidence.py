"""Deterministic lifecycle evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
the transport, admission, manifest, cancellation, and deadline ledgers. It
exercises the real pure lifecycle component with injected timestamps and a
minimal in-memory host model; it never starts a process, sleeps, polls, persists,
or touches runtime/project state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

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
from llm_collab.runtime_adapter_requests import HEALTH_DEADLINE_MS


ARTIFACT_LABEL = "host_lifecycle_harness"
EVIDENCE_KIND = "runtime_adapter_host_lifecycle_model"
HOST_HARNESS_EVIDENCED = "host_harness_evidenced"
_INITIALIZED_AT_MS = 1_000
_HEALTH_REQUEST_ID = "health-1"
_RECOVERY_HEALTH_REQUEST_ID = "health-recovery"
_PROTOCOL_HEALTH_INTERVAL_MS = 10_000
_PROTOCOL_HEALTH_DEADLINE_MS = 5_000


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


def build_lifecycle_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic C11 lifecycle evidence."""

    _validate_clause_refs(protocol_text)
    observation = _lifecycle_observation()
    _validate_observation(observation)
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
            for ref in _LIFECYCLE_REFS
        ),
        "observation": {
            "identity_health_result": dict(observation.identity_health_result),
            "first_health_not_due_before_interval": observation.first_health_not_due_before_interval,
            "first_health_dispatch_at_interval": observation.first_health_dispatch_at_interval,
            "first_health_due_ms": observation.first_health_due_ms,
            "valid_health_completed_inside_deadline": observation.valid_health_completed_inside_deadline,
            "valid_health_result_exact_identity": observation.valid_health_result_exact_identity,
            "later_health_due_from_completion_ms": observation.later_health_due_from_completion_ms,
            "later_health_due_from_dispatch_ms": observation.later_health_due_from_dispatch_ms,
            "later_health_scheduled_from_completion": observation.later_health_scheduled_from_completion,
            "timeout_fault": observation.timeout_fault,
            "timeout_actions": observation.timeout_actions,
            "timeout_counted_once": observation.timeout_counted_once,
            "timeout_no_replacement_initialized": observation.timeout_no_replacement_initialized,
            "old_process_terminated_and_exit_confirmed": observation.old_process_terminated_and_exit_confirmed,
            "deterministic_host_boundary": (
                "models lifecycle disposition only; live OS exit waiting remains outside this evidence"
            ),
            "unhealthy_fault": observation.unhealthy_fault,
            "unhealthy_actions": observation.unhealthy_actions,
            "unhealthy_record": dict(observation.unhealthy_record),
            "normal_work_refused_while_unhealthy": observation.normal_work_refused_while_unhealthy,
            "recovery_health_does_not_clear_unhealthy": observation.recovery_health_does_not_clear_unhealthy,
            "replacement_deferred_while_unhealthy": observation.replacement_deferred_while_unhealthy,
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _LIFECYCLE_REFS:
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


def _validate_observation(observation: LifecycleObservation) -> None:
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
