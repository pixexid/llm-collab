"""Read-only live Codex App Server probe.

The live probe is intentionally narrow: one caller-supplied WebSocket endpoint,
one MCP initialize exchange, one initialized notification, one model/list read,
then disconnect. It does not discover, launch, steer, or store Codex runtime
state.

``probe_exact_thread`` extends that with one native ``thread/read`` so a caller can
prove one exact native thread is addressable on a supplied endpoint; it still
starts no turn and stores nothing.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import socket
import ssl
import time
from typing import Any, Mapping
import urllib.parse

from llm_collab.codex_app_server_probe import CLIENT_CAPABILITIES, CLIENT_INFO, JSONRPC_VERSION, PROTOCOL_VERSION


READ_ONLY_REQUEST_METHODS = ("initialize", "model/list")
READ_ONLY_NOTIFICATION_METHODS = ("initialized",)
# The sole thread-touching request: a native thread/read with exactly threadId
# and includeTurns false (no resume, no mutation, no turn fanout).
THREAD_READ_METHOD = "thread/read"
# Bounds for the bounded notification-aware exchange. MAX_EXCHANGE_BYTES is the
# CUMULATIVE byte budget for one whole exchange (notifications + pings + the
# matched response); _recv_frame refuses a declared payload length above the
# remaining budget before reading/allocating it, so a peer cannot exhaust memory
# with many individually-small frames. Values mirror the App Server event caps
# in bin/codex_stream.py (MAX_PENDING_EVENT_BYTES / MAX_PENDING_EVENTS).
MAX_EXCHANGE_BYTES = 8 * 1024 * 1024
MAX_EXCHANGE_NOTIFICATIONS = 4096
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


@dataclass(frozen=True)
class CodexAppServerExactThreadResult:
    """Minimal proof that one exact native thread is addressable on the supplied
    endpoint: only the proven thread id and the methods actually issued. The
    native initialize response carries no protocolVersion/serverInfo, so this
    result reports neither. Runtime-home binding is the caller's authority."""

    thread_id: str
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


def probe_exact_thread(
    thread_id: str,
    *,
    endpoint_url: str | None = None,
    transport: Any | None = None,
    timeout_seconds: float = 5,
    token: str | None = None,
) -> CodexAppServerExactThreadResult:
    """Read-only exact-thread probe: a minimal initialize (request + initialized
    notification, no field validation) then a native thread/read with exactly
    threadId (starts no model turn). Proves one exact native thread is
    addressable on the supplied endpoint by requiring result.thread.id to equal
    the requested id. Fails closed on a malformed id, JSON-RPC error, timeout,
    envelope drift, or a mismatched returned id. Runtime-home binding is the
    caller's authority; the result reports no identity fields the server does
    not return."""
    _require_thread_id(thread_id)
    if (endpoint_url is None) == (transport is None):
        raise CodexAppServerLiveProbeError("supply exactly one endpoint_url or transport")
    if transport is not None:
        return _probe_exact_thread_transport(transport, thread_id)
    with _WebSocketJsonRpcTransport(
        endpoint_url, timeout_seconds=timeout_seconds, token=token
    ) as live_transport:
        return _probe_exact_thread_transport(live_transport, thread_id)


def _minimal_initialize(transport: Any) -> Mapping[str, Any]:
    # Minimal initialize: issue the request + initialized notification the server
    # needs before thread/read. The native initialize response carries no
    # protocolVersion/serverInfo/capabilities, so none are validated or reported
    # here (the existing probe_live validator is left untouched for its own
    # older observable contract).
    initialize = _request(
        transport,
        1,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "clientInfo": CLIENT_INFO,
            "capabilities": CLIENT_CAPABILITIES,
        },
        require_jsonrpc=False,
    )
    _notify(transport, "initialized")
    return initialize


def probe_runtime_home_identity(
    expected_runtime_home: str,
    *,
    endpoint_url: str | None = None,
    transport: Any | None = None,
    timeout_seconds: float = 5,
    token: str | None = None,
) -> str:
    """Initialize one endpoint and require its own ``codexHome`` identity."""
    _require_runtime_home(expected_runtime_home)
    if (endpoint_url is None) == (transport is None):
        raise CodexAppServerLiveProbeError("supply exactly one endpoint_url or transport")
    if transport is not None:
        return _matching_runtime_home(_minimal_initialize(transport), expected_runtime_home)
    with _WebSocketJsonRpcTransport(
        endpoint_url, timeout_seconds=timeout_seconds, token=token
    ) as live_transport:
        return _matching_runtime_home(
            _minimal_initialize(live_transport), expected_runtime_home
        )


def _require_runtime_home(expected_runtime_home: str) -> None:
    if (
        not isinstance(expected_runtime_home, str)
        or not expected_runtime_home
        or "\x00" in expected_runtime_home
        or expected_runtime_home != os.path.realpath(expected_runtime_home)
    ):
        raise CodexAppServerLiveProbeError(
            "expected_runtime_home must be an absolute normalized realpath"
        )


def _matching_runtime_home(
    initialize: Mapping[str, Any], expected_runtime_home: str
) -> str:
    codex_home = initialize.get("codexHome")
    if codex_home != expected_runtime_home:
        raise CodexAppServerLiveProbeError(
            "initialize codexHome does not match the expected runtime home"
        )
    return codex_home


def _read_exact_thread(transport: Any, thread_id: str) -> Mapping[str, Any]:
    result = _request(
        transport, 2, THREAD_READ_METHOD, {"threadId": thread_id, "includeTurns": False},
        require_jsonrpc=False,
    )
    thread = _required_mapping(result, "thread")
    if _required_string(thread, "id") != thread_id:
        raise CodexAppServerLiveProbeError("thread/read returned a different thread id")
    return thread


def _probe_exact_thread_transport(
    transport: Any, thread_id: str
) -> CodexAppServerExactThreadResult:
    _minimal_initialize(transport)
    _read_exact_thread(transport, thread_id)
    return CodexAppServerExactThreadResult(
        thread_id=thread_id,
        methods=("initialize", THREAD_READ_METHOD),
    )


OBSERVATION_ADMISSIBLE = "admissible"
OBSERVATION_BUSY = "busy"
OBSERVATION_UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class CodexAppServerThreadObservation:
    """Strict read-only observation of one exact native thread (#93): the proven
    identity plus the live-proven classification — status.type "idle" with
    canAcceptDirectInput true is admissible, "active" is busy (direct input
    stays true during active turns, so it never discriminates busy), and
    everything else, including a failed read, is uncertain."""

    thread_id: str
    codex_home: str
    user_agent: str
    status_type: str | None
    can_accept_direct_input: bool | None
    classification: str


def classify_thread_observation(
    status_type: str | None, can_accept_direct_input: bool | None
) -> str:
    if status_type == "idle" and can_accept_direct_input is True:
        return OBSERVATION_ADMISSIBLE
    if status_type == "active":
        return OBSERVATION_BUSY
    return OBSERVATION_UNCERTAIN


def observe_exact_thread(
    thread_id: str,
    *,
    expected_runtime_home: str,
    supported_user_agent_prefixes: frozenset[str] | tuple[str, ...],
    endpoint_url: str | None = None,
    transport: Any | None = None,
    timeout_seconds: float = 5,
    token: str | None = None,
) -> CodexAppServerThreadObservation:
    """Strict read-only exact-thread observation (#93). Validates the initialize
    identity (codexHome equals the caller's absolute normalized runtime-home
    realpath; userAgent starts with a supported prefix) and the exact returned
    thread id, then captures status.type/canAcceptDirectInput and classifies
    with classify_thread_observation. includeTurns stays false; no
    resume/start/fork/turn/archive is issued. Identity failures raise; a
    thread/read request failure after identity proof classifies uncertain."""
    _require_thread_id(thread_id)
    _require_runtime_home(expected_runtime_home)
    if (
        not isinstance(supported_user_agent_prefixes, (frozenset, tuple))
        or not supported_user_agent_prefixes
        or any(not isinstance(prefix, str) or not prefix for prefix in supported_user_agent_prefixes)
    ):
        raise CodexAppServerLiveProbeError("supported_user_agent_prefixes must be non-empty strings")
    if (endpoint_url is None) == (transport is None):
        raise CodexAppServerLiveProbeError("supply exactly one endpoint_url or transport")
    if transport is not None:
        return _observe_exact_thread_transport(
            transport, thread_id, expected_runtime_home, supported_user_agent_prefixes
        )
    with _WebSocketJsonRpcTransport(
        endpoint_url, timeout_seconds=timeout_seconds, token=token
    ) as live_transport:
        return _observe_exact_thread_transport(
            live_transport, thread_id, expected_runtime_home, supported_user_agent_prefixes
        )


def _observe_exact_thread_transport(
    transport: Any,
    thread_id: str,
    expected_runtime_home: str,
    supported_user_agent_prefixes: frozenset[str] | tuple[str, ...],
) -> CodexAppServerThreadObservation:
    initialize = _minimal_initialize(transport)
    codex_home = _matching_runtime_home(initialize, expected_runtime_home)
    user_agent = initialize.get("userAgent")
    if not isinstance(user_agent, str) or not any(
        user_agent.startswith(prefix) for prefix in supported_user_agent_prefixes
    ):
        raise CodexAppServerLiveProbeError("unsupported app server user agent")
    try:
        result = _request(
            transport, 2, THREAD_READ_METHOD, {"threadId": thread_id, "includeTurns": False},
            require_jsonrpc=False,
        )
    except CodexAppServerLiveProbeError:
        result = None
    if result is None:
        status_type = can_accept = None
    else:
        thread = _required_mapping(result, "thread")
        if _required_string(thread, "id") != thread_id:
            raise CodexAppServerLiveProbeError("thread/read returned a different thread id")
        status = thread.get("status")
        status_type = status.get("type") if isinstance(status, Mapping) else None
        if not isinstance(status_type, str) or not status_type:
            status_type = None
        can_accept = thread.get("canAcceptDirectInput")
        if not isinstance(can_accept, bool):
            can_accept = None
    return CodexAppServerThreadObservation(
        thread_id=thread_id,
        codex_home=codex_home,
        user_agent=user_agent,
        status_type=status_type,
        can_accept_direct_input=can_accept,
        classification=classify_thread_observation(status_type, can_accept),
    )


def _require_thread_id(thread_id: object) -> None:
    # Native thread ids are server-owned opaque strings; only non-empty string
    # shape is checked here. thread/read plus the returned-id equality check
    # adjudicate whether the id is real.
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise CodexAppServerLiveProbeError("thread_id is required")


def _request(
    transport: Any,
    request_number: int,
    method: str,
    params: Mapping[str, Any],
    *,
    require_jsonrpc: bool = True,
    allowed_methods: frozenset[str] | None = None,
) -> Mapping[str, Any]:
    allowed = allowed_methods or frozenset((*READ_ONLY_REQUEST_METHODS, THREAD_READ_METHOD))
    if method not in allowed:
        raise CodexAppServerLiveProbeError("method is outside the allowed probe set")
    try:
        response = transport.exchange(
            {"jsonrpc": JSONRPC_VERSION, "id": f"llm-collab-{request_number}", "method": method, "params": dict(params)}
        )
    except Exception as error:
        raise CodexAppServerLiveProbeError(f"{method} failed") from error
    envelope = _envelope(response, require_jsonrpc=require_jsonrpc)
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


def _envelope(raw: Any, *, require_jsonrpc: bool = True) -> Mapping[str, Any]:
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
    # probe_live keeps the strict contract (jsonrpc present and 2.0); only the
    # native exact-thread path is jsonrpc-optional (absent allowed; present must be 2.0).
    if require_jsonrpc:
        if raw.get("jsonrpc") != JSONRPC_VERSION:
            raise CodexAppServerLiveProbeError("invalid jsonrpc version")
    elif "jsonrpc" in raw and raw["jsonrpc"] != JSONRPC_VERSION:
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
        if sock is None:
            self._closed = True
            return
        try:
            self._send_frame(b"", opcode=0x8, sock=sock)
        except OSError:
            pass
        finally:
            self._socket = None
            self._closed = True
            sock.close()

    def exchange(self, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one request and wait for its exact id.

        Notifications (a JSON object with a string ``method`` and no ``id``) may
        precede the response and are skipped, bounded by MAX_EXCHANGE_NOTIFICATIONS.
        A server request (``method`` plus ``id``), a malformed frame, a non-matching
        response id, or deadline / cumulative-byte-budget / notification-count
        exceedance all fail closed. The whole exchange shares one absolute timeout
        (set on the transport) and one cumulative byte budget; notifications do
        not reset either. The raw matching response is returned; the caller applies
        the strict response envelope exactly once.
        """
        deadline = time.monotonic() + self.timeout_seconds
        self._send_json(frame, deadline=deadline)
        expected_id = frame.get("id")
        remaining_bytes = MAX_EXCHANGE_BYTES
        skipped = 0
        while True:
            opcode, payload, consumed = self._recv_frame(remaining_bytes, deadline)
            remaining_bytes -= consumed
            if opcode == 0x8:
                raise CodexAppServerLiveProbeError("websocket closed")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode != 0x1:
                raise CodexAppServerLiveProbeError("unexpected websocket frame")
            try:
                message = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates)
            except _DuplicateMember as error:
                raise CodexAppServerLiveProbeError(f"duplicate frame member {error}")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CodexAppServerLiveProbeError("invalid JSON frame") from error
            if not isinstance(message, dict):
                raise CodexAppServerLiveProbeError("frame is not a JSON object")
            has_method = isinstance(message.get("method"), str)
            has_id = "id" in message
            if has_method and not has_id:
                skipped += 1
                if skipped > MAX_EXCHANGE_NOTIFICATIONS:
                    raise CodexAppServerLiveProbeError("too many notifications before response")
                continue
            if has_method and has_id:
                raise CodexAppServerLiveProbeError("server request during exchange")
            if message.get("id") != expected_id:
                raise CodexAppServerLiveProbeError("response id mismatch")
            return message

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

    def _send_json(self, payload: Mapping[str, Any], *, deadline: float | None = None) -> None:
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send_frame(encoded, deadline=deadline)

    def _send_frame(self, payload: bytes, opcode: int = 0x1, *, sock: socket.socket | None = None, deadline: float | None = None) -> None:
        active = sock or self._socket
        if active is None or self._closed:
            raise CodexAppServerLiveProbeError("websocket is not connected")
        if deadline is not None:
            # Bound the send by the same absolute exchange deadline so socket
            # backpressure cannot run past it.
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise CodexAppServerLiveProbeError("exchange timed out")
            active.settimeout(remaining_time)
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

    def _recv_frame(self, remaining_bytes: int, deadline: float) -> tuple[int, bytes, int]:
        first, second = self._read_exact(2, deadline)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2, deadline), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8, deadline), "big")
        # Refuse a declared payload above the remaining CUMULATIVE budget before
        # reading or allocating it, so many individually-small frames cannot
        # exhaust memory.
        if length > remaining_bytes:
            raise CodexAppServerLiveProbeError("exchange byte budget exceeded")
        mask = self._read_exact(4, deadline) if masked else b""
        payload = self._read_exact(length, deadline) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload, length

    def _read_exact(self, count: int, deadline: float) -> bytes:
        active = self._socket
        if active is None:
            raise CodexAppServerLiveProbeError("websocket is not connected")
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            # One absolute deadline for the whole exchange: recompute and re-clamp
            # before EVERY recv so a peer dripping one byte at a time cannot run
            # past it (no successful recv resets the deadline).
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise CodexAppServerLiveProbeError("exchange timed out")
            active.settimeout(remaining_time)
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
