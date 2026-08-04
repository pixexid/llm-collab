#!/usr/bin/env python3
"""Bounded Livecraft readiness and safe user-service recovery."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import ipaddress
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


DEFAULT_BACKEND_URL = "http://127.0.0.1:43121"
DEFAULT_MANAGER_PORT = 43120
DEFAULT_HEALTH_TIMEOUT_SECONDS = 20.0
HEALTH_POLL_INTERVAL_SECONDS = 0.25
HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
HEALTH_RESPONSE_LIMIT = 64 * 1024
LAUNCH_AGENT_LABEL = "com.pixexid.pi-livecraft"
SERVICE_KICK_TIMEOUT_SECONDS = 10.0
_LOCK_PATH = Path(tempfile.gettempdir()) / f"llm-collab-livecraft-{os.getuid()}.lock"


class LivecraftHealthError(RuntimeError):
    """Livecraft is not safe to use for a worker operation."""


@dataclass(frozen=True)
class HealthStatus:
    """One bounded observation of the Livecraft backend."""

    kind: str
    status_code: int | None = None
    manager_connected: bool | None = None
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.kind == "ready"


def require_loopback(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LivecraftHealthError("Livecraft backend URL must be a plain loopback HTTP URL")
    if parsed.path not in ("", "/"):
        raise LivecraftHealthError("Livecraft backend URL must not include a path")
    try:
        port = parsed.port
    except ValueError as error:
        raise LivecraftHealthError("Livecraft backend URL must use a valid port") from error
    if port is not None and not 1 <= port <= 65535:
        raise LivecraftHealthError("Livecraft backend URL must use a valid port")
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise LivecraftHealthError(f"Livecraft backend URL must be loopback, got host {host!r}")
        except ValueError as error:
            raise LivecraftHealthError(f"Livecraft backend URL must be loopback, got host {host!r}") from error
    return url.rstrip("/")


def _read_json(response) -> object:
    raw = response.read(HEALTH_RESPONSE_LIMIT + 1)
    if len(raw) > HEALTH_RESPONSE_LIMIT:
        raise ValueError("health response exceeds the byte limit")
    return json.loads(raw.decode("utf-8")) if raw else None


def _request_health(backend_url: str) -> tuple[int, object]:
    request = urllib.request.Request(
        require_loopback(backend_url) + "/api/health", method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=HEALTH_REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, _read_json(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, _read_json(error)
        finally:
            error.close()


def _is_connection_refused(error: BaseException) -> bool:
    if isinstance(error, ConnectionRefusedError):
        return True
    reason = getattr(error, "reason", None)
    return isinstance(reason, ConnectionRefusedError) or (
        isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED
    )


def probe_health(
    backend_url: str,
    *,
    request: Callable[[str], tuple[int, object]] | None = None,
) -> HealthStatus:
    """Probe the exact backend health contract without mutating Livecraft."""

    require_loopback(backend_url)
    request = request or _request_health
    try:
        status_code, body = request(backend_url)
    except urllib.error.URLError as error:
        kind = "connection_refused" if _is_connection_refused(error) else "unreachable"
        return HealthStatus(kind, detail=str(error.reason))
    except (ConnectionRefusedError, socket.timeout, TimeoutError) as error:
        kind = "connection_refused" if _is_connection_refused(error) else "timeout"
        return HealthStatus(kind, detail=str(error))
    except OSError as error:
        kind = "connection_refused" if _is_connection_refused(error) else "unreachable"
        return HealthStatus(kind, detail=str(error))
    except (UnicodeDecodeError, ValueError) as error:
        return HealthStatus("invalid", detail=str(error))

    if not isinstance(body, dict):
        return HealthStatus("invalid", status_code=status_code, detail="health response is not an object")
    manager_connected = body.get("managerConnected")
    if manager_connected is False:
        return HealthStatus(
            "manager_disconnected",
            status_code=status_code,
            manager_connected=False,
            detail="managerConnected=false",
        )
    if status_code == 200 and body.get("ok") is True and manager_connected is True:
        return HealthStatus("ready", status_code=200, manager_connected=True)
    return HealthStatus(
        "invalid",
        status_code=status_code,
        manager_connected=manager_connected if isinstance(manager_connected, bool) else None,
        detail="health response is not ready",
    )


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def ports_absent(backend_url: str) -> bool:
    """Return true only when both the manager and backend ports are unused."""

    port = urllib.parse.urlparse(backend_url).port
    return not _port_listening(DEFAULT_MANAGER_PORT) and not _port_listening(port or 80)


@contextlib.contextmanager
def service_lock(lock_path: Path = _LOCK_PATH) -> Iterator[None]:
    """Serialize recovery across workers sharing this machine-wide service."""

    # ponytail: one per-user lock; per-worker locks only if recovery throughput matters.
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def kickstart_service(*, runner=None) -> None:
    runner = runner or subprocess.run
    target = f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
    try:
        result = runner(
            ["launchctl", "kickstart", "-k", target],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SERVICE_KICK_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise LivecraftHealthError("launchctl is unavailable; start the Livecraft service manually") from error
    except subprocess.TimeoutExpired as error:
        raise LivecraftHealthError("Livecraft LaunchAgent kickstart timed out") from error
    except OSError as error:
        raise LivecraftHealthError(f"Livecraft LaunchAgent kickstart failed: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no launchctl details").strip()
        raise LivecraftHealthError(f"Livecraft LaunchAgent kickstart failed: {detail}")


def _status_error(status: HealthStatus) -> LivecraftHealthError:
    if status.kind == "manager_disconnected":
        return LivecraftHealthError(
            "Livecraft backend is globally unhealthy: /api/health reports "
            "managerConnected=false; refusing launchctl kickstart because it could "
            "interrupt active Pi sessions; use the guarded manager restart or operator recovery"
        )
    detail = f" ({status.detail})" if status.detail else ""
    return LivecraftHealthError(
        f"Livecraft backend health failed: {status.kind}{detail}"
    )


def ensure_livecraft_ready(
    backend_url: str = DEFAULT_BACKEND_URL,
    *,
    timeout: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    probe: Callable[[str], HealthStatus] = probe_health,
    are_ports_absent: Callable[[str], bool] = ports_absent,
    kickstart: Callable[[], None] = kickstart_service,
    lock: Callable[[], contextlib.AbstractContextManager] | None = None,
) -> HealthStatus:
    """Ensure readiness, allowing at most one safe service recovery kick."""

    require_loopback(backend_url)
    if timeout <= 0:
        raise ValueError("Livecraft health timeout must be positive")
    deadline = clock() + timeout
    lock = lock or service_lock

    def wait_after_kick() -> HealthStatus:
        while True:
            status = probe(backend_url)
            if status.ready:
                return status
            if status.kind == "manager_disconnected":
                raise _status_error(status)
            if status.kind != "connection_refused":
                raise _status_error(status)
            remaining = deadline - clock()
            if remaining <= 0:
                raise LivecraftHealthError(
                    "Livecraft backend remained unreachable after one LaunchAgent restart"
                )
            sleep(min(HEALTH_POLL_INTERVAL_SECONDS, remaining))

    while True:
        status = probe(backend_url)
        if status.ready:
            return status
        if status.kind == "manager_disconnected":
            raise _status_error(status)
        if status.kind != "connection_refused":
            raise _status_error(status)
        if not are_ports_absent(backend_url):
            raise LivecraftHealthError(
                "Livecraft backend connection was refused while manager/backend ports "
                "are occupied; refusing service restart because active Pi sessions may exist"
            )
        remaining = deadline - clock()
        if remaining <= 0:
            raise LivecraftHealthError("Livecraft backend connection refused before recovery could start")
        with lock():
            if deadline - clock() <= 0:
                raise LivecraftHealthError(
                    "Livecraft backend recovery deadline expired while waiting for the recovery lock"
                )
            status = probe(backend_url)
            if status.ready:
                return status
            if status.kind == "manager_disconnected":
                raise _status_error(status)
            if status.kind != "connection_refused":
                raise _status_error(status)
            if not are_ports_absent(backend_url):
                raise LivecraftHealthError(
                    "Livecraft backend connection was refused while manager/backend ports "
                    "are occupied; refusing service restart because active Pi sessions may exist"
                )
            kickstart()
            return wait_after_kick()
