#!/usr/bin/env python3
"""
deliver.py — Send a message from one agent to another.

Writes the message to Chats/ (canonical record) and appends
a pointer to the recipient's agents/{id}/inbox.json.

If the recipient has activation.type == "human_relay", prints
a ready-to-paste handoff prompt for the human operator.

Usage:
  python bin/deliver.py --chat last --from orchestrator --to worker --title "Implement feature X"
  echo "Body text" | python bin/deliver.py --chat CHAT-abc123 --from orchestrator --to worker --title "..."
  python bin/deliver.py --chat last --from orchestrator --to worker --title "..." --body-file brief.md
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    CHATS_DIR,
    add_to_inbox,
    agent_ids,
    ensure_project,
    find_chat_by_partial,
    get_agent,
    is_human_relay,
    load_chat_meta,
    print_handoff_prompt,
    shortid,
    slugify,
    ts,
    utc_iso,
    write_file,
    dump_frontmatter,
)


def parse_args():
    p = argparse.ArgumentParser(description="Send a message between agents.")
    p.add_argument("--chat", required=True, help='"last", CHAT-id, or partial chat name')
    p.add_argument("--from", dest="sender", required=True, help="Sender agent ID")
    p.add_argument("--to", dest="recipient", required=True, help="Recipient agent ID")
    p.add_argument("--title", required=True, help="Short semantic message title")
    p.add_argument("--priority", default="normal", choices=["low", "normal", "high", "urgent"])
    p.add_argument("--tags", default="", help="Comma-separated tags (default: empty)")
    p.add_argument("--project", default=None, help="project_id this message relates to")
    p.add_argument("--related-task", default=None, help="TASK-id cross-reference")
    p.add_argument("--repo-targets", default="", help="Comma-separated repo IDs in scope")
    p.add_argument("--path-targets", default="", help="Comma-separated file/dir paths in scope")
    p.add_argument(
        "--body-file",
        default="-",
        help='Path to markdown body, or "-" to read from stdin (default: -)',
    )
    return p.parse_args()


def read_body(body_file: str) -> str:
    if body_file == "-":
        if sys.stdin.isatty():
            print("[deliver] Reading body from stdin (Ctrl-D to finish):", file=sys.stderr)
        return sys.stdin.read().strip()
    return Path(body_file).read_text().strip()


def build_message(args, body: str, chat_id: str) -> str:
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    repo_targets = [r.strip() for r in args.repo_targets.split(",") if r.strip()]
    path_targets = [p.strip() for p in args.path_targets.split(",") if p.strip()]

    fm = {
        "chat_id": chat_id,
        "from": args.sender,
        "to": args.recipient,
        "title": args.title,
        "priority": args.priority,
        "tags": tags,
        "project_id": args.project,
        "related_task": args.related_task,
        "repo_targets": repo_targets,
        "path_targets": path_targets,
        "sent_utc": utc_iso(),
    }
    return dump_frontmatter(fm, body or "(no body)")


def main():
    args = parse_args()

    # Validate agents
    known = agent_ids()
    for aid, label in [(args.sender, "--from"), (args.recipient, "--to")]:
        if aid not in known:
            print(f"[error] {label} agent {aid!r} not found in agents.json", file=sys.stderr)
            print(f"       Known agents: {', '.join(known)}", file=sys.stderr)
            sys.exit(1)
    ensure_project(args.project, allow_none=True)

    # Resolve chat
    chat_dir = find_chat_by_partial(args.chat)
    if chat_dir is None:
        print(f"[error] Chat not found: {args.chat!r}", file=sys.stderr)
        print("       Use 'python bin/new_chat.py --title ...' to create one.", file=sys.stderr)
        sys.exit(1)

    meta = load_chat_meta(chat_dir)
    chat_id = meta.get("chat_id", chat_dir.name)

    body = read_body(args.body_file)
    content = build_message(args, body, chat_id)
    slug = slugify(args.title, max_len=40)
    timestamp = ts()

    # Write to-{recipient} file (recipient's copy)
    to_filename = f"{timestamp}_to-{args.recipient}_{slug}.md"
    to_path = chat_dir / to_filename
    write_file(to_path, content)

    # Write from-{sender} file (sender's copy / sent record)
    from_filename = f"{timestamp}_from-{args.sender}_{slug}.md"
    from_path = chat_dir / from_filename
    write_file(from_path, content)

    # Update recipient inbox pointer
    add_to_inbox(args.recipient, to_path)

    result = {
        "chat_id": chat_id,
        "chat_dir": str(chat_dir.relative_to(ROOT)),
        "to_file": str(to_path.relative_to(ROOT)),
        "from_file": str(from_path.relative_to(ROOT)),
    }
    print(json.dumps(result, indent=2))

    # Handoff prompt for human-relay agents
    recipient_agent = get_agent(args.recipient)
    if is_human_relay(recipient_agent):
        print_handoff_prompt(recipient_agent)


if __name__ == "__main__":
    main()
