"""SQLite storage foundation for the inert observation ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .paths import LedgerPaths, validate_project_id, validate_registry_token, validate_workspace_id


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
SYNCHRONOUS_FULL = 2
MIGRATION_TOOL_VERSION = "llm-collab-ledger/1"
SAFE_SQLITE_BACKPORTS = {(3, 44, 6), (3, 50, 7)}
V1_TABLES = frozenset(
    {
        "schema_migrations",
        "workspace_registry_snapshots",
        "project_registry_snapshots",
        "observation_source_registry_snapshots",
        "daemon_instances",
    }
)


class SQLiteSafetyError(RuntimeError):
    pass


class WriterAlreadyOpenError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


def _linked_sqlite_version_info() -> Sequence[int]:
    return sqlite3.sqlite_version_info


def _validate_sqlite_version(raw: Sequence[int]) -> tuple[int, int, int]:
    if len(raw) < 3 or any(isinstance(item, bool) or not isinstance(item, int) for item in raw[:3]):
        raise SQLiteSafetyError("SQLite WAL safety version must contain three integers")
    version = tuple(raw[:3])
    if version not in SAFE_SQLITE_BACKPORTS and version < (3, 51, 3):
        rendered = ".".join(str(item) for item in version)
        raise SQLiteSafetyError(
            f"SQLite {rendered} is unsafe for WAL: the WAL-reset corruption safety fix "
            "requires exactly 3.44.6, 3.50.7, or 3.51.3 and newer"
        )
    return version


def require_safe_sqlite() -> tuple[int, int, int]:
    return _validate_sqlite_version(_linked_sqlite_version_info())


V1_SQL = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        migration_checksum TEXT NOT NULL CHECK (length(migration_checksum) > 0),
        applied_at_utc TEXT NOT NULL CHECK (length(applied_at_utc) > 0),
        tool_version TEXT NOT NULL CHECK (length(tool_version) > 0),
        backup_reference TEXT NOT NULL CHECK (length(backup_reference) > 0)
    ) STRICT
    """,
    """
    CREATE TABLE workspace_registry_snapshots (
        workspace_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        registry_source_sha256 TEXT NOT NULL,
        captured_at_utc TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, registry_revision),
        CHECK (registry_revision = 'sha256:' || registry_source_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE project_registry_snapshots (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, project_id, registry_revision),
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE observation_source_registry_snapshots (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, project_id, source_id, registry_revision),
        FOREIGN KEY (workspace_id, project_id, registry_revision)
            REFERENCES project_registry_snapshots (workspace_id, project_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE daemon_instances (
        workspace_id TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        stopped_at_utc TEXT,
        metadata_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, instance_id),
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
)
MIGRATIONS = ((1, V1_SQL),)
V1_MIGRATION_CHECKSUM = "sha256:ce236daff444f736e01f3666ed44baf1c3ba17e81215fedb638276aff76b01c7"
V1_SCHEMA_FINGERPRINT = "sha256:26a856329406e45d22a8fbecdbd769d9c632acae3652d8c72438d228de7cfca2"


def _migration_checksum(statements: Sequence[str]) -> str:
    encoded = "\x00".join(statements).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _v1_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in V1_SQL:
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _OwnedWriterLock:
    """Own one flock fd and release it exactly once, including on abandonment."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class _PinnedFile:
    """Own one no-follow descriptor and its immutable file identity."""

    def __init__(self, fd: int, identity: tuple[int, int]) -> None:
        self._fd = fd
        self.identity = identity

    def fchmod(self, mode: int) -> None:
        if self._fd is None:
            raise SQLiteSafetyError("pinned SQLite file is closed")
        os.fchmod(self._fd, mode)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _close_connection_and_pin(
    connection: sqlite3.Connection | None, pin: _PinnedFile | None
) -> None:
    try:
        if connection is not None:
            connection.close()
    finally:
        if pin is not None:
            pin.close()


def _stable_fd_record(fd: int, path_reader: Callable[[int], str]) -> tuple[int, int, int, str]:
    before = os.fstat(fd)
    reported_path = path_reader(fd)
    after = os.fstat(fd)
    before_identity = (before.st_dev, before.st_ino, before.st_mode)
    after_identity = (after.st_dev, after.st_ino, after.st_mode)
    if before_identity != after_identity or not reported_path:
        raise OSError("file descriptor changed during inspection")
    return before.st_dev, before.st_ino, before.st_mode, reported_path


def _linux_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    root = "/proc/self/fd"
    records = {}
    for name in os.listdir(root):
        try:
            fd = int(name)
            records[fd] = _stable_fd_record(fd, lambda value: os.readlink(f"{root}/{value}"))
        except (OSError, ValueError):
            continue
    return records


def _darwin_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    root = "/dev/fd"

    def get_path(fd: int) -> str:
        value = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        return value.split(b"\0", 1)[0].decode("utf-8")

    records = {}
    for name in os.listdir(root):
        try:
            fd = int(name)
            records[fd] = _stable_fd_record(fd, get_path)
        except (OSError, UnicodeError, ValueError):
            continue
    return records


def _connection_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    if sys.platform.startswith("linux"):
        return _linux_fd_snapshot()
    if sys.platform == "darwin":
        return _darwin_fd_snapshot()
    raise SQLiteSafetyError(f"SQLite connection identity proof is unsupported on {sys.platform}")


def _reported_path_matches(reported_path: str, database: Path) -> bool:
    if reported_path.endswith(" (deleted)"):
        reported_path = reported_path[: -len(" (deleted)")]
    return os.path.realpath(reported_path) == os.path.realpath(database)


class LedgerStore:
    """One thread-bound writer/checkpointer or query-only reader connection."""

    def __init__(
        self,
        paths: LedgerPaths,
        connection: sqlite3.Connection,
        database_pin: _PinnedFile,
        *,
        read_only: bool,
        writer_lock: _OwnedWriterLock | None,
        clock: Callable[[], datetime],
    ) -> None:
        self.paths = paths
        self._connection = connection
        self._database_pin = database_pin
        self._read_only = read_only
        self._writer_lock = writer_lock
        self._clock = clock
        self._thread_id = threading.get_ident()
        self._closed = False

    @classmethod
    def open_writer(
        cls,
        paths: LedgerPaths,
        *,
        clock: Callable[[], datetime] = _utc_now,
        migrations: Sequence[tuple[int, Sequence[str]]] = MIGRATIONS,
    ) -> "LedgerStore":
        require_safe_sqlite()
        paths.ensure_directories()
        cls._preflight_sqlite_files(paths.ledger)
        writer_lock = cls._acquire_writer_lock(paths)
        connection = None
        database_pin = None
        try:
            connection, database_pin = cls._open_verified_connection(
                paths.ledger,
                read_only=False,
                create=True,
                timeout=BUSY_TIMEOUT_MS / 1_000,
            )
            cls._validate_schema_or_empty(connection, paths)
            paths.assert_contained()
            cls._configure(connection, writer=True)
            store = cls(
                paths,
                connection,
                database_pin,
                read_only=False,
                writer_lock=writer_lock,
                clock=clock,
            )
            store._secure_sqlite_files(paths.ledger, main_pin=database_pin)
            store._migrate(migrations)
            store._validate_schema(connection, paths)
            store._secure_sqlite_files(paths.ledger, main_pin=database_pin)
            return store
        except BaseException:
            try:
                _close_connection_and_pin(connection, database_pin)
            finally:
                writer_lock.close()
            raise

    @classmethod
    def open_reader(
        cls,
        paths: LedgerPaths,
    ) -> "LedgerStore":
        require_safe_sqlite()
        paths.assert_contained()
        cls._preflight_sqlite_files(paths.ledger)
        if not paths.ledger.is_file():
            raise FileNotFoundError(paths.ledger)
        connection, database_pin = cls._open_verified_connection(
            paths.ledger,
            read_only=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
        )
        try:
            cls._validate_schema(connection, paths)
            cls._configure(connection, writer=False)
            return cls(
                paths,
                connection,
                database_pin,
                read_only=True,
                writer_lock=None,
                clock=_utc_now,
            )
        except BaseException:
            _close_connection_and_pin(connection, database_pin)
            raise

    @staticmethod
    def _pin_regular_file(
        path: Path,
        *,
        writable: bool,
        create: bool = False,
        exclusive: bool = False,
    ) -> _PinnedFile:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise SQLiteSafetyError("O_NOFOLLOW is required for SQLite file safety")
        flags = (os.O_RDWR if writable else os.O_RDONLY) | nofollow
        flags |= getattr(os, "O_CLOEXEC", 0)
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        directory_flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            try:
                fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
            except IsADirectoryError as exc:
                raise SQLiteSafetyError(f"refusing non-regular SQLite file: {path}") from exc
        finally:
            os.close(directory_fd)
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                raise SQLiteSafetyError(f"refusing non-regular SQLite file: {path}")
            return _PinnedFile(fd, (status.st_dev, status.st_ino))
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def _open_verified_connection(
        cls,
        path: Path,
        *,
        read_only: bool,
        create: bool = False,
        exclusive: bool = False,
        timeout: float = BUSY_TIMEOUT_MS / 1_000,
    ) -> tuple[sqlite3.Connection, _PinnedFile]:
        pin = cls._pin_regular_file(
            path,
            writable=not read_only,
            create=create,
            exclusive=exclusive,
        )
        connection = None
        try:
            before = _connection_fd_snapshot()
            connection = sqlite3.connect(
                path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                uri=True,
                timeout=timeout,
                isolation_level=None,
            )
            after = _connection_fd_snapshot()
            opened_regular_files = [
                record
                for fd, record in after.items()
                if fd not in before
                and stat.S_ISREG(record[2])
                and _reported_path_matches(record[3], path)
            ]
            if len(opened_regular_files) != 1:
                raise SQLiteSafetyError(
                    "SQLite main-database descriptor proof is unavailable or ambiguous"
                )
            actual = opened_regular_files[0]
            if (actual[0], actual[1]) != pin.identity:
                raise SQLiteSafetyError("SQLite opened a different file than the no-follow pin")
            if not read_only:
                pin.fchmod(0o600)
            return connection, pin
        except BaseException:
            _close_connection_and_pin(connection, pin)
            raise

    @staticmethod
    def _acquire_writer_lock(paths: LedgerPaths) -> _OwnedWriterLock:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(paths.lock, flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise WriterAlreadyOpenError(f"writer/checkpointer already owns {paths.ledger}") from exc
        return _OwnedWriterLock(lock_fd)

    @staticmethod
    def _configure(connection: sqlite3.Connection, *, writer: bool) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if writer:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise SQLiteSafetyError(f"SQLite refused required WAL mode: {mode}")
        elif connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise SQLiteSafetyError("ledger is not in required WAL mode")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA query_only = {'OFF' if writer else 'ON'}")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SQLiteSafetyError("SQLite refused required foreign_keys enforcement")
        if connection.execute("PRAGMA busy_timeout").fetchone()[0] != BUSY_TIMEOUT_MS:
            raise SQLiteSafetyError("SQLite refused the bounded busy timeout")
        if connection.execute("PRAGMA synchronous").fetchone()[0] != SYNCHRONOUS_FULL:
            raise SQLiteSafetyError("SQLite refused required synchronous FULL durability")
        if not writer and connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise SQLiteSafetyError("SQLite refused query-only reader mode")

    @classmethod
    def _validate_schema_or_empty(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Allow only a truly empty database to enter the migration path."""
        try:
            claimed = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = cls._table_names(connection)
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger is corrupt or unreadable") from exc
        if claimed == 0 and not tables:
            return
        cls._validate_schema(connection, paths)

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection, paths: LedgerPaths) -> None:
        """Fail closed on corrupt, unsupported, or incoherent claimed schema."""
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("ledger failed integrity_check")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise MigrationError("ledger failed foreign_key_check")
            claimed = connection.execute("PRAGMA user_version").fetchone()[0]
            if claimed != SCHEMA_VERSION:
                raise MigrationError(
                    f"unsupported ledger schema version {claimed}; expected {SCHEMA_VERSION}"
                )
            rows = connection.execute(
                "SELECT version, migration_checksum, applied_at_utc, tool_version, backup_reference "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
            if _migration_checksum(V1_SQL) != V1_MIGRATION_CHECKSUM:
                raise MigrationError("released v1 migration checksum is incoherent")
            if _v1_schema_fingerprint_from_sql() != V1_SCHEMA_FINGERPRINT:
                raise MigrationError("released v1 schema fingerprint is incoherent")
            if len(rows) != 1 or rows[0][0] != 1:
                raise MigrationError("ledger migration metadata is incoherent")
            _, checksum, applied_at, tool_version, backup_reference = rows[0]
            backup_match = (
                re.fullmatch(r"ledger-0-(\d{8}T\d{12}Z)\.sqlite3", backup_reference)
                if isinstance(backup_reference, str)
                else None
            )
            if (
                checksum != V1_MIGRATION_CHECKSUM
                or not isinstance(applied_at, str)
                or not applied_at
                or tool_version != MIGRATION_TOOL_VERSION
                or backup_match is None
            ):
                raise MigrationError("ledger migration metadata is incoherent")
            backup_time = datetime.strptime(
                backup_match.group(1), "%Y%m%dT%H%M%S%fZ"
            ).replace(tzinfo=timezone.utc)
            if applied_at != backup_time.isoformat():
                raise MigrationError("ledger migration metadata is incoherent")
            backup = paths.backups / backup_reference
            if backup.is_symlink() or not backup.is_file():
                raise MigrationError("ledger migration backup reference is incoherent")
            cls._verify_database(backup)
            actual_tables = cls._table_names(connection)
            if actual_tables != V1_TABLES:
                raise MigrationError(
                    "ledger v1 table set is incoherent: "
                    f"missing={sorted(V1_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V1_TABLES)}"
                )
            if _schema_fingerprint(connection) != V1_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v1 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    def _migrate(self, migrations: Sequence[tuple[int, Sequence[str]]]) -> None:
        current = self.schema_version()
        for version, statements in migrations:
            if version <= current:
                continue
            if version != current + 1:
                raise MigrationError(f"migration gap: expected {current + 1}, got {version}")
            backup = self._backup_before_migration(current)
            backup_stamp = re.fullmatch(
                r"ledger-\d+-(\d{8}T\d{12}Z)\.sqlite3", backup.name
            )
            if backup_stamp is None:
                raise MigrationError("generated migration backup identity is incoherent")
            applied_at = datetime.strptime(
                backup_stamp.group(1), "%Y%m%dT%H%M%S%fZ"
            ).replace(tzinfo=timezone.utc).isoformat()
            try:
                checksum = _migration_checksum(statements)
                if version != 1 or checksum != V1_MIGRATION_CHECKSUM:
                    raise MigrationError(f"migration {version} does not match its released checksum")
                self._connection.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations"
                    "(version, migration_checksum, applied_at_utc, tool_version, backup_reference) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (version, checksum, applied_at, MIGRATION_TOOL_VERSION, backup.name),
                )
                self._connection.execute(f"PRAGMA user_version = {version}")
                if self._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MigrationError(f"migration {version} failed integrity_check")
                if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise MigrationError(f"migration {version} failed foreign_key_check")
                self._connection.execute("COMMIT")
            except BaseException as exc:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                self._restore_from_backup(backup)
                raise MigrationError(f"migration {version} failed; the verified backup was restored") from exc
            current = version

    @staticmethod
    def _sqlite_files(database: Path) -> tuple[Path, Path, Path, Path]:
        return (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
            database.with_name(database.name + "-journal"),
        )

    @classmethod
    def _preflight_sqlite_files(cls, database: Path) -> None:
        for path in cls._sqlite_files(database):
            if path.is_symlink():
                raise SQLiteSafetyError(f"refusing symlinked SQLite artifact: {path}")

    @classmethod
    def _secure_sqlite_files(
        cls, database: Path, *, main_pin: _PinnedFile | None = None
    ) -> None:
        for path in cls._sqlite_files(database):
            try:
                pin = cls._pin_regular_file(path, writable=True)
            except FileNotFoundError:
                continue
            try:
                if path == database and main_pin is not None and pin.identity != main_pin.identity:
                    raise SQLiteSafetyError("SQLite database pathname no longer matches its pin")
                pin.fchmod(0o600)
            finally:
                pin.close()

    def _backup_before_migration(self, schema_version: int) -> Path:
        if self._connection.in_transaction:
            raise MigrationError("refusing backup while a database transaction is active")
        stamp = self._clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.paths.backup_path(schema_version, stamp)
        self._preflight_sqlite_files(backup)
        destination, destination_pin = self._open_verified_connection(
            backup,
            read_only=False,
            create=True,
            exclusive=True,
        )
        try:
            self._connection.backup(destination)
            destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination.execute("PRAGMA journal_mode = DELETE")
        finally:
            _close_connection_and_pin(destination, destination_pin)
        self._secure_sqlite_files(backup)
        self._verify_database(backup)
        return backup

    def _restore_from_backup(self, backup: Path) -> None:
        if self._connection.in_transaction:
            raise MigrationError("refusing restore while a database transaction is active")
        source, source_pin = self._open_verified_connection(backup, read_only=True)
        try:
            source.backup(self._connection)
        finally:
            _close_connection_and_pin(source, source_pin)
        self._database_pin.fchmod(0o600)
        self._secure_sqlite_files(self.paths.ledger, main_pin=self._database_pin)
        if self.integrity_check() != "ok":
            raise MigrationError("restored ledger failed integrity_check")

    @classmethod
    def _verify_database(cls, path: Path) -> None:
        connection, pin = cls._open_verified_connection(path, read_only=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_failure = connection.execute("PRAGMA foreign_key_check").fetchone()
        finally:
            _close_connection_and_pin(connection, pin)
        if result != "ok":
            raise MigrationError(f"backup failed integrity_check: {result}")
        if foreign_key_failure is not None:
            raise MigrationError(f"backup failed foreign_key_check: {foreign_key_failure}")

    def _ensure_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise sqlite3.ProgrammingError("ledger connections may not be reused across threads")
        if self._closed:
            raise sqlite3.ProgrammingError("ledger connection is closed")

    def schema_version(self) -> int:
        self._ensure_thread()
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def integrity_check(self) -> str:
        self._ensure_thread()
        return self._connection.execute("PRAGMA integrity_check").fetchone()[0]

    def record_registry_snapshot(
        self,
        *,
        workspace_id: str,
        registry_revision: str,
        registry_source_sha256: str,
        captured_at_utc: str,
        workspace_snapshot_json: str,
        project_snapshots: Mapping[str, str],
        source_snapshots: Mapping[str, Mapping[str, str]],
    ) -> None:
        """Persist one immutable, fully scoped registry snapshot."""
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot write registry snapshots")
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", registry_revision):
            raise ValueError("registry_revision must be sha256:<lowercase hex>")
        if registry_revision != f"sha256:{registry_source_sha256}":
            raise ValueError("registry revision and exact-source hash differ")
        if not isinstance(captured_at_utc, str) or not captured_at_utc:
            raise ValueError("captured_at_utc is required")
        workspace_snapshot = self._require_json_object(
            workspace_snapshot_json, "workspace snapshot"
        )
        if workspace_snapshot.get("workspace_id") != workspace_id:
            raise ValueError("workspace snapshot workspace_id does not match this ledger")
        project_entries = workspace_snapshot.get("projects")
        if not isinstance(project_entries, list) or not project_entries:
            raise ValueError("workspace snapshot projects must be a non-empty list")
        workspace_project_ids = []
        for entry in project_entries:
            workspace_project_ids.append(
                self._registry_project_identity(
                    entry, "workspace snapshot project", allow_string=True
                )
            )
        if len(workspace_project_ids) != len(set(workspace_project_ids)):
            raise ValueError("workspace snapshot projects must be duplicate-free")
        if set(workspace_project_ids) != set(project_snapshots):
            raise ValueError("workspace snapshot projects do not match project snapshots")
        projects = []
        for project_id, snapshot in project_snapshots.items():
            validate_project_id(project_id)
            project_snapshot = self._require_json_object(
                snapshot, f"project {project_id} snapshot"
            )
            if self._registry_project_identity(
                project_snapshot, f"project {project_id} snapshot", allow_string=False
            ) != project_id:
                raise ValueError(f"project {project_id} snapshot identity does not match its key")
            projects.append((workspace_id, project_id, registry_revision, snapshot))
        sources = []
        for project_id, project_sources in source_snapshots.items():
            validate_project_id(project_id)
            if project_id not in project_snapshots:
                raise ValueError(f"source snapshot project {project_id!r} is absent from this revision")
            for source_id, snapshot in project_sources.items():
                validate_registry_token(source_id, "source_id")
                self._require_json_object(snapshot, f"source {source_id} snapshot")
                sources.append((workspace_id, project_id, source_id, registry_revision, snapshot))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "INSERT INTO workspace_registry_snapshots "
                "(workspace_id, registry_revision, registry_source_sha256, captured_at_utc, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    registry_revision,
                    registry_source_sha256,
                    captured_at_utc,
                    workspace_snapshot_json,
                ),
            )
            self._connection.executemany(
                "INSERT INTO project_registry_snapshots "
                "(workspace_id, project_id, registry_revision, snapshot_json) VALUES (?, ?, ?, ?)",
                projects,
            )
            self._connection.executemany(
                "INSERT INTO observation_source_registry_snapshots "
                "(workspace_id, project_id, source_id, registry_revision, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?)",
                sources,
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _require_json_object(serialized: str, name: str) -> dict[str, object]:
        try:
            parsed = json.loads(serialized)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{name} must be serialized JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{name} must serialize one object")
        return parsed

    @staticmethod
    def _registry_project_identity(
        value: object, name: str, *, allow_string: bool
    ) -> str:
        if allow_string and isinstance(value, str):
            return validate_project_id(value)
        if not isinstance(value, dict):
            raise ValueError(f"{name} must carry an exact project identity")
        aliases = [value[key] for key in ("project_id", "id") if key in value]
        if not aliases or any(alias != aliases[0] for alias in aliases[1:]):
            raise ValueError(f"{name} has missing or conflicting project identity aliases")
        return validate_project_id(aliases[0])

    def get_project_snapshot(
        self,
        *,
        workspace_id: str,
        project_id: str,
        registry_revision: str,
    ) -> dict[str, str] | None:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        row = self._connection.execute(
            "SELECT workspace_id, project_id, registry_revision, snapshot_json "
            "FROM project_registry_snapshots "
            "WHERE workspace_id = ? AND project_id = ? AND registry_revision = ?",
            (workspace_id, project_id, registry_revision),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(("workspace_id", "project_id", "registry_revision", "snapshot_json"), row))

    def get_source_snapshots(
        self,
        *,
        workspace_id: str,
        project_id: str,
        registry_revision: str,
    ) -> list[dict[str, str]]:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        rows = self._connection.execute(
            "SELECT workspace_id, project_id, source_id, registry_revision, snapshot_json "
            "FROM observation_source_registry_snapshots "
            "WHERE workspace_id = ? AND project_id = ? AND registry_revision = ? ORDER BY source_id",
            (workspace_id, project_id, registry_revision),
        ).fetchall()
        keys = ("workspace_id", "project_id", "source_id", "registry_revision", "snapshot_json")
        return [dict(zip(keys, row)) for row in rows]

    def checkpoint(self) -> tuple[int, int, int]:
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("only the writer connection may checkpoint")
        result = tuple(self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        self._secure_sqlite_files(self.paths.ledger, main_pin=self._database_pin)
        return result

    def close(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise sqlite3.ProgrammingError("ledger connections may not be reused across threads")
        if self._closed:
            return
        try:
            _close_connection_and_pin(self._connection, self._database_pin)
        finally:
            try:
                if self._writer_lock is not None:
                    self._writer_lock.close()
            finally:
                self._writer_lock = None
                self._closed = True

    def __enter__(self) -> "LedgerStore":
        self._ensure_thread()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
