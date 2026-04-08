#!/usr/bin/env python3
"""
worktree_ctl.py — Create and manage isolated git worktrees per agent per task.

Each agent gets a dedicated branch and worktree for a task, preventing
merge conflicts when multiple agents work on the same repository.

Worktree metadata is stored in State/worktrees.json.

Branch naming uses the pattern from collab.config.json:
  branch_pattern: "{orchestrator}/{agent}/{task_slug}"
  (default: "collab/{agent}/{task_slug}")

Usage:
  python bin/worktree_ctl.py create --task TASK-ABC123 --agent orchestrator --repo ../my-app
  python bin/worktree_ctl.py create --task TASK-ABC123 --agent worker --repo ../my-app --base-ref main
  python bin/worktree_ctl.py list
  python bin/worktree_ctl.py retire --task TASK-ABC123 --agent worker
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
    config_get,
    find_task_by_id,
    parse_frontmatter,
    shortid,
    slugify,
    utc_iso,
    write_file,
)

WORKTREES_FILE = ROOT / "State" / "worktrees.json"


def parse_args():
    p = argparse.ArgumentParser(description="Manage isolated git worktrees.")
    sub = p.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a worktree for an agent+task")
    create.add_argument("--task", required=True, help="TASK-id")
    create.add_argument("--agent", required=True, help="Agent ID")
    create.add_argument("--repo", required=True, help="Repo path (relative to projects_root or absolute)")
    create.add_argument("--base-ref", default="HEAD", help="Git ref to base from (default: HEAD)")
    create.add_argument("--worktree-root", default=None, help="Override worktree root directory")
    create.add_argument("--branch", default=None, help="Override branch name")

    sub.add_parser("list", help="List all worktrees")

    retire = sub.add_parser("retire", help="Mark a worktree as retired")
    retire.add_argument("--task", required=True)
    retire.add_argument("--agent", required=True)

    return p.parse_args()


def load_worktrees() -> list[dict]:
    if not WORKTREES_FILE.exists():
        return []
    return json.loads(WORKTREES_FILE.read_text())


def save_worktrees(entries: list[dict]) -> None:
    WORKTREES_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_file(WORKTREES_FILE, json.dumps(entries, indent=2))


def resolve_repo(repo_arg: str) -> Path:
    p = Path(repo_arg)
    if p.is_absolute():
        return p.resolve()
    projects_root = config_get("projects_root")
    if projects_root:
        candidate = (Path(projects_root) / repo_arg).resolve()
        if candidate.exists():
            return candidate
    return (ROOT / repo_arg).resolve()


def make_branch_name(agent_id: str, task_slug: str) -> str:
    pattern = config_get("branch_pattern", "collab/{agent}/{task_slug}")
    return pattern.format(
        agent=agent_id,
        task_slug=task_slug,
        orchestrator="collab",
    ).replace(" ", "-")[:72]


def git(args_list: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args_list, cwd=cwd, capture_output=True, text=True)


def get_head_sha(repo: Path) -> str:
    result = git(["rev-parse", "HEAD"], repo)
    return result.stdout.strip()


def create_worktree(args) -> dict:
    if args.agent not in agent_ids():
        print(f"[error] Unknown agent: {args.agent!r}", file=sys.stderr)
        sys.exit(1)

    task_file = find_task_by_id(args.task)
    if task_file is None:
        print(f"[error] Task not found: {args.task}", file=sys.stderr)
        sys.exit(1)

    fm, _ = parse_frontmatter(task_file.read_text())
    task_slug = slugify(fm.get("title", args.task), max_len=32)
    tid = fm.get("task_id", args.task)
    short_task = tid.lower().replace("task-", "t-")

    repo = resolve_repo(args.repo)
    if not repo.exists():
        print(f"[error] Repo not found: {repo}", file=sys.stderr)
        sys.exit(1)

    branch = args.branch or make_branch_name(args.agent, f"{short_task}-{task_slug}")
    worktree_root = args.worktree_root or str(repo.parent / f"{repo.name}-worktrees")
    worktree_path = Path(worktree_root) / args.agent / f"{short_task}-{task_slug}"

    base_sha = get_head_sha(repo)

    result = git(["worktree", "add", "-b", branch, str(worktree_path), args.base_ref], repo)
    if result.returncode != 0:
        print(f"[error] git worktree add failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    entry = {
        "task_id": tid,
        "agent": args.agent,
        "repo": str(repo),
        "worktree_path": str(worktree_path),
        "branch": branch,
        "base_ref": args.base_ref,
        "base_sha": base_sha,
        "integrated": False,
        "retired": False,
        "created_utc": utc_iso(),
    }

    entries = load_worktrees()
    entries.append(entry)
    save_worktrees(entries)

    print(json.dumps(entry, indent=2))
    return entry


def list_worktrees() -> None:
    entries = load_worktrees()
    if not entries:
        print("No worktrees registered.")
        return
    col = "{:<14} {:<14} {:<10} {:<10} {}"
    print(col.format("TASK", "AGENT", "INTEGRATED", "RETIRED", "BRANCH"))
    print("-" * 80)
    for e in entries:
        print(col.format(
            e.get("task_id", ""),
            e.get("agent", ""),
            str(e.get("integrated", False)),
            str(e.get("retired", False)),
            e.get("branch", ""),
        ))


def retire_worktree(task_id: str, agent_id: str) -> None:
    entries = load_worktrees()
    found = False
    for e in entries:
        if e.get("task_id", "").upper() == task_id.upper() and e.get("agent") == agent_id:
            e["retired"] = True
            e["retired_utc"] = utc_iso()
            found = True
    if not found:
        print(f"[warn] No active worktree found for {task_id} / {agent_id}")
    else:
        save_worktrees(entries)
        print(f"[retired] {task_id} / {agent_id}")


def main():
    args = parse_args()
    if args.command == "create":
        create_worktree(args)
    elif args.command == "list":
        list_worktrees()
    elif args.command == "retire":
        retire_worktree(args.task, args.agent)


if __name__ == "__main__":
    main()
