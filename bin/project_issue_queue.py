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

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import ROOT, find_task_by_id, get_project, parse_frontmatter, utc_iso, write_file

QUEUE_STATES = {"ready", "queued", "blocked", "active", "review", "done"}
QUEUE_FILE_NAME = "issue-queue.json"
MARKDOWN_FILE_NAME = "issue-queue.md"
HISTORY_DIR_NAME = "history"
ISSUE_RE = re.compile(r"\bGH[- #]+(\d+)\b", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show, validate, or render a project issue queue.")
    parser.add_argument("command", choices=("show", "validate", "sync-markdown", "archive-complete"))
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    return parser.parse_args()


def queue_dir(project_id: str) -> Path:
    return ROOT / "projects" / project_id


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


def normalize_depends(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


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


def render_markdown(payload: dict) -> str:
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
    completed = payload.get("completed_recently", [])
    last_updated = payload.get("last_updated_utc", "unknown")
    source_issue = payload.get("source_issue")
    source_task = payload.get("source_task")

    ready_lane = next((lane for lane in lanes if lane.get("queue_state") == "ready"), None)
    lines = [
        "# Amiga Ordered Issue Queue",
        "",
        "> Generated from `issue-queue.json`. Edit the JSON, then run `python3 bin/project_issue_queue.py sync-markdown --project amiga`.",
        "",
        f"- Last updated: `{last_updated}`",
        f"- Source issue: `GH-{source_issue}`",
        f"- Source task: `{source_task}`",
    ]

    if ready_lane:
        lines.append(
            f"- Next ready lane: `GH-{ready_lane['issue']}` / `{ready_lane['task_id']}` / `{ready_lane['owner']}`"
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
        notes = str(lane.get("notes", "-")).replace("|", "/")
        lines.append(
            f"| {lane['order']} | GH-{lane['issue']} | {lane['task_id']} | {lane['owner']} | "
            f"{lane['task_status']} | {lane['queue_state']} | {lane['tier']} | {depends_on} | {notes} |"
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
            f"task={lane['task_status']} | queue={lane['queue_state']}"
        )
    return "\n".join(lines)


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
    lane = find_lane(payload, task_id)
    if lane is None:
        return None

    lane["owner"] = owner
    lane["task_status"] = task_status

    if task_status == "in_progress":
        lane["queue_state"] = "active"
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
                f"Last complete queue snapshot archived to {archived_json.relative_to(ROOT)} and {archived_md.relative_to(ROOT)}.",
            ]
            sync_markdown(project_id, payload)
            return {
                "updated": True,
                "archived": True,
                "history_json": str(archived_json.relative_to(ROOT)),
                "history_md": str(archived_md.relative_to(ROOT)),
            }
    sync_markdown(project_id, payload)
    return {"updated": True, "archived": False}


def main() -> int:
    args = parse_args()
    payload = load_queue(args.project)

    if args.command == "show":
        print(show_queue(payload))
        return 0

    if args.command == "validate":
        errors, warnings = validate_queue(args.project, payload)
        print(f"project: {args.project}")
        print(f"queue: {queue_json_path(args.project).relative_to(ROOT)}")
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
                f"next ready lane: GH-{ready_lane['issue']} / {ready_lane['task_id']} / {ready_lane['owner']}"
            )
        else:
            print("queue empty")
        return 0

    if args.command == "archive-complete":
        if payload.get("lanes"):
            print("[error] Refusing archive-complete while lanes remain in the queue.", file=sys.stderr)
            return 1
        archived_json, archived_md = archive_complete_queue(args.project, payload)
        payload["notes"] = [
            "No remaining queued lanes.",
            f"Queue archived manually to {archived_json.relative_to(ROOT)} and {archived_md.relative_to(ROOT)}.",
        ]
        sync_markdown(args.project, payload)
        print(f"archived_json: {archived_json.relative_to(ROOT)}")
        print(f"archived_md: {archived_md.relative_to(ROOT)}")
        return 0

    errors, warnings = validate_queue(args.project, payload)
    if errors:
        print("[error] Queue is invalid; fix queue JSON before syncing markdown.", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    markdown_path = sync_markdown(args.project, payload)
    print(markdown_path.relative_to(ROOT))
    if warnings:
        print("\nwarnings:")
        for warning in warnings:
            print(f"- {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
