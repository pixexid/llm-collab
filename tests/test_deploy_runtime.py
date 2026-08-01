import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deploy_runtime


class DeployRuntimeTest(unittest.TestCase):
    def test_source_head_rejects_unmerged_feature_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            (source / "AGENTS.md").write_text("<!-- CONTRACT_VERSION: 10 -->\n")
            with patch.object(
                deploy_runtime,
                "git",
                side_effect=["", "origin-sha", "feature-sha"],
            ):
                with self.assertRaisesRegex(deploy_runtime.DeployError, "exact origin/main"):
                    deploy_runtime.source_head(source)

    def test_deploy_refreshes_target_only_after_source_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            target = Path(temp_dir) / "target"
            source.mkdir()
            target.mkdir()
            (source / ".git").mkdir()
            (target / ".git").mkdir()
            with (
                patch.object(
                    deploy_runtime,
                    "source_head",
                    return_value=("head-sha", "10"),
                ),
                patch.object(
                    deploy_runtime,
                    "git",
                    side_effect=["old-sha", ""],
                ) as git,
                patch.object(
                    deploy_runtime.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as run,
                patch.object(deploy_runtime, "DEFAULT_TARGET", target),
            ):
                evidence = deploy_runtime.deploy(source)

        self.assertEqual("head-sha", evidence["head"])
        git.assert_has_calls(
            [
                call(target.resolve(), "rev-parse", "HEAD"),
                call(target.resolve(), "status", "--porcelain=v1", "--untracked-files=no"),
            ]
        )
        run.assert_called_once()
        self.assertEqual("head-sha", run.call_args.args[0][-1])

    def test_timeout_rolls_target_back_to_previous_head(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            target = Path(temp_dir) / "target"
            source.mkdir()
            target.mkdir()
            (source / ".git").mkdir()
            (target / ".git").mkdir()
            with (
                patch.object(deploy_runtime, "source_head", return_value=("new-sha", "10")),
                patch.object(
                    deploy_runtime,
                    "git",
                    side_effect=["old-sha", "", "old-sha", ""],
                ),
                patch.object(
                    deploy_runtime.subprocess,
                    "run",
                    side_effect=[
                        subprocess.TimeoutExpired(["git"], 30),
                        subprocess.CompletedProcess([], 0, "", ""),
                    ],
                ),
            ):
                with self.assertRaisesRegex(deploy_runtime.DeployError, "restored target HEAD old-sha"):
                    deploy_runtime.deploy(source, target)

    def test_source_and_target_must_differ(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            with self.assertRaises(deploy_runtime.DeployError):
                deploy_runtime.deploy(path, path)


if __name__ == "__main__":
    unittest.main()
