#!/usr/bin/env python3
"""
deploy_release_watch.py — exact-merge-SHA post-merge deploy release gate (GH-1524).

Release closure does not end at merge. This tool correlates the production
deploy workflow run to the EXACT merge SHA and produces one of five verdicts:

  SUCCESS    deploy run for the exact SHA completed success AND every job
             (detect + deploy, which includes the post-deploy smoke steps)
             concluded success.
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
  bin/deploy_release_watch.py --repo pixexid/amiga --merge-sha <full-sha>
      [--workflow deploy] [--wait] [--timeout-seconds 900] [--poll-seconds 30]
      [--json]

Exit codes: 0 SUCCESS | 10 FAILURE | 11 CANCELLED | 12 MISSING | 13 PENDING
            | 64 usage/environment error.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import subprocess
import time
from dataclasses import dataclass, field


TERMINAL_EXIT = {"SUCCESS": 0, "FAILURE": 10, "CANCELLED": 11, "MISSING": 12, "PENDING": 13}


@dataclass
class Verdict:
    state: str                     # SUCCESS | FAILURE | CANCELLED | MISSING | PENDING
    merge_sha: str
    run_id: int | None = None
    run_conclusion: str | None = None
    failed_jobs: list[str] = field(default_factory=list)
    detail: str = ""


def run_command(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
    except FileNotFoundError as error:
        raise RuntimeError(
            f"executable not found: {argv[0]} — install GitHub CLI (gh) and ensure it is on PATH"
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


# Required release evidence is PER-REPO (POSITIVE lists — an empty/partial
# jobs payload or a skipped heavy job must fail closed, never read as success;
# unknown extra jobs may exist but cannot substitute). The job/step names are
# project-specific: a repo WITHOUT a profile fails closed at the CLI (exit 64)
# unless the caller supplies --required-jobs/--required-smoke-steps explicitly
# (GH-1524 PR review P1 — never grade another project against Amiga's labels).
REQUIRED_JOBS = ("detect", "deploy")
REQUIRED_SMOKE_STEPS = ("Verify production hosts", "Verify production auth")
REPO_PROFILES: dict[str, dict[str, tuple[str, ...]]] = {
    "pixexid/amiga": {
        "required_jobs": REQUIRED_JOBS,
        "required_smoke_steps": REQUIRED_SMOKE_STEPS,
    },
}


def evaluate_release(
    merge_sha: str,
    runs: list[dict],
    jobs_for_run,
    *,
    required_event: str = "push",
    required_branch: str = "main",
    required_jobs: tuple[str, ...] = REQUIRED_JOBS,
    required_smoke_steps: tuple[str, ...] = REQUIRED_SMOKE_STEPS,
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
    # Only the AUTOMATIC main-push deploy counts. A workflow_dispatch (or any
    # other event/branch) run on the same SHA is a manual intervention and can
    # never supersede or cover the automatic run's outcome — that would be a
    # blind-retry laundering channel (GH-1524 cold-review P1).
    excluded = [r for r in runs
                if r.get("event") != required_event or r.get("head_branch") != required_branch]
    candidates = [r for r in runs if r not in excluded]
    if not candidates:
        note = ""
        if excluded:
            ids = ", ".join(str(r.get("id")) for r in excluded)
            note = (f" ({len(excluded)} non-qualifying run(s) exist for this SHA — e.g. "
                    f"workflow_dispatch/off-branch: {ids} — they never satisfy closure)")
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
    for name in required_jobs:
        job = by_name.get(name)
        if job is None:
            problems.append(f"required job '{name}' missing from run evidence")
        elif job.get("conclusion") != "success":
            problems.append(f"required job '{name}' concluded {job.get('conclusion')!r}, not success")
    deploy_job = by_name.get("deploy") or {}
    steps = {s.get("name", ""): s for s in (deploy_job.get("steps") or [])}
    for step_name in required_smoke_steps:
        step = steps.get(step_name)
        if step is None:
            problems.append(f"required smoke step '{step_name}' missing from deploy job evidence")
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
    parser.add_argument("--repo", required=True, help="owner/name, e.g. pixexid/amiga")
    parser.add_argument("--merge-sha", required=True, help="full merge commit SHA on main")
    parser.add_argument("--workflow", default="deploy", help="workflow name (default: deploy)")
    parser.add_argument("--wait", action="store_true",
                        help="poll until the run reaches a terminal state or timeout")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    parser.add_argument("--required-jobs", default=None,
                        help="comma-separated required job names (overrides the repo profile)")
    parser.add_argument("--required-smoke-steps", default=None,
                        help="comma-separated required smoke step names inside the deploy job")
    args = parser.parse_args()

    import re as _re
    if not _re.fullmatch(r"[0-9a-fA-F]{40}", args.merge_sha):
        print("[error] --merge-sha must be exactly 40 hex characters (the full merge SHA)",
              file=sys.stderr)
        return 64

    profile = REPO_PROFILES.get(args.repo, {})
    required_jobs = (tuple(x.strip() for x in args.required_jobs.split(",") if x.strip())
                     if args.required_jobs else profile.get("required_jobs"))
    required_smoke = (tuple(x.strip() for x in args.required_smoke_steps.split(",") if x.strip())
                      if args.required_smoke_steps is not None
                      else profile.get("required_smoke_steps"))
    if not required_jobs:
        print(f"[error] no release-evidence profile for {args.repo!r} — job/step names are "
              "project-specific; pass --required-jobs (and --required-smoke-steps) explicitly",
              file=sys.stderr)
        return 64

    deadline = time.monotonic() + args.timeout_seconds

    def once() -> Verdict:
        runs = fetch_deploy_runs(args.repo, args.merge_sha, args.workflow)
        return evaluate_release(args.merge_sha, runs,
                                lambda run_id: fetch_run_jobs(args.repo, run_id),
                                required_jobs=required_jobs,
                                required_smoke_steps=required_smoke or ())

    try:
        verdict = once()
        while args.wait and verdict.state in ("PENDING", "MISSING") and time.monotonic() < deadline:
            time.sleep(args.poll_seconds)
            verdict = once()
    except RuntimeError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 64

    if args.json:
        print(json.dumps({
            "state": verdict.state, "merge_sha": verdict.merge_sha,
            "run_id": verdict.run_id, "run_conclusion": verdict.run_conclusion,
            "failed_jobs": verdict.failed_jobs, "detail": verdict.detail,
        }, indent=2))
    else:
        print(render(verdict, args.repo))
    return TERMINAL_EXIT[verdict.state]


if __name__ == "__main__":
    sys.exit(main())
