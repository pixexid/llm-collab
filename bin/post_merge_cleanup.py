#!/usr/bin/env python3
"""
Post-merge branch/worktree cleanup gate.

The command is intentionally conservative by default:
- dry-run unless --apply is passed
- removes registered git worktrees before deleting branches
- removes clean done-task worktrees even when the branch was squash-merged
- preserves dirty worktrees unless the dirt is a known disposable generated file
- reports deferred entries so a queue runner cannot silently clear post_merge
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    TASKS_DIR,
    ensure_project,
    parse_frontmatter,
    resolve_project_repo_path,
)


DISPOSABLE_STATUS_PATHS = {
    "public/sitemap.xml",
}


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class TaskRecord:
    task_id: str
    status: str
    branch: str | None
    path: str


@dataclass
class WorktreeRecord:
    path: Path
    branch: str | None
    detached: bool
    head: str | None


def git(repo: Path, args: list[str], *, check: bool = False) -> GitResult:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or f"[error] git {' '.join(args)} failed")
    return GitResult(result.returncode, result.stdout.strip(), result.stderr.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the post-merge cleanup gate for project worktrees and branches.")
    parser.add_argument("--project", required=True, help="Project id from projects.json.")
    parser.add_argument("--repo-key", default="app", help="Project repo key to clean. Default: app.")
    parser.add_argument("--repo", help="Explicit git repo root. Overrides --project/--repo-key resolution.")
    parser.add_argument("--base", default="main", help="Base branch to compare against. Default: main.")
    parser.add_argument("--worktree-root", help="Worktree root to scan. Default: <repo-parent>/<repo-name>-worktrees.")
    parser.add_argument("--apply", action="store_true", help="Actually remove safe candidates. Default is dry-run.")
    parser.add_argument(
        "--remove-plain-dirs",
        action="store_true",
        help="Remove stale non-git directories under the worktree root when --apply is set.",
    )
    parser.add_argument(
        "--discard-disposable-dirty",
        action="store_true",
        help="Restore known generated files such as public/sitemap.xml before removing done-task worktrees.",
    )
    parser.add_argument(
        "--fail-on-blockers",
        action="store_true",
        help="Exit non-zero when cleanup still has removable items or stale done/review blockers.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def resolve_repo(args: argparse.Namespace) -> Path:
    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
    else:
        repo = resolve_project_repo_path(args.project, args.repo_key)
        if repo is None:
            raise SystemExit(f"[error] Cannot resolve repo for project={args.project!r} repo_key={args.repo_key!r}")
    if git(repo, ["rev-parse", "--show-toplevel"]).returncode != 0:
        raise SystemExit(f"[error] Not a git repo: {repo}")
    return repo


def resolve_worktree_root(args: argparse.Namespace, repo: Path) -> Path:
    if args.worktree_root:
        return Path(args.worktree_root).expanduser().resolve()
    return repo.parent / f"{repo.name}-worktrees"


def load_tasks() -> tuple[dict[str, TaskRecord], dict[str, TaskRecord]]:
    by_id: dict[str, TaskRecord] = {}
    by_branch: dict[str, TaskRecord] = {}
    for folder in ("active", "backlog", "done"):
        task_dir = TASKS_DIR / folder
        if not task_dir.exists():
            continue
        for task_path in sorted(task_dir.glob("*.md")):
            frontmatter, _body = parse_frontmatter(task_path.read_text())
            task_id = str(frontmatter.get("task_id") or "").strip()
            if not task_id:
                continue
            branch = frontmatter.get("branch")
            branch_value = str(branch).strip() if branch not in {None, "", "none", "null"} else None
            record = TaskRecord(
                task_id=task_id,
                status=str(frontmatter.get("status") or folder),
                branch=branch_value,
                path=str(task_path),
            )
            by_id[task_id.upper()] = record
            if branch_value:
                by_branch[branch_value] = record
    return by_id, by_branch


def parse_worktrees(repo: Path) -> list[WorktreeRecord]:
    result = git(repo, ["worktree", "list", "--porcelain"], check=True)
    records: list[WorktreeRecord] = []
    current: dict[str, str] = {}
    for line in [*result.stdout.splitlines(), ""]:
        if not line:
            raw_path = current.get("worktree")
            if raw_path:
                raw_branch = current.get("branch")
                branch = raw_branch.removeprefix("refs/heads/") if raw_branch else None
                records.append(
                    WorktreeRecord(
                        path=Path(raw_path),
                        branch=branch,
                        detached=branch is None,
                        head=current.get("HEAD"),
                    )
                )
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def status_lines(path: Path) -> list[str]:
    result = git(path, ["status", "--short", "--untracked-files=all"], check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def status_path(line: str) -> str:
    if " -> " in line:
        return line.rsplit(" -> ", 1)[1].strip()
    return line[3:].strip()


def only_disposable_status(lines: list[str]) -> bool:
    if not lines:
        return True
    return all(status_path(line) in DISPOSABLE_STATUS_PATHS for line in lines)


def branch_merged(repo: Path, branch: str, base: str) -> bool:
    return git(repo, ["merge-base", "--is-ancestor", branch, base]).returncode == 0


def task_from_branch(branch: str, by_id: dict[str, TaskRecord], by_branch: dict[str, TaskRecord]) -> TaskRecord | None:
    if branch in by_branch:
        return by_branch[branch]
    match = re.search(r"task[-_/]([A-Za-z0-9]+)", branch)
    if not match:
        return None
    token = match.group(1).upper()
    return by_id.get(f"TASK-{token}") or by_id.get(token)


def branch_candidates(repo: Path, base: str) -> list[str]:
    result = git(repo, ["branch", "--format=%(refname:short)"], check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip() and line.strip() != base]


def safe_plain_dir(path: Path) -> bool:
    if (path / ".git").exists():
        return False
    if not path.exists() or not path.is_dir():
        return False
    allowed_top = {".local", ".vite", ".turbo", "node_modules", ".next", "dist", "build", "coverage"}
    children = list(path.iterdir())
    if not children:
        return True
    return all(child.name in allowed_top for child in children)


def collect_plain_dirs(worktree_root: Path, registered: set[Path]) -> list[Path]:
    if not worktree_root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(worktree_root.glob("*/*")):
        resolved = path.resolve()
        if not path.is_dir() or resolved in registered:
            continue
        if safe_plain_dir(path):
            candidates.append(path)
    return candidates


def path_inside(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return True
    return False


def collect_empty_dirs(worktree_root: Path, registered: set[Path], skipped_roots: list[Path]) -> list[Path]:
    if not worktree_root.exists():
        return []
    candidates: list[Path] = []
    for path in [*worktree_root.rglob("*"), worktree_root]:
        if not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved in registered or path_inside(path, skipped_roots):
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            candidates.append(path)
    return sorted(candidates, key=lambda item: len(item.parts), reverse=True)


def classify(args: argparse.Namespace) -> dict[str, Any]:
    ensure_project(args.project, allow_none=False)
    repo = resolve_repo(args)
    worktree_root = resolve_worktree_root(args, repo)
    by_id, by_branch = load_tasks()
    worktrees = parse_worktrees(repo)
    registered_paths = {record.path.resolve() for record in worktrees}

    remove_worktrees: list[dict[str, Any]] = []
    deferred_worktrees: list[dict[str, Any]] = []
    remove_branches: list[dict[str, Any]] = []
    deferred_branches: list[dict[str, Any]] = []

    main_path = repo.resolve()
    for record in worktrees:
        if record.path.resolve() == main_path:
            continue
        lines = status_lines(record.path)
        task = task_from_branch(record.branch or "", by_id, by_branch) if record.branch else None
        is_done_task = bool(task and task.status == "done")
        is_review_branch = bool(record.branch and record.branch.startswith("codex/review/"))
        is_detached = record.branch is None
        is_clean = not lines
        disposable_dirty = bool(lines and only_disposable_status(lines))
        merged = bool(record.branch and branch_merged(repo, record.branch, args.base))

        reason = None
        if is_done_task and (is_clean or (args.discard_disposable_dirty and disposable_dirty)):
            reason = "done-task-clean" if is_clean else "done-task-disposable-dirty"
        elif is_review_branch and merged and (is_clean or (args.discard_disposable_dirty and disposable_dirty)):
            reason = "merged-review-branch"
        elif is_detached and is_clean:
            reason = "clean-detached-worktree"

        payload = {
            "path": str(record.path),
            "branch": record.branch,
            "task_id": task.task_id if task else None,
            "task_status": task.status if task else None,
            "dirty": bool(lines),
            "status_lines": lines,
            "merged_to_base": merged,
            "reason": reason,
        }
        if reason:
            remove_worktrees.append(payload)
        else:
            payload["defer_reason"] = defer_reason(record, task, lines, merged)
            deferred_worktrees.append(payload)

    mounted_branches = {record.branch for record in worktrees if record.branch}
    for branch in branch_candidates(repo, args.base):
        if branch in mounted_branches:
            continue
        task = task_from_branch(branch, by_id, by_branch)
        merged = branch_merged(repo, branch, args.base)
        reason = None
        if task and task.status == "done":
            reason = "done-task-branch"
        elif branch.startswith("codex/review/") and merged:
            reason = "merged-review-branch"
        elif merged and (branch.startswith("codex/") or branch.startswith("claude/") or branch.startswith("gemini/")):
            reason = "merged-worker-branch"
        payload = {
            "branch": branch,
            "task_id": task.task_id if task else None,
            "task_status": task.status if task else None,
            "merged_to_base": merged,
            "reason": reason,
        }
        if reason:
            remove_branches.append(payload)
        elif branch.startswith(("codex/", "claude/", "gemini/")):
            payload["defer_reason"] = "no done task or merged-review signal"
            deferred_branches.append(payload)

    plain_dirs = collect_plain_dirs(worktree_root, registered_paths) if args.remove_plain_dirs else []
    empty_dirs = collect_empty_dirs(worktree_root, registered_paths, plain_dirs) if args.remove_plain_dirs else []

    blocking_deferred = [
        item
        for item in [*deferred_worktrees, *deferred_branches]
        if is_blocking_deferred(item)
    ]
    ok_to_clear = not remove_worktrees and not remove_branches and not plain_dirs and not empty_dirs and not blocking_deferred

    return {
        "project": args.project,
        "repo": str(repo),
        "base": args.base,
        "worktree_root": str(worktree_root),
        "apply": bool(args.apply),
        "ok_to_clear_post_merge": ok_to_clear,
        "remove_worktrees": remove_worktrees,
        "remove_branches": remove_branches,
        "remove_plain_dirs": [{"path": str(path), "reason": "stale-plain-disposable-directory"} for path in plain_dirs],
        "remove_empty_dirs": [{"path": str(path), "reason": "empty-worktree-parent-directory"} for path in empty_dirs],
        "deferred_worktrees": deferred_worktrees,
        "deferred_branches": deferred_branches,
        "blocking_deferred": blocking_deferred,
    }


def is_blocking_deferred(item: dict[str, Any]) -> bool:
    task_status = item.get("task_status")
    branch = item.get("branch")
    if task_status == "done":
        return True
    if isinstance(branch, str) and branch.startswith("codex/review/") and item.get("merged_to_base"):
        return True
    if branch is None and item.get("dirty"):
        return True
    return False


def defer_reason(record: WorktreeRecord, task: TaskRecord | None, lines: list[str], merged: bool) -> str:
    if lines and not only_disposable_status(lines):
        return "dirty-non-disposable"
    if lines:
        return "dirty-disposable-but-discard-not-enabled"
    if task and task.status != "done":
        return f"task-status-{task.status}"
    if record.branch and not merged and not task:
        return "branch-not-merged-and-no-task-match"
    if record.branch:
        return "no cleanup signal"
    return "detached-dirty-or-unknown"


def restore_disposable(path: Path, lines: list[str]) -> None:
    tracked_paths = [
        status_path(line)
        for line in lines
        if status_path(line) in DISPOSABLE_STATUS_PATHS and not line.startswith("??")
    ]
    if tracked_paths:
        git(path, ["restore", *tracked_paths], check=True)
    for line in lines:
        if not line.startswith("??"):
            continue
        relative = status_path(line)
        if relative not in DISPOSABLE_STATUS_PATHS:
            continue
        candidate = path / relative
        if candidate.is_dir():
            shutil.rmtree(candidate)
        elif candidate.exists():
            candidate.unlink()


def apply_cleanup(summary: dict[str, Any]) -> None:
    repo = Path(str(summary["repo"]))
    for item in summary["remove_worktrees"]:
        path = Path(str(item["path"]))
        lines = item.get("status_lines") or []
        if lines and only_disposable_status([str(line) for line in lines]):
            restore_disposable(path, [str(line) for line in lines])
        if path.exists():
            git(repo, ["worktree", "remove", str(path)], check=True)
        branch = item.get("branch")
        if isinstance(branch, str) and branch:
            git(repo, ["branch", "-D", branch], check=True)

    for item in summary["remove_branches"]:
        branch = item.get("branch")
        if isinstance(branch, str) and branch:
            git(repo, ["branch", "-D", branch], check=True)

    for item in summary["remove_plain_dirs"]:
        path = Path(str(item["path"]))
        if path.exists() and safe_plain_dir(path):
            shutil.rmtree(path)

    worktree_root = Path(str(summary["worktree_root"]))
    registered_paths = {record.path.resolve() for record in parse_worktrees(repo)}
    while True:
        empty_dirs = collect_empty_dirs(worktree_root, registered_paths, [])
        if not empty_dirs:
            break
        for path in empty_dirs:
            if path.exists():
                path.rmdir()

    git(repo, ["worktree", "prune"], check=True)


def emit(summary: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    print(f"project: {summary['project']}")
    print(f"repo: {summary['repo']}")
    print(f"worktree_root: {summary['worktree_root']}")
    print(f"apply: {summary['apply']}")
    print(f"remove_worktrees: {len(summary['remove_worktrees'])}")
    print(f"remove_branches: {len(summary['remove_branches'])}")
    print(f"remove_plain_dirs: {len(summary['remove_plain_dirs'])}")
    print(f"remove_empty_dirs: {len(summary['remove_empty_dirs'])}")
    print(f"deferred_worktrees: {len(summary['deferred_worktrees'])}")
    print(f"deferred_branches: {len(summary['deferred_branches'])}")
    print(f"blocking_deferred: {len(summary['blocking_deferred'])}")
    print(f"ok_to_clear_post_merge: {summary['ok_to_clear_post_merge']}")
    for item in summary["deferred_worktrees"]:
        print(f"defer worktree: {item.get('branch') or '(detached)'} {item['path']} - {item['defer_reason']}")
    for item in summary["deferred_branches"]:
        print(f"defer branch: {item['branch']} - {item['defer_reason']}")


def main() -> None:
    args = parse_args()
    summary = classify(args)
    if args.apply:
        apply_cleanup(summary)
        summary = classify(args)
        summary["apply"] = True
    emit(summary, as_json=args.json)
    if args.fail_on_blockers and not summary["ok_to_clear_post_merge"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
