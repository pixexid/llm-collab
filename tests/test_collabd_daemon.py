from __future__ import annotations

import inspect
import errno
import json
import os
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
from llm_collab.ledger import LedgerPaths, LedgerStore


SAFE_VERSION = (3, 51, 3)


class DaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.version = patch.object(store_module, "_linked_sqlite_version_info", return_value=SAFE_VERSION)
        self.version.start()
        self.addCleanup(self.version.stop)
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.paths = LedgerPaths.derive(Path(self.tmp.name) / "state", "ws_alpha")

    def start(self, *, peer=None) -> tuple[DaemonServer, threading.Thread]:
        server = DaemonServer(self.paths) if peer is None else DaemonServer(self.paths, peer_uid_getter=peer)
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
            client.shutdown(socket.SHUT_WR)
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
        root = Path(self.tmp.name) / "workspace"
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
