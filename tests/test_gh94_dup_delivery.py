"""GH-94 bounded slice: one packet to Codex must cause at most one delivery.

The app-server path delivered a packet 2-3x. These prove the three defects are
fixed without claiming canonical at-most-once:

1. seen_paths (announced set) is committed BEFORE dispatch in watch_inbox, so a
   dispatch exception cannot re-announce/re-dispatch the poll (source-pinned;
   the loop is a `while True` that resists direct invocation).
2. After turn/start acceptance the packet is delivered: a lost reply view
   (timeout, dropped connection) or a non-completed terminal returns
   returncode 0 (delivered-but-unobserved), never raising and never retried.
   Only a pre-acceptance failure stays retryable.
3. A valid-JSON non-object frame after acceptance is rejected in the
   observation boundary instead of raising AttributeError past the catch.

Each behavioural case has a matching mutation in test_gh94_mutation_proofs.sh.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _session_autobridge as sab


SESSION = {
    "session_id": "SESSION-CODEX-TEST",
    "agent_id": "codex",
    "runtime": {"family": "codex_app", "session_id": "codex-thread-1", "timeout_seconds": 5},
}
MESSAGE = {"path": "/chat/to-codex.md", "frontmatter": {"title": "t", "from": "claude"}}


class FakeClient:
    """A stand-in for JsonRpcWebSocketClient. `recv_script` is a list of frames
    to return, or exceptions to raise, in order. turn_start_ok=False makes the
    turn/start request itself raise (pre-acceptance)."""

    def __init__(self, recv_script, *, turn_start_ok=True):
        self._recv = list(recv_script)
        self._turn_start_ok = turn_start_ok
        self.turn_start_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_deadline(self, deadline):
        pass

    def request(self, method, params=None):
        if method == "turn/start":
            self.turn_start_calls += 1
            if not self._turn_start_ok:
                raise ConnectionError("turn/start not accepted")
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        if method == "model/list":
            return {"data": [{"id": "gpt-test", "isDefault": True}]}
        return {}

    def notify(self, *a, **k):
        pass

    def recv_json(self):
        if not self._recv:
            raise TimeoutError("no more frames")
        item = self._recv.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _run(recv_script, *, turn_start_ok=True):
    client = FakeClient(recv_script, turn_start_ok=turn_start_ok)
    endpoint = {"url": "ws://127.0.0.1:1/", "token_file": None, "pid": 1, "source": "test"}
    with patch.object(sab, "discover_codex_app_server", return_value=endpoint), \
         patch.object(sab, "_codex_app_server_token", return_value=None), \
         patch.object(sab, "build_resume_prompt", return_value="prompt"), \
         patch.object(sab, "JsonRpcWebSocketClient", return_value=client):
        result = sab.execute_codex_app_server_trigger(SESSION, MESSAGE, None)
    return result, client


def _completed(text="done"):
    return {
        "method": "turn/completed",
        "params": {"turn": {"id": "turn-1", "status": "completed"},
                   "item": {"type": "agentMessage", "text": text}},
    }


class PostAcceptanceIsDeliveredTest(unittest.TestCase):
    def test_completed_turn_is_observed_and_delivered(self):
        result, client = _run([_completed("hi")])
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["delivery_observed"])
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(client.turn_start_calls, 1)

    def test_timeout_after_acceptance_is_delivered_unobserved(self):
        # defect #2: recipient already has the packet; a lost view must NOT raise
        # and must report delivered (returncode 0), never a retry.
        result, _ = _run([TimeoutError("deadline")])
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["delivery_observed"])
        self.assertEqual(result["terminal_status"], "unobserved")

    def test_dropped_connection_after_acceptance_is_delivered(self):
        result, _ = _run([ConnectionError("peer reset")])
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["delivery_observed"])
        self.assertEqual(result["terminal_status"], "unobserved")

    def test_non_object_frame_after_acceptance_does_not_raise(self):
        # defect #3: `[]` would hit .get on a list -> AttributeError past the
        # catch. It must be skipped; the turn then times out as unobserved.
        result, _ = _run([[], TimeoutError("deadline")])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["terminal_status"], "unobserved")

    def test_failed_turn_after_acceptance_is_still_delivered_not_retried(self):
        failed = {"method": "turn/failed",
                  "params": {"turn": {"id": "turn-1", "status": "failed",
                                      "error": {"message": "boom"}}}}
        result, _ = _run([failed])
        self.assertEqual(result["returncode"], 0)  # delivered => no retry
        self.assertTrue(result["delivery_observed"])
        self.assertEqual(result["terminal_status"], "failed")

    def test_unaccepted_turn_start_is_not_a_delivery(self):
        # pre-acceptance failure must propagate so it stays retryable / AX-eligible.
        with self.assertRaises(ConnectionError):
            _run([], turn_start_ok=False)

    def test_one_packet_causes_at_most_one_turn_start(self):
        for script in ([TimeoutError("t")], [ConnectionError("c")],
                       [[], TimeoutError("t")], [_completed()]):
            _, client = _run(script)
            self.assertEqual(client.turn_start_calls, 1)


class SeenPathsCommittedBeforeDispatchTest(unittest.TestCase):
    """defect #1, source-pinned: the announced set must be committed before the
    dispatch call, or a dispatch exception replays the whole poll."""

    def test_seen_paths_commit_precedes_dispatch_call(self):
        src = (REPO_ROOT / "bin" / "watch_inbox.py").read_text()
        commit = src.index("seen_paths = seen_paths | new_msgs")
        # The dispatch happens inside this guard block in the poll loop.
        dispatch_block = src.index("if not args.session and not args.no_autobridge:")
        self.assertLess(
            commit, dispatch_block,
            "seen_paths must be committed BEFORE the dispatch block (GH-94)",
        )
        # And it must not ALSO be committed after dispatch (the old bug site).
        self.assertEqual(
            src.count("seen_paths = seen_paths | new_msgs"), 1,
            "exactly one seen_paths commit, before dispatch",
        )


if __name__ == "__main__":
    unittest.main()
