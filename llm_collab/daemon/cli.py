"""Fixed local CLI for the inert daemon."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from llm_collab.daemon.server import DEADLINE_SECONDS, RESPONSE_LIMIT, DaemonServer, parse_request
from llm_collab.ledger import LedgerPaths


def _workspace_root() -> Path:
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "collab.config.json").is_file():
            return candidate
    raise RuntimeError("collab.config.json not found; workspace_id is required")


def _paths() -> LedgerPaths:
    root = _workspace_root()
    try:
        config = json.loads((root / "collab.config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot read collab.config.json") from exc
    workspace_id = config.get("workspace_id") if isinstance(config, dict) else None
    if not workspace_id:
        raise RuntimeError("collab.config.json is missing required workspace_id; run init --add-workspace-id")
    state_root = config.get("project_state_root") or root / "projects"
    state_root = Path(state_root).expanduser()
    if not state_root.is_absolute():
        state_root = root / state_root
    return LedgerPaths.derive(state_root, workspace_id)


def _request(
    paths: LedgerPaths,
    op: str,
    *,
    timeout: float = DEADLINE_SECONDS,
    clock=time.monotonic,
) -> object:
    if timeout <= 0:
        raise TimeoutError("daemon I/O deadline exceeded")
    payload = json.dumps({"version": 1, "op": op}, separators=(",", ":")).encode("utf-8")
    parse_request(payload)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = clock() + timeout
        def set_remaining() -> None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("daemon I/O deadline exceeded")
            client.settimeout(remaining)
        set_remaining()
        client.connect(os.fspath(paths.socket))
        set_remaining()
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        total = 0
        while True:
            set_remaining()
            chunk = client.recv(min(4096, RESPONSE_LIMIT + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > RESPONSE_LIMIT:
                raise RuntimeError("daemon response is oversized")
            chunks.append(chunk)
        response = b"".join(chunks)
        if not response:
            raise RuntimeError("daemon returned no response")
        if len(response) > RESPONSE_LIMIT:
            raise RuntimeError("daemon response is oversized")
        return json.loads(response.decode("utf-8"))
    finally:
        client.close()


def _background(paths: LedgerPaths) -> int:
    command = [sys.executable, str(Path(__file__).parents[2] / "bin" / "llm_collabd.py"), "daemon", "start"]
    child = subprocess.Popen(
        command,
        cwd=_workspace_root(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if child.poll() is not None:
            raise RuntimeError("background daemon exited before readiness")
        try:
            status = _request(paths, "status", timeout=remaining)
            if time.monotonic() >= deadline:
                continue
            if (
                isinstance(status, dict)
                and status.get("running") is True
                and status.get("pid") == child.pid
                and child.poll() is None
            ):
                return 0
        except (OSError, RuntimeError, json.JSONDecodeError):
            time.sleep(0.02)
    child.terminate()
    try:
        child.wait(timeout=DEADLINE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=DEADLINE_SECONDS)
    raise RuntimeError("background daemon did not become ready within 2 seconds")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    daemon_subcommand = argv[:1] == ["daemon"]
    if daemon_subcommand:
        argv.pop(0)
    command = argv.pop(0) if argv else None
    background = command == "start" and argv == ["--background"]
    if (
        command not in {"start", "stop", "status", "logs", "doctor"}
        or (daemon_subcommand and command == "doctor")
        or (argv and not background)
    ):
        print("usage: llm-collabd daemon <start|stop|status|logs> | doctor", file=sys.stderr)
        return 2
    try:
        paths = _paths()
        if command == "start":
            if background:
                return _background(paths)
            DaemonServer(paths).run()
            return 0
        if command == "doctor":
            print(json.dumps({"workspace_id": paths.workspace_id, "socket": str(paths.socket), "status": _request(paths, "status")}, separators=(",", ":")))
            return 0
        response = _request(paths, {"stop": "shutdown", "status": "status", "logs": "logs"}[command])
        print(json.dumps(response, separators=(",", ":")))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"llm-collabd: {exc}", file=sys.stderr)
        return 1
