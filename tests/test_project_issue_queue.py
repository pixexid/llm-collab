from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog
import project_issue_queue


class ProjectIssueQueueBacklogGateTest(unittest.TestCase):
    def test_backlog_consistency_fails_when_empty_queue_has_eligible_issue(self) -> None:
        payload = {"project_id": "amiga", "lanes": []}
        issue = _backlog.BacklogIssue(number=679, title="Fix operations task viewport", labels=())

        with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
            errors, warnings = project_issue_queue.backlog_consistency_errors("amiga", payload)

        self.assertEqual(
            errors,
            ["queue/backlog drift: eligible open GitHub issue(s) missing from issue-queue.json: GH-679"],
        )
        self.assertEqual(warnings, [])

    def test_backlog_consistency_confirms_empty_queue_when_github_backlog_empty(self) -> None:
        payload = {"project_id": "amiga", "lanes": []}

        with patch.object(_backlog, "eligible_open_issues", return_value=[]):
            errors, warnings = project_issue_queue.backlog_consistency_errors("amiga", payload)

        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["queue empty confirmed against GitHub backlog"])

    def test_backlog_consistency_fails_closed_when_github_is_unavailable(self) -> None:
        payload = {"project_id": "amiga", "lanes": []}

        with patch.object(
            _backlog,
            "eligible_open_issues",
            side_effect=_backlog.BacklogUnavailable("gh auth required"),
        ):
            errors, warnings = project_issue_queue.backlog_consistency_errors("amiga", payload)

        self.assertEqual(errors, ["GitHub backlog state unknown for amiga: gh auth required"])
        self.assertEqual(warnings, [])

    def test_backlog_consistency_allows_queued_eligible_issue(self) -> None:
        payload = {
            "project_id": "amiga",
            "lanes": [{"order": 1, "issue": 679, "task_id": "TASK-123456"}],
        }
        issue = _backlog.BacklogIssue(number=679, title="Fix operations task viewport", labels=())

        with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
            errors, warnings = project_issue_queue.backlog_consistency_errors("amiga", payload)

        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
