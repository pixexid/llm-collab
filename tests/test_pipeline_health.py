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

    def add_binding(self, root: Path, session_id: str, *, family: str = "gemini_cli") -> None:
        """The exact binding the router requires; a session alone resolves to no pair."""
        write_json(
            root / "State" / "session_autobridge" / "bindings" / "amiga"
            / "CHAT-HEALTH" / "cdx2.json",
            {
                "project_id": "amiga",
                "chat_id": "CHAT-HEALTH",
                "agent_id": "cdx2",
                "session_id": session_id,
                "runtime_session_id": f"{session_id}-runtime",
                "runtime_family": family,
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

    def run_presend(self, root, *args: str) -> tuple[int, dict]:
        """Like run_health, but without the implicit --agent, so mode flags are explicit."""
        result = subprocess.run(
            [sys.executable, str(HEALTH_SCRIPT), *args, "--json"],
            cwd=root, text=True, capture_output=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(
                [str(root), str(REPO_ROOT), str(REPO_ROOT / "bin"),
                 os.environ.get("PYTHONPATH", "")])},
        )
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def presend_cli(self, root, *args: str):
        return subprocess.run(
            [sys.executable, str(HEALTH_SCRIPT), *args],
            cwd=root, text=True, capture_output=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(
                [str(root), str(REPO_ROOT), str(REPO_ROOT / "bin"),
                 os.environ.get("PYTHONPATH", "")])},
        )

    def workspace_with_two_sessions(self):
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_session(root, "SESSION-DEAD", lease_delta=-600)
        return root

    def presend_checks(self, payload: dict) -> dict:
        return {c["check"]: c for c in payload["agents"][0]["checks"]}

    def add_agent(self, root: Path, agent_id: str, **fields) -> None:
        agents_path = root / "agents.json"
        agents = json.loads(agents_path.read_text())
        agents["agents"].append({"id": agent_id, "display_name": agent_id.upper(), **fields})
        write_json(agents_path, agents)

    def test_a_pre_send_check_blocks_when_nothing_polls_the_inbox(self) -> None:
        """The watcher check says in its own words that nobody will read the packet.

        The agent-wide report keeps this advisory because it answers "is the lane
        configured". This report answers "may I send THIS packet now", and returning exit
        0 with can_wake=true while no watcher is polling is the false green the tool
        exists to prevent. That the process could exit a moment after inspection is true
        of every preflight check.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        code, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        checks = self.presend_checks(payload)
        self.assertEqual(FAIL, checks["watcher"]["status"])
        self.assertFalse(payload["agents"][0]["can_wake"])
        self.assertEqual(2, code)

    def test_a_packet_outside_the_session_repo_scope_is_refused(self) -> None:
        """The pair resolver validates identity; delivery also applies repo scope."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        _, mismatched = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2",
            "--repo-targets", "web")
        self.assertEqual(FAIL, self.presend_checks(mismatched)["repo-scope"]["status"])
        self.assertFalse(mismatched["agents"][0]["can_wake"])

        _, matched = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2",
            "--repo-targets", "app")
        self.assertEqual(OK, self.presend_checks(matched)["repo-scope"]["status"])

    def test_an_unscoped_pre_send_check_says_it_did_not_check_the_scope(self) -> None:
        """Silence about a check that was not run is how a false green reads as a pass."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        _, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        scope = self.presend_checks(payload)["repo-scope"]
        self.assertEqual(OK, scope["status"])
        self.assertIn("--repo-targets", scope["detail"])

    def test_a_session_that_would_not_wake_the_runtime_is_not_wakeable(self) -> None:
        """`session_is_dispatchable` checks status and lease, not the wake action.

        A `mode: manual` session records the packet in processed_messages without waking
        anything, and it is not re-dispatched once the mode is fixed.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-MANUAL", lease_delta=3600)
        path = root / "State" / "session_autobridge" / "sessions" / "SESSION-MANUAL.json"
        session = json.loads(path.read_text())
        session["mode"] = "manual"
        write_json(path, session)
        self.add_binding(root, "SESSION-MANUAL")
        _, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        action = self.presend_checks(payload)["wake-action"]
        self.assertEqual(FAIL, action["status"])
        self.assertIn("manual_noop", action["detail"])
        self.assertFalse(payload["agents"][0]["can_wake"])

    def test_a_disabled_recipient_is_refused_before_anything_else(self) -> None:
        """deliver.py refuses a disabled recipient before it resolves the chat."""
        root = self.workspace()
        agents_path = root / "agents.json"
        agents = json.loads(agents_path.read_text())
        agents["agents"][0]["disabled"] = True
        write_json(agents_path, agents)
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        _, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        agent_check = self.presend_checks(payload)["agent"]
        self.assertEqual(FAIL, agent_check["status"])
        self.assertFalse(payload["agents"][0]["can_wake"])

    def test_an_empty_scope_is_a_mistake_not_a_wildcard(self) -> None:
        """`--project "$PROJECT"` with the variable unset used to mean "every project".

        Both booleans were false, the command silently entered agent-wide inventory mode,
        and a live session for any project produced exit 0 -- while the caller believed an
        exact pair had been checked.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        for args in (
            ("--project", "", "--chat", "CHAT-HEALTH", "--agent", "cdx2"),
            ("--project", "amiga", "--chat", "", "--agent", "cdx2"),
            ("--project", "", "--chat", "", "--agent", "cdx2"),
        ):
            with self.subTest(args=args):
                result = self.presend_cli(root, *args)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("empty", result.stderr)

    def test_the_nearest_problem_names_the_check_that_actually_failed(self) -> None:
        """Re-running the lease check reported a healthy lease as the nearest problem.

        For a stopped session with an unexpired lease the diagnostic said "valid for
        ...", hiding the dispatchability failure and sending the worker to repair
        something that was not broken.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-STOPPED", lease_delta=3600, status="stopped")
        _, payload = self.run_health(root)
        session_check = self.check(payload, "session")
        self.assertEqual(FAIL, session_check["status"])
        self.assertNotIn("valid for", session_check["detail"])
        self.assertIn("dispatchable", session_check["failing_checks"])

    def test_the_entry_point_comes_after_every_test_class(self) -> None:
        """A mid-file `unittest.main()` exits before the classes below it are defined.

        `python tests/test_pipeline_health.py` then reported a confident green with an
        entire class absent, while discovery-based runs happened to include it, which is
        what hid it. Checked structurally rather than by running this file again -- that
        would recurse through this very case.
        """
        import ast

        source = Path(__file__).resolve().read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        mains = [
            n for n in tree.body
            if isinstance(n, ast.If) and ast.dump(n.test).find("__main__") != -1
        ]
        self.assertTrue(classes)
        self.assertEqual(1, len(mains), "exactly one __main__ guard")
        self.assertGreater(
            mains[0].lineno, classes[-1].lineno,
            f"the entry point runs before {classes[-1].name} is defined, so every test "
            "in it is silently skipped on a direct run",
        )

    def test_a_backlog_is_never_absolved_by_a_session_that_cannot_receive(self) -> None:
        """`message_targets_session` answers addressing, not wakeability.

        Passing every session in when none was live let a dead session "match" a packet,
        so a fully broken lane reported an empty backlog -- suppressing the one line that
        would have shown the packets were stuck.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-DEAD", lease_delta=-600)
        self.add_unread(root, "stuck", target="SESSION-DEAD-runtime")
        _, payload = self.run_health(root)
        backlog = self.check(payload, "backlog")
        self.assertIn("no session to route them to", backlog["detail"])
        self.assertFalse(payload["agents"][0]["can_wake"])

    def test_agent_wide_mode_is_an_inventory_not_a_send_verdict(self) -> None:
        """Kept, but it must not be the thing consulted before a send."""
        root = self.workspace_with_two_sessions()
        code, payload = self.run_presend(root, "--agent", "cdx2")
        self.assertEqual(0, code)
        self.assertTrue(payload["agents"][0]["can_wake"])
        # It reports BOTH, which is the honest inventory answer.
        self.assertEqual(["SESSION-LIVE"], payload["agents"][0]["wakeable_sessions"])
        self.assertEqual(["SESSION-DEAD"], payload["agents"][0]["unwakeable_sessions"])

    def test_pre_send_mode_refuses_when_the_exact_pair_is_not_wakeable(self) -> None:
        """Codex's reproduction: a live sibling session is not evidence for this packet."""
        root = self.workspace_with_two_sessions()
        code, payload = self.run_presend(
            root, "--agent", "cdx2", "--project", "amiga", "--chat", "CHAT-NOT-BOUND")
        report = payload["agents"][0]
        self.assertEqual(2, code, report)
        self.assertFalse(report["can_wake"])
        pair = next(c for c in report["checks"] if c["check"] == "exact-pair")
        self.assertEqual("fail", pair["status"])
        self.assertIn("not evidence", pair["detail"])

    def test_pre_send_mode_requires_both_project_and_chat(self) -> None:
        root = self.workspace_with_two_sessions()
        for args in (("--project", "amiga"), ("--chat", "CHAT-X")):
            with self.subTest(args=args):
                result = self.presend_cli(root, "--agent", "cdx2", *args)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pre-send pair", result.stderr)

    def test_pre_send_mode_refuses_all_and_multiple_agents(self) -> None:
        """A send has exactly one recipient; a fan-out verdict would be meaningless."""
        root = self.workspace_with_two_sessions()
        for extra in (["--all"], ["--agent", "other"]):
            with self.subTest(extra=extra):
                result = self.presend_cli(root, "--agent", "cdx2",
                                          "--project", "amiga", "--chat", "CHAT-X", *extra)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("exactly one --agent", result.stderr)

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

    def test_an_unreachable_endpoint_does_not_make_a_session_unwakeable(self) -> None:
        """The advertised rule, now actually tested.

        The return comment says can_wake is keyed to session state alone, because a live
        observation is stale the instant it is taken -- but endpoint FAIL was
        participating in the `live` classification, so an active leased session with an
        unreachable app-server reported can_wake=False. Code and comment disagreed and
        the advertised rule was untested.

        No mock: a codex_app session whose home has no app-server fails the endpoint
        probe on its own.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600, family="codex_app")
        code, payload = self.run_health(root)
        report = self.agent(payload)
        endpoint = self.check(payload, "endpoint")
        self.assertEqual("fail", endpoint["status"], "still reported")
        self.assertTrue(report["can_wake"], "but advisory, exactly as documented")
        self.assertEqual(["SESSION-LIVE"], report["wakeable_sessions"])
        self.assertEqual(0, code, "an advisory failure must not block a send")

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

    def probe_row(self, row, *, fail_stage: str | None = None) -> dict:
        """Drive _activity_check with an arbitrary thread/list row, or a failing stage."""
        from unittest import mock

        class FakeClient:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def notify(self_inner, *a, **k):
                return None

            def request(self_inner, method, params=None):
                if fail_stage and method == fail_stage:
                    raise RuntimeError("app-server said no")
                if method == "thread/list":
                    return {"data": [row] if row is not None else []}
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

    def test_a_malformed_activity_timestamp_warns_instead_of_reading_healthy(self) -> None:
        """`int(None or 0)` dated the thread to 1970 and still reported ok.

        A non-numeric value raised outside the handler and aborted the whole preflight,
        and a future timestamp produced a negative age that read as "active". A row that
        did not answer the question is not a healthy answer to it.
        """
        cases = {
            "missing": {"id": "thread-x"},
            "null": {"id": "thread-x", "updatedAt": None},
            "text": {"id": "thread-x", "updatedAt": "yesterday"},
            "container": {"id": "thread-x", "updatedAt": {"seconds": 5}},
        }
        for name, row in cases.items():
            with self.subTest(row=name):
                check = self.probe_row(row)
                self.assertEqual(self.mod.WARN, check["status"], check)

    def test_a_future_activity_timestamp_is_not_reported_as_active(self) -> None:
        future = int(self.mod.now_utc().timestamp()) + 86_400
        check = self.probe_row({"id": "thread-x", "updatedAt": future})
        self.assertEqual(self.mod.WARN, check["status"])
        self.assertIn("future", check["detail"])

    def test_a_rejected_initialization_fails_instead_of_warning(self) -> None:
        """Real dispatch performs the same initialize, so a rejection is not advisory.

        Degrading it to WARN reported a lane nothing can wake as sendable, because only
        FAIL blocks.
        """
        check = self.probe_row({"id": "thread-x", "updatedAt": 0}, fail_stage="initialize")
        self.assertEqual(self.mod.FAIL, check["status"])
        self.assertIn("initialize", check["detail"])

    def test_a_rejected_thread_list_stays_advisory(self) -> None:
        """The other half of the same rule: an optional probe failing is not a blocker."""
        check = self.probe_row({"id": "thread-x", "updatedAt": 0}, fail_stage="thread/list")
        self.assertEqual(self.mod.WARN, check["status"])

    def test_the_probe_follows_the_endpoint_dispatch_would_choose(self) -> None:
        """A home-scoped probe passed while the watcher would use a stale env override.

        `execute_codex_app_server_trigger` resolves the endpoint with
        LLM_COLLAB_CODEX_APP_SERVER_URL honoured, so a preflight that ignored it reported
        healthy for a different server than the one that would be dispatched to.
        """
        from unittest import mock

        seen = {}

        def fake_discover(home, **kwargs):
            seen.update({"home": home, "kwargs": kwargs})
            return None

        with mock.patch.object(self.mod, "discover_codex_app_server", fake_discover):
            self.mod._endpoint_check(
                {"runtime": {"family": "codex_app", "session_id": "t", "home": "/home"}})
        self.assertNotIn("allow_unscoped_env", seen["kwargs"],
                         "the probe must not opt out of the override dispatch honours")

    def test_the_session_scan_fails_closed_rather_than_reporting_a_partial_inventory(self) -> None:
        """A partial inventory looks exactly like a complete one.

        The directory is untrusted input and was enumerated without any budget, so a
        pathological one could stall the preflight or exhaust memory. The charge is at the
        enumeration boundary, before the agent filter, because filtering first hides the
        cost of the entries that were rejected.
        """
        from unittest import mock

        class ManyPaths:
            def glob(self_inner, pattern):
                return [Path(f"/tmp/nonexistent/sess_{i}.json") for i in range(20)]

        with mock.patch.object(self.mod, "SESSIONS_DIR", ManyPaths()), \
                mock.patch.object(self.mod, "SESSION_SCAN_LIMIT", 5), \
                mock.patch.object(self.mod, "load_session", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                self.mod._sessions_for("cdx2")
        self.assertIn("refusing to scan further", str(caught.exception))

    def test_a_normal_session_directory_is_well_under_the_budget(self) -> None:
        """The budget must not be so tight that ordinary use trips it."""
        from unittest import mock

        class FewPaths:
            def glob(self_inner, pattern):
                return [Path(f"/tmp/nonexistent/sess_{i}.json") for i in range(50)]

        with mock.patch.object(self.mod, "SESSIONS_DIR", FewPaths()), \
                mock.patch.object(self.mod, "load_session", return_value=None):
            self.assertEqual([], self.mod._sessions_for("cdx2"))

    def watcher_with_listing(self, listing: str) -> dict:
        from unittest import mock

        class Result:
            stdout = listing

        with mock.patch.object(self.mod.subprocess, "run", return_value=Result()):
            return self.mod._watcher_check("cdx2")

    def test_a_watcher_polling_another_checkout_is_not_this_workspaces_watcher(self) -> None:
        """Matching the basename counted someone else's mailbox poller as ours.

        The check passed while nothing read packets written here, which is the exact
        false green this tool exists to prevent.
        """
        foreign = "/Users/x/other-collab/bin/watch_inbox.py --me cdx2 --json"
        check = self.watcher_with_listing(f"/bin/zsh\n{foreign}\n")
        self.assertEqual(self.mod.FAIL, check["status"])
        self.assertIn("another checkout", check["detail"])

    def test_a_watcher_from_this_workspace_counts(self) -> None:
        mine = f"python3 {self.mod.ROOT / 'bin' / 'watch_inbox.py'} --me cdx2 --json"
        check = self.watcher_with_listing(f"/bin/zsh\n{mine}\n")
        self.assertEqual(self.mod.OK, check["status"])

    def test_the_unread_queue_is_bounded_like_the_session_directory(self) -> None:
        """The other untrusted enumeration, read and parsed in full."""
        from unittest import mock

        many = [{"path": f"Chats/x/{i}.md", "frontmatter": {}, "body": ""} for i in range(20)]
        with mock.patch.object(self.mod, "get_unread_messages", return_value=many), \
                mock.patch.object(self.mod, "UNREAD_SCAN_LIMIT", 5):
            with self.assertRaises(RuntimeError) as caught:
                self.mod._backlog_check("cdx2", [])
        self.assertIn("partial backlog", str(caught.exception))

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

    def test_the_probe_sends_nothing_when_a_server_request_arrives(self) -> None:
        """Observation must emit no frame at all.

        The client's default policy is SERVER_REQUEST_REFUSE, which SENDS a correlated
        error. A pending server request can be resolved by whichever client answers
        first, so a "read-only" probe answering one would abort work the operator
        started in the desktop app -- the same contract codex_stream.py carries.
        """
        from unittest import mock

        captured = {}

        class RecordingClient:
            def __init__(self, url, token=None, timeout_seconds=None,
                         server_request_policy=None):
                captured["policy"] = server_request_policy

            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def notify(self, *a, **k): return None
            def request(self, method, params=None):
                return {"data": []} if method == "thread/list" else {}

        with mock.patch.object(
            self.mod, "discover_codex_app_server",
            return_value={"url": "ws://127.0.0.1:1", "token_file": None},
        ), mock.patch.object(self.mod, "_codex_app_server_token", return_value=None), \
             mock.patch.object(self.mod, "JsonRpcWebSocketClient", RecordingClient):
            self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "t", "home": "/h"}}
            )
        self.assertEqual(
            self.mod.SERVER_REQUEST_IGNORE, captured["policy"],
            "the refuse default sends an error frame and can abort operator work",
        )

    def test_a_jsonrpc_error_degrades_to_warn_rather_than_crashing(self) -> None:
        """request() raises RuntimeError for an error reply; it escaped the whole preflight."""
        from unittest import mock

        class ErroringClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def notify(self, *a, **k): return None
            def request(self, method, params=None):
                if method == "thread/list":
                    raise RuntimeError("method not supported")
                return {}

        with mock.patch.object(
            self.mod, "discover_codex_app_server",
            return_value={"url": "ws://127.0.0.1:1", "token_file": None},
        ), mock.patch.object(self.mod, "_codex_app_server_token", return_value=None), \
             mock.patch.object(self.mod, "JsonRpcWebSocketClient", ErroringClient):
            check = self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "t", "home": "/h"}}
            )
        self.assertEqual(self.mod.WARN, check["status"])
        self.assertIn("RuntimeError", check["detail"])

    def test_a_malformed_thread_list_result_is_validated_before_indexing(self) -> None:
        from unittest import mock

        for bad in ([], "nope", None, {"data": "not-a-list"}):
            with self.subTest(result=bad):
                class OddClient:
                    def __init__(self, *a, **k): pass
                    def __enter__(self): return self
                    def __exit__(self, *exc): return False
                    def notify(self, *a, **k): return None
                    def request(self, method, params=None):
                        return bad if method == "thread/list" else {}

                with mock.patch.object(
                    self.mod, "discover_codex_app_server",
                    return_value={"url": "ws://127.0.0.1:1", "token_file": None},
                ), mock.patch.object(self.mod, "_codex_app_server_token", return_value=None), \
                     mock.patch.object(self.mod, "JsonRpcWebSocketClient", OddClient):
                    check = self.mod._activity_check(
                        {"runtime": {"family": "codex_app", "session_id": "t", "home": "/h"}}
                    )
                self.assertEqual(self.mod.WARN, check["status"])

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


# Last, after every class. Sitting mid-file, this ran and exited before
# ActivityAndWatchTest was even defined, so `python tests/test_pipeline_health.py`
# reported a confident green with the whole activity-probe, malformed-shape,
# server-request-policy and RuntimeError class absent. Discovery-based runs happened to
# include them, which is what hid it.
if __name__ == "__main__":
    unittest.main()
