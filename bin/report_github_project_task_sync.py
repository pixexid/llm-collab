#!/usr/bin/env python3
"""
report_github_project_task_sync.py — compare GitHub project status with local task mirrors.

Usage:
  python bin/report_github_project_task_sync.py --project my-app
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    all_task_files,
    ensure_project,
    get_project,
    parse_frontmatter,
    utc_iso,
)

ISSUE_TITLE_RE = re.compile(r"^GH #(\d+)(?:\s+-\s+|\s+)")
ISSUE_URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")
ISSUE_FILENAME_RE = re.compile(r"\bgh-(\d+)\b", re.IGNORECASE)


def run_gh_json(command: list[str]) -> Any:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def project_github_config(project_id: str) -> tuple[str, int | None, str | None]:
    project = get_project(project_id)
    if project is None:
        raise SystemExit(f"[error] Unknown project_id: {project_id!r}")
    github = project.get("github", {})
    if not isinstance(github, dict) or not github.get("enabled"):
        raise SystemExit(f"[error] GitHub integration is not enabled for project {project_id!r}")
    repo = github.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise SystemExit(f"[error] Project {project_id!r} has invalid github.repo: {repo!r}")
    project_number = github.get("project_number")
    owner = repo.split("/", 1)[0]
    return repo, project_number if isinstance(project_number, int) else None, owner


def repository_matches(content: dict[str, Any], repo: str) -> bool:
    value = content.get("repository")
    if isinstance(value, str):
        return value == repo or value.endswith(f"/{repo}")
    if isinstance(value, dict):
        owner = value.get("owner")
        name = value.get("name")
        if isinstance(owner, dict):
            owner = owner.get("login")
        if isinstance(owner, str) and isinstance(name, str):
            return f"{owner}/{name}" == repo
        url = value.get("url")
        if isinstance(url, str):
            return url.endswith(f"/{repo}")
    return False


def load_open_issues(repo: str) -> dict[int, dict[str, Any]]:
    payload = run_gh_json([
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,url,labels",
    ])
    issues: dict[int, dict[str, Any]] = {}
    for item in payload:
        number = int(item["number"])
        issues[number] = {
            "number": number,
            "title": str(item["title"]),
            "url": str(item["url"]),
            "labels": [str(label["name"]) for label in item.get("labels", [])],
            "project_items": [],
        }
    return issues


def split_epics(issues: dict[int, dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    execution: dict[int, dict[str, Any]] = {}
    epics: dict[int, dict[str, Any]] = {}
    for number, issue in issues.items():
        labels = set(issue.get("labels", []))
        if "type:epic" in labels:
            epics[number] = issue
        else:
            execution[number] = issue
    return execution, epics


def load_project_title(project_number: int, owner: str) -> str:
    try:
        payload = run_gh_json([
            "gh",
            "project",
            "view",
            str(project_number),
            "--owner",
            owner,
            "--format",
            "json",
        ])
    except subprocess.CalledProcessError:
        return "<project-unavailable>"
    title = payload.get("title")
    return title if isinstance(title, str) and title else "<project-unavailable>"


def load_project_items(project_number: int, owner: str, repo: str) -> dict[int, dict[str, Any]]:
    try:
        payload = run_gh_json([
            "gh",
            "project",
            "item-list",
            str(project_number),
            "--owner",
            owner,
            "--limit",
            "200",
            "--format",
            "json",
        ])
    except subprocess.CalledProcessError:
        return {}

    items: dict[int, dict[str, Any]] = {}
    for item in payload.get("items", []):
        content = item.get("content") or {}
        if content.get("type") != "Issue":
            continue
        if not repository_matches(content, repo):
            continue
        number = content.get("number")
        if not isinstance(number, int):
            continue
        items[number] = {
            "status": str(item.get("status") or "<none>"),
            "phase": str(item.get("phase") or "<none>"),
            "title": str(item.get("title") or content.get("title") or ""),
            "id": str(item.get("id") or ""),
        }
    return items


def task_in_scope(frontmatter: dict, project_id: str, strict_project: bool) -> bool:
    scoped = frontmatter.get("project_id")
    if scoped == project_id:
        return True
    if strict_project:
        return False
    return scoped in (None, "", "null")


def load_local_mirrors(project_id: str, strict_project: bool) -> dict[int, dict[str, Any]]:
    mirrors: dict[int, dict[str, Any]] = {}
    for path in all_task_files():
        fm, body = parse_frontmatter(path.read_text())
        if not task_in_scope(fm, project_id, strict_project):
            continue
        title = str(fm.get("title", ""))

        number: int | None = None
        title_match = ISSUE_TITLE_RE.match(title)
        if title_match:
            number = int(title_match.group(1))
        else:
            url_match = ISSUE_URL_RE.search(body)
            if url_match:
                number = int(url_match.group(1))
            else:
                file_match = ISSUE_FILENAME_RE.search(path.name)
                if file_match:
                    number = int(file_match.group(1))

        if number is None or number in mirrors:
            continue

        mirrors[number] = {
            "path": path,
            "folder": path.parent.name,
            "meta": fm,
        }
    return mirrors


def assess_status_alignment(project_status: str, local_status: str, owner: str) -> str:
    if project_status == "In Progress" and (local_status not in {"in_progress", "review"} or owner == "unassigned"):
        return "mismatch"
    if project_status == "Todo" and local_status not in {"open", "backlog"}:
        return "review"
    if project_status == "Done" and local_status != "done":
        return "review"
    return "ok"


def render_markdown(
    project_id: str,
    repo: str,
    project_url: str,
    generated_at: str,
    issues: dict[int, dict[str, Any]],
    epic_issues: dict[int, dict[str, Any]],
    project_items: dict[int, dict[str, Any]],
    mirrors: dict[int, dict[str, Any]],
) -> str:
    open_numbers = sorted(issues)
    epic_numbers = sorted(epic_issues)
    missing_mirrors = [n for n in open_numbers if n not in mirrors]
    missing_project_items = [n for n in open_numbers if n not in project_items]
    orphan_mirrors = [
        n
        for n in sorted(mirrors)
        if n not in issues and str(mirrors[n].get("folder")) != "done"
    ]

    lines = [
        "# GitHub Project Task Sync Report\n\n",
        f"- generated_utc: {generated_at}\n",
        f"- project_id: {project_id}\n",
        f"- repo: {repo}\n",
        f"- project: {project_url}\n",
        f"- open_issues: {len(open_numbers)}\n",
        f"- open_epics: {len(epic_numbers)}\n",
        f"- project_issue_items: {len(project_items)}\n",
        f"- local_issue_mirrors: {len(mirrors)}\n\n",
        "## Drift Summary\n\n",
        f"- missing local mirrors: {len(missing_mirrors)}\n",
        f"- missing project items: {len(missing_project_items)}\n",
        f"- orphan local mirrors: {len(orphan_mirrors)}\n\n",
        "Alignment legend:\n",
        "- `ok`: no action needed\n",
        "- `mismatch`: execution drift; fix immediately\n",
        "- `review`: operator judgement needed\n\n",
    ]

    lines.append("## Open Issue Alignment\n\n")
    lines.append("| Issue | Project Status | Phase | Local Status | Folder | Owner | Alignment | Task File |\n")
    lines.append("|---|---|---|---|---|---|---|---|\n")
    for number in open_numbers:
        issue = issues[number]
        project_item = project_items.get(number)
        mirror = mirrors.get(number)
        project_status = project_item["status"] if project_item else "<missing>"
        phase = project_item["phase"] if project_item else "<missing>"
        local_status = str(mirror["meta"].get("status", "<missing>")) if mirror else "<missing>"
        folder = str(mirror.get("folder", "<missing>")) if mirror else "<missing>"
        owner = str(mirror["meta"].get("owner", "<missing>")) if mirror else "<missing>"
        alignment = assess_status_alignment(project_status, local_status, owner) if project_item else "missing_project_item"
        task_path = mirror["path"].name if mirror else "<missing>"
        lines.append(
            f"| [#{number}]({issue['url']}) {issue['title']} | {project_status} | {phase} | "
            f"{local_status} | {folder} | {owner} | {alignment} | {task_path} |\n"
        )

    if epic_numbers:
        lines.append("\n## Open Epic Issues\n\n")
        for number in epic_numbers:
            issue = epic_issues[number]
            project_item = project_items.get(number)
            status = project_item["status"] if project_item else "<missing>"
            phase = project_item["phase"] if project_item else "<missing>"
            lines.append(f"- [#{number}]({issue['url']}) {issue['title']} — status: {status}, phase: {phase}\n")

    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Report GitHub Project vs local task sync state.")
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    parser.add_argument("--repo", default=None, help="Optional repo override (owner/repo)")
    parser.add_argument("--project-number", type=int, default=None, help="Optional GitHub project number override")
    parser.add_argument("--project-owner", default=None, help="Optional GitHub project owner override")
    parser.add_argument(
        "--strict-project",
        action="store_true",
        help="Only include tasks with exact project_id match; exclude legacy unscoped tasks.",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "Index" / "github_project_task_sync.md"),
        help="Markdown report output path",
    )
    args = parser.parse_args()

    ensure_project(args.project, allow_none=False)
    repo, configured_number, configured_owner = project_github_config(args.project)
    repo = args.repo or repo
    project_number = args.project_number if args.project_number is not None else configured_number
    owner = args.project_owner or configured_owner or repo.split("/", 1)[0]

    if project_number is None:
        raise SystemExit("[error] GitHub project number is required (set github.project_number or pass --project-number)")

    all_open_issues = load_open_issues(repo)
    issues, epic_issues = split_epics(all_open_issues)
    project_title = load_project_title(project_number, owner)
    project_items = load_project_items(project_number, owner, repo)
    mirrors = load_local_mirrors(args.project, args.strict_project)

    project_url = f"https://github.com/users/{owner}/projects/{project_number}"
    generated_at = utc_iso()
    markdown = render_markdown(
        args.project,
        repo,
        project_url,
        generated_at,
        issues,
        epic_issues,
        project_items,
        mirrors,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    missing_mirrors = [n for n in sorted(issues) if n not in mirrors]
    missing_project_items = [n for n in sorted(issues) if n not in project_items]
    mismatch_numbers = []
    for number in sorted(issues):
        project_item = project_items.get(number)
        mirror = mirrors.get(number)
        if not project_item or not mirror:
            continue
        alignment = assess_status_alignment(
            project_item["status"],
            str(mirror["meta"].get("status", "<missing>")),
            str(mirror["meta"].get("owner", "<missing>")),
        )
        if alignment != "ok":
            mismatch_numbers.append(number)

    print(str(output_path))
    print(f"project: {args.project}")
    print(f"repo: {repo}")
    print(f"project_title: {project_title}")
    print(f"open issues: {len(issues)}")
    print(f"open epics: {len(epic_issues)}")
    print(f"project issue items: {len(project_items)}")
    print(f"local issue mirrors: {len(mirrors)}")
    print(f"missing mirrors: {len(missing_mirrors)}")
    print(f"missing project items: {len(missing_project_items)}")
    print(f"status/owner mismatches: {len(mismatch_numbers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
