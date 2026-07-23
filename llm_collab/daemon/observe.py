"""Bounded hash/metadata-only observation for the fixed Chats/mailbox source."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import base64
import ctypes
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from .registry import RegistrySnapshot, SOURCE_ID, read_registry_snapshot


RECONCILIATION_SECONDS = 30
DEBOUNCE_SECONDS = 1
SCAN_LIMIT = 2_000
WRITE_LIMIT = 500
MAINTENANCE_LIMIT = 500
RETENTION_DAYS = 30
DIAGNOSTIC_GROUP_LIMIT = 50
DIAGNOSTIC_AUDIT_LIMIT = 200
MAX_SOURCE_BYTES = 16 * 1024 * 1024


class ObservationError(RuntimeError):
    pass


def _json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationError("duplicate source JSON member")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ObservationError(f"non-JSON numeric constant: {value}")


def _safe_relative(path: str) -> str:
    if not isinstance(path, str) or "\x00" in path or "\\" in path:
        raise ObservationError("source path is not normalized workspace-relative evidence")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ObservationError("source path is not normalized workspace-relative evidence")
    normalized = parsed.as_posix()
    if normalized != path:
        raise ObservationError("source path is not normalized workspace-relative evidence")
    return normalized


def _open_workspace_file(
    workspace_root: Path,
    relative_path: str,
    *,
    maximum_bytes: int = MAX_SOURCE_BYTES,
    workspace_fd: int | None = None,
) -> tuple[bytes, os.stat_result]:
    """Open every component relative to one held, no-follow workspace directory."""
    relative_path = _safe_relative(relative_path)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ObservationError("O_NOFOLLOW is required for mailbox observation")
    directory_flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    final_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    final_flags |= getattr(os, "O_NONBLOCK", 0)
    owned = [] if workspace_fd is not None else [os.open(workspace_root, directory_flags)]
    parent_fd = workspace_fd if workspace_fd is not None else owned[-1]
    try:
        components = relative_path.split("/")
        for component in components[:-1]:
            child = os.open(component, directory_flags, dir_fd=parent_fd)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise ObservationError("mailbox path contains a non-directory component")
            owned.append(child)
            parent_fd = child
        fd = os.open(components[-1], final_flags, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
                raise ObservationError("mailbox source is not one bounded regular file")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
            if len(raw) > maximum_bytes:
                raise ObservationError("mailbox source exceeds the byte limit")
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ObservationError("mailbox source changed during read")
            return raw, after
        finally:
            os.close(fd)
    except OSError as exc:
        raise ObservationError(f"mailbox source cannot be opened safely: {relative_path}") from exc
    finally:
        for fd in reversed(owned):
            os.close(fd)


def _scalar_project_id(value: bytes) -> str | None:
    text = value.decode("utf-8", errors="strict").strip()
    if not text or text in {"null", "~"}:
        return None
    if text.startswith(('"', "'")):
        if text.startswith('"'):
            decoded = json.loads(text)
        elif len(text) >= 2 and text.endswith("'"):
            decoded = text[1:-1].replace("''", "'")
        else:
            return None
        return decoded if isinstance(decoded, str) and decoded else None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", text) is None:
        return None
    return text


_INVALID_PROJECT = object()


def _packet_frontmatter_project(raw: bytes) -> str | None | object:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != b"---":
        return None
    values: list[str | None] = []
    closed = False
    for line in lines[1:]:
        if line.strip() == b"---":
            closed = True
            break
        match = re.fullmatch(rb"([ \t]*)project_id\s*:\s*(.*)", line)
        if match:
            if match.group(1):
                return _INVALID_PROJECT
            try:
                values.append(_scalar_project_id(match.group(2)))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _INVALID_PROJECT
    if not closed:
        return _INVALID_PROJECT
    if not values:
        return None
    if len(values) != 1 or values[0] is None:
        return _INVALID_PROJECT
    return values[0]


def _chat_meta_project(
    reader: Callable[[str, int], tuple[bytes, os.stat_result]], packet_path: str
) -> str | None | object:
    packet = PurePosixPath(packet_path)
    meta_path = (packet.parent / "meta.json").as_posix()
    try:
        raw, _status = reader(meta_path, 1024 * 1024)
    except FileNotFoundError:
        return None
    except ObservationError as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        return _INVALID_PROJECT
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ObservationError):
        return _INVALID_PROJECT
    if not isinstance(value, dict):
        return _INVALID_PROJECT
    if "project_id" not in value:
        return None
    project_id = value["project_id"]
    if (
        not isinstance(project_id, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", project_id) is None
    ):
        return _INVALID_PROJECT
    return project_id


def _packet_project(
    reader: Callable[[str, int], tuple[bytes, os.stat_result]],
    packet_path: str,
    raw: bytes,
) -> str | None:
    frontmatter = _packet_frontmatter_project(raw)
    if frontmatter is _INVALID_PROJECT:
        return None
    chat_meta = _chat_meta_project(reader, packet_path)
    if chat_meta is _INVALID_PROJECT:
        return None
    if frontmatter is not None and chat_meta is not None and frontmatter != chat_meta:
        return None
    return frontmatter or chat_meta


def _fixed_packet_pointer(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = _safe_relative(value)
    except ObservationError:
        return None
    parts = PurePosixPath(normalized).parts
    if len(parts) != 3 or parts[0] != "Chats" or not parts[2].endswith(".md"):
        return None
    return normalized


def _parse_inbox(raw: bytes) -> list[tuple[str, object]] | None:
    try:
        inbox = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ObservationError):
        return None
    if not isinstance(inbox, dict):
        return None
    flattened: list[tuple[str, object]] = []
    for bucket in ("read", "unread"):
        pointers = inbox.get(bucket)
        if not isinstance(pointers, list):
            return None
        for value in pointers:
            flattened.append((bucket, value))
    return flattened


def _candidate(
    relative_path: str,
    content: bytes,
    status: os.stat_result,
    *,
    scan_cursor: str,
    scan_count: int,
) -> dict[str, object]:
    mtime_ns = status.st_mtime_ns
    if (
        isinstance(mtime_ns, bool)
        or not isinstance(mtime_ns, int)
        or not -(1 << 63) <= mtime_ns <= (1 << 63) - 1
    ):
        raise ObservationError("mailbox source mtime is not a signed 64-bit integer")
    content_sha256 = hashlib.sha256(content).hexdigest()
    identity = f"{relative_path}\x00{content_sha256}".encode("utf-8")
    return {
        "dedupe_key": hashlib.sha256(identity).hexdigest(),
        "path": relative_path,
        "content_sha256": content_sha256,
        "byte_size": len(content),
        "mtime_ns": mtime_ns,
        "scan_cursor": scan_cursor,
        "scan_count": scan_count,
    }


class _BudgetExhausted(Exception):
    pass


class _ScanBudget:
    def __init__(self, limit: int = SCAN_LIMIT) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= SCAN_LIMIT:
            raise ObservationError("scan limit is invalid")
        self.limit = limit
        self.used = 0

    def charge(self) -> None:
        if self.used == self.limit:
            raise _BudgetExhausted
        self.used += 1

    def read(
        self,
        workspace_root: Path,
        path: str,
        maximum_bytes: int = MAX_SOURCE_BYTES,
        *,
        workspace_fd: int | None = None,
    ) -> tuple[bytes, os.stat_result]:
        self.charge()
        return _open_workspace_file(
            workspace_root, path, maximum_bytes=maximum_bytes, workspace_fd=workspace_fd
        )


_DIRENT_BUFFER_BYTES = 1_056
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class _WorkspaceAuthority:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        status = os.fstat(fd)
        if not stat.S_ISDIR(status.st_mode):
            raise ObservationError("workspace root is not a directory")
        self.identity = (status.st_dev, status.st_ino)

    @classmethod
    def open(cls, workspace_root: Path) -> "_WorkspaceAuthority":
        if getattr(os, "O_NOFOLLOW", None) is None:
            raise ObservationError("O_NOFOLLOW is required for mailbox observation")
        try:
            fd = os.open(workspace_root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise ObservationError("workspace root cannot be opened safely") from exc
        try:
            return cls(fd)
        except BaseException:
            os.close(fd)
            raise

    def revalidate(self) -> None:
        status = os.fstat(self.fd)
        if (
            not stat.S_ISDIR(status.st_mode)
            or (status.st_dev, status.st_ino) != self.identity
        ):
            raise ObservationError("workspace root identity changed during reconciliation")

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)


def _pack_names(names: list[bytes]) -> str:
    packed = b"".join(len(name).to_bytes(2, "big") + name for name in names)
    return base64.b64encode(packed).decode("ascii")


def _unpack_names(encoded: object) -> list[bytes]:
    if not isinstance(encoded, str):
        raise ObservationError("observation directory cursor is invalid")
    try:
        packed = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ObservationError("observation directory cursor is invalid") from exc
    names: list[bytes] = []
    offset = 0
    while offset < len(packed):
        if offset + 2 > len(packed):
            raise ObservationError("observation directory cursor is invalid")
        length = int.from_bytes(packed[offset : offset + 2], "big")
        offset += 2
        if length == 0 or offset + length > len(packed):
            raise ObservationError("observation directory cursor is invalid")
        names.append(packed[offset : offset + length])
        offset += length
    return names


def _directory_block(fd: int, offset: int) -> tuple[list[bytes], int]:
    """Read one small native dirent page whose unconsumed names fit the cursor."""
    try:
        os.lseek(fd, offset, os.SEEK_SET)
    except (OSError, OverflowError, ValueError) as exc:
        raise ObservationError("observation directory cursor cannot be resumed") from exc
    library = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(_DIRENT_BUFFER_BYTES)
    if sys.platform == "darwin":
        reader = getattr(library, "__getdirentries64", None)
        if reader is None:
            raise ObservationError("Darwin directory enumeration is unavailable")
        reader.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_longlong),
        ]
        reader.restype = ctypes.c_ssize_t
        base = ctypes.c_longlong()
        count = reader(fd, buffer, _DIRENT_BUFFER_BYTES, ctypes.byref(base))
        name_offset = 21
    elif sys.platform.startswith("linux"):
        reader = getattr(library, "getdents64", None)
        if reader is not None:
            reader.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
            reader.restype = ctypes.c_ssize_t
            count = reader(fd, buffer, _DIRENT_BUFFER_BYTES)
        else:
            machine = os.uname().machine
            syscall_number = {"x86_64": 217, "aarch64": 61}.get(machine)
            if syscall_number is None:
                raise ObservationError(
                    f"Linux directory enumeration is unsupported on {machine}"
                )
            syscall = library.syscall
            syscall.argtypes = [
                ctypes.c_long,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            syscall.restype = ctypes.c_long
            count = syscall(syscall_number, fd, buffer, _DIRENT_BUFFER_BYTES)
        name_offset = 19
    else:
        raise ObservationError(
            f"bounded directory enumeration is unsupported on {sys.platform}"
        )
    if count < 0:
        error = ctypes.get_errno()
        raise ObservationError("bounded directory enumeration failed") from OSError(
            error, os.strerror(error)
        )
    next_offset = os.lseek(fd, 0, os.SEEK_CUR)
    raw = buffer.raw[:count]
    names: list[bytes] = []
    position = 0
    while position < count:
        if position + name_offset + 1 > count:
            raise ObservationError("native directory entry is truncated")
        record_length = int.from_bytes(
            raw[position + 16 : position + 18], sys.byteorder
        )
        if record_length < name_offset + 1 or position + record_length > count:
            raise ObservationError("native directory entry has an invalid length")
        encoded_name = raw[position + name_offset : position + record_length]
        if b"\0" not in encoded_name:
            raise ObservationError("native directory entry name is unterminated")
        name = encoded_name.split(b"\0", 1)[0]
        if name not in {b".", b".."}:
            names.append(name)
        position += record_length
    return names, next_offset


class _DirectoryReader:
    def __init__(self, fd: int, state: object = None) -> None:
        self.fd = fd
        status = os.fstat(fd)
        self.identity = (status.st_dev, status.st_ino, status.st_mtime_ns)
        self.offset = 0
        self.pending: list[bytes] = []
        self.resumed = False
        if state is not None:
            self.validate_state(state)
            if tuple(state[:3]) == self.identity:
                self.offset = state[3]
                self.pending = _unpack_names(state[4])
                self.resumed = True

    @staticmethod
    def validate_state(state: object) -> None:
        if (
            not isinstance(state, list)
            or len(state) != 5
            or any(type(item) is not int for item in state[:4])
            or state[0] < 0
            or state[1] < 0
            or not 0 <= state[3] <= (1 << 63) - 1
            or not isinstance(state[4], str)
        ):
            raise ObservationError("observation directory cursor is invalid")

    @classmethod
    def open_at(
        cls, parent_fd: int, name: str, state: object = None
    ) -> "_DirectoryReader":
        if not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise ObservationError("observation directory component is invalid")
        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise ObservationError("observation directory cannot be opened safely") from exc
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise ObservationError("observation directory is not a directory")
            return cls(fd, state)
        except BaseException:
            os.close(fd)
            raise

    def state(self) -> list[object]:
        return [*self.identity, self.offset, _pack_names(self.pending)]

    def next_name(self, budget: _ScanBudget) -> str | None:
        while True:
            if not self.pending:
                previous_offset = self.offset
                try:
                    self.pending, self.offset = _directory_block(self.fd, self.offset)
                except ObservationError:
                    if self.resumed and previous_offset != 0:
                        self.offset = 0
                        self.pending = []
                        self.resumed = False
                        continue
                    raise
                if not self.pending:
                    if self.offset == previous_offset:
                        return None
                    continue
            budget.charge()
            raw = self.pending.pop(0)
            try:
                name = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
            if name and name not in {".", ".."} and "/" not in name and "\0" not in name:
                return name

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            os.close(fd)


def _decode_walk_cursor(cursor: str) -> dict[str, object] | None:
    if not cursor:
        return None
    if len(cursor.encode("utf-8")) > 4096:
        raise ObservationError("observation cursor is invalid")
    try:
        value = json.loads(
            cursor,
            object_pairs_hook=_json_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ObservationError) as exc:
        raise ObservationError("observation cursor is invalid") from exc
    if (
        not isinstance(value, dict)
        or type(value.get("v")) is not int
        or value.get("v") != 1
        or value.get("p") not in {"c", "a"}
    ):
        raise ObservationError("observation cursor is invalid")
    allowed = {"v", "p", "r", "d", "i"}
    if not set(value) <= allowed or "r" not in value:
        raise ObservationError("observation cursor is invalid")
    if "d" in value and value["p"] != "c":
        raise ObservationError("observation cursor is invalid")
    if "i" in value and value["p"] != "a":
        raise ObservationError("observation cursor is invalid")
    return value


def _encode_walk_cursor(
    phase: str,
    root: _DirectoryReader,
    *,
    child_name: str | None = None,
    child: _DirectoryReader | None = None,
    inbox: dict[str, object] | None = None,
) -> str:
    value: dict[str, object] = {"v": 1, "p": phase, "r": root.state()}
    if child_name is not None and child is not None:
        value["d"] = [child_name, child.state()]
    if inbox is not None:
        value["i"] = inbox
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > 4096:
        raise ObservationError("observation cursor exceeds its fixed bound")
    return encoded


class _SourceWalker:
    def __init__(
        self,
        workspace_fd: int,
        budget: _ScanBudget,
        cursor: str,
    ) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise ObservationError("O_NOFOLLOW is required for mailbox observation")
        self.saved = _decode_walk_cursor(cursor)
        self.workspace_fd = workspace_fd
        self.budget = budget
        self.phase = "c" if self.saved is None else str(self.saved["p"])
        self.root: _DirectoryReader | None = None
        self.child: _DirectoryReader | None = None
        self.child_name: str | None = None
        self.inbox_resume: dict[str, object] | None = None
        self.cursor = cursor
        try:
            self._open_phase()
            if self.phase == "c" and self.root is None:
                self._transition_to_agents()
        except BaseException:
            self.close()
            raise

    def _open_phase(self) -> None:
        name = "Chats" if self.phase == "c" else "agents"
        state = None if self.saved is None else self.saved.get("r")
        if state is not None:
            _DirectoryReader.validate_state(state)
        try:
            self.root = _DirectoryReader.open_at(self.workspace_fd, name, state)
        except ObservationError:
            self.root = None
            self.cursor = ""
            return
        if not self.root.resumed:
            self.saved = None
        if self.phase == "c" and self.saved is not None and "d" in self.saved:
            child_value = self.saved["d"]
            if (
                not isinstance(child_value, list)
                or len(child_value) != 2
                or not isinstance(child_value[0], str)
            ):
                raise ObservationError("observation cursor is invalid")
            self.child_name = child_value[0]
            _DirectoryReader.validate_state(child_value[1])
            try:
                self.child = _DirectoryReader.open_at(
                    self.root.fd, self.child_name, child_value[1]
                )
            except ObservationError:
                self.child = None
                self.child_name = None
            if self.child is not None and not self.child.resumed:
                self.cursor = _encode_walk_cursor(
                    "c", self.root, child_name=self.child_name, child=self.child
                )
        elif self.phase == "a" and self.saved is not None and "i" in self.saved:
            inbox = self.saved["i"]
            if not isinstance(inbox, dict):
                raise ObservationError("observation cursor is invalid")
            self.inbox_resume = inbox
        self._refresh_cursor()

    def _refresh_cursor(self) -> None:
        if self.root is None:
            self.cursor = ""
        elif self.phase == "c" and self.child is not None:
            self.cursor = _encode_walk_cursor(
                "c", self.root, child_name=self.child_name, child=self.child
            )
        else:
            self.cursor = _encode_walk_cursor(self.phase, self.root)

    def _transition_to_agents(self) -> bool:
        if self.child is not None:
            self.child.close()
            self.child = None
            self.child_name = None
        if self.root is not None:
            self.root.close()
        self.phase = "a"
        self.saved = None
        try:
            self.root = _DirectoryReader.open_at(self.workspace_fd, "agents")
        except ObservationError:
            self.root = None
            self.cursor = ""
            return False
        self._refresh_cursor()
        return True

    def next(self) -> dict[str, object] | None:
        if self.phase == "a" and self.inbox_resume is not None:
            inbox, self.inbox_resume = self.inbox_resume, None
            if (
                set(inbox) != {"n", "x", "f"}
                or not isinstance(inbox["n"], str)
                or not inbox["n"]
                or inbox["n"] in {".", ".."}
                or "/" in inbox["n"]
                or "\0" in inbox["n"]
            ):
                raise ObservationError("observation inbox cursor is invalid")
            if type(inbox["x"]) is not int or inbox["x"] < 0:
                raise ObservationError("observation inbox cursor is invalid")
            file_identity = inbox["f"]
            if (
                not isinstance(file_identity, list)
                or len(file_identity) != 4
                or any(type(item) is not int for item in file_identity)
                or any(file_identity[index] < 0 for index in (0, 1, 2))
            ):
                raise ObservationError("observation inbox cursor is invalid")
            after = _encode_walk_cursor("a", self.root)
            return {
                "path": f"agents/{inbox['n']}/inbox.json",
                "before": self.cursor,
                "after": after,
                "agent": inbox["n"],
                "pointer": inbox["x"],
                "file_identity": tuple(file_identity),
            }

        while self.root is not None:
            if self.phase == "c":
                if self.child is not None:
                    before = self.cursor
                    name = self.child.next_name(self.budget)
                    self._refresh_cursor()
                    if name is None:
                        self.child.close()
                        self.child = None
                        self.child_name = None
                        self._refresh_cursor()
                        continue
                    if name.endswith(".md"):
                        return {
                            "path": f"Chats/{self.child_name}/{name}",
                            "before": before,
                            "after": self.cursor,
                        }
                    continue
                name = self.root.next_name(self.budget)
                self._refresh_cursor()
                if name is None:
                    if not self._transition_to_agents():
                        return None
                    continue
                try:
                    child = _DirectoryReader.open_at(self.root.fd, name)
                except ObservationError:
                    continue
                self.child_name = name
                self.child = child
                self._refresh_cursor()
                continue

            before = self.cursor
            name = self.root.next_name(self.budget)
            self._refresh_cursor()
            if name is None:
                self.root.close()
                self.root = None
                self.cursor = ""
                return None
            try:
                directory = _DirectoryReader.open_at(self.root.fd, name)
            except ObservationError:
                continue
            directory.close()
            return {
                "path": f"agents/{name}/inbox.json",
                "before": before,
                "after": self.cursor,
                "agent": name,
                "pointer": 0,
                "file_identity": None,
            }
        return None

    def inbox_cursor(
        self, *, agent: str, pointer: int, status: os.stat_result
    ) -> str:
        if self.root is None or self.phase != "a":
            raise ObservationError("inbox cursor is outside the agents phase")
        return _encode_walk_cursor(
            "a",
            self.root,
            inbox={
                "n": agent,
                "x": pointer,
                "f": [
                    status.st_dev,
                    status.st_ino,
                    status.st_size,
                    status.st_mtime_ns,
                ],
            },
        )

    def close(self) -> None:
        if self.child is not None:
            self.child.close()
            self.child = None
        if self.root is not None:
            self.root.close()
            self.root = None
        self.workspace_fd = -1


def scan_mailbox(
    workspace_root: Path,
    *,
    project_id: str,
    registry_revision: str,
    cursor: str,
    scan_limit: int = SCAN_LIMIT,
    workspace_fd: int | None = None,
) -> tuple[list[dict[str, object]], str, int]:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", registry_revision) is None:
        raise ObservationError("registry revision is invalid")
    _decode_walk_cursor(cursor)
    budget = _ScanBudget(scan_limit)
    candidates: list[dict[str, object]] = []
    authority = (
        None if workspace_fd is not None else _WorkspaceAuthority.open(workspace_root)
    )
    active_workspace_fd = workspace_fd if workspace_fd is not None else authority.fd

    def read(path: str, maximum_bytes: int = MAX_SOURCE_BYTES):
        return budget.read(
            workspace_root, path, maximum_bytes, workspace_fd=active_workspace_fd
        )

    try:
        walker = _SourceWalker(active_workspace_fd, budget, cursor)
        last_cursor = walker.cursor
        try:
            while True:
                try:
                    entry = walker.next()
                except _BudgetExhausted:
                    return candidates, walker.cursor, budget.used
                if entry is None:
                    return candidates, "", budget.used
                relative_path = str(entry["path"])
                last_cursor = str(entry["before"])
                try:
                    raw, status = read(relative_path)
                    if relative_path.startswith("Chats/"):
                        if _packet_project(read, relative_path, raw) == project_id:
                            next_cursor = str(entry["after"])
                            candidates.append(
                                _candidate(
                                    relative_path,
                                    raw,
                                    status,
                                    scan_cursor=next_cursor,
                                    scan_count=budget.used,
                                )
                            )
                        last_cursor = str(entry["after"])
                        continue

                    pointers = _parse_inbox(raw)
                    if pointers is None:
                        last_cursor = str(entry["after"])
                        continue
                    agent = str(entry["agent"])
                    expected = entry["file_identity"]
                    actual = (
                        status.st_dev,
                        status.st_ino,
                        status.st_size,
                        status.st_mtime_ns,
                    )
                    start_pointer = int(entry["pointer"]) if expected == actual else 0
                    for index in range(start_pointer, len(pointers)):
                        last_cursor = walker.inbox_cursor(
                            agent=agent, pointer=index, status=status
                        )
                        budget.charge()
                        bucket, value = pointers[index]
                        next_cursor = walker.inbox_cursor(
                            agent=agent, pointer=index + 1, status=status
                        )
                        pointer = _fixed_packet_pointer(value)
                        if pointer is not None:
                            try:
                                packet_raw, _packet_status = read(pointer)
                                if (
                                    _packet_project(read, pointer, packet_raw)
                                    == project_id
                                ):
                                    content = json.dumps(
                                        {
                                            "agent": agent,
                                            "bucket": bucket,
                                            "pointer": pointer,
                                            "project_id": project_id,
                                        },
                                        ensure_ascii=True,
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    ).encode("utf-8")
                                    candidates.append(
                                        _candidate(
                                            relative_path,
                                            content,
                                            status,
                                            scan_cursor=next_cursor,
                                            scan_count=budget.used,
                                        )
                                    )
                            except ObservationError:
                                pass
                        last_cursor = next_cursor
                    last_cursor = str(entry["after"])
                except _BudgetExhausted:
                    return candidates, last_cursor, budget.used
                except ObservationError:
                    last_cursor = str(entry["after"])
        finally:
            walker.close()
    finally:
        if authority is not None:
            authority.close()


def _load_watchdog() -> tuple[type[object], type[object]]:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    return FileSystemEventHandler, Observer


def _scheduler_order(project_ids: list[str], cursor: str | None) -> list[str]:
    ordered = sorted(project_ids)
    if not ordered:
        return []
    if cursor is None:
        start = 0
    elif cursor in ordered:
        start = ordered.index(cursor)
    else:
        start = 0
        for index, project_id in enumerate(ordered):
            if project_id > cursor:
                start = index
                break
        else:
            start = 0
    return ordered[start:] + ordered[:start]


class ObservationEngine:
    """Run reconciliation on the daemon thread; watchdog callbacks only mark dirty."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace_id: str,
        projects_path: Path,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.workspace_root = workspace_root
        self.workspace_id = workspace_id
        self.projects_path = projects_path
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._dirty = threading.Event()
        self._dirty.set()
        self._last_reconcile: float | None = None
        self._dirty_since: float | None = None
        self._observer: object | None = None
        self._snapshot: RegistrySnapshot | None = None
        self._last_result: dict[str, object] | None = None
        self._watchdog_error: str | None = None

    def start(self) -> None:
        try:
            handler_base, observer_type = _load_watchdog()
        except Exception as exc:
            self._watchdog_error = f"{type(exc).__name__}: {exc}"
            return
        dirty = self.mark_dirty

        class HintHandler(handler_base):
            def on_any_event(self, _event: object) -> None:
                dirty()

        try:
            observer = observer_type()
        except Exception as exc:
            self._watchdog_error = f"{type(exc).__name__}: {exc}"
            return
        scheduled = 0
        for name in ("Chats", "agents"):
            path = self.workspace_root / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    observer.schedule(HintHandler(), os.fspath(path), recursive=True)
                    scheduled += 1
                except Exception as exc:
                    self._watchdog_error = f"{type(exc).__name__}: {exc}"
        if scheduled:
            try:
                observer.start()
                self._observer = observer
            except Exception as exc:
                try:
                    observer.stop()
                    observer.join(timeout=2)
                except Exception:
                    pass
                self._watchdog_error = f"{type(exc).__name__}: {exc}"

    def mark_dirty(self) -> None:
        if not self._dirty.is_set():
            self._dirty_since = self._monotonic()
        self._dirty.set()

    def reconcile_due(self, store: object, *, force: bool = False) -> bool:
        now = self._monotonic()
        if not force and self._last_reconcile is not None:
            periodic = now - self._last_reconcile >= RECONCILIATION_SECONDS
            debounced = (
                self._dirty.is_set()
                and self._dirty_since is not None
                and now - self._dirty_since >= DEBOUNCE_SECONDS
            )
            if not periodic and not debounced:
                return False
        # Anchor and consume the current hint before I/O. A failure is retried on
        # the next 30-second cadence (or a genuinely new debounced event), not
        # every control-loop tick. Events arriving during I/O re-dirty the engine.
        self._last_reconcile = now
        self._dirty.clear()
        self._dirty_since = None
        self._last_result = self.reconcile(store)
        return True

    def reconcile(self, store: object) -> dict[str, object]:
        snapshot = read_registry_snapshot(
            self.projects_path,
            workspace_id=self.workspace_id,
            clock=self._wall_clock,
        )
        snapshot.record(store)
        self._snapshot = snapshot
        now = self._wall_clock().astimezone(timezone.utc)
        observed_at = now.isoformat()
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        results: dict[str, object] = {}
        project_ids = sorted(snapshot.project_ids)
        scan_remaining = SCAN_LIMIT
        write_remaining = WRITE_LIMIT
        maintenance_remaining = MAINTENANCE_LIMIT
        if not project_ids:
            store.clear_observation_scheduler_cursor(
                workspace_id=self.workspace_id,
                source_id=SOURCE_ID,
            )
            return {
                "projects": {},
                "project_count": 0,
                "truncated_projects": 0,
                "budget": {
                    "scan_remaining": scan_remaining,
                    "write_remaining": write_remaining,
                    "maintenance_remaining": maintenance_remaining,
                },
            }
        start_cursor = store.observation_scheduler_cursor(
            workspace_id=self.workspace_id,
            source_id=SOURCE_ID,
        )
        scheduled = _scheduler_order(project_ids, start_cursor)
        authority = _WorkspaceAuthority.open(self.workspace_root)
        try:
            for index, project_id in enumerate(scheduled):
                if (
                    scan_remaining <= 0
                    or write_remaining <= 0
                    or maintenance_remaining < 3
                ):
                    break
                cursor = store.observation_checkpoint_cursor(
                    workspace_id=self.workspace_id,
                    project_id=project_id,
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                )
                candidates, next_cursor, scanned_count = scan_mailbox(
                    self.workspace_root,
                    project_id=project_id,
                    registry_revision=snapshot.registry_revision,
                    cursor=cursor,
                    scan_limit=scan_remaining,
                    workspace_fd=authority.fd,
                )
                scan_remaining -= scanned_count
                authority.revalidate()
                result = store.reconcile_observations(
                    workspace_id=self.workspace_id,
                    project_id=project_id,
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    candidates=candidates,
                    next_cursor=next_cursor,
                    scanned_count=scanned_count,
                    observed_at_utc=observed_at,
                    write_limit=write_remaining,
                )
                maintenance_remaining -= 2
                write_remaining -= int(result["written"])
                next_project_id = scheduled[(index + 1) % len(scheduled)]
                store.advance_observation_scheduler_cursor(
                    workspace_id=self.workspace_id,
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    next_project_id=next_project_id,
                    updated_at_utc=observed_at,
                )
                maintenance_remaining -= 1
                authority.revalidate()
                removed = 0
                if maintenance_remaining >= 2:
                    prune_limit = min(WRITE_LIMIT, maintenance_remaining - 1)
                    removed = store.prune_resolved_observations(
                        workspace_id=self.workspace_id,
                        project_id=project_id,
                        source_id=SOURCE_ID,
                        registry_revision=snapshot.registry_revision,
                        resolved_before_utc=cutoff,
                        occurred_at_utc=observed_at,
                        limit=prune_limit,
                    )
                    maintenance_remaining -= removed + 1
                if index < 50:
                    results[project_id] = {
                        "scanned": result["scanned"],
                        "written": result["written"],
                        "incomplete": bool(result["cursor"]),
                        "pruned": removed,
                    }
        finally:
            authority.close()
        project_count = len(project_ids)
        return {
            "projects": results,
            "project_count": project_count,
            "truncated_projects": max(0, project_count - 50),
            "budget": {
                "scan_remaining": scan_remaining,
                "write_remaining": write_remaining,
                "maintenance_remaining": maintenance_remaining,
            },
        }

    def diagnostics(
        self, store: object, integrity: str | None = None
    ) -> dict[str, object]:
        roots = {}
        for name in ("Chats", "agents"):
            try:
                info = os.lstat(self.workspace_root / name)
                roots[name] = stat.S_ISDIR(info.st_mode)
            except OSError:
                roots[name] = False
        return {
            "source_id": SOURCE_ID,
            "source_reachability": roots,
            "registry_revision": (
                None if self._snapshot is None else self._snapshot.registry_revision
            ),
            "last_reconcile": self._last_result,
            "watchdog_error": self._watchdog_error,
            "ledger": store.observation_diagnostics(
                workspace_id=self.workspace_id,
                integrity=integrity,
                group_limit=DIAGNOSTIC_GROUP_LIMIT,
                audit_limit=DIAGNOSTIC_AUDIT_LIMIT,
            ),
        }

    def close(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)
