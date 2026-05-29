from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import task_contract


class TaskContractDbDetectionTest(unittest.TestCase):
    def test_stale_generated_false_does_not_mask_later_schema_markers(self) -> None:
        # #given
        frontmatter = {
            "db_impact": "shared-supabase-required",
            "db_schema_change_detected": False,
        }
        body = "Add a Supabase migration that creates a new booking table."

        # #when
        synced, changed = task_contract.sync_db_contract(frontmatter, body)

        # #then
        self.assertTrue(synced["db_schema_change_detected"])
        self.assertEqual(synced["db_schema_change_detection"], "auto")
        self.assertIn("db_schema_change_detected", changed)

    def test_manual_false_schema_change_override_is_preserved(self) -> None:
        # #given
        frontmatter = {
            "db_impact": "shared-supabase-required",
            "db_schema_change_detected": False,
            "db_schema_change_detection": "manual_false",
        }
        body = "Update Supabase runbook prose that mentions migration and schema evidence."

        # #when
        synced, changed = task_contract.sync_db_contract(frontmatter, body)

        # #then
        self.assertFalse(synced["db_schema_change_detected"])
        self.assertEqual(synced["db_schema_change_detection"], "manual_false")
        self.assertNotIn("db_schema_change_detected", changed)


if __name__ == "__main__":
    unittest.main()
