"""Watcher liveness markers — the ONE place the marker path and format rules live.

The marker convention is owned by docs/workflows/orchestrator-sessions.md: each
standard orchestrator watcher rewrites

    {project_state_root}/<project-id>/watchers/<name>.alive

every cycle, with JSON content

    {"session_id": "<owning session>", "project_id": "<project>", "started_at": "<UTC ISO-8601>",
     "pid": <watcher pid>, "argv_marker": "orchestrator_watch.py <name> --project <project>"}

Freshness is the mtime — never a timestamp inside the file, because a wedged
loop that still rewrites its own content would then look alive. Ownership is
the content, and it is RECORDED, never inferred (GH-726 I3): a fresh marker
owned by a different session means a predecessor's watcher is still running.

Both bin/session_gate.py (SessionStart hook) and bin/bb_spawn.py
(delegation-time gate) read through this module; a second implementation of the
path or format rule is how these drift. The project id is the caller's explicit
choice — there is deliberately no default, so a caller that forgets it fails
rather than silently reading another project's markers (AGENTS.md → "Project
Boundary"). The writing side is the single documented shape in write_marker();
bin/orchestrator_watch.py calls it in-process.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from _bounded_io import (  # noqa: E402
    UnreadableFile,
    read_regular_file_bounded,
    read_regular_file_bounded_with_identity,
)
from _helpers import get_project, project_state_dir, write_file_durably
from llm_collab.bb_client import subprocess_transport

WATCHER_NAMES = ("worker-lifecycle", "pr-artifacts", "heartbeat")

# The standard watcher periods live in bin/orchestrator_watch.py. All modes have
# a 300s cumulative cycle deadline; heartbeat refreshes every 60s between 600s
# reports. The 60s refresh gap plus 300s work bound leaves 240s of margin inside
# this external bound. Any interval or deadline increase must revisit that margin
# without weakening this shared bound.
WATCHER_MARKER_STALE_AFTER_SECONDS = 600.0

# A marker is a few hundred bytes of JSON written by write_marker. Anything
# larger is not a marker; bound the read so an accidentally huge file classifies
# UNREADABLE instead of being parsed unboundedly.
MAX_MARKER_BYTES = 4096

FRESH = "fresh"
STALE = "stale"
ABSENT = "absent"
UNREADABLE = "unreadable"

# Verdict reasons from evaluate_coverage for FRESH markers. Non-fresh markers
# keep their marker status as the reason.
COVERED = "covered"
FOREIGN = "foreign"
OWNER_UNKNOWN = "owner_unknown"
OWNER_GONE = "owner_gone"
LIVENESS_UNVERIFIABLE = "liveness_unverifiable"

# A ps command line is tiny. The shared transport bounds both streams while
# reading, kills the child on timeout/overflow, and never returns truncation as
# a complete result. A failed probe is classified by evaluate_coverage rather
# than escaping into either gate.
LIVENESS_PROBE_TIMEOUT_SECONDS = 2.0
LIVENESS_PROBE_MAX_RESPONSE_CHARS = 64 * 1024

# Where the future-dated line is drawn. A watcher that atomically rewrites its
# marker between our descriptor read and our clock sample yields a small
# NEGATIVE age — a benign race against a live watcher, and classifying it
# UNREADABLE made the future-dated guard report healthy coverage as broken
# (intermittently, at every watcher cycle). A genuinely future timestamp — a
# moved clock, a dead watcher whose marker carries a future mtime — is seconds
# to minutes out, not milliseconds. 5s absorbs the race and filesystem mtime
# granularity while extending the future-dated exposure by at most 5s against
# the 600s staleness bound.
FUTURE_TOLERANCE_SECONDS = 5.0


def markers_dir(project_id):
    return project_state_dir(project_id) / "watchers"


def handoff_file(project_id):
    """The orchestrator handoff path — project runtime state, not a checkout
    document (GH-726 S6, amended): two registered projects can coordinate the
    same product checkout, so a checkout-relative path would have both
    orchestrators overwrite one succession file.
    """
    return project_state_dir(project_id) / "orchestrator-handoff.md"


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_marker(project_id, name, session_id):
    """The single documented writing shape for a watcher liveness marker.

    The session id is an explicit argument — ownership is recorded by the
    caller that knows it, never inferred here. `started_at` survives rewrites
    by the same session for the same project (it is when THIS watcher's
    session started it, not the last rewrite; mtime already carries that).
    """
    if name not in WATCHER_NAMES:
        raise ValueError(f"unknown watcher name {name!r}; expected one of {WATCHER_NAMES}")
    if get_project(project_id) is None:
        # Project Boundary: a project-aware mutator demands an exact registered
        # project. A typo would report success while the intended project's
        # markers stay stale; path components would escape the state tree.
        raise ValueError(f"unregistered project {project_id!r}; refusing to write a marker")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is required")
    marker = markers_dir(project_id) / f"{name}.alive"
    started_at = _utc_now_iso()
    try:
        # Bounded, regular-file-only read: an oversized or non-regular existing
        # marker is not a marker — discard it rather than preserving its
        # started_at into a new marker, and never block on or parse unbounded
        # input.
        existing = json.loads(
            read_regular_file_bounded(marker, MAX_MARKER_BYTES).decode("utf-8")
        )
    except (FileNotFoundError, UnreadableFile, ValueError, UnicodeDecodeError):
        existing = None
    if (
        isinstance(existing, dict)
        and existing.get("session_id") == session_id
        and existing.get("project_id") == project_id
        and isinstance(existing.get("started_at"), str)
        and existing["started_at"]
    ):
        started_at = existing["started_at"]
    content = {
        "session_id": session_id,
        "project_id": project_id,
        "started_at": started_at,
        "pid": os.getpid(),
        "argv_marker": f"orchestrator_watch.py {name} --project {project_id}",
    }
    write_file_durably(marker, json.dumps(content, sort_keys=True) + "\n")
    return marker


def _parse_owner(data: bytes, project_id):
    """Return (owner_fields, error_detail) for marker bytes. Exactly one is None.

    Malformed or foreign-project content is an error: such a marker classifies
    UNREADABLE, never fresh (a cross-project overwrite is the incident shape
    this exists to make visible).
    """
    try:
        content = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return None, f"marker content is not valid JSON: {error}"
    if not isinstance(content, dict):
        return None, "marker content is not a JSON object"
    session_id = content.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None, "marker content has no session_id"
    if content.get("project_id") != project_id:
        return None, (
            f"marker content names project {content.get('project_id')!r}, "
            f"expected {project_id!r}"
        )
    started_at = content.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        return None, "marker content has no started_at"
    owner = {"session_id": session_id, "started_at": started_at}
    pid = content.get("pid")
    argv_marker = content.get("argv_marker")
    if pid is None and argv_marker is None:
        return owner, None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None, "marker content has no positive integer pid"
    if not isinstance(argv_marker, str) or not argv_marker:
        return None, "marker content has no argv_marker"
    owner.update({"pid": pid, "argv_marker": argv_marker})
    return owner, None


def probe_process_liveness(pid: int, argv_marker: str) -> tuple[bool | None, str | None]:
    """Return paired / gone-or-mismatched / unverifiable, and never raise."""
    try:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, f"pid {pid} is not running"
        except OSError as error:
            return None, f"pid {pid} could not be probed: {error}"

        result = subprocess_transport(
            ("ps",), max_response_chars=LIVENESS_PROBE_MAX_RESPONSE_CHARS
        )(
            ("-ww", "-p", str(pid), "-o", "command="),
            LIVENESS_PROBE_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            # The process may have exited between kill(0) and ps. Distinguish
            # that ordinary race from a ps failure that could not answer.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False, f"pid {pid} exited during the liveness probe"
            except OSError as error:
                return None, f"pid {pid} could not be re-probed: {error}"
            return None, f"ps could not read pid {pid} (exit {result.exit_code})"
        command = result.stdout.strip()
        if not command:
            return None, f"ps returned no command line for pid {pid}"
        if argv_marker not in command:
            return False, (
                f"pid {pid} command line does not contain recorded argv_marker "
                f"{argv_marker!r}"
            )
        return True, None
    except Exception as error:
        return None, f"liveness probe failed: {type(error).__name__}: {error}"


def check_markers(project_id, now=None, stale_after=WATCHER_MARKER_STALE_AFTER_SECONDS):
    """Classify every standard watcher's marker: mtime freshness plus ownership.

    A broken probe reports UNREADABLE, never fresh: a marker that cannot be
    stat'ed or whose content does not parse for THIS project is
    indistinguishable from a dead or foreign watcher and must not read as a
    pass. Freshness stays an mtime question; the content carries the owner.
    When `now` is not supplied the clock is sampled AFTER each descriptor
    read, so a concurrent rewrite reads as a small negative age (see
    FUTURE_TOLERANCE_SECONDS), not as a far-future timestamp.
    """
    try:
        directory = markers_dir(project_id)
    except (Exception, SystemExit) as error:
        # Resolving the state root can itself fail (e.g. a worktree without
        # collab.config.json makes load_config exit). That is UNREADABLE for
        # every watcher, never a crash and never a pass.
        detail = f"markers directory unresolvable: {type(error).__name__}: {error}"
        return [
            {"name": name, "status": UNREADABLE, "marker": "?", "detail": detail}
            for name in WATCHER_NAMES
        ]
    report = []
    for name in WATCHER_NAMES:
        marker = directory / f"{name}.alive"
        entry = {"name": name, "marker": str(marker)}
        try:
            # One descriptor, non-blocking open, regular-file requirement, byte
            # cap, and the mtime from that SAME descriptor: a FIFO or oversized
            # path fails closed as UNREADABLE instead of hanging the hook or the
            # writing-spawn gate inside open().
            data, mtime = read_regular_file_bounded_with_identity(marker, MAX_MARKER_BYTES)
        except FileNotFoundError:
            entry["status"] = ABSENT
            report.append(entry)
            continue
        except UnreadableFile as error:
            entry["status"] = UNREADABLE
            entry["detail"] = str(error)
            report.append(entry)
            continue
        owner, error = _parse_owner(data, project_id)
        if error is not None:
            entry["status"] = UNREADABLE
            entry["detail"] = error
            report.append(entry)
            continue
        if mtime is None:
            entry["status"] = UNREADABLE
            entry["detail"] = "marker mtime unavailable"
            report.append(entry)
            continue
        entry.update(owner)
        moment = now if now is not None else time.time()
        age = moment - mtime
        entry["age_seconds"] = round(age, 1)
        if age < -FUTURE_TOLERANCE_SECONDS:
            # Genuinely future: not "old", so STALE would be a lie about the
            # direction of the evidence; it is inconsistent evidence, which is
            # what UNREADABLE reports. Never FRESH: a clock moved backward must
            # not keep a dead watcher satisfying the gate.
            entry["status"] = UNREADABLE
            entry["detail"] = f"marker mtime is {-age:.1f}s in the future"
        else:
            # A small negative age (a concurrent rewrite landing between the
            # descriptor read and the clock sample) is a LIVE watcher.
            entry["status"] = FRESH if age <= stale_after else STALE
        report.append(entry)
    return report


def not_fresh(report):
    return [entry for entry in report if entry["status"] != FRESH]


def evaluate_coverage(report, current_session_id):
    """The ONE watcher-coverage verdict: freshness AND ownership folded in.

    Both consumers call this and neither re-derives any subset of the policy:
    the SessionStart hook (bin/session_gate.py) renders it, the writing-spawn
    gate (bin/bb_spawn.py) acts on it. Two consumers applying different
    subsets of one signal is how a fresh-but-foreign marker passed the spawn
    gate while the hook called the same marker foreign coverage.

    A FRESH marker is coverage only when its recorded pid is alive and that
    process command line contains its recorded argv_marker. A legacy marker
    without those fields is not verifiable. A FRESH marker owned by a
    DIFFERENT session is not coverage — it is a
    predecessor's watcher still firing into this session. A FRESH marker whose
    owner cannot be compared because the caller has no session identity is not
    coverage either: unknown is never a pass (GH-726 I5).

    Each verdict: {name, acceptable, reason, ...marker fields}. reason is
    COVERED / FOREIGN / OWNER_UNKNOWN / OWNER_GONE / LIVENESS_UNVERIFIABLE for
    fresh markers, else the marker status (stale / absent / unreadable).
    """
    verdicts = []
    for entry in report:
        verdict = {"name": entry["name"], "acceptable": False}
        for key in (
            "session_id",
            "age_seconds",
            "detail",
            "marker",
            "pid",
            "argv_marker",
        ):
            if key in entry:
                verdict[key] = entry[key]
        if entry["status"] != FRESH:
            verdict["reason"] = entry["status"]
        elif "pid" not in entry or "argv_marker" not in entry:
            verdict["reason"] = LIVENESS_UNVERIFIABLE
            verdict["detail"] = "fresh legacy marker has no pid/argv_marker"
        else:
            live, detail = probe_process_liveness(entry["pid"], entry["argv_marker"])
            if detail is not None:
                verdict["detail"] = detail
            if live is None:
                verdict["reason"] = LIVENESS_UNVERIFIABLE
            elif not live:
                verdict["reason"] = OWNER_GONE
            elif current_session_id is None:
                verdict["reason"] = OWNER_UNKNOWN
            elif entry.get("session_id") == current_session_id:
                verdict["acceptable"] = True
                verdict["reason"] = COVERED
            else:
                verdict["reason"] = FOREIGN
        verdicts.append(verdict)
    return verdicts


def uncovered(verdicts):
    return [verdict for verdict in verdicts if not verdict["acceptable"]]
