#!/usr/bin/env python3
"""
review_request.py — post a Codex review request whose SHA can only be real.

The exact-head SHA in a review request is what every terminal signal binds to,
so it must never be hand-typed: PR #347 came to contain a fabricated,
later-retracted SHA precisely because a model typed one. This tool has no
--sha option. It reads the PR head from GitHub, reads the local HEAD, refuses
on mismatch, and enforces the request budget from docs/workflows/
commit-push-prs.md: one initial request per candidate head, plus the single
request-anchored re-trigger as the only exempted recovery.

  python bin/review_request.py --pr 351 \
      --focus "exact-session authority selection, cumulative bounds" \
      --contract 349
  python bin/review_request.py --pr 351 --focus "..." --retrigger
  python bin/review_request.py --pr 351 --focus "..." --dry-run

Exits 0 after posting (or printing with --dry-run); exits 2 with the reason on
any refusal. Read-only against the workspace; the only write is the PR comment.
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

REQUEST_MARKER = "@codex review"
COMMENT_SCAN_LIMIT = 200


def run_json(argv: list[str]) -> object:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"error: {' '.join(argv)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def pr_head(pr: int) -> str:
    data = run_json(["gh", "pr", "view", str(pr), "--json", "headRefOid"])
    return data["headRefOid"]


def pr_comment_bodies(pr: int) -> list[str]:
    data = run_json(
        ["gh", "pr", "view", str(pr), "--json", "comments", "--jq",
         f"[.comments[-{COMMENT_SCAN_LIMIT}:][].body]"]
    )
    return list(data)


def local_head() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise SystemExit(f"error: git rev-parse HEAD failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def count_prior_requests(bodies: list[str], sha: str) -> int:
    return sum(
        1 for body in bodies if body.startswith(REQUEST_MARKER) and sha in body
    )


def refusal_reason(prior: int, retrigger: bool) -> str | None:
    if prior == 0:
        return None
    if prior == 1 and retrigger:
        return None
    if prior >= 2:
        return (
            f"request budget for this head is spent ({prior} requests already); "
            "there is no further request to issue — continue to the exact-head "
            "operator disposition"
        )
    return (
        "an initial request for this exact head already exists; pass "
        "--retrigger only if the connector silently dropped it (the single "
        "exempted recovery)"
    )


def build_request_body(
    focus: str, sha: str, contract: int | None = None, note: str | None = None
) -> str:
    if not focus.strip():
        raise SystemExit("error: --focus must name at least one review lens")
    parts = [f"{REQUEST_MARKER} for {focus.strip()} at exact head `{sha}`."]
    if contract is not None:
        parts.append(
            f"Review the full diff against the lane contract in #{contract} "
            "through those lenses."
        )
    else:
        parts.append("Please review the full diff through those lenses.")
    if note:
        parts.append(note.strip())
    return " ".join(parts)


def post_comment(pr: int, body: str) -> None:
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--body", body],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"error: posting the request failed: {proc.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--focus", required=True,
        help="comma-separated review lenses (every Tier A family the diff touches)",
    )
    parser.add_argument(
        "--contract", type=int, default=None,
        help="issue number carrying the lane contract, for Tier A lanes",
    )
    parser.add_argument("--note", default=None, help="one extra sentence appended verbatim")
    parser.add_argument(
        "--retrigger", action="store_true",
        help="use the single exempted re-trigger for a silently dropped request",
    )
    parser.add_argument(
        "--no-local-check", action="store_true",
        help="skip verifying that local HEAD equals the PR head",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not post")
    args = parser.parse_args(argv)

    sha = pr_head(args.pr)
    if not args.no_local_check:
        local = local_head()
        if local != sha:
            raise SystemExit(
                f"error: local HEAD {local} != PR head {sha}; push first, or "
                "pass --no-local-check if you are requesting from outside the lane"
            )

    prior = count_prior_requests(pr_comment_bodies(args.pr), sha)
    reason = refusal_reason(prior, args.retrigger)
    if reason:
        raise SystemExit(f"error: {reason}")

    body = build_request_body(args.focus, sha, args.contract, args.note)
    if args.dry_run:
        print(body)
        return 0
    post_comment(args.pr, body)
    print(f"posted review request for exact head {sha} on PR #{args.pr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
