"""Watcher liveness markers — the ONE place the marker path and format rules live.

The marker convention is owned by docs/workflows/orchestrator-sessions.md: each
standard orchestrator watcher rewrites

    {project_state_root}/<project-id>/watchers/<name>.alive

every cycle, with JSON content

    {"session_id": "<owning session>", "project_id": "<project>", "started_at": "<UTC ISO-8601>"}

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
the doc lane's watcher templates reference bin/touch_watcher_marker.py.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from _helpers import get_project, project_state_dir, write_file_durably

WATCHER_NAMES = ("worker-lifecycle", "pr-artifacts", "heartbeat")

# The code cannot know each watcher's own cycle period: the watchers are defined
# by doc templates in docs/workflows/orchestrator-sessions.md, not by code in
# this repository. The known watcher cycles in this workspace are minute-scale
# at most (watch_inbox poll default 15s in pm2/ecosystem.config.cjs, pr_watch
# --interval default 60s) and a marker is rewritten EVERY cycle, so 600s is a
# 10x margin over the slowest known cycle while still surfacing a dead watcher
# within the same work segment. If a watcher template ever gets a longer cycle,
# raise this bound in the same change that introduces it.
WATCHER_MARKER_STALE_AFTER_SECONDS = 600.0

# A marker is a few hundred bytes of JSON written by write_marker. Anything
# larger is not a marker; bound the read so an accidentally huge file classifies
# UNREADABLE instead of being parsed unboundedly.
MAX_MARKER_BYTES = 4096

FRESH = "fresh"
STALE = "stale"
ABSENT = "absent"
UNREADABLE = "unreadable"


def markers_dir(project_id):
    return project_state_dir(project_id) / "watchers"


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
        with open(marker, "rb") as handle:
            data = handle.read(MAX_MARKER_BYTES + 1)
        # Bounded read, fail closed: an oversized existing marker is not a
        # marker — discard it rather than preserving its started_at into a new
        # marker, and never parse unbounded input.
        existing = json.loads(data.decode("utf-8")) if len(data) <= MAX_MARKER_BYTES else None
    except (OSError, ValueError, UnicodeDecodeError):
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
    }
    write_file_durably(marker, json.dumps(content, sort_keys=True) + "\n")
    return marker


def _read_owner(marker, project_id):
    """Return (owner_fields, error_detail). Exactly one element is None.

    Malformed, oversized, or foreign-project content is an error: such a
    marker classifies UNREADABLE, never fresh (a cross-project overwrite is
    the incident shape this exists to make visible).
    """
    try:
        with open(marker, "rb") as handle:
            data = handle.read(MAX_MARKER_BYTES + 1)
    except OSError as error:
        return None, str(error)
    if len(data) > MAX_MARKER_BYTES:
        return None, f"marker exceeds {MAX_MARKER_BYTES} bytes"
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
    return {"session_id": session_id, "started_at": started_at}, None


def check_markers(project_id, now=None, stale_after=WATCHER_MARKER_STALE_AFTER_SECONDS):
    """Classify every standard watcher's marker: mtime freshness plus ownership.

    A broken probe reports UNREADABLE, never fresh: a marker that cannot be
    stat'ed or whose content does not parse for THIS project is
    indistinguishable from a dead or foreign watcher and must not read as a
    pass. Freshness stays an mtime question; the content carries the owner.
    """
    moment = time.time() if now is None else now
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
            mtime = marker.stat().st_mtime
        except FileNotFoundError:
            entry["status"] = ABSENT
            report.append(entry)
            continue
        except OSError as error:
            entry["status"] = UNREADABLE
            entry["detail"] = str(error)
            report.append(entry)
            continue
        owner, error = _read_owner(marker, project_id)
        if error is not None:
            entry["status"] = UNREADABLE
            entry["detail"] = error
            report.append(entry)
            continue
        entry.update(owner)
        age = moment - mtime
        entry["age_seconds"] = round(age, 1)
        if age < 0:
            # A future mtime is not "old", so STALE would be a lie about the
            # direction of the evidence; it is inconsistent evidence, which is
            # what UNREADABLE reports. Never FRESH: a clock moved backward must
            # not keep a dead watcher satisfying the gate.
            entry["status"] = UNREADABLE
            entry["detail"] = f"marker mtime is {-age:.1f}s in the future"
        else:
            entry["status"] = FRESH if age <= stale_after else STALE
        report.append(entry)
    return report


def not_fresh(report):
    return [entry for entry in report if entry["status"] != FRESH]


def foreign_fresh(report, current_session_id):
    """Fresh markers whose RECORDED owner is not the current session.

    A None/empty current_session_id means the caller could not establish its
    own identity: ownership cannot be compared, so this returns nothing and
    the caller must say the check did not run — an unknown-owner comparison
    must never silently read as a pass (GH-726 I5).
    """
    if not current_session_id:
        return []
    return [
        entry
        for entry in report
        if entry["status"] == FRESH and entry.get("session_id") != current_session_id
    ]
