"""Tests for the Runtime Adapter stdio supervisor boundary."""

from __future__ import annotations

import ast
import inspect
import os
import sys
import tempfile
import textwrap
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
    MAX_PENDING_FRAMES,
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
        """The child writes more than the pipe holds, so the host must keep draining.

        This is the deadlock property. It deliberately does NOT assert that the response
        racing the overflow is faulted: the protocol requires the host to fail "each
        affected operation ... after the first excess byte", and an operation that
        completes before the host has read that byte is not affected. Asserting otherwise
        is what made this case fail about one run in six -- the drain had simply not been
        scheduled to consume the last 4 KiB yet. The verdict is checked below, on a
        request made after the excess has actually been observed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                f"""
                import sys
                sys.stderr.buffer.write(b"x" * {MAX_STDERR_BYTES_PER_CONNECTION + 4096})
                sys.stderr.buffer.flush()
                while True:
                    line = sys.stdin.buffer.readline()
                    if not line:
                        break
                    sys.stdout.buffer.write(b'{{"jsonrpc":"2.0","id":"r1","result":{{}}}}\\n')
                    sys.stdout.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                first = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertIsNotNone(first, "the child must not have deadlocked")
                self.assertNotEqual("REQUEST_TIMEOUT", first.fault)
                deadline = time.monotonic() + 5
                while not supervisor._stderr_truncated and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(supervisor._stderr_truncated, "overflow never recorded")
                self.assertLessEqual(
                    len(supervisor._stderr), MAX_STDERR_BYTES_PER_CONNECTION
                )

                second = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertEqual("STDERR_LIMIT_EXCEEDED", second.fault)
                self.assertTrue(second.stderr_truncated)
                self.assertTrue(second.should_close)

    def test_an_outcome_reports_a_coherent_diagnostic_snapshot(self) -> None:
        """The flag and the bytes must come from the same instant.

        Publishing only the fault decision left `_outcome` to re-read the live buffer, so
        an outcome could pair a `False` flag with bytes already at the cap, or a `True`
        flag with a short buffer -- two different moments reported as one observation.

        What is deliberately NOT claimed: that `stderr_truncated` describes only bytes the
        frame preceded. Readiness across two pipes carries no chronology, and there is no
        way to make a stdout read and a stderr snapshot atomic against each other. The
        budget is per connection "counted cumulatively from process start through stderr
        EOF", so the flag means the connection had exceeded it as of publication -- and
        faulting an operation that raced the overflow is what the protocol permits.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                f"""
                import sys
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{{"jsonrpc":"2.0","id":"r1","result":{{}}}}\\n')
                sys.stdout.buffer.flush()
                sys.stderr.buffer.write(b"y" * {MAX_STDERR_BYTES_PER_CONNECTION + 4096})
                sys.stderr.buffer.flush()
                sys.stdin.buffer.readline()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                if outcome.stderr_truncated:
                    self.assertEqual(
                        MAX_STDERR_BYTES_PER_CONNECTION, len(outcome.stderr),
                        "reported truncated, but the bytes are from an earlier moment",
                    )
                else:
                    self.assertLess(
                        len(outcome.stderr), MAX_STDERR_BYTES_PER_CONNECTION,
                        "reported not truncated, but the bytes are already at the cap",
                    )

    def test_a_published_snapshot_is_not_topped_up_from_live_state(self) -> None:
        """Both halves of the snapshot travel together, or neither does.

        Carrying only the fault decision and re-reading the bytes pairs one moment's flag
        with another moment's diagnostic. Forced here rather than raced, because the
        window between publishing a frame and building its outcome is real but too narrow
        to hit on demand.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(root, "import sys\nsys.stdin.buffer.readline()\n")
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                published = {"stderr": b"early", "stderr_truncated": False}
                with supervisor._stderr_lock:
                    supervisor._stderr = bytearray(b"L" * MAX_STDERR_BYTES_PER_CONNECTION)
                    supervisor._stderr_truncated = True
                outcome = supervisor._outcome(response="{}", diagnostics=published)
                self.assertEqual(b"early", outcome.stderr)
                self.assertFalse(outcome.stderr_truncated)

    def test_an_outcome_with_no_frame_reads_the_live_diagnostic(self) -> None:
        """A timeout has no published snapshot to be consistent with."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(root, "import sys\nsys.stdin.buffer.readline()\n")
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                with supervisor._stderr_lock:
                    supervisor._stderr = bytearray(b"live")
                    supervisor._stderr_truncated = True
                outcome = supervisor._outcome(fault="REQUEST_TIMEOUT")
                self.assertEqual(b"live", outcome.stderr)
                self.assertTrue(outcome.stderr_truncated)

    def test_stderr_keeps_draining_after_stdout_closes(self) -> None:
        """A child may close stdout and then write cleanup diagnostics.

        Abandoning the descriptor at stdout EOF can block the child mid-write, and the
        protocol requires draining through process exit rather than through stdout.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                f"""
                import os, sys
                sys.stdin.buffer.readline()
                os.close(sys.stdout.fileno())
                sys.stderr.buffer.write(b"z" * {MAX_STDERR_BYTES_PER_CONNECTION + 4096})
                sys.stderr.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertEqual("PROCESS_CLOSED", outcome.fault)
                deadline = time.monotonic() + 5
                while not supervisor._stderr_truncated and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(
                    supervisor._stderr_truncated,
                    "post-stdout-EOF diagnostics were never drained",
                )

    def test_a_noisy_child_does_not_starve_the_response(self) -> None:
        """Draining must not be able to hold up stdout processing.

        A single pump that emptied stderr before every published frame let a child that
        keeps stderr continuously readable stall the caller into REQUEST_TIMEOUT while a
        complete response was already waiting.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys, threading
                def noise():
                    while True:
                        sys.stderr.buffer.write(b"n" * 4096)
                        sys.stderr.buffer.flush()
                threading.Thread(target=noise, daemon=True).start()
                sys.stdin.buffer.readline()
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"r1","result":{}}\\n')
                sys.stdout.buffer.flush()
                sys.stdin.buffer.readline()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
                self.assertNotEqual(
                    "REQUEST_TIMEOUT", outcome.fault,
                    "the response was ready; stderr noise must not delay it",
                )

    def test_a_timeout_returns_even_when_a_descendant_holds_the_pipe(self) -> None:
        """Terminating the child is not enough when a grandchild inherited the pipe.

        The descendant keeps stdout/stderr open after its parent dies, so the pump stays
        blocked in read and the later `stream.close()` waits on the same BufferedReader
        lock -- the timeout never returns and the caller hangs forever. Killing the whole
        process group is what actually closes the descriptors.

        The adapter forks a child that outlives it and then answers nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import os, sys, time
                if os.fork() == 0:
                    # Inherits stdout/stderr and outlives its parent.
                    time.sleep(60)
                    os._exit(0)
                sys.stdin.readline()
                time.sleep(60)
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                started = time.monotonic()
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=1,
                )
                elapsed = time.monotonic() - started
        self.assertEqual("REQUEST_TIMEOUT", outcome.fault)
        self.assertTrue(outcome.should_close)
        self.assertLess(
            elapsed, 20,
            "teardown blocked behind a descendant's inherited pipe handle",
        )

    def test_an_oversized_frame_reports_what_close_drained(self) -> None:
        """close() can drain more stderr through process exit, so read it after.

        A child that publishes an oversized frame and then writes an overflowing
        diagnostic used to return stderr=b'' with truncated=False, while the supervisor's
        own live state held the retained prefix and a true flag -- the outcome
        contradicted the object that produced it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stdin.readline()
                sys.stdout.buffer.write(b"x" * (1_048_576 + 8) + b"\\n")
                sys.stdout.buffer.flush()
                sys.stderr.buffer.write(b"e" * (65_536 * 2))
                sys.stderr.buffer.flush()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                outcome = supervisor.request(
                    '{"jsonrpc":"2.0","id":"r1","method":"runtime.health","params":{}}',
                    timeout_seconds=5,
                )
        self.assertEqual("MESSAGE_TOO_LARGE", outcome.fault)
        # NOT asserted here: that the diagnostics are non-empty. Whether the drain has
        # consumed the child's stderr by this point is a race, so asserting it would pass
        # or fail on scheduling -- I wrote that assertion first and it passed against the
        # unfixed code, which is how I know it discriminates nothing. The ordering
        # property is pinned structurally below instead.
        self.assertTrue(outcome.should_close)

    def test_a_close_first_fault_reads_diagnostics_after_the_close(self) -> None:
        """The ordering is the fix, and only the ordering can be asserted.

        `close()` can drain more stderr through process exit, so a fault that closes must
        build its outcome afterwards. Whether any bytes have arrived by then is a race
        and unassertable; that the read happens after the close is not.

        Structural because behavioural is impossible here: the branch must not pass a
        pre-close snapshot at all.
        """
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        request = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "request"
        )
        closing_faults = {"MESSAGE_TOO_LARGE", "INVALID_FRAMING"}
        seen = set()
        for call in ast.walk(request):
            if not isinstance(call, ast.Call):
                continue
            faults = [
                kw.value.value for kw in call.keywords
                if kw.arg == "fault" and isinstance(kw.value, ast.Constant)
            ]
            if not faults or faults[0] not in closing_faults:
                continue
            seen.add(faults[0])
            self.assertNotIn(
                "diagnostics", [kw.arg for kw in call.keywords],
                f"{faults[0]} closes first, so it must read the live state after close() "
                "rather than carry the publication snapshot past it",
            )
        self.assertEqual(closing_faults, seen, "a close-first fault went unchecked")

    def test_unsolicited_frames_cannot_size_the_hosts_memory(self) -> None:
        """Every frame used to copy the retained 64 KiB prefix into an unbounded queue.

        A child that fills the stderr budget and then emits many tiny frames multiplied
        one bounded buffer into an unbounded one -- about 16k frames is a gigabyte of the
        same duplicated bytes. Two defences, both asserted: the snapshot is SHARED by
        reference rather than copied, and the queue itself stops accepting frames.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = write_script(
                root,
                """
                import sys
                sys.stderr.buffer.write(b"e" * (65_536 * 2))
                sys.stderr.buffer.flush()
                for _ in range(4000):
                    sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":"x","result":{}}\\n')
                sys.stdout.buffer.flush()
                sys.stdin.readline()
                """,
            )
            with StdioSupervisor(resolved_adapter(script, root)) as supervisor:
                deadline = time.monotonic() + 5
                while supervisor._stdout.qsize() < MAX_PENDING_FRAMES and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.2)
                depth = supervisor._stdout.qsize()
                snapshots = [
                    item[1] for item in list(supervisor._stdout.queue) if item is not None
                ]
        self.assertLessEqual(
            depth, MAX_PENDING_FRAMES + 1,
            f"the child queued {depth} frames; the cap must stop it",
        )
        distinct = {id(snapshot) for snapshot in snapshots}
        self.assertLessEqual(
            len(distinct), 2,
            "each frame carried its own copy of the diagnostic instead of sharing one",
        )

    def test_the_stderr_verdict_can_only_be_escalated_by_live_state(self) -> None:
        """Inverted 2026-07-27: this asserted the defect as contract.

        It used to require that `request` never read `_stderr_truncated`, on the argument
        that reading it races the drain. But the two pumps are independent, so the stdout
        thread can snapshot BEFORE the stderr thread has consumed overflow bytes already
        sitting in the pipe -- and trusting the published verdict alone then returned a
        plain success on a connection that had already breached its budget, decided by
        whichever thread ran first.

        Truncation is monotonic, so the live flag can only ever turn a success into a
        fault, never a fault into a success. That is the property worth pinning: the read
        must exist, and it must be used to ESCALATE. A read that could downgrade -- a
        published `True` overridden by a live `False` -- would reintroduce the race in
        the direction that actually loses a signal.
        """
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        request = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "request"
        )
        reads = [
            node
            for node in ast.walk(request)
            if isinstance(node, ast.Attribute) and node.attr == "_stderr_truncated"
        ]
        self.assertTrue(
            reads, "request must consult the live flag or the publication race survives"
        )
        self.assertIn("truncated_when_published or truncated_now", source)
        # The escalation must be a disjunction. `and` would require both, so a breach the
        # drain recorded after publication would once again pass as success.
        self.assertNotIn("truncated_when_published and truncated_now", source)

    def test_the_reader_stops_at_the_protocol_frame_bound(self) -> None:
        """The stop limit is exact, so the read must be limited rather than the buffer.

        Assembling lines from fixed-size raw reads overshot `MAX_MESSAGE_BYTES + 1` by up
        to one read before the check ran, which is buffering an untrusted process past a
        bound the protocol states precisely.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        readlines = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "readline"
        ]
        self.assertEqual(1, len(readlines), "one bounded stdout read")
        self.assertEqual(1, len(readlines[0].args), "readline must carry its limit")
        # The VALUE, not merely its presence. The normative limit is to stop buffering
        # after MAX_MESSAGE_BYTES + 1, and readline counts the newline, so +1 already
        # admits the largest valid payload plus its terminator and still identifies an
        # unterminated oversized frame. +2 buffered one byte past the contract, and
        # asserting only that a limit exists could not see that.
        limit = readlines[0].args[0]
        self.assertIsInstance(limit, ast.BinOp)
        self.assertEqual("MAX_MESSAGE_BYTES", limit.left.id)
        self.assertEqual(1, limit.right.value, "the bound must be MAX_MESSAGE_BYTES + 1")

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


if __name__ == "__main__":
    unittest.main()
