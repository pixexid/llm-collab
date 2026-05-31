from __future__ import annotations

import sys
import unittest
import json
import tempfile
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import project_design_queue


class ProjectDesignQueueTest(unittest.TestCase):
    def test_normalize_lanes_keeps_active_lane_authoritative(self) -> None:
        payload = {
            "lanes": [
                {"order": 2, "task_id": "TASK-B", "queue_state": "ready"},
                {"order": 1, "task_id": "TASK-A", "queue_state": "active"},
            ]
        }

        project_design_queue.normalize_lanes(payload)

        self.assertEqual([lane["order"] for lane in payload["lanes"]], [1, 2])
        self.assertEqual(payload["lanes"][0]["queue_state"], "active")
        self.assertEqual(payload["lanes"][1]["queue_state"], "queued")

    def test_mark_lane_done_removes_design_lane_and_promotes_next_ready(self) -> None:
        payload = {
            "schema_version": 2,
            "artifact_type": "ordered_design_queue",
            "project_id": "amiga",
            "completed_recently": [],
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "First design lane",
                    "owner": "claude",
                    "task_status": "review",
                    "queue_state": "review",
                    "depends_on": [],
                    "blocked_by": [],
                },
                {
                    "order": 2,
                    "issue": 226,
                    "task_id": "TASK-B",
                    "title": "Second design lane",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "queued",
                    "depends_on": [],
                    "blocked_by": [],
                },
            ],
        }
        synced: list[dict] = []

        def fake_sync_markdown(project_id: str, updated: dict, *, mirror_issue_queue: bool = True) -> Path:
            synced.append(updated)
            return Path("/tmp/design-queue.md")

        with patch.object(project_design_queue, "queue_exists", return_value=True):
            with patch.object(project_design_queue, "load_queue", return_value=payload):
                with patch.object(project_design_queue, "sync_markdown", side_effect=fake_sync_markdown):
                    result = project_design_queue.mark_lane_transition(
                        "amiga",
                        "TASK-A",
                        owner="claude",
                        task_status="done",
                    )

        self.assertEqual(result, {"updated": True, "archived": False})
        self.assertEqual(len(synced), 1)
        updated = synced[0]
        self.assertEqual([lane["task_id"] for lane in updated["lanes"]], ["TASK-B"])
        self.assertEqual(updated["lanes"][0]["order"], 1)
        self.assertEqual(updated["lanes"][0]["queue_state"], "ready")
        self.assertEqual(updated["completed_recently"][-1]["task_id"], "TASK-A")

    def test_mark_lane_done_syncs_empty_issue_mirror_for_final_lane(self) -> None:
        payload = {
            "schema_version": 2,
            "artifact_type": "ordered_design_queue",
            "project_id": "amiga",
            "completed_recently": [],
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Final design lane",
                    "owner": "claude",
                    "task_status": "review",
                    "queue_state": "review",
                    "depends_on": [],
                    "blocked_by": [],
                }
            ],
        }
        synced: list[dict] = []
        mirror_flags: list[bool] = []

        def fake_sync_markdown(project_id: str, updated: dict, *, mirror_issue_queue: bool = True) -> Path:
            synced.append(updated)
            mirror_flags.append(mirror_issue_queue)
            return Path("/tmp/design-queue.md")

        with patch.object(project_design_queue, "queue_exists", return_value=True):
            with patch.object(project_design_queue, "load_queue", return_value=payload):
                with patch.object(
                    project_design_queue,
                    "archive_complete_queue",
                    return_value=(Path("/tmp/design-queue.json"), Path("/tmp/design-queue.md")),
                ):
                    with patch.object(project_design_queue, "sync_markdown", side_effect=fake_sync_markdown):
                        result = project_design_queue.mark_lane_transition(
                            "amiga",
                            "TASK-A",
                            owner="claude",
                            task_status="done",
                        )

        self.assertEqual(result["updated"], True)
        self.assertEqual(result["archived"], True)
        self.assertEqual(len(synced), 1)
        self.assertEqual(synced[0]["lanes"], [])
        self.assertEqual(mirror_flags, [True])

    def test_issue_queue_mirror_uses_active_design_lanes_only(self) -> None:
        design_payload = {
            "completed_recently": [
                {"issue": 221, "task_id": "TASK-DONE", "owner": "claude", "status": "done"}
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Design lane",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "ready",
                    "lane_type": "design-spec",
                    "depends_on": ["TASK-DONE"],
                    "blocked_by": [],
                    "notes": "Design first.",
                }
            ],
        }
        captured: dict[str, object] = {}

        def fake_issue_sync(project_id: str, issue_payload: dict) -> Path:
            captured["project_id"] = project_id
            captured["payload"] = issue_payload
            return Path("/tmp/issue-queue.md")

        with patch.object(project_design_queue, "load_issue_queue", return_value=None):
            with patch.object(project_design_queue.issue_queue, "sync_markdown", side_effect=fake_issue_sync):
                path = project_design_queue.sync_issue_queue_mirror("amiga", design_payload)

        self.assertEqual(path, Path("/tmp/issue-queue.md"))
        self.assertEqual(captured["project_id"], "amiga")
        issue_payload = captured["payload"]
        self.assertEqual(issue_payload["artifact_type"], "ordered_issue_queue")
        self.assertEqual(len(issue_payload["lanes"]), 1)
        self.assertEqual(issue_payload["lanes"][0]["task_id"], "TASK-A")
        self.assertEqual(issue_payload["lanes"][0]["queue_state"], "ready")
        self.assertEqual(issue_payload["lanes"][0]["lane_type"], "design-spec")

    def test_issue_queue_mirror_clears_stale_lanes_when_design_queue_is_empty(self) -> None:
        design_payload = {
            "completed_recently": [
                {"issue": 223, "task_id": "TASK-A", "owner": "claude", "status": "done"}
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Finished design lane",
                    "owner": "claude",
                    "task_status": "done",
                    "queue_state": "done",
                    "depends_on": [],
                    "blocked_by": [],
                    "notes": "Done.",
                }
            ],
        }
        existing_issue_payload = {
            "schema_version": 1,
            "artifact_type": "ordered_issue_queue",
            "project_id": "amiga",
            "completed_recently": [],
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Stale lane",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ],
        }
        captured: dict[str, object] = {}

        def fake_issue_sync(project_id: str, issue_payload: dict) -> Path:
            captured["project_id"] = project_id
            captured["payload"] = issue_payload
            return Path("/tmp/issue-queue.md")

        with patch.object(project_design_queue, "load_issue_queue", return_value=existing_issue_payload):
            with patch.object(project_design_queue.issue_queue, "sync_markdown", side_effect=fake_issue_sync):
                path = project_design_queue.sync_issue_queue_mirror("amiga", design_payload)

        self.assertEqual(path, Path("/tmp/issue-queue.md"))
        issue_payload = captured["payload"]
        self.assertEqual(issue_payload["lanes"], [])
        self.assertEqual(issue_payload["completed_recently"][0]["task_id"], "TASK-A")

    def test_issue_queue_mirror_preserves_existing_completed_owner(self) -> None:
        design_payload = {
            "completed_recently": [
                {"issue": 357, "task_id": "TASK-CODEX-DONE", "status": "done"}
            ],
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Design lane",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "ready",
                    "depends_on": [],
                    "blocked_by": [],
                    "notes": "Design first.",
                }
            ],
        }
        existing_issue_payload = {
            "completed_recently": [
                {"issue": 357, "task_id": "TASK-CODEX-DONE", "owner": "codex", "status": "done"}
            ]
        }
        captured: dict[str, object] = {}

        def fake_issue_sync(project_id: str, issue_payload: dict) -> Path:
            captured["payload"] = issue_payload
            return Path("/tmp/issue-queue.md")

        with patch.object(project_design_queue, "load_issue_queue", return_value=existing_issue_payload):
            with patch.object(project_design_queue.issue_queue, "sync_markdown", side_effect=fake_issue_sync):
                project_design_queue.sync_issue_queue_mirror("amiga", design_payload)

        issue_payload = captured["payload"]
        self.assertEqual(issue_payload["completed_recently"][0]["owner"], "codex")

    def test_validate_can_skip_stale_issue_queue_mirror_for_repair_sync(self) -> None:
        payload = {
            "artifact_type": "ordered_design_queue",
            "project_id": "amiga",
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "ready",
                    "lane_type": "design-spec",
                    "depends_on": [],
                }
            ],
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "owner: claude",
                "status: open",
                "depends_on: []",
                "ui_ux_lane: true",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "get_project", return_value={"id": "amiga"}):
            with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
                with patch.object(project_design_queue, "load_issue_queue", return_value={"lanes": []}):
                    errors_with_mirror, _ = project_design_queue.validate_queue(
                        "amiga",
                        payload,
                        check_github=False,
                        check_issue_mirror=True,
                    )
                    errors_without_mirror, _ = project_design_queue.validate_queue(
                        "amiga",
                        payload,
                        check_github=False,
                        check_issue_mirror=False,
                    )

        self.assertTrue(any("issue queue mirror" in error for error in errors_with_mirror))
        self.assertEqual(errors_without_mirror, [])

    def test_validate_materialized_dependency_artifacts_for_active_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            (worktree / "design/surfaces").mkdir(parents=True)
            (worktree / "design/surfaces/notifications.md").write_text("# Notifications\n")
            payload = {
                "artifact_type": "ordered_design_queue",
                "project_id": "amiga",
                "lanes": [
                    {
                        "order": 1,
                        "issue": 279,
                        "task_id": "TASK-A",
                        "owner": "claude",
                        "task_status": "in_progress",
                        "queue_state": "active",
                        "lane_type": "design-audit",
                        "depends_on": [],
                    }
                ],
            }
            task_body = "\n".join(
                [
                    "---",
                    "task_id: TASK-A",
                    "owner: claude",
                    "status: in_progress",
                    "depends_on: []",
                    "ui_ux_lane: true",
                    "dependency_materialization_gate: true",
                    "worktree: " + str(worktree),
                    'required_dependency_artifacts: ["design/surfaces/notifications.md"]',
                    "---",
                    "",
                ]
            )

            class FakeTaskPath:
                def read_text(self) -> str:
                    return task_body

            with patch.object(project_design_queue, "get_project", return_value={"id": "amiga"}):
                with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
                    errors, _ = project_design_queue.validate_queue(
                        "amiga",
                        payload,
                        check_github=False,
                        check_issue_mirror=False,
                    )

            self.assertEqual(errors, [])

    def test_validate_reports_missing_materialized_dependency_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            payload = {
                "artifact_type": "ordered_design_queue",
                "project_id": "amiga",
                "lanes": [
                    {
                        "order": 1,
                        "issue": 279,
                        "task_id": "TASK-A",
                        "owner": "claude",
                        "task_status": "in_progress",
                        "queue_state": "active",
                        "lane_type": "design-audit",
                        "depends_on": [],
                    }
                ],
            }
            task_body = "\n".join(
                [
                    "---",
                    "task_id: TASK-A",
                    "owner: claude",
                    "status: in_progress",
                    "depends_on: []",
                    "ui_ux_lane: true",
                    "dependency_materialization_gate: true",
                    "worktree: " + str(worktree),
                    'required_dependency_artifacts: ["design/surfaces/notifications.md"]',
                    "---",
                    "",
                ]
            )

            class FakeTaskPath:
                def read_text(self) -> str:
                    return task_body

            with patch.object(project_design_queue, "get_project", return_value={"id": "amiga"}):
                with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
                    errors, _ = project_design_queue.validate_queue(
                        "amiga",
                        payload,
                        check_github=False,
                        check_issue_mirror=False,
                    )

            self.assertTrue(any("missing dependency artifact" in error for error in errors))

    def test_ready_context_reports_bridge_metadata_for_ready_lane(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking confirmation design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": ["TASK-DONE"],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "related_chat: CHAT-TEST",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: \"[BRIDGE 12345678] GH-223 booking design\"",
                "claude_desktop_thread_title: Booking design",
                "claude_activation_message_path: /tmp/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            context = project_design_queue.ready_context("amiga", payload)

        self.assertTrue(context["ready"])
        self.assertTrue(context["bridge_metadata_complete"])
        self.assertEqual(context["task_id"], "TASK-A")
        self.assertEqual(context["bridge_thread_uuid"], "12345678-1234-1234-1234-123456789abc")
        self.assertEqual(context["bridge_visible_prefix"], "[BRIDGE 12345678] GH-223 booking design")

    def test_ready_context_tracks_active_lane_when_no_ready_lane(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking confirmation design",
                    "owner": "claude",
                    "queue_state": "active",
                    "depends_on": ["TASK-DONE"],
                },
                {
                    "order": 2,
                    "issue": 226,
                    "task_id": "TASK-B",
                    "title": "Payment design",
                    "owner": "claude",
                    "queue_state": "queued",
                    "depends_on": ["TASK-A"],
                },
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "related_chat: CHAT-TEST",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: \"[BRIDGE 12345678] GH-223 booking design\"",
                "claude_desktop_thread_title: Booking design",
                "claude_activation_message_path: /tmp/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            context = project_design_queue.ready_context("amiga", payload)

        self.assertTrue(context["ready"])
        self.assertEqual(context["queue_state"], "active")
        self.assertEqual(context["task_id"], "TASK-A")
        self.assertTrue(context["bridge_metadata_complete"])

    def test_ready_context_prefers_active_lane_over_ready_lane(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-READY",
                    "title": "Ready design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                },
                {
                    "order": 2,
                    "issue": 226,
                    "task_id": "TASK-ACTIVE",
                    "title": "Active design",
                    "owner": "claude",
                    "queue_state": "active",
                    "depends_on": [],
                },
            ]
        }

        def fake_find_task_by_id(task_id: str) -> object:
            task_body = "\n".join(
                [
                    "---",
                    f"task_id: {task_id}",
                    "related_chat: CHAT-TEST",
                    f"branch: codex/claude/{task_id.lower()}",
                    f"worktree: /tmp/{task_id.lower()}",
                    f"bridge_thread_uuid: {task_id.lower()}-1234-1234-1234-123456789abc",
                    f"bridge_visible_prefix: \"[BRIDGE {task_id}] GH-226 active design\"",
                    "claude_activation_message_path: /tmp/message.md",
                    "---",
                    "",
                ]
            )

            class FakeTaskPath:
                def __str__(self) -> str:
                    return f"/tmp/{task_id}.md"

                def read_text(self) -> str:
                    return task_body

            return FakeTaskPath()

        with patch.object(project_design_queue, "find_task_by_id", side_effect=fake_find_task_by_id):
            context = project_design_queue.ready_context("amiga", payload)

        self.assertTrue(context["ready"])
        self.assertEqual(context["queue_state"], "active")
        self.assertEqual(context["task_id"], "TASK-ACTIVE")

    def test_ready_context_prefers_review_lane_over_ready_lane(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-READY",
                    "title": "Ready design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                },
                {
                    "order": 2,
                    "issue": 226,
                    "task_id": "TASK-REVIEW",
                    "title": "Review design",
                    "owner": "claude",
                    "queue_state": "review",
                    "depends_on": [],
                },
            ]
        }

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-REVIEW.md"

            def read_text(self) -> str:
                return "\n".join(
                    [
                        "---",
                        "task_id: TASK-REVIEW",
                        "related_chat: CHAT-TEST",
                        "branch: codex/claude/task-review",
                        "worktree: /tmp/task-review",
                        "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                        "bridge_visible_prefix: \"[BRIDGE 12345678] GH-226 review design\"",
                        "claude_activation_message_path: /tmp/message.md",
                        "---",
                        "",
                    ]
                )

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            context = project_design_queue.ready_context("amiga", payload)

        self.assertTrue(context["ready"])
        self.assertEqual(context["queue_state"], "review")
        self.assertEqual(context["task_id"], "TASK-REVIEW")

    def test_render_markdown_reports_active_lane_when_no_ready_lane(self) -> None:
        payload = {
            "project_id": "amiga",
            "artifact_type": "ordered_design_queue",
            "mode": "design-only-until-empty",
            "last_updated_utc": "2026-05-16T00:00:00+00:00",
            "lanes": [
                {
                    "order": 1,
                    "phase": "design-refresh",
                    "issue": 2,
                    "task_id": "TASK-A",
                    "title": "Staffing-control workflow design audit and split",
                    "owner": "claude",
                    "task_status": "in_progress",
                    "queue_state": "active",
                    "lane_type": "design-audit-and-split",
                    "depends_on": [],
                },
                {
                    "order": 2,
                    "phase": "design-refresh",
                    "issue": 6,
                    "task_id": "TASK-B",
                    "title": "Dashboard stale issue audit",
                    "owner": "claude",
                    "task_status": "open",
                    "queue_state": "queued",
                    "lane_type": "design-audit-and-split",
                    "depends_on": ["TASK-A"],
                },
            ],
        }

        rendered = project_design_queue.render_markdown(payload)

        self.assertIn("- Active lane: `GH-2` / `TASK-A` / `claude` / `active`", rendered)
        self.assertIn("- Next queued lane: `GH-6` / `TASK-B` / `claude`", rendered)

    def test_validation_status_line_reports_active_lane_when_no_ready_lane(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 2,
                    "task_id": "TASK-A",
                    "owner": "claude",
                    "queue_state": "active",
                },
                {
                    "order": 2,
                    "issue": 6,
                    "task_id": "TASK-B",
                    "owner": "claude",
                    "queue_state": "queued",
                },
            ]
        }

        status = project_design_queue.validation_status_line(payload)

        self.assertEqual(status, "current design lane: GH-2 / TASK-A / claude / active")

    def test_validate_command_json_reports_status_and_errors(self) -> None:
        payload = {
            "project_id": "amiga",
            "artifact_type": "ordered_design_queue",
            "lanes": [
                {
                    "order": 1,
                    "issue": 2,
                    "task_id": "TASK-A",
                    "title": "Staffing-control design",
                    "owner": "claude",
                    "task_status": "in_progress",
                    "queue_state": "active",
                    "lane_type": "design-audit-and-split",
                    "depends_on": [],
                }
            ],
        }

        with patch.object(
            project_design_queue,
            "parse_args",
            return_value=type(
                "Args",
                (),
                {
                    "command": "validate",
                    "project": "amiga",
                    "json": True,
                    "check_github": False,
                    "all_active": False,
                    "reason": "",
                },
            )(),
        ):
            with patch.object(project_design_queue, "load_queue", return_value=payload):
                with patch.object(project_design_queue, "validate_queue", return_value=([], [])):
                    with patch("sys.stdout", new_callable=StringIO) as stdout:
                        exit_code = project_design_queue.main()

        self.assertEqual(exit_code, 0)
        output = json.loads(stdout.getvalue())
        self.assertTrue(output["ok"])
        self.assertEqual(output["status"], "current design lane: GH-2 / TASK-A / claude / active")
        self.assertEqual(output["errors"], [])

    def test_ready_context_tracks_review_lane_when_no_ready_or_active_lane(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking confirmation design",
                    "owner": "claude",
                    "queue_state": "review",
                    "depends_on": ["TASK-DONE"],
                },
                {
                    "order": 2,
                    "issue": 226,
                    "task_id": "TASK-B",
                    "title": "Payment design",
                    "owner": "claude",
                    "queue_state": "queued",
                    "depends_on": ["TASK-A"],
                },
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "related_chat: CHAT-TEST",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: \"[BRIDGE 12345678] GH-223 booking design\"",
                "claude_desktop_thread_title: Booking design",
                "claude_activation_message_path: /tmp/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            context = project_design_queue.ready_context("amiga", payload)

        self.assertTrue(context["ready"])
        self.assertEqual(context["queue_state"], "review")
        self.assertEqual(context["task_id"], "TASK-A")
        self.assertTrue(context["bridge_metadata_complete"])

    def test_ready_context_flags_missing_bridge_metadata(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 226,
                    "task_id": "TASK-B",
                    "title": "Payment design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-B.md"

            def read_text(self) -> str:
                return "---\ntask_id: TASK-B\n---\n"

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            context = project_design_queue.ready_context("amiga", payload)

        self.assertTrue(context["ready"])
        self.assertFalse(context["bridge_metadata_complete"])
        self.assertEqual(context["claude_desktop_thread_title"], "Payment")
        self.assertIn("bridge_thread_uuid", context["missing_bridge_metadata"])

    def test_ensure_bridge_metadata_updates_all_active_lanes(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 226,
                    "task_id": "TASK-B",
                    "title": "Payment capture, void, and post-service notification UX design",
                    "owner": "claude",
                    "queue_state": "queued",
                    "depends_on": [],
                }
            ]
        }
        written: dict[str, str] = {}

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-B.md"

            def read_text(self) -> str:
                return "---\ntask_id: TASK-B\n---\n\nBody\n"

        def fake_write_file(path: object, content: str) -> None:
            written[str(path)] = content

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue.uuid, "uuid4", return_value="abcdef12-3456-7890-abcd-ef1234567890"):
                with patch.object(project_design_queue, "write_file", side_effect=fake_write_file):
                    result = project_design_queue.ensure_bridge_metadata("amiga", payload, all_active=True)

        self.assertEqual(result["updated"][0]["task_id"], "TASK-B")
        self.assertEqual(
            result["updated"][0]["bridge_visible_prefix"],
            "[BRIDGE abcdef12] GH-226 payment capture void post-service notification",
        )
        self.assertIn("bridge_thread_uuid: abcdef12-3456-7890-abcd-ef1234567890", written["/tmp/TASK-B.md"])

    def test_ensure_bridge_metadata_normalizes_quoted_existing_prefix(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        written: dict[str, str] = {}

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def read_text(self) -> str:
                return "\n".join(
                    [
                        "---",
                        "task_id: TASK-A",
                        "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                        "bridge_visible_prefix: \"[BRIDGE 12345678] GH-223 booking design\"",
                        "claude_desktop_thread_title: Booking design",
                        "---",
                        "",
                    ]
                )

        def fake_write_file(path: object, content: str) -> None:
            written[str(path)] = content

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue, "write_file", side_effect=fake_write_file):
                result = project_design_queue.ensure_bridge_metadata("amiga", payload, all_active=True)

        self.assertEqual(result["updated"][0]["bridge_visible_prefix"], "[BRIDGE 12345678] GH-223 booking design")
        self.assertIn("bridge_visible_prefix: [BRIDGE 12345678] GH-223 booking design", written["/tmp/TASK-A.md"])

    def test_render_desktop_prompt_uses_ready_context_fields(self) -> None:
        context = {
            "ready": True,
            "bridge_metadata_complete": True,
            "bridge_visible_prefix": "[BRIDGE 12345678] GH-223 booking design",
            "bridge_thread_uuid": "12345678-1234-1234-1234-123456789abc",
            "project_id": "amiga",
            "issue": 223,
            "task_id": "TASK-A",
            "title": "Booking design",
            "claude_activation_message_path": "/tmp/message.md",
            "worktree": "/tmp/worktree",
            "branch": "codex/claude/task-a",
        }

        prompt = project_design_queue.render_desktop_prompt(context)

        self.assertTrue(prompt.startswith("[BRIDGE 12345678] GH-223 booking design\n"))
        self.assertIn("bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc", prompt)
        self.assertIn("/tmp/message.md", prompt)
        self.assertIn("codex/claude/task-a", prompt)

    def test_render_desktop_prompt_rejects_missing_bridge_metadata(self) -> None:
        context = {
            "ready": True,
            "bridge_metadata_complete": False,
            "missing_bridge_metadata": ["worktree"],
        }

        with self.assertRaises(SystemExit) as raised:
            project_design_queue.render_desktop_prompt(context)

        self.assertIn("worktree", str(raised.exception))

    def test_bridge_status_reports_cpu_busy_without_durable_progress(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "status: open",
                "owner: claude",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: [BRIDGE 12345678] GH-223 booking design",
                "claude_activation_message_path: /repo/Chats/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def exists(self) -> bool:
                return True

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue, "worktree_state", return_value={"exists": True, "dirty": False, "head": "abc base"}):
                with patch.object(project_design_queue, "load_agent_inbox", return_value={"unread": [], "read": []}):
                    with patch.object(project_design_queue, "unread_messages_from", return_value=[]):
                        with patch.object(
                            project_design_queue.bridge_health,
                            "collect_health",
                            return_value={
                                "claude_frontmost": True,
                                "claude_visible": True,
                                "claude_main_process_metrics": {"busy": True, "cpu_percent_total": 67.0},
                            },
                        ):
                            status = project_design_queue.bridge_status("amiga", payload)

        self.assertEqual(status["classification"], "cpu-busy-no-durable-progress")
        self.assertFalse(status["durable_progress"])

    def test_bridge_status_reports_durable_progress_when_worktree_dirty(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "status: open",
                "owner: claude",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: [BRIDGE 12345678] GH-223 booking design",
                "claude_activation_message_path: /repo/Chats/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def exists(self) -> bool:
                return True

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue, "worktree_state", return_value={"exists": True, "dirty": True, "head": "def work"}):
                with patch.object(project_design_queue, "load_agent_inbox", return_value={"unread": [], "read": []}):
                    with patch.object(project_design_queue, "unread_messages_from", return_value=[]):
                        with patch.object(
                            project_design_queue.bridge_health,
                            "collect_health",
                            return_value={
                                "claude_frontmost": True,
                                "claude_visible": True,
                                "claude_main_process_metrics": {"busy": True, "cpu_percent_total": 67.0},
                            },
                        ):
                            status = project_design_queue.bridge_status("amiga", payload)

        self.assertEqual(status["classification"], "durable-progress-visible")
        self.assertTrue(status["durable_progress"])

    def test_bridge_status_reports_computer_use_cooldown_when_idle(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "status: in_progress",
                "owner: claude",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: [BRIDGE 12345678] GH-223 booking design",
                "claude_activation_message_path: /repo/Chats/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def exists(self) -> bool:
                return True

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue, "worktree_state", return_value={"exists": True, "dirty": False, "head": "abc base"}):
                with patch.object(project_design_queue, "load_agent_inbox", return_value={"unread": [], "read": []}):
                    with patch.object(project_design_queue, "unread_messages_from", return_value=[]):
                        with patch.object(
                            project_design_queue,
                            "load_bridge_state",
                            return_value={
                                "computer_use_timeouts": {
                                    "TASK-A": {
                                        "last_timeout_utc": project_design_queue.utc_iso(),
                                        "reason": "timeout",
                                    }
                                }
                            },
                        ):
                            with patch.object(
                                project_design_queue.bridge_health,
                                "collect_health",
                                return_value={
                                    "claude_frontmost": True,
                                    "claude_visible": True,
                                    "claude_main_process_metrics": {"busy": False, "cpu_percent_total": 0.0},
                                },
                            ):
                                status = project_design_queue.bridge_status("amiga", payload)

        self.assertEqual(status["classification"], "computer-use-cooldown-no-durable-progress")
        self.assertTrue(status["computer_use_blocker"]["active"])
        self.assertEqual(status["computer_use_blocker"]["timeout_count"], 1)
        self.assertEqual(status["computer_use_blocker"]["recommended_next_check_minutes"], 30)

    def test_bridge_status_prioritizes_computer_use_cooldown_over_busy_cpu(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "status: in_progress",
                "owner: claude",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: [BRIDGE 12345678] GH-223 booking design",
                "claude_activation_message_path: /repo/Chats/message.md",
                "---",
                "",
            ]
        )

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def exists(self) -> bool:
                return True

            def read_text(self) -> str:
                return task_body

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue, "worktree_state", return_value={"exists": True, "dirty": False, "head": "abc base"}):
                with patch.object(project_design_queue, "load_agent_inbox", return_value={"unread": [], "read": []}):
                    with patch.object(project_design_queue, "unread_messages_from", return_value=[]):
                        with patch.object(
                            project_design_queue,
                            "load_bridge_state",
                            return_value={
                                "computer_use_timeouts": {
                                    "TASK-A": {
                                        "last_timeout_utc": project_design_queue.utc_iso(),
                                        "reason": "timeout",
                                    }
                                }
                            },
                        ):
                            with patch.object(
                                project_design_queue.bridge_health,
                                "collect_health",
                                return_value={
                                    "claude_frontmost": True,
                                    "claude_visible": True,
                                    "claude_main_process_metrics": {"busy": True, "cpu_percent_total": 67.0},
                                },
                            ):
                                status = project_design_queue.bridge_status("amiga", payload)

        self.assertEqual(status["classification"], "computer-use-cooldown-no-durable-progress")
        self.assertTrue(status["computer_use_blocker"]["active"])

    def test_bridge_status_escalates_repeated_computer_use_timeout_cooldown(self) -> None:
        with patch.object(
            project_design_queue,
            "load_bridge_state",
            return_value={
                "computer_use_timeouts": {
                    "TASK-A": {
                        "last_timeout_utc": "2026-01-01T00:00:00+00:00",
                        "timeout_count": 2,
                        "reason": "timeout",
                    }
                }
            },
        ):
            blocker = project_design_queue.computer_use_timeout_status(
                "amiga",
                "TASK-A",
                now=datetime.fromisoformat("2026-01-01T00:30:01+00:00"),
            )

        self.assertTrue(blocker["active"])
        self.assertEqual(blocker["timeout_count"], 2)
        self.assertEqual(blocker["cooldown_seconds"], 3600)
        self.assertEqual(blocker["recommended_next_check_minutes"], 30)

    def test_computer_use_timeout_next_check_does_not_exceed_remaining_cooldown(self) -> None:
        with patch.object(
            project_design_queue,
            "load_bridge_state",
            return_value={
                "computer_use_timeouts": {
                    "TASK-A": {
                        "last_timeout_utc": "2026-01-01T00:00:00+00:00",
                        "timeout_count": 1,
                        "reason": "timeout",
                    }
                }
            },
        ):
            blocker = project_design_queue.computer_use_timeout_status(
                "amiga",
                "TASK-A",
                now=datetime.fromisoformat("2026-01-01T00:28:30+00:00"),
            )

        self.assertTrue(blocker["active"])
        self.assertEqual(blocker["seconds_remaining"], 90)
        self.assertEqual(blocker["recommended_next_check_seconds"], 90)
        self.assertEqual(blocker["recommended_next_check_minutes"], 2)

    def test_record_computer_use_timeout_writes_ready_lane_state(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "bridge_thread_uuid: 12345678-1234-1234-1234-123456789abc",
                "bridge_visible_prefix: [BRIDGE 12345678] GH-223 booking design",
                "claude_activation_message_path: /repo/Chats/message.md",
                "---",
                "",
            ]
        )
        written: dict[str, str] = {}

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def read_text(self) -> str:
                return task_body

        def fake_write_file(path: object, content: str) -> None:
            written[str(path)] = content

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(project_design_queue, "load_bridge_state", return_value={"computer_use_timeouts": {}}):
                with patch.object(project_design_queue, "write_file", side_effect=fake_write_file):
                    with patch.object(project_design_queue, "computer_use_timeout_status", return_value={"active": True}):
                        result = project_design_queue.record_computer_use_timeout("amiga", payload, reason="timeout")

        self.assertEqual(result["task_id"], "TASK-A")
        self.assertTrue(written)
        stored = next(iter(written.values()))
        self.assertIn('"TASK-A"', stored)
        self.assertIn('"timeout_count": 1', stored)
        self.assertIn('"reason": "timeout"', stored)

    def test_record_computer_use_timeout_resets_expired_cooldown_count(self) -> None:
        payload = {
            "lanes": [
                {
                    "order": 1,
                    "issue": 223,
                    "task_id": "TASK-A",
                    "title": "Booking design",
                    "owner": "claude",
                    "queue_state": "ready",
                    "depends_on": [],
                }
            ]
        }
        task_body = "\n".join(
            [
                "---",
                "task_id: TASK-A",
                "branch: codex/claude/task-a",
                "worktree: /tmp/task-a",
                "---",
                "",
            ]
        )
        written: dict[str, str] = {}

        class FakeTaskPath:
            def __str__(self) -> str:
                return "/tmp/TASK-A.md"

            def read_text(self) -> str:
                return task_body

        def fake_write_file(path: object, content: str) -> None:
            written[str(path)] = content

        expired_timeout = {
            "last_timeout_utc": "2000-01-01T00:00:00+00:00",
            "timeout_count": 3,
            "reason": "old timeout",
        }

        with patch.object(project_design_queue, "find_task_by_id", return_value=FakeTaskPath()):
            with patch.object(
                project_design_queue,
                "load_bridge_state",
                return_value={"computer_use_timeouts": {"TASK-A": expired_timeout}},
            ):
                with patch.object(project_design_queue, "write_file", side_effect=fake_write_file):
                    with patch.object(project_design_queue, "computer_use_timeout_status", return_value={"active": True}):
                        result = project_design_queue.record_computer_use_timeout("amiga", payload, reason="timeout")

        self.assertEqual(result["task_id"], "TASK-A")
        stored = next(iter(written.values()))
        self.assertIn('"timeout_count": 1', stored)
        self.assertIn('"reason": "timeout"', stored)


if __name__ == "__main__":
    unittest.main()
