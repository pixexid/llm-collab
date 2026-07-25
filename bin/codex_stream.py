#!/usr/bin/env python3
"""codex_stream.py — watch a worker's App Server thread live.

A peer client that resumes a thread receives the same event fanout as the client
that started the turn: turn/started, item/agentMessage/delta, item/completed,
turn/completed. So "what is Codex doing right now" needs no Accessibility
automation, no foreground window, and no shared-daemon refactor -- only a second
WebSocket connection to the App Server we already run.

Observation only. This never starts, steers, or interrupts a turn, and sends NO
response of any kind to a server-initiated request -- not a result, not an error.
A pending request can be resolved by the first client to answer it, so an observer
that replied could abort work the operator initiated in the desktop app.

Usage:
  python bin/codex_stream.py --agent codex --project amiga --chat CHAT-8976EECB
  python bin/codex_stream.py --agent codex --project amiga --chat last --seconds 60
  python bin/codex_stream.py --thread 019f9452-6954-7301-bff9-db1c47232bc8 --raw
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import socket
import time

import _session_autobridge as autobridge
from _helpers import ROOT

DEFAULT_IDLE_TIMEOUT_SECONDS = 5
BINDINGS_DIR = ROOT / "State" / "session_autobridge" / "bindings"


class ObserverClient(autobridge.JsonRpcWebSocketClient):
    """A connection that answers nothing at all.

    The base client answers any interleaved server request with `{"result": {}}`. Every
    member of the generated ServerRequest union is authority- or data-bearing -- command,
    file, and permission approvals, tool calls, user input, MCP elicitation, auth refresh,
    attestation -- and NO Response schema in the bundle permits an empty object. So that
    envelope is invalid for all ten methods, and for an observer it is indefensible.

    Refusing with a JSON-RPC error is also wrong HERE, though it is right for the client
    that owns a turn. App Server fans a pending request to the subscribed connections, and
    the first response -- result or error -- can resolve it. An observer that auto-errors
    can therefore abort work the operator initiated in ChatGPT.app before the UI is
    answered. The risk is asymmetric: if requests fan out, silence protects the operator
    and an error harms them; if they only ever reach the turn owner, this connection never
    sees one and silence costs nothing. Silence is never worse.

    So: log the request with its identity, respond nothing, keep reading. Receiving one at
    all is itself evidence of fan-out and is reported as such.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.observed_requests: list[str] = []

    def recv_json(self) -> dict:
        while True:
            message = super().recv_json()
            if message.get("id") is not None and message.get("method"):
                method = str(message["method"])
                self.observed_requests.append(method)
                params = message.get("params") or {}
                print(f"[request] {method} id={message['id']} "
                      f"thread={params.get('threadId', '?')} turn={params.get('turnId', '?')}",
                      file=sys.stderr)
                print("[action] observer answers nothing; only the turn's owner may respond",
                      file=sys.stderr)
                continue
            return message


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch a worker's Codex thread live.")
    p.add_argument("--agent", help="Agent id whose bound thread to watch")
    p.add_argument("--project", help="Project id (with --agent)")
    p.add_argument("--chat", help="Chat id, or 'last' for the newest binding (with --agent)")
    p.add_argument("--thread", help="Exact runtime thread id, bypassing binding lookup")
    p.add_argument("--runtime-home", default="/Users/pixexid/.codex", help="CODEX_HOME to discover")
    p.add_argument("--seconds", type=float, help="Stop after this long (default: until Ctrl-C)")
    p.add_argument("--raw", action="store_true", help="Print every notification as JSON")
    return p.parse_args()


def record_matches_path(record: dict, path: Path, agent: str) -> bool:
    """The record must be what its location and the invocation claim it is."""
    return (
        str(record.get("agent_id") or "") == agent
        and str(record.get("chat_id") or "") == path.parent.name
        and str(record.get("project_id") or "") == path.parent.parent.name
    )


def resolve_thread(args: argparse.Namespace) -> tuple[str, str]:
    """Return (thread_id, provenance). Exact bindings only -- never a heuristic guess."""
    if args.thread:
        return args.thread, "--thread"

    if not args.agent:
        raise SystemExit("[error] pass --thread, or --agent with --project/--chat")

    # Every supplied selector narrows the search independently, and is used as a LITERAL
    # path segment. Two bugs lived here in turn: --chat was ignored unless --project came
    # with it, so a named chat silently searched every chat; then the fix interpolated the
    # selectors into a glob pattern, where `CHAT-[A]` became a character class matching
    # CHAT-A and `--project '*'` matched every project. A selector is a name, not a
    # pattern, so only OMITTED levels are enumerated.
    def children(directory: Path) -> list[str]:
        try:
            return sorted(d.name for d in directory.iterdir() if d.is_dir())
        except OSError:
            return []

    chat_named = args.chat and args.chat != "last"
    projects = [args.project] if args.project else children(BINDINGS_DIR)
    candidates: list[Path] = []
    for project in projects:
        project_dir = BINDINGS_DIR / project
        chats = [args.chat] if chat_named else children(project_dir)
        for chat in chats:
            candidate = project_dir / chat / f"{args.agent}.json"
            if candidate.is_file():
                candidates.append(candidate)
    candidates.sort()

    bindings = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or not record.get("runtime_session_id"):
            continue
        # A record at the expected path is not proof of what the record IS. One claiming
        # a different project/chat/agent was admitted on its claims alone and its thread
        # watched, which crosses a project boundary rather than mislabelling a heading.
        if not record_matches_path(record, path, args.agent):
            continue
        bindings.append((record, path))

    if not bindings:
        raise SystemExit(f"[error] no binding with a runtime_session_id for agent {args.agent!r}")

    # An ambiguous --agent lookup must not silently watch one of several threads.
    active = [b for b in bindings if b[0].get("status") == "active"]
    chosen = active or bindings
    if len(chosen) > 1 and args.chat != "last":
        names = "\n  ".join(f"{b[0].get('project_id')}/{b[0].get('chat_id')}" for b in chosen)
        raise SystemExit(
            f"[error] {len(chosen)} bindings match agent {args.agent!r}; name one with "
            f"--project/--chat, or pass --chat last:\n  {names}"
        )
    record, path = max(chosen, key=lambda b: str(b[0].get("updated_utc") or ""))
    return str(record["runtime_session_id"]), f"{record.get('project_id')}/{record.get('chat_id')}"


def describe(method: str, params: dict) -> str | None:
    """One human line per event, or None to stay quiet."""
    if method == "turn/started":
        return f"turn started  {params.get('turn', {}).get('id', '?')}"
    if method == "turn/completed":
        turn = params.get("turn", {})
        return f"turn {turn.get('status', 'completed')}  {turn.get('id', '?')}"
    if method == "thread/status/changed":
        status = params.get("status") or {}
        return f"status: {status.get('type', status)}"
    if method == "item/started":
        item = params.get("item", {})
        kind = item.get("type", "item")
        if kind == "commandExecution":
            return f"  $ {str(item.get('command', ''))[:160]}"
        if kind == "fileChange":
            return f"  edit {str(item.get('path', ''))[:160]}"
        return f"  {kind} started"
    if method == "item/completed":
        item = params.get("item", {})
        if item.get("type") == "agentMessage":
            return None  # already streamed as deltas
        if item.get("type") == "commandExecution":
            return f"  $ exit={item.get('exitCode')}"
        return f"  {item.get('type', 'item')} done"
    if method == "thread/tokenUsage/updated":
        total = (params.get("tokenUsage") or {}).get("total") or {}
        if total.get("totalTokens"):
            return f"tokens: {total['totalTokens']:,}"
    return None


def main() -> None:
    args = parse_args()
    thread_id, provenance = resolve_thread(args)

    endpoint = autobridge.discover_codex_app_server(args.runtime_home)
    if endpoint is None:
        raise SystemExit(
            "[error] no Codex App Server endpoint found for CODEX_HOME "
            f"{args.runtime_home}. Start the sidecar: "
            "python bin/pm2_watchers.py start --agent codex-appserver"
        )

    token = autobridge._codex_app_server_token(endpoint.get("token_file"))
    print(f"[stream] {provenance} thread {thread_id} via {endpoint['url']}")

    deadline = time.time() + args.seconds if args.seconds else None
    pending_text = ""

    failed = False
    with ObserverClient(
        str(endpoint["url"]), token=token, timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS
    ) as client:
        client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "llm-collab-codex-stream", "version": "0.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized")
        client.request("thread/resume", {"threadId": thread_id})
        print("[stream] subscribed; Ctrl-C to stop")

        while deadline is None or time.time() < deadline:
            try:
                message = client.recv_json()
            except (TimeoutError, socket.timeout):
                continue
            except Exception as error:
                # Supervision must be able to tell a dead live view from a finished one.
                print(f"[stream] connection ended: {type(error).__name__}: {error}",
                      file=sys.stderr)
                failed = True
                break

            method = message.get("method")
            if not method:
                continue

            params = message.get("params") or {}
            if params.get("threadId") not in (None, thread_id):
                continue

            if args.raw:
                print(json.dumps(message, default=str))
                continue

            if method == "item/agentMessage/delta":
                chunk = params.get("delta") or params.get("text") or ""
                pending_text += chunk
                sys.stdout.write(chunk)
                sys.stdout.flush()
                continue

            if pending_text:
                sys.stdout.write("\n")
                pending_text = ""

            line = describe(method, params)
            if line:
                print(f"[{time.strftime('%H:%M:%S')}] {line}")

    if pending_text:
        sys.stdout.write("\n")
    if client.observed_requests:
        print(f"[stream] saw {len(client.observed_requests)} server request(s) on this "
              f"observer socket and answered none: "
              f"{', '.join(sorted(set(client.observed_requests)))}. Receiving these here "
              f"means App Server fans requests to non-owner clients.", file=sys.stderr)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
