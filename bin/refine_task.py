#!/usr/bin/env python3
"""
refine_task.py — Mark a task as planned/refined after claude has validated the spec.

Sets refined_by/refined_at and planning_mode in the task frontmatter, unblocking
the in_progress transition in claim_task.py when the task is otherwise accepted.

Usage:
  python bin/refine_task.py --task TASK-ABC123
  python bin/refine_task.py --task TASK-ABC123 --note "Added numeric spec and regression cases"
  python bin/refine_task.py --task TASK-ABC123 --planning-mode authored
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import re

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
SUMMARY_SECTION = "## Summary"
ACCEPTANCE_CRITERIA_SECTION = "## Acceptance Criteria"
VERIFICATION_PLAN_SECTION = "## Verification Plan"
RISK_SECTION = "## Implementation Risk Analysis"
CANONICAL_SECTIONS = (
    SUMMARY_SECTION,
    ACCEPTANCE_CRITERIA_SECTION,
    VERIFICATION_PLAN_SECTION,
    RISK_SECTION,
)
RISK_REQUIRED_LABELS = [
    "Current file/topology reviewed:",
    "Scope split decision:",
    "Estimated diff/risk:",
    "Verification/browser/sign-off plan:",
    "Open decisions/blockers:",
]
RISK_PLACEHOLDER = "(required before refinement)"
SECTION_PLACEHOLDERS = {
    "(describe the task)",
    "describe the task",
    RISK_PLACEHOLDER,
    "(placeholder)",
    "<placeholder>",
    "placeholder",
    "tbd",
    "todo",
}
DESIGN_THINKING_BUDGET_LABEL = "Design thinking in details — polish-pass budget:"
DESIGN_THINKING_SEEDS_LABEL = "Design thinking in details — polish vectors:"


def parse_args():
    p = argparse.ArgumentParser(description="Mark a task as planned/refined by claude.")
    p.add_argument("--task", required=True, help="TASK-id to mark as refined")
    p.add_argument("--note", default=None, help="Optional activity log note describing what was refined")
    p.add_argument(
        "--planning-mode",
        choices=["authored", "refined"],
        default=None,
        help="Whether Claude authored the task plan or refined an existing task.",
    )
    return p.parse_args()


def _section_matches(body: str, heading: str) -> list[re.Match]:
    return list(
        re.finditer(
            rf"^{re.escape(heading)}[ \t]*$",
            body,
            flags=re.MULTILINE,
        )
    )


def _section_body(body: str, heading_match: re.Match) -> str:
    next_heading = re.search(r"^##(?:[ \t]+.*)?$", body[heading_match.end() :], flags=re.MULTILINE)
    end = heading_match.end() + next_heading.start() if next_heading else len(body)
    return body[heading_match.end() : end]


def _content_lines(section: str) -> list[str]:
    without_comments = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    content = []
    for raw_line in without_comments.splitlines():
        line = raw_line.strip()
        line = re.sub(r"^(?:[-*+]\s*)?", "", line)
        line = re.sub(r"^\[(?: |x|X)\]\s*", "", line)
        line = line.strip().strip("*_`").strip()
        if line:
            content.append(line)
    return content


def _is_placeholder_only(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value.strip()).casefold()
    normalized = normalized.strip("*_`").strip()
    return normalized in {placeholder.casefold() for placeholder in SECTION_PLACEHOLDERS}


def _has_substantive_content(section: str) -> bool:
    content = _content_lines(section)
    return bool(content) and any(not _is_placeholder_only(line) for line in content)


def validate_canonical_task_sections(body: str) -> list[str]:
    errors = []
    sections = {}
    for heading in CANONICAL_SECTIONS:
        matches = _section_matches(body, heading)
        if not matches:
            errors.append(f"missing canonical section: {heading}")
            continue
        if len(matches) > 1:
            errors.append(f"duplicate canonical section: {heading} (found {len(matches)})")
            continue
        sections[heading] = _section_body(body, matches[0])

    for heading in (SUMMARY_SECTION, ACCEPTANCE_CRITERIA_SECTION, VERIFICATION_PLAN_SECTION):
        section = sections.get(heading)
        if section is not None and not _has_substantive_content(section):
            errors.append(f"empty or placeholder-only canonical section: {heading}")
    return errors


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
        if not value or _is_placeholder_only(value):
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


def _risk_label_value(section: str, label: str) -> str | None:
    if label not in section:
        return None
    post_label = section.split(label, 1)[1]
    first_line = post_label.splitlines()[0] if post_label.splitlines() else ""
    return first_line.strip()


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
    budget_line = _risk_label_value(section, DESIGN_THINKING_BUDGET_LABEL)
    if budget_line is None:
        errors.append(f"missing risk-analysis label: {DESIGN_THINKING_BUDGET_LABEL}")
    elif not budget_line or RISK_PLACEHOLDER in budget_line:
        errors.append(f"unresolved risk-analysis value: {DESIGN_THINKING_BUDGET_LABEL}")

    budget = frontmatter.get("design_thinking_polish_budget_loc")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        errors.append("missing positive integer frontmatter field: design_thinking_polish_budget_loc")

    seeds = _normalize_list(frontmatter.get("design_thinking_polish_seeds"))
    if len(seeds) < 2:
        errors.append("missing at least 2 frontmatter entries: design_thinking_polish_seeds")

    seeds_line = _risk_label_value(section, DESIGN_THINKING_SEEDS_LABEL)
    if seeds_line is None:
        errors.append(f"missing risk-analysis label: {DESIGN_THINKING_SEEDS_LABEL}")
    elif not seeds_line or RISK_PLACEHOLDER in seeds_line:
        errors.append(f"unresolved risk-analysis value: {DESIGN_THINKING_SEEDS_LABEL}")
    return errors


def validate_refinement(frontmatter, body):
    if frontmatter.get("skip_refinement", False):
        return []

    errors = validate_canonical_task_sections(body)
    errors.extend(validate_implementation_risk_analysis(body))
    errors.extend(validate_design_thinking_refinement(frontmatter, body))
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

    refinement_errors = validate_refinement(fm, body)
    if refinement_errors:
        print(
            json.dumps(
                {
                    "error": "task body is incomplete; refusing refinement mark",
                    "task_id": fm.get("task_id", args.task),
                    "required_sections": CANONICAL_SECTIONS,
                    "required_section": RISK_SECTION,
                    "required_labels": RISK_REQUIRED_LABELS,
                    "errors": refinement_errors,
                    "hint": "Patch the named task section or risk-analysis label with real implementation content before running refine_task.py.",
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
    fm["planning_mode"] = args.planning_mode or ("authored" if fm.get("created_by") == REFINEMENT_AGENT else "refined")

    note = args.note or "Task spec planned/refined — ready for activation"
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
                "planning_mode": fm.get("planning_mode"),
                "path": str(task_file.relative_to(ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
