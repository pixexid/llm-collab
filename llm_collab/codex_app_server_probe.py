"""Inert Codex App Server protocol probe.

This module only speaks to an injected fake transport. It does not discover,
connect to, or mutate any live Codex runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "llm-collab-session-autobridge", "version": "0.0.0"}
CLIENT_CAPABILITIES = {"experimentalApi": True}
READ_ONLY_METHODS = ("initialize", "model/list")
MCP_LIFECYCLE_SPEC = "https://modelcontextprotocol.io/specification/2024-11-05/basic/lifecycle"


class AppServerProbeError(ValueError):
    """Raised when the fake App Server contract drifts or malforms."""


@dataclass(frozen=True)
class AppServerProbeResult:
    protocol_version: str
    server_name: str
    capabilities: frozenset[str]
    default_model: str | None
    methods: tuple[str, ...]


def probe_app_server(
    transport: Any,
    *,
    expected_server_name: str,
    expected_server_capabilities: frozenset[str],
) -> AppServerProbeResult:
    initialize = _request(
        transport,
        1,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "clientInfo": CLIENT_INFO,
            "capabilities": CLIENT_CAPABILITIES,
        },
    )
    _notify(transport, "initialized")

    protocol_version = _required_string(initialize, "protocolVersion")
    if protocol_version != PROTOCOL_VERSION:
        raise AppServerProbeError("unsupported protocolVersion")

    server_info = _required_mapping(initialize, "serverInfo")
    server_name = _required_string(server_info, "name")
    if server_name != expected_server_name:
        raise AppServerProbeError("inconsistent server identity")

    capabilities = _capabilities(_required_mapping(initialize, "capabilities"), expected_server_capabilities)
    default_model = _default_model(_request(transport, 2, "model/list", {}))
    return AppServerProbeResult(
        protocol_version=protocol_version,
        server_name=server_name,
        capabilities=frozenset(capabilities),
        default_model=default_model,
        methods=READ_ONLY_METHODS,
    )


def _request(transport: Any, request_id: int, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
    response = transport.exchange(
        {"jsonrpc": JSONRPC_VERSION, "id": f"llm-collab-{request_id}", "method": method, "params": dict(params)}
    )
    envelope = _envelope(response)
    expected_id = f"llm-collab-{request_id}"
    if envelope.get("id") != expected_id:
        raise AppServerProbeError("response id mismatch")
    if "error" in envelope:
        raise AppServerProbeError(f"{method} failed")
    return _required_mapping(envelope, "result")


def _notify(transport: Any, method: str) -> None:
    transport.notify({"jsonrpc": JSONRPC_VERSION, "method": method, "params": {}})


def _envelope(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw, object_pairs_hook=_no_duplicates)
        except _DuplicateMember as error:
            raise AppServerProbeError(f"duplicate response member {error}") from error
        except json.JSONDecodeError as error:
            raise AppServerProbeError("invalid JSON response") from error
    if not isinstance(raw, dict):
        raise AppServerProbeError("response envelope must be an object")
    unknown = set(raw) - {"jsonrpc", "id", "result", "error"}
    if unknown:
        raise AppServerProbeError(f"unknown response member {sorted(unknown)[0]}")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise AppServerProbeError("invalid jsonrpc version")
    if ("result" in raw) == ("error" in raw):
        raise AppServerProbeError("response must contain exactly one of result or error")
    return raw


def _required_mapping(obj: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = obj.get(key)
    if not isinstance(value, dict):
        raise AppServerProbeError(f"missing or invalid {key}")
    return value


def _required_string(obj: Mapping[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise AppServerProbeError(f"missing or invalid {key}")
    return value


def _capabilities(raw: Mapping[str, Any], expected: frozenset[str]) -> frozenset[str]:
    unknown = set(raw) - expected
    if unknown:
        raise AppServerProbeError(f"unknown capability {sorted(unknown)[0]}")
    for key in expected:
        if key not in raw:
            raise AppServerProbeError(f"missing capability {key}")
    return frozenset(raw)


def _default_model(raw: Mapping[str, Any]) -> str | None:
    data = raw.get("data")
    if not isinstance(data, list):
        raise AppServerProbeError("missing model data")
    for model in data:
        if isinstance(model, dict) and model.get("isDefault") is True and isinstance(model.get("id"), str):
            return model["id"]
    for model in data:
        if isinstance(model, dict) and isinstance(model.get("id"), str):
            return model["id"]
    return None


class _DuplicateMember(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateMember(key)
        seen.add(key)
        out[key] = value
    return out
