#!/usr/bin/env python3
"""
new_collab_session.py — Set up a new collaboration session deterministically.

"New collab session" means a fresh session for EVERY worker involved, not the
reuse of whatever binding happened to be lying around. This helper does the part
the initiator can do safely on its own, and hands every co-worker an exact
copy-paste setup prompt for the part only they can do — registering their OWN
native session. Nobody's native session id is ever guessed.

It:
  1. Refuses if this checkout is behind origin/main (co-workers must run current
     code, not a parked/dirty operator checkout).
  2. Creates the chat.
  3. Registers ONLY the initiator's own, explicitly-supplied native session.
  4. Prints the initiator's own pickup command, branched by its wake channel
     (a watcher-backed initiator arms an inbox watcher; Codex, with no native
     watcher, gets poll/AX guidance) — do it, a packet you never see is a packet
     you never answer.
  5. Emits a per-co-worker setup prompt: the exact `session_autobridge register`
     plus the pickup command for that worker's real wake channel (watcher-backed
     workers watch; Codex has no native watcher and is woken by the sender's AX
     doorbell, so it polls).

Usage:
  python bin/new_collab_session.py \
    --project llm-collab --title "Paseo Phase-1 conformance" \
    --me claude --my-runtime-session-id <native-id> --my-runtime-family claude_app \
    --with codex --repo-target app
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import subprocess

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import ROOT, ensure_project, load_agents

BIN = Path(__file__).parent
DEFAULT_HOMES = {
    "claude_app": "~/.claude",
    "codex_app": "~/.codex",
    "gemini_cli": "~/.gemini",
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def assert_current_checkout() -> None:
    """Fail closed if this checkout is not exact origin/main. A co-worker
    pointed at a stale checkout runs outdated code — the exact trap that made a
    'new' session pick up 100+ commits of old behaviour."""
    try:
        _git("fetch", "origin", "main", "--quiet")
        origin_main = _git("rev-parse", "origin/main")
        head = _git("rev-parse", "HEAD")
        dirty = _git("status", "--porcelain")
    except subprocess.CalledProcessError as exc:  # pragma: no cover - env-dependent
        sys.exit(f"[error] could not compare against origin/main: {exc.stderr or exc}")
    if head != origin_main:
        behind = _git("rev-list", "--count", f"HEAD..{origin_main}") or "?"
        sys.exit(
            "[error] this checkout is not origin/main "
            f"(HEAD={head[:8]} origin/main={origin_main[:8]}, {behind} behind).\n"
            "        Run the helper from the deployed runtime or a fresh "
            "origin/main worktree, never a parked/dirty operator checkout."
        )
    # Same HEAD is not enough: a tracked edit or untracked replacement means the
    # tree is not the verified origin/main code, which is exactly the "dirty
    # operator checkout" the docstring/quickstart promise never to use.
    if dirty:
        sys.exit(
            "[error] working tree is dirty at origin/main "
            f"({len(dirty.splitlines())} change(s)); the new session must run "
            "the verified origin/main tree.\n        Use the deployed runtime or "
            "a clean fresh worktree, never a parked/dirty operator checkout."
        )


def agent_activation(agents: dict, agent_id: str) -> dict:
    for a in agents:
        if a.get("id") == agent_id:
            return a.get("activation", {}) or {}
    sys.exit(f"[error] agent '{agent_id}' is not in agents.json")


def wake_channel(activation: dict) -> str:
    """How this worker actually receives a packet. Keyed off real capability,
    not the aspirational `watcher_enabled` flag (Codex carries both an ax_app
    and watcher_enabled=True, but has no native session watcher — the ax_app
    wins)."""
    if activation.get("ax_attended_only"):
        return "ax_attended"
    if activation.get("ax_app"):
        return "ax_doorbell"
    if activation.get("type") == "cli_session" and activation.get("watcher_enabled"):
        return "watcher"
    if activation.get("type") == "human_relay":
        return "relay"
    return "unknown"


def register_session(session, agent, project, chat, repo_target, family, rsid, home) -> None:
    cmd = [
        sys.executable, str(BIN / "session_autobridge.py"), "register",
        "--session", session, "--agent", agent, "--project", project,
        "--chat", chat, "--repo-target", repo_target,
        "--mode", "auto-read", "--status", "active",
        "--wake-strategy", "runtime_trigger",
        "--runtime-family", family, "--runtime-session-id", rsid,
        "--runtime-home", home, "--runtime-session-source", "first_read",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[error] registering {agent} session failed:\n{r.stderr or r.stdout}")


def watch_cmd(agent, project, chat, session, repo_target, rsid) -> str:
    return (
        f"export LLM_COLLAB_READER_RUNTIME_ID={rsid}\n"
        f"<runtime_root>/bin/llm-collab watch_inbox.py \\\n"
        f"  --me {agent} --project {project} --chat {chat} \\\n"
        f"  --session {session} --repo-target {repo_target} --skip-existing --json"
    )


def pickup_block(channel, agent, project, chat, session, repo_target, rsid) -> list[str]:
    """How THIS agent picks up packets, keyed to its real wake channel. A
    persistent native watcher is printed only for watcher-backed workers; Codex
    has no native session watcher, so it gets polling/AX guidance instead of a
    watcher it cannot run."""
    if channel == "watcher":
        return [
            "# Arm your own inbox watcher in a persistent Monitor:",
            watch_cmd(agent, project, chat, session, repo_target, rsid),
        ]
    if channel == "ax_doorbell":
        return [
            "# You have NO native session watcher. A bound session receives",
            "# autobridge dispatch; between turns, poll your inbox — senders wake",
            "# you with the AX doorbell deliver.py prints when you are unbound:",
            f"python bin/inbox.py --me {agent} --project {project} --chat {chat} \\",
            f"  --session {session} --repo-target {repo_target} --peek --limit 5",
        ]
    return [
        f"# Wake channel '{channel}': pickup follows this agent's activation",
        "# contract (see docs/workflows/session-autobridge-runbook.md).",
    ]


def coworker_prompt(agent, channel, project, chat, repo_target, family) -> str:
    session = f"SESSION-{agent.upper()}-{chat.split('-')[-1]}"
    home = DEFAULT_HOMES.get(family, f"~/.{agent}")
    lines = [
        f"# Setup prompt for {agent} — new collab session on {chat}",
        f"# Project {project}, repo-target {repo_target}. Run from the deployed",
        f"# runtime (~/.local/share/llm-collab/runtime/main), never a parked checkout.",
        "",
        "# 1. Read your OWN native session id (verify it is the thread you are in):",
        f"python bin/session_autobridge.py discover-runtime --runtime-family {family} --json",
        "",
        "# 2. Register THAT id (never guess someone else's):",
        f"python bin/session_autobridge.py register \\",
        f"  --session {session} --agent {agent} \\",
        f"  --project {project} --chat {chat} --repo-target {repo_target} \\",
        f"  --mode auto-read --status active --wake-strategy runtime_trigger \\",
        f"  --runtime-family {family} --runtime-session-id <YOUR_ID> \\",
        f"  --runtime-home {home} --runtime-session-source first_read",
        "",
        "# 3. Pick up packets on YOUR wake channel:",
    ]
    lines += pickup_block(channel, agent, project, chat, session, repo_target, "<YOUR_ID>")
    return "\n".join(lines)


def parse_args():
    p = argparse.ArgumentParser(description="Set up a new collaboration session.")
    p.add_argument("--project", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--me", required=True, help="initiator agent id")
    p.add_argument("--my-runtime-session-id", required=True,
                   help="the initiator's OWN native session id (read it, do not guess)")
    p.add_argument("--my-runtime-family", required=True,
                   choices=("codex_app", "claude_app", "gemini_cli"))
    p.add_argument("--my-runtime-home", default=None,
                   help="defaults per family (~/.claude, ~/.codex, ~/.gemini)")
    p.add_argument("--with", dest="coworkers", required=True,
                   help="comma-separated co-worker agent ids")
    p.add_argument("--repo-target", required=True)
    p.add_argument("--skip-currency-check", action="store_true",
                   help="skip the origin/main guard (tests only)")
    return p.parse_args()


def main():
    args = parse_args()
    ensure_project(args.project, allow_none=False)
    if not args.skip_currency_check:
        assert_current_checkout()

    agents = load_agents()
    coworkers = [c.strip() for c in args.coworkers.split(",") if c.strip()]
    # Validate EVERY identity before any side effect. The initiator was
    # previously validated only implicitly by register_session, which runs after
    # new_chat.py — an unknown --me left an orphan chat behind. Fail closed here.
    for agent_id in [args.me, *coworkers]:
        agent_activation(agents, agent_id)  # existence check, fail closed

    created = subprocess.run(
        [sys.executable, str(BIN / "new_chat.py"),
         "--project", args.project, "--title", args.title],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        sys.exit(f"[error] creating chat failed:\n{created.stderr or created.stdout}")
    chat = json.loads(created.stdout)["chat_id"]
    suffix = chat.split("-")[-1]

    my_session = f"SESSION-{args.me.upper()}-{suffix}"
    my_home = args.my_runtime_home or DEFAULT_HOMES.get(args.my_runtime_family, f"~/.{args.me}")
    register_session(my_session, args.me, args.project, chat, args.repo_target,
                     args.my_runtime_family, args.my_runtime_session_id, my_home)

    print(f"chat_id: {chat}")
    print(f"initiator session: {my_session} ({args.me} / {args.my_runtime_session_id})")
    my_channel = wake_channel(agent_activation(agents, args.me))
    print("\n=== YOUR OWN PICKUP (do this now) ===")
    print("\n".join(pickup_block(my_channel, args.me, args.project, chat,
                                 my_session, args.repo_target, args.my_runtime_session_id)))
    for c in coworkers:
        channel = wake_channel(agent_activation(agents, c))
        family = {"codex": "codex_app", "gemini": "gemini_cli"}.get(c, "claude_app")
        print(f"\n=== SETUP PROMPT — share with {c} ===")
        print(coworker_prompt(c, channel, args.project, chat, args.repo_target, family))


if __name__ == "__main__":
    main()
