"""Focused release-lifecycle regressions outside claim_task's direct gate."""

from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_github_task_mirrors
import init as init_script


def write_mirror(root: Path, status: str, *, frontmatter: bool = True) -> Path:
    path = root / "active" / "gh-100-lane__TASK-TEST01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not frontmatter:
        path.write_text("no frontmatter here\n")
        return path
    path.write_text(
        "---\n"
        "task_id: TASK-TEST01\n"
        "project_id: demo\n"
        f"status: {status}\n"
        "owner: worker\n"
        "---\n"
        "\n# GH-100 test mirror\n\n## Activity Log\n"
    )
    return path


class ArchiveClosedTaskFailsClosedTest(unittest.TestCase):
    def test_refuses_every_non_done_status_without_write_or_move(self) -> None:
        for status in ("open", "in_progress", "blocked", "review"):
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                task_root = Path(tmp) / "Tasks"
                path = write_mirror(task_root, status)
                before = path.read_text()

                result = check_github_task_mirrors.archive_closed_task(path, 100)

                self.assertIsNone(result)
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text(), before)
                self.assertFalse((task_root / "done").exists())

    def test_missing_frontmatter_is_refused_without_write_or_move(self) -> None:
        with TemporaryDirectory() as tmp:
            task_root = Path(tmp) / "Tasks"
            path = write_mirror(task_root, "review", frontmatter=False)
            before = path.read_text()

            result = check_github_task_mirrors.archive_closed_task(path, 100)

            self.assertIsNone(result)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(), before)
            self.assertFalse((task_root / "done").exists())

    def test_archives_only_an_already_done_mirror(self) -> None:
        with TemporaryDirectory() as tmp:
            task_root = Path(tmp) / "Tasks"
            path = write_mirror(task_root, "done")

            result = check_github_task_mirrors.archive_closed_task(path, 100)

            self.assertIsNotNone(result)
            self.assertFalse(path.exists())
            self.assertTrue(result.exists())
            self.assertIn("status: done", result.read_text())
            self.assertIn("archived already-done mirror", result.read_text())

    def test_caller_reports_non_done_closed_issue_as_actionable_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_mirror(root / "Tasks", "review")
            before = path.read_text()
            output = StringIO()
            issue = {
                "title": "Closed",
                "url": "https://github.com/owner/demo/issues/100",
                "state": "CLOSED",
                "labels": "",
            }
            with patch.object(check_github_task_mirrors, "ROOT", root):
                with patch.object(check_github_task_mirrors, "ensure_project"):
                    with patch.object(
                        check_github_task_mirrors,
                        "gh_issue_map",
                        return_value={100: issue},
                    ):
                        with patch.object(
                            check_github_task_mirrors,
                            "iter_project_task_mirrors",
                            return_value=[path],
                        ):
                            argv = [
                                "check_github_task_mirrors.py",
                                "--project", "demo",
                                "--repo", "owner/demo",
                                "--archive-closed-active",
                            ]
                            with patch.object(sys, "argv", argv):
                                with redirect_stdout(output):
                                    exit_code = check_github_task_mirrors.main()

            self.assertEqual(exit_code, 1)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(), before)
            self.assertIn("REFUSED to promote", output.getvalue())
            self.assertIn("remains in place as release-lifecycle drift", output.getvalue())

    def test_archive_flag_help_is_honest(self) -> None:
        source = (REPO_ROOT / "bin" / "check_github_task_mirrors.py").read_text()
        self.assertNotIn("and mark status done", source)
        self.assertIn("Never promotes", source)


class InitReleaseGateAgentTest(unittest.TestCase):
    def test_enabled_agent_ids_excludes_disabled_entries(self) -> None:
        agents = [
            {"id": "codex", "role": "orchestrator", "activation": {"enabled": True}},
            {"id": "claude", "disabled": True, "activation": {}},
            {"id": "legacy", "role": "legacy_disabled_worker", "activation": {}},
            {"id": "off", "activation": {"enabled": False}},
        ]
        self.assertEqual(init_script.enabled_agent_ids(agents), ["codex"])

    def test_selection_reprompts_empty_unknown_and_disabled_ids(self) -> None:
        output = StringIO()
        with patch.object(
            init_script,
            "prompt",
            side_effect=["", "ghost", "codex"],
        ) as prompt:
            with redirect_stdout(output):
                selected = init_script.select_release_gate_agent(
                    "demo",
                    ["codex", "claude"],
                )

        self.assertEqual(selected, "codex")
        self.assertEqual(prompt.call_count, 3)
        self.assertIn("release_gate_agent is required", output.getvalue())
        self.assertIn("Unknown or disabled release_gate_agent 'ghost'", output.getvalue())

    def test_project_generator_always_emits_explicit_selected_release_gate_agent(self) -> None:
        with patch.object(
            init_script,
            "yn",
            side_effect=[True, False, False, False],
        ):
            with patch.object(
                init_script,
                "prompt",
                side_effect=["demo", "Demo", "main", "codex"],
            ):
                with patch.object(
                    init_script,
                    "prompt_list",
                    return_value=["app:demo"],
                ):
                    projects = init_script.collect_projects(["codex", "claude"])

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["id"], "demo")
        self.assertEqual(projects[0]["release_gate_agent"], "codex")
        self.assertIn(projects[0]["release_gate_agent"], {"codex", "claude"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
