#!/usr/bin/env python3
"""Spawn one BB assignment through the frozen preflight gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPT_DIR), str(SCRIPT_DIR.parent)]
from _python_runtime import require_python

require_python()

from _helpers import (  # noqa: E402
    get_project,
    project_state_dir,
    resolve_project_repo_path,
    write_file_durably,
)
from llm_collab.bb_client import BbClient, subprocess_transport  # noqa: E402
from llm_collab.spawn_gate import (  # noqa: E402
    Attached,
    GateRefusal,
    NewWorktree,
    _classify_spawn_failure,
    persist_assignment,
    plan_spawn,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--assignment-kind", choices=("read-only", "writing"), required=True)
    result.add_argument("--collab-project", required=True)
    result.add_argument("--repo-target")
    result.add_argument("--project", required=True, help="BB project ID")
    result.add_argument("--provider")
    result.add_argument("--model")
    result.add_argument("--reasoning-level")
    result.add_argument("--base-sha", required=True)
    result.add_argument("--permission-mode")
    result.add_argument("--title")
    result.add_argument("--prompt", required=True)
    result.add_argument("--json", action="store_true")
    isolation = result.add_mutually_exclusive_group()
    isolation.add_argument("--new-environment", choices=("worktree",))
    isolation.add_argument("--environment")
    return result


def _emit(message: str) -> None:
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except (BrokenPipeError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    registry_entry = get_project(args.collab_project)
    repos = registry_entry.get("repos") if isinstance(registry_entry, dict) else None
    keys = sorted(key for key in repos if isinstance(key, str) and key) if isinstance(repos, dict) else []
    if isinstance(registry_entry, dict):
        registry_entry = {
            **registry_entry,
            "repos": {
                key: resolve_project_repo_path(args.collab_project, key) for key in keys
            },
        }
    environment = (
        NewWorktree()
        if args.new_environment
        else Attached(args.environment)
        if args.environment is not None
        else None
    )
    plan = plan_spawn(
        assignment_kind=args.assignment_kind,
        project_id=args.project,
        registry_entry=registry_entry,
        repo_target=args.repo_target,
        base_sha=args.base_sha,
        environment=environment,
        provider=args.provider,
        model=args.model,
        reasoning_level=args.reasoning_level,
        permission_mode=args.permission_mode,
        title=args.title,
        prompt=args.prompt,
    )
    if isinstance(plan, GateRefusal):
        _emit(f"REFUSED: {plan.reason}: {plan.detail}")
        return 1

    client = BbClient(subprocess_transport(["bb"]), enabled=True, timeout_seconds=60.0)
    state_dir = project_state_dir(args.collab_project) / "bb-assignments"
    outcome: object = None
    try:
        outcome = client.spawn(
            project_id=plan.project_id,
            prompt=plan.prompt,
            profile=plan.profile,
            environment=(
                plan.environment.environment_id
                if isinstance(plan.environment, Attached)
                else None
            ),
            base_sha=plan.base_sha if isinstance(plan.environment, NewWorktree) else None,
            permission_mode=plan.permission_mode,
            title=plan.title,
        )
        record_path = persist_assignment(
            plan, outcome, state_dir, write_durably=write_file_durably
        )
        print(
            json.dumps(
                {
                    "id": outcome.thread_id,
                    "environmentId": outcome.environment_id,
                    "projectId": outcome.project_id,
                    "providerId": outcome.provider_id,
                    "status": outcome.status,
                    "assignmentRecord": str(record_path),
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
    except BaseException as error:
        refusal, retryable = _classify_spawn_failure(error, outcome)
        if retryable:
            _emit(f"REFUSED: {refusal.reason}: {refusal.detail}")
            return 1
        identity = (
            f" native_thread_id={refusal.native_thread_id}"
            if refusal.native_thread_id is not None
            else ""
        )
        _emit(f"DO NOT RETRY: {refusal.reason}{identity}: {refusal.detail}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
