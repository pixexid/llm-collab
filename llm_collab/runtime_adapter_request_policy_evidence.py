"""Deterministic request-policy cancellation evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
the admission, transport, and manifest ledgers. It exercises the pure
in-memory ``RequestPolicy.cancel_delivery`` behavior for C09 cancellation rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from llm_collab.runtime_adapter_conformance import extract_clause_occurrences
from llm_collab.runtime_adapter_requests import (
    INVALID_DELIVERY,
    METHOD_CANCEL,
    METHOD_DELIVER,
    RECONCILIATION_REQUIRED,
    REQUEST_CANCELLED,
    DeliveryRef,
    RequestPolicy,
)


ARTIFACT_LABEL = "request_policy_cancellation"
CANCELLATION_EVIDENCED = "cancellation_evidenced"
_CANCEL_REQUEST_ID = "cancel-1"


class RequestPolicyEvidenceFailure(AssertionError):
    """Raised when request-policy cancellation evidence cannot be built honestly."""


@dataclass(frozen=True)
class RequestPolicyClauseRef:
    clause_key: str
    text_sha256: str


@dataclass(frozen=True)
class CancellationObservation:
    success_original_request_id: str | int | float
    success_delivery_id: str
    success_attempt_id: str
    success_status: str
    success_original_fault: str
    success_state_advanced: bool
    clean_non_acceptance_succeeded: bool
    exact_repeat_same_terminal_result: bool
    pending_removed_after_success: bool
    live_mismatch_faults: Mapping[str, str]
    live_mismatch_no_mutation: Mapping[str, bool]
    live_mismatch_pending_preserved: Mapping[str, bool]
    terminal_mismatch_faults: Mapping[str, str]
    terminal_mismatch_no_mutation: Mapping[str, bool]
    acceptance_fault: str
    acceptance_unresolved: bool
    acceptance_ok: bool
    pending_preserved_on_reconciliation: bool
    unresolved_preserved_on_reconciliation: bool


_CANCELLATION_REFS: tuple[RequestPolicyClauseRef, ...] = (
    RequestPolicyClauseRef(
        "C8fc80ae367f5.1",
        "8fc80ae367f527559cb3eacd58cce2851512af58e5f844b1f576a5765b5a13a1",
    ),
    RequestPolicyClauseRef(
        "Ce2a523abc63e.1",
        "e2a523abc63ebe44116a947b91d11851fcf7b8337c0eca8d14e74856d85f2b6f",
    ),
    RequestPolicyClauseRef(
        "C3d72f2d559af.1",
        "3d72f2d559afeb457c439d3b309e031ce17fade1254239788c4502764ea0e0cd",
    ),
    RequestPolicyClauseRef(
        "C5bdcee0ed51e.1",
        "5bdcee0ed51ef0e22cc0692d3d04b20ca078a0425387d5f8615585646819a74b",
    ),
    RequestPolicyClauseRef(
        "C95661fc1714b.1",
        "95661fc1714b40597cf47fe9509620b749e0ae3174721ac50b0bfb2c06076e0a",
    ),
    RequestPolicyClauseRef(
        "C6a5b4ceb5c98.1",
        "6a5b4ceb5c9855a00ca89119abf0cf627944a52453344e5fa9d979337e9d9df3",
    ),
    RequestPolicyClauseRef(
        "C6a5b4ceb5c98.2",
        "6a5b4ceb5c9855a00ca89119abf0cf627944a52453344e5fa9d979337e9d9df3",
    ),
)


def build_request_policy_cancellation_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic evidence for C09 request-policy cancellation behavior."""

    _validate_clause_refs(protocol_text)
    observation = _cancellation_observation()
    _validate_observation(observation)
    return {
        "schema_version": 1,
        "protocol": "runtime-adapter-jsonrpc-v1",
        "artifact_label": ARTIFACT_LABEL,
        "evidence_kind": "request_policy_cancellation",
        "claim": CANCELLATION_EVIDENCED,
        "clauses": tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": CANCELLATION_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _CANCELLATION_REFS
        ),
        "observation": {
            "success_original_request_id": observation.success_original_request_id,
            "success_delivery_id": observation.success_delivery_id,
            "success_attempt_id": observation.success_attempt_id,
            "success_status": observation.success_status,
            "success_original_fault": observation.success_original_fault,
            "success_state_advanced": observation.success_state_advanced,
            "clean_non_acceptance_succeeded": observation.clean_non_acceptance_succeeded,
            "exact_repeat_same_terminal_result": observation.exact_repeat_same_terminal_result,
            "pending_removed_after_success": observation.pending_removed_after_success,
            "live_mismatch_faults": dict(observation.live_mismatch_faults),
            "live_mismatch_no_mutation": dict(observation.live_mismatch_no_mutation),
            "live_mismatch_pending_preserved": dict(observation.live_mismatch_pending_preserved),
            "terminal_mismatch_faults": dict(observation.terminal_mismatch_faults),
            "terminal_mismatch_no_mutation": dict(observation.terminal_mismatch_no_mutation),
            "acceptance_fault": observation.acceptance_fault,
            "acceptance_unresolved": observation.acceptance_unresolved,
            "acceptance_ok": observation.acceptance_ok,
            "pending_preserved_on_reconciliation": observation.pending_preserved_on_reconciliation,
            "unresolved_preserved_on_reconciliation": observation.unresolved_preserved_on_reconciliation,
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _CANCELLATION_REFS:
        clause = live.get(ref.clause_key)
        if clause is None:
            raise RequestPolicyEvidenceFailure(f"missing cancellation clause: {ref.clause_key}")
        if clause.text_sha256 != ref.text_sha256:
            raise RequestPolicyEvidenceFailure(f"stale cancellation clause: {ref.clause_key}")


def _cancellation_observation() -> CancellationObservation:
    success_policy, success_delivery = _admitted_cancel_policy()
    success = success_policy.cancel_delivery(
        cancel_request_id=_CANCEL_REQUEST_ID,
        session_ref=success_delivery.session_ref,
        original_request_id=success_delivery.original_request_id,
        delivery_id=success_delivery.delivery_id,
        attempt_id=success_delivery.attempt_id,
        acceptance_may_have_occurred=False,
    )
    repeat = success_policy.cancel_delivery(
        cancel_request_id=_CANCEL_REQUEST_ID,
        session_ref=success_delivery.session_ref,
        original_request_id=success_delivery.original_request_id,
        delivery_id=success_delivery.delivery_id,
        attempt_id=success_delivery.attempt_id,
        acceptance_may_have_occurred=False,
    )

    live_mismatch_faults: dict[str, str] = {}
    live_mismatch_no_mutation: dict[str, bool] = {}
    live_mismatch_pending_preserved: dict[str, bool] = {}
    for name in _mismatch_params(_delivery()):
        live_policy, live_delivery = _admitted_cancel_policy()
        before = live_policy.snapshot()
        refused = live_policy.cancel_delivery(cancel_request_id=_CANCEL_REQUEST_ID, **_mismatch_params(live_delivery)[name])
        after = live_policy.snapshot()
        live_mismatch_faults[name] = str(refused.fault)
        live_mismatch_no_mutation[name] = after == before
        live_mismatch_pending_preserved[name] = live_delivery.original_request_id in after.pending_deliveries

    terminal_mismatch_faults: dict[str, str] = {}
    terminal_mismatch_no_mutation: dict[str, bool] = {}
    for name, params in _mismatch_params(success_delivery).items():
        before = success_policy.snapshot()
        refused = success_policy.cancel_delivery(cancel_request_id=_CANCEL_REQUEST_ID, **params)
        terminal_mismatch_faults[name] = str(refused.fault)
        terminal_mismatch_no_mutation[name] = success_policy.snapshot() == before

    reconcile_policy, reconcile_delivery = _admitted_cancel_policy()
    reconcile = reconcile_policy.cancel_delivery(
        cancel_request_id=_CANCEL_REQUEST_ID,
        session_ref=reconcile_delivery.session_ref,
        original_request_id=reconcile_delivery.original_request_id,
        delivery_id=reconcile_delivery.delivery_id,
        attempt_id=reconcile_delivery.attempt_id,
        acceptance_may_have_occurred=True,
    )
    reconcile_snapshot = reconcile_policy.snapshot()
    return CancellationObservation(
        success_original_request_id=str(success.original_request_id),
        success_delivery_id=str(success.delivery_id),
        success_attempt_id=str(success.attempt_id),
        success_status=str(success.status),
        success_original_fault=str(success.original_fault),
        success_state_advanced=success.state_advanced,
        clean_non_acceptance_succeeded=success.ok,
        exact_repeat_same_terminal_result=repeat == success,
        pending_removed_after_success=success_delivery.original_request_id not in success_policy.snapshot().pending_deliveries,
        live_mismatch_faults=live_mismatch_faults,
        live_mismatch_no_mutation=live_mismatch_no_mutation,
        live_mismatch_pending_preserved=live_mismatch_pending_preserved,
        terminal_mismatch_faults=terminal_mismatch_faults,
        terminal_mismatch_no_mutation=terminal_mismatch_no_mutation,
        acceptance_fault=str(reconcile.fault),
        acceptance_unresolved=reconcile.unresolved,
        acceptance_ok=reconcile.ok,
        pending_preserved_on_reconciliation=reconcile_delivery.original_request_id in reconcile_snapshot.pending_deliveries,
        unresolved_preserved_on_reconciliation=reconcile_delivery.original_request_id in reconcile_snapshot.unresolved,
    )


def _admitted_cancel_policy() -> tuple[RequestPolicy, DeliveryRef]:
    policy = RequestPolicy()
    delivery = _delivery()
    delivered = policy.begin_request(METHOD_DELIVER, delivery.original_request_id, received_at_ms=0, delivery=delivery)
    cancel = policy.begin_request(METHOD_CANCEL, _CANCEL_REQUEST_ID, received_at_ms=0)
    if not delivered.accepted or not cancel.accepted:
        raise RequestPolicyEvidenceFailure("valid delivery and cancel must be admitted before cancellation")
    return policy, delivery


def _delivery() -> DeliveryRef:
    return DeliveryRef(
        session_ref={
            "workspace_id": "ws",
            "project_id": "amiga",
            "native_session_id": "native",
        },
        original_request_id="orig-1",
        delivery_id="delivery-1",
        attempt_id="attempt-1",
    )


def _mismatch_params(delivery: DeliveryRef) -> Mapping[str, Mapping[str, object]]:
    exact = {
        "session_ref": delivery.session_ref,
        "original_request_id": delivery.original_request_id,
        "delivery_id": delivery.delivery_id,
        "attempt_id": delivery.attempt_id,
    }
    return {
        "session_ref": {
            **exact,
            "session_ref": {
                "workspace_id": "ws",
                "project_id": "amiga",
            },
        },
        "original_request_id": {**exact, "original_request_id": "orig-other"},
        "delivery_id": {**exact, "delivery_id": "delivery-other"},
        "attempt_id": {**exact, "attempt_id": "attempt-other"},
    }


def _validate_observation(observation: CancellationObservation) -> None:
    expected_delivery = _delivery()
    if observation.success_original_request_id != expected_delivery.original_request_id:
        raise RequestPolicyEvidenceFailure("cancel success returned the wrong original_request_id")
    if observation.success_delivery_id != expected_delivery.delivery_id:
        raise RequestPolicyEvidenceFailure("cancel success returned the wrong delivery_id")
    if observation.success_attempt_id != expected_delivery.attempt_id:
        raise RequestPolicyEvidenceFailure("cancel success returned the wrong attempt_id")
    if observation.success_status != "cancelled":
        raise RequestPolicyEvidenceFailure("cancel success returned the wrong status")
    if observation.success_original_fault != REQUEST_CANCELLED:
        raise RequestPolicyEvidenceFailure("cancel success did not terminate original request as REQUEST_CANCELLED")
    if not observation.success_state_advanced:
        raise RequestPolicyEvidenceFailure("cancel success did not advance cancellation state")
    if not observation.clean_non_acceptance_succeeded:
        raise RequestPolicyEvidenceFailure("clean non-acceptance cancel did not succeed")
    if not observation.exact_repeat_same_terminal_result:
        raise RequestPolicyEvidenceFailure("exact-key cancellation repeat was not idempotent")
    if not observation.pending_removed_after_success:
        raise RequestPolicyEvidenceFailure("cancel success did not remove pending delivery")
    expected_mismatch_names = set(_mismatch_params(expected_delivery))
    if set(observation.live_mismatch_faults) != expected_mismatch_names:
        raise RequestPolicyEvidenceFailure("live identity mismatch coverage drifted")
    if any(fault != INVALID_DELIVERY for fault in observation.live_mismatch_faults.values()):
        raise RequestPolicyEvidenceFailure("live identity mismatch used the wrong fault")
    if not all(observation.live_mismatch_no_mutation.values()):
        raise RequestPolicyEvidenceFailure("live identity mismatch mutated policy state")
    if not all(observation.live_mismatch_pending_preserved.values()):
        raise RequestPolicyEvidenceFailure("live identity mismatch did not preserve pending delivery")
    if set(observation.terminal_mismatch_faults) != expected_mismatch_names:
        raise RequestPolicyEvidenceFailure("terminal identity mismatch coverage drifted")
    if any(fault != INVALID_DELIVERY for fault in observation.terminal_mismatch_faults.values()):
        raise RequestPolicyEvidenceFailure("terminal identity mismatch used the wrong fault")
    if not all(observation.terminal_mismatch_no_mutation.values()):
        raise RequestPolicyEvidenceFailure("terminal identity mismatch mutated policy state")
    if observation.acceptance_fault != RECONCILIATION_REQUIRED:
        raise RequestPolicyEvidenceFailure("possible acceptance did not require reconciliation")
    if not observation.acceptance_unresolved:
        raise RequestPolicyEvidenceFailure("possible acceptance did not mark unresolved")
    if observation.acceptance_ok:
        raise RequestPolicyEvidenceFailure("possible acceptance incorrectly claimed cancel success")
    if not observation.pending_preserved_on_reconciliation:
        raise RequestPolicyEvidenceFailure("possible acceptance did not preserve pending delivery")
    if not observation.unresolved_preserved_on_reconciliation:
        raise RequestPolicyEvidenceFailure("possible acceptance did not preserve unresolved state")
