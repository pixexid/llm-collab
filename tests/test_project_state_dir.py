"""Project-state path-token boundary proofs for GH-744."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import _helpers  # noqa: E402


class ProjectStateDirTest(unittest.TestCase):
    def test_amiga_and_non_amiga_projects_stay_on_their_exact_directories(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            with patch.object(_helpers, "project_state_root", return_value=root):
                amiga = _helpers.project_state_dir("amiga")
                llm_collab = _helpers.project_state_dir("llm-collab")

        self.assertEqual(root / "amiga", amiga)
        self.assertEqual(root / "llm-collab", llm_collab)
        self.assertNotEqual(
            amiga,
            root / "llm-collab",
            "Amiga state must not resolve to the non-Amiga project's directory",
        )
        self.assertNotEqual(
            llm_collab,
            root / "amiga",
            "Non-Amiga state must not resolve to Amiga's directory",
        )

    def test_path_bearing_project_id_cannot_escape_project_state_root(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            with patch.object(_helpers, "project_state_root", return_value=root):
                try:
                    _helpers.project_state_dir("../nuvyr")
                except ValueError as error:
                    self.assertIn("project_id", str(error))
                else:
                    self.fail(
                        "path-bearing project ID escaped project_state_root into another project"
                    )


if __name__ == "__main__":
    unittest.main()
