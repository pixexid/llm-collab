"""Watcher liveness markers — the reading side, and the ONE place the path rule lives.

The marker convention is owned by docs/workflows/orchestrator-sessions.md: each
standard orchestrator watcher touches

    {project_state_root}/llm-collab/watchers/<name>.alive

every cycle. Both bin/session_gate.py (SessionStart hook) and bin/bb_spawn.py
(delegation-time gate) read through this module; a second implementation of the
path rule is how these drift.
"""

from __future__ import annotations

import time

from _helpers import project_state_dir

WATCHER_NAMES = ("worker-lifecycle", "pr-artifacts", "heartbeat")

# The code cannot know each watcher's own cycle period: the watchers are defined
# by doc templates in docs/workflows/orchestrator-sessions.md, not by code in
# this repository. The known watcher cycles in this workspace are minute-scale
# at most (watch_inbox poll default 15s in pm2/ecosystem.config.cjs, pr_watch
# --interval default 60s) and a marker is touched EVERY cycle, so 600s is a 10x
# margin over the slowest known cycle while still surfacing a dead watcher
# within the same work segment. If a watcher template ever gets a longer cycle,
# raise this bound in the same change that introduces it.
WATCHER_MARKER_STALE_AFTER_SECONDS = 600.0

FRESH = "fresh"
STALE = "stale"
ABSENT = "absent"
UNREADABLE = "unreadable"


def markers_dir():
    return project_state_dir("llm-collab") / "watchers"


def check_markers(now=None, stale_after=WATCHER_MARKER_STALE_AFTER_SECONDS):
    """Classify every standard watcher's marker by mtime freshness.

    A broken probe reports UNREADABLE, never fresh: a marker that cannot be
    stat'ed is indistinguishable from a dead watcher and must not read as a pass.
    """
    moment = time.time() if now is None else now
    try:
        directory = markers_dir()
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
        except OSError as error:
            entry["status"] = UNREADABLE
            entry["detail"] = str(error)
        else:
            age = moment - mtime
            entry["age_seconds"] = round(age, 1)
            entry["status"] = FRESH if age <= stale_after else STALE
        report.append(entry)
    return report


def not_fresh(report):
    return [entry for entry in report if entry["status"] != FRESH]
