#!/usr/bin/env python3.11
"""issue_link_check.py — keep merged PRs from orphaning their related issue (GH-507).

The inverse of a "must have an issue to open a PR" gate: this never requires an
issue and never blocks a merge. It only helps when a PR *is* related to an issue —
so the issue gets closed on merge (or is explicitly kept open), never silently
orphaned.

Modes:
  --pr N     Advisory check of one PR: for each issue it references, report whether
             the PR will auto-close it (a GitHub closing keyword precedes the ref),
             explicitly keeps it open (`Related #N`/URL with no keyword), or is an
             ORPHAN RISK (referenced with neither an intended state). Exit 0 always
             unless --strict; orphan risks are warnings.
  --sweep    Backstop: list OPEN issues referenced by recently MERGED PRs — likely
             orphans a human/agent should close or re-mark `Related`.

"Strictly related" is the author's explicit closing keyword, never inferred from a
bare mention — auto-closing a loosely-referenced issue would wrongly close it.

# ponytail: gh CLI via subprocess (no extra deps); pure classifiers below are the
# tested core, the gh layer is thin.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# GitHub's closing keywords (https://docs.github.com/.../linking-a-pull-request-to-an-issue).
CLOSING = r"clos(?:e|es|ed)|fix(?:|es|ed)|resolv(?:e|es|ed)"
# A closing keyword immediately before a #N (optional colon/space), case-insensitive.
CLOSING_REF = re.compile(rf"\b(?:{CLOSING})\b[:\s]+#(\d+)", re.IGNORECASE)
# Any issue reference: #N or a full issues URL.
ANY_REF = re.compile(r"(?:#|/issues/)(\d+)")
# Branch convention: claude/gh505-... encodes issue 505.
BRANCH_REF = re.compile(r"gh-?(\d+)", re.IGNORECASE)
MAX_SWEEP_PRS = 60


def closing_refs(text: str) -> set[int]:
    return {int(m) for m in CLOSING_REF.findall(text or "")}


def any_refs(text: str) -> set[int]:
    return {int(m) for m in ANY_REF.findall(text or "")}


def branch_issue(branch: str) -> int | None:
    m = BRANCH_REF.search(branch or "")
    return int(m.group(1)) if m else None


def classify_pr(title: str, body: str, branch: str) -> dict[str, list[int]]:
    """Classify a PR's issue links.

    The authoritative "this PR is FOR issue N" signal is the branch convention
    (`claude/gh<N>-...`). Orphan risk is that branch-declared issue when the PR does
    not close it — a precise, low-noise nudge. Bare `#N` mentions in prose are
    reported as informational references only (they are too noisy to treat as the
    PR's issue, and auto-closing them would be wrong).
    """
    text = f"{title}\n{body}"
    closing = closing_refs(text)
    b = branch_issue(branch)
    orphan_risk = [b] if (b is not None and b not in closing) else []
    referenced = sorted(any_refs(text) - closing - set(orphan_risk))
    return {
        "closing": sorted(closing),
        "orphan_risk": orphan_risk,
        "referenced": referenced,
    }


def _gh_json(args: list[str]):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def check_pr(repo: str, number: int) -> int:
    pr = _gh_json(["pr", "view", str(number), "--repo", repo,
                   "--json", "title,body,headRefName,state"])
    cls = classify_pr(pr.get("title", ""), pr.get("body", ""), pr.get("headRefName", ""))
    if cls["closing"]:
        print(f"PR #{number}: will auto-close on merge: "
              + ", ".join(f"#{n}" for n in cls["closing"]))
    if cls["referenced"]:
        print(f"PR #{number}: also references (informational): "
              + ", ".join(f"#{n}" for n in cls["referenced"]))
    if cls["orphan_risk"]:
        print(f"PR #{number}: ORPHAN RISK — the branch is for "
              + ", ".join(f"#{n}" for n in cls["orphan_risk"])
              + " but the PR does not close it.")
        print("  If this PR resolves that issue, add `Closes #N` to the body so it "
              "closes on merge; if it intentionally leaves it open, ignore this.",
              file=sys.stderr)
        return 1
    if not cls["closing"]:
        print(f"PR #{number}: references no branch issue to close (fine — none required).")
    return 0


def sweep(repo: str) -> int:
    merged = _gh_json(["pr", "list", "--repo", repo, "--state", "merged",
                       "--limit", str(MAX_SWEEP_PRS), "--json", "number,title,body"]) or []
    # One bounded call for the open set, then intersect — avoids an N+1 gh lookup.
    open_issues = {
        item["number"]
        for item in (_gh_json(["issue", "list", "--repo", repo, "--state", "open",
                               "--limit", "400", "--json", "number"]) or [])
    }
    orphans: dict[int, int] = {}  # issue -> the merged PR that referenced it
    for pr in merged:
        text = f"{pr.get('title','')}\n{pr.get('body','')}"
        for issue in any_refs(text):
            if issue in open_issues and issue not in orphans:
                orphans[issue] = pr["number"]
    if orphans:
        print(f"Open issues referenced by the last {len(merged)} merged PRs "
              "(review — close if resolved, or keep an explicit Related):")
        for issue, pr_n in sorted(orphans.items()):
            print(f"  #{issue}  (referenced by merged PR #{pr_n})")
        return 1
    print(f"No open issues orphaned by the last {len(merged)} merged PRs.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default="pixexid/llm-collab")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="check one PR for orphan risk")
    g.add_argument("--sweep", action="store_true", help="find open issues referenced by merged PRs")
    p.add_argument("--strict", action="store_true", help="exit nonzero on orphan risk / found orphans")
    args = p.parse_args(argv)
    rc = check_pr(args.repo, args.pr) if args.pr is not None else sweep(args.repo)
    return rc if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
