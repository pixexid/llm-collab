#!/usr/bin/env python3
"""
pipeline_health.py — What is observably true about this agent's delivery lane?

**Observations, not a verdict.** Nothing here is permission to send and nothing here
withholds it. The mailbox is durable-first: `deliver.py` writes the packet whether or
not activation is available, and its own result plus the watcher events that follow are
the authority on what happened. An aggregate green here could only ever be a second
implementation of delivery and dispatch — three review rounds each found another
predicate it was missing, which is the argument against having it at all. Exit status
reports whether the observation could be made, never what it found.

Every check corresponds to a way the lane has silently stopped in practice:

  lease        A lease is stamped at register time with a fixed TTL and is NEVER
               renewed by activity. A session dispatched to continuously still dies
               on the wall clock, and dispatch then refuses with `lease_expired`
               while deliver.py keeps writing packets nobody will read.
  target       When the target cannot be validated at write time, the packet is
               written with `target_session_id: null`. Exact-receive sessions refuse
               a null target as `route_ambiguous`, so that packet is permanently
               unroutable -- fixing the lease afterwards does not rescue it.
  endpoint     A codex_app binding can only be woken while its app-server is running
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
import os
import subprocess

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import ROOT, agent_ids, get_agent, is_agent_disabled, load_agent_inbox, parse_frontmatter
from _session_autobridge import (
    BindingUnreadable,
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


def _bounded_session_paths() -> list[Path]:
    """Every directory entry charged as it is seen, before any filtering or sorting.

    `sorted(glob("*.json"))` materialises the whole matching list before a loop can
    charge anything, and the suffix filter drops entries before they are counted -- so
    millions of non-JSON names escaped the budget entirely and millions of JSON ones
    exhausted memory before the check was reached. `scandir` yields incrementally, the
    charge happens per entry, and the sort runs on an already-bounded list.
    """
    if not SESSIONS_DIR.exists():
        return []
    paths: list[Path] = []
    examined = 0
    with os.scandir(SESSIONS_DIR) as entries:
        for entry in entries:
            examined += 1
            if examined > SESSION_SCAN_LIMIT:
                raise RuntimeError(
                    f"session directory holds more than {SESSION_SCAN_LIMIT} entries; "
                    "refusing to scan further rather than report a partial inventory as "
                    "complete. Prune it or raise SESSION_SCAN_LIMIT deliberately."
                )
            if entry.name.endswith(".json"):
                paths.append(Path(entry.path))
    return sorted(paths)


def _bounded_sessions() -> list[dict]:
    """One bounded snapshot, shared by the inventory and the exact resolver."""
    sessions: list[dict] = []
    for path in _bounded_session_paths():
        try:
            session = load_session(path.stem)
        except (FileNotFoundError, ValueError, OSError):
            continue
        if session:
            sessions.append(session)
    return sessions


def _sessions_for(agent_id: str) -> list[dict]:
    return [s for s in _bounded_sessions() if s.get("agent_id") == agent_id]


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
    make that session pick the packet up, so the resolved action is worth reporting as its
    own observation rather than being implied by status and lease.
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


# Mirrors execute_codex_app_server_trigger exactly. A probe that negotiates differently
# from the dispatcher is observing a different connection than the one that matters.
CODEX_APP_SERVER_INITIALIZE_PARAMS = {
    "protocolVersion": "2024-11-05",
    "clientInfo": {"name": "llm-collab-session-autobridge", "version": "0.0.0"},
    "capabilities": {"experimentalApi": True},
}

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
            # The dispatcher's payload, not a second protocol shape. A weaker probe can be
            # accepted by a server that rejects the real handshake, or rejected by one that
            # would have served it -- and the test that claimed these matched only varied
            # the method's return value and never asserted its parameters.
            client.request("initialize", CODEX_APP_SERVER_INITIALIZE_PARAMS)
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
    # Two ways to get this wrong, and the previous two versions each picked one. Matching
    # the basename alone counted a watcher polling a DIFFERENT checkout's mailbox as ours.
    # Requiring the resolved absolute path rejected the DOCUMENTED invocation, `python
    # bin/watch_inbox.py --me <agent>`, because ps keeps the relative argument. So a line
    # is ours if its script argument resolves into this ROOT -- absolute or relative --
    # and foreign only if it names some other absolute path.
    this_script = (ROOT / "bin" / "watch_inbox.py").resolve()
    # Three answers, not two, because `ps axo args=` does not carry the process cwd. An
    # absolute argument attributes the process exactly. A relative one -- which the
    # DOCUMENTED `python bin/watch_inbox.py --me <agent>` form produces -- cannot be
    # attributed at all: resolving it against this ROOT assumes it was launched from here,
    # which invents the fact. Reporting that honestly beats guessing either way; the
    # earlier versions of this check made both mistakes in turn, first counting another
    # checkout's watcher as ours and then rejecting our own.
    mine: list[str] = []
    foreign: list[str] = []
    unattributable: list[str] = []
    for line in listing.splitlines():
        if needle not in line:
            continue
        argument = next(
            (part for part in line.split() if part.endswith("watch_inbox.py")), ""
        )
        if not argument:
            continue
        if not Path(argument).is_absolute():
            unattributable.append(argument)
        elif Path(argument) == this_script:
            mine.append(line)
        else:
            foreign.append(argument)
    if mine:
        return {"check": "watcher", "status": OK,
                "detail": f"watcher running for {this_script}"}
    if unattributable:
        return {
            "check": "watcher",
            "status": WARN,
            "detail": (
                f"a `{needle}` process names its script relatively ({unattributable[0]}), "
                "so which checkout it polls cannot be determined from the process list. "
                "Launch it with an absolute path to make this observable."
            ),
        }
    if foreign:
        return {
            "check": "watcher",
            "status": FAIL,
            "detail": (
                f"a `{needle}` process exists but its script is {foreign[0]}, not "
                f"{this_script}; it polls another checkout's mailbox, so nothing reads a "
                "packet written here."
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


def _backlog_check(
    agent_id: str, sessions: list[dict], *, all_sessions: list[dict] | None = None
) -> dict:
    """Classify the backlog with the ROUTER's predicate, and report its reasons.

    An earlier version asked whether `target_session_id` was null and whether the id
    appeared in `session_target_ids`. That is a second, weaker routability rule invented
    here: it misses repo-scope refusals, binding-generation mismatch and project/chat
    divergence entirely, and it would drift from the router the moment either changed.
    `message_targets_session` is the predicate dispatch actually applies, so it is the
    only one that can answer "will this packet ever be delivered".
    """
    unread = _bounded_unread(agent_id)
    known = all_sessions if all_sessions is not None else sessions
    if not known:
        return {"check": "backlog", "status": OK,
                "detail": f"{len(unread)} unread; no session registered to route them to"}
    if not sessions:
        # Nothing dispatchable, so nothing can absolve a packet -- but the packets that
        # could never route to ANY registered session are permanent, and reporting them
        # only once a session recovers hides them exactly while the lane is down.
        stranded = [
            message["path"]
            for message in unread
            if not any(
                message_targets_session(candidate, message)[0] for candidate in known
            )
        ]
        detail = f"{len(unread)} unread; no dispatchable session to route them to"
        if not stranded:
            return {"check": "backlog", "status": OK, "detail": detail}
        return {
            "check": "backlog",
            "status": WARN,
            "undeliverable": len(stranded),
            "sample": [Path(path).name for path in stranded[-3:]],
            "detail": (
                f"{detail}; {len(stranded)} of them no REGISTERED session would accept "
                "either, so those stay unroutable after the lane recovers"
            ),
        }

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
    check passed while the very command whose refusals this describes would refuse.
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
    reported clean for a packet naming another -- and both of those refuse it as
    `route_ambiguous`. Predicting delivery means applying delivery's whole predicate.
    """
    # deliver.py represents an omitted scope as `repo_targets: []` and still runs the
    # predicate, so reporting OK for a check that was not run was the false-green shape --
    # a malformed stored subscriber scope is refused there and passed here. The omission
    # is treated as the empty list delivery would use.
    matched, reason = repo_scope_matches(
        session.get("repo_targets"),
        packet_repo_targets if packet_repo_targets is not None else [],
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


def _bounded_unread(agent_id: str) -> list[dict]:
    """Charge inbox pointers before reading or parsing a single packet.

    `get_unread_messages` reads and parses every existing packet before it returns, so a
    length check on its result detected the excess only after all the work was done --
    and the test that "proved" the bound handed it an already-materialised list, which
    asserted that a check exists rather than that the work is bounded.
    """
    pointers = load_agent_inbox(agent_id).get("unread", [])
    if len(pointers) > UNREAD_SCAN_LIMIT:
        raise RuntimeError(
            f"unread queue holds {len(pointers)} pointers, above the {UNREAD_SCAN_LIMIT} "
            "limit; refusing to classify a partial backlog as the whole one."
        )
    messages: list[dict] = []
    for rel_path in pointers:
        path = ROOT / rel_path
        if not path.exists():
            continue
        frontmatter, body = parse_frontmatter(path.read_text())
        messages.append({"path": rel_path, "frontmatter": frontmatter, "body": body})
    return messages


def target_report(
    project_id: str,
    chat_id: str,
    agent_id: str,
    *,
    min_lease_seconds: int,
    packet_repo_targets: list[str] | None = None,
) -> dict:
    """Can a packet for THIS project/chat/agent wake its exact binding right now?

    The agent-wide report describes an agent's whole inventory, which cannot answer this:
    a packet addressed to a different, expired or rebound session is not retargeted to a
    live one. This report observes the exact binding the router would use, resolved
    through the router's own resolver rather than re-derived.

    It reports observations and no verdict. Nothing here is a permission to send: the
    mailbox is durable-first, so `deliver.py` writes the packet either way and its result,
    with the watcher events that follow, is the authority on the outcome.
    """
    checks: list[dict] = [_agent_enabled_check(agent_id)]
    try:
        # The resolver falls back to an UNBOUNDED iter_sessions() when given no snapshot,
        # so the budget was absent from the one mode workers are told to trust.
        pair, reason = resolve_exact_dispatch_pair(
            project_id, chat_id, agent_id, _bounded_sessions()
        )
    except BindingUnreadable as exc:
        # deliver.py treats this as a specific runtime-dispatch blocker. Letting it escape
        # meant automation got a traceback and no JSON at all -- worse than a false green,
        # because the promised diagnostic shape simply is not there.
        pair, reason = None, f"binding unreadable: {exc}"
    except RuntimeError as exc:
        pair, reason = None, str(exc)
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
    status = max((c["status"] for c in checks), key=lambda s: _RANK[s])
    return {
        "agent_id": agent_id,
        "project_id": project_id,
        "chat_id": chat_id,
        "status": status,
        "session_id": session["session_id"] if session else None,
        "checks": checks,
    }


def agent_report(agent_id: str, *, min_lease_seconds: int) -> dict:
    """Can a packet wake this agent NOW -- not: is every session it ever had healthy.

    A workspace accumulates dead sessions (probes, superseded activations, months-old
    disposables). Scoring them all together reported FAIL for a lane that was working
    perfectly, which is the kind of always-red signal people learn to ignore. One
    dispatchable session is enough to describe the lane as usable; the rest are reported
    as clutter to prune.
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
        # Endpoint reachability is a live observation that is stale the instant it is
        # taken, so it does not decide whether a session counts as dispatchable -- status,
        # lease and resolved wake action do. It is reported alongside them.
        dispatchable = all(
            c["status"] != FAIL for c in session_checks if c["check"] != "endpoint"
        )
        (live if dispatchable else dead).append(session)
        if dispatchable:
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
                    f"{len(dead)} registered session(s), none dispatchable. Nearest problem: "
                    f"{nearest}"
                ),
            })
        else:
            checks.append({
                "check": "session",
                "status": FAIL,
                "detail": (
                "no registered autobridge session; a packet here is durable and will not "
                "wake anything until one is registered"
            ),
            })
    elif dead:
        checks.append({
            "check": "stale-sessions",
            "status": WARN,
            "count": len(dead),
            "sample": [s["session_id"] for s in dead[:3]],
            "detail": (
                f"{len(live)} dispatchable, {len(dead)} expired or unreachable. The expired "
                "ones cannot receive but do clutter every diagnosis; deactivate them."
            ),
        })

    # Only sessions that can receive may absolve a packet -- message_targets_session
    # answers addressing, not dispatchability, so a dead one could "match" a packet and a
    # broken lane reported an empty backlog. But passing an EMPTY list made the stranded
    # residue invisible precisely while the lane was down, which is when it matters most.
    # So the classification runs against every registered session and the absolution runs
    # only against the dispatchable ones.
    advisory = [
        _watcher_check(agent_id),
        _backlog_check(agent_id, live, all_sessions=sessions),
    ]
    for session in live:
        advisory.insert(0, {**_activity_check(session), "session_id": session["session_id"]})
    checks.extend(advisory)

    status = max((c["status"] for c in checks), key=lambda s: _RANK[s])
    return {
        "agent_id": agent_id,
        "status": status,
        "dispatchable_sessions": [s["session_id"] for s in live],
        "undispatchable_sessions": [s["session_id"] for s in dead],
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report what is observably true about an agent's delivery lane."
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
        help="Exact-binding mode: the packet's project. Requires --chat and one --agent.",
    )
    parser.add_argument(
        "--chat",
        default=_UNSET,
        help="Exact-binding mode: the packet's chat. Requires --project and one --agent.",
    )
    parser.add_argument(
        "--repo-targets",
        default=None,
        help=(
            "Exact-binding mode: the packet's repo_targets, comma-separated. Omitted is "
            "observed as the empty list delivery would use."
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
            parser.error("exact-binding mode needs exactly one --agent, and not --all")
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
                  f" -> {report['agent_id']}")
            for check in report["checks"]:
                where = f" [{check['session_id']}]" if check.get("session_id") else ""
                print(f"  {marks[check['status']]} {check['check']}{where}: {check['detail']}")
            print()
        # Observations only. A lane that looks unhealthy is a fact reported, not a
        # failure of this command: the mailbox contract is durable-first, so deliver.py
        # writes the packet regardless and its own result plus the watcher events are the
        # authority on what happened. An aggregate green here could only ever be a second
        # implementation of delivery, and every round of review found another predicate it
        # was missing. (Codex's scope ruling, 2026-07-26.)
        return 0

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
            print(f"\n{marks[report['status']]} {report['agent_id']}")
            for check in report["checks"]:
                where = f" [{check['session_id']}]" if "session_id" in check else ""
                print(f"  {marks[check['status']]} {check['check']}{where}: {check['detail']}")
        print()

    # Same rule for inventory mode: nonzero is for an invocation this command could not
    # carry out, never for what it observed.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
