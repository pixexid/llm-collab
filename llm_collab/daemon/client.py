"""Small Unix-socket client for the workspace daemon."""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Mapping
from typing import Any

from llm_collab.ledger import LedgerPaths


RESPONSE_LIMIT = 64 * 1024


def project_dispatch_session(session: Mapping[str, object]) -> dict[str, object]:
    """Return the closed session shape consumed by daemon dispatch."""
    runtime = session.get("runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("worker session is missing runtime identity")
    required_runtime = ("session_id", "instance_id", "home")
    if any(not isinstance(runtime.get(key), str) or not runtime[key] for key in required_runtime):
        raise ValueError("worker session is missing exact runtime identity")
    repo_targets = session.get("repo_targets")
    if not isinstance(repo_targets, (list, tuple)) or any(
        not isinstance(repo, str) or not repo or len(repo) > 128 for repo in repo_targets
    ) or len(repo_targets) > 64:
        raise ValueError("worker session repo targets are invalid")
    projection: dict[str, object] = {
        key: session.get(key)
        for key in (
            "project_id",
            "chat_id",
            "agent_id",
            "status",
            "endpoint_id",
            "binding_id",
            "binding_generation",
        )
    }
    projection["repo_targets"] = list(repo_targets)
    projection["session_id"] = session.get("session_id") or runtime["session_id"]
    projection["runtime"] = {key: runtime[key] for key in required_runtime}
    if len(json.dumps(projection, separators=(",", ":"))) > 2048:
        raise ValueError("worker dispatch session projection is oversized")
    return projection


def request(
    paths: LedgerPaths,
    op: str,
    *,
    request: dict[str, object] | None = None,
    timeout: float = 2.0,
) -> Any:
    if timeout <= 0:
        raise TimeoutError("daemon I/O deadline exceeded")
    body: dict[str, object] = {"version": 1, "op": op}
    if request is not None:
        if op != "dispatch":
            raise ValueError("request payload is only valid for dispatch")
        body["request"] = request
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + timeout

        def set_remaining() -> None:
            remaining = deadline - time.monotonic()
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
        if not chunks:
            raise RuntimeError("daemon returned no response")
        return json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        client.close()
