"""Pure redaction transform for Runtime Adapter JSON-RPC V1.

This module owns only the Clause 14 pre-persistence transform. It does not
persist, log, quarantine, spawn, schedule, read environment, or touch runtime
state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from llm_collab.runtime_adapter_supervisor import MAX_STDERR_BYTES_PER_CONNECTION


REDACTION_FAILURE = "REDACTION_FAILURE"
MAX_REDACTION_DEPTH = 8
REDACTION_REASON_GENERIC = "redaction_failed"

_CONSTRUCTOR_TOKEN = object()
_DROP = object()
_SAFE_TOP_LEVEL_FIELDS = frozenset(
    (
        "adapter_id",
        "adapter_revision",
        "diagnostic",
        "endpoint_id",
        "fault",
        "manifest_id",
        "manifest_revision",
        "method",
        "native_session_id",
        "profile_id",
        "project_id",
        "reason",
        "request_id",
        "scope_identity",
        "session_ref_id",
        "stderr",
        "workspace_id",
    )
)
_SAFE_DIAGNOSTIC_FIELDS = frozenset(("code", "context", "detail", "fault", "reason"))
_HASHED_IDENTIFIER_FIELDS = frozenset(("native_session_id", "session_ref_id"))
_REDACTED_TEXT_FIELDS = frozenset(("detail", "reason"))
_FAULT_VALUES = frozenset(
    (
        "ADAPTER_QUARANTINED",
        "ADAPTER_UNHEALTHY",
        "CAPABILITY_NOT_DECLARED",
        "HANDSHAKE_TIMEOUT",
        "HEALTH_TIMEOUT",
        "INVALID_DELIVERY",
        "INVALID_FRAMING",
        "INVALID_PARAMS",
        "INVALID_HEALTH_RESPONSE",
        "INVALID_REQUEST",
        "INVALID_SESSION_REF",
        "INITIALIZE_REQUIRED",
        "INTERNAL_ERROR",
        "METHOD_NOT_FOUND",
        "MESSAGE_TOO_LARGE",
        "PARSE_ERROR",
        "PROCESS_CLOSED",
        "RECONCILIATION_REQUIRED",
        "REDACTION_FAILURE",
        "REQUEST_CANCELLED",
        "REQUEST_TIMEOUT",
        "SHUTDOWN_IN_PROGRESS",
        "STDERR_LIMIT_EXCEEDED",
        "TOO_MANY_IN_FLIGHT",
        "UNSUPPORTED_PROTOCOL_VERSION",
        "UNTRUSTED_MANIFEST_INPUT",
    )
)
_METHOD_VALUES = frozenset(
    (
        "initialize",
        "runtime.cancel",
        "runtime.deliver",
        "runtime.health",
        "runtime.reconcile",
        "runtime.shutdown",
    )
)
_DROP_ONLY_FIELDS = frozenset(
    (
        "configuration_ref",
        "environment",
        "home_path",
        "local_user",
        "project_slug",
        "raw_payload",
    )
)
_REDACTION_REASON_CODES = frozenset(
    (
        "closed_literal_not_string",
        "cyclic_input",
        "cyclic_stderr",
        "identifier_not_string",
        "mapping_field_not_object",
        "no_allowlisted_fields",
        "non_mapping_root",
        "nul_bearing_string",
        "prefix_not_bytes_or_string",
        "redaction_depth_exceeded",
        REDACTION_REASON_GENERIC,
        "request_id_not_scalar",
        "schema_identity_not_string",
        "stderr_not_object",
        "stderr_prefix_too_long_for_total",
        "stderr_truncation_without_discard",
        "stderr_unsupported_field",
        "strict_bool_required",
        "surrogate_bearing_string",
        "unsafe_float",
        "unsafe_key",
        "unsupported_scalar",
        "unexpected_redaction_exception",
        "unsigned_int_required",
    )
)
_SCHEMA_IDENTITY_FIELDS = frozenset(
    (
        "adapter_id",
        "adapter_revision",
        "endpoint_id",
        "manifest_id",
        "manifest_revision",
        "profile_id",
        "project_id",
        "scope_identity",
        "workspace_id",
    )
)


@dataclass(frozen=True)
class RedactionFailure:
    reason: str
    fault: str = REDACTION_FAILURE


class RedactedDocument:
    """Frozen redacted payload constructible only by this module's transform."""

    __slots__ = ("_payload",)

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        _constructor_token: object | None = None,
    ) -> None:
        if _constructor_token is not _CONSTRUCTOR_TOKEN:
            raise TypeError("RedactedDocument values must be produced by redact_document")
        object.__setattr__(self, "_payload", _freeze_mapping(payload))

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_payload" and hasattr(self, "_payload"):
            raise TypeError("RedactedDocument is immutable")
        object.__setattr__(self, name, value)

    @property
    def payload(self) -> Mapping[str, Any]:
        return self._payload

    def as_dict(self) -> dict[str, Any]:
        return _thaw(self._payload)


def redact_document(document: Mapping[str, Any]) -> RedactedDocument | RedactionFailure:
    """Return a redacted wrapper or fail closed with REDACTION_FAILURE."""

    if not isinstance(document, Mapping):
        return RedactionFailure("non_mapping_root")
    seen: set[int] = set()
    try:
        redacted = _redact_mapping(document, _SAFE_TOP_LEVEL_FIELDS, depth=0, seen=seen)
    except RedactionError as exc:
        return RedactionFailure(_safe_redaction_reason(exc))
    except Exception:
        return RedactionFailure("unexpected_redaction_exception")
    if not redacted:
        return RedactionFailure("no_allowlisted_fields")
    return RedactedDocument(redacted, _constructor_token=_CONSTRUCTOR_TOKEN)


class RedactionError(ValueError):
    pass


def _safe_redaction_reason(exc: RedactionError) -> str:
    if len(exc.args) == 1 and isinstance(exc.args[0], str) and exc.args[0] in _REDACTION_REASON_CODES:
        return exc.args[0]
    return REDACTION_REASON_GENERIC


def _redact_mapping(
    value: Mapping[str, Any],
    allowed_fields: frozenset[str],
    *,
    depth: int,
    seen: set[int],
) -> dict[str, Any]:
    _check_depth(depth)
    marker = id(value)
    if marker in seen:
        raise RedactionError("cyclic_input")
    seen.add(marker)
    try:
        output: dict[str, Any] = {}
        for key, raw in value.items():
            if not isinstance(key, str) or "\x00" in key:
                raise RedactionError("unsafe_key")
            if key in _DROP_ONLY_FIELDS:
                continue
            if key not in allowed_fields:
                continue
            redacted = _redact_field(key, raw, depth=depth + 1, seen=seen)
            if redacted is not _DROP:
                output[key] = redacted
        return output
    finally:
        seen.remove(marker)


def _redact_field(key: str, raw: Any, *, depth: int, seen: set[int]) -> Any:
    _check_depth(depth)
    if key == "stderr":
        return _redact_stderr(raw, depth=depth, seen=seen)
    if key in ("diagnostic", "context"):
        if not isinstance(raw, Mapping):
            raise RedactionError("mapping_field_not_object")
        redacted = _redact_mapping(raw, _SAFE_DIAGNOSTIC_FIELDS, depth=depth, seen=seen)
        return redacted if redacted else _DROP
    if key in _REDACTED_TEXT_FIELDS:
        return _redact_text(raw)
    if key in _HASHED_IDENTIFIER_FIELDS:
        return _hash_identifier(raw)
    if key in _SCHEMA_IDENTITY_FIELDS:
        return _schema_identity(raw, key)
    if key == "request_id":
        return _request_id(raw)
    if key == "fault":
        return _closed_string(raw, _FAULT_VALUES, "fault")
    if key == "method":
        return _closed_string(raw, _METHOD_VALUES, "method")
    if key == "code":
        return _redact_code(raw)
    return _redact_scalar(raw)


def _redact_stderr(raw: Any, *, depth: int, seen: set[int]) -> Mapping[str, Any]:
    _check_depth(depth)
    if not isinstance(raw, Mapping):
        raise RedactionError("stderr_not_object")
    marker = id(raw)
    if marker in seen:
        raise RedactionError("cyclic_stderr")
    seen.add(marker)
    try:
        allowed = {"prefix", "total_bytes", "truncated"}
        if any(key not in allowed for key in raw):
            raise RedactionError("stderr_unsupported_field")
        original_retained_bytes = _retained_stderr_prefix_bytes(raw.get("prefix", b""))
        retained_bytes = original_retained_bytes
        total_bytes = _non_negative_int(raw.get("total_bytes"), "total_bytes")
        truncated_input = _strict_bool(raw.get("truncated", False), "truncated")
        if total_bytes < original_retained_bytes:
            raise RedactionError("stderr_prefix_too_long_for_total")
        if retained_bytes > MAX_STDERR_BYTES_PER_CONNECTION:
            retained_bytes = MAX_STDERR_BYTES_PER_CONNECTION
        truncated = truncated_input or total_bytes > retained_bytes
        if truncated and total_bytes == retained_bytes:
            raise RedactionError("stderr_truncation_without_discard")
        return {
            "total_bytes": total_bytes,
            "retained_bytes": retained_bytes,
            "truncated": truncated,
        }
    finally:
        seen.remove(marker)


def _hash_identifier(raw: Any) -> str:
    if not isinstance(raw, str):
        raise RedactionError("identifier_not_string")
    value = _redact_string(raw)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _schema_identity(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise RedactionError("schema_identity_not_string")
    return _redact_string(raw)


def _request_id(raw: Any) -> str | int | float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise RedactionError("request_id_not_scalar")
    if isinstance(raw, str):
        return _redact_string(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise RedactionError("unsafe_float")
        return raw
    raise RedactionError("request_id_not_scalar")


def _redact_scalar(raw: Any) -> str | int | float | bool | None:
    if raw is None or isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise RedactionError("unsafe_float")
        return raw
    if isinstance(raw, bytes):
        return _decode_prefix(raw)
    if isinstance(raw, str):
        return _redact_string(raw)
    raise RedactionError("unsupported_scalar")


def _redact_string(raw: str) -> str:
    if "\x00" in raw:
        raise RedactionError("nul_bearing_string")
    if _contains_surrogate(raw):
        raise RedactionError("surrogate_bearing_string")
    return raw


def _redact_text(raw: Any) -> str | int | float | bool | None:
    value = _redact_scalar(raw)
    if isinstance(value, str):
        return _DROP
    return value


def _redact_code(raw: Any) -> str | int | float | bool | None:
    value = _redact_scalar(raw)
    if isinstance(value, str):
        return _DROP
    return value


def _closed_string(raw: Any, allowed: frozenset[str], name: str) -> str | object:
    value = _redact_scalar(raw)
    if not isinstance(value, str):
        raise RedactionError("closed_literal_not_string")
    if value not in allowed:
        return _DROP
    return value


def _retained_stderr_prefix_bytes(raw: Any) -> int:
    if isinstance(raw, str):
        if _contains_surrogate(raw):
            raise RedactionError("surrogate_bearing_string")
    elif not isinstance(raw, bytes):
        raise RedactionError("prefix_not_bytes_or_string")
    return _retained_byte_count(raw)


def _decode_prefix(raw: Any) -> str:
    if isinstance(raw, bytes):
        decoded = raw.decode("utf-8", "replace")
    elif isinstance(raw, str):
        decoded = raw
    else:
        raise RedactionError("prefix_not_bytes_or_string")
    if _contains_surrogate(decoded):
        raise RedactionError("surrogate_bearing_string")
    return decoded.replace("\x00", "\uFFFD")


def _retained_byte_count(raw: Any) -> int:
    if isinstance(raw, bytes):
        return len(raw)
    if isinstance(raw, str):
        return len(raw.encode("utf-8"))
    raise RedactionError("prefix_not_bytes_or_string")


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RedactionError("unsigned_int_required")
    return value


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RedactionError("strict_bool_required")
    return value


def _check_depth(depth: int) -> None:
    if depth > MAX_REDACTION_DEPTH:
        raise RedactionError("redaction_depth_exceeded")


def _freeze_mapping(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            key: _freeze(value)
            for key, value in mapping.items()
        }
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
