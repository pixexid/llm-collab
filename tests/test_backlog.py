from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog


class BacklogConfigAndPolicyTest(unittest.TestCase):
    def project(self, project_id="amiga", backlog=None):
        github = {"enabled": True, "repo": f"pixexid/{project_id}"}
        if backlog is not None:
            github["backlog"] = backlog
        return {"id": project_id, "github": github}

    def test_contract_floor_is_union_for_amiga_and_non_amiga_configurations(self) -> None:
        for project_id, configured in (
            ("amiga", ["type:epic"]),
            ("nuvyr", ["status:deferred", "epic"]),
        ):
            with self.subTest(project_id=project_id):
                with patch.object(
                    _backlog,
                    "get_project",
                    return_value=self.project(project_id, {"exclude_labels": configured}),
                ):
                    effective = _backlog.project_backlog_config(project_id)["exclude_labels"]
                self.assertEqual(
                    effective[:2],
                    ["epic", "state:parked"],
                    "contract floor must retain epic and state:parked for every project",
                )

    def test_absent_and_empty_configuration_both_keep_floor(self) -> None:
        for backlog in ({}, {"exclude_labels": []}):
            with self.subTest(backlog=backlog):
                with patch.object(
                    _backlog, "get_project", return_value=self.project("docs", backlog)
                ):
                    effective = _backlog.project_backlog_config("docs")["exclude_labels"]
                self.assertEqual(effective[:2], ["epic", "state:parked"])

    def test_custom_exclusions_are_preserved_and_duplicates_removed(self) -> None:
        with patch.object(
            _backlog,
            "get_project",
            return_value=self.project(
                "amiga", {"exclude_labels": ["epic", "custom:*", "CUSTOM:*"]}
            ),
        ):
            effective = _backlog.project_backlog_config("amiga")["exclude_labels"]
        self.assertEqual(effective, ["epic", "state:parked", "custom:*"])

    def test_malformed_exclusion_configuration_fails_before_github_call(self) -> None:
        for malformed in ("epic", ["epic", ""], ["epic", 7], [" "]):
            with self.subTest(malformed=malformed):
                with (
                    patch.object(
                        _backlog,
                        "get_project",
                        return_value=self.project(
                            "amiga", {"exclude_labels": malformed}
                        ),
                    ),
                    patch.object(_backlog, "load_open_github_issues") as github,
                ):
                    with self.assertRaisesRegex(ValueError, "non-empty strings"):
                        _backlog.eligible_open_issues("amiga")
                github.assert_not_called()

    def test_closed_state_vocabulary_and_exclusions_use_one_classifier(self) -> None:
        cases = (
            (("state:active",), "active", "state:active"),
            (("state:blocked",), "blocked", "github:state:blocked"),
            (("state:parked",), "excluded", "excluded:state:parked"),
            (("epic", "state:active"), "excluded", "excluded:epic"),
            ((), "invalid", "missing_state"),
            (("state:active", "state:blocked"), "invalid", "multiple_states"),
            (("state:banana",), "invalid", "unknown_state"),
            (("state:active", "state:banana"), "invalid", "multiple_states"),
        )
        for labels, classification, reason in cases:
            with self.subTest(labels=labels):
                policy = _backlog.classify_issue_labels(
                    labels, _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
                )
                self.assertEqual(policy.classification, classification)
                self.assertEqual(policy.reason, reason)

    def test_epic_is_orthogonal_and_does_not_replace_missing_state(self) -> None:
        policy = _backlog.classify_issue_labels(
            ("epic",), _backlog.CONTRACT_REQUIRED_EXCLUDE_LABELS
        )
        self.assertEqual(policy.classification, "invalid")
        self.assertEqual(policy.reason, "missing_state")

    def test_every_state_violation_reports_exact_issue_and_observed_labels(self) -> None:
        issues = [
            self.issue(10, "Missing", []),
            self.issue(11, "Multiple", ["state:active", "state:blocked"]),
            self.issue(12, "Unknown", ["state:banana"]),
            self.issue(13, "Mixed", ["state:active", "state:unknown"]),
        ]
        with (
            patch.object(_backlog, "get_project", return_value=self.project()),
            patch.object(_backlog, "load_open_github_issues", return_value=issues),
        ):
            with self.assertRaises(_backlog.IssueStatePolicyError) as raised:
                _backlog.eligible_open_issues("amiga")
        self.assertEqual(
            [(item["issue"], item["found"]) for item in raised.exception.violations],
            [
                (10, []),
                (11, ["state:active", "state:blocked"]),
                (12, ["state:banana"]),
                (13, ["state:active", "state:unknown"]),
            ],
        )

    def test_active_and_blocked_candidates_survive_while_parked_and_epic_do_not(self) -> None:
        issues = [
            self.issue(10, "Active", ["state:active"]),
            self.issue(11, "Blocked", ["state:blocked"]),
            self.issue(12, "Parked", ["state:parked"]),
            self.issue(13, "Epic", ["epic", "state:active"]),
        ]
        with (
            patch.object(_backlog, "get_project", return_value=self.project()),
            patch.object(_backlog, "load_open_github_issues", return_value=issues),
        ):
            eligible = _backlog.eligible_open_issues("amiga")
        self.assertEqual([issue.number for issue in eligible], [10, 11])
        self.assertEqual(
            [issue.issue_state for issue in eligible],
            ["state:active", "state:blocked"],
        )

    def test_exact_issue_policy_requires_open_non_pull_request_population(self) -> None:
        records = {
            "issue_closed": {
                "number": 756,
                "title": "Closed issue",
                "state": "closed",
                "labels": [{"name": "state:active"}],
            },
            "pull_request": {
                "number": 756,
                "title": "Open pull request",
                "state": "open",
                "labels": [{"name": "state:active"}],
                "pull_request": {},
            },
        }
        for expected_reason, record in records.items():
            with self.subTest(reason=expected_reason):
                with (
                    patch.object(_backlog, "get_project", return_value=self.project()),
                    patch.object(_backlog, "load_github_issue", return_value=record),
                    self.assertRaises(
                        _backlog.ExactIssuePopulationError,
                        msg="exact issue population gate must reject closed issues and pull requests before classification",
                    ) as raised,
                ):
                    _backlog.exact_issue_policy("amiga", 756)
                self.assertEqual(raised.exception.reason, expected_reason)

    def test_exact_issue_policy_keeps_open_issue_active(self) -> None:
        record = {
            "number": 756,
            "title": "Open issue",
            "state": "open",
            "labels": [{"name": "state:active"}],
        }
        with (
            patch.object(_backlog, "get_project", return_value=self.project()),
            patch.object(_backlog, "load_github_issue", return_value=record),
        ):
            repository, policy = _backlog.exact_issue_policy("amiga", 756)
        self.assertEqual(repository, "pixexid/amiga")
        self.assertEqual(policy.classification, "active")

    def test_required_patterns_and_priority_preserve_existing_behavior(self) -> None:
        project = self.project(
            "amiga",
            {
                "exclude_labels": [],
                "require_any_label": ["area:*"],
                "priority_labels": ["priority:high"],
            },
        )
        issues = [
            self.issue(10, "Ordinary", ["state:active", "area:ops"]),
            self.issue(20, "Priority", ["state:active", "area:ops", "priority:high"]),
            self.issue(5, "Wrong area", ["state:active", "needs:triage"]),
        ]
        with (
            patch.object(_backlog, "get_project", return_value=project),
            patch.object(_backlog, "load_open_github_issues", return_value=issues),
        ):
            eligible = _backlog.eligible_open_issues("amiga")
        self.assertEqual([issue.number for issue in eligible], [20, 10])
        self.assertEqual(eligible[0].priority_rank, 1)

    def test_disabled_github_project_has_empty_backlog(self) -> None:
        with patch.object(
            _backlog,
            "get_project",
            return_value={"id": "docs", "github": {"enabled": False}},
        ):
            self.assertEqual(_backlog.eligible_open_issues("docs"), [])

    @staticmethod
    def issue(number: int, title: str, labels: list[str]) -> dict:
        return {
            "number": number,
            "title": title,
            "html_url": f"https://github.com/pixexid/amiga/issues/{number}",
            "labels": [{"name": label} for label in labels],
        }


class BacklogPaginationTest(unittest.TestCase):
    def completed(self, payload, *, returncode=0, stderr=""):
        return subprocess.CompletedProcess(
            args=["gh"],
            returncode=returncode,
            stdout=json.dumps(payload),
            stderr=stderr,
        )

    def page(self, start: int, count: int) -> list[dict]:
        return [
            {
                "number": number,
                "title": f"Issue {number}",
                "labels": [{"name": "state:active"}],
            }
            for number in range(start, start + count)
        ]

    def test_load_open_github_issues_aggregates_every_page_until_exhaustion(self) -> None:
        first = self.page(1, _backlog.GH_PAGE_SIZE)
        second = self.page(_backlog.GH_PAGE_SIZE + 1, 2)
        with patch.object(
            _backlog,
            "_run_gh_bounded",
            side_effect=[self.completed(first), self.completed(second)],
        ) as run:
            issues = _backlog.load_open_github_issues("pixexid/amiga")
        self.assertEqual(
            len(issues),
            _backlog.GH_PAGE_SIZE + 2,
            "complete enumeration must aggregate the second page",
        )
        self.assertIn("repos/pixexid/amiga/issues", run.call_args_list[0].args[0])
        self.assertIn("page=2", run.call_args_list[1].args[0])

    def test_pull_requests_do_not_enter_the_issue_population(self) -> None:
        payload = [
            {"number": 1, "title": "Issue", "labels": []},
            {"number": 2, "title": "PR", "labels": [], "pull_request": {}},
        ]
        with patch.object(
            _backlog, "_run_gh_bounded", return_value=self.completed(payload)
        ):
            issues = _backlog.load_open_github_issues("pixexid/amiga")
        self.assertEqual([item["number"] for item in issues], [1])

    def test_second_page_failure_returns_no_partial_clean_result(self) -> None:
        first = self.page(1, _backlog.GH_PAGE_SIZE)
        with patch.object(
            _backlog,
            "_run_gh_bounded",
            side_effect=[
                self.completed(first),
                self.completed([], returncode=1, stderr="page two failed"),
            ],
        ):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "page two failed"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_cumulative_byte_budget_exceed_returns_no_partial_clean_result(self) -> None:
        first = self.page(1, _backlog.GH_PAGE_SIZE)
        second = self.page(_backlog.GH_PAGE_SIZE + 1, 1)
        first_result = self.completed(first)
        second_result = self.completed(second)
        cumulative = len(first_result.stdout.encode()) + len(second_result.stdout.encode()) - 1
        with (
            patch.object(_backlog, "GH_TOTAL_MAX_OUTPUT_BYTES", cumulative),
            patch.object(
                _backlog,
                "_run_gh_bounded",
                side_effect=[first_result, second_result],
            ),
        ):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "cumulative"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_full_final_budget_page_fails_instead_of_claiming_exhaustion(self) -> None:
        full = self.page(1, _backlog.GH_PAGE_SIZE)
        with (
            patch.object(_backlog, "GH_MAX_PAGES", 1),
            patch.object(_backlog, "GH_MAX_ITEMS", _backlog.GH_PAGE_SIZE),
            patch.object(_backlog, "_run_gh_bounded", return_value=self.completed(full)),
        ):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "without proving exhaustion"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_cumulative_item_budget_exceed_is_explicit(self) -> None:
        first = self.page(1, _backlog.GH_PAGE_SIZE)
        second = self.page(_backlog.GH_PAGE_SIZE + 1, 1)
        with (
            patch.object(_backlog, "GH_MAX_ITEMS", _backlog.GH_PAGE_SIZE),
            patch.object(
                _backlog,
                "_run_gh_bounded",
                side_effect=[self.completed(first), self.completed(second)],
            ),
        ):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "item budget"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_configured_repository_is_explicit_outside_its_checkout(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as other:
            try:
                os.chdir(other)
                with patch.object(
                    _backlog,
                    "_run_gh_bounded",
                    return_value=self.completed([]),
                ) as run:
                    _backlog.load_open_github_issues("configured/authority")
            finally:
                os.chdir(previous)
        command = run.call_args.args[0]
        self.assertIn("repos/configured/authority/issues", command)

    def test_invalid_json_and_non_list_page_fail_closed(self) -> None:
        invalid = subprocess.CompletedProcess(["gh"], 0, "{", "")
        for result, message in (
            (invalid, "invalid JSON"),
            (self.completed({"number": 1}), "non-list"),
        ):
            with self.subTest(message=message):
                with patch.object(_backlog, "_run_gh_bounded", return_value=result):
                    with self.assertRaisesRegex(_backlog.BacklogUnavailable, message):
                        _backlog.load_open_github_issues("pixexid/amiga")

    def test_non_object_page_item_fails_instead_of_being_silently_dropped(self) -> None:
        with patch.object(
            _backlog,
            "_run_gh_bounded",
            return_value=self.completed([{"number": 1}, "malformed"]),
        ):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "non-object item"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_exact_issue_retries_once_then_succeeds(self) -> None:
        failure = self.completed({}, returncode=1, stderr="temporary TLS failure")
        success = self.completed(
            {"number": 756, "title": "State model", "labels": [{"name": "state:active"}]}
        )
        with (
            patch.object(_backlog, "GH_RETRY_DELAY_SECONDS", 0),
            patch.object(
                _backlog, "_run_gh_bounded", side_effect=[failure, success]
            ) as run,
        ):
            issue = _backlog.load_github_issue("pixexid/llm-collab", 756)
        self.assertEqual(issue["number"], 756)
        self.assertEqual(run.call_count, 2)

    def test_missing_gh_is_typed_as_backlog_unavailable(self) -> None:
        with patch.object(_backlog.subprocess, "Popen", side_effect=FileNotFoundError("gh")):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "gh api unavailable"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_per_call_output_is_bounded_while_the_child_is_read(self) -> None:
        with patch.object(_backlog, "GH_PAGE_MAX_OUTPUT_BYTES", 32):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "output exceeds 32 bytes"):
                _backlog._run_gh_bounded(
                    [sys.executable, "-c", "print('x' * 100)"]
                )

    def test_per_call_deadline_kills_a_stalled_child(self) -> None:
        with patch.object(_backlog, "GH_PAGE_TIMEOUT_SECONDS", 0.05):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "exceeded 0.05s"):
                _backlog._run_gh_bounded(
                    [sys.executable, "-c", "import time; time.sleep(1)"]
                )


if __name__ == "__main__":
    unittest.main()
