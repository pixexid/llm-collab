"""One inert, same-user Unix control socket for the workspace ledger."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import socket
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Callable

from llm_collab.ledger import LedgerPaths, LedgerStore

REQUEST_LIMIT = 4 * 1024
RESPONSE_LIMIT = 64 * 1024
DEADLINE_SECONDS = 2
LOG_LIMIT = 10 * 1024 * 1024


class ProtocolError(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON member")
        result[key] = value
    return result


def parse_request(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
        raise ProtocolError("invalid control request") from exc
    if not isinstance(value, dict) or set(value) != {"version", "op"}:
        raise ProtocolError("control request must contain exactly version and op")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProtocolError("unsupported control version")
    if not isinstance(value["op"], str) or value["op"] not in {"status", "logs", "shutdown"}:
        raise ProtocolError("unsupported control operation")
    return value["op"]


def peer_uid(connection: socket.socket, *, platform: str | None = None) -> int:
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)
            return struct.unpack("3i", credentials)[1]
        except (AttributeError, OSError, struct.error) as exc:
            raise PermissionError("SO_PEERCRED peer proof unavailable") from exc
    if platform == "darwin":
        getter = getattr(connection, "getpeereid", None)
        if getter is not None:
            try:
                uid, _gid = getter()
                return uid
            except OSError as exc:
                raise PermissionError("getpeereid peer proof unavailable") from exc
        try:
            library = ctypes.CDLL(None, use_errno=True)
            uid = ctypes.c_uint()
            gid = ctypes.c_uint()
            if library.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
                return uid.value
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        except (AttributeError, OSError):
            pass
        try:
            credential = connection.getsockopt(
                getattr(socket, "SOL_LOCAL"), getattr(socket, "LOCAL_PEERCRED"), 12
            )
            return struct.unpack("=I I", credential[:8])[1]
        except (AttributeError, OSError, struct.error) as exc:
            raise PermissionError("Darwin LOCAL_PEERCRED peer proof unavailable") from exc
    raise PermissionError(f"peer credential proof unsupported on {platform}")


def _identity(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"refusing non-socket control path: {path}")
    return info.st_dev, info.st_ino


class DaemonServer:
    """Own a P1a writer store while serving the closed lifecycle vocabulary."""

    def __init__(
        self,
        paths: LedgerPaths,
        *,
        owner_uid: int | None = None,
        peer_uid_getter: Callable[[socket.socket], int] = peer_uid,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.paths = paths
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._peer_uid_getter = peer_uid_getter
        self._clock = clock
        self._stopping = False
        self._socket_identity: tuple[int, int] | None = None

    def run(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            if not store.owns_writer_lock:
                raise RuntimeError("daemon did not acquire the ledger writer lock")
            listener = self._open_listener()
            try:
                self._write_log({"event": "started"})
                while not self._stopping:
                    try:
                        connection, _address = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        self._handle(connection)
            finally:
                listener.close()
                self._remove_owned_socket()
                self._write_log({"event": "stopped"})

    def _open_listener(self) -> socket.socket:
        self._recover_stale_socket()
        old_mask = os.umask(0o077)
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(os.fspath(self.paths.socket))
        finally:
            os.umask(old_mask)
        try:
            os.chmod(self.paths.socket, 0o600)
            self._socket_identity = _identity(self.paths.socket)
            listener.listen(8)
            listener.settimeout(0.1)
            return listener
        except BaseException:
            listener.close()
            self._remove_owned_socket()
            raise

    def _recover_stale_socket(self) -> None:
        try:
            first = _identity(self.paths.socket)
        except FileNotFoundError:
            return
        except RuntimeError:
            raise
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(DEADLINE_SECONDS)
            try:
                probe.connect(os.fspath(self.paths.socket))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise RuntimeError("cannot prove control socket is non-listening") from exc
            else:
                raise RuntimeError(f"control socket is already listening: {self.paths.socket}")
        finally:
            probe.close()
        if _identity(self.paths.socket) != first:
            raise RuntimeError("control socket changed during stale recovery")
        os.unlink(self.paths.socket)

    def _remove_owned_socket(self) -> None:
        if self._socket_identity is None:
            return
        try:
            if _identity(self.paths.socket) == self._socket_identity:
                os.unlink(self.paths.socket)
        except (FileNotFoundError, RuntimeError):
            pass
        finally:
            self._socket_identity = None

    def _handle(self, connection: socket.socket) -> None:
        deadline = self._clock() + DEADLINE_SECONDS
        try:
            if self._peer_uid_getter(connection) != self.owner_uid:
                raise PermissionError("control peer UID mismatch")
            chunks: list[bytes] = []
            total = 0
            while True:
                self._set_remaining(connection, deadline)
                chunk = connection.recv(min(1024, REQUEST_LIMIT + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > REQUEST_LIMIT:
                    raise ProtocolError("control request exceeds 4096 bytes")
                chunks.append(chunk)
            if not chunks:
                raise ProtocolError("empty control request")
            op = parse_request(b"".join(chunks))
            if op == "status":
                response: object = {"version": 1, "running": True, "pid": os.getpid()}
            elif op == "logs":
                response = {"version": 1, "logs": self._read_logs()}
            else:
                self._stopping = True
                response = {"version": 1, "stopping": True}
            self._send(connection, response)
        except (OSError, PermissionError, ProtocolError) as exc:
            self._send(connection, {"version": 1, "error": str(exc)})

    def _send(self, connection: socket.socket, response: object) -> None:
        encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > RESPONSE_LIMIT:
            encoded = b'{"version":1,"error":"response exceeds 65536 bytes"}'
        try:
            self._set_remaining(connection, self._clock() + DEADLINE_SECONDS)
            connection.sendall(encoded)
        except OSError:
            pass

    def _set_remaining(self, connection: socket.socket, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("control I/O deadline exceeded")
        connection.settimeout(remaining)

    def _read_logs(self) -> list[str]:
        try:
            return self.paths.log.read_text(encoding="utf-8").splitlines()[-100:]
        except FileNotFoundError:
            return []

    def _write_log(self, event: dict[str, object]) -> None:
        self.paths.logs.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.paths.logs, 0o700)
        sanitized = {key: "[redacted]" if any(word in key.lower() for word in ("body", "secret", "token", "password", "payload")) else value for key, value in event.items()}
        encoded = (json.dumps(sanitized, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        staged = any(
            self.paths.log.with_name(self.paths.log.name + f".{number}.new").exists()
            for number in range(1, 6)
        )
        if staged or self.paths.log.exists() and self.paths.log.stat().st_size + len(encoded) >= LOG_LIMIT:
            self._rotate_logs()
        fd = os.open(self.paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, encoded)
        finally:
            os.close(fd)

    def _rotate_logs(self) -> None:
        stages = {number: self.paths.log.with_name(self.paths.log.name + f".{number}.new") for number in range(1, 6)}
        if self.paths.log.exists():
            for source_number, stage_number in ((4, 5), (3, 4), (2, 3), (1, 2)):
                source = self.paths.log.with_name(self.paths.log.name + f".{source_number}")
                stage = stages[stage_number]
                if stage.exists() and source.exists():
                    raise RuntimeError("log rotation state is ambiguous")
                if not stage.exists() and source.exists():
                    os.replace(source, stage)
            if stages[1].exists():
                raise RuntimeError("log rotation state is ambiguous")
            os.replace(self.paths.log, stages[1])
        for number in range(1, 6):
            stage = stages[number]
            target = self.paths.log.with_name(self.paths.log.name + f".{number}")
            if stage.exists():
                if number != 5 and target.exists():
                    raise RuntimeError("log rotation state is ambiguous")
                os.replace(stage, target)
