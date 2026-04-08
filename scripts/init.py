#!/usr/bin/env python3
"""
scripts/init.py — Interactive workspace initialization.

Creates collab.config.json, agents.json, projects.json, and
generates agents/{id}/identity.md + agents/{id}/memory.md for each agent.

Run from the workspace root:
  python scripts/init.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def prompt(question: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"{question}{hint}: ").strip()
    return val if val else default


def prompt_list(question: str) -> list[str]:
    print(f"{question} (comma-separated, e.g. app,api or leave blank):")
    val = input("> ").strip()
    return [v.strip() for v in val.split(",") if v.strip()]


def yn(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    val = input(f"{question} {hint}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


def divider():
    print("\n" + "─" * 50)


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    print(f"  ✓ {path.relative_to(ROOT)}")


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  ✓ {path.relative_to(ROOT)}")


def build_identity_md(agent: dict, workspace_name: str, all_agent_ids: list[str], projects: list[dict]) -> str:
    aid = agent["id"]
    display = agent.get("display_name", aid)
    role = agent.get("role", "agent")
    activation = agent.get("activation", {})
    identity_note = activation.get("identity_note", "")
    base_model = activation.get("base_model", "")
    other_agents = [a for a in all_agent_ids if a != aid]
    project_list = ", ".join(p["id"] for p in projects) if projects else "(none)"

    lines = [
        f"# Identity: {display}",
        "",
    ]

    if identity_note:
        lines += [
            f"> **IMPORTANT**: {identity_note}",
            "",
        ]
    elif base_model:
        lines += [
            f"> You are **{display}**. You are NOT `{base_model}`.",
            f"> Do not read or respond to messages addressed to `{base_model}`.",
            "",
        ]

    lines += [
        f"## Role",
        f"",
        f"**Agent ID**: `{aid}`  ",
        f"**Role**: {role}  ",
        f"**Workspace**: {workspace_name}  ",
        "",
        "## Workspace",
        "",
        f"- Root: `{ROOT}`",
        f"- Your inbox: `{ROOT}/agents/{aid}/inbox.json`",
        f"- Your memory: `{ROOT}/agents/{aid}/memory.md`",
        "",
        "## Session Bootstrap",
        "",
        f"At the start of every session, run:",
        f"```",
        f"python {ROOT}/bin/session_bootstrap.py --agent {aid}",
        f"```",
        "",
        "## Key Commands",
        "",
        f"```bash",
        f"# Check inbox",
        f"python {ROOT}/bin/inbox.py --me {aid}",
        "",
        f"# Send message",
        f'python {ROOT}/bin/deliver.py --chat last --from {aid} --to <agent> --title "..."',
        "",
        f"# Create task",
        f'python {ROOT}/bin/new_task.py --title "..." --created-by {aid} --project <project_id>',
        "",
        f"# Task board",
        f"python {ROOT}/bin/task_board.py",
        f"```",
        "",
        f"## Active Projects",
        "",
        f"{project_list}",
        "",
        f"## Other Agents",
        "",
    ]

    for oid in other_agents:
        lines.append(f"- `{oid}`")

    lines += [
        "",
        "## Instructions",
        "",
        "- Always bootstrap your session before starting work.",
        "- Always check your inbox before starting new work.",
        "- When you complete a task, update its status: `claim_task.py --status done`",
        "- When you send a message to a human_relay agent, the system will print",
        "  a handoff prompt for the operator to relay to them.",
    ]

    return "\n".join(lines) + "\n"


def build_memory_md(agent: dict) -> str:
    aid = agent["id"]
    display = agent.get("display_name", aid)
    return f"# Memory: {display}\n\n_This file is maintained by `{aid}`. Add persistent notes here._\n\n## Notes\n\n- \n"


def collect_agents() -> list[dict]:
    print("\n[Agents]\n")
    print("Define the agents (LLM instances) that will collaborate.")
    print("You need at least: one human operator + one LLM agent.")
    print()

    agents = []

    while True:
        divider()
        print(f"Agent #{len(agents) + 1}")
        aid = prompt("  Agent ID (lowercase, e.g. orchestrator, claude, codex)").lower().replace(" ", "-")
        if not aid:
            if len(agents) == 0:
                print("[error] You must define at least one agent.")
                continue
            break

        display = prompt(f"  Display name", default=aid.capitalize())
        role = prompt("  Role (e.g. primary_orchestrator, implementation, research, human_dispatcher)", default="agent")

        print(f"  Activation type:")
        print(f"    1) cli_session    — LLM CLI always open, background watcher runs")
        print(f"    2) human_relay    — Human must paste a prompt to a new LLM session")
        print(f"    3) human          — Human operator (no LLM)")
        print(f"    4) api_trigger    — External webhook triggers the agent")
        atype_choice = prompt("  Choice", default="1")
        atype_map = {"1": "cli_session", "2": "human_relay", "3": "human", "4": "api_trigger"}
        atype = atype_map.get(atype_choice, "cli_session")

        activation: dict = {"type": atype}

        if atype == "cli_session":
            activation["watcher_enabled"] = yn("  Enable background inbox watcher?", default=True)
        elif atype == "human_relay":
            activation["watcher_enabled"] = False
            base_model = prompt("  Base model/CLI (e.g. codex, claude, gemini)", default="")
            if base_model:
                activation["base_model"] = base_model
            identity_note = prompt(
                f"  Identity note shown in handoff prompt (e.g. 'You are NOT codex')",
                default=f"You are {display}. Do not read messages addressed to other agents.",
            )
            activation["identity_note"] = identity_note
        elif atype == "human":
            activation["watcher_enabled"] = False

        notes = prompt("  Notes (optional)", default="")

        entry: dict = {
            "id": aid,
            "display_name": display,
            "role": role,
            "activation": activation,
        }
        if notes:
            entry["notes"] = notes

        agents.append(entry)
        print(f"  [added] {aid}")

        if not yn("\nAdd another agent?", default=True):
            break

    return agents


def collect_projects() -> list[dict]:
    print("\n[Projects]\n")
    print("Register the code projects this workspace will coordinate work on.")
    print("Paths can be relative to projects_root or absolute.")
    print()

    projects_root = config_get_local("projects_root", "")
    projects = []

    if not yn("Add projects now?", default=True):
        return projects

    while True:
        divider()
        print(f"Project #{len(projects) + 1}")
        pid = prompt("  Project ID (lowercase, e.g. my-app)").lower().replace(" ", "-")
        if not pid:
            break

        display = prompt("  Display name", default=pid.replace("-", " ").title())
        repos_raw = prompt_list("  Repo IDs and paths (e.g. app:../my-app  or  app)")

        repos = {}
        for r in repos_raw:
            if ":" in r:
                rid, _, rpath = r.partition(":")
                repos[rid.strip()] = rpath.strip()
            else:
                repos[r] = f"../{r}"

        if not repos:
            repos = {pid: f"../{pid}"}

        preflight = None
        if yn(f"  Does this project have a preflight/build check command?", default=False):
            cmd_str = prompt("  Command (e.g. pnpm preflight --json)", default="")
            if cmd_str:
                preflight = cmd_str.split()

        github_enabled = yn("  Enable GitHub integration for this project?", default=False)
        github: dict = {"enabled": github_enabled}
        if github_enabled:
            github["repo"] = prompt("  GitHub repo (owner/repo)")
            pn = prompt("  GitHub Project number (optional)", default="")
            if pn.isdigit():
                github["project_number"] = int(pn)

        projects.append({
            "id": pid,
            "display_name": display,
            "repos": repos,
            "default_branch_base": prompt("  Default branch base", default="main"),
            "preflight_command": preflight,
            "github": github,
        })
        print(f"  [added] {pid}")

        if not yn("\nAdd another project?", default=True):
            break

    return projects


_local_config: dict = {}


def config_get_local(key: str, default=""):
    return _local_config.get(key, default)


def main():
    global _local_config

    print("=" * 60)
    print("  llm-collab workspace init")
    print("=" * 60)
    print(f"\nWorkspace root: {ROOT}\n")

    config_file = ROOT / "collab.config.json"
    if config_file.exists():
        if not yn("collab.config.json already exists. Reinitialize?", default=False):
            print("Aborted.")
            sys.exit(0)

    print("\n[Workspace Config]\n")
    workspace_name = prompt(
        "Workspace name (used for PM2 app names, slugs)",
        default=ROOT.name.lower().replace("_", "-"),
    )
    projects_root = prompt(
        "Projects root path (directory containing your project repos)",
        default=str(Path.home() / "Projects"),
    )
    poll_interval = prompt("Inbox poll interval in seconds", default="15")
    notifications = yn("Enable desktop notifications?", default=True)

    _local_config = {
        "workspace_name": workspace_name,
        "projects_root": projects_root,
        "poll_interval_seconds": int(poll_interval),
        "notifications_enabled": notifications,
    }

    config = {
        "workspace_name": workspace_name,
        "schema_version": 2,
        "projects_root": projects_root,
        "default_tags": [],
        "branch_pattern": "collab/{agent}/{task_slug}",
        "poll_interval_seconds": int(poll_interval),
        "notifications_enabled": notifications,
        "notifications_platform": "auto",
    }

    agents = collect_agents()
    all_agent_ids = [a["id"] for a in agents]

    projects = collect_projects()

    divider()
    print("\n[Writing files]\n")

    write_json(config_file, config)
    write_json(ROOT / "agents.json", {"agents": agents})
    if projects:
        write_json(ROOT / "projects.json", {"projects": projects})

    for agent in agents:
        aid = agent["id"]
        atype = agent.get("activation", {}).get("type", "")
        if atype == "human":
            continue
        identity = build_identity_md(agent, workspace_name, all_agent_ids, projects)
        memory = build_memory_md(agent)
        write_file(ROOT / "agents" / aid / "identity.md", identity)
        write_file(ROOT / "agents" / aid / "memory.md", memory)
        # Create empty inbox
        inbox_path = ROOT / "agents" / aid / "inbox.json"
        if not inbox_path.exists():
            write_json(inbox_path, {"agent": aid, "unread": [], "read": []})

    divider()
    print("\n[Next steps]\n")
    print("1. Bootstrap each agent session:")
    for a in agents:
        if a.get("activation", {}).get("type") not in ("human",):
            print(f"   python bin/session_bootstrap.py --agent {a['id']}")
    print()
    print("2. Generate memory snippets for your LLM tools:")
    for a in agents:
        if a.get("activation", {}).get("type") not in ("human",):
            print(f"   python bin/init_agent_memory.py --agent {a['id']} --target generic")
    print()
    print("3. Start PM2 watchers (optional, requires pm2):")
    print("   python bin/pm2_watchers.py start --all")
    print()
    print("4. Create your first chat:")
    print('   python bin/new_chat.py --title "..." --project <project_id>')
    print()
    print("Done. See docs/getting-started.md for the full workflow.\n")


if __name__ == "__main__":
    main()
