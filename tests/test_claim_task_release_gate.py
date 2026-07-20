from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import claim_task
from _helpers import dump_frontmatter, parse_frontmatter, parse_release_evidence
from deploy_release_watch import ReleaseEvaluation, Verdict


SHA = "a" * 40
TASK_ID = "TASK-GATE01"


def project_fixture(project_id: str = "amiga", *, release_closure: object = True) -> dict:
    project = {
        "id": project_id,
        "release_gate_agent": "codex",
        "default_branch_base": "main",
        "github": {"enabled": True, "repo": f"pixexid/{project_id}"},
    }
    if release_closure is True:
        project["release_closure"] = {
            "workflow": "deploy",
            "trigger_event": "push",
            "required_jobs": ["detect", "deploy"],
            "smoke_job": "deploy",
            "required_smoke_steps": ["Verify production"],
        }
    elif release_closure is not False:
        project["release_closure"] = release_closure
    return project


def success_evaluation(
    project_id: str = "amiga",
    *,
    run_id: int = 41,
    merge_sha: str = SHA,
    workflow: str | None = "deploy",
) -> ReleaseEvaluation:
    return ReleaseEvaluation(
        project_id=project_id,
        repository=f"pixexid/{project_id}",
        workflow=workflow,
        verdict=Verdict(
            state="SUCCESS",
            merge_sha=merge_sha,
            run_id=run_id,
            run_conclusion="success",
            detail="fixture terminal success",
        ),
    )


class ReleaseEvidenceParserTest(unittest.TestCase):
    def test_accepts_only_documented_normalized_shape(self) -> None:
        evidence = parse_release_evidence(json.dumps({
            "merge_sha": SHA.upper(),
            "verdict": "success",
            "run_id": 41,
            "note": "  release verified  ",
        }))
        self.assertEqual(evidence, {
            "merge_sha": SHA,
            "verdict": "success",
            "run_id": 41,
            "note": "release verified",
        })

    def test_rejects_non_objects_unknown_fields_and_malformed_types(self) -> None:
        invalid = (
            None,
            "free text",
            "[]",
            json.dumps({"merge_sha": SHA, "verdict": "success", "run_id": 1, "extra": 1}),
            json.dumps({"merge_sha": "abc", "verdict": "success", "run_id": 1}),
            json.dumps({"merge_sha": SHA, "verdict": "green", "run_id": 1}),
            json.dumps({"merge_sha": SHA, "verdict": "success"}),
            json.dumps({"merge_sha": SHA, "verdict": "success", "run_id": True}),
            json.dumps({"merge_sha": SHA, "verdict": "success", "run_id": 1.0}),
            json.dumps({"merge_sha": SHA, "verdict": "success", "run_id": 0}),
            json.dumps({"merge_sha": SHA, "verdict": "non-production", "note": ""}),
            json.dumps({"merge_sha": SHA, "verdict": "non-production", "note": 7}),
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_release_evidence(raw)


class ReleaseEvidenceRecordTest(unittest.TestCase):
    def test_matching_success_persists_authoritative_bound_record(self) -> None:
        project = project_fixture()
        evaluator = Mock(return_value=success_evaluation())
        with patch.object(claim_task, "get_project", return_value=project):
            record = claim_task.build_release_evidence_record(
                {"task_id": TASK_ID, "project_id": "amiga"},
                "review",
                "codex",
                json.dumps({
                    "merge_sha": SHA,
                    "verdict": "success",
                    "run_id": 41,
                    "note": "exact head",
                }),
                evaluator=evaluator,
                evaluated_at="2026-07-20T12:00:00+00:00",
            )

        evaluator.assert_called_once_with("amiga", SHA, project=project)
        self.assertEqual(record, {
            "project_id": "amiga",
            "task_id": TASK_ID,
            "repository": "pixexid/amiga",
            "merge_sha": SHA,
            "production_impact": "production-release-verified",
            "terminal_verdict": "success",
            "released_by": "codex",
            "evaluated_at": "2026-07-20T12:00:00+00:00",
            "workflow": "deploy",
            "run_id": 41,
            "note": "exact head",
        })

    def test_wrong_release_gate_agent_and_missing_registry_key_fail_closed(self) -> None:
        frontmatter = {"task_id": TASK_ID, "project_id": "amiga"}
        evidence = json.dumps({
            "merge_sha": SHA,
            "verdict": "non-production",
        })
        with patch.object(claim_task, "get_project", return_value=project_fixture()):
            with self.assertRaisesRegex(claim_task.ReleaseGateError, "release_gate_agent 'codex'"):
                claim_task.build_release_evidence_record(
                    frontmatter, "review", "claude", evidence
                )

        project = project_fixture()
        del project["release_gate_agent"]
        with patch.object(claim_task, "get_project", return_value=project):
            with self.assertRaisesRegex(
                claim_task.ReleaseGateError,
                "release_gate_agent.*task project's projects.json entry",
            ):
                claim_task.build_release_evidence_record(
                    frontmatter, "review", "codex", evidence
                )

    def test_nuvyr_non_success_disposition_is_honest_and_does_not_evaluate(self) -> None:
        evaluator = Mock()
        with patch.object(
            claim_task,
            "get_project",
            return_value=project_fixture("nuvyr", release_closure=False),
        ):
            record = claim_task.build_release_evidence_record(
                {"task_id": TASK_ID, "project_id": "nuvyr"},
                "review",
                "codex",
                json.dumps({
                    "merge_sha": SHA,
                    "verdict": "non-production",
                    "note": "documentation-only",
                }),
                evaluator=evaluator,
                evaluated_at="2026-07-20T12:00:00+00:00",
            )

        evaluator.assert_not_called()
        self.assertEqual(record["production_impact"], "non-production")
        self.assertEqual(record["terminal_verdict"], "non-production")
        self.assertNotIn("workflow", record)
        self.assertNotIn("run_id", record)

    def test_github_disabled_allows_both_honest_non_success_dispositions(self) -> None:
        evaluator = Mock()
        for project_id in ("amiga", "nuvyr"):
            project = project_fixture(project_id)
            project["github"] = {"enabled": False}
            with patch.object(claim_task, "get_project", return_value=project):
                for verdict in ("non-production", "risk-accepted-followup"):
                    with self.subTest(project_id=project_id, verdict=verdict):
                        record = claim_task.build_release_evidence_record(
                            {"task_id": TASK_ID, "project_id": project_id},
                            "review",
                            "codex",
                            json.dumps({
                                "merge_sha": SHA,
                                "verdict": verdict,
                                "run_id": 77,
                            }),
                            evaluator=evaluator,
                            evaluated_at="2026-07-20T12:00:00+00:00",
                        )
                        self.assertIsNone(record["repository"])
                        self.assertEqual(record["terminal_verdict"], verdict)
                        self.assertNotIn("run_id", record)
        evaluator.assert_not_called()

    def test_absent_and_empty_closure_allow_non_success_but_refuse_success(self) -> None:
        for project_id in ("amiga", "nuvyr"):
            for closure_name, release_closure in (
                ("absent", False),
                ("empty", {}),
            ):
                with self.subTest(
                    project_id=project_id,
                    closure=closure_name,
                ):
                    project = project_fixture(
                        project_id,
                        release_closure=release_closure,
                    )
                    evaluator = Mock()
                    with patch.object(claim_task, "get_project", return_value=project):
                        for verdict in (
                            "non-production",
                            "risk-accepted-followup",
                        ):
                            record = claim_task.build_release_evidence_record(
                                {"task_id": TASK_ID, "project_id": project_id},
                                "review",
                                "codex",
                                json.dumps({
                                    "merge_sha": SHA,
                                    "verdict": verdict,
                                }),
                                evaluator=evaluator,
                                evaluated_at="2026-07-20T12:00:00+00:00",
                            )
                            self.assertEqual(
                                record["repository"],
                                f"pixexid/{project_id}",
                            )
                            self.assertEqual(record["terminal_verdict"], verdict)

                        with self.assertRaisesRegex(
                            claim_task.ReleaseGateError,
                            "no release_closure config",
                        ):
                            claim_task.build_release_evidence_record(
                                {"task_id": TASK_ID, "project_id": project_id},
                                "review",
                                "codex",
                                json.dumps({
                                    "merge_sha": SHA,
                                    "verdict": "success",
                                    "run_id": 41,
                                }),
                                evaluator=evaluator,
                            )
                    evaluator.assert_not_called()

    def test_pending_failure_cancelled_missing_and_wrong_sha_all_refuse_success(self) -> None:
        project = project_fixture()
        frontmatter = {"task_id": TASK_ID, "project_id": "amiga"}
        evidence = json.dumps({
            "merge_sha": SHA,
            "verdict": "success",
            "run_id": 41,
        })
        refused = (
            ("PENDING", SHA),
            ("FAILURE", SHA),
            ("CANCELLED", SHA),
            ("MISSING", SHA),
            ("SUCCESS", "b" * 40),
        )
        with patch.object(claim_task, "get_project", return_value=project):
            for state, evaluated_sha in refused:
                with self.subTest(state=state, evaluated_sha=evaluated_sha):
                    evaluation = success_evaluation(merge_sha=evaluated_sha)
                    evaluation.verdict.state = state
                    with self.assertRaises(claim_task.ReleaseGateError):
                        claim_task.build_release_evidence_record(
                            frontmatter,
                            "review",
                            "codex",
                            evidence,
                            evaluator=lambda *_args, result=evaluation, **_kwargs: result,
                        )


class ClaimTaskMutationSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="claim-release-gate-")
        self.root = Path(self.temp_dir.name)
        (self.root / "Tasks" / "active").mkdir(parents=True)
        (self.root / "Tasks" / "done").mkdir(parents=True)
        self.task_path = self.root / "Tasks" / "active" / f"fixture__{TASK_ID}.md"
        self.done_path = self.root / "Tasks" / "done" / f"fixture__{TASK_ID}.md"
        self.write_task("review", "amiga")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_task(self, status: str, project_id: str) -> None:
        if self.done_path.exists():
            self.done_path.unlink()
        self.task_path.write_text(dump_frontmatter(
            {
                "task_id": TASK_ID,
                "title": "Release gate fixture",
                "status": status,
                "owner": "worker",
                "project_id": project_id,
            },
            "# Release gate fixture\n",
        ))

    def invoke_done(
        self,
        *,
        project: dict,
        evidence: dict,
        evaluator,
    ) -> tuple[int, str, str]:
        argv = [
            "claim_task.py",
            "--task", TASK_ID,
            "--owner", "worker",
            "--status", "done",
            "--released-by", "codex",
            "--release-evidence", json.dumps(evidence),
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        self.queue_mutator = Mock(return_value={"ok": True})
        self.task_writer = Mock(side_effect=claim_task.write_file)
        exit_code = 0
        with (
            patch.object(sys, "argv", argv),
            patch.object(claim_task, "ROOT", self.root),
            patch.object(claim_task, "find_task_by_id", return_value=self.task_path),
            patch.object(claim_task, "target_task_path", return_value=self.done_path),
            patch.object(claim_task, "get_project", return_value=project),
            patch.object(claim_task, "agent_ids", return_value=["codex", "worker"]),
            patch.object(claim_task, "ensure_agent_enabled", return_value={}),
            patch.object(claim_task, "sync_task_contract", side_effect=lambda fm, body: (fm, {})),
            patch.object(claim_task, "evaluate_project_release", side_effect=evaluator),
            patch.object(claim_task.issue_queue, "queue_exists", return_value=True),
            patch.object(
                claim_task.issue_queue,
                "mark_lane_transition",
                self.queue_mutator,
            ),
            patch.object(claim_task, "write_file", self.task_writer),
            patch.object(claim_task, "utc_iso", return_value="2026-07-20T12:00:00+00:00"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                claim_task.main()
            except SystemExit as error:
                exit_code = int(error.code)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def assert_refusal_did_not_mutate(self, before: str) -> None:
        self.assertTrue(self.task_path.exists())
        self.assertEqual(self.task_path.read_text(), before)
        self.assertFalse(self.done_path.exists())

    def test_arbitrary_caller_run_id_cannot_pass_or_mutate(self) -> None:
        before = self.task_path.read_text()
        code, _stdout, stderr = self.invoke_done(
            project=project_fixture(),
            evidence={"merge_sha": SHA, "verdict": "success", "run_id": 999},
            evaluator=lambda *_args, **_kwargs: success_evaluation(run_id=41),
        )
        self.assertEqual(code, 1)
        self.assertIn("does not match authoritative run_id 41", stderr)
        self.assert_refusal_did_not_mutate(before)

    def test_missing_nuvyr_release_closure_cannot_pass_success_or_mutate(self) -> None:
        self.write_task("review", "nuvyr")
        before = self.task_path.read_text()
        code, _stdout, stderr = self.invoke_done(
            project=project_fixture("nuvyr", release_closure=False),
            evidence={"merge_sha": SHA, "verdict": "success", "run_id": 41},
            evaluator=lambda *_args, **_kwargs: success_evaluation(
                "nuvyr", workflow=None
            ),
        )
        self.assertEqual(code, 1)
        self.assertIn("no release_closure config", stderr)
        self.assert_refusal_did_not_mutate(before)

    def test_github_disabled_success_refuses_without_mutation_or_evaluator(self) -> None:
        before = self.task_path.read_text()
        project = project_fixture()
        project["github"] = {"enabled": False}
        evaluator = Mock(return_value=success_evaluation())
        code, _stdout, stderr = self.invoke_done(
            project=project,
            evidence={"merge_sha": SHA, "verdict": "success", "run_id": 41},
            evaluator=evaluator,
        )
        self.assertEqual(code, 1)
        self.assertIn("no enabled github.repo", stderr)
        evaluator.assert_not_called()
        self.assert_refusal_did_not_mutate(before)

    def test_truthy_non_object_project_mappings_refuse_without_mutation_or_traceback(self) -> None:
        for key, malformed in (
            ("github", "pixexid/amiga"),
            ("release_closure", "deploy"),
        ):
            with self.subTest(key=key):
                before = self.task_path.read_text()
                project = project_fixture()
                project[key] = malformed
                evaluator = Mock(return_value=success_evaluation())
                code, _stdout, stderr = self.invoke_done(
                    project=project,
                    evidence={"merge_sha": SHA, "verdict": "success", "run_id": 41},
                    evaluator=evaluator,
                )
                self.assertEqual(code, 1)
                self.assertIn("project 'amiga'", stderr)
                self.assertIn(f"projects.json key '{key}'", stderr)
                self.assertIn(
                    "repair this task project's 'amiga' entry in projects.json",
                    stderr,
                )
                self.assertNotIn("Traceback", stderr)
                evaluator.assert_not_called()
                self.task_writer.assert_not_called()
                self.queue_mutator.assert_not_called()
                self.assert_refusal_did_not_mutate(before)

    def test_malformed_truthy_closure_refuses_every_verdict_for_paired_projects(self) -> None:
        for project_id in ("amiga", "nuvyr"):
            for malformed in (
                "deploy",
                {"workflow": "deploy"},
            ):
                for verdict in (
                    "success",
                    "non-production",
                    "risk-accepted-followup",
                ):
                    with self.subTest(
                        project_id=project_id,
                        malformed=malformed,
                        verdict=verdict,
                    ):
                        self.write_task("review", project_id)
                        before = self.task_path.read_text()
                        project = project_fixture(project_id)
                        project["release_closure"] = malformed
                        evaluator = Mock(
                            return_value=success_evaluation(project_id)
                        )
                        evidence = {
                            "merge_sha": SHA,
                            "verdict": verdict,
                        }
                        if verdict == "success":
                            evidence["run_id"] = 41

                        code, _stdout, stderr = self.invoke_done(
                            project=project,
                            evidence=evidence,
                            evaluator=evaluator,
                        )

                        self.assertEqual(code, 1)
                        self.assertIn(
                            f"project {project_id!r}",
                            stderr,
                        )
                        self.assertIn(
                            "malformed projects.json key 'release_closure'",
                            stderr,
                        )
                        self.assertIn(
                            f"repair this task project's {project_id!r} entry "
                            "in projects.json at key 'release_closure'",
                            stderr,
                        )
                        evaluator.assert_not_called()
                        self.task_writer.assert_not_called()
                        self.queue_mutator.assert_not_called()
                        self.assert_refusal_did_not_mutate(before)

    def test_pending_missing_failed_and_cancelled_preserve_review_lane(self) -> None:
        for state in ("PENDING", "MISSING", "FAILURE", "CANCELLED"):
            with self.subTest(state=state):
                self.write_task("review", "amiga")
                before = self.task_path.read_text()
                evaluation = success_evaluation()
                evaluation.verdict.state = state
                evaluator = Mock(return_value=evaluation)

                code, _stdout, stderr = self.invoke_done(
                    project=project_fixture(),
                    evidence={
                        "merge_sha": SHA,
                        "verdict": "success",
                        "run_id": 41,
                    },
                    evaluator=evaluator,
                )

                self.assertEqual(code, 1)
                self.assertIn(
                    f"objective release verdict is {state}",
                    stderr,
                )
                evaluator.assert_called_once()
                self.task_writer.assert_not_called()
                self.queue_mutator.assert_not_called()
                self.assert_refusal_did_not_mutate(before)

    def test_non_review_source_cannot_reach_done_or_call_evaluator(self) -> None:
        self.write_task("in_progress", "amiga")
        before = self.task_path.read_text()
        evaluator = Mock(return_value=success_evaluation())
        code, _stdout, stderr = self.invoke_done(
            project=project_fixture(),
            evidence={"merge_sha": SHA, "verdict": "success", "run_id": 41},
            evaluator=evaluator,
        )
        self.assertEqual(code, 1)
        self.assertIn("only review -> done is allowed", stderr)
        evaluator.assert_not_called()
        self.assert_refusal_did_not_mutate(before)

    def test_exact_authoritative_success_moves_once_and_round_trips_record(self) -> None:
        code, stdout, stderr = self.invoke_done(
            project=project_fixture(),
            evidence={"merge_sha": SHA, "verdict": "success", "run_id": 41},
            evaluator=lambda *_args, **_kwargs: success_evaluation(),
        )
        self.assertEqual(code, 0, stderr)
        self.assertFalse(self.task_path.exists())
        self.assertTrue(self.done_path.exists())
        frontmatter, _body = parse_frontmatter(self.done_path.read_text())
        self.assertEqual(frontmatter["status"], "done")
        self.assertEqual(frontmatter["release_evidence"]["run_id"], 41)
        self.assertEqual(
            frontmatter["release_evidence"]["production_impact"],
            "production-release-verified",
        )
        self.queue_mutator.assert_called_once_with(
            "amiga",
            TASK_ID,
            owner="worker",
            task_status="done",
        )
        self.assertEqual(json.loads(stdout)["new_status"], "done")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
