#!/usr/bin/env python3
"""
project_design_queue.py — show/validate/render a project-level design-first queue.

Usage:
  python3 bin/project_design_queue.py show --project amiga
  python3 bin/project_design_queue.py ready-context --project amiga
  python3 bin/project_design_queue.py bridge-status --project amiga --json
  python3 bin/project_design_queue.py record-computer-use-timeout --project amiga
  python3 bin/project_design_queue.py desktop-prompt --project amiga
  python3 bin/project_design_queue.py ensure-bridge-metadata --project amiga --all-active
  python3 bin/project_design_queue.py validate --project amiga
  python3 bin/project_design_queue.py validate --project amiga --check-github
  python3 bin/project_design_queue.py validate --project amiga --check-github --json
  python3 bin/project_design_queue.py sync-markdown --project amiga
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent))
import project_issue_queue as issue_queue
import claude_desktop_bridge_health as bridge_health
from _helpers import ROOT, display_path, dump_frontmatter, find_task_by_id, get_project, parse_frontmatter, project_state_dir, utc_iso, write_file

QUEUE_STATES = {"ready", "queued", "blocked", "active", "review", "done"}
DESIGN_FILE_NAME = "design-queue.json"
MARKDOWN_FILE_NAME = "design-queue.md"
HISTORY_DIR_NAME = "history"
ISSUE_QUEUE_FILE_NAME = "issue-queue.json"
BRIDGE_STATE_FILE_NAME = "claude-desktop-bridge-state.json"
COMPUTER_USE_TIMEOUT_COOLDOWN_SECONDS = 30 * 60
COMPUTER_USE_TIMEOUT_MAX_COOLDOWN_SECONDS = 2 * 60 * 60
DESIGN_OWNER = "claude"
DESIGN_LANE_KEYWORDS = (
    "design",
    "surface",
    "handoff",
    "parity",
    "template",
    "audit",
    "split",
    "shaping",
    "spec",
)
MATERIALIZED_ARTIFACT_QUEUE_STATES = {"ready", "active", "review"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show or validate a project design queue.")
    parser.add_argument(
        "command",
        choices=(
            "show",
            "ready-context",
            "bridge-status",
            "record-computer-use-timeout",
            "desktop-prompt",
            "ensure-bridge-metadata",
            "validate",
            "sync-markdown",
            "archive-complete",
        ),
    )
    parser.add_argument("--project", required=True, help="project_id from projects.json")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON when supported.")
    parser.add_argument(
        "--all-active",
        action="store_true",
        help="For ensure-bridge-metadata, update every non-done lane instead of only the current ready lane.",
    )
    parser.add_argument(
        "--check-github",
        action="store_true",
        help="Use gh to verify active queued issues are still open.",
    )
    parser.add_argument(
        "--reason",
        default="computer-use get_app_state timed out",
        help="Reason to store for record-computer-use-timeout.",
    )
    return parser.parse_args()


def queue_json_path(project_id: str) -> Path:
    return project_state_dir(project_id) / DESIGN_FILE_NAME


def queue_markdown_path(project_id: str) -> Path:
    return project_state_dir(project_id) / MARKDOWN_FILE_NAME


def queue_history_dir(project_id: str) -> Path:
    return project_state_dir(project_id) / HISTORY_DIR_NAME


def queue_exists(project_id: str) -> bool:
    return queue_json_path(project_id).exists()


def issue_queue_json_path(project_id: str) -> Path:
    return project_state_dir(project_id) / ISSUE_QUEUE_FILE_NAME


def bridge_state_path(project_id: str) -> Path:
    return project_state_dir(project_id) / BRIDGE_STATE_FILE_NAME


def load_queue(project_id: str) -> dict:
    if get_project(project_id) is None:
        raise SystemExit(f"[error] Unknown project_id: {project_id!r}")
    path = queue_json_path(project_id)
    if not path.exists():
        raise SystemExit(f"[error] Design queue file not found: {path}")
    return json.loads(path.read_text())


def load_issue_queue(project_id: str) -> dict | None:
    path = issue_queue_json_path(project_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_bridge_state(project_id: str) -> dict:
    path = bridge_state_path(project_id)
    if not path.exists():
        return {"project_id": project_id, "computer_use_timeouts": {}}
    return json.loads(path.read_text())


def save_bridge_state(project_id: str, payload: dict) -> None:
    write_file(bridge_state_path(project_id), json.dumps(payload, indent=2) + "\n")


def save_queue(project_id: str, payload: dict) -> None:
    write_file(queue_json_path(project_id), json.dumps(payload, indent=2) + "\n")


def normalize_depends(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def normalize_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def github_repo(project_id: str) -> str | None:
    project = get_project(project_id)
    if not project:
        return None
    github = project.get("github")
    if not isinstance(github, dict):
        return None
    repo = github.get("repo")
    return str(repo) if repo else None


def github_issue_state(repo: str, issue: int) -> str | None:
    if shutil.which("gh") is None:
        raise RuntimeError("gh CLI not found")
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            repo,
            "--json",
            "state",
            "--jq",
            ".state",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh issue view failed for GH-{issue}")
    return result.stdout.strip()


def validate_queue(
    project_id: str,
    payload: dict,
    *,
    check_github: bool,
    check_issue_mirror: bool = True,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if payload.get("project_id") != project_id:
        errors.append(
            f"queue project_id mismatch: expected {project_id!r}, found {payload.get('project_id')!r}"
        )
    if payload.get("artifact_type") != "ordered_design_queue":
        errors.append(f"artifact_type must be 'ordered_design_queue', found {payload.get('artifact_type')!r}")

    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        errors.append("queue must contain a 'lanes' array")
        return errors, warnings

    repo = github_repo(project_id)
    if check_github and not repo:
        errors.append(f"project {project_id!r} has no github.repo configured")

    seen_orders: set[int] = set()
    seen_tasks: set[str] = set()
    ready_orders: list[int] = []
    orders: list[int] = []

    for lane in lanes:
        order = lane.get("order")
        issue = lane.get("issue")
        task_id = lane.get("task_id")
        owner = lane.get("owner")
        task_status = lane.get("task_status")
        queue_state = lane.get("queue_state")
        lane_type = str(lane.get("lane_type") or "")
        depends_on = normalize_depends(lane.get("depends_on"))

        if not isinstance(order, int):
            errors.append(f"lane has non-integer order: {lane!r}")
            continue
        orders.append(order)
        if order in seen_orders:
            errors.append(f"duplicate queue order: {order}")
        seen_orders.add(order)

        if not isinstance(issue, int):
            errors.append(f"lane {order} has non-integer issue: {issue!r}")
        if not isinstance(task_id, str):
            errors.append(f"lane {order} missing string task_id")
            continue
        if task_id in seen_tasks:
            errors.append(f"duplicate task_id in design queue: {task_id}")
        seen_tasks.add(task_id)

        if queue_state not in QUEUE_STATES:
            errors.append(f"lane {order} has invalid queue_state {queue_state!r}")
        elif queue_state == "ready":
            ready_orders.append(order)

        if owner != DESIGN_OWNER and queue_state != "done":
            errors.append(f"lane {order} owner must be {DESIGN_OWNER!r} for active design work, found {owner!r}")

        if not any(keyword in lane_type for keyword in DESIGN_LANE_KEYWORDS):
            errors.append(f"lane {order} lane_type does not look design-first: {lane_type!r}")

        task_path = find_task_by_id(task_id)
        if task_path is None:
            errors.append(f"lane {order} task mirror not found: {task_id}")
            continue
        frontmatter, _ = parse_frontmatter(task_path.read_text())

        if frontmatter.get("owner") != owner:
            errors.append(
                f"lane {order} owner mismatch for {task_id}: queue {owner!r} vs task {frontmatter.get('owner')!r}"
            )
        if frontmatter.get("status") != task_status:
            errors.append(
                f"lane {order} task_status mismatch for {task_id}: queue {task_status!r} vs task {frontmatter.get('status')!r}"
            )
        if normalize_depends(frontmatter.get("depends_on")) != depends_on:
            errors.append(
                f"lane {order} depends_on mismatch for {task_id}: queue {depends_on!r} vs task {normalize_depends(frontmatter.get('depends_on'))!r}"
            )
        if frontmatter.get("ui_ux_lane") is not True:
            errors.append(f"lane {order} task {task_id} must set ui_ux_lane: true")

        dependency_artifacts = normalize_string_list(frontmatter.get("required_dependency_artifacts"))
        dependency_gate_enabled = frontmatter.get("dependency_materialization_gate") is True or bool(
            dependency_artifacts
        )
        if dependency_gate_enabled and not dependency_artifacts:
            errors.append(f"lane {order} task {task_id} enables dependency materialization gate without artifacts")
        if dependency_gate_enabled and queue_state in MATERIALIZED_ARTIFACT_QUEUE_STATES:
            worktree_value = clean_scalar(frontmatter.get("worktree"))
            if not isinstance(worktree_value, str) or not worktree_value or worktree_value in {"null", "<none>"}:
                errors.append(
                    f"lane {order} task {task_id} requires materialized dependency artifacts but has no worktree"
                )
            else:
                worktree = Path(worktree_value)
                if not worktree.exists():
                    errors.append(
                        f"lane {order} task {task_id} dependency-artifact worktree does not exist: {worktree}"
                    )
                else:
                    for artifact in dependency_artifacts:
                        artifact_path = Path(artifact)
                        candidate = artifact_path if artifact_path.is_absolute() else worktree / artifact_path
                        if not candidate.exists():
                            errors.append(
                                f"lane {order} task {task_id} missing dependency artifact in assigned worktree: {artifact}"
                            )

        if check_github and repo and isinstance(issue, int):
            try:
                state = github_issue_state(repo, issue)
            except RuntimeError as exc:
                errors.append(f"lane {order} GH-{issue} state check failed: {exc}")
            else:
                if queue_state != "done" and state != "OPEN":
                    errors.append(f"lane {order} GH-{issue} is {state}; remove or archive this design lane")

        if queue_state == "blocked" and not lane.get("blocked_by") and not depends_on:
            warnings.append(f"lane {order} is blocked but has no blocked_by/depends_on evidence")

    if orders and sorted(orders) != list(range(1, len(orders) + 1)):
        errors.append(f"queue orders must be contiguous starting at 1, found {sorted(orders)!r}")
    if len(ready_orders) > 1:
        errors.append(f"queue has multiple ready lanes: orders {sorted(ready_orders)!r}")
    elif ready_orders and ready_orders[0] != min(orders):
        warnings.append(f"ready lane order {ready_orders[0]} is not the earliest queue order {min(orders)}")

    if check_issue_mirror:
        validate_issue_queue_mirror(project_id, payload, errors)

    return errors, warnings


def lane_mirror_fields(lane: dict) -> dict:
    return {
        "order": lane.get("order"),
        "issue": lane.get("issue"),
        "task_id": lane.get("task_id"),
        "owner": lane.get("owner"),
        "task_status": lane.get("task_status"),
        "queue_state": lane.get("queue_state"),
        "lane_type": lane.get("lane_type") or "design",
        "depends_on": normalize_depends(lane.get("depends_on")),
    }


def active_design_lanes(design_payload: dict) -> list[dict]:
    return [
        lane
        for lane in design_payload.get("lanes", [])
        if isinstance(lane, dict) and lane.get("queue_state") != "done"
    ]


def validate_issue_queue_mirror(project_id: str, design_payload: dict, errors: list[str]) -> None:
    design_lanes = active_design_lanes(design_payload)
    if not design_lanes:
        return

    issue_payload = load_issue_queue(project_id)
    if issue_payload is None:
        errors.append(
            f"active design queue must be mirrored by issue queue, but {issue_queue_json_path(project_id)} is missing"
        )
        return

    if issue_payload.get("artifact_type") != "ordered_issue_queue":
        errors.append(
            f"issue queue mirror artifact_type must be 'ordered_issue_queue', found {issue_payload.get('artifact_type')!r}"
        )

    issue_lanes = [
        lane
        for lane in issue_payload.get("lanes", [])
        if isinstance(lane, dict) and lane.get("queue_state") != "done"
    ]
    if len(issue_lanes) != len(design_lanes):
        errors.append(
            "issue queue mirror lane count mismatch: "
            f"design has {len(design_lanes)} active lane(s), issue queue has {len(issue_lanes)}"
        )

    issue_by_task = {str(lane.get("task_id")): lane for lane in issue_lanes}
    for design_lane in design_lanes:
        task_id = str(design_lane.get("task_id"))
        issue_lane = issue_by_task.get(task_id)
        if issue_lane is None:
            errors.append(f"issue queue mirror missing active design lane {task_id}")
            continue
        expected = lane_mirror_fields(design_lane)
        actual = lane_mirror_fields(issue_lane)
        if actual != expected:
            errors.append(
                f"issue queue mirror mismatch for {task_id}: expected {expected!r}, found {actual!r}"
            )


def render_markdown(payload: dict) -> str:
    configured_project_id = payload.get("project_id")
    project_id = str(configured_project_id or "project")
    project = get_project(project_id) if configured_project_id else None
    project_name = (project or {}).get("display_name") or (project_id if configured_project_id else "Project")
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
    completed = payload.get("completed_recently", [])
    last_updated = payload.get("last_updated_utc", "unknown")
    mode = payload.get("mode", "unknown")
    current_lane = current_bridge_lane({"lanes": lanes})
    next_queued = next((lane for lane in lanes if lane.get("queue_state") == "queued"), None)

    lines = [
        f"# {project_name} Design Queue",
        "",
        f"> Source: `design-queue.json`. This is a legacy design-first queue artifact. New design lanes should live in `issue-queue.json` with a design `lane_type`.",
        "> Do not treat an empty design queue as proof that project work is done; validate the GitHub-backed issue queue.",
        "",
        f"- Last updated: `{last_updated}`",
        f"- Current mode: `{mode}`",
    ]

    if current_lane:
        lines.append(
            f"- Active lane: `GH-{current_lane['issue']}` / `{current_lane['task_id']}` / `{current_lane['owner']}` / `{current_lane.get('queue_state', '-')}`"
        )
    else:
        lines.append("- Active lane: none")

    if next_queued:
        lines.append(
            f"- Next queued lane: `GH-{next_queued['issue']}` / `{next_queued['task_id']}` / `{next_queued['owner']}`"
        )
    else:
        lines.append("- Next queued lane: none")

    lines.extend(
        [
            "",
            "## Rule",
            "",
            "Legacy design queues are migration artifacts. Keep active design dependencies represented in the canonical issue queue before activating code implementation lanes.",
            "",
            "Use `project_issue_queue.py validate --project <project_id>` for backlog truth. `project_design_queue.py` can still inspect existing design queues and bridge metadata while those projects migrate.",
        ]
    )

    if completed:
        lines.extend(["", "## Recently Completed", ""])
        for item in completed:
            lines.append(
                f"- `GH-{item['issue']}` / `{item['task_id']}` / {item.get('status', 'done')} - {item.get('title', '-')}"
            )

    lines.extend(["", "## Remaining Design Queue", ""])
    if not lanes:
        lines.append("No remaining design lanes.")
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "| Order | Phase | Issue | Task | Owner | Status | Queue | Lane Type | Depends On | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for lane in lanes:
        depends_on = ",".join(lane.get("depends_on", [])) or "-"
        notes = str(lane.get("notes", "-")).replace("|", "/")
        lines.append(
            f"| {lane['order']} | {lane.get('phase', '-')} | GH-{lane['issue']} | {lane['task_id']} | "
            f"{lane['owner']} | {lane['task_status']} | {lane['queue_state']} | "
            f"{lane.get('lane_type', '-')} | {depends_on} | {notes} |"
        )

    lines.extend(
        [
            "",
            "## After This Queue",
            "",
            "When a lane completes, Codex must:",
            "",
            "- verify the task mirror says `done` and has Claude's design-thinking pass",
            "- remove the lane from `design-queue.json`",
            "- regenerate `design-queue.md`",
            "- update the implementation queue only from completed design handoffs",
            "- keep old code tasks blocked until their design dependency is explicit",
        ]
    )

    return "\n".join(lines) + "\n"


def validation_status_line(payload: dict) -> str:
    lane = current_bridge_lane(payload)
    if lane:
        return (
            f"current design lane: GH-{lane['issue']} / {lane['task_id']} / "
            f"{lane['owner']} / {lane.get('queue_state', '-')}"
        )
    if payload.get("lanes"):
        return "design queue has no current lane"
    return "design queue empty"


def show_queue(payload: dict) -> str:
    lanes = sorted(payload.get("lanes", []), key=lambda item: item.get("order", 0))
    if not lanes:
        return "(no remaining design lanes)"
    lines = []
    for lane in lanes:
        depends_on = ",".join(lane.get("depends_on", [])) or "-"
        lines.append(
            f"{lane.get('order'):>2}. GH-{lane.get('issue')} | {lane.get('task_id')} | "
            f"{lane.get('owner')} | task={lane.get('task_status')} | "
            f"queue={lane.get('queue_state')} | depends={depends_on} | {lane.get('title')}"
        )
    return "\n".join(lines)


def next_ready_lane(payload: dict) -> dict | None:
    return next(
        (
            lane
            for lane in sorted(payload.get("lanes", []), key=lambda lane: lane.get("order", 0))
            if lane.get("queue_state") == "ready"
        ),
        None,
    )


def current_bridge_lane(payload: dict) -> dict | None:
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane.get("order", 0))
    return (
        next((lane for lane in lanes if lane.get("queue_state") == "active"), None)
        or next((lane for lane in lanes if lane.get("queue_state") == "review"), None)
        or next((lane for lane in lanes if lane.get("queue_state") == "ready"), None)
    )


def bridge_title_for_lane(lane: dict, frontmatter: dict) -> str:
    configured = frontmatter.get("claude_desktop_thread_title")
    if isinstance(configured, str) and configured.strip():
        return clean_scalar(configured)
    title = str(lane.get("title") or frontmatter.get("title") or lane.get("task_id") or "Design lane")
    short = title.replace(" design", "").replace("Design ", "")
    return short[:64].strip()


def bridge_label_for_lane(lane: dict, frontmatter: dict) -> str:
    configured = frontmatter.get("bridge_short_label")
    if isinstance(configured, str) and configured.strip():
        return clean_scalar(configured)
    title = str(lane.get("title") or frontmatter.get("title") or lane.get("task_id") or "design")
    title = title.replace(" and ", " ")
    title = title.replace(",", "")
    for suffix in (" design contract", " design refresh", " design audit", " design", " UX design"):
        title = title.replace(suffix, "")
    words = [word for word in title.lower().split() if word not in {"the", "and", "for", "with", "before"}]
    return " ".join(words[:5]).strip() or "design lane"


def clean_scalar(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def ready_context(project_id: str, payload: dict) -> dict:
    lane = current_bridge_lane(payload)
    if lane is None:
        return {
            "project_id": project_id,
            "ready": False,
            "queue_empty": not bool(payload.get("lanes")),
            "active_lane_present": any(
                isinstance(candidate, dict) and candidate.get("queue_state") == "active"
                for candidate in payload.get("lanes", [])
            ),
        }

    task_id = str(lane["task_id"])
    task_path = find_task_by_id(task_id)
    frontmatter: dict = {}
    if task_path is not None:
        frontmatter, _ = parse_frontmatter(task_path.read_text())

    bridge_uuid = clean_scalar(frontmatter.get("bridge_thread_uuid"))
    visible_prefix = clean_scalar(frontmatter.get("bridge_visible_prefix"))
    if isinstance(bridge_uuid, str) and bridge_uuid and not visible_prefix:
        visible_prefix = f"[BRIDGE {bridge_uuid[:8]}] GH-{lane['issue']} {bridge_title_for_lane(lane, frontmatter)}"

    required_bridge_fields = {
        "bridge_thread_uuid": bridge_uuid,
        "bridge_visible_prefix": visible_prefix,
        "claude_activation_message_path": clean_scalar(frontmatter.get("claude_activation_message_path")),
        "branch": clean_scalar(frontmatter.get("branch")),
        "worktree": clean_scalar(frontmatter.get("worktree")),
    }
    missing_bridge_metadata = [
        name for name, value in required_bridge_fields.items() if not value or value in {"null", "<none>"}
    ]

    return {
        "project_id": project_id,
        "ready": True,
        "issue": lane.get("issue"),
        "task_id": task_id,
        "owner": lane.get("owner"),
        "title": lane.get("title"),
        "queue_state": lane.get("queue_state"),
        "depends_on": normalize_depends(lane.get("depends_on")),
        "task_path": str(task_path) if task_path else None,
        "related_chat": clean_scalar(frontmatter.get("related_chat")),
        "branch": clean_scalar(frontmatter.get("branch")),
        "worktree": clean_scalar(frontmatter.get("worktree")),
        "bridge_thread_uuid": bridge_uuid,
        "bridge_visible_prefix": visible_prefix,
        "claude_desktop_thread_title": bridge_title_for_lane(lane, frontmatter),
        "claude_activation_message_path": required_bridge_fields["claude_activation_message_path"],
        "missing_bridge_metadata": missing_bridge_metadata,
        "bridge_metadata_complete": not missing_bridge_metadata,
    }


def render_ready_context(context: dict) -> str:
    if not context.get("ready"):
        return "No ready design lane." if not context.get("queue_empty") else "Design queue empty."
    lines = [
        f"Ready design lane: GH-{context['issue']} / {context['task_id']} / {context['owner']}",
        f"Title: {context.get('title')}",
        f"Task: {context.get('task_path')}",
        f"Chat: {context.get('related_chat')}",
        f"Worktree: {context.get('worktree')}",
        f"Branch: {context.get('branch')}",
        f"Claude thread title: {context.get('claude_desktop_thread_title')}",
        f"Bridge UUID: {context.get('bridge_thread_uuid')}",
        f"Bridge prefix: {context.get('bridge_visible_prefix')}",
        f"Activation message: {context.get('claude_activation_message_path')}",
        f"Missing bridge metadata: {', '.join(context.get('missing_bridge_metadata', [])) or '-'}",
        f"Bridge metadata complete: {context.get('bridge_metadata_complete')}",
    ]
    return "\n".join(lines)


def render_desktop_prompt(context: dict) -> str:
    if not context.get("ready"):
        raise SystemExit("[error] No ready design lane.")
    if not context.get("bridge_metadata_complete"):
        missing = ", ".join(context.get("missing_bridge_metadata", [])) or "unknown"
        raise SystemExit(f"[error] Ready design lane is missing bridge metadata: {missing}")

    return "\n".join(
        [
            str(context["bridge_visible_prefix"]),
            f"bridge_thread_uuid: {context['bridge_thread_uuid']}",
            f"Project: {context['project_id']}",
            f"Current lane: GH-{context['issue']} / {context['task_id']} — {context['title']}",
            "",
            "Please read the llm-collab activation packet and work only in the assigned worktree/branch:",
            str(context["claude_activation_message_path"]),
            "",
            "Assigned worktree:",
            str(context["worktree"]),
            "",
            "Assigned branch:",
            str(context["branch"]),
            "",
            "Start with session bootstrap as claude, then follow the activation packet exactly. Do not use the Claude CLI bridge. Do not write into the project's main checkout or any internal .claude worktree.",
        ]
    )


def relative_workspace_path(path_value: object) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value)
    try:
        return str(path.resolve().relative_to(ROOT))
    except (OSError, ValueError):
        return path_value


def load_agent_inbox(agent: str) -> dict:
    path = ROOT / "agents" / agent / "inbox.json"
    if not path.exists():
        return {"unread": [], "read": [], "missing": True}
    return json.loads(path.read_text())


def message_frontmatter_from_relpath(relpath: str) -> dict:
    path = ROOT / relpath
    if not path.exists():
        return {}
    frontmatter, _ = parse_frontmatter(path.read_text())
    return frontmatter


def activation_packet_state(context: dict) -> str:
    activation_path = relative_workspace_path(context.get("claude_activation_message_path"))
    if not activation_path:
        return "missing"
    inbox = load_agent_inbox("claude")
    if activation_path in inbox.get("unread", []):
        return "unread"
    if activation_path in inbox.get("read", []):
        return "read"
    return "not-indexed"


def unread_messages_from(agent: str, *, sender: str, project_id: str | None) -> list[str]:
    inbox = load_agent_inbox(agent)
    matches: list[str] = []
    for relpath in inbox.get("unread", []):
        frontmatter = message_frontmatter_from_relpath(str(relpath))
        if frontmatter.get("from") != sender and frontmatter.get("sender_agent_id") != sender:
            continue
        if project_id and frontmatter.get("project_id") != project_id:
            continue
        matches.append(str(relpath))
    return matches


def worktree_state(worktree_value: object) -> dict:
    if not isinstance(worktree_value, str) or not worktree_value:
        return {"exists": False, "dirty": False, "status_entries": [], "head": None}
    worktree = Path(worktree_value)
    if not worktree.exists():
        return {"exists": False, "dirty": False, "status_entries": [], "head": None}

    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--short", "--branch", "--untracked-files=all"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    head = subprocess.run(
        ["git", "-C", str(worktree), "log", "-1", "--oneline", "--decorate"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status_lines = [line for line in status.stdout.splitlines() if line.strip()]
    entries = [line for line in status_lines if not line.startswith("## ")]
    return {
        "exists": True,
        "dirty": bool(entries),
        "status_header": status_lines[0] if status_lines else "",
        "status_entries": entries,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "errors": {
            "status": status.stderr.strip() if status.returncode != 0 else "",
            "head": head.stderr.strip() if head.returncode != 0 else "",
        },
    }


def task_state(context: dict) -> dict:
    task_path_value = context.get("task_path")
    if not isinstance(task_path_value, str) or not task_path_value:
        return {"exists": False, "status": None, "activity_log_entries": 0}
    path = Path(task_path_value)
    if not path.exists():
        return {"exists": False, "status": None, "activity_log_entries": 0}
    frontmatter, body = parse_frontmatter(path.read_text())
    return {
        "exists": True,
        "status": frontmatter.get("status"),
        "owner": frontmatter.get("owner"),
        "activity_log_entries": body.count("\n- "),
    }


def parse_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def computer_use_timeout_status(project_id: str, task_id: object, *, now: datetime | None = None) -> dict:
    if not isinstance(task_id, str) or not task_id:
        return {"active": False, "last_timeout_utc": None, "cooldown_until_utc": None, "seconds_remaining": 0}

    state = load_bridge_state(project_id)
    entry = state.get("computer_use_timeouts", {}).get(task_id, {})
    last_timeout = parse_utc_datetime(entry.get("last_timeout_utc"))
    if last_timeout is None:
        return {"active": False, "last_timeout_utc": None, "cooldown_until_utc": None, "seconds_remaining": 0}

    current = now or datetime.now(timezone.utc)
    try:
        timeout_count = max(1, int(entry.get("timeout_count", 1)))
    except (TypeError, ValueError):
        timeout_count = 1
    cooldown_seconds = min(
        COMPUTER_USE_TIMEOUT_COOLDOWN_SECONDS * (2 ** (timeout_count - 1)),
        COMPUTER_USE_TIMEOUT_MAX_COOLDOWN_SECONDS,
    )
    cooldown_until = last_timeout + timedelta(seconds=cooldown_seconds)
    seconds_remaining = max(0, int((cooldown_until - current).total_seconds()))
    next_check_seconds = min(seconds_remaining, 30 * 60) if seconds_remaining > 0 else 0
    return {
        "active": seconds_remaining > 0,
        "last_timeout_utc": entry.get("last_timeout_utc"),
        "cooldown_until_utc": cooldown_until.isoformat(),
        "cooldown_seconds": cooldown_seconds,
        "seconds_remaining": seconds_remaining,
        "recommended_next_check_seconds": next_check_seconds,
        "recommended_next_check_minutes": int((next_check_seconds + 59) / 60) if next_check_seconds else 0,
        "timeout_count": timeout_count,
        "reason": entry.get("reason"),
    }


def active_computer_use_timeout_count(entry: dict, *, now: datetime) -> int:
    last_timeout = parse_utc_datetime(entry.get("last_timeout_utc"))
    if last_timeout is None:
        return 0
    try:
        timeout_count = max(1, int(entry.get("timeout_count", 1)))
    except (TypeError, ValueError):
        timeout_count = 1
    cooldown_seconds = min(
        COMPUTER_USE_TIMEOUT_COOLDOWN_SECONDS * (2 ** (timeout_count - 1)),
        COMPUTER_USE_TIMEOUT_MAX_COOLDOWN_SECONDS,
    )
    if last_timeout + timedelta(seconds=cooldown_seconds) <= now:
        return 0
    return timeout_count


def record_computer_use_timeout(project_id: str, payload: dict, *, reason: str) -> dict:
    context = ready_context(project_id, payload)
    if not context.get("ready"):
        raise SystemExit("[error] No ready design lane to record a Computer Use timeout for.")

    task_id = str(context["task_id"])
    state = load_bridge_state(project_id)
    timeouts = state.setdefault("computer_use_timeouts", {})
    previous = timeouts.get(task_id, {})
    current = datetime.now(timezone.utc)
    current_iso = current.isoformat()
    previous_count = active_computer_use_timeout_count(previous, now=current)
    timeouts[task_id] = {
        "last_timeout_utc": current_iso,
        "timeout_count": previous_count + 1,
        "reason": reason,
        "issue": context.get("issue"),
        "bridge_thread_uuid": context.get("bridge_thread_uuid"),
        "worktree": context.get("worktree"),
        "branch": context.get("branch"),
    }
    state["project_id"] = project_id
    state["updated_utc"] = current_iso
    save_bridge_state(project_id, state)
    return {
        "project_id": project_id,
        "task_id": task_id,
        "path": display_path(bridge_state_path(project_id)),
        "computer_use_blocker": computer_use_timeout_status(project_id, task_id),
    }


def bridge_status(project_id: str, payload: dict) -> dict:
    context = ready_context(project_id, payload)
    health = bridge_health.collect_health()
    if not context.get("ready"):
        return {
            "project_id": project_id,
            "ready_context": context,
            "claude_health": health,
            "classification": "queue-empty" if context.get("queue_empty") else "no-ready-lane",
            "durable_progress": False,
        }

    task = task_state(context)
    worktree = worktree_state(context.get("worktree"))
    codex_handoffs = unread_messages_from("codex", sender="claude", project_id=project_id)
    activation_state = activation_packet_state(context)
    computer_use_blocker = computer_use_timeout_status(project_id, context.get("task_id"))
    task_status = task.get("status")
    durable_progress = bool(
        codex_handoffs
        or task_status in {"review", "blocked", "done"}
        or worktree.get("dirty")
    )

    health_metrics = health.get("claude_main_process_metrics", {})
    if not context.get("bridge_metadata_complete"):
        classification = "missing-bridge-metadata"
    elif durable_progress:
        classification = "durable-progress-visible"
    elif computer_use_blocker.get("active"):
        classification = "computer-use-cooldown-no-durable-progress"
    elif health_metrics.get("busy"):
        classification = "cpu-busy-no-durable-progress"
    else:
        classification = "idle-no-durable-progress"

    return {
        "project_id": project_id,
        "ready_context": context,
        "activation_packet_state": activation_state,
        "codex_unread_from_claude": codex_handoffs,
        "task": task,
        "worktree": worktree,
        "claude_health": health,
        "computer_use_blocker": computer_use_blocker,
        "durable_progress": durable_progress,
        "classification": classification,
    }


def render_bridge_status(status: dict) -> str:
    context = status.get("ready_context", {})
    if not context.get("ready"):
        return f"Bridge status: {status['classification']}"
    health_metrics = status.get("claude_health", {}).get("claude_main_process_metrics", {})
    lines = [
        f"Bridge status: {status['classification']}",
        f"Ready lane: GH-{context.get('issue')} / {context.get('task_id')} / {context.get('owner')}",
        f"Activation packet: {status.get('activation_packet_state')}",
        f"Durable progress: {status.get('durable_progress')}",
        f"Task status: {status.get('task', {}).get('status')}",
        f"Worktree dirty: {status.get('worktree', {}).get('dirty')}",
        f"Worktree head: {status.get('worktree', {}).get('head')}",
        f"Codex unread handoffs from Claude: {len(status.get('codex_unread_from_claude', []))}",
        f"Claude frontmost/visible: {status.get('claude_health', {}).get('claude_frontmost')} / {status.get('claude_health', {}).get('claude_visible')}",
        f"Claude CPU busy: {health_metrics.get('busy')} ({health_metrics.get('cpu_percent_total')}%)",
        f"Computer Use blocker active: {status.get('computer_use_blocker', {}).get('active')}",
    ]
    return "\n".join(lines)


def ensure_bridge_metadata(project_id: str, payload: dict, *, all_active: bool) -> dict:
    candidate_lanes = active_design_lanes(payload)
    if not all_active:
        ready = next_ready_lane(payload)
        candidate_lanes = [ready] if ready else []

    updated: list[dict] = []
    skipped: list[dict] = []
    for lane in candidate_lanes:
        if lane is None:
            continue
        task_id = str(lane.get("task_id"))
        task_path = find_task_by_id(task_id)
        if task_path is None:
            skipped.append({"task_id": task_id, "reason": "task mirror not found"})
            continue

        frontmatter, body = parse_frontmatter(task_path.read_text())
        changed = False
        for key in (
            "bridge_thread_uuid",
            "bridge_short_label",
            "bridge_visible_prefix",
            "claude_desktop_thread_title",
            "claude_activation_message_path",
            "branch",
            "worktree",
        ):
            cleaned = clean_scalar(frontmatter.get(key))
            if isinstance(cleaned, str) and frontmatter.get(key) != cleaned:
                frontmatter[key] = cleaned
                changed = True

        bridge_uuid = clean_scalar(frontmatter.get("bridge_thread_uuid"))
        if not bridge_uuid or bridge_uuid in {"null", "<none>"}:
            bridge_uuid = str(uuid.uuid4())
            frontmatter["bridge_thread_uuid"] = bridge_uuid
            changed = True

        if not clean_scalar(frontmatter.get("bridge_short_label")):
            frontmatter["bridge_short_label"] = f"GH-{lane.get('issue')} {bridge_label_for_lane(lane, frontmatter)}"
            changed = True

        if not clean_scalar(frontmatter.get("bridge_visible_prefix")):
            frontmatter["bridge_visible_prefix"] = f"[BRIDGE {bridge_uuid[:8]}] {frontmatter['bridge_short_label']}"
            changed = True

        if not clean_scalar(frontmatter.get("claude_desktop_thread_title")):
            frontmatter["claude_desktop_thread_title"] = bridge_title_for_lane(lane, frontmatter)
            changed = True

        if changed:
            write_file(task_path, dump_frontmatter(frontmatter, body))
            updated.append(
                {
                    "task_id": task_id,
                    "task_path": str(task_path),
                    "bridge_thread_uuid": bridge_uuid,
                    "bridge_visible_prefix": frontmatter["bridge_visible_prefix"],
                    "claude_desktop_thread_title": frontmatter["claude_desktop_thread_title"],
                }
            )
        else:
            skipped.append({"task_id": task_id, "reason": "metadata already present"})

    return {"project_id": project_id, "updated": updated, "skipped": skipped}


def normalize_lanes(payload: dict) -> None:
    lanes = sorted(payload.get("lanes", []), key=lambda lane: lane["order"])
    for index, lane in enumerate(lanes, start=1):
        lane["order"] = index

    active_like = [lane for lane in lanes if lane.get("queue_state") in {"active", "review"}]
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
        canonical_ready = next((lane for lane in lanes if lane.get("queue_state") == "queued"), None)
        if canonical_ready is not None:
            canonical_ready["queue_state"] = "ready"

    payload["lanes"] = lanes


def issue_lane_from_design_lane(lane: dict) -> dict:
    return {
        "order": lane["order"],
        "issue": lane["issue"],
        "task_id": lane["task_id"],
        "title": lane.get("title", ""),
        "owner": lane["owner"],
        "task_status": lane["task_status"],
        "queue_state": lane["queue_state"],
        "lane_type": lane.get("lane_type") or "design",
        "tier": lane.get("tier", 5),
        "depends_on": normalize_depends(lane.get("depends_on")),
        "blocked_by": normalize_depends(lane.get("blocked_by")),
        "notes": lane.get("notes", ""),
    }


def sync_issue_queue_mirror(project_id: str, design_payload: dict) -> Path | None:
    active_lanes = active_design_lanes(design_payload)
    existing_issue_payload = load_issue_queue(project_id)
    existing_completed_by_task = {
        str(item.get("task_id")): item
        for item in (existing_issue_payload or {}).get("completed_recently", [])
        if isinstance(item, dict)
    }
    issue_payload = existing_issue_payload or {
        "schema_version": 1,
        "artifact_type": "ordered_issue_queue",
        "project_id": project_id,
        "source_issue": design_payload.get("source_issue"),
        "source_task": design_payload.get("source_task"),
        "completed_recently": [],
        "lanes": [],
    }
    issue_payload["artifact_type"] = "ordered_issue_queue"
    issue_payload["project_id"] = project_id
    issue_payload["last_updated_utc"] = utc_iso()
    issue_payload["notes"] = [
        "Legacy design-queue migration: active design lanes copied into the canonical issue queue.",
        "Do not maintain design-queue.json as a second source of truth after migration.",
        "All lanes in this queue are design, shaping, /design sandbox, surface-spec, handoff, parity, stale-issue audit, or template-design work assigned to claude.",
        "Do not add backend or /app implementation lanes here until the relevant design queue item is done and the implementation dependency is explicit.",
        "When the final design lane completes, validate the GitHub-backed issue queue before treating the backlog as empty.",
    ]
    issue_payload["completed_recently"] = [
        {
            "issue": item["issue"],
            "task_id": item["task_id"],
            "owner": item.get("owner")
            or existing_completed_by_task.get(str(item.get("task_id")), {}).get("owner")
            or DESIGN_OWNER,
            "status": item.get("status", "done"),
        }
        for item in design_payload.get("completed_recently", [])[-10:]
    ]
    issue_payload["lanes"] = [issue_lane_from_design_lane(lane) for lane in active_lanes]
    return issue_queue.sync_markdown(project_id, issue_payload)


def sync_markdown(project_id: str, payload: dict, *, mirror_issue_queue: bool = True) -> Path:
    updated = dict(payload)
    updated["last_updated_utc"] = utc_iso()
    save_queue(project_id, updated)
    markdown_path = queue_markdown_path(project_id)
    write_file(markdown_path, render_markdown(updated))
    if mirror_issue_queue:
        sync_issue_queue_mirror(project_id, updated)
    return markdown_path


def archive_complete_queue(project_id: str, payload: dict) -> tuple[Path, Path]:
    stamp = utc_iso().replace(":", "").replace("+00:00", "Z")
    history_dir = queue_history_dir(project_id)
    history_json = history_dir / f"design-queue-{stamp}.json"
    history_md = history_dir / f"design-queue-{stamp}.md"
    write_file(history_json, json.dumps(payload, indent=2) + "\n")
    write_file(history_md, render_markdown(payload))
    return history_json, history_md


def mark_lane_transition(project_id: str, task_id: str, *, owner: str, task_status: str) -> dict | None:
    if not queue_exists(project_id):
        return None

    payload = load_queue(project_id)
    lane = next((existing for existing in payload.get("lanes", []) if existing.get("task_id") == task_id), None)
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
        payload["lanes"] = [
            existing for existing in payload.get("lanes", []) if existing.get("task_id") != task_id
        ]
        completed = payload.setdefault("completed_recently", [])
        completed.append(
            {
                "issue": lane["issue"],
                "task_id": lane["task_id"],
                "title": lane.get("title", ""),
                "owner": owner,
                "status": "done",
            }
        )
        payload["completed_recently"] = completed[-25:]

        if not payload["lanes"]:
            archived_json, archived_md = archive_complete_queue(project_id, dict(payload))
            payload["mode"] = "design-queue-empty"
            payload["notes"] = [
                "No remaining design lanes.",
                f"Last complete design queue snapshot archived to {display_path(archived_json)} and {display_path(archived_md)}.",
                "Rebuild implementation queue from completed design handoffs before activating code lanes.",
            ]
            sync_markdown(project_id, payload)
            return {
                "updated": True,
                "archived": True,
                "history_json": display_path(archived_json),
                "history_md": display_path(archived_md),
            }

    normalize_lanes(payload)
    sync_markdown(project_id, payload)
    return {"updated": True, "archived": False}


def main() -> int:
    args = parse_args()
    payload = load_queue(args.project)

    if args.command == "show":
        print(show_queue(payload))
        return 0

    if args.command == "ready-context":
        context = ready_context(args.project, payload)
        if args.json:
            print(json.dumps(context, indent=2, sort_keys=True))
        else:
            print(render_ready_context(context))
        return 0

    if args.command == "bridge-status":
        status = bridge_status(args.project, payload)
        if args.json:
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(render_bridge_status(status))
        return 0

    if args.command == "record-computer-use-timeout":
        result = record_computer_use_timeout(args.project, payload, reason=args.reason)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            blocker = result["computer_use_blocker"]
            print(f"recorded Computer Use timeout for {result['task_id']}")
            print(f"state: {result['path']}")
            print(f"cooldown active: {blocker.get('active')}")
            print(f"cooldown until: {blocker.get('cooldown_until_utc')}")
        return 0

    if args.command == "desktop-prompt":
        print(render_desktop_prompt(ready_context(args.project, payload)))
        return 0

    if args.command == "ensure-bridge-metadata":
        result = ensure_bridge_metadata(args.project, payload, all_active=args.all_active)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            for item in result["updated"]:
                print(
                    f"updated {item['task_id']}: {item['bridge_visible_prefix']} "
                    f"({item['claude_desktop_thread_title']})"
                )
            for item in result["skipped"]:
                print(f"skipped {item['task_id']}: {item['reason']}")
        return 0

    if args.command == "validate":
        errors, warnings = validate_queue(args.project, payload, check_github=args.check_github)
        status_line = validation_status_line(payload)
        if args.json:
            print(
                json.dumps(
                    {
                        "project_id": args.project,
                        "queue_path": display_path(queue_json_path(args.project)),
                        "ok": not bool(errors),
                        "errors": errors,
                        "warnings": warnings,
                        "status": status_line,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0 if not errors else 1
        print(f"project: {args.project}")
        print(f"design queue: {queue_json_path(args.project)}")
        print("")
        if errors:
            print("errors:")
            for error in errors:
                print(f"- {error}")
        if warnings:
            print("warnings:")
            for warning in warnings:
                print(f"- {warning}")
        if errors:
            return 1
        print("design queue validation: ok")
        print(status_line)
        return 0

    if args.command == "archive-complete":
        if payload.get("lanes"):
            print("[error] Refusing archive-complete while lanes remain in the design queue.", file=sys.stderr)
            return 1
        archived_json, archived_md = archive_complete_queue(args.project, payload)
        payload["mode"] = "design-queue-empty"
        payload["notes"] = [
            "No remaining design lanes.",
            f"Design queue archived manually to {display_path(archived_json)} and {display_path(archived_md)}.",
        ]
        sync_markdown(args.project, payload)
        print(f"archived_json: {display_path(archived_json)}")
        print(f"archived_md: {display_path(archived_md)}")
        return 0

    errors, warnings = validate_queue(
        args.project,
        payload,
        check_github=args.check_github,
        check_issue_mirror=False,
    )
    if errors:
        print("[error] Design queue is invalid; fix queue JSON before syncing markdown.", file=sys.stderr)
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
