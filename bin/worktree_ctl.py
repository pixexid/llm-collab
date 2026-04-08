#!/usr/bin/env python3
"""
worktree_ctl.py — Create and manage isolated git worktrees per agent per task.

Worktree metadata is stored in State/worktrees.json.

Branch naming uses the pattern from collab.config.json:
  branch_pattern: "collab/{agent}/{task_slug}"
  (customizable with {agent}, {task_slug}, {orchestrator})
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    config_get,
    dump_frontmatter,
    find_task_by_id,
    parse_frontmatter,
    run_project_preflight,
    slugify,
    utc_iso,
    write_file,
)

WORKTREES_FILE = ROOT / "State" / "worktrees.json"


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)


def load_worktrees() -> list[dict[str, Any]]:
    if not WORKTREES_FILE.exists():
        return []
    payload = json.loads(WORKTREES_FILE.read_text())
    if isinstance(payload, list):
        return payload
    return payload.get("worktrees", []) if isinstance(payload, dict) else []


def save_worktrees(entries: list[dict[str, Any]]) -> None:
    WORKTREES_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_file(WORKTREES_FILE, json.dumps(entries, indent=2) + "\n")


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


def git_repo_root(repo: Path) -> Path:
    result = run_git(["rev-parse", "--show-toplevel"], repo)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"[error] Not a git repo: {repo}")
    return Path(result.stdout.strip()).resolve()


def git_status_short(repo: Path) -> list[str]:
    result = run_git(["status", "--short"], repo)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "[error] Failed to inspect git status")
    return [line for line in result.stdout.splitlines() if line.strip()]


def branch_exists(repo: Path, branch: str) -> bool:
    local = run_git(["show-ref", "--verify", f"refs/heads/{branch}"], repo)
    if local.returncode == 0:
        return True
    remote = run_git(["show-ref", "--verify", f"refs/remotes/origin/{branch}"], repo)
    return remote.returncode == 0


def worktree_exists(repo: Path, worktree_path: Path) -> bool:
    result = run_git(["worktree", "list", "--porcelain"], repo)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "[error] Failed to list git worktrees")
    target = str(worktree_path.resolve())
    for line in result.stdout.splitlines():
        if line.strip() == f"worktree {target}":
            return True
    return False


def current_head_sha(repo: Path, ref: str = "HEAD") -> str:
    result = run_git(["rev-parse", ref], repo)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"[error] Failed to resolve ref: {ref}")
    return result.stdout.strip()


def make_branch_name(agent_id: str, task_slug: str) -> str:
    pattern = config_get("branch_pattern", "collab/{agent}/{task_slug}")
    return pattern.format(
        agent=agent_id,
        task_slug=task_slug,
        orchestrator="collab",
    ).replace(" ", "-")[:72]


def find_entry(entries: list[dict[str, Any]], task_id: str | None, path: str | None) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("retired"):
            continue
        if task_id and str(entry.get("task_id", "")).upper() == task_id.upper():
            return entry
        if path:
            wp = Path(str(entry.get("worktree_path", ""))).resolve()
            if wp == Path(path).resolve():
                return entry
    return None


def add_task_activity(task_path: Path, note: str, branch: str | None = None, worktree_path: Path | None = None) -> None:
    fm, body = parse_frontmatter(task_path.read_text())
    if not fm:
        return
    if branch:
        fm["branch"] = branch
    if worktree_path:
        related = fm.get("related_paths")
        if not isinstance(related, list):
            related = []
        wp = str(worktree_path)
        if wp not in related:
            related.append(wp)
        fm["related_paths"] = related

    line = f"- {utc_iso()} | orchestrator | {note}"
    if "## Activity Log" in body:
        body = body.replace("## Activity Log", f"## Activity Log\n\n{line}", 1)
    else:
        body = body.rstrip() + f"\n\n## Activity Log\n\n{line}\n"
    write_file(task_path, dump_frontmatter(fm, body))


def command_create(args: argparse.Namespace) -> None:
    if args.agent not in agent_ids():
        raise SystemExit(f"[error] Unknown agent: {args.agent!r}")

    task_path = find_task_by_id(args.task)
    if task_path is None:
        raise SystemExit(f"[error] Task not found: {args.task}")
    fm, _ = parse_frontmatter(task_path.read_text())
    title = str(fm.get("title") or args.task)
    task_id = str(fm.get("task_id") or args.task)
    project_id = fm.get("project_id")
    task_slug = slugify(title, max_len=32)
    short_task = task_id.lower().replace("task-", "t-")

    repo = resolve_repo(args.repo)
    if not repo.exists():
        raise SystemExit(f"[error] Repo not found: {repo}")
    repo_root = git_repo_root(repo)

    if git_status_short(repo_root) and not args.allow_dirty_base:
        raise SystemExit(
            "[error] Base repo is dirty. Commit/stash first or pass --allow-dirty-base to bypass."
        )

    if not args.skip_preflight:
        preflight = run_project_preflight(project_id, cwd=repo_root)
        if preflight.get("ran") and not preflight.get("ok"):
            raise SystemExit(
                json.dumps(
                    {
                        "error": "project preflight failed; refusing worktree creation",
                        "task_id": task_id,
                        "project_id": project_id,
                        "repo_root": str(repo_root),
                        "preflight": preflight,
                    },
                    indent=2,
                )
            )

    branch = args.branch or make_branch_name(args.agent, f"{short_task}-{task_slug}")
    worktree_root = Path(args.worktree_root) if args.worktree_root else (repo_root.parent / f"{repo_root.name}-worktrees")
    worktree_path = worktree_root / args.agent / f"{short_task}-{task_slug}"
    base_sha = current_head_sha(repo_root, args.base_ref)

    if branch_exists(repo_root, branch):
        raise SystemExit(f"[error] Branch already exists: {branch}")
    if worktree_path.exists() or worktree_exists(repo_root, worktree_path):
        raise SystemExit(f"[error] Worktree path already exists: {worktree_path}")

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = run_git(["worktree", "add", "-b", branch, str(worktree_path), args.base_ref], repo_root)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "[error] git worktree add failed")

    entry: dict[str, Any] = {
        "task_id": task_id,
        "agent": args.agent,
        "repo": str(repo_root),
        "worktree_path": str(worktree_path),
        "branch": branch,
        "base_ref": args.base_ref,
        "base_sha": base_sha,
        "allowed_workspace": str(worktree_path),
        "checkpoint_commits_required": bool(args.require_checkpoint_commit),
        "integrated": False,
        "retired": False,
        "created_utc": utc_iso(),
    }
    entries = load_worktrees()
    entries.append(entry)
    save_worktrees(entries)
    add_task_activity(
        task_path,
        f"created isolated worktree `{worktree_path}` on branch `{branch}` from `{args.base_ref}`",
        branch=branch,
        worktree_path=worktree_path,
    )
    print(json.dumps(entry, indent=2))


def command_list(_args: argparse.Namespace) -> None:
    entries = load_worktrees()
    if not entries:
        print("No worktrees registered.")
        return
    col = "{:<14} {:<14} {:<10} {:<10} {}"
    print(col.format("TASK", "AGENT", "INTEGRATED", "RETIRED", "BRANCH"))
    print("-" * 80)
    for entry in entries:
        print(
            col.format(
                str(entry.get("task_id", "")),
                str(entry.get("agent", "")),
                str(entry.get("integrated", False)),
                str(entry.get("retired", False)),
                str(entry.get("branch", "")),
            )
        )


def command_preflight(args: argparse.Namespace) -> None:
    entries = load_worktrees()
    entry = find_entry(entries, args.task, args.path)
    if entry is None:
        raise SystemExit("[error] Active worktree entry not found")

    worktree_path = Path(str(entry["worktree_path"])).resolve()
    if not worktree_path.exists():
        raise SystemExit(f"[error] Assigned worktree does not exist: {worktree_path}")

    repo_root = git_repo_root(worktree_path)
    branch_result = run_git(["branch", "--show-current"], worktree_path)
    if branch_result.returncode != 0:
        raise SystemExit(branch_result.stderr.strip() or "[error] Failed to read current branch")
    branch = branch_result.stdout.strip()
    status_lines = git_status_short(worktree_path)

    payload = {
        "task_id": entry.get("task_id"),
        "repo_root": str(repo_root),
        "worktree_path": str(worktree_path),
        "branch": branch,
        "expected_branch": entry.get("branch"),
        "base_sha": entry.get("base_sha"),
        "checkpoint_commits_required": bool(entry.get("checkpoint_commits_required", False)),
        "dirty": bool(status_lines),
        "status_lines": status_lines,
    }
    if repo_root != worktree_path:
        raise SystemExit(
            json.dumps(
                {
                    **payload,
                    "error": "worktree root mismatch; command is not running inside the assigned isolated worktree",
                },
                indent=2,
            )
        )
    if branch != entry.get("branch"):
        raise SystemExit(json.dumps({**payload, "error": "branch mismatch"}, indent=2))
    print(json.dumps(payload, indent=2))


def command_mark_integrated(args: argparse.Namespace) -> None:
    entries = load_worktrees()
    entry = find_entry(entries, args.task, args.path)
    if entry is None:
        raise SystemExit("[error] Active worktree entry not found")

    entry["integrated"] = True
    entry["integrated_utc"] = utc_iso()
    entry["integrated_by"] = args.by
    if args.commit_sha:
        entry["integration_commit_sha"] = args.commit_sha
    save_worktrees(entries)
    print(json.dumps(entry, indent=2))


def command_remove(args: argparse.Namespace) -> None:
    entries = load_worktrees()
    entry = find_entry(entries, args.task, args.path)
    if entry is None:
        raise SystemExit("[error] Active worktree entry not found")

    repo = Path(str(entry["repo"])).resolve()
    worktree_path = Path(str(entry["worktree_path"])).resolve()
    branch = str(entry["branch"])

    status_lines = git_status_short(worktree_path) if worktree_path.exists() else []
    if status_lines and not args.force:
        raise SystemExit(
            "[error] Refusing to retire dirty worktree. Commit/discard changes or pass --force."
        )
    if not entry.get("integrated") and not args.force:
        raise SystemExit(
            "[error] Refusing to retire non-integrated worktree. Run mark-integrated first or pass --force."
        )

    if worktree_path.exists():
        remove_result = run_git(["worktree", "remove", str(worktree_path)], repo)
        if remove_result.returncode != 0:
            raise SystemExit(remove_result.stderr.strip() or remove_result.stdout.strip() or "[error] git worktree remove failed")

    if args.delete_branch:
        delete_result = run_git(["branch", "-D", branch], repo)
        if delete_result.returncode != 0:
            raise SystemExit(delete_result.stderr.strip() or delete_result.stdout.strip() or "[error] git branch -D failed")

    entry["retired"] = True
    entry["retired_utc"] = utc_iso()
    save_worktrees(entries)

    task_id = str(entry.get("task_id", ""))
    task_path = find_task_by_id(task_id) if task_id else None
    if task_path is not None:
        add_task_activity(task_path, f"retired isolated worktree `{worktree_path}`")

    print(
        json.dumps(
            {
                "task": task_id,
                "worktree_path": str(worktree_path),
                "branch": branch,
                "deleted_branch": bool(args.delete_branch),
            },
            indent=2,
        )
    )


def command_retire(args: argparse.Namespace) -> None:
    entries = load_worktrees()
    found = False
    for entry in entries:
        if str(entry.get("task_id", "")).upper() == args.task.upper() and str(entry.get("agent")) == args.agent:
            entry["retired"] = True
            entry["retired_utc"] = utc_iso()
            found = True
    if not found:
        print(f"[warn] No active worktree found for {args.task} / {args.agent}")
    else:
        save_worktrees(entries)
        print(f"[retired] {args.task} / {args.agent}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage isolated git worktrees.")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Create a worktree for an agent+task")
    create.add_argument("--task", required=True, help="TASK-id")
    create.add_argument("--agent", required=True, help="Agent ID")
    create.add_argument("--repo", required=True, help="Repo path (relative to projects_root or absolute)")
    create.add_argument("--base-ref", default="HEAD", help="Git ref to base from (default: HEAD)")
    create.add_argument("--worktree-root", default=None, help="Override worktree root directory")
    create.add_argument("--branch", default=None, help="Override branch name")
    create.add_argument("--allow-dirty-base", action="store_true", help="Allow create from dirty base repo")
    create.add_argument("--skip-preflight", action="store_true", help="Skip project preflight gate before create")
    create.add_argument(
        "--require-checkpoint-commit",
        action="store_true",
        help="Mark worker checkpoint commits as required for this worktree task",
    )
    create.set_defaults(func=command_create)

    list_cmd = sub.add_parser("list", help="List all worktrees")
    list_cmd.set_defaults(func=command_list)

    preflight = sub.add_parser("preflight", help="Check assigned worktree/branch expectations")
    preflight.add_argument("--task", default=None, help="Task ID whose worktree should be checked")
    preflight.add_argument("--path", default=None, help="Explicit worktree path to check")
    preflight.set_defaults(func=command_preflight)

    mark_integrated = sub.add_parser("mark-integrated", help="Mark a tracked worktree as integrated")
    mark_integrated.add_argument("--task", default=None, help="Task ID whose worktree should be marked integrated")
    mark_integrated.add_argument("--path", default=None, help="Explicit worktree path to mark integrated")
    mark_integrated.add_argument("--by", default="orchestrator", help="Who marked the worktree integrated")
    mark_integrated.add_argument("--commit-sha", default=None, help="Integration commit SHA in the main tree")
    mark_integrated.set_defaults(func=command_mark_integrated)

    remove = sub.add_parser("remove", help="Remove a tracked worktree")
    remove.add_argument("--task", default=None, help="Task ID whose worktree should be removed")
    remove.add_argument("--path", default=None, help="Explicit worktree path to remove")
    remove.add_argument("--delete-branch", action="store_true", help="Delete branch after removing worktree")
    remove.add_argument("--force", action="store_true", help="Bypass dirty/integrated guards")
    remove.set_defaults(func=command_remove)

    retire = sub.add_parser("retire", help="Mark a worktree as retired (state only)")
    retire.add_argument("--task", required=True)
    retire.add_argument("--agent", required=True)
    retire.set_defaults(func=command_retire)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
