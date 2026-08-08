"""Focused proof for the BB spawn gate (GH-627)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import bb_spawn  # noqa: E402
from llm_collab.bb_client import (  # noqa: E402
    REFUSAL_IDENTITY_MISMATCH,
    REFUSAL_ORPHANED_THREAD,
    REFUSAL_TRANSPORT_FAILED,
    REFUSAL_VERSION_MISMATCH,
    BbClient,
    BbProfile,
    BbRefusal,
    BbThread,
    BbTransportResult,
    subprocess_transport,
)
from llm_collab.spawn_gate import (  # noqa: E402
    GIT_MAX_RESPONSE_CHARS,
    Attached,
    GateRefusal,
    NewWorktree,
    SpawnPlan,
    _post_spawn_refusal,
    persist_assignment,
    plan_spawn,
)

SHA = "a" * 40
ORIGIN_SHA = "b" * 40
REPO = Path("/registered/project/repo")
REGISTRY = {
    "id": "llm-collab",
    "default_branch_base": "main",
    "repos": {"app": REPO},
    "bb": {
        "enabled": True,
        "project_id": "proj_llm_collab",
        "executable": ["/configured/bb", "--wrapper"],
        "timeout_seconds": 17.0,
    },
}
PROFILE = BbProfile("codex", "gpt-5.6-luna", "medium")
CLI_ARGS = [
    "--assignment-kind", "read-only",
    "--collab-project", "llm-collab",
    "--repo-target", "app",
    "--provider", PROFILE.provider,
    "--model", PROFILE.model,
    "--reasoning-level", PROFILE.reasoning_level,
    "--base-sha", SHA,
    "--new-environment", "worktree",
    "--prompt", "audit",
]


class GitTransport:
    def __init__(self, *, origin_sha: str = SHA, behind: int = 0) -> None:
        self.origin_sha = origin_sha
        self.behind = behind
        self.calls: list[list[str]] = []

    def __call__(self, argv, _timeout):  # noqa: ANN001 - transport protocol
        call = list(argv)
        self.calls.append(call)
        command = call[2:]
        if command[:2] == ["fetch", "--quiet"]:
            return BbTransportResult(0, "", "")
        if command[-1] == f"{SHA}^{{commit}}":
            return BbTransportResult(0, SHA + "\n", "")
        if command[-1].startswith("origin/") and command[-1].endswith("^{commit}"):
            return BbTransportResult(0, self.origin_sha + "\n", "")
        if command[:2] == ["merge-base", "--is-ancestor"]:
            return BbTransportResult(0, "", "")
        if command[:2] == ["rev-list", "--count"]:
            return BbTransportResult(0, f"{self.behind}\n", "")
        raise AssertionError(call)


def planned(*, transport=None, **overrides):
    values = {
        "assignment_kind": "read-only",
        "registry_entry": REGISTRY,
        "repo_target": "app",
        "base_sha": SHA,
        "environment": NewWorktree(),
        "provider": PROFILE.provider,
        "model": PROFILE.model,
        "reasoning_level": PROFILE.reasoning_level,
        "permission_mode": "accept-edits",
        "title": "Audit",
        "prompt": "Do the bounded audit.",
        "transport": transport or GitTransport(),
    }
    values.update(overrides)
    return plan_spawn(**values)


class PreflightRefusalTest(unittest.TestCase):
    def test_missing_triple_refuses(self) -> None:
        for field in ("provider", "model", "reasoning_level"):
            with self.subTest(field=field):
                outcome = planned(**{field: None})
                self.assertIsInstance(outcome, GateRefusal)
                self.assertEqual("incomplete_profile", outcome.reason)

    def test_excluded_models_refuse_every_assignment_kind(self) -> None:
        for model in ("meta/muse-spark-1.2-contributor", "zai/glm-5.2"):
            for assignment_kind in ("read-only", "writing"):
                with self.subTest(model=model, assignment_kind=assignment_kind):
                    outcome = planned(
                        assignment_kind=assignment_kind, provider="pi", model=model
                    )
                    self.assertIsInstance(outcome, GateRefusal)
                    self.assertEqual("excluded_model", outcome.reason)
                    self.assertEqual(
                        f"pi / {model} is excluded from {assignment_kind} assignments",
                        outcome.detail,
                    )

    def test_permitted_model_accepts_every_assignment_kind(self) -> None:
        for assignment_kind in ("read-only", "writing"):
            with self.subTest(assignment_kind=assignment_kind):
                outcome = planned(assignment_kind=assignment_kind)
                self.assertIsInstance(outcome, SpawnPlan)

    def test_missing_worktree_isolation_refuses(self) -> None:
        outcome = planned(environment=None)
        self.assertIsInstance(outcome, GateRefusal)
        self.assertEqual("isolation_required", outcome.reason)

    def test_branch_name_base_refuses_before_git(self) -> None:
        transport = GitTransport()
        outcome = planned(base_sha="main", transport=transport)
        self.assertIsInstance(outcome, GateRefusal)
        self.assertEqual("invalid_base_sha", outcome.reason)
        self.assertEqual([], transport.calls)

    def test_base_behind_origin_main_refuses_with_exact_drift(self) -> None:
        outcome = planned(transport=GitTransport(origin_sha=ORIGIN_SHA, behind=44))
        self.assertIsInstance(outcome, GateRefusal)
        self.assertEqual("base_behind_origin", outcome.reason)
        self.assertIn("44 commits behind origin/main", outcome.detail)


class GitBoundaryTest(unittest.TestCase):
    def test_non_main_default_branch_is_used_for_every_origin_lookup(self) -> None:
        transport = GitTransport(origin_sha=ORIGIN_SHA)
        registry = {**REGISTRY, "default_branch_base": "stable"}
        outcome = planned(registry_entry=registry, transport=transport)
        self.assertIsInstance(outcome, SpawnPlan)
        commands = [call[2:] for call in transport.calls]
        self.assertEqual(["fetch", "--quiet", "origin", "stable"], commands[0])
        self.assertEqual(
            ["rev-parse", "--verify", "origin/stable^{commit}"], commands[2]
        )
        self.assertEqual(
            ["merge-base", "--is-ancestor", SHA, ORIGIN_SHA], commands[3]
        )
        self.assertEqual(
            ["rev-list", "--count", f"{SHA}..{ORIGIN_SHA}"], commands[4]
        )

    def test_every_git_argv_is_scoped_with_dash_c(self) -> None:
        transport = GitTransport()
        outcome = planned(transport=transport)
        self.assertIsInstance(outcome, SpawnPlan)
        self.assertEqual(5, len(transport.calls))
        self.assertTrue(
            all(call[:2] == ["-C", str(REPO)] for call in transport.calls),
            transport.calls,
        )

    def test_fetch_stderr_overflow_refuses_before_any_parse(self) -> None:
        script = f"import sys; sys.stderr.write('x' * {GIT_MAX_RESPONSE_CHARS + 1})"
        bounded = subprocess_transport(
            [sys.executable, "-c", script],
            max_response_chars=GIT_MAX_RESPONSE_CHARS,
        )
        calls = []

        def recording(argv, timeout):  # noqa: ANN001 - transport protocol
            calls.append(list(argv))
            return bounded(argv, timeout)

        outcome = planned(transport=recording)
        self.assertIsInstance(outcome, GateRefusal)
        self.assertEqual("git_read_failed", outcome.reason)
        self.assertIn("exceeded", outcome.detail)
        self.assertEqual([["-C", str(REPO), "fetch", "--quiet", "origin", "main"]], calls)


class PlanConstructionTest(unittest.TestCase):
    def test_direct_spawn_plan_construction_is_refused(self) -> None:
        with self.assertRaisesRegex(TypeError, "plan_spawn"):
            SpawnPlan(
                "project",
                "native-project",
                REPO,
                SHA,
                NewWorktree(),
                PROFILE,
                None,
                None,
                "prompt",
            )


class PersistenceTest(unittest.TestCase):
    @staticmethod
    def write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_valid_id_persists_distinct_projects_and_profiles(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)
        thread = BbThread(
            "thr_worker1", "proj_llm_collab", "env_worker1", "codex", "starting"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = persist_assignment(
                plan,
                thread,
                Path(temporary),
                write_durably=self.write,
            )
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("thr_worker1.json", path.name)
        self.assertEqual("llm-collab", record["project_id"])
        self.assertEqual("proj_llm_collab", record["native_project_id"])
        self.assertEqual(PROFILE.model, record["requested_profile"]["model"])
        self.assertEqual(PROFILE.model, record["executed_profile"]["model"])
        self.assertIn("requested_profile", record)
        self.assertIn("executed_profile", record)

    def test_invalid_id_is_an_orphan_carrying_the_native_id_before_any_write(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)
        for thread_id in ("bad/id", "bad\0id", ".", ".."):
            thread = BbThread(
                thread_id, "proj_llm_collab", "env_worker1", "codex", "starting"
            )
            writer = mock.Mock()
            with self.subTest(thread_id=thread_id), self.assertRaises(Exception) as raised:
                persist_assignment(plan, thread, Path("/state"), write_durably=writer)
            refusal = _post_spawn_refusal(raised.exception, thread)
            self.assertEqual(REFUSAL_ORPHANED_THREAD, refusal.reason)
            self.assertEqual(thread_id, refusal.native_thread_id)
            writer.assert_not_called()


def bb_transport(*, environment_id: str = "env_expected"):
    calls = []
    spawn_payload = {
        "id": "thr_worker1",
        "environmentId": environment_id,
        "projectId": "proj_llm_collab",
        "providerId": PROFILE.provider,
        "status": "starting",
    }
    events = [
        {
            "id": "evt_1",
            "threadId": "thr_worker1",
            "seq": 1,
            "type": "client/turn/requested",
            "data": {
                "source": "spawn",
                "execution": {
                    "model": PROFILE.model,
                    "reasoningLevel": PROFILE.reasoning_level,
                },
            },
        }
    ]

    def transport(argv, _timeout):  # noqa: ANN001 - transport protocol
        calls.append(list(argv))
        if list(argv[:2]) == ["settings", "version"]:
            return BbTransportResult(0, '{"currentVersion":"0.35.1"}', "")
        if list(argv[:2]) == ["thread", "spawn"]:
            return BbTransportResult(0, json.dumps(spawn_payload), "")
        if list(argv[:2]) == ["thread", "log"]:
            return BbTransportResult(0, json.dumps(events), "")
        raise AssertionError(argv)

    return transport, calls


class BbClientSpawnOptionsTest(unittest.TestCase):
    def test_pre_task_refusals_prove_no_spawn_call_was_attempted(self) -> None:
        calls = []

        def wrong_version(argv, _timeout):  # noqa: ANN001 - transport protocol
            calls.append(list(argv))
            return BbTransportResult(0, '{"currentVersion":"0.36.0"}', "")

        version = BbClient(wrong_version, enabled=True).spawn(
            project_id="proj_llm_collab", prompt="audit", profile=PROFILE
        )
        self.assertIsInstance(version, BbRefusal)
        self.assertEqual(REFUSAL_VERSION_MISMATCH, version.reason)
        self.assertIs(version.task_attempted, False)
        self.assertEqual([["settings", "version", "--json"]], calls)

        no_calls = mock.Mock()
        conflict = BbClient(no_calls, enabled=True).spawn(
            project_id="proj_llm_collab",
            prompt="audit",
            profile=PROFILE,
            environment="env_one",
            base_sha=SHA,
        )
        self.assertIsInstance(conflict, BbRefusal)
        self.assertIs(conflict.task_attempted, False)
        no_calls.assert_not_called()

    def test_attached_environment_mismatch_is_orphaned(self) -> None:
        transport, _ = bb_transport(environment_id="env_wrong")
        outcome = BbClient(transport, enabled=True).spawn(
            project_id="proj_llm_collab",
            prompt="write",
            profile=PROFILE,
            environment="env_expected",
        )
        self.assertIsInstance(outcome, BbRefusal)
        self.assertEqual(REFUSAL_IDENTITY_MISMATCH, outcome.reason)
        self.assertEqual("thr_worker1", outcome.native_thread_id)
        self.assertIs(outcome.task_attempted, True)

    def test_new_worktree_options_are_additive_to_the_existing_spawn_call(self) -> None:
        transport, calls = bb_transport()
        outcome = BbClient(transport, enabled=True).spawn(
            project_id="proj_llm_collab",
            prompt="audit",
            profile=PROFILE,
            base_sha=SHA,
            permission_mode="accept-edits",
            title="Audit",
        )
        self.assertIsInstance(outcome, BbThread)
        argv = next(call for call in calls if call[:2] == ["thread", "spawn"])
        self.assertEqual(SHA, argv[argv.index("--base-branch") + 1])
        self.assertEqual("accept-edits", argv[argv.index("--permission-mode") + 1])
        self.assertEqual("Audit", argv[argv.index("--title") + 1])


class CliPhaseTest(unittest.TestCase):
    def test_script_help_runs_from_outside_the_repository(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "bb_spawn.py"), "--help"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--assignment-kind", result.stdout)

    def test_gate_refusal_is_retryable_exit_one(self) -> None:
        refusal = GateRefusal("invalid_base_sha", "branch name")
        with mock.patch.object(bb_spawn, "get_project", return_value=REGISTRY), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "plan_spawn", return_value=refusal), mock.patch.object(
            bb_spawn, "_configured_client"
        ) as client, mock.patch.object(bb_spawn, "_emit") as emit:
            self.assertEqual(1, bb_spawn.main(CLI_ARGS))
        client.assert_not_called()
        emit.assert_called_once_with("REFUSED: invalid_base_sha: branch name")

    def test_client_pre_execution_refusal_is_retryable_exit_one(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)
        client = mock.Mock()
        client.spawn.return_value = BbRefusal(
            REFUSAL_VERSION_MISMATCH,
            "upgrade bb",
            task_attempted=False,
        )
        with mock.patch.object(bb_spawn, "get_project", return_value=REGISTRY), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "plan_spawn", return_value=plan), mock.patch.object(
            bb_spawn, "project_state_dir", return_value=Path("/state")
        ), mock.patch.object(bb_spawn, "_configured_client", return_value=client), mock.patch.object(
            bb_spawn, "_emit"
        ) as emit:
            self.assertEqual(1, bb_spawn.main(CLI_ARGS))
        rendered = emit.call_args.args[0]
        self.assertEqual("REFUSED: bb_version_mismatch: upgrade bb", rendered)
        self.assertNotIn("thread may exist", rendered)

    def test_client_post_execution_refusal_is_exit_two_with_identity(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)
        client = mock.Mock()
        client.spawn.return_value = BbRefusal(
            REFUSAL_IDENTITY_MISMATCH,
            "wrong environment",
            native_thread_id="thr_worker1",
            task_attempted=True,
        )
        with mock.patch.object(bb_spawn, "get_project", return_value=REGISTRY), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "plan_spawn", return_value=plan), mock.patch.object(
            bb_spawn, "project_state_dir", return_value=Path("/state")
        ), mock.patch.object(bb_spawn, "_configured_client", return_value=client), mock.patch.object(
            bb_spawn, "_emit"
        ) as emit:
            self.assertEqual(2, bb_spawn.main(CLI_ARGS))
        rendered = emit.call_args.args[0]
        self.assertIn("DO NOT RETRY", rendered)
        self.assertIn("native_thread_id=thr_worker1", rendered)

    def test_success_output_failure_is_retry_suppressed_exit_two(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)
        thread = BbThread(
            "thr_worker1", "proj_llm_collab", "env_worker1", "codex", "starting"
        )
        client = mock.Mock()
        client.spawn.return_value = thread
        with mock.patch.object(bb_spawn, "get_project", return_value=REGISTRY), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "plan_spawn", return_value=plan), mock.patch.object(
            bb_spawn, "project_state_dir", return_value=Path("/state")
        ), mock.patch.object(bb_spawn, "_configured_client", return_value=client), mock.patch.object(
            bb_spawn, "persist_assignment", return_value=Path("/state/thr_worker1.json")
        ), mock.patch("builtins.print", side_effect=BrokenPipeError("closed")), mock.patch.object(
            bb_spawn, "_emit"
        ) as emit:
            self.assertEqual(2, bb_spawn.main(CLI_ARGS))
        client.spawn.assert_called_once()
        self.assertIn("native_thread_id=thr_worker1", emit.call_args.args[0])

    def test_configured_bb_executable_timeout_and_native_project_are_used(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)
        transport, calls = bb_transport()
        timeouts = []

        def configured_transport(argv, timeout):  # noqa: ANN001 - transport protocol
            timeouts.append(timeout)
            return transport(argv, timeout)

        with mock.patch.object(bb_spawn, "get_project", return_value=REGISTRY), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "plan_spawn", return_value=plan), mock.patch.object(
            bb_spawn, "project_state_dir", return_value=Path("/state")
        ), mock.patch(
            "llm_collab.bb_client.subprocess_transport",
            return_value=configured_transport,
        ) as factory, mock.patch.object(
            bb_spawn, "persist_assignment", return_value=Path("/state/thr_worker1.json")
        ), mock.patch("builtins.print"):
            self.assertEqual(0, bb_spawn.main(CLI_ARGS))
        factory.assert_called_once_with(["/configured/bb", "--wrapper"])
        spawn_argv = next(call for call in calls if call[:2] == ["thread", "spawn"])
        self.assertEqual(
            "proj_llm_collab", spawn_argv[spawn_argv.index("--project") + 1]
        )
        self.assertTrue(timeouts)
        self.assertEqual({17.0}, set(timeouts))

    def test_disabled_project_refuses_before_client_construction(self) -> None:
        disabled = {**REGISTRY, "bb": {**REGISTRY["bb"], "enabled": False}}
        with mock.patch.object(bb_spawn, "get_project", return_value=disabled), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "_configured_client") as client, mock.patch.object(
            bb_spawn, "_emit"
        ) as emit:
            self.assertEqual(1, bb_spawn.main(CLI_ARGS))
        client.assert_not_called()
        self.assertIn("bb adapter is not enabled", emit.call_args.args[0])

    def test_pre_task_launch_failure_is_retryable_exit_one(self) -> None:
        plan = planned()
        self.assertIsInstance(plan, SpawnPlan)

        def missing_executable(_argv, _timeout):  # noqa: ANN001 - transport protocol
            raise FileNotFoundError("configured bb is missing")

        client = BbClient(missing_executable, enabled=True)
        with mock.patch.object(bb_spawn, "get_project", return_value=REGISTRY), mock.patch.object(
            bb_spawn, "resolve_project_repo_path", return_value=REPO
        ), mock.patch.object(bb_spawn, "plan_spawn", return_value=plan), mock.patch.object(
            bb_spawn, "project_state_dir", return_value=Path("/state")
        ), mock.patch.object(
            bb_spawn, "_configured_client", return_value=client
        ), mock.patch.object(bb_spawn, "_emit") as emit:
            self.assertEqual(1, bb_spawn.main(CLI_ARGS))
        rendered = emit.call_args.args[0]
        self.assertIn(REFUSAL_TRANSPORT_FAILED, rendered)
        self.assertNotIn("DO NOT RETRY", rendered)


if __name__ == "__main__":
    unittest.main()
