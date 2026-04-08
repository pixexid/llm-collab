#!/usr/bin/env python3
"""
task_board.py — List and filter tasks.

Usage:
  python bin/task_board.py
  python bin/task_board.py --status in_progress
  python bin/task_board.py --owner orchestrator
  python bin/task_board.py --project my-app
  python bin/task_board.py --json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import all_task_files, parse_frontmatter, TASK_STATUSES


def parse_args():
    p = argparse.ArgumentParser(description="List tasks.")
    p.add_argument("--status", default=None, choices=TASK_STATUSES)
    p.add_argument("--owner", default=None)
    p.add_argument("--project", default=None, help="Filter by project_id")
    p.add_argument("--json", dest="json_output", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    tasks = []

    for f in all_task_files():
        fm, _ = parse_frontmatter(f.read_text())
        if not fm.get("task_id"):
            continue
        if args.status and fm.get("status") != args.status:
            continue
        if args.owner and fm.get("owner") != args.owner:
            continue
        if args.project and fm.get("project_id") != args.project:
            continue
        tasks.append({
            "task_id": fm.get("task_id"),
            "title": fm.get("title"),
            "status": fm.get("status"),
            "owner": fm.get("owner"),
            "priority": fm.get("priority"),
            "project_id": fm.get("project_id"),
            "path": str(f),
        })

    if args.json_output:
        print(json.dumps(tasks, indent=2))
        return

    if not tasks:
        print("No tasks found.")
        return

    col = "{:<14} {:<12} {:<14} {:<10} {:<12} {}"
    print(col.format("TASK ID", "STATUS", "OWNER", "PRIORITY", "PROJECT", "TITLE"))
    print("-" * 90)
    for t in tasks:
        print(col.format(
            t["task_id"] or "",
            t["status"] or "",
            t["owner"] or "",
            t["priority"] or "",
            t["project_id"] or "",
            (t["title"] or "")[:50],
        ))


if __name__ == "__main__":
    main()
