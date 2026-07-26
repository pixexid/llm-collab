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
import select
import subprocess
import threading

from llm_collab.runtime_adapter_manifest import ManifestResolutionError, ResolvedAdapter


MAX_MESSAGE_BYTES = 1_048_576
MAX_STDERR_BYTES_PER_CONNECTION = 65_536
# A pipe holds at most 64 KiB today, but a single read is not promised to
# empty it, so the drain loops until the descriptor would block.
PIPE_READ_BYTES = 65_536


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
        self._stdout: queue.Queue[tuple[bytes, bool] | None] = queue.Queue()
        self._stderr = bytearray()
        self._stderr_truncated = False
        self._stderr_lock = threading.Lock()
        self._pump_thread: threading.Thread | None = None

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
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()
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
            published = self._stdout.get(timeout=timeout_seconds)
        except queue.Empty:
            self.close()
            return self._outcome(fault="REQUEST_TIMEOUT", should_close=True)
        if published is None:
            return self._outcome(fault="PROCESS_CLOSED", should_close=True)
        raw, truncated_when_published = published
        if len(raw) > MAX_MESSAGE_BYTES + 1 or not raw.endswith(b"\n"):
            self.close()
            return self._outcome(fault="MESSAGE_TOO_LARGE", should_close=True)
        if truncated_when_published:
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
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1)

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

    def _pump(self) -> None:
        """Consume both child streams from one thread, stderr always first.

        A response is only published after every stderr byte that was readable
        at that moment has been recorded. The child writes its stderr before
        the response, so those bytes reach the pipe first and are therefore
        readable no later than the response is; draining them ahead of the
        publish makes the truncation verdict a consequence of stream order
        rather than of how promptly a second thread happened to be scheduled.
        """
        process = self._process
        stdout = process.stdout if process is not None else None
        stderr = process.stderr if process is not None else None
        if stdout is None:
            self._stdout.put(None)
            return
        pending = bytearray()
        open_streams = [stream for stream in (stdout, stderr) if stream is not None]
        while open_streams:
            try:
                ready, _, _ = select.select(open_streams, [], [])
            except (OSError, ValueError):
                break
            if stderr in ready and not self._drain_stderr(stderr):
                open_streams.remove(stderr)
            if stdout in ready:
                chunk = self._read_available(stdout)
                if chunk:
                    pending.extend(chunk)
                    self._publish_frames(pending)
                    continue
                if pending:
                    self._publish(bytes(pending))
                self._stdout.put(None)
                return
        self._stdout.put(None)

    def _drain_stderr(self, stderr) -> bool:
        """Record everything readable on stderr now. False once it is at EOF."""
        while True:
            chunk = self._read_available(stderr)
            if not chunk:
                return False
            with self._stderr_lock:
                remaining = MAX_STDERR_BYTES_PER_CONNECTION - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._stderr_truncated = True
            try:
                if not select.select([stderr], [], [], 0)[0]:
                    return True
            except (OSError, ValueError):
                return False

    @staticmethod
    def _read_available(stream) -> bytes:
        try:
            return os.read(stream.fileno(), PIPE_READ_BYTES)
        except (OSError, ValueError):
            return b""

    def _publish_frames(self, pending: bytearray) -> None:
        while True:
            newline = pending.find(b"\n")
            if newline >= 0:
                self._publish(bytes(pending[: newline + 1]))
                del pending[: newline + 1]
                continue
            if len(pending) > MAX_MESSAGE_BYTES + 1:
                self._publish(bytes(pending))
                pending.clear()
            return

    def _publish(self, frame: bytes) -> None:
        """Enqueue a frame with the stderr verdict taken in the drain's own lock.

        The verdict travels with the frame instead of being read from shared
        state by the requesting thread, so a caller cannot observe a stderr
        overflow that this frame preceded, nor miss one that it followed.
        """
        with self._stderr_lock:
            self._stdout.put((frame, self._stderr_truncated))

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
