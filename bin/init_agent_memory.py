#!/usr/bin/env python3
"""
init_agent_memory.py — Generate collab-awareness memory snippets for LLM tools.

Outputs a snippet that tells the LLM what this workspace is, who it is,
and how to use the collab system. Can optionally write the snippet directly
to a supported LLM's memory system.

Usage:
  python bin/init_agent_memory.py --agent orchestrator --target generic
  python bin/init_agent_memory.py --agent claude --target claude-code
  python bin/init_agent_memory.py --agent orchestrator --target codex
  python bin/init_agent_memory.py --agent claude --target claude-md --project-path /path/to/project
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    agent_identity_path,
    agent_memory_path,
    config_get,
    get_agent,
    load_projects,
    project_ids,
    utc_iso,
    write_file,
)

TARGETS = ("generic", "claude-code", "codex", "claude-md")


def parse_args():
    p = argparse.ArgumentParser(description="Generate collab memory snippets for LLMs.")
    p.add_argument("--agent", required=True, help="Agent ID to generate snippet for")
    p.add_argument("--target", required=True, choices=TARGETS, help="LLM memory target format")
    p.add_argument("--project-path", default=None, help="For claude-md: path to project CLAUDE.md")
    p.add_argument("--write", action="store_true", help="Write snippet directly (where supported)")
    return p.parse_args()


def build_snippet(agent_id: str) -> str:
    agent = get_agent(agent_id)
    workspace_name = config_get("workspace_name", "llm-collab")
    projects = load_projects()
    project_list = ", ".join(p["id"] for p in projects) if projects else "(none configured)"
    all_agents = agent_ids()
    other_agents = [a for a in all_agents if a != agent_id]

    lines = [
        "## Collaboration Workspace",
        "",
        f"You participate in a file-based multi-agent collaboration workspace.",
        f"Workspace: `{ROOT}`",
        f"Workspace name: `{workspace_name}`",
        "",
        f"**Your identity**: `{agent_id}` ({agent.get('display_name', agent_id)})",
        f"**Your inbox**: `{ROOT}/agents/{agent_id}/inbox.json`",
        f"**Your memory**: `{ROOT}/agents/{agent_id}/memory.md`",
        f"**Your identity file**: `{ROOT}/agents/{agent_id}/identity.md`",
        "",
        "**Quick commands:**",
        f"- Bootstrap session: `python {ROOT}/bin/session_bootstrap.py --agent {agent_id}`",
        f"- Read inbox: `python {ROOT}/bin/inbox.py --me {agent_id}`",
        f"- Send message: `python {ROOT}/bin/deliver.py --chat last --from {agent_id} --to <agent> --title \"...\"`",
        f"- Create task: `python {ROOT}/bin/new_task.py --title \"...\" --created-by {agent_id}`",
        f"- Task board: `python {ROOT}/bin/task_board.py`",
        "",
        f"**Active projects**: {project_list}",
        f"**Other agents**: {', '.join(other_agents) if other_agents else '(none)'}",
        "",
        "Always bootstrap your session at the start of each conversation.",
        "Always check your inbox before starting new work.",
    ]
    return "\n".join(lines)


def write_claude_code(agent_id: str, snippet: str, write: bool) -> None:
    workspace_slug = str(ROOT).replace("/", "-").lstrip("-")
    memory_dir = Path.home() / ".claude" / "projects" / workspace_slug / "memory"
    out_path = memory_dir / f"collab-{agent_id}.md"

    fm_header = f"---\nname: llm-collab identity ({agent_id})\ndescription: Collab workspace identity and commands for {agent_id}\ntype: user\n---\n\n"
    full_content = fm_header + snippet

    if write:
        write_file(out_path, full_content)
        print(f"[written] {out_path}")
    else:
        print(f"\n# Claude Code memory file")
        print(f"# Target path: {out_path}")
        print(f"# Run with --write to write automatically, or copy manually.\n")
        print(full_content)


def write_claude_md(agent_id: str, snippet: str, project_path: str | None, write: bool) -> None:
    section = f"\n\n## Collaboration System\n\n{snippet}\n"
    if project_path:
        claude_md = Path(project_path) / "CLAUDE.md"
        if write:
            if claude_md.exists():
                existing = claude_md.read_text()
                if "## Collaboration System" not in existing:
                    claude_md.write_text(existing + section)
                    print(f"[appended] {claude_md}")
                else:
                    print(f"[skip] CLAUDE.md already has Collaboration System section.")
            else:
                write_file(claude_md, section.strip())
                print(f"[written] {claude_md}")
        else:
            print(f"\n# Append to: {claude_md}\n")
            print(section)
    else:
        print("\n# Add this section to your project CLAUDE.md:")
        print(section)


def main():
    args = parse_args()

    if args.agent not in agent_ids():
        print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
        sys.exit(1)

    snippet = build_snippet(args.agent)

    if args.target == "generic":
        print(snippet)

    elif args.target == "claude-code":
        write_claude_code(args.agent, snippet, args.write)

    elif args.target == "codex":
        print("\n# Codex memory snippet")
        print("# Copy this into your Codex memory file for the workspace.\n")
        print(snippet)

    elif args.target == "claude-md":
        write_claude_md(args.agent, snippet, args.project_path, args.write)


if __name__ == "__main__":
    main()
