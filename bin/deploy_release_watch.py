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
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stderr.strip()}"
        )
    return result.stdout


def fetch_deploy_runs(repo: str, merge_sha: str, workflow: str, runner=run_command) -> list[dict]:
    """All runs of the deploy workflow for the exact head SHA (API-side filter),
    newest first. The head_sha query is the exact-correlation guarantee: a run
    for any other SHA is never returned, so it can never be misread as covering
    this merge."""
    raw = runner([
        "gh", "api",
        f"repos/{repo}/actions/runs?head_sha={merge_sha}&per_page=50",
        "--jq", ".workflow_runs",
    ])
    runs = json.loads(raw or "[]")
    return [r for r in runs if r.get("name") == workflow or r.get("path", "").endswith(f"/{workflow}.yml")]


def fetch_run_jobs(repo: str, run_id: int, runner=run_command) -> list[dict]:
    raw = runner([
        "gh", "api",
        f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=50",
        "--jq", ".jobs",
    ])
    return json.loads(raw or "[]")


def evaluate_release(merge_sha: str, runs: list[dict], jobs_for_run) -> Verdict:
    """Pure verdict logic. `runs` must already be exact-SHA-filtered (the fetch
    guarantees it; the assertion below fails closed if a caller ever passes a
    mismatched run). `jobs_for_run(run_id) -> list[dict]` supplies job detail
    for the candidate run."""
    for r in runs:
        if r.get("head_sha") != merge_sha:
            raise ValueError(
                f"exact-SHA correlation violated: run {r.get('id')} has head_sha "
                f"{r.get('head_sha')} != merge SHA {merge_sha}"
            )
    if not runs:
        return Verdict(
            state="MISSING", merge_sha=merge_sha,
            detail="no deploy run exists for the exact merge SHA — actionable, not a pass",
        )
    # Latest run per rerun semantics: judge the newest attempt, never a stale
    # earlier run (mirrors the latest-per-context CI rule).
    latest = max(runs, key=lambda r: (r.get("run_attempt") or 0, r.get("id") or 0))
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
    # Run-level success is necessary but not sufficient: every job (detect AND
    # deploy — the deploy job carries the post-deploy smoke steps) must itself
    # be success, so a whitewashed/partial run cannot read as a clean release.
    jobs = jobs_for_run(run_id)
    failed = [j.get("name", "?") for j in jobs
              if j.get("conclusion") not in ("success", "skipped")]
    if failed:
        return Verdict(state="FAILURE", merge_sha=merge_sha, run_id=run_id,
                       run_conclusion=conclusion, failed_jobs=failed,
                       detail=f"run {run_id} concluded success but jobs did not: {', '.join(failed)}")
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
    args = parser.parse_args()

    if len(args.merge_sha) < 40:
        print("[error] --merge-sha must be the FULL 40-char merge SHA (exact correlation)",
              file=sys.stderr)
        return 64

    deadline = time.monotonic() + args.timeout_seconds

    def once() -> Verdict:
        runs = fetch_deploy_runs(args.repo, args.merge_sha, args.workflow)
        return evaluate_release(args.merge_sha, runs,
                                lambda run_id: fetch_run_jobs(args.repo, run_id))

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
