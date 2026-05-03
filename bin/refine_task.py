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
DESIGN_THINKING_BUDGET_LABEL = "Design thinking in details — polish-pass budget:"
DESIGN_THINKING_SEEDS_LABEL = "Design thinking in details — polish vectors:"


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
        label_match = re.search(rf"^[^\n]*{re.escape(label)}[ \t]*(?P<value>[^\n]*)$", section, flags=re.MULTILINE)
        value = label_match.group("value").strip() if label_match else ""
        if not value or RISK_PLACEHOLDER in value:
            errors.append(f"unresolved risk-analysis value: {label}")
    return errors


def _normalize_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"true", "yes", "1"}:
        return True
    if raw in {"false", "no", "0"}:
        return False
    return None


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_list(value) -> list[str]:
    if value in (None, "", "<none>"):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()]


def validate_design_thinking_refinement(frontmatter, body):
    if _normalize_bool(frontmatter.get("ui_ux_lane")) is not True:
        return []
    if _normalize_text(frontmatter.get("ui_ux_mode")) != "implementation":
        return []

    match = re.search(
        rf"^{re.escape(RISK_SECTION)}\s*(?P<section>.*?)(?=^##\s|\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    section = match.group("section") if match else ""
    errors = []
    if DESIGN_THINKING_BUDGET_LABEL not in section:
        errors.append(f"missing risk-analysis label: {DESIGN_THINKING_BUDGET_LABEL}")
    elif RISK_PLACEHOLDER in section.split(DESIGN_THINKING_BUDGET_LABEL, 1)[1].splitlines()[0]:
        errors.append(f"unresolved risk-analysis value: {DESIGN_THINKING_BUDGET_LABEL}")

    budget = frontmatter.get("design_thinking_polish_budget_loc")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        errors.append("missing positive integer frontmatter field: design_thinking_polish_budget_loc")

    seeds = _normalize_list(frontmatter.get("design_thinking_polish_seeds"))
    if len(seeds) < 2:
        errors.append("missing at least 2 frontmatter entries: design_thinking_polish_seeds")

    if DESIGN_THINKING_SEEDS_LABEL not in section:
        errors.append(f"missing risk-analysis label: {DESIGN_THINKING_SEEDS_LABEL}")
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
    risk_errors.extend([] if fm.get("skip_refinement", False) else validate_design_thinking_refinement(fm, body))
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
