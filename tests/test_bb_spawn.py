"""Focused tests for the BB assignment spawn preflight."""

from __future__ import annotations

import io
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
import llm_collab.bb_client as bb_client  # noqa: E402

SHA = "a" * 40
BASE_ARGS = [
    "--assignment-kind", "read-only",
    "--collab-project", "llm-collab",
    "--project", "proj_llm_collab",
    "--base-sha", SHA,
    "--new-environment", "worktree",
    "--provider", "codex",
    "--model", "gpt-5.6-luna",
    "--reasoning-level", "medium",
    "--prompt", "audit",
]


class BbSpawnTest(unittest.TestCase):
    def context(self, root: Path) -> bb_spawn.ProjectContext:
        return bb_spawn.ProjectContext(
            repo_root=root / "registered-repo",
            record_dir=root / "state" / "llm-collab" / "bb-assignments",
            repo_target="app",
        )

    def run_main(self, outcome, root: Path, args=None):
        client = mock.Mock()
        client.spawn.return_value = outcome
        context = self.context(root)
        with mock.patch.object(
            bb_spawn, "project_context", return_value=context
        ), mock.patch.object(
            bb_spawn, "validate_base", return_value=SHA
        ), mock.patch.object(
            bb_spawn, "subprocess_transport", return_value=mock.sentinel.transport
        ), mock.patch.object(
            bb_spawn, "BbClient", return_value=client
        ):
            result = bb_spawn.main(args or BASE_ARGS)
        return result, client, context

    def test_missing_triple_refuses_before_spawn(self):
        args = BASE_ARGS[:]
        del args[args.index("--model") : args.index("--model") + 2]
        with mock.patch.object(bb_spawn, "project_context") as project_context:
            with self.assertRaisesRegex(bb_spawn.Refusal, "missing --model"):
                bb_spawn.main(args)
        project_context.assert_not_called()

    def test_writing_exclusions_refuse(self):
        for model in ("meta/muse-spark-1.2-contributor", "zai/glm-5.2"):
            args = [value if value != "codex" else "pi" for value in BASE_ARGS]
            args[args.index("gpt-5.6-luna")] = model
            args[args.index("read-only")] = "writing"
            with self.subTest(model=model), self.assertRaisesRegex(
                bb_spawn.Refusal, "excluded from writing"
            ):
                bb_spawn.main(args)

    def test_shared_checkout_target_refuses(self):
        args = BASE_ARGS[:]
        del args[args.index("--new-environment") : args.index("--new-environment") + 2]
        with self.assertRaisesRegex(bb_spawn.Refusal, "assignment isolation is required"):
            bb_spawn.main(args)

    def test_branch_name_refuses(self):
        args = ["main" if value == SHA else value for value in BASE_ARGS]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            bb_spawn, "project_context", return_value=self.context(Path(tmp))
        ):
            with self.assertRaisesRegex(bb_spawn.Refusal, "branch name"):
                bb_spawn.main(args)

    def test_stale_base_reports_exact_drift(self):
        origin = "b" * 40
        repo = Path("/registered/project/repo")
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            git_args = argv[3:]
            if git_args[:2] == ["fetch", "--quiet"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if git_args[-1] == f"{SHA}^{{commit}}":
                return subprocess.CompletedProcess(argv, 0, SHA + "\n", "")
            if git_args[-1] == "origin/main^{commit}":
                return subprocess.CompletedProcess(argv, 0, origin + "\n", "")
            if git_args[:2] == ["merge-base", "--is-ancestor"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if git_args[:2] == ["rev-list", "--count"]:
                return subprocess.CompletedProcess(argv, 0, "44\n", "")
            raise AssertionError(argv)

        with mock.patch.object(bb_spawn, "run", side_effect=fake_run):
            with self.assertRaisesRegex(bb_spawn.Refusal, "44 commits behind origin/main"):
                bb_spawn.validate_base(repo, SHA)
        self.assertTrue(calls)
        self.assertTrue(all(call[:3] == ["git", "-C", str(repo)] for call in calls))

    def test_multi_repo_project_requires_explicit_repo_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            docs = root / "docs"
            (root / "projects.json").write_text(json.dumps({
                "projects": [{
                    "id": "multi",
                    "repos": {"app": str(app), "docs": str(docs)},
                }]
            }))
            (root / "collab.config.json").write_text(json.dumps({
                "project_state_root": str(root / "state")
            }))
            with mock.patch.object(bb_spawn, "coordination_root", return_value=root):
                with self.assertRaisesRegex(bb_spawn.Refusal, "--repo-target is required"):
                    bb_spawn.project_context("multi", None)
                context = bb_spawn.project_context("multi", "docs")
        self.assertEqual(docs.resolve(), context.repo_root)
        self.assertEqual("docs", context.repo_target)

    def test_subprocess_output_is_bounded_while_reading(self):
        transport = bb_client.subprocess_transport(
            [sys.executable, "-c", "print('x' * 1000)"],
            max_response_chars=32,
        )
        with self.assertRaises(bb_client.BbResponseTooLarge):
            transport([], 30)

    def test_bb_output_budget_is_cumulative_across_commands_and_streams(self):
        script = (
            "import sys; "
            "sys.stdout.write('o' * 20); sys.stdout.flush(); "
            "sys.stderr.write('e' * 20); sys.stderr.flush()"
        )
        transport = bb_client.subprocess_transport(
            [sys.executable, "-c", script], max_response_chars=60
        )
        first = transport([], 30)
        self.assertEqual(40, len(first.stdout) + len(first.stderr))
        with self.assertRaises(bb_client.BbResponseTooLarge):
            transport([], 30)

    def test_registry_reads_share_one_cumulative_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "projects.json"
            second = Path(tmp) / "collab.config.json"
            first.write_text(json.dumps({"projects": []}))
            second.write_text(json.dumps({"project_state_root": "projects"}))
            budget = [first.stat().st_size + second.stat().st_size - 1]
            bb_spawn.read_json(first, budget)
            with self.assertRaisesRegex(bb_spawn.Refusal, "cumulative"):
                bb_spawn.read_json(second, budget)

    def test_attestation_reuses_bb_client_execution_evidence(self):
        calls = []
        spawn_payload = {
            "id": "thr_worker1",
            "environmentId": "env_worker1",
            "projectId": "proj_llm_collab",
            "providerId": "codex",
            "status": "starting",
        }
        events = [{
            "id": "evt_1",
            "threadId": "thr_worker1",
            "seq": 1,
            "type": "client/turn/requested",
            "data": {
                "source": "spawn",
                "execution": {
                    "model": "gpt-5.6-luna",
                    "reasoningLevel": "medium",
                },
            },
        }]

        def transport(argv, _timeout):
            calls.append(list(argv))
            if list(argv[:2]) == ["settings", "version"]:
                return bb_client.BbTransportResult(
                    0, '{"currentVersion":"0.35.1"}', ""
                )
            if list(argv[:2]) == ["thread", "spawn"]:
                return bb_client.BbTransportResult(0, json.dumps(spawn_payload), "")
            if list(argv[:2]) == ["thread", "log"]:
                return bb_client.BbTransportResult(0, json.dumps(events), "")
            raise AssertionError(argv)

        client = bb_client.BbClient(transport, enabled=True)
        outcome = client.spawn(
            project_id="proj_llm_collab",
            prompt="audit",
            profile=bb_client.BbProfile("codex", "gpt-5.6-luna", "medium"),
            new_worktree_base_sha=SHA,
        )
        self.assertIsInstance(outcome, bb_client.BbThread)
        self.assertEqual(["settings", "version"], calls[0][:2])
        self.assertEqual(["thread", "spawn"], calls[1][:2])
        self.assertIn(SHA, calls[1])

    def test_spawn_timeout_is_ambiguous_and_suppresses_retry(self):
        refusal = bb_client.BbRefusal(
            bb_client.REFUSAL_AMBIGUOUS,
            "thread spawn timed out; the operation may have been performed",
        )
        with tempfile.TemporaryDirectory() as tmp:
            outcome, client, _ = self.run_main(refusal, Path(tmp))
        self.assertIs(outcome, refusal)
        client.spawn.assert_called_once()
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(3, bb_spawn.emit_refusal(outcome))
        self.assertIn("DO NOT RETRY", stderr.getvalue())

    def test_every_exit_zero_decoder_limit_is_ambiguous(self):
        for error in (
            json.JSONDecodeError("bad", "x", 0),
            RecursionError("deep"),
            ValueError("integer limit"),
        ):
            transport = mock.Mock(side_effect=[
                bb_client.BbTransportResult(0, '{"currentVersion":"0.35.1"}', ""),
                bb_client.BbTransportResult(0, "ignored", ""),
            ])
            client = bb_client.BbClient(transport, enabled=True)
            with self.subTest(error=type(error).__name__), mock.patch.object(
                bb_client.json, "loads", side_effect=[{"currentVersion": "0.35.1"}, error]
            ):
                outcome = client.spawn(
                    project_id="proj_llm_collab",
                    prompt="audit",
                    profile=bb_client.BbProfile("codex", "gpt-5.6-luna", "medium"),
                )
            self.assertIsInstance(outcome, bb_client.BbRefusal)
            self.assertEqual(bb_client.REFUSAL_AMBIGUOUS, outcome.reason)
            self.assertIsNone(outcome.native_thread_id)

    def test_unattested_profile_is_recorded_and_suppresses_retry_with_identity(self):
        refusal = bb_client.BbRefusal(
            bb_client.REFUSAL_PROFILE_MISMATCH,
            "requested gpt-5.6-luna; bb ran another-model",
            native_thread_id="thr_worker1",
        )
        with tempfile.TemporaryDirectory() as tmp:
            outcome, _, context = self.run_main(refusal, Path(tmp))
            record = json.loads((context.record_dir / "thr_worker1.json").read_text())
        self.assertEqual(bb_client.REFUSAL_PROFILE_MISMATCH, outcome.reason)
        self.assertEqual("thr_worker1", outcome.native_thread_id)
        self.assertIsNone(record["executed_profile"])
        self.assertEqual("unattested", record["profile_attestation"]["status"])
        self.assertEqual("gpt-5.6-luna", record["requested_profile"]["model"])

    def test_complete_triple_spawns_and_is_recorded(self):
        thread = bb_client.BbThread(
            "thr_worker1", "proj_llm_collab", "env_worker1", "codex", "starting"
        )
        with tempfile.TemporaryDirectory() as tmp:
            outcome, client, context = self.run_main(thread, Path(tmp))
            record = json.loads((context.record_dir / "thr_worker1.json").read_text())
        self.assertIsInstance(outcome, bb_spawn.SpawnSuccess)
        self.assertEqual(
            ("codex", "gpt-5.6-luna", "medium"),
            (record["provider"], record["model"], record["reasoning_level"]),
        )
        self.assertEqual(SHA, record["base_sha"])
        self.assertEqual("attested", record["profile_attestation"]["status"])
        self.assertEqual("gpt-5.6-luna", record["executed_profile"]["model"])
        self.assertEqual(
            SHA, client.spawn.call_args.kwargs["new_worktree_base_sha"]
        )

    def test_broken_stdout_after_persistence_is_retry_suppressed(self):
        success = bb_spawn.SpawnSuccess(
            "{}\n", "thr_worker1", Path("/state/thr_worker1.json")
        )

        class BrokenOutput:
            def write(self, _text):
                raise BrokenPipeError("closed")

            def flush(self):
                raise AssertionError("write should fail first")

        stderr = io.StringIO()
        with mock.patch.object(bb_spawn, "main", return_value=success), mock.patch.object(
            sys, "stdout", BrokenOutput()
        ), mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(3, bb_spawn.cli())
        self.assertIn("DO NOT RETRY", stderr.getvalue())
        self.assertIn("native_thread_id=thr_worker1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
