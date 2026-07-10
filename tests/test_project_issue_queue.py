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


class ProjectIssueQueueNormalizeTest(unittest.TestCase):
    def test_normalize_unblocks_lane_when_exact_task_dependency_is_done(self) -> None:
        payload = {
            "project_id": "amiga",
            "completed_recently": [
                {"issue": 783, "task_id": "TASK-4B2049", "owner": "claude", "status": "done"}
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 784,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "blocked",
                    "depends_on": ["TASK-4B2049"],
                    "blocked_by": ["TASK-4B2049"],
                }
            ],
        }

        project_issue_queue.normalize_lanes(payload)

        self.assertEqual(payload["lanes"][0]["queue_state"], "ready")
        self.assertEqual(payload["lanes"][0]["blocked_by"], [])

    def test_normalize_unblocks_stale_queue_order_blocker_after_issue_done(self) -> None:
        payload = {
            "project_id": "amiga",
            "completed_recently": [
                {"issue": 793, "task_id": "TASK-513B56", "owner": "claude", "status": "done"},
                {"issue": 777, "task_id": "TASK-58746F", "owner": "claude", "status": "done"},
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 795,
                    "task_id": "TASK-BAFE85",
                    "owner": "unassigned",
                    "task_status": "open",
                    "queue_state": "blocked",
                    "depends_on": ["TASK-58746F"],
                    "blocked_by": ["GH-793/TASK-513B56 queue order"],
                }
            ],
        }

        project_issue_queue.normalize_lanes(payload)

        self.assertEqual(payload["lanes"][0]["queue_state"], "ready")
        self.assertEqual(payload["lanes"][0]["blocked_by"], [])

    def test_normalize_keeps_external_evidence_blocker_blocked(self) -> None:
        payload = {
            "project_id": "amiga",
            "completed_recently": [
                {"issue": 760, "task_id": "TASK-38DCF3", "owner": "codex", "status": "done"}
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 761,
                    "task_id": "TASK-A1924E",
                    "owner": "unassigned",
                    "task_status": "blocked",
                    "queue_state": "blocked",
                    "depends_on": ["TASK-38DCF3"],
                    "blocked_by": ["GH-760/TASK-38DCF3 WAF log-only Phase-1 evidence"],
                }
            ],
        }

        project_issue_queue.normalize_lanes(payload)

        self.assertEqual(payload["lanes"][0]["queue_state"], "blocked")
        self.assertEqual(
            payload["lanes"][0]["blocked_by"],
            ["GH-760/TASK-38DCF3 WAF log-only Phase-1 evidence"],
        )


class ProjectIssueQueueReconcileTest(unittest.TestCase):
    def write_task(self, directory: Path, name: str, body: str) -> Path:
        path = directory / name
        path.write_text(body)
        return path

    def test_reconcile_derives_ready_lane_after_completed_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self.write_task(
                root,
                "2026-06-04_gh-784-map__TASK-E8C28D.md",
                """---
task_id: TASK-E8C28D
title: GH-784 Map
status: open
owner: claude
project_id: amiga
depends_on: ["TASK-4B2049"]
skip_refinement: false
refined_by: null
accepted_by: null
---
""",
            )
            issue = _backlog.BacklogIssue(number=784, title="Map", labels=())
            previous = {
                "project_id": "amiga",
                "completed_recently": [
                    {"issue": 783, "task_id": "TASK-4B2049", "owner": "claude", "status": "done"}
                ],
                "lanes": [],
            }

            with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                with patch.object(project_issue_queue, "all_task_files", return_value=[task]):
                    with patch.object(project_issue_queue, "queue_exists", return_value=True):
                        with patch.object(project_issue_queue, "load_queue", return_value=previous):
                            result = project_issue_queue.reconcile_queue("amiga")

        lane = result["projection"]["lanes"][0]
        self.assertEqual(lane["queue_state"], "ready")
        self.assertEqual(lane["blocked_by"], [])
        self.assertTrue(lane["needs_refinement"])

    def test_reconcile_ignores_projectless_and_foreign_project_mirrors(self) -> None:
        # #given
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projectless = self.write_task(
                root,
                "2026-07-10_gh-901-projectless__TASK-NULL01.md",
                """---
task_id: TASK-NULL01
title: GH-901 Projectless
status: open
owner: unassigned
project_id: null
---
""",
            )
            foreign = self.write_task(
                root,
                "2026-07-10_gh-901-amiga__TASK-AMIGA1.md",
                """---
task_id: TASK-AMIGA1
title: GH-901 Amiga
status: open
owner: unassigned
project_id: amiga
---
""",
            )
            nuvyr = self.write_task(
                root,
                "2026-07-10_gh-901-nuvyr__TASK-NUVYR1.md",
                """---
task_id: TASK-NUVYR1
title: GH-901 Nuvyr
status: open
owner: unassigned
project_id: nuvyr
skip_refinement: true
---
""",
            )
            issue = _backlog.BacklogIssue(number=901, title="Nuvyr task", labels=())

            # #when
            with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                with patch.object(
                    project_issue_queue,
                    "all_task_files",
                    return_value=[projectless, foreign, nuvyr],
                ):
                    with patch.object(project_issue_queue, "queue_exists", return_value=False):
                        result = project_issue_queue.reconcile_queue("nuvyr")

        # #then
        self.assertEqual(result["duplicate_mirrors"], [])
        self.assertEqual(result["projection"]["lanes"][0]["task_id"], "TASK-NUVYR1")

    def test_validate_rejects_task_from_another_project(self) -> None:
        # #given
        payload = {
            "project_id": "nuvyr",
            "lanes": [
                {
                    "order": 1,
                    "issue": 72,
                    "task_id": "TASK-A",
                    "owner": "unassigned",
                    "task_status": "open",
                    "queue_state": "ready",
                    "tier": None,
                    "depends_on": [],
                }
            ],
        }
        task_body = """---
task_id: TASK-A
title: GH-72 Foreign task
project_id: amiga
owner: unassigned
status: open
depends_on: []
---
"""

        class FakeTaskPath:
            name = "2026-07-10_gh-72-foreign__TASK-A.md"

            def read_text(self) -> str:
                return task_body

        # #when
        with patch.object(project_issue_queue, "find_task_by_id", return_value=FakeTaskPath()):
            errors, _ = project_issue_queue.validate_queue("nuvyr", payload)

        # #then
        self.assertIn(
            "lane 1 project mismatch for TASK-A: queue 'nuvyr' vs task 'amiga'",
            errors,
        )

    def test_reconcile_reports_missing_task_mirror_without_writing_empty(self) -> None:
        issue = _backlog.BacklogIssue(number=900, title="Unmirrored issue", labels=())

        with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
            with patch.object(project_issue_queue, "all_task_files", return_value=[]):
                with patch.object(project_issue_queue, "queue_exists", return_value=False):
                    result = project_issue_queue.reconcile_queue("amiga")

        self.assertFalse(result["ok"])
        self.assertEqual(result["needs_materialization"], [{"issue": 900, "title": "Unmirrored issue"}])
        self.assertEqual(result["projection"]["lanes"], [])

    def test_reconcile_ignores_done_duplicate_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = self.write_task(
                root,
                "2026-06-04_gh-795-canonical__TASK-BAFE85.md",
                """---
task_id: TASK-BAFE85
title: GH-795 Canonical
status: open
owner: unassigned
project_id: amiga
depends_on: []
skip_refinement: false
refined_by: null
accepted_by: null
---
""",
            )
            duplicate = self.write_task(
                root,
                "2026-06-04_gh-795-placeholder__TASK-300181.md",
                """---
task_id: TASK-300181
title: GH-795 Placeholder
status: done
owner: unassigned
project_id: amiga
depends_on: []
skip_refinement: false
refined_by: null
accepted_by: null
---
""",
            )
            issue = _backlog.BacklogIssue(number=795, title="Inbox source fidelity", labels=())

            with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                with patch.object(project_issue_queue, "all_task_files", return_value=[canonical, duplicate]):
                    with patch.object(project_issue_queue, "queue_exists", return_value=False):
                        result = project_issue_queue.reconcile_queue("amiga")

        self.assertEqual(result["duplicate_mirrors"], [])
        self.assertEqual(result["projection"]["lanes"][0]["task_id"], "TASK-BAFE85")

    def test_reconcile_treats_done_only_mirror_as_unmaterialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done_mirror = self.write_task(
                root,
                "2026-06-04_gh-795-done__TASK-BAFE85.md",
                """---
task_id: TASK-BAFE85
title: GH-795 Done
status: done
owner: unassigned
project_id: amiga
depends_on: []
skip_refinement: false
refined_by: null
accepted_by: null
---
""",
            )
            issue = _backlog.BacklogIssue(number=795, title="Inbox source fidelity", labels=())

            with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                with patch.object(project_issue_queue, "all_task_files", return_value=[done_mirror]):
                    with patch.object(project_issue_queue, "queue_exists", return_value=False):
                        result = project_issue_queue.reconcile_queue("amiga")

        self.assertFalse(result["ok"])
        self.assertEqual(result["needs_materialization"], [{"issue": 795, "title": "Inbox source fidelity"}])
        self.assertEqual(result["projection"]["lanes"], [])

    def test_reconcile_fails_when_issue_has_multiple_active_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.write_task(
                root,
                "2026-06-04_gh-795-first__TASK-BAFE85.md",
                """---
task_id: TASK-BAFE85
title: GH-795 First
status: open
owner: unassigned
project_id: amiga
depends_on: []
skip_refinement: false
refined_by: null
accepted_by: null
---
""",
            )
            second = self.write_task(
                root,
                "2026-06-04_gh-795-second__TASK-300181.md",
                """---
task_id: TASK-300181
title: GH-795 Second
status: open
owner: unassigned
project_id: amiga
depends_on: []
skip_refinement: false
refined_by: null
accepted_by: null
---
""",
            )
            issue = _backlog.BacklogIssue(number=795, title="Inbox source fidelity", labels=())

            with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                with patch.object(project_issue_queue, "all_task_files", return_value=[first, second]):
                    with patch.object(project_issue_queue, "queue_exists", return_value=False):
                        result = project_issue_queue.reconcile_queue("amiga")

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["duplicate_mirrors"],
            [{"issue": 795, "tasks": ["TASK-BAFE85", "TASK-300181"]}],
        )

    def test_reconcile_fails_closed_when_backlog_unknown(self) -> None:
        with patch.object(
            _backlog,
            "eligible_open_issues",
            side_effect=_backlog.BacklogUnavailable("gh down"),
        ):
            result = project_issue_queue.reconcile_queue("amiga")

        self.assertFalse(result["ok"])
        self.assertEqual(result["backlog"], "unknown")
        self.assertEqual(result["reason"], "gh down")

    def test_validate_reports_eligible_lanes_with_no_ready_lane(self) -> None:
        payload = {
            "project_id": "amiga",
            "lanes": [
                {
                    "order": 1,
                    "issue": 784,
                    "task_id": "TASK-E8C28D",
                    "task_status": "open",
                    "queue_state": "queued",
                    "needs_refinement": True,
                }
            ],
        }

        errors, warnings = project_issue_queue.no_ready_lane_errors("amiga", payload)

        self.assertEqual(
            errors,
            ["queue has eligible lanes but no ready lane for amiga: GH-784:needs_refinement"],
        )
        self.assertEqual(warnings, [])

    def test_show_queue_surfaces_refinement_as_next_action(self) -> None:
        payload = {
            "project_id": "amiga",
            "lanes": [
                {
                    "order": 1,
                    "issue": 784,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "ready",
                    "needs_refinement": True,
                }
            ],
        }

        rendered = project_issue_queue.show_queue(payload)

        self.assertIn("next=refine", rendered)

    def test_render_markdown_surfaces_refinement_as_next_action(self) -> None:
        payload = {
            "project_id": "amiga",
            "lanes": [
                {
                    "order": 1,
                    "issue": 784,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "ready",
                    "tier": None,
                    "depends_on": [],
                    "notes": "derived by reconcile",
                    "needs_refinement": True,
                }
            ],
        }

        rendered = project_issue_queue.render_markdown(payload)

        self.assertIn("- Next ready lane: `GH-784` / `TASK-E8C28D` / `claude` (refine)", rendered)

    def test_render_markdown_uses_registered_project_name_and_clean_empty_sources(self) -> None:
        payload = {
            "project_id": "nuvyr",
            "last_updated_utc": "2026-07-10T05:32:42+00:00",
            "source_issue": None,
            "source_task": None,
            "lanes": [],
        }

        with patch.object(
            project_issue_queue,
            "get_project",
            return_value={"id": "nuvyr", "display_name": "Nuvyr"},
        ):
            rendered = project_issue_queue.render_markdown(payload)

        self.assertIn("# Nuvyr Ordered Issue Queue", rendered)
        self.assertIn("- Source issue: none", rendered)
        self.assertIn("- Source task: none", rendered)
        self.assertNotIn("Amiga", rendered)
        self.assertNotIn("GH-None", rendered)


if __name__ == "__main__":
    unittest.main()
