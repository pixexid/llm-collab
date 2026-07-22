"""Reference Runtime Adapter JSON-RPC V1 peer.

This module is an inert conformance subject. It can be spawned over stdio by
tests, but it does not read runtime state, discover manifests, persist data, or
publish conformance claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping

from llm_collab.runtime_adapter_conformance import JSONRPC_VERSION, NEGOTIATED_PROTOCOL_VERSION
from llm_collab.runtime_adapter_requests import (
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
)


METHOD_INITIALIZE = "initialize"
FAULT_DUPLICATE_OUTPUT = "duplicate-output"
FAULT_PROHIBITED_ADAPTER_REQUEST = "prohibited-adapter-request"
FAULT_ABSENT_ID_REQUEST = "absent-id-request"
FAULT_INVALID_FRAMING = "invalid-framing"
FAULT_OVERSIZED_MESSAGE = "oversized-message"
FAULT_STDERR_OVERFLOW = "stderr-overflow"
FAULT_RESULT_SHAPE = "result-shape"
FAULT_CLOSED_ENVELOPE = "closed-envelope"
FAULT_INJECTIONS = frozenset(
    (
        FAULT_DUPLICATE_OUTPUT,
        FAULT_PROHIBITED_ADAPTER_REQUEST,
        FAULT_ABSENT_ID_REQUEST,
        FAULT_INVALID_FRAMING,
        FAULT_OVERSIZED_MESSAGE,
        FAULT_STDERR_OVERFLOW,
        FAULT_RESULT_SHAPE,
        FAULT_CLOSED_ENVELOPE,
    )
)

ERROR_CODES = {
    "PARSE_ERROR": -32700,
    "INVALID_REQUEST": -32600,
    "METHOD_NOT_FOUND": -32601,
    "INVALID_PARAMS": -32602,
    "INITIALIZE_REQUIRED": -32005,
    "UNSUPPORTED_PROTOCOL_VERSION": -32004,
    "INVALID_FRAMING": -32000,
    "MESSAGE_TOO_LARGE": -32001,
    "INVALID_SESSION_REF": -32008,
    "SHUTDOWN_IN_PROGRESS": -32016,
}
MAX_MESSAGE_BYTES = 1_048_576
MAX_STDERR_BYTES_PER_CONNECTION = 65_536


@dataclass(frozen=True)
class AdapterIdentity:
    adapter_id: str = "adapter_alpha"
    adapter_revision: str = "adapter_rev1"
    manifest_id: str = "manifest_alpha"
    manifest_revision: str = "manifest_rev1"
    endpoint_id: str = "endpoint_alpha"
    workspace_id: str = "ws_alpha"
    capability_set_id: str = "caps_alpha"
    capability_set_revision: str = "cap_rev1"

    def endpoint(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "workspace_id": self.workspace_id,
            "scope": {"kind": "workspace"},
            "endpoint_id": self.endpoint_id,
            "agent_id": "agent_alpha",
            "adapter_name": self.adapter_id,
            "adapter_revision": self.adapter_revision,
            "trust_class": "managed",
            "capability_set_id": self.capability_set_id,
            "platform": {"os": "other", "architecture": "test"},
            "configuration_ref": {
                "registry_id": "registry_alpha",
                "revision": "registry_rev1",
                "reference": "reference_alpha",
            },
        }

    def initialize_result(self) -> Mapping[str, Any]:
        return {
            "negotiated_protocol_version": NEGOTIATED_PROTOCOL_VERSION,
            "adapter_id": self.adapter_id,
            "adapter_revision": self.adapter_revision,
            "manifest_id": self.manifest_id,
            "manifest_revision": self.manifest_revision,
            "endpoint": self.endpoint(),
            "capability_set": {
                "schema_version": 1,
                "workspace_id": self.workspace_id,
                "scope": {"kind": "workspace"},
                "capability_set_id": self.capability_set_id,
                "revision": self.capability_set_revision,
                "capabilities": (
                    {"capability": "runtime.health", "quality": "unsupported"},
                    {"capability": "runtime.reconcile", "quality": "authoritative"},
                    {"capability": "runtime_profile", "quality": "authoritative"},
                ),
            },
        }

    def health_result(self, scope: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        active_scope = scope if isinstance(scope, Mapping) else {"kind": "workspace"}
        scope_kind = active_scope.get("kind", "workspace")
        result: dict[str, Any] = {
            "status": "healthy",
            "negotiated_protocol_version": NEGOTIATED_PROTOCOL_VERSION,
            "adapter_id": self.adapter_id,
            "adapter_revision": self.adapter_revision,
            "manifest_id": self.manifest_id,
            "manifest_revision": self.manifest_revision,
            "endpoint_id": self.endpoint_id,
            "workspace_id": self.workspace_id,
            "scope_kind": scope_kind,
            "capability_set_id": self.capability_set_id,
            "capability_set_revision": self.capability_set_revision,
        }
        if scope_kind == "project":
            result["project_id"] = active_scope.get("project_id", "project_alpha")
        return result


class ReferenceAdapter:
    """Small JSON-RPC V1 adapter peer with explicit optional fault injection."""

    def __init__(
        self,
        *,
        identity: AdapterIdentity | None = None,
        fault_injection: str | None = None,
    ) -> None:
        if fault_injection is not None and fault_injection not in FAULT_INJECTIONS:
            raise ValueError("unknown reference-adapter fault injection")
        self._identity = identity or AdapterIdentity()
        self._fault_injection = fault_injection
        self._initialized = False
        self._terminal = False
        self._shutdown = False
        self._scope: Mapping[str, Any] = {"kind": "workspace"}
        self._deliveries: Mapping[Any, Mapping[str, str]] = {
            "deliver-1": {
                "message_id": "msg_alpha",
                "delivery_id": "delivery_alpha",
                "attempt_id": "attempt_alpha",
            },
            42: {
                "message_id": "msg_numeric",
                "delivery_id": "delivery_numeric",
                "attempt_id": "attempt_numeric",
            }
        }

    @property
    def fault_injection(self) -> str | None:
        return self._fault_injection

    def handle_text(self, raw: str) -> str | bytes | None:
        if self._terminal:
            return None
        injected = self._injected_output()
        if injected is not None:
            return injected
        try:
            frame = _load_json_frame(raw)
        except DuplicateMemberError:
            self._terminal = True
            return self._error(None, "PARSE_ERROR")
        except (RecursionError, json.JSONDecodeError):
            self._terminal = True
            return self._error(None, "PARSE_ERROR")
        if not isinstance(frame, dict):
            self._terminal = True
            return self._error(None, "INVALID_REQUEST")
        if "result" in frame or "error" in frame:
            self._terminal = True
            return None
        request_id = frame.get("id") if _is_request_id(frame.get("id")) else None
        if "id" not in frame:
            self._terminal = True
            if set(frame) == {"jsonrpc", "method", "params"} and isinstance(frame.get("method"), str) and isinstance(
                frame.get("params"), dict
            ):
                return None
            return self._error(None, "INVALID_REQUEST")
        if not _is_request_id(frame.get("id")):
            self._terminal = True
            return self._error(None, "INVALID_REQUEST")
        if set(frame) != {"jsonrpc", "id", "method", "params"} or frame.get("jsonrpc") != JSONRPC_VERSION:
            return self._error(request_id, "INVALID_REQUEST")
        method = frame["method"]
        params = frame["params"]
        if not isinstance(method, str) or not isinstance(params, dict):
            return self._error(request_id, "INVALID_REQUEST")
        if self._shutdown:
            return self._error(request_id, "SHUTDOWN_IN_PROGRESS")
        if method == METHOD_INITIALIZE:
            if self._initialized:
                return self._error(request_id, "INVALID_REQUEST")
            return self._handle_initialize(request_id, params)
        if not self._initialized:
            self._terminal = True
            return self._error(request_id, "INITIALIZE_REQUIRED")
        if self._shutdown:
            return self._error(request_id, "SHUTDOWN_IN_PROGRESS")
        if method == METHOD_HEALTH:
            return self._handle_health(request_id, params)
        if method == METHOD_RECONCILE:
            return self._handle_reconcile(request_id, params)
        if method == METHOD_SHUTDOWN:
            return self._handle_shutdown(request_id, params)
        if method in {METHOD_DELIVER, METHOD_CANCEL}:
            return self._error(request_id, "INVALID_PARAMS")
        return self._error(request_id, "METHOD_NOT_FOUND")

    def _handle_initialize(self, request_id: Any, params: Mapping[str, Any]) -> str:
        required = {
            "requested_protocol_version",
            "adapter_id",
            "adapter_revision",
            "manifest_id",
            "manifest_revision",
            "endpoint",
        }
        if set(params) != required:
            return self._error(request_id, "INVALID_PARAMS")
        if params.get("requested_protocol_version") != NEGOTIATED_PROTOCOL_VERSION:
            self._terminal = True
            return self._error(request_id, "UNSUPPORTED_PROTOCOL_VERSION")
        expected = self._identity
        endpoint = params.get("endpoint")
        if not isinstance(endpoint, dict) or endpoint != expected.endpoint():
            return self._error(request_id, "INVALID_PARAMS")
        if (
            params.get("adapter_id") != expected.adapter_id
            or params.get("adapter_revision") != expected.adapter_revision
            or params.get("manifest_id") != expected.manifest_id
            or params.get("manifest_revision") != expected.manifest_revision
        ):
            return self._error(request_id, "INVALID_PARAMS")
        self._initialized = True
        return self._result(request_id, expected.initialize_result())

    def _handle_health(self, request_id: Any, params: Mapping[str, Any]) -> str:
        if params:
            return self._error(request_id, "INVALID_PARAMS")
        return self._result(request_id, self._identity.health_result(self._scope))

    def _handle_shutdown(self, request_id: Any, params: Mapping[str, Any]) -> str:
        if params:
            return self._error(request_id, "INVALID_PARAMS")
        self._shutdown = True
        return self._result(request_id, {"status": "shutdown_started"})

    def _handle_reconcile(self, request_id: Any, params: Mapping[str, Any]) -> str:
        required = {"session_ref", "original_request_id", "delivery_id", "attempt_id"}
        if set(params) != required:
            return self._error(request_id, "INVALID_PARAMS")
        session_ref = params["session_ref"]
        if not isinstance(session_ref, dict):
            return self._error(request_id, "INVALID_PARAMS")
        if not self._valid_session_ref(session_ref):
            return self._error(request_id, "INVALID_SESSION_REF")
        if not _is_request_id(params["original_request_id"]):
            return self._error(request_id, "INVALID_PARAMS")
        for key in ("delivery_id", "attempt_id"):
            if not _is_token(params[key], prefix=None):
                return self._error(request_id, "INVALID_PARAMS")
        delivery = self._deliveries.get(params["original_request_id"])
        if delivery is None:
            return self._error(request_id, "INVALID_PARAMS")
        if delivery["delivery_id"] != params["delivery_id"] or delivery["attempt_id"] != params["attempt_id"]:
            return self._error(request_id, "INVALID_PARAMS")
        self._scope = session_ref["scope"]
        receipt = self._receipt(params)
        return self._result(request_id, receipt)

    def _valid_session_ref(self, value: Mapping[str, Any]) -> bool:
        required = {
            "schema_version",
            "workspace_id",
            "scope",
            "session_ref_id",
            "endpoint_id",
            "native_session_id",
            "evidence",
        }
        if not required <= set(value) <= required | {"repository_binding", "extensions"}:
            return False
        if value["schema_version"] != 1 or value["workspace_id"] != self._identity.workspace_id:
            return False
        if value["scope"] != self._identity.endpoint()["scope"]:
            return False
        if "repository_binding" in value:
            return False
        if "extensions" in value and not isinstance(value["extensions"], Mapping):
            return False
        if not _is_token(value["session_ref_id"], prefix="session_"):
            return False
        if value["endpoint_id"] != self._identity.endpoint_id:
            return False
        if not isinstance(value["native_session_id"], str) or not value["native_session_id"]:
            return False
        evidence = value["evidence"]
        if not isinstance(evidence, Mapping):
            return False
        if set(evidence) != {
            "schema_version",
            "workspace_id",
            "scope",
            "evidence_id",
            "evidence_kind",
            "quality",
            "state",
            "authority",
            "subject",
            "correlation_id",
            "observed_at_utc",
            "integrity",
        }:
            return False
        if evidence["schema_version"] != 1 or evidence["workspace_id"] != value["workspace_id"]:
            return False
        if evidence["scope"] != value["scope"]:
            return False
        if evidence["evidence_kind"] != "exact_session_binding" or evidence["quality"] != "authoritative":
            return False
        if evidence["state"] != "visible" or not _is_token(evidence["evidence_id"], prefix="evidence_"):
            return False
        if not _is_token(evidence["correlation_id"], prefix="corr_"):
            return False
        if not isinstance(evidence["observed_at_utc"], str) or not evidence["observed_at_utc"]:
            return False
        authority = evidence["authority"]
        if not isinstance(authority, Mapping) or set(authority) != {
            "authority_kind",
            "identity",
            "implementation_revision",
            "capability_profile_id",
            "capability_profile_revision",
        }:
            return False
        if (
            authority["authority_kind"] != "trusted_adapter"
            or authority["identity"] != self._identity.adapter_id
            or authority["implementation_revision"] != self._identity.adapter_revision
        ):
            return False
        subject = evidence["subject"]
        if not isinstance(subject, Mapping) or set(subject) != {
            "endpoint_id",
            "session_ref_id",
            "native_session_id",
        }:
            return False
        if (
            subject["endpoint_id"] != value["endpoint_id"]
            or subject["session_ref_id"] != value["session_ref_id"]
            or subject["native_session_id"] != value["native_session_id"]
        ):
            return False
        return evidence["integrity"] == _canonical_digest(evidence)

    def _receipt(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        session_ref = params["session_ref"]
        receipt = {
            "schema_version": 1,
            "workspace_id": session_ref["workspace_id"],
            "scope": session_ref["scope"],
            "receipt_id": f"receipt_{params['attempt_id']}",
            "message_id": self._deliveries[params["original_request_id"]]["message_id"],
            "delivery_id": params["delivery_id"],
            "attempt_id": params["attempt_id"],
            "endpoint_id": session_ref["endpoint_id"],
            "session_ref_id": session_ref["session_ref_id"],
            "state": "completed",
        }
        evidence = {
            "schema_version": 1,
            "workspace_id": receipt["workspace_id"],
            "scope": receipt["scope"],
            "evidence_id": f"evidence_{params['attempt_id']}",
            "evidence_kind": "native_delivery_state",
            "quality": "authoritative",
            "state": "completed",
            "authority": {
                "authority_kind": "trusted_adapter",
                "identity": self._identity.adapter_id,
                "implementation_revision": self._identity.adapter_revision,
                "capability_profile_id": "runtime_profile",
                "capability_profile_revision": self._identity.capability_set_revision,
            },
            "subject": {
                "message_id": receipt["message_id"],
                "delivery_id": receipt["delivery_id"],
                "attempt_id": receipt["attempt_id"],
                "endpoint_id": receipt["endpoint_id"],
                "session_ref_id": receipt["session_ref_id"],
            },
            "correlation_id": f"corr_{params['attempt_id']}",
            "observed_at_utc": "2026-07-22T00:00:00Z",
        }
        evidence["integrity"] = _canonical_digest(evidence)
        return {**receipt, "evidence": evidence}

    def _injected_output(self) -> str | bytes | None:
        fault = self._fault_injection
        if fault is None:
            return None
        if fault == FAULT_DUPLICATE_OUTPUT:
            return '{"jsonrpc":"2.0","id":"fault","result":{},"result":{}}'
        if fault == FAULT_PROHIBITED_ADAPTER_REQUEST:
            return _dump({"jsonrpc": JSONRPC_VERSION, "id": "fault", "method": METHOD_HEALTH, "params": {}})
        if fault == FAULT_ABSENT_ID_REQUEST:
            return _dump({"jsonrpc": JSONRPC_VERSION, "method": METHOD_HEALTH, "params": {}})
        if fault == FAULT_INVALID_FRAMING:
            return b"\xff"
        if fault == FAULT_OVERSIZED_MESSAGE:
            return b"x" * (MAX_MESSAGE_BYTES + 2)
        if fault == FAULT_STDERR_OVERFLOW:
            return _dump({"jsonrpc": JSONRPC_VERSION, "id": "fault", "result": self._identity.health_result()})
        if fault == FAULT_RESULT_SHAPE:
            return _dump({"jsonrpc": JSONRPC_VERSION, "id": "fault", "result": {"status": "healthy"}})
        if fault == FAULT_CLOSED_ENVELOPE:
            return _dump({"jsonrpc": JSONRPC_VERSION, "id": "fault", "result": {}, "extra": True})
        raise AssertionError("unreachable fault injection")

    def _result(self, request_id: Any, result: Mapping[str, Any]) -> str:
        return _dump({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result})

    def _error(self, request_id: Any, name: str) -> str:
        return _dump(
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": request_id,
                "error": {
                    "code": ERROR_CODES[name],
                    "message": name,
                    "data": {
                        "name": name,
                        "retryable": False,
                        "request_id": request_id,
                    },
                },
            }
        )


def _is_request_id(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        try:
            return 1 <= len(value.encode("utf-8")) <= 256
        except UnicodeEncodeError:
            return False
    return isinstance(value, int) and -(2**53 - 1) <= value <= 2**53 - 1


def _is_token(value: Any, *, prefix: str | None) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    if prefix is not None:
        return value.startswith(prefix) and len(value) > len(prefix)
    return True


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class DuplicateMemberError(ValueError):
    """Raised when JSON input repeats an object member."""


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise DuplicateMemberError(key)
        seen.add(key)
        out[key] = value
    return out


def _load_json_frame(raw: str) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_duplicate_rejecting_object,
        parse_constant=_reject_json_constant,
    )


def _reject_json_constant(constant: str) -> None:
    raise json.JSONDecodeError("invalid constant", constant, 0)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    material = dict(value)
    material.pop("integrity", None)
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def serve(
    *,
    adapter: ReferenceAdapter,
    stdin: BinaryIO,
    stdout: BinaryIO,
    stderr: BinaryIO,
) -> int:
    while True:
        raw = stdin.readline(MAX_MESSAGE_BYTES + 2)
        if raw == b"":
            return 0
        if len(raw) > MAX_MESSAGE_BYTES + 1:
            response = adapter._error(None, "MESSAGE_TOO_LARGE")
            _write_response(response, stdout)
            return 0
        if not raw.endswith(b"\n"):
            return 0
        if adapter.fault_injection == FAULT_STDERR_OVERFLOW:
            stderr.write(b"x" * (MAX_STDERR_BYTES_PER_CONNECTION + 4096))
            stderr.flush()
        try:
            text = raw.rstrip(b"\n").decode("utf-8")
        except UnicodeDecodeError:
            return 0
        else:
            response = adapter.handle_text(text)
        if response is None:
            return 0
        _write_response(
            response,
            stdout,
            enforce_bound=adapter.fault_injection not in {FAULT_INVALID_FRAMING, FAULT_OVERSIZED_MESSAGE},
        )
        if adapter.fault_injection is not None or adapter._shutdown:
            return 0


def _write_response(response: str | bytes, stdout: BinaryIO, *, enforce_bound: bool = True) -> None:
    payload = response if isinstance(response, bytes) else response.encode("utf-8")
    if enforce_bound and len(payload) > MAX_MESSAGE_BYTES:
        payload = ReferenceAdapter()._error(None, "MESSAGE_TOO_LARGE").encode("utf-8")
    stdout.write(payload + b"\n")
    stdout.flush()


def main(argv: list[str] | None = None, *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None, stderr: BinaryIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runtime Adapter JSON-RPC V1 reference adapter")
    parser.add_argument("--inject", choices=sorted(FAULT_INJECTIONS), default=None)
    args = parser.parse_args(argv)
    adapter = ReferenceAdapter(fault_injection=args.inject)
    return serve(
        adapter=adapter,
        stdin=stdin if stdin is not None else sys.stdin.buffer,
        stdout=stdout if stdout is not None else sys.stdout.buffer,
        stderr=stderr if stderr is not None else sys.stderr.buffer,
    )


if __name__ == "__main__":
    raise SystemExit(main())
