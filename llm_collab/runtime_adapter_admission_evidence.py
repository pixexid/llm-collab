"""Deterministic request-admission evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
raw transport evidence. It exercises the pure in-memory ``RequestPolicy`` model
for host admission limits that cannot be proven by synchronous wire replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from llm_collab.runtime_adapter_conformance import extract_clause_occurrences
from llm_collab.runtime_adapter_requests import (
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
    MAX_IN_FLIGHT_REQUESTS,
    TOO_MANY_IN_FLIGHT,
    DeliveryRef,
    RequestPolicy,
    _METHOD_CAPACITIES,
)


ARTIFACT_LABEL = "admission_bounded"
ADMISSION_EVIDENCED = "admission_evidenced"
_EXPECTED_TOTAL_CAPACITY = 32
_CONTROL_METHODS = (METHOD_CANCEL, METHOD_RECONCILE, METHOD_HEALTH, METHOD_SHUTDOWN)
_POST_INITIALIZE_METHODS = (METHOD_DELIVER, *_CONTROL_METHODS)


class AdmissionEvidenceFailure(AssertionError):
    """Raised when admission evidence cannot be built honestly."""


@dataclass(frozen=True)
class AdmissionClauseRef:
    clause_key: str
    text_sha256: str


@dataclass(frozen=True)
class AdmissionObservation:
    method_capacities: Mapping[str, int]
    method_cap_faults: Mapping[str, str]
    max_in_flight_requests: int
    capacity_sum: int
    fill_to_capacity_count: int
    rejected_33rd_fault: str
    health_full_deliver_free_fault: str
    deliver_full_health_free_fault: str
    rejected_delivery_pending_count: int
    rejected_delivery_in_flight_count: int


_ADMISSION_REFS: tuple[AdmissionClauseRef, ...] = (
    AdmissionClauseRef(
        "C625baded5cd3.1",
        "625baded5cd3821b3af25b04897e506b3af6d1b6b9f65ff2bf33c06360638400",
    ),
    AdmissionClauseRef(
        "C69d1a7ac8fee.1",
        "69d1a7ac8fee7a982acbdc0b6787f5cdb46ee4b7db4420ab87244628e340cbb2",
    ),
    AdmissionClauseRef(
        "C69d1a7ac8fee.2",
        "69d1a7ac8fee7a982acbdc0b6787f5cdb46ee4b7db4420ab87244628e340cbb2",
    ),
    AdmissionClauseRef(
        "C69d1a7ac8fee.3",
        "69d1a7ac8fee7a982acbdc0b6787f5cdb46ee4b7db4420ab87244628e340cbb2",
    ),
    AdmissionClauseRef(
        "Ca135a0f3d7e4.1",
        "a135a0f3d7e48f8666a0b43bda00e908436d751f8622200ce0e18238a3cb4f91",
    ),
    AdmissionClauseRef(
        "Ca135a0f3d7e4.2",
        "a135a0f3d7e48f8666a0b43bda00e908436d751f8622200ce0e18238a3cb4f91",
    ),
    AdmissionClauseRef(
        "Ca135a0f3d7e4.3",
        "a135a0f3d7e48f8666a0b43bda00e908436d751f8622200ce0e18238a3cb4f91",
    ),
)


def build_admission_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic evidence for request admission policy limits."""

    _validate_clause_refs(protocol_text)
    observation = _admission_observation()
    return {
        "schema_version": 1,
        "protocol": "runtime-adapter-jsonrpc-v1",
        "artifact_label": ARTIFACT_LABEL,
        "evidence_kind": "request_admission_policy",
        "claim": ADMISSION_EVIDENCED,
        "clauses": tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": ADMISSION_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _ADMISSION_REFS
        ),
        "observation": {
            "method_capacities": dict(observation.method_capacities),
            "method_cap_faults": dict(observation.method_cap_faults),
            "max_in_flight_requests": observation.max_in_flight_requests,
            "capacity_sum": observation.capacity_sum,
            "fill_to_capacity_count": observation.fill_to_capacity_count,
            "rejected_33rd_fault": observation.rejected_33rd_fault,
            "health_full_deliver_free_fault": observation.health_full_deliver_free_fault,
            "deliver_full_health_free_fault": observation.deliver_full_health_free_fault,
            "rejected_delivery_pending_count": observation.rejected_delivery_pending_count,
            "rejected_delivery_in_flight_count": observation.rejected_delivery_in_flight_count,
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _ADMISSION_REFS:
        clause = live.get(ref.clause_key)
        if clause is None:
            raise AdmissionEvidenceFailure(f"missing admission clause: {ref.clause_key}")
        if clause.text_sha256 != ref.text_sha256:
            raise AdmissionEvidenceFailure(f"stale admission clause: {ref.clause_key}")


def _admission_observation() -> AdmissionObservation:
    capacities = dict(_METHOD_CAPACITIES)
    _validate_capacity_partition(capacities)

    fill_policy = RequestPolicy()
    for method in _POST_INITIALIZE_METHODS:
        for index in range(capacities[method]):
            result = fill_policy.begin_request(
                method,
                _request_id(method, index),
                received_at_ms=0,
                delivery=_delivery(index) if method == METHOD_DELIVER else None,
            )
            if not result.accepted:
                raise AdmissionEvidenceFailure(f"{method} rejected before its capacity")

    if fill_policy.in_flight_count != MAX_IN_FLIGHT_REQUESTS:
        raise AdmissionEvidenceFailure("filled request count does not match max in-flight requests")
    before_33rd = fill_policy.snapshot()
    rejected_33rd_delivery = _delivery(999)
    rejected_33rd = fill_policy.begin_request(
        METHOD_DELIVER,
        rejected_33rd_delivery.original_request_id,
        received_at_ms=0,
        delivery=rejected_33rd_delivery,
    )
    _assert_refused_without_mutation(
        fill_policy,
        before_33rd,
        rejected_33rd,
        "33rd post-initialize request",
    )

    health_fault = _health_full_deliver_free_fault()
    deliver_fault, pending_count, in_flight_count = _deliver_full_health_free_fault()
    return AdmissionObservation(
        method_capacities=capacities,
        method_cap_faults=_per_method_cap_faults(capacities),
        max_in_flight_requests=MAX_IN_FLIGHT_REQUESTS,
        capacity_sum=sum(capacities.values()),
        fill_to_capacity_count=fill_policy.in_flight_count,
        rejected_33rd_fault=str(rejected_33rd.fault),
        health_full_deliver_free_fault=health_fault,
        deliver_full_health_free_fault=deliver_fault,
        rejected_delivery_pending_count=pending_count,
        rejected_delivery_in_flight_count=in_flight_count,
    )


def _validate_capacity_partition(capacities: Mapping[str, int]) -> None:
    if set(capacities) != set(_POST_INITIALIZE_METHODS):
        raise AdmissionEvidenceFailure("admission capacities do not cover exactly post-initialize methods")
    if sum(capacities.values()) != MAX_IN_FLIGHT_REQUESTS:
        raise AdmissionEvidenceFailure("total admission capacity must be the sum of method pools")
    if MAX_IN_FLIGHT_REQUESTS != _EXPECTED_TOTAL_CAPACITY:
        raise AdmissionEvidenceFailure("protocol total capacity drifted")
    if sum(capacities.values()) != _EXPECTED_TOTAL_CAPACITY:
        raise AdmissionEvidenceFailure("method-pool sum drifted from protocol total")


def _health_full_deliver_free_fault() -> str:
    policy = RequestPolicy()
    first = policy.begin_request(METHOD_HEALTH, "health-1", received_at_ms=0)
    if not first.accepted:
        raise AdmissionEvidenceFailure("first health request must be accepted")

    before = policy.snapshot()
    refused = policy.begin_request(METHOD_HEALTH, "health-2", received_at_ms=0)
    _assert_refused_without_mutation(policy, before, refused, "second health request")

    free_delivery = _delivery(1)
    delivery = policy.begin_request(
        METHOD_DELIVER,
        free_delivery.original_request_id,
        received_at_ms=0,
        delivery=free_delivery,
    )
    if not delivery.accepted:
        raise AdmissionEvidenceFailure("deliver pool must remain available when health pool is full")
    return str(refused.fault)


def _deliver_full_health_free_fault() -> tuple[str, int, int]:
    policy = RequestPolicy()
    for index in range(_METHOD_CAPACITIES[METHOD_DELIVER]):
        delivery = _delivery(index)
        result = policy.begin_request(
            METHOD_DELIVER,
            delivery.original_request_id,
            received_at_ms=0,
            delivery=delivery,
        )
        if not result.accepted:
            raise AdmissionEvidenceFailure("delivery rejected before delivery capacity")

    before = policy.snapshot()
    over_cap = _delivery(999)
    refused = policy.begin_request(
        METHOD_DELIVER,
        over_cap.original_request_id,
        received_at_ms=0,
        delivery=over_cap,
    )
    _assert_refused_without_mutation(policy, before, refused, "over-cap delivery request")
    if over_cap.original_request_id in policy.snapshot().pending_deliveries:
        raise AdmissionEvidenceFailure("rejected delivery was queued")

    health = policy.begin_request(METHOD_HEALTH, "health-free", received_at_ms=0)
    if not health.accepted:
        raise AdmissionEvidenceFailure("health pool must remain available when delivery pool is full")
    after_refusal = before
    return (
        str(refused.fault),
        len(after_refusal.pending_deliveries),
        sum(len(ids) for ids in after_refusal.in_flight_by_method.values()),
    )


def _per_method_cap_faults(capacities: Mapping[str, int]) -> Mapping[str, str]:
    faults: dict[str, str] = {}
    for method in _POST_INITIALIZE_METHODS:
        policy = RequestPolicy()
        for index in range(capacities[method]):
            result = policy.begin_request(
                method,
                _request_id(method, index),
                received_at_ms=0,
                delivery=_delivery(index) if method == METHOD_DELIVER else None,
            )
            if not result.accepted:
                raise AdmissionEvidenceFailure(f"{method} rejected before its exact cap")
        before = policy.snapshot()
        over_cap = policy.begin_request(
            method,
            _request_id(method, 999),
            received_at_ms=0,
            delivery=_delivery(999) if method == METHOD_DELIVER else None,
        )
        _assert_refused_without_mutation(policy, before, over_cap, f"{method} cap+1 request")
        faults[method] = str(over_cap.fault)
    return faults


def _assert_refused_without_mutation(
    policy: RequestPolicy,
    before: object,
    result: object,
    label: str,
) -> None:
    accepted = getattr(result, "accepted", None)
    fault = getattr(result, "fault", None)
    if accepted is not False or fault != TOO_MANY_IN_FLIGHT:
        raise AdmissionEvidenceFailure(f"{label} must refuse with TOO_MANY_IN_FLIGHT")
    if policy.snapshot() != before:
        raise AdmissionEvidenceFailure(f"{label} refusal mutated policy state")


def _delivery(index: int) -> DeliveryRef:
    return DeliveryRef(
        session_ref={
            "workspace_id": "ws",
            "project_id": "amiga",
            "native_session_id": "native",
        },
        original_request_id=_request_id(METHOD_DELIVER, index),
        delivery_id=f"delivery-{index}",
        attempt_id=f"attempt-{index}",
    )


def _request_id(method: str, index: int) -> str:
    name = method.split(".", 1)[1]
    return f"{name}-{index}"
