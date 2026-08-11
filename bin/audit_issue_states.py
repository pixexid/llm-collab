#!/usr/bin/env python3
"""Audit every open issue's state-label vocabulary for one registered project."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json

import _backlog


class IssueAuditUnavailable(RuntimeError):
    def __init__(self, repository: str, detail: str) -> None:
        self.repository = repository
        super().__init__(detail)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def audit_issue_states(project_id: str) -> dict:
    config = _backlog.project_backlog_config(project_id)
    if not config.get("enabled"):
        return {
            "project_id": project_id,
            "repository": None,
            "open_issues_examined": 0,
            "complete": True,
            "state_label_violations": [],
            "parked_issues": [],
        }

    repository = str(config["repo"])
    try:
        raw_issues = _backlog.load_open_github_issues(repository)
    except _backlog.BacklogUnavailable as error:
        raise IssueAuditUnavailable(repository, str(error)) from error
    violations: list[dict] = []
    parked: list[int] = []
    for raw_issue in raw_issues:
        issue = _backlog.parse_backlog_issue(raw_issue)
        if issue is None:
            raise IssueAuditUnavailable(
                repository,
                f"GitHub returned an issue with invalid number/title shape for {repository}",
            )
        policy = _backlog.classify_issue_labels(issue.labels, config["exclude_labels"])
        if policy.classification == "invalid":
            violations.append(_backlog.issue_state_violation(issue.number, policy))
        elif policy.issue_state == "state:parked":
            parked.append(issue.number)
    return {
        "project_id": project_id,
        "repository": repository,
        "open_issues_examined": len(raw_issues),
        "complete": True,
        "state_label_violations": violations,
        "parked_issues": parked,
    }


def render_text(result: dict) -> str:
    lines = [
        f"repository: {result.get('repository')}",
        f"open_issues_examined: {result.get('open_issues_examined', 0)}",
        f"complete: {str(bool(result.get('complete'))).lower()}",
        f"state_label_violations: {len(result.get('state_label_violations', []))}",
    ]
    expected = "/".join(_backlog.RECOGNIZED_ISSUE_STATE_LABELS)
    for violation in result.get("state_label_violations", []):
        found = ", ".join(violation["found"])
        lines.append(
            f"GH-{violation['issue']}: expected exactly one of {expected}; found [{found}]"
        )
    parked = result.get("parked_issues", [])
    if parked:
        lines.append("parked_issues: " + ", ".join(f"GH-{issue}" for issue in parked))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        result = audit_issue_states(args.project)
    except (ValueError, IssueAuditUnavailable) as error:
        result = {
            "project_id": args.project,
            "repository": (
                error.repository if isinstance(error, IssueAuditUnavailable) else None
            ),
            "open_issues_examined": 0,
            "complete": False,
            "state_label_violations": [],
            "parked_issues": [],
            "error": str(error),
        }
        if args.json_output:
            print(json.dumps(result, indent=2))
        else:
            print(render_text(result), file=sys.stderr)
            print(f"error: {error}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2) if args.json_output else render_text(result))
    return 1 if result["state_label_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
