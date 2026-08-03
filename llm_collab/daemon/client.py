"""Small Unix-socket client for the workspace daemon."""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

from llm_collab.ledger import LedgerPaths


RESPONSE_LIMIT = 64 * 1024


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
