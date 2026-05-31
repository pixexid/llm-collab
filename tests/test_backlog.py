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

    @staticmethod
    def issue(number: int, title: str, labels: list[str]) -> dict:
        return {
            "number": number,
            "title": title,
            "url": f"https://github.com/pixexid/amiga/issues/{number}",
            "labels": [{"name": label} for label in labels],
        }


if __name__ == "__main__":
    unittest.main()
