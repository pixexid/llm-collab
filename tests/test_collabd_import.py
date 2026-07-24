from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import llm_collab.compatibility.importer as importer_module
import llm_collab.ledger.store as store_module
from llm_collab.compatibility import LegacyImportError, import_current_provenance
from llm_collab.ledger import LedgerPaths, LedgerStore


SAFE_VERSION = (3, 51, 3)
NOW = datetime(2026, 7, 21, 21, 30, tzinfo=timezone.utc)
REVISION_HASH = "a" * 64
REVISION = f"sha256:{REVISION_HASH}"


class FakeDirectoryEntries:
    def __init__(self, total: int, *, json_at: int | None = None) -> None:
        self.total = total
        self.json_at = json_at
        self.consumed = 0
        self.name = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def __iter__(self):
        return self

    def __next__(self):
        if self.consumed == self.total:
            raise StopIteration
        self.consumed += 1
        suffix = "json" if self.consumed == self.json_at else "ignored"
        self.name = f"{self.consumed}.{suffix}"
        return self


def resolved_import_targets(source: str, module: str, package: str) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".") if package else []
            ascend = node.level - 1
            base_parts = package_parts[: len(package_parts) - ascend]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base:
            targets.add(base)
        for alias in node.names:
            if alias.name != "*":
                targets.add(".".join(part for part in (base, alias.name) if part))
    return targets


def imports_compatibility_importer(source: str, module: str, package: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(alias.name in {
                "llm_collab.compatibility",
                "llm_collab.compatibility.importer",
            } for alias in node.names):
                return True
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = package.split(".") if package else []
            ascend = node.level - 1
            base_parts = package_parts[: len(package_parts) - ascend]
            if node.module:
                base_parts.extend(node.module.split("."))
            base = ".".join(base_parts)
        else:
            base = node.module or ""
        if base == "llm_collab":
            if any(alias.name == "compatibility" for alias in node.names):
                return True
        if base == "llm_collab.compatibility":
            if any(alias.name in {"importer", "import_current_provenance"} for alias in node.names):
                return True
        if base == "llm_collab.compatibility.importer":
            return True
    return False


def module_identity(path: Path, root: Path) -> tuple[str, str]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    module = ".".join(parts)
    package = module if is_package else ".".join(parts[:-1])
    return module, package


def record_registry(store: LedgerStore) -> None:
    store.record_registry_snapshot(
        workspace_id="ws_alpha",
        registry_revision=REVISION,
        registry_source_sha256=REVISION_HASH,
        captured_at_utc=NOW.isoformat(),
        workspace_snapshot_json=json.dumps(
            {"workspace_id": "ws_alpha", "projects": ["amiga", "nuvyr"]}
        ),
        project_snapshots={
            "amiga": json.dumps({"project_id": "amiga"}),
            "nuvyr": json.dumps({"project_id": "nuvyr"}),
        },
        source_snapshots={"amiga": {}, "nuvyr": {}},
    )


def source_dirs(root: Path) -> tuple[Path, Path]:
    sessions = root / "State" / "session_autobridge" / "sessions"
    leases = root / "State" / "session_autobridge" / "activation_leases"
    sessions.mkdir(parents=True)
    leases.mkdir()
    return sessions, leases


class LegacyProvenanceImportTest(unittest.TestCase):
    def setUp(self) -> None:
        linked_version = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        linked_version.start()
        self.addCleanup(linked_version.stop)

    def test_closed_claim_extraction_hash_only_scope_and_idempotency(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, leases = source_dirs(root)
            (sessions / "exact.json").write_text(
                json.dumps({"project_id": "amiga", "identity": {"project": "nuvyr"}})
            )
            (sessions / "nested-only.json").write_text(
                json.dumps({"identity": {"project": "amiga"}})
            )
            (sessions / "duplicate.json").write_text(
                '{"project_id":"amiga","nested":{"x":1,"\\u0078":2}}'
            )
            (sessions / "malformed.json").write_bytes(b'{"project_id":')
            (sessions / "nonstandard.json").write_bytes(
                b'{"project_id":"amiga","value":NaN}'
            )
            (sessions / "foreign.json").write_text(json.dumps({"project_id": "foreign"}))
            (sessions / "null.json").write_text(json.dumps({"project_id": None}))
            (sessions / "ignored.jsonl").write_text('{"project_id":"amiga"}\n')
            nested = sessions / "nested"
            nested.mkdir()
            (nested / "ignored.json").write_text('{"project_id":"amiga"}')
            bindings = root / "State" / "session_autobridge" / "bindings"
            bindings.mkdir()
            (bindings / "ignored.json").write_text('{"project_id":"amiga"}')
            (leases / "exact.json").write_text(
                json.dumps({"project_id": "amiga", "identity": {"project": "nuvyr"}})
            )
            (leases / "fallback-refused.json").write_text(
                json.dumps({"project_id": "amiga", "identity": {"chat": "CHAT-X"}})
            )

            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                self.assertEqual(
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    ),
                    9,
                )
                self.assertEqual(
                    len(
                        store.get_legacy_provenance(
                            workspace_id="ws_alpha",
                            project_id="amiga",
                            registry_revision=REVISION,
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        store.get_legacy_provenance(
                            workspace_id="ws_alpha",
                            project_id="nuvyr",
                            registry_revision=REVISION,
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports "
                        "WHERE scope_kind = 'legacy_unscoped' AND project_id IS NULL"
                    ).fetchone()[0],
                    7,
                )
                columns = {
                    row[1]
                    for row in store._connection.execute(
                        "PRAGMA table_info(legacy_provenance_imports)"
                    )
                }
                for forbidden in (
                    "raw",
                    "json",
                    "payload",
                    "body",
                    "secret",
                    "token",
                    "pid",
                    "fence",
                    "status",
                ):
                    self.assertFalse([column for column in columns if forbidden in column])

                self.assertEqual(
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    ),
                    0,
                )
                (sessions / "exact.json").write_text(
                    json.dumps({"project_id": "amiga", "changed": True})
                )
                self.assertEqual(
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    ),
                    1,
                )

    def test_parser_recursion_failure_is_hashable_legacy_content(self) -> None:
        raw = (
            b'{"project_id":"amiga","value":'
            + b"[" * 2_000
            + b"0"
            + b"]" * 2_000
            + b"}"
        )
        with patch.object(importer_module.json, "loads", side_effect=RecursionError):
            self.assertIsNone(
                importer_module._claimed_project(raw, "session", frozenset({"amiga"}))
            )

    def test_store_preflight_rejects_before_any_filesystem_operation(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            source_dirs(root)
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                store._connection.execute("BEGIN")
                filesystem_patches = [
                    patch.object(importer_module.os, name)
                    for name in ("open", "scandir", "read", "stat", "fstat")
                ]
                mocks = [item.start() for item in filesystem_patches]
                try:
                    with self.assertRaisesRegex(RuntimeError, "transaction is active"):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                    with self.assertRaisesRegex(RuntimeError, "transaction is active"):
                        store.import_legacy_provenance(
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            import_transaction_id="d" * 32,
                            import_revision="legacy-provenance/1",
                            imported_at_utc=NOW.isoformat(),
                            records=[],
                        )
                    for filesystem_mock in mocks:
                        filesystem_mock.assert_not_called()
                finally:
                    for item in reversed(filesystem_patches):
                        item.stop()
                    store._connection.execute("ROLLBACK")

            with LedgerStore.open_reader(paths) as reader:
                filesystem_patches = [
                    patch.object(importer_module.os, name)
                    for name in ("open", "scandir", "read", "stat", "fstat")
                ]
                mocks = [item.start() for item in filesystem_patches]
                try:
                    with self.assertRaisesRegex(PermissionError, "query-only"):
                        import_current_provenance(
                            workspace_root=root,
                            store=reader,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                    for filesystem_mock in mocks:
                        filesystem_mock.assert_not_called()
                finally:
                    for item in reversed(filesystem_patches):
                        item.stop()

    def test_all_reads_precede_transaction_and_late_failure_inserts_nothing(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, leases = source_dirs(root)
            (sessions / "valid.json").write_text(json.dumps({"project_id": "amiga"}))
            outside = Path(tmp) / "outside.json"
            outside.write_text(json.dumps({"identity": {"project": "amiga"}}))
            (leases / "unsafe.json").symlink_to(outside)
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                traced: list[str] = []
                store._connection.set_trace_callback(traced.append)
                original_read = importer_module._read_stable

                def assert_pre_transaction(directory_fd: int, name: str):
                    self.assertFalse(store._connection.in_transaction)
                    self.assertFalse(any(sql == "BEGIN IMMEDIATE" for sql in traced))
                    return original_read(directory_fd, name)

                with patch.object(importer_module, "_read_stable", side_effect=assert_pre_transaction):
                    with self.assertRaisesRegex(LegacyImportError, "unsafe legacy source"):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                self.assertFalse(any(sql == "BEGIN IMMEDIATE" for sql in traced))
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    def test_filesystem_bounds_symlinks_nonregular_and_growth_fail_closed(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, _leases = source_dirs(root)
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)

                (sessions / "too-large.json").write_bytes(b"x" * 1_048_577)
                with self.assertRaisesRegex(LegacyImportError, "1048576"):
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    )
                (sessions / "too-large.json").unlink()

                (sessions / "directory.json").mkdir()
                with self.assertRaisesRegex(LegacyImportError, "non-regular"):
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    )
                (sessions / "directory.json").rmdir()

                growing = sessions / "growing.json"
                growing.write_bytes(b"x" * 70_000)
                original_read = importer_module.os.read
                changed = False

                def grow_after_first_read(fd: int, size: int) -> bytes:
                    nonlocal changed
                    chunk = original_read(fd, size)
                    if chunk and not changed:
                        changed = True
                        with growing.open("ab") as handle:
                            handle.write(b"y")
                    return chunk

                with patch.object(importer_module.os, "read", side_effect=grow_after_first_read):
                    with self.assertRaisesRegex(LegacyImportError, "changed during read"):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation unavailable")
    def test_fifo_candidate_open_is_nonblocking_and_fails_closed(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            os.mkfifo(Path(tmp) / "trap.json")
            script = """
import os
import sys
from llm_collab.compatibility.importer import LegacyImportError, _read_stable

directory_fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    try:
        _read_stable(directory_fd, "trap.json")
    except LegacyImportError:
        raise SystemExit(0)
    raise SystemExit(2)
finally:
    os.close(directory_fd)
"""
            result = subprocess.run(
                [sys.executable, "-c", script, tmp],
                cwd=Path(__file__).parents[1],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_flags_require_nonblocking_opens(self) -> None:
        self.assertEqual(importer_module.MAX_FILE_BYTES, 1048576)
        nonblock = getattr(os, "O_NONBLOCK", None)
        self.assertIsNotNone(nonblock)
        assert nonblock is not None
        self.assertEqual(importer_module._file_flags() & nonblock, nonblock)
        with patch.object(importer_module.os, "O_NONBLOCK", None):
            with self.assertRaisesRegex(LegacyImportError, "O_NONBLOCK is required"):
                importer_module._file_flags()

    def test_final_path_inode_swap_is_rejected_even_when_bytes_match(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, _leases = source_dirs(root)
            source = sessions / "swapped.json"
            raw = json.dumps({"project_id": "amiga"}).encode()
            source.write_bytes(raw)
            parked = sessions / "parked"
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                original_read = importer_module.os.read
                swapped = False

                def swap_after_read(fd: int, size: int) -> bytes:
                    nonlocal swapped
                    chunk = original_read(fd, size)
                    if chunk and not swapped:
                        swapped = True
                        source.rename(parked)
                        source.write_bytes(raw)
                    return chunk

                with patch.object(importer_module.os, "read", side_effect=swap_after_read):
                    with self.assertRaisesRegex(LegacyImportError, "changed during read"):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                self.assertTrue(swapped)
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    def test_one_common_root_rejects_alternating_ancestor_swap(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            parent = Path(tmp)
            root = parent / "workspace"
            alternate = parent / "alternate"
            parked = parent / "parked"
            root.mkdir()
            alternate.mkdir()
            root_sessions, _root_leases = source_dirs(root)
            _alternate_sessions, alternate_leases = source_dirs(alternate)
            (root_sessions / "session.json").write_text(
                json.dumps({"project_id": "amiga"})
            )
            (alternate_leases / "lease.json").write_text(
                json.dumps({"identity": {"project": "nuvyr"}})
            )
            paths = LedgerPaths.derive(parent / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                original_open = importer_module.os.open
                root_open_count = 0
                root_is_first = True

                def alternating_open(path, flags, *args, **kwargs):
                    nonlocal root_open_count, root_is_first
                    fd = original_open(path, flags, *args, **kwargs)
                    if kwargs.get("dir_fd") is None and os.fspath(path) == os.fspath(root):
                        root_open_count += 1
                        if root_is_first:
                            root.rename(parked)
                            alternate.rename(root)
                        else:
                            root.rename(alternate)
                            parked.rename(root)
                        root_is_first = not root_is_first
                    return fd

                with patch.object(importer_module.os, "open", side_effect=alternating_open):
                    with self.assertRaisesRegex(LegacyImportError, "root identity changed"):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                self.assertEqual(root_open_count, 1)
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    def test_absent_family_appearance_is_rejected_before_import(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            sessions = root / "State" / "session_autobridge" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "session.json").write_text(json.dumps({"project_id": "amiga"}))
            leases = sessions.parent / "activation_leases"
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)

                def appearing_clock() -> datetime:
                    leases.mkdir()
                    (leases / "lease.json").write_text(
                        json.dumps({"identity": {"project": "nuvyr"}})
                    )
                    return NOW

                with self.assertRaisesRegex(LegacyImportError, "component appeared"):
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=appearing_clock,
                    )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    def test_family_ancestor_symlink_and_literal_entry_cap_fail_closed(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            outside = Path(tmp) / "outside-state"
            outside.mkdir()
            (root / "State").symlink_to(outside, target_is_directory=True)
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                with self.assertRaisesRegex(LegacyImportError, "unsafe.*unreadable"):
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    )

                self.assertEqual(importer_module.MAX_FILES, 5000)
                entries = FakeDirectoryEntries(5000, json_at=5000)
                with patch.object(importer_module.os, "scandir", return_value=entries):
                    self.assertEqual(
                        importer_module._json_names(9, 5000),
                        (("5000.json",), 5000),
                    )
                self.assertEqual(entries.consumed, 5000)

                entries = FakeDirectoryEntries(5001)
                with patch.object(importer_module.os, "scandir", return_value=entries):
                    with self.assertRaisesRegex(LegacyImportError, "5000 directory entries"):
                        importer_module._json_names(9, 5000)
                self.assertEqual(entries.consumed, 5001)
                self.assertNotIn("os.listdir", inspect.getsource(importer_module))

    def test_directory_entry_budget_is_cumulative_across_source_families(self) -> None:
        self.assertEqual(importer_module.MAX_FILES, 5000)
        self.assertEqual(importer_module.MAX_FILE_BYTES, 1048576)
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, leases = source_dirs(root)
            counts = {
                sessions.stat().st_ino: 4999,
                leases.stat().st_ino: 1,
            }

            def entries_for(directory_fd: int) -> FakeDirectoryEntries:
                return FakeDirectoryEntries(counts[os.fstat(directory_fd).st_ino])

            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                with patch.object(
                    importer_module.os, "scandir", side_effect=entries_for
                ):
                    self.assertEqual(
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        ),
                        0,
                    )

                    counts[sessions.stat().st_ino] = 5000
                    with self.assertRaisesRegex(
                        LegacyImportError, "5000 directory entries"
                    ):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    def test_directory_entry_budget_counts_nested_autobridge_descent(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, _leases = source_dirs(root)
            binding_chat = (
                root
                / "State"
                / "session_autobridge"
                / "bindings"
                / "amiga"
                / "CHAT-SAMEID"
            )
            binding_chat.mkdir(parents=True)
            (binding_chat / "codex.json").write_text(
                json.dumps(
                    {
                        "project_id": "amiga",
                        "chat_id": "CHAT-SAMEID",
                        "agent_id": "codex",
                        "runtime_session_id": "codex-thread-1",
                    }
                )
            )
            original_scandir = importer_module.os.scandir
            counts = {sessions.stat().st_ino: 4997}

            def entries_for(directory_fd: int):
                count = counts.get(os.fstat(directory_fd).st_ino)
                if count is not None:
                    return FakeDirectoryEntries(count)
                return original_scandir(directory_fd)

            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                with patch.object(importer_module.os, "scandir", side_effect=entries_for):
                    self.assertEqual(
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        ),
                        1,
                    )
                    counts[sessions.stat().st_ino] = 4998
                    with self.assertRaisesRegex(LegacyImportError, "5000 directory entries"):
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        )

    def test_nested_autobridge_provenance_projects_all_four_record_kinds(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            sessions, leases = source_dirs(root)
            bridge = root / "State" / "session_autobridge"
            bindings = bridge / "bindings" / "amiga" / "CHAT-SAMEID"
            pairs = bridge / "thread_pairs" / "amiga" / "CHAT-SAMEID"
            bindings.mkdir(parents=True)
            pairs.mkdir(parents=True)
            (sessions / "session.json").write_text(json.dumps({"project_id": "amiga"}))
            (leases / "lease.json").write_text(
                json.dumps({"identity": {"project": "amiga"}})
            )
            (bindings / "codex.json").write_text(
                json.dumps(
                    {
                        "project_id": "amiga",
                        "chat_id": "CHAT-SAMEID",
                        "agent_id": "codex",
                        "runtime_session_id": "codex-thread-1",
                    }
                )
            )
            (pairs / "claude__codex.json").write_text(
                json.dumps(
                    {
                        "project_id": "amiga",
                        "chat_id": "CHAT-SAMEID",
                        "agents": ["claude", "codex"],
                        "sessions": {
                            "claude": "claude-thread-1",
                            "codex": "codex-thread-1",
                        },
                    }
                )
            )
            paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                self.assertEqual(
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=lambda: NOW,
                    ),
                    4,
                )
                projected = store.get_legacy_provenance(
                    workspace_id="ws_alpha",
                    project_id="amiga",
                    registry_revision=REVISION,
                )
                self.assertEqual(
                    {row["record_kind"] for row in projected},
                    {"session", "activation_lease", "binding", "thread_pair"},
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_provenance_imports"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_autobridge_provenance_imports"
                    ).fetchone()[0],
                    2,
                )
                for table in (
                    "conversation_participants",
                    "conversation_bindings",
                    "session_binding_challenges",
                    "conversation_binding_transition_audit",
                ):
                    self.assertEqual(
                        store._connection.execute(
                            f'SELECT count(*) FROM "{table}"'
                        ).fetchone()[0],
                        0,
                    )

    def test_nested_autobridge_path_payload_mismatch_stays_unscoped(self) -> None:
        cases = (
            (
                "binding-project",
                ("bindings", "amiga", "CHAT-SAMEID", "codex.json"),
                {
                    "project_id": "nuvyr",
                    "chat_id": "CHAT-SAMEID",
                    "agent_id": "codex",
                    "runtime_session_id": "codex-thread-1",
                },
            ),
            (
                "binding-chat",
                ("bindings", "amiga", "CHAT-SAMEID", "codex.json"),
                {
                    "project_id": "amiga",
                    "chat_id": "CHAT-OTHER",
                    "agent_id": "codex",
                    "runtime_session_id": "codex-thread-1",
                },
            ),
            (
                "binding-agent",
                ("bindings", "amiga", "CHAT-SAMEID", "codex.json"),
                {
                    "project_id": "amiga",
                    "chat_id": "CHAT-SAMEID",
                    "agent_id": "claude",
                    "runtime_session_id": "codex-thread-1",
                },
            ),
            (
                "thread-pair-agents",
                ("thread_pairs", "amiga", "CHAT-SAMEID", "claude__codex.json"),
                {
                    "project_id": "amiga",
                    "chat_id": "CHAT-SAMEID",
                    "agents": ["claude", "cdx2"],
                    "sessions": {
                        "claude": "claude-thread-1",
                        "cdx2": "cdx2-thread-1",
                    },
                },
            ),
        )
        for case, relative, payload in cases:
            with self.subTest(case=case), TemporaryDirectory(dir="/tmp") as tmp:
                root = Path(tmp) / "workspace"
                root.mkdir()
                source_dirs(root)
                path = root / "State" / "session_autobridge" / Path(*relative)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(payload))
                paths = LedgerPaths.derive(Path(tmp) / "ledger-state", "ws_alpha")
                with LedgerStore.open_writer(paths) as store:
                    record_registry(store)
                    self.assertEqual(
                        import_current_provenance(
                            workspace_root=root,
                            store=store,
                            workspace_id="ws_alpha",
                            registry_revision=REVISION,
                            clock=lambda: NOW,
                        ),
                        1,
                    )
                    self.assertEqual(
                        store.get_legacy_provenance(
                            workspace_id="ws_alpha",
                            project_id="amiga",
                            registry_revision=REVISION,
                        ),
                        [],
                    )
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT count(*) FROM legacy_autobridge_provenance_imports "
                            "WHERE scope_kind = 'legacy_unscoped' AND project_id IS NULL"
                        ).fetchone()[0],
                        1,
                    )

    def test_midlevel_autobridge_chat_directory_swap_fails_closed(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            parent = Path(tmp)
            root = parent / "workspace"
            root.mkdir()
            source_dirs(root)
            chat_dir = (
                root
                / "State"
                / "session_autobridge"
                / "bindings"
                / "amiga"
                / "CHAT-SAMEID"
            )
            chat_dir.mkdir(parents=True)
            payload = {
                "project_id": "amiga",
                "chat_id": "CHAT-SAMEID",
                "agent_id": "codex",
                "runtime_session_id": "codex-thread-1",
            }
            (chat_dir / "codex.json").write_text(json.dumps(payload))
            parked = parent / "parked-chat"
            replacement = parent / "replacement-chat"
            replacement.mkdir()
            (replacement / "codex.json").write_text(json.dumps(payload))
            paths = LedgerPaths.derive(parent / "ledger-state", "ws_alpha")
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                swapped = False

                def swap_clock() -> datetime:
                    nonlocal swapped
                    chat_dir.rename(parked)
                    replacement.rename(chat_dir)
                    swapped = True
                    return NOW

                with self.assertRaisesRegex(LegacyImportError, "component identity changed"):
                    import_current_provenance(
                        workspace_root=root,
                        store=store,
                        workspace_id="ws_alpha",
                        registry_revision=REVISION,
                        clock=swap_clock,
                    )
                self.assertTrue(swapped)
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM legacy_autobridge_provenance_imports"
                    ).fetchone()[0],
                    0,
                )

    def test_compatibility_importer_has_no_runtime_consumer_or_v2_authority_import(self) -> None:
        source = inspect.getsource(importer_module)
        self.assertNotIn("_session_autobridge", source)
        self.assertNotIn("_activation_lease", source)
        self.assertNotIn("raw_json", source)
        root = Path(__file__).parents[1]
        examples = (
            (
                "from ..compatibility import import_current_provenance",
                "llm_collab.daemon.server",
                "llm_collab.daemon",
            ),
            (
                "from . import compatibility",
                "llm_collab.server",
                "llm_collab",
            ),
            (
                "from llm_collab import compatibility",
                "bin.command",
                "bin",
            ),
            ("import llm_collab.compatibility", "bin.command", "bin"),
        )
        for candidate, module, package in examples:
            self.assertTrue(imports_compatibility_importer(candidate, module, package))
        self.assertTrue(
            imports_compatibility_importer(
                "import llm_collab.compatibility.importer",
                "llm_collab.candidate",
                "llm_collab",
            )
        )
        self.assertFalse(
            imports_compatibility_importer(
                "from llm_collab.compatibility import projection",
                "llm_collab.candidate",
                "llm_collab",
            )
        )
        self.assertFalse(
            imports_compatibility_importer(
                "import llm_collab.compatibility.projection",
                "llm_collab.candidate",
                "llm_collab",
            )
        )

        consumers = []
        production_paths = []
        for directory in ("bin", "scripts", "tools", "pm2", "llm_collab"):
            production_paths.extend(
                path
                for path in root.joinpath(directory).rglob("*")
                if path.is_file() and "llm_collab/compatibility" not in path.as_posix()
            )
        for path in production_paths:
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if path.suffix == ".py":
                module, package = module_identity(path, root)
                imports_compatibility = imports_compatibility_importer(
                    text,
                    module,
                    package,
                )
            else:
                imports_compatibility = (
                    "llm_collab.compatibility.importer" in text
                    or "llm_collab.compatibility import importer" in text
                    or "llm_collab.compatibility import import_current_provenance"
                    in text
                )
            if imports_compatibility:
                consumers.append(str(path.relative_to(root)))
        self.assertEqual(consumers, [])


if __name__ == "__main__":
    unittest.main()
