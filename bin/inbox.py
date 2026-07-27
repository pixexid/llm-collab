#!/usr/bin/env python3
"""
inbox.py — List unread messages for an agent.

Reads the agent's pointer index (agents/{id}/inbox.json) and loads
message content from Chats/. Marks messages as read unless --peek.

Usage:
  python bin/inbox.py --me orchestrator
  python bin/inbox.py --me orchestrator --limit 10
  python bin/inbox.py --me orchestrator --project my-app
  python bin/inbox.py --me orchestrator --all
  python bin/inbox.py --me orchestrator --peek
  python bin/inbox.py --me orchestrator --project my-app --mark-all-read
  python bin/inbox.py --me orchestrator --all-projects --mark-all-read
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import os
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from _activation_cleanup import claim_activation_lease
from _activation_identity import classify_activation, lease_key
from _activation_lease import LeaseRefused, load_lease, owner_summary, pid_from_env, runtime_id_from_env
from _helpers import (
    ROOT,
    agent_ids,
    get_unread_messages,
    load_agent_inbox,
    mark_messages_read,
    parse_frontmatter,
    now_utc,
    utc_iso,
)
from _session_autobridge import (
    HEURISTIC_RUNTIME_DISCOVERY_FAMILIES,
    HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
    discover_runtime_session,
    load_session,
    matching_unread_messages,
    repo_scope_matches,
    runtime_metadata,
    save_session,
)
from session_autobridge import register_session

MAX_EXACT_SESSION_UNREAD_ENTRIES = 1_000
MAX_EXACT_SESSION_UNREAD_BYTES = 16 * 1024 * 1024


def parse_args():
    p = argparse.ArgumentParser(description="List unread messages for an agent.")
    p.add_argument("--me", required=True, help="Your agent ID")
    p.add_argument("--limit", type=int, default=None, help="Max messages to show (default: 10)")
    p.add_argument("--all", dest="show_all", action="store_true", help="Show all messages including read")
    p.add_argument("--peek", action="store_true", help="Do not mark shown messages as read")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--project", default=None, help="Filter by exact project_id")
    scope.add_argument(
        "--all-projects",
        action="store_true",
        help="Explicitly target every project (only valid with --mark-all-read)",
    )
    p.add_argument("--chat", default=None, help="Filter by chat substring")
    p.add_argument(
        "--repo-target",
        action="append",
        default=None,
        help="Explicit repository subscription; repeat for multiple repositories",
    )
    p.add_argument(
        "--packet",
        default=None,
        help="Select exactly one packet by basename or relative path across read+unread messages",
    )
    p.add_argument(
        "--mark-all-read",
        action="store_true",
        help="Mark unread messages in the selected project scope as read and exit",
    )
    p.add_argument("--publish-session", action="store_true", help="Publish current runtime session identity before showing inbox")
    p.add_argument("--session", default=None, help="Exact llm-collab session to read or update when publishing runtime identity")
    p.add_argument("--runtime-family", default=None, choices=("codex_app", "claude_app", "gemini_cli"), help="Runtime family for session discovery")
    p.add_argument("--project-path", default=None, help="Optional runtime project path hint for session discovery")
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON output")
    args = p.parse_args()

    if args.project is not None and not args.project.strip():
        p.error("--project requires a non-empty project id")
    if args.repo_target is not None and args.project is None:
        p.error("--repo-target requires --project <id>")

    if args.all_projects and not args.mark_all_read:
        p.error("--all-projects is only valid with --mark-all-read")
    if args.session is not None and not args.publish_session and args.show_all:
        p.error("--session reads exact unread work and does not support --all")

    if args.mark_all_read:
        if args.project is None and not args.all_projects:
            p.error("--mark-all-read requires --project <id> or explicit --all-projects")

        incompatible = []
        if args.chat is not None:
            incompatible.append("--chat")
        if args.packet is not None:
            incompatible.append("--packet")
        if args.show_all:
            incompatible.append("--all")
        if args.peek:
            incompatible.append("--peek")
        if args.limit is not None:
            incompatible.append("--limit")
        if args.publish_session:
            incompatible.append("--publish-session")
        if args.session is not None:
            incompatible.append("--session")
        if args.runtime_family is not None:
            incompatible.append("--runtime-family")
        if args.project_path is not None:
            incompatible.append("--project-path")
        if incompatible:
            p.error(
                "--mark-all-read does not support "
                + ", ".join(incompatible)
                + "; narrow by --project or opt in with --all-projects"
            )

    if args.limit is None:
        args.limit = 10
    return args


def publish_runtime_identity(args) -> dict | None:
    if not args.publish_session:
        return None
    if not args.session:
        raise ValueError("--publish-session requires --session")
    if not args.runtime_family:
        raise ValueError("--publish-session requires --runtime-family")

    if args.runtime_family in HEURISTIC_RUNTIME_DISCOVERY_FAMILIES:
        return {
            "published": False,
            "reason": HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
            "runtime_family": args.runtime_family,
            "hint": (
                "Use session_autobridge.py discover-runtime for read-only diagnostics, "
                "or session_autobridge.py register --runtime-session-id for exact binding."
            ),
        }

    discovered = discover_runtime_session(args.runtime_family, project_path=args.project_path)
    matching_messages = filter_messages(get_unread_messages(args.me), args.project, args.chat)
    chat_ids = {
        str(message["frontmatter"].get("chat_id"))
        for message in matching_messages
        if message["frontmatter"].get("chat_id")
    }
    resolved_chat_id = next(iter(chat_ids)) if len(chat_ids) == 1 else None

    class RegisterArgs:
        pass

    register_args = RegisterArgs()
    register_args.session = args.session
    register_args.agent = args.me
    register_args.project = args.project
    register_args.chat = resolved_chat_id
    register_args.repo_targets = args.repo_target
    register_args.mode = "notify"
    register_args.status = "parked"
    register_args.wake_strategy = "none"
    register_args.lease_owner = args.me
    register_args.ttl_seconds = 3600
    register_args.allowed_actions = []
    register_args.runtime_family = discovered["family"]
    register_args.runtime_session_id = discovered["session_id"]
    register_args.runtime_session_source = discovered["session_source"]
    register_args.supersedes_session = None
    register_args.runtime_command = None
    register_args.runtime_timeout = 30
    payload = register_session(register_args)
    return {"discovered": discovered, "session": payload}


def load_all_messages(agent_id: str) -> list[dict]:
    """Load all messages (read + unread) for an agent."""
    inbox = load_agent_inbox(agent_id)
    all_paths = inbox.get("read", []) + inbox.get("unread", [])
    messages = []
    for rel_path in all_paths:
        abs_path = ROOT / rel_path
        if abs_path.exists():
            fm, body = parse_frontmatter(abs_path.read_text())
            messages.append({
                "path": rel_path,
                "read": rel_path in inbox.get("read", []),
                "frontmatter": fm,
                "body": body,
            })
    return messages


def filter_messages(
    messages: list[dict],
    project: str | None,
    chat: str | None,
    packet: str | None = None,
) -> list[dict]:
    if project:
        messages = [m for m in messages if m["frontmatter"].get("project_id") == project]
    if chat:
        messages = [m for m in messages if chat.lower() in m["path"].lower()]
    if packet:
        packet = packet.strip()
        if "/" in packet:
            messages = [m for m in messages if m["path"] == packet]
        else:
            messages = [m for m in messages if Path(m["path"]).name == packet]
    return messages


def filter_repo_scope(
    messages: list[dict], subscriber_targets: list[str] | None, project_id: str | None
) -> tuple[list[dict], list[dict]]:
    if subscriber_targets is None:
        return messages, []
    matched: list[dict] = []
    refused: list[dict] = []
    for message in messages:
        allowed, reason = repo_scope_matches(
            subscriber_targets,
            message.get("frontmatter", {}).get("repo_targets"),
            subscriber_project=project_id,
            packet_project=message.get("frontmatter", {}).get("project_id"),
        )
        if allowed:
            matched.append(message)
        else:
            refused.append({"path": message["path"], "reason": reason})
    return matched, refused


def recheck_repo_scope_before_read(
    agent_id: str,
    paths: list[str],
    subscriber_targets: list[str] | None,
    project_id: str | None,
) -> tuple[list[str], list[dict]]:
    if subscriber_targets is None:
        return paths, []
    current = {
        message["path"]: message
        for message in unread_messages_with_missing_files(agent_id)
    }
    allowed: list[str] = []
    refused: list[dict] = []
    for path in paths:
        message = current.get(path, {"path": path, "frontmatter": {}})
        matched, reason = repo_scope_matches(
            subscriber_targets,
            message.get("frontmatter", {}).get("repo_targets"),
            subscriber_project=project_id,
            packet_project=message.get("frontmatter", {}).get("project_id"),
        )
        if matched:
            allowed.append(path)
        else:
            refused.append({"path": path, "reason": reason})
    return allowed, refused


def packet_selection_error(args, messages: list[dict]) -> None:
    payload = {
        "error": "packet_selection_not_unique",
        "packet": args.packet,
        "matches": [message["path"] for message in messages],
    }
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"[activation] refused: --packet {args.packet!r} matched "
            f"{len(messages)} messages; expected exactly one.",
            file=sys.stderr,
        )
        for path in payload["matches"]:
            print(f"  - {path}", file=sys.stderr)
    sys.exit(75)


def activation_reader_runtime_id() -> str | None:
    return runtime_id_from_env()


def activation_reader_pid() -> int | None:
    return pid_from_env() or os.getpid()


def activation_reader_session_id(args, identity: dict[str, str]) -> str:
    if args.session:
        return args.session
    runtime_id = activation_reader_runtime_id()
    suffix = runtime_id or f"pid-{activation_reader_pid()}"
    return f"SESSION-activation-{lease_key(identity)}-{str(suffix)[:24]}"


def ensure_reader_session(
    session_id: str,
    agent_id: str,
    identity: dict[str, str],
    *,
    runtime_id: str | None,
) -> dict:
    try:
        return load_session(session_id)
    except FileNotFoundError:
        expires = now_utc().timestamp() + 6 * 60 * 60
        payload = {
            "session_id": session_id,
            "agent_id": agent_id,
            "project_id": identity["project"],
            "chat_id": identity["chat"],
            "mode": "manual",
            "status": "parked",
            "wake_strategy": "none",
            "lease_owner": agent_id,
            "lease_expires_utc": datetime.fromtimestamp(
                expires, tz=now_utc().tzinfo
            ).isoformat(timespec="seconds"),
            "allowed_actions": [],
            "runtime": (
                {
                    "family": "reader",
                    "session_id": runtime_id,
                    "session_source": "activation_reader_env",
                }
                if runtime_id
                else {}
            ),
            "activation_identity": identity,
            "ephemeral_reader": True,
            "created_utc": utc_iso(),
        }
        save_session(payload)
        return payload


def gate_activation_message(args, msg: dict, *, consume: bool) -> dict | None:
    kind, detail = classify_activation(msg["frontmatter"], target_agent=args.me)
    if kind == "none":
        return None
    if kind == "malformed":
        return {
            "authorized": False,
            "reason": "malformed_activation",
            "detail": detail,
        }
    identity = detail or {}
    existing = None
    try:
        existing_lease = load_lease(identity)
        existing = owner_summary(existing_lease) if existing_lease else None
    except Exception as exc:
        existing = {"error": exc.__class__.__name__}
    if not consume:
        return {
            "authorized": False,
            "reason": "peek_only",
            "identity": identity,
            "owner": existing,
        }

    runtime_id = activation_reader_runtime_id()
    if runtime_id is None and args.session:
        runtime_id = runtime_metadata(load_session(args.session)).get("session_id")
    owner_pid = None if runtime_id else activation_reader_pid()
    session_id = activation_reader_session_id(args, identity)
    ensure_reader_session(session_id, args.me, identity, runtime_id=runtime_id)
    try:
        claim = claim_activation_lease(
            identity,
            owner_session_id=session_id,
            owner_pid=owner_pid,
            claimant_runtime_id=runtime_id,
            takeover=True,
        )
    except LeaseRefused as exc:
        return {
            "authorized": False,
            "reason": exc.reason,
            "identity": identity,
            "owner": exc.owner or existing,
        }
    return {
        "authorized": True,
        "reason": "claimed",
        "identity": identity,
        "lease": claim["lease"],
        "fence_token": claim["fence_token"],
        "poller_audit": claim["poller_audit"],
    }


UNSCOPED_PROJECT_BUCKET = "<unscoped-or-missing-project>"
MISSING_MESSAGE_BUCKET = "<missing-message>"


def project_bucket(frontmatter: dict) -> str:
    project_id = frontmatter.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id
    return UNSCOPED_PROJECT_BUCKET


def unread_messages_with_missing_files(agent_id: str) -> list[dict]:
    """Load unread pointers for an explicit all-project mutation."""
    inbox = load_agent_inbox(agent_id)
    messages = []
    for rel_path in inbox.get("unread", []):
        abs_path = ROOT / rel_path
        if abs_path.exists():
            frontmatter, body = parse_frontmatter(abs_path.read_text())
            messages.append(
                {
                    "path": rel_path,
                    "frontmatter": frontmatter,
                    "body": body,
                }
            )
        else:
            messages.append(
                {
                    "path": rel_path,
                    "frontmatter": {},
                    "body": "",
                    "missing_message": True,
                }
            )
    return messages


def mark_all_read(args) -> dict:
    if args.all_projects:
        messages = unread_messages_with_missing_files(args.me)
    else:
        messages = filter_messages(get_unread_messages(args.me), args.project, None)
    messages, repo_scope_refused = filter_repo_scope(
        messages, args.repo_target, args.project
    )

    marked_by_project: dict[str, int] = {}
    held_activation = 0
    held_paths: list[str] = []
    markable: list[dict] = []
    for message in messages:
        if not message.get("missing_message"):
            kind, _ = classify_activation(message["frontmatter"], target_agent=args.me)
            if kind != "none":
                held_activation += 1
                held_paths.append(message["path"])
                continue
        markable.append(message)

    paths = [message["path"] for message in markable]
    paths, late_refused = recheck_repo_scope_before_read(
        args.me, paths, args.repo_target, args.project
    )
    repo_scope_refused.extend(late_refused)
    markable_by_path = {message["path"]: message for message in markable}
    for path in paths:
        message = markable_by_path[path]
        if message.get("missing_message"):
            bucket = MISSING_MESSAGE_BUCKET
        else:
            bucket = project_bucket(message["frontmatter"])
        marked_by_project[bucket] = marked_by_project.get(bucket, 0) + 1
    mark_messages_read(args.me, paths)
    result = {
        "marked_read": len(paths),
        "marked_read_by_project": dict(sorted(marked_by_project.items())),
    }
    if held_activation:
        result["held_activation"] = held_activation
        result["held_activation_paths"] = held_paths
    if repo_scope_refused:
        result["repo_scope_refused"] = repo_scope_refused
    return result


def format_message(msg: dict, index: int) -> str:
    fm = msg["frontmatter"]
    lines = [
        f"── Message {index + 1} {'[READ]' if msg.get('read') else '[UNREAD]'} ──",
        f"  Path:     {msg['path']}",
        f"  From:     {fm.get('from', '?')}",
        f"  Sender:   {fm.get('sender_agent_id', fm.get('from', '?'))}",
        f"  Title:    {fm.get('title', '(no title)')}",
        f"  Priority: {fm.get('priority', 'normal')}",
    ]
    if fm.get("project_id"):
        lines.append(f"  Project:  {fm['project_id']}")
    if fm.get("related_task"):
        lines.append(f"  Task:     {fm['related_task']}")
    if fm.get("repo_targets"):
        lines.append(f"  Repos:    {', '.join(fm['repo_targets'])}")
    if fm.get("sender_session_id"):
        lines.append(f"  Sender Session: {fm['sender_session_id']}")
    if fm.get("target_session_id"):
        lines.append(f"  Target Session: {fm['target_session_id']}")
    if fm.get("supersedes_session_id"):
        lines.append(f"  Supersedes: {fm['supersedes_session_id']}")
    lines.append(f"  Sent:     {fm.get('sent_utc', '?')}")
    activation_gate = msg.get("activation_gate")
    if activation_gate:
        lines.append(f"  Activation Gate: {activation_gate.get('reason')}")
        if activation_gate.get("fence_token") is not None:
            lines.append(f"  Activation Fence: {activation_gate['fence_token']}")
        owner = activation_gate.get("owner") or activation_gate.get("lease")
        if owner:
            lines.append(f"  Activation Owner: {json.dumps(owner, sort_keys=True)}")
    lines.append("")
    lines.append(msg["body"])
    lines.append("")
    return "\n".join(lines)


def main():
    args = parse_args()

    known = agent_ids()
    if args.me not in known:
        print(f"[error] Unknown agent: {args.me!r}", file=sys.stderr)
        print(f"       Known agents: {', '.join(known)}", file=sys.stderr)
        sys.exit(1)

    if args.mark_all_read:
        print(json.dumps(mark_all_read(args), sort_keys=True))
        return

    published_runtime = publish_runtime_identity(args)

    exact_session = None
    repo_scope_refused: list[dict] = []
    if args.session and not args.publish_session:
        exact_session = load_session(args.session)
        if exact_session.get("agent_id") != args.me:
            raise ValueError("--session does not belong to --me")
        if not exact_session.get("project_id") or not exact_session.get("chat_id"):
            raise ValueError("--session exact read requires project and chat scope")
        if args.project and exact_session.get("project_id") != args.project:
            raise ValueError("--session does not belong to --project")
        if args.chat and exact_session.get("chat_id") != args.chat:
            raise ValueError("--session does not belong to --chat")
        messages = matching_unread_messages(
            exact_session,
            invocation_repo_targets=args.repo_target,
            repo_scope_refusals=repo_scope_refused,
            max_entries=MAX_EXACT_SESSION_UNREAD_ENTRIES,
            max_bytes=MAX_EXACT_SESSION_UNREAD_BYTES,
        )
        messages = filter_messages(messages, args.project, args.chat, args.packet)
    elif args.packet:
        messages = load_all_messages(args.me)
    elif args.show_all:
        messages = load_all_messages(args.me)
    else:
        messages = get_unread_messages(args.me)

        messages = filter_messages(messages, args.project, args.chat, args.packet)
    if args.packet and len(messages) != 1:
        packet_selection_error(args, messages)
    messages, filtered_repo_scope = filter_repo_scope(
        messages, args.repo_target, args.project
    )
    repo_scope_refused.extend(filtered_repo_scope)
    if not args.packet and exact_session is None:
        messages = messages[: args.limit]

    if exact_session is not None and repo_scope_refused:
        if args.json_output:
            print(
                json.dumps(
                    {
                        "messages": [],
                        "repo_scope_refused": repo_scope_refused,
                    },
                    indent=2,
                )
            )
        else:
            for refused in repo_scope_refused:
                print(
                    f"[inbox] Repo-scope refused {refused['path']}: {refused['reason']}",
                    file=sys.stderr,
                )
        sys.exit(75)

    if not messages:
        if args.json_output:
            if not repo_scope_refused and published_runtime is None:
                print("[]")
            else:
                payload = {"messages": []}
                if repo_scope_refused:
                    payload["repo_scope_refused"] = repo_scope_refused
                if published_runtime is not None:
                    payload["published_runtime"] = published_runtime
                print(json.dumps(payload, indent=2))
        else:
            if published_runtime is not None:
                if published_runtime.get("published") is False:
                    print(
                        "[session] publish refused "
                        f"{published_runtime['runtime_family']}: "
                        f"{published_runtime['reason']}\n"
                    )
                else:
                    print(
                        "[session] published "
                        f"{published_runtime['session']['runtime']['family']} "
                        f"{published_runtime['session']['runtime']['session_id']} "
                        f"for {published_runtime['session']['session_id']}\n"
                    )
            print(f"[inbox] No {'messages' if args.show_all else 'unread messages'} for {args.me}.")
            for refused in repo_scope_refused:
                print(
                    f"[inbox] Repo-scope refused {refused['path']}: {refused['reason']}",
                    file=sys.stderr,
                )
        return

    consume = not args.peek and not args.show_all
    refused_gates: list[dict] = []
    for message in messages:
        gate = gate_activation_message(args, message, consume=consume)
        if gate is not None:
            message["activation_gate"] = gate
            if consume and not gate.get("authorized"):
                refused_gates.append({"path": message["path"], **gate})

    if refused_gates:
        payload = {"activation_refused": refused_gates, "messages": messages}
        if args.json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for gate in refused_gates:
                print(
                    f"[activation] refused {gate['path']}: {gate.get('reason')}",
                    file=sys.stderr,
                )
                owner = gate.get("owner")
                if owner:
                    print(f"  owner: {json.dumps(owner, sort_keys=True)}", file=sys.stderr)
        sys.exit(75)

    shown_paths = [m["path"] for m in messages if not m.get("read")]
    if consume:
        shown_paths, late_refused = recheck_repo_scope_before_read(
            args.me, shown_paths, args.repo_target, args.project
        )
        repo_scope_refused.extend(late_refused)
        if exact_session is not None:
            exact_paths = {
                message["path"]
                for message in matching_unread_messages(
                    load_session(args.session),
                    invocation_repo_targets=args.repo_target,
                    max_entries=MAX_EXACT_SESSION_UNREAD_ENTRIES,
                    max_bytes=MAX_EXACT_SESSION_UNREAD_BYTES,
                )
            }
            messages = [
                message for message in messages if message["path"] in exact_paths
            ]
            shown_paths = [path for path in shown_paths if path in exact_paths]

    if args.json_output:
        payload: dict[str, object] = {"messages": messages}
        if repo_scope_refused:
            payload["repo_scope_refused"] = repo_scope_refused
        if published_runtime is not None:
            payload["published_runtime"] = published_runtime
        print(json.dumps(payload, indent=2))
    else:
        if published_runtime is not None:
            if published_runtime.get("published") is False:
                print(
                    "[session] publish refused "
                    f"{published_runtime['runtime_family']}: "
                    f"{published_runtime['reason']}\n"
                )
            else:
                print(
                    "[session] published "
                    f"{published_runtime['session']['runtime']['family']} "
                    f"{published_runtime['session']['runtime']['session_id']} "
                    f"for {published_runtime['session']['session_id']}\n"
                )
        print(f"\n[inbox] {len(messages)} {'message(s)' if args.show_all else 'unread message(s)'} for {args.me}\n")
        for i, msg in enumerate(messages):
            print(format_message(msg, i))
        for refused in repo_scope_refused:
            print(
                f"[inbox] Repo-scope refused {refused['path']}: {refused['reason']}",
                file=sys.stderr,
            )

    if consume:
        mark_messages_read(args.me, shown_paths)


if __name__ == "__main__":
    main()
