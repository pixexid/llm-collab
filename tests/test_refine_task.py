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

    def test_skip_refinement_bypasses_only_for_normalized_true(self) -> None:
        for value in (True, "true"):
            with self.subTest(value=value):
                self.assertEqual(
                    [],
                    refine_task.validate_refinement({"skip_refinement": value}, ""),
                )

        for value in (False, "false"):
            with self.subTest(value=value):
                errors = refine_task.validate_refinement({"skip_refinement": value}, "")
                self.assertIn("missing canonical section: ## Summary", errors)

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

    def test_ignores_commented_and_fenced_duplicate_canonical_sections(self) -> None:
        for heading in refine_task.CANONICAL_SECTIONS:
            for artifact in (
                f"<!--\n{heading}\n\nCommented example.\n-->",
                f"```markdown\n{heading}\n\nFenced example.\n```",
                f"~~~~\n{heading}\n\nFenced example.\n~~~~",
            ):
                with self.subTest(heading=heading, artifact=artifact):
                    body = task_body(extra=artifact)
                    self.assertEqual([], refine_task.validate_refinement({}, body))

    def test_commented_and_fenced_headings_cannot_satisfy_missing_sections(self) -> None:
        for heading in refine_task.CANONICAL_SECTIONS:
            for replacement in (
                f"<!-- {heading} -->",
                f"```markdown\n{heading}\n```",
            ):
                with self.subTest(heading=heading, replacement=replacement):
                    body = task_body().replace(heading, replacement, 1)
                    errors = refine_task.validate_refinement({}, body)
                    self.assertIn(f"missing canonical section: {heading}", errors)

    def test_accepts_real_content_with_inline_code_and_comments(self) -> None:
        values = {
            **RISK_VALUES,
            "Current file/topology reviewed:": (
                "Inspect `bin/refine_task.py` <!-- internal note --> on current main."
            ),
        }
        body = task_body(
            summary="Treat `## Summary` as inline code <!-- hidden note --> within real prose.",
            risk_values=values,
        )

        self.assertEqual([], refine_task.validate_refinement({}, body))

    def test_fence_content_does_not_start_an_html_comment_outside_the_fence(self) -> None:
        body = task_body(summary="```\n<!--\n```\nReal summary remains visible.")

        self.assertEqual([], refine_task.validate_refinement({}, body))

    def test_rejects_fenced_only_section_content(self) -> None:
        body = task_body(summary="```markdown\nExample text is not task content.\n```")

        errors = refine_task.validate_refinement({}, body)

        self.assertIn("empty or placeholder-only canonical section: ## Summary", errors)

    def test_rejects_blockquote_fenced_only_section_content(self) -> None:
        body = task_body(summary="> ```text\n> Example only\n> ````")

        errors = refine_task.validate_refinement({}, body)

        self.assertIn("empty or placeholder-only canonical section: ## Summary", errors)

    def test_nested_container_fences_do_not_hide_later_canonical_sections(self) -> None:
        artifacts = (
            "- ```markdown\n  ## Summary\n  Example only\n  ```",
            "> - ~~~markdown\n>   ## Summary\n>   Example only\n>   ~~~~",
            "- > ```text\n  > ## Summary\n  > Example only\n  > ````",
        )
        for artifact in artifacts:
            with self.subTest(artifact=artifact):
                body = task_body(summary=f"Real summary remains visible.\n\n{artifact}")
                self.assertEqual([], refine_task.validate_refinement({}, body))

    def test_nested_container_fences_cannot_supply_canonical_sections(self) -> None:
        body = """> - ```markdown
>   ## Summary
>   Example only
>   ## Acceptance Criteria
>   - [ ] Example only
>   ## Verification Plan
>   - [ ] Example only
>   ## Implementation Risk Analysis
>   Example only
>   ````
"""

        errors = refine_task.validate_refinement({}, body)

        for heading in refine_task.CANONICAL_SECTIONS:
            with self.subTest(heading=heading):
                self.assertIn(f"missing canonical section: {heading}", errors)

    def test_rejects_html_artifact_only_section_content(self) -> None:
        for artifact in (
            "<br>",
            "<br />",
            "<span></span>",
            '<span data-example=">"></span>',
        ):
            with self.subTest(artifact=artifact):
                errors = refine_task.validate_refinement({}, task_body(summary=artifact))
                self.assertIn(
                    "empty or placeholder-only canonical section: ## Summary",
                    errors,
                )

    def test_accepts_real_section_content_around_html_artifacts(self) -> None:
        for summary in ("Needs <br> review.", "<span>Needs review.</span>"):
            with self.subTest(summary=summary):
                self.assertEqual(
                    [],
                    refine_task.validate_refinement({}, task_body(summary=summary)),
                )

    def test_accepts_visible_blockquote_and_list_prose(self) -> None:
        for summary in (
            "> Real quoted task summary.",
            "- Real listed task summary.",
            "> - Real nested task summary.",
        ):
            with self.subTest(summary=summary):
                self.assertEqual(
                    [],
                    refine_task.validate_refinement({}, task_body(summary=summary)),
                )

    def test_unterminated_nested_fence_masks_the_remainder_fail_closed(self) -> None:
        body = task_body(summary="Real summary.\n\n> ```text\n> Unterminated example")

        errors = refine_task.validate_refinement({}, body)

        self.assertIn("missing canonical section: ## Acceptance Criteria", errors)
        self.assertIn("missing canonical section: ## Verification Plan", errors)
        self.assertIn("missing canonical section: ## Implementation Risk Analysis", errors)

    def test_markdown_mask_preserves_line_and_character_alignment(self) -> None:
        body = "> - ```text\r\n>   Example\r\n>   ````\r\n## Summary\r\nReal\r\n"

        masked = refine_task._mask_nonrendered_markdown(body)

        self.assertEqual(len(body), len(masked))
        self.assertEqual(
            [index for index, character in enumerate(body) if character in "\r\n"],
            [index for index, character in enumerate(masked) if character in "\r\n"],
        )
        self.assertIn("## Summary", masked)

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

    def test_commented_and_fenced_labels_cannot_satisfy_missing_values(self) -> None:
        for label in refine_task.RISK_REQUIRED_LABELS:
            values = {key: value for key, value in RISK_VALUES.items() if key != label}
            for artifact in (
                f"<!-- - {label} none -->",
                f"```\n- {label} none\n```",
                f"~~~text\n- {label} none\n~~~",
            ):
                with self.subTest(label=label, artifact=artifact):
                    errors = refine_task.validate_refinement(
                        {},
                        task_body(risk_values=values, extra=artifact),
                    )
                    self.assertIn(f"missing risk-analysis label: {label}", errors)

    def test_blockquote_fenced_labels_cannot_supply_risk_analysis(self) -> None:
        fenced_labels = "\n".join(
            f"> - {label} none" for label in refine_task.RISK_REQUIRED_LABELS
        )
        body = task_body(
            risk_values={},
            extra=f"> ```text\n{fenced_labels}\n> ``` ",
        )

        errors = refine_task.validate_refinement({}, body)

        for label in refine_task.RISK_REQUIRED_LABELS:
            with self.subTest(label=label):
                self.assertIn(f"missing risk-analysis label: {label}", errors)

    def test_rejects_each_absent_or_placeholder_only_required_value(self) -> None:
        for label in refine_task.RISK_REQUIRED_LABELS:
            for value in ("", "(required before refinement)", "placeholder"):
                with self.subTest(label=label, value=value):
                    values = {**RISK_VALUES, label: value}
                    errors = refine_task.validate_refinement({}, task_body(risk_values=values))
                    self.assertIn(f"unresolved risk-analysis value: {label}", errors)

    def test_rejects_checkbox_and_markdown_artifact_only_required_values(self) -> None:
        label = "Open decisions/blockers:"
        for value in ("- [ ]", "- [x]", "-", "**", "_", "` `"):
            with self.subTest(value=value):
                values = {**RISK_VALUES, label: value}
                errors = refine_task.validate_refinement({}, task_body(risk_values=values))
                self.assertIn(f"unresolved risk-analysis value: {label}", errors)

    def test_accepts_checkbox_prefixed_substantive_required_value(self) -> None:
        label = "Open decisions/blockers:"
        values = {**RISK_VALUES, label: "- [ ] Confirm the final reviewer."}

        self.assertEqual([], refine_task.validate_refinement({}, task_body(risk_values=values)))

    def test_rejects_html_artifact_only_required_value(self) -> None:
        label = "Open decisions/blockers:"
        for value in (
            "<br>",
            "<br />",
            "<span></span>",
            '<span data-example=">"></span>',
        ):
            with self.subTest(value=value):
                values = {**RISK_VALUES, label: value}
                errors = refine_task.validate_refinement({}, task_body(risk_values=values))
                self.assertIn(f"unresolved risk-analysis value: {label}", errors)

    def test_accepts_real_required_value_around_html_artifacts(self) -> None:
        label = "Open decisions/blockers:"
        values = {**RISK_VALUES, label: "Needs <br> review"}

        self.assertEqual([], refine_task.validate_refinement({}, task_body(risk_values=values)))

    def test_does_not_reject_real_value_that_mentions_placeholder(self) -> None:
        values = {
            **RISK_VALUES,
            "Open decisions/blockers:": "Remove the placeholder before refinement.",
        }

        self.assertEqual([], refine_task.validate_refinement({}, task_body(risk_values=values)))


class DesignThinkingValueValidationTest(unittest.TestCase):
    def test_blockquote_fenced_labels_cannot_supply_design_thinking_values(self) -> None:
        artifact = (
            "> ```text\n"
            f"> - {refine_task.DESIGN_THINKING_BUDGET_LABEL} 12\n"
            f"> - {refine_task.DESIGN_THINKING_SEEDS_LABEL} hierarchy and error states\n"
            "> ``` "
        )
        frontmatter = {
            "ui_ux_lane": True,
            "ui_ux_mode": "implementation",
            "design_thinking_polish_budget_loc": 12,
            "design_thinking_polish_seeds": ["hierarchy", "error states"],
        }

        errors = refine_task.validate_refinement(
            frontmatter,
            task_body(extra=artifact),
        )

        self.assertIn(
            f"missing risk-analysis label: {refine_task.DESIGN_THINKING_BUDGET_LABEL}",
            errors,
        )
        self.assertIn(
            f"missing risk-analysis label: {refine_task.DESIGN_THINKING_SEEDS_LABEL}",
            errors,
        )


if __name__ == "__main__":
    unittest.main()
