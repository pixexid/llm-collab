"""Tests for the Runtime Adapter stdio supervisor boundary."""

from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from llm_collab.runtime_adapter_manifest import (
    ManifestResolutionError,
    TrustedManifestRegistry,
)
from llm_collab.runtime_adapter_supervisor import (
    MAX_STDERR_BYTES_PER_CONNECTION,
    StdioSupervisor,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_supervisor.py"
TEST_PATH = Path(__file__)


def manifest(script: Path, workdir: Path) -> dict:
    return {
        "adapter_a": {
            "adapter_id": "adapter_a",
            "adapter_revision": "rev_1",
            "manifest_id": "manifest_a",
            "manifest_revision": "manifest_rev_1",
            "endpoint": {
                "endpoint_id": "endpoint_a",
                "adapter_name": "adapter_a",
                "adapter_revision": "rev_1",
            },
            "executable": sys.executable,
            "argv": [sys.executable, str(script)],
            "working_directory": str(workdir),
            "environment": {"PYTHONUNBUFFERED": "1"},
            "environment_allowlist": ["PYTHONUNBUFFERED"],
        }
    }


def resolved_adapter(script: Path, workdir: Path):
    return TrustedManifestRegistry(manifest(script, workdir)).resolve("adapter_a")


def write_script(root: Path, source: str) -> Path:
    path = root / "adapter.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RuntimeAdapterSupervisorTests(unittest.TestCase):
    def test_context_manager_spawns_from_resolved_adapter_and_reaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                pid = supervisor.pid
                self.assertIsInstance(pid, int)
                outcome = supervisor.request('{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}')
                self.assertEqual(outcome.response, '{"jsonrpc":"2.0","id":"r1","result":{}}')
                self.assertIsNone(outcome.fault)
            self.assertFalse(process_alive(pid))

    def test_public_constructor_accepts_only_resolved_adapter(self) -> None:
        params = set(inspect.signature(StdioSupervisor.__init__).parameters)
        self.assertEqual(params, {"self", "resolved"})
        self.assertFalse(
            params
            & {
                "executable",
                "path",
                "argv",
                "env",
                "environment",
                "working_directory",
                "workdir",
                "shell",
                "manifest_path",
                "adapter_alias",
            }
        )

    def test_relative_executable_and_workdir_fail_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(root, "pass\n")
            adapter = resolved_adapter(script, root)
            cases = {
                "executable": replace(adapter, executable="adapter-a"),
                "working_directory": replace(adapter, working_directory="relative-work"),
            }
            for name, candidate in cases.items():
                with self.subTest(name=name), patch(
                    "llm_collab.runtime_adapter_supervisor.subprocess.Popen",
                    side_effect=AssertionError("spawn must not run"),
                ):
                    with self.assertRaises(ManifestResolutionError):
                        with StdioSupervisor(candidate):
                            pass

    def test_shell_false_is_explicit_and_shell_mutation_would_be_detected(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        popen_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
        ]
        self.assertEqual(len(popen_calls), 1)
        shell_keywords = [kw for kw in popen_calls[0].keywords if kw.arg == "shell"]
        self.assertEqual(len(shell_keywords), 1)
        self.assertIs(shell_keywords[0].value.value, False)

    def test_stderr_is_drained_continuously_past_limit_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                f"""
                import sys
                sys.stderr.buffer.write(b"x" * {MAX_STDERR_BYTES_PER_CONNECTION + 4096})
                sys.stderr.buffer.flush()
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{{"jsonrpc":"2.0","id":"r1","result":{{}}}}\\n')
                sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertEqual(outcome.fault, "STDERR_LIMIT_EXCEEDED")
                self.assertTrue(outcome.stderr_truncated)
                self.assertLessEqual(len(outcome.stderr), MAX_STDERR_BYTES_PER_CONNECTION)

    def test_response_waits_for_preceding_stderr_to_be_drained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stderr.buffer.write(b"x")
                sys.stderr.buffer.flush()
                sys.stdin.buffer.readline()
                sys.stderr.buffer.write(b"xxxxx")
                sys.stderr.buffer.flush()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                """,
            )
            read_started = threading.Event()
            second_drain_started = threading.Event()
            release_read = threading.Event()
            release_second_drain = threading.Event()
            request_done = threading.Event()
            result = {}
            drain_calls = 0
            real_drain = StdioSupervisor._drain_stderr_fd

            def paused_drain(supervisor, fd):
                nonlocal drain_calls
                drain_calls += 1
                if drain_calls == 1:
                    result = real_drain(supervisor, fd)
                    read_started.set()
                    release_read.wait(5)
                    return result
                if drain_calls == 2:
                    second_drain_started.set()
                    release_second_drain.wait(5)
                return real_drain(supervisor, fd)

            with patch(
                "llm_collab.runtime_adapter_supervisor.MAX_STDERR_BYTES_PER_CONNECTION", 4
            ), patch.object(StdioSupervisor, "_drain_stderr_fd", paused_drain):
                with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                    def request():
                        result["outcome"] = supervisor.request(
                            '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}'
                        )
                        request_done.set()

                    thread = threading.Thread(target=request)
                    try:
                        self.assertTrue(read_started.wait(5))
                        thread.start()
                        deadline = time.monotonic() + 5
                        while supervisor._stderr_barriers.empty() and time.monotonic() < deadline:
                            time.sleep(0.001)
                        self.assertFalse(supervisor._stderr_barriers.empty())
                        release_read.set()
                        self.assertTrue(second_drain_started.wait(5))
                        self.assertFalse(request_done.wait(0.25))
                    finally:
                        release_read.set()
                        release_second_drain.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result["outcome"].fault, "STDERR_LIMIT_EXCEEDED")

    def test_oversized_stdout_frame_is_bounded_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b"x" * 1048578)
                sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertEqual(outcome.fault, "MESSAGE_TOO_LARGE")
                self.assertTrue(outcome.should_close)

    def test_abnormal_host_exit_reaps_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import time
                time.sleep(30)
                """,
            )
            pid = None
            try:
                with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                    pid = supervisor.pid
                    raise RuntimeError("host failure")
            except RuntimeError:
                pass
            self.assertIsInstance(pid, int)
            self.assertFalse(process_alive(pid))

    def test_thread_start_failure_is_not_masked_and_reaps_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(root, "import time\ntime.sleep(30)\n")
            supervisor = StdioSupervisor(resolved_adapter(script, root))
            real_start = threading.Thread.start
            starts = 0

            def fail_second_start(thread):
                nonlocal starts
                starts += 1
                if starts == 2:
                    raise RuntimeError("thread start failed")
                real_start(thread)

            with patch.object(threading.Thread, "start", new=fail_second_start):
                with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                    supervisor.__enter__()
            self.assertFalse(process_alive(supervisor.pid))

    def test_stderr_drain_failure_faults_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                """,
            )
            with patch.object(
                StdioSupervisor,
                "_drain_stderr_fd",
                side_effect=RuntimeError("drain failed"),
            ):
                with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                    pid = supervisor.pid
                    outcome = supervisor.request(
                        '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}'
                    )
                    self.assertEqual(outcome.fault, "STDERR_DRAIN_FAILED")
                    self.assertTrue(outcome.should_close)
                    self.assertFalse(process_alive(pid))

    def test_no_forbidden_process_or_state_imports(self) -> None:
        forbidden_llm_collab = {
            "canonical",
            "ledger",
            "compatibility",
            "daemon",
            "registry",
            "project_issue_queue",
            "inbox",
        }
        forbidden_os_calls = {"system", "popen"}
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "llm_collab":
                        self.assertFalse(set(parts) & forbidden_llm_collab)
            if isinstance(node, ast.ImportFrom):
                parts = (node.module or "").split(".")
                if parts and parts[0] == "llm_collab":
                    self.assertFalse(set(parts) & forbidden_llm_collab)
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".", 1)[0], forbidden_llm_collab)
                if node.module == "os":
                    self.assertFalse(any(alias.name in forbidden_os_calls for alias in node.names))
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    self.assertFalse(func.value.id == "os" and func.attr in forbidden_os_calls)
                if isinstance(func, ast.Name):
                    self.assertNotIn(func.id, forbidden_os_calls)

    def test_no_bin_consumer_imports_supervisor_module(self) -> None:
        for path in (ROOT / "bin").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(alias.name == "llm_collab.runtime_adapter_supervisor" for alias in node.names),
                        path,
                    )
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "llm_collab.runtime_adapter_supervisor", path)
                    if node.module == "llm_collab":
                        self.assertFalse(
                            any(alias.name == "runtime_adapter_supervisor" for alias in node.names),
                            path,
                        )

    def test_tests_do_not_construct_supervisor_from_raw_execution_inputs(self) -> None:
        params = {"executable", "argv", "environment", "working_directory", "shell"}
        tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "StdioSupervisor":
                    self.assertFalse(any(keyword.arg in params for keyword in node.keywords))



    def test_a_wake_queued_during_the_drain_is_not_swallowed(self) -> None:
        """A barrier queued between the snapshot and the wake drain must still be served.

        The loop used to snapshot barriers and then drain the wake pipe, so a publish
        landing in that window had its wake byte consumed on its behalf: the barrier was
        never signalled and no byte remained to force another pass, leaving the publisher
        blocked in `barrier.wait()` forever. Draining first guarantees any byte written
        afterwards survives into the next `select()`.

        Made deterministic by injecting exactly that interleaving at the start of the
        wake drain rather than racing for it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys, time
                sys.stdin.readline()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                time.sleep(5)
                """,
            )
            injected = threading.Event()
            late_barrier = threading.Event()
            real_drain_fd = StdioSupervisor._drain_fd

            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                def injecting_drain_fd(fd: int) -> None:
                    if not injected.is_set():
                        injected.set()
                        # A publish landing after select() and before the wake drain.
                        supervisor._stderr_barriers.put(late_barrier)
                        supervisor._wake_stderr()
                    real_drain_fd(fd)

                with patch.object(StdioSupervisor, "_drain_fd",
                                  staticmethod(injecting_drain_fd)):
                    outcome = supervisor.request(
                        '{"jsonrpc":"2.0","id":"r1","method":"x","params":{}}',
                        timeout_seconds=5,
                    )
                    self.assertIsNotNone(outcome.response, f"fault={outcome.fault} stderr={outcome.stderr[:200]!r}")
                    self.assertTrue(
                        late_barrier.wait(5),
                        "a barrier queued during the wake drain was never signalled",
                    )

    def test_stdout_eof_waits_for_the_stderr_drain(self) -> None:
        """PROCESS_CLOSED must not carry a half-drained diagnostic.

        Only non-empty lines went through the barrier, so an adapter that wrote stderr
        and exited could publish the EOF sentinel while the drain was still running,
        returning diagnostics that were merely whatever had been read so far.

        The child writes well UNDER the pipe buffer on purpose. An earlier version of
        this test wrote past the stderr limit, which meant the child blocked on a full
        pipe until the drain ran -- so pausing the drain also prevented the exit, no
        early sentinel was possible, and the test passed with or without the barrier.
        """
        payload = 8192
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.readline()
                sys.stderr.buffer.write(b"e" * 8192)
                sys.stderr.buffer.flush()
                """,
            )
            real_drain = StdioSupervisor._drain_stderr_fd
            first = threading.Event()

            def paused_drain(self_inner, fd):
                if not first.is_set():
                    first.set()
                    time.sleep(0.4)
                return real_drain(self_inner, fd)

            with patch.object(StdioSupervisor, "_drain_stderr_fd", paused_drain):
                with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                    outcome = supervisor.request(
                        '{"jsonrpc":"2.0","id":"r1","method":"x","params":{}}',
                        timeout_seconds=5,
                    )
        self.assertEqual("PROCESS_CLOSED", outcome.fault)
        self.assertEqual(
            payload, len(outcome.stderr),
            "the EOF sentinel published before the drain had read the diagnostics",
        )
        self.assertFalse(outcome.stderr_truncated)

    def test_an_unexpected_stderr_read_error_is_a_drain_failure(self) -> None:
        """Only a zero-length read is EOF.

        Treating every `OSError` as EOF let an active-connection failure end the drain
        normally, so a waiting response was released as clean while diagnostics were
        never read. Teardown-caused errors remain suppressed -- that path is exercised by
        every other test in this file, which all close normally.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys, time
                sys.stdin.readline()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                time.sleep(5)
                """,
            )
            real_read = os.read

            def failing_read(fd, size):
                if fd == self._stderr_fd_under_test:
                    raise OSError(5, "Input/output error")
                return real_read(fd, size)

            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                self._stderr_fd_under_test = supervisor._process.stderr.fileno()
                with patch.object(
                    __import__("llm_collab.runtime_adapter_supervisor", fromlist=["os"]).os,
                    "read",
                    failing_read,
                ):
                    deadline = time.monotonic() + 5
                    while not supervisor._stderr_failed.is_set() and time.monotonic() < deadline:
                        supervisor._wake_stderr()
                        time.sleep(0.05)
                    self.assertTrue(
                        supervisor._stderr_failed.is_set(),
                        "an active-connection read error did not fail the drain closed",
                    )
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"x","params":{}}',
                    timeout_seconds=5,
                )
        self.assertEqual("STDERR_DRAIN_FAILED", outcome.fault)
        self.assertTrue(outcome.should_close)

    def test_a_recorded_drain_failure_survives_a_dead_child(self) -> None:
        """The structured fault must not depend on whether the child is still alive.

        `_require_process()` ran first, so an adapter that exited after the drain failure
        produced a `RuntimeError` instead of the promised `STDERR_DRAIN_FAILED` outcome.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(root, "import sys\nsys.stdin.readline()\n")
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                supervisor._stderr_failed.set()
                # Detach the child only for the duration of the call, then put it back so
                # __exit__ can still reap it. Leaving `_process` as None made `close()`
                # return early, orphaning the child and its threads into later tests.
                live = supervisor._process
                supervisor._process = None
                try:
                    outcome = supervisor.request(
                        '{"jsonrpc":"2.0","id":"r1","method":"x","params":{}}',
                        timeout_seconds=5,
                    )
                finally:
                    supervisor._process = live
        self.assertEqual("STDERR_DRAIN_FAILED", outcome.fault)
        self.assertTrue(outcome.should_close)

if __name__ == "__main__":
    unittest.main()
