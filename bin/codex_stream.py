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
# Cumulative budgets over an untrusted bindings tree. Exceeding either fails closed with a
# message naming the limit, rather than consuming unbounded CPU or memory first.
MAX_SCANNED_CHATS = 2000
MAX_BINDING_BYTES = 64 * 1024
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
    # NO default. Discovery matches CODEX_HOME exactly, so defaulting to one author's home
    # made this work on exactly one machine: any binding under a custom or secondary
    # CODEX_HOME either found no endpoint or connected to the wrong server. The selected
    # binding already records the right home; this flag is an explicit override only.
    p.add_argument("--runtime-home", help="Override the CODEX_HOME to discover (default: the "
                                         "selected binding's own runtime_home)")
    p.add_argument("--seconds", type=float, help="Stop after this long (default: until Ctrl-C)")
    p.add_argument("--raw", action="store_true", help="Print every notification as JSON")
    return p.parse_args()


def one_path_component(value: str, *, field: str) -> str:
    """A selector must name exactly ONE literal path component.

    Rejecting glob metacharacters was not enough: a selector is still joined into a path, so
    `--project 'amiga/../nuvyr'` walked out of the segment it named and reached another
    project's thread. record_matches_path() cannot catch it either, because it compares the
    record against the LEXICAL destination component -- nuvyr -- not against what the caller
    actually asked for, so the record looks perfectly consistent.

    Validated before any filesystem read: no separators, no . or .., non-empty, and unchanged
    by Path().name so no platform-specific spelling slips through.
    """
    text = str(value)
    if (not text or text in {".", ".."} or "/" in text or "\\" in text
            or Path(text).name != text):
        raise SystemExit(
            f"[error] --{field} must be one literal name, not a path: {value!r}"
        )
    return text


def record_matches_path(record: dict, path: Path, agent: str) -> bool:
    """The record must be what its location and the invocation claim it is."""
    return (
        str(record.get("agent_id") or "") == agent
        and str(record.get("chat_id") or "") == path.parent.name
        and str(record.get("project_id") or "") == path.parent.parent.name
    )


def registered_project_ids() -> set[str]:
    """Project ids from projects.json, so an unregistered directory cannot be watched."""
    try:
        payload = json.loads((ROOT / "projects.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    projects = payload.get("projects")
    if not isinstance(projects, list):
        return set()
    return {str(p["id"]) for p in projects if isinstance(p, dict) and p.get("id")}


def resolve_thread(args: argparse.Namespace) -> tuple[str, str, str | None]:
    """Return (thread_id, provenance, runtime_home) for ONE registered project's binding.

    Cross-project selection is not an opt-in ambiguity mode. It used to be: omitting
    --project enumerated every project directory, so `--chat last` could select a worker
    thread belonging to a different project, and a supplied project was never checked
    against projects.json at all -- a fabricated or stale directory was accepted on the
    strength of existing.
    """
    if args.thread:
        return args.thread, "--thread", args.runtime_home

    if not args.agent:
        raise SystemExit("[error] pass --thread, or --agent with --project")

    # Every supplied selector is a LITERAL path segment. Three bugs lived here in turn:
    # --chat ignored unless --project came with it; then the fix interpolated selectors into
    # a glob, where `CHAT-[A]` became a character class; then `--chat ""` fell through to
    # every chat. A selector is one name -- asserted once, up front, for all fields.
    agent = one_path_component(args.agent, field="agent")
    if args.project is None:
        raise SystemExit(
            "[error] --project is required: this watches one project's worker, and "
            "enumerating every project could select a thread you did not name"
        )
    project = one_path_component(args.project, field="project")
    registered = registered_project_ids()
    if registered and project not in registered:
        raise SystemExit(
            f"[error] project {project!r} is not registered in projects.json "
            f"(known: {', '.join(sorted(registered))})"
        )
    if args.chat is not None and args.chat != "last":
        one_path_component(args.chat, field="chat")

    chat_named = args.chat is not None and args.chat != "last"
    project_dir = BINDINGS_DIR / project

    if chat_named:
        candidates = [project_dir / args.chat / f"{agent}.json"]
    else:
        # One cumulative budget over an untrusted directory: a workspace with very many
        # entries would otherwise sort and stat all of them before the ambiguity check.
        candidates = []
        try:
            entries = sorted(d for d in project_dir.iterdir() if d.is_dir())
        except OSError:
            entries = []
        if len(entries) > MAX_SCANNED_CHATS:
            raise SystemExit(
                f"[error] {len(entries)} chat directories under {project!r} exceeds the "
                f"{MAX_SCANNED_CHATS} scan budget; name one with --chat"
            )
        candidates = [d / f"{agent}.json" for d in entries]

    bindings = []
    for path in candidates:
        if not path.is_file():
            continue
        # A candidate that EXISTS but cannot be read or validated is a lookup failure, never
        # a skip. Silently discarding one can leave a partial set that looks unambiguous, so
        # a concurrent non-atomic write to a sibling binding could suppress the refusal and
        # get the remaining thread watched.
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise SystemExit(f"[error] cannot read candidate binding {path}: {error}")
        if len(raw) > MAX_BINDING_BYTES:
            raise SystemExit(
                f"[error] binding {path} is {len(raw)} bytes, over the "
                f"{MAX_BINDING_BYTES} limit; refusing to parse it"
            )
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SystemExit(f"[error] candidate binding {path} is unreadable: {error}")
        if not isinstance(record, dict):
            raise SystemExit(f"[error] candidate binding {path} is not a JSON object")
        if not record_matches_path(record, path, agent):
            raise SystemExit(
                f"[error] binding {path} claims "
                f"{record.get('project_id')}/{record.get('chat_id')}/{record.get('agent_id')}, "
                "which is not where it lives"
            )
        if not record.get("runtime_session_id"):
            continue
        bindings.append((record, path))

    if not bindings:
        raise SystemExit(
            f"[error] no binding with a runtime_session_id for agent {agent!r} in {project!r}"
        )

    active = [b for b in bindings if b[0].get("status") == "active"]
    chosen = active or bindings
    if len(chosen) > 1 and args.chat != "last":
        names = "\n  ".join(f"{b[0].get('project_id')}/{b[0].get('chat_id')}" for b in chosen)
        raise SystemExit(
            f"[error] {len(chosen)} bindings match agent {agent!r} in {project!r}; name one "
            f"with --chat, or pass --chat last:\n  {names}"
        )
    record, path = max(chosen, key=lambda b: str(b[0].get("updated_utc") or ""))
    # The binding's own home wins unless explicitly overridden: discovery matches it exactly.
    runtime_home = args.runtime_home or record.get("runtime_home")
    return (str(record["runtime_session_id"]),
            f"{record.get('project_id')}/{record.get('chat_id')}",
            str(runtime_home) if runtime_home else None)


def elide(value: object, limit: int = 160) -> str:
    """Shorten for display, but never let a shortened value look complete.

    A command or path cut at the limit still read like the whole thing, which can hide the
    distinguishing -- or destructive -- suffix of what a worker is actually running.
    """
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [+{len(text) - limit} chars truncated]"


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
            return f"  $ {elide(item.get('command', ''))}"
        if kind == "fileChange":
            return f"  edit {elide(item.get('path', ''))}"
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
    thread_id, provenance, runtime_home = resolve_thread(args)
    if not runtime_home:
        raise SystemExit(
            "[error] the selected binding records no runtime_home, and none was supplied. "
            "Pass --runtime-home explicitly, or re-register the session with one."
        )

    endpoint = autobridge.discover_codex_app_server(runtime_home)
    if endpoint is None:
        raise SystemExit(
            "[error] no Codex App Server endpoint found for CODEX_HOME "
            f"{runtime_home}. Start the sidecar: "
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
