"""Read-only live Codex App Server probe.

The live probe is intentionally narrow: one caller-supplied WebSocket endpoint,
one MCP initialize exchange, one initialized notification, one model/list read,
then disconnect. It does not discover, launch, steer, or store Codex runtime
state.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import socket
import ssl
from typing import Any, Mapping
import urllib.parse

from llm_collab.codex_app_server_probe import CLIENT_CAPABILITIES, CLIENT_INFO, JSONRPC_VERSION, PROTOCOL_VERSION


READ_ONLY_REQUEST_METHODS = ("initialize", "model/list")
READ_ONLY_NOTIFICATION_METHODS = ("initialized",)
EXPECTED_SERVER = "codex-app-server"
EXPECTED_SERVER_CAPABILITIES = frozenset(("tools",))
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CodexAppServerLiveProbeError(ValueError):
    """Raised when the read-only live App Server probe cannot complete safely."""


@dataclass(frozen=True)
class CodexAppServerLiveProbeResult:
    protocol_version: str
    server_name: str
    capabilities: frozenset[str]
    default_model: str | None
    methods: tuple[str, ...]


def probe_live_codex_app_server(
    endpoint_url: str | None = None,
    *,
    transport: Any | None = None,
    expected_server_name: str = EXPECTED_SERVER,
    expected_server_capabilities: frozenset[str] = EXPECTED_SERVER_CAPABILITIES,
    timeout_seconds: float = 5,
    token: str | None = None,
) -> CodexAppServerLiveProbeResult:
    """Run the authorized read-only live probe against a supplied endpoint.

    Default tests pass an injected transport. A real connection is opened only
    when the caller explicitly supplies ``endpoint_url`` and no transport.
    """

    if (endpoint_url is None) == (transport is None):
        raise CodexAppServerLiveProbeError("supply exactly one endpoint_url or transport")
    if transport is not None:
        return _probe_transport(
            transport,
            expected_server_name=expected_server_name,
            expected_server_capabilities=expected_server_capabilities,
        )
    with _WebSocketJsonRpcTransport(endpoint_url, timeout_seconds=timeout_seconds, token=token) as live_transport:
        return _probe_transport(
            live_transport,
            expected_server_name=expected_server_name,
            expected_server_capabilities=expected_server_capabilities,
        )


def _probe_transport(
    transport: Any,
    *,
    expected_server_name: str,
    expected_server_capabilities: frozenset[str],
) -> CodexAppServerLiveProbeResult:
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
    protocol_version = _required_string(initialize, "protocolVersion")
    if protocol_version != PROTOCOL_VERSION:
        raise CodexAppServerLiveProbeError("unsupported protocolVersion")

    server_info = _required_mapping(initialize, "serverInfo")
    server_name = _required_string(server_info, "name")
    if server_name != expected_server_name:
        raise CodexAppServerLiveProbeError("inconsistent server identity")

    capabilities = _capabilities(_required_mapping(initialize, "capabilities"), expected_server_capabilities)
    _notify(transport, "initialized")
    default_model = _default_model(_request(transport, 2, "model/list", {}))
    return CodexAppServerLiveProbeResult(
        protocol_version=protocol_version,
        server_name=server_name,
        capabilities=frozenset(capabilities),
        default_model=default_model,
        methods=READ_ONLY_REQUEST_METHODS,
    )


def _request(transport: Any, request_number: int, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
    if method not in READ_ONLY_REQUEST_METHODS:
        raise CodexAppServerLiveProbeError("method is outside read-only probe set")
    try:
        response = transport.exchange(
            {"jsonrpc": JSONRPC_VERSION, "id": f"llm-collab-{request_number}", "method": method, "params": dict(params)}
        )
    except Exception as error:
        raise CodexAppServerLiveProbeError(f"{method} failed") from error
    envelope = _envelope(response)
    expected_id = f"llm-collab-{request_number}"
    if envelope.get("id") != expected_id:
        raise CodexAppServerLiveProbeError("response id mismatch")
    if "error" in envelope:
        raise CodexAppServerLiveProbeError(f"{method} failed")
    return _required_mapping(envelope, "result")


def _notify(transport: Any, method: str) -> None:
    if method not in READ_ONLY_NOTIFICATION_METHODS:
        raise CodexAppServerLiveProbeError("notification is outside read-only probe set")
    try:
        transport.notify({"jsonrpc": JSONRPC_VERSION, "method": method, "params": {}})
    except Exception as error:
        raise CodexAppServerLiveProbeError(f"{method} failed") from error


def _envelope(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw, object_pairs_hook=_no_duplicates)
        except _DuplicateMember as error:
            raise CodexAppServerLiveProbeError(f"duplicate response member {error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CodexAppServerLiveProbeError("invalid JSON response") from error
    if not isinstance(raw, dict):
        raise CodexAppServerLiveProbeError("response envelope must be an object")
    unknown = set(raw) - {"jsonrpc", "id", "result", "error"}
    if unknown:
        raise CodexAppServerLiveProbeError(f"unknown response member {sorted(unknown)[0]}")
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise CodexAppServerLiveProbeError("invalid jsonrpc version")
    if ("result" in raw) == ("error" in raw):
        raise CodexAppServerLiveProbeError("response must contain exactly one of result or error")
    return raw


def _required_mapping(obj: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = obj.get(key)
    if not isinstance(value, dict):
        raise CodexAppServerLiveProbeError(f"missing or invalid {key}")
    return value


def _required_string(obj: Mapping[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise CodexAppServerLiveProbeError(f"missing or invalid {key}")
    return value


def _capabilities(raw: Mapping[str, Any], expected: frozenset[str]) -> frozenset[str]:
    unknown = set(raw) - expected
    if unknown:
        raise CodexAppServerLiveProbeError(f"unknown capability {sorted(unknown)[0]}")
    missing = expected - set(raw)
    if missing:
        raise CodexAppServerLiveProbeError(f"missing capability {sorted(missing)[0]}")
    return frozenset(raw)


def _default_model(raw: Mapping[str, Any]) -> str | None:
    data = raw.get("data")
    if not isinstance(data, list):
        raise CodexAppServerLiveProbeError("missing model data")
    for model in data:
        if isinstance(model, dict) and model.get("isDefault") is True and isinstance(model.get("id"), str):
            return model["id"]
    for model in data:
        if isinstance(model, dict) and isinstance(model.get("id"), str):
            return model["id"]
    return None


class _WebSocketJsonRpcTransport:
    def __init__(self, endpoint_url: str, *, timeout_seconds: float, token: str | None) -> None:
        self.endpoint_url = endpoint_url
        self.timeout_seconds = timeout_seconds
        self.token = token
        self._socket: socket.socket | None = None
        self._closed = False

    def __enter__(self) -> "_WebSocketJsonRpcTransport":
        parsed = urllib.parse.urlparse(self.endpoint_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise CodexAppServerLiveProbeError("unsupported App Server WebSocket endpoint")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw_socket: socket.socket | None = None
        try:
            raw_socket = socket.create_connection((parsed.hostname, port), timeout=self.timeout_seconds)
            raw_socket.settimeout(self.timeout_seconds)
            if parsed.scheme == "wss":
                raw_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname)
            self._perform_handshake(raw_socket, parsed, port)
        except Exception as error:
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except Exception:
                    pass
            raise CodexAppServerLiveProbeError("websocket handshake failed") from error
        self._socket = raw_socket
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        sock = self._socket
        self._socket = None
        self._closed = True
        if sock is None:
            return
        try:
            self._send_frame(b"", opcode=0x8, sock=sock)
        except OSError:
            pass
        finally:
            sock.close()

    def exchange(self, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        self._send_json(frame)
        return self._recv_json()

    def notify(self, frame: Mapping[str, Any]) -> None:
        self._send_json(frame)

    def _perform_handshake(self, sock: socket.socket, parsed: urllib.parse.ParseResult, port: int) -> None:
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {parsed.hostname}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            headers.append(f"Authorization: Bearer {self.token}")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise CodexAppServerLiveProbeError("empty websocket handshake")
            response += chunk
            if len(response) > 65_536:
                raise CodexAppServerLiveProbeError("oversized websocket handshake")
        header_text = response.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
        status = header_text.splitlines()[0] if header_text.splitlines() else ""
        if " 101 " not in status:
            raise CodexAppServerLiveProbeError("websocket upgrade refused")
        expected_accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode(
            "ascii"
        )
        if expected_accept not in header_text:
            raise CodexAppServerLiveProbeError("invalid websocket accept header")

    def _send_json(self, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send_frame(encoded)

    def _send_frame(self, payload: bytes, opcode: int = 0x1, *, sock: socket.socket | None = None) -> None:
        active = sock or self._socket
        if active is None or self._closed:
            raise CodexAppServerLiveProbeError("websocket is not connected")
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.extend((0x80 | 126, (length >> 8) & 0xFF, length & 0xFF))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        active.sendall(bytes(header) + mask + masked)

    def _recv_json(self) -> Mapping[str, Any]:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise CodexAppServerLiveProbeError("websocket closed")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode != 0x1:
                raise CodexAppServerLiveProbeError("unexpected websocket frame")
            try:
                return _envelope(payload.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise CodexAppServerLiveProbeError("invalid JSON response") from error

    def _recv_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def _read_exact(self, count: int) -> bytes:
        active = self._socket
        if active is None:
            raise CodexAppServerLiveProbeError("websocket is not connected")
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = active.recv(remaining)
            if not chunk:
                raise CodexAppServerLiveProbeError("websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


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
