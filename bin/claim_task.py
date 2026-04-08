#!/usr/bin/env python3
"""
claim_task.py — Assign ownership and update task status.

Moves the task file to the appropriate folder (active/backlog/done)
and appends an activity log entry.

Usage:
  python bin/claim_task.py --task TASK-ABC123 --owner orchestrator --status in_progress
  python bin/claim_task.py --task TASK-ABC123 --owner unassigned --status open --note "Blocked on API spec"
  python bin/claim_task.py --task TASK-ABC123 --owner orchestrator --status done
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    TASK_STATUSES,
    agent_ids,
    dump_frontmatter,
    find_task_by_id,
    parse_frontmatter,
    run_project_preflight,
    target_task_path,
    utc_iso,
    write_file,
)


def parse_args():
    p = argparse.ArgumentParser(description="Claim or update a task.")
    p.add_argument("--task", required=True, help="TASK-id")
    p.add_argument("--owner", required=True, help="Agent ID or 'unassigned'")
    p.add_argument("--status", required=True, choices=TASK_STATUSES)
    p.add_argument("--note", default=None, help="Activity log entry (optional)")
    p.add_argument("--branch", default=None, help="Git branch associated with this task")
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip project preflight gate for this status transition.",
    )
    return p.parse_args()


def main():
    args = parse_args()

    known = agent_ids()
    if args.owner != "unassigned" and args.owner not in known:
        print(f"[error] Owner {args.owner!r} not in agents.json", file=sys.stderr)
        sys.exit(1)

    task_file = find_task_by_id(args.task)
    if task_file is None:
        print(f"[error] Task not found: {args.task}", file=sys.stderr)
        sys.exit(1)

    content = task_file.read_text()
    fm, body = parse_frontmatter(content)

    old_status = fm.get("status", "open")
    project_id = fm.get("project_id")
    preflight_summary = None

    if args.status in ("in_progress", "review") and not args.skip_preflight:
        preflight = run_project_preflight(project_id)
        if preflight.get("ran"):
            preflight_summary = {
                "ran": True,
                "ok": bool(preflight.get("ok")),
                "cwd": preflight.get("cwd"),
                "command": preflight.get("command"),
                "returncode": preflight.get("returncode"),
            }
        if preflight.get("ran") and not preflight.get("ok"):
            print(
                json.dumps(
                    {
                        "error": "project preflight failed; refusing claim_task transition",
                        "task_id": fm.get("task_id", args.task),
                        "target_status": args.status,
                        "project_id": project_id,
                        "preflight": preflight,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

    fm["status"] = args.status
    fm["owner"] = args.owner
    if args.branch:
        fm["branch"] = args.branch

    note = args.note or f"Status → {args.status}, owner → {args.owner}"
    activity_line = f"- {utc_iso()} | {args.owner} | {note}"

    if "## Activity Log" in body:
        body = body.replace("## Activity Log", f"## Activity Log\n\n{activity_line}", 1)
    else:
        body = body.rstrip() + f"\n\n## Activity Log\n\n{activity_line}\n"

    new_content = dump_frontmatter(fm, body)

    # Determine new path (may move between folders)
    title = fm.get("title", args.task)
    tid = fm.get("task_id", args.task)
    new_path = target_task_path(title, tid, args.status)

    if task_file != new_path:
        task_file.unlink()

    write_file(new_path, new_content)

    result = {
        "task_id": tid,
        "old_status": old_status,
        "new_status": args.status,
        "owner": args.owner,
        "path": str(new_path.relative_to(ROOT)),
        "moved": task_file != new_path,
    }
    if preflight_summary is not None:
        result["preflight"] = preflight_summary
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
