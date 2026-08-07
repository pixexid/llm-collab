#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python
from current_runtime import require_current_runtime

require_python()

"""
deliver.py — Send a message from one agent to another.

Writes the message to Chats/ (canonical record) and appends
a pointer to the recipient's agents/{id}/inbox.json.

If the Codex CLI-session recipient configures a supported activation.ax_app
profile, prints an AX doorbell instruction (GH-470: the ring clears/overrides any
composer content and does not require a provably-empty or readable composer); a
supported ax_attended_only recipient (a target whose composer cannot be
resolved/driven at all, not merely a value-opaque one) instead gets an ATTENDED
RECOVERY REQUIRED instruction routing control to Codex. Codex-to-Codex delivery is a
deliberate exception: the durable packet is preserved, but app activation is
suppressed; managed Codex workers are inspected and steered through BB. Every
non-Codex watcher-backed recipient is also excluded:
its durable packet and background watcher are its target-side wake path. If the recipient
has activation.type == "human_relay", prints
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
    build_packet_ring_prompt,
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
from _ax_trust import ax_app_profile, ax_app_supports_routine_doorbell

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
    BINDING_UNREADABLE_REASON,
    BindingUnreadable,
    EXACT_BINDING_MISMATCH_REASON,
    load_thread_pair,
    repo_scope_matches,
    resolve_exact_dispatch_pair,
    session_target_ids,
    update_thread_pair,
)


def packet_repo_targets(args) -> list[str]:
    """The packet's declared repo scope.

    One definition, used by both the frontmatter writer and the preflight. Parsing the same
    argument in two places is how a preflight ends up disagreeing with the packet it is checking.
    """
    return [r.strip() for r in args.repo_targets.split(",") if r.strip()]


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
    repo_targets = packet_repo_targets(args)
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
    if getattr(args, "target_binding_id", None) is not None:
        fm["target_binding_id"] = args.target_binding_id
        fm["target_binding_generation"] = args.target_binding_generation
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


class SenderSessionProvenanceRefusal(ValueError):
    """The fallback pair cannot prove one sender session identity."""

    reason = "sender_session_provenance_refused"


def _pair_sender_session_id(
    pair: dict,
    *,
    sender: str,
    recipient: str,
) -> tuple[str | None, bool]:
    """Read the pair cache and detect disagreement within its own sender records."""
    sessions = pair.get("sessions")
    pair_session_id = None
    if isinstance(sessions, dict) and sessions.get(sender):
        pair_session_id = str(sessions[sender])

    last_direction = pair.get("last_direction")
    direction_session_id = None
    if (
        isinstance(last_direction, dict)
        and last_direction.get("from") == sender
        and last_direction.get("to") == recipient
        and last_direction.get("sender_session_id")
    ):
        direction_session_id = str(last_direction["sender_session_id"])

    return pair_session_id, bool(
        pair_session_id
        and direction_session_id
        and pair_session_id != direction_session_id
    )


def resolve_sender_session(
    project_id: str,
    chat_id: str,
    sender: str,
    recipient: str,
) -> tuple[str | None, str | None]:
    """Resolve sender identity from the current binding, then the pair cache.

    The current per-agent runtime binding is authoritative because a rebind is the
    event that changes ownership. The paired-thread record is only a fallback and
    its old value becomes explicit ``supersedes_session_id`` provenance when the
    live binding replaces it. If no binding exists, an internally contradictory
    pair cannot safely identify a sender and is refused before any write.
    """
    bound_session_id = resolve_bound_runtime_session_id(project_id, chat_id, sender)
    try:
        pair = load_thread_pair(project_id, chat_id, sender, recipient)
    except FileNotFoundError:
        pair = None
    except (OSError, ValueError) as error:
        # The pair is reread by update_thread_pair() after the durable packet is
        # written.  Treating this read as optional would turn that later failure
        # into a retryable error after the side effect already happened.
        raise SenderSessionProvenanceRefusal(
            f"thread pair could not be read before delivery: {error}"
        ) from error

    if pair is None:
        return bound_session_id, None

    pair_session_id, pair_conflicts = _pair_sender_session_id(
        pair,
        sender=sender,
        recipient=recipient,
    )
    if bound_session_id:
        if pair_session_id and pair_session_id != bound_session_id:
            return bound_session_id, pair_session_id
        return bound_session_id, None
    if pair_conflicts:
        raise SenderSessionProvenanceRefusal(
            f"thread pair has conflicting sender sessions for {sender!r}"
        )
    return pair_session_id, None


def resolve_bound_runtime_session_id(project_id: str, chat_id: str, agent_id: str) -> str | None:
    try:
        resolved, _reason, _inactive_pair = resolve_exact_dispatch_pair(
            project_id, chat_id, agent_id
        )
    except BindingUnreadable:
        # Refuse the RUNTIME target, never the durable write. Letting this propagate killed
        # deliver.py with a traceback before read_body(), so an oversized recipient binding meant
        # exit 1 and no packet at all -- and the mailbox is the one channel that must survive
        # every runtime failure. main() records the real cause; this only declines to target.
        return None
    if resolved is None:
        return None
    _session, binding = resolved
    runtime_session_id = binding.get("runtime_session_id")
    if not runtime_session_id:
        return None
    return str(runtime_session_id)


def ax_doorbell_app(recipient_agent: dict) -> str | None:
    if recipient_agent.get("id") != "codex":
        return None
    ax_app = recipient_agent.get("activation", {}).get("ax_app")
    if not ax_app_supports_routine_doorbell(ax_app):
        return None
    return ax_app.strip()


def ax_attended_only(recipient_agent: dict) -> bool:
    """Registry hint: the target's native composer cannot be resolved/driven for
    a routine AX ring, so only Codex-attended recovery may reach it. This only
    yields attended recovery for a no-app target or an ax_app that resolves to a
    supported opaque profile (see is_ax_attended_recovery_target); an ax_app that
    resolves to NO supported profile fails closed (activation_unavailable), not
    attended recovery. Post-GH-470 this flag is NOT about a value-opaque or
    non-empty composer of a *resolvable* Codex composer: a routine ring clears
    and overrides that content and proceeds (see
    tools/axbridge/send-resolution.swift routineRingDecision)."""
    return bool(recipient_agent.get("activation", {}).get("ax_attended_only"))


def is_watcher_only_target(recipient_agent: dict, recipient_id: str) -> bool:
    """A non-Codex worker's durable packet plus its watcher is the wake path."""
    activation = recipient_agent.get("activation", {})
    return (
        recipient_id != "codex"
        and activation.get("watcher_enabled") is True
    )


def is_codex_self_target(sender_id: str, recipient_id: str) -> bool:
    return sender_id == "codex" and recipient_id == "codex"


def is_ax_doorbell_target(
    recipient_agent: dict,
    recipient_id: str,
    *,
    sender_id: str,
) -> bool:
    activation = recipient_agent.get("activation", {})
    return (
        not is_codex_self_target(sender_id, recipient_id)
        and recipient_id != "operator"
        and not is_watcher_only_target(recipient_agent, recipient_id)
        and activation.get("type") == "cli_session"
        and ax_doorbell_app(recipient_agent) is not None
        # An unresolvable/undriveable target (ax_attended_only) never gets a
        # routine doorbell — it routes to Codex-attended recovery instead (never
        # silently to mailbox-only). GH-470: a merely value-opaque or non-empty
        # composer of a resolvable Codex target is NOT ax_attended_only and does
        # get the routine doorbell (the ring clears+overrides).
        and not ax_attended_only(recipient_agent)
    )


def is_ax_attended_recovery_target(
    recipient_agent: dict,
    recipient_id: str,
    *,
    sender_id: str,
) -> bool:
    """A target that cannot be reached by a routine ring at all
    (activation.ax_attended_only — an unresolvable/undriveable composer target,
    not merely a value-opaque one; GH-470): the durable packet is written as
    usual, but activation must be a Codex-attended recovery — an `--attended`
    axsend inside a supervised turn when the target has an ax_app, or an attended
    Computer-Use intervention when it does not
    (Antigravity). This supersedes human-relay routing for flagged targets: the
    operator is never the routine relay for an agent Codex can supervise."""
    activation = recipient_agent.get("activation", {})
    profile = ax_app_profile(activation.get("ax_app"))
    return (
        not is_codex_self_target(sender_id, recipient_id)
        and recipient_id != "operator"
        and not is_watcher_only_target(recipient_agent, recipient_id)
        and (
            ("ax_app" not in activation and profile is None)
            or profile in {"codex", "zcode"}
        )
        and ax_attended_only(recipient_agent)
    )


def main():
    # GH-503: delivery is always a mutation — refuse to write a durable packet
    # from a stale runtime/checkout (fails closed unless a loud recovery waiver).
    require_current_runtime("deliver")
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
    try:
        chat_dir = find_chat_by_partial(args.chat, project=args.project)
    except ValueError as error:
        print(f"[error] {error}", file=sys.stderr)
        sys.exit(1)
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
            try:
                args.sender_session_id, inferred_supersedes_session_id = resolve_sender_session(
                    args.project, chat_id, args.sender, args.recipient
                )
            except SenderSessionProvenanceRefusal as error:
                print(f"[error] {error.reason}: {error}", file=sys.stderr)
                sys.exit(2)
            if args.supersedes_session_id is None:
                args.supersedes_session_id = inferred_supersedes_session_id

    autobridge_target = None
    autobridge_binding = None
    inactive_pair = None
    durable_session = None
    binding_unreadable = False
    dispatch_scope_refused = False
    if not thread_coordination_required:
        try:
            pair, autobridge_refusal_reason, inactive_pair = resolve_exact_dispatch_pair(
                args.project,
                chat_id,
                args.recipient,
            )
            if pair is not None:
                autobridge_target, autobridge_binding = pair
        except BindingUnreadable as error:
            # Distinct from exact_binding_required, which means the binding is ABSENT. This record
            # exists and was refused, so the reason says so and carries the real cause with it.
            autobridge_target = None
            autobridge_refusal_reason = f"{BINDING_UNREADABLE_REASON}: {error}"
            binding_unreadable = True
        durable_session = autobridge_target or (
            inactive_pair[0] if inactive_pair is not None else None
        )
        durable_binding = autobridge_binding or (
            inactive_pair[1] if inactive_pair is not None else None
        )
        resolved_binding_target = (
            durable_binding.get("runtime_session_id") if durable_binding is not None else None
        )
        explicit_mismatches_binding = (
            explicit_target_session_id is not None
            and resolved_binding_target is not None
            and str(explicit_target_session_id) != str(resolved_binding_target)
        )
        if explicit_mismatches_binding:
            # The sender named a specific target thread the recipient's binding contradicts.
            # Do not re-address the packet to the binding's target; that would silently redirect
            # an explicit intent. Refuse so the sender sees the mismatch.
            autobridge_target = None
            autobridge_refusal_reason = EXACT_BINDING_MISMATCH_REASON
            args.target_session_id = None
        elif resolved_binding_target is not None:
            routable, scope_reason = repo_scope_matches(
                durable_session.get("repo_targets"),
                packet_repo_targets(args),
                subscriber_project=durable_session.get("project_id"),
                packet_project=args.project,
            )
            if not routable:
                autobridge_target = None
                autobridge_refusal_reason = scope_reason
                args.target_session_id = None
                dispatch_scope_refused = True
            else:
                args.target_session_id = str(resolved_binding_target)
                args.target_binding_id = durable_binding.get("binding_id")
                args.target_binding_generation = durable_binding.get("binding_generation")
        else:
            args.target_session_id = None
    autobridge_ready = bool(
        autobridge_target is not None
        and args.target_session_id
        and str(args.target_session_id) in session_target_ids(autobridge_target)
    )
    # Resolving the target is not the same as being able to route to it. The watcher applies the
    # repo-scope contract at dispatch time and silently refuses a packet that does not satisfy it;
    # reporting autobridge_ready here without applying the SAME rule is what let 27 packets be
    # written, reported as ready, and never delivered. This adds no second routing rule -- it runs
    # the existing one early so the sender is told the truth.
    # Terminal for the same reason a scope refusal is: no lane may wake a recipient whose
    # authoritative record could not be read. Waking them to "go read it" would be the wrong-wake
    # bug again, one cause over.
    dispatch_scope_refused = dispatch_scope_refused or binding_unreadable
    diagnostic_target = durable_session if dispatch_scope_refused else autobridge_target

    # One predicate for every wake lane, so a refusal cannot reach any of them and a lane added
    # later cannot silently miss the gate by re-deriving `not autobridge_ready` on its own.
    wake_fallback_allowed = not autobridge_ready and not dispatch_scope_refused

    body = read_body(args.body_file)
    slug = slugify(args.title, max_len=40)
    timestamp = ts()
    if not body:
        candidate = chat_dir / f"{timestamp}_to-{args.recipient}_{slug}.md"
        print(
            f"[error] refusing empty message body; packet not written: {candidate}",
            file=sys.stderr,
        )
        sys.exit(2)
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
        and wake_fallback_allowed
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
            ax_doorbell_prompt = build_packet_ring_prompt(
                args.sender, args.recipient, chat_id, to_path.name
            )
    ax_attended_recovery_required = (
        args.recipient != "operator"
        and wake_fallback_allowed
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
    # The project-configured Computer Use fallback was Claude-only, and Claude is
    # never woken by typing into its app. Its only producer is deleted; the keys stay
    # in the result so the scope-refusal wake-flag guard keeps covering them.
    desktop_bridge_required = False
    desktop_bridge_prompt = None
    recipient_activation = recipient_agent.get("activation", {})
    recipient_ax_app_present = "ax_app" in recipient_activation
    recipient_ax_profile = ax_app_profile(recipient_activation.get("ax_app"))
    operator_relay_required = (
        args.recipient != "operator"
        and not thread_coordination_required
        and wake_fallback_allowed
        and not desktop_bridge_required
        and not ax_doorbell_required
        and not ax_attended_recovery_required
        and not is_watcher_only_target(recipient_agent, args.recipient)
        and (
            not recipient_ax_app_present
            or recipient_ax_profile in {"codex", "zcode"}
        )
        and is_human_relay(recipient_agent)
    )
    watcher_pickup_ready = (
        args.recipient != "operator"
        and not thread_coordination_required
        and wake_fallback_allowed
        and is_watcher_only_target(recipient_agent, args.recipient)
    )
    activation_unavailable = (
        args.recipient != "operator"
        and not thread_coordination_required
        and wake_fallback_allowed
        and not desktop_bridge_required
        and not ax_doorbell_required
        and not ax_attended_recovery_required
        and not operator_relay_required
        and not watcher_pickup_ready
    )
    activation_unavailable_reason = None
    if activation_unavailable:
        if recipient_ax_app_present and recipient_ax_profile is None:
            activation_unavailable_reason = (
                "activation.ax_app must be a non-empty string when present"
            )
        elif recipient_ax_profile == "claude":
            activation_unavailable_reason = (
                "activation.ax_app resolves to the Claude profile, which cannot be a "
                "target-side wake transport"
            )
        elif recipient_ax_profile == "unknown":
            activation_unavailable_reason = (
                "activation.ax_app has no supported native composer profile"
            )
        elif autobridge_refusal_reason and recipient_type == "cli_session":
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
        "watcher_pickup_ready": watcher_pickup_ready,
        "activation_unavailable": activation_unavailable,
        "activation_unavailable_reason": activation_unavailable_reason,
        "resolved_target_session_id": args.target_session_id,
        "autobridge_ready": autobridge_ready,
        "autobridge_refusal_reason": autobridge_refusal_reason,
        # An explicit machine-readable blocker, because every wake flag AND activation_unavailable
        # are false in this state and a caller reading only those would see nothing to act on.
        "binding_unreadable_blocker": binding_unreadable,
        "autobridge_session_id": (
            diagnostic_target.get("session_id") if diagnostic_target else None
        ),
    }
    print(json.dumps(result, indent=2))

    # `autobridge_target is not None` was the banner's gate, and the unreadable-binding path sets
    # the target to None -- so the one refusal a sender can do nothing about printed no banner at
    # all, while every wake flag and activation_unavailable were also false. A sender following the
    # documented flags saw no required action for a packet that will wake nobody.
    refusal_banner_required = (
        not autobridge_ready
        and autobridge_target is not None
        and bool(autobridge_refusal_reason)
    )
    if binding_unreadable or dispatch_scope_refused or refusal_banner_required:
        border = "━" * 60
        print(f"\n{border}", file=sys.stderr)
        print("⚠️  DURABLE WRITE OK — RUNTIME DISPATCH REFUSED", file=sys.stderr)
        if binding_unreadable:
            print("blocker: the recipient's binding could not be READ (not missing). Nothing will "
                  "wake them by any lane until it is repaired or removed.", file=sys.stderr)
        print(border, file=sys.stderr)
        print(f"reason: {autobridge_refusal_reason}", file=sys.stderr)
        print(
            f"packet repo_targets: {packet_repo_targets(args) or '[] (none declared)'}",
            file=sys.stderr,
        )
        if diagnostic_target is not None:
            print(
                f"subscriber repo_targets: {diagnostic_target.get('repo_targets')}",
                file=sys.stderr,
            )
        print(file=sys.stderr)
        print(
            "The message IS in the mailbox and readable with inbox.py. It will NOT be "
            "dispatched to the running worker.",
            file=sys.stderr,
        )
        print(
            "If the recipient declares a repo scope, the packet must declare a subset of it: "
            "re-send with --repo-targets <ids>.",
            file=sys.stderr,
        )

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
        print("For a managed Codex worker, use its BB thread for inspection and steering;")
        print("follow docs/workflows/bb-workers.md. This durable packet is coordination")
        print("history, not a BB transport. Do not use AX or Computer Use to route this")
        print("packet to a Codex task.")
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
            f"{recipient_display} ({args.recipient})'s native composer target cannot be "
            "resolved or verified as a safe send target, so a routine axsend ring "
            "cannot reach it (the binary refuses with exit 11; do not bypass with "
            "--attended yourself). GH-470: this is a target-resolution hold, not a "
            "value-opaque or non-empty composer — a resolvable Codex composer is "
            "cleared and overridden by a routine ring."
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
        print("Configure a dispatchable runtime session or a supported AX profile, then retry the wake.")
        print()
        print(border)


if __name__ == "__main__":
    main()
