import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deploy_runtime


class DeployRuntimeTest(unittest.TestCase):
    def test_source_head_rejects_unmerged_feature_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            (source / "AGENTS.md").write_text("<!-- CONTRACT_VERSION: 10 -->\n")
            with patch.object(
                deploy_runtime,
                "git",
                side_effect=["", "origin-sha", "feature-sha"],
            ):
                with self.assertRaisesRegex(deploy_runtime.DeployError, "exact origin/main"):
                    deploy_runtime.source_head(source)

    def test_deploy_refreshes_target_only_after_source_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            target = Path(temp_dir) / "target"
            source.mkdir()
            target.mkdir()
            (source / ".git").mkdir()
            (target / ".git").mkdir()
            events: list[str] = []

            def mark(name: str):
                events.append(name)

            with (
                patch.object(
                    deploy_runtime,
                    "source_head",
                    return_value=("head-sha", "10"),
                ),
                patch.object(deploy_runtime, "target_preflight", return_value="old-sha"),
                patch.object(deploy_runtime, "pm2_binary", return_value="/usr/bin/pm2"),
                patch.object(deploy_runtime, "ecosystem_definitions", return_value={}),
                patch.object(
                    deploy_runtime,
                    "fence_watchers",
                    side_effect=lambda owned_names: mark("fence"),
                ),
                patch.object(
                    deploy_runtime,
                    "reset_target",
                    side_effect=lambda target_path, head: mark(f"reset:{head}"),
                ),
                patch.object(
                    deploy_runtime,
                    "reconcile_pm2",
                    side_effect=lambda target_path, owned_names, definitions: mark("reconcile"),
                ),
                patch.object(
                    deploy_runtime,
                    "verify_deployment",
                    side_effect=lambda target_path, head, owned_names, definitions: mark("verify"),
                ),
                patch.object(deploy_runtime, "pm2_run") as pm2_run,
            ):
                evidence = deploy_runtime.deploy(source, target)

        self.assertEqual("head-sha", evidence["head"])
        self.assertEqual("old-sha", evidence["previous_head"])
        self.assertLess(events.index("fence"), events.index("reset:head-sha"))
        self.assertLess(events.index("reset:head-sha"), events.index("reconcile"))
        self.assertLess(events.index("reconcile"), events.index("verify"))
        pm2_run.assert_called_once_with(["save"])

    def test_timeout_rolls_target_back_to_previous_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target"
            target.mkdir()
            (target / ".git").mkdir()
            with (
                patch.object(
                    deploy_runtime,
                    "git",
                    side_effect=["old-sha", "", "old-sha", ""],
                ),
                patch.object(
                    deploy_runtime.subprocess,
                    "run",
                    side_effect=[
                        subprocess.TimeoutExpired(["git"], 30),
                        subprocess.CompletedProcess([], 0, "", ""),
                    ],
                ),
            ):
                with self.assertRaisesRegex(deploy_runtime.DeployError, "restored target HEAD old-sha"):
                    deploy_runtime.reset_target(target, "new-sha")

    def test_deploy_restores_previous_state_after_verification_failure(self):
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            target = Path(temp_dir) / "target"
            source.mkdir()
            target.mkdir()
            (source / ".git").mkdir()
            (target / ".git").mkdir()
            with (
                patch.object(deploy_runtime, "source_head", return_value=("new-sha", "10")),
                patch.object(deploy_runtime, "target_preflight", return_value="old-sha"),
                patch.object(deploy_runtime, "pm2_binary", return_value="/usr/bin/pm2"),
                patch.object(deploy_runtime, "ecosystem_definitions", return_value={}),
                patch.object(
                    deploy_runtime,
                    "fence_watchers",
                    side_effect=lambda owned_names: events.append("fence"),
                ),
                patch.object(
                    deploy_runtime,
                    "reset_target",
                    side_effect=lambda target_path, head: events.append(f"reset:{head}"),
                ),
                patch.object(
                    deploy_runtime,
                    "reconcile_pm2",
                    side_effect=lambda target_path, owned_names, definitions: events.append("reconcile"),
                ),
                patch.object(
                    deploy_runtime,
                    "verify_deployment",
                    side_effect=deploy_runtime.DeployError("roster mismatch"),
                ),
                patch.object(
                    deploy_runtime,
                    "restore_previous_deployment",
                    side_effect=lambda target_path, previous, owned_names: events.append("restore"),
                ),
                patch.object(deploy_runtime, "pm2_run"),
            ):
                with self.assertRaisesRegex(deploy_runtime.DeployError, "restored target HEAD old-sha"):
                    deploy_runtime.deploy(source, target)

        self.assertEqual(["fence", "reset:new-sha", "reconcile", "restore"], events)

    def test_reconcile_deletes_omitted_processes_before_restart(self):
        current = [{"name": "fixture-old", "pm2_env": {"status": "stopped"}}]
        with (
            patch.object(deploy_runtime, "pm2_jlist", side_effect=[current, []]),
            patch.object(deploy_runtime, "pm2_run") as pm2_run,
        ):
            deploy_runtime.reconcile_pm2(
                Path("/deployed/runtime"),
                frozenset({"fixture-old", "fixture-new"}),
                {"fixture-new": {"name": "fixture-new"}},
            )

        self.assertEqual(
            [
                call(["delete", "fixture-old"]),
                call([
                    "startOrRestart",
                    "/deployed/runtime/pm2/ecosystem.config.cjs",
                    "--update-env",
                ]),
            ],
            pm2_run.call_args_list,
        )

    def test_verify_checks_head_definition_and_log_probe(self):
        record = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
            },
        }
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", return_value=[record]),
            patch.object(deploy_runtime, "pm2_run") as pm2_run,
        ):
            deploy_runtime.verify_deployment(
                Path("/deployed/runtime"),
                "new-sha",
                frozenset({"fixture-new"}),
                {
                    "fixture-new": {
                        "cwd": "/deployed/runtime",
                        "script": "python3",
                        "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                    }
                },
            )

        pm2_run.assert_called_once_with(["logs", "fixture-new", "--lines", "1", "--nostream"])

    def test_verify_waits_for_unpopulated_pm2_metadata(self):
        unpopulated = {"name": "fixture-new", "pm2_env": {"status": "online", "script": None}}
        ready = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
            },
        }
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", side_effect=[[unpopulated], [ready]]) as pm2_jlist,
            patch.object(deploy_runtime, "pm2_run") as pm2_run,
            patch.object(deploy_runtime.time, "monotonic", side_effect=[0.0, 0.0]),
            patch.object(deploy_runtime.time, "sleep") as sleep,
        ):
            deploy_runtime.verify_deployment(
                Path("/deployed/runtime"),
                "new-sha",
                frozenset({"fixture-new"}),
                {
                    "fixture-new": {
                        "cwd": "/deployed/runtime",
                        "script": "python3",
                        "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                    }
                },
            )

        self.assertEqual(2, pm2_jlist.call_count)
        sleep.assert_called_once_with(deploy_runtime.PM2_READINESS_POLL_SECONDS)
        pm2_run.assert_called_once_with(["logs", "fixture-new", "--lines", "1", "--nostream"])

    def test_verify_times_out_on_unpopulated_pm2_metadata(self):
        unpopulated = {"name": "fixture-new", "pm2_env": {"status": "online", "script": None}}
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", side_effect=[[unpopulated], [unpopulated], [unpopulated]]) as pm2_jlist,
            patch.object(deploy_runtime.time, "monotonic", side_effect=[0.0, 0.0, 0.25, 10.0]),
            patch.object(deploy_runtime.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                deploy_runtime.DeployError,
                r"not_ready=fixture-new: status='online' missing=pm_cwd,script,args",
            ):
                deploy_runtime.verify_deployment(
                    Path("/deployed/runtime"),
                    "new-sha",
                    frozenset({"fixture-new"}),
                    {
                        "fixture-new": {
                            "cwd": "/deployed/runtime",
                            "script": "python3",
                            "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                        }
                    },
                )

        self.assertEqual(3, pm2_jlist.call_count)
        self.assertEqual(2, sleep.call_count)

    def test_verify_fails_fast_on_populated_wrong_metadata(self):
        wrong = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "script": "node",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
            },
        }
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", return_value=[wrong]) as pm2_jlist,
            patch.object(deploy_runtime.time, "monotonic", return_value=0.0),
        ):
            with self.assertRaisesRegex(deploy_runtime.DeployError, "script mismatch"):
                deploy_runtime.verify_deployment(
                    Path("/deployed/runtime"),
                    "new-sha",
                    frozenset({"fixture-new"}),
                    {
                        "fixture-new": {
                            "cwd": "/deployed/runtime",
                            "script": "python3",
                            "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                        }
                    },
                )

        pm2_jlist.assert_called_once_with()

    def test_verify_fails_fast_on_populated_terminal_pm2_process(self):
        for status in ("errored", "stopped"):
            with self.subTest(status=status):
                terminal = {
                    "name": "fixture-new",
                    "pm2_env": {
                        "status": status,
                        "pm_cwd": "/deployed/runtime",
                        "script": "python3",
                        "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                    },
                }
                with (
                    patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
                    patch.object(deploy_runtime, "pm2_jlist", return_value=[terminal]) as pm2_jlist,
                    patch.object(deploy_runtime.time, "monotonic", return_value=0.0),
                    patch.object(deploy_runtime.time, "sleep") as sleep,
                ):
                    with self.assertRaisesRegex(deploy_runtime.DeployError, "not online"):
                        deploy_runtime.verify_deployment(
                            Path("/deployed/runtime"),
                            "new-sha",
                            frozenset({"fixture-new"}),
                            {
                                "fixture-new": {
                                    "cwd": "/deployed/runtime",
                                    "script": "python3",
                                    "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                                }
                            },
                        )

                pm2_jlist.assert_called_once_with()
                sleep.assert_not_called()

    def test_verify_timeout_names_missing_pm2_process(self):
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", return_value=[]) as pm2_jlist,
            patch.object(deploy_runtime.time, "monotonic", side_effect=[0.0, 10.0]),
            patch.object(deploy_runtime.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(deploy_runtime.DeployError, r"missing=\['fixture-new'\]"):
                deploy_runtime.verify_deployment(
                    Path("/deployed/runtime"),
                    "new-sha",
                    frozenset({"fixture-new"}),
                    {
                        "fixture-new": {
                            "cwd": "/deployed/runtime",
                            "script": "python3",
                            "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
                        }
                    },
                )

        pm2_jlist.assert_called_once_with()
        sleep.assert_not_called()

    def test_managed_processes_does_not_match_related_workspace_names(self):
        records = [
            {"name": "foo-worker"},
            {"name": "foo-bar-worker"},
        ]

        self.assertEqual(
            {"foo-worker": records[0]},
            deploy_runtime.managed_processes(records, frozenset({"foo-worker"})),
        )

    def test_pm2_jlist_rejects_oversized_output_before_json_parse(self):
        with patch.object(deploy_runtime, "PM2_JLIST_MAX_BYTES", 4), patch.object(
            deploy_runtime,
            "pm2_run",
            return_value=subprocess.CompletedProcess([], 0, "[]xxx", ""),
        ):
            with self.assertRaisesRegex(deploy_runtime.DeployError, "refusing to parse"):
                deploy_runtime.pm2_jlist()

    def test_pm2_run_bounded_rejects_oversized_stdout(self):
        with patch.object(deploy_runtime, "pm2_binary", return_value=sys.executable):
            with self.assertRaisesRegex(deploy_runtime.DeployError, "exceeds 16 bytes"):
                deploy_runtime.pm2_run(
                    ["-c", "import sys; sys.stdout.write('x' * 128)"],
                    max_output_bytes=16,
                )

    def test_source_and_target_must_differ(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            with self.assertRaises(deploy_runtime.DeployError):
                deploy_runtime.deploy(path, path)


if __name__ == "__main__":
    unittest.main()
