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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import os
import shutil
import subprocess

sys.path.insert(0, str(Path(__file__).parent))
from _ax_trust import format_ax_status, probe_ax_trust
from _helpers import ROOT, agent_ids, config_get, get_agent, watcher_enabled_agents

COMMANDS = ("start", "restart", "ensure", "stop", "delete", "status", "logs")
DEFAULT_PM2_TIMEOUT_SECONDS = 15


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


def pm2_timeout_seconds() -> int:
    raw_timeout = os.environ.get("LLM_COLLAB_PM2_TIMEOUT_SECONDS")
    if not raw_timeout:
        return DEFAULT_PM2_TIMEOUT_SECONDS
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError:
        print(
            f"[warn] Invalid LLM_COLLAB_PM2_TIMEOUT_SECONDS={raw_timeout!r}; "
            f"using {DEFAULT_PM2_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return DEFAULT_PM2_TIMEOUT_SECONDS
    return max(1, timeout_seconds)


def pm2_run(args_list: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess:
    pm2 = resolve_pm2()
    if not pm2:
        print("[error] pm2 not found. Install: npm install -g pm2", file=sys.stderr)
        sys.exit(1)
    timeout_seconds = pm2_timeout_seconds()
    try:
        return subprocess.run(
            [pm2] + args_list,
            text=True,
            capture_output=capture_output,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[error] pm2 {' '.join(args_list)} timed out after {timeout_seconds}s",
            file=sys.stderr,
        )
        sys.exit(124)


def ecosystem_path() -> Path:
    return ROOT / "pm2" / "ecosystem.config.cjs"


def start_agent(agent_id: str) -> None:
    agent = get_agent(agent_id)
    if not agent.get("activation", {}).get("watcher_enabled", False):
        print(f"[skip] {agent_id} has watcher_enabled: false")
        return
    pm2_run(["start", str(ecosystem_path()), "--only", app_name(agent_id)])


def ensure_agent(agent_id: str) -> None:
    result = pm2_run(["describe", app_name(agent_id)], capture_output=True)
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

    if args.command == "status":
        # Print every target's AX state before invoking PM2. pm2_run exits on a
        # missing binary or timeout, but neither failure may suppress AX status.
        for agent_id in targets:
            print(format_ax_status(probe_ax_trust(get_agent(agent_id)), agent_id=agent_id))

    status_exit_code = 0
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
            result = pm2_run(["describe", name])
            if result.returncode != 0 and status_exit_code == 0:
                status_exit_code = result.returncode
        elif args.command == "logs":
            pm2_run(["logs", name, "--lines", str(args.lines), "--nostream"])

    if status_exit_code != 0:
        sys.exit(status_exit_code)


if __name__ == "__main__":
    main()
