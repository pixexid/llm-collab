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
import llm_collab.canonical.delivery as delivery_module
import llm_collab.canonical.messages as messages_module
import llm_collab.compatibility.projection as projection_module
import llm_collab.ledger.store as store_module
from llm_collab.canonical import (
    CanonicalConflictError,
    CanonicalIntegrityError,
    append_receipt,
    create_attempt,
    create_deliveries,
    create_or_return_equivalent,
    project_delivery_v1,
    project_message_v1,
    project_receipt_v1,
    read_message,
)
from llm_collab.compatibility import (
    project_chat_packet_v2,
    project_inbox_pointers_v2,
    project_legacy_manifest_provenance_v2,
)
from llm_collab.ledger import LedgerPaths, LedgerStore
from tests.test_collabd_store import legacy_manifest, manifest_entry


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


def state_evidence(
    *,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    endpoint_id: str,
    state: str,
    session_ref_id: str | None = None,
    correlation_id: str = "corr_alpha",
    observed_at_utc: str = NOW,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema_version": 1,
        "workspace_id": WORKSPACE,
        "scope": {"kind": "project", "project_id": PROJECT},
        "evidence_id": f"evidence_{correlation_id}",
        "evidence_kind": "native_delivery_state",
        "quality": "authoritative" if state in {"accepted", "completed"} else "best_effort",
        "state": state,
        "authority": {
            "authority_kind": "native_runtime",
            "identity": "agent_claude",
            "implementation_revision": "rev_v1",
            "capability_profile_id": "profile_claude",
            "capability_profile_revision": "profile_rev_v1",
        },
        "subject": {
            "message_id": message_id,
            "delivery_id": delivery_id,
            "attempt_id": attempt_id,
            "endpoint_id": endpoint_id,
        },
        "correlation_id": correlation_id,
        "observed_at_utc": observed_at_utc,
    }
    if session_ref_id is not None:
        evidence["subject"]["session_ref_id"] = session_ref_id  # type: ignore[index]
    projection = dict(evidence)
    projection.pop("integrity", None)
    body = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    evidence["integrity"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return evidence


def reseal_evidence(evidence: dict[str, object]) -> dict[str, object]:
    projection = dict(evidence)
    projection.pop("integrity", None)
    body = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    evidence["integrity"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return evidence


def assert_no_nulls(testcase: unittest.TestCase, value: object, path: str = "projection") -> None:
    testcase.assertIsNotNone(value, path)
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_nulls(testcase, item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_nulls(testcase, item, f"{path}[{index}]")


def traced_write_statements(store: LedgerStore) -> list[str]:
    statements: list[str] = []

    def trace(statement: str) -> None:
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if operation in {"INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN", "COMMIT", "ROLLBACK"}:
            statements.append(statement)

    store._connection.set_trace_callback(trace)
    return statements


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


_CanonicalMessageTestBase = CanonicalMessageTest


class CompatibilityProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        linked_version = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        linked_version.start()
        self.addCleanup(linked_version.stop)

    def test_p2d_chat_projection_omits_session_keys_and_writes_nothing(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store,
                    **intent(
                        recipients=["agent_claude"],
                        task_link="TASK-3402EB",
                    ),
                )
            with LedgerStore.open_reader(paths) as reader:
                statements = traced_write_statements(reader)
                try:
                    projected = project_chat_packet_v2(
                        reader,
                        workspace_id=WORKSPACE,
                        project_id=PROJECT,
                        message_id=message_id,
                    )
                finally:
                    reader._connection.set_trace_callback(None)

            self.assertEqual(statements, [])
            assert_no_nulls(self, projected)
            self.assertEqual(projected["body"], "hello")
            frontmatter = projected["frontmatter"]
            self.assertEqual(
                set(frontmatter),
                {
                    "canonical_projection",
                    "chat_id",
                    "from",
                    "path_targets",
                    "priority",
                    "project_id",
                    "related_task",
                    "sender_agent_id",
                    "sent_utc",
                    "tags",
                    "title",
                    "to",
                },
            )
            self.assertEqual(frontmatter["from"], "codex")
            self.assertEqual(frontmatter["to"], "claude")
            self.assertNotIn("sender_session_id", frontmatter)
            self.assertNotIn("target_session_id", frontmatter)
            self.assertNotIn("supersedes_session_id", frontmatter)
            self.assertEqual(
                frontmatter["canonical_projection"]["lossy_fields_omitted"],  # type: ignore[index]
                [
                    "sender_session_id",
                    "target_session_id",
                    "supersedes_session_id",
                ],
            )
            with LedgerStore.open_reader(paths) as reader:
                with self.assertRaises(ValueError):
                    project_chat_packet_v2(
                        reader,
                        workspace_id=WORKSPACE,
                        project_id=None,  # type: ignore[arg-type]
                        message_id=message_id,
                    )
                with self.assertRaises(KeyError):
                    project_chat_packet_v2(
                        reader,
                        workspace_id=WORKSPACE,
                        project_id=OTHER_PROJECT,
                        message_id=message_id,
                    )

    def test_p2d_inbox_projection_filters_exact_project_and_claims_no_read_state(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                foreign_id, _created = create_or_return_equivalent(
                    store,
                    **intent(
                        scope_identity=OTHER_PROJECT,
                        dedupe_key="foreign",
                        recipients=["agent_claude"],
                        chat_link="CHAT-FOREIGN",
                    ),
                )
                create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=OTHER_PROJECT,
                    message_id=foreign_id,
                    routes=[("agent_claude", "endpoint_foreign")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
            with LedgerStore.open_reader(paths) as reader:
                statements = traced_write_statements(reader)
                try:
                    projected = project_inbox_pointers_v2(
                        reader,
                        workspace_id=WORKSPACE,
                        project_id=PROJECT,
                        recipient_agent_id="agent_claude",
                    )
                finally:
                    reader._connection.set_trace_callback(None)

            self.assertEqual(statements, [])
            assert_no_nulls(self, projected)
            self.assertNotIn("read", projected)
            self.assertNotIn("unread", projected)
            self.assertEqual(projected["read_state_authority"], "not_projected")
            self.assertEqual(projected["acknowledgment_authority"], "not_inferred")
            self.assertEqual(len(projected["pointers"]), 1)
            pointer = projected["pointers"][0]  # type: ignore[index]
            self.assertEqual(pointer["message_id"], message_id)
            self.assertEqual(pointer["delivery_id"], delivery_id)
            self.assertEqual(pointer["read_state"], "not_projected")
            self.assertEqual(pointer["acknowledgment"], "not_inferred")
            self.assertNotIn(OTHER_PROJECT, pointer["locator"])
            with LedgerStore.open_reader(paths) as reader:
                with self.assertRaises(ValueError):
                    project_inbox_pointers_v2(
                        reader,
                        workspace_id=WORKSPACE,
                        project_id=None,  # type: ignore[arg-type]
                        recipient_agent_id="agent_claude",
                    )

    def test_p2d_manifest_provenance_is_labelled_and_omits_null_record_fields(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            payload = b'{"agent":"claude","unread":[],"read":[]}'
            entry = manifest_entry(
                "/agents/claude/inbox.json",
                payload,
                evidence_form_version="v2_inbox_index",
            )
            manifest = legacy_manifest([entry])
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                store.record_legacy_import_manifest(
                    workspace_id=WORKSPACE,
                    manifest=manifest,
                    records=[
                        {
                            "entry_integrity": entry["integrity"],
                            "record_kind": "inbox_pointer",
                            "scope_kind": None,
                            "scope_identity": None,
                            "message_id": None,
                        }
                    ],
                    imported_at_utc=NOW,
                )
            with LedgerStore.open_reader(paths) as reader:
                statements = traced_write_statements(reader)
                try:
                    projected = project_legacy_manifest_provenance_v2(
                        reader,
                        workspace_id=WORKSPACE,
                        manifest_id="manifest_alpha",
                    )
                finally:
                    reader._connection.set_trace_callback(None)

            self.assertEqual(statements, [])
            assert_no_nulls(self, projected)
            self.assertEqual(
                projected["publication"]["provenance_label"],  # type: ignore[index]
                projection_module.UNAUTHENTICATED_PROVENANCE,
            )
            self.assertEqual(
                projected["publication"]["publisher"]["provenance_label"],  # type: ignore[index]
                projection_module.UNAUTHENTICATED_PROVENANCE,
            )
            self.assertEqual(
                projected["publication"]["source_boundary"]["provenance_label"],  # type: ignore[index]
                projection_module.UNAUTHENTICATED_PROVENANCE,
            )
            self.assertEqual(
                projected["entries"][0]["provenance_label"],  # type: ignore[index]
                projection_module.UNAUTHENTICATED_PROVENANCE,
            )
            self.assertEqual(projected["records"], [{"entry_integrity": entry["integrity"], "record_kind": "inbox_pointer"}])

    def test_p2d_schema_sql_hashes_remain_at_p2c_base(self) -> None:
        expected = {
            "V1_SQL": "d3d65de464559984dc166a2ba5b9d0585f6831ae73843cd5adb7681a9d60bcfc",
            "V2_SQL": "cb64959d0173e133f5ccfdaa0b085fc0d89c22128e81188f5ad72733b51cdc00",
            "V3_SQL": "7dd6f8a09ad1caf4ee41134006526b7d9188e3a4ab4c9fb598f85c93a1d6f087",
            "V4_SQL": "fc9de2db3e4e3340b7a8bbd51121c534d8717a6b6ce5c102cef341b11c9dddaf",
            "V5_SQL": "eae06938359660ded4c99531b46e2de2cc29b8785feb36bcc8bc0fd47a9247be",
            "V6_SQL": "225ece18916fa29ceb40bb72543bf499c42a31a3cd0d38114be0def830570b44",
        }
        self.assertEqual(store_module.SCHEMA_VERSION, 6)
        self.assertEqual(
            {
                name: hashlib.sha256("\n".join(getattr(store_module, name)).encode()).hexdigest()
                for name in expected
            },
            expected,
        )
        self.assertEqual(
            (
                store_module.V1_MIGRATION_CHECKSUM,
                store_module.V2_MIGRATION_CHECKSUM,
                store_module.V3_MIGRATION_CHECKSUM,
                store_module.V4_MIGRATION_CHECKSUM,
                store_module.V5_MIGRATION_CHECKSUM,
                store_module.V6_MIGRATION_CHECKSUM,
                store_module.V1_SCHEMA_FINGERPRINT,
                store_module.V2_SCHEMA_FINGERPRINT,
                store_module.V3_SCHEMA_FINGERPRINT,
                store_module.V4_SCHEMA_FINGERPRINT,
                store_module.V5_SCHEMA_FINGERPRINT,
                store_module.V6_SCHEMA_FINGERPRINT,
            ),
            (
                "sha256:ce236daff444f736e01f3666ed44baf1c3ba17e81215fedb638276aff76b01c7",
                "sha256:338a5d526b6fdea47af667c469897fd38d97a4a2dc8caf90dc5d62c067610e36",
                "sha256:1b8380593b73695bf8824425b58eda7c94f51fc0937f07dbcbd1786a6e5d467b",
                "sha256:63f00990d9c3e01384d14d7613c961856ff48037504b1e0ada1f95b034cedf01",
                "sha256:d6498cf5728ec3d56c0d1360a065243d72384a0de50af55bead8054881bbd9b9",
                "sha256:56e7ca2ba9eb0a8eb79079372abdc7a39c024977e71a40931b8b60a6acc33c00",
                "sha256:26a856329406e45d22a8fbecdbd769d9c632acae3652d8c72438d228de7cfca2",
                "sha256:805aa5ae43c31d85dbe9a84590050b701ddc69cfe1dd225e9c6e67afbd889a7c",
                "sha256:88e59c9be91df366c03985f99f8b3db1c68382b4846612c0334fd15cc505e673",
                "sha256:665e17152991c6c21cb8756a5d5720e35e3154d13a4a069b4c74440ed425b39e",
                "sha256:4495eab6339d339b770442d994b5878e0743d011917cc99b370991a793891a99",
                "sha256:eb8bc4ddd4348ce05874b91c63ce963c5bb3653636363b7437e2046900996d60",
            ),
        )

    def test_p2d_ast_import_graph_has_no_bin_path_to_projection(self) -> None:
        root = Path(__file__).parents[1]
        modules: dict[str, Path] = {}
        for base in (root / "bin", root / "llm_collab"):
            for source in base.rglob("*.py"):
                relative = source.relative_to(root).with_suffix("")
                if relative.parts[-1] == "__init__":
                    module = ".".join(relative.parts[:-1])
                else:
                    module = ".".join(relative.parts)
                modules[module] = source

        graph: dict[str, set[str]] = {module: set() for module in modules}

        def resolve_relative(module: str, level: int, tail: str | None) -> str:
            parts = module.split(".")
            package = parts[:-1]
            prefix = package[: len(package) - level + 1]
            return ".".join((*prefix, *((tail or "").split(".") if tail else ())))

        for module, source in modules.items():
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        graph[module].add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    imported = resolve_relative(module, node.level, node.module) if node.level else (node.module or "")
                    if imported:
                        graph[module].add(imported)
                        for alias in node.names:
                            graph[module].add(f"{imported}.{alias.name}")

        targets = {"llm_collab.compatibility.projection"}
        reached: list[tuple[str, str]] = []
        for start in sorted(module for module in modules if module.startswith("bin.")):
            stack = [(start, start)]
            seen = set()
            while stack:
                node, path = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                if node in targets or any(node.startswith(target + ".") for target in targets):
                    reached.append((start, path))
                    break
                for child in graph.get(node, ()):
                    stack.append((child, f"{path} -> {child}"))
        self.assertEqual(reached, [])


class CanonicalMessageTest(_CanonicalMessageTestBase):

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

    def test_delivery_routes_are_recipient_scoped_and_deadline_enforced(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, created = create_or_return_equivalent(
                    store,
                    **intent(ttl_seconds=1, recipients=["agent_claude"]),
                )
                self.assertTrue(created)
                deliveries = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                self.assertEqual(len(deliveries), 1)
                delivery_id, created = deliveries[0]
                self.assertTrue(created)
                self.assertRegex(delivery_id, r"^delivery_[0-9a-f]{64}$")
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_bodies"
                    ).fetchone()[0],
                    1,
                )

                non_recipient_delivery_id = store_module._derive_delivery_id(
                    WORKSPACE,
                    "project",
                    PROJECT,
                    message_id,
                    "agent_other",
                    "endpoint_other_desktop",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        "INSERT INTO canonical_deliveries "
                        "(workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
                        "recipient_agent_id, endpoint_id, deadline_epoch_ms, created_at_utc) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            non_recipient_delivery_id,
                            "agent_other",
                            "endpoint_other_desktop",
                            2_000,
                            NOW,
                        ),
                    )

                attempt_id, created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_999,
                    created_at_utc=NOW,
                )
                self.assertTrue(created)
                self.assertRegex(attempt_id, r"^attempt_[0-9a-f]{64}$")
                with self.assertRaisesRegex(sqlite3.IntegrityError, "expired"):
                    create_attempt(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_index=1,
                        attempt_epoch_ms=2_000,
                        created_at_utc=NOW,
                    )
                receipt_id, created = append_receipt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    evidence=state_evidence(
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        endpoint_id="endpoint_claude_desktop",
                        state="acknowledged",
                    ),
                    created_at_utc=NOW,
                )
                self.assertTrue(created)
                self.assertRegex(receipt_id, r"^receipt_[0-9a-f]{64}$")

    def test_delivery_retry_returns_existing_route_without_rewriting_metadata(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store,
                    **intent(ttl_seconds=1, recipients=["agent_claude"]),
                )
                ((delivery_id, created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                self.assertTrue(created)
                ((same_delivery_id, created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=9_000,
                    created_at_utc="2026-07-22T00:00:09+00:00",
                )
                self.assertEqual((same_delivery_id, created), (delivery_id, False))
                self.assertEqual(
                    store._connection.execute(
                        "SELECT deadline_epoch_ms, created_at_utc FROM canonical_deliveries "
                        "WHERE delivery_id = ?",
                        (delivery_id,),
                    ).fetchone(),
                    (2_000, NOW),
                )

    def test_delivery_receipt_fold_integrity_projection_and_derivation_are_shared(self) -> None:
        self.assertIs(delivery_module._derive_delivery_id, store_module._derive_delivery_id)
        self.assertIs(delivery_module._derive_attempt_id, store_module._derive_attempt_id)
        self.assertIs(delivery_module._derive_receipt_id, store_module._derive_receipt_id)
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                completed = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="completed",
                    session_ref_id="session_claude_alpha",
                    correlation_id="corr_completed",
                )
                receipt_id, created = append_receipt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    evidence=completed,
                    session_ref_id="session_claude_alpha",
                    created_at_utc=NOW,
                )
                self.assertTrue(created)
                same_receipt, created = append_receipt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    evidence=completed,
                    session_ref_id="session_claude_alpha",
                    created_at_utc=NOW,
                )
                self.assertEqual((same_receipt, created), (receipt_id, False))
                ambiguous = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="ambiguous",
                    correlation_id="corr_ambiguous",
                )
                append_receipt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    evidence=ambiguous,
                    created_at_utc=NOW,
                )

                projected_delivery = project_delivery_v1(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                )
                self.assertEqual(projected_delivery["outcome"], "completed")
                self.assertEqual(projected_delivery["attempt_id"], attempt_id)
                self.assertEqual(projected_delivery["evidence"], completed)
                self.assertEqual(projected_delivery["session_ref_id"], "session_claude_alpha")
                self.assertEqual(
                    set(projected_delivery),
                    {
                        "schema_version",
                        "workspace_id",
                        "scope",
                        "delivery_id",
                        "message_id",
                        "attempt_id",
                        "endpoint_id",
                        "session_ref_id",
                        "outcome",
                        "evidence",
                    },
                )
                projected_receipt = project_receipt_v1(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    receipt_id=receipt_id,
                )
                self.assertEqual(projected_receipt["state"], "completed")
                self.assertEqual(projected_receipt["session_ref_id"], "session_claude_alpha")
                Draft202012Validator(
                    json.loads(
                        (
                            Path(__file__).parents[1]
                            / "schemas/standalone/v1/state-evidence.schema.json"
                        ).read_text()
                    )
                ).validate(projected_receipt["evidence"])

                forged = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="completed",
                    session_ref_id="session_claude_alpha",
                    correlation_id="corr_forged",
                )
                body, evidence_sha256, state, quality, kind = store_module._normalize_evidence(
                    forged
                )
                forged_receipt_id = store_module._derive_receipt_id(
                    WORKSPACE,
                    "project",
                    PROJECT,
                    message_id,
                    delivery_id,
                    attempt_id,
                    evidence_sha256,
                )
                store._connection.execute(
                    "INSERT INTO canonical_evidence_bodies VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, evidence_sha256, len(body), body, NOW),
                )
                store._connection.execute(
                    "INSERT INTO canonical_delivery_receipts "
                    "(workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
                    "attempt_id, receipt_id, evidence_sha256, state, quality, evidence_kind, "
                    "session_ref_id, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        delivery_id,
                        attempt_id,
                        forged_receipt_id,
                        evidence_sha256,
                        "ambiguous",
                        quality,
                        kind,
                        "session_claude_alpha",
                        NOW,
                    ),
                )
                with self.assertRaises(CanonicalIntegrityError):
                    store.read_canonical_receipt(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        receipt_id=forged_receipt_id,
                    )

    def test_receipt_evidence_scope_and_terminal_authority_are_rejected_before_write(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                foreign_scope = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="acknowledged",
                )
                foreign_scope["scope"] = {"kind": "project", "project_id": OTHER_PROJECT}
                reseal_evidence(foreign_scope)
                terminal_best_effort = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="completed",
                    session_ref_id="session_claude_alpha",
                    correlation_id="corr_bad_terminal",
                )
                terminal_best_effort["quality"] = "best_effort"
                terminal_best_effort["evidence_kind"] = "adapter_observation"
                reseal_evidence(terminal_best_effort)
                missing_required = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="processing",
                    correlation_id="corr_missing_required",
                )
                del missing_required["authority"]
                reseal_evidence(missing_required)
                invalid_extensions = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="processing",
                    correlation_id="corr_invalid_extensions",
                )
                invalid_extensions["extensions"] = {"invalid_key": {"nested": "value"}}
                reseal_evidence(invalid_extensions)
                cases = (
                    (foreign_scope, None, "scope mismatch"),
                    (terminal_best_effort, "session_claude_alpha", "authoritative"),
                    (missing_required, None, "missing required"),
                    (invalid_extensions, None, "extension"),
                )
                for evidence, session_ref_id, error in cases:
                    with self.subTest(error=error), self.assertRaisesRegex(
                        CanonicalIntegrityError, error
                    ):
                        append_receipt(
                            store,
                            workspace_id=WORKSPACE,
                            scope_kind="project",
                            scope_identity=PROJECT,
                            message_id=message_id,
                            delivery_id=delivery_id,
                            attempt_id=attempt_id,
                            evidence=evidence,
                            session_ref_id=session_ref_id,
                            created_at_utc=NOW,
                        )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_evidence_bodies"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_delivery_receipts"
                    ).fetchone()[0],
                    0,
                )

    def test_projection_refuses_receiptless_delivery_v1(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                with self.assertRaisesRegex(CanonicalIntegrityError, "receipt-backed"):
                    project_delivery_v1(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                    )
                create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                with self.assertRaisesRegex(CanonicalIntegrityError, "receipt-backed"):
                    project_delivery_v1(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                    )

    def test_read_paths_rederive_v5_identities_from_direct_sql_rows(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                forged_delivery_id = "delivery_" + "f" * 64
                store._connection.execute(
                    "INSERT INTO canonical_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        forged_delivery_id,
                        "agent_claude",
                        "endpoint_forged_route",
                        0,
                        NOW,
                    ),
                )
                with self.assertRaisesRegex(CanonicalIntegrityError, "delivery_id"):
                    store.read_canonical_delivery(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=forged_delivery_id,
                    )

                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                forged_attempt_id = "attempt_" + "e" * 64
                store._connection.execute(
                    "INSERT INTO canonical_delivery_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        delivery_id,
                        forged_attempt_id,
                        0,
                        1_100,
                        NOW,
                    ),
                )
                forged_attempt_evidence = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=forged_attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="acknowledged",
                    correlation_id="corr_forged_attempt",
                )
                body, evidence_sha256, state, quality, kind = store_module._normalize_evidence(
                    forged_attempt_evidence
                )
                forged_attempt_receipt_id = store_module._derive_receipt_id(
                    WORKSPACE,
                    "project",
                    PROJECT,
                    message_id,
                    delivery_id,
                    forged_attempt_id,
                    evidence_sha256,
                )
                store._connection.execute(
                    "INSERT INTO canonical_evidence_bodies VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, evidence_sha256, len(body), body, NOW),
                )
                store._connection.execute(
                    "INSERT INTO canonical_delivery_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        delivery_id,
                        forged_attempt_id,
                        forged_attempt_receipt_id,
                        evidence_sha256,
                        state,
                        quality,
                        kind,
                        None,
                        NOW,
                    ),
                )
                with self.assertRaisesRegex(CanonicalIntegrityError, "attempt_id"):
                    store.read_canonical_receipt(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=forged_attempt_id,
                        receipt_id=forged_attempt_receipt_id,
                    )

                proper_attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=1,
                    attempt_epoch_ms=1_200,
                    created_at_utc=NOW,
                )
                forged_receipt_evidence = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=proper_attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="processing",
                    correlation_id="corr_forged_receipt",
                )
                body, evidence_sha256, state, quality, kind = store_module._normalize_evidence(
                    forged_receipt_evidence
                )
                forged_receipt_id = "receipt_" + "d" * 64
                store._connection.execute(
                    "INSERT INTO canonical_evidence_bodies VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, evidence_sha256, len(body), body, NOW),
                )
                store._connection.execute(
                    "INSERT INTO canonical_delivery_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        delivery_id,
                        proper_attempt_id,
                        forged_receipt_id,
                        evidence_sha256,
                        state,
                        quality,
                        kind,
                        None,
                        NOW,
                    ),
                )
                with self.assertRaisesRegex(CanonicalIntegrityError, "receipt_id"):
                    store.read_canonical_receipt(
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=proper_attempt_id,
                        receipt_id=forged_receipt_id,
                    )

    def test_receipt_read_revalidates_evidence_scope_and_authority_from_direct_sql(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                cases = []
                foreign_scope = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="acknowledged",
                    correlation_id="corr_foreign_scope",
                )
                foreign_scope["scope"] = {"kind": "project", "project_id": OTHER_PROJECT}
                cases.append((reseal_evidence(foreign_scope), None, "scope mismatch"))
                terminal_best_effort = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="completed",
                    session_ref_id="session_claude_alpha",
                    correlation_id="corr_direct_terminal",
                )
                terminal_best_effort["quality"] = "best_effort"
                terminal_best_effort["evidence_kind"] = "adapter_observation"
                cases.append(
                    (
                        reseal_evidence(terminal_best_effort),
                        "session_claude_alpha",
                        "authoritative",
                    )
                )
                missing_required = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="processing",
                    correlation_id="corr_direct_missing_required",
                )
                del missing_required["authority"]
                cases.append((reseal_evidence(missing_required), None, "missing required"))
                invalid_extensions = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="processing",
                    correlation_id="corr_direct_invalid_extensions",
                )
                invalid_extensions["extensions"] = {"invalid_key": {"nested": "value"}}
                cases.append((reseal_evidence(invalid_extensions), None, "extension"))
                for evidence, session_ref_id, error in cases:
                    body, evidence_sha256, state, quality, kind = store_module._normalize_evidence(
                        evidence
                    )
                    receipt_id = store_module._derive_receipt_id(
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        delivery_id,
                        attempt_id,
                        evidence_sha256,
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_evidence_bodies VALUES (?, ?, ?, ?, ?)",
                        (WORKSPACE, evidence_sha256, len(body), body, NOW),
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_delivery_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            delivery_id,
                            attempt_id,
                            receipt_id,
                            evidence_sha256,
                            state,
                            quality,
                            kind,
                            session_ref_id,
                            NOW,
                        ),
                    )
                    with self.subTest(error=error), self.assertRaisesRegex(
                        CanonicalIntegrityError, error
                    ):
                        store.read_canonical_receipt(
                            workspace_id=WORKSPACE,
                            scope_kind="project",
                            scope_identity=PROJECT,
                            message_id=message_id,
                            delivery_id=delivery_id,
                            attempt_id=attempt_id,
                            receipt_id=receipt_id,
                        )

    def test_delivery_equal_rank_tie_selects_lexicographically_smallest_receipt_id(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                receipts: dict[str, dict[str, object]] = {}
                for correlation_id in ("corr_ack_alpha", "corr_ack_beta"):
                    evidence = state_evidence(
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        endpoint_id="endpoint_claude_desktop",
                        state="acknowledged",
                        correlation_id=correlation_id,
                    )
                    receipt_id, created = append_receipt(
                        store,
                        workspace_id=WORKSPACE,
                        scope_kind="project",
                        scope_identity=PROJECT,
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        evidence=evidence,
                        created_at_utc=NOW,
                    )
                    self.assertTrue(created)
                    receipts[receipt_id] = evidence
                selected_receipt_id = min(receipts)
                self.assertGreater(len(set(receipts)), 1)
                projected = project_delivery_v1(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                )
                self.assertEqual(projected["outcome"], "pending")
                self.assertEqual(projected["evidence"], receipts[selected_receipt_id])

    def test_v5_tables_are_behaviorally_append_only_under_direct_sql(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                append_receipt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    evidence=state_evidence(
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        endpoint_id="endpoint_claude_desktop",
                        state="acknowledged",
                    ),
                    created_at_utc=NOW,
                )
                for table in (
                    "canonical_evidence_bodies",
                    "canonical_deliveries",
                    "canonical_delivery_attempts",
                    "canonical_delivery_receipts",
                ):
                    with self.subTest(table=table, operation="update"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "append-only"
                    ):
                        store._connection.execute(f"UPDATE {table} SET rowid = rowid")
                    with self.subTest(table=table, operation="delete"), self.assertRaisesRegex(
                        sqlite3.IntegrityError, "append-only"
                    ):
                        store._connection.execute(f"DELETE FROM {table}")

    def test_state_evidence_canonical_json_rejects_noncanonical_values_before_write(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                message_id, _created = create_or_return_equivalent(
                    store, **intent(recipients=["agent_claude"])
                )
                ((delivery_id, _created),) = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[("agent_claude", "endpoint_claude_desktop")],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                attempt_id, _created = create_attempt(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_index=0,
                    attempt_epoch_ms=1_100,
                    created_at_utc=NOW,
                )
                base = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_claude_desktop",
                    state="processing",
                )
                cases = (
                    ("float", {"extensions": {"x_note_value": 1.5}}),
                    ("nan", {"extensions": {"x_note_value": float("nan")}}),
                    ("unsafe integer", {"extensions": {"x_note_value": 9_007_199_254_740_992}}),
                    ("control", {"extensions": {"x_note_value": "bad\u0001"}}),
                    ("surrogate", {"extensions": {"x_note_value": "\ud800"}}),
                )
                for label, mutation in cases:
                    evidence = dict(base)
                    evidence.update(mutation)
                    projection = dict(evidence)
                    projection.pop("integrity", None)
                    try:
                        body = json.dumps(
                            projection,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8", "surrogatepass")
                    except ValueError:
                        body = b"unserializable"
                    evidence["integrity"] = "sha256:" + hashlib.sha256(body).hexdigest()
                    with self.subTest(label=label), self.assertRaises(ValueError):
                        append_receipt(
                            store,
                            workspace_id=WORKSPACE,
                            scope_kind="project",
                            scope_identity=PROJECT,
                            message_id=message_id,
                            delivery_id=delivery_id,
                            attempt_id=attempt_id,
                            evidence=evidence,
                            created_at_utc=NOW,
                        )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_evidence_bodies"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    store._connection.execute(
                        "SELECT count(*) FROM canonical_delivery_receipts"
                    ).fetchone()[0],
                    0,
                )

    def test_delivery_count_caps_fire_by_direct_sql(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(tmp, WORKSPACE)
            with LedgerStore.open_writer(paths) as store:
                record_registry(store)
                recipients = [f"agent_r{index:03d}" for index in range(256)]
                message_id, _created = create_or_return_equivalent(
                    store,
                    **intent(
                        recipients=recipients,
                        ttl_seconds=0,
                        ack_policy="none",
                        dedupe_key="cap-test",
                    ),
                )
                delivery_rows = create_deliveries(
                    store,
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    message_id=message_id,
                    routes=[
                        (recipient, f"endpoint_route_{index:03d}")
                        for index, recipient in enumerate(recipients)
                    ],
                    now_epoch_ms=1_000,
                    created_at_utc=NOW,
                )
                self.assertEqual(len(delivery_rows), 256)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "delivery count"):
                    extra_delivery_id = store_module._derive_delivery_id(
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        recipients[0],
                        "endpoint_extra_route",
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            extra_delivery_id,
                            recipients[0],
                            "endpoint_extra_route",
                            0,
                            NOW,
                        ),
                    )

                delivery_id = delivery_rows[0][0]
                for index in range(64):
                    attempt_id = store_module._derive_attempt_id(
                        WORKSPACE, "project", PROJECT, message_id, delivery_id, index
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_delivery_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            delivery_id,
                            attempt_id,
                            index,
                            1_000 + index,
                            NOW,
                        ),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "attempt count"):
                    attempt_id = store_module._derive_attempt_id(
                        WORKSPACE, "project", PROJECT, message_id, delivery_id, 64
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_delivery_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            delivery_id,
                            attempt_id,
                            64,
                            1_064,
                            NOW,
                        ),
                    )

                attempt_id = store_module._derive_attempt_id(
                    WORKSPACE, "project", PROJECT, message_id, delivery_id, 0
                )
                for index in range(256):
                    evidence = state_evidence(
                        message_id=message_id,
                        delivery_id=delivery_id,
                        attempt_id=attempt_id,
                        endpoint_id="endpoint_route_000",
                        state="processing",
                        correlation_id=f"corr_receipt_{index:03d}",
                    )
                    body, evidence_sha256, state, quality, kind = store_module._normalize_evidence(
                        evidence
                    )
                    receipt_id = store_module._derive_receipt_id(
                        WORKSPACE,
                        "project",
                        PROJECT,
                        message_id,
                        delivery_id,
                        attempt_id,
                        evidence_sha256,
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_evidence_bodies VALUES (?, ?, ?, ?, ?)",
                        (WORKSPACE, evidence_sha256, len(body), body, NOW),
                    )
                    store._connection.execute(
                        "INSERT INTO canonical_delivery_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            delivery_id,
                            attempt_id,
                            receipt_id,
                            evidence_sha256,
                            state,
                            quality,
                            kind,
                            None,
                            NOW,
                        ),
                    )
                evidence = state_evidence(
                    message_id=message_id,
                    delivery_id=delivery_id,
                    attempt_id=attempt_id,
                    endpoint_id="endpoint_route_000",
                    state="processing",
                    correlation_id="corr_receipt_256",
                )
                body, evidence_sha256, state, quality, kind = store_module._normalize_evidence(
                    evidence
                )
                receipt_id = store_module._derive_receipt_id(
                    WORKSPACE,
                    "project",
                    PROJECT,
                    message_id,
                    delivery_id,
                    attempt_id,
                    evidence_sha256,
                )
                store._connection.execute(
                    "INSERT INTO canonical_evidence_bodies VALUES (?, ?, ?, ?, ?)",
                    (WORKSPACE, evidence_sha256, len(body), body, NOW),
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt count"):
                    store._connection.execute(
                        "INSERT INTO canonical_delivery_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            WORKSPACE,
                            "project",
                            PROJECT,
                            message_id,
                            delivery_id,
                            attempt_id,
                            receipt_id,
                            evidence_sha256,
                            state,
                            quality,
                            kind,
                            None,
                            NOW,
                        ),
                    )

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
                "append_receipt",
                "create_attempt",
                "create_deliveries",
                "create_or_return_equivalent",
                "project_delivery_v1",
                "project_message_v1",
                "project_receipt_v1",
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


del _CanonicalMessageTestBase


if __name__ == "__main__":
    unittest.main()
