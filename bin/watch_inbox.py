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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    agent_inbox_path,
    config_get,
    get_agent,
    load_agent_inbox,
)


def parse_args():
    p = argparse.ArgumentParser(description="Background inbox watcher.")
    p.add_argument("--me", required=True, help="Agent ID to watch for")
    p.add_argument("--poll-seconds", type=int, default=None, help="Poll interval (default: from config)")
    p.add_argument("--max-polls", type=int, default=0, help="Stop after N polls; 0 = forever")
    p.add_argument("--notify", action="store_true", help="Send desktop notification on new messages")
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
                if new_msgs:
                    for path in sorted(new_msgs):
                        ts_str = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds")
                        emit({"ts": ts_str, "event": "new_message", "detail": path, "agent": args.me}, args.json_output)
                        if args.notify:
                            send_notification(
                                f"llm-collab: {args.me}",
                                f"New message: {Path(path).stem}",
                            )
                    seen_paths = seen_paths | new_msgs
        except Exception as e:
            ts_str = __import__("datetime").datetime.utcnow().isoformat(timespec="seconds")
            emit({"ts": ts_str, "event": "error", "detail": str(e)}, args.json_output)

        polls += 1
        if args.max_polls and polls >= args.max_polls:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
