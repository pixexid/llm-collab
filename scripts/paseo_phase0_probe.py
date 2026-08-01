#!/usr/bin/env python3
"""Disposable Paseo Phase 0 setup plus small output classifiers."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


AGENT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CORRELATION_KEYS = ("runId", "messageId", "turnId", "eventId")
JSON_START = re.compile(r"(?m)^[ \t]*[\[{]")
MAX_OUTPUT_BYTES = 64 * 1024
BEFORE_SUBMISSION_CODES = {
    "AGENT_CREATE_FAILED",
    "AGENT_NOT_FOUND",
    "DAEMON_NOT_RUNNING",
    "INVALID_AGENT_ID",
}
_MALFORMED = object()


class OutputLimitExceeded(RuntimeError):
    pass


def parse_json_document(raw: str) -> Any:
    """Parse exactly one JSON document, allowing only trailing whitespace."""

    return json.loads(raw)


def parse_mixed_json(raw: str) -> Any:
    """Parse one JSON value after Paseo's optional human-readable preamble."""

    decoder = json.JSONDecoder()
    match = JSON_START.search(raw)
    if match is None:
        raise ValueError("no complete JSON value found")
    offset = match.start() + len(match.group()) - 1
    preamble = raw[:offset].strip()
    if preamble and any(
        not line.startswith(("Created workspace ", "Tip: "))
        for line in preamble.splitlines()
        if line.strip()
    ):
        raise ValueError("unexpected text before JSON value")
    try:
        value, end = decoder.raw_decode(raw, offset)
    except json.JSONDecodeError as error:
        raise ValueError("no complete JSON value found") from error
    if raw[end:].strip():
        raise ValueError("unexpected text after JSON value")
    return value


def require_full_agent_id(value: str) -> str:
    """Accept only Paseo's canonical full UUID agent identifier."""

    if not isinstance(value, str) or not AGENT_ID.fullmatch(value):
        raise ValueError("agent identifier must be a full canonical UUID")
    return value


def classify_lifecycle(record: dict[str, Any]) -> str:
    """Classify inspect/permission JSON without guessing on unknown states."""

    if not isinstance(record, dict):
        return "unknown"
    if "error" in record:
        error = record["error"]
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), str)
            or not error["code"]
            or any(key in record for key in ("Status", "status", "PendingPermissions", "pendingPermissions"))
        ):
            return "unknown"
        return "error"
    status_keys = [key for key in ("Status", "status") if key in record]
    permission_keys = [
        key for key in ("PendingPermissions", "pendingPermissions") if key in record
    ]
    if len(status_keys) != 1 or len(permission_keys) != 1:
        return "unknown"
    status = record[status_keys[0]]
    pending = record[permission_keys[0]]
    if not isinstance(status, str) or not isinstance(pending, list):
        return "unknown"
    if status in {"running", "idle"} and pending:
        return "permission"
    if status == "running":
        return "running"
    if status == "idle":
        return "idle"
    if status in {"error", "failed"}:
        return "error"
    return "unknown"


def _error_code(record: dict[str, Any]) -> Any:
    if "error" not in record:
        return None
    error = record["error"]
    if not isinstance(error, dict):
        return _MALFORMED  # type: ignore[return-value]
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return _MALFORMED  # type: ignore[return-value]
    return code


def classify_transport(record: dict[str, Any]) -> str:
    """Classify only what the CLI response proves about submission."""

    if not isinstance(record, dict):
        return "unknown"
    if any(
        key in record and (not isinstance(record[key], str) or not record[key].strip())
        for key in ("agentId", "message")
    ):
        return "unknown"
    error_code = _error_code(record)
    status = record.get("status")
    if error_code is _MALFORMED or ("status" in record and not isinstance(status, str)):
        return "unknown"
    if error_code is not None and "status" in record:
        return "acceptance_unknown"
    if error_code in BEFORE_SUBMISSION_CODES:
        return "rejected_before_submission"
    if error_code is not None:
        return "acceptance_unknown"
    if status not in {"sent", "completed", "timeout", "unknown"}:
        return "unknown"
    if status == "sent":
        return "submitted_best_effort"
    if status == "completed":
        return "native_completed_best_effort"
    if status in {"timeout", "unknown"} or _error_code(record):
        return "acceptance_unknown"
    return "unknown"


def correlation_ids(record: dict[str, Any]) -> dict[str, Any]:
    """Return only explicit per-send correlation fields; agentId is not one."""

    if not isinstance(record, dict):
        return {}
    return {
        key: record[key]
        for key in CORRELATION_KEYS
        if isinstance(record.get(key), str) and record[key].strip()
    }


def has_stable_correlation(
    send_record: dict[str, Any], timeline_records: list[dict[str, Any]]
) -> bool:
    """Require one explicit ID to be present in both send and timeline output."""

    if not isinstance(timeline_records, list):
        return False
    sent = correlation_ids(send_record)
    return any(
        any(
            isinstance(item, dict)
            and isinstance(item.get(key), str)
            and item[key].strip()
            and item[key] == sent[key]
            for item in timeline_records
        )
        for key in sent
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)


def _run_bounded(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run one external command with a deadline and cumulative output cap."""

    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[Any, str]] = {}
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is not None:
            selector.register(stream, selectors.EVENT_READ, stream_name)
            streams[stream.fileno()] = (stream, stream_name)
    deadline = time.monotonic() + timeout
    total = 0
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(args, timeout)
            for key, _ in events:
                stream, stream_name = streams[key.fd]
                chunk = os.read(key.fd, min(8192, max_output_bytes - total + 1))
                if not chunk:
                    selector.unregister(stream)
                    streams.pop(key.fd)
                    stream.close()
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    raise OutputLimitExceeded(f"command output exceeds {max_output_bytes} bytes")
                buffers[stream_name].extend(chunk)
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            args,
            returncode,
            bytes(buffers["stdout"]).decode(errors="replace"),
            bytes(buffers["stderr"]).decode(errors="replace"),
        )
    except Exception:
        _stop_process(process)
        raise
    finally:
        selector.close()
        for stream, _ in streams.values():
            stream.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DisposablePaseo:
    """Own one exact temporary Paseo home, repo, daemon, and port."""

    def __init__(self, paseo_bin: str, timeout: float = 15.0) -> None:
        self.paseo_bin = paseo_bin
        self.timeout = timeout
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self.home: Path | None = None
        self.repo: Path | None = None
        self.port: int | None = None
        self._daemon: subprocess.Popen[str] | None = None
        self._env: dict[str, str] | None = None

    @property
    def host(self) -> str:
        if self.port is None:
            raise RuntimeError("Paseo is not started")
        return f"127.0.0.1:{self.port}"

    @property
    def env(self) -> dict[str, str]:
        if self._env is None:
            raise RuntimeError("Paseo is not started")
        return self._env

    def __enter__(self) -> "DisposablePaseo":
        self._tmp = tempfile.TemporaryDirectory(prefix="paseo-phase0-")
        try:
            root = Path(self._tmp.name)
            self.home = root / "home"
            self.repo = root / "repo"
            self.home.mkdir()
            self.repo.mkdir()
            self.port = _free_port()
            env = os.environ.copy()
            env["PASEO_HOME"] = str(self.home)
            self._env = env
            self._daemon = subprocess.Popen(
                [
                    self.paseo_bin,
                    "start",
                    "--listen",
                    self.host,
                    "--foreground",
                    "--no-relay",
                    "--no-mcp",
                    "--no-inject-mcp",
                    "--no-web-ui",
                ],
                cwd=self.repo,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                text=True,
            )
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self._daemon.poll() is not None:
                    raise RuntimeError(f"Paseo daemon exited with {self._daemon.returncode}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    result = _run_bounded(
                        [self.paseo_bin, "ls", "--json", "--host", self.host],
                        cwd=self.repo,
                        env=env,
                        timeout=remaining,
                    )
                    if result.returncode == 0:
                        return self
                except subprocess.TimeoutExpired:
                    break
                except OSError:
                    pass
                time.sleep(0.1)
            raise TimeoutError("Paseo daemon did not become ready")
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_: object) -> None:
        daemon = self._daemon
        if daemon is not None and daemon.poll() is None:
            try:
                os.killpg(daemon.pid, signal.SIGTERM)
                daemon.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if daemon.poll() is None:
                    os.killpg(daemon.pid, signal.SIGKILL)
                    daemon.wait(timeout=5)
        if self._tmp is not None:
            self._tmp.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paseo-bin",
        default="/Applications/Paseo.app/Contents/Resources/bin/paseo",
    )
    args = parser.parse_args()
    report: dict[str, Any]
    with DisposablePaseo(args.paseo_bin) as paseo:
        result = _run_bounded(
            [args.paseo_bin, "ls", "--json", "--host", paseo.host],
            cwd=paseo.repo,
            env=paseo.env,
            timeout=paseo.timeout,
        )
        result.check_returncode()
        version_result = _run_bounded(
            [args.paseo_bin, "--version"],
            cwd=paseo.repo,
            env=paseo.env,
            timeout=paseo.timeout,
        )
        version_result.check_returncode()
        report = {
            "version": version_result.stdout.strip(),
            "host": paseo.host,
            "home": "<DISPOSABLE_PASEO_HOME>",
            "repository": "<DISPOSABLE_REPOSITORY>",
            "agents": parse_json_document(result.stdout),
        }
    report["cleanup"] = "completed"
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
