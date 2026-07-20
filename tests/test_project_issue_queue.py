from __future__ import annotations

import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _backlog
import project_issue_queue
import task_contract


class TaskMirror:
    def __init__(self, *, project_id: str | None, status: str) -> None:
        rendered_project_id = "null" if project_id is None else project_id
        self._content = (
            "---\n"
            "task_id: TASK-E8C28D\n"
            "title: Queue mirror\n"
            f"project_id: {rendered_project_id}\n"
            f"status: {status}\n"
            "owner: claude\n"
            "---\n"
        )

    def read_text(self) -> str:
        return self._content


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

        with patch.object(
            project_issue_queue,
            "find_task_by_id",
            return_value=TaskMirror(project_id="amiga", status="open"),
        ):
            with patch.object(task_contract, "get_project", return_value={"id": "amiga"}):
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

        with patch.object(
            project_issue_queue,
            "find_task_by_id",
            return_value=TaskMirror(project_id="amiga", status="open"),
        ):
            with patch.object(task_contract, "get_project", return_value={"id": "amiga"}):
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

        with patch.object(
            project_issue_queue,
            "find_task_by_id",
            return_value=TaskMirror(project_id="amiga", status="blocked"),
        ):
            with patch.object(task_contract, "get_project", return_value={"id": "amiga"}):
                project_issue_queue.normalize_lanes(payload)

        self.assertEqual(payload["lanes"][0]["queue_state"], "blocked")
        self.assertEqual(
            payload["lanes"][0]["blocked_by"],
            ["GH-760/TASK-38DCF3 WAF log-only Phase-1 evidence"],
        )

    def test_normalize_requires_exact_queue_project_before_policy_evaluation(self) -> None:
        for queue_project_id in (None, "nuvyr"):
            with self.subTest(queue_project_id=queue_project_id):
                payload = {
                    "project_id": queue_project_id,
                    "lanes": [
                        {
                            "order": 1,
                            "issue": 75,
                            "task_id": "TASK-E8C28D",
                            "owner": "claude",
                            "task_status": "open",
                            "queue_state": "ready",
                            "depends_on": [],
                            "blocked_by": [],
                        }
                    ],
                }

                with patch.object(project_issue_queue, "find_task_by_id") as find_task:
                    with patch.object(
                        project_issue_queue,
                        "validate_direct_app_policy",
                    ) as validate_policy:
                        project_issue_queue.normalize_lanes(
                            payload,
                            expected_project_id="amiga",
                        )

                lane = payload["lanes"][0]
                self.assertEqual(lane["queue_state"], "blocked")
                self.assertTrue(
                    any(
                        "policy evidence unavailable: queue project_id" in blocker
                        for blocker in lane["blocked_by"]
                    )
                )
                find_task.assert_not_called()
                validate_policy.assert_not_called()

    def test_normalize_foreign_projectless_and_missing_mirrors_fail_closed(self) -> None:
        cases = (
            ("foreign", TaskMirror(project_id="nuvyr", status="open"), "project mismatch"),
            ("projectless", TaskMirror(project_id=None, status="open"), "project mismatch"),
            ("missing", None, "task mirror not found"),
        )
        for label, mirror, expected_error in cases:
            with self.subTest(mirror=label):
                payload = {
                    "project_id": "amiga",
                    "lanes": [
                        {
                            "order": 1,
                            "issue": 75,
                            "task_id": "TASK-E8C28D",
                            "owner": "claude",
                            "task_status": "open",
                            "queue_state": "ready",
                            "depends_on": [],
                            "blocked_by": [],
                        }
                    ],
                }

                with patch.object(
                    project_issue_queue,
                    "find_task_by_id",
                    return_value=mirror,
                ):
                    with patch.object(
                        project_issue_queue,
                        "validate_direct_app_policy",
                    ) as validate_policy:
                        project_issue_queue.normalize_lanes(payload)

                lane = payload["lanes"][0]
                self.assertEqual(lane["queue_state"], "blocked")
                self.assertTrue(
                    any(expected_error in blocker for blocker in lane["blocked_by"])
                )
                validate_policy.assert_not_called()

    def test_mark_transition_persists_stale_done_mirror_as_blocked(self) -> None:
        payload = {
            "project_id": "amiga",
            "completed_recently": [
                {
                    "issue": 75,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "status": "done",
                }
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 75,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "task_status": "blocked",
                    "queue_state": "blocked",
                    "depends_on": [],
                    "blocked_by": [],
                }
            ],
        }

        with patch.object(project_issue_queue, "queue_exists", return_value=True):
            with patch.object(project_issue_queue, "load_queue", return_value=payload):
                with patch.object(
                    project_issue_queue,
                    "find_task_by_id",
                    return_value=TaskMirror(project_id="amiga", status="done"),
                ):
                    with patch.object(
                        project_issue_queue,
                        "validate_direct_app_policy",
                    ) as validate_policy:
                        with patch.object(project_issue_queue, "sync_markdown") as persist:
                            result = project_issue_queue.mark_lane_transition(
                                "amiga",
                                "TASK-E8C28D",
                                owner="codex",
                                task_status="open",
                            )

        lane = payload["lanes"][0]
        self.assertEqual(result, {"updated": True, "archived": False})
        self.assertEqual(lane["queue_state"], "blocked")
        self.assertTrue(
            any(
                "task mirror status mismatch for TASK-E8C28D: queue 'open', task 'done'"
                in blocker
                for blocker in lane["blocked_by"]
            )
        )
        validate_policy.assert_not_called()
        persist.assert_called_once_with("amiga", payload)

    def test_mark_transition_refuses_invalid_queue_project_before_any_mutation(self) -> None:
        missing = object()
        for label, queue_project_id in (
            ("missing", missing),
            ("null", None),
            ("empty", ""),
            ("foreign", "nuvyr"),
        ):
            for task_status in ("open", "done"):
                with self.subTest(queue_project=label, task_status=task_status):
                    payload = {
                        "completed_recently": [],
                        "lanes": [
                            {
                                "order": 1,
                                "issue": 75,
                                "task_id": "TASK-E8C28D",
                                "owner": "claude",
                                "task_status": "review",
                                "queue_state": "review",
                                "depends_on": [],
                                "blocked_by": [],
                            }
                        ],
                    }
                    if queue_project_id is not missing:
                        payload["project_id"] = queue_project_id
                    before = copy.deepcopy(payload)

                    with patch.object(project_issue_queue, "queue_exists", return_value=True):
                        with patch.object(project_issue_queue, "load_queue", return_value=payload):
                            with patch.object(project_issue_queue, "normalize_lanes") as normalize:
                                with patch.object(
                                    project_issue_queue,
                                    "archive_complete_queue",
                                ) as archive:
                                    with patch.object(project_issue_queue, "sync_markdown") as persist:
                                        with self.assertRaisesRegex(
                                            ValueError,
                                            "queue project_id mismatch.*before mutation",
                                        ):
                                            project_issue_queue.mark_lane_transition(
                                                "amiga",
                                                "TASK-E8C28D",
                                                owner="codex",
                                                task_status=task_status,
                                            )

                    self.assertEqual(payload, before)
                    normalize.assert_not_called()
                    archive.assert_not_called()
                    persist.assert_not_called()

    def test_mark_transition_exact_project_done_still_archives(self) -> None:
        payload = {
            "project_id": "amiga",
            "completed_recently": [],
            "lanes": [
                {
                    "order": 1,
                    "issue": 75,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "task_status": "review",
                    "queue_state": "review",
                    "depends_on": [],
                    "blocked_by": [],
                }
            ],
        }
        archived_paths = (Path("/tmp/issue-queue.json"), Path("/tmp/issue-queue.md"))

        with patch.object(project_issue_queue, "queue_exists", return_value=True):
            with patch.object(project_issue_queue, "load_queue", return_value=payload):
                with patch.object(
                    project_issue_queue,
                    "archive_complete_queue",
                    return_value=archived_paths,
                ) as archive:
                    with patch.object(project_issue_queue, "sync_markdown") as persist:
                        result = project_issue_queue.mark_lane_transition(
                            "amiga",
                            "TASK-E8C28D",
                            owner="codex",
                            task_status="done",
                        )

        self.assertTrue(result["updated"])
        self.assertTrue(result["archived"])
        self.assertEqual(payload["lanes"], [])
        self.assertEqual(payload["completed_recently"][-1]["task_id"], "TASK-E8C28D")
        archive.assert_called_once()
        persist.assert_called_once_with("amiga", payload)

    def test_direct_app_policy_blocker_never_clears_as_completed_queue_order(self) -> None:
        payload = {
            "project_id": "amiga",
            "completed_recently": [
                {
                    "issue": 75,
                    "task_id": "TASK-OTHER",
                    "owner": "claude",
                    "status": "done",
                }
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 76,
                    "task_id": "TASK-E8C28D",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "blocked",
                    "depends_on": [],
                    "blocked_by": [],
                }
            ],
        }
        policy_error = "related path 'design/GH-75 queue order' is forbidden"

        with patch.object(
            project_issue_queue,
            "find_task_by_id",
            return_value=TaskMirror(project_id="amiga", status="open"),
        ):
            with patch.object(
                project_issue_queue,
                "validate_direct_app_policy",
                return_value=([policy_error], {}),
            ):
                project_issue_queue.normalize_lanes(payload)

        lane = payload["lanes"][0]
        self.assertEqual(lane["queue_state"], "blocked")
        self.assertEqual(
            lane["blocked_by"],
            [f"{project_issue_queue.DIRECT_APP_BLOCKER_PREFIX}{policy_error}"],
        )


class ProjectIssueQueueArchiveCompleteTest(unittest.TestCase):
    def archive_args(self) -> SimpleNamespace:
        return SimpleNamespace(
            command="archive-complete",
            project="amiga",
            skip_backlog_check=True,
        )

    def test_archive_complete_requires_exact_embedded_project_before_mutation(self) -> None:
        missing = object()
        for label, embedded_project in (
            ("missing", missing),
            ("null", None),
            ("empty", ""),
            ("foreign", "nuvyr"),
        ):
            with self.subTest(project_id=label):
                payload = {"lanes": []}
                if embedded_project is not missing:
                    payload["project_id"] = embedded_project
                before = copy.deepcopy(payload)
                stderr = io.StringIO()

                with patch.object(project_issue_queue, "parse_args", return_value=self.archive_args()):
                    with patch.object(project_issue_queue, "load_queue", return_value=payload):
                        with patch.object(project_issue_queue, "archive_complete_queue") as archive:
                            with patch.object(project_issue_queue, "sync_markdown") as persist:
                                with patch("sys.stderr", stderr):
                                    result = project_issue_queue.main()

                self.assertEqual(result, 1)
                self.assertEqual(payload, before)
                self.assertIn("expected 'amiga'", stderr.getvalue())
                self.assertIn(f"found {payload.get('project_id')!r}", stderr.getvalue())
                self.assertIn("before mutation", stderr.getvalue())
                archive.assert_not_called()
                persist.assert_not_called()

    def test_archive_complete_exact_empty_queue_archives_and_syncs_once(self) -> None:
        payload = {"project_id": "amiga", "lanes": []}
        archived_paths = (Path("/tmp/issue-queue.json"), Path("/tmp/issue-queue.md"))

        with patch.object(project_issue_queue, "parse_args", return_value=self.archive_args()):
            with patch.object(project_issue_queue, "load_queue", return_value=payload):
                with patch.object(
                    project_issue_queue,
                    "archive_complete_queue",
                    return_value=archived_paths,
                ) as archive:
                    with patch.object(project_issue_queue, "sync_markdown") as persist:
                        result = project_issue_queue.main()

        self.assertEqual(result, 0)
        archive.assert_called_once()
        persist.assert_called_once_with("amiga", payload)


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

    def test_reconcile_dependency_requires_exact_project_done_snapshot(self) -> None:
        cases = (
            ("missing", None, False),
            ("projectless", ("null", "done"), False),
            ("empty-project", ('""', "done"), False),
            ("foreign", ("nuvyr", "done"), False),
            ("missing-status", ("amiga", None), False),
            ("non-done", ("amiga", "review"), False),
            ("exact-done", ("amiga", "done"), True),
        )
        for label, dependency_state, expected_ready in cases:
            with self.subTest(dependency=label):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    active = self.write_task(
                        root,
                        "2026-07-20_gh-784-active__TASK-ACTIVE.md",
                        """---
task_id: TASK-ACTIVE
title: GH-784 Active
status: open
owner: claude
project_id: amiga
depends_on: ["TASK-DEP"]
skip_refinement: true
---
""",
                    )
                    dependency = None
                    if dependency_state is not None:
                        dependency_project, dependency_status = dependency_state
                        status_line = (
                            f"status: {dependency_status}\n"
                            if dependency_status is not None
                            else ""
                        )
                        dependency = self.write_task(
                            root,
                            f"{label}-dependency.md",
                            (
                                "---\n"
                                "task_id: TASK-DEP\n"
                                "title: Dependency\n"
                                f"project_id: {dependency_project}\n"
                                f"{status_line}"
                                "owner: claude\n"
                                "---\n"
                            ),
                        )
                    issue = _backlog.BacklogIssue(number=784, title="Active", labels=())
                    task_files = [active] + ([dependency] if dependency is not None else [])

                    with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                        with patch.object(project_issue_queue, "all_task_files", return_value=task_files):
                            with patch.object(project_issue_queue, "queue_exists", return_value=False):
                                with patch.object(
                                    project_issue_queue,
                                    "find_task_by_id",
                                    return_value=dependency,
                                ) as global_lookup:
                                    with patch.object(
                                        task_contract,
                                        "get_project",
                                        return_value={"id": "amiga"},
                                    ):
                                        result = project_issue_queue.reconcile_queue("amiga")

                    lane = result["projection"]["lanes"][0]
                    self.assertEqual(lane["queue_state"] == "ready", expected_ready)
                    self.assertEqual(lane["blocked_by"], [] if expected_ready else ["TASK-DEP"])
                    global_lookup.assert_not_called()

    def test_reconcile_persisted_completion_requires_exact_queue_and_done_status(self) -> None:
        cases = (
            ("foreign-done", "nuvyr", "done"),
            ("exact-missing-status", "amiga", None),
            ("exact-non-done", "amiga", "review"),
        )
        for label, queue_project_id, completed_status in cases:
            with self.subTest(completion=label):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    active = self.write_task(
                        root,
                        "2026-07-20_gh-784-active__TASK-ACTIVE.md",
                        """---
task_id: TASK-ACTIVE
title: GH-784 Active
status: open
owner: claude
project_id: amiga
depends_on: ["TASK-DEP"]
skip_refinement: true
---
""",
                    )
                    issue = _backlog.BacklogIssue(number=784, title="Active", labels=())
                    completion = {
                        "issue": 783,
                        "task_id": "TASK-DEP",
                        "owner": "claude",
                    }
                    if completed_status is not None:
                        completion["status"] = completed_status
                    previous = {
                        "project_id": queue_project_id,
                        "completed_recently": [completion],
                        "lanes": [],
                    }

                    with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                        with patch.object(project_issue_queue, "all_task_files", return_value=[active]):
                            with patch.object(project_issue_queue, "queue_exists", return_value=True):
                                with patch.object(project_issue_queue, "load_queue", return_value=previous):
                                    with patch.object(
                                        project_issue_queue,
                                        "find_task_by_id",
                                        return_value=TaskMirror(project_id="nuvyr", status="done"),
                                    ) as global_lookup:
                                        with patch.object(
                                            task_contract,
                                            "get_project",
                                            return_value={"id": "amiga"},
                                        ):
                                            result = project_issue_queue.reconcile_queue("amiga")

                    lane = result["projection"]["lanes"][0]
                    self.assertEqual(lane["queue_state"], "blocked")
                    self.assertEqual(lane["blocked_by"], ["TASK-DEP"])
                    global_lookup.assert_not_called()

    def test_task_snapshot_requires_every_exact_project_duplicate_to_be_done(self) -> None:
        for status_label, status_line in (
            ("open", "status: open\n"),
            ("blocked", "status: blocked\n"),
            ("review", "status: review\n"),
            ("missing", ""),
            ("invalid", "status: unexpected\n"),
        ):
            with self.subTest(second_status=status_label):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    done = self.write_task(
                        root,
                        "2026-07-20_gh-783-done__TASK-DEP.md",
                        "---\n"
                        "task_id: TASK-DEP\n"
                        "title: GH-783 Done mirror\n"
                        "project_id: amiga\n"
                        "status: done\n"
                        "---\n",
                    )
                    other = self.write_task(
                        root,
                        "2026-07-20_gh-783-other__TASK-DEP.md",
                        "---\n"
                        "task_id: TASK-DEP\n"
                        "title: GH-783 Other mirror\n"
                        "project_id: amiga\n"
                        f"{status_line}"
                        "---\n",
                    )

                    with patch.object(project_issue_queue, "all_task_files", return_value=[done, other]):
                        _, completed = project_issue_queue.project_task_snapshot("amiga")

                self.assertNotIn("TASK-DEP", completed)

    def test_task_snapshot_accepts_all_done_exact_mirrors_and_excludes_foreign_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_files = []
            for label, project_id, status in (
                ("done-a", "amiga", "done"),
                ("done-b", "amiga", "done"),
                ("foreign", "nuvyr", "open"),
                ("projectless", "null", "blocked"),
            ):
                task_files.append(
                    self.write_task(
                        root,
                        f"2026-07-20_gh-783-{label}__TASK-DEP.md",
                        "---\n"
                        "task_id: TASK-DEP\n"
                        f"title: GH-783 {label}\n"
                        f"project_id: {project_id}\n"
                        f"status: {status}\n"
                        "---\n",
                    )
                )

            with patch.object(project_issue_queue, "all_task_files", return_value=task_files):
                _, completed = project_issue_queue.project_task_snapshot("amiga")

        self.assertIn("TASK-DEP", completed)

    def test_reconcile_blocks_and_reports_direct_app_policy_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self.write_task(
                root,
                "2026-07-20_gh-75-design__TASK-DIRECT.md",
                """---
task_id: TASK-DIRECT
title: GH-75 Design sandbox
status: open
owner: unassigned
project_id: amiga
lane_type: design-spec
depends_on: []
skip_refinement: true
---
""",
            )
            issue = _backlog.BacklogIssue(number=75, title="Design sandbox", labels=())
            project = {"id": "amiga", "ui_ux": {"direct_app_only": True}}

            with patch.object(_backlog, "eligible_open_issues", return_value=[issue]):
                with patch.object(project_issue_queue, "all_task_files", return_value=[task]):
                    with patch.object(project_issue_queue, "queue_exists", return_value=False):
                        with patch.object(task_contract, "get_project", return_value=project):
                            result = project_issue_queue.reconcile_queue("amiga")

            lane = result["projection"]["lanes"][0]
            self.assertFalse(result["ok"])
            self.assertEqual(lane["queue_state"], "blocked")
            self.assertTrue(result["invalid_lanes"])
            self.assertTrue(
                any("ui_ux.direct_app_only" in blocker for blocker in lane["blocked_by"])
            )

            with patch.object(project_issue_queue, "find_task_by_id", return_value=task):
                with patch.object(task_contract, "get_project", return_value=project):
                    errors, _ = project_issue_queue.validate_queue(
                        "amiga",
                        result["projection"],
                    )
            self.assertTrue(
                any("lane 1 task TASK-DIRECT" in error and "`lane_type`" in error for error in errors)
            )

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
