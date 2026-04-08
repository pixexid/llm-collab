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

import argparse
import json
import sys
from pathlib import Path

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


def parse_args():
    p = argparse.ArgumentParser(description="List unread messages for an agent.")
    p.add_argument("--me", required=True, help="Your agent ID")
    p.add_argument("--limit", type=int, default=10, help="Max messages to show (default: 10)")
    p.add_argument("--all", dest="show_all", action="store_true", help="Show all messages including read")
    p.add_argument("--peek", action="store_true", help="Do not mark shown messages as read")
    p.add_argument("--project", default=None, help="Filter by project_id")
    p.add_argument("--chat", default=None, help="Filter by chat substring")
    p.add_argument("--mark-all-read", action="store_true", help="Mark all unread as read and exit")
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON array")
    return p.parse_args()


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


def filter_messages(messages: list[dict], project: str | None, chat: str | None) -> list[dict]:
    if project:
        messages = [m for m in messages if m["frontmatter"].get("project_id") == project]
    if chat:
        messages = [m for m in messages if chat.lower() in m["path"].lower()]
    return messages


def format_message(msg: dict, index: int) -> str:
    fm = msg["frontmatter"]
    lines = [
        f"── Message {index + 1} {'[READ]' if msg.get('read') else '[UNREAD]'} ──",
        f"  Path:     {msg['path']}",
        f"  From:     {fm.get('from', '?')}",
        f"  Title:    {fm.get('title', '(no title)')}",
        f"  Priority: {fm.get('priority', 'normal')}",
    ]
    if fm.get("project_id"):
        lines.append(f"  Project:  {fm['project_id']}")
    if fm.get("related_task"):
        lines.append(f"  Task:     {fm['related_task']}")
    if fm.get("repo_targets"):
        lines.append(f"  Repos:    {', '.join(fm['repo_targets'])}")
    lines.append(f"  Sent:     {fm.get('sent_utc', '?')}")
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

    if args.show_all:
        messages = load_all_messages(args.me)
    else:
        messages = get_unread_messages(args.me)

    messages = filter_messages(messages, args.project, args.chat)
    messages = messages[: args.limit]

    if not messages:
        if args.json_output:
            print("[]")
        else:
            print(f"[inbox] No {'messages' if args.show_all else 'unread messages'} for {args.me}.")
        return

    shown_paths = [m["path"] for m in messages]

    if args.json_output:
        print(json.dumps(messages, indent=2))
    else:
        print(f"\n[inbox] {len(messages)} {'message(s)' if args.show_all else 'unread message(s)'} for {args.me}\n")
        for i, msg in enumerate(messages):
            print(format_message(msg, i))

    if not args.peek and not args.show_all:
        mark_messages_read(args.me, shown_paths)


if __name__ == "__main__":
    main()
