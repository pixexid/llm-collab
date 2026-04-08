#!/usr/bin/env python3
"""
session_bootstrap.py — Initialize an agent session.

Outputs the agent's identity.md FIRST so the LLM immediately knows
who it is, then shows inbox, then starts the watcher if applicable.

Usage:
  python bin/session_bootstrap.py --agent orchestrator
  python bin/session_bootstrap.py --agent worker --limit 5
  python bin/session_bootstrap.py --agent orchestrator --no-watcher
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    agent_identity_path,
    get_agent,
    get_unread_messages,
    is_human_relay,
    utc_iso,
    watcher_enabled_agents,
)


def parse_args():
    p = argparse.ArgumentParser(description="Bootstrap an agent session.")
    p.add_argument("--agent", required=True, help="Your agent ID")
    p.add_argument("--limit", type=int, default=5, help="Inbox items to show (default: 5)")
    p.add_argument("--no-watcher", action="store_true", help="Skip starting the inbox watcher")
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON summary")
    return p.parse_args()


def start_watcher(agent_id: str) -> dict:
    watcher_script = ROOT / "bin" / "pm2_watchers.py"
    if not watcher_script.exists():
        return {"status": "skipped", "reason": "pm2_watchers.py not found"}
    try:
        result = subprocess.run(
            [sys.executable, str(watcher_script), "ensure", "--agent", agent_id],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return {"status": "ok"}
        return {"status": "error", "stderr": result.stderr.strip()}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def main():
    args = parse_args()

    known = agent_ids()
    if args.agent not in known:
        print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
        print(f"       Known agents: {', '.join(known)}", file=sys.stderr)
        sys.exit(1)

    agent = get_agent(args.agent)

    # ── 1. Identity (FIRST — the LLM must read this before anything else) ──
    identity_file = agent_identity_path(args.agent)
    if identity_file.exists():
        identity_content = identity_file.read_text().strip()
        if not args.json_output:
            print("\n" + "═" * 60)
            print("IDENTITY")
            print("═" * 60)
            print(identity_content)
            print("═" * 60 + "\n")
    else:
        if not args.json_output:
            print(f"[warn] No identity file at {identity_file}", file=sys.stderr)
            print(f"       Run: python scripts/init.py to generate identity files.\n", file=sys.stderr)
        identity_content = None

    # ── 2. Inbox ──
    messages = get_unread_messages(args.agent)[: args.limit]
    inbox_summary = {
        "unread_count": len(messages),
        "messages": [
            {
                "path": m["path"],
                "from": m["frontmatter"].get("from"),
                "title": m["frontmatter"].get("title"),
                "priority": m["frontmatter"].get("priority"),
                "project_id": m["frontmatter"].get("project_id"),
            }
            for m in messages
        ],
    }

    if not args.json_output:
        if messages:
            print(f"[inbox] {len(messages)} unread message(s):\n")
            for i, m in enumerate(messages, 1):
                fm = m["frontmatter"]
                proj = f"  [{fm['project_id']}]" if fm.get("project_id") else ""
                print(f"  {i}. [{fm.get('priority','normal').upper()}]{proj} {fm.get('title','(no title)')} (from: {fm.get('from','?')})")
            print(f"\nRun: python bin/inbox.py --me {args.agent}   to read messages\n")
        else:
            print(f"[inbox] No unread messages for {args.agent}.\n")

    # ── 3. Watcher ──
    watcher_result = {"status": "skipped"}
    activation = agent.get("activation", {})
    should_start_watcher = (
        activation.get("watcher_enabled", False)
        and not args.no_watcher
        and not is_human_relay(agent)
    )

    if should_start_watcher:
        watcher_result = start_watcher(args.agent)
        if not args.json_output:
            status = watcher_result.get("status", "?")
            print(f"[watcher] {status}")

    if args.json_output:
        print(json.dumps({
            "agent": args.agent,
            "bootstrapped_utc": utc_iso(),
            "identity_loaded": identity_content is not None,
            "inbox": inbox_summary,
            "watcher": watcher_result,
        }, indent=2))


if __name__ == "__main__":
    main()
