"""Monitor ownership and lifecycle refusals for Pi-native workers (GH-319).

Every case is a way one packet gets woken twice, or a way a monitor belonging to a session
that no longer exists wakes one that does.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from _pi_monitor_lifecycle import (  # noqa: E402
    LIFECYCLE_INVALIDATING_EVENTS,
    REFUSE_DUPLICATE_WAKE_OWNER,
    REFUSE_MONITOR_ABSENT,
    REFUSE_MONITOR_STALE,
    REFUSE_UNKNOWN_WAKE_OWNER,
    WAKE_OWNER_PI_MONITOR,
    WAKE_OWNER_PM2,
    PiMonitorRefused,
    assert_monitor_is_current,
    assert_single_wake_owner,
    invalidate_monitor,
)


def session(*, owners=(WAKE_OWNER_PI_MONITOR,), generation=3, monitor_generation=3,
            monitor=True):
    record = {"session_id": "SESSION-GLIM", "wake_owners": list(owners),
              "runtime_generation": generation}
    if monitor:
        record["pi_monitor"] = {
            "path": "State/doorbells/glim.pointer",
            "runtime_generation": monitor_generation,
        }
    return record


class PiWakeOwnershipTest(unittest.TestCase):
    def test_one_owner_is_accepted(self):
        for owner in (WAKE_OWNER_PM2, WAKE_OWNER_PI_MONITOR):
            with self.subTest(owner=owner):
                self.assertEqual(owner, assert_single_wake_owner(session(owners=(owner,))))

    def test_two_owners_refuse(self):
        """One packet, two wakes, and no lease on the Pi side to detect it afterwards."""
        with self.assertRaises(PiMonitorRefused) as caught:
            assert_single_wake_owner(session(owners=(WAKE_OWNER_PM2, WAKE_OWNER_PI_MONITOR)))
        self.assertEqual(REFUSE_DUPLICATE_WAKE_OWNER, caught.exception.reason)
        self.assertIn("holds no lease", caught.exception.detail)

    def test_the_same_owner_twice_is_not_a_conflict(self):
        """Idempotent registration must not read as two claimants."""
        self.assertEqual(
            WAKE_OWNER_PM2,
            assert_single_wake_owner(session(owners=(WAKE_OWNER_PM2, WAKE_OWNER_PM2))),
        )

    def test_no_declared_owner_refuses_rather_than_defaulting(self):
        """A binding with no owner has no automatic wake; saying otherwise promises one."""
        for owners in ([], None, ""):
            with self.subTest(owners=owners):
                record = session()
                record["wake_owners"] = owners
                with self.assertRaises(PiMonitorRefused) as caught:
                    assert_single_wake_owner(record)
                self.assertEqual(REFUSE_UNKNOWN_WAKE_OWNER, caught.exception.reason)

    def test_an_unrecognised_owner_refuses(self):
        """A typo must not silently become a third wake path."""
        record = session()
        record["wake_owners"] = ["pi_event_monitr"]
        with self.assertRaises(PiMonitorRefused) as caught:
            assert_single_wake_owner(record)
        self.assertEqual(REFUSE_UNKNOWN_WAKE_OWNER, caught.exception.reason)

    def test_a_single_owner_given_as_a_bare_string_is_accepted(self):
        record = session()
        record["wake_owners"] = WAKE_OWNER_PI_MONITOR
        self.assertEqual(WAKE_OWNER_PI_MONITOR, assert_single_wake_owner(record))


class PiMonitorGenerationTest(unittest.TestCase):
    def test_a_current_monitor_is_proved(self):
        monitor = assert_monitor_is_current(session())
        self.assertEqual("State/doorbells/glim.pointer", monitor["path"])

    def test_a_stale_monitor_refuses(self):
        """Installed under an older generation, so a lifecycle event may have dropped it."""
        with self.assertRaises(PiMonitorRefused) as caught:
            assert_monitor_is_current(session(generation=4, monitor_generation=3))
        self.assertEqual(REFUSE_MONITOR_STALE, caught.exception.reason)
        self.assertIn("in-memory", caught.exception.detail)

    def test_an_absent_monitor_is_a_different_refusal_from_a_stale_one(self):
        """Absent means nobody installed it; stale means something may have dropped it.

        Same remedy, different diagnosis -- one of them means a step was forgotten.
        """
        with self.assertRaises(PiMonitorRefused) as caught:
            assert_monitor_is_current(session(monitor=False))
        self.assertEqual(REFUSE_MONITOR_ABSENT, caught.exception.reason)

    def test_an_unprovable_generation_is_treated_as_stale_not_as_current(self):
        """The ambiguity is the point: an unprovable monitor may or may not be running."""
        for session_gen, monitor_gen in ((None, 3), (3, None), ("3", 3), (3, "3")):
            with self.subTest(session=session_gen, monitor=monitor_gen):
                record = session()
                record["runtime_generation"] = session_gen
                record["pi_monitor"]["runtime_generation"] = monitor_gen
                with self.assertRaises(PiMonitorRefused) as caught:
                    assert_monitor_is_current(record)
                self.assertEqual(REFUSE_MONITOR_STALE, caught.exception.reason)
                # "unprovable" and "mismatched" share a code, so the message is what tells
                # an operator which happened. Asserting only the code cannot distinguish
                # them, and a mutation collapsing one into the other would pass.
                self.assertIn("cannot prove", caught.exception.detail)

    def test_a_monitor_from_a_LATER_generation_is_also_refused(self):
        """Not "older than", but "different from".

        A monitor recorded ahead of its session means the records disagree about which
        generation is current, and guessing which is right is how a dead session's monitor
        keeps waking a live one.
        """
        with self.assertRaises(PiMonitorRefused) as caught:
            assert_monitor_is_current(session(generation=3, monitor_generation=9))
        self.assertEqual(REFUSE_MONITOR_STALE, caught.exception.reason)


class PiLifecycleInvalidationTest(unittest.TestCase):
    def test_every_lifecycle_event_bumps_the_generation_and_degrades_to_pull(self):
        for event in LIFECYCLE_INVALIDATING_EVENTS:
            with self.subTest(event=event):
                applied = invalidate_monitor(session(generation=7), event)
                self.assertEqual(8, applied["runtime_generation"])
                self.assertIsNone(applied["pi_monitor"])
                self.assertIs(False, applied["dispatchable"])
                self.assertEqual("pull", applied["pending_delivery_mode"])
                self.assertEqual(event, applied["invalidated_by"])

    def test_invalidation_makes_the_previous_monitor_unusable(self):
        """The generation bump is the guarantee, not a stop call.

        The old monitor is in another process's memory and may already be gone, so a stop
        that fails silently proves nothing. A bumped generation makes it unusable whether
        or not it is still running.
        """
        before = session(generation=3, monitor_generation=3)
        assert_monitor_is_current(before)  # current now
        applied = invalidate_monitor(before, "app_restart")
        after = {**before, **applied}
        after["pi_monitor"] = before["pi_monitor"]  # as if the record survived the event
        with self.assertRaises(PiMonitorRefused) as caught:
            assert_monitor_is_current(after)
        self.assertEqual(REFUSE_MONITOR_STALE, caught.exception.reason)

    def test_an_unrecognised_event_refuses_rather_than_bumping(self):
        """A typo must not silently invalidate, nor silently fail to."""
        with self.assertRaises(PiMonitorRefused):
            invalidate_monitor(session(), "sesion_switch")

    def test_a_session_with_no_generation_starts_at_one(self):
        record = {"session_id": "SESSION-NEW"}
        self.assertEqual(1, invalidate_monitor(record, "binding_lost")["runtime_generation"])

    def test_the_six_events_the_issue_names_are_all_covered(self):
        """The issue lists session switch, fork, reload, quit, app restart and lost binding.

        Asserted as a set so dropping one is a failure rather than a silent gap.
        """
        self.assertEqual(
            {"session_switch", "session_fork", "session_reload", "session_quit",
             "app_restart", "binding_lost"},
            set(LIFECYCLE_INVALIDATING_EVENTS),
        )

    def test_no_refusal_path_returns_a_dispatchable_verdict(self):
        """Every refusal here must leave the packet pull-pending, never dispatchable.

        Read from the source rather than from one call, because the guarantee is about all
        of them: a future branch returning dispatchable=True would be the whole failure.
        """
        source = (REPO_ROOT / "bin" / "_pi_monitor_lifecycle.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "dispatchable":
                self.fail("dispatchable is passed as a keyword somewhere unexpected")
        self.assertIn('"dispatchable": False', source)
        self.assertNotIn('"dispatchable": True', source)


if __name__ == "__main__":
    unittest.main()
