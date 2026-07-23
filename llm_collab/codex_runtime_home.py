"""Inert runtime-home identity binding for managed Codex sessions."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import stat


class RuntimeHomeError(ValueError):
    """Raised when a CODEX_HOME path cannot be bound exactly."""


@dataclass(frozen=True)
class RuntimeHomeIdentity:
    runtime_home_realpath: str
    runtime_home_id: str


def bind_runtime_home(
    codex_home: str | os.PathLike[str],
    *,
    expected_realpath: str | None = None,
    expected_id: str | None = None,
) -> RuntimeHomeIdentity:
    raw = _absolute_input(codex_home)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(raw, flags)
    except OSError as error:
        raise RuntimeHomeError("CODEX_HOME identity proof failed") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISDIR(before.st_mode):
            raise RuntimeHomeError("CODEX_HOME must be a directory")
        realpath = _fd_realpath(fd)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
            raise RuntimeHomeError("CODEX_HOME changed during identity binding")
        if not os.path.isabs(realpath):
            raise RuntimeHomeError("runtime_home_realpath must be absolute")
        runtime_home_id = hashlib.sha256(realpath.encode("utf-8")).hexdigest()
        if expected_realpath is not None and expected_realpath != realpath:
            raise RuntimeHomeError("runtime_home_realpath mismatch")
        if expected_id is not None and expected_id != runtime_home_id:
            raise RuntimeHomeError("runtime_home_id mismatch")
        return RuntimeHomeIdentity(realpath, runtime_home_id)
    except OSError as error:
        raise RuntimeHomeError("CODEX_HOME identity proof failed") from error
    finally:
        os.close(fd)


def _absolute_input(value: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise RuntimeHomeError("CODEX_HOME must be an absolute directory path") from error
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise RuntimeHomeError("CODEX_HOME must be an absolute directory path")
    if not Path(raw).is_absolute():
        raise RuntimeHomeError("CODEX_HOME must be absolute")
    return raw


def _fd_realpath(fd: int) -> str:
    if os.path.isdir("/proc/self/fd"):
        return os.readlink(f"/proc/self/fd/{fd}")
    if os.path.isdir("/dev/fd") and hasattr(fcntl, "F_GETPATH"):
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        return raw.split(b"\0", 1)[0].decode("utf-8")
    raise RuntimeHomeError("runtime-home fd realpath proof is unsupported")
