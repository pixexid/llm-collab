#!/usr/bin/env python3
"""
Persist the current autonomous queue-runner state for a project.

This file does not execute the workflow. It records the loop contract that
Codex heartbeats and orchestrator turns should consult before deciding whether
to wait, fix a blocker, merge a PR, or continue to the next lane.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _helpers import display_path, ensure_project, get_agent, project_state_dir, utc_iso, write_file


STATE_FILE_NAME = "autonomous-loop.json"
REVIEW_POLICY = "local_required_github_codex_opportunistic"
DEFAULT_STOP_CONDITIONS = [
    "operator_interrupt",
    "queue_empty",
    "true_external_blocker",
]
MODES = [
    "idle",
    "next_lane",
    "worker_wait",
    "acceptance",
    "pr_wait",
    "fix_loop",
    "post_merge",
    "blocked",
    "queue_empty",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record autonomous queue-runner state.")
    parser.add_argument("command", choices=["show", "start", "update", "clear"])
    parser.add_argument("--project", required=True, help="Project id from projects.json.")
    parser.add_argument("--state-root", help="Override project_state_root for tests.")
    parser.add_argument("--agent", help="Owning orchestrator agent id.")
    parser.add_argument("--mode", choices=MODES, help="Current loop mode.")
    parser.add_argument("--task", help="Current task id.")
    parser.add_argument("--issue", help="Current GitHub issue id, for example GH-395.")
    parser.add_argument("--pr", help="Current PR number.")
    parser.add_argument("--chat", help="Current llm-collab chat id or path.")
    parser.add_argument("--worker", help="Current external worker id.")
    parser.add_argument("--auto-merge", choices=["true", "false"], help="Whether clean PRs may merge without another operator prompt.")
    parser.add_argument("--review-policy", help="PR review policy label.")
    parser.add_argument("--stop-condition", action="append", dest="stop_conditions", help="Allowed loop stop condition. Repeatable.")
    parser.add_argument("--note", action="append", dest="notes", help="Append a short state note. Repeatable.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args()


def state_dir(project_id: str, state_root: str | None) -> Path:
    if state_root:
        return Path(state_root).expanduser().resolve() / project_id
    return project_state_dir(project_id)


def state_path(project_id: str, state_root: str | None) -> Path:
    return state_dir(project_id, state_root) / STATE_FILE_NAME


def read_state(path: Path, project_id: str) -> dict[str, Any]:
    if not path.exists():
        return default_state(project_id)
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        payload = default_state(project_id)
        payload["notes"].append(
            {
                "at": utc_iso(),
                "text": f"Recovered from unreadable {STATE_FILE_NAME}; previous content ignored.",
            }
        )
    if not isinstance(payload, dict):
        return default_state(project_id)
    return normalize_state(payload, project_id)


def default_state(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "enabled": False,
        "agent": None,
        "mode": "idle",
        "auto_merge": False,
        "review_policy": REVIEW_POLICY,
        "stop_conditions": DEFAULT_STOP_CONDITIONS.copy(),
        "heartbeat": {
            "owner": None,
            "kind": "persistent_queue_runner",
            "singleton": True,
            "cadence_minutes": 6,
            "child_heartbeats": [],
        },
        "current": {
            "task": None,
            "issue": None,
            "pr": None,
            "chat": None,
            "worker": None,
        },
        "last_checked_at": None,
        "next_action": "Recover inbox, queue, bridge status, active PRs, then choose the next executable workflow step.",
        "notes": [],
    }


def normalize_state(payload: dict[str, Any], project_id: str) -> dict[str, Any]:
    state = default_state(project_id)
    for key, value in payload.items():
        if key in {"heartbeat", "current"} and isinstance(value, dict):
            state[key].update(value)
        elif key in {"heartbeat", "current"}:
            continue
        elif key in state:
            state[key] = value
    state["project_id"] = project_id
    state["schema_version"] = 1
    if state.get("mode") not in MODES:
        state["mode"] = "idle"
    if not isinstance(state.get("stop_conditions"), list):
        state["stop_conditions"] = DEFAULT_STOP_CONDITIONS.copy()
    if not isinstance(state.get("notes"), list):
        state["notes"] = []
    return state


def bool_arg(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value == "true"


def apply_updates(state: dict[str, Any], args: argparse.Namespace, *, start: bool) -> dict[str, Any]:
    if args.agent:
        get_agent(args.agent)
        state["agent"] = args.agent
        state["heartbeat"]["owner"] = args.agent
    if args.mode:
        state["mode"] = args.mode
    if args.review_policy:
        state["review_policy"] = args.review_policy
    if args.auto_merge is not None:
        state["auto_merge"] = bool_arg(args.auto_merge, bool(state.get("auto_merge", False)))
    if args.stop_conditions:
        state["stop_conditions"] = args.stop_conditions

    for key in ("task", "issue", "pr", "chat", "worker"):
        value = getattr(args, key)
        if value is not None:
            state["current"][key] = value

    if start:
        if not state.get("agent"):
            raise SystemExit("[error] start requires --agent or an existing state owner")
        state["enabled"] = True
        if state["mode"] == "idle":
            state["mode"] = "next_lane"

    state["last_checked_at"] = utc_iso()
    state["next_action"] = next_action(state)
    for note in args.notes or []:
        state["notes"].append({"at": utc_iso(), "text": note})
    return state


def next_action(state: dict[str, Any]) -> str:
    mode = state.get("mode")
    current = state.get("current", {})
    if mode == "next_lane":
        return "Check llm-collab inbox, validate queues, inspect active PRs, then activate the next ready lane."
    if mode == "worker_wait":
        worker = current.get("worker") or "worker"
        return f"Check inbox, bridge status, and visible {worker} state; do not interrupt a running worker."
    if mode == "acceptance":
        task = current.get("task") or "current task"
        return f"Run dirty-worktree acceptance and task-contract review for {task}."
    if mode == "pr_wait":
        pr = current.get("pr") or "current PR"
        return f"Re-check checks, merge state, comments/reviews, and merge {pr} when policy is satisfied."
    if mode == "fix_loop":
        return "Inspect the blocker, patch or re-delegate the smallest fix, rerun gates, and return to the loop."
    if mode == "post_merge":
        return "Fast-forward main, run post-merge checks, mark task done, run bin/post_merge_cleanup.py, then continue only when cleanup is clear or deferred items are recorded."
    if mode == "blocked":
        return "Stop only if the blocker requires external operator input; otherwise convert it into a fix loop."
    if mode == "queue_empty":
        return "Confirm queues are empty, archive the final snapshot, clear this loop state, and report the summary."
    return "Recover inbox, queue, bridge status, active PRs, then choose the next executable workflow step."


def emit(payload: dict[str, Any], *, as_json: bool, path: Path | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload.get("cleared"):
        print(f"cleared: {payload.get('ok', False)}")
        print(f"path: {payload.get('path', '')}")
        return
    if path:
        print(f"state: {display_path(path)}")
    print(f"project: {payload.get('project_id')}")
    print(f"enabled: {payload.get('enabled')}")
    print(f"mode: {payload.get('mode')}")
    print(f"agent: {payload.get('agent')}")
    print(f"auto_merge: {payload.get('auto_merge')}")
    print(f"review_policy: {payload.get('review_policy')}")
    print(f"next_action: {payload.get('next_action')}")


def main() -> None:
    args = parse_args()
    ensure_project(args.project, allow_none=False)
    path = state_path(args.project, args.state_root)

    if args.command == "clear":
        if path.exists():
            path.unlink()
        payload = {"ok": True, "cleared": True, "path": str(path)}
        emit(payload, as_json=args.json)
        return

    state = read_state(path, args.project)

    if args.command in {"start", "update"}:
        state = apply_updates(state, args, start=args.command == "start")
        write_file(path, json.dumps(state, indent=2, sort_keys=True) + "\n")

    emit(state, as_json=args.json, path=path)


if __name__ == "__main__":
    main()
