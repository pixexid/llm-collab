"""Monitor ownership and lifecycle for Pi-native workers (GH-319).

`pi-event-monitor` monitors are in-memory and session-owned. Nothing on disk outlives a
session switch, fork, reload, quit, app restart or lost binding -- so a monitor that looks
installed may belong to a session that no longer exists, and a wake driven by it would
target a binding nobody registered.

Two rules, both refusals rather than repairs:

**One owner per binding.** Automatic wake for an exact binding is owned by exactly one path
-- the PM2 watcher, or Pi's native monitor. Two owners means two wakes for one packet, and
the Pi path has no lease to serialise against, so the duplicate cannot be detected after
the fact. An explicit pull/manual fallback is always retained, because refusing automatic
wake must never mean the packet is unreachable.

**A monitor's generation must match its session's.** Every lifecycle event that can drop an
in-memory monitor bumps the session's generation. A monitor recorded under an older
generation is stale by definition: it may or may not still be running, and that ambiguity
is the point -- an unprovable monitor is treated as absent, the endpoint becomes
non-dispatchable, and pending messages degrade to pull until the session re-registers and
reinstalls. Re-registration is required rather than inferred, because inferring it is how a
monitor for a dead session keeps waking a live one.
"""

from __future__ import annotations

from typing import Any


WAKE_OWNER_PM2 = "pm2_watcher"
WAKE_OWNER_PI_MONITOR = "pi_event_monitor"
WAKE_OWNERS = (WAKE_OWNER_PM2, WAKE_OWNER_PI_MONITOR)

# Every event that can drop an in-memory, session-owned monitor. Named so a drift record
# says which one happened; the remedies are identical but the diagnosis is not.
LIFECYCLE_INVALIDATING_EVENTS = (
    "session_switch",
    "session_fork",
    "session_reload",
    "session_quit",
    "app_restart",
    "binding_lost",
)

REFUSE_DUPLICATE_WAKE_OWNER = "pi_duplicate_wake_owner"
REFUSE_MONITOR_STALE = "pi_monitor_stale"
REFUSE_MONITOR_ABSENT = "pi_monitor_absent"
REFUSE_UNKNOWN_WAKE_OWNER = "pi_unknown_wake_owner"


class PiMonitorRefused(RuntimeError):
    """Automatic wake refused. The packet stays durable and pull-pending."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def assert_single_wake_owner(session: Any) -> str:
    """Exactly one path may own automatic wake for this binding.

    Returns the owner. Refuses on zero owners as well as two, because a binding that
    declares no owner has no automatic wake at all and reporting it as dispatchable would
    promise one.
    """
    if not isinstance(session, dict):
        raise PiMonitorRefused(
            REFUSE_UNKNOWN_WAKE_OWNER, "no session record to read a wake owner from"
        )
    declared = session.get("wake_owners")
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, list) or not declared:
        raise PiMonitorRefused(
            REFUSE_UNKNOWN_WAKE_OWNER,
            "this binding declares no automatic wake owner; it is pull-only until one is "
            f"registered (expected one of {list(WAKE_OWNERS)})",
        )
    owners = [str(owner) for owner in declared]
    unknown = [owner for owner in owners if owner not in WAKE_OWNERS]
    if unknown:
        raise PiMonitorRefused(
            REFUSE_UNKNOWN_WAKE_OWNER,
            f"unrecognised wake owner(s) {sorted(unknown)}; expected one of "
            f"{list(WAKE_OWNERS)}",
        )
    if len(set(owners)) > 1:
        raise PiMonitorRefused(
            REFUSE_DUPLICATE_WAKE_OWNER,
            f"both {sorted(set(owners))} claim automatic wake for this binding. One packet "
            "would be woken twice, and the Pi path holds no lease to serialise against, so "
            "the duplicate could not be detected afterwards. Choose one and keep pull as "
            "the fallback.",
        )
    return owners[0]


def assert_monitor_is_current(session: Any) -> dict[str, Any]:
    """Prove the recorded monitor belongs to this session's current generation.

    Returns the monitor record. An absent monitor and a stale one are separate refusals:
    absent means automatic wake was never installed, stale means it was and a lifecycle
    event since then may have dropped it. Both leave the packet pull-pending, but only one
    of them means someone forgot a step.
    """
    if not isinstance(session, dict):
        raise PiMonitorRefused(REFUSE_MONITOR_ABSENT, "no session record")
    monitor = session.get("pi_monitor")
    if not isinstance(monitor, dict):
        raise PiMonitorRefused(
            REFUSE_MONITOR_ABSENT,
            "no Pi monitor is recorded for this binding, so nothing is watching the "
            "doorbell path; the packet is durable and must be pulled",
        )
    session_generation = session.get("runtime_generation")
    # `.get` on a possibly-non-dict via a local, so that a bypassed guard above degrades to
    # a refusal instead of raising AttributeError INTO the dispatch path -- the same hazard
    # as the uncaught OverflowError that produced a redelivery in llm-collab#316.
    monitor_generation = monitor.get("runtime_generation") if isinstance(monitor, dict) else None
    if not isinstance(session_generation, int) or not isinstance(monitor_generation, int):
        raise PiMonitorRefused(
            REFUSE_MONITOR_STALE,
            "cannot prove which session generation this monitor was installed under, so it "
            "cannot be shown to be current. An unprovable monitor is treated as absent "
            "rather than assumed live.",
        )
    if monitor_generation != session_generation:
        raise PiMonitorRefused(
            REFUSE_MONITOR_STALE,
            f"the monitor was installed under generation {monitor_generation} and this "
            f"session is at {session_generation}: a lifecycle event since then may have "
            "dropped it, and pi-event-monitor monitors are in-memory so nothing on disk "
            "proves otherwise. Re-register the session and reinstall the monitor.",
        )
    return monitor


def invalidate_monitor(session: Any, event: str) -> dict[str, Any]:
    """Apply a lifecycle event: bump the generation and make the endpoint pull-only.

    Returns the fields to persist. Deliberately does not attempt to stop the old monitor:
    it is in another process's memory and may already be gone. Bumping the generation makes
    it unusable whether or not it is still running, which is the only guarantee available
    from here -- and is stronger than a stop that might have failed silently.
    """
    if event not in LIFECYCLE_INVALIDATING_EVENTS:
        raise PiMonitorRefused(
            REFUSE_MONITOR_STALE,
            f"{event!r} is not a recognised lifecycle event; expected one of "
            f"{list(LIFECYCLE_INVALIDATING_EVENTS)}",
        )
    current = 0
    if isinstance(session, dict) and isinstance(session.get("runtime_generation"), int):
        current = session["runtime_generation"]
    return {
        "runtime_generation": current + 1,
        "pi_monitor": None,
        "dispatchable": False,
        "pending_delivery_mode": "pull",
        "invalidated_by": event,
    }
