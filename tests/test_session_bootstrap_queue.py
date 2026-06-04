from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import session_bootstrap


class SessionBootstrapQueueTest(unittest.TestCase):
    def test_queue_summary_reports_drift_when_reconcile_needs_materialization(self) -> None:
        with patch.object(session_bootstrap, "load_projects", return_value=[{"id": "amiga"}]):
            with patch.object(session_bootstrap.issue_queue, "queue_exists", return_value=True):
                with patch.object(session_bootstrap.issue_queue, "queue_markdown_path", return_value=Path("/tmp/issue-queue.md")):
                    with patch.object(session_bootstrap.issue_queue, "sync_markdown", return_value=Path("/tmp/issue-queue.md")):
                        with patch.object(
                            session_bootstrap.issue_queue,
                            "reconcile_queue",
                            return_value={
                                "backlog": "known",
                                "needs_materialization": [{"issue": 678, "title": "Date formatter UTC offset"}],
                                "duplicate_mirrors": [],
                                "projection": {"project_id": "amiga", "lanes": []},
                            },
                        ):
                            summaries = session_bootstrap.queue_summaries()

        self.assertEqual(summaries[0]["backlog_status"], "drift")
        self.assertEqual(summaries[0]["missing_issues"], [678])
        self.assertTrue(summaries[0]["queue_empty"])

    def test_queue_summary_skips_write_when_reconcile_input_hash_is_unchanged(self) -> None:
        projection = {
            "project_id": "amiga",
            "input_hash": "same-hash",
            "lanes": [
                {
                    "order": 1,
                    "issue": 784,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "queue_state": "ready",
                    "needs_refinement": True,
                }
            ],
        }

        with patch.object(session_bootstrap, "load_projects", return_value=[{"id": "amiga"}]):
            with patch.object(session_bootstrap.issue_queue, "queue_exists", return_value=True):
                with patch.object(session_bootstrap.issue_queue, "queue_markdown_path", return_value=Path("/tmp/issue-queue.md")):
                    with patch.object(
                        session_bootstrap.issue_queue,
                        "reconcile_queue",
                        return_value={
                            "backlog": "known",
                            "needs_materialization": [],
                            "duplicate_mirrors": [],
                            "projection": projection,
                        },
                    ):
                        with patch.object(session_bootstrap.issue_queue, "projection_input_changed", return_value=False):
                            with patch.object(session_bootstrap.issue_queue, "sync_markdown") as sync_markdown:
                                summaries = session_bootstrap.queue_summaries()

        sync_markdown.assert_not_called()
        self.assertEqual(summaries[0]["backlog_status"], "clean")
        self.assertEqual(summaries[0]["ready_lane"]["task_id"], "TASK-E8C28D")

    def test_queue_summary_reports_unknown_when_github_is_unavailable(self) -> None:
        with patch.object(session_bootstrap, "load_projects", return_value=[{"id": "amiga"}]):
            with patch.object(session_bootstrap.issue_queue, "queue_exists", return_value=True):
                with patch.object(
                    session_bootstrap.issue_queue,
                    "load_queue",
                    return_value={"project_id": "amiga", "lanes": []},
                ):
                    with patch.object(session_bootstrap.issue_queue, "queue_markdown_path", return_value=Path("/tmp/issue-queue.md")):
                        with patch.object(
                            session_bootstrap.issue_queue,
                            "reconcile_queue",
                            return_value={
                                "backlog": "unknown",
                                "reason": "gh down",
                                "projection": None,
                            },
                        ):
                            summaries = session_bootstrap.queue_summaries()

        self.assertEqual(summaries[0]["backlog_status"], "unknown")
        self.assertEqual(summaries[0]["backlog_error"], "gh down")


if __name__ == "__main__":
    unittest.main()
