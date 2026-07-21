from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from llm_collab.daemon.registry import (
    SOURCE_ID,
    SOURCE_PATHS,
    RegistryError,
    read_registry_snapshot,
)


FIXED_TIME = datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc)


class RegistrySnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "projects.json"

    def read(self, raw: bytes):
        self.path.write_bytes(raw)
        return read_registry_snapshot(
            self.path,
            workspace_id="ws_alpha",
            clock=lambda: FIXED_TIME,
        )

    def test_exact_bytes_define_revision_and_complete_fixed_source_snapshots(self) -> None:
        raw = b'{"_comment":"official metadata","projects":[{"id":"nuvyr"},{"id":"amiga"}]}'
        snapshot = self.read(raw)
        digest = hashlib.sha256(raw).hexdigest()

        self.assertEqual(snapshot.registry_revision, f"sha256:{digest}")
        self.assertEqual(snapshot.registry_source_sha256, digest)
        self.assertEqual(snapshot.captured_at_utc, FIXED_TIME.isoformat())
        self.assertEqual(snapshot.project_ids, ("amiga", "nuvyr"))
        workspace = json.loads(snapshot.workspace_snapshot_json)
        self.assertEqual(workspace["workspace_id"], "ws_alpha")
        self.assertEqual(workspace["projects"], ["amiga", "nuvyr"])
        self.assertEqual(workspace["projects_json_exact_utf8"].encode(), raw)
        for project_id in snapshot.project_ids:
            source = json.loads(snapshot.source_snapshots[project_id][SOURCE_ID])
            self.assertEqual(source["source_id"], "chats_mailbox")
            self.assertEqual(
                source["path_patterns"],
                ["Chats/*/*.md", "agents/*/inbox.json"],
            )
        self.assertEqual(SOURCE_PATHS, ("Chats/*/*.md", "agents/*/inbox.json"))

    def test_whitespace_changes_the_exact_revision_without_changing_projects(self) -> None:
        compact = self.read(b'{"projects":[{"id":"amiga"}]}')
        spaced = self.read(b'{"projects": [ {"id":"amiga"} ]}\n')
        self.assertNotEqual(compact.registry_revision, spaced.registry_revision)
        self.assertEqual(compact.project_snapshots, spaced.project_snapshots)

    def test_duplicate_members_including_escape_equivalents_fail_closed(self) -> None:
        invalid = (
            b'{"projects":[],"projects":[]}',
            b'{"projects":[{"id":"amiga","id":"amiga"}]}',
            b'{"projects":[{"id":"amiga","\\u0069d":"amiga"}]}',
            b'{"projects":[{"id":"amiga","weight":NaN}]}',
            b'{"projects":[{"id":"amiga","weight":Infinity}]}',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(RegistryError):
                self.read(raw)

    def test_missing_null_empty_duplicate_conflicting_and_mismatched_ids_fail(self) -> None:
        invalid = (
            {"projects": [{}]},
            {"projects": [{"id": None}]},
            {"projects": [{"id": ""}]},
            {"projects": [{"id": "amiga"}, {"project_id": "amiga"}]},
            {"projects": [{"id": "amiga", "project_id": "nuvyr"}]},
            {"workspace_id": "ws_other", "projects": [{"id": "amiga"}]},
            {"workspace_id": None, "projects": [{"id": "amiga"}]},
            {"projects": []},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(RegistryError):
                self.read(json.dumps(value).encode())

    def test_missing_workspace_id_uses_the_exact_ledger_workspace(self) -> None:
        snapshot = self.read(b'{"projects":[{"project_id":"amiga"}]}')
        self.assertEqual(snapshot.workspace_id, "ws_alpha")
        self.assertEqual(json.loads(snapshot.project_snapshots["amiga"])["project_id"], "amiga")

    def test_complete_snapshot_is_recorded_once_and_invalid_input_records_nothing(self) -> None:
        snapshot = self.read(b'{"projects":[{"id":"amiga"}]}')
        store = Mock()
        store.has_registry_snapshot.side_effect = [False, True]
        snapshot.record(store)
        snapshot.record(store)
        store.record_registry_snapshot.assert_called_once()
        kwargs = store.record_registry_snapshot.call_args.kwargs
        self.assertEqual(kwargs["workspace_id"], "ws_alpha")
        self.assertEqual(set(kwargs["project_snapshots"]), {"amiga"})

        store.reset_mock()
        with self.assertRaises(RegistryError):
            self.read(b'{"projects":[{"id":null}]}')
        store.record_registry_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
