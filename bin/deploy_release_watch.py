#!/usr/bin/env python3
"""
deploy_release_watch.py — exact-merge-SHA post-merge deploy release gate (GH-1524).

Release closure does not end at merge. This tool correlates the production
deploy workflow run to the EXACT merge SHA and produces one of five verdicts:

  SUCCESS    deploy run for the exact SHA completed success AND the project's
             configured required jobs (including the smoke-carrying job and its
             required post-deploy smoke steps) all concluded success.
  FAILURE    the exact-SHA run (or any of its jobs) concluded failure.
  CANCELLED  the exact-SHA run concluded cancelled.
  MISSING    no deploy run exists for the exact merge SHA — a DISTINCT
             actionable state, never silence and never a pass.
  PENDING    a run exists but has not reached a terminal state (only surfaces
             on --wait timeout or without --wait).

Safety invariants (from the df55a282/29537490993 incident):
- A deploy run for a DIFFERENT SHA never satisfies this merge's closure,
  no matter how green it is (7e677225's success must not cover df55a282).
- Missing is reported, not silently treated as pass.
- On any non-success the caller (the watching agent) sends ONE durable
  llm-collab packet plus ONE doorbell ring, preserves the run id/logs, and
  does NOT blind-retry or redeploy; the task is not done until Codex records
  a terminal disposition. Ownership: Claude = ongoing watcher, Codex =
  terminal closer (see docs/workflows/commit-push-prs.md).

Usage:
  bin/deploy_release_watch.py --project <id> --merge-sha <full-sha>
      [--wait] [--timeout-seconds 900] [--poll-seconds 30] [--json]

The repo, base branch, workflow, and required job/smoke-step evidence all come
from the registered project's `release_closure` config in projects.json —
job/step names are project-specific and never live in shared bin/ (project
boundary, AGENTS.md). A project without that config fails closed (exit 64).

Exit codes: 0 SUCCESS | 10 FAILURE | 11 CANCELLED | 12 MISSING | 13 PENDING
            | 64 usage/environment error.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

from _helpers import get_project

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, field


TERMINAL_EXIT = {"SUCCESS": 0, "FAILURE": 10, "CANCELLED": 11, "MISSING": 12, "PENDING": 13}


def project_mapping_error(project_id: str, key: str) -> str:
    return (
        f"project {project_id!r} has malformed projects.json key {key!r}: expected an object — "
        f"repair this task project's {project_id!r} entry in projects.json"
    )


@dataclass
class Verdict:
    state: str                     # SUCCESS | FAILURE | CANCELLED | MISSING | PENDING
    merge_sha: str
    run_id: int | None = None
    run_conclusion: str | None = None
    failed_jobs: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass(frozen=True)
class ReleaseEvaluation:
    """Project-bound transition-time result returned to release consumers."""

    project_id: str
    repository: str
    workflow: str
    verdict: Verdict


def run_command(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
    except OSError as error:
        raise RuntimeError(
            f"cannot execute {argv[0]!r} ({error}) — install GitHub CLI (gh) and ensure it is on PATH"
        ) from error
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr.strip()}"
        )
    return result.stdout


def parse_concatenated_json(raw: str) -> list:
    """gh api --paginate (without --jq) emits one JSON document per page,
    concatenated. Decode them all; --slurp is unavailable together with --jq on
    current gh, so page merging happens here."""
    docs = []
    decoder = json.JSONDecoder()
    idx, n = 0, len(raw)
    while idx < n:
        while idx < n and raw[idx].isspace():
            idx += 1
        if idx >= n:
            break
        doc, end = decoder.raw_decode(raw, idx)
        docs.append(doc)
        idx = end
    return docs


def fetch_deploy_runs(repo: str, merge_sha: str, workflow: str, runner=run_command) -> list[dict]:
    """All runs of the deploy workflow for the exact head SHA (API-side filter),
    newest first. The head_sha query is the exact-correlation guarantee: a run
    for any other SHA is never returned, so it can never be misread as covering
    this merge. Event/branch identity is judged in evaluate_release."""
    raw = runner([
        "gh", "api", "--paginate",
        f"repos/{repo}/actions/runs?head_sha={merge_sha}&per_page=100",
    ])
    runs = [r for page in parse_concatenated_json(raw)
            for r in page.get("workflow_runs", [])]
    return [r for r in runs if r.get("name") == workflow or r.get("path", "").endswith(f"/{workflow}.yml")]


def fetch_run_jobs(repo: str, run_id: int, runner=run_command) -> list[dict]:
    # --paginate: a matrix/deploy workflow can exceed one page of jobs; a later
    # failed job must not fall outside the evidence (GH-1524 PR review P2).
    # Page docs are merged in Python (--slurp is incompatible with --jq).
    raw = runner([
        "gh", "api", "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
    ])
    return [{"name": j.get("name"), "conclusion": j.get("conclusion"),
             "steps": [{"name": st.get("name"), "conclusion": st.get("conclusion")}
                       for st in (j.get("steps") or [])]}
            for page in parse_concatenated_json(raw)
            for j in page.get("jobs", [])]


def resolve_release_config(project_id: str, project: dict | None) -> tuple[dict | None, str | None]:
    """Release-evidence identity for one registered project: (config, error).
    Required job/smoke-step names are project-specific and come ONLY from the
    project's narrow `release_closure` object plus its existing `github.repo`
    and `default_branch_base` — never from shared-bin constants (GH-1524 PR111
    P1 / project boundary). Anything unknown or unconfigured fails closed."""
    if project is None:
        return None, f"unknown project_id {project_id!r} — not registered in projects.json"
    github = project.get("github")
    if github is None:
        github = {}
    elif not isinstance(github, dict):
        return None, project_mapping_error(project_id, "github")
    repo = github.get("repo")
    if not github.get("enabled") or not repo:
        return None, f"project {project_id!r} has no enabled github.repo — cannot correlate deploy runs"
    branch = project.get("default_branch_base")
    if not (isinstance(branch, str) and branch.strip()):
        return None, (f"project {project_id!r} has no configured default_branch_base — "
                      "refusing to guess the release branch")
    rc = project.get("release_closure")
    if rc is None or rc == {}:
        return None, (
            f"project {project_id!r} has no release_closure config (workflow, required_jobs, "
            "smoke_job, required_smoke_steps) — job/step names are project-specific; "
            "register them in projects.json before using this gate"
        )
    if not isinstance(rc, dict):
        return None, project_mapping_error(project_id, "release_closure")
    workflow = rc.get("workflow")
    if not (isinstance(workflow, str) and workflow.strip()):
        return None, (f"project {project_id!r} release_closure has no configured workflow — "
                      "refusing to default")
    trigger_event = rc.get("trigger_event")
    if not (isinstance(trigger_event, str) and trigger_event.strip()):
        return None, (f"project {project_id!r} release_closure has no configured trigger_event "
                      "(the automatic event that runs the production deploy, e.g. 'push', "
                      "'workflow_run', 'merge_group') — refusing to default")

    def string_list(value) -> tuple[str, ...] | None:
        if (isinstance(value, (list, tuple)) and value
                and all(isinstance(x, str) and x.strip() for x in value)):
            return tuple(value)
        return None

    required_jobs = string_list(rc.get("required_jobs"))
    required_smoke_steps = string_list(rc.get("required_smoke_steps"))
    smoke_job = rc.get("smoke_job")
    if required_jobs is None or required_smoke_steps is None:
        return None, (f"project {project_id!r} release_closure is malformed — required_jobs and "
                      "required_smoke_steps must be non-empty lists of non-empty strings")
    if not (isinstance(smoke_job, str) and smoke_job.strip()):
        return None, f"project {project_id!r} release_closure has no configured smoke_job"
    if smoke_job not in required_jobs:
        return None, (f"project {project_id!r} release_closure smoke_job {smoke_job!r} is not one "
                      "of its required_jobs — smoke evidence would never be enforced")
    return {
        "repo": repo,
        "branch": branch,
        "workflow": workflow,
        "trigger_event": trigger_event,
        "required_jobs": required_jobs,
        "smoke_job": smoke_job,
        "required_smoke_steps": required_smoke_steps,
    }, None


def evaluate_release(
    merge_sha: str,
    runs: list[dict],
    jobs_for_run,
    *,
    required_jobs: tuple[str, ...],
    smoke_job: str,
    required_smoke_steps: tuple[str, ...],
    required_event: str,
    required_branch: str,
) -> Verdict:
    """Pure verdict logic. `runs` must already be exact-SHA-filtered (the fetch
    guarantees it; the assertion below fails closed if a caller ever passes a
    mismatched run). `jobs_for_run(run_id) -> list[dict]` supplies job detail
    (with steps) for the candidate run."""
    for r in runs:
        if r.get("head_sha") != merge_sha:
            raise ValueError(
                f"exact-SHA correlation violated: run {r.get('id')} has head_sha "
                f"{r.get('head_sha')} != merge SHA {merge_sha}"
            )
    # Only the project's CONFIGURED automatic release identity counts: the
    # trigger_event on the configured default branch. A same-SHA run under any
    # other event or branch is a manual intervention and can never supersede
    # or cover the authoritative run's outcome — that would be a blind-retry
    # laundering channel (GH-1524 cold-review P1).
    excluded = [r for r in runs
                if r.get("event") != required_event or r.get("head_branch") != required_branch]
    candidates = [r for r in runs if r not in excluded]
    if not candidates:
        note = ""
        if excluded:
            ids = ", ".join(str(r.get("id")) for r in excluded)
            note = (f" ({len(excluded)} run(s) exist for this SHA under a different "
                    f"event/branch than the configured release identity: {ids} — "
                    "they never satisfy closure)")
        return Verdict(
            state="MISSING", merge_sha=merge_sha,
            detail="no automatic "
                   f"{required_event}/{required_branch} deploy run exists for the exact "
                   f"merge SHA — actionable, not a pass{note}",
        )
    # Latest run per rerun semantics: judge the newest attempt, never a stale
    # earlier run (mirrors the latest-per-context CI rule).
    latest = max(candidates, key=lambda r: (r.get("run_attempt") or 0, r.get("id") or 0))
    run_id = latest.get("id")
    if latest.get("status") != "completed":
        return Verdict(
            state="PENDING", merge_sha=merge_sha, run_id=run_id,
            detail=f"run {run_id} status={latest.get('status')} — not terminal yet",
        )
    conclusion = latest.get("conclusion")
    if conclusion == "cancelled":
        return Verdict(state="CANCELLED", merge_sha=merge_sha, run_id=run_id,
                       run_conclusion=conclusion,
                       detail=f"run {run_id} was cancelled before completing the release")
    if conclusion != "success":
        return Verdict(state="FAILURE", merge_sha=merge_sha, run_id=run_id,
                       run_conclusion=conclusion,
                       detail=f"run {run_id} concluded {conclusion}")
    # Run-level success is necessary but not sufficient. Require POSITIVE
    # evidence: every required job present with conclusion exactly "success"
    # (a skipped deploy is a docs-only/no-op run, not a release), and every
    # required post-deploy smoke step present and successful inside the deploy
    # job. Missing/partial API data therefore fails closed instead of passing.
    jobs = jobs_for_run(run_id)
    by_name = {j.get("name", ""): j for j in jobs}
    problems: list[str] = []
    # Duplicate required names across (paginated) evidence are ambiguous — a
    # green duplicate must never whitewash a red one, so ambiguity fails closed.
    names = [j.get("name") for j in jobs]
    for name in dict.fromkeys(required_jobs + (smoke_job,)):
        if names.count(name) > 1:
            problems.append(f"ambiguous evidence: job name '{name}' appears {names.count(name)} times")
    for name in required_jobs:
        job = by_name.get(name)
        if job is None:
            problems.append(f"required job '{name}' missing from run evidence")
        elif job.get("conclusion") != "success":
            problems.append(f"required job '{name}' concluded {job.get('conclusion')!r}, not success")
    smoke_carrier = by_name.get(smoke_job) or {}
    carrier_steps = smoke_carrier.get("steps") or []
    step_names = [s.get("name") for s in carrier_steps]
    for step_name in dict.fromkeys(required_smoke_steps):
        if step_names.count(step_name) > 1:
            problems.append(f"ambiguous evidence: smoke step '{step_name}' appears "
                            f"{step_names.count(step_name)} times in '{smoke_job}'")
    steps = {s.get("name", ""): s for s in carrier_steps}
    for step_name in required_smoke_steps:
        step = steps.get(step_name)
        if step is None:
            problems.append(f"required smoke step '{step_name}' missing from '{smoke_job}' job evidence")
        elif step.get("conclusion") != "success":
            problems.append(f"required smoke step '{step_name}' concluded {step.get('conclusion')!r}")
    extra_failed = [j.get("name", "?") for j in jobs
                    if j.get("name") not in required_jobs
                    and j.get("conclusion") not in ("success", "skipped")]
    if extra_failed:
        problems.append(f"non-required job(s) failed: {', '.join(extra_failed)}")
    if problems:
        return Verdict(state="FAILURE", merge_sha=merge_sha, run_id=run_id,
                       run_conclusion=conclusion, failed_jobs=problems,
                       detail=f"run {run_id} concluded success but required release "
                              f"evidence is incomplete: {'; '.join(problems)}")
    return Verdict(state="SUCCESS", merge_sha=merge_sha, run_id=run_id,
                   run_conclusion=conclusion,
                   detail=f"deploy + post-deploy smoke terminal success for run {run_id}")


def evaluate_project_release(
    project_id: str,
    merge_sha: str,
    *,
    project: dict | None = None,
    runner=run_command,
) -> ReleaseEvaluation:
    """Evaluate one exact SHA using the same project authority as the CLI.

    ``runner`` is injectable so lifecycle consumers and tests can reuse the
    complete config/fetch/verdict path without shelling out to this script or
    weakening exact-SHA, event, branch, job, or smoke correlation.
    """
    resolved_project = get_project(project_id) if project is None else project
    config, config_error = resolve_release_config(project_id, resolved_project)
    if config is None:
        raise ValueError(config_error)

    repo = config["repo"]
    runs = fetch_deploy_runs(
        repo,
        merge_sha,
        config["workflow"],
        runner=runner,
    )
    verdict = evaluate_release(
        merge_sha,
        runs,
        lambda run_id: fetch_run_jobs(repo, run_id, runner=runner),
        required_jobs=config["required_jobs"],
        smoke_job=config["smoke_job"],
        required_smoke_steps=config["required_smoke_steps"],
        required_event=config["trigger_event"],
        required_branch=config["branch"],
    )
    return ReleaseEvaluation(
        project_id=project_id,
        repository=repo,
        workflow=config["workflow"],
        verdict=verdict,
    )


def render(verdict: Verdict, repo: str) -> str:
    lines = [
        f"RELEASE {verdict.state}: merge SHA {verdict.merge_sha}",
        f"  {verdict.detail}",
    ]
    if verdict.run_id:
        lines.append(f"  run: https://github.com/{repo}/actions/runs/{verdict.run_id}")
    if verdict.state in ("FAILURE", "CANCELLED"):
        lines += [
            f"  preserve logs: gh run view {verdict.run_id} --repo {repo} --log-failed",
            "  next (release gate, GH-1524): send ONE durable llm-collab packet + ONE doorbell;",
            "  do NOT blind-retry or redeploy; the task is NOT done until Codex records a",
            "  terminal disposition.",
        ]
    if verdict.state == "MISSING":
        lines += [
            "  next (release gate, GH-1524): missing is actionable — send ONE durable packet",
            "  + ONE doorbell so the absent deploy emission is investigated; never treat as pass.",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Exact-merge-SHA deploy release gate (GH-1524).")
    parser.add_argument("--project", required=True,
                        help="registered project id; repo/branch/workflow/evidence come from "
                             "its projects.json release_closure config")
    parser.add_argument("--merge-sha", required=True,
                        help="full merge commit SHA on the project's configured default branch")
    parser.add_argument("--wait", action="store_true",
                        help="poll until the run reaches a terminal state or timeout")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    args = parser.parse_args()

    import re as _re
    if not _re.fullmatch(r"[0-9a-fA-F]{40}", args.merge_sha):
        print("[error] --merge-sha must be exactly 40 hex characters (the full merge SHA)",
              file=sys.stderr)
        return 64
    if args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        print("[error] --poll-seconds and --timeout-seconds must both be > 0",
              file=sys.stderr)
        return 64

    deadline = time.monotonic() + args.timeout_seconds

    def once() -> ReleaseEvaluation:
        return evaluate_project_release(args.project, args.merge_sha)

    try:
        evaluation = once()
        while (args.wait
               and evaluation.verdict.state in ("PENDING", "MISSING")
               and time.monotonic() < deadline):
            time.sleep(args.poll_seconds)
            evaluation = once()
    except (RuntimeError, ValueError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 64

    verdict = evaluation.verdict
    repo = evaluation.repository
    if args.json:
        print(json.dumps({
            "state": verdict.state, "merge_sha": verdict.merge_sha,
            "project": args.project, "repo": repo,
            "run_id": verdict.run_id, "run_conclusion": verdict.run_conclusion,
            "failed_jobs": verdict.failed_jobs, "detail": verdict.detail,
        }, indent=2))
    else:
        print(render(verdict, repo))
    return TERMINAL_EXIT[verdict.state]


if __name__ == "__main__":
    sys.exit(main())
