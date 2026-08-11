from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog
import project_issue_queue


class IssueStateQueueTest(unittest.TestCase):
    def task(self, root: Path, *, status: str = "open", depends_on=()) -> Path:
        path = root / "2026-08-11_gh-756-state__TASK-STATE1.md"
        path.write_text(
            "---\n"
            "task_id: TASK-STATE1\n"
            "title: GH-756 State task\n"
            f"status: {status}\n"
            "owner: codex\n"
            "project_id: llm-collab\n"
            f"depends_on: {list(depends_on)!r}\n"
            "skip_refinement: true\n"
            "---\n"
        )
        return path

    def reconcile(self, issue: _backlog.BacklogIssue, task: Path):
        with (
            patch.object(_backlog, "eligible_open_issues", return_value=[issue]),
            patch.object(project_issue_queue, "all_task_files", return_value=[task]),
            patch.object(project_issue_queue, "queue_exists", return_value=False),
            patch.object(project_issue_queue, "validate_direct_app_policy", return_value=([], {})),
        ):
            return project_issue_queue.reconcile_queue("llm-collab")

    def test_invalid_state_projection_has_no_ready_or_partial_lane(self) -> None:
        violations = [
            {
                "issue": 756,
                "found": ["state:banana"],
                "reason": "unknown_state",
                "expected_one_of": list(_backlog.RECOGNIZED_ISSUE_STATE_LABELS),
            }
        ]
        with patch.object(
            _backlog,
            "eligible_open_issues",
            side_effect=_backlog.IssueStatePolicyError(violations, total_examined=1),
        ):
            result = project_issue_queue.reconcile_queue("llm-collab")
        self.assertFalse(result["ok"])
        self.assertEqual(result["backlog"], "invalid")
        self.assertEqual(result["projection"]["invalid_issue_states"], violations)
        self.assertEqual(
            result["projection"]["lanes"], [], "invalid state must project no executable lane"
        )

    def test_blocked_issue_vetoes_open_in_progress_and_review_tasks(self) -> None:
        for task_status in ("open", "in_progress", "review"):
            with self.subTest(task_status=task_status), tempfile.TemporaryDirectory() as raw:
                task = self.task(Path(raw), status=task_status)
                issue = _backlog.BacklogIssue(
                    number=756,
                    title="State model",
                    labels=("state:blocked",),
                    issue_state="state:blocked",
                    policy_reason="github:state:blocked",
                )
                result = self.reconcile(issue, task)
                lane = result["projection"]["lanes"][0]
                self.assertEqual(
                    lane["queue_state"],
                    "blocked",
                    "GitHub state:blocked must veto every task lifecycle state",
                )
                self.assertIn("github:state:blocked", lane["blocked_by"])

    def test_active_issue_never_clears_task_dependency_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw), depends_on=("TASK-DEP",))
            issue = _backlog.BacklogIssue(
                number=756,
                title="State model",
                labels=("state:active",),
                issue_state="state:active",
                policy_reason="state:active",
            )
            result = self.reconcile(issue, task)
        lane = result["projection"]["lanes"][0]
        self.assertEqual(lane["queue_state"], "blocked")
        self.assertEqual(lane["blocked_by"], ["TASK-DEP"])

    def test_active_issue_with_unblocked_open_task_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw))
            issue = _backlog.BacklogIssue(
                number=756,
                title="State model",
                labels=("state:active",),
                issue_state="state:active",
                policy_reason="state:active",
            )
            result = self.reconcile(issue, task)
        lane = result["projection"]["lanes"][0]
        self.assertEqual(
            lane["queue_state"],
            "ready",
            "an unblocked open task with state:active must be ready",
        )
        self.assertEqual(lane["blocked_by"], [])

    def test_fresh_active_state_clears_only_the_github_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self.task(Path(raw), depends_on=("TASK-DEP",))
            blocked = _backlog.BacklogIssue(
                number=756,
                title="State model",
                labels=("state:blocked",),
                issue_state="state:blocked",
                policy_reason="github:state:blocked",
            )
            active = _backlog.BacklogIssue(
                number=756,
                title="State model",
                labels=("state:active",),
                issue_state="state:active",
                policy_reason="state:active",
            )
            blocked_lane = self.reconcile(blocked, task)["projection"]["lanes"][0]
            active_lane = self.reconcile(active, task)["projection"]["lanes"][0]
        self.assertEqual(blocked_lane["blocked_by"], ["github:state:blocked", "TASK-DEP"])
        self.assertEqual(
            active_lane["blocked_by"],
            ["TASK-DEP"],
            "fresh active state may clear only GitHub's blocker",
        )

    def test_fresh_validation_rejects_cached_issue_state_drift(self) -> None:
        payload = {
            "project_id": "llm-collab",
            "lanes": [{"issue": 756, "issue_state": "state:active"}],
        }
        fresh = _backlog.BacklogIssue(
            number=756,
            title="State model",
            labels=("state:blocked",),
            issue_state="state:blocked",
        )
        with patch.object(_backlog, "eligible_open_issues", return_value=[fresh]):
            errors, _ = project_issue_queue.backlog_consistency_errors(
                "llm-collab", payload
            )
        self.assertIn(
            "queue/GitHub issue_state drift for GH-756: queue 'state:active' vs GitHub 'state:blocked'",
            errors,
        )

    def test_fresh_validation_rejects_lane_excluded_after_projection(self) -> None:
        payload = {
            "project_id": "llm-collab",
            "lanes": [{"issue": 756, "issue_state": "state:active"}],
        }
        with patch.object(_backlog, "eligible_open_issues", return_value=[]):
            errors, _ = project_issue_queue.backlog_consistency_errors(
                "llm-collab", payload
            )
        self.assertIn(
            "queue/backlog drift: queued issue(s) are no longer open eligible work: GH-756",
            errors,
        )

    def test_configuration_error_returns_no_projection_for_write(self) -> None:
        with patch.object(
            _backlog,
            "eligible_open_issues",
            side_effect=ValueError("github.backlog.exclude_labels malformed"),
        ):
            result = project_issue_queue.reconcile_queue("llm-collab")
        self.assertEqual(result["backlog"], "configuration_error")
        self.assertIsNone(result["projection"])


if __name__ == "__main__":
    unittest.main()
