#!/usr/bin/env python3
"""Prompt one exact Livecraft Pi session to drain its durable inbox."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from livecraft_health import ensure_livecraft_ready


MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


def _read_input() -> dict:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("runtime wake payload exceeds the byte limit")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime wake payload is not an object")
    return payload


def _required(mapping: dict, key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"runtime wake payload lacks {key}")
    return value


def _drain_command(*, python_executable: str, runtime_root: str, agent: str,
                   logical: str, native: str, project: str, chat: str, repo: str) -> str:
    parts = [
        f"LLM_COLLAB_READER_RUNTIME_ID={shlex.quote(native)}",
        "LLM_COLLAB_READER_RUNTIME_FAMILY=pi",
        shlex.quote(python_executable),
        shlex.quote(str(Path(runtime_root) / "bin" / "inbox.py")),
        "--me", shlex.quote(agent), "--session", shlex.quote(logical),
        "--project", shlex.quote(project), "--chat", shlex.quote(chat),
        "--repo-target", shlex.quote(repo), "--acknowledge", "--json",
    ]
    return " ".join(parts)


def _prompt(*, backend_url: str, native: str, message: str) -> dict:
    path = "/api/sessions/" + urllib.parse.quote(native, safe="") + "/commands"
    request = urllib.request.Request(
        backend_url.rstrip("/") + path,
        data=json.dumps({"type": "prompt", "message": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Livecraft wake response exceeds the byte limit")
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Livecraft wake failed (HTTP {response.status})")
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Livecraft wake failed (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Livecraft wake request failed: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args(argv)
    try:
        payload = _read_input()
        session = payload.get("session")
        message = payload.get("message")
        if not isinstance(session, dict) or not isinstance(message, dict):
            raise ValueError("runtime wake payload lacks session/message objects")
        agent = _required(session, "agent_id")
        logical = _required(session, "session_id")
        native = _required(session, "runtime_session_id")
        project = _required(session, "project_id")
        chat = _required(session, "chat_id")
        repo_targets = session.get("repo_targets")
        if not isinstance(repo_targets, list) or len(repo_targets) != 1:
            raise ValueError("runtime wake payload lacks one exact repository target")
        repo = _required({"repo": repo_targets[0]}, "repo")
        path = _required(message, "path")
        drain = _drain_command(
            python_executable=sys.executable, runtime_root=args.runtime_root,
            agent=agent, logical=logical, native=native,
            project=project, chat=chat, repo=repo,
        )
        prompt = (
            "A new durable llm-collab packet is waiting for this exact Livecraft worker.\n"
            f"Packet path: {path}\n"
            "Run this exact command first, then follow only the packet bodies returned by it:\n"
            f"{drain}\n"
            "Do not act on this wake prompt alone, do not inspect another worker's inbox, "
            "and do not start a second watcher."
        )
        ensure_livecraft_ready(args.backend_url)
        result = _prompt(backend_url=args.backend_url, native=native, message=prompt)
        print(json.dumps({"prompted": True, "native_session_id": native, "result": result}))
        return 0
    except Exception as exc:
        print(f"livecraft_wake: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
