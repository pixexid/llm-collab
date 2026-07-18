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


class DeployReleaseWatchVerdictTest(unittest.TestCase):
    """GH-1524 verification plan, driven through the production evaluate_release
    logic with fixtures replicating the REAL incident shape."""

    def test_real_incident_failure_fires_the_alarm(self) -> None:
        # Run 29537490993 @ df55a282: build+upload succeeded, the post-deploy
        # smoke step failed, run conclusion = failure. The alarm must fire.
        runs = [run_fixture(29537490993, DF55, conclusion="failure")]
        verdict = watch.evaluate_release(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertEqual(verdict.run_id, 29537490993)

    def test_mismatched_success_never_satisfies_another_merge(self) -> None:
        # 7e677225's success (run 29545634805) must NOT satisfy df55a282's
        # closure. The exact-SHA fetch would never return it; if a caller ever
        # passes it anyway, evaluation fails closed instead of passing.
        mismatched = [run_fixture(29545634805, SEVEN_E, conclusion="success")]
        with self.assertRaises(ValueError):
            watch.evaluate_release(DF55, mismatched, jobs_all_green)

    def test_missing_run_is_actionable_not_pass(self) -> None:
        verdict = watch.evaluate_release(DF55, [], jobs_all_green)
        self.assertEqual(verdict.state, "MISSING")
        self.assertIn("actionable", verdict.detail)

    def test_clean_success_closes_without_false_alarm(self) -> None:
        runs = [run_fixture(29545634805, SEVEN_E, conclusion="success")]
        verdict = watch.evaluate_release(SEVEN_E, runs, jobs_all_green)
        self.assertEqual(verdict.state, "SUCCESS")
        self.assertEqual(verdict.run_id, 29545634805)

    def test_run_success_with_failed_job_is_failure(self) -> None:
        # Run-level success with a red job must not read as a clean release.
        runs = [run_fixture(1, SEVEN_E, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success"},
                    {"name": "deploy", "conclusion": "failure"}]

        verdict = watch.evaluate_release(SEVEN_E, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("required job 'deploy' concluded 'failure'", "; ".join(verdict.failed_jobs))

    def test_cancelled_is_its_own_state(self) -> None:
        runs = [run_fixture(2, DF55, conclusion="cancelled")]
        verdict = watch.evaluate_release(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "CANCELLED")

    def test_in_progress_run_is_pending_not_success(self) -> None:
        runs = [run_fixture(3, DF55, status="in_progress", conclusion=None)]
        verdict = watch.evaluate_release(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "PENDING")

    def test_latest_attempt_wins_over_stale_failure(self) -> None:
        # A rerun that succeeded supersedes the earlier failed attempt of the
        # SAME sha (latest-per-context), while a stale success never covers a
        # newer failed attempt.
        runs = [run_fixture(4, DF55, conclusion="failure", attempt=1),
                run_fixture(4, DF55, conclusion="success", attempt=2)]
        self.assertEqual(watch.evaluate_release(DF55, runs, jobs_all_green).state, "SUCCESS")
        runs_rev = [run_fixture(5, DF55, conclusion="success", attempt=1),
                    run_fixture(5, DF55, conclusion="failure", attempt=2)]
        self.assertEqual(watch.evaluate_release(DF55, runs_rev, jobs_all_green).state, "FAILURE")

    def test_exit_code_map_is_distinct_per_state(self) -> None:
        self.assertEqual(len(set(watch.TERMINAL_EXIT.values())), len(watch.TERMINAL_EXIT))
        self.assertEqual(watch.TERMINAL_EXIT["SUCCESS"], 0)

    def test_failure_render_carries_gate_instructions(self) -> None:
        runs = [run_fixture(29537490993, DF55, conclusion="failure")]
        verdict = watch.evaluate_release(DF55, runs, jobs_all_green)
        text = watch.render(verdict, "pixexid/amiga")
        self.assertIn("--log-failed", text)
        self.assertIn("do NOT blind-retry or redeploy", text)
        self.assertIn("Codex", text)

    def test_missing_render_names_the_actionable_duty(self) -> None:
        verdict = watch.evaluate_release(DF55, [], jobs_all_green)
        text = watch.render(verdict, "pixexid/amiga")
        self.assertIn("ONE durable packet", text)
        self.assertIn("never treat as pass", text)


class DeployReleaseWatchFalseGreenTest(unittest.TestCase):
    """GH-1524 cold-review P1 family: every false-green path must fail closed."""

    def test_same_sha_workflow_dispatch_success_never_satisfies_closure(self) -> None:
        # A manual dispatch on the same SHA cannot cover the automatic push
        # run's outcome — that would launder a blind retry into a green release.
        dispatch = [run_fixture(9, DF55, conclusion="success", event="workflow_dispatch")]
        verdict = watch.evaluate_release(DF55, dispatch, jobs_all_green)
        self.assertEqual(verdict.state, "MISSING")
        self.assertIn("never satisfy closure", verdict.detail)

    def test_dispatch_success_does_not_supersede_automatic_failure(self) -> None:
        runs = [run_fixture(10, DF55, conclusion="failure", event="push"),
                run_fixture(11, DF55, conclusion="success", event="workflow_dispatch")]
        verdict = watch.evaluate_release(DF55, runs, jobs_all_green)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertEqual(verdict.run_id, 10)

    def test_off_branch_push_run_does_not_count(self) -> None:
        runs = [run_fixture(12, DF55, conclusion="success", branch="feature-x")]
        self.assertEqual(watch.evaluate_release(DF55, runs, jobs_all_green).state, "MISSING")

    def test_empty_jobs_payload_fails_closed(self) -> None:
        runs = [run_fixture(13, DF55, conclusion="success")]
        verdict = watch.evaluate_release(DF55, runs, lambda _: [])
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("required job 'detect' missing", "; ".join(verdict.failed_jobs))

    def test_detect_only_payload_fails_closed(self) -> None:
        runs = [run_fixture(14, DF55, conclusion="success")]
        verdict = watch.evaluate_release(
            DF55, runs, lambda _: [{"name": "detect", "conclusion": "success", "steps": []}])
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("required job 'deploy' missing", "; ".join(verdict.failed_jobs))

    def test_skipped_deploy_is_not_a_release(self) -> None:
        runs = [run_fixture(15, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "skipped", "steps": []}]

        verdict = watch.evaluate_release(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("'deploy' concluded 'skipped'", "; ".join(verdict.failed_jobs))

    def test_missing_smoke_step_fails_closed(self) -> None:
        runs = [run_fixture(16, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "success",
                     "steps": [{"name": "Verify production hosts", "conclusion": "success"}]}]

        verdict = watch.evaluate_release(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("Verify production auth' missing", "; ".join(verdict.failed_jobs))

    def test_failed_smoke_step_fails_closed(self) -> None:
        runs = [run_fixture(17, DF55, conclusion="success")]

        def jobs(_):
            return [{"name": "detect", "conclusion": "success", "steps": []},
                    {"name": "deploy", "conclusion": "success",
                     "steps": [{"name": "Verify production hosts", "conclusion": "success"},
                               {"name": "Verify production auth", "conclusion": "failure"}]}]

        verdict = watch.evaluate_release(DF55, runs, jobs)
        self.assertEqual(verdict.state, "FAILURE")
        self.assertIn("Verify production auth' concluded 'failure'", "; ".join(verdict.failed_jobs))


class DeployReleaseWatchFetchTest(unittest.TestCase):
    def test_fetch_filters_to_the_named_workflow(self) -> None:
        payload = [
            {"id": 1, "name": "deploy", "path": ".github/workflows/deploy.yml"},
            {"id": 2, "name": "verify", "path": ".github/workflows/verify.yml"},
        ]
        import json as _json

        def fake_runner(argv):
            self.assertIn("head_sha=" + DF55, argv[2])
            return _json.dumps(payload)

        runs = watch.fetch_deploy_runs("pixexid/amiga", DF55, "deploy", runner=fake_runner)
        self.assertEqual([r["id"] for r in runs], [1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
