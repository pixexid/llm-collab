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
                patch.object(deploy_runtime, "pm2_jlist", return_value=[]),
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
                patch.object(deploy_runtime, "pm2_jlist", return_value=[]),
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

    def test_reconcile_propagates_ecosystem_restart_failure(self):
        # Sibling of GH-678 in the same file: a non-zero startOrRestart must not
        # be swallowed. deploy_runtime.py routes every pm2 call through pm2_run,
        # which raises on a non-zero exit -- so unlike a raw subprocess call, the
        # restart failure propagates out of reconcile_pm2 as a DeployError (and in
        # deploy() reaches the try/except rollback, already covered by
        # test_deploy_restores_previous_state_after_verification_failure). Real
        # pm2_run is exercised: delete succeeds (exit 0), startOrRestart fails
        # (exit 1).
        def fake_run(cmd, **kwargs):
            if "startOrRestart" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "ecosystem restart failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            patch.object(deploy_runtime, "pm2_binary", return_value="/usr/bin/pm2"),
            patch.object(deploy_runtime.subprocess, "run", side_effect=fake_run),
            patch.object(
                deploy_runtime,
                "pm2_jlist",
                side_effect=[[{"name": "fixture-old", "pm2_env": {"status": "stopped"}}], []],
            ),
        ):
            with self.assertRaisesRegex(
                deploy_runtime.DeployError, r"startOrRestart.*ecosystem restart failed"
            ):
                deploy_runtime.reconcile_pm2(
                    Path("/deployed/runtime"),
                    frozenset({"fixture-old", "fixture-new"}),
                    {"fixture-new": {"name": "fixture-new"}},
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
        terminal = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "errored",
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

    def test_verify_waits_for_populated_stopped_pm2_process(self):
        stopped = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "stopped",
                "pm_cwd": "/deployed/runtime",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
            },
        }
        online = {"name": "fixture-new", "pm2_env": {**stopped["pm2_env"], "status": "online"}}
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", side_effect=[[stopped], [online]]) as pm2_jlist,
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

    def test_verify_times_out_on_persistent_stopped_pm2_process(self):
        stopped = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "stopped",
                "pm_cwd": "/deployed/runtime",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
            },
        }
        with (
            patch.object(deploy_runtime, "git", side_effect=["new-sha", ""]),
            patch.object(deploy_runtime, "pm2_jlist", side_effect=[[stopped], [stopped], [stopped]]) as pm2_jlist,
            patch.object(deploy_runtime.time, "monotonic", side_effect=[0.0, 0.0, 0.25, 10.0]),
            patch.object(deploy_runtime.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                deploy_runtime.DeployError,
                r"not_ready=fixture-new: status='stopped'",
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

    def test_refuse_blocks_interpreter_as_script_watcher(self):
        # The live declaration shape: the ecosystem script IS the python interpreter
        # and the watch file is its operand. PM2 reports pm_exec_path as the
        # resolved interpreter binary (NOT the watcher) and exec_interpreter as
        # 'none', so the structured field pm_exec_path does not name the watcher
        # here -- the python-interpreter operand branch is what catches it.
        live_watcher = {
            "name": "collab-shadow",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/usr/bin/python3",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "shadow"],
            },
        }
        with self.assertRaisesRegex(
            deploy_runtime.DeployError,
            r"undeclared PM2 process\(es\) are running the deployed runtime watcher.*collab-shadow",
        ):
            deploy_runtime.refuse_undeclared_runtime_watchers(
                Path("/deployed/runtime"), set(), [live_watcher]
            )

    def test_refuse_blocks_direct_script_watcher(self):
        # The structured-field shape: a process whose pm_exec_path IS the watcher.
        # PM2 resolved the script to the watch file (and may set exec_interpreter
        # from the .py extension). pm_exec_path == watcher is PM2's own resolved
        # answer to "what file does this execute", so no argv reasoning is needed.
        direct = {
            "name": "collab-direct",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/deployed/runtime/bin/watch_inbox.py",
                "script": "bin/watch_inbox.py",
                "args": ["--me", "direct"],
            },
        }
        with self.assertRaisesRegex(
            deploy_runtime.DeployError,
            r"undeclared PM2 process\(es\) are running the deployed runtime watcher.*collab-direct",
        ):
            deploy_runtime.refuse_undeclared_runtime_watchers(
                Path("/deployed/runtime"), set(), [direct]
            )

    def test_refuse_blocks_relative_operand_orphan(self):
        # GH-675 shape: the orphan ran a RELATIVE watch path from cwd = <runtime>.
        # The operand resolves against pm_cwd, so the property (a process executing
        # that script) holds without a name match.
        orphan = {
            "name": "collab-gh562-case5-disposable",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/usr/bin/python3",
                "script": "python3",
                "args": ["bin/watch_inbox.py", "--me", "ghost"],
            },
        }
        with self.assertRaisesRegex(
            deploy_runtime.DeployError,
            r"undeclared PM2 process\(es\) are running the deployed runtime watcher",
        ):
            deploy_runtime.refuse_undeclared_runtime_watchers(
                Path("/deployed/runtime"), set(), [orphan]
            )

    def test_refuse_blocks_watcher_behind_python_options(self):
        # GH-679 fail-open: `python3 -W ignore <watcher>` -- the old "skip leading
        # - tokens" rule made 'ignore' the program, so a live undeclared watcher
        # PASSED conformance. Correct option arity (-W takes the next token as its
        # value) must reach the watcher. -X opt and the -- terminator exercise the
        # same parser paths.
        for argv in (
            ["-W", "ignore", "/deployed/runtime/bin/watch_inbox.py", "--me", "x"],
            ["-X", "faulthandler", "/deployed/runtime/bin/watch_inbox.py"],
            ["-u", "--", "/deployed/runtime/bin/watch_inbox.py"],
        ):
            record = {
                "name": "collab-opts",
                "pm2_env": {
                    "status": "online",
                    "pm_cwd": "/deployed/runtime",
                    "pm_exec_path": "/usr/bin/python3",
                    "script": "python3",
                    "args": argv,
                },
            }
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(
                    deploy_runtime.DeployError,
                    r"undeclared PM2 process\(es\) are running the deployed runtime watcher",
                ):
                    deploy_runtime.refuse_undeclared_runtime_watchers(
                        Path("/deployed/runtime"), set(), [record]
                    )

    def test_python_program_operand_parser(self):
        # Direct unit proof of the option-arity walk for every Python 3.11 shape
        # the discriminator must survive. -c/-m run no file; -W/-X consume their
        # value; -- ends options; flags consume nothing.
        operand = deploy_runtime._python_program_operand
        self.assertEqual(operand(["script.py"]), "script.py")
        self.assertEqual(operand(["-u", "script.py"]), "script.py")
        self.assertEqual(operand(["-W", "ignore", "script.py"]), "script.py")
        self.assertEqual(operand(["-Wignore", "script.py"]), "script.py")
        self.assertEqual(operand(["-X", "faulthandler", "script.py"]), "script.py")
        self.assertEqual(operand(["--", "script.py"]), "script.py")
        self.assertEqual(operand(["--", "-W", "script.py"]), "-W")  # past terminator
        self.assertIsNone(operand(["-c", "print(1)"]))
        self.assertIsNone(operand(["-m", "package.module"]))
        self.assertIsNone(operand(["-"]))
        self.assertIsNone(operand(["-W", "ignore"]))

    def test_refuse_ignores_non_interpreter_with_watcher_as_first_arg(self):
        # GH-679 over-match: returning the first argument without checking that
        # the script is an interpreter classified `cat <watcher>` as executing the
        # watcher and refused every deploy. cat reads the file as DATA; pm_exec_path
        # is /bin/cat, which is not a python interpreter, so the operand branch
        # never runs.
        cat = {
            "name": "unrelated-ingest",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/bin/cat",
                "script": "cat",
                "args": ["/deployed/runtime/bin/watch_inbox.py"],
            },
        }
        # Must NOT raise: the watcher path is data to a non-interpreter.
        deploy_runtime.refuse_undeclared_runtime_watchers(
            Path("/deployed/runtime"), set(), [cat]
        )

    def test_refuse_ignores_watcher_as_data_argument(self):
        # A python process whose PROGRAM is a different script, receiving the
        # watcher path only as a later data argument, is not executing it. The
        # operand is the first program token (other_tool.py); parsing stops there,
        # so the watcher in a later position is never considered. A "watcher
        # anywhere in args" rule would fail this.
        data_arg = {
            "name": "unrelated-tool",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/usr/bin/python3",
                "script": "python3",
                "args": [
                    "/deployed/runtime/bin/other_tool.py",
                    "--input",
                    "/deployed/runtime/bin/watch_inbox.py",
                ],
            },
        }
        deploy_runtime.refuse_undeclared_runtime_watchers(
            Path("/deployed/runtime"), set(), [data_arg]
        )

    def test_refuse_ignores_stopped_watcher(self):
        # HEAD-2 finding 1: a stopped process dispatches nothing and pm2 save
        # resurrects it stopped, not live, so it must not block a deploy.
        stopped = {
            "name": "collab-stopped",
            "pm2_env": {
                "status": "stopped",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/usr/bin/python3",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py"],
            },
        }
        deploy_runtime.refuse_undeclared_runtime_watchers(
            Path("/deployed/runtime"), set(), [stopped]
        )

    def test_refuse_ignores_unrelated_entries(self):
        # The other direction of GH-675: none of these execute THIS runtime's
        # watcher -- an unrelated host app (node), a collab-named process running a
        # different script, and a DIFFERENT runtime's watch_inbox.py whose operand
        # resolves to a different path.
        unrelated = [
            {
                "name": "other-project-worker",
                "pm2_env": {
                    "status": "online",
                    "pm_cwd": "/elsewhere",
                    "pm_exec_path": "/usr/bin/node",
                    "script": "node",
                    "args": ["/elsewhere/server.js"],
                },
            },
            {
                "name": "collab-lookalike",
                "pm2_env": {
                    "status": "online",
                    "pm_cwd": "/deployed/runtime",
                    "pm_exec_path": "/usr/bin/node",
                    "script": "node",
                    "args": ["/deployed/runtime/bin/some_other_tool.js"],
                },
            },
            {
                "name": "stale-other-runtime",
                "pm2_env": {
                    "status": "online",
                    "pm_cwd": "/other/runtime",
                    "pm_exec_path": "/usr/bin/python3",
                    "script": "python3",
                    "args": ["/other/runtime/bin/watch_inbox.py", "--me", "ghost"],
                },
            },
        ]
        deploy_runtime.refuse_undeclared_runtime_watchers(
            Path("/deployed/runtime"), set(), unrelated
        )

    def test_refuse_does_not_implicate_declared_watcher(self):
        # A DECLARED process running the watcher is not an offender: declared names
        # are filtered before the execution check. Guards the property that only
        # UNDECLARED dispatchers are refused.
        declared_watcher = {
            "name": "fixture-new",
            "pm2_env": {
                "status": "online",
                "pm_cwd": "/deployed/runtime",
                "pm_exec_path": "/usr/bin/python3",
                "script": "python3",
                "args": ["/deployed/runtime/bin/watch_inbox.py", "--me", "codex"],
            },
        }
        deploy_runtime.refuse_undeclared_runtime_watchers(
            Path("/deployed/runtime"), {"fixture-new"}, [declared_watcher]
        )

    def test_deploy_refuses_before_first_mutation_when_undeclared_watcher_live(self):
        # GH-679 ordering: the gate must precede the FIRST mutation. No roster
        # fencing (fence_watchers) and no code replacement (reset_target) may run
        # while an undeclared live dispatcher is present, and rollback is never
        # reached because nothing was mutated to roll back.
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            target = Path(temp_dir) / "target"
            source.mkdir()
            target.mkdir()
            (source / ".git").mkdir()
            (target / ".git").mkdir()
            # The watch script must resolve against the ACTUAL target deploy() uses
            # (deploy calls .resolve() on target, which follows the macOS
            # /var -> /private/var symlink), since refuse_undeclared_runtime_watchers
            # builds the path from the resolved target.
            resolved_target = target.resolve()
            watch_script = str(resolved_target / "bin" / "watch_inbox.py")
            live_shadow = {
                "name": "collab-shadow",
                "pm2_env": {
                    "status": "online",
                    "pm_cwd": str(resolved_target),
                    "pm_exec_path": "/usr/bin/python3",
                    "script": "python3",
                    "args": [watch_script, "--me", "shadow"],
                },
            }
            with (
                patch.object(deploy_runtime, "source_head", return_value=("new-sha", "10")),
                patch.object(deploy_runtime, "target_preflight", return_value="old-sha"),
                patch.object(deploy_runtime, "pm2_binary", return_value="/usr/bin/pm2"),
                patch.object(deploy_runtime, "ecosystem_definitions", return_value={}),
                patch.object(deploy_runtime, "pm2_jlist", return_value=[live_shadow]),
                patch.object(
                    deploy_runtime, "fence_watchers",
                    side_effect=lambda *a, **k: events.append("fence"),
                ),
                patch.object(
                    deploy_runtime, "reset_target",
                    side_effect=lambda *a, **k: events.append("reset"),
                ),
                patch.object(
                    deploy_runtime, "reconcile_pm2",
                    side_effect=lambda *a, **k: events.append("reconcile"),
                ),
                patch.object(
                    deploy_runtime, "restore_previous_deployment",
                    side_effect=lambda *a, **k: events.append("restore"),
                ),
                patch.object(deploy_runtime, "pm2_run"),
            ):
                with self.assertRaisesRegex(
                    deploy_runtime.DeployError,
                    r"undeclared PM2 process\(es\) are running the deployed runtime watcher",
                ):
                    deploy_runtime.deploy(source, target)
        # No mutation ran: not fence, not reset (code replacement), and rollback
        # was never invoked.
        self.assertEqual([], events)


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

    def test_pm2_run_raises_on_nonzero_exit(self):
        # The defense for "a failed ecosystem restart is not swallowed": pm2_run
        # converts ANY non-zero PM2 exit into a DeployError before returning, so
        # the startOrRestart call in reconcile_pm2 cannot silently succeed. Both
        # the unbounded path (subprocess.run) and the bounded path
        # (_pm2_run_bounded) route through this one returncode check.
        failing = subprocess.CompletedProcess(
            ["pm2", "startOrRestart", "ecosystem.config.cjs", "--update-env"],
            1,
            "",
            "ecosystem restart failed",
        )
        with (
            patch.object(deploy_runtime, "pm2_binary", return_value="/usr/bin/pm2"),
            patch.object(deploy_runtime.subprocess, "run", return_value=failing),
        ):
            with self.assertRaisesRegex(
                deploy_runtime.DeployError, r"startOrRestart.*ecosystem restart failed"
            ):
                deploy_runtime.pm2_run(
                    ["startOrRestart", "ecosystem.config.cjs", "--update-env"]
                )

    def test_source_and_target_must_differ(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            with self.assertRaises(deploy_runtime.DeployError):
                deploy_runtime.deploy(path, path)


if __name__ == "__main__":
    unittest.main()
