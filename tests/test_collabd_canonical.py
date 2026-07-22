from __future__ import annotations

import hashlib
import ast
import inspect
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jsonschema import Draft202012Validator

import llm_collab.canonical as canonical
import llm_collab.canonical.messages as messages_module
import llm_collab.ledger.store as store_module
from llm_collab.canonical import (
    CanonicalConflictError,
    CanonicalIntegrityError,
    create_or_return_equivalent,
    project_message_v1,
    read_message,
)
from llm_collab.ledger import LedgerPaths, LedgerStore


SAFE_VERSION = (3, 51, 3)
WORKSPACE = "ws_alpha"
PROJECT = "amiga"
OTHER_PROJECT = "nuvyr"
REVISION_HASH = "a" * 64
REVISION = "sha256:" + REVISION_HASH
NOW = "2026-07-22T00:00:00+00:00"


def record_registry(store: LedgerStore, revision_hash: str = REVISION_HASH) -> str:
    revision = "sha256:" + revision_hash
    store.record_registry_snapshot(
        workspace_id=WORKSPACE,
        registry_revision=revision,
        registry_source_sha256=revision_hash,
        captured_at_utc=NOW,
        workspace_snapshot_json=json.dumps(
            {"workspace_id": WORKSPACE, "projects": [PROJECT, OTHER_PROJECT]}
        ),
        project_snapshots={
            PROJECT: json.dumps({"project_id": PROJECT}),
            OTHER_PROJECT: json.dumps({"project_id": OTHER_PROJECT}),
        },
        source_snapshots={PROJECT: {}, OTHER_PROJECT: {}},
    )
    return revision


def intent(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "workspace_id": WORKSPACE,
        "scope_kind": "project",
        "scope_identity": PROJECT,
        "sender_agent_id": "agent_codex",
        "dedupe_key": "send-one",
        "body": b"hello",
        "recipients": ["agent_claude", "agent_codex", "agent_claude"],
        "registry_revision": REVISION,
        "created_at_utc": NOW,
        "title": "Hello",
        "ttl_seconds": 0,
        "ack_policy": "required",
        "artifacts": [("chat", "CHAT-1"), ("path", "docs/file.md")],
        "priority": "high",
        "tags": ["review", "review", "urgent"],
        "chat_link": "CHAT-1",
        "task_link": None,
    }
    result.update(changes)
    return result


class CanonicalMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        linked_version = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        linked_version.start()
        self.addCleanup(linked_version.stop)

    def test_create_is_structurally_idempotent_scoped_and_body_sharing(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, created = create_or_return_equivalent(store, **intent())
                self.assertTrue(created)
                self.assertRegex(message_id, r"^msg_[0-9a-f]{64}$")
                same_id, created = create_or_return_equivalent(
                    store,
                    **intent(
                        created_at_utc="2026-07-22T00:00:01+00:00",
                        registry_revision=record_registry(store, "b" * 64),
                    ),
                )
                self.assertEqual((same_id, created), (message_id, False))

                second_id, created = create_or_return_equivalent(
                    store, **intent(dedupe_key="send-two", recipients=["agent_codex"])
                )
                self.assertTrue(created)
                self.assertNotEqual(second_id, message_id)
                self.assertEqual(
                    store._connection.execute("SELECT count(*) FROM canonical_bodies").fetchone()[0],
                    1,
                )
                loaded = read_message(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                )
                self.assertEqual(loaded["body"], b"hello")
                self.assertEqual(loaded["recipients"], ("agent_claude", "agent_codex"))
                self.assertEqual(loaded["tags"], ("review", "urgent"))
                self.assertEqual(
                    loaded["body_ref"], "body_" + hashlib.sha256(b"hello").hexdigest()
                )
                with self.assertRaises(KeyError):
                    read_message(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=OTHER_PROJECT,
                        message_id=message_id,
                    )

    def test_same_dedupe_different_intent_and_cross_scope_reply_fail_closed(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _ = create_or_return_equivalent(store, **intent())
                with self.assertRaises(CanonicalConflictError):
                    create_or_return_equivalent(store, **intent(body=b"different"))
                with self.assertRaises(sqlite3.IntegrityError):
                    create_or_return_equivalent(
                        store,
                        **intent(
                            scope_identity=OTHER_PROJECT,
                            dedupe_key="cross-scope-reply",
                            reply_to_message_id=message_id,
                        ),
                    )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_messages"
                    ).fetchone()[0],
                    1,
                )

    def test_mutation_10_projection_leak_is_killed_by_required_only_schema(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas/standalone/v1/message.schema.json").read_text()
        )
        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, WORKSPACE)) as store:
                record_registry(store)
                message_id, _ = create_or_return_equivalent(store, **intent())
                projected = project_message_v1(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                )
                self.assertEqual(
                    set(projected),
                    {"schema_version", "workspace_id", "scope", "message_id", "body_ref", "recipients"},
                )
                self.assertEqual(projected["recipients"], ["agent_claude", "agent_codex"])
                Draft202012Validator(schema).validate(projected)

    def test_mutation_08_skipped_body_integrity_verification_is_killed(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, WORKSPACE)) as store:
                record_registry(store)
                message_id, _ = create_or_return_equivalent(store, **intent())
                store._connection.execute("DROP TRIGGER canonical_bodies_no_update")
                store._connection.execute(
                    "UPDATE canonical_bodies SET body = ?", (b"HELLO",)
                )
                with self.assertRaises(CanonicalIntegrityError):
                    read_message(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                    )

    def test_preflight_and_atomic_foreign_key_failure(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                store._connection.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(RuntimeError, "open transaction"):
                    create_or_return_equivalent(store, **intent(body=object()))
                store._connection.execute("ROLLBACK")
                with self.assertRaises(sqlite3.IntegrityError):
                    create_or_return_equivalent(
                        store,
                        **intent(
                            dedupe_key="unknown-revision",
                            registry_revision="sha256:" + "f" * 64,
                            body=b"must-roll-back",
                        ),
                    )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_bodies WHERE body = ?",
                        (b"must-roll-back",),
                    ).fetchone()[0],
                    0,
                )
            with LedgerStore.open_reader(paths) as reader:
                with self.assertRaises(PermissionError):
                    create_or_return_equivalent(reader, **intent(body=object()))

    def test_mutation_07_dropped_dedupe_and_other_equivalence_inputs_are_killed(self) -> None:
        base = {
            "workspace_id": WORKSPACE,
            "scope_kind": "project",
            "scope_identity": PROJECT,
            "sender_agent_id": "agent_codex",
            "dedupe_key": "one",
            "body_sha256": "a" * 64,
            "recipients": ("agent_codex",),
            "reply_to_message_id": None,
            "ttl_seconds": 0,
            "ack_policy": "none",
            "artifacts": (),
            "title": "title",
            "priority": "normal",
            "tags": (),
            "chat_link": None,
            "task_link": None,
        }
        original = messages_module._derive_message_id(**base)
        mutations = {
            "workspace_id": "ws_other",
            "scope_kind": "workspace",
            "scope_identity": "nuvyr",
            "sender_agent_id": "agent_claude",
            "dedupe_key": "two",
            "body_sha256": "b" * 64,
            "recipients": ("agent_claude",),
            "reply_to_message_id": "msg_" + "c" * 64,
            "ttl_seconds": 1,
            "ack_policy": "required",
            "artifacts": (("chat", "CHAT-1"),),
            "title": "other",
            "priority": "high",
            "tags": ("tag",),
            "chat_link": "CHAT-1",
            "task_link": "TASK-1",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = value
                self.assertNotEqual(messages_module._derive_message_id(**changed), original)
    def test_mutation_13_null_marker_collision_is_killed(self) -> None:
        base = {
            "workspace_id": WORKSPACE,
            "scope_kind": "project",
            "scope_identity": PROJECT,
            "sender_agent_id": "agent_codex",
            "dedupe_key": "one",
            "body_sha256": "a" * 64,
            "recipients": ("agent_codex",),
            "reply_to_message_id": None,
            "ttl_seconds": 0,
            "ack_policy": "none",
            "artifacts": (),
            "title": "title",
            "priority": "normal",
            "tags": (),
            "chat_link": None,
            "task_link": None,
        }
        absent = messages_module._derive_message_id(**base)
        present_empty = dict(base)
        present_empty["chat_link"] = ""
        self.assertNotEqual(messages_module._derive_message_id(**present_empty), absent)

    def test_mutation_12_unscoped_body_read_and_runtime_consumer_are_killed(self) -> None:
        self.assertEqual(
            set(canonical.__all__),
            {
                "CanonicalConflictError",
                "CanonicalIntegrityError",
                "create_or_return_equivalent",
                "project_message_v1",
                "read_message",
            },
        )
        self.assertFalse([name for name in canonical.__all__ if "body" in name])
        for function in (read_message, project_message_v1):
            self.assertTrue(
                {"workspace_id", "scope_kind", "scope_identity"}.issubset(
                    inspect.signature(function).parameters
                )
            )

        root = Path(__file__).parents[1]
        package_root = root / "llm_collab"
        consumers = []
        for source in package_root.rglob("*.py"):
            if source.parent == package_root / "canonical":
                continue
            relative = source.relative_to(root).with_suffix("")
            parts = relative.parts
            package = parts[:-1] if parts[-1] != "__init__" else parts[:-1]
            for node in ast.walk(ast.parse(source.read_text())):
                resolved = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "llm_collab.canonical" or alias.name.startswith(
                            "llm_collab.canonical."
                        ):
                            consumers.append((source, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        prefix = package[: len(package) - node.level + 1]
                        resolved = ".".join((*prefix, *(node.module or "").split(".")))
                    else:
                        resolved = node.module or ""
                    if resolved == "llm_collab.canonical" or resolved.startswith(
                        "llm_collab.canonical."
                    ):
                        consumers.append((source, resolved))
        self.assertEqual(consumers, [])

    def test_trust_boundary_types_utf8_and_timestamps_fail_as_value_errors(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, WORKSPACE)) as store:
                record_registry(store)
                invalid = (
                    {"created_at_utc": "not-a-time"},
                    {"created_at_utc": "2026-07-22T01:00:00+01:00"},
                    {"created_at_utc": "2026-07-22T00:00:00"},
                    {"title": "\ud800"},
                    {"ack_policy": []},
                    {"priority": {}},
                    {"artifacts": [([], "ref")]},
                )
                for changes in invalid:
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        create_or_return_equivalent(store, **intent(**changes))


if __name__ == "__main__":
    unittest.main()
