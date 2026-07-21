from __future__ import annotations

import gc
import inspect
import json
import re
import sqlite3
import threading
import unittest
import warnings
import weakref
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.ledger import LedgerPaths, LedgerStore, SQLiteSafetyError, WriterAlreadyOpenError
from llm_collab.ledger.store import (
    BUSY_TIMEOUT_MS,
    MIGRATION_TOOL_VERSION,
    SYNCHRONOUS_FULL,
    V1_MIGRATION_CHECKSUM,
    V1_SCHEMA_FINGERPRINT,
    MigrationError,
    V1_SQL,
    _migration_checksum,
    _v1_schema_fingerprint_from_sql,
    require_safe_sqlite,
)


SAFE_VERSION = (3, 51, 3)
FIXED_TIME = datetime(2026, 7, 21, 8, 5, 6, 123456, tzinfo=timezone.utc)
AMIGA = "amiga"
NUVYR = "nuvyr"


class LedgerStoreTest(unittest.TestCase):
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
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION):
                pass
            with LedgerStore.open_reader(paths, sqlite_version_info=SAFE_VERSION) as reader:
                self.assertEqual(
                    reader._connection.execute("PRAGMA synchronous").fetchone()[0],
                    SYNCHRONOUS_FULL,
                )

    def test_sqlite_wal_safety_gate_is_exact_and_fails_closed(self) -> None:
        for accepted in ((3, 44, 6), (3, 50, 7), (3, 51, 3), (3, 52, 0), (4, 0, 0)):
            with self.subTest(accepted=accepted):
                self.assertEqual(require_safe_sqlite(accepted), accepted)
        for rejected in ((3, 44, 5), (3, 50, 6), (3, 51, 1), (3, 51, 2), (3, 43, 99)):
            with self.subTest(rejected=rejected):
                with self.assertRaisesRegex(SQLiteSafetyError, "unsafe for WAL.*safety fix"):
                    require_safe_sqlite(rejected)
        with self.assertRaises(SQLiteSafetyError):
            require_safe_sqlite((3, 51, True))
        if sqlite3.sqlite_version_info == (3, 51, 1):
            with self.assertRaisesRegex(SQLiteSafetyError, "unsafe for WAL"):
                require_safe_sqlite()

    def test_v1_schema_connection_guards_backup_and_private_permissions(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            state = Path(tmp) / "existing-state"
            state.mkdir(mode=0o755)
            paths = LedgerPaths.derive(state, "ws_alpha")
            with LedgerStore.open_writer(
                paths,
                sqlite_version_info=SAFE_VERSION,
                clock=lambda: FIXED_TIME,
            ) as store:
                connection = store._connection
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 0)
                self.assertEqual(store.schema_version(), 1)
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
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(tables, expected)
                self.assertNotIn("observations", tables)
                self.assertNotIn("checkpoints", tables)
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
            backups = list(paths.backups.iterdir())
            self.assertEqual([path.name for path in backups], ["ledger-0-20260721T080506123456Z.sqlite3"])
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(backups[0]) as backup:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            with sqlite3.connect(paths.ledger) as ledger:
                self.assertEqual(
                    ledger.execute(
                        "SELECT migration_checksum, applied_at_utc, tool_version, backup_reference "
                        "FROM schema_migrations"
                    ).fetchone(),
                    (
                        V1_MIGRATION_CHECKSUM,
                        FIXED_TIME.isoformat(),
                        MIGRATION_TOOL_VERSION,
                        backups[0].name,
                    ),
                )
            self.assertEqual(_migration_checksum(V1_SQL), V1_MIGRATION_CHECKSUM)
            self.assertEqual(_v1_schema_fingerprint_from_sql(), V1_SCHEMA_FINGERPRINT)

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
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION) as writer:
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

            with LedgerStore.open_reader(paths, sqlite_version_info=SAFE_VERSION) as reader:
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
                LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION)
            self.assertEqual(outside.read_bytes(), b"unchanged")
            self.assertFalse(paths.ledger.exists())

    def test_foreign_keys_prevent_cross_scope_source_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION) as store:
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
                with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION):
                    pass
                with sqlite3.connect(paths.ledger) as connection:
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
                before = paths.ledger.read_bytes()

                with self.assertRaisesRegex(MigrationError, "foreign_key_check"):
                    opener(paths, sqlite_version_info=SAFE_VERSION)
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
                        paths, sqlite_version_info=SAFE_VERSION, clock=lambda: FIXED_TIME
                    ):
                        pass
                    with sqlite3.connect(paths.ledger) as connection:
                        connection.executescript(script)
                    before = paths.ledger.read_bytes()

                    with self.assertRaisesRegex(MigrationError, expected):
                        opener(paths, sqlite_version_info=SAFE_VERSION)
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
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION) as writer:
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
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION) as writer:
                with self.assertRaises(WriterAlreadyOpenError):
                    LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION)
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
                self.assertEqual(writer.schema_version(), 1)

            with LedgerStore.open_reader(paths, sqlite_version_info=SAFE_VERSION) as reader:
                self.assertEqual(reader._connection.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(PermissionError):
                    reader.checkpoint()
                with self.assertRaises(sqlite3.OperationalError):
                    reader._connection.execute("CREATE TABLE forbidden_write(value TEXT)")

    def test_writer_lock_is_idempotent_and_released_when_store_is_abandoned(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, "ws_alpha")
            writer = LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION)
            writer.close()
            writer.close()
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION):
                pass

            abandoned = LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION)
            store_reference = weakref.ref(abandoned)
            lock_reference = weakref.ref(abandoned._writer_lock)
            del abandoned
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                gc.collect()
            self.assertIsNone(store_reference())
            self.assertIsNone(lock_reference())
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION) as reopened:
                self.assertEqual(reopened.schema_version(), 1)

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
                        sqlite_version_info=SAFE_VERSION,
                        clock=lambda: FIXED_TIME,
                        migrations=broken,
                    )
            self.assertEqual(observed_transaction_state, [False])
            with sqlite3.connect(paths.ledger) as restored:
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
                with sqlite3.connect(paths.ledger) as connection:
                    connection.execute("PRAGMA user_version = 1")
                before = paths.ledger.read_bytes()

                with self.assertRaisesRegex(MigrationError, "corrupt or incoherent"):
                    opener(paths, sqlite_version_info=SAFE_VERSION)
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
            with LedgerStore.open_writer(paths, sqlite_version_info=SAFE_VERSION):
                pass
            with sqlite3.connect(paths.ledger) as connection:
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
                        with sqlite3.connect(paths.ledger) as connection:
                            connection.execute("PRAGMA user_version = 2")
                        expected = "unsupported ledger schema version"
                    else:
                        if kind == "corrupt":
                            paths.ledger.write_bytes(b"not a sqlite database")
                            expected = "corrupt"
                        elif kind == "extra-table":
                            with LedgerStore.open_writer(
                                paths, sqlite_version_info=SAFE_VERSION
                            ):
                                pass
                            with sqlite3.connect(paths.ledger) as connection:
                                connection.execute("CREATE TABLE unsupported_extra(value TEXT)")
                            expected = "table set is incoherent"
                        else:
                            with LedgerStore.open_writer(
                                paths, sqlite_version_info=SAFE_VERSION
                            ):
                                pass
                            with sqlite3.connect(paths.ledger) as connection:
                                connection.execute(
                                    "UPDATE schema_migrations SET tool_version = 'changed'"
                                )
                            expected = "migration metadata is incoherent"
                    before = paths.ledger.read_bytes()

                    with self.assertRaisesRegex(MigrationError, expected):
                        opener(paths, sqlite_version_info=SAFE_VERSION)
                    self.assertEqual(paths.ledger.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
