#!/usr/bin/env python3
"""
claim_task.py — Assign ownership and update task status.

Moves the task file to the appropriate folder (active/backlog/done)
and appends an activity log entry.

Usage:
  python bin/claim_task.py --task TASK-ABC123 --owner orchestrator --status in_progress
  python bin/claim_task.py --task TASK-ABC123 --owner claude --status in_progress --accepted-by codex
  python bin/claim_task.py --task TASK-ABC123 --owner unassigned --status open --note "Blocked on API spec"
  python bin/claim_task.py --task TASK-ABC123 --owner orchestrator --status done \
    --released-by codex \
    --release-evidence '{"merge_sha":"<40-hex>","verdict":"success","run_id":123}'
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json

sys.path.insert(0, str(Path(__file__).parent))
import project_issue_queue as issue_queue
from _helpers import (
    ROOT,
    TASK_STATUSES,
    agent_ids,
    dump_frontmatter,
    ensure_agent_enabled,
    find_task_by_id,
    get_project,
    parse_release_evidence,
    parse_frontmatter,
    run_project_preflight,
    target_task_path,
    utc_iso,
    write_file,
)
from deploy_release_watch import (
    ReleaseEvaluation,
    evaluate_project_release,
    project_mapping_error,
    resolve_release_config,
)
from refine_task import RISK_REQUIRED_LABELS, RISK_SECTION, validate_implementation_risk_analysis
from task_contract import sync_task_contract, validate_task_contract

PLANNING_AGENT = "claude"
ACCEPTANCE_AGENT = "codex"


class ReleaseGateError(ValueError):
    """A requested done transition lacks valid objective closure authority."""


def validate_truthy_release_closure(
    project_id: str,
    release_closure: object,
) -> None:
    """Refuse a configured-but-malformed closure before verdict-specific work."""
    if not release_closure:
        return

    # Reuse the deploy evaluator's canonical closure validation without making
    # an honest non-success disposition depend on GitHub being enabled or on a
    # production branch being configured.
    validation_project = {
        "github": {"enabled": True, "repo": "validation/release-closure"},
        "default_branch_base": "validation-branch",
        "release_closure": release_closure,
    }
    resolved_config, config_error = resolve_release_config(
        project_id,
        validation_project,
    )
    if resolved_config is None:
        raise ReleaseGateError(
            f"project {project_id!r} has malformed projects.json key "
            f"'release_closure': truthy release_closure must be a complete valid "
            f"object; refusing every release verdict before evaluation: {config_error} "
            f"— repair this task project's {project_id!r} entry in projects.json "
            "at key 'release_closure'"
        )


def build_release_evidence_record(
    frontmatter: dict,
    old_status: str,
    released_by: str | None,
    raw_evidence: str | None,
    *,
    evaluator=None,
    evaluated_at: str | None = None,
) -> dict:
    """Validate a review -> done request without mutating task or queue state."""
    task_id = frontmatter.get("task_id")
    if old_status != "review":
        raise ReleaseGateError(
            f"only review -> done is allowed; task {task_id!r} is currently {old_status!r}"
        )

    project_id = frontmatter.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise ReleaseGateError(
            f"task {task_id!r} has no exact project_id; refusing done transition"
        )
    project = get_project(project_id)
    if project is None:
        raise ReleaseGateError(
            f"task project_id {project_id!r} is not registered in projects.json"
        )

    if "release_gate_agent" not in project:
        raise ReleaseGateError(
            f"project {project_id!r} is missing required projects.json key "
            "release_gate_agent; set it to an enabled agent id in this task "
            "project's projects.json entry"
        )
    required_releaser = project.get("release_gate_agent")
    if not isinstance(required_releaser, str) or not required_releaser.strip():
        raise ReleaseGateError(
            f"project {project_id!r} release_gate_agent must be a non-empty agent id"
        )
    if released_by != required_releaser:
        raise ReleaseGateError(
            f"--released-by must equal project {project_id!r} release_gate_agent "
            f"{required_releaser!r}"
        )

    try:
        evidence = parse_release_evidence(raw_evidence)
    except ValueError as error:
        raise ReleaseGateError(str(error)) from error
    verdict_name = evidence["verdict"]

    github = project.get("github")
    if github is not None and not isinstance(github, dict):
        raise ReleaseGateError(project_mapping_error(project_id, "github"))
    repository_candidate = github.get("repo") if isinstance(github, dict) else None
    repository = (
        repository_candidate
        if (
            isinstance(github, dict)
            and github.get("enabled") is True
            and isinstance(repository_candidate, str)
            and repository_candidate.strip()
        )
        else None
    )
    release_closure = project.get("release_closure")
    validate_truthy_release_closure(project_id, release_closure)
    configured_workflow = (
        release_closure.get("workflow")
        if isinstance(release_closure, dict)
        else None
    )
    authoritative_run_id = None

    if verdict_name == "success":
        if repository is None:
            raise ReleaseGateError(
                f"project {project_id!r} has no enabled github.repo; "
                "success requires an exact repository for objective release evaluation"
            )
        resolved_config, config_error = resolve_release_config(project_id, project)
        if resolved_config is None:
            raise ReleaseGateError(
                f"objective release evaluation failed closed: {config_error}"
            )
        release_evaluator = evaluator or evaluate_project_release
        try:
            evaluation: ReleaseEvaluation = release_evaluator(
                project_id,
                evidence["merge_sha"],
                project=project,
            )
        except (RuntimeError, ValueError) as error:
            raise ReleaseGateError(
                f"objective release evaluation failed closed: {error}"
            ) from error

        verdict = evaluation.verdict
        if evaluation.project_id != project_id:
            raise ReleaseGateError(
                "objective release evaluator returned a different project_id"
            )
        if evaluation.repository != repository:
            raise ReleaseGateError(
                "objective release evaluator returned a repository that does not "
                "match the project registry"
            )
        if evaluation.workflow != configured_workflow:
            raise ReleaseGateError(
                "objective release evaluator returned a workflow that does not "
                "match the project registry"
            )
        if (
            not isinstance(verdict.merge_sha, str)
            or verdict.merge_sha.lower() != evidence["merge_sha"]
        ):
            raise ReleaseGateError(
                "objective release evaluator returned evidence for a different merge SHA"
            )
        if verdict.state != "SUCCESS":
            raise ReleaseGateError(
                f"objective release verdict is {verdict.state}, not terminal SUCCESS: "
                f"{verdict.detail}"
            )
        if (
            isinstance(verdict.run_id, bool)
            or not isinstance(verdict.run_id, int)
            or verdict.run_id <= 0
        ):
            raise ReleaseGateError(
                "objective release evaluator returned no authoritative positive integer run_id"
            )
        if evidence["run_id"] != verdict.run_id:
            raise ReleaseGateError(
                f"release evidence run_id {evidence['run_id']!r} does not match "
                f"authoritative run_id {verdict.run_id}"
            )
        authoritative_run_id = verdict.run_id

    production_impact = {
        "success": "production-release-verified",
        "risk-accepted-followup": "risk-accepted-followup",
        "non-production": "non-production",
    }[verdict_name]
    record = {
        "project_id": project_id,
        "task_id": task_id,
        "repository": repository,
        "merge_sha": evidence["merge_sha"],
        "production_impact": production_impact,
        "terminal_verdict": verdict_name,
        "released_by": released_by,
        "evaluated_at": evaluated_at or utc_iso(),
    }
    if isinstance(configured_workflow, str) and configured_workflow.strip():
        record["workflow"] = configured_workflow
    if authoritative_run_id is not None:
        record["run_id"] = authoritative_run_id
    if "note" in evidence:
        record["note"] = evidence["note"]
    return record


def parse_args():
    p = argparse.ArgumentParser(description="Claim or update a task.")
    p.add_argument("--task", required=True, help="TASK-id")
    p.add_argument("--owner", required=True, help="Agent ID or 'unassigned'")
    p.add_argument("--status", required=True, choices=TASK_STATUSES)
    p.add_argument("--note", default=None, help="Activity log entry (optional)")
    p.add_argument("--branch", default=None, help="Git branch associated with this task")
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip project preflight gate for this status transition.",
    )
    p.add_argument(
        "--allow-queue-override",
        action="store_true",
        help="Allow claiming a queued project lane out of order even if it is not the current ready lane.",
    )
    p.add_argument(
        "--accepted-by",
        default=None,
        help="Agent ID accepting a Claude-authored/Claude-planned task for activation.",
    )
    p.add_argument(
        "--accepted-note",
        default=None,
        help="Optional note recorded when accepting a self-authored planning pass.",
    )
    p.add_argument(
        "--allow-self-plan",
        action="store_true",
        help="Override the Codex acceptance requirement for a Claude-authored/Claude-planned task. Logged in frontmatter.",
    )
    p.add_argument(
        "--released-by",
        default=None,
        help="Configured project release-gate agent authorizing a done transition.",
    )
    p.add_argument(
        "--release-evidence",
        default=None,
        help="Strict JSON release evidence required for a done transition.",
    )
    return p.parse_args()


def requires_codex_acceptance(frontmatter: dict) -> bool:
    if frontmatter.get("skip_refinement", False):
        return False
    return frontmatter.get("created_by") == PLANNING_AGENT and frontmatter.get("refined_by") == PLANNING_AGENT


def has_codex_acceptance(frontmatter: dict) -> bool:
    return frontmatter.get("accepted_by") == ACCEPTANCE_AGENT


def main():
    args = parse_args()

    known = agent_ids()
    if args.owner != "unassigned" and args.owner not in known:
        print(f"[error] Owner {args.owner!r} not in agents.json", file=sys.stderr)
        sys.exit(1)
    if args.accepted_by is not None and args.accepted_by not in known:
        print(f"[error] --accepted-by agent {args.accepted_by!r} not in agents.json", file=sys.stderr)
        sys.exit(1)
    if args.owner != "unassigned":
        ensure_agent_enabled(args.owner, context="task ownership")
    if args.accepted_by is not None:
        ensure_agent_enabled(args.accepted_by, context="task planning acceptance")
    if args.status == "done":
        if args.released_by is None:
            print("[error] --released-by is required for a done transition", file=sys.stderr)
            sys.exit(1)
        if args.released_by not in known:
            print(
                f"[error] --released-by agent {args.released_by!r} not in agents.json",
                file=sys.stderr,
            )
            sys.exit(1)
        ensure_agent_enabled(args.released_by, context="task release closure")

    task_file = find_task_by_id(args.task)
    if task_file is None:
        print(f"[error] Task not found: {args.task}", file=sys.stderr)
        sys.exit(1)

    content = task_file.read_text()
    fm, body = parse_frontmatter(content)
    fm, _ = sync_task_contract(fm, body)

    if args.accepted_by is not None:
        if args.accepted_by != ACCEPTANCE_AGENT:
            print(
                json.dumps(
                    {
                        "error": "self-authored Claude planning acceptance must be recorded by codex",
                        "task_id": fm.get("task_id", args.task),
                        "accepted_by": args.accepted_by,
                        "required_accepted_by": ACCEPTANCE_AGENT,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)
        fm["accepted_by"] = args.accepted_by
        fm["accepted_at"] = utc_iso()
        if args.accepted_note:
            fm["accepted_note"] = args.accepted_note

    old_status = fm.get("status", "open")
    project_id = fm.get("project_id")
    preflight_summary = None
    queue_summary = None
    release_evidence_record = None

    if args.status == "done":
        try:
            release_evidence_record = build_release_evidence_record(
                fm,
                old_status,
                args.released_by,
                args.release_evidence,
            )
        except ReleaseGateError as error:
            print(
                json.dumps(
                    {
                        "error": str(error),
                        "task_id": fm.get("task_id", args.task),
                        "old_status": old_status,
                        "target_status": args.status,
                        "project_id": project_id,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

    if project_id and args.status == "in_progress" and issue_queue.queue_exists(project_id):
        payload = issue_queue.load_queue(project_id)
        lane = issue_queue.find_lane(payload, fm.get("task_id", args.task))
        if lane is not None:
            lane_state = lane.get("queue_state")
            if lane_state not in {"ready", "active", "review"} and not args.allow_queue_override:
                current_ready = issue_queue.next_ready_lane(payload)
                print(
                    json.dumps(
                        {
                            "error": "task is not the current ready queue lane; refusing out-of-order claim",
                            "task_id": fm.get("task_id", args.task),
                            "target_status": args.status,
                            "project_id": project_id,
                            "lane": {
                                "issue": lane.get("issue"),
                                "queue_state": lane_state,
                                "order": lane.get("order"),
                            },
                            "ready_lane": current_ready,
                            "override_flag": "--allow-queue-override",
                        },
                        indent=2,
                    ),
                    file=sys.stderr,
                )
                sys.exit(1)

    if args.status in ("in_progress", "review") and not args.skip_preflight:
        preflight = run_project_preflight(project_id, extra_args=["--browser-check", "skip"])
        if preflight.get("ran"):
            preflight_summary = {
                "ran": True,
                "ok": bool(preflight.get("ok")),
                "cwd": preflight.get("cwd"),
                "command": preflight.get("command"),
                "returncode": preflight.get("returncode"),
            }
        if preflight.get("ran") and not preflight.get("ok"):
            print(
                json.dumps(
                    {
                        "error": "project preflight failed; refusing claim_task transition",
                        "task_id": fm.get("task_id", args.task),
                        "target_status": args.status,
                        "project_id": project_id,
                        "preflight": preflight,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

    if args.status == "in_progress":
        if not fm.get("skip_refinement", False) and fm.get("refined_by") != "claude":
            print(
                json.dumps(
                    {
                        "error": "task has not been refined by claude; refusing in_progress transition",
                        "task_id": fm.get("task_id", args.task),
                        "target_status": args.status,
                        "hint": "Send the task to claude for spec review, then run: python bin/refine_task.py --task TASK-...",
                        "bypass": "For trivial or hotfix tasks, set skip_refinement: true in the task frontmatter at creation time (use --skip-refinement flag in new_task.py).",
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

        if requires_codex_acceptance(fm) and not has_codex_acceptance(fm):
            if not args.allow_self_plan:
                print(
                    json.dumps(
                        {
                            "error": "Claude-authored and Claude-planned task requires Codex acceptance before activation",
                            "task_id": fm.get("task_id", args.task),
                            "target_status": args.status,
                            "created_by": fm.get("created_by"),
                            "refined_by": fm.get("refined_by"),
                            "hint": "Review the task/issue against source evidence, then rerun with --accepted-by codex.",
                            "override_flag": "--allow-self-plan",
                        },
                        indent=2,
                    ),
                    file=sys.stderr,
                )
                sys.exit(1)
            now = utc_iso()
            fm["self_plan_acceptance_override"] = True
            fm["self_plan_acceptance_override_at"] = now

        risk_errors = [] if fm.get("skip_refinement", False) else validate_implementation_risk_analysis(body)
        if risk_errors:
            print(
                json.dumps(
                    {
                        "error": "implementation risk analysis is incomplete; refusing in_progress transition",
                        "task_id": fm.get("task_id", args.task),
                        "target_status": args.status,
                        "required_section": RISK_SECTION,
                        "required_labels": RISK_REQUIRED_LABELS,
                        "problems": risk_errors,
                        "hint": "Patch the task body with real pre-implementation feasibility analysis before activation.",
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

        errors, summary = validate_task_contract(fm, body, stage="assignment")
        if errors:
            print(
                json.dumps(
                    {
                        "error": "task contract is incomplete; refusing in_progress transition",
                        "task_id": fm.get("task_id", args.task),
                        "target_status": args.status,
                        "contract": summary,
                        "problems": errors,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

    if args.status == "review":
        errors, summary = validate_task_contract(fm, body, stage="review")
        if errors:
            print(
                json.dumps(
                    {
                        "error": "task review evidence is incomplete; refusing review transition",
                        "task_id": fm.get("task_id", args.task),
                        "target_status": args.status,
                        "contract": summary,
                        "problems": errors,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            sys.exit(1)

    fm["status"] = args.status
    fm["owner"] = args.owner
    if args.branch:
        fm["branch"] = args.branch
    if release_evidence_record is not None:
        fm["release_evidence"] = release_evidence_record

    note = args.note or f"Status → {args.status}, owner → {args.owner}"
    activity_line = f"- {utc_iso()} | {args.owner} | {note}"

    if "## Activity Log" in body:
        body = body.replace("## Activity Log", f"## Activity Log\n\n{activity_line}", 1)
    else:
        body = body.rstrip() + f"\n\n## Activity Log\n\n{activity_line}\n"

    new_content = dump_frontmatter(fm, body)

    # Determine new path (may move between folders)
    title = fm.get("title", args.task)
    tid = fm.get("task_id", args.task)
    new_path = target_task_path(title, tid, args.status)

    if task_file != new_path:
        task_file.unlink()

    write_file(new_path, new_content)

    if project_id and issue_queue.queue_exists(project_id):
        queue_summary = issue_queue.mark_lane_transition(
            project_id,
            tid,
            owner=args.owner,
            task_status=args.status,
        )
    result = {
        "task_id": tid,
        "old_status": old_status,
        "new_status": args.status,
        "owner": args.owner,
        "path": str(new_path.relative_to(ROOT)),
        "moved": task_file != new_path,
    }
    if preflight_summary is not None:
        result["preflight"] = preflight_summary
    if queue_summary is not None:
        result["queue"] = queue_summary
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
