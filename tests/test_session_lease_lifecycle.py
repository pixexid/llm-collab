"""A session's validity follows its native task, not a clock (GH-324).

A lease is stamped at register time and nothing renews it, so an `active`
session that is alive and working goes undispatchable on a TTL. That is not a
late wake, it is a silent one: on 2026-07-28 a session registered at 19:12
expired at 20:12:01, kept reporting `status: active` with `updated_utc`
19:43:39, and its wake path stayed dead for three hours while the task worked.

Contract decision (Rafael, relayed 2026-07-28): a collaboration lease remains
valid for the lifetime of the same native task and ends only when the task ends
or an explicit continuation supersedes it. `parked` keeps the clock — a parked
claim is not held by a live task, so a TTL is what makes an abandoned one
reclaimable.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from _session_autobridge import session_is_dispatchable, session_is_expired

PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


def session(status: str, lease: str | None = PAST) -> dict:
    record = {"status": status, "agent_id": "claude", "session_id": "SESSION-X"}
    if lease is not None:
        record["lease_expires_utc"] = lease
    return record


class ActiveSessionOutlivesItsLeaseTest(unittest.TestCase):
    def test_an_active_session_with_an_expired_lease_is_still_dispatchable(self) -> None:
        """The 2026-07-28 shape exactly: alive, working, three hours past TTL."""
        dispatchable, reason = session_is_dispatchable(session("active", PAST))
        self.assertTrue(dispatchable, f"refused a live session: {reason}")
        self.assertEqual("ok", reason)

    def test_an_active_session_with_no_lease_field_is_dispatchable(self) -> None:
        self.assertEqual((True, "ok"), session_is_dispatchable(session("active", None)))

    def test_an_active_session_with_a_valid_lease_is_unchanged(self) -> None:
        self.assertEqual((True, "ok"), session_is_dispatchable(session("active", FUTURE)))

    def test_the_clock_fact_itself_is_unchanged(self) -> None:
        """`session_is_expired` still reports the raw clock. The ruling changes what
        dispatch *does* with that fact, not whether the fact is available — a
        caller that legitimately wants expiry, such as reclaiming an abandoned
        claim, must still be able to ask.
        """
        self.assertTrue(session_is_expired(session("active", PAST)))
        self.assertFalse(session_is_expired(session("active", FUTURE)))


class EndedSessionsStillFailClosedTest(unittest.TestCase):
    """The ruling widens `active`; it must not widen anything else."""

    def test_a_parked_session_still_expires_on_its_lease(self) -> None:
        """A parked claim is not held by a live task, so the TTL is what makes an
        abandoned one reclaimable. Removing it there would let a dead claim block
        a replacement forever."""
        self.assertEqual((False, "lease_expired"), session_is_dispatchable(session("parked", PAST)))

    def test_a_parked_session_within_its_lease_is_dispatchable(self) -> None:
        self.assertEqual((True, "ok"), session_is_dispatchable(session("parked", FUTURE)))

    def test_a_stopped_session_is_refused_even_with_a_live_lease(self) -> None:
        dispatchable, reason = session_is_dispatchable(session("stopped", FUTURE))
        self.assertFalse(dispatchable)
        self.assertEqual("status=stopped", reason)

    def test_a_superseded_session_is_refused_even_with_a_live_lease(self) -> None:
        """Explicit supersession is one of the two ways the ruling says a session
        ends, so it must remain terminal regardless of the clock."""
        dispatchable, reason = session_is_dispatchable(session("superseded", FUTURE))
        self.assertFalse(dispatchable)
        self.assertEqual("status=superseded", reason)

    def test_an_unknown_status_is_refused(self) -> None:
        self.assertFalse(session_is_dispatchable(session("wedged", FUTURE))[0])


if __name__ == "__main__":
    unittest.main()
