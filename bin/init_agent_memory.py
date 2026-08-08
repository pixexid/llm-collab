#!/usr/bin/env python3
"""
init_agent_memory.py — Generate collab-awareness memory snippets for LLM tools.

Outputs a snippet that tells the LLM what this workspace is, who it is,
and how to use the collab system. Can optionally write the snippet directly
to a supported LLM's memory system.

Usage:
  bin/llm-collab init_agent_memory.py --agent orchestrator --target generic
  bin/llm-collab init_agent_memory.py --agent claude --target claude-code
  bin/llm-collab init_agent_memory.py --agent orchestrator --target codex
  bin/llm-collab init_agent_memory.py --agent claude --target claude-md --project-path /path/to/project
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import re

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    collab_bootstrap_command,
    collab_join_skill_path,
    config_get,
    get_agent,
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


def build_snippet(agent_id: str, *, skill_capable: bool) -> str:
    agent = get_agent(agent_id)
    workspace_name = config_get("workspace_name", "llm-collab")
    join_skill = collab_join_skill_path()
    bootstrap_cmd = collab_bootstrap_command(agent_id)

    lines = [
        "## Collaboration Workspace",
        "",
        f"Workspace: `{ROOT}`",
        f"Workspace name: `{workspace_name}`",
        f"**Your identity**: `{agent_id}` ({agent.get('display_name', agent_id)})",
        "",
    ]
    if skill_capable:
        lines.extend(
            [
                f"Primary entry skill: `{join_skill}`",
                f"Read and follow that skill. It points to `{ROOT}/AGENTS.md` and the required workflow docs.",
            ]
        )
    else:
        lines.extend(
            [
                f"Primary join-skill pointer: `{join_skill}`",
                "This runtime may not support installable skills, so keep this file as a thin pointer to that skill.",
            ]
        )
    lines.extend(
        [
            f"Before the first watcher-enabled bootstrap: `{ROOT}/docs/workflows/pm2-log-rotation.md`",
            f"Bootstrap every session: `{bootstrap_cmd}`",
            "Keep this memory file as a pointer; do not restate collab command families here.",
        ]
    )
    return "\n".join(lines)


def write_claude_code(agent_id: str, snippet: str, write: bool) -> None:
    workspace_slug = str(ROOT).replace("/", "-").lstrip("-")
    memory_dir = Path.home() / ".claude" / "projects" / workspace_slug / "memory"
    out_path = memory_dir / f"collab-{agent_id}.md"

    fm_header = f"---\nname: llm-collab join pointer ({agent_id})\ndescription: Pointer to the in-repo join skill and bootstrap command for {agent_id}\ntype: user\n---\n\n"
    full_content = fm_header + snippet

    if write:
        write_file(out_path, full_content)
        print(f"[written] {out_path}")
    else:
        print(f"\n# Claude Code memory file")
        print(f"# Target path: {out_path}")
        print(f"# Run with --write to write automatically, or copy manually.\n")
        print(full_content)


def _render_claude_md_section(snippet: str) -> str:
    return f"## Collaboration System\n\n{snippet}\n\n"


def _replace_level2_section(content: str, heading: str, replacement: str) -> tuple[str, bool]:
    heading_pattern = re.compile(rf"(?m)^{re.escape(heading)}\s*$")
    match = heading_pattern.search(content)
    if match is None:
        return content, False
    next_heading = re.compile(r"(?m)^## ")
    following = next_heading.search(content, match.end())
    end = following.start() if following is not None else len(content)
    return content[:match.start()] + replacement + content[end:], True


def write_claude_md(agent_id: str, snippet: str, project_path: str | None, write: bool) -> None:
    section = _render_claude_md_section(snippet)
    if project_path:
        claude_md = Path(project_path) / "CLAUDE.md"
        if write:
            if claude_md.exists():
                existing = claude_md.read_text()
                updated, replaced = _replace_level2_section(
                    existing,
                    "## Collaboration System",
                    section,
                )
                if not replaced:
                    updated = existing + f"\n\n{section}"
                claude_md.write_text(updated)
                print(f"[written] {claude_md}")
            else:
                write_file(claude_md, section.strip())
                print(f"[written] {claude_md}")
        else:
            print(f"\n# Replace or append in: {claude_md}\n")
            print(section)
    else:
        print("\n# Add this section to your project CLAUDE.md:")
        print(section)


def main():
    args = parse_args()

    if args.agent not in agent_ids():
        print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
        sys.exit(1)

    if args.target == "generic":
        print(build_snippet(args.agent, skill_capable=False))

    elif args.target == "claude-code":
        write_claude_code(args.agent, build_snippet(args.agent, skill_capable=True), args.write)

    elif args.target == "codex":
        print("\n# Codex memory snippet")
        print("# Copy this thin pointer into your Codex memory file for the workspace.\n")
        print(build_snippet(args.agent, skill_capable=True))

    elif args.target == "claude-md":
        write_claude_md(args.agent, build_snippet(args.agent, skill_capable=False), args.project_path, args.write)


if __name__ == "__main__":
    main()
