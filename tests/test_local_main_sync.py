"""State coverage for bin/local_main_sync.py: the post-merge local-main gate."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "local_main_sync.py"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def init_identity(repo: Path) -> None:
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "commit.gpgsign", "false")


class LocalMainSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.work = root / "work"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.origin), str(self.work)],
                       check=True, capture_output=True)
        init_identity(self.work)
        (self.work / "f.txt").write_text("v1\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "c1")
        git(self.work, "branch", "-M", "main")
        git(self.work, "push", "-u", "origin", "main")

    def advance_origin(self) -> None:
        """Land a new commit on origin/main via a second clone (simulates a merge)."""
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", str(self.origin), str(other)],
                       check=True, capture_output=True)
        init_identity(other)
        (other / "g.txt").write_text("remote\n")
        git(other, "add", ".")
        git(other, "commit", "-m", "remote-commit")
        git(other, "push", "origin", "main")

    def run_sync(self, *flags: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.work), "--json", *flags],
            capture_output=True, text=True,
        )
        raw = proc.stdout.strip() or proc.stderr.strip()
        return proc.returncode, json.loads(raw)

    def test_already_current(self) -> None:
        rc, info = self.run_sync()
        self.assertEqual(0, rc)
        self.assertEqual("already_current", info["classification"])
        self.assertEqual(info["head"], info["origin_main"])

    def test_untracked_files_ignored(self) -> None:
        (self.work / "runtime_state.log").write_text("noise\n")
        rc, info = self.run_sync()
        self.assertEqual(0, rc)
        self.assertEqual("already_current", info["classification"])

    def test_aligned_to_main_when_detached(self) -> None:
        git(self.work, "checkout", "--detach", "HEAD")
        rc, info = self.run_sync()
        self.assertEqual(0, rc)
        self.assertEqual("aligned_to_main", info["classification"])

    def test_fast_forward_applies(self) -> None:
        self.advance_origin()
        rc, info = self.run_sync()
        self.assertEqual(0, rc)
        self.assertEqual("fast_forwarded", info["classification"])
        self.assertFalse(info["applied"])
        rc, info = self.run_sync("--apply")
        self.assertEqual(0, rc)
        self.assertTrue(info["applied"])
        self.assertEqual(info["head"], info["origin_main"])

    def test_fast_forward_detached_advances_head(self) -> None:
        # The shared canonical checkout is routinely detached (its `main` is held
        # by another worktree), so apply must advance the detached HEAD without a
        # `git checkout main`.
        self.advance_origin()
        git(self.work, "checkout", "--detach", "HEAD")
        rc, info = self.run_sync("--apply")
        self.assertEqual(0, rc)
        self.assertEqual("fast_forwarded", info["classification"])
        self.assertTrue(info["applied"])
        self.assertEqual(info["head"], info["origin_main"])
        self.assertEqual("(detached)", info["branch"])

    def test_active_branch_blocks(self) -> None:
        (self.work / "local.txt").write_text("local\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "local-only")
        rc, info = self.run_sync()
        self.assertEqual(1, rc)
        self.assertEqual("active_branch", info["classification"])
        rc_apply, _ = self.run_sync("--apply")
        self.assertEqual(1, rc_apply, "apply must not act on a blocked classification")

    def test_diverged_blocks(self) -> None:
        (self.work / "local.txt").write_text("local\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "local-only")
        self.advance_origin()
        rc, info = self.run_sync()
        self.assertEqual(1, rc)
        self.assertEqual("diverged", info["classification"])

    def test_dirty_tracked_blocks(self) -> None:
        (self.work / "f.txt").write_text("modified\n")
        rc, info = self.run_sync()
        self.assertEqual(1, rc)
        self.assertEqual("dirty_tracked", info["classification"])

    def test_staged_change_blocks(self) -> None:
        (self.work / "f.txt").write_text("staged\n")
        git(self.work, "add", "f.txt")
        rc, info = self.run_sync()
        self.assertEqual(1, rc)
        self.assertEqual("dirty_tracked", info["classification"])

    def test_not_a_checkout_errors(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(Path(self.tmp.name) / "nope"), "--json"],
            capture_output=True, text=True,
        )
        self.assertEqual(1, proc.returncode)
        self.assertEqual("error", json.loads(proc.stderr.strip())["classification"])


if __name__ == "__main__":
    unittest.main()
