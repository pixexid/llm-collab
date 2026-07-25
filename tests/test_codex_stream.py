"""codex_stream must resolve exactly one thread and never answer a server request.

Two behaviours carry real risk. Resolving `--agent codex` when several bindings match
would silently watch one of several threads, which is the wrong-thread failure the
exact-dispatch contract exists to prevent. Answering a server-initiated request --
an approval -- would vote on the operator's behalf on a turn this observer does not
own; the observer must leave it for the turn owner.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import codex_stream  # noqa: E402


def binding(root: Path, project: str, chat: str, agent: str, thread: str,
            status: str = "active", updated: str = "2026-07-25T00:00:00+00:00") -> None:
    path = root / project / chat / f"{agent}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agent_id": agent, "project_id": project, "chat_id": chat,
        "runtime_session_id": thread, "status": status, "updated_utc": updated,
    }), encoding="utf-8")


class ResolveThreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(codex_stream, "BINDINGS_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, **kw) -> SimpleNamespace:
        base = {"agent": None, "project": None, "chat": None, "thread": None}
        base.update(kw)
        return SimpleNamespace(**base)

    def test_exact_project_and_chat_resolves(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        thread, provenance = codex_stream.resolve_thread(
            self.args(agent="codex", project="amiga", chat="CHAT-AAA"))
        self.assertEqual("thread-1", thread)
        self.assertIn("CHAT-AAA", provenance)

    def test_ambiguous_agent_lookup_refuses_instead_of_picking_one(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1")
        binding(self.root, "nuvyr", "CHAT-BBB", "codex", "thread-2")
        with self.assertRaises(SystemExit) as caught:
            codex_stream.resolve_thread(self.args(agent="codex"))
        message = str(caught.exception)
        self.assertIn("CHAT-AAA", message)
        self.assertIn("CHAT-BBB", message, "both candidates must be named")

    def test_chat_last_accepts_ambiguity_and_takes_the_newest(self) -> None:
        binding(self.root, "amiga", "CHAT-AAA", "codex", "thread-1",
                updated="2026-07-01T00:00:00+00:00")
        binding(self.root, "amiga", "CHAT-BBB", "codex", "thread-2",
                updated="2026-07-25T00:00:00+00:00")
        thread, _ = codex_stream.resolve_thread(self.args(agent="codex", chat="last"))
        self.assertEqual("thread-2", thread)

    def test_parked_binding_loses_to_an_active_one(self) -> None:
        binding(self.root, "amiga", "CHAT-OLD", "codex", "thread-parked", status="parked")
        binding(self.root, "amiga", "CHAT-NEW", "codex", "thread-active", status="active")
        thread, _ = codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))
        self.assertEqual("thread-active", thread)

    def test_binding_without_a_runtime_session_id_is_not_a_candidate(self) -> None:
        path = self.root / "amiga" / "CHAT-AAA" / "codex.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"agent_id": "codex", "runtime_session_id": None}),
                        encoding="utf-8")
        with self.assertRaises(SystemExit):
            codex_stream.resolve_thread(self.args(agent="codex", project="amiga"))

    def test_explicit_thread_bypasses_binding_lookup(self) -> None:
        thread, provenance = codex_stream.resolve_thread(self.args(thread="thread-x"))
        self.assertEqual("thread-x", thread)
        self.assertEqual("--thread", provenance)


class DescribeTest(unittest.TestCase):
    def test_agent_message_completion_stays_quiet_because_deltas_already_printed(self) -> None:
        self.assertIsNone(
            codex_stream.describe("item/completed", {"item": {"type": "agentMessage"}}))

    def test_command_execution_surfaces_the_command_and_exit_code(self) -> None:
        started = codex_stream.describe(
            "item/started", {"item": {"type": "commandExecution", "command": "pytest -q"}})
        self.assertIn("pytest -q", started)
        done = codex_stream.describe(
            "item/completed", {"item": {"type": "commandExecution", "exitCode": 1}})
        self.assertIn("1", done)


if __name__ == "__main__":
    unittest.main()
