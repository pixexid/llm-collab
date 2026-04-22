#!/usr/bin/env python3
"""
session_autobridge.py — experimental session-scoped inbox autobridge registry.

The spike keeps the implementation intentionally small:
- session lease records under State/session_autobridge/sessions/
- a bounded dispatcher that matches unread inbox messages to a parked session
- runtime-trigger support only when an explicit runtime command exists
- human-relay fallback that writes a concrete relay prompt artifact
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from _helpers import get_agent, now_utc, utc_iso
from _session_autobridge import (
    SESSION_MODES,
    SESSION_STATUSES,
    WAKE_STRATEGIES,
    dispatch_session,
    load_session,
    save_session,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Experimental session autobridge registry.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="Create or update a parked session lease")
    register.add_argument("--session", required=True, help="Stable session identifier")
    register.add_argument("--agent", required=True, help="Agent ID the parked session belongs to")
    register.add_argument("--project", default=None, help="Optional project_id filter")
    register.add_argument("--chat", default=None, help="Optional chat_id filter")
    register.add_argument("--mode", default="manual", choices=SESSION_MODES)
    register.add_argument("--status", default="parked", choices=SESSION_STATUSES)
    register.add_argument("--wake-strategy", default="none", choices=WAKE_STRATEGIES)
    register.add_argument("--lease-owner", default=None, help="Who activated this session")
    register.add_argument("--ttl-seconds", type=int, default=3600, help="Lease TTL in seconds")
    register.add_argument("--allowed-action", dest="allowed_actions", action="append", default=[])
    register.add_argument(
        "--runtime-command",
        default=None,
        help="Optional JSON array command for runtime_trigger, e.g. [\"python3\",\"worker.py\"]",
    )
    register.add_argument("--runtime-timeout", type=int, default=30, help="Runtime trigger timeout in seconds")
    register.add_argument("--json", dest="json_output", action="store_true")

    show = subparsers.add_parser("show", help="Show a registered session")
    show.add_argument("--session", required=True)
    show.add_argument("--json", dest="json_output", action="store_true")

    dispatch = subparsers.add_parser("dispatch", help="Run one bounded dispatch pass")
    dispatch.add_argument("--session", required=True)
    dispatch.add_argument("--json", dest="json_output", action="store_true")

    deactivate = subparsers.add_parser("deactivate", help="Stop or supersede a session lease")
    deactivate.add_argument("--session", required=True)
    deactivate.add_argument("--status", default="stopped", choices=("stopped", "superseded"))
    deactivate.add_argument("--superseded-by", default=None)
    deactivate.add_argument("--json", dest="json_output", action="store_true")
    return parser.parse_args()


def emit(payload: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(json.dumps(payload, indent=2, sort_keys=True))


def register_session(args) -> dict:
    agent = get_agent(args.agent)
    now = now_utc()
    expires_at = now.timestamp() + args.ttl_seconds
    lease_expires_utc = __import__("datetime").datetime.fromtimestamp(
        expires_at, tz=now.tzinfo
    ).isoformat(timespec="seconds")

    runtime = None
    if args.runtime_command:
        runtime = {
            "command": json.loads(args.runtime_command),
            "timeout_seconds": args.runtime_timeout,
        }
        if not isinstance(runtime["command"], list) or not all(
            isinstance(token, str) for token in runtime["command"]
        ):
            raise ValueError("--runtime-command must be a JSON array of strings")

    existing = {}
    try:
        existing = load_session(args.session)
    except FileNotFoundError:
        existing = {}

    payload = {
        **existing,
        "session_id": args.session,
        "agent_id": args.agent,
        "agent_activation_type": agent.get("activation", {}).get("type"),
        "project_id": args.project,
        "chat_id": args.chat,
        "mode": args.mode,
        "status": args.status,
        "wake_strategy": args.wake_strategy,
        "allowed_actions": sorted(set(args.allowed_actions)),
        "lease_owner": args.lease_owner,
        "lease_expires_utc": lease_expires_utc,
        "runtime": runtime,
        "created_utc": existing.get("created_utc", utc_iso()),
        "processed_messages": existing.get("processed_messages", []),
    }
    save_session(payload)
    return payload


def show_session(args) -> dict:
    return load_session(args.session)


def deactivate_session(args) -> dict:
    payload = load_session(args.session)
    payload["status"] = args.status
    if args.status == "superseded":
        payload["superseded_by"] = args.superseded_by
    save_session(payload)
    return payload


def main():
    args = parse_args()
    if args.command == "register":
        result = register_session(args)
    elif args.command == "show":
        result = show_session(args)
    elif args.command == "dispatch":
        result = dispatch_session(args.session)
    else:
        result = deactivate_session(args)
    emit(result, getattr(args, "json_output", False))


if __name__ == "__main__":
    main()
