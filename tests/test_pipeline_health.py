"""Preflight health check: can a packet actually wake this agent right now?

Every case here is a way the lane stopped silently in production on 2026-07-26:
a lease that expired mid-conversation with nothing renewing it, packets written
with a null target while it was expired (permanently unroutable, even after the
lease is fixed), and months of dead probe sessions making any all-sessions verdict
permanently red.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HEALTH_SCRIPT = REPO_ROOT / "bin" / "pipeline_health.py"
OK, WARN, FAIL = "ok", "warn", "fail"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def iso(delta_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    ).isoformat(timespec="seconds")


class PipelineHealthTest(unittest.TestCase):
    def workspace(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="health-", dir="/tmp"))
        write_json(
            root / "collab.config.json",
            {
                "workspace_name": "test-collab",
                "schema_version": 2,
                "workspace_id": "ws_alpha",
                "projects_root": str(root),
                "project_state_root": str(root / "project-state"),
                "poll_interval_seconds": 15,
                "notifications_enabled": False,
            },
        )
        write_json(root / "projects.json", {"projects": [
            {"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}}]})
        write_json(root / "agents.json", {"agents": [{"id": "cdx2", "display_name": "CDX2"}]})
        (root / "agents" / "cdx2").mkdir(parents=True, exist_ok=True)
        write_json(root / "agents" / "cdx2" / "inbox.json",
                   {"agent": "cdx2", "unread": [], "read": []})
        return root

    def add_session(
        self,
        root: Path,
        session_id: str,
        *,
        lease_delta: int,
        status: str = "active",
        family: str = "gemini_cli",
    ) -> None:
        write_json(
            root / "State" / "session_autobridge" / "sessions" / f"{session_id}.json",
            {
                "session_id": session_id,
                "agent_id": "cdx2",
                "project_id": "amiga",
                "chat_id": "CHAT-HEALTH",
                "status": status,
                "mode": "auto-read",
                "wake_strategy": "runtime_trigger",
                "repo_targets": ["app"],
                "lease_expires_utc": iso(lease_delta),
                "runtime": {"family": family, "session_id": f"{session_id}-runtime"},
                "processed_messages": [],
            },
        )

    def add_unread(self, root: Path, name: str, *, target: str | None) -> str:
        rel = f"Chats/2026-07-26_h__CHAT-HEALTH/2026-07-26T00-00-00_to-cdx2_{name}.md"
        lines = [
            "---",
            "chat_id: CHAT-HEALTH",
            "from: claude",
            "to: cdx2",
            "title: packet",
            "priority: normal",
            "project_id: amiga",
            'repo_targets: ["app"]',
            f"target_session_id: {target if target else 'null'}",
            "sent_utc: 2026-07-26T00:00:00+00:00",
            "---",
            "",
            "Body.",
        ]
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        write_json(root / "Chats" / path.parent.name / "meta.json",
                   {"chat_id": "CHAT-HEALTH", "project_id": "amiga"})
        inbox_path = root / "agents" / "cdx2" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(rel)
        write_json(inbox_path, inbox)
        return rel

    def run_health(self, root: Path, *args: str) -> tuple[int, dict]:
        result = subprocess.run(
            [sys.executable, str(HEALTH_SCRIPT), "--agent", "cdx2", "--json", *args],
            cwd=root,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [str(root), str(REPO_ROOT), str(REPO_ROOT / "bin"),
                     os.environ.get("PYTHONPATH", "")]
                ),
            },
        )
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def agent(self, payload: dict) -> dict:
        return payload["agents"][0]

    def check(self, payload: dict, name: str) -> dict:
        matches = [c for c in self.agent(payload)["checks"] if c["check"] == name]
        self.assertEqual(1, len(matches), f"{name}: {matches}")
        return matches[0]

    def test_an_expired_lease_is_reported_as_cannot_wake(self) -> None:
        root = self.workspace()
        self.add_session(root, "SESSION-DEAD", lease_delta=-60)
        code, payload = self.run_health(root)
        self.assertEqual(2, code)
        self.assertFalse(self.agent(payload)["can_wake"])
        self.assertIn("lease_expired", json.dumps(payload))

    def test_one_live_session_makes_the_agent_wakeable_despite_dead_ones(self) -> None:
        """The verdict that matters is 'can a packet land', not 'is every session well'.

        Scoring all sessions together reported FAIL for a lane that was working, because
        the workspace still holds probe sessions that expired months ago. An
        always-red check is one nobody reads.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_session(root, "SESSION-OLD-PROBE", lease_delta=-7_000_000)
        code, payload = self.run_health(root)
        self.assertEqual(0, code)
        self.assertTrue(self.agent(payload)["can_wake"])
        self.assertEqual(["SESSION-LIVE"], self.agent(payload)["wakeable_sessions"])
        self.assertEqual(["SESSION-OLD-PROBE"], self.agent(payload)["unwakeable_sessions"])
        self.assertEqual("warn", self.check(payload, "stale-sessions")["status"])

    def test_a_warning_never_blocks_the_send(self) -> None:
        """A gate that fails on advisory noise gets wrapped in `|| true` and ignored."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_session(root, "SESSION-OLD-PROBE", lease_delta=-7_000_000)
        code, payload = self.run_health(root)
        # The aggregate status also absorbs the ambient watcher check, so this asserts
        # the warning it is about, not the roll-up.
        self.assertEqual("warn", self.check(payload, "stale-sessions")["status"])
        self.assertEqual(0, code, "a warning must not fail the preflight")

    def test_a_lease_below_the_margin_warns_but_still_wakes(self) -> None:
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=300)
        code, payload = self.run_health(root, "--min-lease-seconds", "1800")
        self.assertEqual(0, code)
        lease = [c for c in self.agent(payload)["checks"] if c["check"] == "lease"][0]
        self.assertEqual("warn", lease["status"])
        self.assertLess(lease["remaining_seconds"], 1800)

    def test_unread_packets_with_a_null_target_are_counted_as_unroutable(self) -> None:
        """The durable residue of sending while the lease was expired.

        deliver.py writes target_session_id: null when it cannot validate the target, and
        an exact-receive session then refuses it as route_ambiguous forever. Fixing the
        lease does not rescue those packets, so they have to be visible.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_unread(root, "good", target="SESSION-LIVE-runtime")
        self.add_unread(root, "orphan_one", target=None)
        self.add_unread(root, "orphan_two", target=None)
        code, payload = self.run_health(root)
        self.assertEqual(0, code, "unroutable backlog is a warning, not a block")
        backlog = self.check(payload, "backlog")
        self.assertEqual("warn", backlog["status"])
        self.assertEqual(2, backlog["undeliverable"])
        self.assertEqual({"route_ambiguous": 2}, backlog["reasons"])

    def test_a_fully_routable_backlog_is_not_flagged(self) -> None:
        """Otherwise the backlog check warns forever and means nothing."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_unread(root, "good", target="SESSION-LIVE-runtime")
        code, payload = self.run_health(root)
        self.assertEqual(0, code)
        self.assertEqual("ok", self.check(payload, "backlog")["status"])

    def test_no_registered_session_cannot_wake(self) -> None:
        root = self.workspace()
        code, payload = self.run_health(root)
        self.assertEqual(2, code)
        self.assertFalse(self.agent(payload)["can_wake"])
        self.assertIn("durable only", self.check(payload, "session")["detail"])

    def test_the_watcher_is_advisory_and_never_gates_the_verdict(self) -> None:
        """Contract ruling (codex, 2026-07-26): process and endpoint checks are stale the
        instant they are taken, so gating on them makes the verdict a coin flip under
        exactly the conditions it is consulted. An earlier version folded the watcher
        into can_wake; this pins the reversal so it cannot drift back silently.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        code, payload = self.run_health(root)
        watcher = self.check(payload, "watcher")
        # Deliberately NOT asserting the watcher's own status: it reflects whatever
        # processes happen to exist on this machine, and a test that depends on ambient
        # state is a test that fails for reasons unrelated to the contract. The contract
        # is that its value does not influence the verdict.
        self.assertIn(watcher["status"], (OK, WARN, FAIL))
        self.assertTrue(self.agent(payload)["can_wake"], "session state alone decides")
        self.assertEqual(0, code, "an advisory check must not block a send")
        if watcher["status"] == FAIL:
            self.assertEqual("fail", payload["status"], "still visible in the report")

    def test_a_packet_for_an_unbound_chat_is_not_counted_as_broken(self) -> None:
        """Dispatch filters by project/chat BEFORE the target predicate.

        Without that prefilter every packet in a chat this agent has no session for was
        reported undeliverable -- on a real mailbox, nearly all of them. An alarm that is
        always on is one nobody reads.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        rel = self.add_unread(root, "elsewhere", target=None)
        path = root / rel
        path.write_text(
            path.read_text().replace("chat_id: CHAT-HEALTH", "chat_id: CHAT-OTHER"),
            encoding="utf-8",
        )
        _, payload = self.run_health(root)
        backlog = self.check(payload, "backlog")
        self.assertEqual("ok", backlog["status"])
        self.assertIn("no session registered", backlog["detail"])

    def test_backlog_reasons_come_from_the_router_not_a_local_rule(self) -> None:
        """The reason string must be the router's, so the two cannot drift apart."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_unread(root, "orphan", target=None)
        _, payload = self.run_health(root)
        backlog = self.check(payload, "backlog")
        self.assertEqual({"route_ambiguous": 1}, backlog["reasons"])


if __name__ == "__main__":
    unittest.main()


class ActivityAndWatchTest(unittest.TestCase):
    """The live-view surface: is this worker running right now?

    Binding health cannot answer that. A lease can be valid, a watcher running and an
    endpoint reachable while the worker has been silent for an hour -- which is exactly
    the blindness the operator reported.
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "bin"))
        import pipeline_health

        self.mod = pipeline_health

    def test_activity_wording_is_not_a_health_verdict(self) -> None:
        self.assertEqual("active", self.mod.activity_shape(0))
        self.assertEqual("active", self.mod.activity_shape(self.mod.ACTIVE_WITHIN_SECONDS - 1))
        self.assertEqual("idle", self.mod.activity_shape(self.mod.ACTIVE_WITHIN_SECONDS))
        self.assertEqual("idle", self.mod.activity_shape(99999))

    def probe(self, age_seconds: int) -> dict:
        """Drive the real _activity_check with a thread that last moved age_seconds ago."""
        from unittest import mock

        updated = int(self.mod.now_utc().timestamp()) - age_seconds

        class FakeClient:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def notify(self_inner, *a, **k):
                return None

            def request(self_inner, method, params=None):
                if method == "thread/list":
                    return {"data": [{"id": "thread-x", "updatedAt": updated}]}
                return {}

        with mock.patch.object(
            self.mod, "discover_codex_app_server",
            return_value={"url": "ws://127.0.0.1:1", "token_file": None},
        ), mock.patch.object(
            self.mod, "_codex_app_server_token", return_value=None
        ), mock.patch.object(
            self.mod, "JsonRpcWebSocketClient", lambda *a, **k: FakeClient()
        ):
            return self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "thread-x",
                             "home": "/home"}}
            )

    def test_a_long_idle_worker_is_reported_but_never_failed(self) -> None:
        """Quiet is not broken. Failing on it would make the view cry wolf hourly.

        The first version of this test built the result dict by hand, so it asserted
        nothing about _activity_check and survived a mutation that failed every idle
        worker.
        """
        check = self.probe(9999)
        self.assertEqual(self.mod.OK, check["status"])
        self.assertIn("idle", check["detail"])
        self.assertEqual(9999, check["idle_seconds"])

    def test_a_currently_running_worker_reads_as_active(self) -> None:
        check = self.probe(3)
        self.assertEqual(self.mod.OK, check["status"])
        self.assertIn("active", check["detail"])

    def test_a_thread_the_app_server_does_not_list_is_flagged(self) -> None:
        from unittest import mock

        class FakeClient:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *exc): return False
            def notify(self_inner, *a, **k): return None
            def request(self_inner, method, params=None):
                return {"data": []} if method == "thread/list" else {}

        with mock.patch.object(
            self.mod, "discover_codex_app_server",
            return_value={"url": "ws://127.0.0.1:1", "token_file": None},
        ), mock.patch.object(
            self.mod, "_codex_app_server_token", return_value=None
        ), mock.patch.object(
            self.mod, "JsonRpcWebSocketClient", lambda *a, **k: FakeClient()
        ):
            check = self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "ghost", "home": "/h"}}
            )
        self.assertEqual(self.mod.WARN, check["status"])
        self.assertIn("not listed", check["detail"])

    def test_a_family_without_a_thread_probe_is_not_guessed_at(self) -> None:
        for family in ("zcode_cli", "claude_app", "gemini_cli", ""):
            with self.subTest(family=family):
                check = self.mod._activity_check(
                    {"runtime": {"family": family, "session_id": "x"}}
                )
                self.assertEqual(self.mod.OK, check["status"])
                self.assertIn("no thread probe", check["detail"])

    def test_an_unreachable_endpoint_does_not_fail_the_activity_check(self) -> None:
        """The endpoint check already reports that; two failures for one cause is noise."""
        from unittest import mock

        with mock.patch.object(self.mod, "discover_codex_app_server", return_value=None):
            check = self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "x", "home": "/nope"}}
            )
        self.assertEqual(self.mod.OK, check["status"])

    def test_the_tool_stays_a_one_shot_check_not_a_live_view(self) -> None:
        """A refreshing terminal view was built here and removed.

        The operator's blindness is about a surface they actually watch, and a
        terminal is not one -- that need is llm-collab#319's Pi workers, whose UI
        streams natively. Keeping a half-answer here would have been scope with no
        reader.
        """
        result = subprocess.run(
            [sys.executable, str(HEALTH_SCRIPT), "--agent", "cdx2", "--watch", "5"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unrecognized arguments", result.stderr)
