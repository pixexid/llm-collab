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

from _activation_lease import runtime_id_from_env  # noqa: E402
from _helpers import (  # noqa: E402
    get_project,
    project_state_dir,
    resolve_project_repo_path,
    write_file_durably,
)
from _watcher_liveness import (  # noqa: E402
    WATCHER_NAMES,
    check_markers,
    evaluate_coverage,
    uncovered,
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
    result.add_argument(
        "--allow-stale-watchers",
        action="store_true",
        help=(
            "Admit a writing spawn despite unacceptable orchestrator watcher "
            "coverage. The override is recorded in the assignment record."
        ),
    )
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

    # Delegation-time watcher gate (GH-722): a WRITING spawn admitted while the
    # orchestrator watchers are down runs without lifecycle/artifact/heartbeat
    # coverage. Warn-with-recorded-override, not hard refusal: a gate with no
    # override would deadlock the session that is still standing its watchers
    # up, since standing them up requires writing spawns. Read-only spawns are
    # exempt, mirroring the lane-cap exemption. Pre-execution: refusing here
    # performs nothing, and the override path is recorded in the assignment.
    watcher_gate: dict | None = None
    if args.assignment_kind == "writing":
        try:
            # The shared verdict: freshness, process liveness, AND ownership, evaluated once in
            # _watcher_liveness. This gate acts on it; it does not re-derive a
            # subset. The session identity comes from the runtime environment
            # (the same helper bootstrap uses); where it cannot be established,
            # fresh markers are owner-unknown, and unknown is never a pass.
            verdicts = evaluate_coverage(
                check_markers(args.collab_project), runtime_id_from_env()
            )
        except Exception as error:
            # A gate probe that breaks must not render as a pass.
            verdicts = [
                {
                    "name": name,
                    "acceptable": False,
                    "reason": "unreadable",
                    "detail": f"gate probe broke: {type(error).__name__}: {error}",
                }
                for name in WATCHER_NAMES
            ]
        overdue = uncovered(verdicts)
        lines = "\n".join(
            f"  {verdict['name']}: {verdict['reason']}" for verdict in overdue
        )
        if overdue and not args.allow_stale_watchers:
            _emit(
                "⚠️  REFUSED: watcher_markers_not_fresh — orchestrator watcher "
                "coverage is stale, absent, foreign-owned, owner-gone, or "
                "unverifiable:\n"
                f"{lines}\n"
                "  A writing spawn admitted now runs without live watcher "
                "coverage. Pass --allow-stale-watchers to override; the "
                "override is recorded in the assignment record."
            )
            return 1
        if overdue:
            _emit(
                "⚠️  WATCHER GATE OVERRIDE — proceeding with unacceptable "
                "watcher coverage (--allow-stale-watchers); this override is "
                f"recorded in the assignment record:\n{lines}"
            )
            watcher_gate = {
                "override": "--allow-stale-watchers",
                "not_fresh": [
                    {"name": verdict["name"], "status": verdict["reason"]}
                    for verdict in overdue
                ],
            }

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
            plan, outcome, state_dir, write_durably=write_file_durably,
            watcher_gate=watcher_gate,
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
