"""codex_appserver CLI contract: exact request shapes and fail-closed identity.

This CLI can start, steer, and cancel turns on a real Codex account, so the wire
shapes it sends are load-bearing. A fake client records requests and returns the
responses the installed App Server schema actually defines.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import codex_appserver as cli  # noqa: E402

THREAD = "thread-abc"
TURN = "turn-xyz"


class _RecordingSocket:
    """Captures settimeout calls so deadline enforcement is observable."""

    def __init__(self):
        self.timeouts: list[float] = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeClient:
    """Records requests; replays notifications; mimics the real ws client's context."""

    def __init__(self, responses=None, notifications=None, recv_error=None):
        self.requests: list[tuple[str, dict]] = []
        self.notified: list[str] = []
        self.sent: list[dict] = []
        self._responses = responses or {}
        self._notifications = list(notifications or [])
        self._recv_error = recv_error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def request(self, method, params=None):
        self.requests.append((method, params or {}))
        if method in self._responses:
            value = self._responses[method]
            if isinstance(value, Exception):
                raise value
            return value
        return {}

    def notify(self, method, params=None):
        self.notified.append(method)

    def send_json(self, payload):
        self.sent.append(payload)

    def recv_json(self):
        if self._notifications:
            return self._notifications.pop(0)
        if self._recv_error is not None:
            raise self._recv_error
        raise TimeoutError("no more notifications")

    def methods(self):
        return [m for m, _ in self.requests]

    def params_for(self, method):
        return next(p for m, p in self.requests if m == method)


def run_cli(argv, fake):
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["codex_appserver.py"] + argv):
        with mock.patch.object(cli, "connect", return_value=fake):
            with contextlib.redirect_stdout(out):
                cli.main()
    return out.getvalue()


def turn_notification(turn_id=TURN):
    return {"method": "turn/started", "params": {"threadId": THREAD, "turnId": turn_id}}


BASE = ["--runtime-home", "/tmp/codex-home", "--thread", THREAD]


class CodexAppServerCliTest(unittest.TestCase):
    def test_send_uses_turn_start_and_reports_acceptance_not_completion(self) -> None:
        fake = FakeClient(
            responses={"turn/start": {"turn": {"id": TURN, "status": "inProgress"}}}
        )
        text = run_cli(BASE + ["send", "--text", "hello"], fake)
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
        self.assertEqual(TURN, payload["turn_id"])
        self.assertEqual("inProgress", payload["status"])
        params = fake.params_for("turn/start")
        self.assertEqual(THREAD, params["threadId"])
        self.assertEqual([{"type": "text", "text": "hello"}], params["input"])
        # acceptance only: the CLI must never block for a terminal turn event
        self.assertNotIn("turn/completed", fake.methods())

    def test_steer_sends_expected_turn_id_and_reads_top_level_turnid(self) -> None:
        # TurnSteerResponse is {turnId}, NOT {turn:{id}} — reading result.turn here
        # produced an "accepted" receipt with a null id, which looks like success.
        fake = FakeClient(
            responses={"turn/steer": {"turnId": TURN}},
            notifications=[turn_notification()],
        )
        text = run_cli(BASE + ["steer", "--text", "correction"], fake)
        payload = json.loads(text[text.index("{") : text.rindex("}") + 1])
        self.assertEqual(TURN, payload["turn_id"])
        params = fake.params_for("turn/steer")
        self.assertEqual(TURN, params["expectedTurnId"])
        self.assertEqual(THREAD, params["threadId"])

    def test_steer_refuses_when_no_turn_is_running(self) -> None:
        fake = FakeClient(responses={"turn/steer": {"turnId": TURN}}, notifications=[])
        with self.assertRaises(SystemExit) as caught:
            run_cli(BASE + ["steer", "--text", "x", "--observe", "1"], fake)
        self.assertIn("no running turn", str(caught.exception))
        self.assertNotIn("turn/steer", fake.methods())

    def test_interrupt_sends_both_thread_and_turn_id(self) -> None:
        # TurnInterruptParams requires turnId; threadId alone is rejected and cancels
        # nothing, so the CLI must identify the running turn first.
        fake = FakeClient(responses={"turn/interrupt": {}}, notifications=[turn_notification()])
        run_cli(BASE + ["interrupt"], fake)
        params = fake.params_for("turn/interrupt")
        self.assertEqual(THREAD, params["threadId"])
        self.assertEqual(TURN, params["turnId"])

    def test_interrupt_refuses_when_no_turn_is_running(self) -> None:
        fake = FakeClient(responses={"turn/interrupt": {}}, notifications=[])
        with self.assertRaises(SystemExit) as caught:
            run_cli(BASE + ["interrupt", "--observe", "1"], fake)
        self.assertIn("no running turn", str(caught.exception))
        self.assertNotIn("turn/interrupt", fake.methods())

    def test_raw_tail_emits_complete_json_not_truncated(self) -> None:
        big = {"method": "item/agentMessage/delta", "params": {"delta": "x" * 2000}}
        fake = FakeClient(notifications=[big])
        text = run_cli(BASE + ["tail", "--seconds", "1", "--raw"], fake)
        self.assertIn("x" * 2000, text, "--raw promises verbatim output")

    def test_tail_surfaces_transport_failure_with_a_nonzero_status(self) -> None:
        # An earlier version of this test asserted only the printed text, which masked
        # the fact that tail still exited 0 — a caller could not detect the failure.
        fake = FakeClient(recv_error=ConnectionResetError("peer reset"))
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["codex_appserver.py"] + BASE + ["tail", "--seconds", "2"]):
            with mock.patch.object(cli, "connect", return_value=fake):
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main()
        self.assertEqual(1, caught.exception.code, "transport failure must exit non-zero")
        self.assertIn("[transport]", out.getvalue())
        self.assertIn("ConnectionResetError", out.getvalue())

    def test_server_requests_are_refused_never_approved(self) -> None:
        # Replying result:{} was schema-invalid for approvals and could block a turn;
        # this client must never approve an action on the operator's behalf.
        approval = {"id": 7, "method": "execCommandApproval",
                    "params": {"command": "rm -rf /", "threadId": THREAD}}
        fake = FakeClient(notifications=[approval, turn_notification()])
        run_cli(BASE + ["tail", "--seconds", "2"], fake)
        self.assertEqual(1, len(fake.sent), f"expected exactly one reply, got {fake.sent}")
        reply = fake.sent[0]
        self.assertEqual(7, reply["id"])
        self.assertIn("error", reply, "an approval must be refused, not answered with a result")
        self.assertNotIn("result", reply)
        self.assertNotIn("approved", json.dumps(reply).lower())

    def test_deadline_bounds_the_blocking_socket_read(self) -> None:
        # The clock was only checked BETWEEN reads, so a blocking recv could overshoot
        # by the whole socket timeout: tail --seconds 1 could sit for 120s.
        fake = FakeClient(notifications=[turn_notification()])
        fake.sock = _RecordingSocket()
        run_cli(BASE + ["tail", "--seconds", "2"], fake)
        self.assertTrue(fake.sock.timeouts, "recv must be bounded by the remaining deadline")
        self.assertTrue(
            all(t <= 2.0 for t in fake.sock.timeouts),
            f"socket timeout exceeded the deadline budget: {fake.sock.timeouts}",
        )

    def test_send_refuses_a_response_without_a_turn_id(self) -> None:
        fake = FakeClient(responses={"turn/start": {"turn": {}}})
        with self.assertRaises(SystemExit) as caught:
            run_cli(BASE + ["send", "--text", "x"], fake)
        self.assertIn("no turn id", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
