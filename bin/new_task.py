#!/usr/bin/env python3
"""
new_task.py — Create a new task file.

Usage:
  python bin/new_task.py --title "Implement auth flow" --created-by orchestrator --project my-app
  python bin/new_task.py --title "Research DB options" --created-by researcher --owner researcher --project my-app --priority high
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    TASK_PRIORITIES,
    TASK_STATUSES,
    agent_ids,
    date_prefix,
    dump_frontmatter,
    slugify,
    target_task_path,
    task_id,
    utc_iso,
    write_file,
)


def parse_args():
    p = argparse.ArgumentParser(description="Create a new task.")
    p.add_argument("--title", required=True, help="Task title")
    p.add_argument("--created-by", required=True, help="Agent ID creating this task")
    p.add_argument("--requested-by", default="operator", help="Who requested it (default: operator)")
    p.add_argument("--owner", default="unassigned", help="Assignee agent ID (default: unassigned)")
    p.add_argument("--priority", default="normal", choices=TASK_PRIORITIES)
    p.add_argument("--status", default="open", choices=["open", "in_progress"])
    p.add_argument("--project", default=None, help="project_id this task belongs to")
    p.add_argument("--repo-targets", default="", help="Comma-separated repo IDs in scope")
    p.add_argument("--path-targets", default="", help="Comma-separated file/dir paths in scope")
    p.add_argument("--related-chat", default=None, help="CHAT-id cross-reference")
    p.add_argument("--depends-on", default="", help="Comma-separated TASK-ids this depends on")
    return p.parse_args()


def main():
    args = parse_args()

    known = agent_ids()
    if args.created_by not in known:
        print(f"[error] --created-by agent {args.created_by!r} not in agents.json", file=sys.stderr)
        sys.exit(1)
    if args.owner != "unassigned" and args.owner not in known:
        print(f"[error] --owner agent {args.owner!r} not in agents.json", file=sys.stderr)
        sys.exit(1)

    tid = task_id()
    repo_targets = [r.strip() for r in args.repo_targets.split(",") if r.strip()]
    path_targets = [p.strip() for p in args.path_targets.split(",") if p.strip()]
    depends_on = [d.strip() for d in args.depends_on.split(",") if d.strip()]

    fm = {
        "task_id": tid,
        "title": args.title,
        "status": args.status,
        "owner": args.owner,
        "created_by": args.created_by,
        "requested_by": args.requested_by,
        "created_utc": utc_iso(),
        "priority": args.priority,
        "project_id": args.project,
        "related_chat": args.related_chat,
        "related_paths": path_targets,
        "repo_targets": repo_targets,
        "depends_on": depends_on,
        "branch": None,
    }

    body = f"""# {args.title}

## Summary

(describe the task)

## Acceptance Criteria

- [ ]

## Verification Plan

- [ ]

## Notes

## Activity Log

- {utc_iso()} | {args.created_by} | Task created
"""

    content = dump_frontmatter(fm, body)
    path = target_task_path(args.title, tid, args.status)
    write_file(path, content)

    result = {
        "task_id": tid,
        "path": str(path.relative_to(ROOT)),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
