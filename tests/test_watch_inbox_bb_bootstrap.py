from __future__ import annotations

import sys
import tempfile
import unittest
import json
import io
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import watch_inbox  # noqa: E402
import _helpers as helpers  # noqa: E402
import _session_autobridge as session_autobridge  # noqa: E402
from _helpers import InboxScanLimitExceeded  # noqa: E402
import llm_collab.bb_managed_start as bb_managed_start  # noqa: E402
from llm_collab.bb_client import (  # noqa: E402
    PINNED_BB_VERSION,
    SLICE_1A_PROFILE,
    BbClient,
    BbRefusal,
    BbTransportResult,
    BbTransportTimeout,
    subprocess_transport,
)
from llm_collab.bb_bootstrap import BOOTSTRAP_PROFILE_UNAVAILABLE  # noqa: E402


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

    def test_bb_bootstrap_aborts_before_launch_when_unread_scan_exceeds_cap(self) -> None:
        error = InboxScanLimitExceeded("over cap")
        with patch.object(
            watch_inbox, "bb_bootstrap_enabled", return_value=True
        ), patch.object(
            watch_inbox, "get_unread_messages", side_effect=error
        ), patch.object(watch_inbox, "execute_bb_bootstrap_plan") as execute:
            with self.assertRaises(InboxScanLimitExceeded):
                watch_inbox._bootstrap_bb_before_dispatch(
                    "glmpi",
                    False,
                    project_id="project-one",
                    repo_targets=["app"],
                    messages=None,
                )

        execute.assert_not_called()

    def test_default_watcher_reports_over_cap_poll_and_keeps_running(self) -> None:
        error = InboxScanLimitExceeded("over cap")
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-watch-cap-") as raw:
            inbox_path = Path(raw) / "inbox.json"
            inbox_path.write_text('{"unread": ["Chats/packet.md"]}')
            argv = [
                "watch_inbox.py",
                "--me",
                "glmpi",
                "--project",
                "amiga",
                "--poll-seconds",
                "1",
                "--max-polls",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                watch_inbox, "require_current_runtime"
            ), patch.object(
                watch_inbox, "agent_ids", return_value=["glmpi"]
            ), patch.object(
                watch_inbox, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                watch_inbox,
                "load_agent_inbox",
                return_value={"unread": ["Chats/packet.md"]},
            ), patch.object(
                watch_inbox, "get_unread_messages", side_effect=error
            ), patch.object(
                watch_inbox, "load_refusal_progress", return_value={}
            ), patch.object(
                watch_inbox, "dispatch_autobridge"
            ) as dispatch, patch.object(watch_inbox, "emit") as emit:
                watch_inbox.main()

        dispatch.assert_not_called()
        self.assertTrue(
            any(
                call.args[0].get("event") == "error"
                and call.args[0].get("detail") == "over cap"
                for call in emit.call_args_list
            )
        )

    def test_default_watcher_refuses_oversized_index_before_unbounded_index_load(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-watch-bytes-") as raw:
            inbox_path = Path(raw) / "inbox.json"
            inbox_path.write_text('{"unread": [], "read": []}' + " " * 128)
            argv = [
                "watch_inbox.py",
                "--me",
                "glmpi",
                "--project",
                "amiga",
                "--poll-seconds",
                "1",
                "--max-polls",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                watch_inbox, "require_current_runtime"
            ), patch.object(
                watch_inbox, "agent_ids", return_value=["glmpi"]
            ), patch.object(
                watch_inbox, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                helpers, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                session_autobridge,
                "MAX_DISPATCH_INBOX_BYTES",
                inbox_path.stat().st_size - 1,
            ), patch.object(
                watch_inbox, "load_agent_inbox"
            ) as unbounded_load, patch.object(
                watch_inbox, "load_refusal_progress", return_value={}
            ), patch.object(
                watch_inbox, "dispatch_autobridge"
            ) as dispatch, patch.object(watch_inbox, "emit") as emit:
                watch_inbox.main()

        unbounded_load.assert_not_called()
        dispatch.assert_not_called()
        self.assertTrue(
            any(
                call.args[0].get("event") == "error"
                and "byte limit" in call.args[0].get("detail", "")
                for call in emit.call_args_list
            )
        )

    def test_skip_existing_refuses_oversized_index_before_startup_snapshot(self) -> None:
        real_get_unread = watch_inbox.get_unread_messages
        real_load_inbox = watch_inbox.load_agent_inbox
        get_calls = 0
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-watch-skip-") as raw:
            inbox_path = Path(raw) / "inbox.json"
            inbox_path.write_text(
                json.dumps(
                    {
                        "unread": [],
                        "read": [],
                        "padding": "x" * 128,
                    }
                )
            )

            def refuse_then_repair(*args, **kwargs):
                nonlocal get_calls
                get_calls += 1
                try:
                    return real_get_unread(*args, **kwargs)
                except InboxScanLimitExceeded:
                    inbox_path.write_text('{"unread": [], "read": []}')
                    raise

            argv = [
                "watch_inbox.py",
                "--me",
                "glmpi",
                "--project",
                "amiga",
                "--skip-existing",
                "--no-autobridge",
                "--poll-seconds",
                "1",
                "--max-polls",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                watch_inbox, "require_current_runtime"
            ), patch.object(
                watch_inbox, "agent_ids", return_value=["glmpi"]
            ), patch.object(
                watch_inbox, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                helpers, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                session_autobridge, "MAX_DISPATCH_INBOX_BYTES", 64
            ), patch.object(
                watch_inbox,
                "get_unread_messages",
                side_effect=refuse_then_repair,
            ), patch.object(
                watch_inbox,
                "load_agent_inbox",
                wraps=real_load_inbox,
            ) as unbounded_startup, patch.object(
                watch_inbox, "load_refusal_progress", return_value={}
            ), patch.object(watch_inbox.time, "sleep"), patch.object(
                watch_inbox, "emit"
            ) as emit:
                watch_inbox.main()

        unbounded_startup.assert_not_called()
        self.assertGreaterEqual(get_calls, 2, "startup never retried after repair")
        self.assertTrue(
            any(
                call.args[0].get("event") == "error"
                and "byte limit" in call.args[0].get("detail", "")
                for call in emit.call_args_list
            )
        )

    def test_oversized_index_swap_never_reaches_a_second_watcher_read(self) -> None:
        real_get_unread = watch_inbox.get_unread_messages
        real_load_inbox = watch_inbox.load_agent_inbox
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-watch-swap-") as raw:
            inbox_path = Path(raw) / "inbox.json"
            inbox_path.write_text('{"unread": [], "read": []}')

            def snapshot_then_swap(*args, **kwargs):
                messages = real_get_unread(*args, **kwargs)
                inbox_path.write_text(
                    json.dumps(
                        {
                            "unread": [],
                            "read": [],
                            "padding": "x" * 128,
                        }
                    )
                )
                return messages

            argv = [
                "watch_inbox.py",
                "--me",
                "glmpi",
                "--project",
                "amiga",
                "--poll-seconds",
                "1",
                "--max-polls",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                watch_inbox, "require_current_runtime"
            ), patch.object(
                watch_inbox, "agent_ids", return_value=["glmpi"]
            ), patch.object(
                watch_inbox, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                helpers, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                session_autobridge, "MAX_DISPATCH_INBOX_BYTES", 64
            ), patch.object(
                watch_inbox,
                "get_unread_messages",
                side_effect=snapshot_then_swap,
            ), patch.object(
                watch_inbox,
                "load_agent_inbox",
                wraps=real_load_inbox,
            ) as second_read, patch.object(
                watch_inbox, "load_refusal_progress", return_value={}
            ), patch.object(
                watch_inbox, "dispatch_autobridge"
            ) as dispatch, patch.object(watch_inbox, "emit"):
                watch_inbox.main()
            swapped_size = inbox_path.stat().st_size

        self.assertGreater(swapped_size, 64)
        second_read.assert_not_called()
        self.assertEqual({}, dispatch.call_args.kwargs["messages"])

    def test_acknowledgement_cannot_split_unread_snapshot_from_bb_messages(self) -> None:
        packet = self.packet()
        path = packet["path"]

        def bounded_snapshot(_agent_id, *, snapshot_paths, **_kwargs):
            snapshot_paths.add(path)
            return [packet]

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-watch-ack-") as raw:
            inbox_path = Path(raw) / "inbox.json"
            inbox_path.write_text(json.dumps({"unread": [path], "read": []}))
            argv = [
                "watch_inbox.py",
                "--me",
                "glmpi",
                "--project",
                "project-one",
                "--poll-seconds",
                "1",
                "--max-polls",
                "1",
            ]
            with patch.object(sys, "argv", argv), patch.object(
                watch_inbox, "require_current_runtime"
            ), patch.object(
                watch_inbox, "agent_ids", return_value=["glmpi"]
            ), patch.object(
                watch_inbox, "agent_inbox_path", return_value=inbox_path
            ), patch.object(
                watch_inbox,
                "get_unread_messages",
                side_effect=bounded_snapshot,
            ), patch.object(
                watch_inbox,
                "load_agent_inbox",
                return_value={"unread": [], "read": [path]},
            ) as acknowledged_second_read, patch.object(
                watch_inbox, "load_refusal_progress", return_value={}
            ), patch.object(
                watch_inbox, "dispatch_autobridge"
            ) as dispatch, patch.object(watch_inbox, "emit") as emit:
                watch_inbox.main()

        acknowledged_second_read.assert_not_called()
        self.assertTrue(
            any(
                call.args[0].get("event") == "new_message"
                and call.args[0].get("detail") == path
                for call in emit.call_args_list
            ),
            "the watcher treated the packet as acknowledged while passing its older "
            "message snapshot to task-bearing dispatch",
        )
        self.assertEqual({path: packet}, dispatch.call_args.kwargs["messages"])

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

    def test_packet_repo_target_binds_unscoped_watcher_and_missing_refuses(self) -> None:
        project = {"id": "project-one", "bb": {"enabled": True}}
        outcome = SimpleNamespace(
            state=watch_inbox.BOOTSTRAP_STARTED,
            detail="one docs thread started",
            native_thread_id="thread-docs",
        )
        start_repo_ids: list[str] = []

        def start_inputs(_project_id, _project, repo_id):
            start_repo_ids.append(repo_id)
            return {"repo_id": repo_id}

        with patch.object(watch_inbox, "bb_bootstrap_enabled", return_value=True), patch.object(
            watch_inbox, "get_project", return_value=project
        ), patch.object(
            watch_inbox, "_bb_existing_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "_bb_binding_state", return_value=None
        ), patch.object(
            watch_inbox, "_bb_start_inputs", side_effect=start_inputs
        ), patch.object(
            watch_inbox, "execute_bb_bootstrap_plan", return_value=outcome
        ), patch.object(watch_inbox, "emit") as emit:
            consumed = watch_inbox._bootstrap_bb_before_dispatch(
                "glmpi",
                False,
                project_id="project-one",
                repo_targets=None,
                messages={"Chats/project/first.md": self.scoped_packet(["docs"])},
            )

            self.assertEqual(
                ["Chats/project/first.md"],
                consumed,
                "failures=gh581_unscoped_docs_packet_must_bootstrap_once",
            )
            self.assertEqual(
                ["docs"],
                start_repo_ids,
                "failures=gh581_unscoped_docs_packet_must_not_select_app",
            )

            start_repo_ids.clear()
            emit.reset_mock()
            packet_without_repo = self.packet()
            packet_without_repo["frontmatter"] = {
                key: value
                for key, value in packet_without_repo["frontmatter"].items()
                if key != "repo_targets"
            }
            consumed = watch_inbox._bootstrap_bb_before_dispatch(
                "glmpi",
                False,
                project_id="project-one",
                repo_targets=None,
                messages={"Chats/project/first.md": packet_without_repo},
            )
            self.assertEqual([], consumed, "failures=gh581_missing_packet_repo_refuses")
            self.assertEqual([], start_repo_ids, "failures=gh581_missing_packet_repo_no_start")
            self.assertTrue(
                any(
                    call.args[0].get("reason") == "bb_bootstrap_repo_target_required"
                    for call in emit.call_args_list
                ),
                "failures=gh581_missing_packet_repo_typed_refusal",
            )

            emit.reset_mock()
            start_repo_ids.clear()
            consumed = watch_inbox._bootstrap_bb_before_dispatch(
                "glmpi",
                False,
                project_id="project-one",
                repo_targets=["docs"],
                messages={"Chats/project/first.md": self.scoped_packet(["docs"])},
            )

        self.assertEqual(
            ["Chats/project/first.md"],
            consumed,
            "failures=gh581_scoped_docs_control_must_bootstrap",
        )
        self.assertEqual(
            ["docs"],
            start_repo_ids,
            "failures=gh581_scoped_docs_control_must_use_docs",
        )

    def test_a_bootstrap_only_delivery_is_marked_read(self) -> None:
        """GH-584: the executed first packet must not stay unread forever.

        A bootstrap-only delivery publishes a session that already lists the
        packet in `processed_messages`, so its `dispatch_session` yields no
        actions and takes the early `continue`. While mark_messages_read lived
        INSIDE the per-session loop that `continue` skipped it, so the packet
        that was just executed stayed unread and every later poll revisited and
        refused it. Asserting the packet is returned as consumed is not enough —
        it was already returned before the fix — so this asserts what was
        actually MARKED.
        """
        outcome = SimpleNamespace(
            state=watch_inbox.BOOTSTRAP_STARTED,
            detail="one thread started",
            native_thread_id="thread-1",
        )
        project = {"id": "project-one", "bb": {"enabled": True}}
        marked: list[list[str]] = []

        with patch.object(watch_inbox, "bb_bootstrap_enabled", return_value=True), patch.object(
            watch_inbox, "get_project", return_value=project
        ), patch.object(
            watch_inbox, "_bb_existing_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "_bb_binding_state", return_value=None
        ), patch.object(
            watch_inbox, "_bb_start_inputs", return_value={"repo_id": "app"}
        ), patch.object(
            watch_inbox, "execute_bb_bootstrap_plan", return_value=outcome
        ), patch.object(
            # No sessions to enumerate: the bootstrap-only shape, where the loop
            # body never runs to completion for any session.
            watch_inbox, "autobridge_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "mark_messages_read", side_effect=lambda _a, paths: marked.append(list(paths))
        ), patch.object(watch_inbox, "emit"):
            consumed = watch_inbox.dispatch_autobridge(
                "glmpi",
                False,
                project_id="project-one",
                repo_targets=["app"],
                messages={"Chats/project/first.md": self.packet()},
            )

        self.assertEqual(["Chats/project/first.md"], consumed)
        self.assertEqual(
            [["Chats/project/first.md"]],
            marked,
            "the executed bootstrap packet was never marked read, so every later "
            "poll would revisit and refuse it",
        )

    def test_bootstrap_is_acknowledged_even_if_enumeration_raises(self) -> None:
        """The acknowledgement must not depend on anything downstream succeeding.

        Moving mark_messages_read after the per-session loop was not enough: if
        autobridge_session_ids() raises -- a corrupt session record aborts
        enumeration -- the end-of-function mark is never reached. The packet is
        then stranded permanently, because later polls find the session already
        exists so bootstrap returns nothing, while that session already lists the
        packet in processed_messages so no dispatch action re-adds it. Verified by
        probe: without the early mark the marked list is empty here.
        """
        outcome = SimpleNamespace(
            state=watch_inbox.BOOTSTRAP_STARTED,
            detail="one thread started",
            native_thread_id="thread-1",
        )
        marked: list[list[str]] = []

        with patch.object(watch_inbox, "bb_bootstrap_enabled", return_value=True), patch.object(
            watch_inbox, "get_project", return_value={"id": "project-one", "bb": {"enabled": True}}
        ), patch.object(
            watch_inbox, "_bb_existing_session_ids", return_value=[]
        ), patch.object(
            watch_inbox, "_bb_binding_state", return_value=None
        ), patch.object(
            watch_inbox, "_bb_start_inputs", return_value={"repo_id": "app"}
        ), patch.object(
            watch_inbox, "execute_bb_bootstrap_plan", return_value=outcome
        ), patch.object(
            watch_inbox, "autobridge_session_ids", side_effect=ValueError("corrupt session record")
        ), patch.object(
            watch_inbox, "mark_messages_read", side_effect=lambda _a, paths: marked.append(list(paths))
        ), patch.object(watch_inbox, "emit"):
            with self.assertRaises(ValueError):
                watch_inbox.dispatch_autobridge(
                    "glmpi",
                    False,
                    project_id="project-one",
                    repo_targets=["app"],
                    messages={"Chats/project/first.md": self.packet()},
                )

        self.assertEqual(
            [["Chats/project/first.md"]],
            marked,
            "the executed bootstrap packet was not acknowledged before enumeration, "
            "so an enumeration failure strands it permanently",
        )

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

    def _authoring_packet(self) -> dict:
        # A writer-lane first delivery: deliver.py --activation atomically binds
        # a worktree and branch to the packet. The body is irrelevant to the
        # work-type decision (GH-596): the lane identity is the signal.
        packet = self.packet()
        packet["frontmatter"] = {
            **packet["frontmatter"],
            "worktree": "/repo/worktrees/lane",
            "branch": "bb/feat-x",
        }
        return packet

    def test_authoring_first_delivery_refuses_profile_unavailable_without_spawning(self) -> None:
        """GH-596: a writer-lane first delivery must refuse, not launch K3."""
        project = {"id": "project-one", "bb": {"enabled": True}}
        events: list[dict] = []
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-") as raw:
            root = Path(raw)
            state_root = root / "state"
            repo_root = root / "repo"
            runtime_home = root / "runtime-home"
            for path in (repo_root, runtime_home):
                path.mkdir()
            with patch.object(
                watch_inbox, "bb_bootstrap_enabled", return_value=True
            ), patch.object(
                watch_inbox, "config_get", return_value="ws_bb_one"
            ), patch.object(
                watch_inbox, "get_project", return_value=project
            ), patch.object(
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
                session_autobridge, "SESSIONS_DIR", state_root / "sessions"
            ), patch.object(
                session_autobridge, "BINDINGS_DIR", state_root / "bindings"
            ), patch.object(
                session_autobridge, "EVENTS_DIR", state_root / "events"
            ), patch.object(
                session_autobridge, "SESSION_WRITE_LOCK", state_root / "session.lock"
            ), patch.object(
                bb_managed_start, "bb_start_native"
            ) as start_factory, patch.object(
                watch_inbox, "emit", side_effect=lambda event, _json: events.append(event)
            ):
                consumed = watch_inbox._bootstrap_bb_before_dispatch(
                    "glmpi",
                    False,
                    project_id="project-one",
                    repo_targets=["app"],
                    messages={"Chats/project/first.md": self._authoring_packet()},
                )
        self.assertEqual([], consumed, "an authoring refusal must not consume the packet")
        self.assertFalse(start_factory.called, "the spawn must never run for a refused profile")
        refused = [e for e in events if e.get("event") == "bb_bootstrap"]
        self.assertEqual(1, len(refused), events)
        self.assertEqual(BOOTSTRAP_PROFILE_UNAVAILABLE, refused[0]["reason"])

    def test_analysis_first_delivery_still_launches_on_slice_1a_profile(self) -> None:
        """GH-596 both directions: a read-only first delivery still launches K3."""
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
            with patch.object(
                watch_inbox, "bb_bootstrap_enabled", return_value=True
            ), patch.object(
                watch_inbox, "config_get", return_value="ws_bb_one"
            ), patch.object(
                watch_inbox, "get_project", return_value=project
            ), patch.object(
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
                session_autobridge, "SESSIONS_DIR", state_root / "sessions"
            ), patch.object(
                session_autobridge, "BINDINGS_DIR", state_root / "bindings"
            ), patch.object(
                session_autobridge, "EVENTS_DIR", state_root / "events"
            ), patch.object(
                session_autobridge, "SESSION_WRITE_LOCK", state_root / "session.lock"
            ), patch.object(
                bb_managed_start, "bb_start_native"
            ) as start_factory:
                start_factory.return_value = lambda _start_id: candidate
                # self.packet() carries NO worktree/branch: a read-only analysis
                # first delivery, which must still launch on SLICE_1A_PROFILE.
                consumed = watch_inbox._bootstrap_bb_before_dispatch(
                    "glmpi",
                    False,
                    project_id="project-one",
                    repo_targets=["app"],
                    messages={"Chats/project/first.md": self.packet()},
                )
        self.assertTrue(start_factory.called, "an analysis first delivery must still spawn")
        self.assertEqual(SLICE_1A_PROFILE, start_factory.call_args.kwargs["profile"])
        self.assertEqual(["Chats/project/first.md"], consumed)

    def test_unscoped_watcher_observes_using_each_session_project(self) -> None:
        session = {
            "session_id": "bb-session-one",
            "project_id": "project-one",
            "runtime": {"family": "bb", "session_id": "thread-one"},
        }
        store = object()
        result = SimpleNamespace(
            detail="no events",
            state="idle",
            last_event_seq=0,
            processed_events=0,
            receipt_id=None,
        )
        with patch.object(watch_inbox, "bb_bootstrap_enabled", return_value=True), patch.object(
            watch_inbox, "config_get", return_value="ws_bb_one"
        ), patch.object(
            watch_inbox, "project_state_root", return_value=Path("/tmp/state")
        ), patch.object(
            watch_inbox, "get_project", return_value={"id": "project-one", "bb": {"enabled": True}}
        ) as get_project, patch.object(
            watch_inbox.LedgerPaths, "derive", return_value=object()
        ), patch.object(
            watch_inbox.LedgerStore, "open_writer", return_value=nullcontext(store)
        ), patch(
            "llm_collab.bb_continuation.client_from_project",
            return_value=SimpleNamespace(
                _transport=lambda *_args: None,
                events_after=lambda *_args, **_kwargs: None,
            ),
        ), patch(
            "llm_collab.bb_continuation.observe_bb_thread", return_value=result
        ) as observe, patch.object(watch_inbox, "emit"):
            watch_inbox._observe_bb_session(session, False)

        get_project.assert_called_once_with("project-one")
        self.assertEqual(session, observe.call_args.kwargs["session"])

    def test_each_empty_watcher_poll_still_observes_bb_sessions(self) -> None:
        observed_projects: list[str] = []
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="bb-watch-") as raw:
            inbox_path = Path(raw) / "inbox.json"
            inbox_path.write_text('{"unread": []}')
            for project_id in ("amiga", "nuvyr"):
                session = {
                    "session_id": f"bb-session-{project_id}",
                    "project_id": project_id,
                    "runtime": {"family": "bb", "session_id": f"thread-{project_id}"},
                }
                argv = [
                    "watch_inbox.py",
                    "--me",
                    "glmpi",
                    "--project",
                    project_id,
                    "--poll-seconds",
                    "1",
                    "--max-polls",
                    "1",
                ]
                with patch.object(sys, "argv", argv), patch.object(
                    watch_inbox, "require_current_runtime"
                ), patch.object(
                    watch_inbox, "agent_ids", return_value=["glmpi"]
                ), patch.object(
                    watch_inbox, "agent_inbox_path", return_value=inbox_path
                ), patch.object(
                    watch_inbox, "load_agent_inbox", return_value={"unread": []}
                ), patch.object(
                    watch_inbox, "get_unread_messages", return_value=[]
                ), patch.object(
                    watch_inbox, "load_refusal_progress", return_value={}
                ), patch.object(
                    watch_inbox, "bb_bootstrap_enabled", return_value=True
                ), patch.object(
                    watch_inbox,
                    "autobridge_session_ids",
                    return_value=[session["session_id"]],
                ), patch.object(
                    watch_inbox, "load_session", return_value=session
                ), patch.object(
                    watch_inbox, "session_has_exact_canonical_binding", return_value=True
                ), patch.object(
                    watch_inbox, "dispatch_session", return_value={"actions": []}
                ), patch.object(
                    watch_inbox,
                    "_observe_bb_session",
                    side_effect=lambda item, _json: observed_projects.append(
                        str(item["project_id"])
                    ),
                ), patch.object(watch_inbox, "emit"):
                    watch_inbox.main()

        self.assertEqual(["amiga", "nuvyr"], observed_projects)

    def test_global_bb_gate_blocks_existing_session_continuation(self) -> None:
        native_calls: list[str] = []

        class Refused(RuntimeError):
            pass

        continuation_module = SimpleNamespace(
            BbContinuationRefused=Refused,
            client_from_project=lambda _project: object(),
            continue_bb_thread=lambda *_args, **_kwargs: (
                native_calls.append("thread tell")
                or SimpleNamespace(
                    state="queued",
                    detail="queued",
                    message_id="message",
                    delivery_id="delivery",
                    attempt_id="attempt",
                    receipt_id="receipt",
                    native_called=True,
                )
            ),
        )
        ledger_module = SimpleNamespace(
            LedgerPaths=SimpleNamespace(derive=lambda *_args: object()),
            LedgerStore=SimpleNamespace(
                open_writer=lambda _paths: nullcontext(object())
            ),
        )

        def module(name: str):
            return ledger_module if name == "llm_collab.ledger" else continuation_module

        recorded = []
        for project_id in ("amiga", "nuvyr"):
            with patch.object(
                session_autobridge, "bb_bootstrap_enabled", return_value=False
            ), patch.object(
                session_autobridge.importlib, "import_module", side_effect=module
            ), patch.object(
                session_autobridge, "config_get", return_value="ws_test"
            ), patch.object(
                session_autobridge,
                "project_state_root",
                return_value=Path("/tmp/state"),
            ), patch.object(
                session_autobridge, "get_project", return_value={"id": project_id}
            ):
                result = session_autobridge.execute_runtime_trigger(
                    {
                        "project_id": project_id,
                        "runtime": {"family": "bb", "session_id": "thread-one"},
                    },
                    {"body": "must not send"},
                    {"materialized": True},
                )
            recorded.append((project_id, result["status"], result["delivery_accepted"]))

        self.assertEqual(
            (
                [
                    ("amiga", "bb_adapter_disabled", False),
                    ("nuvyr", "bb_adapter_disabled", False),
                ],
                [],
            ),
            (recorded, native_calls),
        )


class BbWatcherBreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        watch_inbox._bb_timeout_streaks.clear()

    def tearDown(self) -> None:
        watch_inbox._bb_timeout_streaks.clear()

    @staticmethod
    def session(session_id: str = "bb-session-one") -> dict:
        return {
            "session_id": session_id,
            "project_id": "project-one",
            "runtime": {"family": "bb", "session_id": "thread-one"},
        }

    @staticmethod
    def observation(state: str) -> SimpleNamespace:
        return SimpleNamespace(
            detail=state,
            state=state,
            last_event_seq=0,
            processed_events=0,
            receipt_id=None,
        )

    @classmethod
    def observe_real_events_after(cls, _store, *, client, **_kwargs):
        page = client.events_after("thread-one", 0)
        return cls.observation("ambiguous" if isinstance(page, BbRefusal) else "idle")

    @staticmethod
    def version_ok() -> BbTransportResult:
        return BbTransportResult(
            0,
            json.dumps({"currentVersion": PINNED_BB_VERSION}),
            "",
        )

    def observation_patches(self, clients, emitted, observe):
        return (
            patch.object(watch_inbox, "bb_bootstrap_enabled", return_value=True),
            patch.object(watch_inbox, "config_get", return_value="workspace-one"),
            patch.object(watch_inbox, "project_state_root", return_value=Path("/tmp/state")),
            patch.object(
                watch_inbox,
                "get_project",
                return_value={"id": "project-one", "bb": {"enabled": True}},
            ),
            patch.object(watch_inbox.LedgerPaths, "derive", return_value=object()),
            patch.object(
                watch_inbox.LedgerStore,
                "open_writer",
                return_value=nullcontext(object()),
            ),
            patch(
                "llm_collab.bb_continuation.client_from_project",
                side_effect=clients,
            ),
            patch("llm_collab.bb_continuation.observe_bb_thread", side_effect=observe),
            patch.object(watch_inbox, "emit", side_effect=lambda event, _json: emitted.append(event)),
        )

    def test_tripped_breaker_blocks_concurrent_popen_entries(self) -> None:
        import llm_collab.bb_client as bb_client

        entries: list[int] = []
        release = threading.Event()

        class StalledPopen:
            def __init__(self, *_args, **_kwargs):
                entries.append(threading.get_ident())
                release.wait()
                self.stdout = io.StringIO("{}")
                self.stderr = io.StringIO("")

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        transport = subprocess_transport(["fake-bb"])
        clients = [BbClient(transport, enabled=True, timeout_seconds=0.02) for _ in range(10)]
        emitted: list[dict] = []
        patches = self.observation_patches(
            clients, emitted, self.observe_real_events_after
        )
        before_threads = set(threading.enumerate())
        try:
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patch.object(
                bb_client.subprocess, "Popen", StalledPopen
            ):
                self.assertFalse(watch_inbox._observe_bb_session(self.session(), False))
                watch_inbox._observe_bb_session(self.session(), False)
                entries_at_trip = len(entries)

                barrier = threading.Barrier(9)
                results: list[bool] = []
                errors: list[BaseException] = []

                def poll() -> None:
                    try:
                        barrier.wait()
                        results.append(watch_inbox._observe_bb_session(self.session(), False))
                    except BaseException as error:
                        errors.append(error)

                threads = [threading.Thread(target=poll) for _ in range(8)]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join(timeout=1)

                self.assertEqual([], errors)
                self.assertEqual(8, len(results))
                self.assertEqual(
                    entries_at_trip,
                    len(entries),
                    "failures=gh597_tripped_breaker_entered_popen",
                )
                self.assertEqual(
                    1,
                    sum(event.get("event") == "bb_breaker_open" for event in emitted),
                    "one trip emitted more than one bb_breaker_open event",
                )
        finally:
            release.set()
            for thread in threading.enumerate():
                if thread not in before_threads and thread.name == "bb-subprocess-launch":
                    thread.join(timeout=1)

    def test_real_events_after_trips_on_second_log_timeout(self) -> None:
        calls: list[tuple[str, str]] = []
        emitted: list[dict] = []

        def version_then_log_timeout(argv, _timeout):
            calls.append(tuple(argv[:2]))
            if argv[:2] == ["settings", "version"]:
                return self.version_ok()
            raise BbTransportTimeout("thread log stalled")

        clients = [
            BbClient(version_then_log_timeout, enabled=True),
            BbClient(version_then_log_timeout, enabled=True),
        ]
        patches = self.observation_patches(
            clients, emitted, self.observe_real_events_after
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            self.assertFalse(watch_inbox._observe_bb_session(self.session(), False))
            self.assertEqual(1, watch_inbox._bb_timeout_streaks["bb-session-one"])
            self.assertTrue(
                watch_inbox._observe_bb_session(self.session(), False),
                "failures=gh597_real_events_after_never_opened_breaker",
            )

        self.assertEqual(
            [
                ("settings", "version"),
                ("thread", "log"),
                ("settings", "version"),
                ("thread", "log"),
            ],
            calls,
        )
        self.assertEqual(2, watch_inbox._bb_timeout_streaks["bb-session-one"])
        self.assertEqual(
            1,
            sum(event.get("event") == "bb_breaker_open" for event in emitted),
        )

    def test_inflight_success_after_trip_resets_and_isolated_timeout_does_not_trip(self) -> None:
        success_started = threading.Event()
        release_success = threading.Event()
        emitted: list[dict] = []

        def late_success(argv, _timeout):
            if argv[:2] == ["settings", "version"]:
                return self.version_ok()
            success_started.set()
            release_success.wait()
            return BbTransportResult(0, "[]", "")

        def timeout(_argv, _timeout):
            raise BbTransportTimeout("stalled")

        clients = [
            BbClient(late_success, enabled=True),
            BbClient(timeout, enabled=True),
            BbClient(timeout, enabled=True),
            BbClient(timeout, enabled=True),
            BbClient(timeout, enabled=True),
        ]
        patches = self.observation_patches(
            clients, emitted, self.observe_real_events_after
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            success_result: list[bool] = []
            success_thread = threading.Thread(
                target=lambda: success_result.append(
                    watch_inbox._observe_bb_session(self.session(), False)
                )
            )
            success_thread.start()
            self.assertTrue(success_started.wait(timeout=1))
            self.assertFalse(watch_inbox._observe_bb_session(self.session(), False))
            self.assertTrue(watch_inbox._observe_bb_session(self.session(), False))
            release_success.set()
            success_thread.join(timeout=1)

            self.assertEqual([False], success_result)
            self.assertNotIn("bb-session-one", watch_inbox._bb_timeout_streaks)
            self.assertFalse(watch_inbox._observe_bb_session(self.session(), False))
            self.assertFalse(
                watch_inbox._observe_bb_session(self.session("bb-session-two"), False)
            )
            self.assertEqual(1, watch_inbox._bb_timeout_streaks["bb-session-one"])
            self.assertEqual(1, watch_inbox._bb_timeout_streaks["bb-session-two"])
            self.assertEqual(
                1,
                sum(event.get("event") == "bb_breaker_open" for event in emitted),
            )

    def test_keyboard_interrupt_passes_through_unchanged(self) -> None:
        import llm_collab.bb_client as bb_client

        interrupt = KeyboardInterrupt("operator interrupt")
        entries: list[int] = []

        class InterruptedPopen:
            def __init__(self, *_args, **_kwargs):
                entries.append(threading.get_ident())
                raise interrupt

        transport = subprocess_transport(["fake-bb"])
        emitted: list[dict] = []
        patches = self.observation_patches(
            [BbClient(transport, enabled=True)],
            emitted,
            self.observe_real_events_after,
        )
        try:
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patch.object(
                bb_client.subprocess, "Popen", InterruptedPopen
            ):
                watch_inbox._observe_bb_session(self.session(), False)
        except BaseException as error:
            self.assertIs(interrupt, error)
        else:
            self.fail("KeyboardInterrupt did not pass through the breaker")
        self.assertEqual(1, len(entries))
        self.assertEqual({}, watch_inbox._bb_timeout_streaks)

    def test_open_breaker_leaves_durable_delivery_state_untouched(self) -> None:
        session = self.session()
        watch_inbox._bb_timeout_streaks[session["session_id"]] = 2
        with patch.object(
            watch_inbox, "_bootstrap_bb_before_dispatch", return_value=[]
        ), patch.object(
            watch_inbox, "bb_bootstrap_enabled", return_value=True
        ), patch.object(
            watch_inbox, "config_get", return_value="workspace-one"
        ), patch.object(
            watch_inbox, "autobridge_session_ids", return_value=[session["session_id"]]
        ), patch.object(
            watch_inbox, "load_session", return_value=session
        ), patch.object(
            watch_inbox, "session_has_exact_canonical_binding", return_value=True
        ), patch.object(
            watch_inbox.LedgerStore, "open_writer"
        ) as ledger_write, patch.object(
            watch_inbox, "dispatch_session"
        ) as dispatch, patch.object(
            watch_inbox, "mark_messages_read"
        ) as mark_read, patch.object(watch_inbox, "emit"):
            consumed = watch_inbox.dispatch_autobridge(
                "glmpi", False, project_id="project-one"
            )

        self.assertEqual([], consumed)
        ledger_write.assert_not_called()
        dispatch.assert_not_called()
        mark_read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
