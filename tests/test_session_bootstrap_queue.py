from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog
import session_bootstrap


class SessionBootstrapQueueTest(unittest.TestCase):
    def test_queue_summary_reports_drift_when_queue_misses_open_github_issue(self) -> None:
        issue = _backlog.BacklogIssue(number=678, title="Date formatter UTC offset", labels=())

        with patch.object(session_bootstrap, "load_projects", return_value=[{"id": "amiga"}]):
            with patch.object(session_bootstrap.issue_queue, "queue_exists", return_value=True):
                with patch.object(
                    session_bootstrap.issue_queue,
                    "load_queue",
                    return_value={"project_id": "amiga", "lanes": []},
                ):
                    with patch.object(session_bootstrap.issue_queue, "queue_markdown_path", return_value=Path("/tmp/issue-queue.md")):
                        with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                            summaries = session_bootstrap.queue_summaries()

        self.assertEqual(summaries[0]["backlog_status"], "drift")
        self.assertEqual(summaries[0]["missing_issues"], [678])
        self.assertTrue(summaries[0]["queue_empty"])

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
                            _backlog,
                            "eligible_open_issues",
                            side_effect=_backlog.BacklogUnavailable("gh down"),
                        ):
                            summaries = session_bootstrap.queue_summaries()

        self.assertEqual(summaries[0]["backlog_status"], "unknown")
        self.assertEqual(summaries[0]["backlog_error"], "gh down")


if __name__ == "__main__":
    unittest.main()
