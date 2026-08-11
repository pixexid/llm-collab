from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any

from _helpers import get_project


RECOGNIZED_ISSUE_STATE_LABELS = (
    "state:active",
    "state:blocked",
    "state:parked",
)
CONTRACT_REQUIRED_EXCLUDE_LABELS = (
    "epic",
    "state:parked",
)
DEFAULT_CONFIGURED_EXCLUDE_LABELS = (
    "type:epic",
    "wontfix",
    "duplicate",
    "invalid",
    "question",
    "status:deferred",
)

GH_PAGE_SIZE = 100
GH_PAGE_TIMEOUT_SECONDS = 15.0
GH_PAGE_MAX_OUTPUT_BYTES = 1 << 20
GH_TOTAL_MAX_OUTPUT_BYTES = 16 << 20
GH_MAX_PAGES = 100
GH_MAX_ITEMS = GH_PAGE_SIZE * GH_MAX_PAGES
GH_EXACT_ISSUE_ATTEMPTS = 2
GH_RETRY_DELAY_SECONDS = 0.25


class BacklogUnavailable(RuntimeError):
    pass


class IssueStatePolicyError(ValueError):
    def __init__(self, violations: list[dict[str, Any]], *, total_examined: int) -> None:
        self.violations = violations
        self.total_examined = total_examined
        issues = ", ".join(f"GH-{item['issue']}" for item in violations)
        super().__init__(f"invalid issue state labels on {issues}")


@dataclass(frozen=True)
class IssuePolicy:
    classification: str
    issue_state: str | None
    reason: str
    state_labels: tuple[str, ...]
    matched_exclusion: str | None = None


@dataclass(frozen=True)
class BacklogIssue:
    number: int
    title: str
    labels: tuple[str, ...]
    url: str | None = None
    priority_label: str | None = None
    priority_rank: int | None = None
    issue_state: str | None = None
    policy_reason: str | None = None


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
    configured_exclusions = _configured_exclude_labels(backlog)
    require_any_label = backlog.get("require_any_label", [])
    priority_labels = backlog.get("priority_labels", [])

    return {
        "enabled": True,
        "repo": repo,
        "exclude_labels": _ordered_unique(
            (*CONTRACT_REQUIRED_EXCLUDE_LABELS, *configured_exclusions)
        ),
        "require_any_label": _string_list(require_any_label, default=()),
        "priority_labels": _string_list(priority_labels, default=()),
    }


def classify_issue_labels(
    labels: tuple[str, ...] | list[str],
    exclude_patterns: tuple[str, ...] | list[str],
) -> IssuePolicy:
    """Classify one label set without I/O or project-specific lookups."""
    normalized = tuple(label.strip().lower() for label in labels)
    state_labels = tuple(label for label in normalized if label.startswith("state:"))
    if not state_labels:
        return IssuePolicy("invalid", None, "missing_state", state_labels)
    if len(state_labels) != 1:
        return IssuePolicy("invalid", None, "multiple_states", state_labels)

    issue_state = state_labels[0]
    if issue_state not in RECOGNIZED_ISSUE_STATE_LABELS:
        return IssuePolicy("invalid", None, "unknown_state", state_labels)

    patterns = tuple(pattern.strip().lower() for pattern in exclude_patterns)
    for pattern in patterns:
        if any(fnmatchcase(label, pattern) for label in normalized):
            return IssuePolicy(
                "excluded",
                issue_state,
                f"excluded:{pattern}",
                state_labels,
                pattern,
            )
    if issue_state == "state:blocked":
        return IssuePolicy("blocked", issue_state, "github:state:blocked", state_labels)
    return IssuePolicy("active", issue_state, "state:active", state_labels)


def eligible_open_issues(project_id: str) -> list[BacklogIssue]:
    config = project_backlog_config(project_id)
    if not config.get("enabled"):
        return []

    raw_issues = load_open_github_issues(str(config["repo"]))
    exclude_patterns = tuple(str(label).lower() for label in config["exclude_labels"])
    require_patterns = tuple(str(label).lower() for label in config["require_any_label"])
    priority_labels = tuple(str(label) for label in config["priority_labels"])
    priority_patterns = tuple(label.lower() for label in priority_labels)

    candidates: list[BacklogIssue] = []
    violations: list[dict[str, Any]] = []
    for raw_issue in raw_issues:
        issue = parse_backlog_issue(raw_issue)
        if issue is None:
            raise BacklogUnavailable(
                f"GitHub returned an issue with invalid number/title shape for {config['repo']}"
            )
        policy = classify_issue_labels(issue.labels, exclude_patterns)
        if policy.classification == "invalid":
            violations.append(issue_state_violation(issue.number, policy))
            continue
        if policy.classification == "excluded":
            continue
        label_names = tuple(label.lower() for label in issue.labels)
        if require_patterns and not any(_matches_any(label, require_patterns) for label in label_names):
            continue
        priority_index = next(
            (
                index
                for index, pattern in enumerate(priority_patterns)
                if pattern in label_names
            ),
            None,
        )
        issue = replace(
            issue,
            issue_state=policy.issue_state,
            policy_reason=policy.reason,
            priority_label=(priority_labels[priority_index] if priority_index is not None else None),
            priority_rank=(priority_index + 1 if priority_index is not None else None),
        )
        candidates.append(issue)

    if violations:
        raise IssueStatePolicyError(violations, total_examined=len(raw_issues))
    return sorted(
        candidates,
        key=lambda issue: (
            issue.priority_rank is None,
            issue.priority_rank or 0,
            issue.number,
        ),
    )


def exact_issue_policy(project_id: str, issue_number: int) -> tuple[str, IssuePolicy] | None:
    config = project_backlog_config(project_id)
    if not config.get("enabled"):
        return None
    raw_issue = load_github_issue(str(config["repo"]), issue_number)
    issue = parse_backlog_issue(raw_issue)
    if issue is None or issue.number != issue_number:
        raise BacklogUnavailable(
            f"GitHub returned invalid data for exact issue GH-{issue_number} in {config['repo']}"
        )
    return str(config["repo"]), classify_issue_labels(issue.labels, config["exclude_labels"])


def load_open_github_issues(repo: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    total_bytes = 0
    total_items = 0
    for page in range(1, GH_MAX_PAGES + 1):
        payload, response_bytes = _gh_json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{repo}/issues",
                "-f",
                "state=open",
                "-f",
                f"per_page={GH_PAGE_SIZE}",
                "-f",
                f"page={page}",
            ],
            context=f"open issues page {page} for {repo}",
        )
        total_bytes += response_bytes
        if total_bytes > GH_TOTAL_MAX_OUTPUT_BYTES:
            raise BacklogUnavailable(
                f"open issue enumeration for {repo} exceeds cumulative "
                f"{GH_TOTAL_MAX_OUTPUT_BYTES}-byte budget"
            )
        if not isinstance(payload, list):
            raise BacklogUnavailable(f"gh returned non-list issue page {page} for {repo}")
        if any(not isinstance(item, dict) for item in payload):
            raise BacklogUnavailable(
                f"gh returned a non-object item on issue page {page} for {repo}"
            )
        total_items += len(payload)
        if total_items > GH_MAX_ITEMS:
            raise BacklogUnavailable(
                f"open issue enumeration for {repo} exceeds cumulative "
                f"{GH_MAX_ITEMS}-item budget"
            )
        page_issues = [item for item in payload if isinstance(item, dict) and "pull_request" not in item]
        issues.extend(page_issues)
        if len(payload) < GH_PAGE_SIZE:
            return issues
    raise BacklogUnavailable(
        f"open issue enumeration for {repo} reached {GH_MAX_PAGES} full pages "
        "without proving exhaustion"
    )


def load_github_issue(repo: str, issue_number: int) -> dict[str, Any]:
    last_error: BacklogUnavailable | None = None
    for attempt in range(1, GH_EXACT_ISSUE_ATTEMPTS + 1):
        try:
            payload, _ = _gh_json(
                ["gh", "api", "--method", "GET", f"repos/{repo}/issues/{issue_number}"],
                context=f"exact issue GH-{issue_number} for {repo}",
            )
            if not isinstance(payload, dict):
                raise BacklogUnavailable(
                    f"gh returned non-object exact issue GH-{issue_number} for {repo}"
                )
            return payload
        except BacklogUnavailable as exc:
            last_error = exc
            if attempt < GH_EXACT_ISSUE_ATTEMPTS:
                time.sleep(GH_RETRY_DELAY_SECONDS)
    assert last_error is not None
    raise last_error


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

    url = raw_issue.get("html_url", raw_issue.get("url"))
    return BacklogIssue(
        number=number,
        title=title,
        labels=tuple(labels),
        url=url if isinstance(url, str) else None,
    )


def issue_state_violation(issue_number: int, policy: IssuePolicy) -> dict[str, Any]:
    return {
        "issue": issue_number,
        "found": list(policy.state_labels),
        "reason": policy.reason,
        "expected_one_of": list(RECOGNIZED_ISSUE_STATE_LABELS),
    }


def _configured_exclude_labels(backlog: dict[str, Any]) -> list[str]:
    if "exclude_labels" not in backlog:
        return list(DEFAULT_CONFIGURED_EXCLUDE_LABELS)
    value = backlog["exclude_labels"]
    if not isinstance(value, list):
        raise ValueError("github.backlog.exclude_labels must be a list of non-empty strings")
    configured: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "github.backlog.exclude_labels must contain only non-empty strings; "
                f"entry {index} is {item!r}"
            )
        configured.append(item.strip())
    return configured


def _ordered_unique(values: tuple[str, ...]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.lower()
        if key not in seen:
            unique.append(value)
            seen.add(key)
    return unique


def _string_list(value: Any, *, default: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    return [item for item in value if isinstance(item, str)]


def _matches_any(label: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(label, pattern) for pattern in patterns)


def _gh_json(command: list[str], *, context: str) -> tuple[Any, int]:
    result = _run_gh_bounded(command)
    response_bytes = len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
    if response_bytes > GH_PAGE_MAX_OUTPUT_BYTES:
        raise BacklogUnavailable(
            f"{context} exceeds per-call {GH_PAGE_MAX_OUTPUT_BYTES}-byte output bound"
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BacklogUnavailable(detail or f"gh api failed for {context}")
    try:
        return json.loads(result.stdout), response_bytes
    except json.JSONDecodeError as exc:
        raise BacklogUnavailable(f"gh returned invalid JSON for {context}: {exc}") from exc


def _run_gh_bounded(command: list[str]) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if process.stdout is None or process.stderr is None:
            raise BacklogUnavailable(f"{' '.join(command)} returned no output pipes")
        outputs = {process.stdout: bytearray(), process.stderr: bytearray()}
        total = 0
        deadline = time.monotonic() + GH_PAGE_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            for stream in outputs:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                events = selector.select(max(0.0, remaining))
                if remaining <= 0 or not events:
                    raise BacklogUnavailable(
                        f"{' '.join(command)} exceeded {GH_PAGE_TIMEOUT_SECONDS}s"
                    )
                for key, _ in events:
                    stream = key.fileobj
                    chunk = os.read(
                        stream.fileno(),
                        min(64 * 1024, GH_PAGE_MAX_OUTPUT_BYTES + 1 - total),
                    )
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    outputs[stream].extend(chunk)
                    total += len(chunk)
                    if total > GH_PAGE_MAX_OUTPUT_BYTES:
                        raise BacklogUnavailable(
                            f"{' '.join(command)} output exceeds "
                            f"{GH_PAGE_MAX_OUTPUT_BYTES} bytes"
                        )
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        try:
            stdout = bytes(outputs[process.stdout]).decode("utf-8")
            stderr = bytes(outputs[process.stderr]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BacklogUnavailable(f"{' '.join(command)} returned non-UTF-8 output") from exc
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    except BacklogUnavailable:
        raise
    except subprocess.TimeoutExpired as exc:
        raise BacklogUnavailable(
            f"{' '.join(command)} exceeded {GH_PAGE_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise BacklogUnavailable(f"gh api unavailable: {exc}") from exc
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
