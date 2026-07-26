#!/usr/bin/env python3
"""
pipeline_health.py — Can a packet to this agent actually wake it, right now?

Run this BEFORE sending, not after wondering why there was no reply. Every check
here corresponds to a way the lane has silently stopped in practice:

  lease        A lease is stamped at register time with a fixed TTL and is NEVER
               renewed by activity. A session dispatched to continuously still dies
               on the wall clock, and dispatch then refuses with `lease_expired`
               while deliver.py keeps writing packets nobody will read.
  target       When the target cannot be validated at write time, the packet is
               written with `target_session_id: null`. Exact-receive sessions refuse
               a null target as `route_ambiguous`, so that packet is permanently
               unroutable -- fixing the lease afterwards does not rescue it.
  endpoint     A codex_app binding is only wakeable while its app-server is running
               under the same home; the binding looks perfect either way.
  watcher      No watcher process means nothing polls the inbox at all.
  backlog      Unread packets that are already unroutable, which is the visible
               symptom of the two failures above.

Usage:
  python bin/pipeline_health.py --agent codex
  python bin/pipeline_health.py --all --json
  python bin/pipeline_health.py --agent codex --min-lease-seconds 900
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import subprocess

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import ROOT, agent_ids, get_agent, get_unread_messages, is_agent_disabled
from _session_autobridge import (
    SERVER_REQUEST_IGNORE,
    SESSIONS_DIR,
    JsonRpcWebSocketClient,
    _codex_app_server_token,
    discover_codex_app_server,
    load_session,
    now_utc,
    parse_iso8601,
    runtime_metadata,
    message_targets_session,
    repo_scope_matches,
    resolve_effective_action,
    resolve_exact_dispatch_pair,
    session_is_dispatchable,
)

_UNSET = object()

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


# Charged at the enumeration boundary, before any filtering: the session directory is
# untrusted input, and filtering first hides the cost of the entries that were rejected.
# A preflight that stalls or exhausts memory on a pathological directory is worse than
# one that refuses, so this fails closed rather than reporting a partial inventory --
# a partial answer here looks exactly like a complete one.
SESSION_SCAN_LIMIT = 5000
# The same rule for the other untrusted enumeration: an unbounded unread queue is read
# and parsed in full by the backlog check.
UNREAD_SCAN_LIMIT = 5000


def _sessions_for(agent_id: str) -> list[dict]:
    sessions = []
    examined = 0
    for path in sorted(SESSIONS_DIR.glob("*.json")):
        examined += 1
        if examined > SESSION_SCAN_LIMIT:
            raise RuntimeError(
                f"session directory holds more than {SESSION_SCAN_LIMIT} entries; "
                "refusing to scan further rather than report a partial inventory as "
                "complete. Prune it or raise SESSION_SCAN_LIMIT deliberately."
            )
        session = load_session(path.stem)
        if session and session.get("agent_id") == agent_id:
            sessions.append(session)
    return sessions


def _lease_check(session: dict, min_seconds: int) -> dict:
    expires = parse_iso8601(session.get("lease_expires_utc"))
    if expires is None:
        return {"check": "lease", "status": OK, "detail": "no lease expiry set"}
    remaining = int((expires - now_utc()).total_seconds())
    if remaining <= 0:
        return {
            "check": "lease",
            "status": FAIL,
            "remaining_seconds": remaining,
            "detail": (
                f"EXPIRED {-remaining}s ago at {session['lease_expires_utc']}. Dispatch "
                "refuses with lease_expired and every packet sent meanwhile is written "
                "with a null target, which stays unroutable after the lease is fixed. "
                "Re-register with --ttl-seconds before sending."
            ),
        }
    if remaining < min_seconds:
        return {
            "check": "lease",
            "status": WARN,
            "remaining_seconds": remaining,
            "detail": (
                f"expires in {remaining}s, under the {min_seconds}s margin. A long "
                "review turn can outlive it and strand the reply."
            ),
        }
    return {"check": "lease", "status": OK, "remaining_seconds": remaining,
            "detail": f"valid for {remaining}s"}


def _dispatchable_check(session: dict) -> dict:
    dispatchable, reason = session_is_dispatchable(session)
    return {
        "check": "dispatchable",
        "status": OK if dispatchable else FAIL,
        "detail": reason,
    }


def _wake_action_check(session: dict) -> dict:
    """A dispatchable session is not the same as a session that will WAKE.

    `session_is_dispatchable` checks status and lease only. The watcher then asks
    `resolve_effective_action`, which turns `mode: manual` into `manual_noop` and
    `mode: notify` into `notify_only` -- and a manual session records the packet in
    `processed_messages` without waking anything, so fixing the mode afterwards does not
    make that session pick the packet up. Reporting can_wake for those is the false green
    this tool exists to prevent.
    """
    try:
        action, reason = resolve_effective_action(session, {})
    except Exception as exc:  # a malformed session must not abort the preflight
        return {"check": "wake-action", "status": FAIL,
                "detail": f"could not resolve the wake action: {type(exc).__name__}: {exc}"}
    if action == "runtime_trigger":
        return {"check": "wake-action", "status": OK, "detail": f"{action} ({reason})"}
    return {
        "check": "wake-action",
        "status": FAIL,
        "detail": (
            f"the watcher would resolve this session to {action} ({reason}), not "
            "runtime_trigger. The packet is recorded as handled without waking the "
            "runtime, and it is not re-dispatched once the mode is fixed."
        ),
    }


def _endpoint_check(session: dict) -> dict:
    runtime = runtime_metadata(session)
    family = str(runtime.get("family", ""))
    if family != "codex_app":
        return {"check": "endpoint", "status": OK,
                "detail": f"{family or 'no family'}: no endpoint probe for this family"}
    # Dispatch resolves the endpoint with the env override honoured, so a preflight that
    # ignored it probed a healthy home-scoped sidecar while the watcher would dispatch to
    # a stale override -- healthy here, failing there.
    endpoint = discover_codex_app_server(runtime.get("home"))
    if endpoint is None:
        return {
            "check": "endpoint",
            "status": FAIL,
            "detail": (
                f"no codex app-server listening under home {runtime.get('home')!r}. "
                "The binding is intact but nothing can be woken through it."
            ),
        }
    scope = (
        " (workspace-wide LLM_COLLAB_CODEX_APP_SERVER_URL override, not home-scoped)"
        if endpoint.get("source") == "env"
        else ""
    )
    return {"check": "endpoint", "status": OK,
            "detail": f"app-server {endpoint.get('url')} (pid {endpoint.get('pid')}){scope}"}


ACTIVE_WITHIN_SECONDS = 90


def activity_shape(age_seconds: int) -> str:
    """Live-view wording only. Deliberately not a health verdict.

    A worker that has been quiet for an hour may be perfectly healthy and simply have
    nothing to do, so this never fails a check -- it answers the operator's question
    ("is it running right now?") and nothing else.
    """
    return "active" if age_seconds < ACTIVE_WITHIN_SECONDS else "idle"


def _activity_check(session: dict) -> dict:
    """When did this worker's runtime thread last do anything?

    Answers the question the operator actually has -- "is it running right now, or has
    it stopped?" -- which no amount of binding health can. A lease can be valid, a
    watcher running and an endpoint reachable while the worker has been silent for an
    hour. Read-only: `thread/list` starts nothing and steers nothing.
    """
    runtime = runtime_metadata(session)
    if str(runtime.get("family", "")) != "codex_app":
        return {"check": "activity", "status": OK, "detail": "no thread probe for this family"}
    endpoint = discover_codex_app_server(runtime.get("home"))
    if endpoint is None:
        return {"check": "activity", "status": OK, "detail": "no endpoint to probe"}
    thread_id = str(runtime.get("session_id") or "")
    stage = "connect"
    try:
        token = _codex_app_server_token(endpoint.get("token_file"))
        # SERVER_REQUEST_IGNORE, not the REFUSE default: refusing SENDS a correlated
        # error frame, and a pending server request can be resolved by whichever client
        # answers first -- so a "read-only" probe would abort work the operator started
        # in the desktop app. codex_stream.py carries the same contract for the same
        # reason. Observation must emit nothing at all.
        with JsonRpcWebSocketClient(
            str(endpoint["url"]),
            token=token,
            timeout_seconds=20,
            server_request_policy=SERVER_REQUEST_IGNORE,
        ) as client:
            stage = "initialize"
            client.request("initialize", {"clientInfo": {"name": "pipeline-health",
                                                         "title": "llm-collab",
                                                         "version": "0.1"}})
            client.notify("initialized")
            stage = "thread/list"
            listed = client.request("thread/list", {})
    except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
        # RuntimeError is what request() raises for a JSON-RPC error reply, so an
        # app-server answering "method not supported" used to crash the whole preflight
        # instead of degrading one advisory check.
        #
        # But only a thread/list failure is advisory. Real dispatch performs the same
        # initialize, so an authentication or protocol-negotiation rejection means the
        # watcher cannot wake this thread either -- degrading that to WARN reported a
        # dead lane as sendable.
        if stage != "thread/list":
            return {
                "check": "activity",
                "status": FAIL,
                "detail": (
                    f"app-server rejected {stage}: {type(exc).__name__}: {exc}. Dispatch "
                    "performs the same initialization, so nothing can be woken here."
                ),
            }
        return {"check": "activity", "status": WARN,
                "detail": f"thread probe failed: {type(exc).__name__}: {exc}"}
    rows = listed.get("data") if isinstance(listed, dict) else None
    if not isinstance(rows, list):
        # Validate before indexing: a malformed result reached .get and raised.
        return {"check": "activity", "status": WARN,
                "detail": f"thread/list returned {type(listed).__name__}, not a result object"}
    row = next(
        (r for r in rows if isinstance(r, dict) and r.get("id") == thread_id), None
    )
    if row is None:
        return {"check": "activity", "status": WARN,
                "detail": f"thread {thread_id[:8]} not listed by the app-server"}
    raw_updated = row.get("updatedAt")
    # `int(None or 0)` produced 0, which dates the thread to 1970 and still reported ok;
    # a non-numeric value raised outside the handler and aborted the whole preflight; and
    # a future timestamp produced a negative age that `activity_shape` called "active".
    # A malformed row is a probe that did not answer, not a healthy one.
    try:
        updated = int(raw_updated)
    except (TypeError, ValueError):
        # Covers missing, null, text and container values alike -- an earlier separate
        # None branch was unreachable, since int(None) lands here anyway.
        return {"check": "activity", "status": WARN,
                "detail": f"thread {thread_id[:8]} reported updatedAt={raw_updated!r}"}
    age = int(now_utc().timestamp()) - updated
    if age < 0:
        return {"check": "activity", "status": WARN, "idle_seconds": age,
                "detail": (
                    f"thread {thread_id[:8]} reports activity {-age}s in the future; "
                    "the clocks disagree and the age cannot be trusted"
                )}
    return {"check": "activity", "status": OK, "idle_seconds": age,
            "detail": f"{activity_shape(age)}: last thread activity {age}s ago"}


def _watcher_check(agent_id: str) -> dict:
    try:
        listing = subprocess.run(
            ["ps", "axo", "args="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"check": "watcher", "status": WARN,
                "detail": f"could not inspect processes: {exc}"}
    needle = f"watch_inbox.py --me {agent_id}"
    # Matching the basename alone counted a watcher polling a DIFFERENT checkout's
    # mailbox as this workspace's watcher, so the check passed while nothing read these
    # packets. The script path is what ties the process to this ROOT.
    this_workspace = str((ROOT / "bin" / "watch_inbox.py").resolve())
    mine = [
        line for line in listing.splitlines()
        if needle in line and this_workspace in line
    ]
    if mine:
        return {"check": "watcher", "status": OK,
                "detail": f"watcher running from {this_workspace}"}
    elsewhere = [line for line in listing.splitlines() if needle in line]
    if elsewhere:
        return {
            "check": "watcher",
            "status": FAIL,
            "detail": (
                f"a `{needle}` process exists but not from {this_workspace}; it polls "
                "another checkout's mailbox, so nothing reads a packet written here."
            ),
        }
    return {
        "check": "watcher",
        "status": FAIL,
        "detail": (
            f"no `{needle}` process. Nothing polls this inbox, so a packet is durable "
            "but nobody is woken and nobody reads it."
        ),
    }


def _backlog_check(agent_id: str, sessions: list[dict]) -> dict:
    """Classify the backlog with the ROUTER's predicate, and report its reasons.

    An earlier version asked whether `target_session_id` was null and whether the id
    appeared in `session_target_ids`. That is a second, weaker routability rule invented
    here: it misses repo-scope refusals, binding-generation mismatch and project/chat
    divergence entirely, and it would drift from the router the moment either changed.
    `message_targets_session` is the predicate dispatch actually applies, so it is the
    only one that can answer "will this packet ever be delivered".
    """
    unread = get_unread_messages(agent_id)
    if len(unread) > UNREAD_SCAN_LIMIT:
        raise RuntimeError(
            f"unread queue holds {len(unread)} messages, above the {UNREAD_SCAN_LIMIT} "
            "limit; refusing to classify a partial backlog as the whole one."
        )
    if not sessions:
        return {"check": "backlog", "status": OK,
                "detail": f"{len(unread)} unread; no session to route them to"}

    reasons: dict[str, int] = {}
    undeliverable: list[str] = []
    unbound_chats: set[str] = set()
    for message in unread:
        frontmatter = message.get("frontmatter", {})
        # Mirror the prefilter dispatch applies before the target predicate. Without it
        # every packet in a chat this agent has no session for is reported as broken,
        # which for a busy mailbox is most of them -- an alarm that is always on.
        candidates = [
            session
            for session in sessions
            if session.get("project_id") == frontmatter.get("project_id")
            and session.get("chat_id") == frontmatter.get("chat_id")
        ]
        if not candidates:
            unbound_chats.add(str(frontmatter.get("chat_id", "?")))
            continue
        verdicts = [message_targets_session(session, message) for session in candidates]
        if any(matched for matched, _ in verdicts):
            continue
        undeliverable.append(message["path"])
        # The least-bad reason across candidate sessions is the actionable one.
        reason = sorted({reason for _, reason in verdicts})[0]
        reasons[reason] = reasons.get(reason, 0) + 1

    detail = f"{len(unread)} unread"
    if unbound_chats:
        detail += f", {len(unbound_chats)} chat(s) with no session registered"
    if not undeliverable:
        return {"check": "backlog", "status": OK, "detail": detail}
    breakdown = ", ".join(f"{count}x {reason}" for reason, count in sorted(reasons.items()))
    return {
        "check": "backlog",
        "status": WARN,
        "undeliverable": len(undeliverable),
        "reasons": reasons,
        "sample": [Path(p).name for p in undeliverable[-3:]],
        "detail": (
            f"{detail}; {len(undeliverable)} addressed to a bound chat that no session "
            f"will accept ({breakdown}). Those never dispatch; they can only be read "
            "manually or re-sent."
        ),
    }


def _agent_enabled_check(agent_id: str) -> dict:
    """`deliver.py` refuses a disabled recipient before it resolves the chat at all.

    A disabled agent can keep an active binding and an unexpired lease, so every session
    check passed and the preflight disagreed with the very command it predicts.
    """
    try:
        agent = get_agent(agent_id)
    except Exception as exc:
        return {"check": "agent", "status": FAIL,
                "detail": f"could not read agent {agent_id!r}: {type(exc).__name__}: {exc}"}
    if is_agent_disabled(agent):
        return {
            "check": "agent",
            "status": FAIL,
            "detail": (
                f"agent {agent_id!r} is disabled. deliver.py refuses this recipient before "
                "resolving the chat, so no packet is written and nothing is woken."
            ),
        }
    return {"check": "agent", "status": OK, "detail": f"agent {agent_id!r} is enabled"}


def _repo_scope_check(session: dict, packet_repo_targets: list[str] | None) -> dict:
    """The scope test real delivery applies, which the pair resolver does not.

    `resolve_exact_dispatch_pair` validates binding identity only. `deliver.py` and the
    watcher additionally run `repo_scope_matches`, so a session scoped to one repo was
    reported wakeable for a packet naming another -- and both of those refuse it as
    `route_ambiguous`. Predicting delivery means applying delivery's whole predicate.
    """
    if packet_repo_targets is None:
        return {
            "check": "repo-scope",
            "status": OK,
            "detail": (
                "no --repo-targets given; pass the packet's scope to have it checked, "
                "since an unscoped check cannot see a route_ambiguous refusal"
            ),
        }
    matched, reason = repo_scope_matches(
        session.get("repo_targets"),
        packet_repo_targets,
        subscriber_project=session.get("project_id"),
        packet_project=session.get("project_id"),
    )
    if matched:
        return {"check": "repo-scope", "status": OK, "detail": reason}
    return {
        "check": "repo-scope",
        "status": FAIL,
        "detail": (
            f"packet scope {packet_repo_targets} against session scope "
            f"{session.get('repo_targets')}: {reason}. deliver.py and the watcher both "
            "refuse this packet."
        ),
    }


def target_report(
    project_id: str,
    chat_id: str,
    agent_id: str,
    *,
    min_lease_seconds: int,
    packet_repo_targets: list[str] | None = None,
) -> dict:
    """Can a packet for THIS project/chat/agent wake its exact binding right now?

    The agent-wide report answers a different and weaker question. It says can_wake as
    soon as ANY session for the agent is live, and a packet addressed to a different,
    expired or rebound session cannot be retargeted to that live one -- so the tool
    could return exit 0 while its own backlog line said the packet was undeliverable.
    That is precisely the false green it exists to prevent.

    Send-time questions are answered through the same exact dispatch-pair resolver the
    router uses, never by inventory.
    """
    pair, reason = resolve_exact_dispatch_pair(project_id, chat_id, agent_id)
    checks: list[dict] = [_agent_enabled_check(agent_id)]
    if pair is None:
        checks.append({
            "check": "exact-pair",
            "status": FAIL,
            "detail": (
                f"no exact dispatch pair for {project_id}/{chat_id}/{agent_id}: {reason}. "
                "Another session for this agent is not evidence that this packet can wake."
            ),
        })
        session = None
    else:
        session, _binding = pair
        checks.append({
            "check": "exact-pair",
            "status": OK,
            "session_id": session["session_id"],
            "detail": f"bound to {session['session_id']}",
        })
        for check in (
            _dispatchable_check(session),
            _lease_check(session, min_lease_seconds),
            _wake_action_check(session),
            _repo_scope_check(session, packet_repo_targets),
            _endpoint_check(session),
            _activity_check(session),
        ):
            checks.append({**check, "session_id": session["session_id"]})

    checks.append(_watcher_check(agent_id))
    # Every FAIL blocks here, endpoint and watcher included. The agent-wide report keeps
    # them advisory because it answers "is this lane configured", and a process
    # observation there is stale the instant it is taken. This report answers "may I send
    # THIS packet now", and both of those checks say in their own words that nothing will
    # read the packet or wake from it. That an observation can go stale after inspection
    # is true of every preflight check and is not a reason to call a known-unreachable
    # lane sendable.
    blocking = [c for c in checks if c["status"] == FAIL]
    status = max((c["status"] for c in checks), key=lambda s: _RANK[s])
    return {
        "agent_id": agent_id,
        "project_id": project_id,
        "chat_id": chat_id,
        "status": status,
        "can_wake": not blocking,
        "session_id": session["session_id"] if session else None,
        "checks": checks,
    }


def agent_report(agent_id: str, *, min_lease_seconds: int) -> dict:
    """Can a packet wake this agent NOW -- not: is every session it ever had healthy.

    A workspace accumulates dead sessions (probes, superseded activations, months-old
    disposables). Scoring them all together reported FAIL for a lane that was working
    perfectly, which is the kind of always-red signal people learn to ignore. One
    wakeable session is sufficient; the rest are reported as clutter to prune.
    """
    sessions = _sessions_for(agent_id)
    live: list[dict] = []
    dead: list[dict] = []
    checks: list[dict] = []

    failures: list[dict] = []
    for session in sessions:
        session_checks = [
            _dispatchable_check(session),
            _lease_check(session, min_lease_seconds),
            _wake_action_check(session),
            _endpoint_check(session),
        ]
        # Endpoint reachability is ADVISORY, as the can_wake comment below states: it is
        # a live observation that is stale the instant it is taken. Letting it decide
        # `live` made the advertised rule false and untested -- an active, leased session
        # with a momentarily unreachable app-server reported can_wake=False.
        wakeable = all(
            c["status"] != FAIL for c in session_checks if c["check"] != "endpoint"
        )
        (live if wakeable else dead).append(session)
        if wakeable:
            checks.extend({**c, "session_id": session["session_id"]} for c in session_checks)
        else:
            failures.extend(
                {**c, "session_id": session["session_id"]}
                for c in session_checks
                if c["status"] == FAIL and c["check"] != "endpoint"
            )

    if not live:
        if dead:
            # Re-running _lease_check on the first sorted session reported "valid for ..."
            # as the nearest problem whenever the real failure was status=stopped or a
            # non-triggering mode -- hiding the actionable fault and sending the worker to
            # repair a lease that was already healthy. Report the check that actually
            # failed.
            nearest = failures[0]["detail"] if failures else "no failing check recorded"
            checks.append({
                "check": "session",
                "status": FAIL,
                "failing_checks": [f["check"] for f in failures],
                "detail": (
                    f"{len(dead)} registered session(s), none wakeable. Nearest problem: "
                    f"{nearest}"
                ),
            })
        else:
            checks.append({
                "check": "session",
                "status": FAIL,
                "detail": "no registered autobridge session; packets are durable only",
            })
    elif dead:
        checks.append({
            "check": "stale-sessions",
            "status": WARN,
            "count": len(dead),
            "sample": [s["session_id"] for s in dead[:3]],
            "detail": (
                f"{len(live)} wakeable, {len(dead)} expired or unreachable. The expired "
                "ones cannot receive but do clutter every diagnosis; deactivate them."
            ),
        })

    # Only sessions that can actually receive may absolve a packet. Falling back to every
    # session let a dead one "match" a packet -- message_targets_session answers
    # addressing, not wakeability -- so a fully broken lane reported an empty backlog,
    # the one line that would have shown the packets were stuck.
    advisory = [_watcher_check(agent_id), _backlog_check(agent_id, live)]
    for session in live:
        advisory.insert(0, {**_activity_check(session), "session_id": session["session_id"]})
    checks.extend(advisory)

    status = max((c["status"] for c in checks), key=lambda s: _RANK[s])
    return {
        "agent_id": agent_id,
        "status": status,
        # Keyed to the session state ALONE, deliberately. A watcher or endpoint
        # observation is stale the instant it is taken -- the process can exit a
        # millisecond later -- so gating on it would make the verdict a coin flip under
        # exactly the conditions it is consulted. Those two are reported as advisory
        # instead. (Codex's contract ruling, 2026-07-26; an earlier version of this file
        # folded the watcher into can_wake.)
        "can_wake": bool(live),
        "wakeable_sessions": [s["session_id"] for s in live],
        "unwakeable_sessions": [s["session_id"] for s in dead],
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check whether a packet can actually wake an agent right now."
    )
    parser.add_argument("--agent", action="append", dest="agents", default=None)
    parser.add_argument("--all", action="store_true", help="Every known agent")
    # An empty value is a MISTAKE, not a wildcard: `--project "$PROJECT"` with the
    # variable unset used to leave both booleans false, drop into agent-wide inventory
    # mode, and return exit 0 on a live session for some other project -- while the caller
    # believed an exact pair had been checked. The sentinel separates "not passed" from
    # "passed empty".
    parser.add_argument(
        "--project",
        default=_UNSET,
        help="Pre-send mode: the packet's project. Requires --chat and one --agent.",
    )
    parser.add_argument(
        "--chat",
        default=_UNSET,
        help="Pre-send mode: the packet's chat. Requires --project and one --agent.",
    )
    parser.add_argument(
        "--repo-targets",
        default=None,
        help=(
            "Pre-send mode: the packet's repo_targets, comma-separated. Without it the "
            "repo-scope refusal that real delivery applies cannot be predicted."
        ),
    )
    parser.add_argument(
        "--min-lease-seconds",
        type=int,
        default=1800,
        help="Warn when a lease has less than this left (default: 1800)",
    )
    parser.add_argument("--json", dest="json_output", action="store_true")
    args = parser.parse_args()

    for name, value in (("--project", args.project), ("--chat", args.chat)):
        if value is not _UNSET and not str(value).strip():
            parser.error(
                f"{name} was passed with an empty value. An empty scope is not a wildcard; "
                "check the variable you interpolated."
            )
    given = [args.project is not _UNSET, args.chat is not _UNSET]
    if any(given) and not all(given):
        parser.error("--project and --chat are the pre-send pair; pass both or neither")
    if all(given):
        if args.all or not args.agents or len(args.agents) != 1:
            parser.error("pre-send mode needs exactly one --agent, and not --all")
        packet_repo_targets = (
            [part.strip() for part in args.repo_targets.split(",") if part.strip()]
            if args.repo_targets is not None
            else None
        )
        report = target_report(
            args.project, args.chat, args.agents[0],
            min_lease_seconds=args.min_lease_seconds,
            packet_repo_targets=packet_repo_targets,
        )
        reports = [report]
        if args.json_output:
            print(json.dumps({"status": report["status"], "agents": reports},
                             indent=2, sort_keys=True))
        else:
            marks = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL"}
            print(f"\n{marks[report['status']]} {args.project}/{args.chat}"
                  f" -> {report['agent_id']}  (can_wake={report['can_wake']})")
            for check in report["checks"]:
                where = f" [{check['session_id']}]" if check.get("session_id") else ""
                print(f"  {marks[check['status']]} {check['check']}{where}: {check['detail']}")
            print()
        return 0 if report["can_wake"] else 2

    if args.all:
        targets = list(agent_ids())
    elif args.agents:
        targets = args.agents
    else:
        parser.error("pass --agent <id> (repeatable) or --all")

    reports = [
        agent_report(agent, min_lease_seconds=args.min_lease_seconds)
        for agent in targets
    ]
    worst = max((r["status"] for r in reports), key=lambda s: _RANK[s])

    if args.json_output:
        print(json.dumps({"status": worst, "agents": reports}, indent=2, sort_keys=True))
    else:
        marks = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL"}
        for report in reports:
            print(f"\n{marks[report['status']]} {report['agent_id']}"
                  f"  (can_wake={report['can_wake']})")
            for check in report["checks"]:
                where = f" [{check['session_id']}]" if "session_id" in check else ""
                print(f"  {marks[check['status']]} {check['check']}{where}: {check['detail']}")
        print()

    # Exit status answers the preflight question only: can every requested agent be
    # woken? Warnings are printed but never block, because a gate that fails on
    # advisory noise is a gate people wrap in `|| true` and stop reading.
    return 0 if all(report["can_wake"] for report in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
