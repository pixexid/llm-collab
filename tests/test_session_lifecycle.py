from __future__ import annotations

import ast
import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.ledger import LedgerPaths, LedgerStore
import llm_collab.ledger.store as store_module
from llm_collab.ledger.store import CanonicalConflictError
from llm_collab.session_lifecycle import (
    FakeLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
    SessionLifecycleError,
    TrustedProjectRoot,
)


WORKSPACE = "ws_alpha"
PROJECT = "amiga"
OTHER_PROJECT = "nuvyr"
NOW = "2026-07-23T00:00:00+00:00"
BEFORE_EXPIRY = "2026-07-23T00:00:59+00:00"
AT_EXPIRY = "2026-07-23T00:01:00+00:00"
SAFE_VERSION = (3, 51, 3)


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
        self.paths = LedgerPaths.derive(root / "state", WORKSPACE)
        self.core = SessionLifecycleCore(
            FakeLifecycleProvider(), token_factory=lambda: "token-alpha"
        )
        patcher = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def reserve(self, store: LedgerStore, active_subject: LifecycleSubject):
        return self.core.reserve(
            store,
            active_subject,
            runtime_home=self.runtime_home,
            created_at_utc=NOW,
            expires_at_utc=AT_EXPIRY,
            correlation_id="corr_reserve",
            trusted_project_root=self.trusted_root,
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

    def test_token_hash_is_stored_not_token_and_default_uses_secrets(self) -> None:
        active_subject = subject()
        with LedgerStore.open_writer(self.paths) as store:
            with patch("secrets.token_urlsafe", return_value="secret-token") as token_urlsafe:
                core = SessionLifecycleCore(FakeLifecycleProvider())
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
        forbidden_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"now", "time", "monotonic"}:
                forbidden_calls.append(node.attr)
        self.assertEqual(forbidden_calls, [])

        offenders = []
        for checked in (root / "bin", root / "scripts", root / "llm_collab"):
            for path in checked.rglob("*.py"):
                if path == lifecycle:
                    continue
                text = path.read_text(encoding="utf-8")
                if "session_lifecycle" in text:
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])
