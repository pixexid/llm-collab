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
  python bin/inbox.py --me orchestrator --mark-all-read
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    get_unread_messages,
    load_agent_inbox,
    mark_messages_read,
    parse_frontmatter,
    CHATS_DIR,
)
from _session_autobridge import ACTIVATION_MARKER_FIELDS, discover_runtime_session
from _activation_lease import (
    gated_claim,
    lease_identity,
    lease_key,
    load_lease,
    owner_summary,
)
from _session_autobridge import load_session, save_session
from _helpers import utc_iso
from session_autobridge import register_session


def parse_args():
    p = argparse.ArgumentParser(description="List unread messages for an agent.")
    p.add_argument("--me", required=True, help="Your agent ID")
    p.add_argument("--limit", type=int, default=10, help="Max messages to show (default: 10)")
    p.add_argument("--all", dest="show_all", action="store_true", help="Show all messages including read")
    p.add_argument("--peek", action="store_true", help="Do not mark shown messages as read")
    p.add_argument("--project", default=None, help="Filter by project_id")
    p.add_argument("--chat", default=None, help="Filter by chat substring")
    p.add_argument(
        "--packet",
        default=None,
        help="Exact packet filename (or relative path) — show/claim only that message",
    )
    p.add_argument("--mark-all-read", action="store_true", help="Mark all unread as read and exit")
    p.add_argument("--publish-session", action="store_true", help="Publish current runtime session identity before showing inbox")
    p.add_argument("--session", default=None, help="Stable llm-collab session id to update when publishing runtime identity")
    p.add_argument("--runtime-family", default=None, choices=("codex_app", "claude_app", "gemini_cli"), help="Runtime family for session discovery")
    p.add_argument("--project-path", default=None, help="Optional runtime project path hint for session discovery")
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON array")
    return p.parse_args()


def publish_runtime_identity(args) -> dict | None:
    if not args.publish_session:
        return None
    if not args.session:
        raise ValueError("--publish-session requires --session")
    if not args.runtime_family:
        raise ValueError("--publish-session requires --runtime-family")

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


def activation_reader_pid() -> int | None:
    """Process identity for lease binding. LLM_COLLAB_READER_PID overrides for
    tests/runtimes with a known stable process. Without an override there is
    NO reliable pid: os.getppid() is the short-lived tool shell in Desktop
    runtimes, and binding a transient pid would make the winner look crashed.
    Prefer the stable runtime identity (activation_reader_runtime_id)."""
    import os

    override = os.environ.get("LLM_COLLAB_READER_PID")
    if override and override.isdigit():
        return int(override)
    return None


READER_RUNTIME_ENV_VARS = (
    "LLM_COLLAB_READER_RUNTIME_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SESSION_ID",
    "GEMINI_SESSION_ID",
)


def activation_reader_runtime_id() -> str | None:
    """Stable per-session runtime identity for lease binding, mechanically
    discovered from the runtime's own environment. Claude Desktop/Code shells
    carry CLAUDE_CODE_SESSION_ID; LLM_COLLAB_READER_RUNTIME_ID overrides for
    tests or unusual runtimes. Constant across one session's short-lived tool
    shells, distinct across sessions — no hand-authored value needed."""
    import os

    for var in READER_RUNTIME_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def activation_reader_session_id(args, identity: dict) -> str:
    if args.session:
        return str(args.session)
    runtime_id = activation_reader_runtime_id()
    if runtime_id:
        return f"SESSION-act-{lease_key(identity)}-r{runtime_id[:12]}"
    import os

    return f"SESSION-act-{lease_key(identity)}-p{activation_reader_pid() or os.getppid()}"


READER_SESSION_TTL_SECONDS = 6 * 3600


def ensure_reader_session(session_id: str, agent_id: str, identity: dict) -> None:
    runtime_id = activation_reader_runtime_id()
    runtime = {"family": "reader", "session_id": runtime_id} if runtime_id else None
    try:
        existing = load_session(session_id)
        if runtime is not None and not (existing.get("runtime") or {}).get("session_id"):
            existing["runtime"] = runtime
            save_session(existing)
        return
    except FileNotFoundError:
        pass
    from datetime import datetime, timezone

    expires = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + READER_SESSION_TTL_SECONDS,
        tz=timezone.utc,
    ).isoformat(timespec="seconds")
    save_session(
        {
            "session_id": session_id,
            "agent_id": agent_id,
            "project_id": identity.get("project"),
            "chat_id": identity.get("chat"),
            "mode": "manual",
            "status": "parked",
            "wake_strategy": "none",
            "allowed_actions": [],
            "lease_owner": agent_id,
            "lease_expires_utc": expires,
            "ephemeral_reader": True,
            "runtime": runtime,
            "supersedes_session_id": None,
            "created_utc": utc_iso(),
            "processed_messages": [],
        }
    )


def gate_activation_message(args, msg: dict, *, consume: bool) -> dict | None:
    """One-writer gate at the mailbox boundary.

    Every activation-marked packet addressed to --me is gated when it is
    consumed: the reader's session claims the lease (audit-first, fail
    closed). A refused reader is told to hold read-only. Peek/--all show the
    current owner without claiming. Malformed activation never downgrades to
    an ordinary message.
    """
    fm = msg.get("frontmatter", {})
    if fm.get("to") != args.me:
        return None
    if not any(fm.get(field) for field in ACTIVATION_MARKER_FIELDS):
        return None
    try:
        identity = lease_identity(
            {
                "project": fm.get("project_id"),
                "chat": fm.get("chat_id"),
                "task": fm.get("related_task"),
                "worktree": fm.get("worktree"),
                "branch": fm.get("branch"),
                "target_agent": args.me,
            }
        )
    except ValueError as exc:
        return {
            "gate": "malformed_activation",
            "detail": str(exc),
            "authorized": False,
        }
    if not consume:
        lease = load_lease(identity)
        if lease is None or lease.get("status") != "active":
            return {"gate": "peek", "authorized": False, "owner": None}
        owner = owner_summary(lease)
        if args.session and args.session == owner.get("owner_session_id"):
            return {"gate": "peek_owner", "authorized": True, "owner": owner}
        return {"gate": "held_read_only", "authorized": False, "owner": owner}
    reader_pid = activation_reader_pid()
    reader_runtime = activation_reader_runtime_id()
    if reader_pid is None and reader_runtime is None:
        return {
            "gate": "refused",
            "authorized": False,
            "reason": "reader_identity_unbound",
            "hint": (
                "activation claims require a stable reader identity: export "
                "LLM_COLLAB_READER_RUNTIME_ID=<your session uuid> (or "
                "LLM_COLLAB_READER_PID for a stable process) and re-run"
            ),
        }
    session_id = activation_reader_session_id(args, identity)
    ensure_reader_session(session_id, args.me, identity)
    authorized, detail = gated_claim(
        identity,
        owner_session_id=session_id,
        owner_pid=reader_pid,
        claimant_runtime_id=reader_runtime,
        takeover=True,
    )
    return {
        "gate": "claimed" if authorized else "refused",
        "authorized": authorized,
        "reader_session_id": session_id,
        **detail,
    }


def format_activation_banner(gate: dict) -> str:
    if gate["gate"] == "malformed_activation":
        return (
            "  !! MALFORMED ACTIVATION PACKET — fail closed. Do NOT act on this "
            f"activation. ({gate['detail']})"
        )
    if gate["gate"] == "peek":
        return "  [activation] lease unclaimed — peek only, not claimed"
    if gate["gate"] == "peek_owner":
        owner = gate["owner"]
        return (
            f"  [activation] you are the writer ({owner.get('owner_session_id')}, "
            f"fence {owner.get('fence_token')})"
        )
    if gate["gate"] == "held_read_only":
        owner = gate["owner"]
        return (
            f"  !! ACTIVATION HELD by {owner.get('owner_session_id')} "
            f"(fence {owner.get('fence_token')}). You are NOT the writer — HOLD "
            "READ-ONLY: do not mutate the worktree."
        )
    if gate["authorized"]:
        return (
            f"  >> ACTIVATION LEASE CLAIMED as {gate['reader_session_id']} "
            f"(fence {gate['fence_token']}). You are the ONE writer. Run lease-assert "
            "with this fence before any repo mutation and before handoff."
        )
    owner = gate.get("owner") or {}
    banner = (
        f"  !! ACTIVATION REFUSED ({gate['reason']}) — owner: "
        f"{owner.get('owner_session_id', 'unknown')}. HOLD READ-ONLY: do not mutate "
        "the worktree, record the refusal, stand down."
    )
    if gate.get("hint"):
        banner += f"\n     hint: {gate['hint']}"
    return banner


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
        if "/" in packet:
            messages = [m for m in messages if m["path"] == packet]
        else:
            messages = [m for m in messages if Path(m["path"]).name == packet]
    return messages


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
    gate = msg.get("activation_gate")
    if gate is not None:
        lines.append(format_activation_banner(gate))
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
        inbox = load_agent_inbox(args.me)
        unread = inbox.get("unread", [])
        mark_messages_read(args.me, unread)
        print(json.dumps({"marked_read": len(unread)}))
        return

    published_runtime = publish_runtime_identity(args)

    if args.show_all:
        messages = load_all_messages(args.me)
    else:
        messages = get_unread_messages(args.me)

    messages = filter_messages(messages, args.project, args.chat, args.packet)

    consume = not args.peek and not args.show_all
    late_packet_mode = False
    if args.packet:
        # Selector cardinality is resolved across the FULL read+unread
        # namespace before any consume/late decision or lease/read-state
        # mutation: a basename colliding across states must never silently
        # pick whichever bucket wins first and claim the wrong activation.
        union = filter_messages(
            load_all_messages(args.me), args.project, args.chat, args.packet
        )
        if len(union) > 1:
            payload = {
                "error": "ambiguous_packet_selector",
                "packet": args.packet,
                "matches": [m["path"] for m in union],
                "hint": "pass the full relative packet path; nothing was claimed or marked read",
            }
            print(json.dumps(payload, indent=2))
            sys.exit(75)
        if consume and len(union) == 1 and union[0].get("read"):
            # The exact emitted claim command also serves a LATE invocation:
            # the winner already consumed the packet, so surface it from
            # `read` and run the gate (held/refused for a live owner, a fresh
            # claim after release) instead of a silent empty exit 0.
            messages = union
            late_packet_mode = True

    messages = messages[: args.limit]

    if not messages:
        if args.json_output:
            print("[]")
        else:
            print(f"[inbox] No {'messages' if args.show_all else 'unread messages'} for {args.me}.")
        return

    activation_refused = False
    for msg in messages:
        gate = gate_activation_message(args, msg, consume=consume)
        if gate is not None:
            msg["activation_gate"] = gate
            if gate["gate"] in {"refused", "malformed_activation", "held_read_only"}:
                activation_refused = True

    if args.json_output:
        payload: dict[str, object] = {"messages": messages}
        if published_runtime is not None:
            payload["published_runtime"] = published_runtime
        print(json.dumps(payload, indent=2))
    else:
        if published_runtime is not None:
            print(
                "[session] published "
                f"{published_runtime['session']['runtime']['family']} "
                f"{published_runtime['session']['runtime']['session_id']} "
                f"for {published_runtime['session']['session_id']}\n"
            )
        print(f"\n[inbox] {len(messages)} {'message(s)' if args.show_all else 'unread message(s)'} for {args.me}\n")
        for i, msg in enumerate(messages):
            print(format_message(msg, i))

    if consume and not late_packet_mode:
        # A refused/malformed/held activation must NOT consume the shared
        # packet: it stays unread and claimable by the rightful owner.
        consumable = [
            m["path"]
            for m in messages
            if m.get("activation_gate") is None or m["activation_gate"].get("authorized")
        ]
        mark_messages_read(args.me, consumable)
    if activation_refused:
        sys.exit(75)


if __name__ == "__main__":
    main()
