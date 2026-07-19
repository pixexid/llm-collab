from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deploy_release_watch as watch


DF55 = "df55a282b0" + "0" * 30            # incident merge SHA (padded fixture form)
SEVEN_E = "7e677225a8" + "0" * 30          # later unrelated merge SHA


def run_fixture(run_id: int, sha: str, *, status: str = "completed",
                conclusion: str | None = "success", attempt: int = 1,
                event: str = "push", branch: str = "main") -> dict:
    return {"id": run_id, "head_sha": sha, "status": status,
            "conclusion": conclusion, "run_attempt": attempt,
            "event": event, "head_branch": branch,
            "name": "deploy", "path": ".github/workflows/deploy.yml"}


SMOKE_GREEN = [{"name": "Verify production hosts", "conclusion": "success"},
               {"name": "Verify production auth", "conclusion": "success"}]


def jobs_all_green(_run_id: int) -> list[dict]:
    return [{"name": "detect", "conclusion": "success", "steps": []},
            {"name": "deploy", "conclusion": "success", "steps": list(SMOKE_GREEN)}]


# Amiga-shaped evidence identity, injected the way main() injects the resolved
# projects.json release_closure config (shared bin/ carries no such constants).
EVIDENCE = {"required_jobs": ("detect", "deploy"), "smoke_job": "deploy",
            "required_smoke_steps": ("Verify production hosts", "Verify production auth")}


def evaluate(merge_sha, runs, jobs_for_run, **overrides):
    return watch.evaluate_release(merge_sha, runs, jobs_for_run, **{**EVIDENCE, **overrides})


def project_fixture(**overrides) -> dict:
    project = {
        "id": "amiga",
        "default_branch_base": "main",
        "github": {"enabled": True, "repo": "pixexid/amiga"},
        "release_closure": {
            "workflow": "deploy",
            "required_jobs": ["detect", "deploy"],
            "smoke_job": "deploy",
            "required_smoke_steps": ["Verify production hosts", "Verify production auth"],
        },
    }
    project.update(overrides)
    return project


class DeployReleaseWatchVerdictTest(unittest.TestCase):
    """GH-1524 verification plan, driven through the production evaluate_release
    logic with fixtures replicating the REAL incident shape."""

    def test_real_incident_failure_fires_the_alarm(self) -> None:
        # Run 29537490993 @ df55a282: build+upload succeeded, the post-deploy
        # smoke step failed, run conclusion = failure. The alarm must fire.
        runs = [run_fixture(29537490993, DF55, conclusion="failure")]
        verdict = evaluate(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertEqual(verdict.run_id, 29537490993)

    def test_mismatched_success_never_satisfies_another_merge(self) -> None:
        # 7e677225's success (run 29545634805) must NOT satisfy df55a282's
        # closure. The exact-SHA fetch would never return it; if a caller ever
        # passes it anyway, evaluation fails closed instead of passing.
        mismatched = [run_fixture(29545634805, SEVEN_E, conclusion="success")]
        with self.assertRaises(ValueError):
            evaluate(DF55, mismatched, jobs_all_green)

    def test_missing_run_is_actionable_not_pass(self) -> None:
        verdict = evaluate(DF55, [], jobs_all_green)
        self.assertEqual(verdict.state, "MISSING")
        self.assertIn("actionable", verdict.detail)

    def test_clean_success_closes_without_false_alarm(self) -> None:
        runs = [run_fixture(29545634805, SEVEN_E, conclusion="success")]
        verdict = evaluate(SEVEN_E, runs, jobs_all_green)
        self.assertEqual(verdict.state, "SUCCESS")
        self.assertEqual(verdict.run_id, 29545634805)

    def test_run_success_with_failed_job_is_failure(self) -> None:
        # Run-level success with a red job must not read as a clean release.
        runs = [run_fixture(1, SEVEN_E, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success"},
                    {"name": "deploy", "conclusion": "failure"}]

        verdict = evaluate(SEVEN_E, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("required job 'deploy' concluded 'failure'", "; ".join(verdict.failed_jobs))

    def test_cancelled_is_its_own_state(self) -> None:
        runs = [run_fixture(2, DF55, conclusion="cancelled")]
        verdict = evaluate(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "CANCELLED")

    def test_in_progress_run_is_pending_not_success(self) -> None:
        runs = [run_fixture(3, DF55, status="in_progress", conclusion=None)]
        verdict = evaluate(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "PENDING")

    def test_latest_attempt_wins_over_stale_failure(self) -> None:
        # A rerun that succeeded supersedes the earlier failed attempt of the
        # SAME sha (latest-per-context), while a stale success never covers a
        # newer failed attempt.
        runs = [run_fixture(4, DF55, conclusion="failure", attempt=1),
                run_fixture(4, DF55, conclusion="success", attempt=2)]
        self.assertEqual(evaluate(DF55, runs, jobs_all_green).state, "SUCCESS")
        runs_rev = [run_fixture(5, DF55, conclusion="success", attempt=1),
                    run_fixture(5, DF55, conclusion="failure", attempt=2)]
        self.assertEqual(evaluate(DF55, runs_rev, jobs_all_green).state, "FAILURE")

    def test_exit_code_map_is_distinct_per_state(self) -> None:
        self.assertEqual(len(set(watch.TERMINAL_EXIT.values())), len(watch.TERMINAL_EXIT))
        self.assertEqual(watch.TERMINAL_EXIT["SUCCESS"], 0)

    def test_failure_render_carries_gate_instructions(self) -> None:
        runs = [run_fixture(29537490993, DF55, conclusion="failure")]
        verdict = evaluate(DF55, runs, jobs_all_green)
        text = watch.render(verdict, "pixexid/amiga")
        self.assertIn("--log-failed", text)
        self.assertIn("do NOT blind-retry or redeploy", text)
        self.assertIn("Codex", text)

    def test_missing_render_names_the_actionable_duty(self) -> None:
        verdict = evaluate(DF55, [], jobs_all_green)
        text = watch.render(verdict, "pixexid/amiga")
        self.assertIn("ONE durable packet", text)
        self.assertIn("never treat as pass", text)


class DeployReleaseWatchFalseGreenTest(unittest.TestCase):
    """GH-1524 cold-review P1 family: every false-green path must fail closed."""

    def test_same_sha_workflow_dispatch_success_never_satisfies_closure(self) -> None:
        # A manual dispatch on the same SHA cannot cover the automatic push
        # run's outcome — that would launder a blind retry into a green release.
        dispatch = [run_fixture(9, DF55, conclusion="success", event="workflow_dispatch")]
        verdict = evaluate(DF55, dispatch, jobs_all_green)
        self.assertEqual(verdict.state, "MISSING")
        self.assertIn("never satisfy closure", verdict.detail)

    def test_dispatch_success_does_not_supersede_automatic_failure(self) -> None:
        runs = [run_fixture(10, DF55, conclusion="failure", event="push"),
                run_fixture(11, DF55, conclusion="success", event="workflow_dispatch")]
        verdict = evaluate(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertEqual(verdict.run_id, 10)

    def test_off_branch_push_run_does_not_count(self) -> None:
        runs = [run_fixture(12, DF55, conclusion="success", branch="feature-x")]
        self.assertEqual(evaluate(DF55, runs, jobs_all_green).state, "MISSING")

    def test_empty_jobs_payload_fails_closed(self) -> None:
        runs = [run_fixture(13, DF55, conclusion="success")]
        verdict = evaluate(DF55, runs, lambda _: [])
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("required job 'detect' missing", "; ".join(verdict.failed_jobs))

    def test_detect_only_payload_fails_closed(self) -> None:
        runs = [run_fixture(14, DF55, conclusion="success")]
        verdict = evaluate(
            DF55, runs, lambda _: [{"name": "detect", "conclusion": "success", "steps": []}])
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("required job 'deploy' missing", "; ".join(verdict.failed_jobs))

    def test_skipped_deploy_is_not_a_release(self) -> None:
        runs = [run_fixture(15, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "skipped", "steps": []}]

        verdict = evaluate(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("'deploy' concluded 'skipped'", "; ".join(verdict.failed_jobs))

    def test_missing_smoke_step_fails_closed(self) -> None:
        runs = [run_fixture(16, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "success",
                     "steps": [{"name": "Verify production hosts", "conclusion": "success"}]}]

        verdict = evaluate(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("Verify production auth' missing", "; ".join(verdict.failed_jobs))

    def test_failed_smoke_step_fails_closed(self) -> None:
        runs = [run_fixture(17, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "success",
                     "steps": [{"name": "Verify production hosts", "conclusion": "success"},
                               {"name": "Verify production auth", "conclusion": "failure"}]}]

        verdict = evaluate(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("Verify production auth' concluded 'failure'", "; ".join(verdict.failed_jobs))


class DeployReleaseWatchEnvironmentTest(unittest.TestCase):
    """GH-1524 PR #111 review round: environment and multi-project safety."""

    def test_missing_gh_executable_is_runtime_error_not_traceback(self) -> None:
        # OSError (missing/unexecutable gh binary) must surface as the
        # documented environment error (exit 64 path), not a raw traceback.
        with self.assertRaises(RuntimeError) as ctx:
            watch.run_command(["definitely-not-a-real-binary-1524"])
        self.assertIn("cannot execute", str(ctx.exception))

    def test_duplicate_required_job_names_fail_closed(self) -> None:
        # A green duplicate of a required job name must never whitewash a red
        # one (paginated evidence could otherwise last-write-wins the dict).
        runs = [run_fixture(18, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "failure", "steps": []},
                    {"name": "deploy", "conclusion": "success", "steps": list(SMOKE_GREEN)}]

        verdict = evaluate(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("ambiguous evidence: job name 'deploy'", "; ".join(verdict.failed_jobs))


class ReleaseConfigResolutionTest(unittest.TestCase):
    """PR #111 round-1 P1: evidence identity comes from the registered project
    config, never from shared-bin constants; anything unconfigured fails closed."""

    def test_registered_project_resolves_full_identity(self) -> None:
        config, error = watch.resolve_release_config("amiga", project_fixture())
        self.assertIsNone(error)
        self.assertEqual(config["repo"], "pixexid/amiga")
        self.assertEqual(config["branch"], "main")
        self.assertEqual(config["workflow"], "deploy")
        self.assertEqual(config["required_jobs"], ("detect", "deploy"))
        self.assertEqual(config["smoke_job"], "deploy")
        self.assertEqual(config["required_smoke_steps"],
                         ("Verify production hosts", "Verify production auth"))

    def test_unknown_project_fails_closed(self) -> None:
        config, error = watch.resolve_release_config("ghost", None)
        self.assertIsNone(config)
        self.assertIn("unknown project_id", error)

    def test_project_without_release_closure_fails_closed(self) -> None:
        project = project_fixture()
        del project["release_closure"]
        config, error = watch.resolve_release_config("nuvyr", project)
        self.assertIsNone(config)
        self.assertIn("no release_closure config", error)

    def test_partial_release_closure_fails_closed(self) -> None:
        project = project_fixture(release_closure={"required_jobs": ["detect", "deploy"]})
        config, error = watch.resolve_release_config("amiga", project)
        self.assertIsNone(config)
        self.assertIn("no release_closure config", error)

    def test_project_without_enabled_github_repo_fails_closed(self) -> None:
        project = project_fixture(github={"enabled": False, "repo": "pixexid/amiga"})
        config, error = watch.resolve_release_config("amiga", project)
        self.assertIsNone(config)
        self.assertIn("no enabled github.repo", error)

    def test_no_shared_bin_evidence_constants_remain(self) -> None:
        for name in ("REPO_PROFILES", "REQUIRED_JOBS", "REQUIRED_SMOKE_STEPS"):
            self.assertFalse(hasattr(watch, name),
                             f"{name} must not exist — project values in shared bin/")


class DeployReleaseWatchFetchTest(unittest.TestCase):
    def test_fetch_filters_to_the_named_workflow(self) -> None:
        payload = [
            {"id": 1, "name": "deploy", "path": ".github/workflows/deploy.yml"},
            {"id": 2, "name": "verify", "path": ".github/workflows/verify.yml"},
        ]
        import json as _json

        def fake_runner(argv):
            self.assertIn("--paginate", argv)
            self.assertNotIn("--slurp", argv)  # incompatible with --jq on current gh
            self.assertNotIn("--jq", argv)
            self.assertTrue(any("head_sha=" + DF55 in a for a in argv))
            # Two concatenated page documents, as gh api --paginate emits them.
            return (_json.dumps({"workflow_runs": payload[:1]})
                    + _json.dumps({"workflow_runs": payload[1:]}))

        runs = watch.fetch_deploy_runs("pixexid/amiga", DF55, "deploy", runner=fake_runner)
        self.assertEqual([r["id"] for r in runs], [1])

    def test_jobs_fetch_merges_pages_and_normalizes_steps(self) -> None:
        import json as _json

        page1 = {"jobs": [{"name": "detect", "conclusion": "success", "steps": []}]}
        page2 = {"jobs": [{"name": "deploy", "conclusion": "failure",
                           "steps": [{"name": "Verify production auth",
                                      "conclusion": "failure", "number": 9}]}]}

        def fake_runner(argv):
            self.assertIn("--paginate", argv)
            return _json.dumps(page1) + _json.dumps(page2)

        jobs = watch.fetch_run_jobs("pixexid/amiga", 1, runner=fake_runner)
        self.assertEqual([j["name"] for j in jobs], ["detect", "deploy"])
        self.assertEqual(jobs[1]["steps"][0]["conclusion"], "failure")

    def test_required_evidence_on_a_later_page_counts_both_ways(self) -> None:
        # The required deploy job (with its smoke steps) arriving only on a
        # later page must satisfy the gate when green — and a later-page
        # failure must fail it (never judged on page 1 alone).
        import json as _json

        def paged_runner(deploy_job):
            page1 = {"jobs": [{"name": "detect", "conclusion": "success", "steps": []}]}
            page2 = {"jobs": [deploy_job]}
            return lambda argv: _json.dumps(page1) + _json.dumps(page2)

        runs = [run_fixture(19, DF55, conclusion="success")]
        green = {"name": "deploy", "conclusion": "success", "steps": list(SMOKE_GREEN)}
        jobs_green = lambda run_id: watch.fetch_run_jobs("pixexid/amiga", run_id,
                                                         runner=paged_runner(green))
        self.assertEqual(evaluate(DF55, runs, jobs_green).state, "SUCCESS")

        red = {"name": "deploy", "conclusion": "failure", "steps": []}
        jobs_red = lambda run_id: watch.fetch_run_jobs("pixexid/amiga", run_id,
                                                       runner=paged_runner(red))
        self.assertEqual(evaluate(DF55, runs, jobs_red).state, "FAILURE")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
