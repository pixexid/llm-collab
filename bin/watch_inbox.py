#!/usr/bin/env python3
"""
watch_inbox.py — Background inbox poller. Run via PM2 (see pm2_watchers.py).

Polls agents/{id}/inbox.json for new unread messages and optionally
sends a desktop notification (macOS, Linux notify-send, or no-op).

Usage:
  python bin/watch_inbox.py --me orchestrator
  python bin/watch_inbox.py --me orchestrator --poll-seconds 30 --notify
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import platform
import subprocess
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    agent_inbox_path,
    config_get,
    get_unread_messages,
    load_agent_inbox,
    mark_messages_read,
    parse_frontmatter,
)
from _session_autobridge import (
    CanonicalBindingNativeMismatch,
    active_read_budget,
    dispatch_session,
    iter_sessions,
    load_session,
    repo_scope_matches,
    resolve_active_canonical_binding,
    resolve_session_receive_binding,
    runtime_delivery_accepted,
    runtime_metadata,
)
from inbox import (
    MAX_EXACT_SESSION_BYTES,
    ExactReadBudget,
    exact_read_messages,
    exact_read_session,
)


def parse_args():
    p = argparse.ArgumentParser(description="Background inbox watcher.")
    p.add_argument("--me", required=True, help="Agent ID to watch for")
    p.add_argument("--project", default=None, help="Filter by exact project_id")
    p.add_argument("--chat", default=None, help="Filter by exact chat_id")
    p.add_argument(
        "--session",
        default=None,
        help="Watch one exact llm-collab session; requires --project and --chat",
    )
    p.add_argument("--poll-seconds", type=int, default=None, help="Poll interval (default: from config)")
    p.add_argument("--max-polls", type=int, default=0, help="Stop after N polls; 0 = forever")
    p.add_argument("--notify", action="store_true", help="Send desktop notification on new messages")
    p.add_argument("--no-autobridge", action="store_true", help="Disable automatic session autobridge dispatch on new unread messages")
    p.add_argument("--skip-existing", action="store_true", help="Treat current unread as already seen")
    p.add_argument(
        "--repo-target",
        action="append",
        default=None,
        help="Explicit repository subscription; repeat for multiple repositories",
    )
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON lines")
    args = p.parse_args()
    if args.repo_target is not None and args.project is None:
        p.error("--repo-target requires --project <id>")
    if args.chat is not None and args.session is None:
        p.error("--chat requires --session")
    if args.session is not None:
        if not args.session.strip() or args.session != args.session.strip():
            p.error("--session requires a non-empty session id")
        if not args.project or not args.chat:
            p.error("--session requires --project and --chat")
        args.packet = None
    return args


def send_notification(title: str, body: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        elif system == "Linux":
            subprocess.run(["notify-send", title, body], check=False, timeout=5)
        # Windows / other: no-op
    except Exception:
        pass


def emit(msg: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(msg), flush=True)
    else:
        ts = msg.get("ts", "")
        event = msg.get("event", "")
        detail = msg.get("detail", "")
        print(f"[{ts}] {event}: {detail}", flush=True)


def utc_now_str() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class ExactWatcherAuthorityError(RuntimeError):
    pass


def session_has_exact_canonical_binding(session: dict) -> bool:
    """Run the receive binding gate without mutating the session file.

    Existing file bindings retain the watcher-local exact check. Otherwise
    ``dispatch_session`` derives an exact native-session ledger binding in
    memory when one exists, while target matching rejects targeted packets when
    it does not.
    """
    if not session.get("binding_id"):
        eligible, _binding = resolve_session_receive_binding(session)
        return eligible
    runtime = runtime_metadata(session)
    runtime_session_id = runtime.get("session_id")
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    agent_id = session.get("agent_id")
    if not (runtime_session_id and project_id and chat_id and agent_id):
        return False
    try:
        canonical = resolve_active_canonical_binding(
            str(project_id), str(chat_id), str(agent_id), str(runtime_session_id)
        )
    except CanonicalBindingNativeMismatch:
        return False
    if canonical is None:
        return False
    return (
        canonical.get("binding_id") == session.get("binding_id")
        and canonical.get("binding_generation") == session.get("binding_generation")
    )


def exact_session_messages(args) -> list[dict]:
    budget = ExactReadBudget(MAX_EXACT_SESSION_BYTES)
    with active_read_budget(budget):
        try:
            session = exact_read_session(args, budget)
        except Exception as error:
            raise ExactWatcherAuthorityError(str(error)) from error
        messages, refusals = exact_read_messages(args, session, budget)
        fatal_refusals = [
            refusal for refusal in refusals if not refusal.get("repo_scope_only")
        ]
        if fatal_refusals:
            raise ExactWatcherAuthorityError(
                "exact_session_repo_scope_refused: "
                f"{json.dumps(fatal_refusals, sort_keys=True)}"
            )
    return messages


def autobridge_session_ids(agent_id: str, project_id: str | None = None) -> list[str]:
    session_ids: list[str] = []
    for session in iter_sessions(agent_id=agent_id):
        if project_id is not None and session.get("project_id") != project_id:
            continue
        if session.get("session_id"):
            session_ids.append(str(session["session_id"]))
    return session_ids


def dispatch_autobridge(
    agent_id: str,
    json_output: bool,
    *,
    project_id: str | None = None,
    repo_targets: list[str] | None = None,
) -> list[str]:
    consumed_paths: list[str] = []

    for session_id in autobridge_session_ids(agent_id, project_id):
        # Canonical binding gate (#95): resolve the session's exact binding from
        # the ledger store BEFORE dispatch. A stale or foreign session (its
        # binding_id/generation differ from the canonical active one) never
        # reaches packet match, materialization, claim, runtime write, or
        # mark_messages_read. No active binding -> fail closed for that session.
        try:
            session = load_session(session_id)
        except Exception as error:
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_dispatch_error",
                    "detail": session_id,
                    "agent": agent_id,
                    "session_id": session_id,
                    "reason": f"{type(error).__name__}: {error}",
                },
                json_output,
            )
            continue
        if not session_has_exact_canonical_binding(session):
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_binding_refused",
                    "detail": session_id,
                    "agent": agent_id,
                    "session_id": session_id,
                    "reason": "stale_or_foreign_canonical_binding",
                },
                json_output,
            )
            continue
        try:
            result = dispatch_session(
                session_id, project_id=project_id, repo_targets=repo_targets
            )
        except Exception as error:
            # Isolate per session: one session's failure (e.g. the save_session
            # resurrection guard racing a deactivation, now that #378 stops sessions on
            # every Pi lifecycle event) must not abort the rest of the cycle. Surface it
            # and move on. The watcher top loop already isolates whole cycles; this
            # isolates siblings within a cycle.
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_dispatch_error",
                    "detail": session_id,
                    "agent": agent_id,
                    "session_id": session_id,
                    "reason": f"{type(error).__name__}: {error}",
                },
                json_output,
            )
            continue
        for refusal in result.get("repo_scope_refused", []):
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_repo_scope_refused",
                    "detail": refusal["path"],
                    "agent": agent_id,
                    "session_id": session_id,
                    "message_path": refusal["path"],
                    "reason": refusal["reason"],
                },
                json_output,
            )
        if not result.get("actions"):
            continue

        emit(
            {
                "ts": utc_now_str(),
                "event": "autobridge_dispatch",
                "detail": session_id,
                "agent": agent_id,
                "session_id": session_id,
                "matched_messages": result.get("matched_messages", 0),
            },
            json_output,
        )

        for action in result["actions"]:
            runtime_result = action.get("runtime_result") or {}
            runtime_ok = runtime_result.get("returncode") == 0
            if (
                action.get("effective_action") == "runtime_trigger"
                and runtime_delivery_accepted(runtime_result)
            ):
                # Stored session scope was already checked by dispatch_session;
                # an explicit watcher scope is rechecked at the read boundary.
                effective_repo_targets = repo_targets
                message_path = ROOT / action["message_path"]
                if effective_repo_targets is None:
                    repo_match, repo_reason = True, "unscoped"
                else:
                    try:
                        frontmatter, _ = parse_frontmatter(message_path.read_text())
                        repo_match, repo_reason = repo_scope_matches(
                            effective_repo_targets,
                            frontmatter.get("repo_targets"),
                            subscriber_project=project_id,
                            packet_project=frontmatter.get("project_id"),
                        )
                    except Exception:
                        repo_match, repo_reason = False, "route_ambiguous"
                if not repo_match:
                    emit(
                        {
                            "ts": utc_now_str(),
                            "event": "autobridge_repo_scope_refused",
                            "detail": action["message_path"],
                            "agent": agent_id,
                            "session_id": session_id,
                            "message_path": action["message_path"],
                            "reason": repo_reason,
                        },
                        json_output,
                    )
                    continue
                consumed_paths.append(action["message_path"])
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_consumed",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                    },
                    json_output,
                )
            elif (
                action.get("effective_action") == "runtime_trigger"
                and runtime_ok
                and not runtime_result.get("skipped")
            ):
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_wake_signaled",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                    },
                    json_output,
                )
            elif action.get("effective_action") == "runtime_trigger":
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_failed",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                        "returncode": runtime_result.get("returncode"),
                    },
                    json_output,
                )
        if consumed_paths:
            mark_messages_read(agent_id, sorted(set(consumed_paths)))

    return consumed_paths


def main():
    args = parse_args()

    if not args.session:
        known = agent_ids()
        if args.me not in known:
            print(f"[error] Unknown agent: {args.me!r}", file=sys.stderr)
            sys.exit(1)

    poll_interval = args.poll_seconds or config_get("poll_interval_seconds", 15)
    inbox_path = agent_inbox_path(args.me)

    seen_paths: set[str] = set()

    if args.skip_existing:
        if args.session:
            while True:
                try:
                    seen_paths = {
                        message["path"] for message in exact_session_messages(args)
                    }
                    break
                except Exception as error:
                    emit(
                        {
                            "ts": utc_now_str(),
                            "event": "error",
                            "detail": str(error),
                        },
                        args.json_output,
                    )
                    if isinstance(error, ExactWatcherAuthorityError):
                        sys.exit(75)
                    time.sleep(poll_interval)
        elif inbox_path.exists():
            data = load_agent_inbox(args.me)
            seen_paths = set(data.get("unread", []))

    polls = 0
    while True:
        try:
            if args.session:
                exact_messages = exact_session_messages(args)
                unread = {message["path"] for message in exact_messages}
                messages = {
                    message["path"]: message for message in exact_messages
                }
            elif inbox_path.exists():
                data = load_agent_inbox(args.me)
                unread = set(data.get("unread", []))
                messages = {message["path"]: message for message in get_unread_messages(args.me)}
            else:
                unread = set()
                messages = {}
            if unread:
                new_msgs = unread - seen_paths
                visible_new_msgs: list[str] = []
                for path in sorted(new_msgs):
                    message = messages.get(path, {"frontmatter": {}})
                    frontmatter = message.get("frontmatter", {})
                    if args.project is not None and frontmatter.get("project_id") != args.project:
                        continue
                    repo_match, repo_reason = repo_scope_matches(
                        args.repo_target,
                        frontmatter.get("repo_targets"),
                        subscriber_project=args.project,
                        packet_project=frontmatter.get("project_id"),
                    )
                    if not repo_match:
                        emit(
                            {
                                "ts": utc_now_str(),
                                "event": "repo_scope_refused",
                                "detail": path,
                                "agent": args.me,
                                "message_path": path,
                                "reason": repo_reason,
                            },
                            args.json_output,
                        )
                        continue
                    visible_new_msgs.append(path)
                for path in visible_new_msgs:
                    ts_str = utc_now_str()
                    emit({"ts": ts_str, "event": "new_message", "detail": path, "agent": args.me}, args.json_output)
                    if args.notify:
                        send_notification(
                            f"llm-collab: {args.me}",
                            f"New message: {Path(path).stem}",
                        )
                # seen_paths records what has been ANNOUNCED, committed BEFORE
                # dispatch. If a dispatch below raises, the except unwinds past
                # this point; committing after dispatch left every message in the
                # poll eligible for re-announcement and re-dispatch on the next
                # poll (GH-94: the observed 3x duplicate delivery to Codex).
                seen_paths = seen_paths | new_msgs
                if not args.session and not args.no_autobridge:
                    consumed_paths = sorted(
                        set(
                            dispatch_autobridge(
                                args.me,
                                args.json_output,
                                project_id=args.project,
                                repo_targets=args.repo_target,
                            )
                        )
                    )
        except Exception as e:
            ts_str = utc_now_str()
            emit({"ts": ts_str, "event": "error", "detail": str(e)}, args.json_output)
            if args.session and isinstance(e, ExactWatcherAuthorityError):
                sys.exit(75)

        polls += 1
        if args.max_polls and polls >= args.max_polls:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
