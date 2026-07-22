from __future__ import annotations

import gc
import hashlib
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
from contextlib import closing, contextmanager
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
    V4_MIGRATION_CHECKSUM,
    V4_SCHEMA_FINGERPRINT,
    V5_MIGRATION_CHECKSUM,
    V5_SCHEMA_FINGERPRINT,
    V6_MIGRATION_CHECKSUM,
    V6_SCHEMA_FINGERPRINT,
    MigrationError,
    V1_SQL,
    V2_SQL,
    V3_SQL,
    V4_SQL,
    V5_SQL,
    V6_SQL,
    CanonicalIntegrityError,
    _close_connection_and_pin,
    _connection_fd_snapshot,
    _darwin_fd_snapshot,
    _linux_fd_snapshot,
    _migration_checksum,
    _validate_sqlite_version,
    _v1_schema_fingerprint_from_sql,
    _v2_schema_fingerprint_from_sql,
    _v3_schema_fingerprint_from_sql,
    _v4_schema_fingerprint_from_sql,
    _v5_schema_fingerprint_from_sql,
    _v6_schema_fingerprint_from_sql,
    require_safe_sqlite,
)


SAFE_VERSION = (3, 51, 3)
FIXED_TIME = datetime(2026, 7, 21, 8, 5, 6, 123456, tzinfo=timezone.utc)
AMIGA = "amiga"
NUVYR = "nuvyr"
GHOST = "ghost"
REVISION_HASH = "a" * 64
REVISION = f"sha256:{REVISION_HASH}"


def with_integrity(value: dict[str, object]) -> dict[str, object]:
    item = dict(value)
    item["integrity"] = hashlib.sha256(store_module._canonical_json_bytes(item)).hexdigest()
    return item


def legacy_manifest(
    entries: list[dict[str, object]],
    *,
    project_id: str = AMIGA,
    cutoff_policy_revision: str = "cutoff_v1",
    publication_cutoff_policy_revision: str | None = None,
) -> dict[str, object]:
    publication_cutoff_policy_revision = publication_cutoff_policy_revision or cutoff_policy_revision
    publication = with_integrity(
        {
            "publisher": {"identity": "codex", "revision": "p2c_v1"},
            "publication_transaction_id": "publish_txn",
            "provenance_id": "publish_proof",
            "workspace_id": "ws_alpha",
            "project_id": project_id,
            "registry_revision": REVISION,
            "cutoff_policy_revision": publication_cutoff_policy_revision,
            "source_boundary": {
                "kind": "content_addressed_revision",
                "identity": "main_1369eafa",
                "immutable": True,
            },
        }
    )
    projection = {
        "manifest_id": "manifest_alpha",
        "cutoff_policy_revision": cutoff_policy_revision,
        "entries": entries,
        "publication": publication,
    }
    return {
        **projection,
        "sealed": True,
        "seal": {
            "algorithm": "sha256",
            "value": hashlib.sha256(store_module._canonical_json_bytes(projection)).hexdigest(),
        },
    }


def manifest_entry(
    locator: str,
    payload: bytes,
    *,
    evidence_form_version: str = "v2_packet",
    cutoff_policy_revision: str = "cutoff_v1",
    workspace_id: str = "ws_alpha",
    project_id: str = AMIGA,
) -> dict[str, object]:
    return with_integrity(
        {
            "canonical_locator": locator,
            "content_hash": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
            "evidence_form_version": evidence_form_version,
            "cutoff_policy_revision": cutoff_policy_revision,
            "source_workspace_id": workspace_id,
            "source_project_id": project_id,
            "source_registry_revision": REVISION,
            "source_boundary": {
                "kind": "content_addressed_revision",
                "identity": "main_1369eafa",
                "immutable": True,
            },
            "trusted_importer": {"identity": "codex", "revision": "p2c_v1"},
            "transaction_id": "source_txn",
            "provenance_id": "source_proof",
        }
    )


def legacy_packet_bytes(
    *,
    sender: str = "codex",
    recipient: str = "claude",
    priority: str = "high",
    project_id: str = AMIGA,
    body: bytes = b"hello exact body\n",
) -> bytes:
    frontmatter = "\n".join(
        (
            "---",
            "chat_id: CHAT-8976EECB",
            f"from: {sender}",
            f"to: {recipient}",
            "title: Imported packet",
            f"priority: {priority}",
            "tags: [p2c, import]",
            f"project_id: {project_id}",
            "related_task: TASK-6C7155",
            'repo_targets: ["llm-collab"]',
            'path_targets: ["llm_collab/ledger/store.py"]',
            f"sent_utc: {FIXED_TIME.isoformat()}",
            "---",
            "",
        )
    ).encode("utf-8")
    return frontmatter + body


def write_source(root: Path, locator: str, payload: bytes) -> None:
    path = root / locator.strip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


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


def create_released_v3(paths: LedgerPaths, *, now: datetime = FIXED_TIME) -> Path:
    """Materialize the exact released v3 bytes for migration-only tests."""
    create_released_v2(paths, now=now)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = paths.backup_path(2, stamp)
    with closing(sqlite3.connect(paths.ledger)) as source, closing(
        sqlite3.connect(backup)
    ) as destination:
        source.backup(destination)
    backup.chmod(0o600)
    with closing(sqlite3.connect(paths.ledger, isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in V3_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, migration_checksum, applied_at_utc, tool_version, backup_reference) "
            "VALUES (3, ?, ?, ?, ?)",
            (V3_MIGRATION_CHECKSUM, now.isoformat(), MIGRATION_TOOL_VERSION, backup.name),
        )
        connection.execute("PRAGMA user_version = 3")
        connection.execute("COMMIT")
    return backup


def create_released_v4(paths: LedgerPaths, *, now: datetime = FIXED_TIME) -> Path:
    """Materialize the exact released v4 bytes for migration-only tests."""
    create_released_v3(paths, now=now)
    stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    backup = paths.backup_path(3, stamp)
    with closing(sqlite3.connect(paths.ledger)) as source, closing(
        sqlite3.connect(backup)
    ) as destination:
        source.backup(destination)
    backup.chmod(0o600)
    with closing(sqlite3.connect(paths.ledger, isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in V4_SQL:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations "
            "(version, migration_checksum, applied_at_utc, tool_version, backup_reference) "
            "VALUES (4, ?, ?, ?, ?)",
            (V4_MIGRATION_CHECKSUM, now.isoformat(), MIGRATION_TOOL_VERSION, backup.name),
        )
        connection.execute("PRAGMA user_version = 4")
        connection.execute("COMMIT")
    return backup


def v4_fingerprint(statements: tuple[str, ...]) -> str:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *statements):
            connection.execute(statement)
        return store_module._schema_fingerprint(connection)


def v5_fingerprint_with_v4(statements: tuple[str, ...]) -> str:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *statements, *V5_SQL):
            connection.execute(statement)
        return store_module._schema_fingerprint(connection)


def v6_fingerprint_with_v4(statements: tuple[str, ...]) -> str:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *statements, *V5_SQL, *V6_SQL):
            connection.execute(statement)
        return store_module._schema_fingerprint(connection)


def mutate_v4(old: str, new: str, *, occurrence: int = 1) -> tuple[str, ...]:
    statements = list(V4_SQL)
    matches = [index for index, statement in enumerate(statements) if old in statement]
    if len(matches) < occurrence:
        raise AssertionError(f"v4 mutation target not found: {old!r}")
    index = matches[occurrence - 1]
    statements[index] = statements[index].replace(old, new, 1)
    return tuple(statements)


@contextmanager
def open_mutated_v4(paths: LedgerPaths, statements: tuple[str, ...]):
    checksum = _migration_checksum(statements)
    fingerprint = v4_fingerprint(statements)
    v5_fingerprint = v5_fingerprint_with_v4(statements)
    v6_fingerprint = v6_fingerprint_with_v4(statements)
    with (
        patch.object(store_module, "V4_SQL", statements),
        patch.object(store_module, "V4_MIGRATION_CHECKSUM", checksum),
        patch.object(store_module, "V4_SCHEMA_FINGERPRINT", fingerprint),
        patch.object(store_module, "V5_SCHEMA_FINGERPRINT", v5_fingerprint),
        patch.object(store_module, "V6_SCHEMA_FINGERPRINT", v6_fingerprint),
        LedgerStore.open_writer(
            paths,
            migrations=(
                (1, V1_SQL),
                (2, V2_SQL),
                (3, V3_SQL),
                (4, statements),
                (5, V5_SQL),
                (6, V6_SQL),
            ),
        ) as store,
    ):
        yield store


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
                self.assertEqual(writer.schema_version(), 6)

    def test_all_file_backed_connects_use_one_verified_noncreating_open(self) -> None:
        source = inspect.getsource(store_module)
        self.assertEqual(source.count("sqlite3.connect("), 7)
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

    def test_v5_schema_connection_guards_backups_and_private_permissions(self) -> None:
        self.assertEqual(sum("CREATE TABLE canonical_" in sql for sql in V4_SQL), 5)
        self.assertEqual(sum("CREATE TRIGGER" in sql for sql in V4_SQL), 18)
        self.assertEqual(sum("CREATE TABLE canonical_" in sql for sql in V5_SQL), 4)
        self.assertEqual(sum("CREATE TRIGGER" in sql for sql in V5_SQL), 12)
        self.assertEqual(sum("CREATE TABLE legacy_import_" in sql for sql in V6_SQL), 3)
        self.assertEqual(sum("CREATE TRIGGER" in sql for sql in V6_SQL), 6)
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
                self.assertEqual(store.schema_version(), 6)
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
                    "canonical_bodies",
                    "canonical_messages",
                    "canonical_message_recipients",
                    "canonical_message_artifacts",
                    "canonical_message_tags",
                    "canonical_evidence_bodies",
                    "canonical_deliveries",
                    "canonical_delivery_attempts",
                    "canonical_delivery_receipts",
                    "legacy_import_manifests",
                    "legacy_import_manifest_entries",
                    "legacy_import_records",
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(tables, expected)
                forbidden = re.compile(r"lease|fence|quarantine|retry|dead_letter")
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
                    "ledger-3-20260721T080506123456Z.sqlite3",
                    "ledger-4-20260721T080506123456Z.sqlite3",
                    "ledger-5-20260721T080506123456Z.sqlite3",
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
                        (
                            V4_MIGRATION_CHECKSUM,
                            FIXED_TIME.isoformat(),
                            MIGRATION_TOOL_VERSION,
                            backups[3].name,
                        ),
                        (
                            V5_MIGRATION_CHECKSUM,
                            FIXED_TIME.isoformat(),
                            MIGRATION_TOOL_VERSION,
                            backups[4].name,
                        ),
                        (
                            V6_MIGRATION_CHECKSUM,
                            FIXED_TIME.isoformat(),
                            MIGRATION_TOOL_VERSION,
                            backups[5].name,
                        ),
                    ],
                )
            self.assertEqual(_migration_checksum(V1_SQL), V1_MIGRATION_CHECKSUM)
            self.assertEqual(_v1_schema_fingerprint_from_sql(), V1_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V2_SQL), V2_MIGRATION_CHECKSUM)
            self.assertEqual(_v2_schema_fingerprint_from_sql(), V2_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V3_SQL), V3_MIGRATION_CHECKSUM)
            self.assertEqual(_v3_schema_fingerprint_from_sql(), V3_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V4_SQL), V4_MIGRATION_CHECKSUM)
            self.assertEqual(_v4_schema_fingerprint_from_sql(), V4_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V5_SQL), V5_MIGRATION_CHECKSUM)
            self.assertEqual(_v5_schema_fingerprint_from_sql(), V5_SCHEMA_FINGERPRINT)
            self.assertEqual(_migration_checksum(V6_SQL), V6_MIGRATION_CHECKSUM)
            self.assertEqual(_v6_schema_fingerprint_from_sql(), V6_SCHEMA_FINGERPRINT)

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

    def test_v6_manifest_recording_is_idempotent_append_only_and_writes_no_v5_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                message_id, created = store.create_canonical_message(
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=AMIGA,
                    sender_agent_id="agent_codex",
                    dedupe_key="import:" + "a" * 64,
                    body=b"hello",
                    recipients=("agent_claude",),
                    registry_revision=REVISION,
                    created_at_utc=FIXED_TIME.isoformat(),
                    title="Imported packet",
                    priority="high",
                    chat_link="CHAT-8976EECB",
                    task_link="TASK-6C7155",
                )
                self.assertTrue(created)
                entries = [
                    manifest_entry("/Chats/chat-a/2026-07-22T00-00-00_to-claude.md", b"hello"),
                    manifest_entry("/agents/claude/inbox.json", b"[]"),
                ]
                manifest = legacy_manifest(entries)
                before_v5 = {
                    table: store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in (
                        "canonical_evidence_bodies",
                        "canonical_deliveries",
                        "canonical_delivery_attempts",
                        "canonical_delivery_receipts",
                    )
                }
                seal, imported = store.record_legacy_import_manifest(
                    workspace_id="ws_alpha",
                    manifest=manifest,
                    records=(
                        {
                            "entry_integrity": entries[0]["integrity"],
                            "record_kind": "message",
                            "scope_kind": "project",
                            "scope_identity": AMIGA,
                            "message_id": message_id,
                        },
                        {
                            "entry_integrity": entries[1]["integrity"],
                            "record_kind": "inbox_pointer",
                        },
                    ),
                    imported_at_utc=FIXED_TIME.isoformat(),
                )
                self.assertTrue(imported)
                self.assertEqual(seal, manifest["seal"]["value"])
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    {
                        table: store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in before_v5
                    },
                    before_v5,
                )
                retry_seal, retry_imported = store.record_legacy_import_manifest(
                    workspace_id="ws_alpha",
                    manifest=manifest,
                    records=(
                        {
                            "entry_integrity": entries[0]["integrity"],
                            "record_kind": "message",
                            "scope_kind": "project",
                            "scope_identity": AMIGA,
                            "message_id": message_id,
                        },
                        {
                            "entry_integrity": entries[1]["integrity"],
                            "record_kind": "inbox_pointer",
                        },
                    ),
                    imported_at_utc=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc).isoformat(),
                )
                self.assertEqual(retry_seal, seal)
                self.assertFalse(retry_imported)
                for table in (
                    "legacy_import_manifests",
                    "legacy_import_manifest_entries",
                    "legacy_import_records",
                ):
                    with self.subTest(table=table, operation="update"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "append-only"
                    ):
                        store._connection.execute(f"UPDATE {table} SET workspace_id = workspace_id")
                    with self.subTest(table=table, operation="delete"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "append-only"
                    ):
                        store._connection.execute(f"DELETE FROM {table}")

    def test_v6_manifest_recomputes_integrity_and_aborts_before_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                entries = [manifest_entry("/Chats/chat-a/2026-07-22T00-00-00_to-claude.md", b"hello")]
                manifest = legacy_manifest(entries)
                manifest["seal"] = {"algorithm": "sha256", "value": "0" * 64}
                with self.assertRaisesRegex(CanonicalIntegrityError, "seal"):
                    store.record_legacy_import_manifest(
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        records=({"entry_integrity": entries[0]["integrity"], "record_kind": "inbox_pointer"},),
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )

    def test_v6_manifest_rejects_duplicate_locators_before_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                locator = "/Chats/chat-a/2026-07-22T00-00-00_to-claude.md"
                entries = [
                    manifest_entry(locator, b"hello"),
                    manifest_entry(locator, b"hello"),
                ]
                manifest = legacy_manifest(entries)
                with self.assertRaisesRegex(ValueError, "duplicate-free canonical_locator"):
                    store.record_legacy_import_manifest(
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        records=(),
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )

    def test_v6_manifest_rejects_unregistered_publication_project_before_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            meta = b'{"chat_id":"CHAT-8976EECB","project_id":"ghost"}'
            entries = [
                manifest_entry(
                    "/Chats/chat-a/meta.json",
                    meta,
                    evidence_form_version="v2_chat_meta",
                    project_id=GHOST,
                )
            ]
            manifest = legacy_manifest(entries, project_id=GHOST)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                with self.assertRaisesRegex(ValueError, "unknown project"):
                    store.record_legacy_import_manifest(
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        records=(),
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                    0,
                )

    def test_v6_import_legacy_v2_pair_meta_and_inbox_in_one_transaction_without_v5_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
            packet = legacy_packet_bytes()
            to_locator = "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md"
            from_locator = "/Chats/chat-a/2026-07-22T00-00-00_from-codex_import.md"
            meta_locator = "/Chats/chat-a/meta.json"
            inbox_locator = "/agents/claude/inbox.json"
            meta = b'{"chat_id":"CHAT-8976EECB","project_id":"amiga"}'
            inbox = json.dumps(
                {
                    "agent": "claude",
                    "queued": [],
                    "read": [],
                    "unread": [to_locator.strip("/")],
                    "updated_utc": FIXED_TIME.isoformat(),
                }
            ).encode("utf-8")
            for locator, payload in (
                (to_locator, packet),
                (from_locator, packet),
                (meta_locator, meta),
                (inbox_locator, inbox),
            ):
                write_source(root, locator, payload)
            entries = [
                manifest_entry(to_locator, packet),
                manifest_entry(from_locator, packet),
                manifest_entry(meta_locator, meta, evidence_form_version="v2_chat_meta"),
                manifest_entry(inbox_locator, inbox, evidence_form_version="v2_inbox_index"),
            ]
            manifest = legacy_manifest(entries)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                before_v5 = {
                    table: store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in (
                        "canonical_evidence_bodies",
                        "canonical_deliveries",
                        "canonical_delivery_attempts",
                        "canonical_delivery_receipts",
                    )
                }
                begin_count = 0

                def counting_trace(statement: str) -> None:
                    nonlocal begin_count
                    if statement == "BEGIN IMMEDIATE":
                        begin_count += 1

                store._connection.set_trace_callback(counting_trace)
                try:
                    seal, imported = store.import_legacy_v2_manifest(
                        workspace_root=root,
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        registry_revision=REVISION,
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                finally:
                    store._connection.set_trace_callback(None)
                self.assertTrue(imported)
                self.assertEqual(seal, manifest["seal"]["value"])
                self.assertEqual(begin_count, 1)
                row = store._connection.execute(
                    "SELECT scope_kind, scope_identity, sender_agent_id, dedupe_key, "
                    "ttl_seconds, ack_policy, priority, chat_link, task_link "
                    "FROM canonical_messages"
                ).fetchone()
                self.assertEqual(
                    row,
                    (
                        "project",
                        AMIGA,
                        "agent_codex",
                        "import:" + hashlib.sha256(to_locator.encode("utf-8")).hexdigest(),
                        0,
                        "none",
                        "high",
                        "CHAT-8976EECB",
                        "TASK-6C7155",
                    ),
                )
                self.assertEqual(
                    store._connection.execute("SELECT body FROM canonical_bodies").fetchone()[0],
                    b"hello exact body\n",
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    store._connection.execute("SELECT record_kind, count(*) FROM legacy_import_records GROUP BY record_kind ORDER BY record_kind").fetchall(),
                    [("inbox_pointer", 1), ("message", 2)],
                )
                self.assertEqual(
                    {
                        table: store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in before_v5
                    },
                    before_v5,
                )
                retry_seal, retry_imported = store.import_legacy_v2_manifest(
                    workspace_root=root,
                    workspace_id="ws_alpha",
                    manifest=manifest,
                    registry_revision=REVISION,
                    imported_at_utc=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc).isoformat(),
                )
                self.assertEqual(retry_seal, seal)
                self.assertFalse(retry_imported)
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                    1,
                )

    def test_v6_import_legacy_v2_real_shaped_inbox_without_project_imports_null_scope_pointer(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
            inbox_locator = "/agents/claude/inbox.json"
            inbox = json.dumps(
                {
                    "agent": "claude",
                    "queued": [],
                    "read": [],
                    "unread": ["Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md"],
                    "updated_utc": FIXED_TIME.isoformat(),
                }
            ).encode("utf-8")
            write_source(root, inbox_locator, inbox)
            entries = [manifest_entry(inbox_locator, inbox, evidence_form_version="v2_inbox_index")]
            manifest = legacy_manifest(entries)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                seal, imported = store.import_legacy_v2_manifest(
                    workspace_root=root,
                    workspace_id="ws_alpha",
                    manifest=manifest,
                    registry_revision=REVISION,
                    imported_at_utc=FIXED_TIME.isoformat(),
                )
                self.assertTrue(imported)
                self.assertEqual(seal, manifest["seal"]["value"])
                self.assertEqual(
                    store._connection.execute(
                        "SELECT record_kind, scope_kind, scope_identity, message_id FROM legacy_import_records"
                    ).fetchall(),
                    [("inbox_pointer", None, None, None)],
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                    0,
                )

    def test_v6_import_legacy_v2_invalid_priority_aborts_without_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
            packet = legacy_packet_bytes(priority="surprise")
            to_locator = "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md"
            from_locator = "/Chats/chat-a/2026-07-22T00-00-00_from-codex_import.md"
            write_source(root, to_locator, packet)
            write_source(root, from_locator, packet)
            manifest = legacy_manifest(
                [manifest_entry(to_locator, packet), manifest_entry(from_locator, packet)]
            )
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                with self.assertRaisesRegex(ValueError, "priority"):
                    store.import_legacy_v2_manifest(
                        workspace_root=root,
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        registry_revision=REVISION,
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )

    def test_v6_import_legacy_v2_project_mismatch_aborts_without_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
            packet = legacy_packet_bytes(project_id=NUVYR)
            to_locator = "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md"
            from_locator = "/Chats/chat-a/2026-07-22T00-00-00_from-codex_import.md"
            write_source(root, to_locator, packet)
            write_source(root, from_locator, packet)
            manifest = legacy_manifest(
                [manifest_entry(to_locator, packet), manifest_entry(from_locator, packet)]
            )
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                with self.assertRaisesRegex(ValueError, "project_id must match manifest provenance"):
                    store.import_legacy_v2_manifest(
                        workspace_root=root,
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        registry_revision=REVISION,
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )

    def test_v6_import_legacy_v2_non_packet_project_mismatch_aborts_without_rows(self) -> None:
        cases = (
            (
                "/Chats/chat-a/meta.json",
                b'{"chat_id":"CHAT-8976EECB","project_id":"nuvyr"}',
                "v2_chat_meta",
            ),
            (
                "/agents/claude/inbox.json",
                b'{"project_id":"nuvyr","unread":[],"read":[]}',
                "v2_inbox_index",
            ),
        )
        for locator, payload, evidence_form_version in cases:
            with self.subTest(evidence_form_version=evidence_form_version), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp) / "workspace"
                root.mkdir()
                paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
                write_source(root, locator, payload)
                entries = [
                    manifest_entry(
                        locator,
                        payload,
                        evidence_form_version=evidence_form_version,
                        project_id=NUVYR,
                    )
                ]
                manifest = legacy_manifest(entries)
                with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                    record_test_registry(store)
                    begin_count = 0

                    def counting_trace(statement: str) -> None:
                        nonlocal begin_count
                        if statement == "BEGIN IMMEDIATE":
                            begin_count += 1

                    store._connection.set_trace_callback(counting_trace)
                    try:
                        with self.assertRaisesRegex(ValueError, "source_project_id"):
                            store.import_legacy_v2_manifest(
                                workspace_root=root,
                                workspace_id="ws_alpha",
                                manifest=manifest,
                                registry_revision=REVISION,
                                imported_at_utc=FIXED_TIME.isoformat(),
                            )
                    finally:
                        store._connection.set_trace_callback(None)
                    self.assertEqual(begin_count, 0)
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                        0,
                    )

    def test_v6_import_legacy_v2_source_workspace_mismatch_aborts_without_rows(self) -> None:
        packet = legacy_packet_bytes()
        cases = (
            (
                (
                    (
                        "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md",
                        packet,
                        "v2_packet",
                    ),
                    (
                        "/Chats/chat-a/2026-07-22T00-00-00_from-codex_import.md",
                        packet,
                        "v2_packet",
                    ),
                ),
                "packet_pair",
            ),
            (
                (
                    (
                        "/Chats/chat-a/meta.json",
                        b'{"chat_id":"CHAT-8976EECB","project_id":"amiga"}',
                        "v2_chat_meta",
                    ),
                ),
                "chat_meta",
            ),
            (
                (
                    (
                        "/agents/claude/inbox.json",
                        b'{"project_id":"amiga","unread":[],"read":[]}',
                        "v2_inbox_index",
                    ),
                ),
                "inbox_pointer",
            ),
        )
        for source_items, label in cases:
            with self.subTest(kind=label), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp) / "workspace"
                root.mkdir()
                paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
                for locator, payload, _evidence_form_version in source_items:
                    write_source(root, locator, payload)
                entries = [
                    manifest_entry(
                        locator,
                        payload,
                        evidence_form_version=evidence_form_version,
                        workspace_id="ws_other",
                    )
                    for locator, payload, evidence_form_version in source_items
                ]
                manifest = legacy_manifest(entries)
                with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                    record_test_registry(store)
                    begin_count = 0

                    def counting_trace(statement: str) -> None:
                        nonlocal begin_count
                        if statement == "BEGIN IMMEDIATE":
                            begin_count += 1

                    store._connection.set_trace_callback(counting_trace)
                    try:
                        with self.assertRaisesRegex(ValueError, "source_workspace_id"):
                            store.import_legacy_v2_manifest(
                                workspace_root=root,
                                workspace_id="ws_alpha",
                                manifest=manifest,
                                registry_revision=REVISION,
                                imported_at_utc=FIXED_TIME.isoformat(),
                            )
                    finally:
                        store._connection.set_trace_callback(None)
                    self.assertEqual(begin_count, 0)
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                        0,
                    )

    def test_v6_import_legacy_v2_metadata_only_unregistered_project_aborts_without_rows(self) -> None:
        cases = (
            (
                "/Chats/chat-a/meta.json",
                b'{"chat_id":"CHAT-8976EECB","project_id":"ghost"}',
                "v2_chat_meta",
            ),
            (
                "/agents/claude/inbox.json",
                b'{"project_id":"ghost","unread":[],"read":[]}',
                "v2_inbox_index",
            ),
        )
        for locator, payload, evidence_form_version in cases:
            with self.subTest(evidence_form_version=evidence_form_version), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp) / "workspace"
                root.mkdir()
                paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
                write_source(root, locator, payload)
                entries = [
                    manifest_entry(
                        locator,
                        payload,
                        evidence_form_version=evidence_form_version,
                        project_id=GHOST,
                    )
                ]
                manifest = legacy_manifest(entries, project_id=GHOST)
                with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                    record_test_registry(store)
                    begin_count = 0

                    def counting_trace(statement: str) -> None:
                        nonlocal begin_count
                        if statement == "BEGIN IMMEDIATE":
                            begin_count += 1

                    store._connection.set_trace_callback(counting_trace)
                    try:
                        with self.assertRaisesRegex(ValueError, "unknown project"):
                            store.import_legacy_v2_manifest(
                                workspace_root=root,
                                workspace_id="ws_alpha",
                                manifest=manifest,
                                registry_revision=REVISION,
                                imported_at_utc=FIXED_TIME.isoformat(),
                            )
                    finally:
                        store._connection.set_trace_callback(None)
                    self.assertEqual(begin_count, 0)
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                        0,
                    )

    def test_v6_manifest_cutoff_policy_mismatch_aborts_without_rows(self) -> None:
        cases = (
            (
                "publication",
                legacy_manifest(
                    [
                        manifest_entry(
                            "/Chats/chat-a/meta.json",
                            b'{"chat_id":"CHAT-8976EECB","project_id":"amiga"}',
                            evidence_form_version="v2_chat_meta",
                        )
                    ],
                    publication_cutoff_policy_revision="cutoff_v2",
                ),
            ),
            (
                "entry",
                legacy_manifest(
                    [
                        manifest_entry(
                            "/Chats/chat-a/meta.json",
                            b'{"chat_id":"CHAT-8976EECB","project_id":"amiga"}',
                            evidence_form_version="v2_chat_meta",
                            cutoff_policy_revision="cutoff_v2",
                        )
                    ]
                ),
            ),
        )
        for label, manifest in cases:
            with self.subTest(label=label), TemporaryDirectory(dir="/tmp") as tmp:
                paths = LedgerPaths.derive(tmp, "ws_alpha")
                with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                    record_test_registry(store)
                    with self.assertRaisesRegex(ValueError, "cutoff_policy_revision"):
                        store.record_legacy_import_manifest(
                            workspace_id="ws_alpha",
                            manifest=manifest,
                            records=(),
                            imported_at_utc=FIXED_TIME.isoformat(),
                        )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                        0,
                    )

    def test_v6_manifest_message_record_scope_must_match_publication_project(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                message_id, created = store.create_canonical_message(
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=NUVYR,
                    sender_agent_id="agent_codex",
                    dedupe_key="import:" + "b" * 64,
                    body=b"hello",
                    recipients=("agent_claude",),
                    registry_revision=REVISION,
                    created_at_utc=FIXED_TIME.isoformat(),
                    title="Imported packet",
                    priority="high",
                )
                self.assertTrue(created)
                entries = [manifest_entry("/Chats/chat-a/2026-07-22T00-00-00_to-claude.md", b"hello")]
                manifest = legacy_manifest(entries)
                with self.assertRaisesRegex(ValueError, "message records"):
                    store.record_legacy_import_manifest(
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        records=(
                            {
                                "entry_integrity": entries[0]["integrity"],
                                "record_kind": "message",
                                "scope_kind": "project",
                                "scope_identity": NUVYR,
                                "message_id": message_id,
                            },
                        ),
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                    0,
                )

    def test_v6_import_legacy_v2_source_declared_project_must_match_publication(self) -> None:
        packet_missing_project = legacy_packet_bytes().replace(b"project_id: amiga\n", b"")
        cases = (
            (
                "packet_missing_project",
                (
                    (
                        "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md",
                        packet_missing_project,
                        "v2_packet",
                    ),
                    (
                        "/Chats/chat-a/2026-07-22T00-00-00_from-codex_import.md",
                        packet_missing_project,
                        "v2_packet",
                    ),
                ),
            ),
            (
                "meta_missing_project",
                (("/Chats/chat-a/meta.json", b'{"chat_id":"CHAT-8976EECB"}', "v2_chat_meta"),),
            ),
            (
                "meta_foreign_project",
                (
                    (
                        "/Chats/chat-a/meta.json",
                        b'{"chat_id":"CHAT-8976EECB","project_id":"nuvyr"}',
                        "v2_chat_meta",
                    ),
                ),
            ),
            (
                "inbox_foreign_project",
                (
                    (
                        "/agents/claude/inbox.json",
                        b'{"project_id":"nuvyr","unread":[],"read":[]}',
                        "v2_inbox_index",
                    ),
                ),
            ),
            (
                "inbox_null_project",
                (
                    (
                        "/agents/claude/inbox.json",
                        b'{"project_id":null,"unread":[],"read":[]}',
                        "v2_inbox_index",
                    ),
                ),
            ),
        )
        for label, source_items in cases:
            with self.subTest(label=label), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp) / "workspace"
                root.mkdir()
                paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
                for locator, payload, _evidence_form_version in source_items:
                    write_source(root, locator, payload)
                entries = [
                    manifest_entry(locator, payload, evidence_form_version=evidence_form_version)
                    for locator, payload, evidence_form_version in source_items
                ]
                manifest = legacy_manifest(entries)
                with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                    record_test_registry(store)
                    begin_count = 0

                    def counting_trace(statement: str) -> None:
                        nonlocal begin_count
                        if statement == "BEGIN IMMEDIATE":
                            begin_count += 1

                    store._connection.set_trace_callback(counting_trace)
                    try:
                        with self.assertRaisesRegex(ValueError, "legacy source project_id"):
                            store.import_legacy_v2_manifest(
                                workspace_root=root,
                                workspace_id="ws_alpha",
                                manifest=manifest,
                                registry_revision=REVISION,
                                imported_at_utc=FIXED_TIME.isoformat(),
                            )
                    finally:
                        store._connection.set_trace_callback(None)
                    self.assertEqual(begin_count, 0)
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_manifest_entries").fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        store._connection.execute("SELECT count(*) FROM legacy_import_records").fetchone()[0],
                        0,
                    )

    def test_v6_import_legacy_v2_missing_pair_member_aborts_without_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
            packet = legacy_packet_bytes()
            to_locator = "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md"
            write_source(root, to_locator, packet)
            manifest = legacy_manifest([manifest_entry(to_locator, packet)])
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    store.import_legacy_v2_manifest(
                        workspace_root=root,
                        workspace_id="ws_alpha",
                        manifest=manifest,
                        registry_revision=REVISION,
                        imported_at_utc=FIXED_TIME.isoformat(),
                    )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM canonical_messages").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
                )

    def test_v6_import_legacy_v2_hash_mismatch_aborts_before_transaction(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            paths = LedgerPaths.derive(Path(tmp) / "ledger", "ws_alpha")
            packet = legacy_packet_bytes()
            to_locator = "/Chats/chat-a/2026-07-22T00-00-00_to-claude_import.md"
            from_locator = "/Chats/chat-a/2026-07-22T00-00-00_from-codex_import.md"
            write_source(root, to_locator, b"tampered")
            write_source(root, from_locator, packet)
            manifest = legacy_manifest(
                [manifest_entry(to_locator, packet), manifest_entry(from_locator, packet)]
            )
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as store:
                record_test_registry(store)
                begin_count = 0

                def counting_trace(statement: str) -> None:
                    nonlocal begin_count
                    if statement == "BEGIN IMMEDIATE":
                        begin_count += 1

                store._connection.set_trace_callback(counting_trace)
                try:
                    with self.assertRaisesRegex(CanonicalIntegrityError, "hash"):
                        store.import_legacy_v2_manifest(
                            workspace_root=root,
                            workspace_id="ws_alpha",
                            manifest=manifest,
                            registry_revision=REVISION,
                            imported_at_utc=FIXED_TIME.isoformat(),
                        )
                finally:
                    store._connection.set_trace_callback(None)
                self.assertEqual(begin_count, 0)
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM legacy_import_manifests").fetchone()[0],
                    0,
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
                self.assertEqual(writer.schema_version(), 6)
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
                self.assertEqual(reader.schema_version(), 6)

    def test_exact_released_v2_migrates_to_v6_and_failed_v3_restores_v2(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            create_released_v2(paths)
            with self.assertRaisesRegex(MigrationError, "unsupported ledger schema version 2"):
                LedgerStore.open_reader(paths)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as writer:
                self.assertEqual(writer.schema_version(), 6)
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

    def test_exact_released_v3_migrates_to_v6_and_failed_v4_restores_v3(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            create_released_v3(paths)
            with self.assertRaisesRegex(MigrationError, "unsupported ledger schema version 3"):
                LedgerStore.open_reader(paths)
            with LedgerStore.open_writer(paths, clock=lambda: FIXED_TIME) as writer:
                self.assertEqual(writer.schema_version(), 6)
            v3_backup = paths.backup_path(3, FIXED_TIME.strftime("%Y%m%dT%H%M%S%fZ"))
            with closing(sqlite3.connect(v3_backup)) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(store_module._schema_fingerprint(backup), V3_SCHEMA_FINGERPRINT)

        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            create_released_v3(paths)
            broken_sql = V4_SQL + ("CREATE TABLE broken(",)
            with (
                patch.object(
                    store_module,
                    "V4_MIGRATION_CHECKSUM",
                    _migration_checksum(broken_sql),
                ),
                self.assertRaisesRegex(MigrationError, "verified backup was restored"),
            ):
                LedgerStore.open_writer(
                    paths,
                    clock=lambda: FIXED_TIME,
                    migrations=((4, broken_sql),),
                )
            with closing(sqlite3.connect(paths.ledger)) as restored:
                self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(
                    store_module._schema_fingerprint(restored), V3_SCHEMA_FINGERPRINT
                )

    def test_v4_direct_sql_constraints_count_caps_and_append_only_guards(self) -> None:
        from llm_collab.canonical import create_or_return_equivalent

        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_test_registry(store)
                message_id, _ = create_or_return_equivalent(
                    store,
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=AMIGA,
                    sender_agent_id="agent_codex",
                    dedupe_key="direct-matrix",
                    body=b"body",
                    recipients=["agent_codex"],
                    artifacts=[("path", "original")],
                    tags=["original"],
                    registry_revision=REVISION,
                    created_at_utc=FIXED_TIME.isoformat(),
                    title="title",
                )
                connection = store._connection
                body_sha256 = "230d8358dc8e8890b4c58deeb62912ee2f20357ae92a5cc861b98e68fe31acb5"
                self.assertEqual(
                    connection.execute(
                        "SELECT body_sha256 FROM canonical_messages WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()[0],
                    body_sha256,
                )

                invalid_bodies = (
                    ("a" * 63 + "\x00", 0, b""),
                    ("A" * 64, 0, b""),
                    ("a" * 63, 0, b""),
                    ("b" * 64, 2, b"x"),
                    ("c" * 64, 1048577, b"x" * 1048577),
                )
                for digest, byte_size, body in invalid_bodies:
                    with self.subTest(body_digest=digest[:8]), self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "INSERT INTO canonical_bodies "
                            "(workspace_id, body_sha256, byte_size, body, created_at_utc) "
                            "VALUES (?, ?, ?, ?, ?)",
                            ("ws_alpha", digest, byte_size, body, FIXED_TIME.isoformat()),
                        )

                insert_message = (
                    "INSERT INTO canonical_messages "
                    "(workspace_id, scope_kind, scope_identity, message_id, sender_agent_id, "
                    "dedupe_key, body_sha256, reply_to_message_id, ttl_seconds, ack_policy, "
                    "title, priority, chat_link, task_link, registry_revision, project_id, "
                    "created_at_utc) VALUES (:workspace_id, :scope_kind, :scope_identity, "
                    ":message_id, :sender_agent_id, :dedupe_key, :body_sha256, "
                    ":reply_to_message_id, :ttl_seconds, :ack_policy, :title, :priority, "
                    ":chat_link, :task_link, :registry_revision, :project_id, :created_at_utc)"
                )
                base = {
                    "workspace_id": "ws_alpha",
                    "scope_kind": "project",
                    "scope_identity": AMIGA,
                    "message_id": "msg_" + "1" * 64,
                    "sender_agent_id": "agent_codex",
                    "dedupe_key": "raw-message",
                    "body_sha256": body_sha256,
                    "reply_to_message_id": None,
                    "ttl_seconds": 0,
                    "ack_policy": "none",
                    "title": "title",
                    "priority": "normal",
                    "chat_link": None,
                    "task_link": None,
                    "registry_revision": REVISION,
                    "project_id": AMIGA,
                    "created_at_utc": FIXED_TIME.isoformat(),
                }
                invalid_messages = (
                    {"scope_kind": "foreign"},
                    {"scope_kind": "workspace", "scope_identity": "workspace"},
                    {"project_id": None},
                    {"scope_identity": NUVYR},
                    {"project_id": "unknown", "scope_identity": "unknown"},
                    {"registry_revision": "sha256:" + "f" * 64},
                    {"priority": "foreign"},
                    {"ack_policy": "foreign"},
                    {"dedupe_key": "d" * 257},
                    {"title": "t" * 513},
                    {"message_id": "msg_" + "A" * 64},
                    {"ttl_seconds": None},
                )
                for number, changes in enumerate(invalid_messages, 2):
                    candidate = dict(base)
                    candidate.update(changes)
                    candidate["message_id"] = changes.get("message_id", "msg_" + f"{number:064x}")
                    candidate["dedupe_key"] = changes.get("dedupe_key", f"raw-{number}")
                    with self.subTest(message_changes=changes), self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(insert_message, candidate)

                workspace_claim = dict(base)
                workspace_claim.update(
                    message_id="msg_" + "e" * 64,
                    dedupe_key="workspace-claim",
                    scope_kind="workspace",
                    scope_identity="workspace",
                    project_id=AMIGA,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(insert_message, workspace_claim)

                cap_cases = (
                    (
                        "canonical_message_recipients",
                        "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                        ((*(("ws_alpha", "project", AMIGA, "msg_" + "a" * 64)), f"agent_r{number:03d}") for number in range(257)),
                        "canonical recipient count exceeds 256",
                    ),
                    (
                        "canonical_message_artifacts",
                        "INSERT INTO canonical_message_artifacts VALUES (?, ?, ?, ?, ?, ?)",
                        ((*(("ws_alpha", "project", AMIGA, "msg_" + "b" * 64)), "path", f"path-{number}") for number in range(257)),
                        "canonical artifact count exceeds 256",
                    ),
                    (
                        "canonical_message_tags",
                        "INSERT INTO canonical_message_tags VALUES (?, ?, ?, ?, ?)",
                        ((*(("ws_alpha", "project", AMIGA, "msg_" + "c" * 64)), f"tag-{number}") for number in range(65)),
                        "canonical tag count exceeds 64",
                    ),
                )
                for table, statement, rows, reason in cap_cases:
                    with self.subTest(cap=table):
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            with self.assertRaisesRegex(sqlite3.IntegrityError, reason):
                                connection.executemany(statement, rows)
                        finally:
                            connection.execute("ROLLBACK")

                prefix = ("ws_alpha", "project", AMIGA, "msg_" + "d" * 64)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO canonical_message_artifacts VALUES (?, ?, ?, ?, ?, ?)",
                        (*prefix, "foreign", "ref"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO canonical_message_artifacts VALUES (?, ?, ?, ?, ?, ?)",
                        (*prefix, "repo", "r" * 4097),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO canonical_message_tags VALUES (?, ?, ?, ?, ?)",
                        (*prefix, "t" * 129),
                    )

                for table in (
                    "canonical_bodies",
                    "canonical_messages",
                    "canonical_message_recipients",
                    "canonical_message_artifacts",
                    "canonical_message_tags",
                ):
                    with self.subTest(table=table, operation="update"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "append-only"
                    ):
                        connection.execute(f"UPDATE {table} SET rowid = rowid")
                    with self.subTest(table=table, operation="delete"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "append-only"
                    ):
                        connection.execute(f"DELETE FROM {table}")

    def test_v4_schema_migration_metadata_rejects_nul_on_insert_and_update(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, "ws_alpha")) as store:
                connection = store._connection
                fields = (
                    "migration_checksum",
                    "applied_at_utc",
                    "tool_version",
                    "backup_reference",
                )
                original = connection.execute(
                    "SELECT migration_checksum, applied_at_utc, tool_version, backup_reference "
                    "FROM schema_migrations WHERE version = 1"
                ).fetchone()
                for index, field in enumerate(fields):
                    with self.subTest(field=field, operation="update"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "contains NUL"
                    ):
                        connection.execute(
                            f"UPDATE schema_migrations SET {field} = ? WHERE version = 1",
                            (str(original[index]) + "\x00hidden",),
                        )
                    values = list(original)
                    values[index] = str(values[index]) + "\x00hidden"
                    with self.subTest(field=field, operation="insert"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "contains NUL"
                    ):
                        connection.execute(
                            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?, ?)",
                            (100 + index, *values),
                        )

    def test_v4_child_seals_resist_direct_sql_tricks_and_deferred_fk_rejects_orphans(self) -> None:
        from llm_collab.canonical import create_or_return_equivalent

        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, "ws_alpha")) as store:
                record_test_registry(store)
                message_id, _ = create_or_return_equivalent(
                    store,
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=AMIGA,
                    sender_agent_id="agent_codex",
                    dedupe_key="sealed",
                    body=b"body",
                    recipients=["agent_codex"],
                    registry_revision=REVISION,
                    created_at_utc=FIXED_TIME.isoformat(),
                    title="title",
                )
                connection = store._connection
                prefix = ("ws_alpha", "project", AMIGA, message_id)
                append_cases = (
                    (
                        "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                        (*prefix, "agent_claude"),
                        "canonical recipients are sealed",
                    ),
                    (
                        "INSERT INTO canonical_message_artifacts VALUES (?, ?, ?, ?, ?, ?)",
                        (*prefix, "path", "later"),
                        "canonical artifacts are sealed",
                    ),
                    (
                        "INSERT INTO canonical_message_tags VALUES (?, ?, ?, ?, ?)",
                        (*prefix, "later"),
                        "canonical tags are sealed",
                    ),
                )
                for statement, parameters, reason in append_cases:
                    with self.subTest(reason=reason), self.assertRaisesRegex(
                        sqlite3.IntegrityError, reason
                    ):
                        connection.execute(statement, parameters)

                with self.assertRaisesRegex(sqlite3.IntegrityError, "recipients are sealed"):
                    connection.execute(
                        "INSERT INTO canonical_message_recipients VALUES "
                        "(?, ?, ?, ?, ?), (?, ?, ?, ?, ?)",
                        (*prefix, "agent_claude", *prefix, "agent_reviewer"),
                    )
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM canonical_message_recipients "
                        "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
                        "AND message_id = ?",
                        prefix,
                    ).fetchone()[0],
                    1,
                )

                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement, parameters, reason in append_cases[1:]:
                        with self.assertRaisesRegex(sqlite3.IntegrityError, reason):
                            connection.execute(statement, parameters)
                finally:
                    connection.execute("ROLLBACK")

                orphan_id = "msg_" + "e" * 64
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO canonical_message_tags VALUES (?, ?, ?, ?, ?)",
                    ("ws_alpha", "project", AMIGA, orphan_id, "orphan"),
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                    connection.execute("COMMIT")
                connection.execute("ROLLBACK")
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM canonical_message_tags WHERE message_id = ?",
                        (orphan_id,),
                    ).fetchone()[0],
                    0,
                )
    def test_v4_ddl_mutations_recompute_metadata_and_break_named_properties(self) -> None:
        from llm_collab.canonical import create_or_return_equivalent

        killed = []

        nul_guard = mutate_v4("instr(body_sha256, char(0)) = 0", "1 = 1")
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), nul_guard
        ) as store:
            store._connection.execute(
                "INSERT INTO canonical_bodies VALUES (?, ?, ?, ?, ?)",
                ("ws_alpha", "a" * 63 + "\x00", 0, b"", FIXED_TIME.isoformat()),
            )
            killed.append("01_drop_nul_guard")

        missing_scope_pk = mutate_v4(
            "PRIMARY KEY (workspace_id, scope_kind, scope_identity, message_id)",
            "PRIMARY KEY (workspace_id, scope_kind, message_id)",
        )
        with TemporaryDirectory(dir="/tmp") as tmp, self.assertRaisesRegex(
            MigrationError, "verified backup was restored"
        ):
            with open_mutated_v4(
                LedgerPaths.derive(tmp, "ws_alpha"), missing_scope_pk
            ):
                pass
        killed.append("02_drop_scope_identity_from_pk")

        nullable_project_unique = mutate_v4(
            "UNIQUE (workspace_id, scope_kind, scope_identity, sender_agent_id, dedupe_key)",
            "UNIQUE (workspace_id, scope_kind, project_id, sender_agent_id, dedupe_key)",
        )
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), nullable_project_unique
        ) as store:
            record_test_registry(store)
            first, _ = create_or_return_equivalent(
                store,
                workspace_id="ws_alpha",
                scope_kind="workspace",
                scope_identity="workspace",
                sender_agent_id="agent_codex",
                dedupe_key="nullable-namespace",
                body=b"body",
                recipients=["agent_codex"],
                registry_revision=REVISION,
                created_at_utc=FIXED_TIME.isoformat(),
                title="one",
            )
            store._connection.execute(
                "INSERT INTO canonical_messages "
                "SELECT workspace_id, scope_kind, scope_identity, ?, sender_agent_id, "
                "dedupe_key, body_sha256, reply_to_message_id, ttl_seconds, ack_policy, ?, "
                "priority, chat_link, task_link, registry_revision, project_id, created_at_utc "
                "FROM canonical_messages WHERE message_id = ?",
                ("msg_" + "f" * 64, "two", first),
            )
            killed.append("03_nullable_project_uniqueness")

        no_append_guard = tuple(
            statement
            for statement in V4_SQL
            if "CREATE TRIGGER canonical_messages_no_update" not in statement
        )
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), no_append_guard
        ) as store:
            record_test_registry(store)
            message_id, _ = create_or_return_equivalent(
                store,
                workspace_id="ws_alpha",
                scope_kind="project",
                scope_identity=AMIGA,
                sender_agent_id="agent_codex",
                dedupe_key="mutable",
                body=b"body",
                recipients=["agent_codex"],
                registry_revision=REVISION,
                created_at_utc=FIXED_TIME.isoformat(),
                title="one",
            )
            store._connection.execute(
                "UPDATE canonical_messages SET title = 'two' WHERE message_id = ?",
                (message_id,),
            )
            killed.append("04_remove_append_only_trigger")

        no_count_cap = tuple(
            statement
            for statement in V4_SQL
            if "CREATE TRIGGER canonical_message_recipients_count_cap" not in statement
        )
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), no_count_cap
        ) as store:
            record_test_registry(store)
            message_id = "msg_" + "d" * 64
            prefix = ("ws_alpha", "project", AMIGA, message_id)
            store._connection.execute("BEGIN IMMEDIATE")
            try:
                store._connection.executemany(
                    "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                    ((*prefix, f"agent_r{number:03d}") for number in range(257)),
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_message_recipients WHERE message_id = ?",
                        (message_id,),
                    ).fetchone()[0],
                    257,
                )
            finally:
                store._connection.execute("ROLLBACK")
            killed.append("05_remove_count_cap_trigger")

        no_seal = tuple(
            statement
            for statement in V4_SQL
            if "CREATE TRIGGER canonical_message_recipients_sealed" not in statement
        )
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), no_seal
        ) as store:
            record_test_registry(store)
            message_id, _ = create_or_return_equivalent(
                store,
                workspace_id="ws_alpha",
                scope_kind="project",
                scope_identity=AMIGA,
                sender_agent_id="agent_codex",
                dedupe_key="unsealed",
                body=b"body",
                recipients=["agent_codex"],
                registry_revision=REVISION,
                created_at_utc=FIXED_TIME.isoformat(),
                title="one",
            )
            store._connection.execute(
                "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                ("ws_alpha", "project", AMIGA, message_id, "agent_claude"),
            )
            killed.append("14_remove_child_seal")

        immediate_recipient_fk = mutate_v4(
            "ON DELETE RESTRICT\n            DEFERRABLE INITIALLY DEFERRED",
            "ON DELETE RESTRICT",
            occurrence=1,
        )
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), immediate_recipient_fk
        ) as store:
            record_test_registry(store)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY"):
                create_or_return_equivalent(
                    store,
                    workspace_id="ws_alpha",
                    scope_kind="project",
                    scope_identity=AMIGA,
                    sender_agent_id="agent_codex",
                    dedupe_key="immediate-child-fk",
                    body=b"body",
                    recipients=["agent_codex"],
                    registry_revision=REVISION,
                    created_at_utc=FIXED_TIME.isoformat(),
                    title="one",
                )
            killed.append("15_remove_deferred_child_fk")

        no_migration_nul_guard = tuple(
            statement
            for statement in V4_SQL
            if "CREATE TRIGGER schema_migrations_no_nul_update" not in statement
        )
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), no_migration_nul_guard
        ) as store:
            store._connection.execute(
                "UPDATE schema_migrations SET tool_version = ? WHERE version = 1",
                (MIGRATION_TOOL_VERSION + "\x00hidden",),
            )
            killed.append("06_remove_schema_migrations_nul_trigger")

        child_fk = """FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED"""
        no_child_fk = mutate_v4(child_fk, "CHECK (1 = 1)", occurrence=1)
        with TemporaryDirectory(dir="/tmp") as tmp, open_mutated_v4(
            LedgerPaths.derive(tmp, "ws_alpha"), no_child_fk
        ) as store:
            store._connection.execute(
                "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                ("ws_alpha", "project", AMIGA, "msg_" + "e" * 64, "agent_codex"),
            )
            killed.append("11_drop_scope_tuple_from_child_fk")

        self.assertEqual(
            killed,
            [
                "01_drop_nul_guard",
                "02_drop_scope_identity_from_pk",
                "03_nullable_project_uniqueness",
                "04_remove_append_only_trigger",
                "05_remove_count_cap_trigger",
                "14_remove_child_seal",
                "15_remove_deferred_child_fk",
                "06_remove_schema_migrations_nul_trigger",
                "11_drop_scope_tuple_from_child_fk",
            ],
        )

    def test_mutation_09_gapped_zero_newer_and_corrupt_migrations_fail_closed(self) -> None:
        cases = ("gapped", "zero-nonempty", "newer", "corrupt-checksum")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory(dir="/tmp") as tmp:
                paths = LedgerPaths.derive(tmp, "ws_alpha")
                if case in {"gapped", "corrupt-checksum"}:
                    create_released_v3(paths)
                    with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                        if case == "gapped":
                            connection.execute("DELETE FROM schema_migrations WHERE version = 2")
                        else:
                            connection.execute(
                                "UPDATE schema_migrations SET migration_checksum = ? WHERE version = 2",
                                ("sha256:" + "f" * 64,),
                            )
                else:
                    paths.ensure_directories()
                    with closing(sqlite3.connect(paths.ledger)) as connection, connection:
                        if case == "zero-nonempty":
                            connection.execute("CREATE TABLE unexpected(value TEXT)")
                        else:
                            connection.execute("PRAGMA user_version = 6")
                before = paths.ledger.read_bytes()
                with self.assertRaises(MigrationError):
                    LedgerStore.open_writer(paths)
                self.assertEqual(paths.ledger.read_bytes(), before)

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
                    self.assertEqual(writer.schema_version(), 6)

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
                    self.assertEqual(writer.schema_version(), 6)

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
                self.assertEqual(writer.schema_version(), 6)

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
                self.assertEqual(reopened.schema_version(), 6)

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
                            connection.execute("PRAGMA user_version = 7")
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
