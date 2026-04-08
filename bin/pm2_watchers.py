#!/usr/bin/env python3
"""
pm2_watchers.py — Manage PM2-based persistent inbox watchers.

PM2 app names use the pattern: {workspace_name}-{agent_id}
(workspace_name from collab.config.json)

Usage:
  python bin/pm2_watchers.py start --agent orchestrator
  python bin/pm2_watchers.py ensure --agent orchestrator   # start if not running
  python bin/pm2_watchers.py status --all
  python bin/pm2_watchers.py stop --agent orchestrator
  python bin/pm2_watchers.py logs --agent orchestrator --lines 50
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    config_get,
    get_agent,
    is_human_relay,
    load_agents,
    watcher_enabled_agents,
)

COMMANDS = ("start", "restart", "ensure", "stop", "delete", "status", "logs")


def parse_args():
    p = argparse.ArgumentParser(description="Manage PM2 inbox watchers.")
    p.add_argument("command", choices=COMMANDS)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--agent", help="Agent ID")
    g.add_argument("--all", action="store_true", help="Apply to all watcher-enabled agents")
    p.add_argument("--lines", type=int, default=40, help="Lines for logs command")
    return p.parse_args()


def resolve_pm2() -> str | None:
    return shutil.which("pm2")


def app_name(agent_id: str) -> str:
    workspace = config_get("workspace_name", "collab")
    return f"{workspace}-{agent_id}"


def pm2_run(args_list: list[str]) -> subprocess.CompletedProcess:
    pm2 = resolve_pm2()
    if not pm2:
        print("[error] pm2 not found. Install: npm install -g pm2", file=sys.stderr)
        sys.exit(1)
    return subprocess.run([pm2] + args_list, text=True)


def ecosystem_path() -> Path:
    return ROOT / "pm2" / "ecosystem.config.cjs"


def start_agent(agent_id: str) -> None:
    agent = get_agent(agent_id)
    if is_human_relay(agent):
        print(f"[skip] {agent_id} is human_relay — no watcher needed.")
        return
    if not agent.get("activation", {}).get("watcher_enabled", False):
        print(f"[skip] {agent_id} has watcher_enabled: false")
        return
    pm2_run(["start", str(ecosystem_path()), "--only", app_name(agent_id)])


def ensure_agent(agent_id: str) -> None:
    pm2 = resolve_pm2()
    if not pm2:
        print("[error] pm2 not found.", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run([pm2, "describe", app_name(agent_id)], capture_output=True, text=True)
    if "online" in result.stdout.lower():
        print(f"[watcher] {agent_id} already running.")
    else:
        start_agent(agent_id)


def main():
    args = parse_args()

    targets: list[str] = []
    if args.all:
        targets = [a["id"] for a in watcher_enabled_agents()]
    elif args.agent:
        if args.agent not in agent_ids():
            print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
            sys.exit(1)
        targets = [args.agent]
    else:
        print("[error] Specify --agent or --all", file=sys.stderr)
        sys.exit(1)

    for agent_id in targets:
        name = app_name(agent_id)
        if args.command == "start":
            start_agent(agent_id)
        elif args.command == "restart":
            pm2_run(["restart", name])
        elif args.command == "ensure":
            ensure_agent(agent_id)
        elif args.command == "stop":
            pm2_run(["stop", name])
        elif args.command == "delete":
            pm2_run(["delete", name])
        elif args.command == "status":
            pm2_run(["describe", name])
        elif args.command == "logs":
            pm2_run(["logs", name, "--lines", str(args.lines)])


if __name__ == "__main__":
    main()
