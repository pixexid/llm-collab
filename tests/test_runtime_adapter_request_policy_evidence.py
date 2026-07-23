"""Tests for deterministic Runtime Adapter request-policy cancellation evidence."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_admission_evidence import build_admission_evidence
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_manifest_evidence import build_manifest_evidence
from llm_collab.runtime_adapter_request_policy_evidence import (
    ARTIFACT_LABEL,
    CANCELLATION_EVIDENCED,
    RequestPolicyEvidenceFailure,
    build_request_policy_cancellation_evidence,
)
from llm_collab.runtime_adapter_requests import INVALID_DELIVERY, RECONCILIATION_REQUIRED, REQUEST_CANCELLED
from llm_collab.runtime_adapter_transport_evidence import build_transport_evidence


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_request_policy_evidence.py"
CLAIM_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
CANCELLATION_KEYS = {
    "C8fc80ae367f5.1",
    "Ce2a523abc63e.1",
    "C3d72f2d559af.1",
    "C5bdcee0ed51e.1",
    "C95661fc1714b.1",
    "C6a5b4ceb5c98.1",
    "C6a5b4ceb5c98.2",
}
DEFERRED_C09_KEYS = {"C41a1a5829726.1"}


class RuntimeAdapterRequestPolicyEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_cancellation_evidence_is_distinct_and_covers_only_c09_policy_rows(self) -> None:
        artifact = build_request_policy_cancellation_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], "request_policy_cancellation")
        self.assertEqual(artifact["claim"], CANCELLATION_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        covered = {clause["clause_key"] for clause in artifact["clauses"]}
        self.assertEqual(covered, CANCELLATION_KEYS)
        self.assertFalse(covered & DEFERRED_C09_KEYS)
        self.assertTrue(
            all(
                clause["state"] == CANCELLATION_EVIDENCED and clause["evidence"] == ARTIFACT_LABEL
                for clause in artifact["clauses"]
            )
        )

    def test_valid_cancel_success_is_non_vacuous_and_closed(self) -> None:
        observation = build_request_policy_cancellation_evidence(self.protocol)["observation"]

        self.assertTrue(observation["clean_non_acceptance_succeeded"])
        self.assertEqual(observation["success_original_request_id"], "orig-1")
        self.assertEqual(observation["success_delivery_id"], "delivery-1")
        self.assertEqual(observation["success_attempt_id"], "attempt-1")
        self.assertEqual(observation["success_status"], "cancelled")
        self.assertEqual(observation["success_original_fault"], REQUEST_CANCELLED)
        self.assertTrue(observation["success_state_advanced"])
        self.assertTrue(observation["pending_removed_after_success"])

    def test_live_pending_identity_mismatches_refuse_without_mutating_or_removing_pending(self) -> None:
        observation = build_request_policy_cancellation_evidence(self.protocol)["observation"]

        self.assertEqual(
            observation["live_mismatch_faults"],
            {
                "session_ref": INVALID_DELIVERY,
                "original_request_id": INVALID_DELIVERY,
                "delivery_id": INVALID_DELIVERY,
                "attempt_id": INVALID_DELIVERY,
            },
        )
        self.assertEqual(
            observation["live_mismatch_no_mutation"],
            {
                "session_ref": True,
                "original_request_id": True,
                "delivery_id": True,
                "attempt_id": True,
            },
        )
        self.assertEqual(
            observation["live_mismatch_pending_preserved"],
            {
                "session_ref": True,
                "original_request_id": True,
                "delivery_id": True,
                "attempt_id": True,
            },
        )

    def test_key_scoped_idempotency_only_reuses_the_exact_terminal_cancel_key(self) -> None:
        observation = build_request_policy_cancellation_evidence(self.protocol)["observation"]

        self.assertTrue(observation["exact_repeat_same_terminal_result"])
        self.assertEqual(
            observation["terminal_mismatch_faults"],
            {
                "session_ref": INVALID_DELIVERY,
                "original_request_id": INVALID_DELIVERY,
                "delivery_id": INVALID_DELIVERY,
                "attempt_id": INVALID_DELIVERY,
            },
        )
        self.assertEqual(
            observation["terminal_mismatch_no_mutation"],
            {
                "session_ref": True,
                "original_request_id": True,
                "delivery_id": True,
                "attempt_id": True,
            },
        )

    def test_possible_acceptance_requires_reconciliation_and_preserves_pending(self) -> None:
        observation = build_request_policy_cancellation_evidence(self.protocol)["observation"]

        self.assertEqual(observation["acceptance_fault"], RECONCILIATION_REQUIRED)
        self.assertTrue(observation["acceptance_unresolved"])
        self.assertFalse(observation["acceptance_ok"])
        self.assertTrue(observation["pending_preserved_on_reconciliation"])
        self.assertTrue(observation["unresolved_preserved_on_reconciliation"])

    def test_build_claim_still_gaps_cancellation_rows_without_global_count_pin(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertLessEqual(CANCELLATION_KEYS, gap_keys)

    def test_cancellation_ledger_is_scoped_disjoint_from_existing_ledgers(self) -> None:
        cancellation = build_request_policy_cancellation_evidence(self.protocol)
        transport = build_transport_evidence(self.protocol)
        admission = build_admission_evidence(self.protocol)
        manifest = build_manifest_evidence(self.protocol)

        cancellation_keys = {clause["clause_key"] for clause in cancellation["clauses"]}
        for name, artifact in (
            ("transport", transport),
            ("admission", admission),
            ("manifest", manifest),
        ):
            with self.subTest(name=name):
                other_keys = {clause["clause_key"] for clause in artifact["clauses"]}
                self.assertFalse(cancellation_keys & other_keys)
                self.assertFalse(other_keys & cancellation_keys)

    def test_clause_text_drift_fails_closed_for_all_cancellation_groups(self) -> None:
        replacements = (
            (
                "The `runtime.cancel` method MUST name the exact\n   original delivery request",
                "The `runtime.cancel` method MUST select the exact\n   original delivery request",
            ),
            (
                "The cancel\n   invocation's only success result is the closed",
                "The cancel\n   invocation's only successful result is the closed",
            ),
            (
                "adapter MUST refuse cancellation unless both its complete `SessionRefV1`",
                "adapter MUST reject cancellation unless both its complete `SessionRefV1`",
            ),
            (
                "original pending delivery request MUST terminate with\n   the `REQUEST_CANCELLED`",
                "original pending delivery request MUST finish with\n   the `REQUEST_CANCELLED`",
            ),
            (
                "Cancellation is idempotent only for the same exact `SessionRefV1`",
                "Cancellation is repeatable only for the same exact `SessionRefV1`",
            ),
            (
                "Cancellation MUST NOT claim success when acceptance may have\n   occurred",
                "Cancellation MUST NOT report success when acceptance may have\n   occurred",
            ),
        )
        for old, new in replacements:
            with self.subTest(old=old):
                changed = self.protocol.replace(old, new)
                self.assertNotEqual(changed, self.protocol)
                with self.assertRaisesRegex(
                    RequestPolicyEvidenceFailure,
                    "missing cancellation clause|stale cancellation clause",
                ):
                    build_request_policy_cancellation_evidence(changed)

    def test_cancellation_module_and_claim_module_remain_disjoint(self) -> None:
        cancellation_tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        claim_tree = ast.parse(CLAIM_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(cancellation_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")
        for node in ast.walk(claim_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_request_policy_evidence" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_request_policy_evidence")


if __name__ == "__main__":
    unittest.main()
