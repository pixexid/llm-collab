#!/usr/bin/env python3.11
"""Watch a GitHub PR for ANY update and report the delta.

Reliable by construction: no webhooks, no ETag bookkeeping. Each poll builds a
signature over three endpoints the connector/CI actually touch:
  - issues/{pr}/timeline  -> comments, reviews, commits, labels, state/merge
  - issues/{pr}/reactions -> the connector's thumbs-up verdict
  - commits/{sha}/check-runs + /status -> CI pass/fail on the current head
The timeline alone misses reactions and CI, which is exactly what we watch for.

Reactions on review-request comments are included, not just PR-level reactions,
so a reaction-only CLEAN verdict is caught. Every gh call is bounded by a timeout
and a page budget, so a stalled or pathologically large response fails the poll
closed (the loop retries) rather than hanging.

Exit 0 on first change (prints a JSON delta), exit 3 on timeout, exit 2 on error.

# ponytail: poll+diff, not ETag/webhooks — 3 reqs/min vs a 5000/hr budget is free.
"""
import argparse
import json
import subprocess
import sys
import time

# A stalled `gh api` must never hang the watch: bound every call, and fail the
# poll closed (the loop retries next interval) rather than blocking forever.
GH_CALL_TIMEOUT = 30.0
PER_PAGE = 100
# Budget guard: paginate manually so the cap is enforced BEFORE each request — a
# pathological timeline can never spend more than MAX_PAGES calls. ~1000 items is
# far beyond any real PR.
MAX_PAGES = 10
# A very chatty PR has unboundedly many comments; fetching per-comment reactions
# for all of them is unbounded work. A fresh connector verdict (its 👍 on an
# "@codex review" comment) rides the most recent comments, so cap the per-comment
# reaction fetch to the newest N — bounded cost, still catches the live signal.
MAX_REACTION_COMMENTS = 30


def _flatten_pages(pages):
    """Flatten per-page values into one list. Array pages are concatenated; a
    non-list page (single-object endpoint) is kept as one element."""
    out = []
    for page in pages:
        out.extend(page if isinstance(page, list) else [page])
    return out


def _gh_call(path):
    """One `gh api` invocation, bounded by a timeout; returns stripped stdout."""
    try:
        r = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, timeout=GH_CALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"gh api {path} exceeded {GH_CALL_TIMEOUT:.0f}s"
        ) from error
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path}: {r.stderr.strip()}")
    return r.stdout.strip()


def _page_len(value):
    """Item count of one page across the shapes we fetch: a bare array, a
    check-runs object ({check_runs: [...]}), or a single object (0 -> no more
    pages, ends pagination)."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict) and isinstance(value.get("check_runs"), list):
        return len(value["check_runs"])
    return 0


def _gh_pages(path):
    """Per-page JSON values, paginating manually with explicit per_page/page so
    the budget is enforced BEFORE each request. A page shorter than PER_PAGE (or
    a single object) ends pagination; a full MAX_PAGES-th page means more remains
    than the budget allows, so fail the poll closed rather than paginate on."""
    sep = "&" if "?" in path else "?"
    pages = []
    for page_num in range(1, MAX_PAGES + 1):
        body = _gh_call(f"{path}{sep}per_page={PER_PAGE}&page={page_num}")
        if not body:
            break
        value = json.loads(body)
        pages.append(value)
        if _page_len(value) < PER_PAGE:
            break
    else:
        raise RuntimeError(
            f"gh api {path}: exceeds MAX_PAGES={MAX_PAGES}; failing closed"
        )
    return pages


def gh_one(path):
    """A single-object resource (first page)."""
    pages = _gh_pages(path)
    return pages[0] if pages else {}


def gh_array(path):
    """A paginated array resource, flattened across all pages."""
    return _flatten_pages(_gh_pages(path))


def _timeline_sig(timeline):
    """Signature tuples for the timeline. Includes an update marker (updated_at)
    so an in-place edit to an existing comment/review — same event and id —
    still changes the signature and is reported."""
    return [
        (e.get("event"), e.get("id") or e.get("sha") or e.get("created_at"),
         e.get("updated_at"))
        for e in timeline
    ]


def snapshot(repo, pr):
    """Return (signature_dict, raw) — one poll's worth of state."""
    pull = gh_one(f"repos/{repo}/pulls/{pr}")
    sha = pull.get("head", {}).get("sha", "")
    state = pull.get("state", "")
    merged = pull.get("merged", False)

    timeline = gh_array(f"repos/{repo}/issues/{pr}/timeline")
    tl = _timeline_sig(timeline)

    reactions = gh_array(f"repos/{repo}/issues/{pr}/reactions")
    rx = [f"{x.get('user', {}).get('login')}:{x.get('content')}" for x in reactions]
    # Reactions can land on a review-request comment, not just the PR itself — the
    # connector's thumbs-up verdict on a manual "@codex review" comment lives
    # there. Include per-comment reactions so a reaction-only CLEAN is not missed.
    for comment in gh_array(f"repos/{repo}/issues/{pr}/comments")[-MAX_REACTION_COMMENTS:]:
        cid = comment.get("id")
        for rxn in gh_array(f"repos/{repo}/issues/comments/{cid}/reactions"):
            rx.append(f"c{cid}:{rxn.get('user', {}).get('login')}:{rxn.get('content')}")
    rx = sorted(rx)

    checks = {}
    if sha:
        for page in _gh_pages(f"repos/{repo}/commits/{sha}/check-runs"):
            for run in page.get("check_runs", []):
                # Key by name#id, not name alone: a re-run shares the name, and
                # collapsing on name would hide the new run's conclusion.
                checks[f"{run.get('name')}#{run.get('id')}"] = (
                    run.get("conclusion") or run.get("status")
                )
        st = gh_one(f"repos/{repo}/commits/{sha}/status")
        checks["_combined_status"] = st.get("state")

    sig = {
        "state": state, "merged": merged, "head": sha,
        "timeline": tl, "reactions": rx, "checks": checks,
    }
    return sig, {"timeline": timeline, "reactions": reactions}


def diff(old, new, raw_new):
    """Human-readable summary of what changed between two signatures."""
    out = []
    if old["head"] != new["head"]:
        out.append(f"head: {old['head'][:10]} -> {new['head'][:10]}")
    if old["state"] != new["state"] or old["merged"] != new["merged"]:
        out.append(f"state: {old['state']}/merged={old['merged']} "
                   f"-> {new['state']}/merged={new['merged']}")
    if old["reactions"] != new["reactions"]:
        added = sorted(set(new["reactions"]) - set(old["reactions"]))
        out.append(f"reactions +{added}" if added else "reactions changed")
    if old["checks"] != new["checks"]:
        changed = {k: v for k, v in new["checks"].items() if old["checks"].get(k) != v}
        out.append(f"checks: {changed}")
    if old["timeline"] != new["timeline"]:
        old_ids = {t[1] for t in old["timeline"]}
        fresh = [e for e in raw_new["timeline"] if (e.get("id") or e.get("sha")
                 or e.get("created_at")) not in old_ids]
        for e in fresh:
            ev = e.get("event")
            who = (e.get("actor") or e.get("user") or {}).get("login", "?")
            note = ""
            if ev in ("commented", "reviewed"):
                body = (e.get("body") or "")[:120].replace("\n", " ")
                note = f" [{e.get('state', '')}] {body}"
            out.append(f"timeline: {ev} by {who}{note}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--repo", required=True,
                    help="owner/name — required so a worker never watches a default repo")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--once", action="store_true",
                    help="print the current signature and exit (no watching)")
    args = ap.parse_args()

    try:
        base, raw = snapshot(args.repo, args.pr)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.once:
        print(json.dumps(base, indent=2))
        return 0

    deadline = time.monotonic() + args.timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Clamp the wait so the last poll never overshoots the deadline by up to
        # a full interval.
        time.sleep(min(args.interval, remaining))
        try:
            cur, raw = snapshot(args.repo, args.pr)
        except RuntimeError as e:
            print(f"WARN: {e}", file=sys.stderr)
            continue
        if cur != base:
            changes = diff(base, cur, raw)
            print(json.dumps({"pr": args.pr, "changes": changes,
                              "signature": cur}, indent=2))
            return 0
    print(f"TIMEOUT: no change on #{args.pr} after {args.timeout:.0f}s",
          file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
