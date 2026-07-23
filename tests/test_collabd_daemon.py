from __future__ import annotations

import contextlib
import inspect
import errno
import io
import json
import os
import runpy
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import llm_collab.ledger.store as store_module
from llm_collab.daemon import cli
from llm_collab.daemon.gate import GateStatus
from llm_collab.daemon.observe import ObservationEngine
from llm_collab.daemon.server import (
    LOG_LIMIT,
    REQUEST_LIMIT,
    RESPONSE_LIMIT,
    DEADLINE_SECONDS,
    DaemonServer,
    ProtocolError,
    parse_request,
    peer_uid,
)
from llm_collab.ledger import LedgerPaths, LedgerStore, WriterAlreadyOpenError


SAFE_VERSION = (3, 51, 3)
ENABLED_ENV = {
    "THREAD_EVENT_RUNNER_ENABLED": "1",
    "THREAD_EVENT_RUNNER_OBSERVE": "1",
}
OBSERVATION_FEATURE = "daemon_" + "observation"
FEATURE_DECLARATION_ID = (
    "https://llm-collab.dev/declarations/standalone/v1/"
    + "feature-declarations.json"
)


def declaration(enabled: bool) -> str:
    return json.dumps(
        {
            "declaration_version": 1,
            "declaration_id": FEATURE_DECLARATION_ID,
            "features": {OBSERVATION_FEATURE: enabled},
        }
    )


class DaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.version = patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION)
        self.version.start()
        self.addCleanup(self.version.stop)
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        (self.root / "collab.config.json").write_text('{"workspace_id":"ws_alpha"}')
        (self.root / "projects.json").write_text(
            json.dumps({"projects": [{"id": "amiga"}]})
        )
        (self.root / "Chats").mkdir()
        (self.root / "agents").mkdir()
        self.declaration = self.root / "declaration.json"
        self.declaration.write_text(declaration(True))
        self.paths = LedgerPaths.derive(Path(self.tmp.name) / "state", "ws_alpha")

    def start(self, *, peer=None) -> tuple[DaemonServer, threading.Thread]:
        kwargs = {
            "workspace_root": self.root,
            "declaration_path": self.declaration,
            "environment": ENABLED_ENV,
        }
        if peer is not None:
            kwargs["peer_uid_getter"] = peer
        server = DaemonServer(self.paths, **kwargs)
        thread = threading.Thread(target=server.run)
        thread.start()
        deadline = time.monotonic() + 2
        while not self.paths.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.paths.socket.exists())
        return server, thread

    def request(self, value: bytes) -> dict:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(2)
            client.connect(os.fspath(self.paths.socket))
            client.sendall(value)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError as exc:
                # The daemon may authenticate/reject and close before the test
                # half-closes; the response is still readable from the socket.
                if exc.errno not in {errno.ENOTCONN, errno.EPIPE}:
                    raise
            return json.loads(client.recv(70_000).decode())
        finally:
            client.close()

    def stop(self, thread: threading.Thread) -> None:
        self.assertEqual(self.request(b'{"version":1,"op":"shutdown"}')["stopping"], True)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_lifecycle_writer_lock_modes_and_restart(self) -> None:
        _server, active = self.start()
        try:
            self.assertEqual(self.paths.socket.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.paths.workspace_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.paths.logs.stat().st_mode & 0o777, 0o700)
            for artifact in (self.paths.ledger, self.paths.lock, self.paths.log):
                self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
            self.assertTrue(self.request(b'{"version":1,"op":"status"}')["running"])
            with self.assertRaises(Exception):
                LedgerStore.open_writer(self.paths)
            with LedgerStore.open_reader(self.paths) as reader:
                self.assertFalse(reader.owns_writer_lock)
            self.stop(active)
            self.assertFalse(self.paths.socket.exists())
            _server, active = self.start()
            self.stop(active)
        finally:
            if active.is_alive():
                self.stop(active)

    def test_gated_off_holds_the_same_lock_without_opening_or_creating_a_ledger(self) -> None:
        self.declaration.write_text(declaration(False))
        server = DaemonServer(
            self.paths,
            workspace_root=self.root,
            declaration_path=self.declaration,
            environment=ENABLED_ENV,
        )
        with patch.object(LedgerStore, "open_writer", side_effect=AssertionError("must not open")):
            thread = threading.Thread(target=server.run)
            thread.start()
            deadline = time.monotonic() + 2
            while not self.paths.socket.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(self.paths.socket.exists())
            status = self.request(b'{"version":1,"op":"status"}')
            self.assertFalse(status["observation_gate"]["effective"])
            self.assertEqual(status["ledger"]["state"], "absent")
            self.assertFalse(self.paths.ledger.exists())
            self.assertEqual(list(self.paths.backups.iterdir()), [])
            self.stop(thread)
        self.assertFalse(self.paths.ledger.exists())

    def test_each_false_gate_and_all_false_perform_no_observation_reads_or_ledger_open(self) -> None:
        cases = (
            ("feature", declaration(False), ENABLED_ENV),
            (
                "runner-enabled-env",
                declaration(True),
                {**ENABLED_ENV, "THREAD_EVENT_RUNNER_ENABLED": "0"},
            ),
            (
                "observe-env",
                declaration(True),
                {**ENABLED_ENV, "THREAD_EVENT_RUNNER_OBSERVE": "0"},
            ),
            (
                "all-false",
                declaration(False),
                {
                    "THREAD_EVENT_RUNNER_ENABLED": "0",
                    "THREAD_EVENT_RUNNER_OBSERVE": "0",
                },
            ),
            ("invalid-declaration", '{"features":', ENABLED_ENV),
        )
        for name, declaration_text, environment in cases:
            with self.subTest(name=name):
                self.declaration.write_text(declaration_text)
                server = DaemonServer(
                    self.paths,
                    workspace_root=self.root,
                    declaration_path=self.declaration,
                    environment=environment,
                )
                with (
                    patch.object(
                        LedgerStore,
                        "open_writer",
                        side_effect=AssertionError("gate-off must not open the ledger"),
                    ) as open_writer,
                    patch(
                        "llm_collab.daemon.observe.read_registry_snapshot",
                        side_effect=AssertionError("gate-off must not read the registry"),
                    ) as registry_read,
                    patch(
                        "llm_collab.daemon.observe._load_watchdog",
                        side_effect=AssertionError("gate-off must not load watchdog"),
                    ) as watchdog_load,
                ):
                    thread = threading.Thread(target=server.run)
                    thread.start()
                    deadline = time.monotonic() + 2
                    while not self.paths.socket.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(self.paths.socket.exists())
                    status = self.request(b'{"version":1,"op":"status"}')
                    self.assertFalse(status["observation_gate"]["effective"])
                    self.assertEqual(status["observation"]["state"], "gated_off")
                    self.assertEqual(status["observation"]["source_reachability"], "not_checked")
                    self.stop(thread)
                    open_writer.assert_not_called()
                    registry_read.assert_not_called()
                    watchdog_load.assert_not_called()
                self.assertFalse(self.paths.ledger.exists())
                self.assertEqual(list(self.paths.backups.iterdir()), [])

    def test_gated_off_daemon_lock_refuses_a_second_writer(self) -> None:
        self.declaration.write_text(declaration(False))
        server = DaemonServer(
            self.paths,
            workspace_root=self.root,
            declaration_path=self.declaration,
            environment=ENABLED_ENV,
        )
        thread = threading.Thread(target=server.run)
        thread.start()
        deadline = time.monotonic() + 2
        while not self.paths.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            with self.assertRaises(WriterAlreadyOpenError):
                LedgerStore.open_writer(self.paths)
        finally:
            self.stop(thread)

    def test_status_runs_one_shared_integrity_scan_or_zero_when_gated_off(self) -> None:
        def request_status(server: DaemonServer) -> dict[str, object]:
            client, connection = socket.socketpair()
            try:
                client.sendall(b'{"version":1,"op":"status"}')
                client.shutdown(socket.SHUT_WR)
                server._handle(connection)
                return json.loads(client.recv(RESPONSE_LIMIT + 1).decode())
            finally:
                connection.close()
                client.close()

        enabled_gate = GateStatus(
            declaration_valid=True,
            features={OBSERVATION_FEATURE: True},
            thread_event_runner_enabled=True,
            thread_event_runner_observe=True,
            effective=True,
        )
        with LedgerStore.open_writer(self.paths) as store:
            engine = ObservationEngine(
                workspace_root=self.root,
                workspace_id="ws_alpha",
                projects_path=self.root / "projects.json",
            )
            server = DaemonServer(
                self.paths,
                workspace_root=self.root,
                peer_uid_getter=lambda _connection: os.getuid(),
            )
            server._gate_status = enabled_gate
            server._store = store
            server._observation = engine

            statements: list[str] = []
            store._connection.set_trace_callback(statements.append)
            try:
                response = request_status(server)
                scans = [
                    statement
                    for statement in statements
                    if statement.strip().lower() == "pragma integrity_check"
                ]
                self.assertEqual(len(scans), 1)
                self.assertEqual(response["ledger"]["integrity"], "ok")
                self.assertEqual(
                    response["observation"]["ledger"]["integrity"],
                    response["ledger"]["integrity"],
                )

                statements.clear()
                server._observation = None
                without_observation = request_status(server)
                scans = [
                    statement
                    for statement in statements
                    if statement.strip().lower() == "pragma integrity_check"
                ]
                self.assertEqual(len(scans), 1)
                self.assertEqual(without_observation["ledger"]["integrity"], "ok")

                statements.clear()
                server._store = None
                gated_off = request_status(server)
                scans = [
                    statement
                    for statement in statements
                    if statement.strip().lower() == "pragma integrity_check"
                ]
                self.assertEqual(scans, [])
                self.assertEqual(
                    gated_off["ledger"]["integrity"], "not_checked_gate_off"
                )
            finally:
                store._connection.set_trace_callback(None)

    def test_server_resolves_nested_cwd_to_the_collab_workspace(self) -> None:
        nested = self.root / "one" / "two"
        nested.mkdir(parents=True)
        old_cwd = Path.cwd()
        os.chdir(nested)
        try:
            self.assertEqual(DaemonServer(self.paths).workspace_root, self.root.resolve())
        finally:
            os.chdir(old_cwd)

    def test_first_status_is_ready_before_slow_initial_reconciliation(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_reconcile(_engine, _store, *, force=False):
            entered.set()
            release.wait(2)
            return True

        with patch(
            "llm_collab.daemon.observe.ObservationEngine.reconcile_due",
            autospec=True,
            side_effect=slow_reconcile,
        ):
            _server, thread = self.start()
            time.sleep(0.2)
            started = time.monotonic()
            status = self.request(b'{"version":1,"op":"status"}')
            self.assertTrue(status["running"])
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(entered.wait(1))
            release.set()
            self.stop(thread)

    def test_listener_and_observer_setup_share_cleanup_discipline(self) -> None:
        gate = GateStatus(
            declaration_valid=True,
            features={OBSERVATION_FEATURE: True},
            thread_event_runner_enabled=True,
            thread_event_runner_observe=True,
            effective=True,
        )
        store = Mock(owns_writer_lock=True)
        server = DaemonServer(self.paths, workspace_root=self.root)
        server._gate_status = gate
        with (
            patch.object(server, "_open_listener", side_effect=RuntimeError("bind failed")),
            patch("llm_collab.daemon.observe.ObservationEngine") as engine_factory,
            patch.object(server, "_write_log"),
            self.assertRaisesRegex(RuntimeError, "bind failed"),
        ):
            server._serve(store)
        engine_factory.assert_not_called()
        self.assertIsNone(server._store)
        self.assertIsNone(server._observation)

        listener = Mock()
        listener.accept.side_effect = RuntimeError("accept failed")
        observer = Mock()
        server = DaemonServer(self.paths, workspace_root=self.root)
        server._gate_status = gate
        with (
            patch.object(server, "_open_listener", return_value=listener),
            patch("llm_collab.daemon.observe.ObservationEngine", return_value=observer),
            patch.object(server, "_write_log"),
            self.assertRaisesRegex(RuntimeError, "accept failed"),
        ):
            server._serve(store)
        observer.close.assert_called_once()
        listener.close.assert_called_once()
        self.assertIsNone(server._store)
        self.assertIsNone(server._observation)

    def test_closed_request_schema_and_size_limits(self) -> None:
        valid = b'{"version":1,"op":"status"}'
        self.assertEqual(parse_request(valid), "status")
        for payload in (
            b'{"version":1,"version":1,"op":"status"}',
            b'{"version":1,"op":"status","params":{}}',
            b'{"version":1,"op":"start"}',
            b'{"version":1,"op":"doctor"}',
            b'{"version":1,"op":[]}',
            b'{"version":1,"op":"status"} trailing',
            b'\xff',
        ):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                parse_request(payload)
        _server, thread = self.start()
        self.assertIn("error", self.request(b"x" * 4097))
        self.assertIn("error", self.request(b'{"version":1,"op":"start"}'))
        self.assertIn("error", self.request(b'{"version":1,"op":[]}'))
        self.assertTrue(self.request(valid)["running"])
        self.stop(thread)

    def test_peer_authentication_precedes_dispatch(self) -> None:
        server, thread = self.start(peer=lambda _connection: os.getuid() + 1)
        with patch("llm_collab.daemon.server.parse_request") as parser:
            result = self.request(b'{"version":1,"op":"shutdown"}')
        self.assertIn("UID mismatch", result["error"])
        parser.assert_not_called()
        server._stopping = True
        thread.join(2)

    def test_linux_and_darwin_peer_paths_fail_closed(self) -> None:
        fake = unittest.mock.Mock()
        fake.getsockopt.return_value = (1).to_bytes(4, "little", signed=True) + (22).to_bytes(4, "little", signed=True) + (3).to_bytes(4, "little", signed=True)
        self.assertEqual(peer_uid(fake, platform="linux"), 22)
        fake.getpeereid.return_value = (23, 24)
        self.assertEqual(peer_uid(fake, platform="darwin"), 23)
        with self.assertRaises(PermissionError):
            peer_uid(unittest.mock.Mock(spec=[]), platform="darwin")

    def test_stale_socket_recovery_refuses_symlink_non_socket_and_listener(self) -> None:
        self.paths.ensure_directories()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(self.paths.socket))
        stale.close()
        with LedgerStore.open_writer(self.paths):
            DaemonServer(self.paths)._recover_stale_socket()
        self.assertFalse(self.paths.socket.exists())
        self.paths.socket.write_text("operator")
        with LedgerStore.open_writer(self.paths), self.assertRaisesRegex(RuntimeError, "non-socket"):
            DaemonServer(self.paths)._recover_stale_socket()
        self.assertEqual(self.paths.socket.read_text(), "operator")
        self.paths.socket.unlink()
        if hasattr(os, "symlink"):
            self.paths.socket.symlink_to(self.paths.log)
            with LedgerStore.open_writer(self.paths), self.assertRaisesRegex(RuntimeError, "non-socket"):
                DaemonServer(self.paths)._recover_stale_socket()
            self.assertTrue(self.paths.socket.is_symlink())
            self.paths.socket.unlink()
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(os.fspath(self.paths.socket))
        live.listen(1)
        with LedgerStore.open_writer(self.paths), self.assertRaisesRegex(RuntimeError, "already listening"):
            DaemonServer(self.paths)._recover_stale_socket()
        live.close()

    def test_stale_socket_ambiguous_probe_errors_preserve_the_path(self) -> None:
        self.paths.ensure_directories()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.fspath(self.paths.socket))
        stale.close()
        for error in (PermissionError("denied"), FileNotFoundError("changed"), socket.timeout("slow")):
            with self.subTest(error=type(error).__name__), LedgerStore.open_writer(self.paths):
                probe = Mock()
                probe.connect.side_effect = error
                with patch("llm_collab.daemon.server.socket.socket", return_value=probe), self.assertRaisesRegex(RuntimeError, "cannot prove"):
                    DaemonServer(self.paths)._recover_stale_socket()
            self.assertTrue(self.paths.socket.exists())

    def test_stale_socket_recovery_rechecks_the_inode_before_unlink(self) -> None:
        probe = Mock()
        probe.connect.side_effect = ConnectionRefusedError(errno.ECONNREFUSED, "stale")
        with (
            patch("llm_collab.daemon.server.socket.socket", return_value=probe),
            patch("llm_collab.daemon.server._identity", side_effect=[(1, 1), (1, 2)]),
            patch("llm_collab.daemon.server.os.unlink") as unlink,
            self.assertRaisesRegex(RuntimeError, "changed during stale recovery"),
        ):
            DaemonServer(self.paths)._recover_stale_socket()
        unlink.assert_not_called()

    def test_request_and_response_deadlines_and_response_bound(self) -> None:
        server = DaemonServer(self.paths, peer_uid_getter=lambda _connection: os.getuid())
        connection = Mock()
        connection.recv.return_value = b""
        server._handle(connection)
        self.assertTrue(connection.settimeout.call_args_list)
        self.assertTrue(all(call.args[0] <= DEADLINE_SECONDS for call in connection.settimeout.call_args_list))
        receiver = Mock()
        server._send(receiver, {"logs": "x" * RESPONSE_LIMIT})
        response = receiver.sendall.call_args.args[0]
        self.assertLessEqual(len(response), RESPONSE_LIMIT)
        self.assertIn(b"response exceeds", response)
        client = Mock()
        client.recv.side_effect = [b'{"version":1}', b""]
        with patch("llm_collab.daemon.cli.socket.socket", return_value=client):
            self.assertEqual(cli._request(self.paths, "status"), {"version": 1})
        self.assertTrue(client.settimeout.call_args_list)
        self.assertTrue(all(call.args[0] <= DEADLINE_SECONDS for call in client.settimeout.call_args_list))
        with patch("llm_collab.daemon.cli.socket.socket") as factory, self.assertRaises(TimeoutError):
            cli._request(self.paths, "status", timeout=0)
        factory.assert_not_called()

    def test_whole_request_and_response_deadlines_do_not_reset_per_chunk(self) -> None:
        server_clock = Mock(side_effect=[0, 0, 2.1, 2.1, 2.1])
        server = DaemonServer(
            self.paths,
            peer_uid_getter=lambda _connection: os.getuid(),
            clock=server_clock,
        )
        connection = Mock()
        connection.recv.return_value = b'{"version":1,'
        server._handle(connection)
        self.assertEqual(connection.recv.call_count, 1)
        self.assertIn(b"deadline exceeded", connection.sendall.call_args.args[0])
        self.assertFalse(server._stopping)
        client = Mock()
        client.recv.side_effect = [b'{"version":1,', b'"running":true}', b""]
        with patch("llm_collab.daemon.cli.socket.socket", return_value=client), self.assertRaisesRegex(
            TimeoutError, "deadline exceeded"
        ):
            cli._request(self.paths, "status", clock=Mock(side_effect=[0, 0, 0.5, 1, 2.1]))
        self.assertEqual(client.recv.call_count, 1)

    def test_redacted_log_rotation_preserves_prior_fifth(self) -> None:
        self.paths.ensure_directories()
        server = DaemonServer(self.paths)
        self.paths.log.write_bytes(b"x" * LOG_LIMIT)
        fifth = self.paths.log.with_name(self.paths.log.name + ".5")
        fifth.write_text("old-fifth")
        server._write_log({"event": "test", "payload": "secret", "body": "hidden"})
        self.assertEqual(fifth.read_text(), "old-fifth")
        self.assertIn("[redacted]", self.paths.log.read_text())
        self.assertNotIn("secret", self.paths.log.read_text())

    def test_rotation_checks_the_incoming_append_boundary(self) -> None:
        self.paths.ensure_directories()
        server = DaemonServer(self.paths)
        encoded = b'{"event":"boundary"}\n'
        self.paths.log.write_bytes(b"x" * (LOG_LIMIT - len(encoded)))
        server._write_log({"event": "boundary"})
        self.assertEqual(self.paths.log.with_name(self.paths.log.name + ".1").stat().st_size, LOG_LIMIT - len(encoded))
        self.assertEqual(self.paths.log.read_bytes(), encoded)

    def test_rotation_retains_exactly_five_numbered_generations(self) -> None:
        self.paths.ensure_directories()
        server = DaemonServer(self.paths)
        self.paths.log.write_bytes(b"active" + b"x" * (LOG_LIMIT - len(b"active")))
        for number in range(1, 6):
            self.paths.log.with_name(self.paths.log.name + f".{number}").write_text(str(number))
        server._write_log({"event": "rotate"})
        expected = {1: "active" + "x" * (LOG_LIMIT - len(b"active")), 2: "1", 3: "2", 4: "3", 5: "4"}
        for number, contents in expected.items():
            self.assertEqual(self.paths.log.with_name(self.paths.log.name + f".{number}").read_text(), contents)
        self.assertFalse(self.paths.log.with_name(self.paths.log.name + ".6").exists())

    def test_rotation_recovers_every_replace_failure_on_retry(self) -> None:
        for failed_call in range(1, 11):
            with self.subTest(failed_call=failed_call), TemporaryDirectory(dir="/tmp") as tmp, patch(
                "llm_collab.daemon.server.LOG_LIMIT", 10
            ):
                paths = LedgerPaths.derive(Path(tmp) / "state", "ws_alpha")
                paths.ensure_directories()
                server = DaemonServer(paths)
                contents = {0: "active", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
                paths.log.write_text(contents[0])
                for number in range(1, 6):
                    paths.log.with_name(paths.log.name + f".{number}").write_text(contents[number])
                real_replace = os.replace
                calls = 0
                def fail_once(source, target):
                    nonlocal calls
                    calls += 1
                    if calls == failed_call:
                        raise OSError("injected rotation failure")
                    return real_replace(source, target)
                with patch("llm_collab.daemon.server.os.replace", side_effect=fail_once), self.assertRaises(OSError):
                    server._write_log({"event": "retry"})
                recovered = []
                for path in [paths.log, *(paths.log.with_name(paths.log.name + f".{number}") for number in range(1, 6)), *(paths.log.with_name(paths.log.name + f".{number}.new") for number in range(1, 6))]:
                    if path.exists():
                        recovered.append(path.read_text())
                self.assertEqual(sorted(recovered), sorted(contents.values()))
                server._write_log({"event": "retry"})
                self.assertEqual(paths.log.with_name(paths.log.name + ".1").read_text(), "active")
                self.assertEqual(paths.log.with_name(paths.log.name + ".2").read_text(), "one")
                self.assertEqual(paths.log.with_name(paths.log.name + ".3").read_text(), "two")
                self.assertEqual(paths.log.with_name(paths.log.name + ".4").read_text(), "three")
                self.assertEqual(paths.log.with_name(paths.log.name + ".5").read_text(), "four")
                self.assertFalse(any(paths.log.with_name(paths.log.name + f".{number}.new").exists() for number in range(1, 6)))
                self.assertFalse(paths.log.with_name(paths.log.name + ".6").exists())

    def test_shutdown_never_unlinks_a_replaced_socket_and_failed_rotation_keeps_fifth(self) -> None:
        server, thread = self.start()
        self.paths.socket.unlink()
        self.paths.socket.write_text("replacement")
        server._stopping = True
        thread.join(2)
        self.assertEqual(self.paths.socket.read_text(), "replacement")
        self.paths.ensure_directories()
        self.paths.log.write_bytes(b"x" * LOG_LIMIT)
        self.paths.log.with_name(self.paths.log.name + ".4").write_text("fourth")
        fifth = self.paths.log.with_name(self.paths.log.name + ".5")
        fifth.write_text("prior-fifth")
        real_replace = os.replace
        def fail_final(source, target):
            if os.fspath(target).endswith(".5"):
                raise OSError("rotation failed")
            return real_replace(source, target)
        with patch("llm_collab.daemon.server.os.replace", side_effect=fail_final), self.assertRaises(OSError):
            server._write_log({"event": "rotate"})
        self.assertEqual(fifth.read_text(), "prior-fifth")

    def test_cli_diagnostics_do_not_create_or_mutate(self) -> None:
        root = Path(self.tmp.name) / "diagnostic-workspace"
        root.mkdir()
        config = root / "collab.config.json"
        original = b'{"project_state_root":"state"}'
        config.write_bytes(original)
        old_cwd = Path.cwd()
        os.chdir(root)
        self.addCleanup(os.chdir, old_cwd)
        self.assertEqual(cli.main(["status"]), 1)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((root / "state").exists())
        config.write_text('{"workspace_id":"ws_alpha","project_state_root":"state"}')
        self.assertEqual(cli.main(["doctor"]), 1)
        self.assertFalse((root / "state").exists())

    def test_doctor_is_top_level_only(self) -> None:
        with patch("llm_collab.daemon.cli._paths", return_value=self.paths), patch(
            "llm_collab.daemon.cli._request", return_value={"running": True}
        ):
            self.assertEqual(cli.main(["daemon", "doctor"]), 2)
            self.assertEqual(cli.main(["doctor"]), 0)

    def test_public_wrapper_reports_shipped_daemon_usage(self) -> None:
        wrapper = Path(__file__).parents[1] / "bin" / "llm-collab"
        with TemporaryDirectory(dir="/tmp") as tmp:
            invalid_verb = subprocess.run(
                [os.fspath(wrapper), "daemon", "restart"],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(invalid_verb.returncode, 2)
            self.assertIn("bin/llm-collab daemon <start|stop|status|logs>", invalid_verb.stderr)
            self.assertIn("bin/llm-collab doctor", invalid_verb.stderr)
            self.assertNotIn("llm-collabd", invalid_verb.stderr)

            nested_doctor = subprocess.run(
                [os.fspath(wrapper), "daemon", "doctor"],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(nested_doctor.returncode, 2)
            self.assertIn("bin/llm-collab doctor", nested_doctor.stderr)
            self.assertNotIn("llm-collabd", nested_doctor.stderr)

            top_level_doctor = subprocess.run(
                [os.fspath(wrapper), "doctor"],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(top_level_doctor.returncode, 1)
            self.assertIn("llm-collab:", top_level_doctor.stderr)
            self.assertNotIn("usage:", top_level_doctor.stderr)
            self.assertNotIn("llm-collabd", top_level_doctor.stderr)

            background_start = subprocess.run(
                [os.fspath(wrapper), "daemon", "start", "--background"],
                cwd=tmp,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(background_start.returncode, 1)
            self.assertIn("llm-collab:", background_start.stderr)
            self.assertNotIn("usage:", background_start.stderr)
            self.assertNotIn("llm-collabd", background_start.stderr)

    def test_direct_entrypoint_guard_precedes_daemon_import(self) -> None:
        root = Path(__file__).parents[1]
        script = root / "bin" / "llm_collabd.py"
        bin_dir = str(root / "bin")
        prior_cli = sys.modules.pop("llm_collab.daemon.cli", None)
        old_path = list(sys.path)
        try:
            sys.path.insert(0, bin_dir)
            import _python_runtime

            stderr = io.StringIO()
            with patch.object(_python_runtime, "MIN_VERSION", (999, 0)):
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    runpy.run_path(os.fspath(script), run_name="llm_collabd_guard_reject")
            self.assertEqual(raised.exception.code, 1)
            self.assertIn("requires Python 999.0+", stderr.getvalue())
            self.assertNotIn("llm_collab.daemon.cli", sys.modules)

            namespace = runpy.run_path(os.fspath(script), run_name="llm_collabd_guard_pass")
            self.assertIn("main", namespace)
            self.assertIn("llm_collab.daemon.cli", sys.modules)
        finally:
            sys.path[:] = old_path
            if prior_cli is not None:
                sys.modules["llm_collab.daemon.cli"] = prior_cli

    def test_cli_route_is_fixed_and_no_second_flock_exists(self) -> None:
        launcher = (Path(__file__).parents[1] / "bin" / "llm-collab").read_text()
        source = inspect.getsource(DaemonServer)
        self.assertIn('script="llm_collabd.py"', launcher)
        self.assertIn('set -- daemon "$@"', launcher)
        self.assertIn('set -- doctor "$@"', launcher)
        self.assertNotIn("flock", source)
        self.assertEqual((REQUEST_LIMIT, RESPONSE_LIMIT, DEADLINE_SECONDS), (4096, 65536, 2))

    def test_background_timeout_terminates_the_one_child(self) -> None:
        child = Mock()
        child.pid = 17
        child.poll.return_value = None
        with (
            patch("llm_collab.daemon.cli.subprocess.Popen", return_value=child) as spawn,
            patch("llm_collab.daemon.cli._workspace_root", return_value=Path(self.tmp.name)),
            patch("llm_collab.daemon.cli.time.monotonic", side_effect=[0, 3]),
            self.assertRaisesRegex(RuntimeError, "did not become ready"),
        ):
            cli._background(self.paths)
        spawn.assert_called_once()
        self.assertEqual(
            spawn.call_args.args[0],
            [
                sys.executable,
                str(Path(cli.__file__).parents[2] / "bin" / "llm_collabd.py"),
                "daemon",
                "start",
            ],
        )
        self.assertEqual(
            spawn.call_args.kwargs,
            {
                "cwd": Path(self.tmp.name),
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "start_new_session": True,
                "close_fds": True,
            },
        )
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=DEADLINE_SECONDS)

    def test_background_requires_the_spawned_child_identity(self) -> None:
        child = Mock()
        child.pid = 17
        child.poll.return_value = None
        with (
            patch("llm_collab.daemon.cli.subprocess.Popen", return_value=child),
            patch("llm_collab.daemon.cli._workspace_root", return_value=Path(self.tmp.name)),
            patch("llm_collab.daemon.cli._request", return_value={"running": True, "pid": 18}),
            patch("llm_collab.daemon.cli.time.monotonic", side_effect=[0, 0, 1, 3]),
            patch("llm_collab.daemon.cli.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "did not become ready"),
        ):
            cli._background(self.paths)
        child.terminate.assert_called_once()

    def test_background_probe_uses_only_its_remaining_readiness_budget(self) -> None:
        child = Mock()
        child.pid = 17
        child.poll.return_value = None
        request = Mock(side_effect=[{"running": True, "pid": 18}, {"running": True, "pid": 17}])
        with (
            patch("llm_collab.daemon.cli.subprocess.Popen", return_value=child),
            patch("llm_collab.daemon.cli._workspace_root", return_value=Path(self.tmp.name)),
            patch("llm_collab.daemon.cli._request", request),
            patch("llm_collab.daemon.cli.time.monotonic", side_effect=[0, 0, 1.9, 1.9, 2.1, 2.1]),
            patch("llm_collab.daemon.cli.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "did not become ready"),
        ):
            cli._background(self.paths)
        self.assertEqual(request.call_args_list[0].kwargs["timeout"], 2)
        self.assertLessEqual(request.call_args_list[1].kwargs["timeout"], 0.100001)
        self.assertEqual(request.call_count, 2)
        child.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
