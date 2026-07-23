"""Deterministic transport-layer evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim``. The claim
module covers deterministic JSON wire replay through ``ReferenceAdapter``;
transport evidence covers raw reader behavior such as bounded frame reads.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
from typing import Any, Mapping

from llm_collab.runtime_adapter_conformance import extract_clause_occurrences
from llm_collab.runtime_adapter_reference import MAX_MESSAGE_BYTES, ReferenceAdapter, serve


ARTIFACT_LABEL = "transport_bounded_read"
TRANSPORT_EVIDENCED = "transport_evidenced"
_MIN_OVERSIZED_MULTIPLIER = 4
_READLINE_LIMIT = MAX_MESSAGE_BYTES + 2


class TransportEvidenceFailure(AssertionError):
    """Raised when transport evidence cannot be built honestly."""


@dataclass(frozen=True)
class TransportClauseRef:
    clause_key: str
    text_sha256: str


@dataclass(frozen=True)
class BoundedReadObservation:
    input_bytes: int
    consumed_bytes: int
    stdout_error_name: str
    stdout_error_code: int
    process_status: int


_BOUNDED_READ_REFS: tuple[TransportClauseRef, ...] = (
    TransportClauseRef(
        "C9614292c6ab1.1",
        "9614292c6ab1616a78df7ae143ce7acdedfa74ca1c36e6731becc8d2e15b6dc4",
    ),
    TransportClauseRef(
        "C3dc535246440.1",
        "3dc53524644025a0b110d4ce45aafd9c8bd0100f2ec25ef0496f569775ae6f9e",
    ),
)


def build_transport_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic transport evidence for bounded raw-frame reads."""

    _validate_clause_refs(protocol_text)
    observation = _bounded_read_observation()
    _validate_bounded_read(observation)
    return {
        "schema_version": 1,
        "protocol": "runtime-adapter-jsonrpc-v1",
        "artifact_label": ARTIFACT_LABEL,
        "evidence_kind": "transport_raw_reader",
        "claim": TRANSPORT_EVIDENCED,
        "clauses": tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": TRANSPORT_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _BOUNDED_READ_REFS
        ),
        "observation": {
            "input_bytes": observation.input_bytes,
            "consumed_bytes": observation.consumed_bytes,
            "readline_limit": _READLINE_LIMIT,
            "stdout_error_name": observation.stdout_error_name,
            "stdout_error_code": observation.stdout_error_code,
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _BOUNDED_READ_REFS:
        clause = live.get(ref.clause_key)
        if clause is None:
            raise TransportEvidenceFailure(f"missing transport clause: {ref.clause_key}")
        if clause.text_sha256 != ref.text_sha256:
            raise TransportEvidenceFailure(f"stale transport clause: {ref.clause_key}")


def _bounded_read_observation() -> BoundedReadObservation:
    raw = b"x" * (MAX_MESSAGE_BYTES * _MIN_OVERSIZED_MULTIPLIER)
    stdin = io.BytesIO(raw)
    stdout = io.BytesIO()
    status = serve(adapter=ReferenceAdapter(), stdin=stdin, stdout=stdout, stderr=io.BytesIO())
    response = _single_json_response(stdout.getvalue())
    error = response.get("error") if isinstance(response, Mapping) else None
    data = error.get("data") if isinstance(error, Mapping) else None
    return BoundedReadObservation(
        input_bytes=len(raw),
        consumed_bytes=stdin.tell(),
        stdout_error_name=str(data.get("name")) if isinstance(data, Mapping) else "",
        stdout_error_code=int(error.get("code")) if isinstance(error, Mapping) and isinstance(error.get("code"), int) else 0,
        process_status=status,
    )


def _single_json_response(raw: bytes) -> Mapping[str, Any]:
    lines = raw.decode("utf-8").splitlines()
    if len(lines) != 1:
        raise TransportEvidenceFailure("transport probe must emit one response")
    payload = json.loads(lines[0])
    if not isinstance(payload, Mapping):
        raise TransportEvidenceFailure("transport probe response must be a JSON object")
    return payload


def _validate_bounded_read(observation: BoundedReadObservation) -> None:
    if observation.input_bytes < MAX_MESSAGE_BYTES * _MIN_OVERSIZED_MULTIPLIER:
        raise TransportEvidenceFailure("transport probe input must dwarf the read bound")
    if observation.process_status != 0:
        raise TransportEvidenceFailure("transport probe must exit cleanly")
    if observation.stdout_error_name != "MESSAGE_TOO_LARGE" or observation.stdout_error_code != -32001:
        raise TransportEvidenceFailure("transport probe must emit MESSAGE_TOO_LARGE")
    if observation.consumed_bytes > _READLINE_LIMIT:
        raise TransportEvidenceFailure("transport probe consumed unbounded input")
