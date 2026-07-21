"""Hash-only provenance import for current session-autobridge JSON files."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from llm_collab.ledger.store import LedgerStore


MAX_FILES = 5_000
MAX_FILE_BYTES = 1_048_576
IMPORT_REVISION = "legacy-provenance/1"
_SOURCES = (
    (
        "session_autobridge",
        "session",
        "sessions",
        ("State", "session_autobridge", "sessions"),
    ),
    (
        "session_autobridge",
        "activation_lease",
        "activation_leases",
        ("State", "session_autobridge", "activation_leases"),
    ),
)


class LegacyImportError(RuntimeError):
    """The closed legacy source set could not be collected safely."""


class _DuplicateMember(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dir_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise LegacyImportError("O_NOFOLLOW is required for legacy provenance import")
    return (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise LegacyImportError("O_NOFOLLOW is required for legacy provenance import")
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nonblock is None:
        raise LegacyImportError("O_NONBLOCK is required for legacy provenance import")
    return os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)


def _identity(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _directory_identity(status: os.stat_result) -> tuple[int, int, int]:
    return status.st_dev, status.st_ino, status.st_mode


def _open_optional_directory(
    parent_fd: int, name: str, stack: ExitStack
) -> tuple[int | None, tuple[int, int, int] | None]:
    try:
        fd = os.open(name, _dir_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return None, None
    stack.callback(os.close, fd)
    return fd, _directory_identity(os.fstat(fd))


def _json_names(directory_fd: int, remaining: int) -> tuple[tuple[str, ...], int]:
    names = []
    scanned = 0
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if scanned == remaining:
                raise LegacyImportError(
                    "legacy source set exceeds 5000 directory entries"
                )
            scanned += 1
            name = entry.name
            if not name.endswith(".json"):
                continue
            try:
                name.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise LegacyImportError("legacy source filename is not valid UTF-8") from exc
            if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
                raise LegacyImportError("legacy source filename is unsafe")
            names.append(name)
    return tuple(sorted(names)), scanned


def _revalidate_root(
    workspace_root: Path, root_fd: int, expected: tuple[int, int, int]
) -> None:
    try:
        path_identity = _directory_identity(
            os.stat(workspace_root, follow_symlinks=False)
        )
    except FileNotFoundError as exc:
        raise LegacyImportError("workspace root disappeared during collection") from exc
    if _directory_identity(os.fstat(root_fd)) != expected or path_identity != expected:
        raise LegacyImportError("workspace root identity changed during collection")


def _revalidate_component(
    parent_fd: int,
    name: str,
    child_fd: int | None,
    expected: tuple[int, int, int] | None,
) -> None:
    try:
        path_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if expected is None:
            return
        raise LegacyImportError(f"legacy source component disappeared: {name}")
    if expected is None or child_fd is None:
        raise LegacyImportError(f"legacy source component appeared during collection: {name}")
    if (
        _directory_identity(os.fstat(child_fd)) != expected
        or _directory_identity(path_status) != expected
    ):
        raise LegacyImportError(f"legacy source component identity changed: {name}")


def _read_stable(
    directory_fd: int, name: str
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    try:
        fd = os.open(name, _file_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise LegacyImportError(f"refusing unsafe legacy source: {name}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise LegacyImportError(f"refusing non-regular legacy source: {name}")
        if before.st_size > MAX_FILE_BYTES:
            raise LegacyImportError(f"legacy source exceeds 1048576 bytes: {name}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(65_536, MAX_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise LegacyImportError(f"legacy source exceeds 1048576 bytes: {name}")
        after = os.fstat(fd)
        if _identity(before) != _identity(after) or size != after.st_size:
            raise LegacyImportError(f"legacy source changed during read: {name}")
        path_status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(path_status) != _identity(after):
            raise LegacyImportError(f"legacy source path identity changed during read: {name}")
        return b"".join(chunks), _identity(after)
    except OSError as exc:
        raise LegacyImportError(f"legacy source read failed: {name}") from exc
    finally:
        os.close(fd)


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember(key)
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _claimed_project(
    raw: bytes, record_kind: str, registered: frozenset[str]
) -> str | None:
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_object_pairs,
            parse_constant=_invalid_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    if record_kind == "session":
        claim = payload.get("project_id")
    else:
        identity = payload.get("identity")
        claim = identity.get("project") if isinstance(identity, dict) else None
    return claim if isinstance(claim, str) and claim and claim in registered else None


def import_current_provenance(
    *,
    workspace_root: str | os.PathLike[str],
    store: LedgerStore,
    workspace_id: str,
    registry_revision: str,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Collect the closed legacy source set, then atomically append hash provenance."""
    root = Path(workspace_root)
    if not root.is_absolute():
        raise LegacyImportError("workspace_root must be absolute")
    registered = store.legacy_import_preflight(
        workspace_id=workspace_id, registry_revision=registry_revision
    )
    records: list[dict[str, object]] = []
    collected_identities: dict[
        tuple[tuple[str, ...], str], tuple[int, int, int, int, int, int]
    ] = {}
    try:
        with ExitStack() as stack:
            root_fd = os.open(root, _dir_flags())
            stack.callback(os.close, root_fd)
            root_identity = _directory_identity(os.fstat(root_fd))
            state_fd, state_identity = _open_optional_directory(root_fd, "State", stack)
            if state_fd is None:
                bridge_fd, bridge_identity = None, None
            else:
                bridge_fd, bridge_identity = _open_optional_directory(
                    state_fd, "session_autobridge", stack
                )

            snapshots = []
            remaining = MAX_FILES
            for source_family, record_kind, directory_name, parts in _SOURCES:
                if bridge_fd is None:
                    directory_fd, directory_identity = None, None
                    names = ()
                    scanned = 0
                else:
                    directory_fd, directory_identity = _open_optional_directory(
                        bridge_fd, directory_name, stack
                    )
                    if directory_fd is None:
                        names, scanned = (), 0
                    else:
                        names, scanned = _json_names(directory_fd, remaining)
                remaining -= scanned
                snapshots.append(
                    (
                        source_family,
                        record_kind,
                        directory_name,
                        parts,
                        directory_fd,
                        directory_identity,
                        names,
                        scanned,
                    )
                )

            observed_at = clock().astimezone(timezone.utc).isoformat()
            for (
                source_family,
                record_kind,
                _directory_name,
                parts,
                directory_fd,
                _directory_identity_value,
                names,
                _scanned,
            ) in snapshots:
                if directory_fd is None:
                    continue
                for name in names:
                    raw, file_identity = _read_stable(directory_fd, name)
                    project_id = _claimed_project(raw, record_kind, registered)
                    locator = "/".join((*parts, name))
                    records.append(
                        {
                            "source_family": source_family,
                            "record_kind": record_kind,
                            "source_locator": locator,
                            "content_sha256": hashlib.sha256(raw).hexdigest(),
                            "byte_size": len(raw),
                            "observed_at_utc": observed_at,
                            "scope_kind": (
                                "exact_project"
                                if project_id is not None
                                else "legacy_unscoped"
                            ),
                            "project_id": project_id,
                        }
                    )
                    collected_identities[(parts, name)] = file_identity

            _revalidate_root(root, root_fd, root_identity)
            _revalidate_component(root_fd, "State", state_fd, state_identity)
            if state_fd is not None:
                _revalidate_component(
                    state_fd,
                    "session_autobridge",
                    bridge_fd,
                    bridge_identity,
                )
            if bridge_fd is not None:
                for (
                    _source_family,
                    _record_kind,
                    directory_name,
                    parts,
                    directory_fd,
                    directory_identity,
                    names,
                    scanned,
                ) in snapshots:
                    _revalidate_component(
                        bridge_fd,
                        directory_name,
                        directory_fd,
                        directory_identity,
                    )
                    if directory_fd is None:
                        continue
                    current_names, current_scanned = _json_names(
                        directory_fd, scanned
                    )
                    if current_names != names or current_scanned != scanned:
                        raise LegacyImportError(
                            "legacy source set changed during collection"
                        )
                    for name in names:
                        current = _identity(
                            os.stat(
                                name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        )
                        if current != collected_identities[(parts, name)]:
                            raise LegacyImportError(
                                f"legacy source path identity changed before import: {name}"
                            )
    except OSError as exc:
        raise LegacyImportError(
            "legacy source is unsafe, changed, or became unreadable"
        ) from exc

    imported_at = clock().astimezone(timezone.utc).isoformat()
    return store.import_legacy_provenance(
        workspace_id=workspace_id,
        registry_revision=registry_revision,
        import_transaction_id=uuid.uuid4().hex,
        import_revision=IMPORT_REVISION,
        imported_at_utc=imported_at,
        records=records,
    )
