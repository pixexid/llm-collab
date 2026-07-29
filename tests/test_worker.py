"""Read-only `worker show/list` proof for GH-396.

Exact lookup/listing through the CLI and cross-project isolation for two
same-agent workers, over the existing ledger/operator-inspection seams.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _helpers  # noqa: E402
import worker as worker_cli  # noqa: E402

import llm_collab.ledger.store as store_module  # noqa: E402
from llm_collab.codex_runtime_home import bind_runtime_home  # noqa: E402
from llm_collab.ledger import LedgerPaths, LedgerStore  # noqa: E402
from llm_collab.session_lifecycle import (  # noqa: E402
    FakeLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
    TrustedProjectRoot,
)
from llm_collab.worker import (  # noqa: E402
    MAX_PROJECT_WORKERS,
    WorkerLookupError,
    derive_worker_id,
    list_workers,
    show_worker,
)

WORKSPACE = "ws_alpha"
NOW = "2026-07-29T00:00:00+00:00"
EXPIRY = "2026-07-29T00:01:00+00:00"
SAFE_VERSION = (3, 51, 3)


class WorkerCliTest(unittest.TestCase):
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
        self.runtime_home = bind_runtime_home(self.codex_home)
        self.paths = LedgerPaths.derive(root / "state", WORKSPACE)
        self.core = SessionLifecycleCore(
            FakeLifecycleProvider(), token_factory=lambda: "token-alpha"
        )
        patcher = mock.patch.object(
            store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        projects_file = root / "projects.json"
        projects_file.write_text(
            '{"projects": [{"id": "amiga"}, {"id": "nuvyr"}]}', encoding="utf-8"
        )
        for target, value in (("PROJECTS_FILE", projects_file), ("_projects_cache", None)):
            patcher = mock.patch.object(_helpers, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_worker(self, store: LedgerStore, *, project: str, chat: str, native: str) -> str:
        subject = LifecycleSubject(
            workspace_id=WORKSPACE,
            scope_kind="project",
            scope_identity=project,
            conversation_id=chat,
            participant_id="participant_codex",
            agent_id="agent_codex",
            endpoint_id="endpoint_codex",
            native_session_id=native,
            runtime_instance_id="runtime_one",
        )
        store._connection.execute(
            """
            INSERT INTO conversation_participants
            (workspace_id, scope_kind, scope_identity, conversation_id, participant_id, agent_id, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject.workspace_id,
                subject.scope_kind,
                subject.scope_identity,
                subject.conversation_id,
                subject.participant_id,
                subject.agent_id,
                NOW,
            ),
        )
        descriptor = self.core.provider.descriptor()
        store._connection.execute(
            """
            INSERT OR IGNORE INTO lifecycle_provider_registry
            (workspace_id, provider_id, provider_revision, trust_class,
             supported_operations_json, challenge_algorithm, challenge_ttl_seconds, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject.workspace_id,
                descriptor["provider_id"],
                descriptor["provider_revision"],
                descriptor["trust_class"],
                descriptor["supported_operations_json"],
                descriptor["challenge_algorithm"],
                descriptor["challenge_ttl_seconds"],
                NOW,
            ),
        )
        trusted_root = TrustedProjectRoot(project, "repo_app", str(self.repo), str(self.cwd))
        challenge = self.core.reserve(
            store,
            subject,
            runtime_home=self.runtime_home,
            created_at_utc=NOW,
            expires_at_utc=EXPIRY,
            correlation_id="corr_reserve",
            trusted_project_root=trusted_root,
        )
        resolved = self.core.consume(
            store,
            subject,
            challenge,
            runtime_home=self.runtime_home,
            consumed_at_utc=NOW,
            correlation_id="corr_consume",
            trusted_project_root=trusted_root,
        )
        self.assertTrue(resolved["resolved"])
        return derive_worker_id(
            workspace_id=WORKSPACE,
            scope_kind="project",
            scope_identity=project,
            conversation_id=chat,
            participant_id="participant_codex",
        )

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(worker_cli, "config_get", return_value=WORKSPACE),
            mock.patch.object(
                worker_cli, "project_state_root", return_value=self.paths.state_root
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = worker_cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_cli_refuses_an_unregistered_project(self) -> None:
        for argv in (["list", "--project", "ghost"], ["show", "worker_" + "0" * 32, "--project", "ghost"]):
            code, _, err = self.run_cli(argv)
            self.assertEqual(code, 1)
            self.assertIn("Unknown project_id", err)

    def test_participant_scan_is_bounded_and_fails_closed(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            store._connection.executemany(
                """
                INSERT INTO conversation_participants
                (workspace_id, scope_kind, scope_identity, conversation_id, participant_id, agent_id, created_at_utc)
                VALUES (?, 'project', 'amiga', ?, 'participant_codex', 'agent_codex', ?)
                """,
                [
                    (WORKSPACE, f"CHAT-{index:04d}", NOW)
                    for index in range(MAX_PROJECT_WORKERS + 1)
                ],
            )
        with LedgerStore.open_reader(self.paths) as store:
            with self.assertRaises(WorkerLookupError):
                list_workers(store, workspace_id=WORKSPACE, project_id="amiga")
        with LedgerStore.open_writer(self.paths) as store:
            store._connection.execute(
                "DELETE FROM conversation_participants WHERE conversation_id = 'CHAT-0000'"
            )
        with LedgerStore.open_reader(self.paths) as store:
            workers = list_workers(store, workspace_id=WORKSPACE, project_id="amiga")
        self.assertEqual(len(workers), MAX_PROJECT_WORKERS)

    def test_pipe_framing_cannot_collide_distinct_tuples(self) -> None:
        base = {
            "workspace_id": WORKSPACE,
            "scope_kind": "project",
            "scope_identity": "amiga",
        }
        first = derive_worker_id(
            **base, conversation_id="CHAT-a|b", participant_id="part-c"
        )
        second = derive_worker_id(
            **base, conversation_id="CHAT-a", participant_id="b|part-c"
        )
        self.assertNotEqual(first, second)

    def test_cli_show_and_list_exact_lookup(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            worker_id = self.add_worker(store, project="amiga", chat="CHAT-ALPHA1", native="native_one")
        code, out, _ = self.run_cli(["list", "--project", "amiga"])
        self.assertEqual(code, 0)
        self.assertIn(worker_id, out)
        self.assertIn("agent_codex", out)
        self.assertIn("CHAT-ALPHA1", out)
        self.assertIn("active", out)

        code, out, _ = self.run_cli(["show", worker_id, "--project", "amiga"])
        self.assertEqual(code, 0)
        self.assertIn(f"worker_id: {worker_id}", out)
        self.assertIn("resolved: True", out)
        self.assertIn("generation: 1", out)
        self.assertIn("session_ref_id: session_", out)
        self.assertIn("native_session_id: native_one", out)

        code, _, err = self.run_cli(["show", "worker_" + "0" * 32, "--project", "amiga"])
        self.assertEqual(code, 1)
        self.assertIn("no worker", err)

    def test_same_agent_workers_are_cross_project_isolated(self) -> None:
        with LedgerStore.open_writer(self.paths) as store:
            amiga_id = self.add_worker(store, project="amiga", chat="CHAT-SAMEID", native="native_one")
            nuvyr_id = self.add_worker(store, project="nuvyr", chat="CHAT-SAMEID", native="native_two")
        self.assertNotEqual(amiga_id, nuvyr_id)

        code, out, _ = self.run_cli(["list", "--project", "amiga"])
        self.assertEqual(code, 0)
        self.assertIn(amiga_id, out)
        self.assertNotIn(nuvyr_id, out)

        code, _, err = self.run_cli(["show", nuvyr_id, "--project", "amiga"])
        self.assertEqual(code, 1)
        self.assertIn("no worker", err)

        with LedgerStore.open_reader(self.paths) as store:
            shown = show_worker(
                store, workspace_id=WORKSPACE, project_id="nuvyr", worker_id=nuvyr_id
            )
        self.assertEqual(shown["scope_identity"], "nuvyr")
        self.assertEqual(shown["native_session_id"], "native_two")


if __name__ == "__main__":
    unittest.main()
