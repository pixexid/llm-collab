from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import task_contract
import claim_task
import new_task


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
            "project_id": "amiga",
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

    def test_project_db_config_replaces_foreign_ref_and_augmented_amiga_surfaces(self) -> None:
        # #given
        project_surfaces = ["supabase_other.execute_sql", "supabase CLI"]
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
            "db_project_ref": "foreign-project-ref",
            "db_required_surfaces": [
                "supabase CLI",
                "supabase_amiga.execute_sql",
                "project_specific.read_only_probe",
                "supabase_amiga.get_project",
                "supabase_amiga.get_advisors",
            ],
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
        self.assertEqual(
            synced["db_required_surfaces"],
            [*project_surfaces, "project_specific.read_only_probe"],
        )
        self.assertIn("db_project_ref", changed)
        self.assertIn("db_required_surfaces", changed)

    def test_configured_project_validation_rejects_unsynced_foreign_ref(self) -> None:
        # #given
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
            "db_project_ref": "foreign-project-ref",
            "db_required_surfaces": ["supabase_other.execute_sql"],
        }
        project = {
            "id": "other",
            "db": {
                "shared_supabase_project_ref": "other-project-ref",
                "required_surfaces": ["supabase_other.execute_sql"],
            },
        }

        # #when
        with patch.object(task_contract, "get_project", return_value=project):
            errors, summary = task_contract.validate_db_contract(
                frontmatter,
                "Apply a shared Supabase migration.",
                stage="plan",
            )

        # #then
        self.assertEqual(summary["db_project_ref"], "foreign-project-ref")
        self.assertIn(
            "Shared Supabase lane must set project-configured `db_project_ref: other-project-ref`.",
            errors,
        )

    def test_configured_project_validation_rejects_missing_persisted_ref(self) -> None:
        # #given
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
            "db_required_surfaces": ["supabase_other.execute_sql"],
        }
        project = {
            "id": "other",
            "db": {
                "shared_supabase_project_ref": "other-project-ref",
                "required_surfaces": ["supabase_other.execute_sql"],
            },
        }

        # #when
        with patch.object(task_contract, "get_project", return_value=project):
            errors, summary = task_contract.validate_db_contract(
                frontmatter,
                "Apply a shared Supabase migration.",
                stage="plan",
            )

        # #then
        self.assertEqual(summary["db_project_ref"], "")
        self.assertIn(
            "Shared Supabase lane must set project-configured `db_project_ref: other-project-ref`.",
            errors,
        )

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

    def test_unconfigured_non_amiga_project_preserves_generic_supabase_cli_surface(self) -> None:
        # #given
        frontmatter = {
            "project_id": "other",
            "db_impact": "shared-supabase-required",
            "db_project_ref": "explicit-project-ref",
            "db_required_surfaces": ["project_db.execute_sql", "supabase CLI"],
        }

        # #when
        with patch.object(task_contract, "get_project", return_value={"id": "other"}):
            synced, _ = task_contract.sync_db_contract(frontmatter, "Apply a shared database migration.")

        # #then
        self.assertEqual(
            synced["db_required_surfaces"],
            ["project_db.execute_sql", "supabase CLI"],
        )


class TaskContractProductionSchemaGuardTest(unittest.TestCase):
    GH_1453_MIGRATIONS = [
        "db/migrations/20260712_gh_1453_acceptance_event_atomic.sql",
        "db/migrations/20260712_gh_1453_series_snapshot_atomic.sql",
    ]

    def project(self, project_id: str, guard: object = True) -> dict:
        return {
            "id": project_id,
            "db": {
                "production_schema_guard": guard,
                "shared_supabase_project_ref": f"{project_id}-project-ref",
                "required_surfaces": [f"{project_id}_db.execute_sql", f"{project_id} db CLI"],
            },
        }

    def shared_task(self, project_id: str = "amiga") -> dict:
        return {
            "project_id": project_id,
            "status": "review",
            "related_paths": list(self.GH_1453_MIGRATIONS),
            "db_impact": "shared-supabase-required",
            "db_schema_change_detected": True,
            "db_schema_change_detection": "auto",
            "db_project_ref": f"{project_id}-project-ref",
            "db_required_surfaces": [
                f"{project_id}_db.execute_sql",
                f"{project_id} db CLI",
            ],
            "db_migration_files": list(self.GH_1453_MIGRATIONS),
            "db_apply_result": "pass: applied to exact project",
            "db_schema_assertion": "pass: schema shape verified",
            "db_advisors_result": "pass: no blocking advisors",
            "db_runtime_validation": "pass: affected runtime exercised",
        }

    def test_missing_and_false_guard_preserve_existing_classification(self) -> None:
        task = {
            "project_id": "nuvyr",
            "status": "review",
            "related_paths": list(self.GH_1453_MIGRATIONS),
            "db_impact": "none",
            "db_schema_change_detected": True,
        }
        projects = {
            "nuvyr": {"id": "nuvyr", "db": {}},
            "amiga": self.project("amiga"),
        }
        for project in (
            projects["nuvyr"],
            self.project("nuvyr", False),
        ):
            with self.subTest(project=project):
                projects["nuvyr"] = project
                with patch.object(task_contract, "get_project", side_effect=projects.get):
                    errors, summary = task_contract.validate_db_contract(
                        task,
                        "",
                        stage="review",
                    )
                self.assertEqual(errors, [])
                self.assertFalse(summary["production_schema_guard"]["enabled"])

    def test_present_non_boolean_guard_fails_every_transition_stage(self) -> None:
        task = {
            "project_id": "nuvyr",
            "status": "review",
            "db_impact": "none",
            "db_schema_change_detected": False,
        }
        for raw_guard in ("true", 1, 0, None, []):
            for stage in ("assignment", "review", "pr", "done"):
                with self.subTest(raw_guard=raw_guard, stage=stage):
                    with patch.object(
                        task_contract,
                        "get_project",
                        return_value=self.project("nuvyr", raw_guard),
                    ):
                        errors, summary = task_contract.validate_db_contract(
                            task,
                            "",
                            stage=stage,
                            transition=stage == "done",
                        )
                    rendered = "\n".join(errors)
                    self.assertIn("Project 'nuvyr'", rendered)
                    self.assertIn("`db.production_schema_guard`", rendered)
                    self.assertIn("projects.json", rendered)
                    self.assertFalse(summary["production_schema_guard"]["enabled"])

    def test_guard_is_generic_exact_project_scoped_and_never_inherited(self) -> None:
        projects = {
            "amiga": self.project("amiga", False),
            "nuvyr": self.project("nuvyr", True),
        }
        task = {
            "status": "review",
            "related_paths": ["db/schema.sql"],
            "db_impact": "none",
            "db_schema_change_detected": True,
        }
        with patch.object(task_contract, "get_project", side_effect=projects.get) as resolver:
            amiga_errors, _ = task_contract.validate_db_contract(
                {**task, "project_id": "amiga"},
                "",
                stage="review",
            )
            nuvyr_errors, _ = task_contract.validate_db_contract(
                {**task, "project_id": "nuvyr"},
                "",
                stage="review",
            )
            missing_errors, missing_summary = task_contract.validate_task_contract(
                task,
                "",
                stage="review",
            )
            empty_errors, empty_summary = task_contract.validate_task_contract(
                {**task, "project_id": ""},
                "",
                stage="review",
            )
            foreign_errors, foreign_summary = task_contract.validate_task_contract(
                {**task, "project_id": "foreign"},
                "",
                stage="review",
            )

        self.assertEqual(amiga_errors, [])
        self.assertTrue(any("cannot set `db_impact: none`" in error for error in nuvyr_errors))
        for errors, summary in (
            (missing_errors, missing_summary),
            (empty_errors, empty_summary),
            (foreign_errors, foreign_summary),
        ):
            self.assertTrue(errors)
            self.assertFalse(summary["db"]["production_schema_guard"]["enabled"])
            self.assertEqual(summary["db"]["db_project_ref"], "")
        self.assertEqual(
            [call.args[0] for call in resolver.call_args_list],
            ["amiga", "nuvyr", "foreign"],
        )

    def test_wrong_id_project_override_is_unknown_and_cannot_inherit_db_config(self) -> None:
        wrong_project = {
            "id": "amiga",
            "db": {
                "production_schema_guard": True,
                "shared_supabase_project_ref": "wrong-amiga-ref-canary",
                "required_surfaces": ["wrong_amiga_db.canary"],
            },
        }
        task = {
            "project_id": "nuvyr",
            "status": "pending",
            "related_paths": ["db/schema.sql"],
            "db_impact": "shared-supabase-required",
            "db_schema_change_detected": True,
        }

        synced, _changed = task_contract.sync_db_contract(
            task,
            "",
            project_override=wrong_project,
        )
        errors, summary = task_contract.validate_task_contract(
            task,
            "",
            stage="assignment",
            project_override=wrong_project,
        )

        rendered = "\n".join(errors)
        self.assertIn("Task references unknown `project_id: nuvyr`.", errors)
        self.assertFalse(summary["project"]["registered"])
        self.assertFalse(summary["db"]["production_schema_guard"]["configured"])
        self.assertFalse(summary["db"]["production_schema_guard"]["enabled"])
        self.assertEqual(summary["db"]["db_project_ref"], "")
        self.assertIsNone(synced["db_project_ref"])
        self.assertEqual(synced["db_required_surfaces"], [])
        self.assertIn("configure `db.shared_supabase_project_ref`", rendered)
        self.assertIn("configure project `db.required_surfaces`", rendered)
        self.assertNotIn("wrong-amiga-ref-canary", rendered)
        self.assertNotIn("wrong_amiga_db.canary", rendered)

    def test_whitespace_modified_project_id_cannot_inherit_exact_override(self) -> None:
        wrong_project = {
            "id": "amiga",
            "db": {
                "production_schema_guard": True,
                "shared_supabase_project_ref": "whitespace-ref-canary",
                "required_surfaces": ["whitespace_surface.canary"],
            },
        }
        task = {
            "project_id": "amiga ",
            "status": "pending",
            "related_paths": ["db/schema.sql"],
            "db_impact": "shared-supabase-required",
            "db_schema_change_detected": True,
        }

        synced, _changed = task_contract.sync_db_contract(
            task,
            "",
            project_override=wrong_project,
        )
        errors, summary = task_contract.validate_task_contract(
            task,
            "",
            stage="assignment",
            project_override=wrong_project,
        )

        rendered = "\n".join(errors)
        self.assertIn("Task references unknown `project_id: amiga`.", errors)
        self.assertFalse(summary["project"]["registered"])
        self.assertFalse(summary["db"]["production_schema_guard"]["configured"])
        self.assertFalse(summary["db"]["production_schema_guard"]["enabled"])
        self.assertIsNone(synced["db_project_ref"])
        self.assertEqual(synced["db_required_surfaces"], [])
        self.assertNotIn("whitespace-ref-canary", rendered)
        self.assertNotIn("whitespace_surface.canary", rendered)

    def test_non_string_project_id_cannot_resolve_or_inherit_coerced_project(self) -> None:
        coerced_project = {
            "id": "17",
            "db": {
                "production_schema_guard": True,
                "shared_supabase_project_ref": "coerced-ref-canary",
                "required_surfaces": ["coerced_surface.canary"],
            },
        }
        task = {
            "project_id": 17,
            "status": "pending",
            "related_paths": ["db/schema.sql"],
            "db_impact": "shared-supabase-required",
            "db_schema_change_detected": True,
        }

        with patch.object(task_contract, "get_project", return_value=coerced_project) as resolver:
            synced, _changed = task_contract.sync_task_contract(task, "")
            errors, summary = task_contract.validate_task_contract(
                task,
                "",
                stage="assignment",
            )

        rendered = "\n".join(errors)
        resolver.assert_not_called()
        self.assertIn("Task references unknown `project_id: 17`.", errors)
        self.assertFalse(summary["project"]["registered"])
        self.assertFalse(summary["db"]["production_schema_guard"]["configured"])
        self.assertFalse(summary["db"]["production_schema_guard"]["enabled"])
        self.assertIsNone(synced["db_project_ref"])
        self.assertEqual(synced["db_required_surfaces"], [])
        self.assertNotIn("coerced-ref-canary", rendered)
        self.assertNotIn("coerced_surface.canary", rendered)

    def test_concrete_schema_paths_defeat_manual_false_but_body_prose_does_not(self) -> None:
        project = self.project("nuvyr")
        for path in (*self.GH_1453_MIGRATIONS, "db/migrations/", "db/schema.sql"):
            with self.subTest(path=path), patch.object(
                task_contract,
                "get_project",
                return_value=project,
            ):
                synced, _changed = task_contract.sync_db_contract(
                    {
                        "project_id": "nuvyr",
                        "related_paths": [path],
                        "db_impact": "shared-supabase-required",
                        "db_schema_change_detected": False,
                        "db_schema_change_detection": "manual_false",
                    },
                    "",
                )
            self.assertTrue(synced["db_schema_change_detected"])

        with patch.object(task_contract, "get_project", return_value=project):
            prose_only, _changed = task_contract.sync_db_contract(
                {
                    "project_id": "nuvyr",
                    "related_paths": ["docs/database-runbook.md"],
                    "db_impact": "none",
                    "db_schema_change_detected": False,
                    "db_schema_change_detection": "manual_false",
                },
                "Document migration, schema, table, and column evidence.",
            )
        self.assertFalse(prose_only["db_schema_change_detected"])

    def test_schema_change_rejects_none_and_requires_complete_exact_local_exception(self) -> None:
        project = self.project("amiga")
        base = {
            "project_id": "amiga",
            "status": "review",
            "related_paths": list(self.GH_1453_MIGRATIONS),
            "db_schema_change_detected": True,
        }
        with patch.object(task_contract, "get_project", return_value=project):
            none_errors, _ = task_contract.validate_db_contract(
                {**base, "db_impact": "none"},
                "",
                stage="review",
            )
        self.assertTrue(any("cannot set `db_impact: none`" in error for error in none_errors))

        complete = {
            **base,
            "db_impact": "local-schema-only",
            "db_local_schema_only_exception": "dev-only-non-production",
            "db_local_schema_only_exception_approved_by": "operator",
            "db_local_schema_only_exception_reason": "Disposable local fixture only.",
        }
        mutations = (
            ("db_local_schema_only_exception", None),
            ("db_local_schema_only_exception", "production"),
            ("db_local_schema_only_exception_approved_by", None),
            ("db_local_schema_only_exception_approved_by", "codex"),
            ("db_local_schema_only_exception_reason", None),
            ("db_local_schema_only_exception_reason", "   "),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value), patch.object(
                task_contract,
                "get_project",
                return_value=project,
            ):
                errors, _ = task_contract.validate_db_contract(
                    {**complete, field: value},
                    "",
                    stage="review",
                )
            self.assertTrue(any(field in error for error in errors), errors)

        with patch.object(task_contract, "get_project", return_value=project):
            errors, _ = task_contract.validate_db_contract(
                complete,
                "",
                stage="review",
            )
        self.assertEqual(errors, [])

    def test_guard_enforces_assignment_review_pr_and_done_but_grandfathers_done_history(self) -> None:
        project = self.project("nuvyr")
        invalid = {
            "project_id": "nuvyr",
            "status": "review",
            "related_paths": ["db/schema.sql"],
            "db_impact": "none",
            "db_schema_change_detected": False,
            "db_schema_change_detection": "manual_false",
        }
        for stage in ("assignment", "review", "pr", "done"):
            with self.subTest(stage=stage), patch.object(
                task_contract,
                "get_project",
                return_value=project,
            ):
                errors, _ = task_contract.validate_db_contract(
                    invalid,
                    "",
                    stage=stage,
                    transition=stage == "done",
                )
            self.assertTrue(any("cannot set `db_impact: none`" in error for error in errors))

        with patch.object(
            task_contract,
            "get_project",
            return_value=self.project("nuvyr", "malformed"),
        ):
            historical_errors, _ = task_contract.validate_db_contract(
                {**invalid, "status": "done"},
                "",
                stage="done",
            )
        self.assertEqual(historical_errors, [])

        historical_shared = self.shared_task("nuvyr")
        historical_shared["status"] = "done"
        for field in (
            "db_migration_files",
            "db_apply_result",
            "db_schema_assertion",
            "db_advisors_result",
            "db_runtime_validation",
        ):
            historical_shared[field] = [] if field == "db_migration_files" else ""
        with patch.object(task_contract, "get_project", return_value=project):
            historical_shared_errors, _ = task_contract.validate_db_contract(
                historical_shared,
                "",
                stage="done",
            )
        self.assertEqual(historical_shared_errors, [])

    def test_shared_database_evidence_is_required_at_review_pr_and_done(self) -> None:
        required_fields = (
            "db_migration_files",
            "db_apply_result",
            "db_schema_assertion",
            "db_advisors_result",
            "db_runtime_validation",
        )
        for project_id in ("amiga", "nuvyr"):
            project = self.project(project_id)
            complete = self.shared_task(project_id)
            complete.update(
                {
                    "db_local_schema_only_exception": "dev-only-non-production",
                    "db_local_schema_only_exception_approved_by": "operator",
                    "db_local_schema_only_exception_reason": "Does not waive shared evidence.",
                }
            )
            with patch.object(task_contract, "get_project", return_value=project):
                assignment_errors, _ = task_contract.validate_db_contract(
                    {**complete, **{field: None for field in required_fields}},
                    "",
                    stage="assignment",
                )
            self.assertEqual(assignment_errors, [])

            for stage in ("review", "pr", "done"):
                for field in required_fields:
                    with self.subTest(project_id=project_id, stage=stage, field=field):
                        invalid = dict(complete)
                        invalid[field] = [] if field == "db_migration_files" else ""
                        with patch.object(task_contract, "get_project", return_value=project):
                            errors, _ = task_contract.validate_db_contract(
                                invalid,
                                "",
                                stage=stage,
                                transition=stage == "done",
                            )
                        self.assertTrue(any(field in error for error in errors), errors)


class TaskContractProjectIdentityTest(unittest.TestCase):
    def test_missing_project_does_not_inherit_amiga_and_fails_validation(self) -> None:
        # #given
        frontmatter = {
            "ui_ux_lane": True,
            "ui_ux_mode": "docs_only",
            "required_design_docs": [task_contract.AMIGA_DESIGN_DOC],
            "db_impact": "shared-supabase-required",
            "db_project_ref": task_contract.AMIGA_SHARED_SUPABASE_PROJECT_REF,
            "db_required_surfaces": task_contract.AMIGA_SHARED_SUPABASE_REQUIRED_SURFACES,
        }
        body = "Update UI design docs and apply a shared database migration."

        # #when
        synced, _ = task_contract.sync_task_contract(frontmatter, body)
        errors, summary = task_contract.validate_task_contract(frontmatter, body, stage="plan")

        # #then
        self.assertEqual(synced["required_design_docs"], [])
        self.assertIsNone(synced["db_project_ref"])
        self.assertEqual(synced["db_required_surfaces"], ["supabase CLI"])
        self.assertIn("Task must set a registered `project_id`.", errors)
        self.assertEqual(summary["project"], {"project_id": None, "registered": False})

    def test_unknown_project_fails_high_level_validation(self) -> None:
        # #given
        frontmatter = {
            "project_id": "unknown",
            "ui_ux_lane": False,
            "db_impact": "none",
        }

        # #when
        with patch.object(task_contract, "get_project", return_value=None):
            errors, summary = task_contract.validate_task_contract(frontmatter, "Backend task.", stage="plan")

        # #then
        self.assertIn("Task references unknown `project_id: unknown`.", errors)
        self.assertEqual(summary["project"], {"project_id": "unknown", "registered": False})


class TaskContractDirectAppPolicyTest(unittest.TestCase):
    def enabled_project(self, **updates) -> dict:
        project = {"id": "amiga", "ui_ux": {"direct_app_only": True}}
        project.update(updates)
        return project

    def validate(self, frontmatter: dict, project: dict | None = None):
        configured_project = self.enabled_project() if project is None else project
        with patch.object(task_contract, "get_project", return_value=configured_project):
            return task_contract.validate_direct_app_policy(frontmatter)

    def test_missing_and_false_config_are_default_off(self) -> None:
        frontmatter = {
            "project_id": "other",
            "status": "open",
            "lane_type": "design-spec",
            "related_paths": ["design/surface.md"],
        }
        for project in (
            {"id": "other"},
            {"id": "other", "ui_ux": {}},
            {"id": "other", "ui_ux": {"direct_app_only": False}},
        ):
            with self.subTest(project=project):
                errors, summary = self.validate(frontmatter, project)
                self.assertEqual(errors, [])
                self.assertFalse(summary["enabled"])

    def test_present_non_boolean_config_fails_instead_of_disabling(self) -> None:
        frontmatter = {"project_id": "amiga", "status": "open"}

        errors, summary = self.validate(
            frontmatter,
            {"id": "amiga", "ui_ux": {"direct_app_only": "true"}},
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("malformed `ui_ux.direct_app_only`", errors[0])
        self.assertFalse(summary["enabled"])

    def test_policy_resolves_only_the_task_exact_project_id(self) -> None:
        projects = {
            "amiga": self.enabled_project(),
            "other": {"id": "other", "ui_ux": {"direct_app_only": False}},
        }
        with patch.object(task_contract, "get_project", side_effect=projects.get) as get_project:
            errors, _ = task_contract.validate_direct_app_policy(
                {
                    "project_id": "other",
                    "status": "open",
                    "lane_type": "design-spec",
                }
            )

        self.assertEqual(errors, [])
        get_project.assert_called_once_with("other")

    def test_forbidden_lane_types_are_normalized(self) -> None:
        for lane_type in (
            "design",
            "UI_SANDBOX",
            "surface-spec",
            "design handoff",
            "route-parity",
            "design-layout-plus-template-spec",
        ):
            with self.subTest(lane_type=lane_type):
                errors, _ = self.validate(
                    {
                        "project_id": "amiga",
                        "status": "open",
                        "lane_type": lane_type,
                    }
                )
                self.assertTrue(any("`lane_type`" in error for error in errors))

    def test_bare_template_is_rejected_but_template_implementation_is_allowed(self) -> None:
        rejected, _ = self.validate(
            {
                "project_id": "amiga",
                "status": "open",
                "lane_type": " TEMPLATE ",
            }
        )
        accepted, _ = self.validate(
            {
                "project_id": "amiga",
                "status": "open",
                "lane_type": "template-implementation",
            }
        )

        self.assertTrue(any("`lane_type`" in error for error in rejected))
        self.assertEqual(accepted, [])

    def test_named_path_pair_rejects_root_design_and_accepts_src_design(self) -> None:
        rejected, _ = self.validate(
            {
                "project_id": "amiga",
                "status": "open",
                "related_paths": ["design/surfaces/app.md"],
            }
        )
        accepted, _ = self.validate(
            {
                "project_id": "amiga",
                "status": "open",
                "related_paths": ["src/design/theme.ts"],
            }
        )

        self.assertTrue(any("repository-root `design/**`" in error for error in rejected))
        self.assertEqual(accepted, [])

    def test_absolute_paths_resolve_against_every_configured_repo_root(self) -> None:
        project = self.enabled_project(
            repos={
                "app": "/projects/amiga",
                "api": "/projects/amiga-api",
            }
        )
        for field in ("related_paths", "required_dependency_artifacts"):
            with self.subTest(field=field):
                rejected, _ = self.validate(
                    {
                        "project_id": "amiga",
                        "status": "open",
                        field: ["/projects/amiga-api/design/contracts/jobs.md"],
                    },
                    project,
                )
                accepted, _ = self.validate(
                    {
                        "project_id": "amiga",
                        "status": "open",
                        field: ["/projects/amiga/src/design/theme.ts"],
                    },
                    project,
                )
                foreign, _ = self.validate(
                    {
                        "project_id": "amiga",
                        "status": "open",
                        field: ["/projects/foreign/design/reference.md"],
                    },
                    project,
                )

                self.assertTrue(
                    any("/projects/amiga-api/design/contracts/jobs.md" in error for error in rejected)
                )
                self.assertEqual(accepted, [])
                self.assertEqual(foreign, [])

    def test_absolute_path_resolution_exceptions_name_field_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "loop-a"
            second = root / "loop-b"
            first.symlink_to(second)
            second.symlink_to(first)
            offending_path = str(first / "design" / "surface.md")
            project = self.enabled_project(repos={"app": str(root)})

            for field in ("related_paths", "required_dependency_artifacts"):
                with self.subTest(field=field):
                    errors, _ = self.validate(
                        {
                            "project_id": "amiga",
                            "status": "open",
                            field: [offending_path],
                        },
                        project,
                    )

                    self.assertTrue(
                        any(
                            f"cannot resolve `{field}` path {offending_path!r}" in error
                            for error in errors
                        ),
                        errors,
                    )

    def test_loop_probe_handles_old_runtime_and_new_runtime_exception_shapes(self) -> None:
        candidate = Path("/synthetic/loop/design/planned.md")
        for error in (RuntimeError("symlink loop"), OSError("too many symbolic links")):
            with self.subTest(error_type=type(error).__name__):
                with patch.object(Path, "resolve", side_effect=error):
                    resolved, resolution_error = task_contract._resolve_direct_app_path(candidate)

                self.assertIsNone(resolved)
                self.assertIn(str(error), resolution_error)

    def test_non_loop_symlinks_preserve_resolved_design_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design").mkdir()
            (root / "src" / "design").mkdir(parents=True)
            (root / "design-link").symlink_to(root / "design")
            (root / "src-design-link").symlink_to(root / "src" / "design")
            project = self.enabled_project(repos={"app": str(root)})

            for field in ("related_paths", "required_dependency_artifacts"):
                with self.subTest(field=field):
                    design_path = str(root / "design-link" / "planned.md")
                    source_path = str(root / "src-design-link" / "planned.ts")
                    rejected, _ = self.validate(
                        {"project_id": "amiga", "status": "open", field: [design_path]},
                        project,
                    )
                    accepted, _ = self.validate(
                        {"project_id": "amiga", "status": "open", field: [source_path]},
                        project,
                    )

                    self.assertTrue(
                        any(design_path in error and "repository-root `design/**`" in error for error in rejected),
                        rejected,
                    )
                    self.assertEqual(accepted, [])

    def test_relative_symlink_loop_names_field_and_offending_path_across_repo_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_repo = root / "app"
            second_repo = root / "api"
            first_repo.mkdir()
            second_repo.mkdir()
            (second_repo / "loop-a").symlink_to(second_repo / "loop-b")
            (second_repo / "loop-b").symlink_to(second_repo / "loop-a")
            offending_path = "loop-a/design/surface.md"
            project = self.enabled_project(
                repos={"app": str(first_repo), "api": str(second_repo)}
            )

            for field in ("related_paths", "required_dependency_artifacts"):
                with self.subTest(field=field):
                    errors, _ = self.validate(
                        {"project_id": "amiga", "status": "open", field: [offending_path]},
                        project,
                    )

                    self.assertTrue(
                        any(
                            f"cannot resolve `{field}` path {offending_path!r}" in error
                            for error in errors
                        ),
                        errors,
                    )

    def test_relative_symlink_aliases_preserve_root_design_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design").mkdir()
            (root / "src" / "design").mkdir(parents=True)
            (root / "design-link").symlink_to(root / "design")
            (root / "src-design-link").symlink_to(root / "src" / "design")
            project = self.enabled_project(repos={"app": str(root)})

            for field in ("related_paths", "required_dependency_artifacts"):
                with self.subTest(field=field):
                    rejected, _ = self.validate(
                        {
                            "project_id": "amiga",
                            "status": "open",
                            field: ["design-link/planned.md"],
                        },
                        project,
                    )
                    accepted, _ = self.validate(
                        {
                            "project_id": "amiga",
                            "status": "open",
                            field: ["src-design-link/planned.ts"],
                        },
                        project,
                    )

                    self.assertTrue(
                        any(
                            "design-link/planned.md" in error
                            and "repository-root `design/**`" in error
                            for error in rejected
                        ),
                        rejected,
                    )
                    self.assertEqual(accepted, [])

    def test_relative_nonexistent_paths_remain_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.enabled_project(repos={"app": str(root)})

            for field in ("related_paths", "required_dependency_artifacts"):
                for path in (
                    "future/components/planned.ts",
                    "src/design/planned.ts",
                    "docs/design/planned.md",
                ):
                    with self.subTest(field=field, path=path):
                        errors, _ = self.validate(
                            {"project_id": "amiga", "status": "open", field: [path]},
                            project,
                        )

                        self.assertEqual(errors, [])

    def test_relative_aliases_evaluate_every_configured_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_repo = root / "app"
            second_repo = root / "api"
            (first_repo / "src" / "design").mkdir(parents=True)
            (second_repo / "design").mkdir(parents=True)
            (first_repo / "shared-alias").symlink_to(first_repo / "src" / "design")
            (second_repo / "blocked-alias").symlink_to(second_repo / "design")
            project = self.enabled_project(
                repos={"app": str(first_repo), "api": str(second_repo)}
            )

            for field in ("related_paths", "required_dependency_artifacts"):
                with self.subTest(field=field):
                    accepted, _ = self.validate(
                        {
                            "project_id": "amiga",
                            "status": "open",
                            field: ["shared-alias/planned.ts"],
                        },
                        project,
                    )
                    rejected, _ = self.validate(
                        {
                            "project_id": "amiga",
                            "status": "open",
                            field: ["blocked-alias/planned.md"],
                        },
                        project,
                    )

                    self.assertEqual(accepted, [])
                    self.assertTrue(
                        any(
                            "blocked-alias/planned.md" in error
                            and "repository-root `design/**`" in error
                            for error in rejected
                        ),
                        rejected,
                    )

    def test_absolute_paths_fail_when_repo_mapping_cannot_be_resolved(self) -> None:
        repo_cases = (
            ("missing", {}),
            ("malformed-object", {"repos": []}),
            ("empty", {"repos": {}}),
            ("malformed-path", {"repos": {"app": 7}}),
            ("unresolvable", {"repos": {"app": "amiga"}}),
        )
        with patch.object(task_contract, "resolve_project_repo_path", return_value=None):
            for field in ("related_paths", "required_dependency_artifacts"):
                for label, update in repo_cases:
                    with self.subTest(field=field, repo_case=label):
                        project = self.enabled_project(**update)
                        errors, _ = self.validate(
                            {
                                "project_id": "amiga",
                                "status": "open",
                                field: ["/projects/amiga/design/surface.md"],
                            },
                            project,
                        )

                        self.assertTrue(
                            any(
                                field in error
                                and "projects.json `repos` is missing, empty, malformed, or unresolvable"
                                in error
                                for error in errors
                            ),
                            errors,
                        )

    def test_dependency_design_output_is_rejected_but_read_only_docs_are_not(self) -> None:
        frontmatter = {
            "project_id": "amiga",
            "status": "open",
            "dependency_materialization_gate": True,
            "required_dependency_artifacts": ["design/handoff/app.md"],
            "required_design_docs": ["/projects/amiga/design/reference.md"],
        }

        errors, _ = self.validate(frontmatter)

        self.assertEqual(len(errors), 1)
        self.assertIn("`required_dependency_artifacts`", errors[0])
        self.assertIn("`dependency_materialization_gate: true`", errors[0])
        self.assertNotIn("required_design_docs", errors[0])

    def test_complete_operator_legacy_maintenance_override_allows_violation(self) -> None:
        frontmatter = {
            "project_id": "amiga",
            "status": "in_progress",
            "lane_type": "design-spec",
            "related_paths": ["design/surface.md"],
            "direct_app_legacy_maintenance": True,
            "direct_app_legacy_maintenance_approved_by": "operator",
            "direct_app_legacy_maintenance_reason": "Maintain the accepted historical spec.",
        }

        errors, summary = self.validate(frontmatter)

        self.assertEqual(errors, [])
        self.assertTrue(summary["legacy_maintenance_override"])

    def test_each_incomplete_legacy_maintenance_override_fails_actionably(self) -> None:
        complete = {
            "project_id": "amiga",
            "status": "open",
            "lane_type": "design-spec",
            "direct_app_legacy_maintenance": True,
            "direct_app_legacy_maintenance_approved_by": "operator",
            "direct_app_legacy_maintenance_reason": "Maintain accepted history.",
        }
        cases = (
            ("direct_app_legacy_maintenance", False),
            ("direct_app_legacy_maintenance_approved_by", None),
            ("direct_app_legacy_maintenance_reason", ""),
        )
        for field, replacement in cases:
            with self.subTest(field=field):
                errors, _ = self.validate({**complete, field: replacement})
                self.assertTrue(any(f"`{field}" in error for error in errors))

    def test_done_history_is_grandfathered_without_override(self) -> None:
        errors, _ = self.validate(
            {
                "project_id": "amiga",
                "status": "done",
                "lane_type": "design-spec",
                "related_paths": ["design/surface.md"],
            }
        )

        self.assertEqual(errors, [])

    def test_done_history_is_grandfathered_even_when_policy_value_is_malformed(self) -> None:
        errors, summary = self.validate(
            {
                "project_id": "amiga",
                "status": "done",
                "lane_type": "design-spec",
                "related_paths": ["design/surface.md"],
            },
            {"id": "amiga", "ui_ux": {"direct_app_only": "true"}},
        )

        self.assertEqual(errors, [])
        self.assertTrue(summary["configured"])
        self.assertFalse(summary["enabled"])


class TaskCreationProjectTest(unittest.TestCase):
    def test_new_task_requires_project_argument(self) -> None:
        # #given
        command = [
            sys.executable,
            str(REPO_ROOT / "bin" / "new_task.py"),
            "--title",
            "Projectless task",
            "--created-by",
            "codex",
        ]

        # #when
        result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False)

        # #then
        self.assertEqual(result.returncode, 2)
        self.assertIn("--project", result.stderr)

    def test_new_task_direct_app_refusal_happens_before_write(self) -> None:
        args = SimpleNamespace(
            title="Create design sandbox",
            created_by="codex",
            requested_by="operator",
            owner="unassigned",
            priority="normal",
            status="open",
            project="amiga",
            repo_targets="app",
            path_targets="design/sandbox.md",
            related_chat=None,
            depends_on="",
            ui_ux_lane="false",
            skip_refinement=False,
        )
        stderr = io.StringIO()
        project = {"id": "amiga", "ui_ux": {"direct_app_only": True}}

        with patch.object(new_task, "parse_args", return_value=args):
            with patch.object(new_task, "agent_ids", return_value=["codex"]):
                with patch.object(new_task, "ensure_agent_enabled"):
                    with patch.object(new_task, "ensure_project"):
                        with patch.object(new_task, "task_id", return_value="TASK-DIRECT"):
                            with patch.object(task_contract, "get_project", return_value=project):
                                with patch.object(new_task, "write_file") as write_file:
                                    with patch("sys.stderr", stderr):
                                        with self.assertRaises(SystemExit):
                                            new_task.main()

        write_file.assert_not_called()
        self.assertIn("rejected task creation before write", stderr.getvalue())
        self.assertIn("design/sandbox.md", stderr.getvalue())

    def test_direct_in_progress_creation_validates_complete_assignment_contract_before_write(self) -> None:
        args = SimpleNamespace(
            title="Malformed production guard",
            created_by="codex",
            requested_by="operator",
            owner="unassigned",
            priority="normal",
            status="in_progress",
            project="amiga",
            repo_targets="app",
            path_targets="design/sandbox.md",
            related_chat=None,
            depends_on="",
            ui_ux_lane="false",
            skip_refinement=True,
        )
        stderr = io.StringIO()
        project = {
            "id": "amiga",
            "ui_ux": {"direct_app_only": True},
            "db": {"production_schema_guard": "true"},
        }

        with patch.object(new_task, "parse_args", return_value=args):
            with patch.object(new_task, "agent_ids", return_value=["codex"]):
                with patch.object(new_task, "ensure_agent_enabled"):
                    with patch.object(new_task, "ensure_project"):
                        with patch.object(new_task, "task_id", return_value="TASK-ASSIGN"):
                            with patch.object(task_contract, "get_project", return_value=project):
                                with patch.object(new_task, "write_file") as write_file:
                                    with patch("sys.stderr", stderr):
                                        with self.assertRaises(SystemExit):
                                            new_task.main()

        write_file.assert_not_called()
        self.assertIn("assignment contract rejected", stderr.getvalue())
        self.assertIn("malformed `db.production_schema_guard`", stderr.getvalue())
        self.assertIn("design/sandbox.md", stderr.getvalue())

    def test_open_creation_keeps_assignment_contract_deferred(self) -> None:
        args = SimpleNamespace(
            title="Open malformed production guard",
            created_by="codex",
            requested_by="operator",
            owner="unassigned",
            priority="normal",
            status="open",
            project="amiga",
            repo_targets="app",
            path_targets="",
            related_chat=None,
            depends_on="",
            ui_ux_lane="auto",
            skip_refinement=False,
        )
        project = {
            "id": "amiga",
            "db": {"production_schema_guard": "true"},
        }

        with patch.object(new_task, "parse_args", return_value=args):
            with patch.object(new_task, "agent_ids", return_value=["codex"]):
                with patch.object(new_task, "ensure_agent_enabled"):
                    with patch.object(new_task, "ensure_project"):
                        with patch.object(new_task, "task_id", return_value="TASK-OPEN"):
                            with patch.object(task_contract, "get_project", return_value=project):
                                with patch.object(new_task, "write_file") as write_file:
                                    with patch("sys.stdout", io.StringIO()):
                                        new_task.main()

        write_file.assert_called_once()

    def test_claim_direct_app_refusal_leaves_task_and_queue_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_path = Path(tmp) / "task.md"
            original = (
                "---\n"
                "task_id: TASK-DIRECT\n"
                "title: Direct app claim\n"
                "project_id: amiga\n"
                "status: open\n"
                "owner: unassigned\n"
                "created_by: codex\n"
                "skip_refinement: true\n"
                "lane_type: design-spec\n"
                "ui_ux_lane: false\n"
                "db_impact: none\n"
                "---\n"
                "# Direct app claim\n"
            )
            task_path.write_text(original)
            args = SimpleNamespace(
                task="TASK-DIRECT",
                owner="codex",
                status="in_progress",
                branch=None,
                note=None,
                skip_preflight=True,
                allow_queue_override=False,
                accepted_by=None,
                accepted_note=None,
                allow_self_plan=False,
                released_by=None,
                release_evidence=None,
            )
            project = {"id": "amiga", "ui_ux": {"direct_app_only": True}}
            stderr = io.StringIO()

            with patch.object(claim_task, "parse_args", return_value=args):
                with patch.object(claim_task, "agent_ids", return_value=["codex"]):
                    with patch.object(claim_task, "ensure_agent_enabled"):
                        with patch.object(claim_task, "find_task_by_id", return_value=task_path):
                            with patch.object(claim_task.issue_queue, "queue_exists", return_value=False):
                                with patch.object(task_contract, "get_project", return_value=project):
                                    with patch.object(claim_task, "write_file") as write_file:
                                        with patch.object(
                                            claim_task.issue_queue,
                                            "mark_lane_transition",
                                        ) as mark_lane_transition:
                                            with patch("sys.stderr", stderr):
                                                with self.assertRaises(SystemExit):
                                                    claim_task.main()

            self.assertEqual(task_path.read_text(), original)
            write_file.assert_not_called()
            mark_lane_transition.assert_not_called()
            self.assertIn("direct-app policy rejects target status", stderr.getvalue())
            self.assertIn("`lane_type` 'design-spec'", stderr.getvalue())

    def test_claim_done_design_history_uses_every_non_done_target_before_any_mutation(self) -> None:
        for target_status in ("open", "blocked", "in_progress", "review"):
            with self.subTest(target_status=target_status):
                with tempfile.TemporaryDirectory() as tmp:
                    task_path = Path(tmp) / "task.md"
                    original = (
                        "---\n"
                        "task_id: TASK-DONE-DESIGN\n"
                        "title: Historical design task\n"
                        "project_id: amiga\n"
                        "status: done\n"
                        "owner: unassigned\n"
                        "created_by: codex\n"
                        "skip_refinement: true\n"
                        "lane_type: design-spec\n"
                        "ui_ux_lane: false\n"
                        "db_impact: none\n"
                        "---\n"
                        "# Historical design task\n"
                    )
                    task_path.write_text(original)
                    args = SimpleNamespace(
                        task="TASK-DONE-DESIGN",
                        owner="codex",
                        status=target_status,
                        branch=None,
                        note=None,
                        skip_preflight=True,
                        allow_queue_override=False,
                        accepted_by=None,
                        accepted_note=None,
                        allow_self_plan=False,
                        released_by=None,
                        release_evidence=None,
                    )
                    project = {"id": "amiga", "ui_ux": {"direct_app_only": True}}
                    queue_payload = {
                        "project_id": "amiga",
                        "lanes": [
                            {
                                "order": 1,
                                "issue": 75,
                                "task_id": "TASK-DONE-DESIGN",
                                "queue_state": "ready",
                            }
                        ],
                    }
                    stderr = io.StringIO()

                    with patch.object(claim_task, "parse_args", return_value=args):
                        with patch.object(claim_task, "agent_ids", return_value=["codex"]):
                            with patch.object(claim_task, "ensure_agent_enabled"):
                                with patch.object(claim_task, "find_task_by_id", return_value=task_path):
                                    with patch.object(
                                        claim_task.issue_queue,
                                        "queue_exists",
                                        return_value=True,
                                    ):
                                        with patch.object(
                                            claim_task.issue_queue,
                                            "load_queue",
                                            return_value=queue_payload,
                                        ):
                                            with patch.object(task_contract, "get_project", return_value=project):
                                                with patch.object(claim_task, "write_file") as write_file:
                                                    with patch.object(
                                                        claim_task.issue_queue,
                                                        "mark_lane_transition",
                                                    ) as mark_lane_transition:
                                                        with patch("sys.stderr", stderr):
                                                            with self.assertRaises(SystemExit):
                                                                claim_task.main()

                    self.assertEqual(task_path.read_text(), original)
                    write_file.assert_not_called()
                    mark_lane_transition.assert_not_called()
                    self.assertIn("`lane_type` 'design-spec'", stderr.getvalue())

    def test_claim_non_done_target_keeps_default_off_and_non_design_controls_usable(self) -> None:
        cases = (
            (
                "default-off",
                {"id": "amiga", "ui_ux": {"direct_app_only": False}},
                "design-spec",
            ),
            (
                "non-design",
                {"id": "amiga", "ui_ux": {"direct_app_only": True}},
                "implementation",
            ),
        )
        for label, project, lane_type in cases:
            with self.subTest(control=label):
                with tempfile.TemporaryDirectory() as tmp:
                    task_path = Path(tmp) / "task.md"
                    task_path.write_text(
                        "---\n"
                        "task_id: TASK-CONTROL\n"
                        "title: Historical control task\n"
                        "project_id: amiga\n"
                        "status: done\n"
                        "owner: codex\n"
                        "created_by: codex\n"
                        "skip_refinement: true\n"
                        f"lane_type: {lane_type}\n"
                        "ui_ux_lane: false\n"
                        "db_impact: none\n"
                        "---\n"
                        "# Historical control task\n"
                    )
                    args = SimpleNamespace(
                        task="TASK-CONTROL",
                        owner="codex",
                        status="open",
                        branch=None,
                        note=None,
                        skip_preflight=True,
                        allow_queue_override=False,
                        accepted_by=None,
                        accepted_note=None,
                        allow_self_plan=False,
                        released_by=None,
                        release_evidence=None,
                    )

                    with patch.object(claim_task, "parse_args", return_value=args):
                        with patch.object(claim_task, "agent_ids", return_value=["codex"]):
                            with patch.object(claim_task, "ensure_agent_enabled"):
                                with patch.object(claim_task, "find_task_by_id", return_value=task_path):
                                    with patch.object(claim_task, "ROOT", Path(tmp)):
                                        with patch.object(claim_task, "target_task_path", return_value=task_path):
                                            with patch.object(claim_task.issue_queue, "queue_exists", return_value=False):
                                                with patch.object(task_contract, "get_project", return_value=project):
                                                    with patch.object(claim_task, "write_file") as write_file:
                                                        claim_task.main()

                    write_file.assert_called_once()
                    _, rendered = write_file.call_args.args
                    transitioned, _ = task_contract.parse_frontmatter(rendered)
                    self.assertEqual(transitioned["status"], "open")

    def test_claim_done_target_uses_release_closure_without_direct_app_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_path = Path(tmp) / "task.md"
            original = (
                "---\n"
                "task_id: TASK-RELEASE\n"
                "title: Historical design release\n"
                "project_id: amiga\n"
                "status: review\n"
                "owner: codex\n"
                "created_by: codex\n"
                "lane_type: design-spec\n"
                "---\n"
                "# Historical design release\n"
            )
            task_path.write_text(original)
            args = SimpleNamespace(
                task="TASK-RELEASE",
                owner="codex",
                status="done",
                branch=None,
                note=None,
                skip_preflight=True,
                allow_queue_override=False,
                accepted_by=None,
                accepted_note=None,
                allow_self_plan=False,
                released_by="codex",
                release_evidence='{"merge_sha":"0123456789abcdef0123456789abcdef01234567","verdict":"success","run_id":1}',
            )
            stderr = io.StringIO()

            with patch.object(claim_task, "parse_args", return_value=args):
                with patch.object(claim_task, "agent_ids", return_value=["codex"]):
                    with patch.object(claim_task, "ensure_agent_enabled"):
                        with patch.object(claim_task, "find_task_by_id", return_value=task_path):
                            with patch.object(
                                claim_task,
                                "build_release_evidence_record",
                                side_effect=claim_task.ReleaseGateError("release closure refused"),
                            ) as release_gate:
                                with patch.object(
                                    claim_task,
                                    "validate_direct_app_policy",
                                    return_value=(["historical design violation"], {}),
                                ) as direct_app:
                                    with patch("sys.stderr", stderr):
                                        with self.assertRaises(SystemExit):
                                            claim_task.main()

            self.assertEqual(task_path.read_text(), original)
            release_gate.assert_called_once()
            direct_app.assert_not_called()
            self.assertIn("release closure refused", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
