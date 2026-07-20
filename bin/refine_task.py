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
from task_contract import validate_direct_app_policy

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
ALL_RISK_LABELS = (
    *RISK_REQUIRED_LABELS,
    DESIGN_THINKING_BUDGET_LABEL,
    DESIGN_THINKING_SEEDS_LABEL,
)
HTML_TAG_RE = re.compile(
    r"</?[A-Za-z][A-Za-z0-9-]*"
    r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*"
    r"\s*/?>",
    flags=re.DOTALL,
)


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


def _mask_non_newline_characters(value: str) -> str:
    return re.sub(r"[^\r\n]", " ", value)


def _indent_width(value: str) -> int:
    width = 0
    for character in value:
        width += 4 - (width % 4) if character == "\t" else 1
    return width


def _container_fence(
    value: str,
    *,
    expected_container: tuple[tuple[str, int], ...] | None = None,
) -> tuple[str, tuple[tuple[str, int], ...], str] | None:
    cursor = 0
    container = []
    if expected_container is None:
        while cursor < len(value):
            quote = re.match(r" {0,3}>[ \t]?", value[cursor:])
            if quote:
                container.append(("quote", 0))
                cursor += quote.end()
                continue

            list_item = re.match(
                r"(?P<leading> {0,3})(?P<marker>[-+*]|\d{1,9}[.)])"
                r"(?P<spacing>[ \t]{1,4})",
                value[cursor:],
            )
            if list_item:
                prefix = (
                    list_item.group("leading")
                    + list_item.group("marker")
                    + list_item.group("spacing")
                )
                container.append(("list", _indent_width(prefix)))
                cursor += list_item.end()
                continue
            break
    else:
        container = list(expected_container)
        for kind, continuation_indent in expected_container:
            if kind == "quote":
                quote = re.match(r" {0,3}>[ \t]?", value[cursor:])
                if not quote:
                    return None
                cursor += quote.end()
                continue

            start = cursor
            width = 0
            while cursor < len(value) and value[cursor] in " \t" and width < continuation_indent:
                character_width = (
                    4 - (width % 4)
                    if value[cursor] == "\t"
                    else 1
                )
                if width + character_width > continuation_indent:
                    return None
                width += character_width
                cursor += 1
            if width != continuation_indent or cursor == start:
                return None

    indentation = re.match(r"[ \t]*", value[cursor:]).group()
    if _indent_width(indentation) > 3:
        return None
    cursor += len(indentation)

    fence_match = re.match(r"(?P<fence>`{3,}|~{3,})(?P<suffix>.*)\Z", value[cursor:])
    if not fence_match:
        return None
    fence = fence_match.group("fence")
    return fence, tuple(container), fence_match.group("suffix")


def _delimiter_is_protected(
    protected: list[bool] | tuple[bool, ...],
    start: int,
    length: int,
) -> bool:
    return bool(protected) and all(protected[start : start + length])


def _backtick_run_is_escaped(value: str, start: int) -> bool:
    backslash_count = 0
    cursor = start - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslash_count += 1
        cursor -= 1
    return backslash_count % 2 == 1


def _html_comment_state_after(
    value: str,
    *,
    offset: int,
    protected: list[bool] | tuple[bool, ...],
    in_comment: bool,
) -> bool:
    cursor = 0
    while cursor < len(value):
        delimiter = "-->" if in_comment else "<!--"
        delimiter_index = value.find(delimiter, cursor)
        if delimiter_index < 0:
            break
        absolute_index = offset + delimiter_index
        if (
            not in_comment
            and _delimiter_is_protected(protected, absolute_index, len(delimiter))
        ):
            cursor = delimiter_index + len(delimiter)
            continue
        in_comment = not in_comment
        cursor = delimiter_index + len(delimiter)
    return in_comment


def _mask_fenced_markdown(
    value: str,
    *,
    protected: list[bool] | tuple[bool, ...] = (),
) -> str:
    lines = []
    fence_character = None
    fence_length = 0
    fence_container = ()
    in_comment = False
    offset = 0
    for line in value.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if fence_character is not None:
            lines.append(_mask_non_newline_characters(line))
            closing = _container_fence(
                content,
                expected_container=fence_container,
            )
            if (
                closing is not None
                and closing[0][0] == fence_character
                and len(closing[0]) >= fence_length
                and not closing[2].strip()
            ):
                fence_character = None
                fence_length = 0
                fence_container = ()
            offset += len(line)
            continue

        if not in_comment:
            opening = _container_fence(content)
            if opening is not None:
                fence, fence_container, suffix = opening
                if fence[0] == "`" and "`" in suffix:
                    opening = None
            if opening is not None:
                fence_character = fence[0]
                fence_length = len(fence)
                lines.append(_mask_non_newline_characters(line))
                offset += len(line)
                continue

        in_comment = _html_comment_state_after(
            line,
            offset=offset,
            protected=protected,
            in_comment=in_comment,
        )
        lines.append(line)
        offset += len(line)
    return "".join(lines)


def _inline_code_protection(value: str) -> list[bool]:
    protected = [False] * len(value)
    cursor = 0
    while cursor < len(value):
        if value.startswith("<!--", cursor):
            comment_end = value.find("-->", cursor + 4)
            if comment_end < 0:
                break
            cursor = comment_end + 3
            continue
        if value[cursor] != "`":
            cursor += 1
            continue

        opening_start = cursor
        opening_end = opening_start + 1
        while opening_end < len(value) and value[opening_end] == "`":
            opening_end += 1
        if _backtick_run_is_escaped(value, opening_start):
            cursor = opening_end
            continue
        opening_length = opening_end - opening_start
        closing_start = opening_end
        while closing_start < len(value):
            closing_start = value.find("`", closing_start)
            if closing_start < 0:
                break
            closing_end = closing_start + 1
            while closing_end < len(value) and value[closing_end] == "`":
                closing_end += 1
            if closing_end - closing_start == opening_length:
                for index in range(opening_start, closing_end):
                    protected[index] = True
                cursor = closing_end
                break
            closing_start = closing_end
        else:
            closing_start = -1
        if closing_start < 0:
            cursor = opening_end
    return protected


def _mask_html_comments(value: str, protected: list[bool]) -> str:
    masked = list(value)
    cursor = 0
    in_comment = False
    while cursor < len(value):
        delimiter = "-->" if in_comment else "<!--"
        if (
            value.startswith(delimiter, cursor)
            and (
                in_comment
                or not _delimiter_is_protected(protected, cursor, len(delimiter))
            )
        ):
            for index in range(cursor, cursor + len(delimiter)):
                if value[index] not in "\r\n":
                    masked[index] = " "
            cursor += len(delimiter)
            in_comment = not in_comment
            continue
        if in_comment and value[cursor] not in "\r\n":
            masked[cursor] = " "
        cursor += 1
    return "".join(masked)


def _mask_nonrendered_markdown(value: str) -> str:
    fenced = _mask_fenced_markdown(value)
    for _ in range(2):
        protected = _inline_code_protection(fenced)
        stabilized = _mask_fenced_markdown(value, protected=protected)
        if stabilized == fenced:
            break
        fenced = stabilized
    protected = _inline_code_protection(fenced)
    return _mask_html_comments(fenced, protected)


def _section_matches(body: str, heading: str) -> list[re.Match]:
    return list(
        re.finditer(
            rf"^{re.escape(heading)}[ \t]*(?=\r?$)",
            body,
            flags=re.MULTILINE,
        )
    )


def _section_body(body: str, heading_match: re.Match) -> str:
    next_heading = re.search(
        r"^##(?:[ \t]+[^\r\n]*)?(?=\r?$)",
        body[heading_match.end() :],
        flags=re.MULTILINE,
    )
    end = heading_match.end() + next_heading.start() if next_heading else len(body)
    return body[heading_match.end() : end]


def _strip_markdown_artifacts(value: str) -> str:
    line = value.strip()
    while True:
        quote = re.match(r"^>\s*", line)
        if quote:
            line = line[quote.end() :]
            continue
        list_item = re.match(r"^(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)", line)
        if list_item:
            line = line[list_item.end() :]
            continue
        break
    line = re.sub(r"^\[(?: |x|X)\]\s*", "", line)
    line = HTML_TAG_RE.sub("", line)
    return line.strip().strip("*_`~#>[](){}|\\-+.!").strip()


def _content_lines(section: str) -> list[str]:
    content = []
    visible_section = HTML_TAG_RE.sub("", _mask_nonrendered_markdown(section))
    for raw_line in visible_section.splitlines():
        line = _strip_markdown_artifacts(raw_line)
        if line:
            content.append(line)
    return content


def _is_placeholder_only(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", _strip_markdown_artifacts(value)).casefold()
    normalized = normalized.rstrip(":").rstrip()
    placeholders = {
        re.sub(r"\s+", " ", _strip_markdown_artifacts(placeholder))
        .casefold()
        .rstrip(":")
        .rstrip()
        for placeholder in SECTION_PLACEHOLDERS
    }
    return normalized in placeholders


def _has_substantive_content(section: str) -> bool:
    content = _content_lines(section)
    return bool(content) and any(not _is_placeholder_only(line) for line in content)


def validate_canonical_task_sections(body: str) -> list[str]:
    errors = []
    sections = {}
    visible_body = _mask_nonrendered_markdown(body)
    for heading in CANONICAL_SECTIONS:
        matches = _section_matches(visible_body, heading)
        if not matches:
            errors.append(f"missing canonical section: {heading}")
            continue
        if len(matches) > 1:
            errors.append(f"duplicate canonical section: {heading} (found {len(matches)})")
            continue
        sections[heading] = _section_body(visible_body, matches[0])

    for heading in (SUMMARY_SECTION, ACCEPTANCE_CRITERIA_SECTION, VERIFICATION_PLAN_SECTION):
        section = sections.get(heading)
        if section is not None and not _has_substantive_content(section):
            errors.append(f"empty or placeholder-only canonical section: {heading}")
    return errors


def validate_implementation_risk_analysis(body):
    visible_body = _mask_nonrendered_markdown(body)
    match = re.search(
        rf"^{re.escape(RISK_SECTION)}[ \t]*(?:\r?\n|\Z)"
        rf"(?P<section>.*?)(?=^##\s|\Z)",
        visible_body,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        return [f"missing {RISK_SECTION} section"]

    section = match.group("section")
    errors = []
    for label in RISK_REQUIRED_LABELS:
        value = _risk_label_value(section, label)
        if value is None:
            errors.append(f"missing risk-analysis label: {label}")
            continue
        if not _has_substantive_content(value):
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


def _skip_refinement_enabled(frontmatter) -> bool:
    return _normalize_bool(frontmatter.get("skip_refinement")) is True


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


def _live_markdown_row_details(
    value: str,
    *,
    expected_container: tuple[tuple[str, int], ...] = (),
) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    cursor = 0
    container = list(expected_container)
    for kind, continuation_indent in expected_container:
        if kind == "quote":
            quote = re.match(r" {0,3}>[ \t]?", value[cursor:])
            if not quote:
                return None
            cursor += quote.end()
            continue

        width = 0
        while cursor < len(value) and value[cursor] in " \t" and width < continuation_indent:
            character_width = 4 - (width % 4) if value[cursor] == "\t" else 1
            if width + character_width > continuation_indent:
                return None
            width += character_width
            cursor += 1
        if width != continuation_indent:
            return None

    while cursor < len(value):
        quote = re.match(r" {0,3}>[ \t]?", value[cursor:])
        if quote:
            container.append(("quote", 0))
            cursor += quote.end()
            continue

        list_item = re.match(
            r"(?P<leading> {0,3})(?:[-+*]|\d{1,9}[.)])"
            r"(?P<spacing>[ \t]+)",
            value[cursor:],
        )
        if list_item:
            if _indent_width(list_item.group("spacing")) > 4:
                return None
            container.append(("list", _indent_width(list_item.group(0))))
            cursor += list_item.end()
            continue
        break

    indentation = re.match(r"[ \t]*", value[cursor:]).group()
    if _indent_width(indentation) > 3:
        return None
    return value[cursor + len(indentation) :], tuple(container)


def _live_markdown_row(value: str) -> str | None:
    details = _live_markdown_row_details(value)
    return details[0] if details is not None else None


def _risk_label_match(row: str) -> tuple[str, str] | None:
    for candidate in ALL_RISK_LABELS:
        label_match = re.match(
            rf"^(?:\*\*|__)?{re.escape(candidate)}(?:\*\*|__)?"
            rf"[ \t]*(?P<value>.*)$",
            row,
        )
        if label_match:
            return candidate, label_match.group("value").strip()
    return None


def _risk_label_value(section: str, label: str) -> str | None:
    visible_section = HTML_TAG_RE.sub("", section)
    lines = visible_section.splitlines()
    for index, raw_line in enumerate(lines):
        row_details = _live_markdown_row_details(raw_line)
        if row_details is None:
            continue
        row, label_container = row_details
        label_match = _risk_label_match(row)
        if label_match is None or label_match[0] != label:
            continue
        if label_match[1]:
            return label_match[1]

        continuation_rows = []
        previous_container = label_container
        for continuation_line in lines[index + 1 :]:
            live_row = _live_markdown_row(continuation_line)
            live_label = _risk_label_match(live_row) if live_row is not None else None
            if live_label is not None:
                break
            if not continuation_line.strip():
                continue

            continuation = _live_markdown_row_details(
                continuation_line,
                expected_container=previous_container,
            )
            if continuation is None and previous_container != label_container:
                continuation = _live_markdown_row_details(
                    continuation_line,
                    expected_container=label_container,
                )
            if continuation is None:
                break
            continuation_rows.append(continuation[0])
            previous_container = continuation[1]
        return "\n".join(continuation_rows)
    return None


def validate_design_thinking_refinement(frontmatter, body):
    if _normalize_bool(frontmatter.get("ui_ux_lane")) is not True:
        return []
    if _normalize_text(frontmatter.get("ui_ux_mode")) != "implementation":
        return []

    visible_body = _mask_nonrendered_markdown(body)
    match = re.search(
        rf"^{re.escape(RISK_SECTION)}[ \t]*(?:\r?\n|\Z)"
        rf"(?P<section>.*?)(?=^##\s|\Z)",
        visible_body,
        flags=re.MULTILINE | re.DOTALL,
    )
    section = match.group("section") if match else ""
    errors = []
    budget_line = _risk_label_value(section, DESIGN_THINKING_BUDGET_LABEL)
    if budget_line is None:
        errors.append(f"missing risk-analysis label: {DESIGN_THINKING_BUDGET_LABEL}")
    elif not _has_substantive_content(budget_line) or RISK_PLACEHOLDER in budget_line:
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
    elif not _has_substantive_content(seeds_line) or RISK_PLACEHOLDER in seeds_line:
        errors.append(f"unresolved risk-analysis value: {DESIGN_THINKING_SEEDS_LABEL}")
    return errors


def validate_refinement(frontmatter, body):
    direct_app_errors, _ = validate_direct_app_policy(frontmatter)
    if _skip_refinement_enabled(frontmatter):
        return direct_app_errors

    errors = list(direct_app_errors)
    errors.extend(validate_canonical_task_sections(body))
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

    if _skip_refinement_enabled(fm):
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
