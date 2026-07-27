"""Stdio process boundary for Runtime Adapter JSON-RPC V1.

This module owns only the physical process and stdio boundary. It does not
resolve manifests, schedule health checks, persist state, or route production
traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import queue
import selectors
import subprocess
import threading

from llm_collab.runtime_adapter_manifest import ManifestResolutionError, ResolvedAdapter


MAX_MESSAGE_BYTES = 1_048_576
MAX_STDERR_BYTES_PER_CONNECTION = 65_536


@dataclass(frozen=True)
class SupervisorOutcome:
    response: str | None = None
    fault: str | None = None
    should_close: bool = False
    stderr: bytes = b""
    stderr_truncated: bool = False


class StdioSupervisor:
    """Context-managed stdio supervisor for one resolved adapter process."""

    def __init__(self, resolved: ResolvedAdapter):
        if not isinstance(resolved, ResolvedAdapter):
            raise TypeError("resolved must be a ResolvedAdapter")
        self._resolved = resolved
        self._process: subprocess.Popen[bytes] | None = None
        self._stdout: queue.Queue[bytes | None] = queue.Queue()
        self._stderr = bytearray()
        self._stderr_truncated = False
        self._stderr_lock = threading.Lock()
        self._stderr_barriers: queue.Queue[threading.Event] = queue.Queue()
        self._stderr_done = threading.Event()
        self._stderr_failed = threading.Event()
        self._stderr_wakeup: tuple[int, int] | None = None
        self._lifecycle_lock = threading.RLock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    def __enter__(self) -> StdioSupervisor:
        self._validate_spawn_paths()
        process = subprocess.Popen(
            self._resolved.argv,
            executable=self._resolved.executable,
            cwd=self._resolved.working_directory,
            env=dict(self._resolved.environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        self._process = process
        try:
            if process.stderr is not None:
                os.set_blocking(process.stderr.fileno(), False)
            self._stderr_wakeup = os.pipe()
            for fd in self._stderr_wakeup:
                os.set_blocking(fd, False)
            self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
            self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self._stdout_thread.start()
            self._stderr_thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def request(self, frame: str, *, timeout_seconds: float = 5.0) -> SupervisorOutcome:
        process = self._require_process()
        if self._stderr_failed.is_set():
            self.close()
            return self._outcome(fault="STDERR_DRAIN_FAILED", should_close=True)
        stdin = process.stdin
        if stdin is None:
            return self._outcome(fault="PROCESS_CLOSED", should_close=True)
        try:
            stdin.write(frame.encode("utf-8") + b"\n")
            stdin.flush()
        except (BrokenPipeError, OSError):
            return self._outcome(fault="PROCESS_CLOSED", should_close=True)

        try:
            raw = self._stdout.get(timeout=timeout_seconds)
        except queue.Empty:
            self.close()
            return self._outcome(fault="REQUEST_TIMEOUT", should_close=True)
        if self._stderr_failed.is_set():
            self.close()
            return self._outcome(fault="STDERR_DRAIN_FAILED", should_close=True)
        if raw is None:
            return self._outcome(fault="PROCESS_CLOSED", should_close=True)
        if len(raw) > MAX_MESSAGE_BYTES + 1 or not raw.endswith(b"\n"):
            self.close()
            return self._outcome(fault="MESSAGE_TOO_LARGE", should_close=True)
        if self._stderr_truncated:
            return self._outcome(fault="STDERR_LIMIT_EXCEEDED", should_close=True)
        try:
            return self._outcome(response=raw[:-1].decode("utf-8"))
        except UnicodeDecodeError:
            self.close()
            return self._outcome(fault="INVALID_FRAMING", should_close=True)

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close()

    def _close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self._wake_stderr()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._stderr_done.set()
        self._release_stderr_barriers()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.ident is not None:
                thread.join(timeout=1)
        wakeup = self._stderr_wakeup
        self._stderr_wakeup = None
        if wakeup is not None:
            for fd in wakeup:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _validate_spawn_paths(self) -> None:
        if not Path(self._resolved.executable).is_absolute():
            raise ManifestResolutionError("executable must be absolute before spawn")
        if not Path(self._resolved.working_directory).is_absolute():
            raise ManifestResolutionError("working_directory must be absolute before spawn")

    def _require_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None:
            raise RuntimeError("supervisor process is not running")
        return process

    def _read_stdout(self) -> None:
        process = self._process
        stdout = process.stdout if process is not None else None
        if stdout is None:
            self._stdout.put(None)
            return
        while True:
            try:
                line = stdout.readline(MAX_MESSAGE_BYTES + 2)
            except OSError:
                self._stdout.put(None)
                return
            if line == b"":
                self._stdout.put(None)
                return
            self._publish(line)

    def _drain_stderr(self) -> None:
        process = self._process
        stderr = process.stderr if process is not None else None
        wakeup = self._stderr_wakeup
        if stderr is None or wakeup is None:
            self._stderr_done.set()
            self._release_stderr_barriers()
            return
        stderr_fd = stderr.fileno()
        wake_read, _ = wakeup
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(stderr_fd, selectors.EVENT_READ)
                selector.register(wake_read, selectors.EVENT_READ)
                while True:
                    events = selector.select()
                    barriers = self._take_stderr_barriers()
                    if any(key.fd == wake_read for key, _ in events):
                        self._drain_fd(wake_read)
                    stderr_closed = self._drain_stderr_fd(stderr_fd)
                    for barrier in barriers:
                        barrier.set()
                    if stderr_closed:
                        return
        except Exception:
            self._stderr_failed.set()
            self._stdout.put(None)
        finally:
            self._stderr_done.set()
            self._release_stderr_barriers()

    def _drain_stderr_fd(self, fd: int) -> bool:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                return False
            except OSError:
                return True
            if not chunk:
                return True
            with self._stderr_lock:
                remaining = MAX_STDERR_BYTES_PER_CONNECTION - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._stderr_truncated = True

    @staticmethod
    def _drain_fd(fd: int) -> None:
        while True:
            try:
                if not os.read(fd, 4096):
                    return
            except (BlockingIOError, OSError):
                return

    def _publish(self, frame: bytes) -> None:
        barrier = threading.Event()
        self._stderr_barriers.put(barrier)
        if self._stderr_done.is_set():
            self._release_stderr_barriers()
        else:
            self._wake_stderr()
        barrier.wait()
        self._stdout.put(frame)

    def _wake_stderr(self) -> None:
        with self._lifecycle_lock:
            if self._stderr_wakeup is None:
                return
            try:
                os.write(self._stderr_wakeup[1], b"\0")
            except (BlockingIOError, OSError):
                pass

    def _take_stderr_barriers(self) -> list[threading.Event]:
        barriers = []
        while True:
            try:
                barriers.append(self._stderr_barriers.get_nowait())
            except queue.Empty:
                return barriers

    def _release_stderr_barriers(self) -> None:
        for barrier in self._take_stderr_barriers():
            barrier.set()

    def _outcome(
        self,
        *,
        response: str | None = None,
        fault: str | None = None,
        should_close: bool = False,
    ) -> SupervisorOutcome:
        with self._stderr_lock:
            return SupervisorOutcome(
                response=response,
                fault=fault,
                should_close=should_close,
                stderr=bytes(self._stderr),
                stderr_truncated=self._stderr_truncated,
            )
