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

from llm_collab import runtime_adapter_reference
from llm_collab.runtime_adapter_conformance import (
    ConformanceFailure,
    JSONRPC_VERSION,
    classify_direction,
    extract_clause_occurrences,
    load_json_frame,
)
from llm_collab.runtime_adapter_reference import FAULT_STDERR_OVERFLOW, MAX_MESSAGE_BYTES, ReferenceAdapter, serve


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


@dataclass(frozen=True)
class StreamSeparationObservation:
    normal_stdout_frames: int
    normal_stdout_successes: int
    normal_stdout_errors: int
    fault_stdout_frames: int
    fault_stderr_bytes: int


@dataclass(frozen=True)
class C01FramingObservation:
    first_frame_id: str
    second_frame_id: str
    embedded_newline_fault: str
    duplicate_member_fault: str
    absent_id_adapter_request_fault: str
    absent_id_adapter_request_quarantines: bool
    oversized_response_error_name: str
    oversized_response_bytes: int


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
_STREAM_SEPARATION_REFS: tuple[TransportClauseRef, ...] = (
    TransportClauseRef(
        "C00951376f21f.1",
        "00951376f21f8d26c8dde8c899fcf0d60b351d671a79e1be0c2e30f4c872e601",
    ),
    TransportClauseRef(
        "C27e614c40ce1.1",
        "27e614c40ce17034bc897a1eb617cc7546c6d8c24d4584b51517b63048ab6de1",
    ),
)
_C01_FRAMING_REFS: tuple[TransportClauseRef, ...] = (
    TransportClauseRef(
        "C9ef1a548fb74.1",
        "9ef1a548fb741dcaa4a84e53ec6ffdf3c3847f26c165c0bd7d0ae0ad9938cc45",
    ),
    TransportClauseRef(
        "C1c16ee3a9a20.1",
        "1c16ee3a9a20a81ee57b70e58bb2bbe23a436486ce431f20612d038ecb985425",
    ),
    TransportClauseRef(
        "C01e408ef2020.1",
        "01e408ef2020634d8f4f8c976a3cab9403c7732abfcc0d7a10466fffc4763098",
    ),
    TransportClauseRef(
        "Cbddfb728b470.1",
        "bddfb728b470c16a881adb646787a68a3ac53a7f1940eb8d8c325ddc633432d7",
    ),
    TransportClauseRef(
        "C15ce93ba85cf.1",
        "15ce93ba85cf7ab7a3862d1e2d1ad176ad943e47011ce3fb6c9c93f313cfd8a1",
    ),
)
_TRANSPORT_REFS = (*_BOUNDED_READ_REFS, *_STREAM_SEPARATION_REFS, *_C01_FRAMING_REFS)


def build_transport_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic transport evidence for bounded raw-frame reads."""

    _validate_clause_refs(protocol_text)
    bounded = _bounded_read_observation()
    stream = _stream_separation_observation()
    framing = _c01_framing_observation()
    _validate_bounded_read(bounded)
    _validate_stream_separation(stream)
    _validate_c01_framing(framing)
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
        )
        + tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": TRANSPORT_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _STREAM_SEPARATION_REFS
        )
        + tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": TRANSPORT_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _C01_FRAMING_REFS
        ),
        "observation": {
            "input_bytes": bounded.input_bytes,
            "consumed_bytes": bounded.consumed_bytes,
            "readline_limit": _READLINE_LIMIT,
            "stdout_error_name": bounded.stdout_error_name,
            "stdout_error_code": bounded.stdout_error_code,
            "normal_stdout_frames": stream.normal_stdout_frames,
            "normal_stdout_successes": stream.normal_stdout_successes,
            "normal_stdout_errors": stream.normal_stdout_errors,
            "fault_stdout_frames": stream.fault_stdout_frames,
            "fault_stderr_bytes": stream.fault_stderr_bytes,
            "c01_framing": {
                "first_frame_id": framing.first_frame_id,
                "second_frame_id": framing.second_frame_id,
                "embedded_newline_fault": framing.embedded_newline_fault,
                "duplicate_member_fault": framing.duplicate_member_fault,
                "absent_id_adapter_request_fault": framing.absent_id_adapter_request_fault,
                "absent_id_adapter_request_quarantines": framing.absent_id_adapter_request_quarantines,
                "oversized_response_error_name": framing.oversized_response_error_name,
                "oversized_response_bytes": framing.oversized_response_bytes,
            },
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _TRANSPORT_REFS:
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


def _stream_separation_observation() -> StreamSeparationObservation:
    normal_stdout = io.BytesIO()
    normal_status = serve(
        adapter=ReferenceAdapter(),
        stdin=io.BytesIO((_initialize_frame("initialize-1") + "\n" + _initialize_frame("initialize-2") + "\n").encode("utf-8")),
        stdout=normal_stdout,
        stderr=io.BytesIO(),
    )
    if normal_status != 0:
        raise TransportEvidenceFailure("normal stream probe must exit cleanly")
    normal_frames = _jsonrpc_responses(normal_stdout.getvalue())

    fault_stdout = io.BytesIO()
    fault_stderr = io.BytesIO()
    fault_status = serve(
        adapter=ReferenceAdapter(fault_injection=FAULT_STDERR_OVERFLOW),
        stdin=io.BytesIO((_initialize_frame("fault-initialize") + "\n").encode("utf-8")),
        stdout=fault_stdout,
        stderr=fault_stderr,
    )
    if fault_status != 0:
        raise TransportEvidenceFailure("fault stream probe must exit cleanly")
    fault_frames = _jsonrpc_responses(fault_stdout.getvalue())
    fault_stderr_bytes = fault_stderr.getvalue()
    if _is_jsonrpc_response(fault_stderr_bytes):
        raise TransportEvidenceFailure("stderr diagnostics parsed as a protocol response")

    return StreamSeparationObservation(
        normal_stdout_frames=len(normal_frames),
        normal_stdout_successes=sum("result" in frame for frame in normal_frames),
        normal_stdout_errors=sum("error" in frame for frame in normal_frames),
        fault_stdout_frames=len(fault_frames),
        fault_stderr_bytes=len(fault_stderr_bytes),
    )


def _c01_framing_observation() -> C01FramingObservation:
    two_frames = io.BytesIO((_initialize_frame("line-1") + "\n" + _initialize_frame("line-2") + "\n").encode("utf-8"))
    first = _line_frame(two_frames)
    second = _line_frame(two_frames)

    embedded_newline_fault = _conformance_fault(
        lambda: load_json_frame(
            runtime_adapter_reference._readline_before_deadline(
                io.BytesIO(
                    b'{"jsonrpc":"2.0","id":"raw-newline","method":"runtime.health","params":{"text":"a\nb"}}\n'
                ),
                None,
            )
            .decode("utf-8")
            .rstrip("\n")
        )
    )
    duplicate_member_fault = _conformance_fault(
        lambda: load_json_frame(
            '{"jsonrpc":"2.0","id":"duplicate","method":"runtime.health","params":{"x":1,"x":2}}'
        )
    )
    absent_id_request = load_json_frame('{"jsonrpc":"2.0","method":"runtime.health","params":{}}')
    direction = classify_direction("adapter", "host", absent_id_request)

    huge_response = json.dumps(
        {
            "jsonrpc": JSONRPC_VERSION,
            "id": "oversized-response",
            "result": {"payload": "x" * MAX_MESSAGE_BYTES},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    stdout = io.BytesIO()
    runtime_adapter_reference._write_response(huge_response, stdout)
    response = _single_json_response(stdout.getvalue())
    error = response.get("error") if isinstance(response, Mapping) else None
    data = error.get("data") if isinstance(error, Mapping) else None

    return C01FramingObservation(
        first_frame_id=str(first.get("id")),
        second_frame_id=str(second.get("id")),
        embedded_newline_fault=embedded_newline_fault,
        duplicate_member_fault=duplicate_member_fault,
        absent_id_adapter_request_fault=str(direction.fault),
        absent_id_adapter_request_quarantines=bool(direction.should_quarantine),
        oversized_response_error_name=str(data.get("name")) if isinstance(data, Mapping) else "",
        oversized_response_bytes=len(stdout.getvalue()),
    )


def _line_frame(stdin: io.BytesIO) -> Mapping[str, Any]:
    raw = runtime_adapter_reference._readline_before_deadline(stdin, None)
    if not raw.endswith(b"\n"):
        raise TransportEvidenceFailure("framing probe did not read one newline-terminated frame")
    return load_json_frame(raw.decode("utf-8").rstrip("\n"))


def _conformance_fault(action: Any) -> str:
    try:
        action()
    except ConformanceFailure as error:
        return str(error.clause)
    except json.JSONDecodeError as error:
        raise TransportEvidenceFailure("C01 framing probe bypassed conformance failure handling") from error
    raise TransportEvidenceFailure("C01 framing probe accepted invalid input")


def _single_json_response(raw: bytes) -> Mapping[str, Any]:
    responses = _jsonrpc_responses(raw)
    if len(responses) != 1:
        raise TransportEvidenceFailure("transport probe must emit one response")
    return responses[0]


def _jsonrpc_responses(raw: bytes) -> tuple[Mapping[str, Any], ...]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise TransportEvidenceFailure("stdout must be UTF-8 protocol frames") from error
    if not lines:
        raise TransportEvidenceFailure("stdout must emit at least one protocol frame")
    responses = tuple(_jsonrpc_response(line) for line in lines)
    return responses


def _jsonrpc_response(line: str) -> Mapping[str, Any]:
    payload = json.loads(line)
    if (
        not isinstance(payload, Mapping)
        or payload.get("jsonrpc") != JSONRPC_VERSION
        or ("result" in payload) == ("error" in payload)
    ):
        raise TransportEvidenceFailure("stdout line must be one JSON-RPC response")
    return payload


def _is_jsonrpc_response(raw: bytes) -> bool:
    try:
        lines = raw.decode("utf-8").splitlines()
        return len(lines) == 1 and bool(_jsonrpc_response(lines[0]))
    except (UnicodeDecodeError, json.JSONDecodeError, TransportEvidenceFailure):
        return False


def _validate_bounded_read(observation: BoundedReadObservation) -> None:
    if observation.input_bytes < MAX_MESSAGE_BYTES * _MIN_OVERSIZED_MULTIPLIER:
        raise TransportEvidenceFailure("transport probe input must dwarf the read bound")
    if observation.process_status != 0:
        raise TransportEvidenceFailure("transport probe must exit cleanly")
    if observation.stdout_error_name != "MESSAGE_TOO_LARGE" or observation.stdout_error_code != -32001:
        raise TransportEvidenceFailure("transport probe must emit MESSAGE_TOO_LARGE")
    if observation.consumed_bytes > _READLINE_LIMIT:
        raise TransportEvidenceFailure("transport probe consumed unbounded input")


def _validate_stream_separation(observation: StreamSeparationObservation) -> None:
    if observation.normal_stdout_frames < 2:
        raise TransportEvidenceFailure("normal stream probe must include multiple stdout frames")
    if observation.normal_stdout_successes < 1 or observation.normal_stdout_errors < 1:
        raise TransportEvidenceFailure("normal stdout proof must include result and error frames")
    if observation.fault_stdout_frames != 1:
        raise TransportEvidenceFailure("fault stream probe must keep stdout frame-only")
    if observation.fault_stderr_bytes <= 0:
        raise TransportEvidenceFailure("fault stream probe must force non-empty stderr diagnostics")


def _validate_c01_framing(observation: C01FramingObservation) -> None:
    if observation.first_frame_id != "line-1" or observation.second_frame_id != "line-2":
        raise TransportEvidenceFailure("C01 framing proof did not preserve newline frame boundaries")
    if observation.embedded_newline_fault != "parse-json":
        raise TransportEvidenceFailure("C01 framing proof accepted an embedded raw newline")
    if observation.duplicate_member_fault != "duplicate-member":
        raise TransportEvidenceFailure("C01 framing proof accepted duplicate JSON members")
    if observation.absent_id_adapter_request_fault != "INVALID_REQUEST":
        raise TransportEvidenceFailure("C01 direction proof used the wrong fault")
    if not observation.absent_id_adapter_request_quarantines:
        raise TransportEvidenceFailure("C01 direction proof did not quarantine adapter-originated requests")
    if observation.oversized_response_error_name != "MESSAGE_TOO_LARGE":
        raise TransportEvidenceFailure("C01 writer proof did not bound complete response frames")
    if observation.oversized_response_bytes > MAX_MESSAGE_BYTES + 1:
        raise TransportEvidenceFailure("C01 writer proof emitted an oversized response frame")


def _initialize_frame(request_id: str) -> str:
    endpoint = ReferenceAdapter()._identity.endpoint()
    return json.dumps(
        {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "method": "initialize",
            "params": {
                "requested_protocol_version": "1.0",
                "adapter_id": "adapter_alpha",
                "adapter_revision": "adapter_rev1",
                "manifest_id": "manifest_alpha",
                "manifest_revision": "manifest_rev1",
                "endpoint": endpoint,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
