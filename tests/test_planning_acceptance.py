from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import claim_task


class PlanningAcceptanceTest(unittest.TestCase):
    def test_claude_created_and_planned_task_requires_codex_acceptance(self) -> None:
        # #given
        frontmatter = {"created_by": "claude", "refined_by": "claude"}

        # #when
        requires_acceptance = claim_task.requires_codex_acceptance(frontmatter)

        # #then
        self.assertTrue(requires_acceptance)

    def test_codex_acceptance_satisfies_self_planned_task_gate(self) -> None:
        # #given
        frontmatter = {"created_by": "claude", "refined_by": "claude", "accepted_by": "codex"}

        # #when
        accepted = claim_task.has_codex_acceptance(frontmatter)

        # #then
        self.assertTrue(accepted)

    def test_cross_agent_refinement_does_not_require_codex_acceptance(self) -> None:
        # #given
        frontmatter = {"created_by": "codex", "refined_by": "claude"}

        # #when
        requires_acceptance = claim_task.requires_codex_acceptance(frontmatter)

        # #then
        self.assertFalse(requires_acceptance)

    def test_skip_refinement_bypass_does_not_require_codex_acceptance(self) -> None:
        # #given
        frontmatter = {"created_by": "claude", "skip_refinement": True}

        # #when
        requires_acceptance = claim_task.requires_codex_acceptance(frontmatter)

        # #then
        self.assertFalse(requires_acceptance)


if __name__ == "__main__":
    unittest.main()
