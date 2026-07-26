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
from unittest.mock import MagicMock, Mock, patch

import llm_collab.ledger.store as store_module
from llm_collab.daemon import cli
from llm_collab.daemon.gate import GateStatus
from llm_collab.daemon.observe import ObservationEngine
from llm_collab.daemon.server import (
    LOG_LIMIT,
    REQUEST_LIMIT,
    RESPONSE_LIMIT,
    DEADLINE_SECONDS,
    INTEGRITY_REFRESH_SECONDS,
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
        try:
            # `start()` cannot be exempt from the cleanup rule: it calls readiness BEFORE
            # returning, so a readiness exception leaves no caller holding the thread to
            # clean up -- the non-daemon accept loop then keeps the whole run alive. Owning
            # the failure here is the only place it can be owned.
            self.wait_until_accepting()
        except BaseException:
            server._stopping = True
            thread.join(2)
            raise
        return server, thread

    def start_without_readiness(self) -> tuple[DaemonServer, threading.Thread]:
        """For the one test that must own the daemon's FIRST operation itself.

        `start()` exchanges a status request, which is the right readiness signal and the
        wrong thing for a test timing the first status.
        """
        server = DaemonServer(
            self.paths,
            workspace_root=self.root,
            declaration_path=self.declaration,
            environment=ENABLED_ENV,
        )
        thread = threading.Thread(target=server.run)
        thread.start()
        # A connect, not a request and not the socket file. The file-existence loop this
        # branch deletes returns during the bind-to-listen window; a connect cannot. It is
        # weaker than `start()`'s exchange -- it can return while the accept loop is still
        # in setup -- and that is exactly what this caller wants, since the setup it is
        # timing must not have been consumed by the probe.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect(os.fspath(self.paths.socket))
                break
            except OSError:
                time.sleep(0.01)
            finally:
                probe.close()
        return server, thread

    def wait_until_accepting(self, timeout: float = 5.0) -> None:
        """Wait until the daemon ANSWERS, not until its socket file appears.

        `_open_listener` binds -- which creates the file -- then restores the umask,
        chmods, stats for its identity, and only then calls listen(). Connecting inside
        that window raises ECONNREFUSED, because a bound-but-unlistening AF_UNIX socket
        refuses. Polling `socket.exists()` therefore returned true while the daemon was
        still unreachable, and under full-suite load the test won the race often enough
        to fail roughly one run in three (llm-collab#320).

        A successful connect is not enough either: with observation enabled, `_serve`
        listens and then runs the integrity probe, builds and starts the observation
        engine, and writes the startup log before it ever reaches `accept()`. The kernel
        completes a connect into the backlog throughout that interval, so a connect-only
        probe can return while the daemon still cannot dispatch -- and a following request
        times out, or is reset if setup aborts. A completed request/response exchange is
        the only evidence that the accept loop is actually serving.
        """
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                answer = self.request(b'{"version":1,"op":"status"}')
            except (OSError, ValueError) as exc:
                last = exc
                time.sleep(0.01)
                continue
            if isinstance(answer, dict):
                return
            last = ValueError(f"status answered with {type(answer).__name__}")
            time.sleep(0.01)
        self.fail(f"daemon never answered a request within {timeout}s: {last!r}")

    def wait_for_log(self) -> Path:
        deadline = time.monotonic() + 2
        while not self.paths.log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.paths.log.exists(), "daemon started log was not written")
        return self.paths.log

    def test_start_survives_a_widened_bind_to_listen_window(self) -> None:
        """The race, made deterministic instead of waited for.

        Rather than run the suite until load happens to open the real bind->listen
        window, this widens it on purpose. A readiness probe that waits for the socket
        FILE returns during the window and the first request hits ECONNREFUSED; a probe
        that waits for a successful connect does not.

        Without this the fix was only supported by 8 consecutive green full-suite runs,
        which under the measured 1-in-3 failure rate could happen by chance about 4% of
        the time. This fails outright on the old probe.
        """
        import llm_collab.daemon.server as server_module

        real_open = server_module.DaemonServer._open_listener

        def slow_open(inner_self):
            inner_self._recover_stale_socket()
            old_mask = os.umask(0o077)
            try:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(os.fspath(inner_self.paths.socket))
            finally:
                os.umask(old_mask)
            # The window that exists for real -- umask restore, chmod, stat -- widened
            # so the test does not depend on scheduling luck.
            time.sleep(0.3)
            os.chmod(inner_self.paths.socket, 0o600)
            inner_self._socket_identity = server_module._identity(inner_self.paths.socket)
            listener.listen(8)
            listener.settimeout(0.1)
            return listener

        with patch.object(server_module.DaemonServer, "_open_listener", slow_open):
            server, thread = self.start()
            try:
                # The first request must land on a listening socket, not a bound one.
                self.assertTrue(self.request(b'{"version":1,"op":"status"}')["running"])
            finally:
                # If the regression recurs, the assertion above raises. `thread` is
                # non-daemon and the server stays in its accept loop, so a test meant to
                # report a failure would instead hang the whole run -- shutdown belongs
                # here, not after the assertion.
                self.stop(thread)
        self.assertIs(real_open, server_module.DaemonServer._open_listener)

    def test_no_launched_daemon_thread_can_escape_cleanup(self) -> None:
        """Every `thread.start()` must be followed immediately by a try/finally.

        A non-daemon thread left in its accept loop turns a reported failure into a hung
        run, so the failure is never seen. Three sites were fixed and two direct launches
        were missed; asserted structurally because the alternative is discovering the next
        one by hanging.
        """
        import ast

        tree = ast.parse(Path(__file__).resolve().read_text(encoding="utf-8"))
        unprotected: list[str] = []
        examined: list[str] = []
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            if function.name == "start_without_readiness":
                # Deliberately hands the thread back before any request, for the one test
                # that must own the daemon's first operation; its caller protects it.
                continue
            launches = [
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Thread"
                and any(
                    isinstance(keyword.value, ast.Attribute)
                    and keyword.value.attr == "run"
                    for keyword in node.keywords
                )
            ]
            if not launches:
                continue
            examined.append(function.name)
            # A `finally`, or an `except` that re-raises after cleaning up. `start()` needs
            # the second shape: it must clean up only when readiness fails, because on
            # success the thread belongs to its caller.
            protections = [
                node.lineno
                for node in ast.walk(function)
                if isinstance(node, ast.Try)
                and (
                    node.finalbody
                    or any(
                        any(isinstance(inner, ast.Raise) for inner in ast.walk(handler))
                        for handler in node.handlers
                    )
                )
            ]
            for line in launches:
                if not any(protection > line for protection in protections):
                    unprotected.append(f"{function.name}:{line}")
        self.assertEqual(
            [], unprotected,
            "a daemon server thread is launched with no try/finally after it",
        )
        # The exemption list is part of the rule, so what the rule actually looked at is
        # asserted too: widening the skip set is how this check was made to pass while
        # `start()` still leaked a thread on a readiness failure.
        self.assertIn(
            "start", examined,
            "start() was skipped, so this check cannot see whether it leaks a thread",
        )

    def test_the_harness_has_exactly_one_readiness_mechanism(self) -> None:
        """A second readiness mechanism is a second chance to get it wrong.

        Fixing `start()` left three direct launches polling `paths.socket.exists()`, which
        carries the same bind-to-listen TOCTOU this file reproduces. Source inspection is
        a weak proof of behaviour, but "no other readiness mechanism exists here" is a
        property OF the source, so it is the right thing to assert -- and the widened-
        window cases above are what prove the surviving mechanism works.
        """
        source = Path(__file__).resolve().read_text(encoding="utf-8")
        # Matched by the loop's own shape rather than by a keyword, so this detector does
        # not report itself -- the first version did exactly that.
        needle = "while not self." + "paths.socket.exists()"
        polls = [
            number
            for number, line in enumerate(source.splitlines(), start=1)
            if needle in line
        ]
        self.assertEqual([], polls, "a readiness loop over the socket file survives")

    def test_readiness_waits_for_a_served_request_not_for_a_connect(self) -> None:
        """A connect can complete into the backlog while the daemon cannot dispatch.

        With observation enabled, `_serve` listens and then runs the integrity probe,
        builds and starts the observation engine, and writes the startup log before it
        reaches `accept()`. The kernel accepts connections into the backlog throughout
        that interval, so a connect-only probe returns while nothing is serving -- and a
        following request times out, or is reset if setup aborts. Widened here, on the
        startup log, so the difference does not depend on scheduling luck.
        """
        real_write_log = DaemonServer._write_log

        def slow_started_log(inner_self, event: dict[str, object]) -> None:
            if event.get("event") == "started":
                time.sleep(0.4)
            real_write_log(inner_self, event)

        with patch.object(DaemonServer, "_write_log", slow_started_log):
            _server, thread = self.start()
            try:
                # Readiness has returned, so the accept loop is serving: a request with
                # far less patience than the widened window must still be answered. A
                # connect-only probe would have returned mid-window and this would time
                # out in the backlog.
                self.assertTrue(
                    self.request(b'{"version":1,"op":"status"}', timeout=0.15)["running"]
                )
            finally:
                self.stop(thread)

    def test_readiness_waits_for_accept_not_for_the_socket_file(self) -> None:
        """The race this harness had, pinned deterministically.

        _open_listener() binds -- creating the socket file -- then restores the umask,
        chmods and stats before calling listen(). A bound-but-unlistening AF_UNIX socket
        refuses connections, so a probe that waits for the FILE returns while the daemon
        is still unreachable. Under full-suite load that window was won often enough to
        fail about one run in three (llm-collab#320).

        Rather than time the real window, this reproduces its shape directly: a socket
        that is bound and not yet listening must refuse, which is what makes file
        existence the wrong readiness signal.
        """
        with TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "probe.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(os.fspath(path))

            self.assertTrue(path.exists(), "bind() alone makes the file appear")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1)
            self.addCleanup(probe.close)
            with self.assertRaises(ConnectionRefusedError):
                probe.connect(os.fspath(path))

            listener.listen(8)
            accepted = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            accepted.settimeout(1)
            self.addCleanup(accepted.close)
            accepted.connect(os.fspath(path))

    def test_a_readiness_failure_in_start_leaves_no_live_thread(self) -> None:
        """`start()` calls readiness before returning, so only it can own that failure.

        Exempting it from the cleanup rule on the assumption that the caller holds the
        thread is wrong: on a readiness failure there is no caller yet, and the non-daemon
        accept loop keeps the whole run alive.
        """
        import llm_collab.daemon.server as server_module

        def bind_but_never_listen(inner_self):
            inner_self._recover_stale_socket()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(os.fspath(inner_self.paths.socket))
            inner_self._socket_identity = server_module._identity(inner_self.paths.socket)
            listener.settimeout(0.1)
            self.addCleanup(listener.close)
            return listener

        with patch.object(
            server_module.DaemonServer, "_open_listener", bind_but_never_listen
        ):
            with self.assertRaises(AssertionError):
                self.start()
        for thread in threading.enumerate():
            self.assertFalse(
                thread.name.startswith("Thread-") and thread.is_alive()
                and getattr(thread, "_target", None) is not None
                and getattr(thread._target, "__name__", "") == "run",
                "a daemon thread survived a readiness failure in start()",
            )

    def test_an_unexpected_server_thread_exit_is_reported(self) -> None:
        """`if thread.is_alive()` reads as a double-shutdown guard and hides a death.

        In a body that never shuts the server down, a dead thread means the accept loop
        exited on its own -- and after the status response every assertion can pass while
        the guard silently swallows it.
        """
        finished = threading.Thread(target=lambda: None)
        finished.start()
        finished.join(1)
        with self.assertRaises(AssertionError) as caught:
            self.stop_or_report(finished)
        self.assertIn("exited before cleanup", str(caught.exception))

    def test_the_readiness_probe_reports_failure_rather_than_hanging(self) -> None:
        """A readiness helper that waits forever turns a fast failure into a stall."""
        from types import SimpleNamespace

        with TemporaryDirectory(dir="/tmp") as tmp:
            original = self.paths
            self.paths = SimpleNamespace(socket=Path(tmp) / "never-created.sock")
            try:
                with self.assertRaises(AssertionError) as caught:
                    self.wait_until_accepting(timeout=0.3)
                self.assertIn("never answered a request", str(caught.exception))
            finally:
                self.paths = original

    def request(self, value: bytes, *, timeout: float = 2) -> dict:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(timeout)
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

    def stop_or_report(self, thread: threading.Thread) -> None:
        """Shut down, or say why there was nothing to shut down.

        `if thread.is_alive(): stop(...)` reads as a benign double-shutdown guard, but in a
        body that never shuts the server down a dead thread means the accept loop exited on
        its own -- and if that happened after the status response, every assertion could
        pass while this guard silently swallowed it.
        """
        if thread.is_alive():
            self.stop(thread)
            return
        self.fail("the server thread exited before cleanup; the accept loop died")

    def stop(self, thread: threading.Thread) -> None:
        self.assertEqual(self.request(b'{"version":1,"op":"shutdown"}')["stopping"], True)
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_lifecycle_writer_lock_modes_and_restart(self) -> None:
        _server, active = self.start()
        try:
            log = self.wait_for_log()
            self.assertEqual(self.paths.socket.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.paths.workspace_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.paths.logs.stat().st_mode & 0o777, 0o700)
            for artifact in (self.paths.ledger, self.paths.lock, log):
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

    def test_status_readiness_does_not_require_started_log_artifact(self) -> None:
        original_write_log = DaemonServer._write_log
        skipped_started_log = threading.Event()

        def write_log(server: DaemonServer, event: dict[str, object]) -> None:
            if event.get("event") == "started":
                skipped_started_log.set()
                return
            original_write_log(server, event)

        with patch.object(DaemonServer, "_write_log", autospec=True, side_effect=write_log):
            _server, active = self.start()
            try:
                self.assertTrue(skipped_started_log.wait(1))
                self.assertFalse(self.paths.log.exists())
                self.assertTrue(self.request(b'{"version":1,"op":"status"}')["running"])
                self.assertEqual(self.request(b'{"version":1,"op":"logs"}')["logs"], [])
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
            # From immediately after start(), not after the assertions: a readiness
            # failure raises before any of them and would otherwise leave this non-daemon
            # thread in its accept loop, hanging the run instead of reporting.
            try:
                self.wait_until_accepting()
                status = self.request(b'{"version":1,"op":"status"}')
                self.assertFalse(status["observation_gate"]["effective"])
                self.assertEqual(status["ledger"]["state"], "absent")
                self.assertFalse(self.paths.ledger.exists())
                self.assertEqual(list(self.paths.backups.iterdir()), [])
            finally:
                self.stop_or_report(thread)
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
                    try:
                        self.wait_until_accepting()
                        status = self.request(b'{"version":1,"op":"status"}')
                        self.assertFalse(status["observation_gate"]["effective"])
                        self.assertEqual(status["observation"]["state"], "gated_off")
                        self.assertEqual(
                            status["observation"]["source_reachability"], "not_checked"
                        )
                    finally:
                        if thread.is_alive():
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
        self.wait_until_accepting()
        try:
            with self.assertRaises(WriterAlreadyOpenError):
                LedgerStore.open_writer(self.paths)
        finally:
            self.stop(thread)

    def test_status_uses_cached_integrity_snapshot_or_gate_off_shape(self) -> None:
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
            server._record_integrity_result("ok")

            statements: list[str] = []
            store._connection.set_trace_callback(statements.append)
            try:
                response = request_status(server)
                scans = [
                    statement
                    for statement in statements
                    if statement.strip().lower() == "pragma integrity_check"
                ]
                self.assertEqual(scans, [])
                self.assertEqual(response["ledger"]["integrity"]["state"], "ok")
                self.assertEqual(response["ledger"]["integrity"]["freshness"], "current")
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
                self.assertEqual(scans, [])
                self.assertEqual(
                    without_observation["ledger"]["integrity"],
                    response["ledger"]["integrity"],
                )

                statements.clear()
                server._store = None
                gated_off = request_status(server)
                scans = [
                    statement
                    for statement in statements
                    if statement.strip().lower() == "pragma integrity_check"
                ]
                self.assertEqual(scans, [])
                self.assertEqual(gated_off["ledger"]["integrity"]["state"], "gate_off")
                self.assertEqual(gated_off["ledger"]["integrity"]["freshness"], "unknown")
            finally:
                store._connection.set_trace_callback(None)

    def test_status_returns_while_integrity_probe_is_blocked(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_integrity() -> str:
            entered.set()
            release.wait(2)
            return "ok"

        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.__exit__.return_value = False
        reader.database_identity = (1, 1)
        reader.integrity_check.side_effect = blocked_integrity
        writer = Mock()
        writer.database_identity = (1, 1)
        writer.schema_version.return_value = 8
        writer.integrity_check.side_effect = blocked_integrity
        server = DaemonServer(self.paths, clock=time.monotonic)
        server._store = writer
        with patch.object(LedgerStore, "open_reader", return_value=reader):
            server._start_integrity_probe()
            self.assertTrue(entered.wait(1))
            completed = threading.Event()
            response: dict[str, object] = {}

            def request_status() -> None:
                response.update(server._status_response())
                completed.set()

            request = threading.Thread(target=request_status)
            request.start()
            try:
                self.assertTrue(completed.wait(0.5))
                self.assertEqual(
                    response["ledger"]["integrity"]["state"],  # type: ignore[index]
                    "checking",
                )
                writer.integrity_check.assert_not_called()
            finally:
                release.set()
                server._stop_integrity_probe()
                request.join(1)

    def test_integrity_declares_probe_coverage_in_every_state(self) -> None:
        """`ok` must not imply a whole-chain guarantee the probe cannot give.

        Named probe_scope, not verified_scope: the field appears in unknown, checking,
        gate_off and pre-identity failures too, where nothing has been verified. Calling
        it "verified" there would swap one overclaim for another.
        """
        from llm_collab.daemon.server import PROBE_SCOPE, _integrity_snapshot

        self.assertEqual("main_database_identity", PROBE_SCOPE)
        for state in ("unknown", "checking", "ok", "failed", "gate_off"):
            snapshot = _integrity_snapshot(state)
            self.assertEqual(PROBE_SCOPE, snapshot["probe_scope"], state)
            self.assertNotIn(
                "verified_scope", snapshot,
                "the field must not claim verification in states where none occurred",
            )

    def test_integrity_snapshot_reports_stale_and_bounded_failure(self) -> None:
        now = [100.0]
        server = DaemonServer(self.paths, clock=lambda: now[0])
        self.assertEqual(server._integrity_status()["state"], "unknown")
        server._record_integrity_result("ok")
        self.assertEqual(server._integrity_status()["freshness"], "current")
        now[0] += INTEGRITY_REFRESH_SECONDS + 1
        stale = server._integrity_status()
        self.assertEqual(stale["state"], "ok")
        self.assertEqual(stale["freshness"], "stale")
        server._record_integrity_result("failed", error="x" * 1000)
        failed = server._integrity_status()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["freshness"], "current")
        self.assertEqual(len(failed["error"]), 256)
        self.assertTrue(failed["error_truncated"])

    def test_integrity_probe_preserves_diagnostic_from_one_reader_scan(self) -> None:
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.__exit__.return_value = False
        reader.database_identity = (1, 1)
        reader.integrity_check.return_value = "row 1 missing from index"
        writer = Mock()
        writer.database_identity = (1, 1)
        server = DaemonServer(self.paths)
        server._store = writer
        recorded = threading.Event()
        original_record = server._record_integrity_result

        def record(*args, **kwargs):
            original_record(*args, **kwargs)
            recorded.set()

        with patch.object(server, "_record_integrity_result", side_effect=record):
            with patch.object(LedgerStore, "open_reader", return_value=reader) as open_reader:
                server._start_integrity_probe()
                try:
                    self.assertTrue(recorded.wait(1))
                finally:
                    server._stop_integrity_probe()

        open_reader.assert_called_once_with(self.paths, validate_integrity=False)
        reader.integrity_check.assert_called_once_with()
        result = server._integrity_status()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], "row 1 missing from index")
        self.assertFalse(result["error_truncated"])

    def test_shutdown_does_not_wait_for_blocked_integrity_probe(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocked_integrity() -> str:
            entered.set()
            release.wait(2)
            return "ok"

        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.__exit__.return_value = False
        reader.database_identity = (1, 1)
        reader.integrity_check.side_effect = blocked_integrity
        server = DaemonServer(self.paths)
        server._store = Mock(schema_version=Mock(return_value=8))
        server._store.database_identity = (1, 1)
        with patch.object(LedgerStore, "open_reader", return_value=reader):
            server._start_integrity_probe()
            thread = server._integrity_thread
            self.assertIsNotNone(thread)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            server._stop_integrity_probe()
            self.assertLess(time.monotonic() - started, 0.5)
            release.set()
            thread.join(1)

    def test_concurrent_reader_pins_cannot_enter_one_descriptor_proof_window(self) -> None:
        with LedgerStore.open_writer(self.paths):
            pass

        first_snapshot = threading.Event()
        second_pin = threading.Event()
        original_snapshot = store_module._connection_fd_snapshot
        original_pin = LedgerStore._pin_regular_file
        snapshot_calls: dict[str, int] = {}

        def snapshot() -> dict[int, tuple[int, int, int, str]]:
            result = original_snapshot()
            if threading.current_thread().name == "reader-a":
                calls = snapshot_calls.get("reader-a", 0) + 1
                snapshot_calls["reader-a"] = calls
                if calls == 1:
                    first_snapshot.set()
                    second_pin.wait(1)
            return result

        def pin(path: Path, **kwargs):
            result = original_pin(path, **kwargs)
            if threading.current_thread().name == "reader-b":
                second_pin.set()
            return result

        errors: list[BaseException] = []

        def open_reader() -> None:
            connection = None
            pin = None
            try:
                connection, pin = LedgerStore._open_verified_connection(
                    self.paths.ledger,
                    read_only=True,
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                if connection is not None:
                    connection.close()
                if pin is not None:
                    pin.close()

        with patch.object(store_module, "_connection_fd_snapshot", side_effect=snapshot):
            with patch.object(LedgerStore, "_pin_regular_file", side_effect=pin):
                thread_a = threading.Thread(target=open_reader, name="reader-a")
                thread_a.start()
                self.assertTrue(first_snapshot.wait(1))
                thread_b = threading.Thread(target=open_reader, name="reader-b")
                thread_b.start()
                thread_a.join(2)
                thread_b.join(2)

        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertEqual(errors, [])

    def test_verified_close_cannot_churn_descriptor_proof_window(self) -> None:
        with LedgerStore.open_writer(self.paths):
            pass
        self.paths.ensure_directories()
        target = self.paths.backups / "descriptor-race.sqlite3"
        close_go = threading.Event()
        probe_ready = threading.Event()
        close_attempted = threading.Event()
        close_finished = threading.Event()
        close_errors: list[BaseException] = []
        probe: dict[str, object] = {}
        snapshot_calls = 0
        original_snapshot = store_module._connection_fd_snapshot
        original_close = store_module._close_connection_and_pin

        def snapshot() -> dict[int, tuple[int, int, int, str]]:
            nonlocal snapshot_calls
            result = original_snapshot()
            snapshot_calls += 1
            if snapshot_calls == 1:
                close_go.set()
                if not close_attempted.wait(1):
                    raise AssertionError("probe close did not enter the proof window")
                if close_finished.wait(0.2):
                    raise AssertionError("probe close raced the descriptor proof window")
            return result

        def close(connection, pin) -> None:
            if threading.current_thread().name == "probe-close":
                close_attempted.set()
            try:
                original_close(connection, pin)
            finally:
                if threading.current_thread().name == "probe-close":
                    close_finished.set()

        def close_probe() -> None:
            try:
                probe["connection"], probe["pin"] = LedgerStore._open_verified_connection(
                    self.paths.ledger,
                    read_only=True,
                )
                probe_ready.set()
            except BaseException as exc:
                close_errors.append(exc)
                probe_ready.set()
                return
            close_go.wait(1)
            try:
                close(probe["connection"], probe["pin"])
            except BaseException as exc:
                close_errors.append(exc)

        thread = threading.Thread(target=close_probe, name="probe-close")
        connection = pin = None
        thread.start()
        self.assertTrue(probe_ready.wait(1))
        with (
            patch.object(store_module, "_connection_fd_snapshot", side_effect=snapshot),
            patch.object(store_module, "_close_connection_and_pin", side_effect=close),
        ):
            try:
                connection, pin = LedgerStore._open_verified_connection(
                    target,
                    read_only=False,
                    create=True,
                    exclusive=True,
                )
            finally:
                close_go.set()
                thread.join(2)

        try:
            self.assertFalse(thread.is_alive())
            self.assertEqual(close_errors, [])
            self.assertIsNotNone(connection)
            self.assertIsNotNone(pin)
        finally:
            original_close(connection, pin)

    def test_integrity_probe_fails_closed_on_writer_reader_identity_mismatch(self) -> None:
        reader = MagicMock()
        reader.__enter__.return_value = reader
        reader.__exit__.return_value = False
        reader.database_identity = (2, 20)
        writer = Mock()
        writer.database_identity = (1, 10)
        server = DaemonServer(self.paths)
        server._store = writer
        recorded = threading.Event()
        original_record = server._record_integrity_result

        def record(*args, **kwargs):
            original_record(*args, **kwargs)
            recorded.set()

        with patch.object(server, "_record_integrity_result", side_effect=record):
            with patch.object(LedgerStore, "open_reader", return_value=reader):
                server._start_integrity_probe()
                try:
                    self.assertTrue(recorded.wait(1))
                    result = server._integrity_status()
                    self.assertEqual(result["state"], "failed")
                    self.assertIn("different ledger file", result["error"])
                    reader.integrity_check.assert_not_called()
                finally:
                    server._stop_integrity_probe()

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
        """Readiness must not consume the operation this test is timing.

        `start()` now exchanges a status request, so the "first" status here was really the
        second -- a regression that served the blocked reconciliation before the first
        status would have been hidden. This one starts the daemon without the readiness
        exchange so the request it times really is the first.
        """
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
            _server, thread = self.start_without_readiness()
            try:
                time.sleep(0.2)
                started = time.monotonic()
                status = self.request(b'{"version":1,"op":"status"}')
                self.assertTrue(status["running"])
                self.assertLess(time.monotonic() - started, 2)
                self.assertTrue(entered.wait(1))
            finally:
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
        try:
            self.assertIn("error", self.request(b"x" * 4097))
            self.assertIn("error", self.request(b'{"version":1,"op":"start"}'))
            self.assertIn("error", self.request(b'{"version":1,"op":[]}'))
            self.assertTrue(self.request(valid)["running"])
        finally:
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
