from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import llm_collab.daemon.observe as observe_module
import llm_collab.ledger.store as store_module
from llm_collab.daemon.observe import (
    ObservationEngine,
    ObservationError,
    _candidate,
    _open_workspace_file,
    scan_mailbox,
)
from llm_collab.daemon.registry import SOURCE_ID, read_registry_snapshot
from llm_collab.daemon.server import RESPONSE_LIMIT, DaemonServer
from llm_collab.ledger import LedgerPaths, LedgerStore


SAFE_VERSION = (3, 51, 3)
START = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


def packet(project_id: str | None, body: str = "secret body") -> bytes:
    if project_id is None:
        return body.encode()
    return f"---\nproject_id: {project_id}\n---\n{body}\n".encode()


class MutableClock:
    def __init__(self) -> None:
        self.monotonic_value = 0.0
        self.wall_value = START

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> datetime:
        return self.wall_value


class ObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        version = patch.object(
            store_module,
            "_linked_sqlite_version_info",
            return_value=SAFE_VERSION,
        )
        version.start()
        self.addCleanup(version.stop)
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "Chats").mkdir()
        (self.root / "agents").mkdir()
        self.projects = self.root / "projects.json"
        self.projects.write_text(
            json.dumps({"projects": [{"id": "amiga"}, {"id": "nuvyr"}]})
        )
        self.paths = LedgerPaths.derive(Path(self.tmp.name) / "state", "ws_alpha")

    def make_chat(self, name: str, filename: str, raw: bytes) -> Path:
        directory = self.root / "Chats" / name
        directory.mkdir(exist_ok=True)
        path = directory / filename
        path.write_bytes(raw)
        return path

    def snapshot(self, store: LedgerStore):
        value = read_registry_snapshot(
            self.projects,
            workspace_id="ws_alpha",
            clock=lambda: START,
        )
        value.record(store)
        return value

    def candidate_for(self, path: str = "Chats/chat/item.md") -> dict[str, object]:
        raw = b"hash-only"
        return _candidate(
            path,
            raw,
            SimpleNamespace(st_mtime_ns=1),
            scan_cursor='{"after":"Chats/chat/item.md"}',
            scan_count=1,
        )

    def set_projects(self, *project_ids: str) -> None:
        self.projects.write_text(
            json.dumps({"projects": [{"id": project_id} for project_id in project_ids]})
        )

    def scheduler_candidate(
        self, project_id: str, serial: str, *, scan_count: int
    ) -> dict[str, object]:
        return _candidate(
            f"Chats/{project_id}/{serial}.md",
            f"{project_id}:{serial}".encode(),
            SimpleNamespace(st_mtime_ns=scan_count),
            scan_cursor=f"{project_id}:{serial}:cursor",
            scan_count=scan_count,
        )

    def observation_counts(self, store: LedgerStore) -> dict[str, int]:
        return dict(
            store._connection.execute(
                "SELECT project_id, count(*) FROM observations GROUP BY project_id"
            ).fetchall()
        )

    def audit_counts(self, store: LedgerStore) -> dict[str, int]:
        return dict(
            store._connection.execute(
                "SELECT action, count(*) FROM observation_audit GROUP BY action"
            ).fetchall()
        )

    def test_fixed_sources_filter_exact_project_and_store_hash_metadata_only(self) -> None:
        amiga = self.make_chat("chat-a", "amiga.md", packet("amiga", "AMIGA-RAW-SECRET"))
        self.make_chat("chat-a", "nuvyr.md", packet("nuvyr", "NUVYR-RAW-SECRET"))
        self.make_chat("chat-a", "missing.md", packet(None))
        self.make_chat("chat-a", "empty.md", packet("null"))
        conflict = self.make_chat("chat-b", "conflict.md", packet("nuvyr"))
        (conflict.parent / "meta.json").write_text('{"project_id":"amiga"}')
        inbox = self.root / "agents" / "codex"
        inbox.mkdir()
        (inbox / "inbox.json").write_text(
            json.dumps(
                {
                    "read": ["Chats/chat-a/amiga.md", "Chats/chat-a/nuvyr.md"],
                    "unread": [
                        "README.md",
                        "../escape.md",
                        None,
                    ],
                }
            )
        )

        amiga_rows, amiga_cursor, amiga_scanned = scan_mailbox(
            self.root,
            project_id="amiga",
            registry_revision="sha256:" + "a" * 64,
            cursor="",
        )
        nuvyr_rows, _cursor, _scanned = scan_mailbox(
            self.root,
            project_id="nuvyr",
            registry_revision="sha256:" + "a" * 64,
            cursor="",
        )

        self.assertEqual(amiga_cursor, "")
        self.assertLessEqual(amiga_scanned, 2000)
        self.assertEqual([row["path"] for row in amiga_rows].count("Chats/chat-a/amiga.md"), 1)
        self.assertEqual([row["path"] for row in amiga_rows].count("agents/codex/inbox.json"), 1)
        self.assertNotIn("Chats/chat-a/nuvyr.md", {row["path"] for row in amiga_rows})
        self.assertEqual([row["path"] for row in nuvyr_rows].count("Chats/chat-a/nuvyr.md"), 1)
        self.assertEqual([row["path"] for row in nuvyr_rows].count("agents/codex/inbox.json"), 1)
        direct = next(row for row in amiga_rows if row["path"] == "Chats/chat-a/amiga.md")
        self.assertEqual(direct["content_sha256"], hashlib.sha256(amiga.read_bytes()).hexdigest())
        self.assertEqual(
            set(direct),
            {
                "dedupe_key",
                "path",
                "content_sha256",
                "byte_size",
                "mtime_ns",
                "scan_cursor",
                "scan_count",
            },
        )
        self.assertNotIn("AMIGA-RAW-SECRET", json.dumps(amiga_rows))

    def test_chat_metadata_matches_but_conflicts_and_invalid_ids_fail_closed(self) -> None:
        inherited = self.make_chat("meta-chat", "inherited.md", packet(None))
        (inherited.parent / "meta.json").write_text('{"project_id":"amiga"}')
        self.make_chat("meta-chat", "conflict.md", packet("nuvyr"))
        self.make_chat(
            "meta-chat",
            "duplicate-frontmatter.md",
            b"---\nproject_id: amiga\nproject_id: amiga\n---\nbody\n",
        )
        self.make_chat(
            "meta-chat",
            "invalid-frontmatter.md",
            b"---\nproject_id: ../amiga\n---\nbody\n",
        )
        self.make_chat(
            "meta-chat",
            "null-frontmatter.md",
            b"---\nproject_id: null\n---\nbody\n",
        )
        self.make_chat(
            "meta-chat",
            "unclosed-frontmatter.md",
            b"---\nproject_id: amiga\nbody without closing delimiter\n",
        )
        self.make_chat("broken", "duplicate.md", packet(None))
        (self.root / "Chats" / "broken" / "meta.json").write_text(
            '{"project_id":"amiga","project_id":"amiga"}'
        )
        duplicate_foreign = self.make_chat(
            "duplicate-foreign-meta", "packet.md", packet("amiga")
        )
        (duplicate_foreign.parent / "meta.json").write_text(
            '{"project_id":"nuvyr","project_id":"nuvyr"}'
        )
        indented_foreign = self.make_chat(
            "indented-foreign-frontmatter",
            "packet.md",
            b"---\n  project_id: nuvyr\n---\nbody\n",
        )
        (indented_foreign.parent / "meta.json").write_text(
            '{"project_id":"amiga"}'
        )
        for name, metadata in (
            ("malformed-meta", '{"project_id":'),
            ("null-meta", '{"project_id":null}'),
            ("empty-meta", '{"project_id":""}'),
        ):
            source = self.make_chat(name, "packet.md", packet("amiga"))
            (source.parent / "meta.json").write_text(metadata)
        missing_meta_key = self.make_chat(
            "missing-meta-key", "packet.md", packet("amiga")
        )
        (missing_meta_key.parent / "meta.json").write_text('{"topic":"safe"}')

        rows, _cursor, _scanned = scan_mailbox(
            self.root,
            project_id="amiga",
            registry_revision="sha256:" + "b" * 64,
            cursor="",
        )
        paths = {row["path"] for row in rows}
        self.assertIn("Chats/meta-chat/inherited.md", paths)
        self.assertNotIn("Chats/meta-chat/conflict.md", paths)
        self.assertNotIn("Chats/meta-chat/duplicate-frontmatter.md", paths)
        self.assertNotIn("Chats/meta-chat/invalid-frontmatter.md", paths)
        self.assertNotIn("Chats/meta-chat/null-frontmatter.md", paths)
        self.assertNotIn("Chats/meta-chat/unclosed-frontmatter.md", paths)
        self.assertNotIn("Chats/broken/duplicate.md", paths)
        self.assertNotIn("Chats/duplicate-foreign-meta/packet.md", paths)
        self.assertNotIn("Chats/indented-foreign-frontmatter/packet.md", paths)
        self.assertNotIn("Chats/malformed-meta/packet.md", paths)
        self.assertNotIn("Chats/null-meta/packet.md", paths)
        self.assertNotIn("Chats/empty-meta/packet.md", paths)
        self.assertIn("Chats/missing-meta-key/packet.md", paths)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_anchored_reader_refuses_symlink_ancestor_fifo_and_escape_without_blocking(self) -> None:
        safe = self.make_chat("safe", "packet.md", packet("amiga"))
        linked = self.root / "Chats" / "linked"
        linked.symlink_to(safe.parent, target_is_directory=True)
        fifo = self.root / "agents" / "fifo"
        os.mkfifo(fifo)
        for path in ("Chats/linked/packet.md", "agents/fifo", "../projects.json"):
            with self.subTest(path=path), self.assertRaises(ObservationError):
                _open_workspace_file(self.root, path)

    def test_dedupe_ignores_mtime_and_size_but_not_content_or_path(self) -> None:
        first = _candidate(
            "Chats/a/item.md",
            b"same",
            SimpleNamespace(st_mtime_ns=1),
            scan_cursor="one",
            scan_count=1,
        )
        metadata_changed = _candidate(
            "Chats/a/item.md",
            b"same",
            SimpleNamespace(st_mtime_ns=999),
            scan_cursor="two",
            scan_count=2,
        )
        content_changed = _candidate(
            "Chats/a/item.md",
            b"changed",
            SimpleNamespace(st_mtime_ns=999),
            scan_cursor="three",
            scan_count=3,
        )
        path_changed = _candidate(
            "Chats/b/item.md",
            b"same",
            SimpleNamespace(st_mtime_ns=1),
            scan_cursor="four",
            scan_count=4,
        )
        self.assertEqual(first["dedupe_key"], metadata_changed["dedupe_key"])
        self.assertNotEqual(first["dedupe_key"], content_changed["dedupe_key"])
        self.assertNotEqual(first["dedupe_key"], path_changed["dedupe_key"])

    def test_signed_mtime_from_real_packet_is_observed_and_bounded(self) -> None:
        source = self.make_chat("signed-mtime", "packet.md", packet("amiga"))
        os.utime(source, ns=(-1, -1))
        self.assertEqual(source.stat().st_mtime_ns, -1)

        rows, cursor, _scanned = scan_mailbox(
            self.root,
            project_id="amiga",
            registry_revision="sha256:" + "9" * 64,
            cursor="",
        )
        candidate = next(
            row for row in rows if row["path"] == "Chats/signed-mtime/packet.md"
        )
        self.assertEqual(candidate["mtime_ns"], -1)
        self.assertEqual(cursor, "")

        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            candidate["scan_cursor"] = ""
            store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=[candidate],
                next_cursor="",
                scanned_count=int(candidate["scan_count"]),
                observed_at_utc=START.isoformat(),
            )
            stored = store._connection.execute(
                "SELECT mtime_ns FROM observations WHERE project_id = 'amiga'"
            ).fetchone()[0]
            self.assertEqual(stored, -1)

        for invalid in (-(1 << 63) - 1, 1 << 63):
            with self.subTest(invalid=invalid), self.assertRaises(ObservationError):
                _candidate(
                    "Chats/signed-mtime/packet.md",
                    b"body",
                    SimpleNamespace(st_mtime_ns=invalid),
                    scan_cursor="",
                    scan_count=1,
                )

    def test_filtered_work_is_bounded_and_checkpoint_cursor_advances(self) -> None:
        chat = self.root / "Chats" / "large"
        chat.mkdir()
        for number in range(1_050):
            (chat / f"{number:04d}.md").write_bytes(packet("nuvyr"))
        real_open = observe_module._open_workspace_file
        with patch.object(
            observe_module,
            "_open_workspace_file",
            wraps=real_open,
        ) as opened:
            rows, cursor, scanned = scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="sha256:" + "c" * 64,
                cursor="",
            )
        self.assertEqual(rows, [])
        self.assertEqual(scanned, 2000)
        self.assertLessEqual(opened.call_count, 2000)
        self.assertGreater(opened.call_count, 0)
        self.assertNotEqual(cursor, "")

        with patch.object(
            observe_module,
            "_open_workspace_file",
            wraps=real_open,
        ) as resumed:
            rows, next_cursor, scanned = scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="sha256:" + "c" * 64,
                cursor=cursor,
            )
        self.assertEqual(rows, [])
        self.assertEqual(next_cursor, "")
        self.assertLessEqual(scanned, 2000)
        self.assertLessEqual(resumed.call_count, 2000)

    def test_every_nonmatching_dirent_is_bounded_and_mutation_resets_resume_safely(self) -> None:
        chat = self.root / "Chats" / "nonmatching"
        chat.mkdir()
        for number in range(2_100):
            (chat / f"ignored-{number:04d}.txt").write_text("not a packet")
        with patch.object(
            observe_module,
            "_open_workspace_file",
            wraps=observe_module._open_workspace_file,
        ) as opened:
            rows, cursor, scanned = scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="sha256:" + "d" * 64,
                cursor="",
            )
        self.assertEqual(rows, [])
        self.assertEqual(scanned, 2000)
        self.assertEqual(opened.call_count, 0)
        self.assertNotEqual(cursor, "")

        # A directory mutation can reorder native enumeration. Its identity in
        # the opaque cursor changes, so resume restarts that directory instead
        # of applying a stale path-order watermark that could skip this packet.
        (chat / "new-earlier-entry.md").write_bytes(packet("amiga", "new"))
        observed: set[str] = set()
        for _pass in range(3):
            rows, cursor, scanned = scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="sha256:" + "d" * 64,
                cursor=cursor,
            )
            self.assertLessEqual(scanned, 2000)
            observed.update(str(row["path"]) for row in rows)
            if cursor == "":
                break
        self.assertIn("Chats/nonmatching/new-earlier-entry.md", observed)
        self.assertEqual(cursor, "")

    def test_cursor_json_is_closed_duplicate_free_and_identity_validated(self) -> None:
        chat = self.root / "Chats" / "cursor"
        chat.mkdir()
        for number in range(2_100):
            (chat / f"ignored-{number:04d}.txt").write_text("x")
        _rows, cursor, _scanned = scan_mailbox(
            self.root,
            project_id="amiga",
            registry_revision="sha256:" + "e" * 64,
            cursor="",
        )
        self.assertNotEqual(cursor, "")
        duplicate = cursor[:-1] + ',"v":1}'
        with patch.object(observe_module.os, "open", wraps=os.open) as opener:
            with self.assertRaises(ObservationError):
                scan_mailbox(
                    self.root,
                    project_id="amiga",
                    registry_revision="sha256:" + "e" * 64,
                    cursor=duplicate,
                )
            opener.assert_not_called()
        with self.assertRaises(ObservationError):
            scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="not-a-revision",
                cursor="",
            )
        oversized = json.loads(cursor)
        oversized["r"][3] = 10**200
        with self.assertRaises(ObservationError):
            scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="sha256:" + "e" * 64,
                cursor=json.dumps(oversized, separators=(",", ":")),
            )
        chats = (self.root / "Chats").stat()
        stale_cookie = json.dumps(
            {
                "v": 1,
                "p": "c",
                "r": [
                    chats.st_dev,
                    chats.st_ino,
                    chats.st_mtime_ns,
                    (1 << 63) - 1,
                    "",
                ],
            },
            separators=(",", ":"),
        )
        rows, reset_cursor, scanned = scan_mailbox(
            self.root,
            project_id="amiga",
            registry_revision="sha256:" + "e" * 64,
            cursor=stale_cookie,
        )
        self.assertEqual(rows, [])
        self.assertLessEqual(scanned, 2000)
        self.assertNotEqual(reset_cursor, stale_cookie)

    def test_cursor_accepts_signed_directory_mtime_identity(self) -> None:
        fd = os.open(self.root / "Chats", os.O_RDONLY)
        self.addCleanup(os.close, fd)
        actual = os.fstat(fd)
        state = [actual.st_dev, actual.st_ino, -1, 0, ""]
        fake = SimpleNamespace(
            st_dev=actual.st_dev,
            st_ino=actual.st_ino,
            st_mtime_ns=-1,
        )
        with patch.object(observe_module.os, "fstat", return_value=fake):
            reader = observe_module._DirectoryReader(fd, state)
        self.assertTrue(reader.resumed)
        reader.fd = -1

    def test_utf8_native_cursor_boundary_stays_within_four_kibibytes(self) -> None:
        root = Mock()
        root.state.return_value = [1, 2, 3, 4, observe_module._pack_names([b"r" * 210])]
        child = Mock()
        child.state.return_value = [5, 6, 7, 8, observe_module._pack_names([b"c" * 1_008])]
        cursor = observe_module._encode_walk_cursor(
            "c",
            root,
            child_name="é" * 400,
            child=child,
        )
        self.assertLessEqual(len(cursor.encode("utf-8")), 4_096)
        self.assertEqual(json.loads(cursor)["d"][0], "é" * 400)

    def test_initial_native_enumeration_failure_propagates_and_closes_descriptors(self) -> None:
        fd_root = Path("/dev/fd") if Path("/dev/fd").is_dir() else Path("/proc/self/fd")
        before = len(list(fd_root.iterdir()))
        with patch.object(
            observe_module,
            "_directory_block",
            side_effect=ObservationError("initial enumeration failed"),
        ), self.assertRaisesRegex(ObservationError, "initial enumeration failed"):
            scan_mailbox(
                self.root,
                project_id="amiga",
                registry_revision="sha256:" + "e" * 64,
                cursor="",
            )
        self.assertEqual(len(list(fd_root.iterdir())), before)

    def test_atomic_failure_rolls_back_and_retry_dedupes(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            candidate = self.candidate_for()

            def fail(stage: str) -> None:
                if stage == "after_checkpoint":
                    raise RuntimeError("crash")

            with self.assertRaisesRegex(RuntimeError, "crash"):
                store.reconcile_observations(
                    workspace_id="ws_alpha",
                    project_id="amiga",
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    candidates=[candidate],
                    next_cursor="complete",
                    scanned_count=1,
                    observed_at_utc=START.isoformat(),
                    failpoint=fail,
                )
            for table in ("observations", "observation_checkpoints", "observation_audit"):
                self.assertEqual(
                    store._connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0],
                    0,
                )

            result = store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=[candidate],
                next_cursor="complete",
                scanned_count=1,
                observed_at_utc=START.isoformat(),
            )
            duplicate = store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=[candidate],
                next_cursor="complete",
                scanned_count=1,
                observed_at_utc=(START + timedelta(seconds=1)).isoformat(),
            )
            self.assertEqual(result["written"], 1)
            self.assertEqual(duplicate["written"], 0)
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                1,
            )

    def test_project_and_registry_revision_are_part_of_dedupe_scope(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            first = self.snapshot(store)
            candidate = self.candidate_for()
            for project_id in ("amiga", "nuvyr"):
                store.reconcile_observations(
                    workspace_id="ws_alpha",
                    project_id=project_id,
                    source_id=SOURCE_ID,
                    registry_revision=first.registry_revision,
                    candidates=[candidate],
                    next_cursor="",
                    scanned_count=1,
                    observed_at_utc=START.isoformat(),
                )
            self.projects.write_text(
                json.dumps({"_comment": "revision two", "projects": [{"id": "amiga"}, {"id": "nuvyr"}]})
            )
            second = self.snapshot(store)
            store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=second.registry_revision,
                candidates=[candidate],
                next_cursor="",
                scanned_count=1,
                observed_at_utc=START.isoformat(),
            )
            scopes = store._connection.execute(
                "SELECT project_id, registry_revision FROM observations ORDER BY project_id, registry_revision"
            ).fetchall()
            self.assertEqual(len(scopes), 3)
            self.assertEqual({project for project, _revision in scopes}, {"amiga", "nuvyr"})
            self.assertEqual(len({revision for _project, revision in scopes}), 2)

    def test_five_hundred_new_write_cap_stops_at_exact_candidate_cursor(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            candidates = [
                _candidate(
                    f"Chats/bulk/{number:03d}.md",
                    f"content-{number}".encode(),
                    SimpleNamespace(st_mtime_ns=number),
                    scan_cursor=f"cursor-{number}",
                    scan_count=number + 1,
                )
                for number in range(501)
            ]
            result = store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=candidates,
                next_cursor="complete",
                scanned_count=501,
                observed_at_utc=START.isoformat(),
            )
            self.assertEqual(result, {"cursor": "cursor-499", "scanned": 500, "written": 500})
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                500,
            )
            with self.assertRaisesRegex(ValueError, "at most 500"):
                store.reconcile_observations(
                    workspace_id="ws_alpha",
                    project_id="nuvyr",
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    candidates=candidates,
                    next_cursor="complete",
                    scanned_count=501,
                    observed_at_utc=START.isoformat(),
                    write_limit=501,
                )
            with self.assertRaisesRegex(ValueError, "invalid field set"):
                store._insert_observation_audit(
                    workspace_id="ws_alpha",
                    project_id="amiga",
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    action="reconcile",
                    occurred_at_utc=START.isoformat(),
                    detail={"body": "must-never-enter-audit"},
                )

    def test_unresolved_is_never_pruned_and_resolved_waits_thirty_days(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            candidate = self.candidate_for()
            store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=[candidate],
                next_cursor="",
                scanned_count=1,
                observed_at_utc=START.isoformat(),
            )
            scope = {
                "workspace_id": "ws_alpha",
                "project_id": "amiga",
                "source_id": SOURCE_ID,
                "registry_revision": snapshot.registry_revision,
            }
            self.assertEqual(
                store.prune_resolved_observations(
                    **scope,
                    resolved_before_utc=(START + timedelta(days=100)).isoformat(),
                    occurred_at_utc=(START + timedelta(days=100)).isoformat(),
                ),
                0,
            )
            resolved = START + timedelta(days=1)
            self.assertTrue(
                store.resolve_observation(
                    **scope,
                    dedupe_key=candidate["dedupe_key"],
                    resolved_at_utc=resolved.isoformat(),
                )
            )
            self.assertEqual(
                store.prune_resolved_observations(
                    **scope,
                    resolved_before_utc=(resolved - timedelta(days=1)).isoformat(),
                    occurred_at_utc=(resolved + timedelta(days=29)).isoformat(),
                ),
                0,
            )
            self.assertEqual(
                store.prune_resolved_observations(
                    **scope,
                    resolved_before_utc=(resolved + timedelta(days=1)).isoformat(),
                    occurred_at_utc=(resolved + timedelta(days=31)).isoformat(),
                ),
                1,
            )

    def test_periodic_reconciliation_is_authority_across_event_loss_time_jump_and_restart(self) -> None:
        clock = MutableClock()
        first_packet = self.make_chat("periodic", "one.md", packet("amiga", "one"))
        with LedgerStore.open_writer(self.paths) as store:
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
                wall_clock=clock.wall,
                monotonic=clock.monotonic,
            )
            self.assertTrue(engine.reconcile_due(store, force=True))
            first_count = store._connection.execute(
                "SELECT count(*) FROM observations WHERE project_id = 'amiga'"
            ).fetchone()[0]
            self.assertEqual(first_count, 1)

            self.make_chat("periodic", "two.md", packet("amiga", "two"))
            clock.monotonic_value = 30
            clock.wall_value -= timedelta(days=365)
            self.assertTrue(engine.reconcile_due(store))
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observations WHERE project_id = 'amiga'"
                ).fetchone()[0],
                2,
            )
            engine.close()

            restarted = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
                wall_clock=clock.wall,
                monotonic=clock.monotonic,
            )
            self.assertTrue(restarted.reconcile_due(store, force=True))
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observations WHERE project_id = 'amiga'"
                ).fetchone()[0],
                2,
            )
            self.assertTrue(first_packet.exists())

    def test_reconcile_opens_one_workspace_root_for_multi_project_cadence(self) -> None:
        self.make_chat("single-root", "amiga.md", packet("amiga"))
        self.make_chat("single-root", "nuvyr.md", packet("nuvyr"))
        real_open = os.open
        root_opens: list[str] = []

        def tracked_open(path, flags, *args, **kwargs):
            if os.fspath(path) == os.fspath(self.root) and "dir_fd" not in kwargs:
                root_opens.append(os.fspath(path))
            return real_open(path, flags, *args, **kwargs)

        with patch.object(observe_module.os, "open", side_effect=tracked_open):
            with LedgerStore.open_writer(self.paths) as store:
                engine = ObservationEngine(
                    workspace_root=self.root,
                    workspace_id="ws_alpha",
                    projects_path=self.projects,
                )
                engine.reconcile(store)
                counts = dict(
                    store._connection.execute(
                        "SELECT project_id, count(*) FROM observations GROUP BY project_id"
                    ).fetchall()
                )

        self.assertEqual(root_opens[1:], [os.fspath(self.root)])
        self.assertEqual(counts, {"amiga": 1, "nuvyr": 1})

    def test_reconcile_uses_pinned_root_after_pathname_swap(self) -> None:
        self.make_chat("old-root", "amiga.md", packet("amiga"))
        old_root = self.root.with_name("workspace-old")
        real_open = os.open
        swapped = False

        def tracked_open(path, flags, *args, **kwargs):
            nonlocal swapped
            is_root_open = os.fspath(path) == os.fspath(self.root) and "dir_fd" not in kwargs
            fd = real_open(path, flags, *args, **kwargs)
            if is_root_open and not swapped and getattr(tracked_open, "root_opens", 0) == 1:
                self.root.rename(old_root)
                self.root.mkdir()
                (self.root / "Chats").mkdir()
                (self.root / "agents").mkdir()
                new_chat = self.root / "Chats" / "new-root"
                new_chat.mkdir()
                (new_chat / "nuvyr.md").write_bytes(packet("nuvyr"))
                swapped = True
            if is_root_open:
                tracked_open.root_opens = getattr(tracked_open, "root_opens", 0) + 1
            return fd

        with patch.object(observe_module.os, "open", side_effect=tracked_open):
            with LedgerStore.open_writer(self.paths) as store:
                engine = ObservationEngine(
                    workspace_root=self.root,
                    workspace_id="ws_alpha",
                    projects_path=self.projects,
                )
                engine.reconcile(store)
                rows = store._connection.execute(
                    "SELECT project_id, path FROM observations ORDER BY project_id, path"
                ).fetchall()

        self.assertTrue(swapped)
        self.assertEqual(rows, [("amiga", "Chats/old-root/amiga.md")])

    def test_reconcile_revalidates_pinned_root_before_writing(self) -> None:
        self.make_chat("revalidate", "amiga.md", packet("amiga"))
        with LedgerStore.open_writer(self.paths) as store:
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            with patch.object(
                observe_module._WorkspaceAuthority,
                "revalidate",
                side_effect=ObservationError("workspace root identity changed"),
            ), self.assertRaisesRegex(ObservationError, "workspace root identity changed"):
                engine.reconcile(store)
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                0,
            )

    def test_reconcile_fails_closed_when_workspace_root_is_not_safe_directory(self) -> None:
        for name, bad_root in (
            ("symlink", self.root.with_name("workspace-link")),
            ("file", self.root.with_name("workspace-file")),
        ):
            if name == "symlink":
                bad_root.symlink_to(self.root, target_is_directory=True)
            else:
                bad_root.write_text("not a directory")
            with self.subTest(name=name):
                with LedgerStore.open_writer(self.paths) as store:
                    engine = ObservationEngine(
                        workspace_root=bad_root,
                        workspace_id="ws_alpha",
                        projects_path=self.projects,
                    )
                    with self.assertRaisesRegex(
                        ObservationError, "workspace root cannot be opened safely"
                    ):
                        engine.reconcile(store)
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT count(*) FROM observations"
                        ).fetchone()[0],
                        0,
                    )

    def test_scheduler_enforces_aggregate_scan_write_and_maintenance_bounds(self) -> None:
        self.set_projects("amiga", "nuvyr", "zcode")
        calls: list[tuple[str, int]] = []

        def fake_scan(_root, *, project_id, scan_limit, **_kwargs):
            calls.append((project_id, scan_limit))
            candidates = [
                self.scheduler_candidate(project_id, f"{index:03d}", scan_count=index + 1)
                for index in range(500)
            ]
            return candidates, "remaining", 2000

        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "scan_mailbox", side_effect=fake_scan
        ):
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            result = engine.reconcile(store)
            self.assertEqual(calls, [("amiga", 2000)])
            self.assertEqual(self.observation_counts(store), {"amiga": 500})
            self.assertEqual(self.audit_counts(store), {"reconcile": 1, "retention": 1})
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "nuvyr",
            )
            self.assertEqual(result["budget"]["scan_remaining"], 0)
            self.assertEqual(result["budget"]["write_remaining"], 0)
            self.assertEqual(result["budget"]["maintenance_remaining"], 496)

    def test_scheduler_stops_before_scan_when_minimum_maintenance_is_unavailable(self) -> None:
        self.set_projects("amiga", "nuvyr")
        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "MAINTENANCE_LIMIT", 2
        ), patch.object(observe_module, "scan_mailbox") as scan:
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            result = engine.reconcile(store)
            scan.assert_not_called()
            self.assertEqual(self.observation_counts(store), {})
            self.assertIsNone(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                )
            )
            self.assertEqual(result["projects"], {})
            self.assertEqual(result["budget"]["maintenance_remaining"], 2)

    def test_scheduler_rotates_start_and_eventually_attempts_every_project(self) -> None:
        self.set_projects("amiga", "nuvyr", "zcode")
        attempted: list[str] = []

        def fake_scan(_root, *, project_id, scan_limit, **_kwargs):
            attempted.append(project_id)
            return [self.scheduler_candidate(project_id, str(len(attempted)), scan_count=1)], "", 2000

        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "scan_mailbox", side_effect=fake_scan
        ):
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            for _ in range(3):
                engine.reconcile(store)
            self.assertEqual(attempted, ["amiga", "nuvyr", "zcode"])
            self.assertEqual(self.observation_counts(store), {"amiga": 1, "nuvyr": 1, "zcode": 1})
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "amiga",
            )

    def test_scheduler_reconciles_cursor_by_project_id_across_add_remove_and_reorder(self) -> None:
        self.set_projects("amiga", "nuvyr", "zcode")
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            store.advance_observation_scheduler_cursor(
                workspace_id="ws_alpha",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                next_project_id="nuvyr",
                updated_at_utc=START.isoformat(),
            )
        self.set_projects("zcode", "amiga", "omega")
        attempted: list[str] = []

        def fake_scan(_root, *, project_id, **_kwargs):
            attempted.append(project_id)
            return [self.scheduler_candidate(project_id, str(len(attempted)), scan_count=1)], "", 1

        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "scan_mailbox", side_effect=fake_scan
        ):
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            engine.reconcile(store)
            self.assertEqual(attempted, ["omega", "zcode", "amiga"])
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "omega",
            )

    def test_scheduler_deletes_cursor_only_for_fully_empty_snapshot(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            store.advance_observation_scheduler_cursor(
                workspace_id="ws_alpha",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                next_project_id="nuvyr",
                updated_at_utc=START.isoformat(),
            )
            empty_snapshot = SimpleNamespace(
                project_ids=[],
                registry_revision=snapshot.registry_revision,
                record=lambda store: None,
            )
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            with patch.object(
                observe_module, "read_registry_snapshot", return_value=empty_snapshot
            ):
                result = engine.reconcile(store)
            self.assertEqual(result["project_count"], 0)
            self.assertIsNone(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                )
            )

    def test_scheduler_crash_after_project_commit_before_cursor_advance_repeats_without_duplicate(self) -> None:
        self.set_projects("amiga")

        def fake_scan(_root, *, project_id, **_kwargs):
            return [self.scheduler_candidate(project_id, "same", scan_count=1)], "", 1

        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "scan_mailbox", side_effect=fake_scan
        ):
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            with patch.object(
                store,
                "advance_observation_scheduler_cursor",
                side_effect=RuntimeError("simulated crash"),
            ), self.assertRaisesRegex(RuntimeError, "simulated crash"):
                engine.reconcile(store)
            self.assertEqual(self.observation_counts(store), {"amiga": 1})
            self.assertIsNone(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                )
            )
            engine.reconcile(store)
            self.assertEqual(self.observation_counts(store), {"amiga": 1})
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "amiga",
            )

    def test_scheduler_preserves_committed_projects_and_does_not_advance_failed_project(self) -> None:
        self.set_projects("amiga", "nuvyr")

        def fake_scan(_root, *, project_id, **_kwargs):
            if project_id == "nuvyr":
                raise ObservationError("project scan failed")
            return [self.scheduler_candidate(project_id, "ok", scan_count=1)], "", 1

        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "scan_mailbox", side_effect=fake_scan
        ):
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            with self.assertRaisesRegex(ObservationError, "project scan failed"):
                engine.reconcile(store)
            self.assertEqual(self.observation_counts(store), {"amiga": 1})
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "nuvyr",
            )

    def test_scheduler_counts_retention_prune_rows_inside_maintenance_budget(self) -> None:
        self.set_projects("amiga")
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            candidate = self.scheduler_candidate("amiga", "resolved", scan_count=1)
            store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=[candidate],
                next_cursor="",
                scanned_count=1,
                observed_at_utc=START.isoformat(),
            )
            store.resolve_observation(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                dedupe_key=str(candidate["dedupe_key"]),
                resolved_at_utc=(START + timedelta(days=1)).isoformat(),
            )

            def fake_scan(_root, *, project_id, **_kwargs):
                return [], "", 1

            with patch.object(observe_module, "scan_mailbox", side_effect=fake_scan):
                engine = ObservationEngine(
                    workspace_root=self.root,
                    workspace_id="ws_alpha",
                    projects_path=self.projects,
                    wall_clock=lambda: START + timedelta(days=40),
                )
                result = engine.reconcile(store)
            self.assertEqual(result["projects"]["amiga"]["pruned"], 1)
            self.assertEqual(result["budget"]["maintenance_remaining"], 495)
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observations WHERE resolution_state = 'resolved'"
                ).fetchone()[0],
                0,
            )

    def test_scheduler_bounds_audit_tail_without_audit_of_audit(self) -> None:
        self.set_projects("amiga")

        def fake_scan(_root, *, project_id, **_kwargs):
            return [], "", 1

        with LedgerStore.open_writer(self.paths) as store, patch.object(
            observe_module, "AUDIT_RETENTION_LIMIT", 3
        ), patch.object(observe_module, "scan_mailbox", side_effect=fake_scan):
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            for _ in range(8):
                result = engine.reconcile(store)

            self.assertLessEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observation_audit WHERE workspace_id = 'ws_alpha' "
                    "AND project_id = 'amiga' AND source_id = ?",
                    (SOURCE_ID,),
                ).fetchone()[0],
                3,
            )
            self.assertGreater(result["projects"]["amiga"]["audit_pruned"], 0)
            self.assertEqual(
                {
                    action
                    for (action,) in store._connection.execute(
                        "SELECT DISTINCT action FROM observation_audit"
                    ).fetchall()
                },
                {"reconcile", "retention"},
            )

    def test_scheduler_charges_audit_prune_to_maintenance_budget_and_defers(self) -> None:
        self.set_projects("amiga")

        def fake_scan(_root, *, project_id, **_kwargs):
            return [], "", 1

        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            for index in range(5):
                store._insert_observation_audit(
                    workspace_id="ws_alpha",
                    project_id="amiga",
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    action="retention",
                    occurred_at_utc=(START + timedelta(seconds=index)).isoformat(),
                    detail={"removed": 0},
                )

            with patch.object(observe_module, "AUDIT_RETENTION_LIMIT", 2), patch.object(
                observe_module, "MAINTENANCE_LIMIT", 5
            ), patch.object(observe_module, "scan_mailbox", side_effect=fake_scan):
                engine = ObservationEngine(
                    workspace_root=self.root,
                    workspace_id="ws_alpha",
                    projects_path=self.projects,
                )
                result = engine.reconcile(store)

            self.assertEqual(result["projects"]["amiga"]["audit_pruned"], 1)
            self.assertEqual(result["budget"]["maintenance_remaining"], 0)
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observation_audit WHERE workspace_id = 'ws_alpha' "
                    "AND project_id = 'amiga' AND source_id = ?",
                    (SOURCE_ID,),
                ).fetchone()[0],
                6,
            )
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "amiga",
            )

    def test_scheduler_prunes_small_superseded_revision_when_total_exceeds_tail(self) -> None:
        self.set_projects("amiga")

        def fake_scan(_root, *, project_id, **_kwargs):
            return [], "", 1

        with LedgerStore.open_writer(self.paths) as store:
            current = self.snapshot(store)
            old_hash = "b" * 64
            old_revision = f"sha256:{old_hash}"
            store.record_registry_snapshot(
                workspace_id="ws_alpha",
                registry_revision=old_revision,
                registry_source_sha256=old_hash,
                captured_at_utc=START.isoformat(),
                workspace_snapshot_json=json.dumps(
                    {"workspace_id": "ws_alpha", "projects": ["amiga"]}
                ),
                project_snapshots={"amiga": json.dumps({"project_id": "amiga"})},
                source_snapshots={"amiga": {SOURCE_ID: "{}"}},
            )
            store._insert_observation_audit(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=old_revision,
                action="retention",
                occurred_at_utc=(START - timedelta(days=1)).isoformat(),
                detail={"removed": 0},
            )

            with patch.object(observe_module, "AUDIT_RETENTION_LIMIT", 2), patch.object(
                observe_module, "scan_mailbox", side_effect=fake_scan
            ):
                engine = ObservationEngine(
                    workspace_root=self.root,
                    workspace_id="ws_alpha",
                    projects_path=self.projects,
                )
                result = engine.reconcile(store)

            self.assertEqual(result["projects"]["amiga"]["audit_pruned"], 1)
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observation_audit WHERE workspace_id = 'ws_alpha' "
                    "AND project_id = 'amiga' AND source_id = ? AND registry_revision = ?",
                    (SOURCE_ID, old_revision),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM observation_audit WHERE workspace_id = 'ws_alpha' "
                    "AND project_id = 'amiga' AND source_id = ? AND registry_revision = ?",
                    (SOURCE_ID, current.registry_revision),
                ).fetchone()[0],
                2,
            )

    def test_scheduler_audit_prune_preserves_observations_checkpoint_and_cursor(self) -> None:
        self.set_projects("amiga")

        def fake_scan(_root, *, project_id, **_kwargs):
            return [], "checkpoint-after-empty-scan", 1

        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            candidate = self.scheduler_candidate("amiga", "unresolved", scan_count=1)
            store.reconcile_observations(
                workspace_id="ws_alpha",
                project_id="amiga",
                source_id=SOURCE_ID,
                registry_revision=snapshot.registry_revision,
                candidates=[candidate],
                next_cursor="checkpoint-before-prune",
                scanned_count=1,
                observed_at_utc=START.isoformat(),
            )
            for index in range(5):
                store._insert_observation_audit(
                    workspace_id="ws_alpha",
                    project_id="amiga",
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    action="retention",
                    occurred_at_utc=(START + timedelta(seconds=10 + index)).isoformat(),
                    detail={"removed": 0},
                )

            with patch.object(observe_module, "AUDIT_RETENTION_LIMIT", 2), patch.object(
                observe_module, "scan_mailbox", side_effect=fake_scan
            ):
                engine = ObservationEngine(
                    workspace_root=self.root,
                    workspace_id="ws_alpha",
                    projects_path=self.projects,
                )
                engine.reconcile(store)

            self.assertEqual(
                store._connection.execute(
                    "SELECT resolution_state FROM observations WHERE workspace_id = 'ws_alpha' "
                    "AND project_id = 'amiga'"
                ).fetchall(),
                [("unresolved",)],
            )
            self.assertEqual(
                store.observation_checkpoint_cursor(
                    workspace_id="ws_alpha",
                    project_id="amiga",
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                ),
                "checkpoint-after-empty-scan",
            )
            self.assertEqual(
                store.observation_scheduler_cursor(
                    workspace_id="ws_alpha", source_id=SOURCE_ID
                ),
                "amiga",
            )

    def test_watchdog_is_hint_only_and_failure_does_not_disable_reconciliation(self) -> None:
        clock = MutableClock()
        engine = ObservationEngine(
            workspace_root=self.root,
            workspace_id="ws_alpha",
            projects_path=self.projects,
            wall_clock=clock.wall,
            monotonic=clock.monotonic,
        )
        with patch.object(observe_module, "_load_watchdog", side_effect=ImportError("absent")):
            engine.start()
        self.assertIn("ImportError", engine._watchdog_error)
        self.make_chat("watchdog", "one.md", packet("amiga"))
        with LedgerStore.open_writer(self.paths) as store:
            self.assertTrue(engine.reconcile_due(store, force=True))
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                1,
            )

    def test_partially_started_watchdog_is_stopped_when_start_raises(self) -> None:
        class Handler:
            pass

        observer = Mock()
        observer.start.side_effect = RuntimeError("partial start")
        engine = ObservationEngine(
            workspace_root=self.root,
            workspace_id="ws_alpha",
            projects_path=self.projects,
        )
        with patch.object(
            observe_module,
            "_load_watchdog",
            return_value=(Handler, lambda: observer),
        ):
            engine.start()
        observer.stop.assert_called_once()
        observer.join.assert_called_once_with(timeout=2)
        self.assertIsNone(engine._observer)
        self.assertIn("partial start", engine._watchdog_error)

    def test_failed_reconciliation_is_rate_limited_then_periodically_retried(self) -> None:
        clock = MutableClock()
        engine = ObservationEngine(
            workspace_root=self.root,
            workspace_id="ws_alpha",
            projects_path=self.projects,
            wall_clock=clock.wall,
            monotonic=clock.monotonic,
        )
        store = object()
        with patch.object(
            engine,
            "reconcile",
            side_effect=[ObservationError("persistent"), {"amiga": {"written": 0}}],
        ) as reconcile:
            with self.assertRaisesRegex(ObservationError, "persistent"):
                engine.reconcile_due(store, force=True)
            for tick in (0.1, 1, 29.999):
                clock.monotonic_value = tick
                self.assertFalse(engine.reconcile_due(store))
            self.assertEqual(reconcile.call_count, 1)
            clock.monotonic_value = 30
            self.assertTrue(engine.reconcile_due(store))
            self.assertEqual(reconcile.call_count, 2)

    def test_d9_reverse_consumers_and_dispatch_schema_remain_absent(self) -> None:
        root = Path(__file__).parents[1]
        consumer_paths = (
            root / "bin" / "deliver.py",
            root / "bin" / "inbox.py",
            root / "bin" / "_session_autobridge.py",
            root / "bin" / "pm2_watchers.py",
            root / "tools" / "axbridge",
            root / "pm2",
        )
        forbidden_consumer = re.compile(
            r"\b(?:from|import)\s+llm_collab\b|"
            r"llm_collab\.(?:ledger|daemon|compatibility)|bin/llm_collabd\.py|llm_collabd"
        )
        for forbidden_import in (
            "from llm_collab import daemon",
            "from llm_collab import ledger",
            "import llm_collab.daemon",
        ):
            self.assertIsNotNone(forbidden_consumer.search(forbidden_import))
        for target in consumer_paths:
            files = [target] if target.is_file() else [path for path in target.rglob("*") if path.is_file()]
            for source in files:
                with self.subTest(source=source):
                    self.assertIsNone(
                        forbidden_consumer.search(source.read_text(errors="ignore"))
                    )
        forbidden_sql = re.compile(
            r"message|delivery|attempt|receipt|lease|fence|quarantine|retry|dead_letter"
        )
        sql = "\n".join((*store_module.V1_SQL, *store_module.V2_SQL)).lower()
        self.assertIsNone(forbidden_sql.search(sql))

    def test_frozen_constants_are_independent_literals_and_diagnostics_are_bounded(self) -> None:
        self.assertEqual(
            (
                observe_module.RECONCILIATION_SECONDS,
                observe_module.DEBOUNCE_SECONDS,
                observe_module.SCAN_LIMIT,
                observe_module.WRITE_LIMIT,
                observe_module.RETENTION_DAYS,
                observe_module.DIAGNOSTIC_GROUP_LIMIT,
                observe_module.DIAGNOSTIC_AUDIT_LIMIT,
            ),
            (30, 1, 2000, 500, 30, 50, 200),
        )
        project_ids = [f"p{'x' * 122}{number:05d}" for number in range(55)]
        projects = [{"id": project_id} for project_id in project_ids]
        self.projects.write_text(json.dumps({"projects": projects}))
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.projects,
            )
            reconciled = engine.reconcile(store)
            engine._last_result = reconciled
            candidate = self.candidate_for()
            for project in projects:
                store.reconcile_observations(
                    workspace_id="ws_alpha",
                    project_id=project["id"],
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    candidates=[candidate],
                    next_cursor="z" * 3_900,
                    scanned_count=1,
                    observed_at_utc=START.isoformat(),
                )
            for second in range(150):
                store.reconcile_observations(
                    workspace_id="ws_alpha",
                    project_id=project_ids[0],
                    source_id=SOURCE_ID,
                    registry_revision=snapshot.registry_revision,
                    candidates=[candidate],
                    next_cursor="z" * 3_900,
                    scanned_count=1,
                    observed_at_utc=(START + timedelta(seconds=second + 1)).isoformat(),
                )
            diagnostics = store.observation_diagnostics(workspace_id="ws_alpha")
            self.assertLessEqual(len(diagnostics["groups"]), 50)
            self.assertLessEqual(len(diagnostics["audit"]), 200)
            self.assertTrue(diagnostics["groups_truncated"])
            self.assertTrue(diagnostics["audit_truncated"])
            with self.assertRaises(ValueError):
                store.observation_diagnostics(
                    workspace_id="ws_alpha", group_limit=51, audit_limit=200
                )
            with self.assertRaises(ValueError):
                store.observation_diagnostics(
                    workspace_id="ws_alpha", group_limit=50, audit_limit=201
                )
            self.assertEqual(reconciled["project_count"], 55)
            self.assertEqual(len(reconciled["projects"]), 50)
            self.assertEqual(reconciled["truncated_projects"], 5)
            self.assertLess(len(json.dumps(reconciled).encode()), 65_536)
            server = DaemonServer(self.paths, workspace_root=self.root)
            server._store = store
            server._observation = engine
            response = server._status_response()
            encoded = json.dumps(response, separators=(",", ":")).encode()
            self.assertLessEqual(len(encoded), RESPONSE_LIMIT)
            receiver = Mock()
            server._send(receiver, response)
            self.assertNotIn(b"response exceeds", receiver.sendall.call_args.args[0])

    def test_schema_constraints_reject_unsafe_rows_and_unknown_source(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            snapshot = self.snapshot(store)
            scope = (
                "ws_alpha",
                "amiga",
                SOURCE_ID,
                snapshot.registry_revision,
            )
            base = self.candidate_for()
            invalid = {
                "absolute-path": {**base, "path": "/tmp/body"},
                "parent-path": {**base, "path": "Chats/../body"},
                "uppercase-hash": {**base, "content_sha256": "A" * 64},
                "negative-size": {**base, "byte_size": -1},
                "bool-mtime": {**base, "mtime_ns": True},
                "mtime-underflow": {**base, "mtime_ns": -(1 << 63) - 1},
                "mtime-overflow": {**base, "mtime_ns": 1 << 63},
            }
            for name, candidate in invalid.items():
                with self.subTest(name=name), self.assertRaises(ValueError):
                    store.reconcile_observations(
                        workspace_id=scope[0],
                        project_id=scope[1],
                        source_id=scope[2],
                        registry_revision=scope[3],
                        candidates=[candidate],
                        next_cursor="",
                        scanned_count=1,
                        observed_at_utc=START.isoformat(),
                    )
            insert_sql = (
                "INSERT INTO observations "
                "(workspace_id, project_id, source_id, registry_revision, dedupe_key, path, "
                "content_sha256, byte_size, mtime_ns, resolution_state, observed_at_utc, "
                "resolved_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 1, -1, 'unresolved', ?, NULL)"
            )
            for name, dedupe_key, content_sha256 in (
                ("nul-dedupe", "f" * 64 + "\x00NOTHEX", "e" * 64),
                ("nul-content", "d" * 64, "e" * 64 + "\x00NOTHEX"),
            ):
                with self.subTest(name=name), self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        insert_sql,
                        (
                            scope[0],
                            scope[1],
                            scope[2],
                            scope[3],
                            dedupe_key,
                            f"Chats/a/{name}.md",
                            content_sha256,
                            START.isoformat(),
                        ),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                store._connection.execute(
                    insert_sql,
                    (
                        scope[0],
                        scope[1],
                        scope[2],
                        scope[3],
                        "c" * 64,
                        "Chats/a/nul-observed-at.md",
                        "b" * 64,
                        START.isoformat() + "\x00tail",
                    ),
                )
            resolved_insert = (
                "INSERT INTO observations "
                "(workspace_id, project_id, source_id, registry_revision, dedupe_key, path, "
                "content_sha256, byte_size, mtime_ns, resolution_state, observed_at_utc, "
                "resolved_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 1, -1, 'resolved', ?, ?)"
            )
            for name, resolved_at in (
                ("nul-resolved-at", START.isoformat() + "\x00tail"),
                ("null-resolved-at", None),
            ):
                with self.subTest(name=name), self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        resolved_insert,
                        (
                            scope[0],
                            scope[1],
                            scope[2],
                            scope[3],
                            "a" * 63 + ("1" if resolved_at is None else "2"),
                            f"Chats/a/{name}.md",
                            "9" * 64,
                            START.isoformat(),
                            resolved_at,
                        ),
                    )
            with self.assertRaises(sqlite3.IntegrityError):
                store._connection.execute(
                    "INSERT INTO observation_checkpoints "
                    "(workspace_id, project_id, source_id, registry_revision, cursor, "
                    "scanned_count, written_count, updated_at_utc) "
                    "VALUES (?, ?, ?, ?, '', 0, 0, ?)",
                    (*scope, START.isoformat() + "\x00tail"),
                )
            for name, occurred_at, detail_json in (
                ("nul-occurred-at", START.isoformat() + "\x00tail", "{}"),
                ("nul-detail", START.isoformat(), "{}\x00tail"),
            ):
                with self.subTest(name=name), self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        "INSERT INTO observation_audit "
                        "(workspace_id, project_id, source_id, registry_revision, audit_id, "
                        "action, result, occurred_at_utc, detail_json) "
                        "VALUES (?, ?, ?, ?, 1, 'reconcile', 'committed', ?, ?)",
                        (*scope, occurred_at, detail_json),
                    )
            with self.assertRaises(ValueError):
                store.reconcile_observations(
                    workspace_id=scope[0],
                    project_id=scope[1],
                    source_id="arbitrary_path",
                    registry_revision=scope[3],
                    candidates=[base],
                    next_cursor="",
                    scanned_count=1,
                    observed_at_utc=START.isoformat(),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                store._connection.execute(
                    "INSERT INTO observations "
                    "(workspace_id, project_id, source_id, registry_revision, dedupe_key, path, "
                    "content_sha256, byte_size, mtime_ns, resolution_state, observed_at_utc, "
                    "resolved_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 'unresolved', ?, NULL)",
                    (
                        scope[0],
                        scope[1],
                        scope[2],
                        scope[3],
                        "f" * 64,
                        "Chats/a/unsafe\x00body.md",
                        "e" * 64,
                        START.isoformat(),
                    ),
                )
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                0,
            )
            v2_sql = "\n".join(store_module.V2_SQL)
            self.assertIn("instr(dedupe_key, char(0)) = 0", v2_sql)
            self.assertIn("length(CAST(dedupe_key AS BLOB)) = 64", v2_sql)
            self.assertIn("instr(content_sha256, char(0)) = 0", v2_sql)
            self.assertIn("length(CAST(content_sha256 AS BLOB)) = 64", v2_sql)
            self.assertIn(
                "mtime_ns BETWEEN -9223372036854775808 AND 9223372036854775807",
                v2_sql,
            )
            for field in (
                "observed_at_utc",
                "resolved_at_utc",
                "updated_at_utc",
                "occurred_at_utc",
                "detail_json",
            ):
                with self.subTest(field=field):
                    self.assertIn(f"instr({field}, char(0)) = 0", v2_sql)
                    self.assertIn(f"length(CAST({field} AS BLOB))", v2_sql)


if __name__ == "__main__":
    unittest.main()
