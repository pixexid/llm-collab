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
    SESSIONS_DIR,
    dispatch_session,
    load_session,
    repo_scope_matches,
)


def parse_args():
    p = argparse.ArgumentParser(description="Background inbox watcher.")
    p.add_argument("--me", required=True, help="Agent ID to watch for")
    p.add_argument("--project", default=None, help="Filter by exact project_id")
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


def autobridge_session_ids(agent_id: str, project_id: str | None = None) -> list[str]:
    if not SESSIONS_DIR.exists():
        return []

    session_ids: list[str] = []
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            session = load_session(path.stem)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if session.get("agent_id") != agent_id:
            continue
        if project_id is not None and session.get("project_id") != project_id:
            continue
        session_ids.append(path.stem)
    return session_ids


# How much of a failed turn's error the event carries. Truncation is always reported
# alongside it, never applied silently.
TURN_ERROR_LIMIT = 2000


def dispatch_autobridge(
    agent_id: str,
    json_output: bool,
    *,
    project_id: str | None = None,
    repo_targets: list[str] | None = None,
) -> list[str]:
    consumed_paths: list[str] = []

    for session_id in autobridge_session_ids(agent_id, project_id):
        result = dispatch_session(
            session_id, project_id=project_id, repo_targets=repo_targets
        )
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
            if action.get("effective_action") == "runtime_trigger" and runtime_ok:
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
                runtime_result = action.get("runtime_result") or {}
                # An accepted turn that failed is still delivered, so it is consumed and
                # never retried -- but branching on returncode alone dropped
                # turn_succeeded, terminal_status and stderr on the floor, and the packet
                # was marked read with no failure signal anywhere. At-most-once delivery
                # must not mean at-most-once VISIBILITY.
                if runtime_result.get("turn_succeeded") is False:
                    # The cap stays -- an unbounded error would swamp the log -- but a
                    # silent slice reads as the whole error. This event exists to make a
                    # failure visible, so hiding part of it inside the visibility fix
                    # defeats the fix, and "bounded work fails closed and never
                    # truncates" is this repo's rule besides.
                    raw_error = runtime_result.get("stderr") or ""
                    truncated = len(raw_error) > TURN_ERROR_LIMIT
                    emit(
                        {
                            "ts": utc_now_str(),
                            "event": "autobridge_turn_failed",
                            "detail": action["message_path"],
                            "agent": agent_id,
                            "session_id": session_id,
                            "message_path": action["message_path"],
                            "terminal_status": runtime_result.get("terminal_status"),
                            "delivery_observed": runtime_result.get("delivery_observed"),
                            "error": raw_error[:TURN_ERROR_LIMIT],
                            "error_truncated": truncated,
                            "error_length": len(raw_error),
                            "retried": False,
                        },
                        json_output,
                    )
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_consumed",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                        "terminal_status": runtime_result.get("terminal_status"),
                        "turn_succeeded": runtime_result.get("turn_succeeded"),
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

    known = agent_ids()
    if args.me not in known:
        print(f"[error] Unknown agent: {args.me!r}", file=sys.stderr)
        sys.exit(1)

    poll_interval = args.poll_seconds or config_get("poll_interval_seconds", 15)
    inbox_path = agent_inbox_path(args.me)

    seen_paths: set[str] = set()

    if args.skip_existing:
        if inbox_path.exists():
            data = load_agent_inbox(args.me)
            seen_paths = set(data.get("unread", []))

    polls = 0
    while True:
        try:
            if inbox_path.exists():
                data = load_agent_inbox(args.me)
                unread = set(data.get("unread", []))
                new_msgs = unread - seen_paths
                messages = {message["path"]: message for message in get_unread_messages(args.me)}
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
                # `seen_paths` records what has been ANNOUNCED, so it is committed before dispatch.
                # Committing it afterwards let a dispatch exception replay the announcement -- and
                # its desktop notification -- for every message in the poll, on every later poll.
                seen_paths = seen_paths | new_msgs
                if unread and not args.no_autobridge:
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

        polls += 1
        if args.max_polls and polls >= args.max_polls:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
