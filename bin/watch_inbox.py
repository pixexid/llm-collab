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
import re
import subprocess
import time
from datetime import datetime, timedelta

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
    active_read_budget,
    dispatch_session,
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
REFUSAL_WINDOW_DAYS = 7
_PACKET_TS = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})_")


def refusal_progress_path(agent_id: str) -> Path:
    return agent_dir(agent_id) / "watcher-refusal-progress.json"


def load_refusal_progress(agent_id: str) -> dict:
    path = refusal_progress_path(agent_id)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    refused = data.get("refused")
    if not isinstance(refused, dict):
        # Valid JSON of the wrong SHAPE (e.g. {"refused": []}) must degrade the
        # same way as corrupt JSON: progress is an optimisation, never a gate,
        # and a list here would blow up progress.get() in the watcher loop.
        return {}
    return {k: v for k, v in refused.items() if isinstance(k, str) and isinstance(v, str)}


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


def refusal_fingerprint(reason: str, repo_targets, packet_repo_targets, subscriber_project, packet_project) -> str:
    """Bind terminality to the ROUTING DECISION, not just the path, so corrected
    routing re-opens eligibility instead of suppressing the message forever."""
    payload = json.dumps(
        {
            "reason": reason,
            "repo_targets": sorted(repo_targets) if repo_targets else None,
            "packet_repo_targets": sorted(packet_repo_targets) if isinstance(packet_repo_targets, list) else packet_repo_targets,
            "subscriber_project": subscriber_project,
            "packet_project": packet_project,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def within_refusal_window(message_path: str, now: datetime | None = None) -> bool:
    """True when the packet's own filename timestamp is inside the recent window.
    An unparseable name is treated as IN-window: never hide a message because its
    name did not match a convention."""
    match = _PACKET_TS.search(Path(message_path).name)
    if not match:
        return True
    try:
        stamp = datetime.strptime(
            f"{match.group(1)}T{match.group(2)}:{match.group(3)}:{match.group(4)}",
            "%Y-%m-%dT%H:%M:%S",
        )
    except ValueError:
        return True
    return stamp >= (now or datetime.utcnow()) - timedelta(days=REFUSAL_WINDOW_DAYS)


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

    def record_refusal(path: str, reason: str, packet_repo_targets=None, packet_project=None) -> bool:
        """True when this refusal is NEW and should be logged. A repeat of the same
        routing decision is counted for the aggregate summary and not re-logged."""
        fingerprint = refusal_fingerprint(
            reason, repo_targets, packet_repo_targets, project_id, packet_project
        )
        if progress.get(path) == fingerprint:
            stats[reason] = stats.get(reason, 0) + 1
            return False
        progress[path] = fingerprint
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
            result = dispatch_session(
                session_id, project_id=project_id, repo_targets=repo_targets
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
