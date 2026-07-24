#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

"""
deliver.py — Send a message from one agent to another.

Writes the message to Chats/ (canonical record) and appends
a pointer to the recipient's agents/{id}/inbox.json.

If a CLI-session recipient explicitly configures activation.ax_app (and is not
ax_attended_only), prints an AX doorbell instruction; an ax_attended_only
recipient (AXValue-opaque composer) instead gets an ATTENDED RECOVERY REQUIRED
instruction routing control to Codex (GH-1547). Codex-to-Codex delivery is a
deliberate exception:
the durable packet is preserved, but app activation is suppressed in favor of
Thread Coordination. Projects may opt Claude into a desktop-bridge fallback for
non-CLI targets. If the recipient has activation.type == "human_relay", prints
a ready-to-paste handoff prompt for the human operator. Other unresolved
activation types report an explicit unavailable state.

Usage:
  bin/deliver.py --chat last --from orchestrator --to worker --project my-app --title "Implement feature X"
  echo "Body text" | bin/deliver.py --chat CHAT-abc123 --from orchestrator --to worker --project my-app --title "..."
  bin/deliver.py --chat last --from orchestrator --to worker --project my-app --title "..." --body-file brief.md
"""

import argparse
import json

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
    get_project,
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
    ensure_agent_enabled,
    write_chat_note,
)
from _activation_identity import (
    activation_body_banner,
    build_activation_consume_command,
    build_activation_ring_prompt,
    canonical_worktree,
    normalized_identity_field,
)

# Lane C (GH-1572) flips this to True in the same commit that makes the
# packet's claim command (`inbox.py --packet`) runnable. Deliberately a code
# constant, NOT an environment variable or flag: the production CLI must have
# no way to enable activation delivery before the runtime integration exists.
ACTIVATION_RUNTIME_INTEGRATED = True


def allocate_activation_packet_paths(
    chat_dir: Path, timestamp: str, recipient: str, sender: str, slug: str
) -> tuple[Path, Path]:
    """Atomically reserve collision-free recipient AND sender packet paths.

    O_CREAT|O_EXCL makes each reservation atomic against concurrent writers;
    the attempt counter makes allocation deterministic even under repeating
    randomness — an existing packet is NEVER overwritten. Both copies share
    one suffix so the pair stays correlated."""
    for attempt in range(1, 200):
        nonce = os.urandom(3).hex()
        suffix = f"-{nonce}" if attempt == 1 else f"-{nonce}-{attempt}"
        to_path = chat_dir / f"{timestamp}_to-{recipient}_{slug}{suffix}.md"
        from_path = chat_dir / f"{timestamp}_from-{sender}_{slug}{suffix}.md"
        try:
            to_fd = os.open(to_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        try:
            from_fd = os.open(from_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            os.close(to_fd)
            os.unlink(to_path)
            continue
        os.close(to_fd)
        os.close(from_fd)
        return to_path, from_path
    raise OSError("exhausted unique activation packet name attempts")
from _session_autobridge import (
    EXACT_BINDING_MISMATCH_REASON,
    load_binding,
    resolve_exact_dispatch_target,
    resolve_thread_pair_session_id,
    session_target_ids,
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
    p.add_argument(
        "--activation",
        action="store_true",
        help="Mark this packet as a writer ACTIVATION. Requires --related-task, --worktree, and --branch together.",
    )
    p.add_argument(
        "--worktree",
        default=None,
        help="Assigned absolute worktree path (activation packets only; requires --activation)",
    )
    p.add_argument(
        "--branch",
        default=None,
        help="Assigned branch (activation packets only; requires --activation)",
    )
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


def build_message(args, body: str, chat_id: str, packet_name: str | None = None) -> str:
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    repo_targets = [r.strip() for r in args.repo_targets.split(",") if r.strip()]
    path_targets = [p.strip() for p in args.path_targets.split(",") if p.strip()]
    codex_self_target = is_codex_self_target(args.sender, args.recipient)

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
    if args.activation:
        fm["activation"] = True
        fm["worktree"] = args.worktree
        fm["branch"] = args.branch
        consume_command = build_activation_consume_command(
            args.recipient, args.project, chat_id, packet_name or "<packet>"
        )
        body = "\n".join([activation_body_banner(consume_command), "", body or "(no body)"])
    if codex_self_target:
        fm["autobridge_skip"] = True
        fm["autobridge_skip_reason"] = "codex_self_target"
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


def is_claude_desktop_bridge_target(
    project_id: str,
    recipient_agent: dict,
    recipient_id: str,
) -> bool:
    project = get_project(project_id) or {}
    activation_type = recipient_agent.get("activation", {}).get("type")
    return (
        bool(project.get("claude_desktop_bridge"))
        and recipient_id == "claude"
        and activation_type != "cli_session"
    )


def ax_doorbell_app(recipient_agent: dict) -> str | None:
    ax_app = recipient_agent.get("activation", {}).get("ax_app")
    if not isinstance(ax_app, str) or not ax_app.strip():
        return None
    return ax_app.strip()


def ax_attended_only(recipient_agent: dict) -> bool:
    """GH-1547 registry hint: the target's composer is AXValue-opaque, so
    routine AX doorbells are forbidden — only Codex-attended recovery may
    touch it. Must agree with the axsend binary's composer opacity table
    (tools/axbridge/send-resolution.swift); a fixture asserts they match."""
    return bool(recipient_agent.get("activation", {}).get("ax_attended_only"))


def is_codex_self_target(sender_id: str, recipient_id: str) -> bool:
    return sender_id == "codex" and recipient_id == "codex"


def is_ax_doorbell_target(
    recipient_agent: dict,
    recipient_id: str,
    *,
    sender_id: str,
) -> bool:
    activation_type = recipient_agent.get("activation", {}).get("type")
    return (
        not is_codex_self_target(sender_id, recipient_id)
        and recipient_id != "operator"
        and activation_type == "cli_session"
        and ax_doorbell_app(recipient_agent) is not None
        # GH-1547: an AXValue-opaque target never gets a routine doorbell —
        # it routes to Codex-attended recovery instead (never silently to
        # mailbox-only).
        and not ax_attended_only(recipient_agent)
    )


def is_ax_attended_recovery_target(
    recipient_agent: dict,
    recipient_id: str,
    *,
    sender_id: str,
) -> bool:
    """A target whose composer is opaque (activation.ax_attended_only): the
    durable packet is written as usual, but activation must be a Codex-attended
    recovery — an `--attended` axsend inside a supervised turn when the target
    has an ax_app, or an attended Computer-Use intervention when it does not
    (Antigravity). This supersedes human-relay routing for flagged targets: the
    operator is never the routine relay for an agent Codex can supervise."""
    return (
        not is_codex_self_target(sender_id, recipient_id)
        and recipient_id != "operator"
        and ax_attended_only(recipient_agent)
    )


def build_desktop_bridge_prompt(chat_id: str, recipient_id: str, message_path: Path) -> str:
    bridge_id = shortid(8)
    filename = message_path.name
    prompt = f"[BRIDGE {bridge_id}] Read latest {recipient_id} packet in {chat_id}: {filename}"
    if len(prompt) <= 240:
        return prompt
    return f"[BRIDGE {bridge_id}] Read latest {recipient_id} inbox packet for {chat_id} and respond here."


def main():
    args = parse_args()
    if args.activation:
        try:
            # The SAME validators the shared identity path uses
            # (bin/_activation_identity.py): missing/whitespace-only fields,
            # control characters (frontmatter-injection channel), and
            # relative worktrees all refuse here, before any file or inbox
            # mutation.
            args.related_task = normalized_identity_field("task", args.related_task)
            args.branch = normalized_identity_field("branch", args.branch)
            args.worktree = canonical_worktree(
                normalized_identity_field("worktree", args.worktree)
            )
        except ValueError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            sys.exit(2)
        if not ACTIVATION_RUNTIME_INTEGRATED:
            # Lane A ships the identity/serialization CONTRACT only. The
            # claim command the packet instructs (`inbox.py --packet`) is not
            # runnable until the runtime-integration lane (GH-1572) lands, so
            # delivering an activation packet now would hand a worker an
            # impossible required step. Fail closed pre-write. This is a
            # module constant — no environment variable or CLI flag can
            # enable it; Lane C deletes the guard when the exact command is
            # runnable.
            print(
                "[error] activation delivery unavailable: runtime integration "
                "(GH-1572 inbox claim/consumption) has not landed, so the "
                "packet's required claim command would not be runnable. "
                "Deliver an ordinary packet instead.",
                file=sys.stderr,
            )
            sys.exit(2)
    elif args.worktree or args.branch:
        print(
            "[error] --worktree/--branch are activation identity fields; pass --activation "
            "(with --related-task, --worktree, --branch) or drop them",
            file=sys.stderr,
        )
        sys.exit(2)
    thread_coordination_required = is_codex_self_target(args.sender, args.recipient)

    # Validate agents
    known = agent_ids()
    for aid, label in [(args.sender, "--from"), (args.recipient, "--to")]:
        if aid not in known:
            print(f"[error] {label} agent {aid!r} not found in agents.json", file=sys.stderr)
            print(f"       Known agents: {', '.join(known)}", file=sys.stderr)
            sys.exit(1)
        ensure_agent_enabled(aid, context=f"{label} message routing")
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

    explicit_target_session_id = args.target_session_id
    autobridge_refusal_reason = None
    if thread_coordination_required:
        # A Codex self-target is durable history only. Never retain a runtime
        # target that a later watcher could interpret as an app wake request.
        args.target_session_id = None
    else:
        if args.sender_session_id is None:
            args.sender_session_id = (
                resolve_thread_pair_session_id(args.project, chat_id, args.sender, args.recipient)
                or resolve_bound_runtime_session_id(args.project, chat_id, args.sender)
            )

    autobridge_target = None
    if not thread_coordination_required:
        autobridge_target, autobridge_refusal_reason = resolve_exact_dispatch_target(
            args.project,
            chat_id,
            args.recipient,
        )
        resolved_binding_target = resolve_bound_runtime_session_id(
            args.project,
            chat_id,
            args.recipient,
        )
        if autobridge_target is not None and resolved_binding_target is not None:
            if explicit_target_session_id and str(explicit_target_session_id) != resolved_binding_target:
                autobridge_target = None
                autobridge_refusal_reason = EXACT_BINDING_MISMATCH_REASON
                args.target_session_id = None
            else:
                args.target_session_id = resolved_binding_target
        else:
            args.target_session_id = None
    autobridge_ready = bool(
        autobridge_target is not None
        and args.target_session_id
        and str(args.target_session_id) in session_target_ids(autobridge_target)
    )

    body = read_body(args.body_file)
    recipient_agent = get_agent(args.recipient)
    recipient_type = recipient_agent.get("activation", {}).get("type")
    should_consider_onboarding = recipient_type != "human" and not args.skip_awareness_instruction
    first_time_awareness = should_consider_onboarding and not has_collab_awareness(args.recipient)

    if first_time_awareness:
        onboarding = build_handoff_prompt(
            recipient_agent,
            sender_id=args.sender,
            first_time=True,
        )
        body = f"{onboarding}\n\n---\n\n## Work packet\n\n{body or '(no body)'}"

    slug = slugify(args.title, max_len=40)
    timestamp = ts()
    activation_paths: tuple[Path, Path] | None = None
    if args.activation:
        # ts() has second precision: two same-title activations in one second
        # would collide, overwrite one packet, and dedupe to one inbox
        # pointer — silently losing an activation whose banner/ring command
        # must select exactly its own immutable packet. Names are allocated
        # with O_CREAT|O_EXCL (atomic against concurrent writers) and a
        # deterministic attempt counter, so even REPEATING randomness cannot
        # overwrite an existing recipient or sender packet. Ordinary-message
        # naming is unchanged.
        try:
            activation_paths = allocate_activation_packet_paths(
                chat_dir, timestamp, args.recipient, args.sender, slug
            )
        except OSError as exc:
            print(f"[error] could not allocate activation packet name: {exc}", file=sys.stderr)
            sys.exit(2)
        to_filename = activation_paths[0].name
    else:
        to_filename = f"{timestamp}_to-{args.recipient}_{slug}.md"
    # Pre-write wake-path classification: the same value later selects the
    # ring form, resolved before any file exists so downstream policy lanes
    # can fail closed pre-write.
    ax_doorbell_required = (
        args.recipient != "operator"
        and not autobridge_ready
        and is_ax_doorbell_target(
            recipient_agent,
            args.recipient,
            sender_id=args.sender,
        )
    )
    activation_ring_prompt = None
    if args.activation and ax_doorbell_required:
        # Built and bounded BEFORE any write: there is no generic/raw-file
        # ring for an activation packet, so an unfittable prompt fails the
        # delivery closed instead of degrading after files exist.
        try:
            activation_ring_prompt = build_activation_ring_prompt(
                args.sender,
                str(args.related_task),
                build_activation_consume_command(
                    args.recipient, args.project, chat_id, to_filename
                ),
            )
        except ValueError as exc:
            if activation_paths is not None:
                for reserved in activation_paths:
                    try:
                        reserved.unlink()
                    except FileNotFoundError:
                        pass
            print(f"[error] {exc}", file=sys.stderr)
            sys.exit(2)
    content = build_message(args, body, chat_id, packet_name=to_filename)

    # Write to-{recipient} file (recipient's copy)
    to_path = chat_dir / to_filename
    write_file(to_path, content)

    # Write from-{sender} file (sender's copy / sent record)
    if activation_paths is not None:
        from_path = activation_paths[1]
        from_filename = from_path.name
    else:
        from_filename = f"{timestamp}_from-{args.sender}_{slug}.md"
        from_path = chat_dir / from_filename
    write_file(from_path, content)

    # Update recipient inbox pointer
    add_to_inbox(args.recipient, to_path)
    if first_time_awareness:
        set_collab_awareness(args.recipient, to_path)

    if not thread_coordination_required and (args.sender_session_id or args.target_session_id):
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

    # (ax_doorbell_required and the activation ring were resolved pre-write;
    # activation packets never get a generic/raw-file ring.)
    ax_doorbell_prompt = None
    if ax_doorbell_required:
        if args.activation:
            ax_doorbell_prompt = activation_ring_prompt
        else:
            ax_doorbell_prompt = (
                f"[from {args.sender}] Read latest {args.recipient} packet in {chat_id}: {to_path.name}"
            )
    ax_attended_recovery_required = (
        args.recipient != "operator"
        and not autobridge_ready
        and is_ax_attended_recovery_target(
            recipient_agent,
            args.recipient,
            sender_id=args.sender,
        )
    )
    ax_attended_recovery_prompt = (
        f"[from {args.sender}] ATTENDED-RECOVERY needed for {args.recipient}: "
        f"read latest {args.recipient} packet in {chat_id}: {to_path.name} — "
        f"composer is AX-opaque; routine rings are refused."
        if ax_attended_recovery_required
        else None
    )
    desktop_bridge_required = (
        args.recipient != "operator"
        and not thread_coordination_required
        and not autobridge_ready
        and not ax_doorbell_required
        and not ax_attended_recovery_required
        and is_claude_desktop_bridge_target(args.project, recipient_agent, args.recipient)
    )
    desktop_bridge_prompt = (
        build_desktop_bridge_prompt(chat_id, args.recipient, to_path)
        if desktop_bridge_required
        else None
    )
    operator_relay_required = (
        args.recipient != "operator"
        and not thread_coordination_required
        and not autobridge_ready
        and not desktop_bridge_required
        and not ax_doorbell_required
        and not ax_attended_recovery_required
        and is_human_relay(recipient_agent)
    )
    activation_unavailable = (
        args.recipient != "operator"
        and not thread_coordination_required
        and not autobridge_ready
        and not desktop_bridge_required
        and not ax_doorbell_required
        and not ax_attended_recovery_required
        and not operator_relay_required
    )
    activation_unavailable_reason = None
    if activation_unavailable:
        if autobridge_refusal_reason and recipient_type == "cli_session":
            activation_unavailable_reason = autobridge_refusal_reason
        elif recipient_type == "cli_session":
            activation_unavailable_reason = (
                "cli_session has no dispatchable runtime session or activation.ax_app"
            )
        else:
            activation_unavailable_reason = (
                f"activation type {recipient_type!r} has no dispatchable runtime session"
            )

    result = {
        "chat_id": chat_id,
        "chat_dir": str(chat_dir.relative_to(ROOT)),
        "to_file": str(to_path.relative_to(ROOT)),
        "from_file": str(from_path.relative_to(ROOT)),
        "recipient_first_time_awareness": bool(first_time_awareness),
        "relay_required": operator_relay_required,
        "operator_relay_required": operator_relay_required,
        "desktop_bridge_required": desktop_bridge_required,
        "desktop_bridge_prompt": desktop_bridge_prompt,
        "ax_doorbell_required": ax_doorbell_required,
        "ax_doorbell_prompt": ax_doorbell_prompt,
        "ax_attended_recovery_required": ax_attended_recovery_required,
        "ax_attended_recovery_prompt": ax_attended_recovery_prompt,
        "thread_coordination_required": thread_coordination_required,
        "activation_unavailable": activation_unavailable,
        "activation_unavailable_reason": activation_unavailable_reason,
        "resolved_target_session_id": args.target_session_id,
        "autobridge_ready": autobridge_ready,
        "autobridge_refusal_reason": autobridge_refusal_reason,
        "autobridge_session_id": autobridge_target.get("session_id") if autobridge_target else None,
    }
    print(json.dumps(result, indent=2))

    if thread_coordination_required:
        border = "━" * 60
        print(f"\n{border}")
        print("🧭 CODEX THREAD COORDINATION REQUIRED")
        print(border)
        print()
        print(
            "The durable codex -> codex packet was written, but app activation "
            "was intentionally suppressed."
        )
        print()
        print("For a managed Codex worker, inspect it with read_thread and send focused")
        print("unblocks with send_message_to_thread. Use native subagent coordination")
        print("for bounded local support. Do not use AX or Computer Use to route this")
        print("packet to a Codex task.")
        print(border)
    elif desktop_bridge_required:
        recipient_display = recipient_agent.get("display_name", args.recipient)
        border = "━" * 60
        print(f"\n{border}")
        print("🖥️  CLAUDE DESKTOP BRIDGE REQUIRED")
        print(border)
        print()
        print(
            f"Use Computer Use against /Applications/Claude.app to wake "
            f"{recipient_display} ({args.recipient}) for chat {chat_id}."
        )
        print("Do not ask the operator to relay, paste, click, or manually wake Claude.")
        print()
        print("Visible one-line prompt:")
        print(desktop_bridge_prompt)
        print()
        print("If Computer Use is blocked or Claude is not idle, keep the heartbeat active,")
        print("retry through Codex/Computer Use when appropriate, and record exact failed attempts.")
        print(border)
    # GH-1547 (#110 P2 3609336511): the relay print must mirror the computed
    # operator_relay_required (which excludes attended-recovery targets) — the
    # raw is_human_relay() check made this branch shadow the attended-recovery
    # banner for Antigravity.
    elif operator_relay_required:
        print_handoff_prompt(
            recipient_agent,
            sender_id=args.sender,
            first_time=bool(first_time_awareness),
        )
    elif ax_attended_recovery_required:
        recipient_display = recipient_agent.get("display_name", args.recipient)
        border = "\u2501" * 60
        print(f"\n{border}")
        print("\u26d4 ATTENDED RECOVERY REQUIRED \u2014 routine AX ring is refused")
        print(border)
        print()
        print(
            f"{recipient_display} ({args.recipient}) has an AXValue-opaque composer: "
            "emptiness cannot be proven, so a routine axsend ring must not touch it "
            "(the binary refuses with exit 11; do not bypass with --attended yourself)."
        )
        print()
        print(
            "The durable packet above stays authoritative. Route control to Codex, "
            "the attended-recovery supervisor:"
        )
        print()
        if args.sender == "codex":
            recovery_ax_app = ax_doorbell_app(recipient_agent)
            if recovery_ax_app:
                mechanism = (
                    f"visible UI intervention, or `axsend ring --app "
                    f"{json.dumps(recovery_ax_app)} --attended ...` inside your "
                    "supervised turn"
                )
            else:
                mechanism = (
                    "attended Computer-Use intervention — this target has no "
                    "ax_app, so axsend cannot address it"
                )
            print(
                "You ARE the attended supervisor: perform the Codex-attended recovery "
                f"for {recipient_display} ({mechanism}) and verify the composer afterwards."
            )
        else:
            print("One-line prompt:")
            print(ax_attended_recovery_prompt)
            print()
            print("Command:")
            print(
                f"{ROOT}/bin/axsend-ensure ring --app \"Codex\" "
                f"--submit --verify --text {json.dumps(ax_attended_recovery_prompt)}"
            )
        print()
        print("Never fall back to mailbox-only silence: if Codex cannot be reached, record the blocker in the mailbox and keep the attended-recovery requirement visible.")
        print(border)
    elif ax_doorbell_required:
        recipient_display = recipient_agent.get("display_name", args.recipient)
        ax_app = ax_doorbell_app(recipient_agent)
        border = "━" * 60
        print(f"\n{border}")
        print("🔔 AX DOORBELL REQUIRED")
        print(border)
        print()
        print(
            f"Ring {recipient_display} ({args.recipient}) with axsend; "
            "do not ask the operator to relay."
        )
        print()
        print("One-line prompt:")
        print(ax_doorbell_prompt)
        print()
        print("Command:")
        print(
            f"{ROOT}/bin/axsend-ensure ring --app {json.dumps(ax_app)} "
            f"--submit --verify --text {json.dumps(ax_doorbell_prompt)}"
        )
        print()
        print("If axsend fails after retry/confirm, record the AX blocker in the mailbox.")
        print(border)
    elif activation_unavailable:
        recipient_display = recipient_agent.get("display_name", args.recipient)
        border = "━" * 60
        print(f"\n{border}")
        print("⚠️  ACTIVATION UNAVAILABLE")
        print(border)
        print()
        print(
            f"The durable packet for {recipient_display} ({args.recipient}) was written, "
            "but no wake transport is configured."
        )
        print()
        print(f"Reason: {activation_unavailable_reason}")
        print("Configure a dispatchable runtime session or activation.ax_app, then retry the wake.")
        print()
        print(border)


if __name__ == "__main__":
    main()
