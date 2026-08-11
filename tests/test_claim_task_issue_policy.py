from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog
import claim_task


class ClaimTaskIssuePolicyTest(unittest.TestCase):
    def claim(self, policy=None, *, unavailable=None, queue_exists: bool = False):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "2026-08-11_gh-756-state__TASK-STATE1.md"
            task.write_text(
                "---\n"
                "task_id: TASK-STATE1\n"
                "title: GH-756 State model\n"
                "status: open\n"
                "owner: codex\n"
                "created_by: codex\n"
                "project_id: llm-collab\n"
                "depends_on: []\n"
                "skip_refinement: false\n"
                "---\n"
            )
            fm = {
                "task_id": "TASK-STATE1",
                "title": "GH-756 State model",
                "status": "open",
                "owner": "codex",
                "created_by": "codex",
                "project_id": "llm-collab",
                "depends_on": [],
                "skip_refinement": False,
            }
            err = io.StringIO()
            writer = patch.object(claim_task, "write_file")
            loader = patch.object(
                claim_task.issue_queue,
                "load_queue",
                return_value={
                    "lanes": [
                        {
                            "task_id": "TASK-STATE1",
                            "queue_state": "ready",
                            "issue_state": "state:active",
                        }
                    ]
                },
            )
            exact = (
                patch.object(
                    claim_task._backlog,
                    "exact_issue_policy",
                    side_effect=unavailable,
                )
                if unavailable is not None
                else patch.object(
                    claim_task._backlog,
                    "exact_issue_policy",
                    return_value=("pixexid/llm-collab", policy),
                )
            )
            with (
                patch.object(sys, "argv", [
                    "claim_task.py",
                    "--task", "TASK-STATE1",
                    "--owner", "codex",
                    "--status", "in_progress",
                    "--skip-preflight",
                    "--allow-queue-override",
                ]),
                patch.object(claim_task, "agent_ids", return_value=["codex"]),
                patch.object(claim_task, "ensure_agent_enabled"),
                patch.object(claim_task, "find_task_by_id", return_value=task),
                patch.object(claim_task, "sync_task_contract", return_value=(fm, "")),
                patch.object(claim_task, "validate_direct_app_policy", return_value=([], {})),
                patch.object(
                    claim_task.issue_queue,
                    "queue_exists",
                    return_value=queue_exists,
                ),
                exact,
                writer as write,
                loader as load,
                redirect_stderr(err),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    claim_task.main()
            return raised.exception.code, err.getvalue(), write, load

    def test_excluded_malformed_and_blocked_issues_refuse_even_with_queue_override(self) -> None:
        cases = {
            "epic": _backlog.classify_issue_labels(
                ("epic", "state:active"), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
            ),
            "state:parked": _backlog.classify_issue_labels(
                ("state:parked",), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
            ),
            "malformed": _backlog.classify_issue_labels(
                ("state:banana",), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
            ),
            "state:blocked": _backlog.classify_issue_labels(
                ("state:blocked",), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
            ),
        }
        for label, policy in cases.items():
            with self.subTest(label=label):
                code, err, write, _ = self.claim(policy)
                self.assertEqual(code, 1)
                self.assertIn(
                    '"reason": "issue_policy_refusal"',
                    err,
                    "activation policy gate must refuse excluded, malformed, and blocked issues",
                )
                self.assertIn(f'"classification": "{policy.classification}"', err)
                write.assert_not_called()

    def test_live_policy_catches_relabel_after_ready_queue_generation(self) -> None:
        parked = _backlog.classify_issue_labels(
            ("state:parked",), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
        )
        code, err, write, load = self.claim(parked, queue_exists=True)
        self.assertEqual(code, 1)
        self.assertIn('"reason": "issue_policy_refusal"', err)
        self.assertIn('"policy_reason": "excluded:state:parked"', err)
        load.assert_not_called()
        write.assert_not_called()

    def test_github_unreachable_is_distinct_and_precedes_task_mutation(self) -> None:
        code, err, write, _ = self.claim(
            unavailable=_backlog.BacklogUnavailable("TLS handshake failed twice")
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "github_unreachable"', err)
        self.assertNotIn('"reason": "issue_policy_refusal"', err)
        self.assertIn('"attempts": 2', err)
        write.assert_not_called()

    def test_closed_issue_and_pull_request_have_distinct_activation_refusals(self) -> None:
        for reason in ("issue_closed", "pull_request"):
            with self.subTest(reason=reason):
                code, err, write, load = self.claim(
                    unavailable=_backlog.ExactIssuePopulationError(
                        reason,
                        repository="pixexid/llm-collab",
                        issue_number=756,
                    )
                )
                self.assertEqual(code, 1)
                self.assertIn(
                    f'"reason": "{reason}"',
                    err,
                    "activation must distinguish closed issues and pull requests from policy and availability refusals",
                )
                self.assertNotIn('"reason": "issue_policy_refusal"', err)
                self.assertNotIn('"reason": "github_unreachable"', err)
                load.assert_not_called()
                write.assert_not_called()

    def test_active_issue_passes_policy_gate_to_the_next_existing_gate(self) -> None:
        active = _backlog.classify_issue_labels(
            ("state:active",), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
        )
        code, err, write, _ = self.claim(active)
        self.assertEqual(code, 1)
        self.assertNotIn("issue_policy_refusal", err)
        self.assertIn("has not been refined", err)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
