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


class TaskContractUiVisualFeedbackTest(unittest.TestCase):
    def test_ui_contract_defaults_to_ten_minute_operator_feedback_timeout(self) -> None:
        # #given
        frontmatter = {"ui_ux_lane": True, "ui_ux_mode": "implementation"}
        body = "Update src/components/ui/change-request-review-panel.tsx."

        # #when
        synced, changed = task_contract.sync_ui_ux_contract(frontmatter, body)

        # #then
        self.assertEqual(synced["operator_visual_feedback_timeout_minutes"], 10)
        self.assertIn("operator_visual_feedback_timeout_minutes", changed)

    def test_ui_review_requires_feedback_request_not_operator_reply(self) -> None:
        # #given
        frontmatter = {
            "ui_ux_lane": True,
            "ui_ux_mode": "implementation",
            "required_design_docs": [task_contract.AMIGA_DESIGN_DOC],
            "required_design_skills": ["impeccable"],
            "impeccable_required": True,
            "impeccable_antipatterns_enforced": True,
            "design_doc_update_review_required": True,
            "impeccable_commands_required": ["/impeccable craft", "/polish"],
            "design_docs_read": [task_contract.AMIGA_DESIGN_DOC],
            "design_skills_used": ["impeccable"],
            "impeccable_commands_used": ["/impeccable craft", "/polish"],
            "impeccable_detect_result": "pass",
            "browser_validation_desktop": "pass",
            "browser_validation_mobile": "pass",
            "operator_visual_feedback_requested": True,
            "operator_visual_feedback_timeout_minutes": 10,
            "operator_visual_feedback_disposition": "timeout_proceeded",
            "design_doc_update_decision": "reviewed; no docs change needed",
            "design_thinking_polish_budget_loc": 40,
            "design_thinking_polish_seeds": ["mobile wrap", "null separator"],
            "design_thinking_pass_items": [
                {"finding": "Badge wraps cleanly", "disposition": "shipped"},
                {"finding": "Icon alignment holds", "disposition": "shipped"},
                {"finding": "Null state omits separator", "disposition": "shipped"},
            ],
        }
        body = "Update src/components/ui/change-request-review-panel.tsx."

        # #when
        errors, _summary = task_contract.validate_ui_ux_contract(frontmatter, body, stage="review")

        # #then
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
