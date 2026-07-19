from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import plan_task
import refine_task


RISK_VALUES = {
    "Current file/topology reviewed:": "bin/refine_task.py owns the validator.",
    "Scope split decision:": "Keep the validator and its focused tests together.",
    "Estimated diff/risk:": "Small diff with a universal authoring-gate blast radius.",
    "Verification/browser/sign-off plan:": "Run focused and full unit tests; no browser applies.",
    "Open decisions/blockers:": "none",
}


def task_body(
    *,
    summary: str = "Reject hollow task contracts.",
    acceptance_criteria: str = "- [ ] Reject the empty template.",
    verification_plan: str = "- [ ] Run the focused unit tests.",
    risk_values: dict[str, str] | None = None,
    extra: str = "",
) -> str:
    values = RISK_VALUES if risk_values is None else risk_values
    risk_lines = "\n".join(f"- {label} {value}" for label, value in values.items())
    return f"""# Harden refinement

## Summary

{summary}

## Acceptance Criteria

{acceptance_criteria}

## Verification Plan

{verification_plan}

## Implementation Risk Analysis

{risk_lines}

{extra}"""


class CanonicalTaskSectionValidationTest(unittest.TestCase):
    def test_accepts_concise_real_task(self) -> None:
        for project_id in ("amiga", "nuvyr"):
            with self.subTest(project_id=project_id):
                errors = refine_task.validate_refinement({"project_id": project_id}, task_body())
                self.assertEqual([], errors)

    def test_accepts_mixed_checked_and_unchecked_criteria(self) -> None:
        body = task_body(
            acceptance_criteria="- [x] Existing behavior reproduced.\n- [ ] Add the regression test."
        )

        self.assertEqual([], refine_task.validate_refinement({}, body))

    def test_accepts_extra_non_canonical_sections(self) -> None:
        body = task_body(extra="## Notes\n\nKeep this change narrow.\n\n## Activity Log\n\n- Task created")

        self.assertEqual([], refine_task.validate_refinement({}, body))

    def test_plan_task_uses_the_same_validation_entrypoint(self) -> None:
        self.assertIs(plan_task.main, refine_task.main)

    def test_skip_refinement_bypasses_all_body_validation(self) -> None:
        errors = refine_task.validate_refinement({"skip_refinement": True}, "")

        self.assertEqual([], errors)

    def test_rejects_missing_canonical_sections(self) -> None:
        body = task_body().replace("## Verification Plan", "## Testing")

        errors = refine_task.validate_refinement({}, body)

        self.assertIn("missing canonical section: ## Verification Plan", errors)

    def test_rejects_empty_summary(self) -> None:
        errors = refine_task.validate_refinement({}, task_body(summary=""))

        self.assertIn("empty or placeholder-only canonical section: ## Summary", errors)

    def test_rejects_literal_placeholder_summary(self) -> None:
        errors = refine_task.validate_refinement({}, task_body(summary="(describe the task)"))

        self.assertIn("empty or placeholder-only canonical section: ## Summary", errors)

    def test_rejects_empty_or_placeholder_only_acceptance_criteria(self) -> None:
        for content in ("", "- [ ]", "- [x]", "- placeholder"):
            with self.subTest(content=content):
                errors = refine_task.validate_refinement({}, task_body(acceptance_criteria=content))
                self.assertIn(
                    "empty or placeholder-only canonical section: ## Acceptance Criteria",
                    errors,
                )

    def test_rejects_empty_or_placeholder_only_verification_plan(self) -> None:
        for content in ("", "- [ ]", "- [x]", "- TBD"):
            with self.subTest(content=content):
                errors = refine_task.validate_refinement({}, task_body(verification_plan=content))
                self.assertIn(
                    "empty or placeholder-only canonical section: ## Verification Plan",
                    errors,
                )

    def test_rejects_every_duplicate_canonical_section(self) -> None:
        body = task_body()
        for heading in refine_task.CANONICAL_SECTIONS:
            with self.subTest(heading=heading):
                duplicated = f"{body}\n\n{heading}\n\nDuplicate content."
                errors = refine_task.validate_refinement({}, duplicated)
                self.assertIn(f"duplicate canonical section: {heading} (found 2)", errors)

    def test_rejects_structurally_truncated_body(self) -> None:
        body = task_body().split("## Implementation Risk Analysis", 1)[0]

        errors = refine_task.validate_refinement({}, body)

        self.assertIn("missing canonical section: ## Implementation Risk Analysis", errors)


class RiskAnalysisValueValidationTest(unittest.TestCase):
    def test_rejects_each_missing_required_label(self) -> None:
        for label in refine_task.RISK_REQUIRED_LABELS:
            with self.subTest(label=label):
                values = {key: value for key, value in RISK_VALUES.items() if key != label}
                errors = refine_task.validate_refinement({}, task_body(risk_values=values))
                self.assertIn(f"missing risk-analysis label: {label}", errors)

    def test_rejects_each_absent_or_placeholder_only_required_value(self) -> None:
        for label in refine_task.RISK_REQUIRED_LABELS:
            for value in ("", "(required before refinement)", "placeholder"):
                with self.subTest(label=label, value=value):
                    values = {**RISK_VALUES, label: value}
                    errors = refine_task.validate_refinement({}, task_body(risk_values=values))
                    self.assertIn(f"unresolved risk-analysis value: {label}", errors)

    def test_does_not_reject_real_value_that_mentions_placeholder(self) -> None:
        values = {
            **RISK_VALUES,
            "Open decisions/blockers:": "Remove the placeholder before refinement.",
        }

        self.assertEqual([], refine_task.validate_refinement({}, task_body(risk_values=values)))


if __name__ == "__main__":
    unittest.main()
