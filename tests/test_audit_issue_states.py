from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog
import audit_issue_states


class AuditIssueStatesTest(unittest.TestCase):
    CONFIG = {
        "enabled": True,
        "repo": "pixexid/llm-collab",
        "exclude_labels": ["epic", "state:parked"],
    }

    @staticmethod
    def issue(number: int, labels: list[str]) -> dict:
        return {
            "number": number,
            "title": f"Issue {number}",
            "labels": [{"name": label} for label in labels],
        }

    def invoke(self, issues=None, *, json_output=False, error=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = ["audit_issue_states.py", "--project", "llm-collab"]
        if json_output:
            argv.append("--json")
        loader = (
            patch.object(_backlog, "load_open_github_issues", side_effect=error)
            if error is not None
            else patch.object(_backlog, "load_open_github_issues", return_value=issues)
        )
        with (
            patch.object(sys, "argv", argv),
            patch.object(_backlog, "project_backlog_config", return_value=self.CONFIG),
            loader,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = audit_issue_states.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_more_than_thirty_open_issues_are_all_examined(self) -> None:
        issues = [self.issue(number, ["state:active"]) for number in range(1, 36)]
        result = audit_issue_states.audit_issue_states
        with (
            patch.object(_backlog, "project_backlog_config", return_value=self.CONFIG),
            patch.object(_backlog, "load_open_github_issues", return_value=issues),
        ):
            audited = result("llm-collab")
        self.assertEqual(audited["open_issues_examined"], 35)
        self.assertTrue(audited["complete"])

    def test_unknown_single_state_label_is_a_violation(self) -> None:
        code, stdout, _ = self.invoke([self.issue(756, ["state:banana"])])
        self.assertEqual(code, 1)
        self.assertIn("repository: pixexid/llm-collab", stdout)
        self.assertIn("open_issues_examined: 1", stdout)
        self.assertIn("complete: true", stdout)
        self.assertIn("state_label_violations: 1", stdout)
        self.assertIn("GH-756", stdout)
        self.assertIn("state:banana", stdout)

    def test_clean_violation_and_unknown_have_distinct_exit_results(self) -> None:
        clean, _, _ = self.invoke([self.issue(1, ["state:active"])])
        violation, _, _ = self.invoke([self.issue(1, [])])
        unknown, _, _ = self.invoke(
            error=_backlog.BacklogUnavailable("GitHub timeout")
        )
        self.assertEqual((clean, violation, unknown), (0, 1, 2))

    def test_json_output_reports_repository_total_completeness_and_violations(self) -> None:
        code, stdout, _ = self.invoke(
            [
                self.issue(1, ["state:active"]),
                self.issue(2, ["state:active", "state:unknown"]),
                self.issue(3, ["state:parked"]),
            ],
            json_output=True,
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 1)
        self.assertEqual(payload["repository"], "pixexid/llm-collab")
        self.assertEqual(payload["open_issues_examined"], 3)
        self.assertTrue(payload["complete"])
        self.assertEqual(payload["state_label_violations"][0]["issue"], 2)
        self.assertEqual(payload["parked_issues"], [3])

    def test_unknown_json_output_never_claims_clean_partial_result(self) -> None:
        code, stdout, _ = self.invoke(
            json_output=True,
            error=_backlog.BacklogUnavailable("page two failed"),
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 2)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["repository"], "pixexid/llm-collab")
        self.assertEqual(payload["open_issues_examined"], 0)
        self.assertEqual(payload["state_label_violations"], [])
        self.assertIn("page two failed", payload["error"])


if __name__ == "__main__":
    unittest.main()
