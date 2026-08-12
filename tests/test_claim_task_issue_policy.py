from __future__ import annotations

import io
import json
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


class SupervisorAcceptanceClaimTest(unittest.TestCase):
    TASK_ID = "TASK-573923"
    DECISION = "DEC-GH1621-REFINE-1"

    def supervisor_record(self) -> dict:
        return {
            "supervisor_acceptance_override": True,
            "supervisor_acceptance_override_decision": self.DECISION,
            "supervisor_acceptance_override_thread": "thr_pft3kb9hsm",
            "supervisor_acceptance_override_scope": "TASK-573923 / GH-1621 only",
            "supervisor_acceptance_override_non_precedent": True,
            "supervisor_acceptance_override_revert": "release claim and requeue",
            "supervisor_acceptance_override_provenance_followup": (
                "GH-784-class machine-distinguishable two-key provenance seam"
            ),
        }

    def raw_record(self, **overrides: object) -> str:
        frontmatter = {**self.supervisor_record(), **overrides}
        return claim_task.dump_frontmatter(frontmatter, "# GH-1621 verify equivalence")

    def invoke(
        self,
        supervisor_fields: dict | None = None,
        *,
        project_id: str = "amiga",
        frontmatter_task_id: str | None = None,
        task_title: str = "GH-1621 verify equivalence",
        raw_content: str | None = None,
        frontmatter_overrides: dict | None = None,
        risk_errors: list[str] | None = None,
        contract_errors: list[str] | None = None,
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "Tasks" / "active" / f"fixture__{self.TASK_ID}.md"
            task.parent.mkdir(parents=True)
            frontmatter = {
                "task_id": (
                    self.TASK_ID if frontmatter_task_id is None else frontmatter_task_id
                ),
                "title": task_title,
                "status": "open",
                "owner": "codex",
                "created_by": "codex",
                "project_id": project_id,
                "depends_on": [],
                "skip_refinement": False,
                "refined_by": None,
            }
            if frontmatter_overrides is not None:
                frontmatter.update(frontmatter_overrides)
            if supervisor_fields is not None:
                frontmatter.update(supervisor_fields)
            body = f"# {task_title}"
            task.write_text(
                claim_task.dump_frontmatter(frontmatter, body)
                if raw_content is None
                else raw_content
            )
            before = task.read_text()
            stderr = io.StringIO()
            stdout = io.StringIO()
            writer = patch.object(claim_task, "write_file")
            risk = patch.object(
                claim_task,
                "validate_implementation_risk_analysis",
                return_value=(risk_errors if risk_errors is not None else ["risk sentinel"]),
            )
            contract = patch.object(
                claim_task,
                "validate_task_contract",
                return_value=(contract_errors if contract_errors is not None else [], {}),
            )
            remover = patch.object(Path, "unlink")
            active_policy = _backlog.IssuePolicy(
                "active", "state:active", "state:active", ("state:active",)
            )
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "claim_task.py",
                        "--task",
                        self.TASK_ID,
                        "--owner",
                        "codex",
                        "--status",
                        "in_progress",
                        "--skip-preflight",
                    ],
                ),
                patch.object(claim_task, "ROOT", root),
                patch.object(claim_task, "agent_ids", return_value=["codex"]),
                patch.object(claim_task, "ensure_agent_enabled"),
                patch.object(claim_task, "find_task_by_id", return_value=task),
                patch.object(
                    claim_task,
                    "sync_task_contract",
                    return_value=(frontmatter, body),
                ),
                patch.object(
                    claim_task,
                    "validate_direct_app_policy",
                    return_value=([], {}),
                ),
                patch.object(claim_task.issue_queue, "queue_exists", return_value=False),
                patch.object(
                    claim_task._backlog,
                    "exact_issue_policy",
                    return_value=(f"pixexid/{project_id}", active_policy),
                ),
                patch.object(claim_task, "target_task_path", return_value=task),
                patch.object(claim_task, "utc_iso", return_value="2026-08-12T00:00:00+00:00"),
                writer as write,
                remover as remove,
                risk as risk_mock,
                contract as contract_mock,
                redirect_stderr(stderr),
                redirect_stdout(stdout),
            ):
                exit_code = 0
                try:
                    claim_task.main()
                except SystemExit as error:
                    exit_code = int(error.code)
            return (
                exit_code,
                stdout.getvalue(),
                stderr.getvalue(),
                write,
                remove,
                risk_mock,
                before,
                task.read_text(),
            )

    def test_complete_real_record_reaches_the_existing_risk_gate(self) -> None:
        code, _stdout, stderr, write, _remove, risk, _before, _after = self.invoke(
            self.supervisor_record()
        )
        self.assertEqual(code, 1)
        self.assertIn("implementation risk analysis is incomplete", stderr)
        self.assertNotIn("supervisor_acceptance_invalid", stderr)
        risk.assert_called_once()
        write.assert_not_called()

    def test_complete_real_record_mutates_and_names_the_exact_decision(self) -> None:
        code, stdout, stderr, write, _remove, _risk, _before, _after = self.invoke(
            self.supervisor_record(),
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual([], stderr.splitlines())
        self.assertEqual("in_progress", json.loads(stdout)["new_status"])
        write.assert_called_once()
        _path, rendered = write.call_args.args
        self.assertIn(
            f"supervisor_acceptance_override_decision={self.DECISION}",
            rendered,
        )

    def test_no_supervisor_record_preserves_the_existing_refinement_refusal(self) -> None:
        code, _stdout, stderr, write, _remove, risk, before, after = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("has not been refined by claude", stderr)
        self.assertNotIn("supervisor_acceptance_invalid", stderr)
        risk.assert_not_called()
        write.assert_not_called()
        self.assertEqual(before, after)

    def test_invalid_supervisor_shapes_refuse_before_risk_or_write(self) -> None:
        complete = self.supervisor_record()
        cases = {
            "missing": {"supervisor_acceptance_override": True},
            "false": {**complete, "supervisor_acceptance_override": False},
            "padded": {
                **complete,
                "supervisor_acceptance_override_decision": f" {self.DECISION}",
            },
            "malformed": {
                **complete,
                "supervisor_acceptance_override_scope": "TASK-573923 / GH-1621",
            },
            "partial": {
                key: value
                for key, value in complete.items()
                if key != "supervisor_acceptance_override_provenance_followup"
            },
            "cross-task": {
                **complete,
                "supervisor_acceptance_override_scope": "TASK-OTHER / GH-1621 only",
            },
        }
        for label, fields in cases.items():
            with self.subTest(label=label):
                code, _stdout, stderr, write, _remove, risk, before, after = self.invoke(fields)
                self.assertEqual(code, 1)
                self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
                self.assertIn("supervisor_acceptance", stderr)
                write.assert_not_called()
                risk.assert_not_called()
                self.assertEqual(before, after)

    def test_decision_rejects_newline_and_control_injection_before_mutation(self) -> None:
        for label, decision in {
            "newline": f"{self.DECISION}\nforged activity entry",
            "control": f"{self.DECISION}\x1bforged activity entry",
        }.items():
            with self.subTest(label=label):
                code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
                    {
                        **self.supervisor_record(),
                        "supervisor_acceptance_override_decision": decision,
                    },
                    risk_errors=[],
                    contract_errors=[],
                )
                self.assertEqual(code, 1)
                self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
                self.assertIn("printable single-line authority token", stderr)
                write.assert_not_called()
                remove.assert_not_called()
                risk.assert_not_called()
                self.assertEqual(before, after)

    def test_selector_frontmatter_mismatch_refuses_before_write_or_move(self) -> None:
        code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
            self.supervisor_record(),
            frontmatter_task_id="TASK-OTHER",
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "task_selector_mismatch"', stderr)
        self.assertIn(f'"task_selector": "{self.TASK_ID}"', stderr)
        self.assertIn('"record_task_id": "TASK-OTHER"', stderr)
        write.assert_not_called()
        remove.assert_not_called()
        risk.assert_not_called()
        self.assertEqual(before, after)

    def test_invalid_override_never_falls_through_legacy_authority(self) -> None:
        malformed = {
            **self.supervisor_record(),
            "supervisor_acceptance_override_scope": "TASK-573923 / GH-1621",
        }
        for legacy in (
            {"skip_refinement": True},
            {"refined_by": "claude"},
        ):
            with self.subTest(legacy=legacy):
                code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
                    malformed,
                    frontmatter_overrides=legacy,
                )
                self.assertEqual(code, 1)
                self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
                write.assert_not_called()
                remove.assert_not_called()
                risk.assert_not_called()
                self.assertEqual(before, after)

    def test_unknown_override_field_refuses_before_legacy_authority_or_mutation(self) -> None:
        for legacy in (
            {"refined_by": "claude"},
            {"skip_refinement": True},
        ):
            with self.subTest(legacy=legacy):
                code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
                    {"supervisor_acceptance_override_decison": self.DECISION},
                    frontmatter_overrides=legacy,
                )
                self.assertEqual(code, 1)
                self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
                self.assertIn(
                    "supervisor_acceptance_override_decison is not an allowed supervisor acceptance field",
                    stderr,
                )
                write.assert_not_called()
                remove.assert_not_called()
                risk.assert_not_called()
                self.assertEqual(before, after)

    def test_bootstrap_metadata_extensions_preserve_complete_record(self) -> None:
        code, stdout, stderr, write, remove, _risk, _before, _after = self.invoke(
            {
                **self.supervisor_record(),
                "supervisor_acceptance_override_bootstrap_decision": "DEC-GH1621-ACTIVATION-1",
                "supervisor_acceptance_override_bootstrap_thread": "thr_m5m4xgf6u3",
            },
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual("in_progress", json.loads(stdout)["new_status"])
        write.assert_called_once()
        remove.assert_not_called()

    def test_raw_padded_decision_value_refuses_before_legacy_authority_or_mutation(self) -> None:
        raw_content = self.raw_record(refined_by="claude").replace(
            f"supervisor_acceptance_override_decision: {self.DECISION}",
            f"supervisor_acceptance_override_decision:  {self.DECISION}",
            1,
        )
        code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
            raw_content=raw_content,
            frontmatter_overrides={"refined_by": "claude"},
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
        self.assertIn("value must not be padded", stderr)
        write.assert_not_called()
        remove.assert_not_called()
        risk.assert_not_called()
        self.assertEqual(before, after)

    def test_raw_padded_decision_key_refuses_before_legacy_authority_or_mutation(self) -> None:
        raw_content = self.raw_record(skip_refinement=True).replace(
            f"supervisor_acceptance_override_decision: {self.DECISION}",
            f" supervisor_acceptance_override_decision: {self.DECISION}",
            1,
        )
        code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
            raw_content=raw_content,
            frontmatter_overrides={"skip_refinement": True},
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
        self.assertIn("key must not be padded", stderr)
        write.assert_not_called()
        remove.assert_not_called()
        risk.assert_not_called()
        self.assertEqual(before, after)

    def test_raw_duplicate_supervisor_field_refuses_before_mutation(self) -> None:
        raw_content = self.raw_record().replace(
            "\n---\n\n# GH-1621 verify equivalence",
            "\nsupervisor_acceptance_override_thread: thr_pft3kb9hsm\n"
            "supervisor_acceptance_override_thread: thr_pft3kb9hsm\n---",
            1,
        )
        code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
            raw_content=raw_content,
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
        self.assertIn("must not be duplicated", stderr)
        write.assert_not_called()
        remove.assert_not_called()
        risk.assert_not_called()
        self.assertEqual(before, after)

    def test_registered_non_amiga_project_accepts_matching_derived_issue_scope(self) -> None:
        code, stdout, stderr, write, remove, _risk, _before, _after = self.invoke(
            self.supervisor_record(),
            project_id="llm-collab",
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual("in_progress", json.loads(stdout)["new_status"])
        write.assert_called_once()
        remove.assert_not_called()
        _path, rendered = write.call_args.args
        self.assertIn(
            f"supervisor_acceptance_override_decision={self.DECISION}",
            rendered,
        )

    def test_mismatched_derived_issue_scope_refuses_before_risk_or_write(self) -> None:
        fields = {
            **self.supervisor_record(),
            "supervisor_acceptance_override_scope": "TASK-573923 / GH-999 only",
        }
        code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
            fields,
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
        self.assertIn("issue number must equal the derived issue number", stderr)
        write.assert_not_called()
        remove.assert_not_called()
        risk.assert_not_called()
        self.assertEqual(before, after)

    def test_missing_derived_issue_refuses_before_risk_or_write(self) -> None:
        code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
            self.supervisor_record(),
            task_title="verify equivalence",
            risk_errors=[],
            contract_errors=[],
        )
        self.assertEqual(code, 1)
        self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
        self.assertIn("issue number cannot be derived", stderr)
        write.assert_not_called()
        remove.assert_not_called()
        risk.assert_not_called()
        self.assertEqual(before, after)

    def test_registered_non_amiga_project_refuses_malformed_and_cross_task_records(self) -> None:
        complete = self.supervisor_record()
        cases = {
            "malformed": {
                **complete,
                "supervisor_acceptance_override_scope": "TASK-573923 / GH-1621",
            },
            "cross-task": {
                **complete,
                "supervisor_acceptance_override_scope": "TASK-OTHER / GH-1621 only",
            },
        }
        for label, fields in cases.items():
            with self.subTest(label=label):
                code, _stdout, stderr, write, remove, risk, before, after = self.invoke(
                    fields,
                    project_id="llm-collab",
                )
                self.assertEqual(code, 1)
                self.assertIn('"reason": "supervisor_acceptance_invalid"', stderr)
                write.assert_not_called()
                remove.assert_not_called()
                risk.assert_not_called()
                self.assertEqual(before, after)

    def test_legacy_authorities_still_win_without_supervisor_validation(self) -> None:
        self.assertEqual(
            (True, None),
            claim_task.resolve_activation_authority(
                {"skip_refinement": True}, self.TASK_ID
            ),
        )
        self.assertEqual(
            (True, None),
            claim_task.resolve_activation_authority(
                {"refined_by": "claude"}, self.TASK_ID
            ),
        )


if __name__ == "__main__":
    unittest.main()
