#!/usr/bin/env python3
"""
Legacy compatibility wrapper for old `.ai-collaboration/bin/watcher_ctl.py`.

Maps legacy commands to `pm2_watchers.py`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Legacy wrapper for pm2_watchers.py")
    parser.add_argument("command", choices=["start", "stop", "status", "restart"])
    parser.add_argument("--agent", default=None, help="Agent ID")
    parser.add_argument("--all", action="store_true", help="Apply to all watcher-enabled agents")
    parser.add_argument("--poll-seconds", type=int, default=None, help="Ignored legacy option")
    parser.add_argument("--notify", action="store_true", help="Ignored legacy option")
    parser.add_argument("--skip-existing", action="store_true", help="Ignored legacy option")
    parser.add_argument("--force", action="store_true", help="Ignored legacy option")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ignored = []
    if args.poll_seconds is not None:
        ignored.append("--poll-seconds")
    if args.notify:
        ignored.append("--notify")
    if args.skip_existing:
        ignored.append("--skip-existing")
    if args.force:
        ignored.append("--force")
    if ignored:
        print(
            f"[warn] Ignoring legacy options not supported by pm2_watchers: {', '.join(ignored)}",
            file=sys.stderr,
        )

    cmd = [sys.executable, str(Path(__file__).parent / "pm2_watchers.py"), args.command]
    if args.all:
        cmd.append("--all")
    elif args.agent:
        cmd.extend(["--agent", args.agent])
    else:
        raise SystemExit("[error] Provide --agent <id> or --all")

    print("[deprecated] watcher_ctl.py -> pm2_watchers.py compatibility mode", file=sys.stderr)
    result = subprocess.run(cmd, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

