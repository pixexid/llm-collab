from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import check_github_task_mirrors
import report_github_project_task_sync


class GitHubProjectAdapterScopeTest(unittest.TestCase):
    def test_mirror_checker_accepts_exact_project(self) -> None:
        # #given
        frontmatter = {"project_id": "amiga"}

        # #when
        in_scope = check_github_task_mirrors.task_in_scope(frontmatter, "amiga")

        # #then
        self.assertTrue(in_scope)

    def test_mirror_checker_rejects_projectless_task(self) -> None:
        # #given
        frontmatter = {}

        # #when
        in_scope = check_github_task_mirrors.task_in_scope(frontmatter, "amiga")

        # #then
        self.assertFalse(in_scope)

    def test_mirror_checker_rejects_foreign_project(self) -> None:
        # #given
        frontmatter = {"project_id": "nuvyr"}

        # #when
        in_scope = check_github_task_mirrors.task_in_scope(frontmatter, "amiga")

        # #then
        self.assertFalse(in_scope)

    def test_sync_report_rejects_projectless_task(self) -> None:
        # #given
        frontmatter = {}

        # #when
        in_scope = report_github_project_task_sync.task_in_scope(frontmatter, "amiga")

        # #then
        self.assertFalse(in_scope)

    def test_sync_report_rejects_foreign_project(self) -> None:
        # #given
        frontmatter = {"project_id": "nuvyr"}

        # #when
        in_scope = report_github_project_task_sync.task_in_scope(frontmatter, "amiga")

        # #then
        self.assertFalse(in_scope)

    def test_sync_report_defaults_to_project_state_directory(self) -> None:
        # #given
        state_directory = Path("/tmp/llm-collab-projects/amiga")

        # #when
        with patch.object(
            report_github_project_task_sync,
            "project_state_dir",
            return_value=state_directory,
        ):
            output_path = report_github_project_task_sync.default_output_path("amiga")

        # #then
        self.assertEqual(output_path, state_directory / "github-project-task-sync.md")


if __name__ == "__main__":
    unittest.main()
