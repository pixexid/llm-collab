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

    def test_an_observation_never_gates_the_exit_status(self) -> None:
        """Observations report; they do not gate.

        The mailbox is durable-first, so `deliver.py` writes the packet regardless and its
        own result plus the watcher events are the authority. An aggregate green here could
        only ever be a second implementation of delivery, and every review round found
        another predicate it was missing. Exit nonzero is for an invocation this command
        could not carry out. (Codex's scope ruling, 2026-07-26.)
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        code, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        self.assertEqual(0, code)
        self.assertIn("watcher", self.presend_checks(payload))
        self.assertNotIn("can_wake", payload["agents"][0])

    def test_a_packet_outside_the_session_repo_scope_is_refused(self) -> None:
        """The pair resolver validates identity; delivery also applies repo scope."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        _, mismatched = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2",
            "--repo-targets", "web")
        self.assertEqual(FAIL, self.presend_checks(mismatched)["repo-scope"]["status"])

        _, matched = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2",
            "--repo-targets", "app")
        self.assertEqual(OK, self.presend_checks(matched)["repo-scope"]["status"])

    def test_an_omitted_scope_is_checked_as_the_empty_list_delivery_would_use(self) -> None:
        """`deliver.py` represents an omitted scope as `repo_targets: []` and still runs
        the predicate, so reporting OK for a check that was not run was a false green.

        A session scoped to `app` refuses an unscoped packet there, so it must refuse here.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        _, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        scope = self.presend_checks(payload)["repo-scope"]
        self.assertEqual(FAIL, scope["status"])

    def test_a_session_that_would_not_wake_the_runtime_is_reported_as_such(self) -> None:
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
        self.assertIn("no dispatchable session to route them to", backlog["detail"])

    def test_a_stranded_packet_is_reported_while_the_lane_is_down(self) -> None:
        """The permanent residue must not disappear exactly when it matters.

        Classifying the backlog only against dispatchable sessions made packets that no
        REGISTERED session would accept invisible while nothing was live -- they surfaced
        as warnings only after a session recovered, which is the opposite of useful.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-DEAD", lease_delta=-600)
        self.add_unread(root, "stranded", target="SOMEONE-ELSES-RUNTIME")
        _, payload = self.run_health(root)
        backlog = self.check(payload, "backlog")
        self.assertEqual(WARN, backlog["status"])
        self.assertEqual(1, backlog["undeliverable"])
        self.assertIn("stay unroutable after the lane recovers", backlog["detail"])

    def test_the_exact_resolver_gets_the_bounded_snapshot(self) -> None:
        """The resolver falls back to an UNBOUNDED scan when given no snapshot.

        The budget lived in `_sessions_for`, which the exact path never calls, so it was
        absent from the one mode a worker consults about a specific packet.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        driver = root / "drive.py"
        driver.write_text(
            "\n".join([
                "import sys, json",
                f"sys.path.insert(0, {str(REPO_ROOT / 'bin')!r})",
                "import _session_autobridge as ab",
                "def boom(*a, **k):",
                "    raise AssertionError('unbounded iter_sessions must not be used')",
                "ab.iter_sessions = boom",
                "import pipeline_health",
                "pipeline_health.iter_sessions = boom",
                "report = pipeline_health.target_report(",
                "    'amiga', 'CHAT-HEALTH', 'cdx2', min_lease_seconds=1800)",
                "print(json.dumps({c['check']: c['status'] for c in report['checks']}))",
            ]),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(driver)], cwd=root, text=True, capture_output=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(
                [str(root), str(REPO_ROOT), str(REPO_ROOT / "bin"),
                 os.environ.get("PYTHONPATH", "")])},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(OK, json.loads(result.stdout)["exact-pair"])

    def test_an_unreadable_binding_is_a_finding_not_a_traceback(self) -> None:
        """`deliver.py` treats this as a specific blocker; escaping it emits no JSON.

        Automation asking for the diagnostic shape got a stack trace instead, which is
        worse than a wrong answer because there is no answer at all.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        binding = (root / "State" / "session_autobridge" / "bindings" / "amiga"
                   / "CHAT-HEALTH" / "cdx2.json")
        binding.parent.mkdir(parents=True, exist_ok=True)
        binding.write_bytes(b'{"project_id": "amiga", "pad": "' + b"x" * 400_000 + b'"}')
        code, payload = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        pair = self.presend_checks(payload)["exact-pair"]
        self.assertEqual(FAIL, pair["status"])
        self.assertIn(
            "binding unreadable", pair["detail"],
            "the oversized binding must be reported as unreadable, not as a mismatch",
        )
        self.assertEqual(0, code)

    def test_no_report_carries_an_aggregate_verdict(self) -> None:
        """Dropping the exit gate while keeping the roll-up moved the verdict, not removed it.

        `report["status"] == "ok"` reads as permission to send however the exit code
        behaves, so there is no report-wide `status` at all -- only the worst single
        observation, named as such.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        for args in (
            ("--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2"),
            ("--agent", "cdx2"),
        ):
            with self.subTest(args=args):
                _, payload = self.run_presend(root, *args)
                self.assertNotIn("status", payload)
                self.assertNotIn("status", payload["agents"][0])
                self.assertIn("worst_observation", payload["agents"][0])

    def test_an_unregistered_project_is_refused_before_the_lookup(self) -> None:
        """`deliver.py` refuses at `ensure_project` before it resolves a chat.

        Binding resolution took the CLI project on trust, so a typo -- or stale state for a
        removed project -- could resolve a live pair under an ID the workspace does not
        register and be reported as an ordinary observation.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_binding(root, "SESSION-LIVE")
        _, payload = self.run_presend(
            root, "--project", "amigo", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        project = self.presend_checks(payload)["project"]
        self.assertEqual(FAIL, project["status"])
        self.assertIn("not in projects.json", project["detail"])

    def test_inventory_mode_validates_the_agent_too(self) -> None:
        """Only exact mode checked the recipient, so a disabled agent read as dispatchable."""
        root = self.workspace()
        agents_path = root / "agents.json"
        agents = json.loads(agents_path.read_text())
        agents["agents"][0]["disabled"] = True
        write_json(agents_path, agents)
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        _, payload = self.run_health(root)
        self.assertEqual(FAIL, self.check(payload, "agent")["status"])

    def test_a_chat_whose_only_sessions_are_dead_is_not_called_unbound(self) -> None:
        """Two different facts were reported as one.

        A packet for a chat whose only registered session is expired is not "a chat with no
        session" -- and folding it into that note hid it entirely whenever some other chat
        had a live session.
        """
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        path = root / "State" / "session_autobridge" / "sessions" / "SESSION-DEADCHAT.json"
        write_json(path, {
            "session_id": "SESSION-DEADCHAT", "agent_id": "cdx2", "project_id": "amiga",
            "chat_id": "CHAT-OTHER", "status": "active", "mode": "auto-read",
            "wake_strategy": "runtime_trigger", "repo_targets": ["app"],
            "lease_expires_utc": iso(-600),
            "runtime": {"family": "gemini_cli", "session_id": "SESSION-DEADCHAT-runtime"},
            "processed_messages": [],
        })
        rel = "Chats/2026-07-26_o__CHAT-OTHER/2026-07-26T00-00-00_to-cdx2_p.md"
        packet = root / rel
        packet.parent.mkdir(parents=True, exist_ok=True)
        packet.write_text("\n".join([
            "---", "chat_id: CHAT-OTHER", "from: claude", "to: cdx2", "title: packet",
            "priority: normal", "project_id: amiga", 'repo_targets: ["app"]',
            "target_session_id: SESSION-DEADCHAT-runtime",
            "sent_utc: 2026-07-26T00:00:00+00:00", "---", "", "Body.",
        ]), encoding="utf-8")
        write_json(root / "Chats" / packet.parent.name / "meta.json",
                   {"chat_id": "CHAT-OTHER", "project_id": "amiga"})
        inbox_path = root / "agents" / "cdx2" / "inbox.json"
        inbox = json.loads(inbox_path.read_text())
        inbox["unread"].append(rel)
        write_json(inbox_path, inbox)

        _, payload = self.run_health(root)
        backlog = self.check(payload, "backlog")
        self.assertIn("CHAT-OTHER", backlog.get("dead_chats", []))
        self.assertIn("expired or\nstopped".replace("\n", " "), backlog["detail"])

    def test_a_second_project_is_observed_independently(self) -> None:
        """Every other case in this file is built on `amiga` alone.

        A shared, project-aware contract proven against one project ID is not proven to be
        project-aware at all: an Amiga-shaped assumption would pass every case above.
        """
        root = self.workspace()
        write_json(root / "projects.json", {"projects": [
            {"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}},
            {"id": "nuvyr", "display_name": "Nuvyr", "repos": {"api": "."}}]})
        write_json(
            root / "State" / "session_autobridge" / "sessions" / "SESSION-NUVYR.json",
            {
                "session_id": "SESSION-NUVYR", "agent_id": "cdx2", "project_id": "nuvyr",
                "chat_id": "CHAT-NUVYR", "status": "active", "mode": "auto-read",
                "wake_strategy": "runtime_trigger", "repo_targets": ["api"],
                "lease_expires_utc": iso(3600),
                "runtime": {"family": "gemini_cli", "session_id": "SESSION-NUVYR-runtime"},
                "processed_messages": [],
            },
        )
        write_json(
            root / "State" / "session_autobridge" / "bindings" / "nuvyr"
            / "CHAT-NUVYR" / "cdx2.json",
            {
                "project_id": "nuvyr", "chat_id": "CHAT-NUVYR", "agent_id": "cdx2",
                "session_id": "SESSION-NUVYR",
                "runtime_session_id": "SESSION-NUVYR-runtime",
                "runtime_family": "gemini_cli",
            },
        )

        _, nuvyr = self.run_presend(
            root, "--project", "nuvyr", "--chat", "CHAT-NUVYR", "--agent", "cdx2",
            "--repo-targets", "api")
        checks = self.presend_checks(nuvyr)
        self.assertEqual(OK, checks["exact-pair"]["status"])
        self.assertEqual("SESSION-NUVYR", nuvyr["agents"][0]["session_id"])
        self.assertEqual(OK, checks["repo-scope"]["status"])

        # The amiga chat has no session here, so the same agent's nuvyr binding must not
        # be offered for it -- that is the cross-project leak this case exists to exclude.
        _, amiga = self.run_presend(
            root, "--project", "amiga", "--chat", "CHAT-HEALTH", "--agent", "cdx2")
        self.assertEqual(FAIL, self.presend_checks(amiga)["exact-pair"]["status"])
        self.assertIsNone(amiga["agents"][0]["session_id"])

    def test_agent_wide_mode_reports_the_whole_inventory(self) -> None:
        """Kept, but it must not be the thing consulted before a send."""
        root = self.workspace_with_two_sessions()
        code, payload = self.run_presend(root, "--agent", "cdx2")
        self.assertEqual(0, code)
        # It reports BOTH, which is the honest inventory answer.
        self.assertEqual(["SESSION-LIVE"], payload["agents"][0]["dispatchable_sessions"])
        self.assertEqual(["SESSION-DEAD"], payload["agents"][0]["undispatchable_sessions"])

    def test_exact_binding_mode_reports_when_there_is_no_pair(self) -> None:
        """Codex's reproduction: a live sibling session is not evidence for this packet."""
        root = self.workspace_with_two_sessions()
        code, payload = self.run_presend(
            root, "--agent", "cdx2", "--project", "amiga", "--chat", "CHAT-NOT-BOUND")
        report = payload["agents"][0]
        self.assertEqual(0, code, report)
        self.assertEqual(FAIL, report["worst_observation"])
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

    def test_an_expired_lease_is_reported_as_a_failing_observation(self) -> None:
        root = self.workspace()
        self.add_session(root, "SESSION-DEAD", lease_delta=-60)
        code, payload = self.run_health(root)
        self.assertEqual(0, code)
        self.assertEqual(FAIL, self.agent(payload)["worst_observation"])
        self.assertIn("lease_expired", json.dumps(payload))

    def test_one_live_session_is_reported_dispatchable_despite_dead_ones(self) -> None:
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
        self.assertEqual(["SESSION-LIVE"], self.agent(payload)["dispatchable_sessions"])
        self.assertEqual(
            ["SESSION-OLD-PROBE"], self.agent(payload)["undispatchable_sessions"]
        )
        self.assertEqual("warn", self.check(payload, "stale-sessions")["status"])

    def test_an_unreachable_endpoint_does_not_make_a_session_undispatchable(self) -> None:
        """The advertised rule, now actually tested.

        Dispatchability is keyed to session state alone, because a live
        observation is stale the instant it is taken -- but endpoint FAIL was
        participating in the `live` classification, so an active leased session with an
        unreachable app-server was reported undispatchable. Code and comment disagreed and
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
        self.assertEqual(["SESSION-LIVE"], report["dispatchable_sessions"])
        self.assertEqual(0, code, "an observation reports; it does not gate")

    def test_a_warning_never_blocks_the_send(self) -> None:
        """A gate that fails on advisory noise gets wrapped in `|| true` and ignored."""
        root = self.workspace()
        self.add_session(root, "SESSION-LIVE", lease_delta=3600)
        self.add_session(root, "SESSION-OLD-PROBE", lease_delta=-7_000_000)
        code, payload = self.run_health(root)
        # The aggregate status also absorbs the ambient watcher check, so this asserts
        # the warning it is about, not the roll-up.
        self.assertEqual("warn", self.check(payload, "stale-sessions")["status"])
        self.assertEqual(0, code, "an observation reports; it does not gate")

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
        self.assertEqual(0, code, "an observation reports; it does not gate")
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

    def test_no_registered_session_is_reported_as_a_failing_observation(self) -> None:
        root = self.workspace()
        code, payload = self.run_health(root)
        self.assertEqual(0, code)
        self.assertEqual(FAIL, self.agent(payload)["worst_observation"])
        self.assertIn("will not wake anything", self.check(payload, "session")["detail"])

    def test_the_watcher_observation_does_not_decide_dispatchability(self) -> None:
        """Contract ruling (codex, 2026-07-26): process and endpoint checks are stale the
        instant they are taken, so gating on them makes the verdict a coin flip under
        exactly the conditions it is consulted. An earlier version folded the watcher
        into dispatchability; this pins the reversal so it cannot drift back silently.
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
        self.assertEqual(
            ["SESSION-LIVE"], self.agent(payload)["dispatchable_sessions"],
            "status, lease and resolved action decide dispatchability, not the watcher",
        )
        self.assertEqual(0, code, "an observation reports; it does not gate")
        if watcher["status"] == FAIL:
            self.assertEqual("fail", payload["agents"][0]["worst_observation"], "still visible in the report")

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
        # The session snapshot is per RUN; in-process tests share the module, so each one
        # starts its own run.
        pipeline_health.reset_scan_budget()

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
            def set_deadline(self_inner, *a, **k):
                return None

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def notify(self_inner, *a, **k):
                return None

            def set_deadline(self_inner, *a, **k):
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
            def set_deadline(self_inner, *a, **k):
                return None

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def notify(self_inner, *a, **k):
                return None

            def set_deadline(self_inner, *a, **k):
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
            # `int()` coerced all three of these instead of rejecting them. `True` was the
            # worst: it became 1 and read as a healthy observation dated to 1970.
            "boolean": {"id": "thread-x", "updatedAt": True},
            "float": {"id": "thread-x", "updatedAt": 1.5},
            "numeric_string": {"id": "thread-x", "updatedAt": "123"},
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

    def test_the_session_scan_charges_entries_the_filter_would_have_hidden(self) -> None:
        """A real directory, because mocking `glob` proves nothing about the bound.

        `sorted(glob("*.json"))` materialises the whole matching list before a loop can
        charge anything, and the suffix filter drops entries before they are counted -- so
        non-JSON names escaped the budget entirely. The previous version of this test
        handed the function a fake `glob`, which asserted that a check exists rather than
        that the work is bounded.
        """
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(12):
                (directory / f"noise_{index}.txt").write_text("x", encoding="utf-8")
            (directory / "sess_real.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(self.mod, "SESSIONS_DIR", directory), \
                    mock.patch.object(self.mod, "SESSION_SCAN_LIMIT", 5):
                with self.assertRaises(RuntimeError) as caught:
                    self.mod._sessions_for("cdx2")
        self.assertIn("refusing to scan further", str(caught.exception))

    def test_a_normal_session_directory_is_well_under_the_budget(self) -> None:
        """The budget must not be so tight that ordinary use trips it."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(50):
                (directory / f"sess_{index}.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(self.mod, "SESSIONS_DIR", directory):
                self.assertEqual([], self.mod._sessions_for("cdx2"))

    def test_the_unread_queue_is_refused_before_any_packet_is_parsed(self) -> None:
        """The charge is on the pointers, not on the parsed result.

        `get_unread_messages` reads and parses every existing packet before returning, so
        a length check on its result detected the excess only after all the work was done.
        Parsing is made to explode here, so the refusal can only pass if nothing was
        parsed.
        """
        from unittest import mock

        inbox = {"agent": "cdx2", "unread": [f"Chats/x/{i}.md" for i in range(20)], "read": []}
        with mock.patch.object(self.mod, "load_agent_inbox", return_value=inbox), \
                mock.patch.object(self.mod, "UNREAD_SCAN_LIMIT", 5), \
                mock.patch.object(
                    self.mod, "parse_frontmatter",
                    side_effect=AssertionError("must refuse before parsing"),
                ):
            with self.assertRaises(RuntimeError) as caught:
                self.mod._bounded_unread("cdx2")
        self.assertIn("partial backlog", str(caught.exception))

    def watcher_with_listing(self, listing: str) -> dict:
        from unittest import mock

        class Result:
            stdout = listing

        with mock.patch.object(self.mod.subprocess, "run", return_value=Result()):
            return self.mod._watcher_check("cdx2")

    def test_no_watcher_at_all_is_a_failing_observation(self) -> None:
        """Injected rather than read from the host.

        A real watcher for this agent on the machine running the suite would otherwise
        decide the result -- which it did, and the earlier version of this case passed or
        failed depending on what was running.
        """
        check = self.watcher_with_listing("/bin/zsh\n/usr/bin/ssh\n")
        self.assertEqual(self.mod.FAIL, check["status"])
        self.assertIn("Nothing polls this inbox", check["detail"])

    def test_a_watcher_from_this_workspace_counts(self) -> None:
        mine = f"python3 {self.mod.ROOT / 'bin' / 'watch_inbox.py'} --me cdx2 --json"
        check = self.watcher_with_listing(f"/bin/zsh\n{mine}\n")
        self.assertEqual(self.mod.OK, check["status"])

    def test_an_absolute_foreign_watcher_is_reported_as_foreign(self) -> None:
        """Matching the basename alone counted another checkout's mailbox poller as ours."""
        foreign = "/Users/x/other-collab/bin/watch_inbox.py --me cdx2 --json"
        check = self.watcher_with_listing(f"/bin/zsh\n{foreign}\n")
        self.assertEqual(self.mod.FAIL, check["status"])
        self.assertIn("another checkout", check["detail"])

    def test_a_longer_agent_id_does_not_satisfy_a_shorter_one(self) -> None:
        """`--me codex2` satisfied a substring search for `--me codex`.

        With this checkout's absolute script path that read as OK, so inspecting the
        shorter of two registered IDs reported the longer one's watcher as its own.
        """
        other = f"python3 {self.mod.ROOT / 'bin' / 'watch_inbox.py'} --me cdx2extra --json"
        check = self.watcher_with_listing(f"/bin/zsh\n{other}\n")
        self.assertEqual(self.mod.FAIL, check["status"])

    def test_thread_list_pagination_is_followed(self) -> None:
        """The schema paginates; reading one page reported a listed thread as absent."""
        from unittest import mock

        pages = [
            {"data": [{"id": "other-1", "updatedAt": 0}], "nextCursor": "c1"},
            {"data": [{"id": "thread-x", "updatedAt": int(self.mod.now_utc().timestamp())}],
             "nextCursor": None},
        ]
        calls: list[object] = []

        class PagingClient:
            def __init__(self, *a, **k): pass
            def set_deadline(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def notify(self, *a, **k): return None
            def request(self, method, params=None):
                if method == "thread/list":
                    calls.append((params or {}).get("cursor"))
                    return pages[len(calls) - 1]
                return {}

        with mock.patch.object(
            self.mod, "discover_codex_app_server",
            return_value={"url": "ws://127.0.0.1:1", "token_file": None},
        ), mock.patch.object(
            self.mod, "_codex_app_server_token", return_value=None
        ), mock.patch.object(self.mod, "JsonRpcWebSocketClient", PagingClient):
            check = self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "thread-x",
                             "home": "/home"}}
            )
        self.assertEqual(self.mod.OK, check["status"], check)
        self.assertEqual([None, "c1"], calls, "the second page was not requested")

    def test_a_relatively_named_watcher_is_reported_as_unattributable(self) -> None:
        """`ps` does not carry the process cwd, so a relative argument names nothing.

        The documented `python bin/watch_inbox.py --me <agent>` form produces one. Calling
        it foreign rejects a real watcher; resolving it against this ROOT invents the fact
        that it was launched from here. Both were shipped in turn, so neither guess is an
        observation -- this reports the uncertainty and says how to remove it.
        """
        relative = "python3 bin/watch_inbox.py --me cdx2 --json"
        check = self.watcher_with_listing(f"/bin/zsh\n{relative}\n")
        self.assertEqual(self.mod.WARN, check["status"])
        self.assertIn("cannot be determined", check["detail"])

    def test_the_probe_negotiates_exactly_as_the_dispatcher_does(self) -> None:
        """A weaker handshake observes a different connection than the one that matters.

        A server can accept `clientInfo` alone and reject the real negotiation, or reject a
        malformed probe that dispatch would have completed. The earlier test only varied
        the method's return value and never looked at the parameters.
        """
        from unittest import mock

        seen: dict[str, object] = {}

        class FakeClient:
            def set_deadline(self_inner, *a, **k):
                return None

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def notify(self_inner, *a, **k):
                return None

            def set_deadline(self_inner, *a, **k):
                return None

            def request(self_inner, method, params=None):
                seen[method] = params
                if method == "thread/list":
                    return {"data": [{"id": "thread-x", "updatedAt": 0}]}
                return {}

        with mock.patch.object(
            self.mod, "discover_codex_app_server",
            return_value={"url": "ws://127.0.0.1:1", "token_file": None},
        ), mock.patch.object(
            self.mod, "_codex_app_server_token", return_value=None
        ), mock.patch.object(
            self.mod, "JsonRpcWebSocketClient", lambda *a, **k: FakeClient()
        ):
            self.mod._activity_check(
                {"runtime": {"family": "codex_app", "session_id": "thread-x",
                             "home": "/home"}}
            )
        self.assertEqual(self.mod.CODEX_APP_SERVER_INITIALIZE_PARAMS, seen["initialize"])
        self.assertEqual("2024-11-05", seen["initialize"]["protocolVersion"])
        self.assertTrue(seen["initialize"]["capabilities"]["experimentalApi"])

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
            def set_deadline(self_inner, *a, **k):
                return None

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
                         server_request_policy=None, max_frame_bytes=None):
                captured["policy"] = server_request_policy
                captured["max_frame_bytes"] = max_frame_bytes

            def set_deadline(self, *a, **k): captured["deadline_set"] = True
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
        # The probe reads another process's output -- genuinely input we do not control --
        # so it carries a frame ceiling and one absolute deadline rather than a per-read
        # timeout a peer can renew forever by trickling bytes.
        self.assertEqual(self.mod.PROBE_MAX_FRAME_BYTES, captured["max_frame_bytes"])
        self.assertTrue(captured.get("deadline_set"), "no absolute deadline was installed")

    def test_an_exact_mode_scan_refusal_is_not_reported_as_an_observation(self) -> None:
        """A refused snapshot means the observation could not be made.

        Swallowing it into an `exact-pair` finding produced exit 0 for a check that never
        ran -- the one thing the exit status is defined to report.
        """
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(12):
                (directory / f"sess_{index}.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(self.mod, "SESSIONS_DIR", directory), \
                    mock.patch.object(self.mod, "SESSION_SCAN_LIMIT", 5):
                self.mod.reset_scan_budget()
                with self.assertRaises(RuntimeError):
                    self.mod.target_report(
                        "amiga", "CHAT-HEALTH", "cdx2", min_lease_seconds=1800
                    )

    def test_a_jsonrpc_error_degrades_to_warn_rather_than_crashing(self) -> None:
        """request() raises RuntimeError for an error reply; it escaped the whole preflight."""
        from unittest import mock

        class ErroringClient:
            def __init__(self, *a, **k): pass
            def set_deadline(self, *a, **k): pass
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
                    def set_deadline(self, *a, **k): pass
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
