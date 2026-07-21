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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from _helpers import get_agent, now_utc, utc_iso
from _session_autobridge import (
    SESSION_MODES,
    SESSION_STATUSES,
    WAKE_STRATEGIES,
    discover_runtime_session,
    dispatch_session,
    load_binding,
    load_session,
    runtime_home_from_source,
    save_session,
    update_binding_from_session,
)
from _activation_lease import (
    LeaseRefused,
    assert_lease,
    claim_lease,
    lease_identity,
    load_lease,
    owner_summary,
    release_lease,
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
    register.add_argument("--runtime-family", default=None, help="Runtime family, e.g. codex_app, claude_app, gemini_cli")
    register.add_argument("--runtime-session-id", default=None, help="Current runtime-native session identifier")
    register.add_argument("--runtime-session-source", default=None, help="Where the runtime session identifier came from")
    register.add_argument("--supersedes-session", default=None, help="Older llm-collab session replaced by this registration")
    register.add_argument(
        "--runtime-command",
        default=None,
        help="Optional JSON array command for runtime_trigger, e.g. [\"python3\",\"worker.py\"]",
    )
    register.add_argument("--runtime-timeout", type=int, default=30, help="Runtime trigger timeout in seconds")
    register.add_argument("--json", dest="json_output", action="store_true")

    discover = subparsers.add_parser("discover-runtime", help="Discover current runtime session metadata")
    discover.add_argument("--runtime-family", required=True, choices=("codex_app", "claude_app", "gemini_cli"))
    discover.add_argument("--project-path", default=None, help="Optional project path hint for runtime discovery")
    discover.add_argument("--json", dest="json_output", action="store_true")

    publish = subparsers.add_parser("publish-current", help="Discover and publish current runtime session into a session lease")
    publish.add_argument("--session", required=True, help="Stable llm-collab session identifier")
    publish.add_argument("--agent", required=True, help="Agent ID the parked session belongs to")
    publish.add_argument("--runtime-family", required=True, choices=("codex_app", "claude_app", "gemini_cli"))
    publish.add_argument("--project", default=None, help="Optional project_id filter")
    publish.add_argument("--chat", default=None, help="Optional chat_id filter")
    publish.add_argument("--project-path", default=None, help="Optional runtime project path hint")
    publish.add_argument("--mode", default="notify", choices=SESSION_MODES)
    publish.add_argument("--status", default="parked", choices=SESSION_STATUSES)
    publish.add_argument("--wake-strategy", default="none", choices=WAKE_STRATEGIES)
    publish.add_argument("--lease-owner", default=None, help="Who activated this session")
    publish.add_argument("--ttl-seconds", type=int, default=3600, help="Lease TTL in seconds")
    publish.add_argument("--supersedes-session", default=None, help="Older llm-collab session replaced by this registration")
    publish.add_argument("--json", dest="json_output", action="store_true")

    show = subparsers.add_parser("show", help="Show a registered session")
    show.add_argument("--session", required=True)
    show.add_argument("--json", dest="json_output", action="store_true")

    show_binding = subparsers.add_parser("show-binding", help="Show a canonical chat/agent runtime binding")
    show_binding.add_argument("--project", required=True)
    show_binding.add_argument("--chat", required=True)
    show_binding.add_argument("--agent", required=True)
    show_binding.add_argument("--json", dest="json_output", action="store_true")

    dispatch = subparsers.add_parser("dispatch", help="Run one bounded dispatch pass")
    dispatch.add_argument("--session", required=True)
    dispatch.add_argument("--json", dest="json_output", action="store_true")

    deactivate = subparsers.add_parser("deactivate", help="Stop or supersede a session lease")
    deactivate.add_argument("--session", required=True)
    deactivate.add_argument("--status", default="stopped", choices=("stopped", "superseded"))
    deactivate.add_argument("--superseded-by", default=None)
    deactivate.add_argument("--json", dest="json_output", action="store_true")

    def add_activation_identity(subparser):
        subparser.add_argument("--project", required=True)
        subparser.add_argument("--chat", required=True)
        subparser.add_argument("--task", required=True)
        subparser.add_argument("--worktree", required=True)
        subparser.add_argument("--branch", required=True)
        subparser.add_argument("--target-agent", dest="target_agent", required=True)
        subparser.add_argument("--json", dest="json_output", action="store_true")

    lease_claim = subparsers.add_parser(
        "lease-claim",
        help="Claim the one-writer activation lease for an exact activation identity",
    )
    add_activation_identity(lease_claim)
    lease_claim.add_argument("--session", required=True, help="Claiming session identifier")
    lease_claim.add_argument("--owner-pid", type=int, default=None, help="Live claiming process id")
    lease_claim.add_argument("--claimant-runtime-id", default=None, help="Current runtime/session id")
    lease_claim.add_argument("--ttl-seconds", type=int, default=3600)
    lease_claim.add_argument("--takeover", action="store_true", help="Explicitly replace an expired or provably dead owner")

    lease_show = subparsers.add_parser("lease-show", help="Show the activation lease for an identity")
    add_activation_identity(lease_show)

    lease_assert = subparsers.add_parser(
        "lease-assert",
        help="Assert current activation authority before mutating",
    )
    add_activation_identity(lease_assert)
    lease_assert.add_argument("--session", required=True, help="Owning session identifier")
    lease_assert.add_argument("--fence-token", type=int, required=True)
    lease_assert.add_argument("--owner-pid", type=int, default=None, help="Asserting process id")
    lease_assert.add_argument("--claimant-runtime-id", default=None, help="Current runtime/session id")

    lease_release = subparsers.add_parser("lease-release", help="Release an activation lease you own")
    add_activation_identity(lease_release)
    lease_release.add_argument("--session", required=True, help="Owning session identifier")
    lease_release.add_argument("--fence-token", type=int, required=True)
    lease_release.add_argument("--owner-pid", type=int, default=None, help="Releasing process id")
    lease_release.add_argument("--claimant-runtime-id", default=None, help="Current runtime/session id")
    lease_release.add_argument("--status", default="released", choices=("released", "superseded"))
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

    existing = {}
    try:
        existing = load_session(args.session)
    except FileNotFoundError:
        existing = {}

    runtime = existing.get("runtime") if isinstance(existing.get("runtime"), dict) else {}
    runtime = dict(runtime)
    if args.runtime_family is not None:
        runtime["family"] = args.runtime_family
    if args.runtime_session_id is not None:
        runtime["session_id"] = args.runtime_session_id
    if args.runtime_session_source is not None:
        runtime["session_source"] = args.runtime_session_source
    runtime_home = getattr(args, "runtime_home", None)
    if runtime_home is None and runtime.get("family") and runtime.get("session_source"):
        runtime_home = runtime_home_from_source(str(runtime["family"]), runtime.get("session_source"))
    if runtime_home is not None:
        runtime["home"] = runtime_home
    if args.runtime_command:
        runtime["command"] = json.loads(args.runtime_command)
        runtime["timeout_seconds"] = args.runtime_timeout
        if not isinstance(runtime["command"], list) or not all(
            isinstance(token, str) for token in runtime["command"]
        ):
            raise ValueError("--runtime-command must be a JSON array of strings")
    elif "command" in runtime and "timeout_seconds" not in runtime:
        runtime["timeout_seconds"] = args.runtime_timeout

    if not runtime:
        runtime = None

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
        "supersedes_session_id": args.supersedes_session,
        "created_utc": existing.get("created_utc", utc_iso()),
        "processed_messages": existing.get("processed_messages", []),
    }
    save_session(payload)
    binding = update_binding_from_session(payload)
    if binding is not None:
        payload["binding"] = binding
    return payload


def show_session(args) -> dict:
    return load_session(args.session)


def show_binding(args) -> dict:
    return load_binding(args.project, args.chat, args.agent)


def discover_runtime(args) -> dict:
    return discover_runtime_session(args.runtime_family, project_path=args.project_path)


def publish_current_session(args) -> dict:
    class RegisterArgs:
        pass

    discovered = discover_runtime_session(args.runtime_family, project_path=args.project_path)
    register_args = RegisterArgs()
    register_args.session = args.session
    register_args.agent = args.agent
    register_args.project = args.project
    register_args.chat = args.chat
    register_args.mode = args.mode
    register_args.status = args.status
    register_args.wake_strategy = args.wake_strategy
    register_args.lease_owner = args.lease_owner
    register_args.ttl_seconds = args.ttl_seconds
    register_args.allowed_actions = []
    register_args.runtime_family = discovered["family"]
    register_args.runtime_session_id = discovered["session_id"]
    register_args.runtime_session_source = discovered["session_source"]
    register_args.runtime_home = discovered.get("home")
    register_args.supersedes_session = args.supersedes_session
    register_args.runtime_command = None
    register_args.runtime_timeout = 30
    payload = register_session(register_args)
    return {"discovered": discovered, "session": payload}


def deactivate_session(args) -> dict:
    payload = load_session(args.session)
    payload["status"] = args.status
    if args.status == "superseded":
        payload["superseded_by"] = args.superseded_by
    save_session(payload)
    return payload


def _refusal_payload(kind: str, identity: dict, refusal: LeaseRefused) -> dict:
    payload = {
        kind: False,
        "reason": refusal.reason,
        "identity": identity,
        "owner": refusal.owner,
    }
    if kind == "claimed":
        payload["hint"] = "activation authority was not granted; hold read-only and do not mutate the worktree"
    return payload


def lease_claim_command(args) -> tuple[dict, int]:
    identity = lease_identity(args)
    try:
        lease = claim_lease(
            identity,
            owner_session_id=args.session,
            owner_pid=args.owner_pid,
            claimant_runtime_id=args.claimant_runtime_id,
            ttl_seconds=args.ttl_seconds,
            takeover=args.takeover,
        )
    except LeaseRefused as refusal:
        return (_refusal_payload("claimed", identity, refusal), 75)
    return ({"claimed": True, "lease": lease}, 0)


def lease_show_command(args) -> tuple[dict, int]:
    identity = lease_identity(args)
    lease = load_lease(identity)
    if lease is None:
        return ({"identity": identity, "lease": None}, 0)
    return ({"identity": identity, "lease": lease, "owner": owner_summary(lease)}, 0)


def lease_assert_command(args) -> tuple[dict, int]:
    identity = lease_identity(args)
    try:
        lease = assert_lease(
            identity,
            owner_session_id=args.session,
            fence_token=args.fence_token,
            owner_pid=args.owner_pid,
            claimant_runtime_id=args.claimant_runtime_id,
        )
    except LeaseRefused as refusal:
        return (_refusal_payload("asserted", identity, refusal), 75)
    return ({"asserted": True, "lease": lease}, 0)


def lease_release_command(args) -> tuple[dict, int]:
    identity = lease_identity(args)
    try:
        lease = release_lease(
            identity,
            owner_session_id=args.session,
            fence_token=args.fence_token,
            owner_pid=args.owner_pid,
            claimant_runtime_id=args.claimant_runtime_id,
            status=args.status,
        )
    except LeaseRefused as refusal:
        return (_refusal_payload("released", identity, refusal), 75)
    return ({"released": True, "lease": lease}, 0)


def main():
    args = parse_args()
    exit_code = 0
    if args.command == "register":
        result = register_session(args)
    elif args.command == "discover-runtime":
        result = discover_runtime(args)
    elif args.command == "publish-current":
        result = publish_current_session(args)
    elif args.command == "show":
        result = show_session(args)
    elif args.command == "show-binding":
        result = show_binding(args)
    elif args.command == "dispatch":
        result = dispatch_session(args.session)
    elif args.command == "lease-claim":
        result, exit_code = lease_claim_command(args)
    elif args.command == "lease-show":
        result, exit_code = lease_show_command(args)
    elif args.command == "lease-assert":
        result, exit_code = lease_assert_command(args)
    elif args.command == "lease-release":
        result, exit_code = lease_release_command(args)
    else:
        result = deactivate_session(args)
    emit(result, getattr(args, "json_output", False))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
