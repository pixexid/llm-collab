from __future__ import annotations

import contextlib
import sys
import urllib.error
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from livecraft_health import (  # noqa: E402
    HealthStatus,
    LivecraftHealthError,
    ensure_livecraft_ready,
    probe_health,
)


class LivecraftHealthTest(unittest.TestCase):
    def test_probe_distinguishes_ready_manager_loss_and_refusal(self):
        self.assertTrue(
            probe_health(
                "http://127.0.0.1:43121",
                request=lambda _url: (200, {"ok": True, "managerConnected": True}),
            ).ready
        )
        disconnected = probe_health(
            "http://127.0.0.1:43121",
            request=lambda _url: (503, {"ok": True, "managerConnected": False}),
        )
        self.assertEqual(disconnected.kind, "manager_disconnected")

        def refused(_url):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        self.assertEqual(probe_health("http://127.0.0.1:43121", request=refused).kind,
                         "connection_refused")

    def test_recovery_kicks_once_then_waits_for_ready(self):
        refused = HealthStatus("connection_refused", detail="refused")
        statuses = iter((refused, refused, HealthStatus("ready", status_code=200,
                                                         manager_connected=True)))
        kicks = []
        result = ensure_livecraft_ready(
            timeout=1,
            probe=lambda _url: next(statuses),
            are_ports_absent=lambda _url: True,
            kickstart=lambda: kicks.append("kick"),
            lock=lambda: contextlib.nullcontext(),
            sleep=lambda _seconds: None,
            clock=lambda: 0,
        )
        self.assertTrue(result.ready)
        self.assertEqual(kicks, ["kick"])

    def test_manager_disconnected_never_kicks(self):
        kicks = []
        with self.assertRaisesRegex(LivecraftHealthError, "managerConnected=false.*refusing launchctl"):
            ensure_livecraft_ready(
                probe=lambda _url: HealthStatus(
                    "manager_disconnected", status_code=503, manager_connected=False,
                ),
                are_ports_absent=lambda _url: True,
                kickstart=lambda: kicks.append("kick"),
                lock=lambda: contextlib.nullcontext(),
            )
        self.assertEqual(kicks, [])

    def test_refusal_with_occupied_ports_never_kicks(self):
        kicks = []
        with self.assertRaisesRegex(LivecraftHealthError, "ports are occupied"):
            ensure_livecraft_ready(
                probe=lambda _url: HealthStatus("connection_refused"),
                are_ports_absent=lambda _url: False,
                kickstart=lambda: kicks.append("kick"),
                lock=lambda: contextlib.nullcontext(),
            )
        self.assertEqual(kicks, [])

    def test_recovery_wait_is_bounded_and_does_not_loop_kicks(self):
        now = [0.0]
        kicks = []
        with self.assertRaisesRegex(LivecraftHealthError, "after one LaunchAgent restart"):
            ensure_livecraft_ready(
                timeout=0.5,
                probe=lambda _url: HealthStatus("connection_refused"),
                are_ports_absent=lambda _url: True,
                kickstart=lambda: kicks.append("kick"),
                lock=lambda: contextlib.nullcontext(),
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                clock=lambda: now[0],
            )
        self.assertEqual(kicks, ["kick"])

    def test_second_caller_does_not_kick_after_recovery_lock_deadline(self):
        now = [0.0]
        kicks = []
        acquisitions = [0]

        @contextlib.contextmanager
        def delayed_lock():
            acquisitions[0] += 1
            if acquisitions[0] == 2:
                now[0] += 1.0
            yield

        def refused(_url):
            return HealthStatus("connection_refused")

        with self.assertRaisesRegex(LivecraftHealthError, "after one LaunchAgent restart"):
            ensure_livecraft_ready(
                timeout=0.5,
                probe=refused,
                are_ports_absent=lambda _url: True,
                kickstart=lambda: kicks.append("kick"),
                lock=lambda: delayed_lock(),
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                clock=lambda: now[0],
            )
        with self.assertRaisesRegex(LivecraftHealthError, "expired while waiting"):
            ensure_livecraft_ready(
                timeout=0.5,
                probe=refused,
                are_ports_absent=lambda _url: True,
                kickstart=lambda: kicks.append("kick"),
                lock=lambda: delayed_lock(),
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                clock=lambda: now[0],
            )
        self.assertEqual(kicks, ["kick"])


if __name__ == "__main__":
    unittest.main()
