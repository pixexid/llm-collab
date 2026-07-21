"""Non-overridable paths for one workspace ledger."""

from __future__ import annotations

import base64
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path


NAMESPACE = "llm-collabd"
MAX_UNIX_SOCKET_PATH_BYTES = 103
_WORKSPACE_ID = re.compile(r"ws_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_PROJECT_ID = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}\Z")
_REGISTRY_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}\Z")
_RESERVED_PROJECT_IDS = frozenset(
    {
        NAMESPACE,
        "ledger.sqlite3",
        "backups",
        "daemon.sock",
        "daemon.lock",
        "logs",
        "llm-collabd.jsonl",
    }
)


def generate_workspace_id() -> str:
    """Return a WorkspaceV1-compatible, opaque identity."""
    encoded = base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode("ascii")
    return f"ws_w{encoded}"


def validate_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str) or _WORKSPACE_ID.fullmatch(workspace_id) is None:
        raise ValueError("workspace_id must be a WorkspaceV1 ws_-prefixed identifier")
    return workspace_id


def validate_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("project_id must be an exact registered-project token")
    if project_id.casefold() in _RESERVED_PROJECT_IDS:
        raise ValueError(f"project_id {project_id!r} collides with a reserved ledger artifact")
    return project_id


def validate_registry_token(value: str, name: str) -> str:
    if not isinstance(value, str) or _REGISTRY_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact registry token")
    return value


def _require_beneath(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"ledger path escapes project_state_root: {path}") from exc


@dataclass(frozen=True)
class LedgerPaths:
    """All artifacts derived from one trusted state root and workspace identity."""

    state_root: Path
    workspace_id: str
    workspace_root: Path
    ledger: Path
    backups: Path
    socket: Path
    lock: Path
    logs: Path
    log: Path

    def __post_init__(self) -> None:
        validate_workspace_id(self.workspace_id)
        canonical_root = self.state_root.expanduser().resolve(strict=False)
        if self.state_root != canonical_root:
            raise ValueError("state_root must be canonical")
        expected_root = canonical_root / NAMESPACE / self.workspace_id
        expected = {
            "workspace_root": expected_root,
            "ledger": expected_root / "ledger.sqlite3",
            "backups": expected_root / "backups",
            "socket": expected_root / "daemon.sock",
            "lock": expected_root / "daemon.lock",
            "logs": expected_root / "logs",
            "log": expected_root / "logs" / "llm-collabd.jsonl",
        }
        for field, path in expected.items():
            if getattr(self, field) != path:
                raise ValueError(f"caller may not override ledger artifact path {field}")
        socket_bytes = len(os.fsencode(self.socket))
        if socket_bytes > MAX_UNIX_SOCKET_PATH_BYTES:
            raise ValueError(
                f"AF_UNIX socket path is {socket_bytes} encoded bytes; portable limit is "
                f"{MAX_UNIX_SOCKET_PATH_BYTES}. Shorten project_state_root: {self.socket}"
            )
        self.assert_contained()

    @classmethod
    def derive(cls, project_state_root: str | os.PathLike[str], workspace_id: str) -> "LedgerPaths":
        validate_workspace_id(workspace_id)
        root = Path(project_state_root).expanduser().resolve(strict=False)
        workspace_root = root / NAMESPACE / workspace_id
        result = cls(
            state_root=root,
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            ledger=workspace_root / "ledger.sqlite3",
            backups=workspace_root / "backups",
            socket=workspace_root / "daemon.sock",
            lock=workspace_root / "daemon.lock",
            logs=workspace_root / "logs",
            log=workspace_root / "logs" / "llm-collabd.jsonl",
        )
        result.assert_contained()
        return result

    def assert_contained(self) -> None:
        for path in (
            self.workspace_root,
            self.ledger,
            self.backups,
            self.socket,
            self.lock,
            self.logs,
            self.log,
        ):
            _require_beneath(path, self.state_root)

    def ensure_directories(self) -> None:
        """Create fixed children through directory fds without following symlinks."""
        self.assert_contained()
        self.state_root.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        root_fd = os.open(self.state_root, flags)
        namespace_fd = workspace_fd = backup_fd = logs_fd = None
        try:
            namespace_fd = self._ensure_child_directory(
                root_fd, NAMESPACE, flags=flags
            )
            workspace_fd = self._ensure_child_directory(
                namespace_fd, self.workspace_id, flags=flags
            )
            backup_fd = self._ensure_child_directory(workspace_fd, "backups", flags=flags)
            logs_fd = self._ensure_child_directory(workspace_fd, "logs", flags=flags)
            self._revalidate_child(workspace_fd, "backups", backup_fd)
            self._revalidate_child(workspace_fd, "logs", logs_fd)
            self._revalidate_child(namespace_fd, self.workspace_id, workspace_fd)
            self._revalidate_child(root_fd, NAMESPACE, namespace_fd)
        finally:
            if logs_fd is not None:
                os.close(logs_fd)
            if backup_fd is not None:
                os.close(backup_fd)
            if workspace_fd is not None:
                os.close(workspace_fd)
            if namespace_fd is not None:
                os.close(namespace_fd)
            os.close(root_fd)

    @staticmethod
    def _ensure_child_directory(
        parent_fd: int,
        name: str,
        *,
        flags: int,
    ) -> int:
        """Create/open one literal child and return an owned verified directory fd."""
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise ValueError("ledger child directory name must be one literal path component")
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        try:
            child_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"ledger child is not a no-follow directory: {name}") from exc
        try:
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                raise ValueError(f"ledger child is not a directory: {name}")
            LedgerPaths._revalidate_child(parent_fd, name, child_fd)
            os.fchmod(child_fd, 0o700)
            return child_fd
        except BaseException:
            os.close(child_fd)
            raise

    @staticmethod
    def _revalidate_child(parent_fd: int, name: str, child_fd: int) -> None:
        """Require the held fd to remain the exact no-follow directory at its edge."""
        opened = os.fstat(child_fd)
        try:
            edge = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"ledger child changed during directory walk: {name}") from exc
        if (
            not stat.S_ISDIR(edge.st_mode)
            or edge.st_dev != opened.st_dev
            or edge.st_ino != opened.st_ino
        ):
            raise ValueError(f"ledger child changed during directory walk: {name}")

    def backup_path(self, schema_version: int, utc_stamp: str) -> Path:
        if schema_version < 0 or not re.fullmatch(r"\d{8}T\d{6}\d{6}Z", utc_stamp):
            raise ValueError("invalid backup identity")
        path = self.backups / f"ledger-{schema_version}-{utc_stamp}.sqlite3"
        _require_beneath(path, self.state_root)
        return path
