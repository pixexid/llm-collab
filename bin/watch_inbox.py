#!/usr/bin/env python3
"""
watch_inbox.py — Background inbox poller. Run via PM2 (see pm2_watchers.py).

Polls agents/{id}/inbox.json for new unread messages and optionally
sends a desktop notification (macOS, Linux notify-send, or no-op).

Usage:
  python bin/watch_inbox.py --me orchestrator
  python bin/watch_inbox.py --me orchestrator --poll-seconds 30 --notify
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python
from current_runtime import require_current_runtime

require_python()

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import (
    ROOT,
    agent_ids,
    agent_dir,
    agent_inbox_path,
    config_get,
    get_unread_messages,
    load_agent_inbox,
    mark_messages_read,
    parse_frontmatter,
)
from _session_autobridge import (
    CanonicalBindingNativeMismatch,
    UnreadableFile,
    active_read_budget,
    dispatch_session,
    read_regular_file_bounded,
    iter_sessions,
    load_session,
    repo_scope_matches,
    resolve_active_canonical_binding,
    resolve_session_receive_binding,
    runtime_delivery_accepted,
    runtime_metadata,
)
from inbox import (
    MAX_EXACT_SESSION_BYTES,
    ExactReadBudget,
    exact_read_messages,
    exact_read_session,
)


def parse_args():
    p = argparse.ArgumentParser(description="Background inbox watcher.")
    p.add_argument("--me", required=True, help="Agent ID to watch for")
    p.add_argument("--project", default=None, help="Filter by exact project_id")
    p.add_argument("--chat", default=None, help="Filter by exact chat_id")
    p.add_argument(
        "--session",
        default=None,
        help="Watch one exact llm-collab session; requires --project and --chat",
    )
    p.add_argument("--poll-seconds", type=int, default=None, help="Poll interval (default: from config)")
    p.add_argument("--max-polls", type=int, default=0, help="Stop after N polls; 0 = forever")
    p.add_argument("--notify", action="store_true", help="Send desktop notification on new messages")
    p.add_argument("--no-autobridge", action="store_true", help="Disable automatic session autobridge dispatch on new unread messages")
    p.add_argument("--skip-existing", action="store_true", help="Treat current unread as already seen")
    p.add_argument(
        "--repo-target",
        action="append",
        default=None,
        help="Explicit repository subscription; repeat for multiple repositories",
    )
    p.add_argument("--json", dest="json_output", action="store_true", help="Emit JSON lines")
    args = p.parse_args()
    if args.repo_target is not None and args.project is None:
        p.error("--repo-target requires --project <id>")
    if args.chat is not None and args.session is None:
        p.error("--chat requires --session")
    if args.session is not None:
        if not args.session.strip() or args.session != args.session.strip():
            p.error("--session requires a non-empty session id")
        if not args.project or not args.chat:
            p.error("--session requires --project and --chat")
        args.packet = None
    return args


def send_notification(title: str, body: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        elif system == "Linux":
            subprocess.run(["notify-send", title, body], check=False, timeout=5)
        # Windows / other: no-op
    except Exception:
        pass


def emit(msg: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(msg), flush=True)
    else:
        ts = msg.get("ts", "")
        event = msg.get("event", "")
        detail = msg.get("detail", "")
        print(f"[{ts}] {event}: {detail}", flush=True)


def utc_now_str() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class ExactWatcherAuthorityError(RuntimeError):
    pass


def session_has_exact_canonical_binding(session: dict) -> bool:
    """Run the receive binding gate without mutating the session file.

    Existing file bindings retain the watcher-local exact check. Otherwise
    ``dispatch_session`` derives an exact native-session ledger binding in
    memory when one exists, while target matching rejects targeted packets when
    it does not.
    """
    if not session.get("binding_id"):
        eligible, _binding = resolve_session_receive_binding(session)
        return eligible
    runtime = runtime_metadata(session)
    runtime_session_id = runtime.get("session_id")
    project_id = session.get("project_id")
    chat_id = session.get("chat_id")
    agent_id = session.get("agent_id")
    if not (runtime_session_id and project_id and chat_id and agent_id):
        return False
    try:
        canonical = resolve_active_canonical_binding(
            str(project_id), str(chat_id), str(agent_id), str(runtime_session_id)
        )
    except CanonicalBindingNativeMismatch:
        return False
    if canonical is None:
        return False
    return (
        canonical.get("binding_id") == session.get("binding_id")
        and canonical.get("binding_generation") == session.get("binding_generation")
    )


def exact_session_messages(args) -> list[dict]:
    budget = ExactReadBudget(MAX_EXACT_SESSION_BYTES)
    with active_read_budget(budget):
        try:
            session = exact_read_session(args, budget)
        except Exception as error:
            raise ExactWatcherAuthorityError(str(error)) from error
        messages, refusals = exact_read_messages(args, session, budget)
        fatal_refusals = [
            refusal for refusal in refusals if not refusal.get("repo_scope_only")
        ]
        if fatal_refusals:
            raise ExactWatcherAuthorityError(
                "exact_session_repo_scope_refused: "
                f"{json.dumps(fatal_refusals, sort_keys=True)}"
            )
    return messages


def autobridge_session_ids(agent_id: str, project_id: str | None = None) -> list[str]:
    session_ids: list[str] = []
    for session in iter_sessions(agent_id=agent_id):
        if project_id is not None and session.get("project_id") != project_id:
            continue
        if session.get("session_id"):
            session_ids.append(str(session["session_id"]))
    return session_ids


# GH-539: a repo-scope refusal used to be emitted but never recorded, so the same
# stale message was re-decided and re-logged on every poll — refusal work was
# O(unread) per poll forever (2239 unread produced 9523 identical log lines).
# Progress is watcher-owned state: it records that a DECISION was made. It never
# marks a message read and never touches the durable inbox, so this is not backlog
# cleanup by another name.
def refusal_progress_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "watcher-refusal-progress.json"


MAX_REFUSAL_PROGRESS_BYTES = 1 << 20  # 1 MiB, mirrors the registry read budget


def load_refusal_progress(agent_id: str) -> dict:
    path = refusal_progress_path(agent_id)
    try:
        # stat-then-read is two different objects and a growth race, and a plain
        # open() on a writer-less FIFO blocks forever BEFORE any cap applies.
        # read_regular_file_bounded does the non-blocking open, fstats the SAME
        # descriptor, requires a regular file, and caps the read loop.
        raw = read_regular_file_bounded(path, MAX_REFUSAL_PROGRESS_BYTES)
    except (FileNotFoundError, UnreadableFile, OSError):
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    refused = data.get("refused")
    if not isinstance(refused, dict):
        # Valid JSON of the wrong SHAPE (e.g. {"refused": []}) must degrade the
        # same way as corrupt JSON: progress is an optimisation, never a gate,
        # and a list here would blow up progress.get() in the watcher loop.
        return {}
    clean: dict = {}
    for key, value in refused.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, dict) and isinstance(value.get("fp"), str):
            # Every field terminal_refusal_paths feeds back into
            # refusal_fingerprint must survive the round trip, or a restart
            # recomputes a DIFFERENT fingerprint and re-evaluates the stale
            # refusal — which would defeat persistence entirely.
            reason = value.get("reason")
            packet_repo_targets = value.get("packet_repo_targets")
            packet_project = value.get("packet_project")
            clean[key] = {
                "fp": value["fp"],
                "mtime": value.get("mtime") if isinstance(value.get("mtime"), (int, float)) else None,
                "reason": reason if isinstance(reason, str) else "",
                "packet_repo_targets": packet_repo_targets
                if isinstance(packet_repo_targets, list) or packet_repo_targets is None
                else None,
                "packet_project": packet_project
                if isinstance(packet_project, str) or packet_project is None
                else None,
                "session_id": value.get("session_id")
                if isinstance(value.get("session_id"), str)
                else None,
                "path": value.get("path") if isinstance(value.get("path"), str) else None,
                "session_scope": value.get("session_scope"),
                "session_repo_targets": value.get("session_repo_targets")
                if isinstance(value.get("session_repo_targets"), list)
                or value.get("session_repo_targets") is None
                else None,
            }
        elif isinstance(value, str):  # pre-GH-539 shape: fingerprint only
            clean[key] = {
                "fp": value,
                "mtime": None,
                "reason": "",
                "packet_repo_targets": None,
                "packet_project": None,
                "session_id": None,
                "path": None,
                "session_repo_targets": None,
                "session_scope": None,
            }
    return clean


def save_refusal_progress(agent_id: str, refused: dict) -> None:
    """Atomic single-writer update; a partial write must never be readable."""
    path = refusal_progress_path(agent_id)
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"version": 1, "refused": refused}, indent=2))
        os.replace(tmp, path)
    except OSError:
        # Progress state is an optimisation, never a gate: if it cannot be
        # persisted the watcher still refuses correctly, it just re-logs.
        try:
            tmp.unlink()
        except OSError:
            pass


def _packet_mtime(message_path: str) -> float | None:
    try:
        return (ROOT / message_path).stat().st_mtime
    except OSError:
        return None


def progress_key(session_id: str | None, path: str) -> str:
    """Progress is per (deciding session, path). Keying on path alone let two
    sessions refusing the SAME packet in one poll overwrite each other, so the
    loser repeated its refusal on every later poll."""
    return f"{session_id or ''}\u0000{path}"


def terminal_refusal_paths(progress: dict, repo_targets, project_id, session_id: str | None = None, session_scope=None) -> set[str]:
    """Paths whose repo-scope refusal is already terminal under the CURRENT
    subscriber decision AND whose packet file is unchanged. Either side moving
    re-opens eligibility (AC4): a changed subscriber decision changes the
    fingerprint, a rerouted packet changes its mtime."""
    skip: set[str] = set()
    for key, entry in progress.items():
        if entry.get("session_id") != session_id:
            continue  # another session's decision never satisfies this one
        if entry.get("session_scope") != _stable_targets(session_scope):
            # The SAME session was re-registered with a different stored scope:
            # the old decision no longer describes what would happen now.
            continue
        if entry.get("session_repo_targets") != repo_targets:
            # The session was re-registered with a corrected scope: the stored
            # decision was made under the OLD scope and must be re-evaluated.
            continue
        path = entry.get("path") or key.split("\u0000", 1)[-1]
        expected = refusal_fingerprint(
            entry.get("reason", ""),
            repo_targets,
            entry.get("packet_repo_targets"),
            project_id,
            entry.get("packet_project"),
            # The ASKING session, not the stored one: a decision made by another
            # session must not satisfy this session's check (P1).
            session_id,
            # ...and the CURRENTLY loaded session scope, which
            # _session_repo_scope_matches also consults.
            session_scope,
        )
        if entry.get("fp") != expected:
            continue
        if entry.get("mtime") != _packet_mtime(path):
            continue
        skip.add(path)
    return skip


def _stable_targets(value):
    """Order-insensitive, type-safe. A malformed packet can carry a mixed list
    (e.g. [1, "app"]); sorting that raises TypeError, which would escape the
    per-session handler and stall the whole poll on one bad packet."""
    if value is None:
        return None
    if isinstance(value, list):
        return sorted(repr(item) for item in value)
    return repr(value)


def refusal_fingerprint(reason: str, repo_targets, packet_repo_targets, subscriber_project, packet_project, session_id: str | None = None, session_scope=None) -> str:
    """Bind terminality to the ROUTING DECISION, not just the path, so corrected
    routing re-opens eligibility instead of suppressing the message forever."""
    payload = json.dumps(
        {
            "reason": reason,
            "repo_targets": _stable_targets(repo_targets),
            "packet_repo_targets": _stable_targets(packet_repo_targets),
            "subscriber_project": subscriber_project,
            "packet_project": packet_project,
            # P1: a refusal is a decision made by ONE session's repo scope. Without
            # this, a packet refused by the `app` session would be skipped before
            # the `docs` session ever evaluated it, stranding the message.
            "session_id": session_id,
            # _session_repo_scope_matches consults BOTH the loaded session scope
            # and the invocation scope, so a decision is only identical when both
            # are. Omitting the session scope let a re-registered session reuse a
            # stale refusal while the invocation scope stayed put.
            "session_scope": _stable_targets(
                session_scope
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def dispatch_autobridge(
    agent_id: str,
    json_output: bool,
    *,
    project_id: str | None = None,
    repo_targets: list[str] | None = None,
    refusal_progress: dict | None = None,
    refusal_stats: dict | None = None,
) -> list[str]:
    consumed_paths: list[str] = []
    progress = refusal_progress if refusal_progress is not None else {}
    stats = refusal_stats if refusal_stats is not None else {}

    def record_refusal(path: str, reason: str, packet_repo_targets=None, packet_project=None, session_scope=None, packet_mtime=None) -> bool:
        """True when this refusal is NEW and should be logged. A repeat of the same
        routing decision is counted for the aggregate summary and not re-logged."""
        fingerprint = refusal_fingerprint(
            reason, repo_targets, packet_repo_targets, project_id, packet_project,
            session_id,
            session_scope,
        )
        key = progress_key(session_id, path)
        existing = progress.get(key) or {}
        # Use the mtime observed WITH the body that produced this decision. A
        # fresh stat here would certify a version we never examined.
        observed = packet_mtime if packet_mtime is not None else _packet_mtime(path)
        existing = progress.get(key) or {}
        if existing.get("fp") == fingerprint and existing.get("mtime") == observed:
            stats[reason] = stats.get(reason, 0) + 1
            return False
        progress[key] = {
            "path": path,
            "session_repo_targets": repo_targets,
            "session_scope": _stable_targets(session_scope),
            "fp": fingerprint,
            "mtime": observed,
            "reason": reason,
            "packet_repo_targets": packet_repo_targets,
            "packet_project": packet_project,
            "session_id": session_id,
        }
        stats["_new"] = stats.get("_new", 0) + 1
        return True

    for session_id in autobridge_session_ids(agent_id, project_id):
        # Canonical binding gate (#95): resolve the session's exact binding from
        # the ledger store BEFORE dispatch. A stale or foreign session (its
        # binding_id/generation differ from the canonical active one) never
        # reaches packet match, materialization, claim, runtime write, or
        # mark_messages_read. No active binding -> fail closed for that session.
        try:
            session = load_session(session_id)
        except Exception as error:
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_dispatch_error",
                    "detail": session_id,
                    "agent": agent_id,
                    "session_id": session_id,
                    "reason": f"{type(error).__name__}: {error}",
                },
                json_output,
            )
            continue
        if not session_has_exact_canonical_binding(session):
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_binding_refused",
                    "detail": session_id,
                    "agent": agent_id,
                    "session_id": session_id,
                    "reason": "stale_or_foreign_canonical_binding",
                },
                json_output,
            )
            continue
        try:
            try:
                session_scope = (load_session(session_id) or {}).get("repo_targets")
            except Exception:
                session_scope = None
            result = dispatch_session(
                session_id,
                project_id=project_id,
                repo_targets=repo_targets,
                skip_paths=terminal_refusal_paths(
                    progress, repo_targets, project_id, session_id, session_scope
                ),
            )
        except Exception as error:
            # Isolate per session: one session's failure (e.g. the save_session
            # resurrection guard racing a deactivation, now that #378 stops sessions on
            # every Pi lifecycle event) must not abort the rest of the cycle. Surface it
            # and move on. The watcher top loop already isolates whole cycles; this
            # isolates siblings within a cycle.
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_dispatch_error",
                    "detail": session_id,
                    "agent": agent_id,
                    "session_id": session_id,
                    "reason": f"{type(error).__name__}: {error}",
                },
                json_output,
            )
            continue
        for refusal in result.get("repo_scope_refused", []):
            if not record_refusal(
                refusal["path"],
                refusal["reason"],
                packet_repo_targets=refusal.get("packet_repo_targets"),
                packet_project=refusal.get("packet_project"),
                session_scope=session_scope,
                packet_mtime=refusal.get("packet_mtime"),
            ):
                continue
            emit(
                {
                    "ts": utc_now_str(),
                    "event": "autobridge_repo_scope_refused",
                    "detail": refusal["path"],
                    "agent": agent_id,
                    "session_id": session_id,
                    "message_path": refusal["path"],
                    "reason": refusal["reason"],
                },
                json_output,
            )
        if not result.get("actions"):
            continue

        emit(
            {
                "ts": utc_now_str(),
                "event": "autobridge_dispatch",
                "detail": session_id,
                "agent": agent_id,
                "session_id": session_id,
                "matched_messages": result.get("matched_messages", 0),
            },
            json_output,
        )

        for action in result["actions"]:
            runtime_result = action.get("runtime_result") or {}
            runtime_ok = runtime_result.get("returncode") == 0
            if (
                action.get("effective_action") == "runtime_trigger"
                and runtime_delivery_accepted(runtime_result)
            ):
                # Stored session scope was already checked by dispatch_session;
                # an explicit watcher scope is rechecked at the read boundary.
                effective_repo_targets = repo_targets
                message_path = ROOT / action["message_path"]
                frontmatter: dict = {}
                observed_mtime = _packet_mtime(action["message_path"])
                if effective_repo_targets is None:
                    repo_match, repo_reason = True, "unscoped"
                else:
                    try:
                        frontmatter, _ = parse_frontmatter(message_path.read_text())
                        repo_match, repo_reason = repo_scope_matches(
                            effective_repo_targets,
                            frontmatter.get("repo_targets"),
                            subscriber_project=project_id,
                            packet_project=frontmatter.get("project_id"),
                        )
                    except Exception:
                        repo_match, repo_reason = False, "route_ambiguous"
                if not repo_match:
                    # Fail-closed is unconditional: the message is ALWAYS refused
                    # here. Only whether we log it again is deduped.
                    if record_refusal(
                        action["message_path"],
                        repo_reason,
                        packet_repo_targets=frontmatter.get("repo_targets"),
                        packet_project=frontmatter.get("project_id"),
                        session_scope=session_scope,
                        packet_mtime=observed_mtime,
                    ):
                        emit(
                            {
                                "ts": utc_now_str(),
                                "event": "autobridge_repo_scope_refused",
                                "detail": action["message_path"],
                                "agent": agent_id,
                                "session_id": session_id,
                                "message_path": action["message_path"],
                                "reason": repo_reason,
                            },
                            json_output,
                        )
                    continue
                consumed_paths.append(action["message_path"])
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_consumed",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                    },
                    json_output,
                )
            elif (
                action.get("effective_action") == "runtime_trigger"
                and runtime_ok
                and not runtime_result.get("skipped")
            ):
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_wake_signaled",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                    },
                    json_output,
                )
            elif action.get("effective_action") == "runtime_trigger":
                emit(
                    {
                        "ts": utc_now_str(),
                        "event": "autobridge_failed",
                        "detail": action["message_path"],
                        "agent": agent_id,
                        "session_id": session_id,
                        "message_path": action["message_path"],
                        "returncode": runtime_result.get("returncode"),
                    },
                    json_output,
                )
        if consumed_paths:
            mark_messages_read(agent_id, sorted(set(consumed_paths)))

    return consumed_paths


def main():
    # GH-503: a watcher that dispatches from a stale runtime keeps re-triggering
    # stale delivery code — refuse to start from a stale tree (fails closed).
    require_current_runtime("watch")
    args = parse_args()

    if not args.session:
        known = agent_ids()
        if args.me not in known:
            print(f"[error] Unknown agent: {args.me!r}", file=sys.stderr)
            sys.exit(1)

    poll_interval = args.poll_seconds or config_get("poll_interval_seconds", 15)
    inbox_path = agent_inbox_path(args.me)

    seen_paths: set[str] = set()
    refusal_progress = load_refusal_progress(args.me)

    if args.skip_existing:
        if args.session:
            while True:
                try:
                    seen_paths = {
                        message["path"] for message in exact_session_messages(args)
                    }
                    break
                except Exception as error:
                    emit(
                        {
                            "ts": utc_now_str(),
                            "event": "error",
                            "detail": str(error),
                        },
                        args.json_output,
                    )
                    if isinstance(error, ExactWatcherAuthorityError):
                        sys.exit(75)
                    time.sleep(poll_interval)
        elif inbox_path.exists():
            data = load_agent_inbox(args.me)
            seen_paths = set(data.get("unread", []))

    polls = 0
    while True:
        try:
            if args.session:
                exact_messages = exact_session_messages(args)
                unread = {message["path"] for message in exact_messages}
                messages = {
                    message["path"]: message for message in exact_messages
                }
            elif inbox_path.exists():
                data = load_agent_inbox(args.me)
                unread = set(data.get("unread", []))
                messages = {message["path"]: message for message in get_unread_messages(args.me)}
            else:
                unread = set()
                messages = {}
            if unread:
                new_msgs = unread - seen_paths
                visible_new_msgs: list[str] = []
                for path in sorted(new_msgs):
                    message = messages.get(path, {"frontmatter": {}})
                    frontmatter = message.get("frontmatter", {})
                    if args.project is not None and frontmatter.get("project_id") != args.project:
                        continue
                    repo_match, repo_reason = repo_scope_matches(
                        args.repo_target,
                        frontmatter.get("repo_targets"),
                        subscriber_project=args.project,
                        packet_project=frontmatter.get("project_id"),
                    )
                    if not repo_match:
                        emit(
                            {
                                "ts": utc_now_str(),
                                "event": "repo_scope_refused",
                                "detail": path,
                                "agent": args.me,
                                "message_path": path,
                                "reason": repo_reason,
                            },
                            args.json_output,
                        )
                        continue
                    visible_new_msgs.append(path)
                for path in visible_new_msgs:
                    ts_str = utc_now_str()
                    emit({"ts": ts_str, "event": "new_message", "detail": path, "agent": args.me}, args.json_output)
                    if args.notify:
                        send_notification(
                            f"llm-collab: {args.me}",
                            f"New message: {Path(path).stem}",
                        )
                # seen_paths records what has been ANNOUNCED, committed BEFORE
                # dispatch. If a dispatch below raises, the except unwinds past
                # this point; committing after dispatch re-emitted the
                # new_message announcement for every packet in the poll on the
                # next poll. This dedups ANNOUNCEMENTS only — duplicate DISPATCH
                # is prevented by the processed-messages ledger (a delivered turn
                # returns returncode 0 and is marked processed), not by this set,
                # since dispatch_autobridge runs whenever `unread` is nonempty.
                seen_paths = seen_paths | new_msgs
                if not args.session and not args.no_autobridge:
                    refusal_stats: dict = {}
                    before = dict(refusal_progress)
                    consumed_paths = sorted(
                        set(
                            dispatch_autobridge(
                                args.me,
                                args.json_output,
                                project_id=args.project,
                                repo_targets=args.repo_target,
                                refusal_progress=refusal_progress,
                                refusal_stats=refusal_stats,
                            )
                        )
                    )
                    if refusal_progress != before:
                        save_refusal_progress(args.me, refusal_progress)
                    suppressed = {
                        reason: count
                        for reason, count in refusal_stats.items()
                        if reason != "_new"
                    }
                    if suppressed:
                        # GH-539: one aggregate line per poll instead of one line
                        # per already-decided message per poll.
                        emit(
                            {
                                "ts": utc_now_str(),
                                "event": "autobridge_refusal_summary",
                                "detail": f"{sum(suppressed.values())} already-refused message(s) skipped",
                                "agent": args.me,
                                "suppressed_by_reason": suppressed,
                                "new_refusals": refusal_stats.get("_new", 0),
                            },
                            args.json_output,
                        )
        except Exception as e:
            ts_str = utc_now_str()
            emit({"ts": ts_str, "event": "error", "detail": str(e)}, args.json_output)
            if args.session and isinstance(e, ExactWatcherAuthorityError):
                sys.exit(75)

        polls += 1
        if args.max_polls and polls >= args.max_polls:
            break
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
