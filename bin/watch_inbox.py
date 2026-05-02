#!/usr/bin/env python3
"""
watch_inbox.py — Background inbox poller. Run via PM2 (see pm2_watchers.py).

Polls agents/{id}/inbox.json for new unread messages and optionally
sends a desktop notification (macOS, Linux notify-send, or no-op).

Usage:
  python bin/watch_inbox.py --me orchestrator
  python bin/watch_inbox.py --me orchestrator --poll-seconds 30 --notify
"""

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    agent_inbox_path,
    config_get,
    load_agent_inbox,
    mark_messages_read,
)
from _session_autobridge import SESSIONS_DIR, dispatch_session, load_session


def parse_args():
    p = argparse.ArgumentParser(description="Background inbox watcher.")
    p.add_argument("--me", required=True, help="Agent ID to watch for")
    p.add_argument("--poll-seconds", type=int, default=None, help="Poll interval (default: from config)")
    p.add_argument("--max-polls", type=int, default=0, help="Stop after N polls; 0 = forever")
    p.add_argument("--notify", action="store_true", help="Send desktop notification on new messages")
    p.add_argument("--no-autobridge", action="store_true", help="Disable automatic session autobridge dispatch on new unread messages")
    p.add_argument("--skip-existing", action="store_true", help="Treat current unread as already seen")
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON lines")
    return p.parse_args()


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


def autobridge_session_ids(agent_id: str) -> list[str]:
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
        session_ids.append(path.stem)
    return session_ids


def dispatch_autobridge(agent_id: str, json_output: bool) -> list[str]:
    consumed_paths: list[str] = []

    for session_id in autobridge_session_ids(agent_id):
        result = dispatch_session(session_id)
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
                for path in sorted(new_msgs):
                    ts_str = utc_now_str()
                    emit({"ts": ts_str, "event": "new_message", "detail": path, "agent": args.me}, args.json_output)
                    if args.notify:
                        send_notification(
                            f"llm-collab: {args.me}",
                            f"New message: {Path(path).stem}",
                        )
                if unread and not args.no_autobridge:
                    consumed_paths = sorted(set(dispatch_autobridge(args.me, args.json_output)))
                    if consumed_paths:
                        mark_messages_read(args.me, consumed_paths)
                seen_paths = seen_paths | new_msgs
        except Exception as e:
            ts_str = utc_now_str()
            emit({"ts": ts_str, "event": "error", "detail": str(e)}, args.json_output)

        polls += 1
        if args.max_polls and polls >= args.max_polls:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
