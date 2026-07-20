"""Focused release-lifecycle regressions outside claim_task's direct gate."""

from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_github_task_mirrors
import init as init_script


def write_mirror(root: Path, status: str, *, frontmatter: bool = True) -> Path:
    path = root / "active" / "gh-100-lane__TASK-TEST01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not frontmatter:
        path.write_text("no frontmatter here\n")
        return path
    path.write_text(
        "---\n"
        "task_id: TASK-TEST01\n"
        "project_id: demo\n"
        f"status: {status}\n"
        "owner: worker\n"
        "---\n"
        "\n# GH-100 test mirror\n\n## Activity Log\n"
    )
    return path


class ArchiveClosedTaskFailsClosedTest(unittest.TestCase):
    def test_refuses_every_non_done_status_without_write_or_move(self) -> None:
        for status in ("open", "in_progress", "blocked", "review"):
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                task_root = Path(tmp) / "Tasks"
                path = write_mirror(task_root, status)
                before = path.read_text()

                result = check_github_task_mirrors.archive_closed_task(path, 100)

                self.assertIsNone(result)
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text(), before)
                self.assertFalse((task_root / "done").exists())

    def test_missing_frontmatter_is_refused_without_write_or_move(self) -> None:
        with TemporaryDirectory() as tmp:
            task_root = Path(tmp) / "Tasks"
            path = write_mirror(task_root, "review", frontmatter=False)
            before = path.read_text()

            result = check_github_task_mirrors.archive_closed_task(path, 100)

            self.assertIsNone(result)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(), before)
            self.assertFalse((task_root / "done").exists())

    def test_archives_only_an_already_done_mirror(self) -> None:
        with TemporaryDirectory() as tmp:
            task_root = Path(tmp) / "Tasks"
            path = write_mirror(task_root, "done")

            result = check_github_task_mirrors.archive_closed_task(path, 100)

            self.assertIsNotNone(result)
            self.assertFalse(path.exists())
            self.assertTrue(result.exists())
            self.assertIn("status: done", result.read_text())
            self.assertIn("archived already-done mirror", result.read_text())

    def test_caller_reports_non_done_closed_issue_as_actionable_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_mirror(root / "Tasks", "review")
            before = path.read_text()
            output = StringIO()
            issue = {
                "title": "Closed",
                "url": "https://github.com/owner/demo/issues/100",
                "state": "CLOSED",
                "labels": "",
            }
            with patch.object(check_github_task_mirrors, "ROOT", root):
                with patch.object(check_github_task_mirrors, "ensure_project"):
                    with patch.object(
                        check_github_task_mirrors,
                        "gh_issue_map",
                        return_value={100: issue},
                    ):
                        with patch.object(
                            check_github_task_mirrors,
                            "iter_project_task_mirrors",
                            return_value=[path],
                        ):
                            argv = [
                                "check_github_task_mirrors.py",
                                "--project", "demo",
                                "--repo", "owner/demo",
                                "--archive-closed-active",
                            ]
                            with patch.object(sys, "argv", argv):
                                with redirect_stdout(output):
                                    exit_code = check_github_task_mirrors.main()

            self.assertEqual(exit_code, 1)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(), before)
            self.assertIn("REFUSED to promote", output.getvalue())
            self.assertIn("remains in place as release-lifecycle drift", output.getvalue())

    def test_archive_flag_help_is_honest(self) -> None:
        source = (REPO_ROOT / "bin" / "check_github_task_mirrors.py").read_text()
        self.assertNotIn("and mark status done", source)
        self.assertIn("Never promotes", source)


class InitReleaseGateAgentTest(unittest.TestCase):
    def test_generated_identity_routes_completed_implementation_to_review(self) -> None:
        identity = init_script.build_identity_md(
            {
                "id": "worker",
                "display_name": "Worker",
                "role": "implementer",
                "activation": {},
            },
            "Fixture",
            ["worker", "reviewer"],
            [{"id": "demo"}],
        )

        self.assertIn("claim_task.py --status review", identity)
        self.assertNotIn("claim_task.py --status done", identity)

    def test_enabled_agent_ids_excludes_disabled_entries(self) -> None:
        agents = [
            {"id": "codex", "role": "orchestrator", "activation": {"enabled": True}},
            {"id": "claude", "disabled": True, "activation": {}},
            {"id": "legacy", "role": "legacy_disabled_worker", "activation": {}},
            {"id": "off", "activation": {"enabled": False}},
        ]
        self.assertEqual(init_script.enabled_agent_ids(agents), ["codex"])

    def test_selection_reprompts_empty_unknown_and_disabled_ids(self) -> None:
        output = StringIO()
        with patch.object(
            init_script,
            "prompt",
            side_effect=["", "ghost", "codex"],
        ) as prompt:
            with redirect_stdout(output):
                selected = init_script.select_release_gate_agent(
                    "demo",
                    ["codex", "claude"],
                )

        self.assertEqual(selected, "codex")
        self.assertEqual(prompt.call_count, 3)
        self.assertIn("release_gate_agent is required", output.getvalue())
        self.assertIn("Unknown or disabled release_gate_agent 'ghost'", output.getvalue())

    def test_project_generator_always_emits_explicit_selected_release_gate_agent(self) -> None:
        with patch.object(
            init_script,
            "yn",
            side_effect=[True, False, False, False],
        ):
            with patch.object(
                init_script,
                "prompt",
                side_effect=["demo", "Demo", "main", "codex"],
            ):
                with patch.object(
                    init_script,
                    "prompt_list",
                    return_value=["app:demo"],
                ):
                    projects = init_script.collect_projects(["codex", "claude"])

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["id"], "demo")
        self.assertEqual(projects[0]["release_gate_agent"], "codex")
        self.assertIn(projects[0]["release_gate_agent"], {"codex", "claude"})


class ReleaseLifecycleGuidanceTest(unittest.TestCase):
    def test_production_schema_guard_is_documented_across_contract_workflows(self) -> None:
        paths = (
            REPO_ROOT / "docs" / "schema-reference.md",
            REPO_ROOT / "docs" / "multi-project.md",
            REPO_ROOT / "docs" / "workflows" / "task-intake-and-delegation.md",
            REPO_ROOT / "docs" / "workflows" / "review-and-handoff.md",
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md",
        )
        for path in paths:
            with self.subTest(path=path.name):
                guidance = path.read_text()
                self.assertIn("db.production_schema_guard", guidance)
                self.assertIn("local-schema-only", guidance)
                self.assertIn("shared-supabase-required", guidance)

        schema = paths[0].read_text()
        self.assertIn("db_local_schema_only_exception: dev-only-non-production", schema)
        self.assertIn("db_local_schema_only_exception_approved_by: operator", schema)
        self.assertIn("db/migrations/**", schema)
        self.assertIn("db/schema.sql", schema)

    def test_done_contract_validation_precedes_release_evaluator_and_cleanup_has_no_authority(self) -> None:
        claim_source = (REPO_ROOT / "bin" / "claim_task.py").read_text()
        build = claim_source.split("def build_release_evidence_record", 1)[1].split(
            "def parse_args", 1
        )[0]
        contract = build.index("validate_task_contract(")
        evidence_parse = build.index("parse_release_evidence(")
        evaluator = build.index("release_evaluator(")
        self.assertLess(contract, evidence_parse)
        self.assertLess(contract, evaluator)

        cleanup_source = (REPO_ROOT / "bin" / "post_merge_cleanup.py").read_text()
        self.assertNotIn("claim_task", cleanup_source)
        self.assertNotIn("validate_task_contract", cleanup_source)

    def test_commit_push_guidance_orders_evaluation_done_queue_and_cleanup(self) -> None:
        guidance = (
            REPO_ROOT / "docs" / "workflows" / "commit-push-prs.md"
        ).read_text()
        post_merge = guidance.split("## Post-merge", 1)[1].split(
            "## Release closure does not end at merge", 1
        )[0]
        exact_sha = post_merge.index(
            "3. evaluate the exact merge SHA through the project's configured release"
        )
        done = post_merge.index(
            "4. only after terminal success or an explicit honest non-success disposition"
        )
        queue = post_merge.index(
            "5. after the `review → done` transition succeeds, perform any required project"
        )
        cleanup = post_merge.index(
            "6. only then run the branch/worktree cleanup gate in applying mode"
        )

        self.assertLess(exact_sha, done)
        self.assertLess(done, queue)
        self.assertLess(queue, cleanup)
        self.assertIn(
            "`PENDING`, `MISSING`, `FAILURE`, or `CANCELLED` stops this sequence: preserve\n"
            "the task in `review` and preserve the implementation lane. Do not apply cleanup\n"
            "or advance the queue runner beyond `post_merge`.",
            post_merge,
        )
        self.assertIn(
            "the related local task mirror has an exact `project_id` match for the\n"
            "   cleanup command's `--project`; a missing, empty, null, or foreign project ID\n"
            "   is not a task match",
            guidance,
        )

    def test_guidance_orders_evaluation_done_and_cleanup(self) -> None:
        guidance = (
            REPO_ROOT / "docs" / "workflows" / "task-intake-and-delegation.md"
        ).read_text()
        exact_sha = guidance.index(
            "1. Evaluate the exact merge SHA through the configured release authority."
        )
        done = guidance.index(
            "2. After terminal success or an explicit honest non-success disposition"
        )
        cleanup = guidance.index(
            "3. Only after the `done` transition succeeds, run post-merge cleanup."
        )

        self.assertLess(exact_sha, done)
        self.assertLess(done, cleanup)
        self.assertIn(
            "It preserves the task in `review` and preserves the\n"
            "implementation lane; do not promote the task or clean the lane.",
            guidance,
        )

    def test_guidance_distinguishes_enabled_repository_from_disabled_null(self) -> None:
        guidance = (
            REPO_ROOT / "docs" / "workflows" / "task-intake-and-delegation.md"
        ).read_text()

        self.assertIn(
            "For a GitHub-enabled project with a configured repository, an honest\n"
            "non-success record preserves that repository identity.",
            guidance,
        )
        self.assertIn(
            "Only the GitHub-disabled\ncase binds `repository: null`.",
            guidance,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
