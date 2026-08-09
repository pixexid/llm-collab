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
from llm_collab.bb_client import BbClient  # noqa: E402
from llm_collab.bb_continuation import (  # noqa: E402
    BbContinuationRefused,
    client_from_project,
)
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


def _configured_client(registry_entry: dict) -> BbClient | GateRefusal:
    try:
        return client_from_project(registry_entry)
    except BbContinuationRefused as error:
        return GateRefusal("bb_config_invalid", str(error))


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
    if args.new_environment:
        # Refuse BEFORE the non-idempotent spawn, not after it. bb provisions a new
        # worktree asynchronously and returns `environmentId: null`, which the client
        # rejects as malformed — but only once a real thread exists, so every attempt
        # would leave an orphan with no assignment record. A clean pre-execution
        # refusal performs nothing and is honest about a path that cannot succeed.
        # Create the environment first and pass --environment. Removing this guard
        # requires GH-718's resolution semantics, not making the field optional.
        _emit(
            "REFUSED: new_worktree_unsupported: bb returns environmentId=null for an "
            "asynchronously provisioned worktree; create the environment first and pass "
            "--environment <id>. See GH-718."
        )
        return 1
    environment = Attached(args.environment) if args.environment is not None else None
    plan = plan_spawn(
        assignment_kind=args.assignment_kind,
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

    client = _configured_client(registry_entry)
    if isinstance(client, GateRefusal):
        _emit(f"REFUSED: {client.reason}: {client.detail}")
        return 1
    state_dir = project_state_dir(plan.project_id) / "bb-assignments"
    outcome: object = None
    try:
        outcome = client.spawn(
            project_id=plan.native_project_id,
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
