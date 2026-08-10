from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog


class BacklogTest(unittest.TestCase):
    def test_eligible_open_issues_excludes_epics_deferred_and_terminal_labels(self) -> None:
        project = {
            "id": "amiga",
            "github": {
                "enabled": True,
                "repo": "pixexid/amiga",
                "backlog": {
                    "exclude_labels": ["type:epic", "status:deferred", "duplicate"],
                    "require_any_label": [],
                },
            },
        }
        issues = [
            self.issue(10, "Runtime bug", ["area:operations"]),
            self.issue(11, "Planning parent", ["type:epic"]),
            self.issue(12, "Later work", ["status:deferred"]),
            self.issue(13, "Duplicate", ["duplicate"]),
        ]

        with patch.object(_backlog, "get_project", return_value=project):
            with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                eligible = _backlog.eligible_open_issues("amiga")

        self.assertEqual([issue.number for issue in eligible], [10])

    def test_contract_exclusions_augment_realistic_amiga_and_nuvyr_configs(self) -> None:
        legacy_exclusions = [
            "type:epic",
            "wontfix",
            "duplicate",
            "invalid",
            "question",
            "status:deferred",
        ]
        for project_id, repo in (
            ("amiga", "pixexid/amiga"),
            ("nuvyr", "pixexid/nuvyr"),
        ):
            with self.subTest(project_id=project_id):
                project = {
                    "id": project_id,
                    "github": {
                        "enabled": True,
                        "repo": repo,
                        "backlog": {"exclude_labels": legacy_exclusions},
                    },
                }
                issues = [
                    self.issue(10, "Parked", ["state:parked"], repo=repo),
                    self.issue(11, "Epic", ["epic"], repo=repo),
                    self.issue(12, "Active", ["state:active"], repo=repo),
                    self.issue(13, "Unlabeled", [], repo=repo),
                    self.issue(14, "Legacy epic", ["type:epic"], repo=repo),
                    self.issue(15, "Legacy deferred", ["status:deferred"], repo=repo),
                ]
                with patch.object(_backlog, "get_project", return_value=project):
                    with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                        eligible = _backlog.eligible_open_issues(project_id)

                self.assertEqual([issue.number for issue in eligible], [12, 13])

    def test_contract_exclusions_apply_without_backlog_config(self) -> None:
        project = {
            "id": "synthetic-defaults",
            "github": {"enabled": True, "repo": "example/synthetic-defaults"},
        }
        issues = [
            self.issue(10, "Parked", ["state:parked"]),
            self.issue(11, "Epic", ["epic"]),
            self.issue(12, "Active", ["state:active"]),
            self.issue(13, "Unlabeled", []),
        ]

        with patch.object(_backlog, "get_project", return_value=project):
            with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                eligible = _backlog.eligible_open_issues("synthetic-defaults")

        self.assertEqual([issue.number for issue in eligible], [12, 13])

    def test_contract_exclusions_preserve_project_specific_exclusions(self) -> None:
        project = {
            "id": "synthetic-custom",
            "github": {
                "enabled": True,
                "repo": "example/synthetic-custom",
                "backlog": {
                    "exclude_labels": ["team:paused", "epic", "team:paused"]
                },
            },
        }
        issues = [
            self.issue(10, "Parked", ["state:parked"]),
            self.issue(11, "Epic", ["epic"]),
            self.issue(12, "Active", ["state:active"]),
            self.issue(13, "Unlabeled", []),
            self.issue(14, "Project exclusion", ["team:paused"]),
        ]

        with patch.object(_backlog, "get_project", return_value=project):
            config = _backlog.project_backlog_config("synthetic-custom")
            with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                eligible = _backlog.eligible_open_issues("synthetic-custom")

        self.assertEqual(
            config["exclude_labels"],
            ["team:paused", "epic", "state:parked"],
        )
        self.assertEqual([issue.number for issue in eligible], [12, 13])

    def test_eligible_open_issues_includes_non_parity_titles_by_default(self) -> None:
        project = {
            "id": "amiga",
            "github": {
                "enabled": True,
                "repo": "pixexid/amiga",
                "backlog": {"exclude_labels": ["type:epic"], "require_any_label": []},
            },
        }
        issues = [self.issue(44, "Fix notification persistence", ["area:notifications"])]

        with patch.object(_backlog, "get_project", return_value=project):
            with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                eligible = _backlog.eligible_open_issues("amiga")

        self.assertEqual([issue.number for issue in eligible], [44])

    def test_eligible_open_issues_supports_required_label_patterns(self) -> None:
        project = {
            "id": "amiga",
            "github": {
                "enabled": True,
                "repo": "pixexid/amiga",
                "backlog": {
                    "exclude_labels": ["status:deferred"],
                    "require_any_label": ["area:*"],
                },
            },
        }
        issues = [
            self.issue(20, "Area issue", ["area:dispatch"]),
            self.issue(21, "Untriaged issue", ["needs:triage"]),
        ]

        with patch.object(_backlog, "get_project", return_value=project):
            with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                eligible = _backlog.eligible_open_issues("amiga")

        self.assertEqual([issue.number for issue in eligible], [20])

    def test_amiga_priority_labels_rank_before_issue_number_with_stable_ties(self) -> None:
        project = {
            "id": "amiga",
            "github": {
                "enabled": True,
                "repo": "pixexid/amiga",
                "backlog": {
                    "priority_labels": ["area:livecraft", "priority:high"],
                },
            },
        }
        issues = [
            self.issue(10, "Unmatched", ["area:operations"]),
            self.issue(50, "Later livecraft", ["area:livecraft"]),
            self.issue(40, "High priority", ["priority:high"]),
            self.issue(30, "Earlier livecraft", ["area:livecraft"]),
            self.issue(
                35,
                "First configured match wins",
                ["priority:high", "area:livecraft"],
            ),
        ]

        with patch.object(_backlog, "get_project", return_value=project):
            with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                eligible = _backlog.eligible_open_issues("amiga")

        self.assertEqual([issue.number for issue in eligible], [30, 35, 50, 40, 10])
        self.assertEqual(
            [(issue.priority_label, issue.priority_rank) for issue in eligible],
            [
                ("area:livecraft", 1),
                ("area:livecraft", 1),
                ("area:livecraft", 1),
                ("priority:high", 2),
                (None, None),
            ],
        )

    def test_non_amiga_absent_or_empty_priority_labels_preserve_issue_order(self) -> None:
        issues = [
            self.issue(9, "Later issue", ["priority:high"]),
            self.issue(2, "Earlier issue", []),
        ]
        for backlog in ({}, {"priority_labels": []}):
            with self.subTest(backlog=backlog):
                project = {
                    "id": "nuvyr",
                    "github": {
                        "enabled": True,
                        "repo": "pixexid/nuvyr",
                        "backlog": backlog,
                    },
                }
                with patch.object(_backlog, "get_project", return_value=project):
                    with patch.object(_backlog, "load_open_github_issues", return_value=issues):
                        eligible = _backlog.eligible_open_issues("nuvyr")

                self.assertEqual([issue.number for issue in eligible], [2, 9])
                self.assertTrue(all(issue.priority_rank is None for issue in eligible))

    def test_disabled_github_project_has_empty_backlog(self) -> None:
        with patch.object(_backlog, "get_project", return_value={"id": "docs", "github": {"enabled": False}}):
            self.assertEqual(_backlog.eligible_open_issues("docs"), [])

    def test_load_open_github_issues_reports_unavailable_gh(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh"],
            returncode=1,
            stdout="",
            stderr="authentication required",
        )

        with patch("subprocess.run", return_value=result):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "authentication required"):
                _backlog.load_open_github_issues("pixexid/amiga")

    def test_load_open_github_issues_reports_missing_gh_as_unavailable(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh")):
            with self.assertRaisesRegex(_backlog.BacklogUnavailable, "gh issue list unavailable"):
                _backlog.load_open_github_issues("pixexid/amiga")

    @staticmethod
    def issue(
        number: int,
        title: str,
        labels: list[str],
        *,
        repo: str = "pixexid/amiga",
    ) -> dict:
        return {
            "number": number,
            "title": title,
            "url": f"https://github.com/{repo}/issues/{number}",
            "labels": [{"name": label} for label in labels],
        }


if __name__ == "__main__":
    unittest.main()
