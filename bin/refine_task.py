#!/usr/bin/env python3
"""
refine_task.py — Mark a task as refined after claude has reviewed and patched the spec.

Sets refined_by and refined_at in the task frontmatter, unblocking the in_progress
transition in claim_task.py.

Usage:
  python bin/refine_task.py --task TASK-ABC123
  python bin/refine_task.py --task TASK-ABC123 --note "Added numeric spec and regression cases"
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    dump_frontmatter,
    find_task_by_id,
    parse_frontmatter,
    utc_iso,
    write_file,
)

REFINEMENT_AGENT = "claude"
RISK_SECTION = "## Implementation Risk Analysis"
RISK_REQUIRED_LABELS = [
    "Current file/topology reviewed:",
    "Scope split decision:",
    "Estimated diff/risk:",
    "Verification/browser/sign-off plan:",
    "Open decisions/blockers:",
]
RISK_PLACEHOLDER = "(required before refinement)"


def parse_args():
    p = argparse.ArgumentParser(description="Mark a task as refined by claude.")
    p.add_argument("--task", required=True, help="TASK-id to mark as refined")
    p.add_argument("--note", default=None, help="Optional activity log note describing what was refined")
    return p.parse_args()


def validate_implementation_risk_analysis(body):
    match = re.search(
        rf"^{re.escape(RISK_SECTION)}\s*(?P<section>.*?)(?=^##\s|\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return [f"missing {RISK_SECTION} section"]

    section = match.group("section")
    errors = []
    for label in RISK_REQUIRED_LABELS:
        if label not in section:
            errors.append(f"missing risk-analysis label: {label}")
            continue
        label_match = re.search(rf"{re.escape(label)}\s*(?P<value>.*)", section)
        value = label_match.group("value").strip() if label_match else ""
        if not value or RISK_PLACEHOLDER in value:
            errors.append(f"unresolved risk-analysis value: {label}")
    return errors


def main():
    args = parse_args()

    task_file = find_task_by_id(args.task)
    if task_file is None:
        print(f"[error] Task not found: {args.task}", file=sys.stderr)
        sys.exit(1)

    content = task_file.read_text()
    fm, body = parse_frontmatter(content)

    if fm.get("skip_refinement", False):
        print(
            json.dumps(
                {
                    "warning": "task has skip_refinement: true — refinement mark is redundant but harmless",
                    "task_id": fm.get("task_id", args.task),
                },
                indent=2,
            )
        )

    risk_errors = [] if fm.get("skip_refinement", False) else validate_implementation_risk_analysis(body)
    if risk_errors:
        print(
            json.dumps(
                {
                    "error": "implementation risk analysis is incomplete; refusing refinement mark",
                    "task_id": fm.get("task_id", args.task),
                    "required_section": RISK_SECTION,
                    "required_labels": RISK_REQUIRED_LABELS,
                    "errors": risk_errors,
                    "hint": "Patch the task body with real pre-implementation feasibility analysis before running refine_task.py.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    already_refined = fm.get("refined_by") == REFINEMENT_AGENT
    if already_refined:
        print(
            json.dumps(
                {
                    "warning": "task already refined by claude",
                    "task_id": fm.get("task_id", args.task),
                    "refined_at": fm.get("refined_at"),
                },
                indent=2,
            )
        )
        return

    now = utc_iso()
    fm["refined_by"] = REFINEMENT_AGENT
    fm["refined_at"] = now

    note = args.note or "Task spec refined — ready for activation"
    activity_line = f"- {now} | {REFINEMENT_AGENT} | {note}"

    if "## Activity Log" in body:
        body = body.replace("## Activity Log", f"## Activity Log\n\n{activity_line}", 1)
    else:
        body = body.rstrip() + f"\n\n## Activity Log\n\n{activity_line}\n"

    write_file(task_file, dump_frontmatter(fm, body))

    print(
        json.dumps(
            {
                "task_id": fm.get("task_id", args.task),
                "refined_by": REFINEMENT_AGENT,
                "refined_at": now,
                "path": str(task_file.relative_to(ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
