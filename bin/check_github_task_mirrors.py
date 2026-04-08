#!/usr/bin/env python3
"""
check_github_task_mirrors.py — detect drift between GitHub issues and local task mirrors.

Usage:
  python bin/check_github_task_mirrors.py --project my-app
  python bin/check_github_task_mirrors.py --project my-app --archive-closed-active
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    all_task_files,
    dump_frontmatter,
    ensure_project,
    get_project,
    parse_frontmatter,
    utc_iso,
    write_file,
)

ISSUE_TITLE_RE = re.compile(r"^GH #(\d+)(?:\s+-\s+|\s+)")
ISSUE_URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")
ISSUE_FILENAME_RE = re.compile(r"\bgh-(\d+)\b", re.IGNORECASE)
ISSUE_BODY_RE = re.compile(r"\bGH[- #]+(\d+)\b", re.IGNORECASE)


def get_repo_from_project(project_id: str) -> str:
    project = get_project(project_id)
    if project is None:
        raise SystemExit(f"[error] Unknown project_id: {project_id!r}")
    github = project.get("github", {})
    if not isinstance(github, dict) or not github.get("enabled"):
        raise SystemExit(f"[error] GitHub integration is not enabled for project {project_id!r}")
    repo = github.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise SystemExit(f"[error] Project {project_id!r} has invalid github.repo: {repo!r}")
    return repo


def gh_issue_map(repo: str) -> dict[int, dict[str, str]]:
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        "500",
        "--json",
        "number,title,url,state,labels",
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    mapped: dict[int, dict[str, str]] = {}
    for issue in payload:
        number = int(issue["number"])
        labels = ",".join(str(label["name"]) for label in issue.get("labels", []))
        mapped[number] = {
            "title": str(issue["title"]),
            "url": str(issue["url"]),
            "state": str(issue["state"]),
            "labels": labels,
        }
    return mapped


def is_epic(issue: dict[str, str]) -> bool:
    labels = {label.strip() for label in issue.get("labels", "").split(",") if label.strip()}
    return "type:epic" in labels


def extract_issue_number(task_path: Path) -> int | None:
    fm, body = parse_frontmatter(task_path.read_text())
    title = str(fm.get("title", ""))
    match = ISSUE_TITLE_RE.match(title)
    if match:
        return int(match.group(1))
    url_match = ISSUE_URL_RE.search(body)
    if url_match:
        return int(url_match.group(1))
    body_match = ISSUE_BODY_RE.search(body)
    if body_match:
        return int(body_match.group(1))
    filename_match = ISSUE_FILENAME_RE.search(task_path.name)
    if filename_match:
        return int(filename_match.group(1))
    return None


def add_activity_log(body: str, actor: str, note: str) -> str:
    stamp = utc_iso()
    line = f"- {stamp} | {actor} | {note}"
    if "## Activity Log" in body:
        return body.replace("## Activity Log", f"## Activity Log\n\n{line}", 1)
    return body.rstrip() + f"\n\n## Activity Log\n\n{line}\n"


def archive_closed_task(path: Path, issue_number: int) -> Path:
    fm, body = parse_frontmatter(path.read_text())
    if fm:
        fm["status"] = "done"
        body = add_activity_log(body, "sync", f"archived mirror after issue #{issue_number} closed on GitHub")
        content = dump_frontmatter(fm, body)
    else:
        content = body

    target = path.parent.parent / "done" / path.name
    write_file(target, content)
    path.unlink()
    return target


def task_in_scope(frontmatter: dict, project_id: str, strict_project: bool) -> bool:
    scoped = frontmatter.get("project_id")
    if scoped == project_id:
        return True
    if strict_project:
        return False
    return scoped in (None, "", "null")


def iter_project_task_mirrors(project_id: str, strict_project: bool) -> list[Path]:
    result: list[Path] = []
    for path in all_task_files():
        fm, _ = parse_frontmatter(path.read_text())
        if not task_in_scope(fm, project_id, strict_project):
            continue
        if extract_issue_number(path) is None:
            continue
        result.append(path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check drift between GitHub issues and local task mirrors.")
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    parser.add_argument("--repo", default=None, help="Optional repo override (owner/repo)")
    parser.add_argument(
        "--archive-closed-active",
        action="store_true",
        help="Move active/backlog mirrors for closed issues into Tasks/done and mark status done.",
    )
    parser.add_argument(
        "--strict-project",
        action="store_true",
        help="Only include tasks with exact project_id match; exclude legacy unscoped tasks.",
    )
    args = parser.parse_args()

    ensure_project(args.project, allow_none=False)
    repo = args.repo or get_repo_from_project(args.project)

    issue_map = gh_issue_map(repo)
    open_issues = {
        number: issue
        for number, issue in issue_map.items()
        if issue["state"] == "OPEN" and not is_epic(issue)
    }
    missing_issue_numbers = sorted(open_issues)
    stale_active: list[dict] = []
    premature_done: list[dict] = []
    unresolved_mirrors: list[dict] = []
    mirrors = []
    archived_paths: list[Path] = []

    for path in iter_project_task_mirrors(args.project, args.strict_project):
        issue_number = extract_issue_number(path)
        if issue_number is None:
            continue

        folder = path.parent.name
        mirrors.append({"number": issue_number, "path": path, "folder": folder})

        if issue_number in missing_issue_numbers:
            missing_issue_numbers.remove(issue_number)

        issue = issue_map.get(issue_number)
        if issue is None:
            unresolved_mirrors.append({"number": issue_number, "path": path, "folder": folder})
            continue

        if folder in {"active", "backlog"} and issue["state"] != "OPEN":
            stale_active.append({"number": issue_number, "path": path, "folder": folder})
            continue

        if folder == "done" and issue["state"] == "OPEN":
            premature_done.append({"number": issue_number, "path": path, "folder": folder, "url": issue["url"]})

    if args.archive_closed_active:
        for item in stale_active:
            archived_paths.append(archive_closed_task(item["path"], item["number"]))
        stale_active = []

    unresolved_mirrors = [item for item in unresolved_mirrors if item["folder"] != "done"]

    print(f"project: {args.project}")
    print(f"repo: {repo}")
    print(f"all GitHub issues loaded: {len(issue_map)}")
    print(f"open GitHub issues: {len(open_issues)}")
    print(f"local issue mirrors: {len(mirrors)}")
    print(f"missing open issue mirrors: {len(missing_issue_numbers)}")
    print(f"stale active/backlog mirrors: {len(stale_active)}")
    print(f"premature done mirrors: {len(premature_done)}")
    print(f"unresolved mirrors: {len(unresolved_mirrors)}")

    if archived_paths:
        print("\nArchived closed active/backlog mirrors:")
        for path in archived_paths:
            print(f"- {path.relative_to(ROOT)}")

    if missing_issue_numbers:
        print("\nMissing local mirrors for open issues:")
        for number in missing_issue_numbers:
            issue = open_issues[number]
            print(f"- #{number} {issue['title']} | {issue['url']}")
    else:
        print("\nMissing local mirrors for open issues:\n- none")

    if stale_active:
        print("\nStale active/backlog mirrors:")
        for item in stale_active:
            print(f"- #{item['number']} [{item['folder']}] {item['path'].relative_to(ROOT)}")
    else:
        print("\nStale active/backlog mirrors:\n- none")

    if premature_done:
        print("\nPremature done mirrors for still-open issues:")
        for item in premature_done:
            print(f"- #{item['number']} {Path(item['path']).relative_to(ROOT)} | {item['url']}")
    else:
        print("\nPremature done mirrors for still-open issues:\n- none")

    if unresolved_mirrors:
        print("\nUnresolved local mirrors:")
        for item in unresolved_mirrors:
            print(f"- #{item['number']} [{item['folder']}] {Path(item['path']).relative_to(ROOT)}")
    else:
        print("\nUnresolved local mirrors:\n- none")

    if missing_issue_numbers or stale_active or premature_done or unresolved_mirrors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
