"""In-process conformance helpers for Runtime Adapter JSON-RPC V1.

This module is intentionally inert: it parses and validates JSON-RPC frames
against the frozen protocol contract, but it never starts a process, reads
runtime state, or touches canonical/ledger/inbox surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


JSONRPC_VERSION = "2.0"
NEGOTIATED_PROTOCOL_VERSION = "1.0"
METHODS = frozenset(
    (
        "initialize",
        "runtime.deliver",
        "runtime.cancel",
        "runtime.reconcile",
        "runtime.health",
        "runtime.shutdown",
    )
)
J7_OWNER_VOCABULARY = frozenset(
    (
        "P3a",
        "P3b",
        "P3c",
        "P3d-request",
        "P3d-lifecycle",
        "P3e-redact",
        "P3e-state",
        "P3f",
    )
)

_KEYWORD_PATTERN = re.compile(r"\bMUST NOT\b|\bMUST\b(?!\s+NOT)|\bSHALL\b")
_SECTION_PATTERN = re.compile(r"^(\d+)\. \*\*(.+?) \(normative\)\.\*\*", re.M)
_MARKDOWN_STRIP_PATTERN = re.compile(r"[*_`]+")

_REQUEST_PARAM_KEYS: Mapping[str, frozenset[str]] = {
    "initialize": frozenset(
        (
            "requested_protocol_version",
            "adapter_id",
            "adapter_revision",
            "manifest_id",
            "manifest_revision",
            "endpoint",
        )
    ),
    "runtime.deliver": frozenset(("session_ref", "delivery")),
    "runtime.cancel": frozenset(
        ("session_ref", "original_request_id", "delivery_id", "attempt_id")
    ),
    "runtime.reconcile": frozenset(
        ("session_ref", "original_request_id", "delivery_id", "attempt_id")
    ),
    "runtime.health": frozenset(),
    "runtime.shutdown": frozenset(),
}

_ERROR_CODES = {
    "PARSE_ERROR": -32700,
    "INVALID_REQUEST": -32600,
    "METHOD_NOT_FOUND": -32601,
    "INVALID_PARAMS": -32602,
}


class ConformanceFailure(AssertionError):
    """Raised when a frame or ledger violates a named conformance clause."""

    def __init__(self, clause: str, message: str):
        super().__init__(f"{clause}: {message}")
        self.clause = clause
        self.message = message


class DuplicateMemberError(ValueError):
    """Raised when a JSON object repeats a member name at any depth."""


@dataclass(frozen=True)
class DirectionOutcome:
    direction_valid: bool
    form: str
    fault: str | None = None
    should_close: bool = False
    should_quarantine: bool = False
    send_response: bool = False


@dataclass(frozen=True)
class AdapterOutcome:
    response: str | None
    fault: str | None = None
    should_close: bool = False


@dataclass(frozen=True)
class ClauseOccurrence:
    clause_key: str
    text_sha256: str
    normalized_sentence: str
    keyword: str
    source_line: int
    section: str


@dataclass(frozen=True)
class LedgerRow:
    clause_key: str
    text_sha256: str
    classification: str
    owners: frozenset[str]
    reason: str = ""
    covered_by: tuple[str, ...] = ()
    claim_refs: Mapping[str, str] | None = None
    source_line: int | None = None


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise DuplicateMemberError(key)
        seen.add(key)
        out[key] = value
    return out


def load_json_frame(raw: str) -> Any:
    """Load one JSON frame while rejecting duplicate object members."""

    try:
        return json.loads(raw, object_pairs_hook=_duplicate_rejecting_object)
    except DuplicateMemberError as error:
        raise ConformanceFailure("duplicate-member", f"duplicate member {error}") from error
    except json.JSONDecodeError as error:
        raise ConformanceFailure("parse-json", "invalid JSON") from error


def dumps_frame(obj: Mapping[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _is_request_id(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _is_request_form(obj: Mapping[str, Any]) -> bool:
    return "method" in obj and "result" not in obj and "error" not in obj


def _is_response_form(obj: Mapping[str, Any]) -> bool:
    return "result" in obj or "error" in obj


def classify_direction(sender: str, receiver: str, obj: Any) -> DirectionOutcome:
    """Apply the four-row direction matrix after P1 succeeds."""

    if isinstance(obj, list):
        raise ConformanceFailure("batch-rejected", "JSON-RPC batch input is not a V1 frame")
    if not isinstance(obj, dict):
        raise ConformanceFailure("closed-envelope", "frame must be a JSON object")

    form = "response" if _is_response_form(obj) else "request" if "method" in obj else "unknown"
    if sender == "host" and receiver == "adapter" and form == "request":
        return DirectionOutcome(True, form, send_response=True)
    if sender == "adapter" and receiver == "host" and form == "response":
        return DirectionOutcome(True, form)
    if sender == "adapter" and receiver == "host" and form == "request":
        return DirectionOutcome(
            False,
            form,
            fault="INVALID_REQUEST",
            should_close=True,
            should_quarantine=True,
        )
    if sender == "host" and receiver == "adapter" and form == "response":
        return DirectionOutcome(False, form, fault="INVALID_REQUEST", should_close=True)
    raise ConformanceFailure("direction-matrix", f"unknown direction {sender}->{receiver}")


def validate_request(obj: Any) -> tuple[Any, str, Mapping[str, Any]]:
    if not isinstance(obj, dict):
        raise ConformanceFailure("closed-request", "request must be an object")
    if set(obj) != {"jsonrpc", "id", "method", "params"}:
        if "id" not in obj and set(obj).issubset({"jsonrpc", "method", "params"}):
            raise ConformanceFailure("notification-rejected", "host request omitted id")
        raise ConformanceFailure("closed-request", "request envelope has missing or extra members")
    if obj["jsonrpc"] != JSONRPC_VERSION:
        raise ConformanceFailure("jsonrpc-constant", "jsonrpc must be 2.0")
    if not _is_request_id(obj["id"]):
        raise ConformanceFailure("request-id", "id must be non-null string or number")
    method = obj["method"]
    if method not in METHODS:
        raise ConformanceFailure("closed-method-set", f"unknown method {method!r}")
    params = obj["params"]
    if not isinstance(params, dict):
        raise ConformanceFailure("closed-params", "params must be an object")
    expected = _REQUEST_PARAM_KEYS[method]
    if set(params) != set(expected):
        raise ConformanceFailure("closed-params", f"{method} params mismatch")
    return obj["id"], method, params


def validate_response(obj: Any, request_id: Any) -> Mapping[str, Any]:
    if not isinstance(obj, dict):
        raise ConformanceFailure("closed-response", "response must be an object")
    if obj.get("jsonrpc") != JSONRPC_VERSION:
        raise ConformanceFailure("jsonrpc-constant", "jsonrpc must be 2.0")
    if obj.get("id") != request_id:
        raise ConformanceFailure("response-correlation", "response id does not echo request id")
    has_result = "result" in obj
    has_error = "error" in obj
    if has_result == has_error:
        raise ConformanceFailure("closed-response", "response needs exactly one result or error")
    if has_result and set(obj) == {"jsonrpc", "id", "result"}:
        return obj
    if has_error and set(obj) == {"jsonrpc", "id", "error"}:
        error = obj["error"]
        if not isinstance(error, dict) or set(error) != {"code", "message", "data"}:
            raise ConformanceFailure("closed-error", "error object is not closed")
        data = error["data"]
        if not isinstance(data, dict) or set(data) != {"name", "retryable", "request_id"}:
            raise ConformanceFailure("closed-error", "error data is not closed")
        if data["request_id"] != request_id:
            raise ConformanceFailure("closed-error", "error.data.request_id mismatch")
        return obj
    raise ConformanceFailure("closed-response", "response envelope has extra members")


def error_response(request_id: Any, name: str) -> str:
    code = _ERROR_CODES.get(name, -32000)
    return dumps_frame(
        {
            "jsonrpc": JSONRPC_VERSION,
            "id": request_id,
            "error": {
                "code": code,
                "message": name,
                "data": {
                    "name": name,
                    "retryable": False,
                    "request_id": request_id,
                },
            },
        }
    )


class FakeAdapter:
    """Deterministic table-driven adapter peer for in-process conformance tests."""

    def __init__(self, routes: Mapping[str, Any]):
        self._routes = dict(routes)
        self.observed_frames: list[Mapping[str, Any]] = []

    def handle(self, raw: str) -> AdapterOutcome:
        try:
            frame = load_json_frame(raw)
            classify_direction("host", "adapter", frame)
            request_id, method, _params = validate_request(frame)
        except ConformanceFailure as failure:
            if failure.clause == "notification-rejected":
                return AdapterOutcome(None, "INVALID_REQUEST", should_close=True)
            if failure.clause in {"parse-json", "duplicate-member"}:
                return AdapterOutcome(error_response(None, "PARSE_ERROR"), "PARSE_ERROR")
            if failure.clause == "closed-method-set":
                request_id = frame.get("id") if isinstance(frame, dict) else None
                return AdapterOutcome(error_response(request_id, "METHOD_NOT_FOUND"), "METHOD_NOT_FOUND")
            request_id = frame.get("id") if isinstance(frame, dict) and _is_request_id(frame.get("id")) else None
            return AdapterOutcome(error_response(request_id, "INVALID_REQUEST"), "INVALID_REQUEST")

        self.observed_frames.append(frame)
        if method not in self._routes:
            return AdapterOutcome(error_response(request_id, "METHOD_NOT_FOUND"), "METHOD_NOT_FOUND")
        route = self._routes[method]
        if isinstance(route, Mapping) and "error" in route:
            return AdapterOutcome(error_response(request_id, str(route["error"])), str(route["error"]))
        return AdapterOutcome(dumps_frame({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": route}))


def normalize_clause_text(text: str) -> str:
    return " ".join(_MARKDOWN_STRIP_PATTERN.sub("", text).split())


def _normative_section(protocol_text: str) -> tuple[int, str]:
    marker = "## Normative clauses"
    start = protocol_text.index(marker)
    return start, protocol_text[start:]


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def split_normative_sentences(protocol_text: str) -> list[tuple[int, str, str]]:
    """Return (absolute offset, section id, sentence text) for the pinned splitter."""

    section_start, normative = _normative_section(protocol_text)
    headers = [
        (match.start(), f"C{int(match.group(1)):02d} {match.group(2)}")
        for match in _SECTION_PATTERN.finditer(normative)
    ]

    def section_for(relative_offset: int) -> str:
        section = "UNKNOWN"
        for header_offset, header in headers:
            if header_offset <= relative_offset:
                section = header
            else:
                break
        return section

    sentences: list[tuple[int, str, str]] = []
    for paragraph in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|$)", normative, re.S):
        raw = paragraph.group(0).strip()
        if not raw:
            continue
        if raw.startswith("|"):
            line_offset = paragraph.start()
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped:
                    sentences.append((section_start + line_offset, section_for(line_offset), stripped))
                line_offset += len(line) + 1
            continue
        raw = " ".join(line.strip() for line in raw.splitlines())
        start = 0
        for split in re.finditer(r"(?<=[.!?])\s+(?=(?:`|\*\*|[A-Z0-9]))", raw):
            sentence = raw[start : split.start()].strip()
            if sentence:
                sentences.append(
                    (section_start + paragraph.start() + start, section_for(paragraph.start() + start), sentence)
                )
            start = split.end()
        tail = raw[start:].strip()
        if tail:
            sentences.append(
                (section_start + paragraph.start() + start, section_for(paragraph.start() + start), tail)
            )
    return sentences


def extract_clause_occurrences(protocol_text: str) -> tuple[ClauseOccurrence, ...]:
    occurrences: list[ClauseOccurrence] = []
    seen: set[str] = set()
    for offset, section, sentence in split_normative_sentences(protocol_text):
        normalized = normalize_clause_text(sentence)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        for index, match in enumerate(_KEYWORD_PATTERN.finditer(normalized), start=1):
            key = f"C{digest[:12]}.{index}"
            if key in seen:
                raise ConformanceFailure("clause-key-collision", key)
            seen.add(key)
            occurrences.append(
                ClauseOccurrence(
                    clause_key=key,
                    text_sha256=digest,
                    normalized_sentence=normalized,
                    keyword=match.group(0),
                    source_line=_line_for_offset(protocol_text, offset),
                    section=section,
                )
            )
    return tuple(occurrences)


def _owners_for_clause(clause: ClauseOccurrence) -> frozenset[str]:
    text = clause.normalized_sentence
    section = clause.section
    if section.startswith("C01"):
        if any(token in text for token in ("stderr", "process start", "Standard output", "Standard error", "stdio")):
            return frozenset(("P3c",))
        return frozenset(("P3a",))
    if section.startswith("C02"):
        if any(
            token in text
            for token in (
                "in-flight",
                "active request",
                "queued",
                "pool",
                "Deliveries MUST never exceed",
                "post-initialize requests together",
            )
        ):
            return frozenset(("P3d-request",))
        return frozenset(("P3a",))
    if section.startswith("C03"):
        return frozenset(("P3d-request",))
    if section.startswith("C04"):
        return frozenset(("P3a",))
    if section.startswith("C05"):
        if any(token in text for token in ("execute", "shell", "spawned", "program directly")):
            return frozenset(("P3c",))
        return frozenset(("P3b",))
    if section.startswith(("C06", "C07", "C13")):
        return frozenset(("P3a",))
    if section.startswith("C08"):
        if any(
            token in text
            for token in ("canonical delivery state", "possible acceptance", "StateEvidenceV1.integrity", "ReceiptV1")
        ):
            return frozenset(("P3e-state",))
        return frozenset(("P3a",))
    if section.startswith("C09"):
        if any(token in text for token in ("pending delivery", "acceptance may", "unresolved", "REQUEST_CANCELLED")):
            return frozenset(("P3d-request",))
        return frozenset(("P3a",))
    if section.startswith("C10"):
        if any(token in text for token in ("restart", "connection loss", "manual replay")):
            return frozenset(("P3f",))
        return frozenset(("P3e-state",))
    if section.startswith("C11"):
        return frozenset(("P3d-lifecycle",))
    if section.startswith("C12"):
        return frozenset(("P3e-state",))
    if section.startswith("C14"):
        if any(token in text for token in ("written", "persisted", "persistence", "quarantine")):
            return frozenset(("P3e-redact", "P3e-state"))
        return frozenset(("P3e-redact",))
    if section.startswith("C15"):
        return frozenset(("P3d-lifecycle",))
    if section.startswith("C16"):
        if any(
            token in text
            for token in ("executable path", "argv", "working directory", "environment", "shell", "manifest path")
        ):
            return frozenset(("P3b", "P3c"))
        return frozenset(("P3a",))
    return frozenset(("P3a",))


def build_clause_ledger(protocol_text: str) -> tuple[LedgerRow, ...]:
    rows: list[LedgerRow] = []
    for clause in extract_clause_occurrences(protocol_text):
        owners = _owners_for_clause(clause)
        classification = "covered_here" if owners == frozenset(("P3a",)) else "deferred"
        covered_by = (f"test_{clause.section.split()[0].lower()}_family",) if classification == "covered_here" else ()
        claim_refs = {owner: f"{owner}:pending-contract-claim" for owner in owners} if len(owners) > 1 else None
        reason = "" if classification == "covered_here" else f"owned by {', '.join(sorted(owners))}"
        rows.append(
            LedgerRow(
                clause_key=clause.clause_key,
                text_sha256=clause.text_sha256,
                classification=classification,
                owners=owners,
                covered_by=covered_by,
                claim_refs=claim_refs,
                reason=reason,
                source_line=clause.source_line,
            )
        )
    return tuple(rows)


def validate_clause_ledger(
    occurrences: Iterable[ClauseOccurrence],
    rows: Iterable[LedgerRow],
    *,
    implementing_child: str,
    stamped_claims: Mapping[str, set[str]],
) -> None:
    occurrence_rows = tuple(occurrences)
    ledger_rows = tuple(rows)
    by_key = {occurrence.clause_key: occurrence for occurrence in occurrence_rows}
    ledger = {row.clause_key: row for row in ledger_rows}
    if set(by_key) != set(ledger):
        raise ConformanceFailure("ledger-bijection", "ledger rows do not match extracted clauses")
    if len(ledger) != len(ledger_rows):
        raise ConformanceFailure("ledger-duplicate", "ledger repeats a clause key")
    for row in ledger.values():
        if row.text_sha256 != by_key[row.clause_key].text_sha256:
            raise ConformanceFailure("ledger-text-hash", row.clause_key)
        if row.classification not in {"covered_here", "deferred", "not_mechanically_testable"}:
            raise ConformanceFailure("ledger-classification", row.clause_key)
        if not row.owners:
            raise ConformanceFailure("ledger-owner", row.clause_key)
        if not row.owners <= J7_OWNER_VOCABULARY:
            raise ConformanceFailure("ledger-owner-vocabulary", row.clause_key)
        if row.classification == "covered_here" and not row.covered_by:
            raise ConformanceFailure("ledger-covered-by", row.clause_key)
        if row.classification == "deferred" and implementing_child in row.owners:
            raise ConformanceFailure("ledger-deferred-to-self", row.clause_key)
        if row.classification in {"deferred", "not_mechanically_testable"} and not row.reason:
            raise ConformanceFailure("ledger-reason", row.clause_key)
        if row.classification == "not_mechanically_testable" and "testable" not in row.reason:
            raise ConformanceFailure("ledger-actionable-untestable", row.clause_key)
        if len(row.owners) > 1:
            refs = row.claim_refs or {}
            if set(refs) != set(row.owners):
                raise ConformanceFailure("ledger-reciprocal-claims", row.clause_key)
        for owner in row.owners & set(stamped_claims):
            if row.clause_key not in stamped_claims[owner]:
                raise ConformanceFailure("ledger-silent-rehome", row.clause_key)
