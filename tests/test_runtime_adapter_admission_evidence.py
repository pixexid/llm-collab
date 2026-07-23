"""Tests for deterministic Runtime Adapter request-admission evidence."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_admission_evidence import (
    ADMISSION_EVIDENCED,
    ARTIFACT_LABEL,
    AdmissionEvidenceFailure,
    build_admission_evidence,
)
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_requests import (
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
    MAX_IN_FLIGHT_REQUESTS,
    TOO_MANY_IN_FLIGHT,
    DeliveryRef,
    RequestPolicy,
    _METHOD_CAPACITIES,
)
from llm_collab.runtime_adapter_transport_evidence import build_transport_evidence


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_admission_evidence.py"
CLAIM_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
ADMISSION_KEYS = {
    "C625baded5cd3.1",
    "C69d1a7ac8fee.1",
    "C69d1a7ac8fee.2",
    "C69d1a7ac8fee.3",
    "Ca135a0f3d7e4.1",
    "Ca135a0f3d7e4.2",
    "Ca135a0f3d7e4.3",
}
BYTE_LENGTH_KEYS = {"C9614292c6ab1.1", "C3dc535246440.1"}


def _delivery(index: int) -> DeliveryRef:
    return DeliveryRef(
        session_ref={
            "workspace_id": "ws",
            "project_id": "amiga",
            "native_session_id": "native",
        },
        original_request_id=f"deliver-{index}",
        delivery_id=f"delivery-{index}",
        attempt_id=f"attempt-{index}",
    )


class RuntimeAdapterAdmissionEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_admission_evidence_is_distinct_and_covers_only_admission_rows(self) -> None:
        artifact = build_admission_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], "request_admission_policy")
        self.assertEqual(artifact["claim"], ADMISSION_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        self.assertEqual({clause["clause_key"] for clause in artifact["clauses"]}, ADMISSION_KEYS)
        self.assertTrue(
            all(clause["state"] == ADMISSION_EVIDENCED and clause["evidence"] == ARTIFACT_LABEL for clause in artifact["clauses"])
        )

    def test_total_32_is_emergent_from_method_pool_sum_and_behavior(self) -> None:
        artifact = build_admission_evidence(self.protocol)
        observation = artifact["observation"]

        self.assertEqual(sum(_METHOD_CAPACITIES.values()), MAX_IN_FLIGHT_REQUESTS)
        self.assertEqual(observation["capacity_sum"], MAX_IN_FLIGHT_REQUESTS)
        self.assertEqual(observation["max_in_flight_requests"], 32)
        self.assertEqual(observation["capacity_sum"], 32)
        self.assertEqual(observation["fill_to_capacity_count"], 32)
        self.assertEqual(observation["rejected_33rd_fault"], TOO_MANY_IN_FLIGHT)

    def test_per_method_pools_are_non_borrowable_in_both_directions(self) -> None:
        artifact = build_admission_evidence(self.protocol)
        observation = artifact["observation"]

        self.assertEqual(
            observation["method_cap_faults"],
            {
                METHOD_DELIVER: TOO_MANY_IN_FLIGHT,
                METHOD_CANCEL: TOO_MANY_IN_FLIGHT,
                METHOD_RECONCILE: TOO_MANY_IN_FLIGHT,
                METHOD_HEALTH: TOO_MANY_IN_FLIGHT,
                METHOD_SHUTDOWN: TOO_MANY_IN_FLIGHT,
            },
        )
        self.assertEqual(observation["health_full_deliver_free_fault"], TOO_MANY_IN_FLIGHT)
        self.assertEqual(observation["deliver_full_health_free_fault"], TOO_MANY_IN_FLIGHT)

        health_full = RequestPolicy()
        self.assertTrue(health_full.begin_request(METHOD_HEALTH, "health-1", received_at_ms=0).accepted)
        before = health_full.snapshot()
        refused_health = health_full.begin_request(METHOD_HEALTH, "health-2", received_at_ms=0)
        self.assertFalse(refused_health.accepted)
        self.assertEqual(refused_health.fault, TOO_MANY_IN_FLIGHT)
        self.assertEqual(health_full.snapshot(), before)
        free_delivery = _delivery(1)
        self.assertTrue(
            health_full.begin_request(
                METHOD_DELIVER,
                free_delivery.original_request_id,
                received_at_ms=0,
                delivery=free_delivery,
            ).accepted
        )

        delivery_full = RequestPolicy()
        for index in range(_METHOD_CAPACITIES[METHOD_DELIVER]):
            delivery = _delivery(index)
            self.assertTrue(
                delivery_full.begin_request(
                    METHOD_DELIVER,
                    delivery.original_request_id,
                    received_at_ms=0,
                    delivery=delivery,
                ).accepted
            )
        before = delivery_full.snapshot()
        over_cap = _delivery(999)
        refused_delivery = delivery_full.begin_request(
            METHOD_DELIVER,
            over_cap.original_request_id,
            received_at_ms=0,
            delivery=over_cap,
        )
        self.assertFalse(refused_delivery.accepted)
        self.assertEqual(refused_delivery.fault, TOO_MANY_IN_FLIGHT)
        self.assertEqual(delivery_full.snapshot(), before)
        self.assertNotIn(over_cap.original_request_id, delivery_full.snapshot().pending_deliveries)
        self.assertTrue(delivery_full.begin_request(METHOD_HEALTH, "health-free", received_at_ms=0).accepted)

    def test_over_cap_refusal_leaves_no_pending_delivery_or_inflight_growth(self) -> None:
        artifact = build_admission_evidence(self.protocol)
        observation = artifact["observation"]

        self.assertEqual(observation["rejected_delivery_pending_count"], _METHOD_CAPACITIES[METHOD_DELIVER])
        self.assertEqual(observation["rejected_delivery_in_flight_count"], _METHOD_CAPACITIES[METHOD_DELIVER])

    def test_build_claim_still_gaps_all_admission_rows(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        self.assertEqual(len(result.gaps), 111)
        self.assertLessEqual(ADMISSION_KEYS, {gap["clause_key"] for gap in result.gaps})

    def test_transport_evidence_still_covers_only_byte_length_rows(self) -> None:
        transport = build_transport_evidence(self.protocol)

        self.assertEqual({clause["clause_key"] for clause in transport["clauses"]}, BYTE_LENGTH_KEYS)

    def test_clause_text_drift_fails_closed(self) -> None:
        changed = self.protocol.replace(
            "Each second concurrent request for the same method",
            "Each additional concurrent request for the same method",
        )

        with self.assertRaisesRegex(AdmissionEvidenceFailure, "missing admission clause|stale admission clause"):
            build_admission_evidence(changed)

    def test_admission_module_and_claim_module_remain_disjoint(self) -> None:
        admission_tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        claim_tree = ast.parse(CLAIM_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(admission_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")
        for node in ast.walk(claim_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_admission_evidence" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_admission_evidence")


if __name__ == "__main__":
    unittest.main()
