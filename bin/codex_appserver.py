#!/usr/bin/env python3
"""
codex_appserver.py — operator/worker access to a Codex worker over the App Server.

Delivery, live observation, and mid-turn control for an exact Codex thread,
without AX and without depending on the desktop renderer.

  status                 what the worker is doing right now
  tail                   stream reasoning/output/plan/diff until interrupted
  send    --text ...     start a new turn (returns once accepted, does not block)
  steer   --text ...     inject input into the RUNNING turn
  interrupt              cancel the running turn
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import time

from _session_autobridge import (
    JsonRpcWebSocketClient,
    _codex_app_server_token,
    discover_codex_app_server,
    load_session,
)

# ponytail: only the notifications a human actually wants to watch. Everything
# else stays available via --raw rather than growing a filter config.
INTERESTING = {
    "item/reasoning/textDelta": "think",
    "item/reasoning/summaryTextDelta": "think",
    "item/agentMessage/delta": "say",
    "item/commandExecution/outputDelta": "exec",
    "item/plan/delta": "plan",
    "turn/plan/updated": "plan",
    "turn/diff/updated": "diff",
    "item/started": "item+",
    "item/completed": "item-",
    "thread/status/changed": "status",
    "thread/tokenUsage/updated": "tokens",
    "thread/goal/updated": "goal",
    "turn/started": "turn+",
    "turn/completed": "turn-",
    "turn/failed": "turn!",
}
TERMINAL = {"turn/completed", "turn/failed", "turn/cancelled"}
TRANSPORT_ERROR = "transport-error"
RECV_CAP_SECONDS = 30.0


def resolve_target(args) -> tuple[str, str]:
    """Return (runtime_home, thread_id) from an explicit pair or a session record."""
    if args.session:
        session = load_session(args.session)
        runtime = session.get("runtime") or {}
        home = args.runtime_home or runtime.get("home")
        thread = args.thread or runtime.get("session_id")
    else:
        home, thread = args.runtime_home, args.thread
    if not home or not thread:
        raise SystemExit(
            "need --session (with a registered runtime) or both --runtime-home and --thread"
        )
    return str(home), str(thread)


def connect(runtime_home: str, timeout: int) -> JsonRpcWebSocketClient:
    endpoint = discover_codex_app_server(runtime_home)
    if endpoint is None:
        raise SystemExit(
            f"no codex app-server listening on ws:// for CODEX_HOME={runtime_home}. "
            "Start it with: pm2 start pm2/ecosystem.config.cjs --only <workspace>-codex-appserver"
        )
    token = _codex_app_server_token(endpoint.get("token_file"))
    client = JsonRpcWebSocketClient(endpoint["url"], token=token, timeout_seconds=timeout)
    return client


def handshake(client: JsonRpcWebSocketClient, thread_id: str | None = None) -> None:
    client.request(
        "initialize",
        {"clientInfo": {"name": "llm-collab-cli", "title": "llm-collab", "version": "0.1"}},
    )
    client.notify("initialized")
    if thread_id:
        client.request("thread/resume", {"threadId": thread_id})


def text_of(params: object) -> str:
    """Pull the human-meaningful line out of a notification payload."""
    if not isinstance(params, dict):
        return ""
    for key in ("delta", "text", "summary"):
        value = params.get(key)
        if isinstance(value, str):
            return value

    goal = params.get("goal")
    if isinstance(goal, dict):
        # the signal that told us Codex was stuck on an already-merged PR
        return f"[{goal.get('status')}] {goal.get('objective') or ''}"

    usage = params.get("tokenUsage")
    if isinstance(usage, dict):
        last = usage.get("last") or {}
        return (
            f"turn={last.get('totalTokens')} "
            f"total={(usage.get('total') or {}).get('totalTokens')} "
            f"window={usage.get('modelContextWindow')}"
        )

    limits = params.get("rateLimits")
    if isinstance(limits, dict):
        primary = limits.get("primary") or {}
        return f"{primary.get('usedPercent')}% used, plan={limits.get('planType')}"

    item = params.get("item")
    if isinstance(item, dict):
        for key in ("text", "command", "title", "type"):
            value = item.get(key)
            if isinstance(value, str):
                return f"{item.get('type')}: {value}" if key != "type" else value
    for key in ("status", "plan", "diff"):
        value = params.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value)[:300]
    return ""


def refuse_server_request(client, message: dict) -> None:
    """Answer a server->client request with a JSON-RPC error. Never approve anything.

    Replying `{"result": {}}` was schema-invalid for every approval/input request
    (ExecCommandApprovalResponse requires `decision`, PermissionsRequestApprovalResponse
    requires `permissions`, ToolRequestUserInputResponse requires `answers`) and could
    block or fail a turn. Mapping each method's denial const would risk sending an
    approval by mistake, and this CLI is an observer/controller: it must never approve
    an action on the operator's behalf. An error response is always protocol-valid and
    unambiguously fails closed.
    """
    client.send_json(
        {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {
                "code": -32601,
                "message": (
                    "llm-collab codex_appserver is an observer client and does not "
                    f"answer server requests ({message.get('method')})"
                ),
            },
        }
    )


def bound_recv(client, deadline: float):
    """Read one message with the socket bounded by the REMAINING deadline.

    Checking the clock only between reads made deadlines advisory: a blocking
    recv_json could overshoot by the whole socket timeout, so `tail --seconds 1`
    could sit for 120s.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline reached")
    sock = getattr(client, "sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        # remaining is inf for an unbounded tail, and socket.settimeout(inf) raises
        # OverflowError -- which made default tail exit 1 immediately. Clamp to the
        # client's own finite socket budget.
        cap = getattr(client, "timeout_seconds", None) or RECV_CAP_SECONDS
        sock.settimeout(max(0.05, min(remaining, float(cap))))
    return client.recv_json()


def pump(client: JsonRpcWebSocketClient, *, deadline: float, raw: bool, stop_on_terminal: bool) -> str | None:
    """Print notifications until deadline or a terminal turn event. Returns last terminal."""
    while time.monotonic() < deadline:
        try:
            message = bound_recv(client, deadline)
        except TimeoutError:
            # An idle socket timeout is NOT completion. With an infinite deadline the
            # clamp is the client's finite socket budget, so the first quiet stretch
            # raised TimeoutError and tail returned as though the turn had ended.
            # Only a genuinely reached deadline ends the loop.
            if time.monotonic() < deadline:
                continue
            return None
        except Exception as exc:
            # Silence must never be indistinguishable from success: a dropped socket
            # or protocol fault is reported, not swallowed as a quiet clean exit.
            print(f"[transport] {type(exc).__name__}: {exc}", flush=True)
            return TRANSPORT_ERROR
        if message.get("id") and message.get("method"):
            refuse_server_request(client, message)
            continue
        method = str(message.get("method") or "")
        if not method:
            continue
        if raw:
            print(json.dumps(message), flush=True)
        elif method in INTERESTING:
            body = text_of(message.get("params")).replace("\n", " ")
            label = INTERESTING[method]
            print(f"[{label}] {body[:400]}" if body else f"[{label}]", flush=True)
        if stop_on_terminal and method in TERMINAL:
            return method
    return None


def observe_running_turn(client: JsonRpcWebSocketClient, *, seconds: int) -> str | None:
    """Read notifications briefly and return the turnId of the turn actually running."""
    deadline = time.monotonic() + max(seconds, 1)
    while time.monotonic() < deadline:
        try:
            message = bound_recv(client, deadline)
        except Exception:
            return None
        if message.get("id") and message.get("method"):
            refuse_server_request(client, message)
            continue
        method = str(message.get("method") or "")
        params = message.get("params")
        if method in TERMINAL:
            return None
        turn_id = active_turn_id(params)
        if turn_id:
            return turn_id
    return None


def active_turn_id(params: object) -> str | None:
    """Extract a turn id from a notification, honouring both shapes in the contract.

    TurnStartedNotification is {threadId, turn} with the id at params.turn.id, NOT a
    top-level params.turnId. Reading only turnId meant a turn was never discovered
    when turn/started was the sole evidence -- and the hand-written test fixture used
    the invalid top-level shape, so the tests validated an invented contract rather
    than the real one.
    """
    if not isinstance(params, dict):
        return None
    turn = params.get("turn")
    if isinstance(turn, dict) and turn.get("id"):
        return str(turn["id"])
    if params.get("turnId"):
        return str(params["turnId"])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "tail", "send", "steer", "interrupt"))
    parser.add_argument("--session", default=None, help="Registered llm-collab session to resolve the target from")
    parser.add_argument("--runtime-home", default=None, help="Exact CODEX_HOME of the worker")
    parser.add_argument("--thread", default=None, help="Exact native thread id")
    parser.add_argument("--text", default=None, help="Message body for send/steer")
    parser.add_argument("--seconds", type=int, default=0, help="tail: stop after N seconds (0 = until turn ends)")
    parser.add_argument("--turn", default=None, help="steer: exact turn id (default: observe the running turn)")
    parser.add_argument("--observe", type=int, default=10, help="steer: seconds to observe for the running turn")
    parser.add_argument("--raw", action="store_true", help="Print every notification verbatim")
    parser.add_argument("--timeout", type=int, default=30, help="Socket timeout in seconds")
    args = parser.parse_args()

    runtime_home, thread_id = resolve_target(args)

    if args.command == "status":
        with connect(runtime_home, args.timeout) as client:
            handshake(client)
            listing = client.request("thread/list", {})
            row = next((t for t in listing.get("data", []) if t.get("id") == thread_id), None)
            if row is None:
                raise SystemExit(f"thread {thread_id} not found under {runtime_home}")
            print(json.dumps({
                "thread": thread_id,
                "runtime_home": runtime_home,
                "updated_at": row.get("updatedAt"),
                "preview": (row.get("preview") or "")[:200],
            }, indent=2))
        return

    if args.command == "interrupt":
        with connect(runtime_home, args.timeout) as client:
            handshake(client, thread_id)
            # TurnInterruptParams requires threadId AND turnId. Sending threadId alone
            # is rejected and cancels nothing, so identify the running turn first and
            # refuse when there is none rather than reporting a hollow success.
            turn_id = args.turn or observe_running_turn(client, seconds=args.observe)
            if not turn_id:
                raise SystemExit(
                    "no running turn observed to interrupt — nothing to cancel, "
                    "or pass --turn if you already know the id"
                )
            result = client.request(
                "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
            )
            print(json.dumps({"interrupted": True, "turn_id": turn_id, "result": result}))
        return

    if args.command in ("send", "steer"):
        if not args.text:
            raise SystemExit(f"{args.command} requires --text")
        method = "turn/start" if args.command == "send" else "turn/steer"
        with connect(runtime_home, args.timeout) as client:
            handshake(client, thread_id)
            payload = {"threadId": thread_id, "input": [{"type": "text", "text": args.text}]}
            if method == "turn/steer":
                # steer is fail-closed: it requires the exact turn being steered, so a
                # race cannot land our input in a different turn. Observe it rather
                # than guess it.
                turn_id = args.turn or observe_running_turn(client, seconds=args.observe)
                if not turn_id:
                    raise SystemExit(
                        "no running turn observed to steer — use `send` to start one, "
                        "or pass --turn if you already know the id"
                    )
                payload["expectedTurnId"] = turn_id
                print(f"[steer] targeting turn {turn_id}", flush=True)
            result = client.request(method, payload)
            # The two responses differ: TurnStartResponse nests {turn:{id,status}},
            # TurnSteerResponse is top-level {turnId}. Reading result.turn for both
            # yielded an "accepted" receipt with a null id, which is worse than an
            # error because it looks like success.
            if method == "turn/start":
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = (turn or {}).get("id")
                status = (turn or {}).get("status")
            else:
                turn_id = result.get("turnId") if isinstance(result, dict) else None
                status = "steered"
            if not turn_id:
                raise SystemExit(f"{method} returned no turn id: {json.dumps(result)[:200]}")
            # ponytail: confirm ACCEPTANCE, never wait for turn/completed — a turn can
            # run for minutes and blocking here would stall the caller (and the watcher).
            print(json.dumps({
                "accepted": True,
                "method": method,
                "turn_id": turn_id,
                "status": status,
            }, indent=2))
            # Return on acceptance, as documented. Observation is `tail`'s job; pumping
            # here contradicted the promise and delayed the caller for no benefit.
        return

    # tail
    with connect(runtime_home, max(args.timeout, 120)) as client:
        handshake(client, thread_id)
        print(f"[tail] {thread_id} @ {runtime_home} — ctrl-c to stop", flush=True)
        deadline = time.monotonic() + args.seconds if args.seconds else float("inf")
        try:
            terminal = pump(client, deadline=deadline, raw=args.raw, stop_on_terminal=bool(args.seconds == 0))
            if terminal == TRANSPORT_ERROR:
                # a transport failure must be detectable by a caller, not only visible
                raise SystemExit(1)
            if terminal:
                print(f"[end] {terminal}", flush=True)
        except KeyboardInterrupt:
            print("[tail] stopped", flush=True)


if __name__ == "__main__":
    main()
