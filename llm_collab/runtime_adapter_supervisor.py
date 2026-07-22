"""Stdio process boundary for Runtime Adapter JSON-RPC V1.

This module owns only the physical process and stdio boundary. It does not
resolve manifests, schedule health checks, persist state, or route production
traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import queue
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
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def request(self, frame: str, *, timeout_seconds: float = 5.0) -> SupervisorOutcome:
        process = self._require_process()
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
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=1)

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
            self._stdout.put(line)

    def _drain_stderr(self) -> None:
        process = self._process
        stderr = process.stderr if process is not None else None
        if stderr is None:
            return
        while True:
            try:
                chunk = stderr.read(4096)
            except OSError:
                return
            if not chunk:
                return
            with self._stderr_lock:
                remaining = MAX_STDERR_BYTES_PER_CONNECTION - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._stderr_truncated = True

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
