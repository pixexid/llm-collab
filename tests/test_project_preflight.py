from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import _helpers  # noqa: E402
import claim_task  # noqa: E402


class ProjectPreflightTest(unittest.TestCase):
    def test_registered_commands_are_executed_without_cross_project_suffixes(self) -> None:
        cases = {
            "amiga": ["pnpm", "preflight", "--json", "--browser-check", "skip"],
            "llm-collab": ["python3.11", "bin/verify.py"],
            "nuvyr": ["sh", "-lc", "pnpm --dir nuvyr build"],
        }
        with tempfile.TemporaryDirectory(prefix="preflight-") as raw_root:
            root = Path(raw_root)
            for project_id, command in cases.items():
                with self.subTest(project_id=project_id):
                    completed = subprocess.CompletedProcess(command, 0, "", "")
                    project = {"id": project_id, "preflight_command": command}
                    with (
                        patch.object(_helpers, "get_project", return_value=project),
                        patch.object(_helpers, "_resolve_command_path", side_effect=lambda value, _cwd: value),
                        patch.object(_helpers, "_build_command_env", return_value={}),
                        patch.object(_helpers.subprocess, "run", return_value=completed) as run,
                    ):
                        result = _helpers.run_project_preflight(project_id, cwd=root)

                    self.assertTrue(result["ok"])
                    self.assertEqual(command, result["command"])
                    run.assert_called_once_with(
                        command,
                        cwd=root.resolve(),
                        text=True,
                        capture_output=True,
                        check=False,
                        env={},
                    )

    def test_claim_task_uses_the_registered_command_without_extra_args(self) -> None:
        project_id = "amiga"
        command = ["pnpm", "preflight", "--json", "--browser-check", "skip"]
        frontmatter = (
            "---\n"
            "task_id: TASK-PREFLIGHT\n"
            "title: preflight test\n"
            "status: open\n"
            "owner: codex\n"
            "created_by: codex\n"
            "refined_by: claude\n"
            "project_id: amiga\n"
            "skip_refinement: false\n"
            "---\n"
        )
        with tempfile.TemporaryDirectory(prefix="claim-preflight-") as raw_root:
            task_file = Path(raw_root) / "task.md"
            task_file.write_text(frontmatter, encoding="utf-8")
            preflight = {
                "ran": True,
                "ok": True,
                "cwd": raw_root,
                "command": command,
                "returncode": 0,
            }
            with (
                patch.object(claim_task, "agent_ids", return_value=["codex"]),
                patch.object(claim_task, "ensure_agent_enabled"),
                patch.object(claim_task, "find_task_by_id", return_value=task_file),
                patch.object(claim_task, "sync_task_contract", return_value=(
                    {
                        "task_id": "TASK-PREFLIGHT",
                        "title": "preflight test",
                        "status": "open",
                        "owner": "codex",
                        "created_by": "codex",
                        "refined_by": "claude",
                        "project_id": project_id,
                        "skip_refinement": False,
                    },
                    "",
                )),
                patch.object(claim_task, "validate_direct_app_policy", return_value=([], {})),
                patch.object(claim_task, "validate_implementation_risk_analysis", return_value=[]),
                patch.object(claim_task, "validate_task_contract", return_value=([], {})),
                patch.object(claim_task.issue_queue, "queue_exists", return_value=False),
                patch.object(claim_task, "run_project_preflight", return_value=preflight) as run,
                patch.object(claim_task, "target_task_path", return_value=task_file),
                patch.object(claim_task, "write_file"),
                patch.object(claim_task, "ROOT", Path(raw_root)),
                patch.object(sys, "argv", [
                    "claim_task.py",
                    "--task",
                    "TASK-PREFLIGHT",
                    "--owner",
                    "codex",
                    "--status",
                    "in_progress",
                ]),
            ):
                with redirect_stdout(StringIO()):
                    claim_task.main()

            run.assert_called_once_with(project_id)


if __name__ == "__main__":
    unittest.main()
