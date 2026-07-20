#!/usr/bin/env python3
"""
project_issue_queue.py — show/validate/render a project-level ordered issue queue.

Usage:
  python3 bin/project_issue_queue.py show --project amiga
  python3 bin/project_issue_queue.py validate --project amiga
  python3 bin/project_issue_queue.py sync-markdown --project amiga
  python3 bin/project_issue_queue.py archive-complete --project amiga
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import hashlib
import json
import re

sys.path.insert(0, str(Path(__file__).parent))
import _backlog
from _helpers import (
    all_task_files,
    display_path,
    find_task_by_id,
    get_project,
    parse_frontmatter,
    project_state_dir,
    utc_iso,
    write_file,
)
from task_contract import validate_direct_app_policy

QUEUE_STATES = {"ready", "queued", "blocked", "active", "review", "done"}
QUEUE_FILE_NAME = "issue-queue.json"
MARKDOWN_FILE_NAME = "issue-queue.md"
HISTORY_DIR_NAME = "history"
DIRECT_APP_BLOCKER_PREFIX = "ui_ux.direct_app_only: "
ISSUE_RE = re.compile(r"\bGH[- #]+(\d+)\b", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show, validate, or render a project issue queue.")
    parser.add_argument("command", choices=("show", "validate", "sync-markdown", "archive-complete", "reconcile"))
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit machine-readable JSON.")
    parser.add_argument("--write", action="store_true", help="Write reconciled queue projection to disk.")
    parser.add_argument(
        "--require-clean-backlog",
        action="store_true",
        help="Require GitHub-backed queue/backlog consistency. This is the default for validate.",
    )
    parser.add_argument(
        "--skip-backlog-check",
        action="store_true",
        help="Skip GitHub backlog consistency checks for offline/manual validation.",
    )
    return parser.parse_args()


def queue_dir(project_id: str) -> Path:
    return project_state_dir(project_id)


def queue_json_path(project_id: str) -> Path:
    return queue_dir(project_id) / QUEUE_FILE_NAME


def queue_markdown_path(project_id: str) -> Path:
    return queue_dir(project_id) / MARKDOWN_FILE_NAME


def queue_history_dir(project_id: str) -> Path:
    return queue_dir(project_id) / HISTORY_DIR_NAME


def queue_exists(project_id: str) -> bool:
    return queue_json_path(project_id).exists()


def load_queue(project_id: str) -> dict:
    project = get_project(project_id)
    if project is None:
        raise SystemExit(f"[error] Unknown project_id: {project_id!r}")
    path = queue_json_path(project_id)
    if not path.exists():
        raise SystemExit(f"[error] Queue file not found: {path}")
    return json.loads(path.read_text())


def save_queue(project_id: str, payload: dict) -> None:
    write_file(queue_json_path(project_id), json.dumps(payload, indent=2) + "\n")


def extract_issue_number(frontmatter: dict, task_path: Path) -> int | None:
    title = str(frontmatter.get("title", ""))
    title_match = ISSUE_RE.search(title)
    if title_match:
        return int(title_match.group(1))
    related_issue = frontmatter.get("related_issue")
    if isinstance(related_issue, str):
        related_match = ISSUE_RE.search(related_issue)
        if related_match:
            return int(related_match.group(1))
    filename_match = re.search(r"\bgh-(\d+)\b", task_path.name, re.IGNORECASE)
    if filename_match:
        return int(filename_match.group(1))
    return None


def project_task_snapshot(project_id: str) -> tuple[dict[int, list[dict]], set[str]]:
    mirrors: dict[int, list[dict]] = {}
    statuses_by_task: dict[str, list[object]] = {}
    for task_path in all_task_files():
        frontmatter, _ = parse_frontmatter(task_path.read_text())
        scoped_project = frontmatter.get("project_id")
        if scoped_project != project_id:
            continue
        task_id = frontmatter.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        status = frontmatter.get("status")
        statuses_by_task.setdefault(task_id, []).append(status)
        issue = extract_issue_number(frontmatter, task_path)
        if issue is None:
            continue
        mirrors.setdefault(issue, []).append(
            {
                "path": task_path,
                "frontmatter": frontmatter,
                "task_id": task_id,
                "status": status if isinstance(status, str) else "open",
            }
        )
    completed_task_ids = {
        task_id
        for task_id, statuses in statuses_by_task.items()
        if statuses and all(status == "done" for status in statuses)
    }
    return mirrors, completed_task_ids


def mirror_sort_key(mirror: dict) -> tuple[int, str]:
    status = mirror.get("status")
    status_rank = {
        "in_progress": 0,
        "review": 1,
        "open": 2,
        "blocked": 3,
        "done": 4,
    }.get(status, 5)
    return status_rank, str(mirror["path"])


def canonical_mirror(mirrors: list[dict]) -> dict:
    return sorted(mirrors, key=mirror_sort_key)[0]


def normalize_depends(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def completed_tasks(payload: dict) -> set[str]:
    completed = {
        str(entry.get("task_id"))
        for entry in payload.get("completed_recently", [])
        if (
            isinstance(entry, dict)
            and entry.get("task_id")
            and entry.get("status") == "done"
        )
    }
    for lane in payload.get("lanes", []):
        if isinstance(lane, dict) and lane.get("task_status") == "done" and lane.get("task_id"):
            completed.add(str(lane["task_id"]))
    return completed


def completed_issues(payload: dict) -> set[int]:
    completed: set[int] = set()
    for entry in payload.get("completed_recently", []):
        if (
            isinstance(entry, dict)
            and entry.get("status") == "done"
            and isinstance(entry.get("issue"), int)
        ):
            completed.add(entry["issue"])
    for lane in payload.get("lanes", []):
        if (
            isinstance(lane, dict)
            and lane.get("task_status") == "done"
            and isinstance(lane.get("issue"), int)
        ):
            completed.add(lane["issue"])
    return completed


def dependency_is_satisfied(task_id: str, completed_task_ids: set[str]) -> bool:
    return task_id in completed_task_ids


def dependency_blockers(depends_on: list[str], completed_task_ids: set[str]) -> list[str]:
    return [task_id for task_id in depends_on if not dependency_is_satisfied(task_id, completed_task_ids)]


def blocker_is_satisfied(blocker: object, completed_task_ids: set[str], completed_issue_ids: set[int]) -> bool:
    if not isinstance(blocker, str):
        return False
    stripped = blocker.strip()
    if stripped.startswith(DIRECT_APP_BLOCKER_PREFIX):
        return False
    if stripped in completed_task_ids:
        return True
    issue_match = re.fullmatch(r"GH[- #]*(\d+)", stripped, re.IGNORECASE)
    if issue_match and int(issue_match.group(1)) in completed_issue_ids:
        return True
    if "queue order" not in stripped.lower():
        return False
    if any(task_id in stripped for task_id in completed_task_ids):
        return True
    return any(re.search(rf"\bGH[- #]*{issue}\b", stripped, re.IGNORECASE) for issue in completed_issue_ids)


def unblock_satisfied_lanes(payload: dict) -> None:
    completed_task_ids = completed_tasks(payload)
    completed_issue_ids = completed_issues(payload)

    for lane in payload.get("lanes", []):
        if not isinstance(lane, dict) or lane.get("queue_state") != "blocked":
            continue

        depends_on = normalize_depends(lane.get("depends_on"))
        if depends_on and not all(dependency_is_satisfied(dep, completed_task_ids) for dep in depends_on):
            continue

        blockers = normalize_depends(lane.get("blocked_by"))
        remaining_blockers = [
            blocker
            for blocker in blockers
            if not blocker_is_satisfied(blocker, completed_task_ids, completed_issue_ids)
        ]
        if remaining_blockers:
            lane["blocked_by"] = remaining_blockers
            continue

        lane["queue_state"] = "queued"
        lane["blocked_by"] = []


def validate_queue(project_id: str, payload: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if payload.get("project_id") != project_id:
        errors.append(
            f"queue project_id mismatch: expected {project_id!r}, found {payload.get('project_id')!r}"
        )

    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        errors.append("queue must contain a 'lanes' array")
        return errors, warnings

    seen_orders: set[int] = set()
    seen_issues: set[int] = set()
    seen_tasks: set[str] = set()

    ready_orders: list[int] = []
    queue_orders: list[int] = []

    for lane in lanes:
        order = lane.get("order")
        issue = lane.get("issue")
        task_id = lane.get("task_id")
        queue_state = lane.get("queue_state")
        owner = lane.get("owner")
        task_status = lane.get("task_status")
        tier = lane.get("tier")
        depends_on = normalize_depends(lane.get("depends_on"))

        if not isinstance(order, int):
            errors.append(f"lane has non-integer order: {lane!r}")
            continue
        if order in seen_orders:
            errors.append(f"duplicate queue order: {order}")
        seen_orders.add(order)
        queue_orders.append(order)

        if not isinstance(issue, int):
            errors.append(f"lane {order} has non-integer issue: {issue!r}")
        elif issue in seen_issues:
            errors.append(f"duplicate issue in queue: GH-{issue}")
        else:
            seen_issues.add(issue)

        if not isinstance(task_id, str):
            errors.append(f"lane {order} missing string task_id")
            continue
        if task_id in seen_tasks:
            errors.append(f"duplicate task_id in queue: {task_id}")
        seen_tasks.add(task_id)

        if queue_state not in QUEUE_STATES:
            errors.append(f"lane {order} has invalid queue_state {queue_state!r}")
        elif queue_state == "ready":
            ready_orders.append(order)

        task_path = find_task_by_id(task_id)
        if task_path is None:
            errors.append(f"lane {order} task mirror not found: {task_id}")
            continue

        frontmatter, _ = parse_frontmatter(task_path.read_text())
        task_project_id = frontmatter.get("project_id")
        if task_project_id != project_id:
            errors.append(
                f"lane {order} project mismatch for {task_id}: "
                f"queue {project_id!r} vs task {task_project_id!r}"
            )
        task_issue = extract_issue_number(frontmatter, task_path)
        if task_issue != issue:
            errors.append(
                f"lane {order} issue/task mismatch: queue GH-{issue} vs task mirror GH-{task_issue}"
            )

        if owner != frontmatter.get("owner"):
            errors.append(
                f"lane {order} owner mismatch for {task_id}: queue {owner!r} vs task {frontmatter.get('owner')!r}"
            )

        if task_status != frontmatter.get("status"):
            errors.append(
                f"lane {order} task_status mismatch for {task_id}: queue {task_status!r} vs task {frontmatter.get('status')!r}"
            )
        if task_project_id == project_id and task_status == frontmatter.get("status"):
            direct_app_errors, _ = validate_direct_app_policy(frontmatter)
            errors.extend(
                f"lane {order} task {task_id}: {error}"
                for error in direct_app_errors
            )

        frontmatter_tier = frontmatter.get("tier")
        if tier is not None and frontmatter_tier != tier:
            errors.append(
                f"lane {order} tier mismatch for {task_id}: queue {tier!r} vs task {frontmatter_tier!r}"
            )

        task_depends = normalize_depends(frontmatter.get("depends_on"))
        if task_depends != depends_on:
            errors.append(
                f"lane {order} depends_on mismatch for {task_id}: queue {depends_on!r} vs task {task_depends!r}"
            )

        if queue_state == "blocked" and not lane.get("blocked_by") and not depends_on:
            warnings.append(f"lane {order} is blocked but has no blocked_by/depends_on evidence")

    if queue_orders and sorted(queue_orders) != list(range(1, len(queue_orders) + 1)):
        errors.append(f"queue orders must be contiguous starting at 1, found {sorted(queue_orders)!r}")

    if len(ready_orders) > 1:
        errors.append(f"queue has multiple ready lanes: orders {sorted(ready_orders)!r}")
    elif ready_orders and ready_orders[0] != min(queue_orders):
        warnings.append(
            f"ready lane order {ready_orders[0]} is not the earliest queue order {min(queue_orders)}"
        )

    return errors, warnings


def backlog_consistency_errors(project_id: str, payload: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        eligible = _backlog.eligible_open_issues(project_id)
    except _backlog.BacklogUnavailable as exc:
        errors.append(f"GitHub backlog state unknown for {project_id}: {exc}")
        return errors, warnings
    except ValueError as exc:
        errors.append(str(exc))
        return errors, warnings

    queued_issues = {
        lane.get("issue")
        for lane in payload.get("lanes", [])
        if isinstance(lane, dict) and isinstance(lane.get("issue"), int)
    }
    missing = [issue for issue in eligible if issue.number not in queued_issues]
    if missing:
        formatted = ", ".join(f"GH-{issue.number}" for issue in missing)
        errors.append(
            f"queue/backlog drift: eligible open GitHub issue(s) missing from issue-queue.json: {formatted}"
        )
    elif not payload.get("lanes"):
        warnings.append("queue empty confirmed against GitHub backlog")

    return errors, warnings


def lane_reason(lane: dict) -> str:
    if lane.get("queue_state") == "blocked":
        blockers = normalize_depends(lane.get("blocked_by"))
        return "blocked_by:" + ",".join(blockers or normalize_depends(lane.get("depends_on")))
    if lane.get("task_status") == "done":
        return "done"
    if lane.get("needs_refinement"):
        return "needs_refinement"
    if lane.get("needs_acceptance"):
        return "needs_acceptance"
    return str(lane.get("queue_state", "unknown"))


def lane_next_action(lane: dict) -> str:
    if lane.get("needs_refinement"):
        return "refine"
    if lane.get("needs_acceptance"):
        return "accept"
    if lane.get("queue_state") == "ready":
        return "activate"
    return lane_reason(lane)


def no_ready_lane_errors(project_id: str, payload: dict) -> tuple[list[str], list[str]]:
    lanes = [lane for lane in payload.get("lanes", []) if isinstance(lane, dict)]
    if next_ready_lane(payload) is not None:
        return [], []
    if any(lane.get("queue_state") in {"active", "review"} for lane in lanes):
        return [], []
    if not lanes:
        return [], []
    genuinely_blocked = [
        lane
        for lane in lanes
        if lane.get("queue_state") == "blocked" and (lane.get("blocked_by") or lane.get("depends_on"))
    ]
    if len(genuinely_blocked) == len(lanes):
        return [], []
    diagnostics = ", ".join(
        f"GH-{lane.get('issue')}:{lane_reason(lane)}" for lane in sorted(lanes, key=lambda item: item.get("order", 0))
    )
    return [f"queue has eligible lanes but no ready lane for {project_id}: {diagnostics}"], []


def reconciliation_input_hash(project_id: str, issues: list[_backlog.BacklogIssue], mirrors: dict[int, list[dict]]) -> str:
    task_inputs = []
    for issue, issue_mirrors in sorted(mirrors.items()):
        for mirror in sorted(issue_mirrors, key=lambda item: str(item["path"])):
            frontmatter = mirror["frontmatter"]
            task_inputs.append(
                {
                    "issue": issue,
                    "task_id": mirror["task_id"],
                    "status": frontmatter.get("status"),
                    "owner": frontmatter.get("owner"),
                    "depends_on": normalize_depends(frontmatter.get("depends_on")),
                    "refined_by": frontmatter.get("refined_by"),
                    "skip_refinement": frontmatter.get("skip_refinement"),
                    "accepted_by": frontmatter.get("accepted_by"),
                    "lane_type": frontmatter.get("lane_type"),
                    "related_paths": frontmatter.get("related_paths"),
                    "dependency_materialization_gate": frontmatter.get(
                        "dependency_materialization_gate"
                    ),
                    "required_dependency_artifacts": frontmatter.get(
                        "required_dependency_artifacts"
                    ),
                    "direct_app_legacy_maintenance": frontmatter.get(
                        "direct_app_legacy_maintenance"
                    ),
                    "direct_app_legacy_maintenance_approved_by": frontmatter.get(
                        "direct_app_legacy_maintenance_approved_by"
                    ),
                    "direct_app_legacy_maintenance_reason": frontmatter.get(
                        "direct_app_legacy_maintenance_reason"
                    ),
                    "path": display_path(mirror["path"]),
                }
            )
    project = get_project(project_id)
    ui_ux = project.get("ui_ux") if isinstance(project, dict) else None
    direct_app_configured = isinstance(ui_ux, dict) and "direct_app_only" in ui_ux
    raw = json.dumps(
        {
            "project_id": project_id,
            "direct_app_only_configured": direct_app_configured,
            "direct_app_only": ui_ux.get("direct_app_only") if direct_app_configured else None,
            "issues": [{"number": issue.number, "title": issue.title, "labels": issue.labels} for issue in issues],
            "tasks": task_inputs,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reconcile_queue(project_id: str) -> dict:
    try:
        eligible_issues = _backlog.eligible_open_issues(project_id)
    except _backlog.BacklogUnavailable as exc:
        return {
            "ok": False,
            "backlog": "unknown",
            "project_id": project_id,
            "reason": str(exc),
            "projection": None,
        }

    mirrors_by_issue, mirror_completed_task_ids = project_task_snapshot(project_id)
    loaded_previous = load_queue(project_id) if queue_exists(project_id) else {}
    previous_payload = (
        loaded_previous
        if isinstance(loaded_previous, dict) and loaded_previous.get("project_id") == project_id
        else {"completed_recently": []}
    )
    completed_task_ids = completed_tasks(previous_payload)
    completed_issue_ids = completed_issues(previous_payload)
    completed_task_ids.update(mirror_completed_task_ids)
    for issue, mirrors in mirrors_by_issue.items():
        for mirror in mirrors:
            if mirror["task_id"] in mirror_completed_task_ids:
                completed_issue_ids.add(issue)

    lanes: list[dict] = []
    projection_frontmatters: dict[str, dict] = {}
    needs_materialization: list[dict] = []
    duplicate_mirrors: list[dict] = []
    invalid_lanes: list[dict] = []
    completed_recently = previous_payload.get("completed_recently", [])
    if not isinstance(completed_recently, list):
        completed_recently = []

    for issue in eligible_issues:
        mirrors = mirrors_by_issue.get(issue.number, [])
        if not mirrors:
            needs_materialization.append({"issue": issue.number, "title": issue.title})
            continue
        open_mirrors = [mirror for mirror in mirrors if mirror.get("status") != "done"]
        if not open_mirrors:
            needs_materialization.append({"issue": issue.number, "title": issue.title})
            continue
        if len(open_mirrors) > 1:
            duplicate_mirrors.append(
                {
                    "issue": issue.number,
                    "tasks": [mirror["task_id"] for mirror in sorted(open_mirrors, key=mirror_sort_key)],
                }
            )
        mirror = canonical_mirror(mirrors)
        frontmatter = mirror["frontmatter"]
        task_status_value = str(frontmatter.get("status") or "open")
        if task_status_value == "done":
            continue
        projection_frontmatters[mirror["task_id"]] = frontmatter
        depends_on = normalize_depends(frontmatter.get("depends_on"))
        blockers = dependency_blockers(depends_on, completed_task_ids)
        direct_app_errors, _ = validate_direct_app_policy(frontmatter)
        if direct_app_errors:
            invalid_lanes.append(
                {
                    "issue": issue.number,
                    "task_id": mirror["task_id"],
                    "errors": direct_app_errors,
                }
            )
        refined = bool(frontmatter.get("refined_by")) or bool(frontmatter.get("skip_refinement"))
        needs_acceptance = (
            frontmatter.get("created_by") == "claude"
            and bool(frontmatter.get("refined_by"))
            and not bool(frontmatter.get("accepted_by"))
        )
        if direct_app_errors:
            queue_state = "blocked"
        elif task_status_value == "in_progress":
            queue_state = "active"
        elif task_status_value == "review":
            queue_state = "review"
        elif task_status_value == "blocked" or blockers:
            queue_state = "blocked"
        else:
            queue_state = "queued"

        lanes.append(
            {
                "order": len(lanes) + 1,
                "issue": issue.number,
                "task_id": mirror["task_id"],
                "owner": str(frontmatter.get("owner") or "unassigned"),
                "task_status": task_status_value,
                "queue_state": queue_state,
                "tier": frontmatter.get("tier"),
                "lane_type": frontmatter.get("lane_type"),
                "depends_on": depends_on,
                "blocked_by": [
                    *blockers,
                    *[
                        f"{DIRECT_APP_BLOCKER_PREFIX}{error}"
                        for error in direct_app_errors
                    ],
                ],
                "needs_refinement": not refined,
                "needs_acceptance": needs_acceptance,
                "title": issue.title,
                "notes": "derived by reconcile",
            }
        )

    projection = {
        "project_id": project_id,
        "generated_by": "project_issue_queue.py reconcile",
        "generated_utc": utc_iso(),
        "input_hash": reconciliation_input_hash(project_id, eligible_issues, mirrors_by_issue),
        "backlog": "known",
        "source": "github_issues_and_task_mirrors",
        "source_issue": previous_payload.get("source_issue"),
        "source_task": previous_payload.get("source_task"),
        "needs_materialization": needs_materialization,
        "duplicate_mirrors": duplicate_mirrors,
        "invalid_lanes": invalid_lanes,
        "completed_recently": completed_recently[-10:],
        "lanes": lanes,
    }
    normalize_lanes(
        projection,
        expected_project_id=project_id,
        task_frontmatters=projection_frontmatters,
    )
    return {
        "ok": not needs_materialization and not duplicate_mirrors and not invalid_lanes,
        "backlog": "known",
        "project_id": project_id,
        "needs_materialization": needs_materialization,
        "duplicate_mirrors": duplicate_mirrors,
        "invalid_lanes": invalid_lanes,
        "projection": projection,
    }


def render_markdown(payload: dict) -> str:
    configured_project_id = payload.get("project_id")
    project_id = str(configured_project_id or "project")
    project = get_project(project_id) if configured_project_id else None
    project_name = (project or {}).get("display_name") or (project_id if configured_project_id else "Project")
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
    completed = payload.get("completed_recently", [])
    last_updated = payload.get("last_updated_utc", "unknown")
    source_issue = payload.get("source_issue")
    source_task = payload.get("source_task")
    source_issue_label = f"`GH-{source_issue}`" if isinstance(source_issue, int) else "none"
    source_task_label = f"`{source_task}`" if source_task else "none"

    ready_lane = next((lane for lane in lanes if lane.get("queue_state") == "ready"), None)
    lines = [
        f"# {project_name} Ordered Issue Queue",
        "",
        f"> Generated from GitHub issues and task mirrors. Run `python3 bin/project_issue_queue.py reconcile --project {project_id} --write` to refresh this projection.",
        "",
        f"- Last updated: `{last_updated}`",
        f"- Source issue: {source_issue_label}",
        f"- Source task: {source_task_label}",
    ]

    if ready_lane:
        lines.append(
            f"- Next ready lane: `GH-{ready_lane['issue']}` / `{ready_lane['task_id']}` / `{ready_lane['owner']}` "
            f"({lane_next_action(ready_lane)})"
        )
    else:
        lines.append("- Next ready lane: none")

    if completed:
        lines.extend(
            [
                "",
                "## Recently Completed",
                "",
            ]
        )
        for item in completed:
            lines.append(
                f"- `GH-{item['issue']}` / `{item['task_id']}` / `{item['owner']}` / `{item['status']}`"
            )

    lines.extend(
        [
            "",
            "## Remaining Queue",
            "",
        ]
    )

    if not lanes:
        lines.append("No remaining queued lanes.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| Order | Issue | Task | Owner | Task Status | Queue State | Tier | Depends On | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )

    for lane in lanes:
        depends_on = ", ".join(lane.get("depends_on", [])) or "-"
        tier = lane["tier"] if lane.get("tier") is not None else "-"
        notes = str(lane.get("notes", "-")).replace("|", "/")
        lines.append(
            f"| {lane['order']} | GH-{lane['issue']} | {lane['task_id']} | {lane['owner']} | "
            f"{lane['task_status']} | {lane['queue_state']} | {tier} | {depends_on} | {notes} |"
        )

    return "\n".join(lines) + "\n"


def show_queue(payload: dict) -> str:
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
    if not lanes:
        return "(no remaining queued lanes)"
    lines = []
    for lane in lanes:
        lines.append(
            f"{lane['order']:>2}. GH-{lane['issue']} | {lane['task_id']} | {lane['owner']} | "
            f"task={lane['task_status']} | queue={lane['queue_state']} | next={lane_next_action(lane)}"
        )
    return "\n".join(lines)


def projection_input_changed(project_id: str, projection: dict) -> bool:
    if not queue_exists(project_id):
        return True
    existing = load_queue(project_id)
    return existing.get("input_hash") != projection.get("input_hash")


def sync_markdown(project_id: str, payload: dict) -> Path:
    updated = dict(payload)
    updated["last_updated_utc"] = utc_iso()
    save_queue(project_id, updated)
    markdown = render_markdown(updated)
    markdown_path = queue_markdown_path(project_id)
    write_file(markdown_path, markdown)
    return markdown_path


def next_ready_lane(payload: dict) -> dict | None:
    return next(
        (
            lane
            for lane in sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
            if lane.get("queue_state") == "ready"
        ),
        None,
    )


def find_lane(payload: dict, task_id: str) -> dict | None:
    return next((lane for lane in payload.get("lanes", []) if lane.get("task_id") == task_id), None)


def normalize_lanes(
    payload: dict,
    *,
    expected_project_id: str | None = None,
    task_frontmatters: dict[str, dict] | None = None,
) -> None:
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
    payload["lanes"] = lanes

    project_id = payload.get("project_id")
    queue_project_error = None
    if not isinstance(project_id, str) or not project_id.strip():
        queue_project_error = (
            f"queue project_id must be a non-empty string, found {project_id!r}"
        )
    elif expected_project_id is not None and project_id != expected_project_id:
        queue_project_error = (
            f"queue project_id mismatch: expected {expected_project_id!r}, found {project_id!r}"
        )

    for lane in lanes:
        task_id = lane.get("task_id")
        evidence_error = queue_project_error
        frontmatter = None
        if evidence_error is None and not isinstance(task_id, str):
            evidence_error = f"lane has no string task_id, found {task_id!r}"
        if evidence_error is None:
            if task_frontmatters is not None:
                frontmatter = task_frontmatters.get(task_id)
                if frontmatter is None:
                    evidence_error = f"task mirror not found for {task_id}"
            else:
                task_path = find_task_by_id(task_id)
                if task_path is None:
                    evidence_error = f"task mirror not found for {task_id}"
        if evidence_error is None and frontmatter is None:
            try:
                frontmatter, _ = parse_frontmatter(task_path.read_text())
            except (OSError, TypeError, UnicodeError, ValueError) as error:
                evidence_error = f"task mirror for {task_id} cannot be read: {error}"
        if evidence_error is None and not isinstance(frontmatter, dict):
            evidence_error = f"task mirror not found for {task_id}"
        if evidence_error is None:
            task_project_id = frontmatter.get("project_id")
            if task_project_id != project_id:
                evidence_error = (
                    f"task mirror project mismatch for {task_id}: "
                    f"queue {project_id!r}, task {task_project_id!r}"
                )
        if evidence_error is None:
            task_status_value = frontmatter.get("status")
            if task_status_value != lane.get("task_status"):
                evidence_error = (
                    f"task mirror status mismatch for {task_id}: "
                    f"queue {lane.get('task_status')!r}, task {task_status_value!r}"
                )

        direct_app_errors = []
        if evidence_error is None:
            direct_app_errors, _ = validate_direct_app_policy(frontmatter)
        managed_errors = (
            [f"policy evidence unavailable: {evidence_error}"]
            if evidence_error is not None
            else direct_app_errors
        )
        prior_blockers = [
            blocker
            for blocker in normalize_depends(lane.get("blocked_by"))
            if not blocker.startswith(DIRECT_APP_BLOCKER_PREFIX)
        ]
        lane["blocked_by"] = [
            *prior_blockers,
            *[
                f"{DIRECT_APP_BLOCKER_PREFIX}{error}"
                for error in managed_errors
            ],
        ]
        if managed_errors:
            lane["queue_state"] = "blocked"

    unblock_satisfied_lanes(payload)

    for index, lane in enumerate(lanes, start=1):
        lane["order"] = index

    active_like = [
        lane for lane in lanes if lane.get("queue_state") in {"active", "review"}
    ]
    ready_lanes = [lane for lane in lanes if lane.get("queue_state") == "ready"]

    if active_like:
        for lane in ready_lanes:
            lane["queue_state"] = "queued"
        payload["lanes"] = lanes
        return

    canonical_ready: dict | None = None
    if ready_lanes:
        canonical_ready = min(ready_lanes, key=lambda lane: lane["order"])
        for lane in ready_lanes:
            if lane is not canonical_ready:
                lane["queue_state"] = "queued"

    if canonical_ready is None:
        canonical_ready = next(
            (lane for lane in lanes if lane.get("queue_state") == "queued"),
            None,
        )
        if canonical_ready is not None:
            canonical_ready["queue_state"] = "ready"

    payload["lanes"] = lanes


def archive_complete_queue(project_id: str, payload: dict) -> tuple[Path, Path]:
    stamp = utc_iso().replace(":", "").replace("+00:00", "Z")
    history_dir = queue_history_dir(project_id)
    history_json = history_dir / f"issue-queue-{stamp}.json"
    history_md = history_dir / f"issue-queue-{stamp}.md"
    write_file(history_json, json.dumps(payload, indent=2) + "\n")
    write_file(history_md, render_markdown(payload))
    return history_json, history_md


def mark_lane_transition(project_id: str, task_id: str, *, owner: str, task_status: str) -> dict | None:
    if not queue_exists(project_id):
        return None

    payload = load_queue(project_id)
    loaded_project_id = payload.get("project_id") if isinstance(payload, dict) else None
    if loaded_project_id != project_id:
        raise ValueError(
            f"queue project_id mismatch: expected {project_id!r}, found {loaded_project_id!r}; "
            "refusing lane transition before mutation"
        )
    lane = find_lane(payload, task_id)
    if lane is None:
        return None

    lane["owner"] = owner
    lane["task_status"] = task_status

    if task_status == "in_progress":
        lane["queue_state"] = "active"
    elif task_status == "open":
        lane["queue_state"] = "queued"
    elif task_status == "blocked":
        lane["queue_state"] = "blocked"
    elif task_status == "review":
        lane["queue_state"] = "review"
    elif task_status == "done":
        payload["lanes"] = [existing for existing in payload.get("lanes", []) if existing.get("task_id") != task_id]
        completed = payload.setdefault("completed_recently", [])
        completed.append(
            {
                "issue": lane["issue"],
                "task_id": lane["task_id"],
                "owner": owner,
                "status": "done",
            }
        )
        payload["completed_recently"] = completed[-10:]

        if not payload["lanes"]:
            archived_json, archived_md = archive_complete_queue(project_id, dict(payload))
            payload["notes"] = [
                "No remaining queued lanes.",
                f"Last complete queue snapshot archived to {display_path(archived_json)} and {display_path(archived_md)}.",
            ]
            sync_markdown(project_id, payload)
            return {
                "updated": True,
                "archived": True,
                "history_json": display_path(archived_json),
                "history_md": display_path(archived_md),
            }
    normalize_lanes(payload, expected_project_id=project_id)
    sync_markdown(project_id, payload)
    return {"updated": True, "archived": False}


def main() -> int:
    args = parse_args()

    if args.command == "reconcile":
        result = reconcile_queue(args.project)
        if result.get("backlog") == "unknown":
            if args.json_output:
                print(json.dumps(result, indent=2))
            else:
                print(f"[error] GitHub backlog state unknown for {args.project}: {result.get('reason')}", file=sys.stderr)
            return 1
        projection = result["projection"]
        if args.write:
            sync_markdown(args.project, projection)
        if args.json_output:
            print(json.dumps(result, indent=2))
        else:
            if args.write:
                print(f"reconciled: {display_path(queue_json_path(args.project))}")
                print(f"markdown: {display_path(queue_markdown_path(args.project))}")
            else:
                print(show_queue(projection))
            if result.get("needs_materialization"):
                issues = ", ".join(f"GH-{item['issue']}" for item in result["needs_materialization"])
                print(f"needs materialization: {issues}")
            if result.get("duplicate_mirrors"):
                issues = ", ".join(f"GH-{item['issue']}" for item in result["duplicate_mirrors"])
                print(f"duplicate mirrors: {issues}")
            if result.get("invalid_lanes"):
                issues = ", ".join(f"GH-{item['issue']}" for item in result["invalid_lanes"])
                print(f"direct-app policy violations: {issues}")
        return 0 if result.get("ok") else 1

    payload = load_queue(args.project)

    if args.command == "show":
        print(show_queue(payload))
        return 0

    if args.command == "validate":
        errors, warnings = validate_queue(args.project, payload)
        ready_errors, ready_warnings = no_ready_lane_errors(args.project, payload)
        errors.extend(ready_errors)
        warnings.extend(ready_warnings)
        if not args.skip_backlog_check:
            backlog_errors, backlog_warnings = backlog_consistency_errors(args.project, payload)
            errors.extend(backlog_errors)
            warnings.extend(backlog_warnings)
        print(f"project: {args.project}")
        print(f"queue: {display_path(queue_json_path(args.project))}")
        if warnings:
            print("\nwarnings:")
            for warning in warnings:
                print(f"- {warning}")
        if errors:
            print("\nerrors:")
            for error in errors:
                print(f"- {error}")
            return 1
        print("\nqueue validation: ok")
        ready_lane = next_ready_lane(payload)
        if ready_lane:
            print(
                f"next ready lane: GH-{ready_lane['issue']} / {ready_lane['task_id']} / {ready_lane['owner']} "
                f"({lane_next_action(ready_lane)})"
            )
        elif payload.get("lanes"):
            print("queue has no ready lane")
        else:
            print("queue empty: confirmed against GitHub backlog")
        return 0

    if args.command == "archive-complete":
        if not args.skip_backlog_check:
            backlog_errors, _ = backlog_consistency_errors(args.project, payload)
            if backlog_errors:
                print("[error] Refusing archive-complete while GitHub backlog is not clean.", file=sys.stderr)
                for error in backlog_errors:
                    print(f"- {error}", file=sys.stderr)
                return 1
        if payload.get("lanes"):
            print("[error] Refusing archive-complete while lanes remain in the queue.", file=sys.stderr)
            return 1
        archived_json, archived_md = archive_complete_queue(args.project, payload)
        payload["notes"] = [
            "No remaining queued lanes.",
            f"Queue archived manually to {display_path(archived_json)} and {display_path(archived_md)}.",
        ]
        sync_markdown(args.project, payload)
        print(f"archived_json: {display_path(archived_json)}")
        print(f"archived_md: {display_path(archived_md)}")
        return 0

    normalize_lanes(payload, expected_project_id=args.project)
    errors, warnings = validate_queue(args.project, payload)
    if errors:
        print("[error] Queue is invalid; fix queue JSON before syncing markdown.", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    markdown_path = sync_markdown(args.project, payload)
    print(display_path(markdown_path))
    if warnings:
        print("\nwarnings:")
        for warning in warnings:
            print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
