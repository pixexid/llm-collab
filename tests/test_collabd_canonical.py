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

    def test_store_derives_generated_fields_and_rejects_supplied_values(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                supplied_values = {
                    "body_sha256": "f" * 64,
                    "message_id": "msg_" + "f" * 64,
                }
                for field, value in supplied_values.items():
                    with self.subTest(field=field), self.assertRaisesRegex(
                        TypeError, "unexpected keyword argument"
                    ):
                        store.create_canonical_message(**intent(), **{field: value})
                parameters = inspect.signature(
                    LedgerStore.create_canonical_message
                ).parameters
                self.assertNotIn("body_sha256", parameters)
                self.assertNotIn("message_id", parameters)
                self.assertNotIn("project_id", parameters)
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_bodies"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_messages"
                    ).fetchone()[0],
                    0,
                )

    def test_store_and_canonical_share_derivation_and_set_normalization(self) -> None:
        self.assertIs(messages_module._derive_message_id, store_module._derive_message_id)
        direct_intent = intent(
            recipients=["agent_codex", "agent_claude", "agent_codex"],
            artifacts=[
                ("path", "docs/file.md"),
                ("chat", "CHAT-1"),
                ("path", "docs/file.md"),
            ],
            tags=["urgent", "review", "urgent"],
        )
        canonical_intent = intent(
            recipients=["agent_claude", "agent_codex"],
            artifacts=[("chat", "CHAT-1"), ("path", "docs/file.md")],
            tags=["review", "urgent"],
        )
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                direct_id, created = store.create_canonical_message(**direct_intent)
                self.assertTrue(created)
                canonical_id, created = create_or_return_equivalent(
                    store, **canonical_intent
                )
                self.assertEqual((canonical_id, created), (direct_id, False))
                self.assertEqual(
                    direct_id,
                    messages_module._derive_message_id(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        sender_agent_id="agent_codex",
                        dedupe_key="send-one",
                        body_sha256=hashlib.sha256(b"hello").hexdigest(),
                        recipients=("agent_claude", "agent_codex"),
                        reply_to_message_id=None,
                        ttl_seconds=0,
                        ack_policy="required",
                        artifacts=(("chat", "CHAT-1"), ("path", "docs/file.md")),
                        title="Hello",
                        priority="high",
                        tags=("review", "urgent"),
                        chat_link="CHAT-1",
                        task_link=None,
                    ),
                )
                loaded = store.read_canonical_message(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=direct_id,
                )
                self.assertEqual(loaded["recipients"], ("agent_claude", "agent_codex"))
                self.assertEqual(
                    loaded["artifacts"],
                    (("chat", "CHAT-1"), ("path", "docs/file.md")),
                )
                self.assertEqual(loaded["tags"], ("review", "urgent"))

    def test_store_rejects_invalid_normalized_sets_before_write(self) -> None:
        invalid_sets = (
            ("empty recipients", {"recipients": []}),
            ("recipient type", {"recipients": [object()]}),
            (
                "recipient cap",
                {"recipients": [f"agent_{index:03d}" for index in range(257)]},
            ),
            ("artifact shape", {"artifacts": ["not-a-pair"]}),
            ("artifact reference type", {"artifacts": [("chat", object())]}),
            (
                "artifact cap",
                {"artifacts": [("chat", f"CHAT-{index}") for index in range(257)]},
            ),
            ("tag type", {"tags": [object()]}),
            ("tag cap", {"tags": [f"tag-{index}" for index in range(65)]}),
        )
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                for label, changes in invalid_sets:
                    with self.subTest(label=label), self.assertRaises(ValueError):
                        store.create_canonical_message(**intent(**changes))
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_bodies"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_messages"
                    ).fetchone()[0],
                    0,
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

    def test_equivalent_candidate_does_not_mask_cross_scope_id_conflict(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _ = create_or_return_equivalent(store, **intent())
                body_sha256 = hashlib.sha256(b"hello").hexdigest()
                store._connection.execute("BEGIN IMMEDIATE")
                store._connection.execute(
                    "INSERT INTO canonical_message_recipients "
                    "(workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, "project", OTHER_PROJECT, message_id, "agent_claude"),
                )
                store._connection.execute(
                    "INSERT INTO canonical_messages "
                    "(workspace_id, scope_kind, scope_identity, message_id, sender_agent_id, "
                    "dedupe_key, body_sha256, reply_to_message_id, ttl_seconds, ack_policy, "
                    "title, priority, chat_link, task_link, registry_revision, project_id, "
                    "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        OTHER_PROJECT,
                        message_id,
                        "agent_claude",
                        "cross-scope-collision",
                        body_sha256,
                        None,
                        0,
                        "none",
                        "Conflicting intent",
                        "normal",
                        None,
                        None,
                        REVISION,
                        OTHER_PROJECT,
                        NOW,
                    ),
                )
                store._connection.execute("COMMIT")

                with self.assertRaises(CanonicalConflictError):
                    create_or_return_equivalent(store, **intent())

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
                readers = (
                    (store.read_canonical_message, False),
                    (read_message, True),
                    (project_message_v1, True),
                )
                for reader, needs_store in readers:
                    with self.subTest(reader=reader.__name__), self.assertRaisesRegex(
                        CanonicalIntegrityError, "body failed"
                    ):
                        reader(
                            workspace_id=WORKSPACE,
                            scope_kind="project",
                            scope_identity=PROJECT,
                            message_id=message_id,
                            **({"store": store} if needs_store else {}),
                        )

    def test_child_first_publication_and_forged_whole_message_detection(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, WORKSPACE)) as store:
                record_registry(store)
                statements = []
                store._connection.set_trace_callback(statements.append)
                message_id, _ = create_or_return_equivalent(store, **intent())
                store._connection.set_trace_callback(None)
                inserts = [
                    " ".join(statement.split())
                    for statement in statements
                    if statement.lstrip().startswith("INSERT INTO canonical_")
                ]
                parent_index = next(
                    index
                    for index, statement in enumerate(inserts)
                    if statement.startswith("INSERT INTO canonical_messages ")
                )
                for family in (
                    "canonical_message_recipients",
                    "canonical_message_artifacts",
                    "canonical_message_tags",
                ):
                    self.assertLess(
                        next(
                            index
                            for index, statement in enumerate(inserts)
                            if statement.startswith(f"INSERT INTO {family} ")
                        ),
                        parent_index,
                    )

                forged_id = "msg_" + "f" * 64
                store._connection.execute("BEGIN IMMEDIATE")
                store._connection.execute(
                    "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, "project", PROJECT, forged_id, "agent_codex"),
                )
                store._connection.execute(
                    "INSERT INTO canonical_messages "
                    "SELECT workspace_id, scope_kind, scope_identity, ?, sender_agent_id, ?, "
                    "body_sha256, reply_to_message_id, ttl_seconds, ack_policy, title, priority, "
                    "chat_link, task_link, registry_revision, project_id, created_at_utc "
                    "FROM canonical_messages WHERE workspace_id = ? AND scope_kind = ? "
                    "AND scope_identity = ? AND message_id = ?",
                    (forged_id, "forged", WORKSPACE, "project", PROJECT, message_id),
                )
                store._connection.execute("COMMIT")

                readers = (
                    (store.read_canonical_message, False),
                    (read_message, True),
                    (project_message_v1, True),
                )
                for reader, needs_store in readers:
                    with self.subTest(reader=reader.__name__), self.assertRaisesRegex(
                        CanonicalIntegrityError, "does not match"
                    ):
                        reader(
                            workspace_id=WORKSPACE,
                            scope_kind="project",
                            scope_identity=PROJECT,
                            message_id=forged_id,
                            **({"store": store} if needs_store else {}),
                        )

    def test_mutation_store_body_validation_rejects_self_consistent_false_digest(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            with LedgerStore.open_writer(LedgerPaths.derive(tmp, WORKSPACE)) as store:
                record_registry(store)
                false_digest = "d" * 64
                false_body = b"forged body bytes"
                false_digest_id = messages_module._derive_message_id(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    sender_agent_id="agent_codex",
                    dedupe_key="false-body-digest",
                    body_sha256=false_digest,
                    recipients=("agent_codex",),
                    reply_to_message_id=None,
                    ttl_seconds=0,
                    ack_policy="none",
                    artifacts=(),
                    title="forged body",
                    priority="normal",
                    tags=(),
                    chat_link=None,
                    task_link=None,
                )
                store._connection.execute("BEGIN IMMEDIATE")
                store._connection.execute(
                    "INSERT INTO canonical_bodies VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, false_digest, len(false_body), false_body, NOW),
                )
                store._connection.execute(
                    "INSERT INTO canonical_message_recipients VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, "project", PROJECT, false_digest_id, "agent_codex"),
                )
                store._connection.execute(
                    "INSERT INTO canonical_messages VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        PROJECT,
                        false_digest_id,
                        "agent_codex",
                        "false-body-digest",
                        false_digest,
                        None,
                        0,
                        "none",
                        "forged body",
                        "normal",
                        None,
                        None,
                        REVISION,
                        PROJECT,
                        NOW,
                    ),
                )
                store._connection.execute("COMMIT")

                readers = (
                    (store.read_canonical_message, False),
                    (read_message, True),
                    (project_message_v1, True),
                )
                for reader, needs_store in readers:
                    with self.subTest(
                        false_digest_reader=reader.__name__
                    ), self.assertRaisesRegex(CanonicalIntegrityError, "body failed"):
                        reader(
                            workspace_id=WORKSPACE,
                            scope_kind="project",
                            scope_identity=PROJECT,
                            message_id=false_digest_id,
                            **({"store": store} if needs_store else {}),
                        )

                with self.assertRaises(CanonicalConflictError):
                    create_or_return_equivalent(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        sender_agent_id="agent_codex",
                        dedupe_key="false-body-digest",
                        body=false_body,
                        recipients=["agent_codex"],
                        registry_revision=REVISION,
                        created_at_utc=NOW,
                        title="forged body",
                    )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_messages WHERE dedupe_key = ?",
                        ("false-body-digest",),
                    ).fetchone()[0],
                    1,
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
                for table in (
                    "canonical_message_recipients",
                    "canonical_message_artifacts",
                    "canonical_message_tags",
                ):
                    self.assertEqual(
                        store._connection.execute(
                            f"SELECT count(*) FROM {table}"
                        ).fetchone()[0],
                        0,
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
                        store.create_canonical_message(**intent(**changes))
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_messages"
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
