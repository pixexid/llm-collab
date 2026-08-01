import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deploy_runtime


class DeployRuntimeTest(unittest.TestCase):
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
                patch.object(deploy_runtime, "git") as git,
                patch.object(deploy_runtime, "DEFAULT_TARGET", target),
            ):
                evidence = deploy_runtime.deploy(source)

        self.assertEqual("head-sha", evidence["head"])
        git.assert_called_once_with(target.resolve(), "reset", "--hard", "head-sha")

    def test_source_and_target_must_differ(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            with self.assertRaises(deploy_runtime.DeployError):
                deploy_runtime.deploy(path, path)


if __name__ == "__main__":
    unittest.main()
