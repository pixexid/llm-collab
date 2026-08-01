#!/usr/bin/env python3
"""Disposable Paseo Phase 0 setup plus small output classifiers."""

from __future__ import annotations

import argparse
import json
import os
import re
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
BEFORE_SUBMISSION_CODES = {
    "AGENT_CREATE_FAILED",
    "AGENT_NOT_FOUND",
    "DAEMON_NOT_RUNNING",
    "INVALID_AGENT_ID",
}


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

    if "error" in record:
        return "error"
    pending = record.get("PendingPermissions", record.get("pendingPermissions", []))
    if pending:
        return "permission"
    status = record.get("Status", record.get("status"))
    if status == "running":
        return "running"
    if status == "idle":
        return "idle"
    if status in {"error", "failed"}:
        return "error"
    return "unknown"


def _error_code(record: dict[str, Any]) -> str | None:
    error = record.get("error")
    return error.get("code") if isinstance(error, dict) else None


def classify_transport(record: dict[str, Any]) -> str:
    """Classify only what the CLI response proves about submission."""

    if _error_code(record) in BEFORE_SUBMISSION_CODES:
        return "rejected_before_submission"
    status = record.get("status")
    if status == "sent":
        return "submitted_best_effort"
    if status == "completed":
        return "native_completed_best_effort"
    if status in {"timeout", "unknown"} or _error_code(record):
        return "acceptance_unknown"
    return "unknown"


def correlation_ids(record: dict[str, Any]) -> dict[str, Any]:
    """Return only explicit per-send correlation fields; agentId is not one."""

    return {key: record[key] for key in CORRELATION_KEYS if key in record}


def has_stable_correlation(
    send_record: dict[str, Any], timeline_records: list[dict[str, Any]]
) -> bool:
    """Require one explicit ID to be present in both send and timeline output."""

    sent = correlation_ids(send_record)
    return any(
        sent.get(key) is not None
        and any(item.get(key) == sent[key] for item in timeline_records)
        for key in sent
    )


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

    @property
    def host(self) -> str:
        if self.port is None:
            raise RuntimeError("Paseo is not started")
        return f"127.0.0.1:{self.port}"

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
                try:
                    result = subprocess.run(
                        [self.paseo_bin, "ls", "--json", "--host", self.host],
                        cwd=self.repo,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode == 0:
                        return self
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
    with DisposablePaseo(args.paseo_bin) as paseo:
        raw = subprocess.run(
            [args.paseo_bin, "ls", "--json", "--host", paseo.host],
            cwd=paseo.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        version = subprocess.run(
            [args.paseo_bin, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        print(
            json.dumps(
                {
                    "version": version,
                    "host": paseo.host,
                    "home": "<DISPOSABLE_PASEO_HOME>",
                    "repository": "<DISPOSABLE_REPOSITORY>",
                    "agents": parse_json_document(raw),
                    "cleanup": "completed",
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
