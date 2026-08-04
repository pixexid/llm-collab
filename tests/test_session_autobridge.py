from __future__ import annotations
import sys as _grsys; from pathlib import Path as _grPath
_grsys.path.insert(0, str(_grPath(__file__).resolve().parent)); import _runtime_gate_testkit  # noqa: E402,F401  GH-503: deterministic gate-bypass install (any run form)

import argparse
import json
import os
import base64
import hashlib
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "bin" / "session_autobridge.py"
DELIVER_SCRIPT = REPO_ROOT / "bin" / "deliver.py"
INBOX_SCRIPT = REPO_ROOT / "bin" / "inbox.py"
WATCH_INBOX_SCRIPT = REPO_ROOT / "bin" / "watch_inbox.py"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "bin"))

from types import SimpleNamespace
import _session_autobridge as session_autobridge_lib
import _helpers as helpers_lib
import _activation_cleanup as activation_cleanup_lib
import _activation_lease as activation_lease_lib
import operator_digest as operator_digest_lib
import session_autobridge as session_autobridge_cli
import watch_inbox as watch_inbox_lib
from _helpers import parse_frontmatter
from llm_collab.ledger import LedgerPaths, LedgerStore
import llm_collab.ledger.store as store_module
from llm_collab.session_lifecycle import (
    FakeLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
    TrustedProjectRoot,
)
from llm_collab.codex_runtime_home import bind_runtime_home


SAFE_VERSION = (3, 51, 3)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
    write(path, json.dumps(payload, indent=2))


def write_claude_session_jsonl(
    path: Path, *, cwd: Path, session_id: str | None = None
) -> None:
    """Write one current-Claude per-session .jsonl record (the cwd-bearing format
    current Claude Code writes under ~/.claude/projects/<slug>/). session_id
    defaults to the filename stem; pass a different value to forge a mismatch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session_id if session_id is not None else path.stem,
                "cwd": str(cwd),
                "timestamp": "2026-07-29T00:00:00Z",
            }
        )
        + "\n"
    )


def run_cli_with_eacces_on(root, target_path, module_name, argv):
    """Run a CLI in a child process with os.open raising EACCES for one path.

    `target_path` may be absolute; only its last four components are matched in the child.

    chmod(0o000) does NOT make a file unreadable for UID 0, so a containerized or root test runner
    silently stopped exercising the I/O-failure contract and the test failed instead. Injecting the
    error is identity-independent: it proves the same refusal whoever runs the suite.
    """
    import subprocess as _sp
    import sys as _sys
    import textwrap as _tw

    suffix = "/".join(Path(target_path).parts[-4:])
    program = _tw.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO_ROOT / "bin")!r})
        # Match on a path SUFFIX, not an absolute string: the CLI resolves this binding through its
        # own configured state root, so an absolute comparison silently never fired and the
        # injection did nothing while the test reported the command had simply succeeded.
        denied_suffix = {suffix!r}
        real_open = os.open
        def guarded(path, flags, *a, **k):
            if str(path).endswith(denied_suffix):
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *a, **k)
        os.open = guarded
        real_io_open = open
        def guarded_io(path, *a, **k):
            if str(path).endswith(denied_suffix):
                raise PermissionError(13, "Permission denied")
            return real_io_open(path, *a, **k)
        import builtins
        builtins.open = guarded_io
        sys.argv = {argv!r}
        import {module_name} as cli
        try:
            cli.main()
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
            if error.code not in (0, None):
                print(error.code, file=sys.stderr)
            sys.exit(code if isinstance(code, int) else 1)
    """)
    return _sp.run([_sys.executable, "-c", program], cwd=root, text=True,
                   capture_output=True, input="probe body", timeout=60)


class SessionAutobridgeTest(unittest.TestCase):
    def test_pi_wake_namespace_cannot_collide_with_a_session_id(self):
        self.assertNotEqual(
            session_autobridge_lib.autobridge_event_log_path("X.wake"),
            session_autobridge_lib.autobridge_wake_log_path("X"),
        )

    def make_workspace(self) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix="lca-", dir="/tmp"))
        write(
            temp_root / "sitecustomize.py",
            "\n".join(
                [
                    "import llm_collab.ledger.store as store_module",
                    "store_module._linked_sqlite_version_info = lambda: (3, 51, 3)",
                ]
            ),
        )
        write_json(
            temp_root / "collab.config.json",
            {
                "workspace_name": "test-collab",
                "schema_version": 2,
                "workspace_id": "ws_alpha",
                "projects_root": str(temp_root),
                "project_state_root": str(temp_root / "project-state"),
                "poll_interval_seconds": 15,
                "notifications_enabled": False,
            },
        )
        write_json(
            temp_root / "projects.json",
            {
                "projects": [
                    {
                        "id": "amiga",
                        "display_name": "Amiga",
                        "repos": {"app": "."},
                        "claude_desktop_bridge": True,
                    },
                    {
                        "id": "nuvyr",
                        "display_name": "Nuvyr",
                        "repos": {"app": "."},
                    },
                ]
            },
        )
        return temp_root

    def add_agent(self, root: Path, agent: dict) -> None:
        agents_file = root / "agents.json"
        if agents_file.exists():
            payload = json.loads(agents_file.read_text())
        else:
            payload = {"agents": []}
        payload["agents"].append(agent)
        write_json(agents_file, payload)
        write(root / "agents" / agent["id"] / "identity.md", f"# Identity: {agent['id']}\n")
        write_json(root / "agents" / agent["id"] / "inbox.json", {"agent": agent["id"], "unread": [], "read": []})

    def test_committed_atomic_write_ignores_a_broken_warning_stream(self):
        root = self.make_workspace()
        path = root / "State" / "session.json"
        path.parent.mkdir(parents=True)

        class BrokenStream:
            def write(self, _value):
                raise BrokenPipeError("closed")

        with patch.object(
            session_autobridge_lib.os,
            "fsync",
            side_effect=(None, OSError("directory fsync failed")),
        ), patch.object(session_autobridge_lib.sys, "stderr", BrokenStream()):
            try:
                session_autobridge_lib.write_regular_file_atomically(path, "committed")
            except Exception as error:
                self.fail(f"committed replacement raised: {error}")

        self.assertEqual("committed", path.read_text())

    def test_atomic_write_fsyncs_the_parent_chain(self):
        root = self.make_workspace()
        path = root / "State" / "nested" / "session.json"
        path.parent.mkdir(parents=True)
        opened_directories = []
        original_open = os.open

        def observe_open(candidate, flags, *args):
            descriptor = original_open(candidate, flags, *args)
            candidate_path = Path(candidate)
            if candidate_path.is_dir():
                opened_directories.append(candidate_path)
            return descriptor

        with patch.object(
            session_autobridge_lib.os,
            "open",
            side_effect=observe_open,
        ):
            session_autobridge_lib.write_regular_file_atomically(path, "durable")

        self.assertIn(path.parent, opened_directories)
        self.assertIn(path.parent.parent, opened_directories)

    def test_session_save_compacts_only_histories_already_read_from_the_inbox(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        inbox_path = root / "agents" / "codex" / "inbox.json"
        read_paths = [f"Chats/read/{index:02d}-{'x' * 40}.md" for index in range(40)]
        unread_path = "Chats/unread/keep-authority.md"
        write_json(
            inbox_path,
            {"agent": "codex", "unread": [unread_path], "read": read_paths},
        )
        session = {
            "session_id": "SESSION-COMPACT",
            "agent_id": "codex",
            "processed_messages": [*read_paths, unread_path],
            "canonical_settled_messages": {
                path: {"reason": "accepted"} for path in [*read_paths, unread_path]
            },
        }

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "agent_inbox_path",
            return_value=inbox_path,
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_BYTES",
            1024,
        ):
            try:
                session_autobridge_lib.save_session(session)
            except Exception as error:
                self.fail(f"read-history compaction raised: {error}")
            loaded = session_autobridge_lib.load_session("SESSION-COMPACT")

        self.assertEqual([unread_path], loaded["processed_messages"])
        self.assertEqual(
            {unread_path},
            set(loaded["canonical_settled_messages"]),
        )

    def test_session_save_refuses_to_drop_unread_duplicate_authority(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        session_path = sessions / "SESSION-UNREAD.json"
        inbox_path = root / "agents" / "codex" / "inbox.json"
        unread_paths = [f"Chats/unread/{index:02d}-{'x' * 40}.md" for index in range(40)]
        write_json(
            inbox_path,
            {"agent": "codex", "unread": unread_paths, "read": []},
        )
        write(session_path, "previous")
        session = {
            "session_id": "SESSION-UNREAD",
            "agent_id": "codex",
            "processed_messages": unread_paths,
        }

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "agent_inbox_path",
            return_value=inbox_path,
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_BYTES",
            1024,
        ):
            with self.assertRaisesRegex(ValueError, "session payload exceeds"):
                session_autobridge_lib.save_session(session)

        self.assertEqual("previous", session_path.read_text())
        self.assertEqual(unread_paths, session["processed_messages"])

    def test_session_reader_refuses_state_above_the_writer_limit(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        write_json(
            sessions / "SESSION-OVERSIZED.json",
            {"session_id": "SESSION-OVERSIZED", "padding": "x" * 1024},
        )

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_BYTES",
            256,
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "exceeds the 256 byte limit",
            ):
                session_autobridge_lib.load_session("SESSION-OVERSIZED")

    def test_session_save_refuses_overlapping_inbox_authority(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        inbox_path = root / "agents" / "codex" / "inbox.json"
        shared_path = "Chats/shared/must-remain-authoritative.md"
        write_json(
            inbox_path,
            {"agent": "codex", "unread": [shared_path], "read": [shared_path]},
        )
        session = {
            "session_id": "SESSION-OVERLAP",
            "agent_id": "codex",
            "processed_messages": [shared_path] * 100,
        }

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "agent_inbox_path",
            return_value=inbox_path,
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_BYTES",
            1024,
        ):
            with self.assertRaisesRegex(ValueError, "must not overlap"):
                session_autobridge_lib.save_session(session)

        self.assertFalse((sessions / "SESSION-OVERLAP.json").exists())

    def test_session_scan_propagates_an_oversized_record(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        write_json(
            sessions / "SESSION-OVERSIZED.json",
            {"session_id": "SESSION-OVERSIZED", "padding": "x" * 1024},
        )

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_BYTES",
            256,
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "exceeds the 256 byte limit",
            ):
                session_autobridge_lib.iter_sessions()

    def test_session_scan_counts_entries_before_filtering(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        for index in range(3):
            write(sessions / f"foreign-{index}.tmp", "ignored")

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "MAX_SCANNED_SESSIONS",
            2,
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "exceed the 2 entry limit",
            ):
                session_autobridge_lib.iter_sessions()

    def test_session_scan_has_one_cumulative_byte_budget(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        for index in range(2):
            write_json(
                sessions / f"SESSION-{index}.json",
                {"session_id": f"SESSION-{index}", "padding": "x" * 128},
            )

        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_BYTES",
            1024,
        ), patch.object(
            session_autobridge_lib,
            "MAX_SESSION_SCAN_BYTES",
            256,
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "exceeds the",
            ):
                session_autobridge_lib.iter_sessions()

    def test_session_scan_refuses_invalid_utf8(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / "SESSION-BROKEN.json").write_bytes(
            b'{"session_id":"' + b"\xff" + b'"}'
        )

        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaises(UnicodeDecodeError):
                session_autobridge_lib.iter_sessions()

    def test_session_scan_refuses_an_identity_that_disagrees_with_its_filename(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        write_json(
            sessions / "SESSION-A.json",
            {"session_id": "SESSION-B", "agent_id": "claude"},
        )

        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "identity does not match filename"):
                session_autobridge_lib.iter_sessions()

    def test_registration_checks_capacity_before_publishing_a_binding(self):
        args = argparse.Namespace(
            session="SESSION-LARGE",
            agent="claude",
            project="amiga",
            chat="CHAT-LARGE",
            repo_targets=None,
            mode="notify",
            status="parked",
            wake_strategy="none",
            lease_owner=None,
            ttl_seconds=3600,
            allowed_actions=[],
            runtime_family="claude_app",
            runtime_session_id="runtime-large",
            runtime_session_source="test",
            runtime_home=None,
            runtime_command=None,
            runtime_timeout=30,
            supersedes_session=None,
        )
        with patch.object(
            session_autobridge_cli, "get_agent", return_value={"activation": {}}
        ), patch.object(
            session_autobridge_cli, "load_session", side_effect=FileNotFoundError
        ), patch.object(
            session_autobridge_cli,
            "prepare_session_write",
            side_effect=ValueError("session payload exceeds"),
        ), patch.object(
            session_autobridge_cli, "existing_binding_snapshot_or_refuse"
        ) as binding_read, patch.object(
            session_autobridge_cli, "update_binding_from_session"
        ) as binding_write:
            with self.assertRaisesRegex(ValueError, "session payload exceeds"):
                session_autobridge_cli.register_session(args)
            binding_read.assert_not_called()
            binding_write.assert_not_called()

    # ---- GH-468: a native session may back at most one ACTIVE chat lease ----
    OWNER_FAMILY = "claude_app"

    def _sessions_with_active_native(self, status="active", native="NAT-1",
                                     expires=None, family=OWNER_FAMILY):
        # Self-contained temp sessions dir (no make_workspace side effects).
        sessions = Path(tempfile.mkdtemp(prefix="gh468-", dir="/tmp")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        record = {
            "session_id": "SESSION-A", "agent_id": "claude",
            "project_id": "llm-collab", "chat_id": "CHAT-A",
            "status": status, "runtime": {"family": family, "session_id": native},
        }
        if expires is not None:
            record["lease_expires_utc"] = expires
        write_json(sessions / "SESSION-A.json", record)
        return sessions

    def _guard(self, session_id, project, chat, native, status, family=OWNER_FAMILY):
        # Native identity is (family, id); default the registering family to the
        # owner's so existing scope/status cases keep testing one identity.
        return session_autobridge_cli.refuse_native_session_active_elsewhere(
            session_id, project, chat, native, family, status)

    def _sessions_dir(self, *records):
        # Write arbitrary lease records for resolver tests. iter_sessions(strict)
        # requires the filename stem to equal session_id.
        sessions = Path(tempfile.mkdtemp(prefix="gh468-rnf-", dir="/tmp")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        for rec in records:
            write_json(sessions / f"{rec['session_id']}.json", rec)
        return sessions

    def _rec(self, sid, family, native, status="active", chat="CHAT-A"):
        return {
            "session_id": sid, "agent_id": "claude",
            "project_id": "llm-collab", "chat_id": chat,
            "status": status, "runtime": {"family": family, "session_id": native},
        }

    def test_gh468_resolve_native_family_single_live_returns_it(self):
        sessions = self._sessions_dir(self._rec("S0", "claude_app", "NAT-1"))
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self.assertEqual(
                "claude_app",
                session_autobridge_cli.resolve_native_family("NAT-1"),
            )

    def test_gh468_resolve_native_family_none_when_unowned(self):
        sessions = self._sessions_dir(self._rec("S0", "claude_app", "NAT-1"))
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self.assertIsNone(session_autobridge_cli.resolve_native_family("NAT-OTHER"))

    def test_gh468_resolve_native_family_ignores_stopped_prefers_live(self):
        # A stopped lease no longer owns the native: its family must not be chosen
        # over the LIVE owner's, even though it is written first.
        sessions = self._sessions_dir(
            self._rec("S0", "claude_app", "NAT-1", status="stopped"),
            self._rec("S1", "gemini_cli", "NAT-1", status="active", chat="CHAT-B"),
        )
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self.assertEqual(
                "gemini_cli",
                session_autobridge_cli.resolve_native_family("NAT-1"),
            )

    def _reader_record(self, family=None, status="parked"):
        rec = {
            "session_id": "SESSION-R", "agent_id": "claude",
            "project_id": "llm-collab", "chat_id": "CHAT-A",
            "status": status, "ephemeral_reader": True,
            "runtime": {"session_id": "NAT-1"},
        }
        if family is not None:
            rec["runtime"]["family"] = family
        return rec

    def test_gh468_ephemeral_reader_without_family_is_not_dispatchable(self):
        ok, reason = session_autobridge_lib.session_is_dispatchable(self._reader_record())
        self.assertFalse(ok)
        self.assertEqual("reader_runtime_family_unresolved", reason)

    def test_gh468_ephemeral_reader_legacy_reader_family_is_not_dispatchable(self):
        ok, reason = session_autobridge_lib.session_is_dispatchable(
            self._reader_record(family="reader"))
        self.assertFalse(ok)
        self.assertEqual("reader_runtime_family_unresolved", reason)

    def test_gh468_ephemeral_reader_with_real_family_is_dispatchable(self):
        ok, _ = session_autobridge_lib.session_is_dispatchable(
            self._reader_record(family="claude_app"))
        self.assertTrue(ok)

    def test_gh468_non_reader_parked_is_still_dispatchable(self):
        # The reader rule must not over-reach to ordinary parked leases.
        rec = self._reader_record(family="reader")
        del rec["ephemeral_reader"]
        self.assertTrue(session_autobridge_lib.session_is_dispatchable(rec)[0])

    def test_gh468_unresolved_reader_does_not_block_ordinary_registration(self):
        # A non-dispatchable unresolved reader must neither collide with nor mask a
        # later ordinary registration of the same native in another scope.
        sessions = self._sessions_dir(self._reader_record(family="reader"))
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "active",
                        family="claude_app")
            self.assertIsNone(session_autobridge_cli.resolve_native_family("NAT-1"))

    def test_gh468_reader_first_then_pi_registration_is_refused(self):
        # A JSON activation-reader lease for a Pi native in scope A has no canonical
        # ledger row, so the Pi canonical check cannot see it. The Pi path must run
        # the session-registry ownership scan too: a Pi registration for the same
        # (family, id) in a DIFFERENT scope must refuse and write no second owner.
        root = self.make_workspace()
        self.add_agent(root, {
            "id": "glmpi", "display_name": "Glim",
            "activation": {"type": "cli_session", "watcher_enabled": True},
        })
        sessions_dir = root / "State" / "session_autobridge" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        write_json(sessions_dir / "SESSION-READER-A.json", {
            "session_id": "SESSION-READER-A", "agent_id": "glmpi",
            "project_id": "amiga", "chat_id": "CHAT-A", "status": "parked",
            "ephemeral_reader": True, "lease_expires_utc": "2999-01-01T00:00:00+00:00",
            "runtime": {"family": "pi", "session_id": "pi-native-1"},
        })
        pi_cwd = root / "pi-cwd"; pi_cwd.mkdir()
        done = self._register_pi(
            root, session="SESSION-PI-B", project="amiga", chat="CHAT-B",
            native="pi-native-1", endpoint="endpoint_pi_b",
            runtime_instance="runtime_pi_b", cwd=pi_cwd, home=root / "pi-home",
            repo_target="app", check=False,
        )
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("dispatchable binding", done.stderr + done.stdout)
        self.assertFalse((sessions_dir / "SESSION-PI-B.json").exists(),
                         "a refused Pi registration must not write a second owner")

    def test_gh468_routable_command_session_without_family_is_refused(self):
        # A --runtime-command + --runtime-session-id session is exact-routable even
        # without a family, so it would slip past the (family, id) ownership scan.
        # Refuse it before any write rather than leave an unguarded phantom owner.
        root = self.make_workspace()
        self.add_agent(root, {
            "id": "codex", "display_name": "Codex",
            "activation": {"type": "cli_session", "watcher_enabled": False},
        })
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.run_cli(
                root, "register",
                "--session", "SESSION-CMD", "--agent", "codex",
                "--project", "amiga", "--chat", "CHAT-CMD",
                "--mode", "auto-read", "--wake-strategy", "runtime_trigger",
                "--runtime-session-id", "cmd-native-1",
                "--runtime-session-source", "test_fixture",
                "--runtime-command", json.dumps([sys.executable, "-c", "pass"]),
            )
        out = (cm.exception.stderr or "") + (cm.exception.stdout or "")
        self.assertIn("routable", out)
        self.assertFalse(
            (root / "State" / "session_autobridge" / "sessions" / "SESSION-CMD.json").exists(),
            "a refused routable registration must not write a lease",
        )

    def test_gh468_resolve_native_family_ambiguous_multiple_live_fails_closed(self):
        # Two live different-family leases share one id (the identity model allows
        # it): a reader has no basis to choose and must fail closed.
        sessions = self._sessions_dir(
            self._rec("S0", "claude_app", "NAT-1", status="active", chat="CHAT-A"),
            self._rec("S1", "gemini_cli", "NAT-1", status="active", chat="CHAT-B"),
        )
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaises(session_autobridge_cli.AmbiguousNativeFamily):
                session_autobridge_cli.resolve_native_family("NAT-1")

    def test_gh468_native_session_active_in_another_chat_is_refused(self):
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "already owns a dispatchable binding"):
                self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "active")

    def test_gh468_exact_same_lease_reregistration_is_allowed(self):
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            # In-place re-registration in the same (project, chat) scope is not a
            # cross-routing collision.
            self._guard("SESSION-A", "llm-collab", "CHAT-A", "NAT-1", "active")

    def test_gh468_second_lease_in_same_scope_is_allowed(self):
        # The exclusion unit is the (project, chat) SCOPE, not the lease: within
        # one chat, binding-scoped dispatch disambiguates several leases sharing a
        # native (e.g. a wildcard lease alongside a binding-pinned one), so a
        # DIFFERENT session_id in the SAME scope on the same native is allowed.
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-A", "NAT-1", "active")

    def test_gh468_same_session_move_to_a_different_chat_is_allowed(self):
        # A record is unique per session_id, so re-registering the SAME session_id
        # into another chat MOVES that one record (a rebind/takeover); it is not a
        # second owner. Mirrors test_activation_lease's rebound-owner contract.
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-A", "llm-collab", "CHAT-B", "NAT-1", "active")

    def test_gh468_different_project_same_chat_is_refused(self):
        # Finding 2: the collision key is (project, chat) — the exact key dispatch
        # resolves by. A DIFFERENT session reusing the native under the same
        # chat_id but a different project is a second dispatchable routing target
        # and must refuse.
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "already owns a dispatchable binding"):
                self._guard("SESSION-B", "other-project", "CHAT-A", "NAT-1", "active")

    def test_gh468_different_native_id_is_allowed(self):
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-2", "active")

    def test_gh468_same_native_id_different_family_is_allowed(self):
        # Native identity is (runtime_family, runtime_session_id) — the exact key
        # resolve_exact_dispatch_pair() matches on. A different family reusing the
        # same textual id is a DISTINCT native (dispatch would not route to it), so
        # it must not be refused (id-only comparison would over-block it).
        sessions = self._sessions_with_active_native(family="claude_app")
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "active",
                        family="gemini_cli")

    def test_gh468_reuse_after_other_lease_stopped_is_allowed(self):
        sessions = self._sessions_with_active_native(status="stopped")
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "active")

    def test_gh468_parked_registration_against_dispatchable_owner_is_refused(self):
        # Connector P1: register/publish-current DEFAULT to `parked`, and
        # session_is_dispatchable() routes an unexpired `parked` lease. So a
        # parked NEW registration for a native already held elsewhere must refuse
        # too — guarding only `active` left the common path open.
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "already owns a dispatchable binding"):
                self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "parked")

    def test_gh468_parked_owner_blocks_a_second_parked_lease(self):
        # Connector P1, other-lease side: the EXISTING owner is an unexpired
        # `parked` lease (the default register status), not `active`. Two chats
        # defaulting to parked would both stay dispatchable and route to one
        # native conversation unless the guard counts parked owners.
        sessions = self._sessions_with_active_native(status="parked")
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "already owns a dispatchable binding"):
                self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "parked")

    def test_gh468_reuse_after_other_parked_lease_expired_is_allowed(self):
        # An expired parked owner is not dispatchable (session_is_dispatchable
        # drops it), so it no longer holds the native — reuse is allowed.
        sessions = self._sessions_with_active_native(
            status="parked", expires="2000-01-01T00:00:00+00:00"
        )
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "parked")

    def test_gh468_terminal_status_registration_is_not_guarded(self):
        # A non-dispatchable NEW status (e.g. stopped) creates no routable lease,
        # so it is not subject to the ownership scan.
        sessions = self._sessions_with_active_native()
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "stopped")

    def test_gh468_malformed_lease_fails_the_ownership_scan_closed(self):
        # Finding: the ownership scan is an authority decision, so a malformed
        # (unreadable) lease must fail CLOSED — refuse — not be skipped as absent
        # (it could be the active owner of this native session).
        sessions = Path(tempfile.mkdtemp(prefix="gh468-", dir="/tmp")) / "sessions"
        sessions.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, sessions.parent, ignore_errors=True)
        (sessions / "SESSION-BAD.json").write_text("{not valid json")
        with patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(ValueError, "malformed session record"):
                self._guard("SESSION-B", "llm-collab", "CHAT-B", "NAT-1", "active")

    def test_gh468_write_lock_is_reentrant(self):
        # Finding #2 prerequisite: the register write lock must be reentrant so
        # the ownership scan can be held across save_session (which re-acquires
        # it) without a self-deadlock.
        with session_autobridge_lib._session_write_lock():
            with session_autobridge_lib._session_write_lock():
                pass  # nested acquisition must not deadlock or raise

    def test_gh468_scan_and_write_share_one_lock(self):
        # Finding #2: the ownership scan and the register writes must be one
        # critical section — the guard call and save_session both inside a single
        # `with _session_write_lock()` in the ordinary register branch.
        src = (REPO_ROOT / "bin" / "session_autobridge.py").read_text()
        anchor = "if pi_native_session_id is None:"
        block = src[src.index(anchor):]
        block = block[: block.index("return payload") + len("return payload")]
        lock_at = block.index("with _session_write_lock():")
        guard_at = block.index("refuse_native_session_active_elsewhere(")
        save_at = block.index("save_session(")
        self.assertLess(lock_at, guard_at, "guard must be inside the write lock")
        self.assertLess(guard_at, save_at, "guard must precede the write")
        self.assertLess(lock_at, save_at, "save must be inside the same lock")

    def test_gh468_pi_reader_scan_and_write_share_one_lock(self):
        # The Pi reader scan must be held under ONE continuous write lock through
        # the canonical mint and save_session — releasing the lock after the scan
        # leaves a TOCTOU window where a concurrent reader could publish a second
        # cross-scope lease. Assert no lock re-open between the Pi scan and its save.
        src = (REPO_ROOT / "bin" / "session_autobridge.py").read_text()
        scan_at = src.index("readers_only=True,")
        lock_at = src.rindex("with _session_write_lock():", 0, scan_at)
        save_at = src.index(
            "save_session(payload, prepared=prepared, allow_reactivation=True)", scan_at)
        self.assertLess(lock_at, scan_at)
        self.assertLess(scan_at, save_at)
        between = src[lock_at:save_at]
        self.assertEqual(
            1, between.count("with _session_write_lock():"),
            "the Pi reader scan and save must share ONE continuous lock hold "
            "(no lock release/re-open between the scan and the ownership write)",
        )

    def test_dispatch_refuses_capacity_before_the_runtime_side_effect(self):
        root = self.make_workspace()
        inbox_path = root / "agents" / "gemini" / "inbox.json"
        message_path = "Chats/capacity/packet.md"
        write_json(
            inbox_path,
            {"agent": "gemini", "unread": [message_path], "read": []},
        )
        session = {
            "session_id": "SESSION-CAPACITY",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "gemini_cli", "session_id": "runtime-capacity"},
            "padding": "x" * 256,
        }
        message = {"path": message_path, "frontmatter": {}}
        baseline = {
            **session,
            "updated_utc": "2026-07-28T06:00:00+00:00",
        }
        limit = len(json.dumps(baseline, indent=2, sort_keys=True).encode("utf-8")) + 1

        claim = Mock(return_value=(True, None))
        trigger = Mock(return_value={"returncode": 0})
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib, "claim_message_activation", claim
        ), patch.object(
            session_autobridge_lib, "execute_runtime_trigger", trigger
        ), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_SESSION_BYTES", limit
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-CAPACITY")

        self.assertEqual(1, result["matched_messages"])
        self.assertEqual("session_capacity_refused", result["actions"][0]["reason"])
        claim.assert_not_called()
        trigger.assert_not_called()

    def test_dispatch_capacity_is_cumulative_across_one_poll(self):
        root = self.make_workspace()
        inbox_path = root / "agents" / "gemini" / "inbox.json"
        messages = [
            {"path": "Chats/capacity/first.md", "frontmatter": {}},
            {"path": "Chats/capacity/second.md", "frontmatter": {}},
        ]
        write_json(
            inbox_path,
            {
                "agent": "gemini",
                "unread": [message["path"] for message in messages],
                "read": [],
            },
        )
        session = {
            "session_id": "SESSION-CUMULATIVE",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "gemini_cli", "session_id": "runtime-capacity"},
            "padding": "x" * 128,
        }
        one = {
            **session,
            "processed_messages": [messages[0]["path"]],
            "updated_utc": "2026-07-28T06:00:00+00:00",
        }
        two = {
            **session,
            "processed_messages": [message["path"] for message in messages],
            "updated_utc": "2026-07-28T06:00:00+00:00",
        }
        limit = (
            len(json.dumps(one, indent=2, sort_keys=True).encode("utf-8"))
            + len(json.dumps(two, indent=2, sort_keys=True).encode("utf-8"))
        ) // 2
        claim = Mock(return_value=(True, None))
        trigger = Mock(return_value={"returncode": 0})

        def mark_processed(_session, path, *, prepared=None):
            if prepared is not None:
                _session.clear()
                _session.update(prepared[0])
            else:
                _session.setdefault("processed_messages", []).append(path)

        with self._dispatch_patch_context(session, messages), patch.object(
            session_autobridge_lib, "claim_message_activation", claim
        ), patch.object(
            session_autobridge_lib, "execute_runtime_trigger", trigger
        ), patch.object(
            session_autobridge_lib,
            "mark_message_processed",
            side_effect=mark_processed,
        ), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_SESSION_BYTES", limit
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-CUMULATIVE")

        self.assertEqual(1, trigger.call_count)
        self.assertEqual(1, claim.call_count)
        self.assertEqual("session_capacity_refused", result["actions"][1]["reason"])

    def test_dispatch_reserves_canonical_settlement_before_materialization(self):
        root = self.make_workspace()
        inbox_path = root / "agents" / "gemini" / "inbox.json"
        message = {"path": "Chats/capacity/canonical.md", "frontmatter": {}}
        write_json(
            inbox_path,
            {"agent": "gemini", "unread": [message["path"]], "read": []},
        )
        session = {
            "session_id": "SESSION-CANONICAL",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": "binding-capacity",
            "runtime": {"family": "gemini_cli", "session_id": "runtime-capacity"},
        }
        processed = {
            **session,
            "processed_messages": [message["path"]],
            "updated_utc": "2026-07-28T06:00:00+00:00",
        }
        canonical = {
            **processed,
            "canonical_settled_messages": {
                message["path"]: {"reason": "gate_disabled"}
            },
        }
        limit = (
            len(json.dumps(processed, indent=2, sort_keys=True).encode("utf-8"))
            + len(json.dumps(canonical, indent=2, sort_keys=True).encode("utf-8"))
        ) // 2
        trigger = Mock(return_value={"returncode": 0})
        materialize = Mock()
        claim = Mock(return_value=(True, None))
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib, "execute_runtime_trigger", trigger
        ), patch.object(
            session_autobridge_lib, "claim_message_activation", claim
        ), patch.object(
            session_autobridge_lib,
            "materialize_selected_runtime_packet",
            materialize,
        ), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_SESSION_BYTES", limit
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-CANONICAL")

        self.assertEqual("session_capacity_refused", result["actions"][0]["reason"])
        materialize.assert_not_called()
        trigger.assert_not_called()
        claim.assert_not_called()

    def test_inbox_persistence_uses_the_durable_writer(self):
        with patch.object(helpers_lib, "write_file_durably") as durable:
            helpers_lib.save_agent_inbox("codex", {"agent": "codex", "unread": [], "read": []})

        self.assertEqual(2, durable.call_count)
        pending = json.loads(durable.call_args_list[0].args[1])
        confirmed = json.loads(durable.call_args_list[1].args[1])
        self.assertIs(pending["_durability_pending"], True)
        self.assertNotIn("_durability_pending", confirmed)

    def test_inbox_persistence_refuses_an_unconfirmed_directory_sync(self):
        root = self.make_workspace()
        inbox_path = root / "agents" / "codex" / "inbox.json"
        with patch.object(
            helpers_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            helpers_lib.os,
            "fsync",
            side_effect=(None, OSError("directory fsync failed")),
        ):
            with self.assertRaisesRegex(OSError, "directory fsync failed"):
                helpers_lib.save_agent_inbox(
                    "codex",
                    {"agent": "codex", "unread": [], "read": []},
                )
        self.assertIs(json.loads(inbox_path.read_text())["_durability_pending"], True)

        session = {
            "session_id": "SESSION-INBOX-DURABILITY",
            "agent_id": "codex",
            "processed_messages": ["Chats/durable/read.md"],
        }
        with patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_SESSION_BYTES", 150
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "durability is unconfirmed",
            ):
                session_autobridge_lib.prepare_session_write(session)

        recovered = json.loads(inbox_path.read_text())
        recovered["read"] = ["Chats/durable/read.md"]
        with patch.object(helpers_lib, "agent_inbox_path", return_value=inbox_path):
            helpers_lib.save_agent_inbox("codex", recovered)
        self.assertNotIn("_durability_pending", json.loads(inbox_path.read_text()))
        with patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_SESSION_BYTES", 150
        ):
            candidate, _ = session_autobridge_lib.prepare_session_write(session)
        self.assertEqual([], candidate["processed_messages"])

    def test_dispatch_carries_the_prepared_result_through_the_post_effect_save(self):
        session = {
            "session_id": "SESSION-PINNED",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "gemini_cli", "session_id": "runtime-pinned"},
        }
        message = {"path": "Chats/pinned/packet.md", "frontmatter": {}}
        prepared = (
            {**session, "processed_messages": [message["path"]]},
            "prepared bytes",
        )
        mark = Mock()
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib,
            "reserve_message_result",
            return_value=prepared,
        ), patch.object(
            session_autobridge_lib,
            "mark_message_processed",
            mark,
        ):
            session_autobridge_lib.dispatch_session("SESSION-PINNED")

        mark.assert_called_once_with(
            session,
            message["path"],
            prepared=prepared,
        )

    def test_watcher_and_digest_use_the_bounded_session_iterator(self):
        sessions = [
            {
                "session_id": "SESSION-CLAUDE",
                "agent_id": "claude",
                "project_id": "amiga",
                "status": "parked",
            }
        ]
        with patch.object(
            watch_inbox_lib, "iter_sessions", return_value=sessions
        ) as watcher_iter:
            self.assertEqual(
                ["SESSION-CLAUDE"],
                watch_inbox_lib.autobridge_session_ids("claude", "amiga"),
            )
        watcher_iter.assert_called_once_with(agent_id="claude")

        with patch.object(
            session_autobridge_lib, "iter_sessions", return_value=sessions
        ) as digest_iter, patch.object(
            session_autobridge_lib,
            "session_is_dispatchable",
            return_value=(True, "ok"),
        ):
            live, stale = operator_digest_lib.worker_sessions()

        self.assertEqual(sessions, live)
        self.assertEqual(0, stale)
        digest_iter.assert_called_once_with()

    def test_dispatch_autobridge_isolates_a_failing_session(self):
        # #393: one session raising (e.g. the save_session resurrection guard racing a
        # #378 deactivation) must not abort the rest of the cycle. The failure is
        # emitted as an event and the remaining sessions still dispatch.
        dispatched = []
        events = []

        def fake_dispatch(session_id, **_kwargs):
            dispatched.append(session_id)
            if session_id == "SESSION-RACED":
                raise ValueError("refusing to resurrect stopped session SESSION-RACED")
            return {"actions": [], "repo_scope_refused": []}

        with patch.object(
            watch_inbox_lib, "autobridge_session_ids",
            return_value=["SESSION-RACED", "SESSION-OK"],
        ), patch.object(
            watch_inbox_lib, "load_session", return_value={},
        ), patch.object(
            watch_inbox_lib, "session_has_exact_canonical_binding", return_value=True,
        ), patch.object(
            watch_inbox_lib, "dispatch_session", side_effect=fake_dispatch
        ), patch.object(
            watch_inbox_lib, "emit", side_effect=lambda payload, _json: events.append(payload)
        ):
            watch_inbox_lib.dispatch_autobridge("glmpi", False)

        self.assertEqual(["SESSION-RACED", "SESSION-OK"], dispatched)
        error_events = [e for e in events if e.get("event") == "autobridge_dispatch_error"]
        self.assertEqual(1, len(error_events))
        self.assertEqual("SESSION-RACED", error_events[0]["session_id"])
        self.assertIn("resurrect stopped session", error_events[0]["reason"])

    def test_dispatch_autobridge_gates_on_exact_canonical_binding(self):
        # #95: the watcher resolves each session's exact canonical binding from the
        # ledger store BEFORE dispatch. Two sessions under one agent share one
        # canonical active binding; only the session whose own binding_id /
        # generation match it reaches dispatch_session (materialization / claim /
        # runtime write / mark_messages_read). The stale session fails closed and
        # its packets stay unread. Mutation-proof: removing the gate lets both
        # sessions through.
        exact_session = {
            "session_id": "SESSION-EXACT",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-A",
            "binding_id": "binding-active",
            "binding_generation": 2,
            "runtime": {"family": "claude_app", "session_id": "rt-exact"},
        }
        stale_session = {
            "session_id": "SESSION-STALE",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-A",
            "binding_id": "binding-stale",
            "binding_generation": 1,
            "runtime": {"family": "claude_app", "session_id": "rt-stale"},
        }
        canonical_active = {
            "binding_id": "binding-active",
            "binding_generation": 2,
            "endpoint_id": "endpoint-claude",
        }
        sessions = {"SESSION-EXACT": exact_session, "SESSION-STALE": stale_session}
        dispatched: list[str] = []
        marked_read: list[str] = []
        events: list[dict] = []

        def fake_dispatch(session_id, **_kwargs):
            dispatched.append(session_id)
            return {
                "actions": [
                    {
                        "effective_action": "runtime_trigger",
                        "message_path": f"Chats/packet-{session_id}.md",
                        "runtime_result": {"returncode": 0, "delivery_accepted": True},
                    }
                ],
                "repo_scope_refused": [],
                "matched_messages": 1,
            }

        with patch.object(
            watch_inbox_lib, "autobridge_session_ids",
            return_value=["SESSION-EXACT", "SESSION-STALE"],
        ), patch.object(
            watch_inbox_lib, "load_session",
            side_effect=lambda sid: sessions[sid],
        ), patch.object(
            watch_inbox_lib, "resolve_active_canonical_binding",
            return_value=canonical_active,
        ), patch.object(
            watch_inbox_lib, "dispatch_session", side_effect=fake_dispatch,
        ), patch.object(
            watch_inbox_lib, "mark_messages_read",
            side_effect=lambda _agent, paths: marked_read.extend(paths),
        ), patch.object(
            watch_inbox_lib, "emit",
            side_effect=lambda payload, _json: events.append(payload),
        ):
            watch_inbox_lib.dispatch_autobridge("claude", False)

        # Only the exact session reaches dispatch + mark_messages_read.
        self.assertEqual(["SESSION-EXACT"], dispatched)
        self.assertEqual(["Chats/packet-SESSION-EXACT.md"], marked_read)
        refused = [e for e in events if e.get("event") == "autobridge_binding_refused"]
        self.assertEqual(1, len(refused))
        self.assertEqual("SESSION-STALE", refused[0]["session_id"])
        self.assertEqual("stale_or_foreign_canonical_binding", refused[0]["reason"])

    def test_dispatch_autobridge_fails_closed_when_no_active_binding(self):
        # #95: no resolvable active binding -> fail closed. The session never
        # reaches dispatch_session and nothing is marked read.
        session = {
            "session_id": "SESSION-NONE",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-A",
            "binding_id": "binding-x",
            "binding_generation": 1,
            "runtime": {"family": "claude_app", "session_id": "rt-x"},
        }
        dispatched: list[str] = []
        events: list[dict] = []
        with patch.object(
            watch_inbox_lib, "autobridge_session_ids", return_value=["SESSION-NONE"],
        ), patch.object(
            watch_inbox_lib, "load_session", return_value=session,
        ), patch.object(
            watch_inbox_lib, "resolve_active_canonical_binding", return_value=None,
        ), patch.object(
            watch_inbox_lib, "dispatch_session",
            side_effect=lambda sid, **_: dispatched.append(sid) or {"actions": [], "repo_scope_refused": []},
        ), patch.object(
            watch_inbox_lib, "emit",
            side_effect=lambda payload, _json: events.append(payload),
        ):
            watch_inbox_lib.dispatch_autobridge("claude", False)
        self.assertEqual([], dispatched)
        refused = [e for e in events if e.get("event") == "autobridge_binding_refused"]
        self.assertEqual(1, len(refused))

    def test_bound_session_refuses_generic_null_target_packet(self):
        # #95 frozen invariant half 2: a bound session must refuse a generic
        # (null-target_binding_id) packet; only an exact binding-targeted packet
        # proceeds to materialization/claim. Uses the REAL matcher (NOT stubbed):
        # matching_unread_messages -> message_targets_session ->
        # binding_scoped_message_matches_session. Mutation-proof: neuter the
        # null-target refusal -> the generic packet leaks through -> test fails.
        root = self.make_workspace()
        agent_id = "claude"
        binding_id = "binding-bound"
        runtime_id = "runtime-bound"
        targeted = "Chats/gate/packet-targeted.md"
        generic = "Chats/gate/packet-generic.md"
        inbox_path = root / "agents" / agent_id / "inbox.json"
        write_json(
            inbox_path,
            # generic first: with the null-target refusal removed, the generic
            # packet would match and consume the one-per-poll materialization
            # slot before the targeted packet — making the mutation visible.
            {"agent": agent_id, "unread": [generic, targeted], "read": []},
        )

        def packet_frontmatter(extra: dict) -> str:
            fm = {
                "to": agent_id,
                "project_id": "amiga",
                "chat_id": "CHAT-GATE",
                "target_session_id": runtime_id,
                **extra,
            }
            lines = ["---"]
            for key in sorted(fm):
                lines.append(f"{key}: {fm[key]}")
            lines.append("---")
            lines.append("")
            lines.append("body")
            return "\n".join(lines)

        write(root / targeted, packet_frontmatter({"target_binding_id": binding_id}))
        write(root / generic, packet_frontmatter({}))

        session = {
            "session_id": "SESSION-BOUND",
            "agent_id": agent_id,
            "project_id": "amiga",
            "chat_id": "CHAT-GATE",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": binding_id,
            "binding_generation": 1,
            "runtime": {"family": "claude_app", "session_id": runtime_id},
        }
        materialized: list[str] = []

        with patch.object(session_autobridge_lib, "ROOT", root), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "load_session", return_value=session,
        ), patch.object(
            session_autobridge_lib, "session_is_dispatchable",
            return_value=(True, "ok"),
        ), patch.object(
            session_autobridge_lib, "processed_messages", return_value=set(),
        ), patch.object(
            session_autobridge_lib, "reserve_message_result",
            return_value=({}, "prepared"),
        ), patch.object(
            session_autobridge_lib, "classify_activation",
            return_value=("ok", ""),
        ), patch.object(
            session_autobridge_lib, "claim_message_activation",
            return_value=(True, None),
        ), patch.object(
            session_autobridge_lib, "resolve_effective_action",
            return_value=("runtime_trigger", "test"),
        ), patch.object(
            session_autobridge_lib, "materialize_selected_runtime_packet",
            side_effect=lambda _s, msg: materialized.append(msg["path"]) or {
                "resolved": True, "canonical_write_started": True, "created": True,
            },
        ), patch.object(
            session_autobridge_lib, "mark_canonical_settlement_complete",
        ), patch.object(
            session_autobridge_lib, "execute_runtime_trigger",
            return_value={"returncode": 0},
        ), patch.object(
            session_autobridge_lib, "mark_message_processed",
        ), patch.object(
            session_autobridge_lib, "save_session",
        ), patch.object(
            session_autobridge_lib, "append_event",
        ), patch.object(
            session_autobridge_lib, "write_operator_turn_summary",
            return_value={},
        ), patch.object(
            session_autobridge_lib, "refresh_runtime_ui", return_value={},
        ):
            session_autobridge_lib.dispatch_session("SESSION-BOUND")

        # Only the binding-targeted packet reaches materialization.
        self.assertEqual([targeted], materialized)

    def test_dispatch_inbox_counts_entries_before_project_filtering(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session"},
            },
        )
        for index in range(3):
            self.add_message(
                root,
                agent_id="gemini",
                chat_id="CHAT-FOREIGN",
                project_id="nuvyr",
                title=f"foreign {index}",
                packet_slug=str(index),
            )
        session = {
            "session_id": "SESSION-BOUND",
            "agent_id": "gemini",
            "project_id": "amiga",
        }
        with patch.object(
            session_autobridge_lib, "ROOT", root
        ), patch.object(
            session_autobridge_lib,
            "agent_inbox_path",
            return_value=root / "agents" / "gemini" / "inbox.json",
        ), patch.object(
            session_autobridge_lib, "MAX_DISPATCH_INBOX_ENTRIES", 2
        ):
            with self.assertRaisesRegex(ValueError, "exceeds 2 unread entries"):
                session_autobridge_lib.matching_unread_messages(session)

    def test_dispatch_inbox_has_one_cumulative_byte_budget(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session"},
            },
        )
        paths = [
            self.add_message(
                root,
                agent_id="gemini",
                chat_id="CHAT-BOUND",
                project_id="amiga",
                title=f"packet {index}",
                packet_slug=str(index),
            )
            for index in range(2)
        ]
        inbox_path = root / "agents" / "gemini" / "inbox.json"
        total = inbox_path.stat().st_size + sum((root / path).stat().st_size for path in paths)
        session = {
            "session_id": "SESSION-BOUND",
            "agent_id": "gemini",
            "project_id": "amiga",
            "chat_id": "CHAT-BOUND",
        }
        with patch.object(
            session_autobridge_lib, "ROOT", root
        ), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_DISPATCH_INBOX_BYTES", total - 1
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "exceeds the .* byte limit",
            ):
                session_autobridge_lib.matching_unread_messages(session)

    def test_dispatch_inbox_refuses_missing_malformed_and_oversized_packets(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session"},
            },
        )
        inbox_path = root / "agents" / "gemini" / "inbox.json"
        packet_path = root / "Chats" / "broken.md"
        session = {"session_id": "SESSION-BOUND", "agent_id": "gemini"}
        common = (
            patch.object(session_autobridge_lib, "ROOT", root),
            patch.object(
                session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
            ),
        )

        write_json(
            inbox_path,
            {"agent": "gemini", "unread": ["Chats/missing.md"], "read": []},
        )
        with common[0], common[1]:
            with self.assertRaisesRegex(ValueError, "missing unread packet"):
                session_autobridge_lib.matching_unread_messages(session)

        write(packet_path, "not a packet")
        write_json(
            inbox_path,
            {"agent": "gemini", "unread": ["Chats/broken.md"], "read": []},
        )
        with patch.object(
            session_autobridge_lib, "ROOT", root
        ), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ):
            with self.assertRaisesRegex(ValueError, "malformed unread packet"):
                session_autobridge_lib.matching_unread_messages(session)

        write(packet_path, "---\nproject_id: amiga\n---\n" + "x" * 256)
        with patch.object(
            session_autobridge_lib, "ROOT", root
        ), patch.object(
            session_autobridge_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            session_autobridge_lib, "MAX_DISPATCH_PACKET_BYTES", 64
        ):
            with self.assertRaisesRegex(
                session_autobridge_lib.UnreadableFile,
                "exceeds the 64 byte limit",
            ):
                session_autobridge_lib.matching_unread_messages(session)

    def add_message(
        self,
        root: Path,
        *,
        agent_id: str,
        chat_id: str,
        project_id: str,
        title: str,
        sender_session_id: str | None = None,
        target_session_id: str | None = None,
        sender_agent_id: str | None = None,
        repo_targets: list[str] | None = None,
        target_binding_id: str | None = None,
        target_binding_generation: int | None = None,
        packet_slug: str = "test",
    ) -> str:
        chat_dir = root / "Chats" / f"2026-04-22_autobridge-test__{chat_id}"
        write_json(chat_dir / "meta.json", {"chat_id": chat_id, "project_id": project_id})
        message_rel = f"Chats/{chat_dir.name}/2026-04-22T00-00-00_to-{agent_id}_{packet_slug}.md"
        message_path = root / message_rel
        frontmatter_lines = [
            "---",
            f"chat_id: {chat_id}",
            f"from: {sender_agent_id or 'codex'}",
            f"to: {agent_id}",
            f"title: {title}",
            "priority: normal",
            f"project_id: {project_id}",
            "sent_utc: 2026-04-22T00:00:00+00:00",
        ]
        if sender_session_id:
            frontmatter_lines.append(f"sender_session_id: {sender_session_id}")
        if target_session_id:
            frontmatter_lines.append(f"target_session_id: {target_session_id}")
        if repo_targets is not None:
            frontmatter_lines.append("repo_targets: " + json.dumps(repo_targets))
        if target_binding_id:
            frontmatter_lines.append(f"target_binding_id: {target_binding_id}")
        if target_binding_generation is not None:
            frontmatter_lines.append(f"target_binding_generation: {target_binding_generation}")
        frontmatter_lines.extend(
            [
                "---",
                "",
                "Hello from the test harness.",
            ]
        )
        write(
            message_path,
            "\n".join(frontmatter_lines),
        )
        inbox_path = root / "agents" / agent_id / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(message_rel)
        write_json(inbox_path, inbox)
        return message_rel

    def seed_binding_ledger(
        self,
        root: Path,
        *,
        chat_id: str,
        agent_id: str,
        binding_id: str,
        generation: int,
        endpoint_id: str,
        native_session_id: str,
    ) -> None:
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            writer = LedgerStore.open_writer(paths)
        with writer as store:
            write_gate_key = "canonical" + "_" + "writes"
            store.record_registry_snapshot(
                workspace_id="ws_alpha",
                registry_revision="sha256:" + "a" * 64,
                registry_source_sha256="a" * 64,
                captured_at_utc="2026-04-22T00:00:00+00:00",
                workspace_snapshot_json=json.dumps(
                    {"workspace_id": "ws_alpha", "projects": ["amiga", "nuvyr"]}
                ),
                project_snapshots={
                    "amiga": json.dumps(
                        {"project_id": "amiga", write_gate_key: True}
                    ),
                    "nuvyr": json.dumps({"project_id": "nuvyr"}),
                },
                source_snapshots={"amiga": {}, "nuvyr": {}},
            )
            store._connection.execute(
                """
                INSERT OR IGNORE INTO lifecycle_provider_registry
                (
                    workspace_id, provider_id, provider_revision, trust_class,
                    supported_operations_json, challenge_algorithm,
                    challenge_ttl_seconds, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ws_alpha",
                    "provider_codex",
                    "revision_1",
                    "managed",
                    '["attach"]',
                    "sha256",
                    60,
                    "2026-04-22T00:00:00+00:00",
                ),
            )
            store._connection.execute(
                """
                INSERT OR IGNORE INTO conversation_participants
                (
                    workspace_id, scope_kind, scope_identity, conversation_id,
                    participant_id, agent_id, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ws_alpha",
                    "project",
                    "amiga",
                    chat_id,
                    "participant_" + agent_id,
                    "agent_" + agent_id,
                    "2026-04-22T00:00:00+00:00",
                ),
            )
            store._connection.execute(
                """
                INSERT INTO conversation_bindings
                (
                    workspace_id, scope_kind, scope_identity, conversation_id,
                    participant_id, binding_id, generation, state, mutation_capable,
                    provider_id, provider_revision, endpoint_id, session_ref_id,
                    native_session_id, runtime_instance_id, registered_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ws_alpha",
                    "project",
                    "amiga",
                    chat_id,
                    "participant_" + agent_id,
                    binding_id,
                    generation,
                    "active",
                    1,
                    "provider_codex",
                    "revision_1",
                    endpoint_id,
                    "session_ref_" + binding_id.replace("-", "_"),
                    native_session_id,
                    "runtime_" + binding_id.replace("-", "_"),
                    "2026-04-22T00:00:00+00:00",
                ),
            )

    def run_cli(self, root: Path, *args: str) -> dict:
        return self.run_cli_with_env(root, None, *args)

    def run_cli_with_env(self, root: Path, env: dict[str, str] | None, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args, "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env={**self.subprocess_env(root), **(env or {})},
            check=True,
        )
        return json.loads(result.stdout)

    def subprocess_env(self, root: Path) -> dict[str, str]:
        return {
            **os.environ,
            "LLM_COLLAB_UI_REFRESH": "0",
            "LLM_COLLAB_CANONICAL_CONTROL": "enabled",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(root),
                    str(REPO_ROOT),
                    str(REPO_ROOT / "bin"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        }

    def _dispatch_patch_context(self, session: dict, messages: list[dict]):
        return patch.multiple(
            session_autobridge_lib,
            load_session=Mock(return_value=session),
            session_is_dispatchable=Mock(return_value=(True, "ok")),
            matching_unread_messages=Mock(return_value=messages),
            processed_messages=Mock(
                side_effect=lambda _session: set(session.get("processed_messages", []))
            ),
            message_targets_session=Mock(return_value=(True, "test")),
            claim_message_activation=Mock(return_value=(True, None)),
            should_skip_for_loop_protection=Mock(return_value=(False, "ok")),
            resolve_effective_action=Mock(return_value=("runtime_trigger", "test")),
            append_event=Mock(),
            write_operator_turn_summary=Mock(return_value={}),
            execute_runtime_trigger=Mock(return_value={"returncode": 0}),
            refresh_runtime_ui=Mock(return_value={}),
            mark_message_processed=Mock(),
            save_session=Mock(),
        )

    def test_dispatch_materializes_at_most_one_bound_packet_per_poll_and_defers_the_rest(self):
        session = {
            "session_id": "SESSION-SLOT",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": "binding-slot",
            "binding_generation": 1,
            "runtime": {"session_id": "runtime-slot"},
        }
        messages = [
            {"path": "Chats/slot/first.md", "frontmatter": {}},
            {"path": "Chats/slot/second.md", "frontmatter": {}},
        ]
        with self._dispatch_patch_context(session, messages), patch.object(
            session_autobridge_lib,
            "materialize_selected_runtime_packet",
            side_effect=[
                {"resolved": True, "canonical_write_started": True},
                {"resolved": True, "canonical_write_started": True},
            ],
        ) as materialize:
            result = session_autobridge_lib.dispatch_session("SESSION-SLOT")

        self.assertEqual(2, len(result["actions"]))
        self.assertEqual(1, materialize.call_count)
        self.assertEqual("pull_pending", result["actions"][1]["reason"])
        self.assertTrue(result["actions"][1]["canonical_materialization_result"]["deferred"])

    def test_dispatch_structures_malformed_materialization_refusal_and_continues(self):
        session = {
            "session_id": "SESSION-MALFORMED",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": "binding-malformed",
            "binding_generation": 1,
            "runtime": {"session_id": "runtime-malformed"},
        }
        messages = [
            {"path": "Chats/malformed/bad.md", "frontmatter": {}},
            {"path": "Chats/malformed/good.md", "frontmatter": {}},
        ]
        from llm_collab.canonical import legacy_packet_materialization

        writer = MagicMock()
        writer.__enter__.return_value = object()
        writer.__exit__.return_value = False
        with self._dispatch_patch_context(session, messages), patch.object(
            legacy_packet_materialization,
            "materialize_selected_legacy_packet",
            side_effect=RuntimeError("malformed packet"),
        ) as materialize, patch.object(
            session_autobridge_lib,
            "_repo_package_root",
        ), patch.object(
            session_autobridge_lib,
            "config_get",
            return_value="ws_alpha",
        ), patch.object(
            session_autobridge_lib,
            "project_state_root",
            return_value=Path("/tmp/lca-f5-ledger"),
        ), patch.object(
            LedgerStore,
            "open_writer",
            return_value=writer,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-MALFORMED")

        self.assertEqual(2, len(result["actions"]))
        self.assertEqual(1, materialize.call_count)
        self.assertEqual("route_ambiguous", result["actions"][0]["reason"])
        self.assertEqual("pull_pending", result["actions"][1]["reason"])

    def test_existing_canonical_attempt_stops_automatic_retry(self):
        session = {
            "session_id": "SESSION-AMBIGUOUS",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": "binding-ambiguous",
            "binding_generation": 1,
            "runtime": {"session_id": "runtime-ambiguous"},
        }
        message = {"path": "Chats/ambiguous/packet.md", "frontmatter": {}}
        runtime_trigger = Mock(return_value={"returncode": 0})
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib,
            "materialize_selected_runtime_packet",
            return_value={
                "resolved": True,
                "materialized": True,
                "created": False,
                "canonical_write_started": False,
            },
        ), patch.object(
            session_autobridge_lib,
            "execute_runtime_trigger",
            new=runtime_trigger,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-AMBIGUOUS")

        runtime_trigger.assert_not_called()
        self.assertEqual("pull_pending", result["actions"][0]["reason"])
        self.assertNotIn("runtime_result", result["actions"][0])

    def test_existing_canonical_attempt_does_not_starve_a_new_packet(self):
        session = {
            "session_id": "SESSION-AMBIGUOUS",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": "binding-ambiguous",
            "binding_generation": 1,
            "runtime": {"session_id": "runtime-ambiguous"},
        }
        messages = [
            {"path": "Chats/ambiguous/old.md", "frontmatter": {}},
            {"path": "Chats/ambiguous/new.md", "frontmatter": {}},
        ]
        runtime_trigger = Mock(return_value={"returncode": 0})
        with self._dispatch_patch_context(session, messages), patch.object(
            session_autobridge_lib,
            "materialize_selected_runtime_packet",
            side_effect=[
                {
                    "resolved": True,
                    "materialized": True,
                    "created": False,
                    "canonical_write_started": False,
                },
                {
                    "resolved": True,
                    "materialized": True,
                    "created": True,
                    "canonical_write_started": True,
                },
            ],
        ) as materialize, patch.object(
            session_autobridge_lib,
            "execute_runtime_trigger",
            new=runtime_trigger,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-AMBIGUOUS")

        self.assertEqual(2, materialize.call_count)
        runtime_trigger.assert_called_once_with(session, messages[1])
        self.assertEqual("pull_pending", result["actions"][0]["reason"])
        self.assertIn("runtime_result", result["actions"][1])

    def test_processed_bound_packet_does_not_retrigger_runtime_before_legacy_mark_read(self):
        session = {
            "session_id": "SESSION-PROCESSED",
            "agent_id": "gemini",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "binding_id": "binding-processed",
            "binding_generation": 1,
            "runtime": {"session_id": "runtime-processed"},
        }
        message = {"path": "Chats/processed/packet.md", "frontmatter": {}}
        runtime_trigger = Mock(return_value={"returncode": 0})
        original_mark_message_processed = session_autobridge_lib.mark_message_processed

        def apply_prepared(payload, prepared=None):
            if prepared is not None:
                payload.clear()
                payload.update(prepared[0])

        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib,
            "materialize_selected_runtime_packet",
            return_value={
                "resolved": True,
                "materialized": False,
                "gate": "disabled",
                "canonical_write_started": False,
            },
        ), patch.object(
            session_autobridge_lib,
            "execute_runtime_trigger",
            new=runtime_trigger,
        ), patch.object(
            session_autobridge_lib,
            "mark_message_processed",
            wraps=original_mark_message_processed,
        ), patch.object(
            session_autobridge_lib,
            "save_session",
            side_effect=apply_prepared,
        ):
            first = session_autobridge_lib.dispatch_session("SESSION-PROCESSED")
            second = session_autobridge_lib.dispatch_session("SESSION-PROCESSED")

        self.assertEqual(1, runtime_trigger.call_count)
        self.assertEqual(
            {"reason": "gate_disabled"},
            session["canonical_settled_messages"][message["path"]],
        )
        self.assertEqual("runtime_trigger", first["actions"][0]["effective_action"])
        self.assertEqual("message_already_consumed", second["actions"][0]["event"])
        self.assertTrue(second["actions"][0]["runtime_result"]["skipped"])

    def create_chat(self, root: Path, *, chat_dir_name: str, chat_id: str, project_id: str) -> Path:
        chat_dir = root / "Chats" / chat_dir_name
        write_json(chat_dir / "meta.json", {"chat_id": chat_id, "project_id": project_id})
        return chat_dir

    def test_runtime_trigger_executes_once(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "api-bot",
                "display_name": "API Bot",
                "activation": {"type": "api_trigger", "watcher_enabled": False},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="api-bot",
            chat_id="CHAT-TEST1234",
            project_id="amiga",
            title="Runtime trigger",
            target_session_id="api-trigger-1",
        )
        worker_script = root / "runtime_worker.py"
        output_file = root / "runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps({",
                    "    'session_id': os.environ['LLM_COLLAB_SESSION_ID'],",
                    "    'message_path': os.environ['LLM_COLLAB_MESSAGE_PATH'],",
                    "    'title': payload['message']['title'],",
                    "}, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-RUNTIME",
            "--agent",
            "api-bot",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TEST1234",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "api_trigger",
            "--runtime-session-id",
            "api-trigger-1",
            "--runtime-session-source",
            "test_fixture",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )
        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RUNTIME")

        self.assertTrue(dispatch_result["dispatchable"])
        self.assertEqual(1, len(dispatch_result["actions"]))
        self.assertEqual("runtime_trigger", dispatch_result["actions"][0]["effective_action"])
        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("SESSION-RUNTIME", runtime_payload["session_id"])
        self.assertEqual(message_rel, runtime_payload["message_path"])

        dispatch_again = self.run_cli(root, "dispatch", "--session", "SESSION-RUNTIME")
        self.assertEqual([], dispatch_again["actions"])

    def test_activation_lookup_requires_exact_scope_during_search(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-A-WILDCARD",
            "--agent",
            "codex",
            "--mode",
            "notify",
            "--status",
            "parked",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-Z-EXACT",
            "--agent",
            "codex",
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT",
            "--mode",
            "notify",
            "--status",
            "parked",
        )

        with patch.object(
            session_autobridge_lib,
            "SESSIONS_DIR",
            root / "State" / "session_autobridge" / "sessions",
        ):
            ordinary = session_autobridge_lib.find_dispatchable_target_session(
                agent_id="codex",
                project_id="amiga",
                chat_id="CHAT-EXACT",
                target_session_id=None,
            )
            activation = session_autobridge_lib.find_dispatchable_target_session(
                agent_id="codex",
                project_id="amiga",
                chat_id="CHAT-EXACT",
                target_session_id=None,
                require_exact_scope=True,
            )

        self.assertEqual("SESSION-A-WILDCARD", ordinary["session_id"])
        self.assertEqual("SESSION-Z-EXACT", activation["session_id"])

    def test_activation_lease_is_carried_in_payload_and_resume_prompt(self):
        session = {
            "session_id": "SESSION-ACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-ACT",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "allowed_actions": [],
            "runtime": {"family": "codex_app", "session_id": "runtime-act"},
        }
        message = {
            "path": "Chats/x/packet.md",
            "frontmatter": {
                "from": "claude",
                "to": "codex",
                "title": "Activation",
                "project_id": "amiga",
                "chat_id": "CHAT-ACT",
                "related_task": "TASK-97402D",
            },
            "body": "Do the lane.",
            "activation_lease": {
                "identity": {
                    "project": "amiga",
                    "chat": "CHAT-ACT",
                    "task": "TASK-97402D",
                    "worktree": "/tmp/lane",
                    "branch": "codex/gh-1572-runtime-integration",
                    "target_agent": "codex",
                },
                "lease": {
                    "lease_key": "lease123",
                    "owner_session_id": "SESSION-ACT",
                    "fence_token": 2,
                },
                "owner_session_id": "SESSION-ACT",
                "fence_token": 2,
            },
        }

        payload = session_autobridge_lib.build_runtime_payload(session, message)
        prompt = session_autobridge_lib.build_resume_prompt(session, message)

        self.assertEqual(message["activation_lease"], payload["activation_lease"])
        self.assertIn("activation_fence_token: 2", prompt)
        self.assertIn("Before mutating protected lane state", prompt)
        self.assertIn("Activation packet body:\nDo the lane.", prompt)

    def test_activation_assert_refusal_stops_before_protected_runtime_mutations(self):
        session = {
            "session_id": "SESSION-ACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-ACT",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "codex_app", "session_id": "runtime-act"},
        }
        message = {
            "path": "Chats/x/packet.md",
            "frontmatter": {
                "from": "claude",
                "to": "codex",
                "title": "Activation",
                "project_id": "amiga",
                "chat_id": "CHAT-ACT",
            },
            "body": "Do the lane.",
            "activation_lease": {
                "identity": {
                    "project": "amiga",
                    "chat": "CHAT-ACT",
                    "task": "TASK-97402D",
                    "worktree": "/tmp/lane",
                    "branch": "codex/gh-1572-runtime-integration",
                    "target_agent": "codex",
                },
                "lease": {"lease_key": "lease123"},
                "owner_session_id": "SESSION-ACT",
                "fence_token": 2,
            },
        }
        events: list[dict] = []

        with (
            patch.object(session_autobridge_lib, "load_session", return_value=session),
            patch.object(session_autobridge_lib, "session_is_dispatchable", return_value=(True, "ok")),
            patch.object(session_autobridge_lib, "matching_unread_messages", return_value=[message]),
            patch.object(session_autobridge_lib, "processed_messages", return_value=set()),
            patch.object(session_autobridge_lib, "message_targets_session", return_value=(True, "ok")),
            patch.object(session_autobridge_lib, "claim_message_activation", return_value=(True, {"event": "activation_claimed"})),
            patch.object(session_autobridge_lib, "should_skip_for_loop_protection", return_value=(False, "ok")),
            patch.object(session_autobridge_lib, "resolve_effective_action", return_value=("runtime_trigger", "runtime_command_available")),
            patch.object(
                session_autobridge_lib,
                "activation_fenced_mutation",
                return_value=(
                    False,
                    {
                        "event": "activation_assert_refused",
                        "boundary": "operator_turn_summary",
                        "reason": "stale_fence_token",
                    },
                    None,
                ),
            ),
            patch.object(session_autobridge_lib, "append_event", side_effect=lambda _sid, event: events.append(event)),
            patch.object(session_autobridge_lib, "write_operator_turn_summary") as write_summary,
            patch.object(session_autobridge_lib, "execute_runtime_trigger") as runtime_trigger,
            patch.object(session_autobridge_lib, "mark_message_processed") as mark_processed,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-ACT")

        self.assertEqual(1, len(result["actions"]))
        self.assertEqual("stale_fence_token", result["actions"][0]["reason"])
        write_summary.assert_not_called()
        runtime_trigger.assert_not_called()
        mark_processed.assert_not_called()
        self.assertTrue(any(event.get("event") == "message_dispatched" for event in events))

    def test_malformed_activation_dispatch_never_downgrades_or_marks_processed(self):
        session = {
            "session_id": "SESSION-ACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-ACT",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
        }
        malformed = {
            "path": "Chats/x/malformed.md",
            "frontmatter": {
                "from": "claude",
                "to": "codex",
                "title": "Malformed activation",
                "project_id": "amiga",
                "chat_id": "CHAT-ACT",
                "activation": True,
            },
            "body": "missing identity",
        }
        events: list[dict] = []

        with (
            patch.object(session_autobridge_lib, "load_session", return_value=session),
            patch.object(session_autobridge_lib, "session_is_dispatchable", return_value=(True, "ok")),
            patch.object(session_autobridge_lib, "matching_unread_messages", return_value=[malformed]),
            patch.object(session_autobridge_lib, "processed_messages", return_value=set()),
            patch.object(session_autobridge_lib, "message_targets_session", return_value=(True, "ok")),
            patch.object(session_autobridge_lib, "append_event", side_effect=lambda _sid, event: events.append(event)),
            patch.object(
                session_autobridge_lib,
                "resolve_effective_action",
                return_value=("runtime_trigger", "runtime_session_adapter_available"),
            ) as resolve_action,
            patch.object(session_autobridge_lib, "mark_message_processed") as mark_processed,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-ACT")

        self.assertEqual([], result["actions"])
        self.assertEqual("activation_refused", events[-1]["event"])
        self.assertEqual("malformed_activation", events[-1]["reason"])
        resolve_action.assert_not_called()
        mark_processed.assert_not_called()

    def test_concurrent_activation_loser_remains_unprocessed(self):
        session = {
            "session_id": "SESSION-ACT",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-ACT",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
        }
        message = {
            "path": "Chats/x/packet.md",
            "frontmatter": {
                "from": "claude",
                "to": "codex",
                "title": "Activation",
                "project_id": "amiga",
                "chat_id": "CHAT-ACT",
            },
            "body": "Do the lane.",
        }
        events: list[dict] = []

        with (
            patch.object(session_autobridge_lib, "load_session", return_value=session),
            patch.object(session_autobridge_lib, "session_is_dispatchable", return_value=(True, "ok")),
            patch.object(session_autobridge_lib, "matching_unread_messages", return_value=[message]),
            patch.object(session_autobridge_lib, "processed_messages", return_value=set()),
            patch.object(session_autobridge_lib, "message_targets_session", return_value=(True, "ok")),
            patch.object(
                session_autobridge_lib,
                "claim_message_activation",
                return_value=(
                    False,
                    {
                        "event": "activation_refused",
                        "message_path": "Chats/x/packet.md",
                        "reason": "same_session_different_claimant",
                    },
                ),
            ),
            patch.object(session_autobridge_lib, "append_event", side_effect=lambda _sid, event: events.append(event)),
            patch.object(
                session_autobridge_lib,
                "resolve_effective_action",
                return_value=("runtime_trigger", "runtime_session_adapter_available"),
            ) as resolve_action,
            patch.object(session_autobridge_lib, "mark_message_processed") as mark_processed,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-ACT")

        self.assertEqual([], result["actions"])
        self.assertEqual("same_session_different_claimant", events[-1]["reason"])
        resolve_action.assert_called_once_with(session, message)
        mark_processed.assert_not_called()

    def test_loop_protection_skips_before_activation_claim_and_takeover(self):
        root = self.make_workspace()
        sessions_dir = root / "State" / "session_autobridge" / "sessions"
        leases_dir = root / "projects" / "amiga" / "activation_leases"
        worktree = root / "skip-lane"
        worktree.mkdir()
        owner = {
            "session_id": "SESSION-OWNER",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-SKIP",
            "status": "parked",
            "lease_expires_utc": "2999-01-01T00:00:00+00:00",
        }
        session = {
            "session_id": "SESSION-SKIP",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-SKIP",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "codex_app", "session_id": "runtime-skip"},
        }
        message = {
            "path": "Chats/skip/packet.md",
            "frontmatter": {
                "from": "codex",
                "to": "codex",
                "project_id": "amiga",
                "chat_id": "CHAT-SKIP",
                "activation": True,
                "related_task": "TASK-SKIP",
                "worktree": str(worktree),
                "branch": "codex/skip-lane",
            },
            "body": "Durable thread coordination only.",
        }
        events: list[dict] = []

        write_json(sessions_dir / "SESSION-OWNER.json", owner)
        write_json(sessions_dir / "SESSION-SKIP.json", session)
        identity = activation_lease_lib.lease_identity(
            {
                "project": "amiga",
                "chat": "CHAT-SKIP",
                "task": "TASK-SKIP",
                "worktree": str(worktree),
                "branch": "codex/skip-lane",
                "target_agent": "codex",
            }
        )

        with (
            patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions_dir),
            patch.object(
                activation_lease_lib,
                "project_state_dir",
                lambda _project, _root=root: _root / "State" / "session_autobridge" / "per-project" / _project,
            ),
            patch.object(activation_lease_lib, "get_project", lambda pid: {"id": pid}),
            patch.object(activation_cleanup_lib, "audit_activation_pollers", return_value=[]),
            patch.object(session_autobridge_lib, "load_session", return_value=session),
            patch.object(session_autobridge_lib, "session_is_dispatchable", return_value=(True, "ok")),
            patch.object(session_autobridge_lib, "matching_unread_messages", return_value=[message]),
            patch.object(session_autobridge_lib, "processed_messages", return_value=set()),
            patch.object(session_autobridge_lib, "append_event", side_effect=lambda _sid, event: events.append(event)),
            patch.object(session_autobridge_lib, "mark_message_processed") as mark_processed,
            patch.object(session_autobridge_lib, "save_session"),
        ):
            held_lease = activation_lease_lib.claim_lease(
                identity,
                owner_session_id="SESSION-OWNER",
                claimant_runtime_id="runtime-owner",
            )
            owner["status"] = "stopped"
            write_json(sessions_dir / "SESSION-OWNER.json", owner)
            result = session_autobridge_lib.dispatch_session("SESSION-SKIP")
            remaining_lease = activation_lease_lib.load_lease(identity)

        self.assertEqual([], result["actions"])
        self.assertEqual("SESSION-OWNER", remaining_lease["owner_session_id"])
        self.assertEqual(held_lease["fence_token"], remaining_lease["fence_token"])
        mark_processed.assert_called_once_with(session, message["path"])
        self.assertTrue(
            any(
                event.get("event") == "message_skipped"
                and event.get("reason") == "codex_self_target_thread_coordination"
                for event in events
            )
        )

    def test_runtime_trigger_derives_resume_command_from_registered_session(self):
        fixtures = [
            ("codex_app", "LLM_COLLAB_CODEX_BIN", ["exec", "resume"], ["--json", "--skip-git-repo-check"]),
            ("claude_app", "LLM_COLLAB_CLAUDE_BIN", ["-p", "--output-format", "json", "--resume"], []),
            ("gemini_cli", "LLM_COLLAB_GEMINI_BIN", ["--prompt"], []),
        ]

        for runtime_family, env_var, expected_prefix, expected_suffix in fixtures:
            with self.subTest(runtime_family=runtime_family):
                root = self.make_workspace()
                self.add_agent(
                    root,
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "activation": {"type": "cli_session", "watcher_enabled": True},
                    },
                )
                self.add_message(
                    root,
                    agent_id="codex",
                    chat_id="CHAT-DERIVED123",
                    project_id="amiga",
                    title="Derived runtime wake",
                    sender_session_id="claude-session-2",
                    target_session_id=f"{runtime_family}-session-1",
                    sender_agent_id="claude",
                )

                output_file = root / f"{runtime_family}-runtime-result.json"
                runtime_script = root / f"{runtime_family}-runtime.py"
                write(
                    runtime_script,
                    "\n".join(
                        [
                            "#!/usr/bin/env python3",
                            "import json",
                            "import os",
                            "import sys",
                            "from pathlib import Path",
                            "payload = {",
                            "    'argv': sys.argv[1:],",
                            "    'stdin': sys.stdin.read(),",
                            "    'env': {",
                            "        'session_id': os.environ.get('LLM_COLLAB_SESSION_ID'),",
                            "        'runtime_family': os.environ.get('LLM_COLLAB_RUNTIME_FAMILY'),",
                            "        'runtime_session_id': os.environ.get('LLM_COLLAB_RUNTIME_SESSION_ID'),",
                            "        'runtime_home': os.environ.get('LLM_COLLAB_RUNTIME_HOME'),",
                            "        'codex_home': os.environ.get('CODEX_HOME'),",
                            "        'claude_home': os.environ.get('CLAUDE_HOME'),",
                            "        'gemini_home': os.environ.get('GEMINI_HOME'),",
                            "        'target_session_id': os.environ.get('LLM_COLLAB_TARGET_SESSION_ID'),",
                            "        'sender_session_id': os.environ.get('LLM_COLLAB_SENDER_SESSION_ID'),",
                            "    },",
                            "}",
                            f"Path({json.dumps(str(output_file))}).write_text(json.dumps(payload, indent=2))",
                        ]
                    ),
                )
                runtime_script.chmod(0o755)

                self.run_cli(
                    root,
                    "register",
                    "--session",
                    "SESSION-DERIVED",
                    "--agent",
                    "codex",
                    "--project",
                    "amiga",
                    "--chat",
                    "CHAT-DERIVED123",
                    "--mode",
                    "auto-read",
                    "--wake-strategy",
                    "runtime_trigger",
                    "--runtime-family",
                    runtime_family,
                    "--runtime-session-id",
                    f"{runtime_family}-session-1",
                    "--runtime-session-source",
                    "first_read",
                )

                runtime_home = root / f"{runtime_family}-home"
                runtime_home.mkdir(parents=True, exist_ok=True)
                session_payload = self.run_cli(root, "show", "--session", "SESSION-DERIVED")
                session_payload["runtime"]["home"] = str(runtime_home)
                write_json(
                    root / "State" / "session_autobridge" / "sessions" / "SESSION-DERIVED.json",
                    session_payload,
                )

                dispatch_result = self.run_cli_with_env(
                    root,
                    {env_var: str(runtime_script)},
                    "dispatch",
                    "--session",
                    "SESSION-DERIVED",
                )

                self.assertEqual(1, len(dispatch_result["actions"]))
                action = dispatch_result["actions"][0]
                self.assertEqual("runtime_trigger", action["effective_action"])
                self.assertTrue(action["runtime_result"]["derived_command"])
                self.assertEqual(0, action["runtime_result"]["returncode"])

                runtime_payload = json.loads(output_file.read_text())
                argv = runtime_payload["argv"]
                self.assertEqual(expected_prefix, argv[: len(expected_prefix)])
                if expected_suffix:
                    self.assertEqual(expected_suffix, argv[-len(expected_suffix) :])
                self.assertIn(f"{runtime_family}-session-1", argv)
                if runtime_family == "gemini_cli":
                    resume_index = argv.index("--resume")
                    self.assertEqual(f"{runtime_family}-session-1", argv[resume_index + 1])
                    output_index = argv.index("--output-format")
                    self.assertEqual("json", argv[output_index + 1])
                self.assertEqual("", runtime_payload["stdin"])
                self.assertEqual("SESSION-DERIVED", runtime_payload["env"]["session_id"])
                self.assertEqual(runtime_family, runtime_payload["env"]["runtime_family"])
                self.assertEqual(f"{runtime_family}-session-1", runtime_payload["env"]["runtime_session_id"])
                self.assertEqual(str(runtime_home), runtime_payload["env"]["runtime_home"])
                if runtime_family == "codex_app":
                    self.assertEqual(str(runtime_home), runtime_payload["env"]["codex_home"])
                if runtime_family == "claude_app":
                    self.assertEqual(str(runtime_home), runtime_payload["env"]["claude_home"])
                if runtime_family == "gemini_cli":
                    self.assertEqual(str(runtime_home), runtime_payload["env"]["gemini_home"])
                self.assertEqual(f"{runtime_family}-session-1", runtime_payload["env"]["target_session_id"])
                self.assertEqual("claude-session-2", runtime_payload["env"]["sender_session_id"])

    def test_claude_app_uses_its_mailbox_watcher_not_cli_resume(self):
        session = {
            "session_id": "SESSION-CLAUDE",
            "agent_id": "claude",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {
                "family": "claude_app",
                "session_id": "claude-thread",
            },
        }
        message = {
            "path": "Chats/packet.md",
            "frontmatter": {},
        }

        with patch.object(
            session_autobridge_lib,
            "get_agent",
            return_value={"activation": {"type": "cli_session"}},
        ):
            self.assertEqual(
                ("notify_only", "claude_desktop_mailbox_watcher"),
                session_autobridge_lib.resolve_effective_action(session, message),
            )
        self.assertIsNone(
            session_autobridge_lib.derived_runtime_command(session, message)
        )

    def test_codex_runtime_trigger_prefers_app_server_when_available(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-APPSERVER",
            project_id="amiga",
            title="App server visible refresh",
            target_session_id="codex-thread-appserver",
        )

        request_log: list[dict] = []
        ready = threading.Event()

        def read_exact(conn: socket.socket, count: int) -> bytes:
            chunks: list[bytes] = []
            remaining = count
            while remaining:
                chunk = conn.recv(remaining)
                if not chunk:
                    raise ConnectionError("closed")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def read_frame(conn: socket.socket) -> dict:
            first, second = read_exact(conn, 2)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(read_exact(conn, 2), "big")
            elif length == 127:
                length = int.from_bytes(read_exact(conn, 8), "big")
            mask = read_exact(conn, 4) if second & 0x80 else b""
            payload = read_exact(conn, length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            self.assertEqual(0x1, first & 0x0F)
            return json.loads(payload.decode("utf-8"))

        def write_frame(conn: socket.socket, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            header = bytearray([0x81])
            if len(body) < 126:
                header.append(len(body))
            elif len(body) <= 0xFFFF:
                header.extend([126, (len(body) >> 8) & 0xFF, len(body) & 0xFF])
            else:
                header.append(127)
                header.extend(len(body).to_bytes(8, "big"))
            conn.sendall(bytes(header) + body)

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def serve() -> None:
            ready.set()
            conn, _ = server.accept()
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                headers = request.decode("iso-8859-1")
                key_line = next(line for line in headers.splitlines() if line.lower().startswith("sec-websocket-key:"))
                key = key_line.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                while True:
                    frame = read_frame(conn)
                    method = frame.get("method")
                    request_log.append(frame)
                    if frame.get("id"):
                        if method == "initialize":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"serverInfo": {"name": "fake"}}})
                        elif method == "thread/resume":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"thread": {"id": "codex-thread-appserver"}}})
                        elif method == "model/list":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"data": [{"id": "gpt-test", "isDefault": True}]}})
                        elif method == "turn/start":
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
                            write_frame(conn, {"jsonrpc": "2.0", "method": "turn/started", "params": {"threadId": "codex-thread-appserver", "turn": {"id": "turn-1"}}})
                            write_frame(conn, {"jsonrpc": "2.0", "method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "APP_SERVER_OK"}}})
                            write_frame(conn, {"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}})
                            break
                        else:
                            write_frame(conn, {"jsonrpc": "2.0", "id": frame["id"], "result": {}})
            server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        ready.wait(timeout=2)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-APPSERVER",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-APPSERVER",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-appserver",
            "--runtime-session-source",
            "first_read",
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {"LLM_COLLAB_CODEX_APP_SERVER_URL": f"ws://127.0.0.1:{port}"},
            "dispatch",
            "--session",
            "SESSION-CODEX-APPSERVER",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_app_server", action["runtime_result"]["adapter"])
        self.assertEqual("APP_SERVER_OK", action["runtime_result"]["stdout"])
        self.assertIn("turn/started", action["runtime_result"]["notifications"])
        self.assertIn("turn/completed", action["runtime_result"]["notifications"])
        turn_start = next(frame for frame in request_log if frame.get("method") == "turn/start")
        self.assertEqual("gpt-test", turn_start["params"]["model"])

    def test_codex_app_server_discovery_matches_exact_codex_home(self):
        rows = [
            {
                "pid": 10,
                "command": (
                    "/Applications/Codex.app/Contents/Resources/codex app-server "
                    "--listen ws://127.0.0.1:8765 "
                    "CODEX_HOME=/Users/test/.codex-app-account2"
                ),
            },
            {
                "pid": 11,
                "command": (
                    "/Applications/Codex.app/Contents/Resources/codex app-server "
                    "--listen ws://127.0.0.1:8767 "
                    "--ws-token-file /tmp/main-token "
                    "CODEX_HOME=/Users/test/.codex"
                ),
            },
        ]

        with patch.object(session_autobridge_lib, "codex_app_server_process_rows", return_value=rows):
            result = session_autobridge_lib.discover_codex_app_server("/Users/test/.codex")

        self.assertIsNotNone(result)
        self.assertEqual(11, result["pid"])
        self.assertEqual("ws://127.0.0.1:8767", result["url"])
        self.assertEqual("/tmp/main-token", result["token_file"])

    def test_claude_ui_refresh_stays_disabled_even_when_requested(self):
        session = {
            "session_id": "SESSION-NONCLAUDE-CLAUDE-RUNTIME",
            "agent_id": "other-agent",
            "runtime": {
                "family": "claude_app",
                "session_id": "claude-thread",
            },
        }
        with (
            patch.dict(os.environ, {"LLM_COLLAB_UI_REFRESH": "1"}, clear=False),
            patch.object(session_autobridge_lib, "run_osascript") as run_osascript,
        ):
            result = session_autobridge_lib.refresh_runtime_ui(session)

        self.assertEqual(
            {"skipped": True, "reason": "unsupported_runtime_family=claude_app"},
            result,
        )
        run_osascript.assert_not_called()

    def test_codex_shortcut_refresh_is_reported_unsupported(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-REFRESH",
            project_id="amiga",
            title="Refresh visible Codex UI",
            target_session_id="codex-thread-1",
        )

        worker_script = root / "codex_runtime.py"
        write(
            worker_script,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "import sys",
                    "json.load(sys.stdin)",
                    "print('ok')",
                ]
            ),
        )
        worker_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-REFRESH",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-REFRESH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "shortcut",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-REFRESH",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertTrue(action["ui_refresh_result"]["skipped"])
        self.assertEqual("codex_shortcut_refresh_unsupported", action["ui_refresh_result"]["reason"])

    def test_codex_cdp_refresh_reports_missing_debug_port(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-CDP",
            project_id="amiga",
            title="Refresh visible Codex UI through CDP",
            target_session_id="codex-thread-cdp",
        )

        worker_script = root / "codex_cdp_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-CDP",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-CDP",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-cdp",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "cdp",
                "LLM_COLLAB_CODEX_CDP_PORT": "9",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-CDP",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_cdp_refresh", action["ui_refresh_result"]["method"])
        self.assertEqual(1, action["ui_refresh_result"]["returncode"])
        self.assertIn("remote-debugging-port", action["ui_refresh_result"]["stderr"])

    def test_successful_codex_runtime_trigger_can_reopen_thread_deeplink(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        runtime_session_id = "019dbb4c-ac68-7f10-8332-77ea314a137f"
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-DEEPLINK",
            project_id="amiga",
            title="Refresh visible Codex account UI by deeplink",
            target_session_id=runtime_session_id,
        )

        worker_script = root / "codex_deeplink_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        fake_app_log = root / "fake_codex_deeplink_app.json"
        fake_app = root / "fake_codex_deeplink_app.py"
        write(
            fake_app,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    f"Path({json.dumps(str(fake_app_log))}).write_text(json.dumps({{'CODEX_HOME': os.environ.get('CODEX_HOME'), 'argv': sys.argv[1:]}}, indent=2))",
                ]
            ),
        )
        fake_app.chmod(0o755)

        runtime_home = root / ".codex-app-account2"
        runtime_home.mkdir()
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-DEEPLINK",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-DEEPLINK",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            runtime_session_id,
            "--runtime-session-source",
            str(runtime_home / "session_index.jsonl"),
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "deeplink",
                "LLM_COLLAB_CODEX_APP_BIN": str(fake_app),
                "LLM_COLLAB_CODEX_DEEPLINK_REQUIRE_PROCESS": "0",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-DEEPLINK",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_thread_deeplink", action["ui_refresh_result"]["method"])
        self.assertEqual(0, action["ui_refresh_result"]["returncode"])

        for _ in range(20):
            if fake_app_log.exists():
                break
            __import__("time").sleep(0.1)
        self.assertTrue(fake_app_log.exists())
        fake_payload = json.loads(fake_app_log.read_text())
        self.assertEqual(str(runtime_home), fake_payload["CODEX_HOME"])
        self.assertIn("codex://threads/019dbb4c-ac68-7f10-8332-77ea314a137f", fake_payload["argv"])

    def test_successful_codex_runtime_trigger_can_relaunch_account_ui(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-CODEX-RELAUNCH",
            project_id="amiga",
            title="Relaunch visible Codex account UI",
            target_session_id="codex-thread-relaunch",
        )

        worker_script = root / "codex_relaunch_runtime.py"
        write(worker_script, "#!/usr/bin/env python3\nimport json, sys\njson.load(sys.stdin)\nprint('ok')\n")
        worker_script.chmod(0o755)

        fake_app_log = root / "fake_codex_app.log"
        fake_app = root / "fake_codex_app.py"
        write(
            fake_app,
            "\n".join(
                [
                    "#!/bin/sh",
                    f"printf '%s' \"$CODEX_HOME\" > {json.dumps(str(fake_app_log))}",
                ]
            ),
        )
        fake_app.chmod(0o755)

        runtime_home = root / ".codex-worker"
        runtime_home.mkdir()
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-RELAUNCH",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-RELAUNCH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-thread-relaunch",
            "--runtime-session-source",
            str(runtime_home / "session_index.jsonl"),
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_CODEX_UI_REFRESH_METHOD": "relaunch_account",
                "LLM_COLLAB_CODEX_APP_BIN": str(fake_app),
                "LLM_COLLAB_CODEX_REMOTE_DEBUGGING_PORT": "9224",
            },
            "dispatch",
            "--session",
            "SESSION-CODEX-RELAUNCH",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertEqual("codex_relaunch_account", action["ui_refresh_result"]["method"])
        self.assertEqual(0, action["ui_refresh_result"]["returncode"])
        self.assertIsNone(action["ui_refresh_result"]["terminated_pid"])
        self.assertEqual("9224", action["ui_refresh_result"]["remote_debugging_port"])

        for _ in range(20):
            if fake_app_log.exists():
                break
            __import__("time").sleep(0.1)
        self.assertTrue(fake_app_log.exists())
        self.assertEqual(str(runtime_home), fake_app_log.read_text())

    def test_claude_app_dispatch_leaves_pickup_to_background_watcher(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="claude",
            chat_id="CHAT-CLAUDE-REFRESH",
            project_id="amiga",
            title="Refresh visible Claude UI",
            target_session_id="claude-thread-1",
        )

        worker_log = root / "claude_runtime.log"
        worker_script = root / "claude_runtime.py"
        write(
            worker_script,
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({json.dumps(str(worker_log))}).write_text('called')\n",
        )
        worker_script.chmod(0o755)

        osascript_log = root / "claude_osascript.log"
        osascript_script = root / "fake_claude_osascript.py"
        write(
            osascript_script,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    "from pathlib import Path",
                    f"Path({json.dumps(str(osascript_log))}).write_text(sys.stdin.read())",
                ]
            ),
        )
        osascript_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-REFRESH",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CLAUDE-REFRESH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "claude-thread-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_OSASCRIPT_BIN": str(osascript_script),
            },
            "dispatch",
            "--session",
            "SESSION-CLAUDE-REFRESH",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual("notify_only", action["effective_action"])
        self.assertEqual("claude_desktop_mailbox_watcher", action["reason"])
        self.assertNotIn("runtime_result", action)
        self.assertNotIn("ui_refresh_result", action)
        self.assertFalse(worker_log.exists())
        self.assertFalse(osascript_log.exists())

    def test_claude_dispatch_is_mailbox_only_despite_a_mismatched_runtime_family(self):
        # amiga carries claude_desktop_bridge in the fixture and nuvyr does not, so a
        # project-specific bridge setting cannot be what produces the mailbox-only route.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="claude",
            chat_id="CHAT-CLAUDE-NUVYR",
            project_id="nuvyr",
            title="Nuvyr lane packet",
            target_session_id="claude-thread-nuvyr",
        )

        worker_log = root / "claude_runtime_nuvyr.log"
        worker_script = root / "claude_runtime_nuvyr.py"
        write(
            worker_script,
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            f"Path({json.dumps(str(worker_log))}).write_text('called')\n",
        )
        worker_script.chmod(0o755)

        osascript_log = root / "claude_osascript_nuvyr.log"
        osascript_script = root / "fake_claude_osascript_nuvyr.py"
        write(
            osascript_script,
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys",
                    "from pathlib import Path",
                    f"Path({json.dumps(str(osascript_log))}).write_text(sys.stdin.read())",
                ]
            ),
        )
        osascript_script.chmod(0o755)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-NUVYR",
            "--agent",
            "claude",
            "--project",
            "nuvyr",
            "--chat",
            "CHAT-CLAUDE-NUVYR",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "claude-thread-nuvyr",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        dispatch_result = self.run_cli_with_env(
            root,
            {
                "LLM_COLLAB_UI_REFRESH": "1",
                "LLM_COLLAB_OSASCRIPT_BIN": str(osascript_script),
            },
            "dispatch",
            "--session",
            "SESSION-CLAUDE-NUVYR",
        )

        action = dispatch_result["actions"][0]
        self.assertEqual("notify_only", action["effective_action"])
        self.assertEqual("claude_desktop_mailbox_watcher", action["reason"])
        self.assertNotIn("runtime_result", action)
        self.assertNotIn("ui_refresh_result", action)
        self.assertFalse(worker_log.exists())
        self.assertFalse(osascript_log.exists())

    def test_claude_activation_stays_claimable_despite_a_mismatched_runtime_family(self):
        root = self.make_workspace()
        leases_dir = root / "projects" / "amiga" / "activation_leases"
        worktree = root / "claude-lane"
        worktree.mkdir()
        session = {
            "session_id": "SESSION-CLAUDE-ACTIVATION",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-CLAUDE-ACTIVATION",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "codex_app", "session_id": "claude-thread-1"},
        }
        message = {
            "path": "Chats/claude/activation.md",
            "frontmatter": {
                "from": "codex",
                "to": "claude",
                "project_id": "amiga",
                "chat_id": "CHAT-CLAUDE-ACTIVATION",
                "activation": True,
                "related_task": "TASK-CLAUDE",
                "worktree": str(worktree),
                "branch": "claude/lane",
            },
        }

        sessions_dir = root / "State" / "session_autobridge" / "sessions"
        # Registered live and bound, so the poller's claim would succeed if it tried:
        # the contract under test is that it does not try, not that it would fail.
        write_json(
            sessions_dir / "SESSION-CLAUDE-ACTIVATION.json",
            {**session, "status": "active", "lease_expires_utc": "2999-01-01T00:00:00+00:00"},
        )
        write_json(
            sessions_dir / "SESSION-activation-reader.json",
            {
                "session_id": "SESSION-activation-reader",
                "agent_id": "claude",
                "project_id": "amiga",
                "chat_id": "CHAT-CLAUDE-ACTIVATION",
                "mode": "manual",
                "status": "parked",
                "wake_strategy": "none",
                "lease_expires_utc": "2999-01-01T00:00:00+00:00",
                "runtime": {"family": "reader", "session_id": "claude-thread-1"},
                "ephemeral_reader": True,
            },
        )

        with (
            patch.object(session_autobridge_lib, "SESSIONS_DIR", sessions_dir),
            patch.object(
                activation_lease_lib,
                "project_state_dir",
                lambda _project, _root=root: _root / "State" / "session_autobridge" / "per-project" / _project,
            ),
            patch.object(activation_lease_lib, "get_project", lambda pid: {"id": pid}),
            patch.object(activation_cleanup_lib, "audit_activation_pollers", return_value=[]),
        ):
            allowed, event = session_autobridge_lib.claim_message_activation(session, message)
            self.assertTrue(allowed)

            # The binding assertion: the app watcher picks the packet up under its own
            # reader identity, and a lease the poller took first refuses it
            # (same_session_different_claimant), stranding the packet.
            identity = activation_lease_lib.lease_identity(
                {
                    "project": "amiga",
                    "chat": "CHAT-CLAUDE-ACTIVATION",
                    "task": "TASK-CLAUDE",
                    "worktree": str(worktree),
                    "branch": "claude/lane",
                    "target_agent": "claude",
                }
            )
            try:
                watcher_claim = activation_cleanup_lib.claim_activation_lease(
                    identity,
                    owner_session_id="SESSION-activation-reader",
                    owner_pid=os.getpid(),
                    claimant_runtime_id="claude-thread-1",
                )
            except activation_lease_lib.LeaseRefused as exc:
                self.fail(f"app watcher refused after poller pickup: {exc.reason}")

            self.assertEqual("activation_left_to_watcher", event["event"])
            self.assertEqual("claude_desktop_mailbox_watcher", event["reason"])
            self.assertNotIn("activation_lease", message)

        self.assertEqual("SESSION-activation-reader", watcher_claim["lease"]["owner_session_id"])

    def test_human_relay_downgrades_to_prompt_without_runtime_hook(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {
                    "type": "human_relay",
                    "watcher_enabled": False,
                    "identity_note": "You are CDX2 (cdx2). Read only messages addressed to 'cdx2'.",
                },
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-TEST5678",
            project_id="amiga",
            title="Relay fallback",
        )
        worker_script = root / "human_relay_worker.py"
        output_file = root / "human_relay_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "from pathlib import Path",
                    "import sys",
                    "Path(sys.argv[1]).write_text('should-not-run')",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-RELAY",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TEST5678",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "relay",
        )
        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RELAY")

        self.assertEqual(1, len(dispatch_result["actions"]))
        action = dispatch_result["actions"][0]
        self.assertEqual("relay_prompt", action["effective_action"])
        self.assertFalse(output_file.exists())
        prompt_path = root / action["relay_result"]["prompt_path"]
        self.assertTrue(prompt_path.exists())
        prompt_text = prompt_path.read_text()
        self.assertIn("Please check your inbox now and execute the latest task.", prompt_text)
        self.assertIn("session_autobridge.py", str(SCRIPT_PATH))

    def test_human_relay_uses_runtime_trigger_when_runtime_hook_exists(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {
                    "type": "human_relay",
                    "watcher_enabled": False,
                    "identity_note": "You are CDX2 (cdx2). Read only messages addressed to 'cdx2'.",
                },
            },
        )
        self.add_message(
            root,
            agent_id="cdx2",
            chat_id="CHAT-RELAYRUNTIME",
            project_id="amiga",
            title="Relay runtime hook",
            target_session_id="cdx2-runtime-1",
        )
        worker_script = root / "human_relay_runtime_worker.py"
        output_file = root / "human_relay_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-RELAY-RUNTIME",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-RELAYRUNTIME",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "cdx2-runtime-1",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        dispatch_result = self.run_cli(root, "dispatch", "--session", "SESSION-RELAY-RUNTIME")
        self.assertEqual(1, len(dispatch_result["actions"]))
        action = dispatch_result["actions"][0]
        self.assertEqual("runtime_trigger", action["effective_action"])
        self.assertEqual(0, action["runtime_result"]["returncode"])
        self.assertTrue(output_file.exists())

    def test_explicit_target_session_id_routes_only_to_matching_session(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="codex",
            chat_id="CHAT-TARGET123",
            project_id="amiga",
            title="Targeted wake",
            sender_session_id="claude-session-a",
            target_session_id="codex-runtime-b",
            sender_agent_id="claude",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-A",
            "--agent",
            "codex",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TARGET123",
            "--mode",
            "notify",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-runtime-a",
            "--runtime-session-source",
            "first_read",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-B",
            "--agent",
            "codex",
            "--project",
            "amiga",
            "--chat",
            "CHAT-TARGET123",
            "--mode",
            "notify",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-runtime-b",
            "--runtime-session-source",
            "first_read",
            "--supersedes-session",
            "SESSION-CODEX-A",
        )

        dispatch_a = self.run_cli(root, "dispatch", "--session", "SESSION-CODEX-A")
        dispatch_b = self.run_cli(root, "dispatch", "--session", "SESSION-CODEX-B")

        self.assertEqual([], dispatch_a["actions"])
        self.assertEqual(1, len(dispatch_b["actions"]))
        self.assertEqual("claude-session-a", dispatch_b["actions"][0]["sender_session_id"])
        self.assertEqual("codex-runtime-b", dispatch_b["actions"][0]["target_session_id"])

    def test_deliver_and_inbox_surface_session_protocol_fields(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-22_protocol-test__CHAT-PROTO1",
            chat_id="CHAT-PROTO1",
            project_id="amiga",
        )
        subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PROTO1",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Protocol message",
                "--sender-session-id",
                "codex-app-session-1",
                "--target-session-id",
                "claude-app-session-9",
                "--supersedes-session-id",
                "codex-app-session-0",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Session-aware protocol body.",
            capture_output=True,
            check=True,
        )

        delivered_file = chat_dir / "2026-04-22T00-00-00_to-claude_test.md"
        if not delivered_file.exists():
            delivered_candidates = sorted(chat_dir.glob("*_to-claude_*.md"))
            self.assertTrue(delivered_candidates)
            delivered_file = delivered_candidates[-1]

        delivered_text = delivered_file.read_text()
        self.assertIn("sender_session_id: codex-app-session-1", delivered_text)
        self.assertIn("target_session_id: null", delivered_text)
        self.assertIn("supersedes_session_id: codex-app-session-0", delivered_text)

        inbox_result = subprocess.run(
            [
                sys.executable,
                str(INBOX_SCRIPT),
                "--me",
                "claude",
                "--peek",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Sender Session: codex-app-session-1", inbox_result.stdout)
        self.assertNotIn("Target Session: claude-app-session-9", inbox_result.stdout)
        self.assertIn("Supersedes: codex-app-session-0", inbox_result.stdout)

    def test_discover_runtime_for_codex_remains_read_only_but_inbox_publish_refuses(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="codex",
            chat_id="CHAT-PUBLISH1",
            project_id="amiga",
            title="Publish runtime",
        )

        codex_home = root / ".codex"
        write(codex_home / "session_index.jsonl", json.dumps({
            "id": "codex-thread-123",
            "thread_name": "Autobridge runtime publish",
            "updated_at": "2026-04-22T20:00:00Z",
        }) + "\n")

        discovered = self.run_cli_with_env(
            root,
            {"CODEX_HOME": str(codex_home)},
            "discover-runtime",
            "--runtime-family",
            "codex_app",
        )
        self.assertEqual("codex-thread-123", discovered["session_id"])

        inbox_result = subprocess.run(
            [
                sys.executable,
                str(INBOX_SCRIPT),
                "--me",
                "codex",
                "--peek",
                "--project",
                "amiga",
                "--publish-session",
                "--session",
                "SESSION-CODEX-PUBLISH",
                "--runtime-family",
                "codex_app",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            check=True,
        )
        self.assertIn(
            "[session] publish refused codex_app: heuristic_runtime_discovery_refused",
            inbox_result.stdout,
        )

    def test_publish_current_refuses_heuristic_runtime_discovery_for_all_families(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )

        for runtime_family in ("codex_app", "claude_app", "gemini_cli"):
            with self.subTest(runtime_family=runtime_family):
                result = self.run_cli(
                    root,
                    "publish-current",
                    "--session",
                    f"SESSION-{runtime_family}",
                    "--agent",
                    "codex",
                    "--runtime-family",
                    runtime_family,
                    "--project",
                    "amiga",
                    "--chat",
                    "CHAT-PUBLISH-REFUSE",
                )
                self.assertFalse(result["published"])
                self.assertEqual(
                    session_autobridge_lib.HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
                    result["reason"],
                )

    def test_deliver_autobridge_readiness_does_not_reference_first_match_helper(self):
        source = DELIVER_SCRIPT.read_text()
        self.assertNotIn("find_dispatchable_target_session", source)
        self.assertIn("resolve_exact_dispatch_pair", source)

    def test_load_binding_rejects_malformed_and_non_object_json(self):
        root = self.make_workspace()
        binding_root = root / "State" / "session_autobridge" / "bindings"
        path = binding_root / "amiga" / "CHAT-BAD-BINDING" / "claude.json"

        with patch.object(session_autobridge_lib, "BINDINGS_DIR", binding_root):
            for payload in ("{", "[]"):
                with self.subTest(payload=payload):
                    write(path, payload)
                    with self.assertRaises(FileNotFoundError):
                        session_autobridge_lib.load_binding(
                            "amiga", "CHAT-BAD-BINDING", "claude"
                        )
            path.write_bytes(b"\xff")
            with self.assertRaises(FileNotFoundError):
                session_autobridge_lib.load_binding(
                    "amiga", "CHAT-BAD-BINDING", "claude"
                )

    def test_inbox_publish_refuses_heuristic_runtime_discovery_for_all_families(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_message(
            root,
            agent_id="codex",
            chat_id="CHAT-INBOX-PUBLISH-REFUSE",
            project_id="amiga",
            title="Inbox publish refusal",
        )

        for runtime_family in ("codex_app", "claude_app", "gemini_cli"):
            with self.subTest(runtime_family=runtime_family):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(INBOX_SCRIPT),
                        "--me",
                        "codex",
                        "--peek",
                        "--project",
                        "amiga",
                        "--publish-session",
                        "--session",
                        f"SESSION-INBOX-{runtime_family}",
                        "--runtime-family",
                        runtime_family,
                        "--json",
                    ],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                payload = json.loads(result.stdout)
                self.assertFalse(payload["published_runtime"]["published"])
                self.assertEqual(
                    session_autobridge_lib.HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
                    payload["published_runtime"]["reason"],
                )

    def test_inbox_publish_refusal_is_reported_for_empty_inbox(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )

        json_result = subprocess.run(
            [
                sys.executable,
                str(INBOX_SCRIPT),
                "--me",
                "codex",
                "--peek",
                "--project",
                "amiga",
                "--publish-session",
                "--session",
                "SESSION-INBOX-EMPTY",
                "--runtime-family",
                "codex_app",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(json_result.stdout)
        self.assertEqual([], payload["messages"])
        self.assertFalse(payload["published_runtime"]["published"])
        self.assertEqual(
            session_autobridge_lib.HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
            payload["published_runtime"]["reason"],
        )

        human_result = subprocess.run(
            [
                sys.executable,
                str(INBOX_SCRIPT),
                "--me",
                "codex",
                "--peek",
                "--project",
                "amiga",
                "--publish-session",
                "--session",
                "SESSION-INBOX-EMPTY",
                "--runtime-family",
                "codex_app",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn(
            "[session] publish refused codex_app: heuristic_runtime_discovery_refused",
            human_result.stdout,
        )
        self.assertIn("[inbox] No unread messages for codex.", human_result.stdout)

    def test_discover_runtime_for_claude_project_session_jsonl(self):
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        project_slug = str(project_path.resolve()).replace("/", "-")
        write_claude_session_jsonl(
            claude_home / "projects" / project_slug / "claude-session-456.jsonl",
            cwd=project_path.resolve(),
        )

        discovered = self.run_cli_with_env(
            root,
            {"CLAUDE_HOME": str(claude_home)},
            "discover-runtime",
            "--runtime-family",
            "claude_app",
            "--project-path",
            str(project_path),
        )
        self.assertEqual("claude-session-456", discovered["session_id"])

    def test_claude_discovery_does_not_fall_back_to_other_projects_legacy_index(self):
        # Regression for the 2026-07-27 defect (#95): current Claude writes
        # per-session .jsonl, not the legacy sessions-index.json, so an unscoped
        # discover-runtime returned another project's stale index entry.
        # Discovery must select the exact project's .jsonl or refuse -- never
        # another project's session.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_a = root / "project-a"
        project_b = root / "project-b"
        project_a.mkdir(parents=True, exist_ok=True)
        project_b.mkdir(parents=True, exist_ok=True)
        slug_a = str(project_a.resolve()).replace("/", "-")
        write_claude_session_jsonl(
            claude_home / "projects" / slug_a / "session-A.jsonl",
            cwd=project_a.resolve(),
        )
        # Project B carries only a legacy sessions-index.json (the old
        # cross-project fallback source).
        slug_b = str(project_b.resolve()).replace("/", "-")
        write_json(
            claude_home / "projects" / slug_b / "sessions-index.json",
            {
                "version": 1,
                "entries": [
                    {
                        "sessionId": "stale-session-B",
                        "projectPath": str(project_b.resolve()),
                        "fileMtime": 1771371735466,
                        "modified": "2026-03-11T18:04:49Z",
                    }
                ],
            },
        )
        # Scoped to A: selects A's .jsonl, never B's legacy entry.
        discovered = self.run_cli_with_env(
            root,
            {"CLAUDE_HOME": str(claude_home)},
            "discover-runtime",
            "--runtime-family",
            "claude_app",
            "--project-path",
            str(project_a),
        )
        self.assertEqual("session-A", discovered["session_id"])
        # Unscoped: refuses rather than returning B's stale session.
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
            )

    def test_claude_discovery_fails_closed_on_zero_and_multiple_sessions(self):
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        project_slug = str(project_path.resolve()).replace("/", "-")
        session_dir = claude_home / "projects" / project_slug
        # Zero sessions -> refuse (no fallback to another project).
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )
        write_claude_session_jsonl(session_dir / "session-1.jsonl", cwd=project_path.resolve())
        write_claude_session_jsonl(session_dir / "session-2.jsonl", cwd=project_path.resolve())
        # Multiple sessions -> refuse (do not guess the newest).
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_discover_runtime_for_gemini_chat_file(self):
        root = self.make_workspace()
        gemini_home = root / ".gemini"
        chat_file = gemini_home / "tmp" / "pixexid" / "chats" / "session-2026-04-22T20-11-test.json"
        write_json(
            chat_file,
            {
                "sessionId": "gemini-session-789",
                "startTime": "2026-04-22T20:11:00Z",
                "lastUpdated": "2026-04-22T20:30:00Z",
            },
        )

        discovered = self.run_cli_with_env(
            root,
            {"GEMINI_HOME": str(gemini_home)},
            "discover-runtime",
            "--runtime-family",
            "gemini_cli",
        )
        self.assertEqual("gemini-session-789", discovered["session_id"])

    def test_claude_discovery_proves_exact_project_by_cwd_not_colliding_slug(self):
        # path.replace("/", "-") is NOT injective: <root>/s/a-b/c and
        # <root>/s/a/b-c collapse to the same slug, so Claude writes both
        # projects' sessions into one shared directory. The slug directory alone
        # is not project proof; each candidate's canonical cwd must match.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_one = root / "s" / "a-b" / "c"
        project_two = root / "s" / "a" / "b-c"
        project_one.mkdir(parents=True, exist_ok=True)
        project_two.mkdir(parents=True, exist_ok=True)
        slug_one = str(project_one.resolve()).replace("/", "-")
        slug_two = str(project_two.resolve()).replace("/", "-")
        self.assertEqual(slug_one, slug_two)  # documents the collision premise
        shared_dir = claude_home / "projects" / slug_one
        write_claude_session_jsonl(
            shared_dir / "session-one.jsonl", cwd=project_one.resolve()
        )
        write_claude_session_jsonl(
            shared_dir / "session-two.jsonl", cwd=project_two.resolve()
        )
        # Each project selects only its own session despite the shared slug dir.
        one = self.run_cli_with_env(
            root,
            {"CLAUDE_HOME": str(claude_home)},
            "discover-runtime",
            "--runtime-family",
            "claude_app",
            "--project-path",
            str(project_one),
        )
        self.assertEqual("session-one", one["session_id"])
        two = self.run_cli_with_env(
            root,
            {"CLAUDE_HOME": str(claude_home)},
            "discover-runtime",
            "--runtime-family",
            "claude_app",
            "--project-path",
            str(project_two),
        )
        self.assertEqual("session-two", two["session_id"])

    def test_claude_discovery_fails_closed_on_filename_session_id_mismatch(self):
        # cwd proves the project, but the artifact identity must be proved by the
        # record sessionId equal to the filename -- a body/filename mismatch is a
        # corruption signal and fails closed rather than selecting the artifact.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        slug = str(project_path.resolve()).replace("/", "-")
        session_dir = claude_home / "projects" / slug
        write_claude_session_jsonl(
            session_dir / "session-one.jsonl",
            cwd=project_path.resolve(),
            session_id="not-the-filename-stem",
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_claude_discovery_directory_budget_counts_non_jsonl_entries(self):
        # Enumeration is bounded at the scandir boundary: every directory entry
        # counts, not just *.jsonl, so a directory padded with non-jsonl siblings
        # fails closed instead of globbing down to a single candidate.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        slug = str(project_path.resolve()).replace("/", "-")
        session_dir = claude_home / "projects" / slug
        session_dir.mkdir(parents=True, exist_ok=True)
        write_claude_session_jsonl(session_dir / "session.jsonl", cwd=project_path.resolve())
        for index in range(session_autobridge_lib.MAX_CLAUDE_CANDIDATES):
            (session_dir / f"noise-{index}.txt").write_text("noise\n")
        # 1 .jsonl + MAX non-jsonl = MAX+1 entries -> fail closed at scandir.
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_claude_discovery_fails_closed_when_a_sibling_artifact_is_unreadable(self):
        # P1: an unreadable candidate may be a second exact session, so discovery
        # must fail closed -- never silently skip it and return the readable one.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        slug = str(project_path.resolve()).replace("/", "-")
        session_dir = claude_home / "projects" / slug
        write_claude_session_jsonl(session_dir / "good.jsonl", cwd=project_path.resolve())
        hidden = session_dir / "hidden.jsonl"
        write_claude_session_jsonl(hidden, cwd=project_path.resolve())
        os.chmod(hidden, 0o000)
        self.addCleanup(os.chmod, hidden, 0o600)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_claude_discovery_requires_cwd_and_session_id_in_the_same_record(self):
        # P1: cwd and sessionId must be asserted by the SAME record. Accumulating
        # them independently across records would let a fabricated pair prove a
        # synthetic identity.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        slug = str(project_path.resolve()).replace("/", "-")
        artifact = claude_home / "projects" / slug / "session-a.jsonl"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps({"type": "user", "sessionId": "session-a"})
            + "\n"
            + json.dumps(
                {
                    "type": "attachment",
                    "sessionId": "session-b",
                    "cwd": str(project_path.resolve()),
                }
            )
            + "\n"
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_claude_discovery_rejects_relative_cwd_from_artifact(self):
        # P1: project evidence must be an ABSOLUTE canonical cwd from the
        # artifact; a relative cwd must never be resolved against the discovery
        # process cwd.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        slug = str(project_path.resolve()).replace("/", "-")
        artifact = claude_home / "projects" / slug / "session.jsonl"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps({"type": "attachment", "sessionId": "session", "cwd": "."})
            + "\n"
        )
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_claude_discovery_fails_closed_on_a_non_regular_jsonl_sibling(self):
        # A .jsonl FIFO/dir/broken-symlink must reach the reader and classify as
        # unprovable (fail closed), not be silently dropped by an is_file()
        # prefilter so a readable sibling looks unique.
        root = self.make_workspace()
        claude_home = root / ".claude"
        project_path = root / "fake-project"
        project_path.mkdir(parents=True, exist_ok=True)
        slug = str(project_path.resolve()).replace("/", "-")
        session_dir = claude_home / "projects" / slug
        write_claude_session_jsonl(session_dir / "good.jsonl", cwd=project_path.resolve())
        # A directory masquerading as a .jsonl artifact.
        os.mkdir(session_dir / "evil.jsonl")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_cli_with_env(
                root,
                {"CLAUDE_HOME": str(claude_home)},
                "discover-runtime",
                "--runtime-family",
                "claude_app",
                "--project-path",
                str(project_path),
            )

    def test_resolve_exact_dispatch_target_refuses_missing_and_stale_bindings(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        with patch.object(
            session_autobridge_lib,
            "BINDINGS_DIR",
            root / "State" / "session_autobridge" / "bindings",
        ), patch.object(
            session_autobridge_lib,
            "SESSIONS_DIR",
            root / "State" / "session_autobridge" / "sessions",
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-EXACT-REFUSE",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON, reason)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-STALE",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-EXACT-REFUSE",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "runtime-old",
            "--runtime-session-source",
            "exact",
        )
        session_path = root / "State" / "session_autobridge" / "sessions" / "SESSION-CLAUDE-STALE.json"
        payload = json.loads(session_path.read_text())
        payload["runtime"]["session_id"] = "runtime-new"
        write_json(session_path, payload)

        with patch.object(
            session_autobridge_lib,
            "BINDINGS_DIR",
            root / "State" / "session_autobridge" / "bindings",
        ), patch.object(
            session_autobridge_lib,
            "SESSIONS_DIR",
            root / "State" / "session_autobridge" / "sessions",
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-EXACT-REFUSE",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON, reason)

    def test_resolve_exact_dispatch_target_refuses_ambiguous_and_stopped_sessions(self):
        duplicate_a = {
            "session_id": "SESSION-DUP",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-EXACT-DUP",
            "status": "parked",
            "runtime": {"family": "claude_app", "session_id": "runtime-dup"},
        }
        duplicate_b = dict(duplicate_a)

        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[duplicate_a, duplicate_b],
        ):
            pair, reason, inactive_binding = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertIsNone(inactive_binding)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_AMBIGUOUS_REASON, reason)

            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-EXACT-DUP",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_AMBIGUOUS_REASON, reason)

        expired_a = {**duplicate_a, "lease_expires_utc": "2000-01-01T00:00:00+00:00"}
        expired_b = dict(expired_a)
        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[expired_a, expired_b],
        ):
            pair, reason, inactive_binding = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertIsNone(inactive_binding)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_AMBIGUOUS_REASON, reason)

        reused_runtime = {
            **expired_a,
            "session_id": "SESSION-OTHER",
            "lease_expires_utc": "2999-01-01T00:00:00+00:00",
        }
        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[expired_a, reused_runtime],
        ):
            pair, reason, inactive_binding = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertIsNone(inactive_binding)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_AMBIGUOUS_REASON, reason)

        superseded_runtime = {
            **reused_runtime,
            "status": "superseded",
        }
        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[expired_a, superseded_runtime],
        ):
            pair, reason, inactive_pair = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertEqual(
                session_autobridge_lib.EXACT_BINDING_NOT_DISPATCHABLE_REASON,
                reason,
            )
            self.assertEqual(expired_a, inactive_pair[0])

        cross_scope_runtime = {
            **reused_runtime,
            "project_id": "nuvyr",
            "chat_id": "CHAT-OTHER",
        }
        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[expired_a, cross_scope_runtime],
        ):
            pair, reason, inactive_pair = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertIsNone(inactive_pair)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_AMBIGUOUS_REASON, reason)

        cross_agent_runtime = {
            **reused_runtime,
            "agent_id": "relay",
        }
        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[expired_a, cross_agent_runtime],
        ):
            pair, reason, inactive_pair = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertIsNone(inactive_pair)
            self.assertEqual(
                session_autobridge_lib.EXACT_BINDING_AMBIGUOUS_REASON,
                reason,
            )

        stopped = dict(duplicate_a)
        stopped["status"] = "stopped"
        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-EXACT-DUP",
                "agent_id": "claude",
                "session_id": "SESSION-DUP",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-dup",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[stopped],
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-EXACT-DUP",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(
                session_autobridge_lib.EXACT_BINDING_NOT_DISPATCHABLE_REASON,
                reason,
            )
            pair, reason, inactive_binding = (
                session_autobridge_lib.resolve_exact_dispatch_pair(
                    "amiga",
                    "CHAT-EXACT-DUP",
                    "claude",
                )
            )
            self.assertIsNone(pair)
            self.assertIsNone(inactive_binding)
            self.assertEqual(
                session_autobridge_lib.EXACT_BINDING_NOT_DISPATCHABLE_REASON,
                reason,
            )

    def test_resolve_exact_dispatch_target_refuses_session_id_drift_with_reused_runtime(self):
        wrong_session = {
            "session_id": "SESSION-B",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-SESSION-DRIFT",
            "status": "parked",
            "runtime": {"family": "claude_app", "session_id": "runtime-reused"},
        }

        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-SESSION-DRIFT",
                "agent_id": "claude",
                "session_id": "SESSION-A",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-reused",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[wrong_session],
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-SESSION-DRIFT",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON, reason)

    def test_resolve_exact_dispatch_target_refuses_scope_drift_on_bound_session(self):
        foreign_scope_session = {
            "session_id": "SESSION-A",
            "agent_id": "claude",
            "project_id": "nuvyr",
            "chat_id": "CHAT-SESSION-DRIFT",
            "status": "parked",
            "runtime": {"family": "claude_app", "session_id": "runtime-bound"},
        }

        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-SESSION-DRIFT",
                "agent_id": "claude",
                "session_id": "SESSION-A",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-bound",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[foreign_scope_session],
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-SESSION-DRIFT",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON, reason)

    def test_resolve_exact_dispatch_target_compares_runtime_id_not_target_ids(self):
        drifted_runtime = {
            "session_id": "SESSION-STABLE",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-RUNTIME-DRIFT",
            "status": "parked",
            "runtime": {"family": "claude_app", "session_id": "runtime-current"},
        }

        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-RUNTIME-DRIFT",
                "agent_id": "claude",
                "session_id": "SESSION-STABLE",
                "runtime_family": "claude_app",
                "runtime_session_id": "SESSION-STABLE",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[drifted_runtime],
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-RUNTIME-DRIFT",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON, reason)

    def test_resolve_exact_dispatch_target_requires_runtime_family_match(self):
        family_drift = {
            "session_id": "SESSION-FAMILY",
            "agent_id": "claude",
            "project_id": "amiga",
            "chat_id": "CHAT-FAMILY-DRIFT",
            "status": "parked",
            "runtime": {"family": "gemini_cli", "session_id": "runtime-shared"},
        }

        with patch.object(
            session_autobridge_lib,
            "load_binding",
            return_value={
                "project_id": "amiga",
                "chat_id": "CHAT-FAMILY-DRIFT",
                "agent_id": "claude",
                "session_id": "SESSION-FAMILY",
                "runtime_family": "claude_app",
                "runtime_session_id": "runtime-shared",
            },
        ), patch.object(
            session_autobridge_lib,
            "iter_sessions",
            return_value=[family_drift],
        ):
            session, reason = session_autobridge_lib.resolve_exact_dispatch_target(
                "amiga",
                "CHAT-FAMILY-DRIFT",
                "claude",
            )
            self.assertIsNone(session)
            self.assertEqual(session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON, reason)

    def test_deliver_treats_malformed_binding_as_exact_refusal_and_writes_packet(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-23_malformed-binding__CHAT-BIND-MALFORMED",
            chat_id="CHAT-BIND-MALFORMED",
            project_id="amiga",
        )
        binding_path = (
            root
            / "State"
            / "session_autobridge"
            / "bindings"
            / "amiga"
            / "CHAT-BIND-MALFORMED"
            / "claude.json"
        )
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding_path.write_bytes(b"\xff")

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-BIND-MALFORMED",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Malformed binding still delivers",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Write the durable packet even when exact runtime binding is unreadable.",
            capture_output=True,
            check=True,
        )
        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["autobridge_ready"])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON,
            result_payload["autobridge_refusal_reason"],
        )
        self.assertIsNone(result_payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-claude_*.md"))
        self.assertTrue(delivered_candidates)
        frontmatter, _ = parse_frontmatter(delivered_candidates[-1].read_text())
        self.assertIsNone(frontmatter["target_session_id"])

    def test_expired_lease_does_not_write_permanently_unroutable_packet(self):
        for project, chat_id in (
            ("amiga", "CHAT-EXP-AMIGA"),
            ("nuvyr", "CHAT-EXP-NUVYR"),
        ):
            with self.subTest(project=project):
                root = self.make_workspace()
                for agent in ("codex", "relay"):
                    self.add_agent(
                        root,
                        {
                            "id": agent,
                            "display_name": agent.title(),
                            "activation": {
                                "type": "cli_session",
                                "watcher_enabled": True,
                            },
                        },
                    )
                chat_dir = self.create_chat(
                    root,
                    chat_dir_name=f"2026-07-27_expired-lease__{chat_id}",
                    chat_id=chat_id,
                    project_id=project,
                )
                runtime_id = f"relay-runtime-expired-{project}"
                session_id = f"SESSION-RELAY-EXP-{project.upper()}"
                worker_script = root / "record_dispatch.py"
                dispatch_output = root / "dispatch.json"
                write(
                    worker_script,
                    "\n".join(
                        [
                            "import json, sys",
                            "from pathlib import Path",
                            "Path(sys.argv[1]).write_text(json.dumps(json.load(sys.stdin)))",
                        ]
                    ),
                )
                self.run_cli(
                    root,
                    "register",
                    "--session", session_id,
                    "--agent", "relay",
                    "--project", project,
                    "--chat", chat_id,
                    "--mode", "auto-read",
                    "--wake-strategy", "runtime_trigger",
                    "--runtime-family", "codex_app",
                    "--runtime-session-id", runtime_id,
                    "--runtime-session-source", "first_read",
                    "--runtime-command",
                    json.dumps([sys.executable, str(worker_script), str(dispatch_output)]),
                )
                session_path = (
                    root / "State" / "session_autobridge" / "sessions"
                    / f"{session_id}.json"
                )
                session = json.loads(session_path.read_text())
                session["lease_expires_utc"] = "2000-01-01T00:00:00+00:00"
                write_json(session_path, session)

                result = subprocess.run(
                    [
                        sys.executable, str(DELIVER_SCRIPT),
                        "--chat", chat_id,
                        "--from", "codex",
                        "--to", "relay",
                        "--project", project,
                        "--title", "Strand guard",
                        "--sender-session-id", "codex-session-1",
                        "--body-file", "-",
                    ],
                    cwd=root,
                    text=True,
                    input="Body for the expired-lease packet.",
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                payload = json.loads(result.stdout.split("\n\n", 1)[0])
                self.assertFalse(payload["autobridge_ready"])
                self.assertEqual(
                    session_autobridge_lib.EXACT_BINDING_NOT_DISPATCHABLE_REASON,
                    payload["autobridge_refusal_reason"],
                )
                self.assertEqual(runtime_id, payload["resolved_target_session_id"])

                packet = sorted(chat_dir.glob("*_to-relay_*.md"))[-1]
                frontmatter, _ = parse_frontmatter(packet.read_text())
                self.assertEqual(runtime_id, frontmatter["target_session_id"])

                session["lease_expires_utc"] = "2999-01-01T00:00:00+00:00"
                session["status"] = "active"
                write_json(session_path, session)
                dispatch = self.run_cli(root, "dispatch", "--session", session_id)
                packet_rel = str(packet.relative_to(root))
                self.assertEqual(1, dispatch["matched_messages"])
                self.assertEqual(packet_rel, dispatch["actions"][0]["message_path"])
                self.assertIn("runtime_result", dispatch["actions"][0], dispatch)
                self.assertEqual(0, dispatch["actions"][0]["runtime_result"]["returncode"])
                self.assertEqual(packet_rel, json.loads(dispatch_output.read_text())["message"]["path"])
                renewed_session = json.loads(session_path.read_text())
                self.assertIn(packet_rel, renewed_session["processed_messages"])

                session["lease_expires_utc"] = "2000-01-01T00:00:00+00:00"
                session["repo_targets"] = ["app"]
                write_json(session_path, session)
                refused = subprocess.run(
                    [
                        sys.executable, str(DELIVER_SCRIPT),
                        "--chat", chat_id,
                        "--from", "codex",
                        "--to", "relay",
                        "--project", project,
                        "--title", "Expired scope refusal",
                        "--sender-session-id", "codex-session-1",
                        "--body-file", "-",
                    ],
                    cwd=root,
                    text=True,
                    input="This packet cannot satisfy the registered repo scope.",
                    capture_output=True,
                    check=True,
                )
                refused_payload = json.loads(refused.stdout.split("\n\n", 1)[0])
                self.assertFalse(refused_payload["autobridge_ready"])
                self.assertEqual(
                    session_autobridge_lib.ROUTE_AMBIGUOUS_REASON,
                    refused_payload["autobridge_refusal_reason"],
                )
                self.assertIsNone(refused_payload["resolved_target_session_id"])
                self.assertEqual(
                    session_id,
                    refused_payload["autobridge_session_id"],
                )
                self.assertIn(
                    "subscriber repo_targets: ['app']",
                    refused.stderr,
                )
                self.assertFalse(refused_payload["ax_doorbell_required"])
                self.assertFalse(refused_payload["ax_attended_recovery_required"])
                self.assertFalse(refused_payload["operator_relay_required"])
                self.assertFalse(refused_payload["activation_unavailable"])
                refused_frontmatter = next(
                    frontmatter
                    for candidate in chat_dir.glob("*_to-relay_*.md")
                    for frontmatter, _ in [parse_frontmatter(candidate.read_text())]
                    if frontmatter.get("title") == "Expired scope refusal"
                )
                self.assertIsNone(refused_frontmatter["target_session_id"])

    def test_mismatched_binding_never_supplies_a_durable_target(self):
        root = self.make_workspace()
        for agent in ("codex", "claude"):
            self.add_agent(
                root,
                {
                    "id": agent,
                    "display_name": agent.title(),
                    "activation": {"type": "cli_session", "watcher_enabled": True},
                },
            )
        chat_id = "CHAT-BINDING-DRIFT"
        chat_dir = self.create_chat(
            root,
            chat_dir_name=f"2026-07-27_binding-drift__{chat_id}",
            chat_id=chat_id,
            project_id="amiga",
        )
        self.run_cli(
            root,
            "register",
            "--session", "SESSION-CLAUDE-DRIFT",
            "--agent", "claude",
            "--project", "amiga",
            "--chat", chat_id,
            "--mode", "auto-read",
            "--runtime-family", "claude_app",
            "--runtime-session-id", "foreign-runtime",
            "--runtime-session-source", "first_read",
        )
        session_path = (
            root / "State" / "session_autobridge" / "sessions"
            / "SESSION-CLAUDE-DRIFT.json"
        )
        session = json.loads(session_path.read_text())
        session["runtime"]["session_id"] = "current-runtime"
        write_json(session_path, session)

        result = subprocess.run(
            [
                sys.executable, str(DELIVER_SCRIPT),
                "--chat", chat_id,
                "--from", "codex",
                "--to", "claude",
                "--project", "amiga",
                "--title", "Binding drift",
                "--sender-session-id", "codex-session-1",
                "--body-file", "-",
            ],
            cwd=root,
            text=True,
            input="Do not target the rejected binding.",
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout.split("\n\n", 1)[0])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON,
            payload["autobridge_refusal_reason"],
        )
        self.assertIsNone(payload["resolved_target_session_id"])
        packet = sorted(chat_dir.glob("*_to-claude_*.md"))[-1]
        frontmatter, _ = parse_frontmatter(packet.read_text())
        self.assertIsNone(frontmatter["target_session_id"])

    def deliver_with_scope(self, root, chat_id, *, repo_targets=None, project="amiga",
                           recipient="claude"):
        """Run deliver.py and return its JSON payload plus stderr."""
        argv = [
            sys.executable, str(DELIVER_SCRIPT),
            "--chat", chat_id, "--from", "codex", "--to", recipient,
            "--project", project, "--title", "Scope preflight probe",
            "--sender-session-id", "codex-session-9", "--body-file", "-",
        ]
        if repo_targets is not None:
            argv += ["--repo-targets", repo_targets]
        # check=False plus an explicit assertion: check=True raises CalledProcessError with the
        # stderr hidden inside it, so a failing delivery reported only "exit status 1" and I had to
        # go digging. The command's own error message is the diagnostic.
        done = subprocess.run(argv, cwd=root, text=True, input="scope probe",
                              capture_output=True, check=False)
        if done.returncode != 0:
            self.fail(f"deliver.py exited {done.returncode}\n"
                      f"stdout:\n{done.stdout[-1500:]}\nstderr:\n{done.stderr[-1500:]}")
        return json.loads(done.stdout.split("\n\n", 1)[0]), done.stderr

    def scoped_subscriber_workspace(self, *, subscriber_repo_targets, subscriber_ax_app=None,
                                    subscriber_agent="claude", runtime_family="claude_app",
                                    register_session=True,
                                    project="amiga", chat_id="CHAT-SCOPE1"):
        """A bound session in `chat_id`/`project`, optionally declaring a repo scope.

        Parameterized by project because this patch changes the SHARED deliver.py routing contract,
        and AGENTS.md:43-44 requires focused coverage for Amiga and at least one non-Amiga project.
        Hard-coding amiga in the helper meant every case could only ever exercise one project, so a
        project-specific behaviour leaking into the universal path would have been invisible.

        Parameterized by subscriber so scope refusal is checked independently of one
        canonical worker identity.
        """
        root = self.make_workspace()
        for agent in ("codex", subscriber_agent):
            activation = {"type": "cli_session", "watcher_enabled": True}
            if agent == subscriber_agent and subscriber_ax_app:
                # Malformed legacy entries must not override recipient identity.
                activation["ax_app"] = subscriber_ax_app
            self.add_agent(root, {
                "id": agent,
                "display_name": agent.title(),
                "activation": activation,
            })
        chat_dir = self.create_chat(
            root,
            chat_dir_name=f"2026-07-25_scope-preflight__{chat_id}",
            chat_id=chat_id,
            project_id=project,
        )
        register = [
            "register",
            "--session", f"SESSION-{subscriber_agent.upper()}-SCOPED-{project.upper()}",
            "--agent", subscriber_agent,
            "--project", project, "--chat", chat_id, "--mode", "notify",
            "--runtime-family", runtime_family,
            "--runtime-session-id", f"{subscriber_agent}-scoped-session",
            "--runtime-session-source", "first_read",
        ]
        for target in subscriber_repo_targets or []:
            register += ["--repo-target", target]
        if register_session:
            self.run_cli(root, *register)
        return root, chat_dir

    # --- addressing survives undispatchability (GH-324) ------------------------------------
    #
    # An undispatchable session must still be ADDRESSABLE. #340 made deliver.py fall back to the
    # inactive pair so the packet keeps its exact target while the wake is withheld; nothing
    # pinned it, and losing it is not a late packet but a permanently unroutable one — re-
    # registering the session afterwards cannot rescue a packet already written with a null
    # target. On 2026-07-28 that shape cost three packets, from a checkout predating #340.

    def expire_session_lease(self, root, *, status="parked"):
        """Force the registered session past its TTL, as a real long-lived lane does."""
        sessions_dir = root / "State" / "session_autobridge" / "sessions"
        record_path = sorted(sessions_dir.glob("*.json"))[0]
        record = json.loads(record_path.read_text())
        record["status"] = status
        record["lease_expires_utc"] = "2020-01-01T00:00:00+00:00"
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True))
        return record

    def test_an_undispatchable_session_still_gets_its_exact_address(self):
        """The wake may be withheld; the address may not be dropped."""
        root, chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"])
        record = self.expire_session_lease(root)
        expected = record["runtime"]["session_id"]

        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1", repo_targets="llm-collab")

        self.assertFalse(payload["autobridge_ready"], "an expired parked claim must not wake")
        self.assertEqual(expected, payload["resolved_target_session_id"])
        packet = sorted(chat_dir.glob("*_to-claude_*.md"))[-1]
        frontmatter, _ = parse_frontmatter(packet.read_text())
        self.assertEqual(
            expected, frontmatter["target_session_id"],
            "a packet written without its address can never be routed by any later fix")

    def test_an_active_session_past_its_ttl_both_addresses_and_wakes(self):
        """The other half of GH-324: an active session's validity follows its native
        task, so the TTL alone must not withhold the wake either."""
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"])
        record = self.expire_session_lease(root, status="active")

        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1", repo_targets="llm-collab")

        self.assertEqual(record["runtime"]["session_id"], payload["resolved_target_session_id"])
        self.assertTrue(payload["autobridge_ready"],
                        "a live session was refused on a clock, which is the GH-324 defect")

    # --- deliver.py must apply the SAME routing contract the watcher will apply -------------
    #
    # 27 packets were written, reported autobridge_ready: true, and never dispatched, because
    # deliver.py resolved the target session without ever running repo_scope_matches against it.
    # The three cases below are the contract, not a new rule: fail-closed for a scoped subscriber
    # with an empty packet scope, accept a declared subset, and leave unscoped acceptance alone.

    # --- a scope refusal is TERMINAL: no lane may wake the recipient ------------------------
    #
    # Every wake lane was gated on `not autobridge_ready`, which means "autobridge cannot take it,
    # try another way to wake them". Setting that flag for a scope refusal therefore turned a silent
    # drop into a WRONG WAKE: the refused packet raised ax_doorbell_required with a prompt telling
    # the recipient to go read it. The scope contract says this packet does not reach this
    # subscriber by ANY lane.

    WAKE_FLAGS = ("ax_doorbell_required", "ax_attended_recovery_required",
                  "desktop_bridge_required", "operator_relay_required")
    WAKE_PROMPTS = ("ax_doorbell_prompt", "ax_attended_recovery_prompt", "desktop_bridge_prompt")

    def test_the_scope_fixture_uses_its_watcher_when_there_is_no_session(self):
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Codex",
            subscriber_agent="relay", runtime_family="codex_app",
            register_session=False)
        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1", recipient="relay")
        self.assertFalse(payload["autobridge_ready"])
        self.assertTrue(payload["watcher_pickup_ready"])
        self.assertFalse(payload["activation_unavailable"])
        self.assertFalse(payload["ax_doorbell_required"])

    def test_a_scope_refusal_wakes_the_recipient_by_no_lane_at_all(self):
        """A scope refusal suppresses the worker's watcher pickup too."""
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Codex",
            subscriber_agent="relay", runtime_family="codex_app")
        payload, stderr = self.deliver_with_scope(root, "CHAT-SCOPE1", recipient="relay")
        self.assertFalse(payload["autobridge_ready"])
        self.assertEqual("route_ambiguous", payload["autobridge_refusal_reason"])
        for flag in self.WAKE_FLAGS:
            self.assertFalse(payload.get(flag),
                             f"{flag} must be false for a refused packet, got {payload.get(flag)!r}")
        for prompt in self.WAKE_PROMPTS:
            self.assertIsNone(payload.get(prompt),
                              f"{prompt} must be null, got {payload.get(prompt)!r}")
        self.assertFalse(payload["watcher_pickup_ready"])
        self.assertIn("RUNTIME DISPATCH REFUSED", stderr)

    def test_the_same_agent_shape_dispatches_when_scope_matches(self):
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Codex",
            subscriber_agent="relay", runtime_family="codex_app")
        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1",
                                                  repo_targets="llm-collab",
                                                  recipient="relay")
        self.assertTrue(payload["autobridge_ready"])
        self.assertFalse(payload["ax_doorbell_required"],
                         "a dispatchable packet needs no doorbell either -- autobridge has it")

    def test_a_non_codex_ax_app_cannot_override_watcher_pickup(self):
        root = self.make_workspace()
        for agent, activation in (
            ("codex", {"type": "cli_session", "watcher_enabled": True}),
            ("relay", {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"}),
        ):
            self.add_agent(root, {"id": agent, "display_name": agent.title(),
                                  "activation": activation})
        self.create_chat(root, chat_dir_name="2026-07-25_no-session__CHAT-NOSESS",
                         chat_id="CHAT-NOSESS", project_id="amiga")
        payload, _stderr = self.deliver_with_scope(root, "CHAT-NOSESS", recipient="relay")
        self.assertFalse(payload["autobridge_ready"])
        self.assertTrue(payload["watcher_pickup_ready"])
        self.assertFalse(payload["activation_unavailable"])
        self.assertFalse(payload["ax_doorbell_required"])
        self.assertIsNone(payload["ax_doorbell_prompt"])

    def test_claude_profile_cannot_fall_through_to_human_relay(self):
        root = self.make_workspace()
        for agent, activation in (
            ("codex", {"type": "cli_session", "watcher_enabled": True}),
            (
                "custom",
                {
                    "type": "human_relay",
                    "watcher_enabled": False,
                    "ax_app": "Claude",
                },
            ),
        ):
            self.add_agent(
                root,
                {
                    "id": agent,
                    "display_name": agent.title(),
                    "activation": activation,
                },
            )
        self.create_chat(
            root,
            chat_dir_name="2026-07-27_claude-profile-relay__CHAT-CLAUDE-PROFILE",
            chat_id="CHAT-CLAUDE-PROFILE",
            project_id="amiga",
        )

        payload, _stderr = self.deliver_with_scope(
            root,
            "CHAT-CLAUDE-PROFILE",
            recipient="custom",
        )

        self.assertFalse(payload["operator_relay_required"])
        self.assertTrue(payload["activation_unavailable"])
        self.assertIn("Claude profile", payload["activation_unavailable_reason"])

    def test_unsupported_profile_cannot_fall_through_to_human_relay(self):
        for index, ax_app in enumerate(("Unknown Electron App", "", 7)):
            with self.subTest(ax_app=ax_app):
                root = self.make_workspace()
                for agent, activation in (
                    ("codex", {"type": "cli_session", "watcher_enabled": True}),
                    (
                        "custom",
                        {
                            "type": "human_relay",
                            "watcher_enabled": False,
                            "ax_app": ax_app,
                        },
                    ),
                ):
                    self.add_agent(
                        root,
                        {
                            "id": agent,
                            "display_name": agent.title(),
                            "activation": activation,
                        },
                    )
                chat_id = f"CHAT-UNSUPPORTED-PROFILE-{index}"
                self.create_chat(
                    root,
                    chat_dir_name=f"2026-07-27_unsupported-profile-relay__{chat_id}",
                    chat_id=chat_id,
                    project_id="amiga",
                )

                payload, _stderr = self.deliver_with_scope(
                    root,
                    chat_id,
                    recipient="custom",
                )

                self.assertFalse(payload["operator_relay_required"])
                self.assertTrue(payload["activation_unavailable"])
                self.assertIn(
                    "activation.ax_app",
                    payload["activation_unavailable_reason"],
                )

    def test_no_wake_lane_re_derives_the_flag_it_must_not_use(self):
        """Structural: five lanes each gated on `not autobridge_ready` is five chances to miss one.

        They now share one predicate, so a lane added later cannot quietly opt out by writing the
        old condition again.
        """
        source = (REPO_ROOT / "bin" / "deliver.py").read_text(encoding="utf-8")
        self.assertNotIn("        and not autobridge_ready\n", source,
                         "a wake lane is gating on autobridge_ready directly again")
        # Four lanes since the Claude desktop/Computer Use lane was deleted.
        self.assertGreaterEqual(source.count("and wake_fallback_allowed"), 4)

    # --- registration is all-or-nothing across BOTH writes ----------------------------------
    #
    # register_session wrote the session file and THEN read the existing binding, so an unreadable
    # one failed between the two writes: the new session persisted, the old binding still pointed at
    # the previous thread, and the command reported failure having already created partial
    # authoritative state. Reproduced by Codex through the real CLI.

    def registered_workspace_with_binding(self):
        root = self.make_workspace()
        for agent in ("codex", "claude"):
            self.add_agent(root, {"id": agent, "display_name": agent.title(),
                                  "activation": {"type": "cli_session", "watcher_enabled": True}})
        self.create_chat(root, chat_dir_name="2026-07-25_reg__CHAT-REG1",
                         chat_id="CHAT-REG1", project_id="amiga")
        self.run_cli(root, "register", "--session", "SESSION-OLD", "--agent", "claude",
                     "--project", "amiga", "--chat", "CHAT-REG1", "--mode", "notify",
                     "--runtime-family", "claude_app",
                     "--runtime-session-id", "THREAD-OLD",
                     "--runtime-session-source", "first_read")
        binding = (root / "State" / "session_autobridge" / "bindings" / "amiga" / "CHAT-REG1"
                   / "claude.json")
        self.assertTrue(binding.exists())
        return root, binding

    def register_new_expecting_failure(self, root):
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "register", "--session", "SESSION-NEW",
             "--agent", "claude", "--project", "amiga", "--chat", "CHAT-REG1",
             "--mode", "notify", "--runtime-family", "claude_app",
             "--runtime-session-id", "THREAD-NEW",
             "--runtime-session-source", "first_read", "--json"],
            cwd=root, text=True, capture_output=True,
            env=self.subprocess_env(root),
        )

    def test_a_binding_pathname_swapped_for_a_FIFO_cannot_hang_or_split_the_write(self):
        """The write side, which carrying the read snapshot does not protect.

        Registration published the session and THEN reopened the binding pathname through
        Path.write_text. A pathname swapped for a writer-less FIFO blocked forever there, or a
        directory raised -- either way with the session already updated and the authoritative
        binding still pointing at the previous thread. The snapshot protects the VALUES; it says
        nothing about the descriptor the write lands on.
        """
        import os as _os
        import subprocess as _sp
        import sys as _sys
        import textwrap as _tw

        root, binding = self.registered_workspace_with_binding()
        binding.unlink()
        _os.mkfifo(binding, 0o600)

        program = _tw.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO_ROOT / "bin")!r})
            sys.argv = ['session_autobridge.py', 'register', '--session', 'SESSION-NEW',
                        '--agent', 'claude', '--project', 'amiga', '--chat', 'CHAT-REG1',
                        '--mode', 'notify', '--runtime-family', 'claude_app',
                        '--runtime-session-id', 'THREAD-NEW',
                        '--runtime-session-source', 'first_read', '--json']
            import session_autobridge as cli
            try:
                cli.main()
                print('COMPLETED')
            except SystemExit as error:
                print('REFUSED:', error)
            except Exception as error:
                print('RAISED:', type(error).__name__, error)
        """)
        try:
            done = _sp.run([_sys.executable, "-c", program], cwd=root, text=True,
                           capture_output=True, timeout=20)
        except _sp.TimeoutExpired:
            self.fail("registration blocked on the swapped binding pathname")

        combined = done.stdout + done.stderr
        self.assertNotIn("COMPLETED", combined,
                         "a non-regular binding pathname must not be silently replaced")
        self.assertIn("not a regular file", combined)
        self.assertFalse(
            (root / "State" / "session_autobridge" / "sessions" / "SESSION-NEW.json").exists(),
            "the session must NOT have been published before the binding write refused")

    def test_the_binding_is_written_before_the_session_is_published(self):
        """Two independent writes cannot be atomic, so the ORDER decides the failure mode.

        Binding first means a failed session write leaves a binding referencing a session that does
        not exist, and the exact-binding resolver requires them to match, so the pair refuses. The
        old order left a live session bound to a stale thread, which resolves happily and is wrong.
        """
        source = (REPO_ROOT / "bin" / "session_autobridge.py").read_text(encoding="utf-8")
        body = source[source.index("def register_session"):]
        body = body[:body.index("\ndef ")]
        # Comments stripped first. My initial version compared raw offsets and matched a COMMENT
        # that mentions save_session() above the actual call, so it failed against correct code --
        # an assertion a comment can satisfy is not an assertion, which is the third time that
        # exact mistake has appeared on this PR.
        code = "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertLess(code.index("update_binding_from_session("), code.index("save_session("),
                        "the binding write must precede the session publish")

    def test_registration_reads_the_existing_binding_EXACTLY_ONCE(self):
        """A preflight that validates and then lets the update reopen the path is a TOCTOU.

        Codex's deterministic second-read fault: validate, write SESSION-NEW, then fail on the
        REREAD -- the original partial-state bug wearing a check. Counting the reads is what pins
        the fix; asserting the happy path cannot distinguish one read from two.
        """
        root, binding = self.registered_workspace_with_binding()
        probe = root / "count_reads.py"
        probe.write_text(
            "import json, sys\n"
            "sys.path.insert(0, 'bin')\n"
            "import _session_autobridge as ab\n"
            "import session_autobridge as cli\n"
            "reads = []\n"
            "real = ab.load_binding\n"
            "def counting(p, c, a):\n"
            "    reads.append((p, c, a))\n"
            "    return real(p, c, a)\n"
            "ab.load_binding = counting\n"
            "cli.load_binding = counting\n"
            "sys.argv = ['session_autobridge.py',\n"
            "    'register', '--session', 'SESSION-NEW', '--agent', 'claude',\n"
            "    '--project', 'amiga', '--chat', 'CHAT-REG1', '--mode', 'notify',\n"
            "    '--runtime-family', 'claude_app', '--runtime-session-id', 'THREAD-NEW',\n"
            "    '--runtime-session-source', 'first_read']\n"
            "cli.register_session(cli.parse_args())\n"
            "print(json.dumps({'reads': len(reads)}))\n",
            encoding="utf-8",
        )
        done = subprocess.run([sys.executable, str(probe)], cwd=root, text=True,
                              capture_output=True, env=self.subprocess_env(root))
        self.assertEqual(0, done.returncode, done.stderr[-600:])
        self.assertEqual(1, json.loads(done.stdout.strip().splitlines()[-1])["reads"],
                         "the existing binding must be read once and carried forward")

    def test_a_swap_after_validation_cannot_influence_the_written_binding(self):
        """The other half of the TOCTOU: the update must use the bytes already in hand."""
        root, binding = self.registered_workspace_with_binding()
        probe = root / "swap_after.py"
        probe.write_text(
            "import json, sys, pathlib\n"
            "sys.path.insert(0, 'bin')\n"
            "import _session_autobridge as ab\n"
            "import session_autobridge as cli\n"
            "path = pathlib.Path('State/session_autobridge/bindings/amiga/CHAT-REG1/claude.json')\n"
            "real = ab.load_binding\n"
            "swapped = []\n"
            "def swapping(p, c, a):\n"
            "    snapshot = real(p, c, a)\n"
            "    swapped.append(1)\n"
            "    poisoned = dict(snapshot, runtime_home='/tmp/POISONED', pad='x' * 300000)\n"
            "    path.write_text(json.dumps(poisoned))\n"
            "    return snapshot\n"
            "ab.load_binding = swapping\n"
            "cli.load_binding = swapping\n"
            "sys.argv = ['session_autobridge.py',\n"
            "    'register', '--session', 'SESSION-NEW', '--agent', 'claude',\n"
            "    '--project', 'amiga', '--chat', 'CHAT-REG1', '--mode', 'notify',\n"
            "    '--runtime-family', 'claude_app', '--runtime-session-id', 'THREAD-NEW',\n"
            "    '--runtime-session-source', 'first_read']\n"
            "result = cli.register_session(cli.parse_args())\n"
            "print(json.dumps({'home': result.get('binding', {}).get('runtime_home'),\n"
            "                  'swapped': swapped}))\n",
            encoding="utf-8",
        )
        done = subprocess.run([sys.executable, str(probe)], cwd=root, text=True,
                              capture_output=True, env=self.subprocess_env(root))
        self.assertEqual(0, done.returncode, done.stderr[-600:])
        emitted = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(emitted["swapped"],
                        "the swap never ran, so this test would pass while proving nothing")
        home = emitted["home"]
        self.assertNotEqual("/tmp/POISONED", home,
                            "a binding swapped after validation must not reach the written record")
        written = json.loads(binding.read_text())
        self.assertEqual("THREAD-NEW", written["runtime_session_id"])
        self.assertNotEqual("/tmp/POISONED", written.get("runtime_home"))

    def test_registration_refuses_on_an_OVERSIZED_existing_binding(self):
        root, binding = self.registered_workspace_with_binding()
        payload = json.loads(binding.read_text())
        payload["pad"] = "z" * (256 * 1024 + 2048)
        binding.write_text(json.dumps(payload), encoding="utf-8")
        before = binding.read_bytes()

        done = self.register_new_expecting_failure(root)

        self.assertNotEqual(0, done.returncode)
        self.assertNotIn("Traceback", done.stderr, done.stderr[-400:])
        self.assertIn("byte limit", done.stderr)
        self.assertFalse(
            (root / "State" / "session_autobridge" / "sessions" / "SESSION-NEW.json").exists(),
            "no session may be written when registration is refused")
        self.assertEqual(before, binding.read_bytes(),
                         "the unreadable binding must be left byte-identical, never replaced")

    def test_registration_refuses_on_an_IO_FAILED_existing_binding(self):
        """EACCES injected in the child rather than chmod(0o000).

        chmod does not make a file unreadable for UID 0, so under a root or containerized runner
        registration SUCCEEDED and this test failed rather than exercising the contract.
        """
        root, binding = self.registered_workspace_with_binding()
        done = run_cli_with_eacces_on(
            root, binding, "session_autobridge",
            ["session_autobridge.py", "register", "--session", "SESSION-NEW", "--agent", "claude",
             "--project", "amiga", "--chat", "CHAT-REG1", "--mode", "notify",
             "--runtime-family", "claude_app", "--runtime-session-id", "THREAD-NEW",
             "--runtime-session-source", "first_read", "--json"],
        )

        self.assertNotEqual(0, done.returncode)
        self.assertNotIn("Traceback", done.stderr, done.stderr[-400:])
        self.assertFalse(
            (root / "State" / "session_autobridge" / "sessions" / "SESSION-NEW.json").exists())
        self.assertEqual("THREAD-OLD", json.loads(binding.read_text())["runtime_session_id"],
                         "the old binding must still point where it did")

    def test_an_ordinary_re_registration_still_succeeds(self):
        """The control: without it these tests only prove I broke registration."""
        root, binding = self.registered_workspace_with_binding()
        done = self.register_new_expecting_failure(root)
        self.assertEqual(0, done.returncode, done.stderr[-400:])
        self.assertEqual("THREAD-NEW", json.loads(binding.read_text())["runtime_session_id"])

    def test_a_FIRST_registration_with_no_existing_binding_still_works(self):
        """FileNotFoundError is the ordinary case and must pass straight through the preflight."""
        root = self.make_workspace()
        self.add_agent(root, {"id": "claude", "display_name": "Claude",
                              "activation": {"type": "cli_session", "watcher_enabled": True}})
        self.create_chat(root, chat_dir_name="2026-07-25_first__CHAT-FIRST",
                         chat_id="CHAT-FIRST", project_id="amiga")
        result = self.run_cli(root, "register", "--session", "SESSION-FIRST", "--agent", "claude",
                              "--project", "amiga", "--chat", "CHAT-FIRST", "--mode", "notify",
                              "--runtime-family", "claude_app",
                              "--runtime-session-id", "THREAD-FIRST",
                              "--runtime-session-source", "first_read")
        self.assertEqual("SESSION-FIRST", result["session_id"])

    # --- an unreadable RECIPIENT binding must not cost the durable packet -------------------
    #
    # Making BindingUnreadable propagate was right for reporting and wrong for deliver.py: it
    # escaped before read_body(), so an oversized recipient binding meant exit 1, no packet, and a
    # traceback. I changed a shared function's failure mode and handled it in one caller.

    def oversize_recipient_binding(self, root, chat_id="CHAT-SCOPE1"):
        path = (root / "State" / "session_autobridge" / "bindings" / "amiga" / chat_id
                / "claude.json")
        self.assertTrue(path.exists(), f"expected a binding at {path}")
        payload = json.loads(path.read_text())
        payload["pad"] = "z" * (256 * 1024 + 2048)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_an_oversized_recipient_binding_still_writes_the_durable_packet(self):
        root, chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Claude")
        self.oversize_recipient_binding(root)
        payload, stderr = self.deliver_with_scope(root, "CHAT-SCOPE1",
                                                 repo_targets="llm-collab")
        self.assertFalse(payload["autobridge_ready"])
        self.assertIn("binding_unreadable", payload["autobridge_refusal_reason"])
        self.assertNotIn("exact_binding_required", payload["autobridge_refusal_reason"],
                         "an unreadable binding EXISTS; it must not be reported as absent")
        self.assertTrue(sorted(chat_dir.glob("*_to-claude_*.md")),
                        "the durable packet must survive a runtime refusal")
        self.assertNotIn("Traceback", stderr)

    def test_an_oversized_recipient_binding_wakes_nobody(self):
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Claude")
        self.oversize_recipient_binding(root)
        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1",
                                                  repo_targets="llm-collab")
        for flag in self.WAKE_FLAGS:
            self.assertFalse(payload.get(flag),
                             f"{flag} must be false: waking someone to read a packet whose "
                             "binding we could not read is the wrong-wake bug again")
        for prompt in self.WAKE_PROMPTS:
            self.assertIsNone(payload.get(prompt))

    def test_an_IO_failed_recipient_binding_behaves_the_same(self):
        """Permission denied, not oversize -- the other half of "unreadable"."""
        root, chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Claude")
        path = (root / "State" / "session_autobridge" / "bindings" / "amiga" / "CHAT-SCOPE1"
                / "claude.json")
        done = run_cli_with_eacces_on(
            root, path, "deliver",
            ["deliver.py", "--chat", "CHAT-SCOPE1", "--from", "codex", "--to", "claude",
             "--project", "amiga", "--title", "IO probe", "--sender-session-id", "codex-session-9",
             "--repo-targets", "llm-collab", "--body-file", "-"],
        )
        self.assertNotIn("Traceback", done.stderr, done.stderr[-500:])
        payload = json.loads(done.stdout.split("\n\n", 1)[0])
        self.assertFalse(payload["autobridge_ready"])
        self.assertIn("binding_unreadable", payload["autobridge_refusal_reason"])
        self.assertTrue(payload["binding_unreadable_blocker"],
                        "the blocker flag is the only machine-readable signal in this state")
        self.assertIn("blocker:", done.stderr)
        self.assertTrue(sorted(chat_dir.glob("*_to-claude_*.md")),
                        "the durable packet must survive an I/O-failed binding")

    def test_a_readable_binding_is_unaffected(self):
        """The control: this must still dispatch, or the tests above prove only that I broke it."""
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Claude")
        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1",
                                                   repo_targets="llm-collab")
        self.assertTrue(payload["autobridge_ready"])
        self.assertIsNone(payload["autobridge_refusal_reason"])

    def test_the_routing_contract_behaves_identically_on_a_NON_amiga_project(self):
        """AGENTS.md:43-44 -- a shared-contract change needs Amiga and a non-Amiga project.

        nuvyr is registered by this fixture. Run the whole representative set rather than one case:
        the risk being covered is a project-specific behaviour leaking into the universal path, and
        a single case cannot distinguish "works for nuvyr" from "refuses everything for nuvyr".
        """
        for project, chat_id in (("amiga", "CHAT-SCOPE-A"), ("nuvyr", "CHAT-SCOPE-N")):
            with self.subTest(project=project):
                root, chat_dir = self.scoped_subscriber_workspace(
                    subscriber_repo_targets=["llm-collab"], subscriber_ax_app="Claude",
                    project=project, chat_id=chat_id)

                refused, stderr = self.deliver_with_scope(root, chat_id, project=project)
                self.assertFalse(refused["autobridge_ready"],
                                 f"{project}: an empty packet scope must not route")
                self.assertEqual("route_ambiguous", refused["autobridge_refusal_reason"])
                self.assertIn("RUNTIME DISPATCH REFUSED", stderr)
                for flag in self.WAKE_FLAGS:
                    self.assertFalse(refused.get(flag), f"{project}: {flag} must be false")
                for prompt in self.WAKE_PROMPTS:
                    self.assertIsNone(refused.get(prompt), f"{project}: {prompt} must be null")
                self.assertTrue(sorted(chat_dir.glob("*_to-claude_*.md")),
                                f"{project}: the durable record must survive the refusal")

                accepted, _stderr = self.deliver_with_scope(root, chat_id, project=project,
                                                            repo_targets="llm-collab")
                self.assertTrue(accepted["autobridge_ready"],
                                f"{project}: a declared subset must route")
                self.assertIsNone(accepted["autobridge_refusal_reason"])

                outside, _stderr = self.deliver_with_scope(root, chat_id, project=project,
                                                           repo_targets="some-other-repo")
                self.assertFalse(outside["autobridge_ready"],
                                 f"{project}: a packet outside the subscriber scope must not route")

    def test_scoped_subscriber_refuses_an_empty_packet_scope(self):
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"])
        payload, stderr = self.deliver_with_scope(root, "CHAT-SCOPE1")
        self.assertFalse(payload["autobridge_ready"],
                         "a packet the watcher will refuse must not be reported as ready")
        self.assertEqual("route_ambiguous", payload["autobridge_refusal_reason"])
        self.assertIn("DURABLE WRITE OK", stderr)
        self.assertIn("RUNTIME DISPATCH REFUSED", stderr)
        self.assertIn("--repo-targets", stderr,
                      "the operator must be told how to fix it, not just that it failed")

    def test_scoped_subscriber_accepts_a_declared_subset(self):
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab", "amiga"])
        payload, stderr = self.deliver_with_scope(root, "CHAT-SCOPE1",
                                                 repo_targets="llm-collab")
        self.assertTrue(payload["autobridge_ready"])
        self.assertIsNone(payload["autobridge_refusal_reason"])
        self.assertNotIn("RUNTIME DISPATCH REFUSED", stderr)

    def test_scoped_subscriber_refuses_a_packet_outside_its_scope(self):
        root, _chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"])
        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1",
                                                  repo_targets="some-other-repo")
        self.assertFalse(payload["autobridge_ready"])
        self.assertEqual("route_ambiguous", payload["autobridge_refusal_reason"])

    def test_unscoped_subscriber_still_accepts_an_empty_packet_scope(self):
        """The rule Codex insisted on preserving: do NOT make --repo-targets globally required."""
        root, _chat_dir = self.scoped_subscriber_workspace(subscriber_repo_targets=None)
        payload, stderr = self.deliver_with_scope(root, "CHAT-SCOPE1")
        self.assertTrue(payload["autobridge_ready"],
                        "an unscoped subscriber accepts an unscoped packet, as before")
        self.assertIsNone(payload["autobridge_refusal_reason"])
        self.assertNotIn("RUNTIME DISPATCH REFUSED", stderr)

    def test_a_refused_packet_is_still_written_to_the_mailbox(self):
        """Fail-closed on DISPATCH must not mean losing the durable record."""
        root, chat_dir = self.scoped_subscriber_workspace(
            subscriber_repo_targets=["llm-collab"])
        payload, _stderr = self.deliver_with_scope(root, "CHAT-SCOPE1")
        self.assertFalse(payload["autobridge_ready"])
        written = sorted(chat_dir.glob("*_to-claude_*.md"))
        self.assertTrue(written, "the packet must remain readable with inbox.py")
        frontmatter, _ = parse_frontmatter(written[-1].read_text())
        self.assertEqual([], frontmatter["repo_targets"])

    def test_deliver_uses_canonical_binding_for_target_session_id(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-23_binding-test__CHAT-BIND1",
            chat_id="CHAT-BIND1",
            project_id="amiga",
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-BOUND",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-BIND1",
            "--mode",
            "notify",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "claude-bound-session-42",
            "--runtime-session-source",
            "first_read",
        )
        session_path = (
            root
            / "State"
            / "session_autobridge"
            / "sessions"
            / "SESSION-CLAUDE-BOUND.json"
        )
        session = json.loads(session_path.read_text())
        session.update({"binding_id": "binding-canonical", "binding_generation": 7})
        write_json(session_path, session)
        binding_path = (
            root
            / "State"
            / "session_autobridge"
            / "bindings"
            / "amiga"
            / "CHAT-BIND1"
            / "claude.json"
        )
        binding = json.loads(binding_path.read_text())
        binding.update({"binding_id": "binding-canonical", "binding_generation": 7})
        write_json(binding_path, binding)

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-BIND1",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Binding targeted message",
                "--sender-session-id",
                "codex-session-5",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the canonical binding.",
            capture_output=True,
            check=True,
        )
        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertTrue(result_payload["autobridge_ready"])
        self.assertIsNone(result_payload["autobridge_refusal_reason"])
        self.assertEqual("claude-bound-session-42", result_payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-claude_*.md"))
        self.assertTrue(delivered_candidates)
        delivered_text = delivered_candidates[-1].read_text()
        self.assertIn("target_session_id: claude-bound-session-42", delivered_text)
        frontmatter, _ = parse_frontmatter(delivered_text)
        self.assertEqual("binding-canonical", frontmatter["target_binding_id"])
        self.assertEqual(7, frontmatter["target_binding_generation"])

    def test_deliver_false_readiness_engages_fallback_and_writes_packet(self):
        # The subject is relay, not claude: this lane asserts the doorbell fallback
        # engages when a session claims readiness it cannot back, and Claude is
        # excluded from that fallback entirely.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "relay",
                "display_name": "Relay",
                "activation": {
                    "type": "cli_session",
                    "watcher_enabled": True,
                    "ax_app": "Codex",
                },
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-23_readiness-drift__CHAT-READY-DRIFT",
            chat_id="CHAT-READY-DRIFT",
            project_id="amiga",
        )
        body_file = root / "readiness-drift-body.txt"
        write(body_file, "Durably deliver this packet and wake the receiver.")
        # Same identity as the harness and the delivered packet. Left as claude, the
        # message_targets_session assertion below returned route_ambiguous trivially --
        # the packet was simply addressed to someone else -- instead of proving that a
        # session claiming readiness it cannot back is rejected.
        target = {
            "session_id": "SESSION-READY-DRIFT",
            "agent_id": "relay",
            "project_id": "amiga",
            "chat_id": "CHAT-READY-DRIFT",
            "status": "parked",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "zcode_cli", "session_id": "relay-runtime-drift"},
        }
        harness = root / "deliver_readiness_drift.py"
        write(
            harness,
            "\n".join(
                [
                    "import sys",
                    "import deliver",
                    "target = {",
                    "    'session_id': 'SESSION-READY-DRIFT',",
                    "    'agent_id': 'relay',",
                    "    'project_id': 'amiga',",
                    "    'chat_id': 'CHAT-READY-DRIFT',",
                    "    'status': 'parked',",
                    "    'wake_strategy': 'runtime_trigger',",
                    "    'runtime': {'family': 'zcode_cli', 'session_id': 'relay-runtime-drift'},",
                    "}",
                    "deliver.resolve_exact_dispatch_target = lambda *_args: (target, None)",
                    "deliver.resolve_bound_runtime_session_id = lambda *_args: None",
                    "sys.argv = [",
                    "    sys.argv[0], '--chat', 'CHAT-READY-DRIFT', '--from', 'codex',",
                    "    '--to', 'relay', '--project', 'amiga', '--title', 'Readiness drift',",
                    "    '--body-file', 'readiness-drift-body.txt', '--skip-awareness-instruction',",
                    "]",
                    "deliver.main()",
                ]
            ),
        )

        result = subprocess.run(
            [sys.executable, str(harness)],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        payload = json.loads(result.stdout.split("\n\n", 1)[0])
        self.assertFalse(payload["autobridge_ready"])
        self.assertTrue(payload["watcher_pickup_ready"])
        self.assertFalse(payload["ax_doorbell_required"])
        self.assertIsNone(payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-relay_readiness-drift.md"))
        self.assertTrue(delivered_candidates)
        frontmatter, _ = parse_frontmatter(delivered_candidates[-1].read_text())
        self.assertEqual("Readiness drift", frontmatter["title"])
        # Identity now matches the delivered packet, so the refusal has to come from the
        # real cause -- no exact target was written for a session whose readiness was
        # false -- rather than from the packet being addressed to another agent.
        self.assertEqual("relay", frontmatter["to"])
        self.assertIsNone(frontmatter.get("target_session_id"))
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(
                target, {"frontmatter": frontmatter}
            ),
        )

    def test_deliver_refuses_explicit_target_that_disagrees_with_exact_binding(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-23_binding-explicit-mismatch__CHAT-BIND2",
            chat_id="CHAT-BIND2",
            project_id="amiga",
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-BOUND",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-BIND2",
            "--mode",
            "notify",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "claude-bound-session-42",
            "--runtime-session-source",
            "first_read",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-BIND2",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Binding explicit mismatch",
                "--sender-session-id",
                "codex-session-5",
                "--target-session-id",
                "wrong-runtime",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Do not silently override the explicit target.",
            capture_output=True,
            check=True,
        )
        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["autobridge_ready"])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_MISMATCH_REASON,
            result_payload["autobridge_refusal_reason"],
        )
        self.assertIsNone(result_payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-claude_*.md"))
        self.assertTrue(delivered_candidates)
        frontmatter, _ = parse_frontmatter(delivered_candidates[-1].read_text())
        self.assertIsNone(frontmatter["target_session_id"])

    def test_deliver_suppresses_codex_self_activation_for_amiga_and_non_amiga(self):
        cases = (("amiga", "CHAT-SELF-AMIGA"), ("nuvyr", "CHAT-SELF-NUVYR"))
        for project_id, chat_id in cases:
            with self.subTest(project_id=project_id):
                root = self.make_workspace()
                self.add_agent(
                    root,
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "activation": {
                            "type": "cli_session",
                            "watcher_enabled": True,
                            "ax_app": "Codex",
                        },
                    },
                )
                chat_dir = self.create_chat(
                    root,
                    chat_dir_name=f"2026-07-12_codex-self-target__{chat_id}",
                    chat_id=chat_id,
                    project_id=project_id,
                )

                deliver_result = subprocess.run(
                    [
                        sys.executable,
                        str(DELIVER_SCRIPT),
                        "--chat",
                        chat_id,
                        "--from",
                        "codex",
                        "--to",
                        "codex",
                        "--project",
                        project_id,
                        "--title",
                        "Codex self handoff",
                        "--body-file",
                        "-",
                    ],
                    cwd=root,
                    text=True,
                    input="Preserve this durable handoff without an app self-doorbell.",
                    capture_output=True,
                    check=True,
                )

                result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
                self.assertTrue(result_payload["thread_coordination_required"])
                self.assertFalse(result_payload["autobridge_ready"])
                self.assertFalse(result_payload["ax_doorbell_required"])
                self.assertFalse(result_payload["desktop_bridge_required"])
                self.assertFalse(result_payload["operator_relay_required"])
                self.assertFalse(result_payload["activation_unavailable"])
                self.assertIn("CODEX THREAD COORDINATION REQUIRED", deliver_result.stdout)
                self.assertIn("read_thread", deliver_result.stdout)
                self.assertIn("send_message_to_thread", deliver_result.stdout)
                self.assertNotIn("AX DOORBELL REQUIRED", deliver_result.stdout)
                self.assertNotIn("CLAUDE DESKTOP BRIDGE REQUIRED", deliver_result.stdout)
                delivered_candidates = sorted(chat_dir.glob("*_to-codex_*.md"))
                self.assertTrue(delivered_candidates)
                self.assertIn(
                    "Preserve this durable handoff without an app self-doorbell.",
                    delivered_candidates[-1].read_text(),
                )

    def test_external_workers_can_still_ring_codex_for_amiga_and_non_amiga(self):
        cases = (
            ("amiga", "claude", "Claude", "CHAT-CLAUDE-CODEX"),
            ("nuvyr", "zcode", "ZCode", "CHAT-ZCODE-CODEX"),
        )
        for project_id, sender_id, sender_display, chat_id in cases:
            with self.subTest(project_id=project_id, sender_id=sender_id):
                root = self.make_workspace()
                self.add_agent(
                    root,
                    {
                        "id": sender_id,
                        "display_name": sender_display,
                        "activation": {"type": "cli_session", "watcher_enabled": True},
                    },
                )
                self.add_agent(
                    root,
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "activation": {
                            "type": "cli_session",
                            "watcher_enabled": True,
                            "ax_app": "Codex",
                        },
                    },
                )
                self.create_chat(
                    root,
                    chat_dir_name=f"2026-07-12_external-to-codex__{chat_id}",
                    chat_id=chat_id,
                    project_id=project_id,
                )

                deliver_result = subprocess.run(
                    [
                        sys.executable,
                        str(DELIVER_SCRIPT),
                        "--chat",
                        chat_id,
                        "--from",
                        sender_id,
                        "--to",
                        "codex",
                        "--project",
                        project_id,
                        "--title",
                        "External handoff to root Codex",
                        "--body-file",
                        "-",
                    ],
                    cwd=root,
                    text=True,
                    input="Ring root Codex after preserving this packet.",
                    capture_output=True,
                    check=True,
                )

                result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
                self.assertFalse(result_payload["thread_coordination_required"])
                self.assertTrue(result_payload["ax_doorbell_required"])
                self.assertIn(f"[from {sender_id}]", result_payload["ax_doorbell_prompt"])
                self.assertIn("AX DOORBELL REQUIRED", deliver_result.stdout)
                self.assertIn('axsend-ensure ring --app "Codex"', deliver_result.stdout)

    def test_deliver_never_rings_a_claude_cli_session_without_a_binding(self):
        # The case the AX doorbell exists for -- no dispatchable autobridge -- is exactly
        # where a worker used to be handed a runnable ring for Claude.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {
                    "type": "cli_session",
                    "watcher_enabled": True,
                    "ax_app": "Claude",
                },
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-23_claude-no-binding__CHAT-NOBIND",
            chat_id="CHAT-NOBIND",
            project_id="amiga",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-NOBIND",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Claude packet with no dispatchable binding",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Durable packet only.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["ax_doorbell_required"])
        self.assertIsNone(result_payload["ax_doorbell_prompt"])
        self.assertFalse(result_payload["desktop_bridge_required"])
        self.assertFalse(result_payload["operator_relay_required"])
        # Every lane, not just the one that used to fire: excluding Claude from the
        # routine doorbell alone pushed delivery into attended recovery, and
        # suppressing the desktop bridge pushed it into human relay. Both shipped.
        self.assertFalse(result_payload["ax_attended_recovery_required"])
        self.assertIsNone(result_payload["ax_attended_recovery_prompt"])
        self.assertNotIn("AX DOORBELL REQUIRED", deliver_result.stdout)
        self.assertNotIn("ATTENDED RECOVERY REQUIRED", deliver_result.stdout.upper())
        self.assertNotIn("RELAY REQUIRED", deliver_result.stdout.upper())
        self.assertNotIn("ACTIVATION UNAVAILABLE", deliver_result.stdout)
        self.assertNotIn("axsend", deliver_result.stdout)
        self.assertNotIn("Computer Use", deliver_result.stdout)

        # The packet is durable and the watcher owns pickup without a second wake path.
        self.assertTrue((root / result_payload["to_file"]).exists())
        self.assertTrue(result_payload["watcher_pickup_ready"])
        self.assertFalse(result_payload["activation_unavailable"])
        self.assertIsNone(result_payload["activation_unavailable_reason"])

    def test_deliver_never_attends_recovery_for_an_opaque_claude(self):
        # ax_attended_only routes a target to Codex-attended AX/Computer Use. Excluding
        # Claude from the routine doorbell alone left this lane wide open.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {
                    "type": "cli_session",
                    "watcher_enabled": True,
                    "ax_app": "Claude",
                    "ax_attended_only": True,
                },
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-23_claude-opaque__CHAT-OPAQUE",
            chat_id="CHAT-OPAQUE",
            project_id="amiga",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-OPAQUE",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Opaque composer Claude",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Durable packet only.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["ax_attended_recovery_required"])
        self.assertIsNone(result_payload["ax_attended_recovery_prompt"])
        self.assertFalse(result_payload["ax_doorbell_required"])
        self.assertFalse(result_payload["operator_relay_required"])
        self.assertNotIn("ATTENDED RECOVERY REQUIRED", deliver_result.stdout.upper())
        self.assertNotIn("axsend", deliver_result.stdout)
        self.assertNotIn("Computer Use", deliver_result.stdout)
        self.assertTrue((root / result_payload["to_file"]).exists())

    def test_deliver_never_uses_computer_use_for_a_desktop_bridge_project(self):
        # amiga carries claude_desktop_bridge: true in the fixture, and this Claude is
        # registered non-cli_session -- the exact shape that used to select the
        # Computer Use fallback.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "human_relay", "watcher_enabled": True},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-23_claude-desktop-fallback__CHAT-BRIDGE2",
            chat_id="CHAT-BRIDGE2",
            project_id="amiga",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-BRIDGE2",
                "--from",
                "codex",
                "--to",
                "claude",
                "--project",
                "amiga",
                "--title",
                "Claude desktop fallback",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Durable packet only.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["desktop_bridge_required"])
        self.assertIsNone(result_payload["desktop_bridge_prompt"])
        self.assertFalse(result_payload["ax_doorbell_required"])
        self.assertNotIn("CLAUDE DESKTOP BRIDGE REQUIRED", deliver_result.stdout)
        self.assertNotIn("Computer Use", deliver_result.stdout)
        self.assertNotIn("Claude.app", deliver_result.stdout)

        # This registration is human_relay, so suppressing the desktop bridge handed the
        # packet to the operator-relay branch instead: a printed handoff asking the
        # operator to activate Claude. One forbidden wake replaced by another.
        self.assertFalse(result_payload["operator_relay_required"])
        self.assertFalse(result_payload["relay_required"])
        self.assertTrue(result_payload["watcher_pickup_ready"])
        self.assertNotIn("RELAY REQUIRED", deliver_result.stdout.upper())

        self.assertTrue((root / result_payload["to_file"]).exists())

    def test_non_codex_identity_cannot_get_ax_doorbell_from_spoofed_app(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "Codex Worker",
                "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-06-26_codex-doorbell__CHAT-CODEX2",
            chat_id="CHAT-CODEX2",
            project_id="nuvyr",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-CODEX2",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "nuvyr",
                "--title",
                "Codex doorbell",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the durable packet, then ring Codex with axsend.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["relay_required"])
        self.assertFalse(result_payload["operator_relay_required"])
        self.assertFalse(result_payload["desktop_bridge_required"])
        self.assertFalse(result_payload["ax_doorbell_required"])
        self.assertIsNone(result_payload["ax_doorbell_prompt"])
        self.assertTrue(result_payload["watcher_pickup_ready"])
        self.assertFalse(result_payload["activation_unavailable"])
        self.assertNotIn("AX DOORBELL REQUIRED", deliver_result.stdout)
        self.assertNotIn("axsend-ensure ring", deliver_result.stdout)
        self.assertNotIn("RELAY REQUIRED FOR OPERATOR", deliver_result.stdout)
        delivered_candidates = sorted(
            (root / "Chats" / "2026-06-26_codex-doorbell__CHAT-CODEX2").glob("*_to-cdx2_*.md")
        )
        self.assertTrue(delivered_candidates)
        delivered_text = delivered_candidates[-1].read_text()
        self.assertIn("First-time setup required before task work:", delivered_text)
        self.assertIn(f"{root}/AGENTS.md", delivered_text)
        self.assertIn("Use the durable packet, then ring Codex with axsend.", delivered_text)

    def test_deliver_reports_terminal_only_cli_session_as_unavailable(self):
        # #given
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "terminal-worker",
                "display_name": "Terminal Worker",
                "activation": {"type": "cli_session", "watcher_enabled": False},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-07-10_terminal-only__CHAT-TERM1",
            chat_id="CHAT-TERM1",
            project_id="nuvyr",
        )

        # #when
        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-TERM1",
                "--from",
                "codex",
                "--to",
                "terminal-worker",
                "--project",
                "nuvyr",
                "--title",
                "Terminal delivery",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the durable packet.",
            capture_output=True,
            check=True,
        )

        # #then
        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertTrue(result_payload["activation_unavailable"])
        self.assertFalse(result_payload["relay_required"])
        self.assertFalse(result_payload["ax_doorbell_required"])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON,
            result_payload["activation_unavailable_reason"],
        )
        self.assertIn("ACTIVATION UNAVAILABLE", deliver_result.stdout)
        self.assertNotIn("RELAY REQUIRED FOR OPERATOR", deliver_result.stdout)

    def test_deliver_reports_api_trigger_without_runtime_as_unavailable(self):
        # #given
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "hook",
                "display_name": "Webhook Worker",
                "activation": {"type": "api_trigger", "watcher_enabled": False},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-07-10_api-trigger__CHAT-API1",
            chat_id="CHAT-API1",
            project_id="nuvyr",
        )

        # #when
        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-API1",
                "--from",
                "codex",
                "--to",
                "hook",
                "--project",
                "nuvyr",
                "--title",
                "API delivery",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the durable packet.",
            capture_output=True,
            check=True,
        )

        # #then
        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertTrue(result_payload["activation_unavailable"])
        self.assertFalse(result_payload["relay_required"])
        self.assertFalse(result_payload["operator_relay_required"])
        self.assertIn("api_trigger", result_payload["activation_unavailable_reason"])
        self.assertIn("ACTIVATION UNAVAILABLE", deliver_result.stdout)
        self.assertNotIn("RELAY REQUIRED FOR OPERATOR", deliver_result.stdout)

    def test_deliver_suppresses_manual_relay_when_autobridge_target_is_dispatchable(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": True},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-25_dispatchable-target__CHAT-DISPATCH1",
            chat_id="CHAT-DISPATCH1",
            project_id="amiga",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CDX2-DISPATCHABLE",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-DISPATCH1",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "cdx2-thread-1",
            "--runtime-session-source",
            "first_read",
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-DISPATCH1",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "amiga",
                "--title",
                "Dispatchable receiver",
                "--sender-session-id",
                "codex-thread-1",
                "--target-session-id",
                "cdx2-thread-1",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use autobridge.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertTrue(result_payload["autobridge_ready"])
        self.assertEqual("SESSION-CDX2-DISPATCHABLE", result_payload["autobridge_session_id"])
        self.assertFalse(result_payload["relay_required"])
        self.assertNotIn("RELAY REQUIRED FOR OPERATOR", deliver_result.stdout)

    def test_deliver_refuses_untargeted_dispatchable_session_as_exact_binding_required(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "human_relay", "watcher_enabled": False},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-25_dispatchable-broadcast__CHAT-DISPATCH2",
            chat_id="CHAT-DISPATCH2",
            project_id="amiga",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CDX2-BROADCAST",
            "--agent",
            "cdx2",
            "--project",
            "amiga",
            "--chat",
            "CHAT-DISPATCH2",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, "-c", "import sys; sys.exit(0)"]),
        )

        deliver_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-DISPATCH2",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "amiga",
                "--title",
                "Untargeted dispatchable receiver",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Use the chat-scoped autobridge session.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(deliver_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["autobridge_ready"])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON,
            result_payload["autobridge_refusal_reason"],
        )
        self.assertIsNone(result_payload["autobridge_session_id"])
        self.assertTrue(result_payload["relay_required"])
        self.assertIsNone(result_payload["resolved_target_session_id"])
        delivered_candidates = sorted(
            (root / "Chats" / "2026-04-25_dispatchable-broadcast__CHAT-DISPATCH2").glob(
                "*_to-cdx2_*.md"
            )
        )
        self.assertTrue(delivered_candidates)
        frontmatter, _ = parse_frontmatter(delivered_candidates[-1].read_text())
        self.assertIsNone(frontmatter["target_session_id"])

    def test_deliver_refuses_thread_pair_as_dispatch_authority(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-24_pairing-test__CHAT-PAIR1",
            chat_id="CHAT-PAIR1",
            project_id="amiga",
        )

        subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PAIR1",
                "--from",
                "codex",
                "--to",
                "cdx2",
                "--project",
                "amiga",
                "--title",
                "Seed receiver thread",
                "--sender-session-id",
                "codex-thread-1",
                "--target-session-id",
                "cdx2-thread-9",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Create the paired thread.",
            capture_output=True,
            check=True,
        )

        reverse_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PAIR1",
                "--from",
                "cdx2",
                "--to",
                "codex",
                "--project",
                "amiga",
                "--title",
                "Reply to sender thread",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Reply on the paired thread.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(reverse_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["autobridge_ready"])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON,
            result_payload["autobridge_refusal_reason"],
        )
        self.assertIsNone(result_payload["resolved_target_session_id"])

        delivered_candidates = sorted(chat_dir.glob("*_to-codex_*.md"))
        self.assertTrue(delivered_candidates)
        delivered_text = delivered_candidates[-1].read_text()
        self.assertIn("target_session_id:", delivered_text)
        frontmatter, _ = parse_frontmatter(delivered_text)
        self.assertIsNone(frontmatter["target_session_id"])

        pair_path = root / "State" / "session_autobridge" / "thread_pairs" / "amiga" / "CHAT-PAIR1" / "cdx2__codex.json"
        pair = json.loads(pair_path.read_text())
        self.assertEqual("codex-thread-1", pair["sessions"]["codex"])

        note_candidates = sorted(chat_dir.glob("*_note-cdx2_*.md"))
        self.assertTrue(note_candidates)
        note_text = note_candidates[-1].read_text()
        self.assertIn("summary_event: sent", note_text)
        self.assertIn("target_session_id:", note_text)
        self.assertIn("cdx2 sent `Reply to sender thread` to codex.", note_text)

    def test_deliver_preserves_thread_pair_sender_state_without_authorizing_dispatch(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(
            root,
            {
                "id": "cdx2",
                "display_name": "CDX2",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.create_chat(
            root,
            chat_dir_name="2026-04-24_pairing-update__CHAT-PAIR2",
            chat_id="CHAT-PAIR2",
            project_id="amiga",
        )

        for sender_session_id in ("codex-thread-1", "codex-thread-2"):
            subprocess.run(
                [
                    sys.executable,
                    str(DELIVER_SCRIPT),
                    "--chat",
                    "CHAT-PAIR2",
                    "--from",
                    "codex",
                    "--to",
                    "cdx2",
                    "--project",
                    "amiga",
                    "--title",
                    f"Seed {sender_session_id}",
                    "--sender-session-id",
                    sender_session_id,
                    "--target-session-id",
                    "cdx2-thread-9",
                    "--body-file",
                    "-",
                ],
                cwd=root,
                text=True,
                input=f"Use {sender_session_id}.",
                capture_output=True,
                check=True,
            )

        reverse_result = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PAIR2",
                "--from",
                "cdx2",
                "--to",
                "codex",
                "--project",
                "amiga",
                "--title",
                "Reply after sender moved sessions",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Reply on the latest sender thread.",
            capture_output=True,
            check=True,
        )

        result_payload = json.loads(reverse_result.stdout.split("\n\n", 1)[0])
        self.assertFalse(result_payload["autobridge_ready"])
        self.assertEqual(
            session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON,
            result_payload["autobridge_refusal_reason"],
        )
        self.assertIsNone(result_payload["resolved_target_session_id"])

        pair_path = root / "State" / "session_autobridge" / "thread_pairs" / "amiga" / "CHAT-PAIR2" / "cdx2__codex.json"
        pair = json.loads(pair_path.read_text())
        self.assertEqual("codex-thread-2", pair["sessions"]["codex"])
        self.assertNotIn("cdx2", pair["sessions"])

    def test_dispatch_writes_operator_picked_up_note(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-04-24_operator-summary__CHAT-SUM1",
            chat_id="CHAT-SUM1",
            project_id="amiga",
        )
        worker_script = root / "operator_summary_worker.py"
        output_file = root / "operator_summary_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )
        self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-SUM1",
            project_id="amiga",
            title="Operator summary pickup",
            sender_session_id="codex-thread-1",
            target_session_id="gemini-thread-1",
            sender_agent_id="codex",
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-SUMMARY",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-SUM1",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-thread-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        dispatch = self.run_cli(root, "dispatch", "--session", "SESSION-SUMMARY")
        self.assertEqual(1, dispatch["matched_messages"])

        note_candidates = sorted(chat_dir.glob("*_note-gemini_*.md"))
        self.assertTrue(note_candidates)
        note_text = note_candidates[-1].read_text()
        self.assertIn("summary_event: picked_up", note_text)
        self.assertIn("runtime_session_id: gemini-thread-1", note_text)
        self.assertIn("gemini picked up `Operator summary pickup`.", note_text)

    def test_pull_pending_runtime_does_not_write_picked_up_summary(self):
        session = {
            "session_id": "SESSION-PULL-PENDING",
            "agent_id": "relay",
            "project_id": "amiga",
            "chat_id": "CHAT-PULL-PENDING",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "gemini_cli", "session_id": "relay-native"},
        }
        message = {
            "path": "Chats/pull-pending/packet.md",
            "frontmatter": {
                "from": "claude",
                "sender_agent_id": "claude",
                "title": "Still waiting for native acceptance",
                "chat_id": "CHAT-PULL-PENDING",
            },
        }
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib,
            "execute_runtime_trigger",
            return_value={
                "returncode": 0,
                "delivery_accepted": False,
                "reason": "pull_pending",
            },
        ), patch.object(
            session_autobridge_lib,
            "write_operator_turn_summary",
        ) as write_summary:
            result = session_autobridge_lib.dispatch_session("SESSION-PULL-PENDING")

        write_summary.assert_not_called()
        self.assertFalse(result["actions"][0]["runtime_result"]["delivery_accepted"])

    def test_watch_inbox_skips_codex_self_target_but_activates_external_sender(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "codex",
                "display_name": "Codex",
                "activation": {
                    "type": "cli_session",
                    "watcher_enabled": True,
                    "ax_app": "Codex",
                },
            },
        )
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_dir = self.create_chat(
            root,
            chat_dir_name="2026-07-12_codex-watcher-guard__CHAT-CODEX-WATCH",
            chat_id="CHAT-CODEX-WATCH",
            project_id="amiga",
        )
        worker_script = root / "codex_watcher_runtime.py"
        output_file = root / "codex_watcher_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CODEX-WATCH",
            "--agent",
            "codex",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CODEX-WATCH",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "codex_app",
            "--runtime-session-id",
            "codex-runtime-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        self_delivery = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-CODEX-WATCH",
                "--from",
                "codex",
                "--to",
                "codex",
                "--project",
                "amiga",
                "--title",
                "Codex self watcher guard",
                "--sender-session-id",
                "codex-root-1",
                "--target-session-id",
                "codex-runtime-1",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Keep this durable without waking a Codex runtime.",
            capture_output=True,
            check=True,
        )
        self_payload = json.loads(self_delivery.stdout.split("\n\n", 1)[0])
        self.assertTrue(self_payload["thread_coordination_required"])
        self.assertIsNone(self_payload["resolved_target_session_id"])
        self_message = sorted(chat_dir.glob("*_to-codex_codex-self-watcher-guard.md"))[-1]
        self_frontmatter, _ = parse_frontmatter(self_message.read_text())
        self.assertTrue(self_frontmatter["autobridge_skip"])
        self.assertEqual("codex_self_target", self_frontmatter["autobridge_skip_reason"])
        self.assertIsNone(self_frontmatter["target_session_id"])

        external_delivery = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-CODEX-WATCH",
                "--from",
                "claude",
                "--to",
                "codex",
                "--project",
                "amiga",
                "--title",
                "External Codex watcher routing",
                "--target-session-id",
                "codex-runtime-1",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="Wake the registered Codex runtime for this external handoff.",
            capture_output=True,
            check=True,
        )
        external_payload = json.loads(external_delivery.stdout.strip())
        self.assertTrue(external_payload["autobridge_ready"])
        self.assertFalse(external_payload["thread_coordination_required"])
        external_message = sorted(
            chat_dir.glob("*_to-codex_external-codex-watcher-routing.md")
        )[-1]
        external_frontmatter, _ = parse_frontmatter(external_message.read_text())
        self.assertNotIn("autobridge_skip", external_frontmatter)
        self.assertEqual("codex-runtime-1", external_frontmatter["target_session_id"])
        legacy_self_rel = self.add_message(
            root,
            agent_id="codex",
            chat_id="CHAT-CODEX-WATCH",
            project_id="amiga",
            title="Legacy Codex self packet",
            sender_session_id="codex-legacy-root",
            target_session_id="codex-runtime-1",
            sender_agent_id="codex",
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "codex",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [
            json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()
        ]
        consumed_paths = {
            event["message_path"]
            for event in watcher_events
            if event["event"] == "autobridge_consumed"
        }
        self_rel = str(self_message.relative_to(root))
        external_rel = str(external_message.relative_to(root))
        self.assertNotIn(self_rel, consumed_paths)
        self.assertNotIn(legacy_self_rel, consumed_paths)
        self.assertIn(external_rel, consumed_paths)

        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("claude", runtime_payload["message"]["from"])
        self.assertEqual("External Codex watcher routing", runtime_payload["message"]["title"])

        inbox = json.loads((root / "agents" / "codex" / "inbox.json").read_text())
        self.assertIn(self_rel, inbox["unread"])
        self.assertIn(legacy_self_rel, inbox["unread"])
        self.assertIn(external_rel, inbox["read"])
        event_log = root / "State" / "session_autobridge" / "events" / "SESSION-CODEX-WATCH.jsonl"
        session_events = [json.loads(line) for line in event_log.read_text().splitlines()]
        self.assertTrue(
            any(
                event.get("event") == "message_skipped"
                and event.get("message_path") == self_rel
                and event.get("reason") == "codex_self_target_thread_coordination"
                for event in session_events
            )
        )
        self.assertTrue(
            any(
                event.get("event") == "message_skipped"
                and event.get("message_path") == legacy_self_rel
                and event.get("reason") == "codex_self_target_thread_coordination"
                for event in session_events
            )
        )

    def test_watch_inbox_autobridges_runtime_trigger_and_marks_message_read(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCH123",
            project_id="amiga",
            title="Watcher autobridge",
            sender_session_id="codex-live-1",
            target_session_id="gemini-runtime-1",
            sender_agent_id="codex",
        )
        worker_script = root / "watcher_runtime_worker.py"
        output_file = root / "watcher_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-WATCHER",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-WATCH123",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-1",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        self.assertTrue(any(event["event"] == "new_message" for event in watcher_events))
        self.assertTrue(any(event["event"] == "autobridge_dispatch" for event in watcher_events))
        self.assertTrue(any(event["event"] == "autobridge_consumed" and event["message_path"] == message_rel for event in watcher_events))

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertIn(message_rel, inbox["read"])

        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("Watcher autobridge", runtime_payload["message"]["title"])
        session_payload = self.run_cli(root, "show", "--session", "SESSION-WATCHER")
        self.assertIn(message_rel, session_payload["processed_messages"])

    def test_pi_doorbell_wakes_once_without_claiming_acceptance(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "glmpi",
                "display_name": "Glim",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="glmpi",
            chat_id="CHAT-PI-WAKE",
            project_id="amiga",
            title="Pi pointer wake",
            sender_session_id="codex-pi-wake",
            target_session_id="pi-glim-1",
            sender_agent_id="codex",
            repo_targets=["llm-collab"],
            target_binding_id="binding-pi-glim",
            target_binding_generation=1,
        )
        self.seed_binding_ledger(
            root,
            chat_id="CHAT-PI-WAKE",
            agent_id="glmpi",
            binding_id="binding-pi-glim",
            generation=1,
            endpoint_id="endpoint_pi_glim",
            native_session_id="pi-glim-1",
        )
        doorbell = root / "State" / "pi" / "glmpi.pointer"
        pi_cwd = root / "pi-cwd"; pi_cwd.mkdir()
        pi_source = self._write_pi_session_source(root, "pi-glim-1", cwd=str(pi_cwd))
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-PI-GLIM",
            "--agent",
            "glmpi",
            "--project",
            "amiga",
            "--chat",
            "CHAT-PI-WAKE",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "pi",
            "--runtime-session-id",
            "pi-glim-1",
            "--runtime-session-source",
            str(pi_source),
            "--cwd",
            str(pi_cwd),
            "--runtime-command",
            json.dumps([sys.executable, "-c", "pass"]),
        )
        session_path = (
            root
            / "State"
            / "session_autobridge"
            / "sessions"
            / "SESSION-PI-GLIM.json"
        )
        session_payload = json.loads(session_path.read_text())
        session_payload.update(
            {
                "repo_targets": ["llm-collab"],
                "binding_id": "binding-pi-glim",
                "binding_generation": 1,
                "endpoint_id": "endpoint_pi_glim",
            }
        )
        write_json(session_path, session_payload)

        command = [
            sys.executable,
            str(WATCH_INBOX_SCRIPT),
            "--me",
            "glmpi",
            "--max-polls",
            "1",
            "--json",
        ]
        first = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        event_path = (
            root
            / "State"
            / "session_autobridge"
            / "events"
            / "SESSION-PI-GLIM.jsonl"
        )
        wake_path = event_path.parent / "wake" / "SESSION-PI-GLIM.jsonl"
        events_after_wake = event_path.read_text()
        wake_events_after_wake = wake_path.read_text()
        self.assertEqual(
            1,
            sum(
                json.loads(line).get("event") == "pi_inbox_wake"
                for line in wake_events_after_wake.splitlines()
                if line.strip()
            ),
        )
        settled = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        self.assertEqual(events_after_wake, event_path.read_text())
        self.assertEqual(wake_events_after_wake, wake_path.read_text())
        session_autobridge_lib.append_event(
            "SESSION-PI-GLIM", {"event": "session_skipped", "reason": "stopped"}
        )
        self.assertEqual(
            wake_events_after_wake,
            wake_path.read_text(),
            "diagnostic appends must not wake the Pi monitor",
        )
        session_payload = json.loads(session_path.read_text())
        self.assertIn(
            message_rel,
            session_payload.get("processed_messages", []),
            first.stdout
            + first.stderr
            + event_path.read_text(),
        )
        session_payload["processed_messages"].remove(message_rel)
        write_json(session_path, session_payload)
        recovered = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        first_events = [json.loads(line) for line in first.stdout.splitlines() if line.strip()]
        settled_events = [
            json.loads(line) for line in settled.stdout.splitlines() if line.strip()
        ]
        recovered_events = [
            json.loads(line) for line in recovered.stdout.splitlines() if line.strip()
        ]

        # #343: the wake is one durable pi_inbox_wake on the exact-session event
        # log, not a mutable doorbell pointer file (pi_doorbell.py is deleted).
        self.assertFalse(doorbell.exists(), "no pointer file is written any more")
        self.assertTrue(
            any(
                event.get("event") == "pi_inbox_wake"
                and event.get("message_path") == message_rel
                for event in (json.loads(line)
                              for line in event_path.read_text().splitlines() if line.strip())
            ),
            "the durable event log must carry a pi_inbox_wake for the packet",
        )
        self.assertTrue(
            all(
                event.get("event") == "pi_inbox_wake"
                for event in (
                    json.loads(line)
                    for line in wake_path.read_text().splitlines()
                    if line.strip()
                )
            ),
            "the Pi monitor stream must contain wake events only",
        )
        self.assertTrue(
            any(
                event["event"] == "autobridge_wake_signaled"
                and event["message_path"] == message_rel
                for event in first_events
            )
        )
        self.assertFalse(any(event["event"] == "autobridge_consumed" for event in first_events))
        self.assertFalse(
            any(event["event"] == "autobridge_wake_signaled" for event in settled_events)
        )
        self.assertFalse(
            any(event["event"] == "autobridge_wake_signaled" for event in recovered_events)
        )
        inbox = json.loads((root / "agents" / "glmpi" / "inbox.json").read_text())
        self.assertIn(message_rel, inbox["unread"])
        self.assertNotIn(message_rel, inbox["read"])
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            with LedgerStore.open_reader(paths) as store:
                self.assertEqual(
                    (1, 1, 1, 1),
                    store._connection.execute(
                        """
                        SELECT
                          (SELECT count(*) FROM canonical_messages),
                          (SELECT count(*) FROM canonical_deliveries),
                          (SELECT count(*) FROM canonical_delivery_attempts),
                          (SELECT count(*) FROM canonical_delivery_attempt_binding_freezes)
                        """
                    ).fetchone(),
                )

    def _mint_pi_binding_through_lifecycle(
        self,
        root,
        *,
        chat_id,
        project,
        agent_id,
        endpoint_id,
        native_session_id,
        runtime_instance_id,
    ):
        """One active canonical Pi binding via the public lifecycle seam.

        reserve -> consume MINTS the conversation_bindings row (binding_id and
        generation come from the authority), so the test never hand-writes the
        binding identity. Participant, provider registry, and the canonical-write
        gate are the ordinary pre-provisioned prerequisites reserve() demands.
        """
        created = "2026-04-22T00:00:00+00:00"
        expires = "2026-04-22T00:00:30+00:00"
        consumed = "2026-04-22T00:00:10+00:00"
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        subject = LifecycleSubject(
            workspace_id="ws_alpha",
            scope_kind="project",
            scope_identity=project,
            conversation_id=chat_id,
            participant_id="participant_" + agent_id,
            agent_id="agent_" + agent_id,
            endpoint_id=endpoint_id,
            native_session_id=native_session_id,
            runtime_instance_id=runtime_instance_id,
        )
        provider = FakeLifecycleProvider()
        core = SessionLifecycleCore(provider, token_factory=lambda: "token-pi")
        endpoint_home = root / "pi-endpoint-home"
        endpoint_home.mkdir(exist_ok=True)
        runtime_home = bind_runtime_home(endpoint_home)
        repo = root / "pi-repo"
        (repo / "work").mkdir(parents=True, exist_ok=True)
        trusted = TrustedProjectRoot(project, "repo_app", str(repo), str(repo / "work"))
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            writer = LedgerStore.open_writer(paths)
        with writer as store:
            store.record_registry_snapshot(
                workspace_id="ws_alpha",
                registry_revision="sha256:" + "a" * 64,
                registry_source_sha256="a" * 64,
                captured_at_utc=created,
                workspace_snapshot_json=json.dumps(
                    {"workspace_id": "ws_alpha", "projects": [project]}
                ),
                project_snapshots={
                    project: json.dumps(
                        {"project_id": project, "canonical" + "_" + "writes": True}
                    )
                },
                source_snapshots={project: {}},
            )
            descriptor = provider.descriptor()
            store._connection.execute(
                """
                INSERT OR IGNORE INTO lifecycle_provider_registry
                (
                    workspace_id, provider_id, provider_revision, trust_class,
                    supported_operations_json, challenge_algorithm,
                    challenge_ttl_seconds, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ws_alpha",
                    descriptor["provider_id"],
                    descriptor["provider_revision"],
                    descriptor["trust_class"],
                    descriptor["supported_operations_json"],
                    descriptor["challenge_algorithm"],
                    descriptor["challenge_ttl_seconds"],
                    created,
                ),
            )
            store._connection.execute(
                """
                INSERT OR IGNORE INTO conversation_participants
                (
                    workspace_id, scope_kind, scope_identity, conversation_id,
                    participant_id, agent_id, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ws_alpha",
                    "project",
                    project,
                    chat_id,
                    "participant_" + agent_id,
                    "agent_" + agent_id,
                    created,
                ),
            )
            challenge = core.reserve(
                store,
                subject,
                runtime_home=runtime_home,
                created_at_utc=created,
                expires_at_utc=expires,
                correlation_id="corr_reserve_pi",
                trusted_project_root=trusted,
            )
            resolved = core.consume(
                store,
                subject,
                challenge,
                runtime_home=runtime_home,
                consumed_at_utc=consumed,
                correlation_id="corr_consume_pi",
                trusted_project_root=trusted,
            )
        self.assertTrue(resolved["resolved"], resolved)
        return resolved

    def test_registration_stamps_canonical_binding_and_pi_dispatches(self):
        # GH-346: normal register -> deliver -> dispatch reaches pi_inbox_wake with
        # no hand-edited session/binding/frontmatter. register resolves the active
        # canonical binding and stamps it; the whole chain consumes that stamp.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "glmpi",
                "display_name": "Glim",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(root, {"id": "codex", "display_name": "Codex"})
        self.create_chat(
            root,
            chat_dir_name="2026-07-29_pi-bind__CHAT-PI-BIND",
            chat_id="CHAT-PI-BIND",
            project_id="amiga",
        )
        resolved = self._mint_pi_binding_through_lifecycle(
            root,
            chat_id="CHAT-PI-BIND",
            project="amiga",
            agent_id="glmpi",
            endpoint_id="endpoint_pi_glim",
            native_session_id="pi-glim-1",
            runtime_instance_id="runtime_pi_glim",
        )
        pi_cwd = root / "pi-cwd"; pi_cwd.mkdir()
        pi_source = self._write_pi_session_source(root, "pi-glim-1", cwd=str(pi_cwd))
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-PI-GLIM",
            "--agent",
            "glmpi",
            "--project",
            "amiga",
            "--chat",
            "CHAT-PI-BIND",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "pi",
            "--runtime-session-id",
            "pi-glim-1",
            "--runtime-session-source",
            str(pi_source),
            "--cwd",
            str(pi_cwd),
            "--runtime-command",
            json.dumps([sys.executable, "-c", "pass"]),
            "--repo-target",
            "llm-collab",
        )
        session_path = (
            root / "State" / "session_autobridge" / "sessions" / "SESSION-PI-GLIM.json"
        )
        session_payload = json.loads(session_path.read_text())
        self.assertEqual(resolved["binding_id"], session_payload.get("binding_id"))
        self.assertEqual(resolved["generation"], session_payload.get("binding_generation"))
        self.assertEqual(resolved["endpoint_id"], session_payload.get("endpoint_id"))
        binding_payload = json.loads(
            (
                root
                / "State"
                / "session_autobridge"
                / "bindings"
                / "amiga"
                / "CHAT-PI-BIND"
                / "glmpi.json"
            ).read_text()
        )
        self.assertEqual(
            session_payload["repo_targets"], binding_payload.get("repo_targets")
        )
        self.assertEqual(
            session_payload["pi_fingerprint"], binding_payload.get("pi_fingerprint")
        )

        deliver = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PI-BIND",
                "--from",
                "codex",
                "--to",
                "glmpi",
                "--project",
                "amiga",
                "--title",
                "Pi canonical wake",
                "--sender-session-id",
                "codex-pi-send",
                "--repo-targets",
                "llm-collab",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="canonical pi packet",
            capture_output=True,
            check=False,
            env=self.subprocess_env(root),
        )
        self.assertEqual(deliver.returncode, 0, deliver.stdout + deliver.stderr)
        packets = sorted(root.glob("Chats/**/*_to-glmpi_*.md"))
        self.assertTrue(packets, "deliver.py wrote no packet")
        frontmatter, _ = parse_frontmatter(packets[-1].read_text())
        self.assertEqual(resolved["binding_id"], frontmatter.get("target_binding_id"))
        self.assertEqual(
            resolved["generation"], frontmatter.get("target_binding_generation")
        )

        subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "glmpi",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        event_path = (
            root / "State" / "session_autobridge" / "events" / "SESSION-PI-GLIM.jsonl"
        )
        events = [
            json.loads(line)
            for line in event_path.read_text().splitlines()
            if line.strip()
        ]
        packet_rel = packets[-1].relative_to(root).as_posix()
        self.assertTrue(
            any(
                event.get("event") == "pi_inbox_wake"
                and event.get("message_path") == packet_rel
                for event in events
            ),
            events,
        )
        self.assertFalse(
            any(
                event.get("reason") == session_autobridge_lib.EXACT_BINDING_REQUIRED_REASON
                for event in events
            ),
            events,
        )

        # Option B: remove the file projection without touching the active ledger
        # row. The exact native-session resolver must derive the binding only in
        # memory, and dispatch must not write those fields back to the session file.
        session_payload.pop("binding_id")
        session_payload.pop("binding_generation")
        session_payload.pop("endpoint_id")
        write_json(session_path, session_payload)
        unbound_deliver = subprocess.run(
            [
                sys.executable,
                str(DELIVER_SCRIPT),
                "--chat",
                "CHAT-PI-BIND",
                "--from",
                "codex",
                "--to",
                "glmpi",
                "--project",
                "amiga",
                "--title",
                "Pi native-resolved wake",
                "--sender-session-id",
                "codex-pi-send-2",
                "--repo-targets",
                "llm-collab",
                "--body-file",
                "-",
            ],
            cwd=root,
            text=True,
            input="native-resolved packet",
            capture_output=True,
            check=False,
            env=self.subprocess_env(root),
        )
        self.assertEqual(0, unbound_deliver.returncode, unbound_deliver.stdout + unbound_deliver.stderr)
        unbound_packet = sorted(root.glob("Chats/**/*_to-glmpi_*.md"))[-1]
        subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "glmpi",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        unbound_events = [
            json.loads(line)
            for line in event_path.read_text().splitlines()
            if line.strip()
        ]
        unbound_packet_rel = unbound_packet.relative_to(root).as_posix()
        self.assertTrue(
            any(
                event.get("event") == "pi_inbox_wake"
                and event.get("message_path") == unbound_packet_rel
                for event in unbound_events
            ),
            unbound_events,
        )
        session_after_dispatch = json.loads(session_path.read_text())
        self.assertIn(
            unbound_packet_rel,
            session_after_dispatch.get("processed_messages", []),
            unbound_events,
        )
        self.assertNotIn("binding_id", session_after_dispatch)
        self.assertNotIn("binding_generation", session_after_dispatch)
        self.assertNotIn("endpoint_id", session_after_dispatch)

    def test_unbound_native_resolver_does_not_cross_route_same_chat_sessions(self):
        binding = {
            "binding_id": "binding-native-a",
            "binding_generation": 4,
            "endpoint_id": "endpoint-native-a",
        }
        session_a = {
            "session_id": "SESSION-NATIVE-A",
            "agent_id": "codex",
            "project_id": "amiga",
            "chat_id": "CHAT-NATIVE-PAIR",
            "wake_strategy": "runtime_trigger",
            "runtime": {"family": "codex_app", "session_id": "native-a"},
        }
        session_b = {
            **session_a,
            "session_id": "SESSION-NATIVE-B",
            "runtime": {"family": "codex_app", "session_id": "native-b"},
        }

        def exact_resolver(project, chat, agent, native):
            self.assertEqual(("amiga", "CHAT-NATIVE-PAIR", "codex"), (project, chat, agent))
            return binding if native == "native-a" else None

        with patch.object(
            session_autobridge_lib,
            "resolve_active_canonical_binding",
            side_effect=exact_resolver,
        ):
            eligible_a, resolved_a = session_autobridge_lib.resolve_session_receive_binding(session_a)
            eligible_b, resolved_b = session_autobridge_lib.resolve_session_receive_binding(session_b)

        self.assertTrue(eligible_a)
        self.assertEqual(binding, resolved_a)
        self.assertTrue(eligible_b)
        self.assertIsNone(resolved_b)
        packet_a = {
            "frontmatter": {
                "project_id": "amiga",
                "chat_id": "CHAT-NATIVE-PAIR",
                "target_session_id": "native-a",
                "target_binding_id": "binding-native-a",
                "target_binding_generation": 4,
            }
        }
        packet_b = {
            "frontmatter": {
                **packet_a["frontmatter"],
                "target_session_id": "native-b",
            }
        }
        self.assertEqual(
            (True, "explicit_target_match"),
            session_autobridge_lib.message_targets_session(
                {**session_a, **(resolved_a or {})}, packet_a
            ),
        )
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(session_b, packet_b),
        )
        self.assertNotIn("binding_id", session_a)
        self.assertNotIn("binding_id", session_b)

    def test_attach_ledger_binding_composes_with_exact_receive_dispatch(self):
        """The attach-shaped lifecycle flow reaches only its exact native session."""
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.add_agent(root, {"id": "codex", "display_name": "Codex"})
        chat_id = "CHAT-F1-E2E"
        native_n = "native-session-n"
        native_m = "native-session-m"
        resolved = self._mint_pi_binding_through_lifecycle(
            root,
            chat_id=chat_id,
            project="amiga",
            agent_id="gemini",
            endpoint_id="endpoint_f1_e2e",
            native_session_id=native_n,
            runtime_instance_id="runtime-f1-e2e",
        )
        worker = root / "gemini_worker.py"
        output = root / "gemini_worker_output.json"
        write(
            worker,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True))",
                ]
            ),
        )
        register_args = [
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            chat_id,
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-source",
            "explicit",
            "--runtime-command",
            json.dumps([sys.executable, str(worker), str(output)]),
            "--repo-target",
            "llm-collab",
        ]
        self.run_cli(root, "register", "--session", "SESSION-F1-N", "--runtime-session-id", native_n, *register_args)
        self.run_cli(root, "register", "--session", "SESSION-F1-M", "--runtime-session-id", native_m, *register_args)

        target_path = self.add_message(
            root,
            agent_id="gemini",
            chat_id=chat_id,
            project_id="amiga",
            title="F1 exact target",
            sender_session_id="codex-f1",
            target_session_id=native_n,
            sender_agent_id="codex",
            repo_targets=["llm-collab"],
            target_binding_id=resolved["binding_id"],
            target_binding_generation=resolved["generation"],
            packet_slug="target",
        )
        generic_path = self.add_message(
            root,
            agent_id="gemini",
            chat_id=chat_id,
            project_id="amiga",
            title="F1 generic packet",
            sender_session_id="codex-f1-generic",
            sender_agent_id="codex",
            repo_targets=["llm-collab"],
            packet_slug="generic",
        )

        watcher = subprocess.run(
            [sys.executable, str(WATCH_INBOX_SCRIPT), "--me", "gemini", "--max-polls", "1", "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        self.assertNotIn("autobridge_binding_refused", watcher.stdout)
        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertIn(target_path, inbox["read"])
        self.assertIn(generic_path, inbox["unread"])

        session_n = json.loads(
            (root / "State" / "session_autobridge" / "sessions" / "SESSION-F1-N.json").read_text()
        )
        session_m = json.loads(
            (root / "State" / "session_autobridge" / "sessions" / "SESSION-F1-M.json").read_text()
        )
        self.assertNotIn("binding_id", session_n)
        self.assertNotIn("binding_generation", session_n)
        self.assertIn(target_path, session_n["processed_messages"])
        self.assertNotIn(target_path, session_m.get("processed_messages", []))

        events = [
            json.loads(line)
            for line in (
                root
                / "State"
                / "session_autobridge"
                / "events"
                / "SESSION-F1-N.jsonl"
            ).read_text().splitlines()
            if line.strip()
        ]
        dispatch = next(
            event
            for event in events
            if event.get("event") == "message_dispatched"
            and event.get("message_path") == target_path
        )
        self.assertTrue(dispatch["canonical_materialization_result"]["resolved"])
        self.assertTrue(dispatch["canonical_materialization_result"]["created"])
        self.assertTrue(
            any(
                event.get("event") == "autobridge_consumed"
                and event.get("message_path") == target_path
                for event in (
                    json.loads(line)
                    for line in watcher.stdout.splitlines()
                    if line.strip()
                )
            )
        )

        # A session with no native identity remains on the legacy generic route;
        # the otherwise-unbound native-M session does not.
        truly_unbound = {
            "session_id": "SESSION-F1-UNBOUND",
            "agent_id": "gemini",
            "project_id": "amiga",
            "chat_id": chat_id,
            "mode": "notify",
            "wake_strategy": "notify",
            "runtime": {},
        }
        generic_message = {"frontmatter": {"project_id": "amiga", "chat_id": chat_id}}
        self.assertEqual(
            (True, "broadcast_or_agent_scoped"),
            session_autobridge_lib.message_targets_session(truly_unbound, generic_message),
        )
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(session_m, generic_message),
        )

    def test_pi_registration_fails_closed_without_a_canonical_binding(self):
        # GH-346/#378: with a valid fingerprint source but no resolvable binding and
        # incomplete provisioning inputs, register must refuse rather than publish a
        # Pi session every later packet would reject. (The bare no-native-context path
        # is now unreachable because a fingerprinted pi register always carries --cwd.)
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "glmpi",
                "display_name": "Glim",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        pi_cwd = root / "pi-cwd"; pi_cwd.mkdir()
        pi_source = self._write_pi_session_source(root, "pi-glim-unprov", cwd=str(pi_cwd))
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "register",
                "--session",
                "SESSION-PI-UNPROVISIONED",
                "--agent",
                "glmpi",
                "--project",
                "amiga",
                "--chat",
                "CHAT-PI-NOBIND",
                "--mode",
                "auto-read",
                "--wake-strategy",
                "runtime_trigger",
                "--runtime-family",
                "pi",
                "--runtime-session-id",
                "pi-glim-unprov",
                "--runtime-session-source",
                str(pi_source),
                "--cwd",
                str(pi_cwd),
                "--runtime-command",
                json.dumps([sys.executable, "-c", "pass"]),
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_provisioning_incomplete", done.stderr)
        session_path = (
            root
            / "State"
            / "session_autobridge"
            / "sessions"
            / "SESSION-PI-UNPROVISIONED.json"
        )
        self.assertFalse(session_path.exists(), "no session may be published on refusal")

    def test_pi_provision_returns_the_committed_binding_without_reopening_the_ledger(self):
        args = SimpleNamespace(
            project="amiga", chat="CHAT-PI", agent="glmpi",
            endpoint_id="endpoint-pi", runtime_instance_id="pi-web",
            cwd="/tmp/work", repo_targets=["app"],
        )
        canonical = {
            "binding_id": "binding-new",
            "binding_generation": 2,
            "endpoint_id": "endpoint-pi",
        }
        with patch.object(
            session_autobridge_cli,
            "provision_pi_canonical_binding",
            return_value=canonical,
        ), patch.object(
            session_autobridge_cli,
            "resolve_active_canonical_binding",
            side_effect=AssertionError("ledger reopened after commit"),
        ):
            self.assertEqual(
                canonical,
                session_autobridge_cli._provision_pi_binding_or_refuse(
                    args,
                    {"home": "/tmp/pi-home"},
                    "pi-new",
                    predecessor={"binding_id": "binding-old", "generation": 1},
                    actor_id="SESSION-NEW",
                ),
            )

    def test_compaction_continuity_preserves_claude_file_binding(self):
        # GH-457 proof 1: a Claude continuation re-registers the SAME native
        # session through the supported CLI supersession path. Claude owns the
        # file binding, not a canonical ledger binding, so preserve the file's
        # stable scope/native identity and original bind time instead of asserting
        # Pi-only binding_id/binding_generation fields.
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        chat_id = "CHAT-CLAUDE-COMPACTION"
        self.create_chat(
            root,
            chat_dir_name="2026-08-02_claude_compaction__CHAT-CLAUDE-COMPACTION",
            chat_id=chat_id,
            project_id="amiga",
        )
        sessions = root / "State" / "session_autobridge" / "sessions"
        binding_path = (
            root
            / "State"
            / "session_autobridge"
            / "bindings"
            / "amiga"
            / chat_id
            / "claude.json"
        )
        native_session_id = "CLAUDE-NATIVE-COMPACTION"
        common = (
            "--agent", "claude",
            "--project", "amiga",
            "--chat", chat_id,
            "--mode", "notify",
            "--runtime-family", "claude_app",
            "--runtime-session-id", native_session_id,
            "--runtime-session-source", "first_read",
        )
        self.run_cli(
            root,
            "register",
            "--session", "SESSION-CLAUDE-COMPACTION-OLD",
            *common,
        )
        before = json.loads(binding_path.read_text())
        self.assertEqual(
            {
                "project_id": "amiga",
                "chat_id": chat_id,
                "agent_id": "claude",
                "runtime_family": "claude_app",
                "runtime_session_id": native_session_id,
            },
            {key: before[key] for key in (
                "project_id", "chat_id", "agent_id",
                "runtime_family", "runtime_session_id",
            )},
        )
        original_bound_at = before["bound_at_utc"]

        self.run_cli(
            root,
            "register",
            "--session", "SESSION-CLAUDE-COMPACTION-NEW",
            *common,
            "--supersedes-session", "SESSION-CLAUDE-COMPACTION-OLD",
        )

        retired = json.loads(
            (sessions / "SESSION-CLAUDE-COMPACTION-OLD.json").read_text()
        )
        successor = json.loads(
            (sessions / "SESSION-CLAUDE-COMPACTION-NEW.json").read_text()
        )
        after = json.loads(binding_path.read_text())
        self.assertEqual("superseded", retired["status"])
        self.assertEqual(
            "SESSION-CLAUDE-COMPACTION-NEW", retired["superseded_by"]
        )
        self.assertEqual(native_session_id, successor["runtime"]["session_id"])
        self.assertEqual(native_session_id, after["runtime_session_id"])
        self.assertEqual(original_bound_at, after["bound_at_utc"])
        self.assertEqual(
            {key: before[key] for key in (
                "project_id", "chat_id", "agent_id", "runtime_family",
                "runtime_session_id", "runtime_session_source", "runtime_home",
                "bound_at_utc",
            )},
            {key: after[key] for key in (
                "project_id", "chat_id", "agent_id", "runtime_family",
                "runtime_session_id", "runtime_session_source", "runtime_home",
                "bound_at_utc",
            )},
        )

    def test_pi_registration_refuses_a_foreign_native_session(self):
        # GH-346 P1: the participant's binding is minted for one native session.
        # Registering a different --runtime-session-id must not inherit it (that
        # would wake the wrong native session behind a passing fence).
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "glmpi",
                "display_name": "Glim",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self._mint_pi_binding_through_lifecycle(
            root,
            chat_id="CHAT-PI-BIND",
            project="amiga",
            agent_id="glmpi",
            endpoint_id="endpoint_pi_glim",
            native_session_id="pi-glim-1",
            runtime_instance_id="runtime_pi_glim",
        )
        pi_cwd = root / "pi-cwd"; pi_cwd.mkdir()
        pi_source = self._write_pi_session_source(root, "pi-glim-2", cwd=str(pi_cwd))
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "register",
                "--session",
                "SESSION-PI-FOREIGN",
                "--agent",
                "glmpi",
                "--project",
                "amiga",
                "--chat",
                "CHAT-PI-BIND",
                "--mode",
                "auto-read",
                "--wake-strategy",
                "runtime_trigger",
                "--runtime-family",
                "pi",
                "--runtime-session-id",
                "pi-glim-2",
                "--runtime-session-source",
                str(pi_source),
                "--cwd",
                str(pi_cwd),
                "--runtime-command",
                json.dumps([sys.executable, "-c", "pass"]),
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_binding_native_session_mismatch", done.stderr)
        session_path = (
            root
            / "State"
            / "session_autobridge"
            / "sessions"
            / "SESSION-PI-FOREIGN.json"
        )
        self.assertFalse(session_path.exists(), "no session may be published on mismatch")

    def _seed_pi_registry_snapshot(self, root, projects):
        """Seed ONLY the daemon-owned registry snapshot (with the canonical-write
        gate) for `projects`. #378 register mints the provider/participant/binding;
        this never hand-writes them."""
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        gate = "canonical" + "_" + "writes"
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            writer = LedgerStore.open_writer(paths)
        with writer as store:
            store.record_registry_snapshot(
                workspace_id="ws_alpha",
                registry_revision="sha256:" + "a" * 64,
                registry_source_sha256="a" * 64,
                captured_at_utc="2026-04-22T00:00:00+00:00",
                workspace_snapshot_json=json.dumps(
                    {"workspace_id": "ws_alpha", "projects": list(projects)}
                ),
                project_snapshots={
                    p: json.dumps({"project_id": p, gate: True}) for p in projects
                },
                source_snapshots={p: {} for p in projects},
            )

    def _write_pi_session_source(
        self, root, native, *, cwd="/pi/cwd", provider="pi-provider",
        model_id="m1", thinking_level="high", header_version=3, extra_lines=(),
    ) -> Path:
        """A minimal valid native Pi session .jsonl (header + model_change +
        thinking_level_change), for the fingerprint pin/compare seam."""
        lines = [
            {"type": "session", "version": header_version, "id": native,
             "timestamp": "2026-07-29T00:00:00.000Z", "cwd": cwd},
            {"type": "model_change", "id": "mc1", "parentId": None,
             "timestamp": "2026-07-29T00:00:00.001Z", "provider": provider, "modelId": model_id},
            {"type": "thinking_level_change", "id": "tl1", "parentId": "mc1",
             "timestamp": "2026-07-29T00:00:00.002Z", "thinkingLevel": thinking_level},
            *extra_lines,
        ]
        path = root / f"pi-source-{native}.jsonl"
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def _register_pi(
        self,
        root,
        *,
        session,
        project,
        chat,
        native,
        endpoint,
        runtime_instance,
        cwd,
        home,
        repo_target,
        agent="glmpi",
        session_source=None,
        supersedes=None,
        expect_fingerprint=None,
        check=True,
    ):
        if session_source is None:
            session_source = self._write_pi_session_source(root, native, cwd=str(cwd))
        argv = [
            sys.executable, str(SCRIPT_PATH), "register",
            "--session", session, "--agent", agent,
            "--project", project, "--chat", chat,
            "--mode", "auto-read", "--wake-strategy", "runtime_trigger",
            "--runtime-family", "pi", "--runtime-session-id", native,
            "--runtime-session-source", str(session_source),
            "--runtime-command", json.dumps([sys.executable, "-c", "pass"]),
            "--endpoint-id", endpoint, "--runtime-instance-id", runtime_instance,
            "--cwd", str(cwd), "--runtime-home", str(home),
            "--repo-target", repo_target, "--json",
        ]
        if supersedes is not None:
            argv += ["--supersedes-session", supersedes]
        if expect_fingerprint is not None:
            provider, model, thinking = expect_fingerprint
            for flag, value in (
                ("--expect-pi-provider", provider),
                ("--expect-pi-model", model),
                ("--expect-pi-thinking", thinking),
            ):
                if value is not None:
                    argv += [flag, value]
        done = subprocess.run(
            argv,
            cwd=root, text=True, capture_output=True,
            env=self.subprocess_env(root), check=False,
        )
        if check:
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return done

    def _session_json(self, root, session):
        return (
            root / "State" / "session_autobridge" / "sessions" / f"{session}.json"
        )

    def test_pi_lifecycle_deactivates_only_the_exact_native_session(self):
        root = self.make_workspace()
        sessions = root / "State" / "session_autobridge" / "sessions"
        write_json(
            sessions / "SESSION-PI-A.json",
            {
                "session_id": "SESSION-PI-A",
                "agent_id": "glmpi",
                "status": "active",
                "runtime": {"family": "pi", "session_id": "native-a"},
            },
        )
        write_json(
            sessions / "SESSION-PI-B.json",
            {
                "session_id": "SESSION-PI-B",
                "agent_id": "relay",
                "status": "active",
                "runtime": {"family": "pi", "session_id": "native-b"},
            },
        )

        result = self.run_cli(
            root, "deactivate-pi", "--native-session-id", "native-a"
        )

        self.assertEqual(["SESSION-PI-A"], result["deactivated_sessions"])
        self.assertEqual(
            "stopped", json.loads((sessions / "SESSION-PI-A.json").read_text())["status"]
        )
        self.assertEqual(
            "active", json.loads((sessions / "SESSION-PI-B.json").read_text())["status"]
        )
        self.assertEqual(
            [],
            self.run_cli(
                root, "deactivate-pi", "--native-session-id", "native-a"
            )["deactivated_sessions"],
        )

        stale = {
            "session_id": "SESSION-PI-A",
            "agent_id": "glmpi",
            "status": "active",
            "runtime": {"family": "pi", "session_id": "native-a"},
            "processed_messages": ["late"],
        }
        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "SESSION_WRITE_LOCK",
            root / "State" / "session_autobridge" / ".session-write.lock",
        ), self.assertRaisesRegex(
            ValueError, "refusing to resurrect stopped session"
        ):
            session_autobridge_lib.save_session(stale)
        self.assertEqual(
            "stopped", json.loads((sessions / "SESSION-PI-A.json").read_text())["status"]
        )
        with patch.object(
            session_autobridge_lib, "SESSIONS_DIR", sessions
        ), patch.object(
            session_autobridge_lib,
            "SESSION_WRITE_LOCK",
            root / "State" / "session_autobridge" / ".session-write.lock",
        ):
            session_autobridge_lib.save_session(stale, allow_reactivation=True)
        self.assertEqual(
            "active", json.loads((sessions / "SESSION-PI-A.json").read_text())["status"]
        )

    def test_pi_lifecycle_extension_deactivates_from_symlink(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is required to execute the Pi extension")
        if subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module", "-e", ""],
            text=True,
            capture_output=True,
        ).returncode:
            self.skipTest("Node must support TypeScript type stripping")
        extension = REPO_ROOT / "pi-extensions" / "llm-collab-lifecycle.ts"
        installed = Path(tempfile.mkdtemp(prefix="pi-extension-", dir="/tmp")) / extension.name
        installed.symlink_to(extension)
        program = """
            import { pathToFileURL } from "node:url";
            const handlers = {};
            const calls = [];
            const pi = {
              on(name, handler) { handlers[name] = handler; },
              async exec(command, args, options) {
                calls.push({command, args, options});
                return {code: 0, stdout: "", stderr: ""};
              },
            };
            const extension = await import(pathToFileURL(process.argv[1]).href);
            extension.default(pi);
            const ctx = {sessionManager: {getSessionId: () => "native-exact"}};
            await handlers.session_start({}, ctx);
            await handlers.session_before_switch({}, ctx);
            await handlers.session_before_fork({}, ctx);
            await handlers.session_shutdown({}, ctx);
            process.stdout.write(JSON.stringify({
              calls,
              cli: extension.resolveCli(pathToFileURL(process.argv[2]).href),
            }));
        """
        result = subprocess.run(
            [
                node,
                "--experimental-strip-types",
                "--input-type=module",
                "-e",
                program,
                str(extension),
                str(installed),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        output = json.loads(result.stdout)
        self.assertEqual(str(REPO_ROOT / "bin" / "llm-collab"), output["cli"])
        calls = output["calls"]
        self.assertEqual(4, len(calls))
        for call in calls:
            self.assertEqual(str(REPO_ROOT / "bin" / "llm-collab"), call["command"])
            self.assertEqual(
                [
                    "session_autobridge.py",
                    "deactivate-pi",
                    "--native-session-id",
                    "native-exact",
                ],
                call["args"],
            )

    def test_pi_register_provisions_two_scopes_for_one_agent(self):
        # #378: one logical agent registers two different project/chat scopes with
        # two fresh native Pi sessions; each mints its own canonical binding through
        # reserve/consume, no hand-written binding.
        root = self.make_workspace()
        self.add_agent(
            root,
            {"id": "glmpi", "display_name": "Glim",
             "activation": {"type": "cli_session", "watcher_enabled": True}},
        )
        self._seed_pi_registry_snapshot(root, ["amiga", "nuvyr"])
        work = root / "work"; work.mkdir()
        home = root / "pi-home"; home.mkdir()
        self._register_pi(
            root, session="SESSION-PI-A", project="amiga", chat="CHAT-A",
            native="pi-native-a", endpoint="endpoint_native_a", runtime_instance="runtime-a",
            cwd=work, home=home, repo_target="app",
        )
        self._register_pi(
            root, session="SESSION-PI-B", project="nuvyr", chat="CHAT-B",
            native="pi-native-b", endpoint="endpoint_native_b", runtime_instance="runtime-b",
            cwd=work, home=home, repo_target="app",
        )
        a = json.loads(self._session_json(root, "SESSION-PI-A").read_text())
        b = json.loads(self._session_json(root, "SESSION-PI-B").read_text())
        self.assertEqual("endpoint_native_a", a.get("endpoint_id"))
        self.assertEqual("endpoint_native_b", b.get("endpoint_id"))
        for s in (a, b):
            self.assertTrue(s.get("binding_id"), s)
            self.assertEqual(1, s.get("binding_generation"))
        self.assertNotEqual(
            a["binding_id"], b["binding_id"],
            "each native scope must mint a distinct canonical binding",
        )

    def test_pi_register_fails_before_write_on_bad_provisioning(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {"id": "glmpi", "display_name": "Glim",
             "activation": {"type": "cli_session", "watcher_enabled": True}},
        )
        work = root / "work"; work.mkdir()
        home = root / "pi-home"; home.mkdir()
        # No registry snapshot: fail before any write.
        done = self._register_pi(
            root, session="SESSION-PI-NS", project="amiga", chat="CHAT-NS",
            native="pi-native-a", endpoint="endpoint_native_a", runtime_instance="runtime-a",
            cwd=work, home=home, repo_target="app", check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_project_snapshot_required", done.stderr)
        self.assertFalse(self._session_json(root, "SESSION-PI-NS").exists())
        # With a snapshot, a foreign native session for an already-bound scope refuses.
        self._seed_pi_registry_snapshot(root, ["amiga", "nuvyr"])
        self._register_pi(
            root, session="SESSION-PI-A", project="amiga", chat="CHAT-A",
            native="pi-native-a", endpoint="endpoint_native_a", runtime_instance="runtime-a",
            cwd=work, home=home, repo_target="app",
        )
        done = self._register_pi(
            root, session="SESSION-PI-A2", project="amiga", chat="CHAT-A",
            native="pi-native-b", endpoint="endpoint_native_b", runtime_instance="runtime-b",
            cwd=work, home=home, repo_target="app", check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_binding_native_session_mismatch", done.stderr)
        self.assertFalse(self._session_json(root, "SESSION-PI-A2").exists())

    def test_pi_register_refuses_duplicate_native_owner_across_scopes(self):
        # #381 P1: a native session owns at most one mutation-capable binding. A
        # second scope with the SAME endpoint/native/runtime/home must refuse
        # cleanly — stable nonzero reason, no traceback, no legacy session, no
        # second active binding.
        root = self.make_workspace()
        for aid in ("glmpi", "relay"):
            self.add_agent(
                root,
                {"id": aid, "display_name": aid,
                 "activation": {"type": "cli_session", "watcher_enabled": True}},
            )
        self._seed_pi_registry_snapshot(root, ["amiga", "nuvyr"])
        work = root / "work"; work.mkdir()
        home = root / "pi-home"; home.mkdir()
        ident = dict(
            native="pi-native-shared", endpoint="endpoint_native_shared",
            runtime_instance="runtime-shared", cwd=work, home=home, repo_target="app",
        )
        self._register_pi(
            root, session="SESSION-PI-A", agent="glmpi", project="amiga",
            chat="CHAT-A", **ident,
        )
        done = self._register_pi(
            root, session="SESSION-PI-B", agent="relay", project="nuvyr",
            chat="CHAT-B", check=False, **ident,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_native_session_already_bound", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertFalse(self._session_json(root, "SESSION-PI-B").exists())
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            with LedgerStore.open_reader(paths) as store:
                active = store._connection.execute(
                    "SELECT count(*) FROM conversation_bindings WHERE native_session_id = ? "
                    "AND mutation_capable = 1 AND state IN ('active', 'draining')",
                    ("pi-native-shared",),
                ).fetchone()[0]
                pending = store._connection.execute(
                    "SELECT count(*) FROM session_binding_challenges WHERE challenge_state = 'pending'"
                ).fetchone()[0]
        self.assertEqual(1, active, "the duplicate must not create a second active binding")
        self.assertEqual(0, pending, "the refused duplicate must not leave a pending challenge")

    def _active_binding_count(self, root, native):
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            with LedgerStore.open_reader(paths) as store:
                return store._connection.execute(
                    "SELECT count(*) FROM conversation_bindings WHERE native_session_id = ?",
                    (native,),
                ).fetchone()[0]

    def _binding_rows(self, root, project, chat, agent="glmpi"):
        """(native_session_id, generation, state) for a scope, ordered by generation."""
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            with LedgerStore.open_reader(paths) as store:
                return store._connection.execute(
                    "SELECT native_session_id, generation, state FROM conversation_bindings "
                    "WHERE scope_identity = ? AND conversation_id = ? AND participant_id = ? "
                    "ORDER BY generation",
                    (project, chat, "participant_" + agent),
                ).fetchall()

    def _make_pi_replacement_workspace(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {"id": "glmpi", "display_name": "Glim",
             "activation": {"type": "cli_session", "watcher_enabled": True}},
        )
        self._seed_pi_registry_snapshot(root, ["amiga", "nuvyr"])
        work = root / "work"; work.mkdir()
        home = root / "pi-home"; home.mkdir()
        return root, work, home

    def test_pi_native_session_replacement_swaps_owner_via_rebind(self):
        # #319: a brand-new Pi native session replaces the current owner of the SAME
        # scope. --supersedes-session names the exact active owner; the rebind
        # supersedes it and activates the new native at generation+1, and the old
        # legacy session record follows to superseded. Amiga + non-Amiga.
        for project, chat in (("amiga", "CHAT-RA"), ("nuvyr", "CHAT-RN")):
            root, work, home = self._make_pi_replacement_workspace()
            self._register_pi(
                root, session="SESSION-OLD", project=project, chat=chat,
                native="pi-old", endpoint="endpoint_old", runtime_instance="rt-old",
                cwd=work, home=home, repo_target="app",
            )
            done = self._register_pi(
                root, session="SESSION-NEW", project=project, chat=chat,
                native="pi-new", endpoint="endpoint_new", runtime_instance="rt-new",
                cwd=work, home=home, repo_target="app", supersedes="SESSION-OLD",
            )
            self.assertEqual(
                [("pi-old", 1, "superseded"), ("pi-new", 2, "active")],
                self._binding_rows(root, project, chat),
                "rebind must supersede the old owner and activate the new native at gen+1",
            )
            new = json.loads(self._session_json(root, "SESSION-NEW").read_text())
            self.assertEqual(2, new.get("binding_generation"))
            self.assertEqual("endpoint_new", new.get("endpoint_id"))
            self.assertEqual(
                new["binding_id"],
                json.loads(done.stdout).get("binding", {}).get("binding_id"),
            )
            old = json.loads(self._session_json(root, "SESSION-OLD").read_text())
            self.assertEqual("superseded", old.get("status"))
            self.assertEqual("SESSION-NEW", old.get("superseded_by"))

    def test_pi_native_session_replacement_refusals_preserve_predecessor(self):
        # #319: replacement is authorized ONLY by --supersedes-session naming the exact
        # active owner. A bare mismatch (no supersedes) and a supersedes naming a
        # non-owner both refuse with the predecessor left active and no new session.
        root, work, home = self._make_pi_replacement_workspace()
        self._register_pi(
            root, session="SESSION-OLD", project="amiga", chat="CHAT-R",
            native="pi-old", endpoint="endpoint_old", runtime_instance="rt-old",
            cwd=work, home=home, repo_target="app",
        )
        self._register_pi(
            root, session="SESSION-OTHER", project="nuvyr", chat="CHAT-O",
            native="pi-other", endpoint="endpoint_other", runtime_instance="rt-other",
            cwd=work, home=home, repo_target="app",
        )
        baseline = self._binding_rows(root, "amiga", "CHAT-R")
        self.assertEqual([("pi-old", 1, "active")], baseline)

        # (a) No --supersedes-session: the native fence refuses, predecessor untouched.
        done = self._register_pi(
            root, session="SESSION-NEW", project="amiga", chat="CHAT-R",
            native="pi-new", endpoint="endpoint_new", runtime_instance="rt-new",
            cwd=work, home=home, repo_target="app", check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_binding_native_session_mismatch", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertFalse(self._session_json(root, "SESSION-NEW").exists())
        self.assertEqual(baseline, self._binding_rows(root, "amiga", "CHAT-R"))

        # (b) --supersedes-session names a session that is NOT the current owner.
        done = self._register_pi(
            root, session="SESSION-NEW2", project="amiga", chat="CHAT-R",
            native="pi-new2", endpoint="endpoint_new2", runtime_instance="rt-new2",
            cwd=work, home=home, repo_target="app", supersedes="SESSION-OTHER", check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_replacement_predecessor_mismatch", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertFalse(self._session_json(root, "SESSION-NEW2").exists())
        self.assertEqual(baseline, self._binding_rows(root, "amiga", "CHAT-R"))
        # The predecessor legacy record must NOT be retired by a refused replacement.
        old = json.loads(self._session_json(root, "SESSION-OLD").read_text())
        self.assertNotEqual("superseded", old.get("status"))
        self.assertIsNone(old.get("superseded_by"))

        # (c) The registering session becomes the canonical transition actor. Refuse
        # an actor the ledger cannot store before reserve/consume writes anything.
        invalid_actor = "S" * 129
        done = self._register_pi(
            root, session=invalid_actor, project="amiga", chat="CHAT-R",
            native="pi-new3", endpoint="endpoint_new3", runtime_instance="rt-new3",
            cwd=work, home=home, repo_target="app", supersedes="SESSION-OLD", check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("canonical_replacement_actor_invalid", done.stderr)
        self.assertFalse(self._session_json(root, invalid_actor).exists())
        self.assertEqual(baseline, self._binding_rows(root, "amiga", "CHAT-R"))

    def test_pi_replacement_pure_refusal_runs_before_the_canonical_swap(self):
        # #319 ordering: every pure refusal (here: an unreadable existing binding)
        # runs BEFORE the rebind. A refusal must leave the canonical predecessor active
        # and the old legacy record un-retired, with no new session — never a swapped
        # owner with no replacement written.
        root, work, home = self._make_pi_replacement_workspace()
        self._register_pi(
            root, session="SESSION-OLD", project="amiga", chat="CHAT-R",
            native="pi-old", endpoint="endpoint_old", runtime_instance="rt-old",
            cwd=work, home=home, repo_target="app",
        )
        baseline = self._binding_rows(root, "amiga", "CHAT-R")
        binding_file = (
            root / "State" / "session_autobridge" / "bindings" / "amiga" / "CHAT-R" / "glmpi.json"
        )
        # Oversized (> MAX_BINDING_BYTES) makes the existing binding unreadable, which is
        # a hard pre-swap refusal (a malformed one is treated as absent, not a refusal).
        binding_file.write_text("x" * (256 * 1024 + 1))
        done = self._register_pi(
            root, session="SESSION-NEW", project="amiga", chat="CHAT-R",
            native="pi-new", endpoint="endpoint_new", runtime_instance="rt-new",
            cwd=work, home=home, repo_target="app", supersedes="SESSION-OLD", check=False,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertNotIn("Traceback", done.stderr)
        self.assertFalse(self._session_json(root, "SESSION-NEW").exists())
        self.assertEqual(
            baseline, self._binding_rows(root, "amiga", "CHAT-R"),
            "a pre-swap refusal must leave the canonical predecessor unchanged",
        )
        self.assertNotEqual(
            "superseded",
            json.loads(self._session_json(root, "SESSION-OLD").read_text()).get("status"),
        )

    def test_pi_replacement_refuses_binding_growth_before_the_canonical_swap(self):
        root, work, home = self._make_pi_replacement_workspace()
        self._register_pi(
            root, session="SESSION-OLD", project="amiga", chat="CHAT-R",
            native="pi-old", endpoint="endpoint_old", runtime_instance="rt-old",
            cwd=work, home=home, repo_target="app",
        )
        baseline = self._binding_rows(root, "amiga", "CHAT-R")
        binding_file = (
            root / "State" / "session_autobridge" / "bindings" / "amiga" / "CHAT-R" / "glmpi.json"
        )
        binding = json.loads(binding_file.read_text())
        source = self._write_pi_session_source(root, "pi-new-long", cwd=str(work))
        endpoint = "endpoint_" + "a" * 119
        binding["padding"] = ""
        empty = json.dumps(binding, indent=2, sort_keys=True)
        binding["padding"] = "x" * (256 * 1024 - len(empty.encode("utf-8")) - 1)
        binding_file.write_text(json.dumps(binding, indent=2, sort_keys=True))
        self.assertLessEqual(binding_file.stat().st_size, 256 * 1024)

        done = self._register_pi(
            root, session="SESSION-NEW-LONG", project="amiga", chat="CHAT-R",
            native="pi-new-long", endpoint=endpoint, runtime_instance="rt-new-long",
            cwd=work, home=home, repo_target="app", supersedes="SESSION-OLD", check=False,
            session_source=source,
        )
        self.assertNotEqual(0, done.returncode)
        self.assertIn("binding exceeds", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(baseline, self._binding_rows(root, "amiga", "CHAT-R"))
        self.assertNotEqual(
            "superseded",
            json.loads(self._session_json(root, "SESSION-OLD").read_text()).get("status"),
        )

    def test_pi_replacement_does_not_prepare_again_after_the_swap(self):
        args = SimpleNamespace(
            session="SESSION-NEW", agent="glmpi", project="amiga", chat="CHAT-R",
            repo_targets=["app"], mode="auto-read", status="active",
            wake_strategy="runtime_trigger", lease_owner=None, ttl_seconds=3600,
            allowed_actions=[], runtime_family="pi", runtime_session_id="pi-new",
            runtime_session_source="/tmp/pi-new.jsonl", runtime_home="/tmp/pi-home",
            runtime_command=None, runtime_timeout=30, endpoint_id="endpoint-new",
            runtime_instance_id="pi-web", cwd="/tmp/work",
            supersedes_session="SESSION-OLD",
        )
        settled = {
            "session_id": "SESSION-NEW",
            "agent_id": "glmpi",
            "project_id": "amiga",
            "chat_id": "CHAT-R",
            "runtime": {"family": "pi", "session_id": "pi-new"},
            "updated_utc": "2026-07-29T09:00:00+00:00",
        }
        with patch.object(
            session_autobridge_cli, "get_agent", return_value={"activation": {}}
        ), patch.object(
            session_autobridge_cli,
            "read_pi_session_fingerprint",
            return_value={
                "provider": "zai", "model": "glm-5.2", "thinking_level": "max",
                "cwd": "/tmp/work",
            },
        ), patch.object(
            session_autobridge_cli,
            "resolve_active_canonical_binding",
            side_effect=session_autobridge_cli.CanonicalBindingNativeMismatch(
                canonical_native_session_id="pi-old",
                requested_runtime_session_id="pi-new",
            ),
        ), patch.object(
            session_autobridge_cli,
            "_authorize_pi_replacement_or_refuse",
            return_value={"binding_id": "binding-old", "generation": 1},
        ), patch.object(
            session_autobridge_cli,
            "_replace_pi_binding_or_refuse",
            return_value={
                "binding_id": "binding-new", "binding_generation": 2,
                "endpoint_id": "endpoint-new",
            },
        ), patch.object(
            session_autobridge_cli,
            "prepare_session_write",
            side_effect=[(settled, json.dumps(settled)), AssertionError("prepared twice")],
        ) as prepare, patch.object(
            session_autobridge_cli, "existing_binding_snapshot_or_refuse", return_value={}
        ), patch.object(
            session_autobridge_cli, "plan_superseded_retirement", return_value=None
        ), patch.object(
            session_autobridge_cli, "update_binding_from_session", return_value=None
        ), patch.object(session_autobridge_cli, "save_session") as save:
            session_autobridge_cli.register_session(args)

        prepare.assert_called_once()
        self.assertEqual("binding-new", save.call_args.kwargs["prepared"][0]["binding_id"])

    def test_pi_register_requires_and_persists_complete_fingerprint(self):
        # #378 fingerprint: registration reads + pins the exact native source's
        # provider/model/thinking/cwd BEFORE any canonical write. A missing/incomplete
        # source or a cwd that is not the registering checkout refuses with NO session
        # and NO canonical binding (fail before publishing authority). Amiga + non-Amiga.
        for project, chat, native in (
            ("amiga", "CHAT-FP-A", "pi-fp-a"),
            ("nuvyr", "CHAT-FP-N", "pi-fp-n"),
        ):
            root = self.make_workspace()
            self.add_agent(
                root,
                {"id": "glmpi", "display_name": "Glim",
                 "activation": {"type": "cli_session", "watcher_enabled": True}},
            )
            self._seed_pi_registry_snapshot(root, [project])
            work = root / "work"; work.mkdir()
            home = root / "pi-home"; home.mkdir()
            with self.subTest(project=project, case="missing-source"):
                miss = self._register_pi(
                    root, session="SESSION-FP-MISS", project=project, chat=chat,
                    native=native, endpoint="endpoint_native", runtime_instance="rt-fp",
                    cwd=work, home=home, repo_target="app",
                    session_source=str(root / "does-not-exist.jsonl"), check=False,
                )
                self.assertNotEqual(0, miss.returncode)
                self.assertIn("canonical_pi_fingerprint_required", miss.stderr)
                self.assertFalse(self._session_json(root, "SESSION-FP-MISS").exists())
                self.assertEqual(
                    0, self._active_binding_count(root, native),
                    "a fingerprint refusal must not leave a canonical binding",
                )
            with self.subTest(project=project, case="cwd-mismatch"):
                other = self._write_pi_session_source(
                    root, native, cwd="/some/other/checkout", provider="p", model_id="m",
                    thinking_level="high",
                )
                bad = self._register_pi(
                    root, session="SESSION-FP-CWD", project=project, chat=chat,
                    native=native, endpoint="endpoint_native", runtime_instance="rt-fp",
                    cwd=work, home=home, repo_target="app", session_source=str(other),
                    check=False,
                )
                self.assertNotEqual(0, bad.returncode)
                self.assertIn("canonical_pi_cwd_mismatch", bad.stderr)
                self.assertFalse(self._session_json(root, "SESSION-FP-CWD").exists())
                self.assertEqual(
                    0, self._active_binding_count(root, native),
                    "a cwd-mismatch refusal must not leave a canonical binding",
                )
            with self.subTest(project=project, case="valid-source"):
                source = self._write_pi_session_source(
                    root, native, cwd=str(work), provider="pinned-prov",
                    model_id="pinned-model", thinking_level="high",
                )
                partial = self._register_pi(
                    root, session="SESSION-FP-PARTIAL", project=project, chat=chat,
                    native=native, endpoint="endpoint_native", runtime_instance="rt-fp",
                    cwd=work, home=home, repo_target="app", session_source=str(source),
                    expect_fingerprint=("pinned-prov", None, None), check=False,
                )
                self.assertNotEqual(0, partial.returncode)
                self.assertIn("canonical_pi_fingerprint_precondition_incomplete", partial.stderr)
                self.assertFalse(self._session_json(root, "SESSION-FP-PARTIAL").exists())
                mismatch = self._register_pi(
                    root, session="SESSION-FP-MISMATCH", project=project, chat=chat,
                    native=native, endpoint="endpoint_native", runtime_instance="rt-fp",
                    cwd=work, home=home, repo_target="app", session_source=str(source),
                    expect_fingerprint=("pinned-prov", "different-model", "high"),
                    check=False,
                )
                self.assertNotEqual(0, mismatch.returncode)
                self.assertIn("canonical_pi_fingerprint_precondition_mismatch", mismatch.stderr)
                self.assertFalse(self._session_json(root, "SESSION-FP-MISMATCH").exists())
                self.assertEqual(0, self._active_binding_count(root, native))
                self._register_pi(
                    root, session="SESSION-FP-OK", project=project, chat=chat,
                    native=native, endpoint="endpoint_native", runtime_instance="rt-fp",
                    cwd=work, home=home, repo_target="app", session_source=str(source),
                    expect_fingerprint=("pinned-prov", "pinned-model", "high"),
                )
                record = json.loads(self._session_json(root, "SESSION-FP-OK").read_text())
                self.assertEqual(
                    {"cwd": str(work), "provider": "pinned-prov",
                     "model_id": "pinned-model", "thinking_level": "high"},
                    record.get("pi_fingerprint"),
                )
                self.assertEqual(1, self._active_binding_count(root, native))

    def test_pi_dispatch_drift_or_missing_source_emits_no_wake_and_preserves_unread(self):
        # #378 drift guard: after registering with a matching source, a drifted or
        # missing native source emits NO pi_inbox_wake and leaves the packet unread
        # (not marked processed). Amiga + non-Amiga.
        for project, chat, native in (
            ("amiga", "CHAT-DRIFT-A", "pi-drift-a"),
            ("nuvyr", "CHAT-DRIFT-N", "pi-drift-n"),
        ):
            root = self.make_workspace()
            for aid in ("glmpi", "codex"):
                self.add_agent(
                    root,
                    {"id": aid, "display_name": aid,
                     "activation": {"type": "cli_session", "watcher_enabled": True}},
                )
            self._seed_pi_registry_snapshot(root, [project])
            self.create_chat(root, chat_dir_name=f"{chat}-dir", chat_id=chat, project_id=project)
            work = root / "work"; work.mkdir()
            home = root / "pi-home"; home.mkdir()
            source = self._write_pi_session_source(
                root, native, cwd=str(work), provider="p0", model_id="m0", thinking_level="high",
            )
            self._register_pi(
                root, session="SESSION-DRIFT", project=project, chat=chat, native=native,
                endpoint="endpoint_drift", runtime_instance="rt-drift", cwd=work, home=home,
                repo_target="app", session_source=str(source),
            )
            deliver = subprocess.run(
                [
                    sys.executable, str(DELIVER_SCRIPT), "--chat", chat, "--from", "codex",
                    "--to", "glmpi", "--project", project, "--title", "drift probe",
                    "--sender-session-id", "codex-drift", "--repo-targets", "app",
                    "--body-file", "-",
                ],
                cwd=root, text=True, input="drift probe", capture_output=True,
                check=False, env=self.subprocess_env(root),
            )
            self.assertEqual(0, deliver.returncode, deliver.stdout + deliver.stderr)
            event_path = root / "State" / "session_autobridge" / "events" / "SESSION-DRIFT.jsonl"

            def poll_and_events():
                subprocess.run(
                    [sys.executable, str(WATCH_INBOX_SCRIPT), "--me", "glmpi",
                     "--max-polls", "1", "--json"],
                    cwd=root, text=True, capture_output=True,
                    env=self.subprocess_env(root), check=True,
                )
                if not event_path.exists():
                    return []
                return [json.loads(l) for l in event_path.read_text().splitlines() if l.strip()]

            # Drift: rewrite the exact source with a different thinking level.
            self._write_pi_session_source(
                root, native, cwd=str(work), provider="p0", model_id="m0", thinking_level="low",
            )
            events = poll_and_events()
            self.assertFalse(
                any(e.get("event") == "pi_inbox_wake" for e in events),
                f"[{project}] drift must emit no pi_inbox_wake: {events}",
            )
            inbox = json.loads((root / "agents" / "glmpi" / "inbox.json").read_text())
            self.assertTrue(inbox["unread"], f"[{project}] drifted packet must stay unread")
            session_record = json.loads(
                self._session_json(root, "SESSION-DRIFT").read_text()
            )
            self.assertEqual(
                [], session_record.get("processed_messages", []),
                f"[{project}] drift must not mark the packet processed",
            )

            # Missing source: same fail-closed behavior.
            source.unlink()
            events = poll_and_events()
            self.assertFalse(
                any(e.get("event") == "pi_inbox_wake" for e in events),
                f"[{project}] missing source must emit no pi_inbox_wake: {events}",
            )

    def test_pi_runtime_refuses_without_an_exact_bound_attempt(self):
        session = {
            "session_id": "SESSION-PI-UNBOUND",
            "agent_id": "glmpi",
            "project_id": "amiga",
            "chat_id": "CHAT-PI-UNBOUND",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {
                "family": "pi",
                "session_id": "pi-unbound",
                "command": [sys.executable, "-c", "pass"],
            },
        }
        message = {
            "path": "Chats/2026-07-27/packet.md",
            "frontmatter": {"target_session_id": "pi-unbound"},
        }
        runtime_trigger = Mock(return_value={"returncode": 0})
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib,
            "execute_runtime_trigger",
            new=runtime_trigger,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-PI-UNBOUND")

        runtime_trigger.assert_not_called()
        self.assertEqual("exact_binding_required", result["actions"][0]["reason"])

        bound_session = {
            **session,
            "binding_id": "binding-pi",
            "binding_generation": 1,
        }
        bound_message = {
            **message,
            "frontmatter": {
                **message["frontmatter"],
                "target_binding_id": "binding-pi",
                "target_binding_generation": 1,
            },
        }
        with self._dispatch_patch_context(
            bound_session,
            [bound_message],
        ), patch.object(
            session_autobridge_lib,
            "materialize_selected_runtime_packet",
            return_value={
                "resolved": True,
                "materialized": False,
                "created": False,
                "gate": "disabled",
                "canonical_write_started": False,
            },
        ), patch.object(
            session_autobridge_lib,
            "execute_runtime_trigger",
            new=runtime_trigger,
        ):
            result = session_autobridge_lib.dispatch_session("SESSION-PI-UNBOUND")

        runtime_trigger.assert_not_called()
        self.assertEqual("pull_pending", result["actions"][0]["reason"])

    def test_unbound_livecraft_runtime_is_held_without_marking_packet_processed(self):
        session = {
            "session_id": "SESSION-LIVECRAFT-UNBOUND",
            "agent_id": "glmpi",
            "project_id": "llm-collab",
            "chat_id": "CHAT-LIVECRAFT-UNBOUND",
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "endpoint_id": "endpoint_pi_livecraft_local",
            "runtime": {
                "family": "pi",
                "session_id": "livecraft-native-unbound",
                "command": [sys.executable, "-c", "pass"],
            },
        }
        message = {
            "path": "Chats/livecraft-unbound/packet.md",
            "frontmatter": {"target_session_id": "livecraft-native-unbound"},
        }
        runtime_trigger = Mock(return_value={"returncode": 0})
        mark_processed = Mock()
        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib, "execute_runtime_trigger", runtime_trigger
        ), patch.object(
            session_autobridge_lib, "mark_message_processed", mark_processed
        ):
            result = session_autobridge_lib.dispatch_session(session["session_id"])

        self.assertEqual("exact_binding_required", result["actions"][0]["reason"])
        runtime_trigger.assert_not_called()
        mark_processed.assert_not_called()

    def test_watch_inbox_default_off_empty_ledger_preserves_legacy_runtime_trigger(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-EMPTY-LEDGER",
            project_id="amiga",
            title="Empty ledger gate",
            sender_session_id="codex-empty-ledger",
            target_session_id="gemini-runtime-empty-ledger",
            sender_agent_id="codex",
            repo_targets=["llm-collab"],
            target_binding_id="binding-empty-ledger",
            target_binding_generation=1,
            packet_slug="empty-ledger",
        )
        self.seed_binding_ledger(
            root,
            chat_id="CHAT-EMPTY-LEDGER",
            agent_id="gemini",
            binding_id="binding-empty-ledger",
            generation=1,
            endpoint_id="endpoint_gemini_runtime_empty_ledger",
            native_session_id="gemini-runtime-empty-ledger",
        )
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            with LedgerStore.open_writer(paths) as store:
                store.record_registry_snapshot(
                    workspace_id="ws_alpha",
                    registry_revision="sha256:" + "b" * 64,
                    registry_source_sha256="b" * 64,
                    captured_at_utc="2026-04-22T00:00:01+00:00",
                    workspace_snapshot_json=json.dumps(
                        {"workspace_id": "ws_alpha", "projects": ["nuvyr"]}
                    ),
                    project_snapshots={"nuvyr": json.dumps({"project_id": "nuvyr"})},
                    source_snapshots={"nuvyr": {}},
                )

        worker_script = root / "empty_ledger_runtime_worker.py"
        output_file = root / "empty_ledger_runtime_result.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-EMPTY-LEDGER",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-EMPTY-LEDGER",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-empty-ledger",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )
        session_path = (
            root
            / "State"
            / "session_autobridge"
            / "sessions"
            / "SESSION-EMPTY-LEDGER.json"
        )
        session_payload = json.loads(session_path.read_text())
        session_payload.update(
            {
                "repo_targets": ["llm-collab"],
                "binding_id": "binding-empty-ledger",
                "binding_generation": 1,
                "endpoint_id": "endpoint_gemini_runtime_empty_ledger",
            }
        )
        write_json(session_path, session_payload)

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env={**self.subprocess_env(root), "LLM_COLLAB_CANONICAL_CONTROL": ""},
            check=True,
        )
        watcher_events = [
            json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()
        ]
        self.assertTrue(any(event.get("event") == "autobridge_dispatch" for event in watcher_events))
        self.assertTrue(
            any(
                event.get("event") == "autobridge_consumed"
                and event.get("detail") == message_rel
                for event in watcher_events
            )
        )
        session_events = [
            json.loads(line)
            for line in (
                root
                / "State"
                / "session_autobridge"
                / "events"
                / "SESSION-EMPTY-LEDGER.jsonl"
            ).read_text().splitlines()
            if line.strip()
        ]
        dispatch = next(
            event
            for event in session_events
            if event.get("event") == "message_dispatched"
            and event.get("message_path") == message_rel
        )
        self.assertEqual(
            {
                "resolved": True,
                "materialized": False,
                "gate": "disabled",
                "registry_revision": None,
            },
            {
                key: dispatch["canonical_materialization_result"][key]
                for key in ("resolved", "materialized", "gate", "registry_revision")
            },
        )
        self.assertTrue(output_file.exists())
        self.assertNotIn("route_ambiguous", watcher_result.stdout)
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            with LedgerStore.open_reader(paths) as store:
                self.assertEqual(
                    (0, 0, 0),
                    store._connection.execute(
                        "SELECT count(*), (SELECT count(*) FROM canonical_deliveries), "
                        "(SELECT count(*) FROM canonical_delivery_attempts) "
                        "FROM canonical_messages"
                    ).fetchone(),
                )

    def test_runtime_receive_requires_explicit_target_session(self):
        runtime_session = {
            "session_id": "SESSION-RUNTIME",
            "agent_id": "gemini",
            "wake_strategy": "runtime_trigger",
            "runtime": {
                "family": "gemini_cli",
                "session_id": "gemini-runtime-1",
            },
        }
        notify_session = {
            **runtime_session,
            "wake_strategy": "notify",
        }
        message = {"frontmatter": {}}

        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(runtime_session, message),
        )
        self.assertEqual(
            (True, "broadcast_or_agent_scoped"),
            session_autobridge_lib.message_targets_session(notify_session, message),
        )

    def test_runtime_receive_rejects_repo_binding_and_generation_mismatch(self):
        session = {
            "session_id": "SESSION-RUNTIME",
            "agent_id": "gemini",
            "project_id": "amiga",
            "chat_id": "CHAT-BIND-SAFE",
            "wake_strategy": "runtime_trigger",
            "runtime": {
                "family": "gemini_cli",
                "session_id": "gemini-runtime-1",
            },
            "repo_targets": ["llm-collab"],
            "binding_id": "binding_current",
            "binding_generation": 7,
        }
        base_frontmatter = {
            "project_id": "amiga",
            "chat_id": "CHAT-BIND-SAFE",
            "target_session_id": "gemini-runtime-1",
            "repo_targets": ["llm-collab"],
            "target_binding_id": "binding_current",
            "target_binding_generation": 7,
        }

        self.assertEqual(
            (True, "explicit_target_match"),
            session_autobridge_lib.message_targets_session(
                session, {"frontmatter": dict(base_frontmatter)}
            ),
        )

        cases = [
            (
                {"repo_targets": ["amiga"]},
                session_autobridge_lib.ROUTE_AMBIGUOUS_REASON,
            ),
            (
                {"repo_targets": ["llm-collab", "amiga"]},
                session_autobridge_lib.ROUTE_AMBIGUOUS_REASON,
            ),
            (
                {"target_binding_id": "binding_other"},
                session_autobridge_lib.ROUTE_AMBIGUOUS_REASON,
            ),
            (
                {"target_binding_generation": 6},
                session_autobridge_lib.STALE_GENERATION_REASON,
            ),
        ]
        for override, reason in cases:
            with self.subTest(override=override):
                frontmatter = {**base_frontmatter, **override}
                self.assertEqual(
                    (False, reason),
                    session_autobridge_lib.message_targets_session(
                        session, {"frontmatter": frontmatter}
                    ),
                )

        missing_binding_session = {**session}
        missing_binding_session.pop("binding_id")
        missing_binding_session.pop("binding_generation")
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(
                missing_binding_session,
                {"frontmatter": dict(base_frontmatter)},
            ),
        )
        missing_generation_session = {**session}
        missing_generation_session.pop("binding_generation")
        self.assertEqual(
            (False, session_autobridge_lib.STALE_GENERATION_REASON),
            session_autobridge_lib.message_targets_session(
                missing_generation_session,
                {"frontmatter": dict(base_frontmatter)},
            ),
        )

    def test_runtime_receive_rejects_wildcard_session_for_targeted_packet(self):
        scoped_session = {
            "session_id": "SESSION-SCOPED",
            "agent_id": "gemini",
            "project_id": "amiga",
            "chat_id": "CHAT-BIND-SAFE",
            "wake_strategy": "runtime_trigger",
            "runtime": {
                "family": "gemini_cli",
                "session_id": "gemini-runtime-1",
            },
            "repo_targets": ["llm-collab"],
            "binding_id": "binding_current",
            "binding_generation": 7,
        }
        wildcard_session = {
            "session_id": "SESSION-WILDCARD",
            "agent_id": "gemini",
            "wake_strategy": "runtime_trigger",
            "runtime": {
                "family": "gemini_cli",
                "session_id": "gemini-runtime-1",
            },
        }
        message = {
            "frontmatter": {
                "project_id": "amiga",
                "chat_id": "CHAT-BIND-SAFE",
                "target_session_id": "gemini-runtime-1",
                "repo_targets": ["llm-collab"],
                "target_binding_id": "binding_current",
                "target_binding_generation": 7,
            }
        }

        self.assertEqual(
            (True, "explicit_target_match"),
            session_autobridge_lib.message_targets_session(scoped_session, message),
        )
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(wildcard_session, message),
        )
        missing_scope_message = {
            "frontmatter": {
                "target_session_id": "gemini-runtime-1",
                "repo_targets": ["llm-collab"],
                "target_binding_id": "binding_current",
                "target_binding_generation": 7,
            }
        }
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(
                wildcard_session, missing_scope_message
            ),
        )

    def test_repo_scope_is_unscoped_or_strict_subset_everywhere(self):
        self.assertEqual(
            (True, "unscoped"),
            session_autobridge_lib.repo_scope_matches(None, None),
        )
        self.assertEqual(
            (True, "repo_scope_match"),
            session_autobridge_lib.repo_scope_matches(
                ["llm-collab", "amiga"],
                ["llm-collab"],
                subscriber_project="amiga",
                packet_project="amiga",
            ),
        )
        for subscriber, packet in (
            (["llm-collab"], ["llm-collab", "amiga"]),
            (["llm-collab"], None),
            (["llm-collab"], []),
            (["llm-collab"], [" llm-collab"]),
            (["llm-collab", "llm-collab"], ["llm-collab"]),
        ):
            with self.subTest(subscriber=subscriber, packet=packet):
                self.assertEqual(
                    (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
                    session_autobridge_lib.repo_scope_matches(
                        subscriber,
                        packet,
                        subscriber_project="amiga",
                        packet_project="amiga",
                    ),
                )

        general_session = {
            "session_id": "SESSION-GENERAL",
            "agent_id": "gemini",
            "project_id": "amiga",
            "wake_strategy": "notify",
            "repo_targets": ["llm-collab"],
        }
        self.assertEqual(
            (True, "broadcast_or_agent_scoped"),
            session_autobridge_lib.message_targets_session(
                general_session,
                {"frontmatter": {"project_id": "amiga", "repo_targets": ["llm-collab"]}},
            ),
        )
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(
                general_session,
                {"frontmatter": {"project_id": "amiga", "repo_targets": ["llm-collab", "amiga"]}},
            ),
        )

    def test_repo_scope_requires_exact_project_match(self):
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.repo_scope_matches(
                ["llm-collab"],
                ["llm-collab"],
                subscriber_project="amiga",
                packet_project="nuvyr",
            ),
        )
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.repo_scope_matches(
                ["llm-collab"],
                ["llm-collab"],
                subscriber_project=None,
                packet_project="amiga",
            ),
        )

    def test_dispatch_invocation_scope_is_anded_and_not_persisted(self):
        session = {
            "session_id": "SESSION-TRANSIENT-SCOPE",
            "agent_id": "gemini",
            "project_id": "amiga",
            "repo_targets": ["llm-collab"],
            "mode": "auto-read",
            "wake_strategy": "runtime_trigger",
            "runtime": {"session_id": "runtime-transient"},
        }
        message = {
            "path": "Chats/transient/matched.md",
            "frontmatter": {
                "project_id": "amiga",
                "repo_targets": ["llm-collab"],
            },
        }
        self.assertEqual(
            (False, session_autobridge_lib.ROUTE_AMBIGUOUS_REASON),
            session_autobridge_lib.message_targets_session(
                session,
                message,
                invocation_repo_targets=["amiga"],
            ),
        )
        self.assertEqual(
            (True, "broadcast_or_agent_scoped"),
            session_autobridge_lib.message_targets_session(
                session,
                message,
                invocation_repo_targets=["llm-collab"],
            ),
        )

        saved_sessions = []

        def record_saved(saved, _path, *, prepared=None):
            saved_sessions.append(dict(saved))

        with self._dispatch_patch_context(session, [message]), patch.object(
            session_autobridge_lib,
            "mark_message_processed",
            side_effect=record_saved,
        ):
            result = session_autobridge_lib.dispatch_session(
                "SESSION-TRANSIENT-SCOPE",
                project_id="amiga",
                repo_targets=["llm-collab"],
            )

        self.assertEqual(1, len(result["actions"]))
        self.assertTrue(saved_sessions)
        self.assertNotIn("_invocation_repo_targets", saved_sessions[0])
        self.assertNotIn("_invocation_repo_targets", session)

    def test_dispatch_reports_scoped_refusal_without_consuming_packet(self):
        session = {
            "session_id": "SESSION-REFUSAL",
            "agent_id": "gemini",
            "project_id": "amiga",
            "repo_targets": ["llm-collab"],
            "status": "parked",
            "mode": "notify",
            "wake_strategy": "none",
        }
        message = {
            "path": "Chats/refusal/wrong.md",
            "frontmatter": {
                "project_id": "amiga",
                "repo_targets": ["amiga"],
            },
        }
        with patch.object(
            session_autobridge_lib, "load_session", return_value=session
        ), patch.object(
            session_autobridge_lib, "bounded_unread_messages", return_value=[message]
        ), patch.object(session_autobridge_lib, "append_event") as append_event:
            result = session_autobridge_lib.dispatch_session("SESSION-REFUSAL")

        self.assertEqual(
            [{"path": message["path"], "reason": "route_ambiguous"}],
            result["repo_scope_refused"],
        )
        self.assertTrue(
            any(
                call.args[1]["event"] == "message_skipped"
                and call.args[1]["message_path"] == message["path"]
                for call in append_event.call_args_list
            )
        )

    def test_watcher_filters_new_message_notifications_by_project_and_repo(self):
        inbox_path = Path(tempfile.mkdtemp(prefix="lca-watch-notify-")) / "inbox.json"
        inbox_path.write_text("{}")
        messages = [
            {
                "path": "Chats/notify/matched.md",
                "frontmatter": {
                    "project_id": "amiga",
                    "repo_targets": ["llm-collab"],
                },
            },
            {
                "path": "Chats/notify/wrong.md",
                "frontmatter": {
                    "project_id": "amiga",
                    "repo_targets": ["amiga"],
                },
            },
        ]
        stdout = StringIO()
        with patch.object(watch_inbox_lib, "agent_ids", return_value=["gemini"]), patch.object(
            watch_inbox_lib, "agent_inbox_path", return_value=inbox_path
        ), patch.object(
            watch_inbox_lib,
            "load_agent_inbox",
            return_value={"unread": [message["path"] for message in messages]},
        ), patch.object(
            watch_inbox_lib, "get_unread_messages", return_value=messages
        ), patch.object(
            watch_inbox_lib, "dispatch_autobridge", return_value=[]
        ), patch.object(watch_inbox_lib, "send_notification") as notify, redirect_stdout(stdout):
            with patch.object(
                sys,
                "argv",
                [
                    "watch_inbox.py",
                    "--me",
                    "gemini",
                    "--project",
                    "amiga",
                    "--repo-target",
                    "llm-collab",
                    "--notify",
                    "--poll-seconds",
                    "1",
                    "--max-polls",
                    "1",
                    "--json",
                ],
            ):
                watch_inbox_lib.main()

        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            ["Chats/notify/matched.md"],
            [event["detail"] for event in events if event["event"] == "new_message"],
        )
        notify.assert_called_once()

    def test_watcher_exact_session_emits_only_that_sessions_packets(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "claude",
                "display_name": "Claude",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-CLAUDE-A",
            "--agent",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CLAUDE-A",
            "--repo-target",
            "app",
            "--mode",
            "notify",
            "--runtime-family",
            "claude_app",
            "--runtime-session-id",
            "runtime-claude-a",
            "--runtime-session-source",
            "test_fixture",
        )
        packet_a = self.add_message(
            root,
            agent_id="claude",
            chat_id="CHAT-CLAUDE-A",
            project_id="amiga",
            title="For A",
            target_session_id="runtime-claude-a",
            repo_targets=["app"],
            packet_slug="for-a",
        )
        self.add_message(
            root,
            agent_id="claude",
            chat_id="CHAT-CLAUDE-A",
            project_id="amiga",
            title="Wrong repo",
            target_session_id="runtime-claude-a",
            repo_targets=["docs"],
            packet_slug="wrong-repo",
        )
        self.add_message(
            root,
            agent_id="claude",
            chat_id="CHAT-CLAUDE-A",
            project_id="amiga",
            title="For B",
            target_session_id="runtime-claude-b",
            repo_targets=["app"],
            packet_slug="for-b",
        )

        command = [
            sys.executable,
            str(WATCH_INBOX_SCRIPT),
            "--me",
            "claude",
            "--project",
            "amiga",
            "--chat",
            "CHAT-CLAUDE-A",
            "--session",
            "SESSION-CLAUDE-A",
            "--repo-target",
            "app",
            "--max-polls",
            "1",
            "--json",
        ]
        result = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            env={
                **self.subprocess_env(root),
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-claude-a",
            },
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            [packet_a],
            [event["detail"] for event in events if event["event"] == "new_message"],
            result.stdout + result.stderr,
        )
        self.assertFalse(
            any(event["event"].startswith("autobridge_") for event in events)
        )
        self.assertEqual(
            3,
            len(json.loads((root / "agents" / "claude" / "inbox.json").read_text())["unread"]),
        )
        stale = subprocess.run(
            command,
            cwd=root,
            text=True,
            capture_output=True,
            env={
                **self.subprocess_env(root),
                "LLM_COLLAB_READER_RUNTIME_ID": "runtime-claude-b",
            },
        )
        self.assertEqual(75, stale.returncode)
        self.assertIn("exact_session_runtime_mismatch", stale.stdout)

    def test_exact_watcher_stays_armed_and_emits_a_later_packet(self):
        old = {
            "path": "Chats/exact/old.md",
            "frontmatter": {
                "project_id": "llm-collab",
                "repo_targets": ["llm-collab"],
            },
        }
        new = {
            "path": "Chats/exact/new.md",
            "frontmatter": {
                "project_id": "llm-collab",
                "repo_targets": ["llm-collab"],
            },
        }
        stdout = StringIO()
        argv = [
            "watch_inbox.py",
            "--me",
            "claude",
            "--project",
            "llm-collab",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--repo-target",
            "llm-collab",
            "--skip-existing",
            "--max-polls",
            "2",
            "--poll-seconds",
            "1",
            "--json",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            watch_inbox_lib, "agent_ids", return_value=["claude"]
        ), patch.object(
            watch_inbox_lib,
            "exact_session_messages",
            side_effect=[[old], [old], [old, new]],
        ), patch.object(watch_inbox_lib.time, "sleep"), redirect_stdout(stdout):
            watch_inbox_lib.main()

        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(
            [new["path"]],
            [event["detail"] for event in events if event["event"] == "new_message"],
        )

    def test_exact_watcher_retries_a_transient_read_failure(self):
        message = {
            "path": "Chats/exact/recovered.md",
            "frontmatter": {
                "project_id": "llm-collab",
                "repo_targets": ["llm-collab"],
            },
        }
        stdout = StringIO()
        argv = [
            "watch_inbox.py",
            "--me",
            "claude",
            "--project",
            "llm-collab",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--repo-target",
            "llm-collab",
            "--max-polls",
            "2",
            "--poll-seconds",
            "1",
            "--json",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            watch_inbox_lib, "agent_ids", return_value=["claude"]
        ), patch.object(
            watch_inbox_lib,
            "exact_session_messages",
            side_effect=[ValueError("temporary read failure"), [message]],
        ), patch.object(watch_inbox_lib.time, "sleep"), redirect_stdout(stdout):
            watch_inbox_lib.main()

        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(["error", "new_message"], [event["event"] for event in events])
        self.assertEqual(message["path"], events[-1]["detail"])

    def test_exact_watcher_retries_a_transient_baseline_failure(self):
        old = {
            "path": "Chats/exact/old.md",
            "frontmatter": {
                "project_id": "llm-collab",
                "repo_targets": ["llm-collab"],
            },
        }
        new = {
            "path": "Chats/exact/new.md",
            "frontmatter": {
                "project_id": "llm-collab",
                "repo_targets": ["llm-collab"],
            },
        }
        stdout = StringIO()
        argv = [
            "watch_inbox.py",
            "--me",
            "claude",
            "--project",
            "llm-collab",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--repo-target",
            "llm-collab",
            "--skip-existing",
            "--max-polls",
            "1",
            "--poll-seconds",
            "1",
            "--json",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            watch_inbox_lib, "agent_ids", return_value=["claude"]
        ), patch.object(
            watch_inbox_lib,
            "exact_session_messages",
            side_effect=[ValueError("temporary baseline failure"), [old], [old, new]],
        ), patch.object(watch_inbox_lib.time, "sleep"), redirect_stdout(stdout):
            watch_inbox_lib.main()

        events = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(["error", "new_message"], [event["event"] for event in events])
        self.assertEqual(new["path"], events[-1]["detail"])

    def test_exact_watcher_uses_only_the_bounded_registry_reader(self):
        argv = [
            "watch_inbox.py",
            "--me",
            "claude",
            "--project",
            "llm-collab",
            "--chat",
            "CHAT-EXACT",
            "--session",
            "SESSION-EXACT",
            "--max-polls",
            "1",
            "--json",
        ]
        with patch.object(sys, "argv", argv), patch.object(
            watch_inbox_lib, "agent_ids", side_effect=AssertionError("unbounded read")
        ), patch.object(
            watch_inbox_lib, "config_get", return_value=1
        ), patch.object(
            watch_inbox_lib, "exact_session_messages", return_value=[]
        ):
            watch_inbox_lib.main()

    def test_exact_watcher_refuses_repository_scope_mismatch(self):
        args = object()
        with patch.object(
            watch_inbox_lib, "exact_read_session", return_value={}
        ), patch.object(
            watch_inbox_lib,
            "exact_read_messages",
            return_value=(
                [],
                [
                    {
                        "path": "Chats/exact/wrong.md",
                        "reason": "binding_mismatch",
                        "repo_scope_only": False,
                    }
                ],
            ),
        ):
            with self.assertRaisesRegex(
                watch_inbox_lib.ExactWatcherAuthorityError,
                "exact_session_repo_scope_refused",
            ):
                watch_inbox_lib.exact_session_messages(args)

    def test_exact_watcher_skips_only_repository_scope_refusals(self):
        args = object()
        messages = [{"path": "Chats/exact/valid.md"}]
        with patch.object(
            watch_inbox_lib, "exact_read_session", return_value={}
        ), patch.object(
            watch_inbox_lib,
            "exact_read_messages",
            return_value=(
                messages,
                [
                    {
                        "path": "Chats/exact/wrong.md",
                        "reason": "route_ambiguous",
                        "repo_scope_only": True,
                    }
                ],
            ),
        ):
            self.assertEqual(
                messages,
                watch_inbox_lib.exact_session_messages(args),
            )

    def test_registration_retires_the_session_it_supersedes(self):
        """GH-373: end to end through the CLI, not the helper in isolation. Under
        the #324 rule an active session no longer expires, so registering a
        continuation must retire the record it supersedes, or the old session
        stays dispatchable — two writers on one thread. Proves the wiring, so
        removing the retire call from register_session fails here.
        """
        root = self.make_workspace()
        for agent in ("codex", "claude"):
            self.add_agent(root, {"id": agent, "display_name": agent.title(),
                                  "activation": {"type": "cli_session", "watcher_enabled": True}})
        self.create_chat(root, chat_dir_name="2026-07-29_sup__CHAT-SUP",
                         chat_id="CHAT-SUP", project_id="amiga")
        sessions = root / "State" / "session_autobridge" / "sessions"
        self.run_cli(
            root, "register", "--session", "SESSION-OLD", "--agent", "claude",
            "--project", "amiga", "--chat", "CHAT-SUP", "--mode", "notify",
            "--status", "active",
            "--runtime-family", "claude_app", "--runtime-session-id", "THREAD-OLD",
            "--runtime-session-source", "first_read",
        )
        old = json.loads((sessions / "SESSION-OLD.json").read_text())
        self.assertIn(old["status"], {"active", "parked"})  # live before supersession

        self.run_cli(
            root, "register", "--session", "SESSION-NEW", "--agent", "claude",
            "--project", "amiga", "--chat", "CHAT-SUP", "--mode", "notify",
            "--runtime-family", "claude_app", "--runtime-session-id", "THREAD-NEW",
            "--runtime-session-source", "first_read",
            "--supersedes-session", "SESSION-OLD",
        )
        retired = json.loads((sessions / "SESSION-OLD.json").read_text())
        self.assertEqual("superseded", retired["status"])
        self.assertEqual("SESSION-NEW", retired["superseded_by"])
        new = json.loads((sessions / "SESSION-NEW.json").read_text())
        self.assertIn(new["status"], {"active", "parked"})  # the replacement is live, not retired

    def test_a_refused_continuation_does_not_retire_the_predecessor(self):
        """GH-373 finding 2: every refusal preflight runs BEFORE the retire, so a
        continuation that cannot complete never destroys the valid predecessor.
        Proved by ordering — the binding preflight raises and retire must not have
        been reached.
        """
        agent = {"id": "claude", "activation": {"type": "cli_session"}}
        args = SimpleNamespace(
            session="SESSION-NEW", agent="claude", project="amiga", chat="CHAT-X",
            mode="notify", status="active", wake_strategy="none", allowed_actions=[],
            lease_owner=None, ttl_seconds=3600, runtime_family="claude_app",
            runtime_session_id="THREAD-NEW", runtime_session_source="first_read",
            runtime_home=None, supersedes_session="SESSION-OLD", runtime_command=None,
            runtime_timeout=None, repo_targets=None,
        )
        with patch.object(session_autobridge_cli, "get_agent",
                          return_value=agent), \
             patch.object(session_autobridge_cli, "load_session", return_value={}), \
             patch.object(session_autobridge_cli, "prepare_session_write",
                          return_value=({"session_id": "SESSION-NEW"}, "{}")), \
             patch.object(session_autobridge_cli, "existing_binding_snapshot_or_refuse",
                          side_effect=RuntimeError("binding unreadable")), \
             patch.object(session_autobridge_cli, "retire_superseded_session") as retire:
            with self.assertRaises(RuntimeError):
                session_autobridge_cli.register_session(args)
        retire.assert_not_called()

    def test_register_persists_explicit_repo_subscription(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        session = self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-REPO-SCOPED",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--repo-target",
            "llm-collab",
            "--repo-target",
            "amiga",
        )
        self.assertEqual(["llm-collab", "amiga"], session["repo_targets"])

    def test_watcher_passes_explicit_repo_subscription_to_dispatch(self):
        with patch.object(
            watch_inbox_lib, "autobridge_session_ids", return_value=["SESSION-REPO"]
        ), patch.object(
            watch_inbox_lib, "load_session", return_value={},
        ), patch.object(
            watch_inbox_lib, "session_has_exact_canonical_binding", return_value=True,
        ), patch.object(
            watch_inbox_lib,
            "dispatch_session",
            return_value={"actions": []},
        ) as dispatch:
            self.assertEqual(
                [],
                watch_inbox_lib.dispatch_autobridge(
                    "gemini",
                    json_output=True,
                    project_id="amiga",
                    repo_targets=["llm-collab"],
                ),
            )
        dispatch.assert_called_once_with(
            "SESSION-REPO", project_id="amiga", repo_targets=["llm-collab"]
        )

    def test_watcher_repo_scope_recheck_blocks_wrong_packet_before_read(self):
        with tempfile.TemporaryDirectory(prefix="lca-watch-repo-") as temp_dir:
            root = Path(temp_dir)
            packet_path = root / "Chats" / "wrong.md"
            write(
                packet_path,
                "\n".join(
                    [
                        "---",
                        "repo_targets: [amiga]",
                        "---",
                        "",
                        "wrong repository",
                    ]
                ),
            )
            action = {
                "effective_action": "runtime_trigger",
                "message_path": "Chats/wrong.md",
                "runtime_result": {"returncode": 0},
            }
            with patch.object(
                watch_inbox_lib, "ROOT", root
            ), patch.object(
                watch_inbox_lib, "autobridge_session_ids", return_value=["SESSION-REPO"]
            ), patch.object(
                watch_inbox_lib,
                "dispatch_session",
                return_value={"actions": [action]},
            ), patch.object(watch_inbox_lib, "mark_messages_read") as mark_read:
                self.assertEqual(
                    [],
                    watch_inbox_lib.dispatch_autobridge(
                        "gemini", json_output=True, repo_targets=["llm-collab"]
                    ),
                )
            mark_read.assert_not_called()

    def test_watch_inbox_marks_only_binding_matched_runtime_paths(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        matched = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-BIND-SAFE",
            project_id="amiga",
            title="Matched binding",
            target_session_id="gemini-runtime-a",
            repo_targets=["llm-collab"],
            target_binding_id="binding-a",
            target_binding_generation=7,
            sender_agent_id="codex",
            packet_slug="matched",
        )
        wrong_repo = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-BIND-SAFE",
            project_id="amiga",
            title="Wrong repo",
            target_session_id="gemini-runtime-a",
            repo_targets=["amiga"],
            target_binding_id="binding-a",
            target_binding_generation=7,
            sender_agent_id="codex",
            packet_slug="wrong-repo",
        )
        wrong_binding = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-BIND-SAFE",
            project_id="amiga",
            title="Wrong binding",
            target_session_id="gemini-runtime-a",
            repo_targets=["llm-collab"],
            target_binding_id="binding-b",
            target_binding_generation=7,
            sender_agent_id="codex",
            packet_slug="wrong-binding",
        )
        missing_target = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-BIND-SAFE",
            project_id="amiga",
            title="Missing target",
            repo_targets=["llm-collab"],
            target_binding_id="binding-a",
            target_binding_generation=7,
            sender_agent_id="codex",
            packet_slug="missing-target",
        )
        self.seed_binding_ledger(
            root,
            chat_id="CHAT-BIND-SAFE",
            agent_id="gemini",
            binding_id="binding-a",
            generation=7,
            endpoint_id="endpoint_gemini_runtime_a",
            native_session_id="gemini-runtime-a",
        )
        worker_script = root / "binding_scoped_runtime.py"
        output_a = root / "binding_scoped_runtime_a.json"
        output_b = root / "binding_scoped_runtime_b.json"
        output_wildcard = root / "binding_scoped_runtime_wildcard.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        for session_id, runtime_id, output_file in (
            ("SESSION-BIND-A", "gemini-runtime-a", output_a),
            ("SESSION-BIND-B", "gemini-runtime-b", output_b),
        ):
            self.run_cli(
                root,
                "register",
                "--session",
                session_id,
                "--agent",
                "gemini",
                "--project",
                "amiga",
                "--chat",
                "CHAT-BIND-SAFE",
                "--mode",
                "auto-read",
                "--wake-strategy",
                "runtime_trigger",
                "--runtime-family",
                "gemini_cli",
                "--runtime-session-id",
                runtime_id,
                "--runtime-session-source",
                "first_read",
                "--runtime-command",
                json.dumps([sys.executable, str(worker_script), str(output_file)]),
            )
            session_path = root / "State" / "session_autobridge" / "sessions" / f"{session_id}.json"
            session_payload = json.loads(session_path.read_text())
            session_payload["repo_targets"] = ["llm-collab"]
            session_payload["binding_id"] = "binding-a" if session_id.endswith("-A") else "binding-b"
            session_payload["binding_generation"] = 7
            session_payload["endpoint_id"] = (
                "endpoint_gemini_runtime_a"
                if session_id.endswith("-A")
                else "endpoint_gemini_runtime_b"
            )
            write_json(session_path, session_payload)

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-BIND-0-WILDCARD",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-BIND-SAFE",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-a",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_wildcard)]),
        )
        wildcard_path = root / "State" / "session_autobridge" / "sessions" / "SESSION-BIND-0-WILDCARD.json"
        wildcard_payload = json.loads(wildcard_path.read_text())
        wildcard_payload.pop("project_id")
        wildcard_payload.pop("chat_id")
        write_json(wildcard_path, wildcard_payload)

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        watcher_events = [
            json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()
        ]
        consumed = [
            event["message_path"]
            for event in watcher_events
            if event["event"] == "autobridge_consumed"
        ]
        self.assertEqual([matched], consumed)
        self.assertTrue(output_a.exists())
        self.assertFalse(output_b.exists())
        self.assertFalse(output_wildcard.exists())

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([matched], inbox["read"])
        self.assertEqual([wrong_repo, wrong_binding, missing_target], inbox["unread"])

        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            reader = LedgerStore.open_reader(paths)
        with reader as store:
            self.assertEqual(
                (1, 1, 1),
                store._connection.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM canonical_messages),
                      (SELECT count(*) FROM canonical_deliveries),
                      (SELECT count(*) FROM canonical_delivery_attempts)
                    """
                ).fetchone(),
            )

    def test_watch_inbox_recovers_after_bind_before_mark_read_without_duplicate_rows(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-BIND-RECOVER",
            project_id="amiga",
            title="Recover binding",
            target_session_id="gemini-runtime-recover",
            repo_targets=["llm-collab"],
            target_binding_id="binding-recover",
            target_binding_generation=3,
            sender_agent_id="codex",
            packet_slug="recover",
        )
        self.seed_binding_ledger(
            root,
            chat_id="CHAT-BIND-RECOVER",
            agent_id="gemini",
            binding_id="binding-recover",
            generation=3,
            endpoint_id="endpoint_gemini_recover",
            native_session_id="gemini-runtime-recover",
        )
        worker_script = root / "recover_runtime.py"
        output_file = root / "recover_runtime.jsonl"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "path = Path(sys.argv[1])",
                    "previous = path.read_text() if path.exists() else ''",
                    "path.write_text(previous + json.dumps(payload['message']['path']) + '\\n')",
                ]
            ),
        )
        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-BIND-RECOVER",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-BIND-RECOVER",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-recover",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file)]),
        )
        session_path = root / "State" / "session_autobridge" / "sessions" / "SESSION-BIND-RECOVER.json"
        session_payload = json.loads(session_path.read_text())
        session_payload["repo_targets"] = ["llm-collab"]
        session_payload["binding_id"] = "binding-recover"
        session_payload["binding_generation"] = 3
        session_payload["endpoint_id"] = "endpoint_gemini_recover"
        write_json(session_path, session_payload)

        dispatch_only = self.run_cli(root, "dispatch", "--session", "SESSION-BIND-RECOVER")
        self.assertEqual(message_rel, dispatch_only["actions"][0]["message_path"])
        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([message_rel], inbox["unread"])
        self.assertEqual([], inbox["read"])

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        watcher_events = [
            json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()
        ]
        self.assertEqual(
            [message_rel],
            [
                event["message_path"]
                for event in watcher_events
                if event["event"] == "autobridge_consumed"
            ],
        )
        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertEqual([message_rel], inbox["read"])
        paths = LedgerPaths.derive(root / "project-state", "ws_alpha")
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            reader = LedgerStore.open_reader(paths)
        with reader as store:
            first_counts = store._connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM canonical_messages),
                  (SELECT count(*) FROM canonical_deliveries),
                  (SELECT count(*) FROM canonical_delivery_attempts),
                  (SELECT count(*) FROM canonical_delivery_attempt_binding_freezes)
                """
            ).fetchone()
            self.assertEqual((1, 1, 1, 1), first_counts)

        # GH-457 proof 3 (duplicate-event idempotency): replay the same unread
        # filesystem event after the first dispatch/ack cycle. The watcher must
        # recognize the already-settled packet, avoid a second runtime turn, and
        # leave all canonical rows unchanged.
        write_json(
            root / "agents" / "gemini" / "inbox.json",
            {"agent": "gemini", "unread": [message_rel], "read": []},
        )
        replay_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            env=self.subprocess_env(root),
            check=True,
        )
        replay_events = [
            json.loads(line)
            for line in replay_result.stdout.splitlines()
            if line.strip()
        ]
        self.assertNotIn(
            "autobridge_failed",
            [event["event"] for event in replay_events],
        )
        self.assertEqual(
            1,
            len(output_file.read_text().splitlines()),
            "replaying one packet must not trigger a second runtime turn",
        )
        replay_session = json.loads(session_path.read_text())
        self.assertEqual(
            1,
            replay_session["processed_messages"].count(message_rel),
        )
        replay_inbox = json.loads(
            (root / "agents" / "gemini" / "inbox.json").read_text()
        )
        self.assertNotIn(message_rel, replay_inbox["unread"])
        self.assertIn(
            message_rel,
            replay_inbox["read"],
            "replayed duplicate must be recognized and settled, not silently dropped",
        )
        with patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION):
            reader = LedgerStore.open_reader(paths)
        with reader as store:
            replay_counts = store._connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM canonical_messages),
                  (SELECT count(*) FROM canonical_deliveries),
                  (SELECT count(*) FROM canonical_delivery_attempts),
                  (SELECT count(*) FROM canonical_delivery_attempt_binding_freezes)
                """
            ).fetchone()
        self.assertEqual(first_counts, replay_counts)

    def test_watch_inbox_read_state_guard_is_not_in_helpers(self):
        with patch.object(watch_inbox_lib, "autobridge_session_ids", return_value=["SESSION-A"]):
            with patch.object(
                watch_inbox_lib, "load_session", return_value={},
            ), patch.object(
                watch_inbox_lib, "session_has_exact_canonical_binding", return_value=True,
            ), patch.object(
                watch_inbox_lib,
                "dispatch_session",
                return_value={
                    "actions": [
                        {
                            "effective_action": "runtime_trigger",
                            "message_path": "Chats/x/matched.md",
                            "runtime_result": {"returncode": 0},
                        }
                    ]
                },
            ):
                with patch.object(watch_inbox_lib, "mark_messages_read") as mark_read:
                    self.assertEqual(
                        ["Chats/x/matched.md"],
                        watch_inbox_lib.dispatch_autobridge("gemini", json_output=True),
                    )

        mark_read.assert_called_once_with("gemini", ["Chats/x/matched.md"])

    def test_watch_inbox_consumes_unread_in_only_one_overlapping_session(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCHOVERLAP",
            project_id="amiga",
            title="Watcher overlap",
            sender_session_id="codex-live-1",
            target_session_id="session-watcher-a",
            sender_agent_id="codex",
        )
        worker_script = root / "watcher_runtime_overlap.py"
        output_a = root / "watcher_runtime_overlap_a.json"
        output_b = root / "watcher_runtime_overlap_b.json"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "Path(sys.argv[1]).write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        for session_id, output_file in (
            ("SESSION-WATCHER-A", output_a),
            ("SESSION-WATCHER-B", output_b),
        ):
            self.run_cli(
                root,
                "register",
                "--session",
                session_id,
                "--agent",
                "gemini",
                "--project",
                "amiga",
                "--chat",
                "CHAT-WATCHOVERLAP",
                "--mode",
                "auto-read",
                "--wake-strategy",
                "runtime_trigger",
                "--runtime-family",
                "gemini_cli",
                "--runtime-session-id",
                session_id.lower(),
                "--runtime-session-source",
                "first_read",
                "--runtime-command",
                json.dumps([sys.executable, str(worker_script), str(output_file)]),
            )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        consumed = [event for event in watcher_events if event["event"] == "autobridge_consumed"]
        self.assertEqual(1, len(consumed))
        self.assertEqual(message_rel, consumed[0]["message_path"])
        self.assertTrue(output_a.exists() ^ output_b.exists())

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertIn(message_rel, inbox["read"])

        session_a = self.run_cli(root, "show", "--session", "SESSION-WATCHER-A")
        session_b = self.run_cli(root, "show", "--session", "SESSION-WATCHER-B")
        processed_count = sum(
            message_rel in session["processed_messages"]
            for session in (session_a, session_b)
        )
        self.assertEqual(1, processed_count)

    def test_watch_inbox_keeps_message_unread_when_runtime_trigger_fails(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCHFAIL",
            project_id="amiga",
            title="Watcher failure",
            target_session_id="gemini-runtime-fail",
        )
        worker_script = root / "watcher_runtime_fail.py"
        write(
            worker_script,
            "\n".join(
                [
                    "import sys",
                    "sys.exit(7)",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-WATCHER-FAIL",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-WATCHFAIL",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-fail",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script)]),
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        self.assertTrue(any(event["event"] == "autobridge_failed" and event["message_path"] == message_rel for event in watcher_events))

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertIn(message_rel, inbox["unread"])
        self.assertEqual([], inbox["read"])

        session_payload = self.run_cli(root, "show", "--session", "SESSION-WATCHER-FAIL")
        self.assertEqual([], session_payload["processed_messages"])

    def test_watch_inbox_retries_deferred_message_without_new_message(self):
        root = self.make_workspace()
        self.add_agent(
            root,
            {
                "id": "gemini",
                "display_name": "Gemini",
                "activation": {"type": "cli_session", "watcher_enabled": True},
            },
        )
        message_rel = self.add_message(
            root,
            agent_id="gemini",
            chat_id="CHAT-WATCHRETRY",
            project_id="amiga",
            title="Watcher retry",
            target_session_id="gemini-runtime-retry",
        )
        worker_script = root / "watcher_runtime_retry.py"
        output_file = root / "watcher_runtime_retry_result.json"
        marker_file = root / "watcher_runtime_retry_busy"
        write(
            worker_script,
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "payload = json.load(sys.stdin)",
                    "output_file = Path(sys.argv[1])",
                    "marker_file = Path(sys.argv[2])",
                    "if not marker_file.exists():",
                    "    marker_file.write_text('busy')",
                    "    sys.exit(7)",
                    "output_file.write_text(json.dumps(payload, indent=2))",
                ]
            ),
        )

        self.run_cli(
            root,
            "register",
            "--session",
            "SESSION-WATCHER-RETRY",
            "--agent",
            "gemini",
            "--project",
            "amiga",
            "--chat",
            "CHAT-WATCHRETRY",
            "--mode",
            "auto-read",
            "--wake-strategy",
            "runtime_trigger",
            "--runtime-family",
            "gemini_cli",
            "--runtime-session-id",
            "gemini-runtime-retry",
            "--runtime-session-source",
            "first_read",
            "--runtime-command",
            json.dumps([sys.executable, str(worker_script), str(output_file), str(marker_file)]),
        )

        watcher_result = subprocess.run(
            [
                sys.executable,
                str(WATCH_INBOX_SCRIPT),
                "--me",
                "gemini",
                "--max-polls",
                "2",
                "--poll-seconds",
                "1",
                "--json",
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        watcher_events = [json.loads(line) for line in watcher_result.stdout.splitlines() if line.strip()]

        new_message_events = [event for event in watcher_events if event["event"] == "new_message"]
        self.assertEqual(1, len(new_message_events))
        self.assertTrue(
            any(event["event"] == "autobridge_failed" and event["message_path"] == message_rel for event in watcher_events)
        )
        self.assertTrue(
            any(event["event"] == "autobridge_consumed" and event["message_path"] == message_rel for event in watcher_events)
        )

        inbox = json.loads((root / "agents" / "gemini" / "inbox.json").read_text())
        self.assertEqual([], inbox["unread"])
        self.assertIn(message_rel, inbox["read"])

        runtime_payload = json.loads(output_file.read_text())
        self.assertEqual("Watcher retry", runtime_payload["message"]["title"])
        session_payload = self.run_cli(root, "show", "--session", "SESSION-WATCHER-RETRY")
        self.assertIn(message_rel, session_payload["processed_messages"])


if __name__ == "__main__":
    unittest.main()


class RenamedCodexBinaryDiscoveryTest(unittest.TestCase):
    """LLM_COLLAB_CODEX_BIN accepts any path, so discovery must not key on the filename.

    This PR adds and documents that override. A wrapper, symlink, or versioned executable
    launches correctly, and a literal "codex app-server" match would then never discover it --
    registered sessions would lose the very transport this PR advertises. The override and the
    discovery are one concern because the same change introduces the override.
    """

    def rows(self, commands):
        import subprocess as sp
        from unittest import mock as m
        listing = "\n".join(f"{1000 + i} {c}" for i, c in enumerate(commands))
        with m.patch.object(sp, "run", return_value=m.Mock(stdout=listing, returncode=0)):
            return session_autobridge_lib.codex_app_server_process_rows()

    def test_a_renamed_or_wrapped_binary_is_discovered(self) -> None:
        found = self.rows([
            "/opt/bin/codex-cli app-server --listen ws://127.0.0.1:8767 --ws-auth capability-token",
            "/usr/local/bin/codex-0.146.0 app-server --listen ws://127.0.0.1:8768",
            "/Applications/ChatGPT.app/Contents/Resources/codex app-server --listen ws://127.0.0.1:8769",
        ])
        self.assertEqual(3, len(found), f"all three should be discovered: {found}")

    def test_a_codex_process_that_is_not_an_app_server_is_ignored(self) -> None:
        found = self.rows([
            "/opt/bin/codex exec --prompt hello",
            "/opt/bin/codex login",
            "grep -r codex app-server /tmp",
        ])
        self.assertEqual([], found, "a false positive points delivery at the wrong endpoint")

    def test_ordinary_global_flags_do_not_hide_the_subcommand(self) -> None:
        """Assuming every unknown option takes a value swallowed the subcommand.

        These are all valid installed-CLI invocations, and each one made a reachable server
        vanish from discovery.
        """
        found = self.rows([
            "/opt/bin/codex --strict-config app-server --listen ws://127.0.0.1:8767",
            "/opt/bin/codex --oss app-server --listen ws://127.0.0.1:8768",
            "/opt/bin/codex --search app-server --listen ws://127.0.0.1:8769",
            "/opt/bin/codex --dangerously-bypass-approvals-and-sandbox "
            "app-server --listen ws://127.0.0.1:8770",
            "/opt/bin/codex --no-alt-screen app-server --listen ws://127.0.0.1:8771",
        ])
        self.assertEqual(5, len(found), f"all five are reachable servers: {found}")

    def test_value_taking_options_still_consume_their_value(self) -> None:
        found = self.rows([
            "/opt/bin/codex -m gpt-5 app-server --listen ws://127.0.0.1:8767",
            "/opt/bin/codex --cd /tmp app-server --listen ws://127.0.0.1:8768",
            "/opt/bin/codex -c features.x=true --strict-config -p prof "
            "app-server --listen ws://127.0.0.1:8769",
        ])
        self.assertEqual(3, len(found), f"{found}")

    def test_the_option_table_matches_the_installed_cli(self) -> None:
        """Keeps the transcribed table honest against the binary, not against my memory.

        Every option `codex --help` shows with a <VALUE> placeholder must be listed as
        value-taking, and no bare flag may be.
        """
        import re as _re
        import shutil as _shutil
        import subprocess as _sp

        binary = "/Applications/ChatGPT.app/Contents/Resources/codex"
        if not Path(binary).exists():
            binary = _shutil.which("codex") or ""
        if not binary:
            self.skipTest("no codex binary available to compare against")
        text = _sp.run([binary, "--help"], capture_output=True, text=True,
                       timeout=60).stdout
        table = session_autobridge_lib.VALUE_TAKING_GLOBAL_OPTIONS
        checked = 0
        for line in text.splitlines():
            if not _re.match(r"^  +-", line):
                continue
            names = _re.findall(r"(-{1,2}[A-Za-z][\w-]*)", line.split("  ", 3)[-1] or line)
            names = [n for n in _re.findall(r"(?:^|[\s,])(-{1,2}[A-Za-z][\w-]*)", line)]
            if not names:
                continue
            takes_value = "<" in line
            for name in names:
                if name in {"-h", "--help", "-V", "--version"}:
                    continue
                checked += 1
                if takes_value:
                    self.assertIn(name, table,
                                  f"{name} takes a value per --help but is missing from the table")
                else:
                    self.assertNotIn(name, table,
                                     f"{name} is a bare flag per --help but listed as value-taking")
        self.assertGreater(checked, 10, "the help output should have yielded many options")

    def test_an_executable_that_is_not_codex_is_refused(self) -> None:
        """The option table cannot separate these; the executable can.

        With unknown flags treated as valueless -- which is required so that --strict-config
        does not hide the subcommand -- `worker --label app-server` puts app-server in the
        subcommand slot. Only the executable's identity rejects it.
        """
        found = self.rows([
            "/opt/bin/worker --label app-server --listen ws://127.0.0.1:9998",
            "/usr/bin/python3 -m something app-server --listen ws://127.0.0.1:9997",
        ])
        self.assertEqual([], found, f"{found}")

    def test_the_configured_binary_is_accepted_whatever_it_is_called(self) -> None:
        # the point of LLM_COLLAB_CODEX_BIN: a wrapper named nothing like codex still works
        from unittest import mock as m
        with m.patch.dict("os.environ", {"LLM_COLLAB_CODEX_BIN": "/opt/custom/run-server"}):
            found = self.rows(["/opt/custom/run-server app-server --listen ws://127.0.0.1:8767"])
        self.assertEqual(1, len(found))
        unconfigured = self.rows(["/opt/custom/run-server app-server --listen ws://127.0.0.1:8767"])
        self.assertEqual([], unconfigured, "and only when it is configured")

    def test_a_substring_match_is_not_enough(self) -> None:
        """These two were admitted by `"app-server" in command`.

        discover_codex_app_server takes the FIRST matching row, so a false positive points
        delivery at somebody else's socket -- worse than the false negative being fixed.
        """
        found = self.rows([
            "/opt/bin/worker-app-server-proxy --listen ws://127.0.0.1:9999 CODEX_HOME=/tmp/home",
            "/opt/bin/worker --label app-server --listen ws://127.0.0.1:9998",
            "/opt/bin/app-server-shim --listen ws://127.0.0.1:9997",
        ])
        self.assertEqual([], found, f"none of these is a codex app-server: {found}")

    def test_the_real_desktop_invocation_with_a_separated_option_is_matched(self) -> None:
        # `-c key=value` before the subcommand is exactly how the desktop app launches
        found = self.rows([
            "/Applications/ChatGPT.app/Contents/Resources/codex "
            "-c features.code_mode_host=true app-server --listen ws://127.0.0.1:8767",
        ])
        self.assertEqual(1, len(found))

    def test_a_listen_flag_joined_with_equals_still_counts(self) -> None:
        found = self.rows(["/opt/bin/codex app-server --listen=ws://127.0.0.1:8767"])
        self.assertEqual(1, len(found))

    def test_discovery_still_requires_the_exact_codex_home_marker(self) -> None:
        """Being an app-server is necessary, not sufficient: the home must match too."""
        from unittest import mock as m
        import subprocess as sp
        listing = ("111 /opt/bin/codex-cli app-server --listen ws://127.0.0.1:8767 "
                   "CODEX_HOME=/Users/other/.codex")
        with m.patch.object(sp, "run", return_value=m.Mock(stdout=listing, returncode=0)):
            with m.patch.dict("os.environ", {}, clear=False):
                found = session_autobridge_lib.discover_codex_app_server("/Users/me/.codex")
        self.assertIsNone(found, "a matching invocation under another home is not our endpoint")

    def test_an_app_server_with_no_listener_flag_is_ignored(self) -> None:
        # the desktop app runs one of these; it is not reachable, so it is not an endpoint
        self.assertEqual([], self.rows([
            "/Applications/ChatGPT.app/Contents/Resources/codex "
            "-c features.code_mode_host=true app-server --analytics-default-enabled",
        ]))


# /bin/sh is the only shell the repository can assume: the reviewer's Linux checkout has
# no zsh, and every real-shell test raised FileNotFoundError there before asserting
# anything. POSIX sh is also the stricter target for the quoting these tests prove.
POSIX_SHELL = "/bin/sh"


class ResumePromptNamesTheReplyChannelTest(unittest.TestCase):
    """A woken worker must be told where its answer goes, and which packet it answers.

    Observed live on 2026-07-26: codex, woken through the app-server adapter, answered a
    review handoff by posting a PR comment and delivering nothing. The prompt carried the
    body but neither the packet's path nor any statement that the mailbox is the channel.

    This class once also proved a copyable `deliver.py` invocation. That template is
    withdrawn -- it could not carry a validated return address or a loop-protection
    marker, so a delayed reply could wake a rebound runtime and two workers following it
    could wake each other indefinitely. Roughly a dozen tests covering its quoting, scope
    rendering and shell execution went with it, because they proved properties of text
    that is no longer emitted. They are not replaced by weaker versions; they are
    replaced by a guard that no runnable command may reappear before the contracts it
    needs exist.
    """

    PROJECTS = ("amiga", "nuvyr")

    def prompt(
        self,
        *,
        repo_targets: Any = ["llm-collab"],
        project_id: str = "amiga",
        **overrides: str,
    ) -> str:
        session = {
            "session_id": "SESSION-REPLY",
            "agent_id": "codex",
            "project_id": project_id,
            "chat_id": "CHAT-REPLY",
            "runtime": {"family": "codex_app", "session_id": "runtime-reply"},
        }
        frontmatter = {
            "from": "claude",
            "sender_agent_id": "claude",
            "to": "codex",
            "title": "Review handoff",
            "project_id": project_id,
            "chat_id": "CHAT-REPLY",
        }
        if repo_targets is not None:
            frontmatter["repo_targets"] = repo_targets
        frontmatter.update(overrides)
        message = {
            "path": "Chats/2026-07-26_x__CHAT-REPLY/2026-07-26T00-00-00_to-codex_packet.md",
            "frontmatter": frontmatter,
            "body": "Do the lane.",
        }
        return session_autobridge_lib.build_resume_prompt(session, message)

    def test_the_prompt_is_a_packet_pointer_and_the_thread_is_receipt_only(self) -> None:
        for project_id in self.PROJECTS:
            with self.subTest(project_id=project_id):
                prompt = self.prompt(project_id=project_id)
                self.assertIn(
                    "message_path: "
                    f"{session_autobridge_lib.ROOT}/Chats/2026-07-26_x__CHAT-REPLY/"
                    "2026-07-26T00-00-00_to-codex_packet.md",
                    prompt,
                )
                self.assertNotIn("Do the lane.", prompt)
                self.assertNotIn("Message body:", prompt)
                self.assertIn("Do not answer it in this runtime thread", prompt)
                self.assertIn("Open `message_path` directly, read-only", prompt)
                self.assertIn("use `--peek` so reading does not acknowledge it", prompt)

    def test_the_prompt_names_the_mailbox_as_the_only_channel(self) -> None:
        for project_id in self.PROJECTS:
            with self.subTest(project_id=project_id):
                prompt = self.prompt(project_id=project_id)
                self.assertIn("explicitly requests a substantive response", prompt)
                self.assertIn("itself only a reply\nor delivery receipt", prompt)
                self.assertNotIn("Even a trivial answer is a mailbox packet", prompt)
                self.assertIn("deliver.py", prompt)

    def test_the_prompt_says_a_pr_comment_does_not_reach_the_sender(self) -> None:
        self.assertIn("does NOT reach the sender", self.prompt())

    def test_the_prompt_still_permits_a_pr_post_alongside_the_packet(self) -> None:
        """Connector review requests live on the PR; the rule is 'as well as', not 'never'."""
        prompt = self.prompt()
        self.assertIn("connector review", prompt)
        self.assertIn("deliver the packet as well", prompt)

    def test_no_runnable_reply_command_is_emitted(self) -> None:
        """The withdrawal, pinned.

        A copyable invocation cannot be correct until two contracts exist: a validated
        return address, so a delayed reply cannot wake a rebound runtime that never saw
        the request; and a loop-protection marker, without which
        should_skip_for_loop_protection returns (False, "ok") and two runtime-triggered
        workers following the instruction wake each other indefinitely. Naming the
        channel needs neither. Reinstating a command here before those land re-opens
        both.
        """
        for project_id in self.PROJECTS:
            for repo_targets in (["llm-collab"], ["pixexid/amiga", "app"], None, []):
                with self.subTest(project_id=project_id, repo_targets=repo_targets):
                    prompt = self.prompt(project_id=project_id, repo_targets=repo_targets)
                    for flag in ("--chat", "--from", "--to", "--project",
                                 "--repo-targets", "--body-file", "--title"):
                        self.assertNotIn(flag, prompt, f"{flag} is part of a runnable command")
                    for line in prompt.splitlines():
                        self.assertFalse(
                            line.startswith("  /") or line.strip().startswith("bin/"),
                            f"looks like a copyable command: {line!r}",
                        )

    def test_the_prompt_never_interpolates_packet_text_into_a_command(self) -> None:
        """Hostile frontmatter has nothing executable to reach any more.

        The withdrawn template rendered chat_id, project_id and the agent ids into a
        shell line; unquoted, `chat_id: "CHAT-X;echo"` injected a second command. With no
        command emitted, those values appear only as prompt metadata.
        """
        prompt = self.prompt(chat_id="CHAT-X;echo", project_id="amiga&&echo")
        self.assertIn("chat_id: CHAT-X;echo", prompt)
        for line in prompt.splitlines():
            self.assertFalse(line.startswith("  /"), f"executable-looking line: {line!r}")
