#!/usr/bin/env python3
from __future__ import annotations

"""
deliver.py — Send a message from one agent to another.

Writes the message to Chats/ (canonical record) and appends
a pointer to the recipient's agents/{id}/inbox.json.

If the recipient has activation.type == "human_relay", prints
a ready-to-paste handoff prompt for the human operator.

Usage:
  bin/deliver.py --chat last --from orchestrator --to worker --title "Implement feature X"
  echo "Body text" | bin/deliver.py --chat CHAT-abc123 --from orchestrator --to worker --title "..."
  bin/deliver.py --chat last --from orchestrator --to worker --title "..." --body-file brief.md
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
    build_handoff_prompt,
    ensure_project,
    has_collab_awareness,
    set_collab_awareness,
    find_chat_by_partial,
    get_agent,
    is_human_relay,
    python_cmd,
    load_chat_meta,
    print_handoff_prompt,
    shortid,
    slugify,
    ts,
    utc_iso,
    write_file,
    dump_frontmatter,
    write_chat_note,
)
from _session_autobridge import (
    find_dispatchable_target_session,
    load_binding,
    resolve_thread_pair_session_id,
    update_thread_pair,
)


def parse_args():
    p = argparse.ArgumentParser(description="Send a message between agents.")
    p.add_argument("--chat", required=True, help='"last", CHAT-id, or partial chat name')
    p.add_argument("--from", dest="sender", required=True, help="Sender agent ID")
    p.add_argument("--to", dest="recipient", required=True, help="Recipient agent ID")
    p.add_argument("--title", required=True, help="Short semantic message title")
    p.add_argument("--priority", default="normal", choices=["low", "normal", "high", "urgent"])
    p.add_argument("--tags", default="", help="Comma-separated tags (default: empty)")
    p.add_argument("--project", required=True, help="project_id this message relates to")
    p.add_argument("--related-task", default=None, help="TASK-id cross-reference")
    p.add_argument("--repo-targets", default="", help="Comma-separated repo IDs in scope")
    p.add_argument("--path-targets", default="", help="Comma-separated file/dir paths in scope")
    p.add_argument("--sender-agent-id", default=None, help="Override sender identity recorded in frontmatter")
    p.add_argument("--sender-session-id", default=None, help="Runtime session identifier for the sender")
    p.add_argument("--target-session-id", default=None, help="Explicit runtime session identifier to target")
    p.add_argument("--supersedes-session-id", default=None, help="Older sender session replaced by this sender session")
    p.add_argument(
        "--skip-awareness-instruction",
        action="store_true",
        help="Skip first-time awareness tracking/onboarding behavior for this delivery.",
    )
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
        "sender_agent_id": args.sender_agent_id or args.sender,
        "sender_session_id": args.sender_session_id,
        "target_session_id": args.target_session_id,
        "supersedes_session_id": args.supersedes_session_id,
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


def resolve_bound_runtime_session_id(project_id: str, chat_id: str, agent_id: str) -> str | None:
    try:
        binding = load_binding(project_id, chat_id, agent_id)
    except FileNotFoundError:
        return None
    runtime_session_id = binding.get("runtime_session_id")
    if not runtime_session_id:
        return None
    return str(runtime_session_id)


def main():
    args = parse_args()

    # Validate agents
    known = agent_ids()
    for aid, label in [(args.sender, "--from"), (args.recipient, "--to")]:
        if aid not in known:
            print(f"[error] {label} agent {aid!r} not found in agents.json", file=sys.stderr)
            print(f"       Known agents: {', '.join(known)}", file=sys.stderr)
            sys.exit(1)
    ensure_project(args.project, allow_none=False)

    # Resolve chat
    chat_dir = find_chat_by_partial(args.chat)
    if chat_dir is None:
        print(f"[error] Chat not found: {args.chat!r}", file=sys.stderr)
        print("       Use 'python bin/new_chat.py --title ...' to create one.", file=sys.stderr)
        sys.exit(1)

    meta = load_chat_meta(chat_dir)
    chat_id = meta.get("chat_id", chat_dir.name)
    chat_project_id = meta.get("project_id")
    if not chat_project_id:
        print(
            f"[error] Chat {chat_id} has no project_id in meta.json. "
            "Project scoping is required for messages.",
            file=sys.stderr,
        )
        print(
            "       Create a new chat with --project, or fix chat meta project_id before sending.",
            file=sys.stderr,
        )
        sys.exit(1)
    if chat_project_id != args.project:
        print(
            f"[error] Project mismatch for chat {chat_id}: "
            f"chat project_id={chat_project_id!r}, --project={args.project!r}",
            file=sys.stderr,
        )
        print(
            "       Send with the chat's project_id or use a chat for the intended project.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.sender_session_id is None:
        args.sender_session_id = (
            resolve_thread_pair_session_id(args.project, chat_id, args.sender, args.recipient)
            or resolve_bound_runtime_session_id(args.project, chat_id, args.sender)
        )

    if args.target_session_id is None:
        args.target_session_id = (
            resolve_thread_pair_session_id(args.project, chat_id, args.recipient, args.sender)
            or resolve_bound_runtime_session_id(args.project, chat_id, args.recipient)
        )
    autobridge_target = find_dispatchable_target_session(
        agent_id=args.recipient,
        project_id=args.project,
        chat_id=chat_id,
        target_session_id=args.target_session_id,
    )
    autobridge_ready = autobridge_target is not None

    body = read_body(args.body_file)
    recipient_agent = get_agent(args.recipient)
    recipient_type = recipient_agent.get("activation", {}).get("type")
    should_consider_onboarding = recipient_type != "human" and not args.skip_awareness_instruction
    first_time_awareness = should_consider_onboarding and not has_collab_awareness(args.recipient)

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
    if first_time_awareness:
        set_collab_awareness(args.recipient, to_path)

    if args.sender_session_id or args.target_session_id:
        update_thread_pair(
            args.project,
            chat_id,
            args.sender,
            args.recipient,
            sender_session_id=args.sender_session_id,
            target_session_id=args.target_session_id,
        )

    note_lines = [
        f"{args.sender} sent `{args.title}` to {args.recipient}.",
        f"Chat: `{chat_id}`",
    ]
    if args.sender_session_id:
        note_lines.append(f"Sender thread: `{args.sender_session_id}`")
    if args.target_session_id:
        note_lines.append(f"Target thread: `{args.target_session_id}`")
    write_chat_note(
        chat_dir,
        title=f"{args.sender} -> {args.recipient}: {args.title}",
        body="\n".join(note_lines),
        sender=args.sender,
        recipient="operator",
        project_id=args.project,
        extra_frontmatter={
            "informational_kind": "autobridge_turn_summary",
            "summary_event": "sent",
            "summary_sender": args.sender,
            "summary_recipient": args.recipient,
            "sender_session_id": args.sender_session_id,
            "target_session_id": args.target_session_id,
            "related_message_path": str(to_path.relative_to(ROOT)),
        },
    )

    result = {
        "chat_id": chat_id,
        "chat_dir": str(chat_dir.relative_to(ROOT)),
        "to_file": str(to_path.relative_to(ROOT)),
        "from_file": str(from_path.relative_to(ROOT)),
        "recipient_first_time_awareness": bool(first_time_awareness),
        "relay_required": args.recipient != "operator" and not autobridge_ready,
        "resolved_target_session_id": args.target_session_id,
        "autobridge_ready": autobridge_ready,
        "autobridge_session_id": autobridge_target.get("session_id") if autobridge_target else None,
    }
    print(json.dumps(result, indent=2))

    # Relay prompt for the human operator (always printed).
    if is_human_relay(recipient_agent) and not autobridge_ready:
        print_handoff_prompt(
            recipient_agent,
            sender_id=args.sender,
            first_time=bool(first_time_awareness),
        )
    elif not autobridge_ready:
        recipient_display = recipient_agent.get("display_name", args.recipient)
        border = "━" * 60
        print(f"\n{border}")
        print("📨 RELAY REQUIRED FOR OPERATOR:")
        print(border)
        print()
        print(
            f"Please send this to {recipient_display} ({args.recipient}) "
            f"for chat {chat_id} (project: {args.project}):"
        )
        print()
        print(
            build_handoff_prompt(
                recipient_agent,
                sender_id=args.sender,
                first_time=bool(first_time_awareness),
            )
        )
        print()
        print(border)


if __name__ == "__main__":
    main()
