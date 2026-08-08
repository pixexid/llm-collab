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
from current_runtime import require_current_runtime

require_python()

import argparse
import json
import os
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from _activation_cleanup import claim_activation_lease
from _activation_identity import classify_activation, lease_key
from _activation_lease import (
    LeaseRefused,
    load_lease,
    owner_summary,
    pid_from_env,
    runtime_family_from_env,
    runtime_id_from_env,
)
from _helpers import (
    AGENTS_FILE,
    PROJECTS_FILE,
    ROOT,
    agent_inbox_path,
    agent_ids,
    get_unread_messages,
    load_agent_inbox,
    mark_messages_read,
    parse_frontmatter,
    now_utc,
    utc_iso,
)
from _session_autobridge import (
    _repo_target_set,
    BindingUnreadable,
    HEURISTIC_RUNTIME_DISCOVERY_FAMILIES,
    HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
    MAX_DISPATCH_INBOX_BYTES,
    MAX_DISPATCH_PACKET_BYTES,
    ReadBudget as ExactReadBudget,
    UnreadableFile,
    active_read_budget,
    autobridge_session_path,
    binding_scoped_message_matches_session,
    discover_runtime_session,
    load_binding,
    load_session,
    read_regular_file_bounded,
    repo_scope_matches,
    runtime_metadata,
    save_session,
    session_target_ids,
    _session_write_lock,
)
from session_autobridge import (
    AmbiguousNativeFamily,
    NativeSessionOwnedElsewhere,
    register_session,
    refuse_native_session_active_elsewhere,
    resolve_native_family,
)

MAX_EXACT_SESSION_ENTRIES = 5_000
MAX_EXACT_SESSION_BYTES = 16 * 1024 * 1024
MAX_MESSAGE_SCAN_ENTRIES = MAX_EXACT_SESSION_ENTRIES


class InboxScanLimitExceeded(ValueError):
    """The complete listing cannot be proved within the scan bound."""


def exact_registry_contains(
    path: Path,
    collection: str,
    identity: str,
    budget: ExactReadBudget,
) -> bool:
    raw = read_regular_file_bounded(path, budget.remaining)
    payload = json.loads(raw.decode("utf-8"))
    records = payload.get(collection) if isinstance(payload, dict) else None
    return isinstance(records, list) and any(
        isinstance(record, dict) and record.get("id") == identity
        for record in records
    )


def parse_args():
    p = argparse.ArgumentParser(description="List unread messages for an agent.")
    p.add_argument("--me", required=True, help="Your agent ID")
    p.add_argument("--limit", type=int, default=None, help="Max ordinary inbox messages to show (default: 10)")
    p.add_argument("--all", dest="show_all", action="store_true", help="Show all messages including read")
    p.add_argument("--peek", action="store_true", help="Do not mark shown messages as read")
    p.add_argument(
        "--acknowledge",
        action="store_true",
        help="Exact-session only: read the bound session's unread set and mark exactly "
        "that set read. This is the Pi wake drain — one event, drain the durable queue.",
    )
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
    if args.session is not None and (
        not args.session.strip() or args.session != args.session.strip()
    ):
        p.error("--session requires a non-empty session id")
    if args.packet is not None and (
        not args.packet.strip() or args.packet != args.packet.strip()
    ):
        p.error("--packet requires a non-empty packet selector")
    if args.repo_target is not None and args.project is None:
        p.error("--repo-target requires --project <id>")

    if args.all_projects and not args.mark_all_read:
        p.error("--all-projects is only valid with --mark-all-read")

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

    if args.acknowledge and args.session is None:
        p.error("--acknowledge is an exact-session operation and requires --session")
    exact_read = args.session is not None and not args.publish_session
    if exact_read:
        if args.peek == args.acknowledge:
            p.error("--session requires exactly one of --peek (read) or "
                    "--acknowledge (read and drain)")
        if args.show_all:
            p.error("--session exact reads do not support --all")
        if args.project is None or args.chat is None:
            p.error("--session exact reads require --project and --chat")
        if not args.chat.strip():
            p.error("--session exact reads require a non-empty --chat")

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
    """Load a complete, bounded message listing for an agent."""
    budget = ExactReadBudget(MAX_DISPATCH_INBOX_BYTES, "inbox scan")
    with active_read_budget(budget):
        try:
            raw = read_regular_file_bounded(agent_inbox_path(agent_id), budget.remaining)
        except FileNotFoundError:
            return []
        inbox = json.loads(raw.decode("utf-8"))
        if not isinstance(inbox, dict):
            raise ValueError("inbox index must be an object")
        read_paths = inbox.get("read", [])
        unread_paths = inbox.get("unread", [])
        if not isinstance(read_paths, list) or not isinstance(unread_paths, list):
            raise ValueError("inbox index must contain read and unread lists")
        total = len(read_paths) + len(unread_paths)
        if total > MAX_MESSAGE_SCAN_ENTRIES:
            raise InboxScanLimitExceeded(
                f"inbox scan exceeds {MAX_MESSAGE_SCAN_ENTRIES} entries; refusing an incomplete result"
            )
        paths = [*read_paths, *unread_paths]
        if any(
            not isinstance(path, str)
            or not path
            or path != path.strip()
            or "\x00" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            for path in paths
        ):
            raise ValueError("inbox index contains invalid message pointers")
        read_set = set(read_paths)
        messages = []
        for rel_path in paths:
            abs_path = ROOT / rel_path
            if abs_path.exists():
                message_raw = read_regular_file_bounded(
                    abs_path,
                    min(MAX_DISPATCH_PACKET_BYTES, budget.remaining),
                )
                fm, body = parse_frontmatter(message_raw.decode("utf-8"))
                messages.append({
                    "path": rel_path,
                    "read": rel_path in read_set,
                    "frontmatter": fm,
                    "body": body,
                })
        return messages


def exact_read_session(args, budget: ExactReadBudget) -> dict:
    if not exact_registry_contains(AGENTS_FILE, "agents", args.me, budget):
        raise ValueError("unknown_agent")
    if not exact_registry_contains(PROJECTS_FILE, "projects", args.project, budget):
        raise ValueError("unknown_project")
    raw = read_regular_file_bounded(
        autobridge_session_path(args.session), budget.remaining
    )
    session = json.loads(raw.decode("utf-8"))
    if not isinstance(session, dict):
        raise ValueError("exact-session record must be an object")
    runtime = runtime_metadata(session)
    expected = {
        "session_id": args.session,
        "agent_id": args.me,
        "project_id": args.project,
        "chat_id": args.chat,
    }
    if any(session.get(key) != value for key, value in expected.items()):
        raise ValueError("exact_session_mismatch")
    if session.get("status") not in {"active", "parked"}:
        raise ValueError("exact_session_inactive")
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in (runtime.get("family"), runtime.get("session_id"))
    ):
        raise ValueError("exact_session_runtime_missing")

    binding = load_binding(args.project, args.chat, args.me)
    binding_expected = {
        **expected,
        "runtime_family": runtime["family"],
        "runtime_session_id": runtime["session_id"],
    }
    if any(binding.get(key) != value for key, value in binding_expected.items()):
        raise ValueError("exact_binding_mismatch")
    if any(
        binding.get(key) != session.get(key)
        for key in ("binding_id", "binding_generation")
    ):
        raise ValueError("exact_binding_generation_mismatch")
    reader_runtime_id = activation_reader_runtime_id()
    if not reader_runtime_id:
        raise ValueError("exact_session_runtime_missing")
    if reader_runtime_id != runtime["session_id"]:
        raise ValueError("exact_session_runtime_mismatch")
    return session


def packet_path_matches(path: str, selector: str) -> bool:
    return path == selector if "/" in selector else Path(path).name == selector


def exact_read_messages(
    args, session: dict, budget: ExactReadBudget
) -> tuple[list[dict], list[dict]]:
    raw = read_regular_file_bounded(agent_inbox_path(args.me), budget.remaining)
    inbox = json.loads(raw.decode("utf-8"))
    if not isinstance(inbox, dict):
        raise ValueError("exact-session inbox index must be an object")
    unread = inbox.get("unread")
    read = inbox.get("read")
    if not isinstance(unread, list) or not isinstance(read, list):
        raise ValueError("exact-session inbox index must contain unread and read lists")
    entries = [*unread, *read]
    if len(entries) > MAX_EXACT_SESSION_ENTRIES:
        raise ValueError(
            f"exact-session inbox exceeds {MAX_EXACT_SESSION_ENTRIES} entries"
        )
    if any(
        not isinstance(path, str)
        or not path
        or path != path.strip()
        or "\x00" in path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        for path in entries
    ):
        raise ValueError("exact-session inbox contains invalid pointers")
    if len(entries) != len(set(entries)):
        raise ValueError("exact-session inbox contains duplicate pointers")

    messages = []
    refusals = []
    selected = (
        [path for path in entries if packet_path_matches(path, args.packet)]
        if args.packet
        else unread
    )
    targets = session_target_ids(session)
    for relative_path in selected:
        path = ROOT / relative_path
        try:
            message_raw = read_regular_file_bounded(path, budget.remaining)
        except FileNotFoundError as error:
            raise ValueError(
                f"missing exact-session packet: {relative_path}"
            ) from error
        frontmatter, body = parse_frontmatter(message_raw.decode("utf-8"))
        message = {
            "path": relative_path,
            "read": relative_path in read,
            "frontmatter": frontmatter,
            "body": body,
        }
        if (
            frontmatter.get("project_id") != args.project
            or frontmatter.get("chat_id") != args.chat
            or frontmatter.get("to") != args.me
            or str(frontmatter.get("target_session_id") or "") not in targets
        ):
            continue
        repo_targets = frontmatter.get("repo_targets")
        if repo_targets is not None and _repo_target_set(repo_targets) is None:
            raise ValueError(
                f"malformed exact-session packet repo_targets: {relative_path}"
            )
        matched, reason = binding_scoped_message_matches_session(
            session,
            message,
            invocation_repo_targets=args.repo_target,
        )
        if matched:
            messages.append(message)
        else:
            # GH-417: distinguish a REPO-SCOPE refusal (the packet is simply not for
            # this repo scope -> skippable) from a binding-identity or stale-generation
            # refusal (fail-closed) by re-running the repo-scope check, NOT by the
            # shared route_ambiguous reason string, which binding_scoped_message_
            # matches_session returns for BOTH repo-scope and target_binding_id
            # mismatch. `repo_scope_only` is True only when repo scope is the failing
            # dimension.
            repo_allowed, _repo_reason = repo_scope_matches(
                args.repo_target,
                frontmatter.get("repo_targets"),
                subscriber_project=args.project,
                packet_project=frontmatter.get("project_id"),
            )
            refusals.append(
                {
                    "path": relative_path,
                    "reason": reason,
                    "repo_scope_only": not repo_allowed,
                }
            )
    return messages, refusals


def select_exact_read_messages(
    args, session: dict, budget: ExactReadBudget
) -> tuple[list[dict], list[dict], bool]:
    """Return routable messages, public refusals, and refusal-only status."""
    messages, refusals = exact_read_messages(args, session, budget)
    refusal_only = not messages and any(
        not refusal.get("repo_scope_only") for refusal in refusals
    )
    return (
        messages,
        [
            {"path": refusal["path"], "reason": refusal["reason"]}
            for refusal in refusals
        ],
        refusal_only,
    )


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
        messages = [
            message
            for message in messages
            if packet_path_matches(message["path"], packet)
        ]
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


def activation_reader_runtime_family() -> str | None:
    return runtime_family_from_env()


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
    runtime_family: str | None = None,
) -> dict:
    try:
        existing = load_session(session_id)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        # GH-468: a first family-less attempt persists a synthetic/unresolved reader
        # that the consume gate refuses. If a later attempt now carries (or can
        # resolve) the real family, upgrade this cached reader in place — otherwise
        # the packet is refused forever. Only touch an ephemeral reader whose family
        # is still unresolved; never mutate an ordinary or already-resolved session.
        if existing.get("ephemeral_reader") and runtime_id:
            cur_family = (existing.get("runtime") or {}).get("family")
            if not cur_family or cur_family == "reader":
                new_family = runtime_family or resolve_native_family(runtime_id)
                if new_family:
                    with _session_write_lock():
                        refuse_native_session_active_elsewhere(
                            existing["session_id"],
                            existing["project_id"],
                            existing["chat_id"],
                            runtime_id,
                            new_family,
                            existing.get("status", "parked"),
                        )
                        existing.setdefault("runtime", {})["family"] = new_family
                        save_session(existing)
        return existing
    if existing is None:
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
        # GH-468: the auto-created reader lease is parked+unexpired+native-bound,
        # i.e. dispatchable, so it is a second write path that could bind a native
        # already dispatchable in another (project, chat). Route it through the
        # same locked ownership seam as register — the guard raises (no write)
        # before save_session on a cross-scope collision.
        #
        # LLM_COLLAB_READER_RUNTIME_ID carries the REGISTERED native id, so a
        # reader's runtime id IS the worker's ordinary native. Native identity is
        # (family, id), so the reader must carry its ACTUAL family. Prefer the
        # family carried from the environment (LLM_COLLAB_READER_RUNTIME_FAMILY or
        # a family-specific id var) — that is authoritative even before any
        # ordinary lease exists. Otherwise fall back to the family of an existing
        # ordinary lease for this native, and only to the synthetic "reader" when
        # neither is available (no owner to collide with). Resolve + guard + write
        # under one lock.
        with _session_write_lock():
            native_family = (
                (runtime_family or resolve_native_family(runtime_id) or "reader")
                if runtime_id
                else None
            )
            if runtime_id:
                payload["runtime"]["family"] = native_family
            refuse_native_session_active_elsewhere(
                session_id,
                identity["project"],
                identity["chat"],
                runtime_id,
                native_family,
                "parked",
            )
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
    except LeaseRefused as exc:
        # Resolving the lease directory fails closed on an invalid/unregistered project
        # scope (and the legacy/bounded guards). That is TERMINAL: it must not fall through
        # to reader-session creation or the claim's poller cleanup, which would mutate on
        # behalf of a scope that can never be granted. A genuinely unreadable existing
        # lease is a different error class and is still tolerated below.
        return {
            "authorized": False,
            "reason": exc.reason,
            "identity": identity,
            "owner": exc.owner,
        }
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
    owner_pid = None if runtime_id else activation_reader_pid()
    session_id = activation_reader_session_id(args, identity)
    try:
        reader = ensure_reader_session(
            session_id, args.me, identity,
            runtime_id=runtime_id,
            runtime_family=activation_reader_runtime_family(),
        )
    except NativeSessionOwnedElsewhere as exc:
        # A cross-scope ownership collision is a normal terminal outcome: surface
        # a stable structured refusal (JSON clients branch on this reason).
        return {
            "authorized": False,
            "reason": "native_session_owned_elsewhere",
            "identity": identity,
            "detail": str(exc),
        }
    except AmbiguousNativeFamily as exc:
        # Several live families share the native id: a distinct reason, not an
        # ownership collision — the remediation differs (disambiguate the id).
        return {
            "authorized": False,
            "reason": "native_family_ambiguous",
            "identity": identity,
            "detail": str(exc),
        }
    # Registry corruption / save-validation raise other exceptions; they are NOT
    # ownership outcomes and must surface distinctly rather than be mislabeled.

    # GH-468: making an unresolved-family reader non-dispatchable only closes the
    # autobridge route; the direct consume path below would still claim the lease
    # and mark the packet read, so native X could serve scope A here while a later
    # ordinary (real_family, id) registration serves scope B. When a reader has a
    # runtime id but no resolvable real family (absent or the synthetic "reader"),
    # native ownership cannot be established, so REFUSE before claiming — leave the
    # packet unread. Readers with a carried/resolved family, or with no runtime id
    # at all, are unaffected.
    if runtime_id:
        reader_family = (reader.get("runtime") or {}).get("family")
        if not reader_family or reader_family == "reader":
            return {
                "authorized": False,
                "reason": "reader_runtime_family_unresolved",
                "identity": identity,
            }

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


def _print_refused_gates(refused_gates: list[dict]) -> None:
    for gate in refused_gates:
        print(
            f"[activation] refused {gate['path']}: {gate.get('reason')}",
            file=sys.stderr,
        )
        owner = gate.get("owner")
        if owner:
            print(f"  owner: {json.dumps(owner, sort_keys=True)}", file=sys.stderr)


def main():
    args = parse_args()

    exact_requested = args.session is not None and not args.publish_session
    # GH-503: inbox consuming modes are mutations — they claim an activation lease
    # (gate_activation_message) and mark_messages_read(), consuming durable packets
    # and acquiring writer authority. Gate them on exact-current runtime; leave
    # --peek and --show-all (read-only diagnostics) ungated.
    if args.mark_all_read:
        require_current_runtime("inbox:mark-all-read")
    elif exact_requested and args.acknowledge:
        require_current_runtime("inbox:acknowledge")
    elif not exact_requested and not args.peek and not args.show_all:
        require_current_runtime("inbox:consume")

    if not exact_requested:
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
    if exact_requested:
        budget = ExactReadBudget(MAX_EXACT_SESSION_BYTES)
        try:
            with active_read_budget(budget):
                exact_session = exact_read_session(args, budget)
                messages, repo_scope_refused, refusal_only = select_exact_read_messages(
                    args, exact_session, budget
                )
        except (
            BindingUnreadable,
            FileNotFoundError,
            UnreadableFile,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            payload = {"messages": [], "exact_session_refused": str(error)}
            if args.json_output:
                print(json.dumps(payload, indent=2))
            else:
                print(
                    f"[inbox] Exact-session read refused: {error}",
                    file=sys.stderr,
                )
            sys.exit(75)
        # Refused packets stay excluded from messages and are reported below. A
        # superseded/foreign binding must never fall through to the current session;
        # skipping it here lets the routable packets through without weakening that
        # selection fence.
        if refusal_only:
            payload = {"messages": [], "repo_scope_refused": repo_scope_refused}
            if args.json_output:
                print(json.dumps(payload, indent=2))
            else:
                for refusal in repo_scope_refused:
                    print(
                        f"[inbox] Exact-session refused {refusal['path']}: "
                        f"{refusal['reason']}",
                        file=sys.stderr,
                    )
            sys.exit(75)
        if args.acknowledge:
            # Drain: mark exactly the packets this exact-session read selected. The
            # binding was already validated by exact_read_session, and the set is the
            # bounded exact-session unread — so a coalesced wake drains every packet
            # for this binding and a packet for another session was never in `messages`
            # to begin with. mark_messages_read is the same durable serialized inbox
            # write deliver.py uses; no second queue, no dispatch loop (which would
            # re-emit a wake).
            acknowledged = [message["path"] for message in messages]
            # Render the selected set BEFORE marking read, following inbox.py's
            # existing consume order: a render/output failure after the mark would
            # lose already-acknowledged packets. And a Pi worker draining on the
            # human path must SEE the bodies, not just a count, or the wake loses
            # the work while claiming acknowledgment.
            if args.json_output:
                drained_payload: dict[str, object] = {
                    "acknowledged": acknowledged,
                    "messages": messages,
                }
                if repo_scope_refused:
                    drained_payload["repo_scope_refused"] = repo_scope_refused
                print(json.dumps(drained_payload, indent=2, sort_keys=True))
            else:
                for index, message in enumerate(messages):
                    print(format_message(message, index))
                for refusal in repo_scope_refused:
                    print(
                        f"[inbox] Exact-session refused {refusal['path']}: "
                        f"{refusal['reason']}",
                        file=sys.stderr,
                    )
                print(f"[inbox] Acknowledged {len(acknowledged)} exact-session packet(s).")
            mark_messages_read(args.me, acknowledged)
            return
    if not exact_requested:
        try:
            if args.packet:
                messages = load_all_messages(args.me)
            elif args.show_all:
                messages = load_all_messages(args.me)
            else:
                messages = get_unread_messages(args.me)
        except InboxScanLimitExceeded as error:
            payload = {
                "error": "inbox_scan_limit_exceeded",
                "detail": str(error),
                "messages": [],
            }
            if args.json_output:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"[inbox] {error}", file=sys.stderr)
            sys.exit(75)
    messages = filter_messages(
        messages,
        None if exact_requested else args.project,
        None if exact_requested else args.chat,
        args.packet,
    )
    if args.packet and len(messages) != 1:
        packet_selection_error(args, messages)
    messages, filtered_repo_scope = filter_repo_scope(
        messages, args.repo_target, args.project
    )
    repo_scope_refused.extend(filtered_repo_scope)

    if not messages:
        if args.json_output:
            if not repo_scope_refused and published_runtime is None:
                print("[]")
                return
            payload = {"messages": []}
            if repo_scope_refused:
                payload["repo_scope_refused"] = repo_scope_refused
            if published_runtime is None:
                print(json.dumps(payload, indent=2))
            else:
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
    if not exact_requested:
        # Select up to --limit *valid* packets, scanning past refused activations
        # instead of letting a refused prefix consume the budget: with --limit N, N
        # refused entries ahead of valid mail must not hide it permanently — refused
        # packets stay unread, so every retry re-selects the same prefix (GH-502).
        # A refused packet is skipped (left unread) and surfaced in activation_refused.
        # --packet selects exactly one message and is never limited here.
        limit = None if args.packet else args.limit
        selected: list[dict] = []
        for message in messages:
            if limit is not None and len(selected) >= limit:
                break
            gate = gate_activation_message(args, message, consume=consume)
            if gate is not None:
                message["activation_gate"] = gate
                if consume and not gate.get("authorized"):
                    refused_gates.append({"path": message["path"], **gate})
                    continue
            selected.append(message)
        messages = selected

    # Only an all-refused scan (no valid packet anywhere to show or drain) keeps the
    # exit-75 failure; a mixed batch shows/drains the valid packets and exits 0.
    shown_paths = [m["path"] for m in messages if not m.get("read")]
    if refused_gates and not shown_paths:
        if args.json_output:
            print(
                json.dumps(
                    {"activation_refused": refused_gates, "messages": messages},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            _print_refused_gates(refused_gates)
        sys.exit(75)
    if consume and not exact_requested:
        shown_paths, late_refused = recheck_repo_scope_before_read(
            args.me, shown_paths, args.repo_target, args.project
        )
        repo_scope_refused.extend(late_refused)

    if args.json_output:
        payload: dict[str, object] = {"messages": messages}
        if refused_gates:
            payload["activation_refused"] = refused_gates
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
        if refused_gates:
            _print_refused_gates(refused_gates)
        for refused in repo_scope_refused:
            scope = "Exact-session" if exact_requested else "Repo-scope"
            print(
                f"[inbox] {scope} refused {refused['path']}: {refused['reason']}",
                file=sys.stderr,
            )

    if consume and not exact_requested:
        mark_messages_read(args.me, shown_paths)


if __name__ == "__main__":
    main()
