from __future__ import annotations
import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)

import ast
import hashlib
import inspect
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.ledger import LedgerPaths, LedgerStore
import llm_collab.ledger.store as store_module
from llm_collab.ledger.store import CanonicalConflictError
from llm_collab.codex_app_server_live_probe import CodexAppServerExactThreadResult
from llm_collab.session_lifecycle import (
    CodexLifecycleProvider,
    FakeLifecycleProvider,
    LifecycleSubject,
    ManagedStartOrphaned,
    ManagedStartRequest,
    ManagedStartResponseLost,
    validate_codex_start_evidence,
    SessionLifecycleCore,
    SessionLifecycleError,
    TrustedProjectRoot,
    codex_start_evidence_digest,
)


WORKSPACE = "ws_alpha"
PROJECT = "amiga"
OTHER_PROJECT = "nuvyr"
TEST_REGISTRY_REVISION = "sha256:" + hashlib.sha256(b"test-registry-provision").hexdigest()
NOW = "2026-07-23T00:00:00+00:00"
BEFORE_EXPIRY = "2026-07-23T00:00:59+00:00"
AT_EXPIRY = "2026-07-23T00:01:00+00:00"
SAFE_VERSION = (3, 51, 3)
OPERATOR_INSPECTION_KEYS = {
    "projection_kind",
    "authority",
    "resolved",
    "reason",
    "workspace_id",
    "scope_kind",
    "scope_identity",
    "conversation_id",
    "participant_id",
    "binding_id",
    "generation",
    "state",
    "mutation_capable",
    "provider_id",
    "provider_revision",
    "endpoint_id",
    "session_ref_id",
    "native_session_id",
    "runtime_instance_id",
}
LIFECYCLE_ROW_COUNT_TABLES = (
    "conversation_participants",
    "lifecycle_provider_registry",
    "conversation_bindings",
    "session_binding_challenges",
    "canonical_delivery_attempt_binding_freezes",
    "conversation_binding_transition_audit",
    "legacy_provenance_imports",
    "legacy_autobridge_provenance_imports",
    "managed_start_reservations",
)
WRITE_SQL_PREFIXES = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "VACUUM",
    "ATTACH",
    "DETACH",
)


def frame(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return b"\x01" + len(encoded).to_bytes(8, "big") + encoded


def expected_binding_id(*, session_ref_id: str, generation: int) -> str:
    fields = (
        "conversation-binding-v1",
        WORKSPACE,
        "project",
        PROJECT,
        "CHAT-SAMEID",
        "participant_codex",
        str(generation),
        "provider_codex",
        "revision_1",
        "endpoint_codex",
        session_ref_id,
        "native_session_one",
        "runtime_one",
    )
    return "binding_" + hashlib.sha256(b"".join(frame(value) for value in fields)).hexdigest()


def row_counts(store: LedgerStore) -> dict[str, int]:
    return {
        table: store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in LIFECYCLE_ROW_COUNT_TABLES
    }


def subject(**changes: str) -> LifecycleSubject:
    values = {
        "workspace_id": WORKSPACE,
        "scope_kind": "project",
        "scope_identity": PROJECT,
        "conversation_id": "CHAT-SAMEID",
        "participant_id": "participant_codex",
        "agent_id": "agent_codex",
        "endpoint_id": "endpoint_codex",
        "native_session_id": "native_session_one",
        "runtime_instance_id": "runtime_one",
    }
    values.update(changes)
    return LifecycleSubject(**values)


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir()
        self.repo = root / "repo"
        self.repo.mkdir()
        self.cwd = self.repo / "work"
        self.cwd.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.runtime_home = bind_runtime_home(self.codex_home)
        self.trusted_root = TrustedProjectRoot(PROJECT, "repo_app", str(self.repo), str(self.cwd))
        patcher = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = LedgerPaths.derive(root / "state", WORKSPACE)
        self.paths2 = LedgerPaths.derive(root / "state2", WORKSPACE)
        self.paths3 = LedgerPaths.derive(root / "state3", WORKSPACE)
        for _paths in (self.paths, self.paths2, self.paths3):
            with LedgerStore.open_writer(_paths) as _store:
                self._register_projects(_store)
        self.core = SessionLifecycleCore(
            FakeLifecycleProvider(), token_factory=lambda: "token-alpha"
        )

    def reserve(self, store: LedgerStore, active_subject: LifecycleSubject):
        self.provision(store, active_subject, self.core.provider)
        return self.core.reserve(
            store,
            active_subject,
            runtime_home=self.runtime_home,
            created_at_utc=NOW,
            expires_at_utc=AT_EXPIRY,
            correlation_id="corr_reserve",
            trusted_project_root=self.trusted_root,
        )

    def _register_projects(self, store: LedgerStore) -> None:
        # Idempotent test-setup: register PROJECT + OTHER_PROJECT via the proper
        # record_registry_snapshot (FK-safe). Trusted-operator authority (tests).
        already = store._connection.execute(
            "SELECT 1 FROM project_registry_snapshots "
            "WHERE workspace_id = ? AND project_id = ? LIMIT 1",
            (WORKSPACE, PROJECT),
        ).fetchone()
        if already is not None:
            return
        store.record_registry_snapshot(
            workspace_id=WORKSPACE,
            registry_revision=TEST_REGISTRY_REVISION,
            registry_source_sha256=TEST_REGISTRY_REVISION.split(":", 1)[1],
            captured_at_utc=NOW,
            workspace_snapshot_json=json.dumps({
                "workspace_id": WORKSPACE,
                "projects": [PROJECT, OTHER_PROJECT],
            }),
            project_snapshots={
                PROJECT: json.dumps({"id": PROJECT}),
                OTHER_PROJECT: json.dumps({"id": OTHER_PROJECT}),
            },
            source_snapshots={},
        )

    def provision(
        self,
        store: LedgerStore,
        active_subject: LifecycleSubject,
        provider: FakeLifecycleProvider,
    ) -> None:
        self._register_projects(store)
        core = SessionLifecycleCore(provider, token_factory=lambda: "token-provision")
        core.register_participant(store, active_subject, created_at_utc=NOW,
            registry_revision=TEST_REGISTRY_REVISION,
        )
        store.register_lifecycle_provider(
            workspace_id=active_subject.workspace_id,
            provider_descriptor=provider.descriptor(),
            created_at_utc=NOW,
        )

    def consume(self, store: LedgerStore, active_subject: LifecycleSubject, challenge):
        return self.core.consume(
            store,
            active_subject,
            challenge,
            runtime_home=self.runtime_home,
            consumed_at_utc=BEFORE_EXPIRY,
            correlation_id="corr_consume",
            trusted_project_root=self.trusted_root,
        )

    def managed_request(self) -> ManagedStartRequest:
        return ManagedStartRequest(
            workspace_id=WORKSPACE,
            scope_kind="project",
            scope_identity=PROJECT,
            conversation_id="CHAT-SAMEID",
            participant_id="participant_codex",
            agent_id="agent_codex",
            endpoint_id="endpoint_codex",
            runtime_instance_id="runtime_one",
        )

    def start_candidate(self, native_session_id="native_session_new") -> dict[str, object]:
        cwd = str(self.cwd.resolve())
        return {
            "native_thread_id": native_session_id,
            "endpoint_id": "endpoint_codex",
            "runtime_instance_id": "runtime_one",
            "runtime_home_id": self.runtime_home.runtime_home_id,
            "runtime_home_realpath": self.runtime_home.runtime_home_realpath,
            "project_id": PROJECT,
            "repo_id": "repo_app",
            "canonical_cwd": cwd,
            "provider_revision": "revision_1",
            "creation_provenance": {
                "source": "managed_thread_start",
                "native_thread_id": native_session_id,
                "approval_policy": "never",
                "sandbox": {"type": "readOnly"},
                "model": "gpt-test",
                "cwd": cwd,
            },
            "read_back": {
                "operation": "thread_read",
                "native_thread_id": native_session_id,
                "endpoint_id": "endpoint_codex",
                "runtime_instance_id": "runtime_one",
                "runtime_home_id": self.runtime_home.runtime_home_id,
                "runtime_home_realpath": self.runtime_home.runtime_home_realpath,
                "project_id": PROJECT,
                "repo_id": "repo_app",
                "canonical_cwd": cwd,
                "provider_revision": "revision_1",
            },
        }

    def test_codex_start_evidence_accepts_codex_0146_create_shape_without_correlation(self) -> None:
        candidate = self.start_candidate()
        evidence = validate_codex_start_evidence(
            candidate,
            runtime_home=self.runtime_home,
            trusted_project_root=self.trusted_root,
            expected_endpoint_id="endpoint_codex",
            expected_runtime_instance_id="runtime_one",
            provider_revision="revision_1",
        )
        self.assertIsNone(evidence.creation.server_correlation_id)
        self.assertEqual("never", evidence.creation.approval_policy)
        self.assertEqual({"type": "readOnly"}, evidence.creation.sandbox)

    def test_codex_start_evidence_rejects_attach_shaped_readback_without_create_fields(self) -> None:
        candidate = self.start_candidate()
        candidate["creation_provenance"] = dict(candidate["creation_provenance"])
        del candidate["creation_provenance"]["sandbox"]
        with self.assertRaisesRegex(SessionLifecycleError, "creation provenance is incomplete"):
            validate_codex_start_evidence(
                candidate,
                runtime_home=self.runtime_home,
                trusted_project_root=self.trusted_root,
                expected_endpoint_id="endpoint_codex",
                expected_runtime_instance_id="runtime_one",
                provider_revision="revision_1",
            )

    def test_codex_start_evidence_rejects_trusted_cwd_mismatch_without_ledger_mutation(self) -> None:
        candidate = {
            "native_thread_id": "native_session_new",
            "endpoint_id": "endpoint_codex",
            "runtime_instance_id": "runtime_one",
            "runtime_home_id": self.runtime_home.runtime_home_id,
            "runtime_home_realpath": self.runtime_home.runtime_home_realpath,
            "project_id": PROJECT,
            "repo_id": "repo_app",
            "canonical_cwd": str(self.outside),
            "provider_revision": "revision_1",
            "creation_provenance": {
                "source": "managed_thread_start",
                "native_thread_id": "native_session_new",
                "approval_policy": "never",
                "sandbox": {"type": "readOnly"},
                "model": "gpt-test",
                "cwd": str(self.outside),
            },
            "read_back": {
                "operation": "thread_read",
                "native_thread_id": "native_session_new",
                "endpoint_id": "endpoint_codex",
                "runtime_instance_id": "runtime_one",
                "runtime_home_id": self.runtime_home.runtime_home_id,
                "runtime_home_realpath": self.runtime_home.runtime_home_realpath,
                "project_id": PROJECT,
                "repo_id": "repo_app",
                "canonical_cwd": str(self.outside),
                "provider_revision": "revision_1",
            },
        }
        with LedgerStore.open_writer(self.paths) as store:
            before = row_counts(store)
            with self.assertRaises(SessionLifecycleError):
                validate_codex_start_evidence(
                    candidate,
                    runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root,
                    expected_endpoint_id="endpoint_codex",
                    expected_runtime_instance_id="runtime_one",
                    provider_revision="revision_1",
                )
            self.assertEqual(before, row_counts(store))

    def test_managed_start_binds_valid_evidence_without_placeholder_native_id(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)
            observed_during_start = []

            def fake_start(start_id):
                observed_during_start.append(
                    store._connection.execute(
                        "SELECT native_session_id FROM managed_start_reservations WHERE start_id = ?",
                        (start_id,),
                    ).fetchone()[0]
                )
                return self.start_candidate()

            result = self.core.start_managed(
                store, self.managed_request(), runtime_home=self.runtime_home,
                trusted_project_root=self.trusted_root, created_at_utc=NOW,
                expires_at_utc=AT_EXPIRY, correlation_id="corr_start",
                start_native=fake_start,
            )
            self.assertEqual(observed_during_start, [None])
            self.assertTrue(result["binding"]["resolved"])
            self.assertEqual(
                store._connection.execute(
                    "SELECT state, native_session_id FROM managed_start_reservations"
                ).fetchall(),
                [("bound", "native_session_new")],
            )
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM conversation_bindings").fetchone()[0],
                1,
            )
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM session_binding_challenges WHERE challenge_state = 'consumed'"
                ).fetchone()[0],
                1,
            )

    def test_managed_start_pending_reservation_fences_concurrent_native_starts(self) -> None:
        active_subject = subject()
        starts = []
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)

            def fake_start(_start_id):
                with self.assertRaises(CanonicalConflictError):
                    self.core.start_managed(
                        store, self.managed_request(), runtime_home=self.runtime_home,
                        trusted_project_root=self.trusted_root, created_at_utc=NOW,
                        expires_at_utc=AT_EXPIRY, correlation_id="corr_nested",
                        start_native=lambda _nested: self.start_candidate("native_nested"),
                    )
                starts.append(True)
                return self.start_candidate()

            self.core.start_managed(
                store, self.managed_request(), runtime_home=self.runtime_home,
                trusted_project_root=self.trusted_root, created_at_utc=NOW,
                expires_at_utc=AT_EXPIRY, correlation_id="corr_start",
                start_native=fake_start,
            )
        self.assertEqual(starts, [True])

    def test_managed_start_refuses_an_unverified_existing_binding_before_native_io(self) -> None:
        active_subject = subject(native_session_id="native_existing")
        native_calls = []
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)
            challenge = self.core.reserve(
                store, active_subject, runtime_home=self.runtime_home,
                created_at_utc=NOW, expires_at_utc=AT_EXPIRY,
                correlation_id="corr_existing", trusted_project_root=self.trusted_root,
            )
            binding = self.core.consume(
                store, active_subject, challenge, runtime_home=self.runtime_home,
                consumed_at_utc=NOW, correlation_id="corr_existing",
                trusted_project_root=self.trusted_root,
            )
            store.update_conversation_binding_state(
                workspace_id=active_subject.workspace_id,
                scope_kind=active_subject.scope_kind,
                scope_identity=active_subject.scope_identity,
                conversation_id=active_subject.conversation_id,
                participant_id=active_subject.participant_id,
                binding_id=str(binding["binding_id"]),
                generation=int(binding["generation"]),
                state="unverified",
            )
            with self.assertRaisesRegex(CanonicalConflictError, "unresolved binding"):
                self.core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_duplicate",
                    start_native=lambda _start_id: native_calls.append(True),
                )
        self.assertEqual([], native_calls)

    def test_managed_start_lost_response_is_ambiguous_and_blocks_retry(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)
            with self.assertRaises(ManagedStartResponseLost):
                self.core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_lost",
                    start_native=lambda _start_id: (_ for _ in ()).throw(
                        ManagedStartResponseLost("lost")
                    ),
                )
            self.assertEqual(
                store._connection.execute(
                    "SELECT state, native_session_id FROM managed_start_reservations"
                ).fetchall(),
                [("ambiguous_start", None)],
            )
            with self.assertRaises(CanonicalConflictError):
                self.core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_retry",
                    start_native=lambda _start_id: self.start_candidate("native_retry"),
                )

    def test_managed_start_post_execution_identity_is_durably_orphaned(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)
            with self.assertRaises(ManagedStartOrphaned):
                self.core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_orphaned_transport",
                    start_native=lambda _start_id: (_ for _ in ()).throw(
                        ManagedStartOrphaned(
                            "read-back failed", native_session_id="native_session_orphan"
                        )
                    ),
                )
            row = store._connection.execute(
                "SELECT state, native_session_id, session_ref_id, evidence_sha256 "
                "FROM managed_start_reservations"
            ).fetchone()
        self.assertEqual(row[0:2], ("orphaned", "native_session_orphan"))
        self.assertTrue(row[2].startswith("session_"))
        self.assertRegex(row[3], r"^[0-9a-f]{64}$")

    def test_managed_start_unreattestable_identity_stays_ambiguous(self) -> None:
        def fail_probe(_native_session_id: str):
            raise ValueError("thread disappeared")

        provider = CodexLifecycleProvider(
            exact_thread_probe=fail_probe,
            supported_operations_json='["reserve","attach","start"]',
        )
        core = SessionLifecycleCore(provider)
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, subject(), provider)
            with self.assertRaises(ManagedStartResponseLost) as caught:
                core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_ambiguous_orphan",
                    start_native=lambda _start_id: (_ for _ in ()).throw(
                        ManagedStartOrphaned(
                            "read-back failed", native_session_id="native_session_orphan"
                        )
                    ),
                )
            row = store._connection.execute(
                "SELECT state, native_session_id FROM managed_start_reservations"
            ).fetchone()
        self.assertEqual("native_session_orphan", caught.exception.native_session_id)
        self.assertEqual(("ambiguous_start", None), row)

    def test_returned_candidate_with_failed_reattestation_blocks_retry(self) -> None:
        def fail_probe(_native_session_id: str):
            raise ValueError("thread disappeared")

        provider = CodexLifecycleProvider(
            exact_thread_probe=fail_probe,
            supported_operations_json='["reserve","attach","start"]',
        )
        core = SessionLifecycleCore(provider)
        native_calls = []
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, subject(), provider)

            def start_native(_start_id):
                native_calls.append(True)
                return self.start_candidate()

            with self.assertRaises(ManagedStartResponseLost) as caught:
                core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_post_return",
                    start_native=start_native,
                )
            self.assertEqual("native_session_new", caught.exception.native_session_id)
            self.assertEqual(
                [("ambiguous_start", None)],
                store._connection.execute(
                    "SELECT state, native_session_id FROM managed_start_reservations"
                ).fetchall(),
            )
            with self.assertRaises(CanonicalConflictError):
                core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_post_return_retry",
                    start_native=start_native,
                )
        self.assertEqual([True], native_calls)

    def test_returned_unvalidated_candidate_is_ambiguous(self) -> None:
        malformed = self.start_candidate()
        del malformed["native_thread_id"]
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, subject(), self.core.provider)
            with self.assertRaises(ManagedStartResponseLost) as caught:
                self.core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_unvalidated",
                    start_native=lambda _start_id: malformed,
                )
            self.assertIsNone(caught.exception.native_session_id)
            self.assertEqual(
                [("ambiguous_start", None)],
                store._connection.execute(
                    "SELECT state, native_session_id FROM managed_start_reservations"
                ).fetchall(),
            )

    def test_managed_start_bind_conflict_orphans_exact_validated_native_evidence(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)

            def fake_start(_start_id):
                other_subject = subject(
                    participant_id="participant_other", native_session_id="native_session_new"
                )
                self.provision(store, other_subject, self.core.provider)
                existing_core = SessionLifecycleCore(
                    self.core.provider, token_factory=lambda: "token-existing"
                )
                challenge = existing_core.reserve(
                    store, other_subject,
                    runtime_home=self.runtime_home, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_existing",
                    trusted_project_root=self.trusted_root,
                )
                existing_core.consume(
                    store, other_subject, challenge,
                    runtime_home=self.runtime_home, consumed_at_utc=NOW,
                    correlation_id="corr_existing_consume",
                    trusted_project_root=self.trusted_root,
                )
                return self.start_candidate()

            with self.assertRaises(ManagedStartOrphaned) as caught:
                self.core.start_managed(
                    store, self.managed_request(), runtime_home=self.runtime_home,
                    trusted_project_root=self.trusted_root, created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY, correlation_id="corr_orphan",
                    start_native=fake_start,
                )
            self.assertEqual("native_session_new", caught.exception.native_session_id)
            row = store._connection.execute(
                "SELECT state, native_session_id, evidence_sha256 FROM managed_start_reservations"
            ).fetchone()
            expected = validate_codex_start_evidence(
                self.start_candidate(), runtime_home=self.runtime_home,
                trusted_project_root=self.trusted_root,
                expected_endpoint_id="endpoint_codex",
                expected_runtime_instance_id="runtime_one",
                provider_revision="revision_1",
            )
            self.assertEqual(row[0:2], ("orphaned", "native_session_new"))
            self.assertEqual(row[2], codex_start_evidence_digest(expected))
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM session_binding_challenges").fetchone()[0],
                1,
            )
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM conversation_bindings").fetchone()[0],
                1,
            )

    def test_codex_lifecycle_provider_attests_after_an_exact_thread_match(self) -> None:
        native = "native_session_one"
        received = []

        def probe(thread_id):
            received.append(thread_id)
            return CodexAppServerExactThreadResult(thread_id=native, methods=("initialize", "thread/read"))

        provider = CodexLifecycleProvider(exact_thread_probe=probe)
        ref = provider.attest(
            subject(native_session_id=native),
            runtime_home=self.runtime_home,
            observed_at_utc=NOW,
            correlation_id="corr-1",
            trusted_project_root=self.trusted_root,
        )
        # The probe is called exactly once, with the subject's native session id.
        self.assertEqual([native], received)
        self.assertEqual(native, ref["native_session_id"])
        self.assertIsInstance(ref["session_ref_id"], str)

    def test_codex_lifecycle_provider_fails_closed_on_a_thread_id_mismatch(self) -> None:
        provider = CodexLifecycleProvider(
            exact_thread_probe=lambda _tid: CodexAppServerExactThreadResult(
                thread_id="a-different-thread", methods=("initialize", "thread/read")
            )
        )
        with self.assertRaises(SessionLifecycleError):
            provider.attest(
                subject(native_session_id="native_session_one"),
                runtime_home=self.runtime_home,
                observed_at_utc=NOW,
                correlation_id="corr-1",
                trusted_project_root=self.trusted_root,
            )

    def test_codex_lifecycle_provider_propagates_a_probe_failure(self) -> None:
        from llm_collab.codex_app_server_live_probe import CodexAppServerLiveProbeError

        def failing_probe(_thread_id):
            raise CodexAppServerLiveProbeError("thread/read failed")

        provider = CodexLifecycleProvider(exact_thread_probe=failing_probe)
        # A probe failure fails closed and retains its real type/cause (not normalized).
        with self.assertRaises(CodexAppServerLiveProbeError):
            provider.attest(
                subject(native_session_id="native_session_one"),
                runtime_home=self.runtime_home,
                observed_at_utc=NOW,
                correlation_id="corr-1",
                trusted_project_root=self.trusted_root,
            )

    def test_codex_lifecycle_provider_is_identity_only_with_no_start_or_open_ui(self) -> None:
        provider = CodexLifecycleProvider(
            exact_thread_probe=lambda _tid: CodexAppServerExactThreadResult(
                thread_id="x", methods=("initialize", "thread/read")
            )
        )
        # Identity-only: advertises only reserve/attach, and open_ui fails closed.
        self.assertEqual('["reserve","attach"]', provider.supported_operations_json)
        with self.assertRaises(SessionLifecycleError):
            provider.open_ui(subject())

    def test_codex_provider_probe_failure_leaves_the_ledger_unchanged(self) -> None:
        # reserve() calls provider.attest FIRST; a probe mismatch must raise before
        # any ledger mutation. Snapshot lifecycle row counts; they must be unchanged.
        failing_provider = CodexLifecycleProvider(
            exact_thread_probe=lambda _tid: CodexAppServerExactThreadResult(
                thread_id="not-the-subject-native-id", methods=("initialize", "thread/read")
            )
        )
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, failing_provider)
            core = SessionLifecycleCore(failing_provider, token_factory=lambda: "token-zeta")
            before = row_counts(store)
            with self.assertRaises(SessionLifecycleError):
                core.reserve(
                    store,
                    active_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_reserve",
                    trusted_project_root=self.trusted_root,
                )
            self.assertEqual(before, row_counts(store))

    def test_codex_provider_reserve_creates_a_challenge_when_the_exact_thread_matches(self) -> None:
        # reserve() consumes the CodexLifecycleProvider attester end-to-end: a
        # matching probe mints exactly one challenge row (the success complement
        # to the mismatch fail-closed / ledger-unchanged test in #415).
        native = "native_session_one"
        provider = CodexLifecycleProvider(
            exact_thread_probe=lambda _tid: CodexAppServerExactThreadResult(
                thread_id=native, methods=("initialize", "thread/read")
            )
        )
        active_subject = subject(native_session_id=native)
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, provider)
            core = SessionLifecycleCore(provider, token_factory=lambda: "token-reserve")
            before = row_counts(store)
            challenge = core.reserve(
                store,
                active_subject,
                runtime_home=self.runtime_home,
                created_at_utc=NOW,
                expires_at_utc=AT_EXPIRY,
                correlation_id="corr_reserve",
                trusted_project_root=self.trusted_root,
            )
            after = row_counts(store)
        self.assertTrue(challenge.challenge_id)
        diffs = {t: after[t] - before[t] for t in LIFECYCLE_ROW_COUNT_TABLES if after[t] != before[t]}
        self.assertEqual({"session_binding_challenges": 1}, diffs)

    def test_register_participant_enables_reserve_and_is_idempotent(self) -> None:
        # register_participant is the production provision seam: before it, reserve
        # fails closed (participant not provisioned); after it, reserve succeeds;
        # re-registering is idempotent (no error, no duplicate rows).
        native = "native_session_one"
        provider = CodexLifecycleProvider(
            exact_thread_probe=lambda _tid: CodexAppServerExactThreadResult(
                thread_id=native, methods=("initialize", "thread/read")
            )
        )
        core = SessionLifecycleCore(provider, token_factory=lambda: "token-provision")
        active_subject = subject(native_session_id=native)
        with LedgerStore.open_writer(self.paths) as store:
            with self.assertRaises(SessionLifecycleError):
                core.reserve(
                    store, active_subject, runtime_home=self.runtime_home,
                    created_at_utc=NOW, expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_reserve", trusted_project_root=self.trusted_root,
                )
            core.register_participant(store, active_subject, created_at_utc=NOW,
            registry_revision=TEST_REGISTRY_REVISION)
            store.register_lifecycle_provider(
                workspace_id=active_subject.workspace_id,
                provider_descriptor=provider.descriptor(),
                created_at_utc=NOW,
            )
            before = row_counts(store)
            challenge = core.reserve(
                store, active_subject, runtime_home=self.runtime_home,
                created_at_utc=NOW, expires_at_utc=AT_EXPIRY,
                correlation_id="corr_reserve", trusted_project_root=self.trusted_root,
            )
            after = row_counts(store)
            self.assertTrue(challenge.challenge_id)
            self.assertEqual(
                {"session_binding_challenges": 1},
                {t: after[t] - before[t] for t in LIFECYCLE_ROW_COUNT_TABLES if after[t] != before[t]},
            )
            participants = after["conversation_participants"]
            providers = after["lifecycle_provider_registry"]
            core.register_participant(store, active_subject, created_at_utc=NOW,
            registry_revision=TEST_REGISTRY_REVISION)
            final = row_counts(store)
            self.assertEqual(participants, final["conversation_participants"])
            self.assertEqual(providers, final["lifecycle_provider_registry"])

    def test_register_participant_does_not_self_mint_the_trusted_provider_registry(self) -> None:
        # P1-1: register_participant must NOT write the provider registry.
        with LedgerStore.open_writer(self.paths) as store:
            before = row_counts(store)["lifecycle_provider_registry"]
            self.core.register_participant(store, subject(), created_at_utc=NOW,
            registry_revision=TEST_REGISTRY_REVISION)
            after = row_counts(store)["lifecycle_provider_registry"]
        self.assertEqual(before, after)

    def test_register_participant_rejects_a_conflicting_agent(self) -> None:
        # P1-2: same participant key + different agent -> CanonicalConflictError.
        active = subject()
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active, FakeLifecycleProvider())
            with self.assertRaises(CanonicalConflictError):
                self.core.register_participant(
                    store, subject(agent_id="agent_other"), created_at_utc=NOW,
                    registry_revision=TEST_REGISTRY_REVISION
                )

    def test_register_lifecycle_provider_rejects_a_conflicting_descriptor(self) -> None:
        # P1-2: same provider id + different descriptor -> CanonicalConflictError.
        with LedgerStore.open_writer(self.paths) as store:
            store.register_lifecycle_provider(
                workspace_id=WORKSPACE,
                provider_descriptor=FakeLifecycleProvider().descriptor(),
                created_at_utc=NOW,
            )
            altered = FakeLifecycleProvider(trust_class="native_attached")
            with self.assertRaises(CanonicalConflictError):
                store.register_lifecycle_provider(
                    workspace_id=WORKSPACE,
                    provider_descriptor=altered.descriptor(),
                    created_at_utc=NOW,
                )

    def test_register_lifecycle_provider_allows_a_new_revision_but_rejects_a_conflicting_descriptor(self) -> None:
        # The registry is keyed by (provider_id, provider_revision): a new
        # revision is a legitimate insert (upgrade); same key + different
        # descriptor is a conflict.
        with LedgerStore.open_writer(self.paths) as store:
            store.register_lifecycle_provider(
                workspace_id=WORKSPACE,
                provider_descriptor=FakeLifecycleProvider().descriptor(),
                created_at_utc=NOW,
            )
            upgraded = FakeLifecycleProvider(provider_revision="revision_2")
            store.register_lifecycle_provider(
                workspace_id=WORKSPACE,
                provider_descriptor=upgraded.descriptor(),
                created_at_utc=NOW,
            )
            rows = store._connection.execute(
                "SELECT provider_revision FROM lifecycle_provider_registry"
                " WHERE workspace_id = ? AND provider_id = ? ORDER BY provider_revision",
                (WORKSPACE, FakeLifecycleProvider().provider_id),
            ).fetchall()
            self.assertEqual([("revision_1",), ("revision_2",)], rows)
            altered = FakeLifecycleProvider(provider_revision="revision_1", trust_class="native_attached")
            with self.assertRaises(CanonicalConflictError):
                store.register_lifecycle_provider(
                    workspace_id=WORKSPACE,
                    provider_descriptor=altered.descriptor(),
                    created_at_utc=NOW,
                )

    def test_register_lifecycle_provider_accepts_a_start_only_descriptor(self) -> None:
        provider = FakeLifecycleProvider(
            provider_revision="revision_start_only",
            supported_operations_json='["start"]',
        )
        with LedgerStore.open_writer(self.paths) as store:
            store.register_lifecycle_provider(
                workspace_id=WORKSPACE,
                provider_descriptor=provider.descriptor(),
                created_at_utc=NOW,
            )
            self.assertTrue(
                store.has_lifecycle_provider(
                    workspace_id=WORKSPACE,
                    provider_id=provider.provider_id,
                    provider_revision=provider.provider_revision,
                )
            )

    def test_register_conversation_participant_rejects_an_unregistered_project(self) -> None:
        # A well-formed but unregistered project_id must not create a participant row.
        with LedgerStore.open_writer(self.paths) as store:
            with self.assertRaises(ValueError):
                store.register_conversation_participant(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity="unregistered_project",
                    conversation_id="CHAT-X",
                    participant_id="p1",
                    agent_id="agent_codex",
                    created_at_utc=NOW,
                    registry_revision=TEST_REGISTRY_REVISION,
                )

    def test_register_conversation_participant_rejects_a_project_dropped_from_the_supplied_revision(self) -> None:
        # A project present in an OLDER revision but dropped from the SUPPLIED
        # revision must be rejected — immutable history is not current authority.
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(Path(tmp) / "state", WORKSPACE)
            _s1 = hashlib.sha256(b"rev-1").hexdigest()
            _s2 = hashlib.sha256(b"rev-2").hexdigest()
            with LedgerStore.open_writer(paths) as store:
                store.record_registry_snapshot(
                    workspace_id=WORKSPACE,
                    registry_revision=f"sha256:{_s1}",
                    registry_source_sha256=_s1,
                    captured_at_utc=NOW,
                    workspace_snapshot_json=json.dumps({"workspace_id": WORKSPACE, "projects": [PROJECT, OTHER_PROJECT]}),
                    project_snapshots={PROJECT: json.dumps({"id": PROJECT}), OTHER_PROJECT: json.dumps({"id": OTHER_PROJECT})},
                    source_snapshots={},
                )
                store.record_registry_snapshot(
                    workspace_id=WORKSPACE,
                    registry_revision=f"sha256:{_s2}",
                    registry_source_sha256=_s2,
                    captured_at_utc=NOW,
                    workspace_snapshot_json=json.dumps({"workspace_id": WORKSPACE, "projects": [PROJECT]}),
                    project_snapshots={PROJECT: json.dumps({"id": PROJECT})},
                    source_snapshots={},
                )
                # PROJECT is in revision_2 -> succeeds.
                store.register_conversation_participant(
                    workspace_id=WORKSPACE, scope_kind="project", scope_identity=PROJECT,
                    conversation_id="CHAT-X", participant_id="participant_test_a", agent_id="agent_codex",
                    created_at_utc=NOW, registry_revision=f"sha256:{_s2}",
                )
                # OTHER_PROJECT is in revision_1 but NOT revision_2 -> fails.
                with self.assertRaises(ValueError):
                    store.register_conversation_participant(
                        workspace_id=WORKSPACE, scope_kind="project", scope_identity=OTHER_PROJECT,
                        conversation_id="CHAT-Y", participant_id="participant_test_b", agent_id="agent_codex",
                        created_at_utc=NOW, registry_revision=f"sha256:{_s2}",
                    )

    def test_register_conversation_participant_rejects_an_old_supplied_revision(self) -> None:
        # P1: a caller supplying an OLD registry revision (not current) must be
        # rejected — historical snapshots are not current authority.
        with TemporaryDirectory(dir="/tmp") as tmp:
            paths = LedgerPaths.derive(Path(tmp) / "state", WORKSPACE)
            _s1 = hashlib.sha256(b"old-rev").hexdigest()
            _s2 = hashlib.sha256(b"new-rev").hexdigest()
            with LedgerStore.open_writer(paths) as store:
                store.record_registry_snapshot(
                    workspace_id=WORKSPACE, registry_revision=f"sha256:{_s1}",
                    registry_source_sha256=_s1, captured_at_utc="2026-01-01T00:00:00+00:00",
                    workspace_snapshot_json=json.dumps({"workspace_id": WORKSPACE, "projects": [PROJECT, OTHER_PROJECT]}),
                    project_snapshots={PROJECT: json.dumps({"id": PROJECT}), OTHER_PROJECT: json.dumps({"id": OTHER_PROJECT})},
                    source_snapshots={},
                )
                store.record_registry_snapshot(
                    workspace_id=WORKSPACE, registry_revision=f"sha256:{_s2}",
                    registry_source_sha256=_s2, captured_at_utc="2026-07-01T00:00:00+00:00",
                    workspace_snapshot_json=json.dumps({"workspace_id": WORKSPACE, "projects": [PROJECT]}),
                    project_snapshots={PROJECT: json.dumps({"id": PROJECT})}, source_snapshots={},
                )
                # Supply the OLD revision + OTHER_PROJECT → must fail (not current).
                with self.assertRaises(ValueError):
                    store.register_conversation_participant(
                        workspace_id=WORKSPACE, scope_kind="project", scope_identity=OTHER_PROJECT,
                        conversation_id="CHAT-X", participant_id="participant_test_c",
                        agent_id="agent_kimi", created_at_utc=NOW, registry_revision=f"sha256:{_s1}",
                    )
                # Supply the CURRENT revision + PROJECT → succeeds.
                store.register_conversation_participant(
                    workspace_id=WORKSPACE, scope_kind="project", scope_identity=PROJECT,
                    conversation_id="CHAT-Y", participant_id="participant_test_d",
                    agent_id="agent_kimi", created_at_utc=NOW, registry_revision=f"sha256:{_s2}",
                )

    def test_worker_start_consume_failure_leaves_zero_binding_rows(self) -> None:
        # ponytail ceiling: a consume failure (attestation raises at consume time)
        # leaves a self-expiring dangling challenge but ZERO binding rows — no
        # partial binding is ever written.
        from llm_collab.codex_app_server_live_probe import CodexAppServerLiveProbeError
        native = "native_session_one"
        call_count = [0]

        def probe(tid):
            call_count[0] += 1
            if call_count[0] > 1:
                raise CodexAppServerLiveProbeError("thread drifted at consume")
            return CodexAppServerExactThreadResult(
                thread_id=native, methods=("initialize", "thread/read"))

        provider = CodexLifecycleProvider(exact_thread_probe=probe)
        core = SessionLifecycleCore(provider)
        active_subject = subject(native_session_id=native)
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, provider)
            challenge = core.reserve(
                store, active_subject, runtime_home=self.runtime_home,
                created_at_utc=NOW, expires_at_utc=AT_EXPIRY,
                correlation_id="corr_start", trusted_project_root=self.trusted_root,
            )
            before = row_counts(store)
            with self.assertRaises(CodexAppServerLiveProbeError):
                core.consume(
                    store, active_subject, challenge,
                    runtime_home=self.runtime_home, consumed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_start", trusted_project_root=self.trusted_root,
                )
            after = row_counts(store)
            self.assertEqual(
                before["conversation_bindings"], after["conversation_bindings"])

    def test_worker_attach_command_refuses_without_preapproved_provider(self) -> None:
        # P1-4: the command body is actually exercised — calling
        # bin.worker.main(["attach", ...]) directly with a registered project +
        # participant but NO lifecycle_provider row fails closed (reserve cannot
        # find the trusted provider) and leaves provider/challenge/binding counts
        # unchanged.
        import bin.worker as worker_module
        with LedgerStore.open_writer(self.paths) as store:
            self._register_projects(store)
            self.core.register_participant(
                store, subject(), created_at_utc=NOW,
                registry_revision=TEST_REGISTRY_REVISION)
            before = row_counts(store)
        from llm_collab.codex_app_server_live_probe import CodexAppServerExactThreadResult
        _fake_probe = lambda tid: CodexAppServerExactThreadResult(
            thread_id=tid, methods=("initialize", "thread/read"))
        with patch.object(worker_module, "ensure_project"), \
             patch.object(worker_module, "config_get", return_value=WORKSPACE), \
             patch.object(worker_module, "project_state_root",
                          return_value=Path(self.tmp.name) / "state"), \
             patch.object(worker_module, "resolve_project_repo_path",
                          return_value=self.repo), \
             patch.object(worker_module, "probe_exact_thread", side_effect=_fake_probe):
            with self.assertRaisesRegex(SessionLifecycleError, "pre-approved"):
                worker_module.main([
                    "attach", "--project", PROJECT, "--chat", "CHAT-SAMEID",
                    "--participant", "participant_kimi", "--agent", "agent_kimi",
                    "--endpoint-id", "endpoint_codex", "--native-session", "native_session_one",
                    "--runtime-instance", "runtime_one", "--codex-home", str(self.codex_home),
                    "--endpoint", "ws://127.0.0.1:1",
                ])
        with LedgerStore.open_writer(self.paths) as store:
            after = row_counts(store)
        # Provider/challenge/binding counts must be unchanged (the participant may
        # have been re-registered — that's idempotent bridge bookkeeping, not
        # trusted-registry authority).
        for table in ("lifecycle_provider_registry",
                      "session_binding_challenges",
                      "conversation_bindings"):
            self.assertEqual(before[table], after[table])

    def test_reserve_consume_resolves_and_replay_fails(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            self.assertEqual(challenge.challenge_token, "token-alpha")
            resolved = self.consume(store, active_subject, challenge)
            self.assertTrue(resolved["resolved"])
            self.assertEqual(resolved["generation"], 1)
            self.assertEqual(resolved["provider_id"], "provider_codex")
            self.assertEqual(resolved["endpoint_id"], "endpoint_codex")
            self.assertEqual(
                resolved["binding_id"],
                expected_binding_id(
                    session_ref_id=str(resolved["session_ref_id"]),
                    generation=1,
                ),
            )
            with self.assertRaisesRegex(CanonicalConflictError, "not pending"):
                self.consume(store, active_subject, challenge)
            self.assertEqual(
                store._connection.execute(
                    "SELECT challenge_state FROM session_binding_challenges"
                ).fetchall(),
                [("consumed",)],
            )
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM conversation_bindings"
                ).fetchone()[0],
                1,
            )

    def test_consume_requires_the_preprovisioned_participant_agent(self) -> None:
        active_subject = subject()
        other_agent = subject(agent_id="agent_claude")
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            before = row_counts(store)
            with self.assertRaisesRegex(CanonicalConflictError, "not pending or does not match"):
                self.consume(store, other_agent, challenge)
            self.assertEqual(row_counts(store), before)
            self.assertEqual(
                store._connection.execute(
                    "SELECT challenge_state FROM session_binding_challenges"
                ).fetchone()[0],
                "pending",
            )

    def test_reserve_requires_preprovisioned_provider_and_participant(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            before = row_counts(store)
            with self.assertRaisesRegex(SessionLifecycleError, "pre-approved"):
                self.core.reserve(
                    store,
                    active_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_missing_identity",
                    trusted_project_root=self.trusted_root,
                )
            self.assertEqual(row_counts(store), before)

            self.provision(store, active_subject, self.core.provider)
            altered = FakeLifecycleProvider(trust_class="native_attached")
            altered_core = SessionLifecycleCore(altered, token_factory=lambda: "token-altered")
            before = row_counts(store)
            with self.assertRaisesRegex(CanonicalConflictError, "not allowlisted"):
                altered_core.reserve(
                    store,
                    active_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_altered_descriptor",
                    trusted_project_root=self.trusted_root,
                )
            self.assertEqual(row_counts(store), before)

    def test_workspace_scope_cannot_register_attached_session(self) -> None:
        workspace_subject = subject(scope_kind="workspace", scope_identity="workspace")
        with LedgerStore.open_writer(self.paths) as store:
            before = row_counts(store)
            with self.assertRaisesRegex(SessionLifecycleError, "requires project scope"):
                self.core.reserve(
                    store,
                    workspace_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_workspace_scope",
                )
            self.assertEqual(row_counts(store), before)

            descriptor = self.core.provider.descriptor()
            with self.assertRaisesRegex(
                CanonicalConflictError, "requires project scope"
            ):
                store.reserve_session_binding_challenge(
                    workspace_id=WORKSPACE,
                    scope_kind="workspace",
                    scope_identity="workspace",
                    conversation_id="CHAT-SAMEID",
                    participant_id="participant_codex",
                    agent_id="agent_codex",
                    provider_descriptor=descriptor,
                    endpoint_id="endpoint_codex",
                    session_ref_id="session_ref_workspace",
                    native_session_id="native_session_one",
                    runtime_instance_id="runtime_one",
                    challenge_id="challenge_workspace",
                    challenge_token_sha256="a" * 64,
                    expires_at_utc=AT_EXPIRY,
                    created_at_utc=NOW,
                )
            with self.assertRaisesRegex(
                CanonicalConflictError, "requires project scope"
            ):
                store.consume_session_binding_challenge(
                    workspace_id=WORKSPACE,
                    scope_kind="workspace",
                    scope_identity="workspace",
                    conversation_id="CHAT-SAMEID",
                    participant_id="participant_codex",
                    agent_id="agent_codex",
                    challenge_id="challenge_workspace",
                    challenge_token_sha256="a" * 64,
                    provider_id="provider_codex",
                    provider_revision="revision_1",
                    endpoint_id="endpoint_codex",
                    session_ref_id="session_ref_workspace",
                    session_owner_key="owner_" + "a" * 32,
                    native_session_id="native_session_one",
                    runtime_instance_id="runtime_one",
                    consumed_at_utc=BEFORE_EXPIRY,
                )
            self.assertEqual(row_counts(store), before)

    def test_consume_uses_reserved_agent_when_participant_row_changes(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            store._connection.execute(
                """
                UPDATE conversation_participants SET agent_id = 'agent_claude'
                WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ?
                  AND conversation_id = ? AND participant_id = ?
                """,
                (
                    active_subject.workspace_id,
                    active_subject.scope_kind,
                    active_subject.scope_identity,
                    active_subject.conversation_id,
                    active_subject.participant_id,
                ),
            )
            resolved = self.consume(store, active_subject, challenge)
            self.assertTrue(resolved["resolved"])
            self.assertEqual(
                store._connection.execute(
                    "SELECT agent_id FROM session_binding_challenges WHERE challenge_id = ?",
                    (challenge.challenge_id,),
                ).fetchone()[0],
                "agent_codex",
            )

    def test_consume_rejects_wrong_agent_with_valid_reserved_token(self) -> None:
        active_subject = subject()
        wrong_agent = subject(agent_id="agent_claude")
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            before = row_counts(store)
            with self.assertRaisesRegex(
                CanonicalConflictError, "not pending or does not match"
            ):
                self.consume(store, wrong_agent, challenge)
            self.assertEqual(row_counts(store), before)
            self.assertEqual(
                store._connection.execute(
                    "SELECT challenge_state FROM session_binding_challenges"
                ).fetchone()[0],
                "pending",
            )
            self.assertEqual(
                store._connection.execute(
                    "SELECT count(*) FROM conversation_bindings"
                ).fetchone()[0],
                0,
            )

    def test_subset_provider_operations_support_reserve_and_consume(self) -> None:
        active_subject = subject()
        provider = FakeLifecycleProvider(supported_operations_json='["reserve","attach"]')
        core = SessionLifecycleCore(provider, token_factory=lambda: "token-subset")
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, provider)
            challenge = core.reserve(
                store,
                active_subject,
                runtime_home=self.runtime_home,
                created_at_utc=NOW,
                expires_at_utc=AT_EXPIRY,
                correlation_id="corr_subset_reserve",
                trusted_project_root=self.trusted_root,
            )
            resolved = core.consume(
                store,
                active_subject,
                challenge,
                runtime_home=self.runtime_home,
                consumed_at_utc=BEFORE_EXPIRY,
                correlation_id="corr_subset_consume",
                trusted_project_root=self.trusted_root,
            )
            self.assertTrue(resolved["resolved"])

    def test_same_native_session_cannot_become_two_project_owners(self) -> None:
        active_subject = subject()
        other_repo = self.outside / "other-repo"
        other_repo.mkdir()
        other_cwd = other_repo / "work"
        other_cwd.mkdir()
        other_root = TrustedProjectRoot(OTHER_PROJECT, "repo_other", str(other_repo), str(other_cwd))
        other_subject = subject(scope_identity=OTHER_PROJECT)
        with LedgerStore.open_writer(self.paths) as store:
            first = self.reserve(store, active_subject)
            first_binding = self.consume(store, active_subject, first)
            self.provision(store, other_subject, self.core.provider)
            second = self.core.reserve(
                store,
                other_subject,
                runtime_home=self.runtime_home,
                created_at_utc=NOW,
                expires_at_utc=AT_EXPIRY,
                correlation_id="corr_other_project",
                trusted_project_root=other_root,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.core.consume(
                    store,
                    other_subject,
                    second,
                    runtime_home=self.runtime_home,
                    consumed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_other_project_consume",
                    trusted_project_root=other_root,
                )
            self.assertEqual(
                store._connection.execute(
                    "SELECT challenge_state FROM session_binding_challenges WHERE challenge_id = ?",
                    (second.challenge_id,),
                ).fetchone()[0],
                "pending",
            )
            self.assertEqual(
                store._connection.execute("SELECT count(*) FROM conversation_bindings").fetchone()[0],
                1,
            )
            second_owner = store._connection.execute(
                "SELECT agent_id FROM session_binding_challenges WHERE challenge_id = ?",
                (second.challenge_id,),
            ).fetchone()[0]
            first_owner = store._connection.execute(
                "SELECT owner_key FROM conversation_bindings WHERE binding_id = ?",
                (first_binding["binding_id"],),
            ).fetchone()[0]
            self.assertEqual(second_owner, active_subject.agent_id)
            self.assertRegex(first_owner, r"^owner_[0-9a-f]{32}$")

    def test_token_hash_is_stored_not_token_and_default_uses_secrets(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            with patch("secrets.token_urlsafe", return_value="secret-token") as token_urlsafe:
                core = SessionLifecycleCore(FakeLifecycleProvider())
                self.provision(store, active_subject, core.provider)
                challenge = core.reserve(
                    store,
                    active_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_reserve",
                    trusted_project_root=self.trusted_root,
                )
            token_urlsafe.assert_called_once_with(32)
            self.assertEqual(challenge.challenge_token, "secret-token")
            stored = store._connection.execute(
                "SELECT challenge_token_sha256 FROM session_binding_challenges"
            ).fetchone()[0]
            self.assertNotEqual(stored, "secret-token")
            self.assertRegex(stored, r"^[0-9a-f]{64}$")

    def test_expiry_boundary_and_tuple_mismatch_preserve_pending(self) -> None:
        for label, consume_subject, consume_time in (
            ("expired", subject(), AT_EXPIRY),
            ("wrong_project", subject(scope_identity=OTHER_PROJECT), BEFORE_EXPIRY),
            ("wrong_conversation", subject(conversation_id="CHAT-OTHER"), BEFORE_EXPIRY),
            ("wrong_participant", subject(participant_id="participant_claude"), BEFORE_EXPIRY),
            ("wrong_endpoint", subject(endpoint_id="endpoint_other"), BEFORE_EXPIRY),
            ("wrong_native", subject(native_session_id="native_session_two"), BEFORE_EXPIRY),
            ("wrong_runtime", subject(runtime_instance_id="runtime_two"), BEFORE_EXPIRY),
        ):
            with self.subTest(label=label), TemporaryDirectory(dir="/tmp") as tmp:
                paths = LedgerPaths.derive(Path(tmp) / "state", WORKSPACE)
                active_subject = subject()
                with LedgerStore.open_writer(paths) as store:
                    challenge = self.reserve(store, active_subject)
                    with self.assertRaises((CanonicalConflictError, SessionLifecycleError)):
                        self.core.consume(
                            store,
                            consume_subject,
                            challenge,
                            runtime_home=self.runtime_home,
                            consumed_at_utc=consume_time,
                            correlation_id="corr_consume",
                            trusted_project_root=self.trusted_root,
                        )
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT challenge_state FROM session_binding_challenges"
                        ).fetchone()[0],
                        "pending",
                    )
                    self.assertEqual(
                        store._connection.execute(
                            "SELECT count(*) FROM conversation_bindings"
                        ).fetchone()[0],
                        0,
                    )

    def test_partial_bind_failure_rolls_back_challenge_consume(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            first = self.consume(store, active_subject, challenge)
            other_core = SessionLifecycleCore(
                FakeLifecycleProvider(), token_factory=lambda: "token-beta"
            )
            second = other_core.reserve(
                store,
                active_subject,
                runtime_home=self.runtime_home,
                created_at_utc=NOW,
                expires_at_utc=AT_EXPIRY,
                correlation_id="corr_second",
                trusted_project_root=self.trusted_root,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self.consume(store, active_subject, second)
            self.assertEqual(
                store._connection.execute(
                    "SELECT challenge_state FROM session_binding_challenges WHERE challenge_id = ?",
                    (second.challenge_id,),
                ).fetchone()[0],
                "pending",
            )
            self.assertEqual(
                store.resolve_conversation_binding(
                    workspace_id=WORKSPACE,
                    scope_kind="project",
                    scope_identity=PROJECT,
                    conversation_id="CHAT-SAMEID",
                    participant_id="participant_codex",
                )["binding_id"],
                first["binding_id"],
            )

    def test_trusted_root_validates_on_reserve_consume_heartbeat_and_restart(self) -> None:
        active_subject = subject()
        wrong_root = TrustedProjectRoot(OTHER_PROJECT, "repo_app", str(self.repo), str(self.cwd))
        outside_root = TrustedProjectRoot(PROJECT, "repo_app", str(self.repo), str(self.outside))
        with LedgerStore.open_writer(self.paths) as store:
            self.provision(store, active_subject, self.core.provider)
            with self.assertRaisesRegex(SessionLifecycleError, "trusted project root"):
                self.core.reserve(
                    store,
                    active_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_bad_project",
                    trusted_project_root=wrong_root,
                )
            with self.assertRaises(Exception):
                self.core.reserve(
                    store,
                    active_subject,
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=AT_EXPIRY,
                    correlation_id="corr_bad_cwd",
                    trusted_project_root=outside_root,
                )
            challenge = self.reserve(store, active_subject)
            with self.assertRaises(Exception):
                self.core.consume(
                    store,
                    active_subject,
                    challenge,
                    runtime_home=self.runtime_home,
                    consumed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_bad_consume",
                    trusted_project_root=outside_root,
                )
            self.assertEqual(
                store._connection.execute(
                    "SELECT challenge_state FROM session_binding_challenges"
                ).fetchone()[0],
                "pending",
            )
            binding = self.consume(store, active_subject, challenge)
            with self.assertRaises(Exception):
                self.core.heartbeat(
                    store,
                    active_subject,
                    binding,
                    runtime_home=self.runtime_home,
                    observed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_bad_heartbeat",
                    trusted_project_root=outside_root,
                )
            self.assertTrue(self.core.inspect(store, active_subject)["resolved"])
            with self.assertRaises(Exception):
                self.core.mark_restart_unverified(
                    store,
                    active_subject,
                    binding,
                    runtime_home=self.runtime_home,
                    observed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_bad_restart",
                    trusted_project_root=outside_root,
                )
            self.assertTrue(self.core.inspect(store, active_subject)["resolved"])
            self.assertEqual(
                self.core.mark_restart_unverified(
                    store,
                    active_subject,
                    binding,
                    runtime_home=self.runtime_home,
                    observed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_restart",
                    trusted_project_root=self.trusted_root,
                )["reason"],
                "session_unverified",
            )

    def test_operator_inspection_is_query_only_closed_and_non_authoritative(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            binding = self.consume(store, active_subject, challenge)

        with LedgerStore.open_reader(self.paths) as reader:
            self.assertEqual(reader._connection.execute("PRAGMA query_only").fetchone()[0], 1)
            before = row_counts(reader)
            statements: list[str] = []
            reader._connection.set_trace_callback(statements.append)
            try:
                projected = self.core.inspect_for_operator(reader, active_subject)
                stale = self.core.inspect_for_operator(
                    reader,
                    active_subject,
                    expected_binding_id=str(binding["binding_id"]),
                    expected_generation=2,
                )
            finally:
                reader._connection.set_trace_callback(None)
            after = row_counts(reader)

        self.assertEqual(before, after)
        write_statements = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(WRITE_SQL_PREFIXES)
        ]
        self.assertEqual(write_statements, [])
        self.assertEqual(set(projected), OPERATOR_INSPECTION_KEYS)
        self.assertEqual(projected["projection_kind"], "session_lifecycle_operator_inspection_v1")
        self.assertEqual(projected["authority"], "read_only_inspection")
        self.assertIs(projected["resolved"], True)
        self.assertEqual(projected["binding_id"], binding["binding_id"])
        self.assertEqual(projected["generation"], 1)
        self.assertEqual(projected["state"], "active")
        self.assertEqual(projected["session_ref_id"], binding["session_ref_id"])
        self.assertIsInstance(projected["authority"], str)
        self.assertFalse(
            {
                "evidence",
                "extensions",
                "repository_binding",
                "runtime_home",
                "scope",
                "schema_version",
            }
            & set(projected)
        )
        self.assertEqual(set(stale), OPERATOR_INSPECTION_KEYS)
        self.assertIs(stale["resolved"], False)
        self.assertEqual(stale["reason"], "stale_generation")
        self.assertIsNone(stale["binding_id"])

    def test_operator_inspection_requires_full_subject_and_query_only_reader(self) -> None:
        self.assertEqual(
            [
                name
                for name in inspect.signature(
                    SessionLifecycleCore.inspect_for_operator
                ).parameters
                if name not in {
                    "self",
                    "store",
                    "subject",
                    "expected_binding_id",
                    "expected_generation",
                }
            ],
            [],
        )
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            self.consume(store, active_subject, challenge)
            with self.assertRaisesRegex(SessionLifecycleError, "query-only reader"):
                self.core.inspect_for_operator(store, active_subject)

        with LedgerStore.open_reader(self.paths) as reader:
            missing_participant = self.core.inspect_for_operator(
                reader,
                subject(participant_id="participant_missing"),
            )
        self.assertEqual(missing_participant["reason"], "waiting_for_session")

    def test_retire_open_ui_and_consumer_boundaries(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            binding = self.consume(store, active_subject, challenge)
            self.assertTrue(
                self.core.heartbeat(
                    store,
                    active_subject,
                    binding,
                    runtime_home=self.runtime_home,
                    observed_at_utc=BEFORE_EXPIRY,
                    correlation_id="corr_heartbeat",
                    trusted_project_root=self.trusted_root,
                )["resolved"]
            )
            writes: list[str] = []
            store._connection.set_trace_callback(writes.append)
            self.assertTrue(self.core.provider.open_ui(active_subject)["presentation_only"])
            store._connection.set_trace_callback(None)
            self.assertEqual(writes, [])
            self.assertEqual(
                self.core.retire(store, active_subject, binding)["reason"],
                "pull_pending",
            )

    def test_rebind_records_derived_zero_transfer_transition(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            challenge = self.reserve(store, active_subject)
            predecessor = self.consume(store, active_subject, challenge)
            store._connection.execute(
                """
                INSERT INTO conversation_bindings
                (
                    workspace_id, scope_kind, scope_identity, conversation_id, participant_id,
                    binding_id, generation, state, mutation_capable, provider_id,
                    provider_revision, endpoint_id, session_ref_id, native_session_id,
                    runtime_instance_id, registered_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    WORKSPACE,
                    "project",
                    PROJECT,
                    "CHAT-SAMEID",
                    "participant_codex",
                    "binding_successor",
                    2,
                    "reserved",
                    1,
                    "provider_codex",
                    "revision_1",
                    "endpoint_codex",
                    "session_ref_two",
                    "native_session_two",
                    "runtime_two",
                    NOW,
                ),
            )
            successor = {"binding_id": "binding_successor", "generation": 2}
            result = self.core.rebind(
                store,
                active_subject,
                predecessor,
                successor,
                transition_kind="rebind",
                actor_id="operator",
                reason="operator_rebind",
                evidence=b"operator approval",
                created_at_utc=NOW,
            )

            self.assertNotIn(
                "transition_id", inspect.signature(SessionLifecycleCore.rebind).parameters
            )
            self.assertEqual(result["transferred_pending_count"], 0)
            self.assertEqual(
                store._connection.execute(
                    "SELECT binding_id, state FROM conversation_bindings ORDER BY generation"
                ).fetchall(),
                [(predecessor["binding_id"], "superseded"), ("binding_successor", "active")],
            )
            self.assertEqual(
                store._connection.execute(
                    "SELECT actor_id, reason, transferred_pending_count FROM conversation_binding_transition_audit"
                ).fetchall(),
                [("operator", "operator_rebind", 0)],
            )

    def test_consume_binding_state_defaults_active_and_honors_reserved(self) -> None:
        # #319 seam: the challenge-consume authority mints either the `active` binding
        # (the default, unchanged) or a pre-active `reserved` successor. A reserved
        # binding is NOT the resolvable active owner — that is exactly what lets it be
        # minted at generation+1 alongside the still-active predecessor and then swapped
        # in by rebind. An unknown state is rejected before any write.
        with LedgerStore.open_writer(self.paths) as store:
            default_subject = subject()
            default_binding = self.consume(
                store, default_subject, self.reserve(store, default_subject)
            )
            self.assertEqual("active", self._binding_state(store, default_binding["binding_id"]))

        with LedgerStore.open_writer(self.paths2) as store:
            reserved_subject = subject()
            reserved = self.core.consume(
                store, reserved_subject, self.reserve(store, reserved_subject),
                runtime_home=self.runtime_home, consumed_at_utc=BEFORE_EXPIRY,
                correlation_id="corr_consume", trusted_project_root=self.trusted_root,
                binding_state="reserved",
            )
            self.assertEqual("reserved", reserved["state"])
            self.assertEqual("reserved", self._binding_state(store, reserved["binding_id"]))
            self.assertFalse(
                store.resolve_conversation_binding(
                    workspace_id=WORKSPACE, scope_kind="project", scope_identity=PROJECT,
                    conversation_id=reserved_subject.conversation_id,
                    participant_id=reserved_subject.participant_id,
                ).get("resolved"),
                "a reserved successor must not resolve as the active owner",
            )

        with LedgerStore.open_writer(self.paths3) as store:
            bad_subject = subject()
            challenge = self.reserve(store, bad_subject)
            with self.assertRaises(ValueError):
                self.core.consume(
                    store, bad_subject, challenge, runtime_home=self.runtime_home,
                    consumed_at_utc=BEFORE_EXPIRY, correlation_id="corr_consume",
                    trusted_project_root=self.trusted_root, binding_state="draining",
                )

    def _binding_state(self, store, binding_id):
        return store._connection.execute(
            "SELECT state FROM conversation_bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()[0]

    def test_no_process_socket_ax_wallclock_or_runtime_consumers(self) -> None:
        root = Path(__file__).parents[1]
        lifecycle = root / "llm_collab" / "session_lifecycle.py"
        tree = ast.parse(lifecycle.read_text(encoding="utf-8"))
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports |= {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertFalse({"subprocess", "socket", "time", "datetime", "Quartz"} & imports)
        text = lifecycle.read_text(encoding="utf-8")
        for forbidden in (
            "_session_autobridge",
            "runtime_adapter",
            "Computer Use",
            "computer_use",
            "browser",
            "webbrowser",
        ):
            self.assertNotIn(forbidden, text)
        forbidden_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"now", "time", "monotonic"}:
                forbidden_calls.append(node.attr)
        self.assertEqual(forbidden_calls, [])

        # Pi registration (#378) drives reserve/consume to mint the canonical
        # binding, so this exact file references the lifecycle. Any OTHER runtime
        # path importing it is still an offender.
        allowed = {
            Path("bin/_session_autobridge.py"),
            # Worker projection (#396) composes the read-only operator
            # inspection seam for `worker show/list`; it is query-only and
            # never calls the provider or a mutation path.
            Path("llm_collab/worker.py"),
            # worker start verb (#271) drives register->reserve->consume->active.
            Path("bin/worker.py"),
            # The worker command keeps the Codex-specific transport and exact
            # worktree proof in a bounded subcommand module; it is the same
            # worker start verb, not a second runtime consumer.
            Path("bin/worker_codex.py"),
            # Codex delivery (#94) calls provider.attest as the exact-thread
            # identity proof before one idle-thread turn; it never drives
            # reserve/consume/retire lifecycle state in this slice.
            Path("llm_collab/canonical/codex_delivery.py"),
            # The daemon-owned GH-94 dispatch boundary constructs the provider
            # only after its independent exact-thread gate is effective.
            Path("llm_collab/daemon/server.py"),
        }
        offenders = []
        for checked in (root / "bin", root / "scripts", root / "llm_collab"):
            for path in checked.rglob("*.py"):
                if path == lifecycle or path.relative_to(root) in allowed:
                    continue
                text = path.read_text(encoding="utf-8")
                if "session_lifecycle" in text:
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])
