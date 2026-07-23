"""Deterministic deadline evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
the transport, admission, manifest, and cancellation ledgers. It exercises the
pure in-memory ``RequestPolicy`` deadline classifiers with injected timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from llm_collab.runtime_adapter_conformance import extract_clause_occurrences
from llm_collab.runtime_adapter_requests import (
    HANDSHAKE_DEADLINE_MS,
    HANDSHAKE_TIMEOUT,
    POST_INITIALIZE_METHODS,
    REQUEST_TIMEOUT,
    RequestPolicy,
    deadline_for_method,
)


ARTIFACT_LABEL = "request_policy_deadline"
DEADLINE_EVIDENCED = "deadline_evidenced"
_REQUEST_RECEIVED_AT_MS = 100
_PROCESS_STARTED_AT_MS = 200


class DeadlineEvidenceFailure(AssertionError):
    """Raised when request-policy deadline evidence cannot be built honestly."""


@dataclass(frozen=True)
class DeadlineClauseRef:
    clause_key: str
    text_sha256: str


@dataclass(frozen=True)
class RequestDeadlineObservation:
    method_deadlines: Mapping[str, int]
    before_deadline_expired: Mapping[str, bool]
    at_deadline_expired: Mapping[str, bool]
    at_deadline_faults: Mapping[str, str]
    unresolved_request_ids: Mapping[str, str]
    automatic_retry: Mapping[str, bool]


@dataclass(frozen=True)
class HandshakeDeadlineObservation:
    deadline_ms: int
    before_deadline_expired: bool
    at_deadline_expired: bool
    at_deadline_fault: str
    should_close: bool


@dataclass(frozen=True)
class DeadlineObservation:
    request: RequestDeadlineObservation
    handshake: HandshakeDeadlineObservation


_DEADLINE_REFS: tuple[DeadlineClauseRef, ...] = (
    DeadlineClauseRef(
        "Ce54676312948.1",
        "e54676312948a03f19d27df1722201729a607f6f4b0f24c4b0c149e2ee2ff0d1",
    ),
    DeadlineClauseRef(
        "Cf67f5a54aadc.1",
        "f67f5a54aadceb32909d9077c88c2b1831175bda292f6f40e11b827b8bc12e90",
    ),
    DeadlineClauseRef(
        "Cf67f5a54aadc.2",
        "f67f5a54aadceb32909d9077c88c2b1831175bda292f6f40e11b827b8bc12e90",
    ),
    DeadlineClauseRef(
        "Cf67f5a54aadc.3",
        "f67f5a54aadceb32909d9077c88c2b1831175bda292f6f40e11b827b8bc12e90",
    ),
)
DEFERRED_DEADLINE_KEYS = frozenset(("Cdc2b6cb59c4c.1",))


def build_deadline_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic evidence for C03 deadline classifier return values."""

    _validate_clause_refs(protocol_text)
    observation = _deadline_observation()
    _validate_observation(observation)
    return {
        "schema_version": 1,
        "protocol": "runtime-adapter-jsonrpc-v1",
        "artifact_label": ARTIFACT_LABEL,
        "evidence_kind": "request_policy_deadline",
        "claim": DEADLINE_EVIDENCED,
        "clauses": tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": DEADLINE_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _DEADLINE_REFS
        ),
        "observation": {
            "request": {
                "method_deadlines": dict(observation.request.method_deadlines),
                "before_deadline_expired": dict(observation.request.before_deadline_expired),
                "at_deadline_expired": dict(observation.request.at_deadline_expired),
                "at_deadline_faults": dict(observation.request.at_deadline_faults),
                "unresolved_request_ids": dict(observation.request.unresolved_request_ids),
                "automatic_retry": dict(observation.request.automatic_retry),
            },
            "handshake": {
                "deadline_ms": observation.handshake.deadline_ms,
                "before_deadline_expired": observation.handshake.before_deadline_expired,
                "at_deadline_expired": observation.handshake.at_deadline_expired,
                "at_deadline_fault": observation.handshake.at_deadline_fault,
                "should_close": observation.handshake.should_close,
            },
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _DEADLINE_REFS:
        clause = live.get(ref.clause_key)
        if clause is None:
            raise DeadlineEvidenceFailure(f"missing deadline clause: {ref.clause_key}")
        if clause.text_sha256 != ref.text_sha256:
            raise DeadlineEvidenceFailure(f"stale deadline clause: {ref.clause_key}")


def _deadline_observation() -> DeadlineObservation:
    return DeadlineObservation(
        request=_request_deadline_observation(),
        handshake=_handshake_deadline_observation(),
    )


def _request_deadline_observation() -> RequestDeadlineObservation:
    method_deadlines = {method: deadline_for_method(method) for method in sorted(POST_INITIALIZE_METHODS)}
    before_deadline_expired: dict[str, bool] = {}
    at_deadline_expired: dict[str, bool] = {}
    at_deadline_faults: dict[str, str] = {}
    unresolved_request_ids: dict[str, str] = {}
    automatic_retry: dict[str, bool] = {}
    for method, deadline_ms in method_deadlines.items():
        request_id = _request_id(method)
        before = RequestPolicy().classify_request_deadline(
            method,
            request_id,
            received_at_ms=_REQUEST_RECEIVED_AT_MS,
            now_ms=_REQUEST_RECEIVED_AT_MS + deadline_ms - 1,
        )
        before_deadline_expired[method] = before.expired
        at = RequestPolicy().classify_request_deadline(
            method,
            request_id,
            received_at_ms=_REQUEST_RECEIVED_AT_MS,
            now_ms=_REQUEST_RECEIVED_AT_MS + deadline_ms,
        )
        at_deadline_expired[method] = at.expired
        at_deadline_faults[method] = str(at.fault)
        unresolved_request_ids[method] = str(at.unresolved_request_id)
        automatic_retry[method] = at.automatic_retry
    return RequestDeadlineObservation(
        method_deadlines=method_deadlines,
        before_deadline_expired=before_deadline_expired,
        at_deadline_expired=at_deadline_expired,
        at_deadline_faults=at_deadline_faults,
        unresolved_request_ids=unresolved_request_ids,
        automatic_retry=automatic_retry,
    )


def _handshake_deadline_observation() -> HandshakeDeadlineObservation:
    before = RequestPolicy().classify_handshake_deadline(
        process_started_at_ms=_PROCESS_STARTED_AT_MS,
        now_ms=_PROCESS_STARTED_AT_MS + HANDSHAKE_DEADLINE_MS - 1,
    )
    at = RequestPolicy().classify_handshake_deadline(
        process_started_at_ms=_PROCESS_STARTED_AT_MS,
        now_ms=_PROCESS_STARTED_AT_MS + HANDSHAKE_DEADLINE_MS,
    )
    return HandshakeDeadlineObservation(
        deadline_ms=HANDSHAKE_DEADLINE_MS,
        before_deadline_expired=before.expired,
        at_deadline_expired=at.expired,
        at_deadline_fault=str(at.fault),
        should_close=at.should_close,
    )


def _request_id(method: str) -> str:
    return f"{method}:deadline-probe"


def _validate_observation(observation: DeadlineObservation) -> None:
    expected_methods = set(POST_INITIALIZE_METHODS)
    request = observation.request
    if set(request.method_deadlines) != expected_methods:
        raise DeadlineEvidenceFailure("deadline method coverage drifted")
    if any(request.before_deadline_expired.values()):
        raise DeadlineEvidenceFailure("request expired before its deadline")
    if not all(request.at_deadline_expired.values()):
        raise DeadlineEvidenceFailure("request did not expire at its deadline")
    if any(fault != REQUEST_TIMEOUT for fault in request.at_deadline_faults.values()):
        raise DeadlineEvidenceFailure("request deadline used the wrong fault")
    if any(request.unresolved_request_ids[method] != _request_id(method) for method in expected_methods):
        raise DeadlineEvidenceFailure("request deadline did not preserve unresolved request id")
    if any(request.automatic_retry.values()):
        raise DeadlineEvidenceFailure("request deadline allowed automatic retry")

    handshake = observation.handshake
    if handshake.deadline_ms != HANDSHAKE_DEADLINE_MS:
        raise DeadlineEvidenceFailure("handshake deadline drifted")
    if handshake.before_deadline_expired:
        raise DeadlineEvidenceFailure("handshake expired before its deadline")
    if not handshake.at_deadline_expired:
        raise DeadlineEvidenceFailure("handshake did not expire at its deadline")
    if handshake.at_deadline_fault != HANDSHAKE_TIMEOUT:
        raise DeadlineEvidenceFailure("handshake deadline used the wrong fault")
    if not handshake.should_close:
        raise DeadlineEvidenceFailure("handshake deadline did not close the connection")
