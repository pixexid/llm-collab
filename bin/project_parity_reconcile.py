#!/usr/bin/env python3
"""
project_parity_reconcile.py — compare the Amiga parity queue with durable sources.

Usage:
  python3 bin/project_parity_reconcile.py --project amiga
  python3 bin/project_parity_reconcile.py --project amiga --json
  python3 bin/project_parity_reconcile.py --project amiga --fail-on-findings
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
from _helpers import ROOT, TASKS_DIR, all_task_files, display_path, get_project, parse_frontmatter, project_state_dir

QUEUE_FILE_NAME = "issue-queue.json"
DEFAULT_SOURCE_ISSUE = 525
ISSUE_REF_RE = re.compile(r"(?:GH[- #]+|#)(\d+)\b", re.IGNORECASE)
ISSUE_TITLE_RE = re.compile(r"\bGH[- #]+(\d+)\b", re.IGNORECASE)
ISSUE_URL_RE = re.compile(r"https://github\.com/[^/]+/[^/]+/issues/(\d+)")
ISSUE_FILENAME_RE = re.compile(r"\bgh-(\d+)\b", re.IGNORECASE)
CHECKBOX_LINE_RE = re.compile(r"^- \[(?P<mark>[ xX])\]\s+(?P<text>.+)$")

PARITY_KEYWORDS = (
    "/app",
    "/design",
    "accepted design",
    "app route",
    "dashboard",
    "d8",
    "design",
    "drawer",
    "iab",
    "operations",
    "parity",
    "payments",
    "rendered",
    "responsive",
    "runtime",
    "shell",
    "surface",
    "ui",
    "ux",
    "workspace",
)

FOLLOW_UP_KEYWORDS = (
    "accepted remaining",
    "audit needed",
    "blocked gap",
    "create focused issue",
    "deferred",
    "follow-up",
    "follow up",
    "gap",
    "not yet ticketed",
    "out of scope",
    "out-of-scope",
    "remaining lane",
    "remaining lanes",
    "remaining parity",
    "remaining surface",
    "still needs",
    "todo",
)


@dataclass(frozen=True)
class TaskMirror:
    issue: int
    task_id: str | None
    title: str
    status: str | None
    owner: str | None
    folder: str
    path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report parity source drift against the project issue queue.")
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    parser.add_argument("--repo", default=None, help="Optional GitHub repo override (owner/repo).")
    parser.add_argument(
        "--source-issue",
        type=int,
        default=DEFAULT_SOURCE_ISSUE,
        help="GitHub issue that owns the parity map.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Exit non-zero when reconciliation finds unqueued parity work or buried follow-ups.",
    )
    parser.add_argument(
        "--max-followups",
        type=int,
        default=40,
        help="Maximum buried task-doc follow-up snippets to include.",
    )
    return parser.parse_args()


def github_repo(project_id: str) -> str:
    project = get_project(project_id)
    if project is None:
        raise SystemExit(f"[error] Unknown project_id: {project_id!r}")
    github = project.get("github")
    if not isinstance(github, dict) or not github.get("enabled"):
        raise SystemExit(f"[error] GitHub integration is not enabled for project {project_id!r}")
    repo = github.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise SystemExit(f"[error] Project {project_id!r} has invalid github.repo: {repo!r}")
    return repo


def run_gh_json(args: list[str]) -> Any:
    if shutil.which("gh") is None:
        raise RuntimeError("gh CLI not found")
    result = subprocess.run(["gh", *args], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    return json.loads(result.stdout)


def queue_path(project_id: str) -> Path:
    return project_state_dir(project_id) / QUEUE_FILE_NAME


def load_queue(project_id: str) -> dict[str, Any]:
    path = queue_path(project_id)
    if not path.exists():
        return {"project_id": project_id, "lanes": []}
    return json.loads(path.read_text())


def label_names(issue: dict[str, Any]) -> list[str]:
    return [str(label.get("name", "")) for label in issue.get("labels", []) if isinstance(label, dict)]


def is_epic(issue: dict[str, Any]) -> bool:
    return "type:epic" in set(label_names(issue))


def has_parity_signal(*values: str) -> bool:
    haystack = " ".join(values).lower()
    return any(keyword in haystack for keyword in PARITY_KEYWORDS)


def issue_is_parity_relevant(issue: dict[str, Any], source_issue: int) -> bool:
    if int(issue["number"]) == source_issue:
        return True
    return has_parity_signal(str(issue.get("title", "")))


def extract_issue_number(frontmatter: dict[str, Any], body: str, path: Path) -> int | None:
    title_match = ISSUE_TITLE_RE.search(str(frontmatter.get("title", "")))
    if title_match:
        return int(title_match.group(1))
    related_issue = frontmatter.get("related_issue")
    if isinstance(related_issue, str):
        related_match = ISSUE_REF_RE.search(related_issue)
        if related_match:
            return int(related_match.group(1))
    url_match = ISSUE_URL_RE.search(body)
    if url_match:
        return int(url_match.group(1))
    body_match = ISSUE_REF_RE.search(body)
    if body_match:
        return int(body_match.group(1))
    filename_match = ISSUE_FILENAME_RE.search(path.name)
    if filename_match:
        return int(filename_match.group(1))
    return None


def task_mirrors(project_id: str) -> list[TaskMirror]:
    mirrors: list[TaskMirror] = []
    for path in all_task_files():
        frontmatter, body = parse_frontmatter(path.read_text())
        scoped_project = frontmatter.get("project_id")
        if scoped_project not in (project_id, None, "", "null"):
            continue
        issue = extract_issue_number(frontmatter, body, path)
        if issue is None:
            continue
        mirrors.append(
            TaskMirror(
                issue=issue,
                task_id=str(frontmatter.get("task_id")) if frontmatter.get("task_id") else None,
                title=str(frontmatter.get("title", path.stem)),
                status=str(frontmatter.get("status")) if frontmatter.get("status") else None,
                owner=str(frontmatter.get("owner")) if frontmatter.get("owner") else None,
                folder=path.parent.name,
                path=path,
            )
        )
    return mirrors


def source_issue_refs(body: str) -> list[int]:
    refs = sorted({int(match.group(1)) for match in ISSUE_REF_RE.finditer(body)})
    return refs


def source_untracked_audit_gaps(body: str) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("- [ ]"):
            continue
        if ISSUE_REF_RE.search(stripped):
            continue
        if not has_parity_signal(stripped):
            continue
        lowered = stripped.lower()
        if "audit needed" not in lowered and "create focused" not in lowered:
            continue
        gaps.append(
            {
                "line": line_number,
                "snippet": re.sub(r"\s+", " ", stripped).strip()[:320],
            }
        )
    return gaps


def source_checkbox_drift(body: str, all_issue_map: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    drift: list[dict[str, Any]] = []
    for line_number, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        checkbox_match = CHECKBOX_LINE_RE.match(stripped)
        if not checkbox_match:
            continue
        refs = sorted({int(match.group(1)) for match in ISSUE_REF_RE.finditer(stripped)})
        if not refs:
            continue
        known_issues = [all_issue_map[issue] for issue in refs if issue in all_issue_map]
        if not known_issues:
            continue
        checked = checkbox_match.group("mark").lower() == "x"
        open_issues = [issue for issue in known_issues if str(issue.get("state")) == "OPEN"]
        all_known_closed = all(str(issue.get("state")) == "CLOSED" for issue in known_issues)
        if checked and open_issues:
            drift.append(
                {
                    "line": line_number,
                    "kind": "checked_open_issue",
                    "issues": [int(issue["number"]) for issue in open_issues],
                    "snippet": re.sub(r"\s+", " ", stripped).strip()[:320],
                }
            )
        elif not checked and all_known_closed:
            drift.append(
                {
                    "line": line_number,
                    "kind": "unchecked_closed_issue",
                    "issues": [int(issue["number"]) for issue in known_issues],
                    "snippet": re.sub(r"\s+", " ", stripped).strip()[:320],
                }
            )
    return drift


def line_has_followup_signal(line: str) -> bool:
    lowered = line.lower()
    if not any(keyword in lowered for keyword in FOLLOW_UP_KEYWORDS):
        return False
    return has_parity_signal(lowered)


def scan_task_followups(project_id: str, max_followups: int, queued_tasks: set[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in sorted(all_task_files(), key=lambda candidate: candidate.stat().st_mtime, reverse=True):
        frontmatter, body = parse_frontmatter(path.read_text())
        scoped_project = frontmatter.get("project_id")
        if scoped_project not in (project_id, None, "", "null"):
            continue
        task_id = str(frontmatter.get("task_id", ""))
        if task_id in queued_tasks:
            continue
        if not has_parity_signal(str(frontmatter.get("title", "")), body):
            continue
        title = str(frontmatter.get("title", path.stem))
        status = str(frontmatter.get("status", ""))
        for line_number, line in enumerate(body.splitlines(), start=1):
            if not line_has_followup_signal(line):
                continue
            snippet = re.sub(r"\s+", " ", line.strip()).strip("- ")
            if not snippet:
                continue
            findings.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "folder": path.parent.name,
                    "path": display_path(path),
                    "line": line_number,
                    "snippet": snippet[:320],
                }
            )
            if len(findings) >= max_followups:
                return findings
    return findings


def build_report(project_id: str, repo: str, source_issue: int, max_followups: int) -> dict[str, Any]:
    queue = load_queue(project_id)
    queue_lanes = queue.get("lanes", [])
    queued_issues = {
        int(lane["issue"])
        for lane in queue_lanes
        if isinstance(lane, dict) and isinstance(lane.get("issue"), int)
    }
    queued_tasks = {
        str(lane["task_id"])
        for lane in queue_lanes
        if isinstance(lane, dict) and isinstance(lane.get("task_id"), str)
    }
    source_issue_queued = source_issue in queued_issues

    issues = run_gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,title,url,labels,updatedAt",
        ]
    )
    all_issues = run_gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number,title,url,state,labels,updatedAt,closedAt",
        ]
    )
    source = run_gh_json(
        [
            "issue",
            "view",
            str(source_issue),
            "--repo",
            repo,
            "--json",
            "number,title,url,state,body",
        ]
    )

    open_issue_map = {int(issue["number"]): issue for issue in issues}
    all_issue_map = {int(issue["number"]): issue for issue in all_issues}
    mirrors = task_mirrors(project_id)
    mirror_by_issue: dict[int, list[TaskMirror]] = {}
    for mirror in mirrors:
        mirror_by_issue.setdefault(mirror.issue, []).append(mirror)
    source_issue_has_done_reconciliation = any(
        mirror.status == "done" for mirror in mirror_by_issue.get(source_issue, [])
    )
    source_issue_covered = source_issue_queued or source_issue_has_done_reconciliation

    open_parity_issues = [
        issue
        for issue in issues
        if issue_is_parity_relevant(issue, source_issue) and not is_epic(issue)
    ]
    unqueued_open_parity = [
        {
            "issue": int(issue["number"]),
            "title": str(issue["title"]),
            "url": str(issue["url"]),
            "updatedAt": str(issue.get("updatedAt", "")),
            "labels": label_names(issue),
            "local_mirrors": [
                {
                    "task_id": mirror.task_id,
                    "status": mirror.status,
                    "folder": mirror.folder,
                    "path": display_path(mirror.path),
                }
                for mirror in mirror_by_issue.get(int(issue["number"]), [])
            ],
        }
        for issue in open_parity_issues
        if int(issue["number"]) not in queued_issues
    ]

    refs = [issue_number for issue_number in source_issue_refs(str(source.get("body", ""))) if issue_number != source_issue]
    source_refs_open_unqueued = [
        {
            "issue": issue_number,
            "title": str(open_issue_map[issue_number]["title"]),
            "url": str(open_issue_map[issue_number]["url"]),
        }
        for issue_number in refs
        if issue_number in open_issue_map and issue_number not in queued_issues
    ]
    untracked_audit_gaps = source_untracked_audit_gaps(str(source.get("body", "")))
    checkbox_drift = source_checkbox_drift(str(source.get("body", "")), all_issue_map)

    buried_followups = scan_task_followups(project_id, max_followups, queued_tasks)
    unowned_buried_followups = [] if source_issue_covered else buried_followups
    unowned_audit_gaps = [] if source_issue_covered else untracked_audit_gaps
    unowned_checkbox_drift = [] if source_issue_covered else checkbox_drift
    findings = bool(
        unqueued_open_parity
        or source_refs_open_unqueued
        or unowned_checkbox_drift
        or unowned_audit_gaps
        or unowned_buried_followups
    )

    return {
        "ok": not findings,
        "project_id": project_id,
        "repo": repo,
        "source_issue": source_issue,
        "queue_path": display_path(queue_path(project_id)),
        "queued_issues": sorted(queued_issues),
        "queued_tasks": sorted(queued_tasks),
        "source_issue_queued": source_issue_queued,
        "source_issue_has_done_reconciliation": source_issue_has_done_reconciliation,
        "source_issue_covered": source_issue_covered,
        "open_issue_count": len(issues),
        "open_parity_issue_count": len(open_parity_issues),
        "unqueued_open_parity_issues": unqueued_open_parity,
        "source_issue_open_refs_missing_from_queue": source_refs_open_unqueued,
        "source_issue_checkbox_drift": checkbox_drift,
        "unowned_source_issue_checkbox_drift": unowned_checkbox_drift,
        "source_issue_untracked_audit_gaps": untracked_audit_gaps,
        "unowned_source_issue_untracked_audit_gaps": unowned_audit_gaps,
        "buried_task_followups": buried_followups,
        "unowned_buried_task_followups": unowned_buried_followups,
    }


def render_markdown(report: dict[str, Any]) -> str:
    status = "ok" if report["ok"] else "findings"
    lines = [
        f"# Parity Reconciliation ({status})",
        "",
        f"- Project: `{report['project_id']}`",
        f"- Repo: `{report['repo']}`",
        f"- Source issue: `GH-{report['source_issue']}`",
        f"- Queue: `{report['queue_path']}`",
        f"- Queued issues: `{', '.join(f'GH-{issue}' for issue in report['queued_issues']) or 'none'}`",
        f"- Open parity issues detected: `{report['open_parity_issue_count']}`",
        "",
        "## Open Parity Issues Not In Queue",
        "",
    ]

    if report["unqueued_open_parity_issues"]:
        for issue in report["unqueued_open_parity_issues"]:
            mirrors = issue["local_mirrors"]
            mirror_note = ", ".join(
                f"{mirror['task_id']} ({mirror['folder']}/{mirror['status']})"
                for mirror in mirrors
                if mirror["task_id"]
            ) or "no local mirror"
            lines.append(f"- `GH-{issue['issue']}` {issue['title']} ({mirror_note})")
    else:
        lines.append("- none")

    lines.extend(["", "## GH Source Map Open Refs Missing From Queue", ""])
    if report["source_issue_open_refs_missing_from_queue"]:
        for issue in report["source_issue_open_refs_missing_from_queue"]:
            lines.append(f"- `GH-{issue['issue']}` {issue['title']}")
    else:
        lines.append("- none")

    lines.extend(["", "## GH Source Map Checkbox Drift", ""])
    if report["source_issue_covered"]:
        lines.append(
            f"Source issue `GH-{report['source_issue']}` has an active or completed reconciliation lane, so checkbox drift is covered until that lane updates the source map or records an intentional exception."
        )
        lines.append("")
    if report["source_issue_checkbox_drift"]:
        for drift in report["source_issue_checkbox_drift"]:
            issues = ", ".join(f"GH-{issue}" for issue in drift["issues"])
            lines.append(f"- line {drift['line']} ({drift['kind']}, {issues}) — {drift['snippet']}")
    else:
        lines.append("- none")

    lines.extend(["", "## GH Source Map Audit Gaps Without Issue Refs", ""])
    if report["source_issue_covered"]:
        lines.append(
            f"Source issue `GH-{report['source_issue']}` has an active or completed reconciliation lane, so these GH-less audit gaps are covered until it creates focused issues or records closure evidence."
        )
        lines.append("")
    if report["source_issue_untracked_audit_gaps"]:
        for gap in report["source_issue_untracked_audit_gaps"]:
            lines.append(f"- line {gap['line']} — {gap['snippet']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Buried Task Follow-Up Signals", ""])
    if report["source_issue_covered"]:
        lines.append(
            f"Source issue `GH-{report['source_issue']}` has an active or completed reconciliation lane, so these candidates are covered by the recorded reconciliation disposition."
        )
        lines.append("")
    if report["buried_task_followups"]:
        for finding in report["buried_task_followups"]:
            location = f"{finding['path']}:{finding['line']}"
            lines.append(
                f"- `{finding['task_id']}` ({finding['folder']}/{finding['status']}) "
                f"{location} — {finding['snippet']}"
            )
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    repo = args.repo or github_repo(args.project)
    try:
        report = build_report(args.project, repo, args.source_issue, args.max_followups)
    except RuntimeError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report), end="")

    if args.fail_on_findings and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
