from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import worker_codex  # noqa: E402

from llm_collab.codex_app_server_live_probe import (  # noqa: E402
    CodexAppServerExactThreadResult,
)
from llm_collab.ledger import LedgerPaths, LedgerStore  # noqa: E402
from llm_collab.ledger.store import CanonicalConflictError  # noqa: E402
from llm_collab.worker import derive_worker_id  # noqa: E402


WORKSPACE = "ws_codex_start"
PROJECT = "llm-collab"
CHAT = "CHAT-CODEX-START"
PARTICIPANT = "participant_codex"
AGENT = "agent_codex"
THREAD_ID = "019f9452-6954-7301-bff9-db1c47432bc8"
VERSION = worker_codex.DEFAULT_CODEX_CLI_VERSION


class FakeAppServer:
    def __init__(self, *, codex_home: str, cwd: str) -> None:
        self.codex_home = codex_home
        self.cwd = cwd
        self.requests: list[dict] = []
        self.notifications: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def thread(self) -> dict:
        return {
            "cliVersion": VERSION,
            "createdAt": 1,
            "cwd": self.cwd,
            "ephemeral": False,
            "id": THREAD_ID,
            "modelProvider": "openai",
            "preview": "",
            "sessionId": "native-session-tree",
            "source": "appServer",
            "status": {"type": "idle"},
            "turns": [],
            "updatedAt": 1,
        }

    def exchange(self, frame: dict) -> dict:
        self.requests.append(frame)
        method = frame["method"]
        if method == "initialize":
            result = {
                "codexHome": self.codex_home,
                "userAgent": f"Codex Desktop/{VERSION} (test)",
            }
        elif method == "thread/start":
            result = {
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "cwd": self.cwd,
                "model": "gpt-test",
                "modelProvider": "openai",
                "sandbox": {"type": "readOnly"},
                "thread": self.thread(),
            }
        elif method == "thread/read":
            result = {"thread": self.thread()}
        else:
            raise AssertionError(f"unexpected method {method}")
        return {"jsonrpc": "2.0", "id": frame["id"], "result": result}

    def notify(self, frame: dict) -> None:
        self.notifications.append(frame)


class CodexWorkerStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.worktree = root / "worktree"
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir()
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True
        )
        (self.repo / "tracked").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True
        )
        subprocess.run(
            [
                "git", "-C", str(self.repo), "worktree", "add", "-q", "-b",
                "test-worktree", str(self.worktree),
            ],
            check=True,
        )
        self.cwd = self.worktree / "nested"
        self.cwd.mkdir()
        self.paths = LedgerPaths.derive(root / "state", WORKSPACE)
        with LedgerStore.open_writer(self.paths) as store:
            store._connection.execute(
                """
                INSERT INTO conversation_participants
                (workspace_id, scope_kind, scope_identity, conversation_id,
                 participant_id, agent_id, created_at_utc)
                VALUES (?, 'project', ?, ?, ?, ?, '2026-08-06T00:00:00+00:00')
                """,
                (WORKSPACE, PROJECT, CHAT, PARTICIPANT, AGENT),
            )
        self.worker_id = derive_worker_id(
            workspace_id=WORKSPACE,
            scope_kind="project",
            scope_identity=PROJECT,
            conversation_id=CHAT,
            participant_id=PARTICIPANT,
        )

    def args(self) -> argparse.Namespace:
        return argparse.Namespace(
            worker_id=self.worker_id,
            project=PROJECT,
            endpoint="ws://127.0.0.1:8767",
            endpoint_id="endpoint_codex_local",
            runtime_instance="runtime_codex_local",
            codex_home=str(self.codex_home),
            token_file=None,
            repo_id="app",
            cwd=str(self.cwd),
            model="gpt-test",
            model_provider="openai",
            timeout_seconds=5.0,
            expected_cli_version=VERSION,
            user_agent_product="Codex Desktop",
        )

    def patches(self, fake: FakeAppServer):
        return (
            mock.patch.object(worker_codex, "config_get", return_value=WORKSPACE),
            mock.patch.object(
                worker_codex, "project_state_root", return_value=self.paths.state_root
            ),
            mock.patch.object(
                worker_codex, "resolve_project_repo_path", return_value=self.repo
            ),
            mock.patch.object(
                worker_codex, "_WebSocketJsonRpcTransport", return_value=fake
            ),
            mock.patch.object(
                worker_codex,
                "probe_exact_thread",
                side_effect=lambda thread_id, **_kwargs: CodexAppServerExactThreadResult(
                    thread_id=thread_id, methods=("initialize", "thread/read")
                ),
            ),
        )

    def approve(self) -> None:
        with (
            mock.patch.object(worker_codex, "config_get", return_value=WORKSPACE),
            mock.patch.object(
                worker_codex, "project_state_root", return_value=self.paths.state_root
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            worker_codex.approve_codex_start(argparse.Namespace(project=PROJECT))

    def test_start_refuses_without_separate_provider_approval_before_native_io(self) -> None:
        fake = FakeAppServer(
            codex_home=str(self.codex_home.resolve()), cwd=str(self.cwd.resolve())
        )
        with contextlib.ExitStack() as stack:
            for patcher in self.patches(fake):
                stack.enter_context(patcher)
            with self.assertRaisesRegex(CanonicalConflictError, "allowlisted"):
                worker_codex.start_codex(self.args())
        self.assertEqual([], fake.requests)
        with LedgerStore.open_reader(self.paths) as store:
            count = store._connection.execute(
                "SELECT count(*) FROM lifecycle_provider_registry"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_start_creates_one_exact_persistent_thread_and_no_turn(self) -> None:
        self.approve()
        fake = FakeAppServer(
            codex_home=str(self.codex_home.resolve()), cwd=str(self.cwd.resolve())
        )
        with contextlib.ExitStack() as stack:
            for patcher in self.patches(fake):
                stack.enter_context(patcher)
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = worker_codex.start_codex(self.args())
        self.assertEqual(0, code)
        methods = [request["method"] for request in fake.requests]
        self.assertEqual(["initialize", "thread/start", "thread/read"], methods)
        self.assertNotIn("turn/start", methods)
        self.assertFalse(fake.requests[1]["params"]["ephemeral"])
        with LedgerStore.open_reader(self.paths) as store:
            binding = store._connection.execute(
                """
                SELECT provider_revision, native_session_id, state
                FROM conversation_bindings
                WHERE workspace_id = ? AND conversation_id = ?
                """,
                (WORKSPACE, CHAT),
            ).fetchone()
            reservation = store._connection.execute(
                "SELECT canonical_cwd, state FROM managed_start_reservations"
            ).fetchone()
        self.assertEqual(("revision_2", THREAD_ID, "active"), binding)
        self.assertEqual((str(self.cwd.resolve()), "bound"), reservation)

    def test_foreign_repository_is_rejected_before_native_io(self) -> None:
        foreign = Path(self.tmp.name) / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        with mock.patch.object(
            worker_codex, "resolve_project_repo_path", return_value=self.repo
        ):
            with self.assertRaisesRegex(
                worker_codex.CodexWorkerStartError, "not a worktree"
            ):
                worker_codex._trusted_worktree(PROJECT, "app", str(foreign))


if __name__ == "__main__":
    unittest.main()
