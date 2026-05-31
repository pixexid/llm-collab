from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from _helpers import get_project


DEFAULT_EXCLUDE_LABELS = (
    "type:epic",
    "wontfix",
    "duplicate",
    "invalid",
    "question",
    "status:deferred",
)


class BacklogUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BacklogIssue:
    number: int
    title: str
    labels: tuple[str, ...]
    url: str | None = None


def project_backlog_config(project_id: str) -> dict[str, Any]:
    project = get_project(project_id)
    if project is None:
        raise ValueError(f"unknown project_id: {project_id!r}")

    github = project.get("github")
    if not isinstance(github, dict) or not github.get("enabled"):
        return {"enabled": False}

    repo = github.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise ValueError(f"project {project_id!r} has invalid github.repo: {repo!r}")

    raw_backlog = github.get("backlog", {})
    backlog = raw_backlog if isinstance(raw_backlog, dict) else {}
    exclude_labels = backlog.get("exclude_labels", DEFAULT_EXCLUDE_LABELS)
    require_any_label = backlog.get("require_any_label", [])

    return {
        "enabled": True,
        "repo": repo,
        "exclude_labels": _string_list(exclude_labels, default=DEFAULT_EXCLUDE_LABELS),
        "require_any_label": _string_list(require_any_label, default=()),
    }


def eligible_open_issues(project_id: str) -> list[BacklogIssue]:
    config = project_backlog_config(project_id)
    if not config.get("enabled"):
        return []

    raw_issues = load_open_github_issues(str(config["repo"]))
    exclude_patterns = tuple(str(label).lower() for label in config["exclude_labels"])
    require_patterns = tuple(str(label).lower() for label in config["require_any_label"])

    eligible: list[BacklogIssue] = []
    for raw_issue in raw_issues:
        issue = parse_backlog_issue(raw_issue)
        if issue is None:
            continue
        label_names = tuple(label.lower() for label in issue.labels)
        if any(_matches_any(label, exclude_patterns) for label in label_names):
            continue
        if require_patterns and not any(_matches_any(label, require_patterns) for label in label_names):
            continue
        eligible.append(issue)

    return sorted(eligible, key=lambda issue: issue.number)


def load_open_github_issues(repo: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,labels,url",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BacklogUnavailable(detail or f"gh issue list failed for {repo}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise BacklogUnavailable(f"gh returned invalid JSON for {repo}: {exc}") from exc
    if not isinstance(payload, list):
        raise BacklogUnavailable(f"gh returned non-list issue payload for {repo}")
    return [item for item in payload if isinstance(item, dict)]


def parse_backlog_issue(raw_issue: dict[str, Any]) -> BacklogIssue | None:
    number = raw_issue.get("number")
    title = raw_issue.get("title")
    if not isinstance(number, int) or not isinstance(title, str):
        return None

    labels: list[str] = []
    raw_labels = raw_issue.get("labels", [])
    if isinstance(raw_labels, list):
        for label in raw_labels:
            if isinstance(label, dict) and isinstance(label.get("name"), str):
                labels.append(label["name"])
            elif isinstance(label, str):
                labels.append(label)

    url = raw_issue.get("url")
    return BacklogIssue(
        number=number,
        title=title,
        labels=tuple(labels),
        url=url if isinstance(url, str) else None,
    )


def _string_list(value: Any, *, default: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    return [item for item in value if isinstance(item, str)]


def _matches_any(label: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(label, pattern) for pattern in patterns)
