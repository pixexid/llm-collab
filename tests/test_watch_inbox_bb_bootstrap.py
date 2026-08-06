from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import watch_inbox  # noqa: E402
import _session_autobridge as session_autobridge  # noqa: E402
import llm_collab.bb_managed_start as bb_managed_start  # noqa: E402


class BbWatcherBootstrapTest(unittest.TestCase):
    def packet(self, *, canonical_message_id: str | None = "cmid_1") -> dict:
        return {
            "path": "Chats/project/first.md",
            "frontmatter": {
                "project_id": "project-one",
                "chat_id": "CHAT-ONE",
                "canonical_message_id": canonical_message_id,
                # Every real packet declares its repo scope -- deliver.py has
                # required --repo-targets since #309 -- and a scoped subscriber
                # refuses an undeclared packet as route_ambiguous. Omitting it
                # here would encode a shape dispatch itself would never accept.
                "repo_targets": ["app"],
            },
            "body": "start the worker",
        }

    def scoped_packet(self, repo_targets: list[str]) -> dict:
        packet = self.packet()
        packet["frontmatter"] = {**packet["frontmatter"], "repo_targets": repo_targets}
        return packet

    def test_a_foreign_repo_packet_is_not_a_bootstrap_candidate(self) -> None:
        """Bootstrap runs BEFORE dispatch_session's repo gate.

        Without the scope check here a packet addressed to another repository in
        the same project would spawn a real bb thread and execute its prompt.
        The in-scope control is asserted alongside it so this cannot pass by
        excluding everything.
        """
        foreign = watch_inbox._bb_first_packets(
            "glmpi", "project-one", [self.scoped_packet(["other"])], ["app"]
        )
        self.assertEqual({}, foreign, "a packet scoped to another repo reached bootstrap")

        in_scope = watch_inbox._bb_first_packets(
            "glmpi", "project-one", [self.scoped_packet(["app"])], ["app"]
        )
        self.assertIn("CHAT-ONE", in_scope, "the scope filter excluded a legitimate packet")

    def test_disabled_gate_does_not_read_project_or_start(self) -> None:
        with patch.object(watch_inbox, "config_get", return_value=False), patch.object(
            watch_inbox, "get_project", side_effect=AssertionError("project lookup")
        ), patch.object(watch_inbox, "autobridge_session_ids", return_value=[]):
            consumed = watch_inbox.dispatch_autobridge(
                "glmpi",
                False,
                project_id="project-one",
                messages={"Chats/project/first.md": self.packet()},
            )
        self.assertEqual([], consumed)

    def test_first_delivery_bootstraps_before_session_enumeration(self) -> None:
        order: list[str] = []
        outcome = SimpleNamespace(
            state=watch_inbox.BOOTSTRAP_STARTED,
            detail="one thread started",
            native_thread_id="thread-1",
        )
        project = {"id": "project-one", "bb": {"enabled": True}}
        with patch.object(watch_inbox, "config_get", return_value=True), patch.object(
            watch_inbox, "get_project", return_value=project
        ), patch.object(
            watch_inbox, "_bb_existing_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "_bb_binding_state", return_value=None
        ), patch.object(
            watch_inbox,
            "_bb_start_inputs",
            return_value={"endpoint_id": "endpoint_bb", "runtime_instance_id": "runtime_bb"},
        ), patch.object(
            watch_inbox,
            "execute_bb_bootstrap_plan",
            side_effect=lambda *_args: (order.append("start") or outcome),
        ), patch.object(
            watch_inbox,
            "autobridge_session_ids",
            side_effect=lambda *_args: (order.append("enumerate") or []),
        ), patch.object(watch_inbox, "emit"):
            consumed = watch_inbox.dispatch_autobridge(
                "glmpi",
                False,
                project_id="project-one",
                messages={"Chats/project/first.md": self.packet()},
            )
        self.assertEqual(["start", "enumerate"], order)
        self.assertEqual(["Chats/project/first.md"], consumed)

    def test_terminal_binding_is_refused_without_bootstrap(self) -> None:
        project = {"id": "project-one", "bb": {"enabled": True}}
        with patch.object(watch_inbox, "config_get", return_value=True), patch.object(
            watch_inbox, "get_project", return_value=project
        ), patch.object(
            watch_inbox, "_bb_existing_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "_bb_binding_state", return_value="mismatch"
        ), patch.object(
            watch_inbox,
            "execute_bb_bootstrap_plan",
            side_effect=AssertionError("terminal binding must not start"),
        ), patch.object(watch_inbox, "autobridge_session_ids", return_value=[]), patch.object(
            watch_inbox, "emit"
        ) as emit:
            consumed = watch_inbox.dispatch_autobridge(
                "glmpi",
                False,
                project_id="project-one",
                messages={"Chats/project/first.md": self.packet()},
            )
        self.assertEqual([], consumed)
        self.assertTrue(
            any(
                call.args[0].get("reason") == "bb_bootstrap_terminal_binding"
                for call in emit.call_args_list
            )
        )

    def test_missing_canonical_id_is_not_spawnable(self) -> None:
        project = {"id": "project-one", "bb": {"enabled": True}}
        with patch.object(watch_inbox, "config_get", return_value=True), patch.object(
            watch_inbox, "get_project", return_value=project
        ), patch.object(
            watch_inbox, "_bb_existing_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "_bb_binding_state", return_value=None
        ), patch.object(
            watch_inbox,
            "_bb_start_inputs",
            side_effect=AssertionError("missing packet identity must refuse first"),
        ), patch.object(watch_inbox, "autobridge_session_ids", return_value=[]), patch.object(
            watch_inbox, "emit"
        ) as emit:
            consumed = watch_inbox.dispatch_autobridge(
                "glmpi",
                False,
                project_id="project-one",
                messages={"Chats/project/first.md": self.packet(canonical_message_id=None)},
            )
        self.assertEqual([], consumed)
        self.assertTrue(
            any(
                call.args[0].get("reason") == "bb_bootstrap_no_first_packet"
                for call in emit.call_args_list
            )
        )

    def test_first_delivery_completes_binding_before_publishing_session(self) -> None:
        project = {"id": "project-one", "bb": {"enabled": True}}
        candidate = {
            "native_thread_id": "thread_bb_one",
            "project_id": "project-one",
            "environment_id": "environment_one",
            "provider_id": "pi",
            "status": "active",
            "endpoint_id": "endpoint_bb_one",
            "runtime_instance_id": "runtime_bb_one",
            "provider": "pi",
            "model": "kimi-coding/k3",
            "reasoning_level": "high",
            "source": "managed_bb_thread_start",
        }
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-") as raw:
            root = Path(raw)
            state_root = root / "state"
            repo_root = root / "repo"
            runtime_home = root / "runtime-home"
            for path in (repo_root, runtime_home):
                path.mkdir()
            sessions = state_root / "sessions"
            bindings = state_root / "bindings"
            events = []
            with patch.object(watch_inbox, "bb_bootstrap_enabled", return_value=True), patch.object(
                watch_inbox, "config_get", return_value="ws_bb_one"
            ), patch.object(watch_inbox, "get_project", return_value=project), patch.object(
                watch_inbox,
                "_bb_start_inputs",
                return_value={
                    "native_project_id": "project-one",
                    "endpoint_id": "endpoint_bb_one",
                    "runtime_instance_id": "runtime_bb_one",
                    "runtime_home": str(runtime_home),
                    "repo_id": "app",
                    "repo_root": str(repo_root),
                    "cwd": str(repo_root),
                    "executable": ["bb"],
                    "timeout_seconds": 1.0,
                    "session_source": None,
                },
            ), patch.object(
                session_autobridge, "config_get", return_value="ws_bb_one"
            ), patch.object(
                session_autobridge, "project_state_root", return_value=state_root
            ), patch.object(
                session_autobridge, "AUTOBRIDGE_ROOT", state_root
            ), patch.object(
                session_autobridge, "SESSIONS_DIR", sessions
            ), patch.object(
                session_autobridge, "BINDINGS_DIR", bindings
            ), patch.object(
                session_autobridge, "EVENTS_DIR", state_root / "events"
            ), patch.object(
                session_autobridge, "SESSION_WRITE_LOCK", state_root / "session.lock"
            ), patch.object(
                bb_managed_start, "bb_start_native"
            ) as start_factory, patch.object(
                watch_inbox, "emit", side_effect=lambda event, _json: events.append(event)
            ):
                start_factory.return_value = lambda _start_id: candidate
                consumed = watch_inbox._bootstrap_bb_before_dispatch(
                    "glmpi",
                    False,
                    project_id="project-one",
                    repo_targets=["app"],
                    messages={"Chats/project/first.md": self.packet()},
                )
                published = [path.stem for path in sessions.glob("*.json")]
                session_payload = json.loads((sessions / f"{published[0]}.json").read_text())
            self.assertEqual(["Chats/project/first.md"], consumed, events)
            self.assertEqual(1, len(published))
            self.assertEqual(candidate["native_thread_id"], session_payload["runtime"]["session_id"])
            self.assertEqual(["Chats/project/first.md"], session_payload["processed_messages"])
            self.assertTrue((bindings / "project-one" / "CHAT-ONE" / "glmpi.json").is_file())


if __name__ == "__main__":
    unittest.main()
