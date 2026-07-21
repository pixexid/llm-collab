from __future__ import annotations

import gc
import inspect
import json
import os
import re
import sqlite3
import stat
import threading
import unittest
import warnings
import weakref
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import llm_collab.ledger.store as store_module
from llm_collab.ledger import LedgerPaths, LedgerStore, SQLiteSafetyError, WriterAlreadyOpenError
from llm_collab.ledger.store import (
    BUSY_TIMEOUT_MS,
    MIGRATION_TOOL_VERSION,
    SYNCHRONOUS_FULL,
    V1_MIGRATION_CHECKSUM,
    V1_SCHEMA_FINGERPRINT,
    V2_MIGRATION_CHECKSUM,
    V2_SCHEMA_FINGERPRINT,
    V3_MIGRATION_CHECKSUM,
    V3_SCHEMA_FINGERPRINT,
    MigrationError,
    V1_SQL,
    V2_SQL,
    V3_SQL,
    _close_connection_and_pin,
    _connection_fd_snapshot,
    _darwin_fd_snapshot,
    _linux_fd_snapshot,
    _migration_checksum,
    _validate_sqlite_version,
    _v1_schema_fingerprint_from_sql,
    _v2_schema_fingerprint_from_sql,
    _v3_schema_fingerprint_from_sql,
    require_safe_sqlite,
)


SAFE_VERSION = (3, 51, 3)
FIXED_TIME = datetime(2026, 7, 21, 8, 5, 6, 123456, tzinfo=timezone.utc)
AMIGA = "amiga"
NUVYR = "nuvyr"
REVISION_HASH = "a" * 64
REVISION = f"sha256:{REVISION_HASH}"


def create_released_v1(paths: LedgerPaths, *, now: datetime = FIXED_TIME) -> Path:
    """Materialize the exact released v1 bytes for migration-only tests."""
    paths.ensure_directories()
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = paths.backup_path(0, stamp)
    with closing(sqlite3.connect(backup)) as connection:
        self_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if self_check != "ok":
            raise AssertionError(self_check)
    backup.chmod(0o600)
    with closing(sqlite3.connect(paths.ledger, isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in V1_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, migration_checksum, applied_at_utc, tool_version, backup_reference) "
            "VALUES (1, ?, ?, ?, ?)",
            (V1_MIGRATION_CHECKSUM, now.isoformat(), MIGRATION_TOOL_VERSION, backup.name),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.execute("COMMIT")
    paths.ledger.chmod(0o600)
    return backup


def create_released_v2(paths: LedgerPaths, *, now: datetime = FIXED_TIME) -> Path:
    """Materialize the exact released v2 bytes for migration-only tests."""
    create_released_v1(paths, now=now)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = paths.backup_path(1, stamp)
    with closing(sqlite3.connect(paths.ledger)) as source, closing(
        sqlite3.connect(backup)
    ) as destination:
        source.backup(destination)
    backup.chmod(0o600)
    with closing(sqlite3.connect(paths.ledger, isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in V2_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, migration_checksum, applied_at_utc, tool_version, backup_reference) "
            "VALUES (2, ?, ?, ?, ?)",
            (V2_MIGRATION_CHECKSUM, now.isoformat(), MIGRATION_TOOL_VERSION, backup.name),
        )
        connection.execute("PRAGMA user_version = 2")
        connection.execute("COMMIT")
    return backup


def record_test_registry(store: LedgerStore) -> None:
    store.record_registry_snapshot(
        workspace_id="ws_alpha",
        registry_revision=REVISION,
        registry_source_sha256=REVISION_HASH,
        captured_at_utc=FIXED_TIME.isoformat(),
        workspace_snapshot_json=json.dumps(
            {"workspace_id": "ws_alpha", "projects": [AMIGA, NUVYR]}
        ),
        project_snapshots={
            AMIGA: json.dumps({"project_id": AMIGA}),
            NUVYR: json.dumps({"project_id": NUVYR}),
        },
        source_snapshots={AMIGA: {}, NUVYR: {}},
    )


def provenance_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "source_family": "session_autobridge",
        "record_kind": "session",
        "source_locator": "State/session_autobridge/sessions/session.json",
        "content_sha256": "b" * 64,
        "byte_size": 2,
        "observed_at_utc": FIXED_TIME.isoformat(),
        "scope_kind": "exact_project",
        "project_id": AMIGA,
    }
    record.update(changes)
    return record


class LedgerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        linked_version = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        linked_version.start()
        self.addCleanup(linked_version.stop)

    def test_busy_timeout_policy_is_literal_and_configure_overrides_zero(self) -> None:
        self.assertEqual(BUSY_TIMEOUT_MS, 5_000)
        with TemporaryDirectory(dir="/tmp") as tmp:
            connection = sqlite3.connect(Path(tmp) / "timeout.sqlite3", timeout=0, isolation_level=None)
            try:
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 0)
                LedgerStore._configure(connection, writer=True)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5_000)
            finally:
                connection.close()

    def test_synchronous_full_policy_is_literal_and_each_connection_overrides_off(self) -> None:
        self.assertEqual(SYNCHRONOUS_FULL, 2)
        with TemporaryDirectory(dir="/tmp") as tmp:
            database = Path(tmp) / "sync.sqlite3"
            connection = sqlite3.connect(database, isolation_level=None)
            try:
                connection.execute("PRAGMA synchronous = OFF")
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 0)
                LedgerStore._configure(connection, writer=True)
                self.assertEqual(
                    connection.execute("PRAGMA synchronous").fetchone()[0], SYNCHRONOUS_FULL
                )
                connection.execute("PRAGMA synchronous = OFF")
                LedgerStore._configure(connection, writer=False)
                self.assertEqual(
                    connection.execute("PRAGMA synchronous").fetchone()[0], SYNCHRONOUS_FULL
                )
            finally:
                connection.close()

        class RefusedSynchronousFull:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, sql: str, *args):
                if sql == "PRAGMA synchronous = FULL":
                    return self.connection.execute("SELECT 1")
                return self.connection.execute(sql, *args)

        with TemporaryDirectory(dir="/tmp") as tmp:
            connection = sqlite3.connect(Path(tmp) / "refused.sqlite3", isolation_level=None)
            try:
                connection.execute("PRAGMA synchronous = NORMAL")
                with self.assertRaisesRegex(SQLiteSafetyError, "synchronous FULL"):
                    LedgerStore._configure(RefusedSynchronousFull(connection), writer=True)
            finally:
                connection.close()

        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(Path(tmp) / "state", "ws_alpha")
            with LedgerStore.open_writer(paths):
                pass
            with LedgerStore.open_reader(paths) as reader:
                self.assertEqual(
                    reader._connection.execute("PRAGMA synchronous").fetchone()[0],
                    SYNCHRONOUS_FULL,
                )

    def test_sqlite_wal_safety_gate_is_exact_and_fails_closed(self) -> None:
        for accepted in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 52, 0), (4, 0, 0)):
            with self.subTest(accepted=accepted):
                self.assertEqual(_validate_sqlite_version(accepted), accepted)
        for rejected in ((3, 44, 5), (3, 50, 6), (3, 51, 1), (3, 51, 2), (3, 43, 99)):
            with self.subTest(rejected=rejected):
                with self.assertRaisesRegex(SQLiteSafetyError, "unsafe for WAL.*safety fix"):
                    _validate_sqlite_version(rejected)
        with self.assertRaises(SQLiteSafetyError):
            _validate_sqlite_version((3, 51, True))
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=(3, 51, 1)):
            with self.assertRaisesRegex(SQLiteSafetyError, "unsafe for WAL"):
                require_safe_sqlite()
        self.assertNotIn("sqlite_version_info", inspect.signature(require_safe_sqlite).parameters)
        self.assertNotIn("sqlite_version_info", inspect.signature(LedgerStore.open_writer).parameters)
        self.assertNotIn("sqlite_version_info", inspect.signature(LedgerStore.open_reader).parameters)
        with TemporaryDirectory(dir="/tmp") as tmp, patch.object(
            store_module, "_linked_sqlite_version_info", return_value=(3, 51, 1)
        ):
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with self.assertRaisesRegex(SQLiteSafetyError, "unsafe for WAL"):
                LedgerStore.open_writer(paths)
            self.assertFalse(paths.workspace_root.exists())

    def test_linux_and_darwin_fd_enumeration_is_stable_and_fail_closed(self) -> None:
        regular = Mock(st_dev=11, st_ino=22, st_mode=stat.S_IFREG | 0o600)
        with (
            patch.object(store_module.os, "listdir", return_value=["7", "not-an-fd"]),
            patch.object(store_module.os, "readlink", return_value="/tmp/ledger.sqlite3"),
            patch.object(store_module.os, "fstat", return_value=regular),
        ):
            self.assertEqual(
                _linux_fd_snapshot(),
                {7: (11, 22, stat.S_IFREG | 0o600, "/tmp/ledger.sqlite3")},
            )

        encoded_path = b"/tmp/ledger.sqlite3\0" + b"\0" * 1000
        with (
            patch.object(store_module.os, "listdir", return_value=["8"]),
            patch.object(store_module.fcntl, "fcntl", return_value=encoded_path),
            patch.object(store_module.fcntl, "F_GETPATH", 50, create=True),
            patch.object(store_module.os, "fstat", return_value=regular),
        ):
            self.assertEqual(
                _darwin_fd_snapshot(),
                {8: (11, 22, stat.S_IFREG | 0o600, "/tmp/ledger.sqlite3")},
            )

        with patch.object(store_module, "fcntl", Mock(spec=["fcntl"])):
            with self.assertRaisesRegex(SQLiteSafetyError, "unsupported.*F_GETPATH"):
                _darwin_fd_snapshot()

        with patch.object(store_module.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(SQLiteSafetyError, "unsupported"):
                _connection_fd_snapshot()

    def test_verified_open_rejects_mismatch_ambiguity_and_unavailable_proof_with_cleanup(self) -> None:
        cases = {
            "mismatch": ({}, {91: (1, 2, stat.S_IFREG | 0o600, "/tmp/db")}),
            "ambiguous": (
                {},
                {
                    91: (1, 2, stat.S_IFREG | 0o600, "/tmp/db"),
                    92: (1, 2, stat.S_IFREG | 0o600, "/tmp/db"),
                },
            ),
            "unavailable": OSError("fd surface unavailable"),
        }
        for kind, snapshots in cases.items():
            with self.subTest(kind=kind), TemporaryDirectory(dir="/tmp") as tmp:
                database = Path(tmp) / "db"
                database.touch()
                pinned = []
                fake_connection = Mock()
                original_pin = LedgerStore._pin_regular_file

                def capture_pin(*args, **kwargs):
                    pin = original_pin(*args, **kwargs)
                    pinned.append(pin)
                    return pin

                if isinstance(snapshots, BaseException):
                    snapshot_patch = patch.object(
                        store_module, "_connection_fd_snapshot", side_effect=snapshots
                    )
                else:
                    before, after = snapshots
                    if kind == "mismatch":
                        status = database.stat()
                        after[91] = (
                            status.st_dev,
                            status.st_ino + 1,
                            stat.S_IFREG | 0o600,
                            str(database),
                        )
                    else:
                        identity = (database.stat().st_dev, database.stat().st_ino)
                        after = {
                            fd: (identity[0], identity[1], value[2], str(database))
                            for fd, value in after.items()
                        }
                    snapshot_patch = patch.object(
                        store_module, "_connection_fd_snapshot", side_effect=[before, after]
                    )

                with (
                    patch.object(LedgerStore, "_pin_regular_file", side_effect=capture_pin),
                    patch.object(store_module.sqlite3, "connect", return_value=fake_connection),
                    snapshot_patch,
                ):
                    with self.assertRaises((OSError, SQLiteSafetyError)):
                        LedgerStore._open_verified_connection(database, read_only=True)
                self.assertIsNone(pinned[0]._fd)
                if kind == "unavailable":
                    fake_connection.close.assert_not_called()
                else:
                    fake_connection.close.assert_called_once_with()

    def test_identity_mismatch_does_not_chmod_the_pinned_file(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            database = Path(tmp) / "db"
            database.touch(mode=0o644)
            status = database.stat()
            mismatch = (
                status.st_dev,
                status.st_ino + 1,
                stat.S_IFREG | 0o644,
                str(database),
            )
            fake_connection = Mock()
            with (
                patch.object(store_module, "_connection_fd_snapshot", side_effect=[{}, {91: mismatch}]),
                patch.object(store_module.sqlite3, "connect", return_value=fake_connection),
            ):
                with self.assertRaisesRegex(SQLiteSafetyError, "different file"):
                    LedgerStore._open_verified_connection(database, read_only=False)
            self.assertEqual(database.stat().st_mode & 0o777, 0o644)
            fake_connection.close.assert_called_once_with()

    def test_secure_main_compares_identity_before_chmod(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            database = Path(tmp) / "db"
            database.touch(mode=0o600)
            main_pin = LedgerStore._pin_regular_file(database, writable=True)
            parked = database.with_name("db.pinned")
            database.rename(parked)
            database.touch(mode=0o644)
            try:
                with self.assertRaisesRegex(SQLiteSafetyError, "no longer matches"):
                    LedgerStore._secure_sqlite_files(database, main_pin=main_pin)
                self.assertEqual(database.stat().st_mode & 0o777, 0o644)
            finally:
                main_pin.close()

    def test_close_helper_closes_pin_when_connection_close_raises(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            database = Path(tmp) / "db"
            database.touch()
            pin = LedgerStore._pin_regular_file(database, writable=False)
            connection = Mock()
            connection.close.side_effect = RuntimeError("close failed")
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                _close_connection_and_pin(connection, pin)
            connection.close.assert_called_once_with()
            self.assertIsNone(pin._fd)

    def test_open_writer_close_error_still_closes_pin_and_releases_lock(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            paths.ensure_directories()
            paths.ledger.touch()
            pin = LedgerStore._pin_regular_file(paths.ledger, writable=True)
            connection = Mock()
            connection.close.side_effect = RuntimeError("close failed")
            acquired_locks = []
            original_acquire = LedgerStore._acquire_writer_lock

            def capture_lock(target_paths):
                writer_lock = original_acquire(target_paths)
                acquired_locks.append(writer_lock)
                return writer_lock

            with (
                patch.object(
                    LedgerStore,
                    "_acquire_writer_lock",
                    side_effect=capture_lock,
                ),
                patch.object(
                    LedgerStore,
                    "_open_verified_connection",
                    return_value=(connection, pin),
                ),
                patch.object(
                    LedgerStore,
                    "_validate_schema_or_empty",
                    side_effect=MigrationError("validation failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    LedgerStore.open_writer(paths)
            connection.close.assert_called_once_with()
            self.assertIsNone(pin._fd)
            self.assertEqual(len(acquired_locks), 1)
            self.assertIsNone(acquired_locks[0]._fd)
            writer_lock = LedgerStore._acquire_writer_lock(paths)
            writer_lock.close()

    def test_open_reader_close_error_still_closes_pin(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            paths.ensure_directories()
            paths.ledger.touch()
            pin = LedgerStore._pin_regular_file(paths.ledger, writable=False)
            connection = Mock()
            connection.close.side_effect = RuntimeError("close failed")
            with (
                patch.object(
                    LedgerStore,
                    "_open_verified_connection",
                    return_value=(connection, pin),
                ),
                patch.object(
                    LedgerStore,
                    "_validate_schema",
                    side_effect=MigrationError("validation failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "close failed"):
                    LedgerStore.open_reader(paths)
            connection.close.assert_called_once_with()
            self.assertIsNone(pin._fd)

    def test_verified_open_proves_identity_before_any_connection_operation(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            database = Path(tmp) / "db"
            database.touch()
            identity = (database.stat().st_dev, database.stat().st_ino)
            record = (identity[0], identity[1], stat.S_IFREG | 0o600, str(database))
            fake_connection = Mock()
            with (
                patch.object(store_module, "_connection_fd_snapshot", side_effect=[{}, {91: record}]),
                patch.object(store_module.sqlite3, "connect", return_value=fake_connection),
            ):
                connection, pin = LedgerStore._open_verified_connection(database, read_only=True)
            self.assertIs(connection, fake_connection)
            fake_connection.execute.assert_not_called()
            fake_connection.close()
            pin.close()

    def test_writer_releases_lock_when_fd_proof_is_unavailable(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with patch.object(
                store_module,
                "_connection_fd_snapshot",
                side_effect=OSError("fd surface unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "fd surface unavailable"):
                    LedgerStore.open_writer(paths)
            with LedgerStore.open_writer(paths) as writer:
                self.assertEqual(writer.schema_version(), 3)

    def test_all_file_backed_connects_use_one_verified_noncreating_open(self) -> None:
        source = inspect.getsource(store_module)
        self.assertEqual(source.count("sqlite3.connect("), 4)
        self.assertEqual(source.count("_close_connection_and_pin("), 8)
        self.assertIn('path.as_uri() + ("?mode=ro" if read_only else "?mode=rw")', source)
        self.assertNotIn(".resolve().as_uri()", source)
        self.assertNotIn(".chmod(", source)
        self.assertNotIn("fchmod", inspect.getsource(LedgerStore._pin_regular_file))

    def test_no_follow_pin_refuses_symlink_and_nonregular_final_components(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.write_bytes(b"operator-owned")
            symlink = root / "symlink"
            symlink.symlink_to(outside)
            with self.assertRaises(OSError):
                LedgerStore._pin_regular_file(symlink, writable=True)
            directory = root / "directory"
            directory.mkdir()
            with self.assertRaisesRegex(SQLiteSafetyError, "non-regular"):
                LedgerStore._pin_regular_file(directory, writable=True)
            self.assertEqual(outside.read_bytes(), b"operator-owned")

    def test_v3_schema_connection_guards_backups_and_private_permissions(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            state = Path(tmp) / "existing-state"
            state.mkdir(mode=0o755)
            paths = LedgerPaths.derive(state, "ws_alpha")
            with LedgerStore.open_writer(
                paths,
                clock=lambda: FIXED_TIME,
            ) as store:
                connection = store._connection
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 0)
                self.assertEqual(store.schema_version(), 3)
                self.assertEqual(store.integrity_check(), "ok")
                for suffix in ("-wal", "-shm"):
                    sidecar = paths.ledger.with_name(paths.ledger.name + suffix)
                    self.assertTrue(sidecar.exists())
                    self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

                expected = {
                    "schema_migrations",
                    "workspace_registry_snapshots",
                    "project_registry_snapshots",
                    "observation_source_registry_snapshots",
                    "daemon_instances",
                    "observations",
                    "observation_checkpoints",
                    "observation_audit",
                    "legacy_provenance_imports",
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(tables, expected)
                forbidden = re.compile(
                    r"message|delivery|attempt|receipt|lease|fence|quarantine|retry|dead_letter"
                )
                for table in tables:
                    self.assertIsNone(forbidden.search(table))
                    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                    self.assertFalse([column for column in columns if forbidden.search(column)])

            self.assertEqual(state.stat().st_mode & 0o777, 0o755)
            for directory in (paths.workspace_root, paths.backups, paths.logs):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for file_path in (paths.ledger, paths.lock):
                self.assertEqual(file_path.stat().st_mode & 0o777, 0o600)
            backups = sorted(paths.backups.iterdir())
            self.assertEqual(
                [path.name for path in backups],
                [
                    "ledger-0-20260721T080506123456Z.sqlite3",
                    "ledger-1-20260721T080506123456Z.sqlite3",
                    "ledger-2-20260721T080506123456Z.sqlite3",
                ],
            )
            for version, backup in enumerate(backups):
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                with closing(sqlite3.connect(backup)) as connection, connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], version)
            with closing(sqlite3.connect(paths.ledger)) as ledger, ledger:
                self.assertEqual(
                    ledger.execute(
                        "SELECT migration_checksum, applied_at_utc, tool_version, backup_reference "
                        "FROM schema_migrations ORDER BY version"
                    ).fetchall(),
                    [
                        (
                            V1_MIGRATION_CHECKSUM,
                            FIXED_TIME.isoformat(),
                            MIGRATION_TOOL_VERSION,
                            backups[0].name,
                        ),
                        (
                            V2_MIGRATION_CHECKSUM,
                            FIXED_TIME.isoformat(),
                            MIGRATION_TOOL_VERSION,
                            backups[1].name,
                        ),
                        (
                            V3_MIGRATION_CHECKSUM,
                            FIXED_TIME.isoformat(),
                            MIGRATION_TOOL_VERSION,
                            backups[2].name,
                        ),
                    ],
                )
            self.assertEqual(_migration_checksum(V1_SQL), V1_MIGRATION_CHECKSUM)
            self.assertEqual(_v1_schema_fingerprint_from_sql(), V1_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V2_SQL), V2_MIGRATION_CHECKSUM)
            self.assertEqual(_v2_schema_fingerprint_from_sql(), V2_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V3_SQL), V3_MIGRATION_CHECKSUM)
            self.assertEqual(_v3_schema_fingerprint_from_sql(), V3_SCHEMA_FINGERPRINT)

            source = inspect.getsource(__import__("llm_collab.ledger.store", fromlist=["*"]))
            self.assertIn(".backup(", source)
            self.assertNotIn("shutil", source)
            self.assertNotIn("copyfile", source)
            self.assertIsNone(
                re.search(
                    r"message|delivery|attempt|receipt|lease|fence|quarantine|retry|dead_letter",
                    "\n".join(V1_SQL),
                )
            )

    def test_paired_project_snapshots_are_exact_scoped_and_fully_serialized(self) -> None:
        revision_hash = "a" * 64
        revision = f"sha256:{revision_hash}"
        workspace_json = json.dumps({"workspace_id": "ws_alpha", "projects": [AMIGA, NUVYR]})
        projects = {
            AMIGA: json.dumps({"project_id": AMIGA, "repo": "pixexid/amiga"}),
            NUVYR: json.dumps({"project_id": NUVYR, "repo": "pixexid/nuvyr"}),
        }
        sources = {
            AMIGA: {"chat_index": json.dumps({"root": "Chats/amiga"})},
            NUVYR: {"task_index": json.dumps({"root": "Tasks/nuvyr"})},
        }
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as writer:
                writer.record_registry_snapshot(
                    workspace_id="ws_alpha",
                    registry_revision=revision,
                    registry_source_sha256=revision_hash,
                    captured_at_utc="2026-07-21T08:05:06+00:00",
                    workspace_snapshot_json=workspace_json,
                    project_snapshots=projects,
                    source_snapshots=sources,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    writer.record_registry_snapshot(
                        workspace_id="ws_alpha",
                        registry_revision=revision,
                        registry_source_sha256=revision_hash,
                        captured_at_utc="2026-07-21T08:05:06+00:00",
                        workspace_snapshot_json=workspace_json,
                        project_snapshots=projects,
                        source_snapshots=sources,
                    )

            with LedgerStore.open_reader(paths) as reader:
                expected_amiga = {
                    "workspace_id": "ws_alpha",
                    "project_id": AMIGA,
                    "registry_revision": revision,
                    "snapshot_json": projects[AMIGA],
                }
                expected_nuvyr = {
                    "workspace_id": "ws_alpha",
                    "project_id": NUVYR,
                    "registry_revision": revision,
                    "snapshot_json": projects[NUVYR],
                }
                self.assertEqual(
                    reader.get_project_snapshot(
                        workspace_id="ws_alpha", project_id=AMIGA, registry_revision=revision
                    ),
                    expected_amiga,
                )
                self.assertEqual(
                    reader.get_project_snapshot(
                        workspace_id="ws_alpha", project_id=NUVYR, registry_revision=revision
                    ),
                    expected_nuvyr,
                )
                self.assertNotEqual(expected_amiga["snapshot_json"], expected_nuvyr["snapshot_json"])
                self.assertEqual(
                    reader.get_source_snapshots(
                        workspace_id="ws_alpha", project_id=AMIGA, registry_revision=revision
                    ),
                    [
                        {
                            "workspace_id": "ws_alpha",
                            "project_id": AMIGA,
                            "source_id": "chat_index",
                            "registry_revision": revision,
                            "snapshot_json": sources[AMIGA]["chat_index"],
                        }
                    ],
                )
                self.assertEqual(
                    reader.get_source_snapshots(
                        workspace_id="ws_alpha", project_id=NUVYR, registry_revision=revision
                    )[0]["snapshot_json"],
                    sources[NUVYR]["task_index"],
                )
                self.assertIsNone(
                    reader.get_project_snapshot(
                        workspace_id="ws_alpha", project_id="other", registry_revision=revision
                    )
                )
                self.assertIsNone(
                    reader.get_project_snapshot(
                        workspace_id="ws_alpha",
                        project_id=AMIGA,
                        registry_revision="sha256:" + "b" * 64,
                    )
                )
                with self.assertRaisesRegex(ValueError, "does not own"):
                    reader.get_project_snapshot(
                        workspace_id="ws_other", project_id=AMIGA, registry_revision=revision
                    )
                with self.assertRaises(PermissionError):
                    reader.record_registry_snapshot(
                        workspace_id="ws_alpha",
                        registry_revision=revision,
                        registry_source_sha256=revision_hash,
                        captured_at_utc="now",
                        workspace_snapshot_json="{}",
                        project_snapshots={},
                        source_snapshots={},
                    )

    def test_exact_released_v1_is_writer_migrated_backed_up_and_reader_rejected(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            v0_backup = create_released_v1(paths)
            with self.assertRaisesRegex(MigrationError, "unsupported ledger schema version 1"):
                LedgerStore.open_reader(paths)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as writer:
                self.assertEqual(writer.schema_version(), 3)
                self.assertEqual(writer.integrity_check(), "ok")
            v1_backup = paths.backup_path(
                1, FIXED_TIME.strftime("%Y%m%dT%H%M%S%fZ")
            )
            self.assertTrue(v0_backup.is_file())
            self.assertTrue(v1_backup.is_file())
            with closing(sqlite3.connect(v1_backup)) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(
                    {
                        row[0]
                        for row in backup.execute(
                            "SELECT name FROM sqlite_schema WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'"
                        )
                    },
                    {
                        "schema_migrations",
                        "workspace_registry_snapshots",
                        "project_registry_snapshots",
                        "observation_source_registry_snapshots",
                        "daemon_instances",
                    },
                )
            with LedgerStore.open_reader(paths) as reader:
                self.assertEqual(reader.schema_version(), 3)

    def test_exact_released_v2_migrates_to_v3_and_failed_v3_restores_v2(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            create_released_v2(paths)
            with self.assertRaisesRegex(MigrationError, "unsupported ledger schema version 2"):
                LedgerStore.open_reader(paths)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as writer:
                self.assertEqual(writer.schema_version(), 3)
            v2_backup = paths.backup_path(2, FIXED_TIME.strftime("%Y%m%dT%H%M%S%fZ"))
            with closing(sqlite3.connect(v2_backup)) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(store_module._schema_fingerprint(backup), V2_SCHEMA_FINGERPRINT)

        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            create_released_v2(paths)
            broken_v3 = ((3, V3_SQL + ("CREATE TABLE broken(",)),)
            with (
                patch.object(
                    store_module,
                    "V3_MIGRATION_CHECKSUM",
                    _migration_checksum(broken_v3[0][1]),
                ),
                self.assertRaisesRegex(MigrationError, "verified backup was restored"),
            ):
                LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME, migrations=broken_v3)
            with closing(sqlite3.connect(paths.ledger)) as restored:
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    store_module._schema_fingerprint(restored), V2_SCHEMA_FINGERPRINT
                )

    def test_legacy_provenance_is_atomic_idempotent_scoped_and_append_only(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as writer:
                record_test_registry(writer)
                exact = provenance_record()
                legacy = provenance_record(
                    source_locator="State/session_autobridge/sessions/legacy.json",
                    content_sha256="c" * 64,
                    scope_kind="legacy_unscoped",
                    project_id=None,
                )
                arguments = {
                    "workspace_id": "ws_alpha",
                    "registry_revision": REVISION,
                    "import_transaction_id": "d" * 32,
                    "import_revision": "legacy-provenance/1",
                    "imported_at_utc": FIXED_TIME.isoformat(),
                    "records": [exact, legacy],
                }
                self.assertEqual(writer.import_legacy_provenance(**arguments), 2)
                arguments["import_transaction_id"] = "e" * 32
                self.assertEqual(writer.import_legacy_provenance(**arguments), 0)
                self.assertEqual(len(writer.get_legacy_provenance(
                    workspace_id="ws_alpha", project_id=AMIGA, registry_revision=REVISION
                )), 1)
                self.assertEqual(
                    writer._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports "
                        "WHERE scope_kind = 'legacy_unscoped' AND project_id IS NULL"
                    ).fetchone()[0],
                    1,
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    writer._connection.execute(
                        "UPDATE legacy_provenance_imports SET byte_size = byte_size"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    writer._connection.execute("DELETE FROM legacy_provenance_imports")

                def fail(_stage: str) -> None:
                    raise RuntimeError("injected")

                changed = provenance_record(content_sha256="f" * 64)
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    writer.import_legacy_provenance(
                        **{**arguments, "records": [changed], "failpoint": fail}
                    )
                self.assertEqual(
                    writer._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    2,
                )
                with self.assertRaisesRegex(ValueError, "registered project"):
                    writer.import_legacy_provenance(
                        **{
                            **arguments,
                            "records": [provenance_record(project_id="foreign")],
                        }
                    )

    def test_each_v3_text_guard_independently_rejects_embedded_nul(self) -> None:
        insert = (
            "INSERT INTO legacy_provenance_imports "
            "(workspace_id, registry_revision, scope_kind, scope_identity, project_id, "
            "source_family, record_kind, source_locator, content_sha256, byte_size, "
            "observed_at_utc, imported_at_utc, import_transaction_id, import_revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        base = [
            "ws_alpha",
            REVISION,
            "legacy_unscoped",
            "legacy_unscoped",
            None,
            "session_autobridge",
            "session",
            "State/session_autobridge/sessions/a.json",
            "b" * 64,
            2,
            FIXED_TIME.isoformat(),
            FIXED_TIME.isoformat(),
            "d" * 32,
            "legacy-provenance/1",
        ]
        cases = {
            "workspace_id": (0, "ws\x00alpha"),
            "registry_revision": (1, "sha256:" + "a" * 63 + "\x00"),
            "project_id": (4, "amiga\x00x"),
            "source_locator": (7, "State/session_autobridge/sessions/a.json\x00"),
            "content_sha256": (8, "b" * 63 + "\x00"),
            "observed_at_utc": (10, "now\x00later"),
            "imported_at_utc": (11, "now\x00later"),
            "import_transaction_id": (12, "d" * 31 + "\x00"),
        }
        for column, (index, value) in cases.items():
            with self.subTest(column=column):
                values = list(base)
                values[index] = value
                if column == "project_id":
                    values[2] = "exact_project"
                    values[3] = value
                with closing(sqlite3.connect(":memory:", isolation_level=None)) as original:
                    for statement in V3_SQL:
                        original.execute(statement)
                    with self.assertRaises(sqlite3.IntegrityError):
                        original.execute(insert, values)

                needle = f"instr({column}, char(0)) = 0"
                mutated_table = V3_SQL[0].replace(needle, "1 = 1", 1)
                self.assertNotEqual(mutated_table, V3_SQL[0])
                with closing(sqlite3.connect(":memory:", isolation_level=None)) as mutated:
                    for statement in (mutated_table, *V3_SQL[1:]):
                        mutated.execute(statement)
                    mutated.execute(insert, values)

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_symlinked_sqlite_sidecar_is_rejected_before_sqlite_open(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            paths = LedgerPaths.derive(root / "state", "ws_alpha")
            paths.ensure_directories()
            outside = root / "operator-owned"
            outside.write_bytes(b"unchanged")
            sidecar = paths.ledger.with_name(paths.ledger.name + "-wal")
            sidecar.symlink_to(outside)

            with self.assertRaisesRegex(SQLiteSafetyError, "symlinked SQLite artifact"):
                LedgerStore.open_writer(paths)
            self.assertEqual(outside.read_bytes(), b"unchanged")
            self.assertFalse(paths.ledger.exists())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "no-follow opens unavailable")
    def test_writer_and_reader_refuse_swap_then_restore_to_outside_targets(self) -> None:
        real_connect = sqlite3.connect
        for opener in (LedgerStore.open_writer, LedgerStore.open_reader):
            for outside_exists in (True, False):
                with self.subTest(opener=opener.__name__, outside_exists=outside_exists), TemporaryDirectory(
                    dir="/tmp"
                ) as tmp:
                    root = Path(tmp)
                    paths = LedgerPaths.derive(root / "state", "ws_alpha")
                    with LedgerStore.open_writer(paths):
                        pass
                    original_identity = (paths.ledger.stat().st_dev, paths.ledger.stat().st_ino)
                    outside = root / "operator-owned.sqlite3"
                    if outside_exists:
                        outside.write_bytes(b"operator-owned")
                    parked = paths.ledger.with_name(paths.ledger.name + ".pinned")
                    swapped = False

                    def connect_with_swap(database, *args, **kwargs):
                        nonlocal swapped
                        if not swapped and str(database).startswith(paths.ledger.as_uri()):
                            swapped = True
                            paths.ledger.rename(parked)
                            paths.ledger.symlink_to(outside)
                            try:
                                return real_connect(database, *args, **kwargs)
                            finally:
                                paths.ledger.unlink()
                                parked.rename(paths.ledger)
                        return real_connect(database, *args, **kwargs)

                    with patch.object(store_module.sqlite3, "connect", side_effect=connect_with_swap):
                        with self.assertRaises((sqlite3.Error, SQLiteSafetyError)):
                            opener(paths)
                    self.assertTrue(swapped)
                    self.assertEqual(
                        (paths.ledger.stat().st_dev, paths.ledger.stat().st_ino),
                        original_identity,
                    )
                    if outside_exists:
                        self.assertEqual(outside.read_bytes(), b"operator-owned")
                    else:
                        self.assertFalse(outside.exists())
                    with LedgerStore.open_writer(paths):
                        pass

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "no-follow opens unavailable")
    def test_backup_destination_refuses_swap_then_restore_to_outside_targets(self) -> None:
        real_connect = sqlite3.connect
        for outside_exists in (True, False):
            with self.subTest(outside_exists=outside_exists), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp)
                paths = LedgerPaths.derive(root / "state", "ws_alpha")
                with LedgerStore.open_writer(paths) as writer:
                    backup_time = FIXED_TIME.replace(second=FIXED_TIME.second + 1)
                    writer._clock = lambda: backup_time
                    backup = paths.backup_path(
                        1, backup_time.strftime("%Y%m%dT%H%M%S%fZ")
                    )
                    outside = root / "operator-owned.sqlite3"
                    if outside_exists:
                        outside.write_bytes(b"operator-owned")
                    parked = backup.with_name(backup.name + ".pinned")
                    swapped = False

                    def connect_with_swap(database, *args, **kwargs):
                        nonlocal swapped
                        if not swapped and str(database).startswith(backup.as_uri()):
                            swapped = True
                            backup.rename(parked)
                            backup.symlink_to(outside)
                            try:
                                return real_connect(database, *args, **kwargs)
                            finally:
                                backup.unlink()
                                parked.rename(backup)
                        return real_connect(database, *args, **kwargs)

                    with patch.object(store_module.sqlite3, "connect", side_effect=connect_with_swap):
                        with self.assertRaises((sqlite3.Error, SQLiteSafetyError)):
                            writer._backup_before_migration(1)
                    self.assertTrue(swapped)
                    if outside_exists:
                        self.assertEqual(outside.read_bytes(), b"operator-owned")
                    else:
                        self.assertFalse(outside.exists())
                    self.assertEqual(writer.schema_version(), 3)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "no-follow opens unavailable")
    def test_restore_source_refuses_swap_then_restore_to_outside_targets(self) -> None:
        real_connect = sqlite3.connect
        for outside_exists in (True, False):
            with self.subTest(outside_exists=outside_exists), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp)
                paths = LedgerPaths.derive(root / "state", "ws_alpha")
                with LedgerStore.open_writer(paths) as writer:
                    backup = next(paths.backups.iterdir())
                    original_identity = (backup.stat().st_dev, backup.stat().st_ino)
                    outside = root / "operator-owned.sqlite3"
                    if outside_exists:
                        outside.write_bytes(b"operator-owned")
                    parked = backup.with_name(backup.name + ".pinned")
                    swapped = False

                    def connect_with_swap(database, *args, **kwargs):
                        nonlocal swapped
                        if not swapped and str(database).startswith(backup.as_uri()):
                            swapped = True
                            backup.rename(parked)
                            backup.symlink_to(outside)
                            try:
                                return real_connect(database, *args, **kwargs)
                            finally:
                                backup.unlink()
                                parked.rename(backup)
                        return real_connect(database, *args, **kwargs)

                    with patch.object(store_module.sqlite3, "connect", side_effect=connect_with_swap):
                        with self.assertRaises((sqlite3.Error, SQLiteSafetyError)):
                            writer._restore_from_backup(backup)
                    self.assertTrue(swapped)
                    self.assertEqual(
                        (backup.stat().st_dev, backup.stat().st_ino), original_identity
                    )
                    if outside_exists:
                        self.assertEqual(outside.read_bytes(), b"operator-owned")
                    else:
                        self.assertFalse(outside.exists())
                    self.assertEqual(writer.schema_version(), 3)

    def test_foreign_keys_prevent_cross_scope_source_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                with self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        "INSERT INTO observation_source_registry_snapshots "
                        "(workspace_id, project_id, source_id, registry_revision, snapshot_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("ws_alpha", "amiga", "chat_index", "sha256:" + "b" * 64, "{}"),
                    )

    def test_foreign_key_check_rejects_real_orphans_for_reader_writer_and_backup_verify(self) -> None:
        for opener in (LedgerStore.open_writer, LedgerStore.open_reader):
            with self.subTest(opener=opener.__name__), TemporaryDirectory(dir="/tmp") as tmp:
                paths = LedgerPaths.derive(tmp, "ws_alpha")
                with LedgerStore.open_writer(paths):
                    pass
                with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                    connection.execute("PRAGMA foreign_keys = OFF")
                    connection.execute(
                        "INSERT INTO project_registry_snapshots "
                        "(workspace_id, project_id, registry_revision, snapshot_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            "ws_orphan",
                            AMIGA,
                            "sha256:" + "b" * 64,
                            json.dumps({"project_id": AMIGA}),
                        ),
                    )
                connection.close()
                before = paths.ledger.read_bytes()

                with self.assertRaisesRegex(MigrationError, "foreign_key_check"):
                    opener(paths)
                self.assertEqual(paths.ledger.read_bytes(), before)
                with self.assertRaisesRegex(MigrationError, "foreign_key_check"):
                    LedgerStore._verify_database(paths.ledger)

    def test_migration_metadata_and_schema_fingerprint_are_exact_and_fail_closed(self) -> None:
        corruptions = {
            "empty-checksum": (
                "PRAGMA ignore_check_constraints = ON; "
                "UPDATE schema_migrations SET migration_checksum = ''",
                "metadata|integrity_check",
            ),
            "changed-checksum": (
                "UPDATE schema_migrations SET migration_checksum = 'sha256:" + "0" * 64 + "'",
                "metadata",
            ),
            "empty-time": (
                "PRAGMA ignore_check_constraints = ON; "
                "UPDATE schema_migrations SET applied_at_utc = ''",
                "metadata|integrity_check",
            ),
            "changed-time": (
                "UPDATE schema_migrations SET applied_at_utc = '2026-07-21T08:05:06.123457+00:00'",
                "metadata",
            ),
            "empty-tool-version": (
                "PRAGMA ignore_check_constraints = ON; "
                "UPDATE schema_migrations SET tool_version = ''",
                "metadata|integrity_check",
            ),
            "changed-tool-version": (
                "UPDATE schema_migrations SET tool_version = 'llm-collab-ledger/changed'",
                "metadata",
            ),
            "empty-backup-reference": (
                "PRAGMA ignore_check_constraints = ON; "
                "UPDATE schema_migrations SET backup_reference = ''",
                "metadata|integrity_check",
            ),
            "changed-backup-reference": (
                "UPDATE schema_migrations SET backup_reference = "
                "'ledger-0-20000101T000000000000Z.sqlite3'",
                "metadata|backup reference",
            ),
            "changed-schema-definition": (
                "ALTER TABLE daemon_instances ADD COLUMN unexpected TEXT",
                "fingerprint",
            ),
        }
        for kind, (script, expected) in corruptions.items():
            for opener in (LedgerStore.open_writer, LedgerStore.open_reader):
                with self.subTest(kind=kind, opener=opener.__name__), TemporaryDirectory(
                    dir="/tmp"
                ) as tmp:
                    paths = LedgerPaths.derive(tmp, "ws_alpha")
                    with LedgerStore.open_writer(
                        paths, clock=lambda: FIXED_TIME
                    ):
                        pass
                    with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                        connection.executescript(script)
                    before = paths.ledger.read_bytes()

                    with self.assertRaisesRegex(MigrationError, expected):
                        opener(paths)
                    self.assertEqual(paths.ledger.read_bytes(), before)

    def test_registry_snapshot_identity_validation_is_pretransactional(self) -> None:
        revision_hash = "c" * 64
        base = {
            "workspace_id": "ws_alpha",
            "registry_revision": "sha256:" + revision_hash,
            "registry_source_sha256": revision_hash,
            "captured_at_utc": "2026-07-21T08:05:06+00:00",
            "workspace_snapshot_json": json.dumps(
                {"workspace_id": "ws_alpha", "projects": [AMIGA, NUVYR]}
            ),
            "project_snapshots": {
                AMIGA: json.dumps({"project_id": AMIGA}),
                NUVYR: json.dumps({"id": NUVYR}),
            },
            "source_snapshots": {
                AMIGA: {"chat_index": json.dumps({"root": "Chats/amiga"})},
                NUVYR: {"task_index": json.dumps({"root": "Tasks/nuvyr"})},
            },
        }
        invalid = {
            "malformed-workspace": {"workspace_snapshot_json": "{"},
            "missing-workspace-id": {
                "workspace_snapshot_json": json.dumps({"projects": [AMIGA, NUVYR]})
            },
            "null-workspace-id": {
                "workspace_snapshot_json": json.dumps(
                    {"workspace_id": None, "projects": [AMIGA, NUVYR]}
                )
            },
            "empty-workspace-id": {
                "workspace_snapshot_json": json.dumps(
                    {"workspace_id": "", "projects": [AMIGA, NUVYR]}
                )
            },
            "wrong-workspace-id": {
                "workspace_snapshot_json": json.dumps(
                    {"workspace_id": "ws_other", "projects": [AMIGA, NUVYR]}
                )
            },
            "missing-project-list": {
                "workspace_snapshot_json": json.dumps({"workspace_id": "ws_alpha"})
            },
            "empty-project-list": {
                "workspace_snapshot_json": json.dumps(
                    {"workspace_id": "ws_alpha", "projects": []}
                )
            },
            "duplicate-project-list": {
                "workspace_snapshot_json": json.dumps(
                    {"workspace_id": "ws_alpha", "projects": [AMIGA, AMIGA]}
                )
            },
            "project-set-mismatch": {
                "workspace_snapshot_json": json.dumps(
                    {"workspace_id": "ws_alpha", "projects": [AMIGA]}
                )
            },
            "malformed-project": {
                "project_snapshots": {AMIGA: "{", NUVYR: base["project_snapshots"][NUVYR]}
            },
            "missing-project-id": {
                "project_snapshots": {
                    AMIGA: json.dumps({"repo": "pixexid/amiga"}),
                    NUVYR: base["project_snapshots"][NUVYR],
                }
            },
            "null-project-id": {
                "project_snapshots": {
                    AMIGA: json.dumps({"project_id": None}),
                    NUVYR: base["project_snapshots"][NUVYR],
                }
            },
            "empty-project-id": {
                "project_snapshots": {
                    AMIGA: json.dumps({"project_id": ""}),
                    NUVYR: base["project_snapshots"][NUVYR],
                }
            },
            "wrong-project-id": {
                "project_snapshots": {
                    AMIGA: json.dumps({"project_id": NUVYR}),
                    NUVYR: base["project_snapshots"][NUVYR],
                }
            },
            "conflicting-project-aliases": {
                "project_snapshots": {
                    AMIGA: json.dumps({"project_id": AMIGA, "id": NUVYR}),
                    NUVYR: base["project_snapshots"][NUVYR],
                }
            },
            "malformed-source": {
                "source_snapshots": {
                    AMIGA: {"chat_index": "[1]"},
                    NUVYR: base["source_snapshots"][NUVYR],
                }
            },
            "source-project-outside-set": {
                "source_snapshots": {"other": {"chat_index": "{}"}}
            },
        }
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as writer:
                tables = (
                    "workspace_registry_snapshots",
                    "project_registry_snapshots",
                    "observation_source_registry_snapshots",
                )
                for kind, override in invalid.items():
                    with self.subTest(kind=kind):
                        kwargs = dict(base)
                        kwargs.update(override)
                        with self.assertRaises(ValueError):
                            writer.record_registry_snapshot(**kwargs)
                        self.assertFalse(writer._connection.in_transaction)
                        self.assertEqual(
                            [
                                writer._connection.execute(
                                    f'SELECT count(*) FROM "{table}"'
                                ).fetchone()[0]
                                for table in tables
                            ],
                            [0, 0, 0],
                        )

    def test_writer_checkpoint_is_exclusive_and_connections_are_thread_bound(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as writer:
                with self.assertRaises(WriterAlreadyOpenError):
                    LedgerStore.open_writer(paths)
                errors = []

                def cross_thread_query() -> None:
                    try:
                        writer.schema_version()
                    except BaseException as exc:
                        errors.append(exc)

                thread = threading.Thread(target=cross_thread_query)
                thread.start()
                thread.join()
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], sqlite3.ProgrammingError)
                self.assertEqual(writer.checkpoint()[0], 0)

                close_errors = []

                def cross_thread_close() -> None:
                    try:
                        writer.close()
                    except BaseException as exc:
                        close_errors.append(exc)

                close_thread = threading.Thread(target=cross_thread_close)
                close_thread.start()
                close_thread.join()
                self.assertEqual(len(close_errors), 1)
                self.assertIsInstance(close_errors[0], sqlite3.ProgrammingError)
                self.assertEqual(writer.schema_version(), 3)

            with LedgerStore.open_reader(paths) as reader:
                self.assertEqual(reader._connection.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(PermissionError):
                    reader.checkpoint()
                with self.assertRaises(sqlite3.OperationalError):
                    reader._connection.execute("CREATE TABLE forbidden_write(value TEXT)")

    def test_writer_lock_is_idempotent_and_released_when_store_is_abandoned(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            writer = LedgerStore.open_writer(paths)
            writer.close()
            writer.close()
            with LedgerStore.open_writer(paths):
                pass

            abandoned = LedgerStore.open_writer(paths)
            store_reference = weakref.ref(abandoned)
            lock_reference = weakref.ref(abandoned._writer_lock)
            del abandoned
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                gc.collect()
            self.assertIsNone(store_reference())
            self.assertIsNone(lock_reference())
            with LedgerStore.open_writer(paths) as reopened:
                self.assertEqual(reopened.schema_version(), 3)

    def test_failed_migration_restores_verified_pre_migration_database(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            observed_transaction_state = []
            original = LedgerStore._backup_before_migration

            def checked_backup(store: LedgerStore, version: int):
                observed_transaction_state.append(store._connection.in_transaction)
                return original(store, version)

            broken = ((1, V1_SQL + ("CREATE TABLE broken(",)),)
            with patch.object(LedgerStore, "_backup_before_migration", checked_backup):
                with self.assertRaisesRegex(RuntimeError, "verified backup was restored"):
                    LedgerStore.open_writer(
                        paths,
                        clock=lambda: FIXED_TIME,
                        migrations=broken,
                    )
            self.assertEqual(observed_transaction_state, [False])
            with closing(sqlite3.connect(paths.ledger)) as restored, restored:
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(
                    restored.execute(
                        "SELECT count(*) FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchone()[0],
                    0,
                )

    def test_writer_and_reader_refuse_claimed_empty_schema_without_mutation(self) -> None:
        for opener in (LedgerStore.open_writer, LedgerStore.open_reader):
            with self.subTest(opener=opener.__name__), TemporaryDirectory(dir="/tmp") as tmp:
                paths = LedgerPaths.derive(tmp, "ws_alpha")
                paths.ensure_directories()
                with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                    connection.execute("PRAGMA user_version = 1")
                before = paths.ledger.read_bytes()

                with self.assertRaisesRegex(
                    MigrationError, "corrupt or incoherent|unsupported ledger schema version 1"
                ):
                    opener(paths)
                self.assertEqual(paths.ledger.read_bytes(), before)

    def test_shared_schema_validator_rejects_failed_integrity_check(self) -> None:
        class FailedResult:
            @staticmethod
            def fetchone():
                return ("injected integrity failure",)

        class IntegrityFailureConnection:
            def __init__(self, connection: sqlite3.Connection) -> None:
                self.connection = connection

            def execute(self, sql: str, *args):
                if sql == "PRAGMA integrity_check":
                    return FailedResult()
                return self.connection.execute(sql, *args)

        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths):
                pass
            with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                with self.assertRaisesRegex(MigrationError, "failed integrity_check"):
                    LedgerStore._validate_schema(IntegrityFailureConnection(connection), paths)

    def test_writer_and_reader_refuse_unsupported_or_corrupt_schema_without_mutation(self) -> None:
        for kind in ("unsupported", "corrupt", "extra-table", "bad-metadata"):
            for opener in (LedgerStore.open_writer, LedgerStore.open_reader):
                with self.subTest(kind=kind, opener=opener.__name__), TemporaryDirectory(
                    dir="/tmp"
                ) as tmp:
                    paths = LedgerPaths.derive(tmp, "ws_alpha")
                    paths.ensure_directories()
                    if kind == "unsupported":
                        with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                            connection.execute("PRAGMA user_version = 4")
                        expected = "unsupported ledger schema version"
                    else:
                        if kind == "corrupt":
                            paths.ledger.write_bytes(b"not a sqlite database")
                            expected = "corrupt"
                        elif kind == "extra-table":
                            with LedgerStore.open_writer(paths):
                                pass
                            with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                                connection.execute("CREATE TABLE unsupported_extra(value TEXT)")
                            expected = "table set is incoherent"
                        else:
                            with LedgerStore.open_writer(paths):
                                pass
                            with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                                connection.execute(
                                    "UPDATE schema_migrations SET tool_version = 'changed'"
                                )
                            expected = "migration metadata is incoherent"
                    before = paths.ledger.read_bytes()

                    with self.assertRaisesRegex(MigrationError, expected):
                        opener(paths)
                    self.assertEqual(paths.ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
