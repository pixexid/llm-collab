from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


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


class TaskContractProjectDesignDocsTest(unittest.TestCase):
    def test_amiga_ui_contract_keeps_amiga_design_doc_default(self) -> None:
        # #given
        frontmatter = {
            "project_id": "amiga",
            "ui_ux_lane": True,
            "ui_ux_mode": "docs_only",
        }

        # #when
        synced, _changed = task_contract.sync_ui_ux_contract(frontmatter, "Update UI workflow docs.")

        # #then
        self.assertEqual(synced["required_design_docs"], [task_contract.AMIGA_DESIGN_DOC])

    def test_project_config_replaces_foreign_amiga_default_and_preserves_task_docs(self) -> None:
        # #given
        project_docs = ["/projects/nuvyr/docs/product/website-design.md"]
        frontmatter = {
            "project_id": "nuvyr",
            "ui_ux_lane": True,
            "ui_ux_mode": "docs_only",
            "required_design_docs": [
                task_contract.AMIGA_DESIGN_DOC,
                "/projects/nuvyr/docs/product/canonical-facts.md",
            ],
        }

        # #when
        with patch.object(
            task_contract,
            "get_project",
            return_value={"id": "nuvyr", "ui_ux": {"required_design_docs": project_docs}},
        ):
            synced, changed = task_contract.sync_ui_ux_contract(frontmatter, "Update public UI docs.")

        # #then
        self.assertEqual(
            synced["required_design_docs"],
            [*project_docs, "/projects/nuvyr/docs/product/canonical-facts.md"],
        )
        self.assertIn("required_design_docs", changed)

    def test_unconfigured_non_amiga_project_requires_explicit_design_source(self) -> None:
        # #given
        frontmatter = {
            "project_id": "other",
            "ui_ux_lane": True,
            "ui_ux_mode": "docs_only",
            "required_design_docs": [task_contract.AMIGA_DESIGN_DOC],
        }

        # #when
        with patch.object(task_contract, "get_project", return_value={"id": "other"}):
            errors, summary = task_contract.validate_ui_ux_contract(
                frontmatter,
                "Update public UI docs.",
                stage="plan",
            )

        # #then
        self.assertEqual(summary["required_design_docs"], [])
        self.assertIn(
            "UI/UX lane must list at least one project design source in `required_design_docs`.",
            errors,
        )


class TaskContractProjectDbConfigTest(unittest.TestCase):
    def test_amiga_db_contract_keeps_legacy_defaults(self) -> None:
        # #given
        frontmatter = {
            "project_id": "amiga",
            "db_impact": "shared-supabase-required",
        }

        # #when
        synced, _changed = task_contract.sync_db_contract(frontmatter, "Apply a shared Supabase migration.")

        # #then
        self.assertEqual(synced["db_project_ref"], task_contract.AMIGA_SHARED_SUPABASE_PROJECT_REF)
        self.assertEqual(
            synced["db_required_surfaces"],
            task_contract.AMIGA_SHARED_SUPABASE_REQUIRED_SURFACES,
        )

    def test_project_db_config_replaces_foreign_amiga_defaults(self) -> None:
        # #given
        project_surfaces = ["supabase_other.execute_sql", "supabase CLI"]
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
            "db_project_ref": task_contract.AMIGA_SHARED_SUPABASE_PROJECT_REF,
            "db_required_surfaces": task_contract.AMIGA_SHARED_SUPABASE_REQUIRED_SURFACES,
        }

        # #when
        with patch.object(
            task_contract,
            "get_project",
            return_value={
                "id": "other",
                "db": {
                    "shared_supabase_project_ref": "other-project-ref",
                    "required_surfaces": project_surfaces,
                },
            },
        ):
            synced, changed = task_contract.sync_db_contract(frontmatter, "Apply a shared Supabase migration.")

        # #then
        self.assertEqual(synced["db_project_ref"], "other-project-ref")
        self.assertEqual(synced["db_required_surfaces"], project_surfaces)
        self.assertIn("db_project_ref", changed)
        self.assertIn("db_required_surfaces", changed)

    def test_unconfigured_non_amiga_project_requires_explicit_db_contract(self) -> None:
        # #given
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
        }

        # #when
        with patch.object(task_contract, "get_project", return_value={"id": "other"}):
            errors, summary = task_contract.validate_db_contract(
                frontmatter,
                "Apply a shared Supabase migration.",
                stage="plan",
            )

        # #then
        self.assertEqual(summary["db_project_ref"], "")
        self.assertIn(
            "Shared Supabase lane must configure `db.shared_supabase_project_ref` for the project "
            "or provide an explicit task-level `db_project_ref`.",
            errors,
        )
        self.assertIn(
            "Shared Supabase lane must configure project `db.required_surfaces` or provide "
            "explicit task-level `db_required_surfaces`.",
            errors,
        )

    def test_unconfigured_non_amiga_project_accepts_explicit_task_db_contract(self) -> None:
        # #given
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
            "db_project_ref": "explicit-project-ref",
            "db_required_surfaces": ["project_db.execute_sql", "project db CLI"],
        }

        # #when
        with patch.object(task_contract, "get_project", return_value={"id": "other"}):
            errors, summary = task_contract.validate_db_contract(
                frontmatter,
                "Apply a shared database migration.",
                stage="plan",
            )

        # #then
        self.assertEqual(errors, [])
        self.assertEqual(summary["db_project_ref"], "explicit-project-ref")


if __name__ == "__main__":
    unittest.main()
