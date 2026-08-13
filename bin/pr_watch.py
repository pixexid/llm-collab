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
from datetime import datetime
import json
import re
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
# for all of them is unbounded work. Bound it with a cap and FAIL CLOSED past the
# cap (the poll retries + WARNs) — silently skipping older comments could miss the
# authoritative reaction verdict, which is worse than a loud, retryable failure.
MAX_REACTION_COMMENTS = 30
# MAX_PAGES is PER-CALL; one snapshot makes ~36 paginated calls (up to
# MAX_REACTION_COMMENTS per-comment reaction loops), so per-call caps alone let a
# pathological PR issue hundreds of sequential requests and overrun the watcher
# deadline by hours. A single shared budget across the whole snapshot bounds the
# cumulative page count, and the deadline is observed before every request.
MAX_SNAPSHOT_PAGES = 80
CONNECTOR_LOGINS = frozenset({"chatgpt-codex-connector[bot]", "chatgpt-codex-connector"})
REVIEW_REQUEST_MARKER = "@codex review"
FULL_SHA_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{40})(?![0-9a-fA-F])")
# A tail snapshot starts after the observation window, so it gets one bounded
# request budget of its own rather than making the observation deadline porous.
FINAL_SNAPSHOT_GRACE = GH_CALL_TIMEOUT


class _Budget:
    """Cumulative page + deadline budget shared across one snapshot's gh calls,
    charged before every page request so many paginated endpoints can't
    collectively exceed MAX_SNAPSHOT_PAGES requests or run past the watcher
    deadline. Fails the poll closed (it retries) on either."""

    def __init__(self, max_pages, deadline=None):
        self.pages_left = max_pages
        self.deadline = deadline

    def charge(self, path):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RuntimeError(f"snapshot exceeded watcher deadline before {path}")
        if self.pages_left <= 0:
            raise RuntimeError(
                f"snapshot exceeded MAX_SNAPSHOT_PAGES budget before {path}"
            )
        self.pages_left -= 1


def _flatten_pages(pages):
    """Flatten per-page values into one list. Array pages are concatenated; a
    non-list page (single-object endpoint) is kept as one element."""
    out = []
    for page in pages:
        out.extend(page if isinstance(page, list) else [page])
    return out


def _gh_call(path, deadline=None):
    """One `gh api` invocation, bounded by the shared deadline."""
    timeout = GH_CALL_TIMEOUT
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise RuntimeError(f"gh api {path}: watcher deadline reached")
    try:
        r = subprocess.run(
            ["gh", "api", path],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"gh api {path} exceeded {timeout:.3f}s"
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


def _gh_pages(path, budget=None):
    """Per-page JSON values, paginating manually with explicit per_page/page so
    the per-call cap is enforced BEFORE each request. A page shorter than PER_PAGE
    (or a single object) ends pagination; a full MAX_PAGES-th page means more
    remains than the budget allows, so fail the poll closed rather than paginate
    on. When a shared `budget` is passed it is charged before every request, so
    the whole snapshot's cumulative page count and deadline are bounded too."""
    sep = "&" if "?" in path else "?"
    pages = []
    for page_num in range(1, MAX_PAGES + 1):
        if budget is not None:
            budget.charge(path)
        request_path = f"{path}{sep}per_page={PER_PAGE}&page={page_num}"
        body = (
            _gh_call(request_path)
            if budget is None or budget.deadline is None
            else _gh_call(request_path, budget.deadline)
        )
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


def gh_one(path, budget=None):
    """A single-object resource (first page)."""
    pages = _gh_pages(path, budget)
    return pages[0] if pages else {}


def gh_array(path, budget=None):
    """A paginated array resource, flattened across all pages."""
    return _flatten_pages(_gh_pages(path, budget))


def _timeline_sig(timeline):
    """Signature tuples for the timeline. Includes an update marker (updated_at)
    so an in-place edit to an existing comment/review — same event and id —
    still changes the signature and is reported."""
    return [
        (e.get("event"), e.get("id") or e.get("sha") or e.get("created_at"),
         e.get("updated_at"))
        for e in timeline
    ]


def connector_review_oids(timeline):
    """Return exact commit OIDs reviewed by the Codex connector."""
    return sorted(
        {
            event["commit_id"]
            for event in timeline
            if event.get("event") == "reviewed"
            and (event.get("user") or event.get("actor") or {}).get("login")
            in CONNECTOR_LOGINS
            and isinstance(event.get("commit_id"), str)
            and event["commit_id"]
        }
    )


def _connector_event(event):
    return (event.get("user") or event.get("actor") or {}).get("login") in CONNECTOR_LOGINS


def _exact_connector_reviews(timeline, head):
    return [
        event
        for event in timeline
        if event.get("event") == "reviewed"
        and _connector_event(event)
        and event.get("commit_id") == head
    ]


def connector_review_status(timeline, head):
    """Classify only connector reviews bound to the captured head.

    A review container without a body proves pickup, not a terminal verdict:
    inline review threads carry the authoritative findings and are adjudicated
    by the normal reviewed-artifact gate.
    """
    if not isinstance(timeline, list):
        return "no_connector_review"
    reviews = _exact_connector_reviews(timeline, head)
    if any(isinstance(event.get("body"), str) and event["body"].strip()
           for event in reviews):
        return "review_seen"
    if reviews:
        return "review_pending"
    return "no_connector_review"


def _github_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _reaction_artifacts(raw):
    reactions = raw.get("reactions")
    comment_reactions = raw.get("comment_reactions")
    if not isinstance(reactions, list) or not isinstance(comment_reactions, list):
        return []
    return (
        [{"comment": None, "reaction": reaction}
         for reaction in reactions]
        + comment_reactions
    )


def _reaction_key(artifact):
    reaction = artifact.get("reaction") or {}
    comment = artifact.get("comment") or {}
    comment_id = comment.get("id")
    return (comment_id, reaction["id"]) if reaction.get("id") is not None else None


def _new_connector_plus_ones(current_raw, baseline_raw):
    baseline_capture = _github_timestamp(baseline_raw.get("captured_at"))
    current_capture = _github_timestamp(current_raw.get("captured_at"))
    if baseline_capture is None or current_capture is None:
        return []
    baseline_keys = {
        key for artifact in _reaction_artifacts(baseline_raw)
        if (key := _reaction_key(artifact)) is not None
    }
    fresh = []
    for artifact in _reaction_artifacts(current_raw):
        reaction = artifact.get("reaction") or {}
        if ((reaction.get("user") or {}).get("login") not in CONNECTOR_LOGINS
                or reaction.get("content") != "+1"):
            continue
        created = _github_timestamp(reaction.get("created_at"))
        key = _reaction_key(artifact)
        if (created is not None and baseline_capture < created <= current_capture
                and key is not None and key not in baseline_keys):
            fresh.append(artifact)
    return fresh


def _full_sha_tokens(value):
    if not isinstance(value, str):
        return []
    return [match.group(1).lower() for match in FULL_SHA_RE.finditer(value)]


def _names_exact_head(value, head):
    return isinstance(head, str) and _full_sha_tokens(value) == [head.lower()]


def _request_comments(raw, head):
    if not isinstance(raw.get("comments"), list):
        return []
    candidates = []
    for comment in raw.get("comments", []):
        body = comment.get("body") or ""
        if REVIEW_REQUEST_MARKER not in body:
            continue
        shas = _full_sha_tokens(body)
        if not isinstance(head, str) or head.lower() not in shas:
            continue
        if not _names_exact_head(body, head):
            return []
        created = _github_timestamp(comment.get("created_at"))
        if created is None:
            return []
        candidates.append((created, comment))
    if not candidates:
        return []
    return [comment for _, comment in sorted(candidates, key=lambda item: item[0])]


def _manual_request_plus_one(current_raw, head, fresh):
    requests = _request_comments(current_raw, head)
    if not requests:
        return False
    latest = requests[-1]
    latest_id = latest.get("id")
    if latest_id is None or (latest.get("user") or {}).get("login") in CONNECTOR_LOGINS:
        return False
    for comment in current_raw.get("comments", []):
        if ((comment.get("user") or {}).get("login") in CONNECTOR_LOGINS
                and comment.get("id") != latest_id):
            return False
    if any((reaction.get("user") or {}).get("login") in CONNECTOR_LOGINS
           for reaction in current_raw.get("reactions", [])):
        return False
    for artifact in current_raw.get("comment_reactions", []):
        reaction = artifact.get("reaction") or {}
        if ((reaction.get("user") or {}).get("login") not in CONNECTOR_LOGINS):
            continue
        if ((artifact.get("comment") or {}).get("id") != latest_id
                or reaction.get("content") not in {"+1", "eyes"}):
            return False
    for artifact in fresh:
        comment = artifact.get("comment") or {}
        reaction = artifact.get("reaction") or {}
        if comment.get("id") != latest_id:
            continue
        reacted = _github_timestamp(reaction.get("created_at"))
        edited = _github_timestamp(comment.get("updated_at"))
        if reacted is not None and edited is not None and edited < reacted:
            return True
    return False


def connector_artifact_status(raw, head, baseline_raw=None, prior_pending=False,
                              prior_connector_artifact=False):
    """Return the one settle status for a bounded exact-head observation."""
    review_status = connector_review_status(raw.get("timeline", []), head)
    if review_status != "no_connector_review":
        return review_status
    if prior_pending:
        return "review_pending"
    if not all(key in raw for key in (
            "timeline", "reactions", "comments", "comment_reactions", "captured_at")):
        return "no_connector_review"
    if (not isinstance(raw["timeline"], list)
            or not isinstance(raw["reactions"], list)
            or not isinstance(raw["comments"], list)
            or not isinstance(raw["comment_reactions"], list)):
        return "no_connector_review"
    connector_artifact = any(
        _connector_event(event) for event in raw.get("timeline", [])
    )
    if prior_connector_artifact or connector_artifact or baseline_raw is None:
        return "no_connector_review"
    fresh = _new_connector_plus_ones(raw, baseline_raw)
    if _manual_request_plus_one(raw, head, fresh):
        return "review_seen"
    return "no_connector_review"


def snapshot(repo, pr, deadline=None):
    """Return (signature_dict, raw) — one poll's worth of state. All gh calls
    share one page+deadline budget so the whole snapshot is bounded, not just
    each call."""
    budget = _Budget(MAX_SNAPSHOT_PAGES, deadline)
    pull = gh_one(f"repos/{repo}/pulls/{pr}", budget)
    sha = pull.get("head", {}).get("sha", "")
    state = pull.get("state", "")
    merged = pull.get("merged", False)

    timeline = gh_array(f"repos/{repo}/issues/{pr}/timeline", budget)
    tl = _timeline_sig(timeline)

    reactions = gh_array(f"repos/{repo}/issues/{pr}/reactions", budget)
    rx = [f"{x.get('user', {}).get('login')}:{x.get('content')}" for x in reactions]
    # Reactions can land on a review-request comment, not just the PR itself — the
    # connector's thumbs-up verdict on a manual "@codex review" comment lives
    # there. Include per-comment reactions so a reaction-only CLEAN is not missed.
    # Fail closed past the cap rather than silently truncate: a dropped comment
    # could carry the authoritative verdict, and a bounded-but-complete-looking
    # result must never hide what it skipped.
    comments = gh_array(f"repos/{repo}/issues/{pr}/comments", budget)
    if len(comments) > MAX_REACTION_COMMENTS:
        raise RuntimeError(
            f"{len(comments)} comments exceeds MAX_REACTION_COMMENTS="
            f"{MAX_REACTION_COMMENTS}; failing closed rather than skip reactions"
        )
    comment_reactions = []
    for comment in comments:
        cid = comment.get("id")
        for rxn in gh_array(f"repos/{repo}/issues/comments/{cid}/reactions", budget):
            rx.append(f"c{cid}:{rxn.get('user', {}).get('login')}:{rxn.get('content')}")
            comment_reactions.append({"comment": comment, "reaction": rxn})
    rx = sorted(rx)

    checks = {}
    if sha:
        for page in _gh_pages(f"repos/{repo}/commits/{sha}/check-runs", budget):
            for run in page.get("check_runs", []):
                # Key by name#id, not name alone: a re-run shares the name, and
                # collapsing on name would hide the new run's conclusion.
                checks[f"{run.get('name')}#{run.get('id')}"] = (
                    run.get("conclusion") or run.get("status")
                )
        st = gh_one(f"repos/{repo}/commits/{sha}/status", budget)
        checks["_combined_status"] = st.get("state")

    sig = {
        "state": state, "merged": merged, "head": sha,
        "timeline": tl, "connector_review_oids": connector_review_oids(timeline),
        "reactions": rx, "checks": checks,
    }
    return sig, {
        "timeline": timeline,
        "reactions": reactions,
        "comments": comments,
        "comment_reactions": comment_reactions,
        "captured_at": datetime.now().astimezone().isoformat(),
    }


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
    if old.get("connector_review_oids") != new.get("connector_review_oids"):
        out.append(
            "connector review OIDs: "
            f"{new.get('connector_review_oids', [])}"
        )
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


def settle_after_push(repo, pr, interval, timeout):
    """Observe a pushed head, then take one bounded tail snapshot."""
    started = time.monotonic()
    observation_deadline = started + timeout
    final_deadline = observation_deadline + FINAL_SNAPSHOT_GRACE
    base, base_raw = snapshot(repo, pr, observation_deadline)
    head = base["head"]
    pending_seen = False
    connector_artifact_seen = False
    status = connector_artifact_status(base_raw, head)
    pending_seen = status == "review_pending"
    connector_artifact_seen = any(
        _connector_event(event) for event in base_raw.get("timeline", [])
    )
    if status == "review_seen":
        print(json.dumps({
            "settle": status,
            "head": head,
            "waited_seconds": 0,
            "final_snapshot_seconds": 0.0,
            "connector_review_oids": base.get("connector_review_oids", []),
        }, indent=2))
        return 0

    while True:
        remaining = observation_deadline - time.monotonic()
        if remaining < 0:
            break
        time.sleep(min(interval, remaining))
        if time.monotonic() >= observation_deadline:
            break
        try:
            current, current_raw = snapshot(repo, pr, observation_deadline)
        except RuntimeError as error:
            print(f"WARN: {error}", file=sys.stderr)
            continue
        if current["head"] != head:
            print(
                json.dumps({
                    "settle": "head_changed",
                    "before": head,
                    "after": current["head"],
                }),
                file=sys.stderr,
            )
            return 2
        status = connector_artifact_status(
            current_raw,
            head,
            base_raw,
            prior_pending=pending_seen,
            prior_connector_artifact=connector_artifact_seen,
        )
        pending_seen = pending_seen or status == "review_pending"
        connector_artifact_seen = connector_artifact_seen or any(
            _connector_event(event) for event in current_raw.get("timeline", [])
        )
        if status == "review_seen":
            print(json.dumps({
                "settle": status,
                "head": head,
                "waited_seconds": round(time.monotonic() - started, 3),
                "final_snapshot_seconds": 0.0,
                "connector_review_oids": current.get("connector_review_oids", []),
            }, indent=2))
            return 0
    try:
        tail, tail_raw = snapshot(repo, pr, final_deadline)
    except RuntimeError as error:
        print(
            f"ERROR: no successful tail snapshot at the end of the settle window: {error}",
            file=sys.stderr,
        )
        return 2
    if tail["head"] != head:
        print(
            json.dumps({"settle": "head_changed", "before": head, "after": tail["head"]}),
            file=sys.stderr,
        )
        return 2
    status = connector_artifact_status(
        tail_raw,
        head,
        base_raw,
        prior_pending=pending_seen,
        prior_connector_artifact=connector_artifact_seen,
    )
    result = {
        "settle": status,
        "head": head,
        "waited_seconds": timeout,
        "final_snapshot_seconds": round(
            max(0.0, time.monotonic() - observation_deadline), 3
        ),
        "connector_review_oids": tail.get("connector_review_oids", []),
    }
    print(json.dumps(result, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--repo", required=True,
                    help="owner/name — required so a worker never watches a default repo")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--once", action="store_true",
                    help="print the current signature and exit (no watching)")
    ap.add_argument("--settle-after-push", type=float, metavar="SECONDS",
                    help="wait a bounded post-push window for a connector review")
    args = ap.parse_args()

    if args.once and args.settle_after_push is not None:
        ap.error("--once and --settle-after-push are mutually exclusive")
    if args.settle_after_push is not None:
        if args.settle_after_push <= 0 or args.interval <= 0:
            ap.error("--interval and --settle-after-push must both be > 0")
        try:
            return settle_after_push(
                args.repo, args.pr, args.interval, args.settle_after_push
            )
        except RuntimeError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2

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
            cur, raw = snapshot(args.repo, args.pr, deadline)
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
