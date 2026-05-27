from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import post_merge_cleanup


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class PostMergeCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "amiga"
        self.tasks = self.root / "Tasks"
        self.worktree_root = self.root / "amiga-worktrees"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test User")
        (self.repo / "README.md").write_text("main\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def args(self, *, apply: bool = False, discard: bool = False, plain_dirs: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            project="amiga",
            repo_key="app",
            repo=str(self.repo),
            base="main",
            worktree_root=str(self.worktree_root),
            apply=apply,
            remove_plain_dirs=plain_dirs,
            discard_disposable_dirty=discard,
            json=False,
        )

    def write_task(self, task_id: str, status: str, branch: str) -> None:
        task_dir = self.tasks / ("done" if status == "done" else "active")
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / f"{task_id.lower()}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"task_id: {task_id}",
                    f"status: {status}",
                    f"branch: {branch}",
                    "---",
                    "",
                    "# Task",
                    "",
                ]
            )
        )

    def create_worktree(self, branch: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-b", branch, str(path), "main")
        (path / "branch.txt").write_text(branch)
        git(path, "add", "branch.txt")
        git(path, "commit", "-m", branch)

    def test_done_task_worktree_is_removed_even_when_branch_is_not_merged(self) -> None:
        branch = "codex/claude/task-ABC123-example"
        path = self.worktree_root / "claude" / "task-ABC123-example"
        self.create_worktree(branch, path)
        self.write_task("TASK-ABC123", "done", branch)

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args())

        self.assertEqual([item["branch"] for item in summary["remove_worktrees"]], [branch])
        self.assertFalse(summary["deferred_worktrees"])
        self.assertFalse(summary["ok_to_clear_post_merge"])

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args(apply=True))
                post_merge_cleanup.apply_cleanup(summary)

        self.assertFalse(path.exists())
        self.assertNotIn(branch, git(self.repo, "branch", "--format=%(refname:short)").splitlines())

    def test_dirty_non_disposable_done_task_is_deferred(self) -> None:
        branch = "codex/cdx2/task-DEF456-example"
        path = self.worktree_root / "cdx2" / "task-DEF456-example"
        self.create_worktree(branch, path)
        self.write_task("TASK-DEF456", "done", branch)
        (path / "human-note.md").write_text("do not delete\n")

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args())

        self.assertFalse(summary["remove_worktrees"])
        self.assertEqual(summary["deferred_worktrees"][0]["defer_reason"], "dirty-non-disposable")
        self.assertEqual(summary["blocking_deferred"][0]["branch"], branch)

    def test_disposable_sitemap_dirty_can_be_removed_when_enabled(self) -> None:
        branch = "codex/claude/task-GHI789-example"
        path = self.worktree_root / "claude" / "task-GHI789-example"
        self.create_worktree(branch, path)
        self.write_task("TASK-GHI789", "done", branch)
        (path / "public").mkdir()
        (path / "public" / "sitemap.xml").write_text("generated\n")

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args(discard=True))

        self.assertEqual(summary["remove_worktrees"][0]["reason"], "done-task-disposable-dirty")

    def test_plain_disposable_directory_is_reported(self) -> None:
        stale = self.worktree_root / "codex" / "old-check"
        (stale / ".vite").mkdir(parents=True)

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args(plain_dirs=True))

        self.assertEqual(Path(summary["remove_plain_dirs"][0]["path"]).resolve(), stale.resolve())
        self.assertFalse(summary["ok_to_clear_post_merge"])

    def test_empty_worktree_parent_directory_is_removed(self) -> None:
        empty_parent = self.worktree_root / "claude"
        empty_parent.mkdir(parents=True)

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args(plain_dirs=True))

        self.assertEqual(Path(summary["remove_empty_dirs"][0]["path"]).resolve(), empty_parent.resolve())
        self.assertFalse(summary["ok_to_clear_post_merge"])

        post_merge_cleanup.apply_cleanup(summary)

        self.assertFalse(self.worktree_root.exists())

    def test_empty_dirs_inside_registered_worktrees_are_not_reported(self) -> None:
        branch = "codex/cdx2/task-JKL012-example"
        path = self.worktree_root / "cdx2" / "task-JKL012-example"
        self.create_worktree(branch, path)
        self.write_task("TASK-JKL012", "review", branch)
        nested_empty_dir = path / "node_modules" / ".cache" / "empty"
        nested_empty_dir.mkdir(parents=True)

        with patch.object(post_merge_cleanup, "ensure_project", return_value=None):
            with patch.object(post_merge_cleanup, "TASKS_DIR", self.tasks):
                summary = post_merge_cleanup.classify(self.args(plain_dirs=True))

        empty_paths = {Path(item["path"]).resolve() for item in summary["remove_empty_dirs"]}
        self.assertNotIn(nested_empty_dir.resolve(), empty_paths)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
