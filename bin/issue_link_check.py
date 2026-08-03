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
# The (?<![\w-]) lookbehind keeps the keyword a standalone word: prose like
# "auto-close #503" or "preclose #7" must NOT count as a closing directive (it
# would falsely claim an auto-close GitHub won't actually perform).
CLOSING_REF = re.compile(rf"(?<![\w-])(?:{CLOSING})\b[:\s]+#(\d+)", re.IGNORECASE)
# Any issue reference: #N or a full issues URL.
ANY_REF = re.compile(r"(?:#|/issues/)(\d+)")
# Branch convention: claude/gh505-... encodes issue 505.
BRANCH_REF = re.compile(r"gh-?(\d+)", re.IGNORECASE)
MAX_SWEEP_PRS = 60
MAX_OPEN_ISSUES = 400


def closing_refs(text: str) -> set[int]:
    return {int(m) for m in CLOSING_REF.findall(text or "")}


def any_refs(text: str) -> set[int]:
    return {int(m) for m in ANY_REF.findall(text or "")}


def branch_issue(branch: str) -> int | None:
    m = BRANCH_REF.search(branch or "")
    return int(m.group(1)) if m else None


def classify_pr(title: str, body: str, branch: str) -> dict[str, list[int]]:
    """Classify a PR's issue links.

    Closing is detected in the PR BODY only: GitHub's durable auto-close fires from
    closing keywords in the PR body (and commit messages), NOT the PR title — a
    `Closes #N` in the title does not auto-close, so counting it would over-promise.

    The authoritative "this PR is FOR issue N" signal is the branch convention
    (`claude/gh<N>-...`). Orphan risk is that branch-declared issue when the body
    does not close it — a precise, low-noise nudge. Bare `#N` mentions anywhere are
    reported as informational references only (too noisy to treat as the PR's issue,
    and auto-closing them would be wrong).
    """
    closing = closing_refs(body)
    b = branch_issue(branch)
    orphan_risk = [b] if (b is not None and b not in closing) else []
    referenced = sorted(any_refs(f"{title}\n{body}") - closing - set(orphan_risk))
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


def resolve_repo(repo: str | None) -> str:
    """Explicit --repo, else the current checkout's repo — never a hardcoded default."""
    if repo:
        return repo
    got = _gh_json(["repo", "view", "--json", "nameWithOwner"])
    name = (got or {}).get("nameWithOwner")
    if not name:
        raise RuntimeError("could not resolve the repository; pass --repo <owner/name>")
    return name


def default_branch(repo: str) -> str:
    got = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])
    name = ((got or {}).get("defaultBranchRef") or {}).get("name")
    if not name:
        raise RuntimeError(f"could not resolve the default branch for {repo}")
    return name


def check_pr(repo: str, number: int) -> int:
    pr = _gh_json(["pr", "view", str(number), "--repo", repo,
                   "--json", "title,body,headRefName,baseRefName,state"])
    cls = classify_pr(pr.get("title", ""), pr.get("body", ""), pr.get("headRefName", ""))
    base = pr.get("baseRefName", "")
    default = default_branch(repo)
    if cls["closing"]:
        if base == default:
            print(f"PR #{number}: will auto-close on merge: "
                  + ", ".join(f"#{n}" for n in cls["closing"]))
        else:
            # Closing keywords only auto-close when the PR merges to the default branch.
            print(f"PR #{number}: has closing keyword(s) for "
                  + ", ".join(f"#{n}" for n in cls["closing"])
                  + f" but targets base '{base}', not the default '{default}' — these will "
                  "NOT auto-close on merge.")
            print("  Retarget the PR at the default branch, or close the issue manually.",
                  file=sys.stderr)
            return 1
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
    open_list = _gh_json(["issue", "list", "--repo", repo, "--state", "open",
                          "--limit", str(MAX_OPEN_ISSUES), "--json", "number"]) or []
    open_issues = {item["number"] for item in open_list}
    # Never silently treat a capped result as complete — say what may be missed.
    if len(merged) >= MAX_SWEEP_PRS:
        print(f"WARNING: hit the {MAX_SWEEP_PRS}-merged-PR cap — older merged PRs were "
              "not scanned; orphans beyond that window may be missed.", file=sys.stderr)
    if len(open_list) >= MAX_OPEN_ISSUES:
        print(f"WARNING: hit the {MAX_OPEN_ISSUES}-open-issue cap — some open issues were "
              "not loaded; results may be incomplete.", file=sys.stderr)
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
    p.add_argument("--repo", default=None,
                   help="owner/name; defaults to the current checkout's repo (never hardcoded)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="check one PR for orphan risk")
    g.add_argument("--sweep", action="store_true", help="find open issues referenced by merged PRs")
    p.add_argument("--strict", action="store_true", help="exit nonzero on orphan risk / found orphans")
    args = p.parse_args(argv)
    repo = resolve_repo(args.repo)
    rc = check_pr(repo, args.pr) if args.pr is not None else sweep(repo)
    return rc if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
