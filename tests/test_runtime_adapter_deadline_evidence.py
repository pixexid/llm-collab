"""Tests for deterministic Runtime Adapter request-policy deadline evidence."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_admission_evidence import build_admission_evidence
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_deadline_evidence import (
    ARTIFACT_LABEL,
    DEADLINE_EVIDENCED,
    DEFERRED_DEADLINE_KEYS,
    DeadlineEvidenceFailure,
    build_deadline_evidence,
)
from llm_collab.runtime_adapter_manifest_evidence import build_manifest_evidence
from llm_collab.runtime_adapter_request_policy_evidence import build_request_policy_cancellation_evidence
from llm_collab.runtime_adapter_requests import (
    HANDSHAKE_DEADLINE_MS,
    HANDSHAKE_TIMEOUT,
    POST_INITIALIZE_METHODS,
    REQUEST_TIMEOUT,
    deadline_for_method,
)
from llm_collab.runtime_adapter_transport_evidence import build_transport_evidence


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_deadline_evidence.py"
CLAIM_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
DEADLINE_KEYS = {
    "Ce54676312948.1",
    "Cf67f5a54aadc.1",
    "Cf67f5a54aadc.2",
    "Cf67f5a54aadc.3",
}


class RuntimeAdapterDeadlineEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_deadline_evidence_is_distinct_and_covers_only_expiry_return_rows(self) -> None:
        artifact = build_deadline_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], "request_policy_deadline")
        self.assertEqual(artifact["claim"], DEADLINE_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        covered = {clause["clause_key"] for clause in artifact["clauses"]}
        self.assertEqual(covered, DEADLINE_KEYS)
        self.assertFalse(covered & DEFERRED_DEADLINE_KEYS)
        self.assertTrue(
            all(
                clause["state"] == DEADLINE_EVIDENCED and clause["evidence"] == ARTIFACT_LABEL
                for clause in artifact["clauses"]
            )
        )

    def test_request_deadline_probe_is_both_edge_and_method_set_derived(self) -> None:
        observation = build_deadline_evidence(self.protocol)["observation"]["request"]
        expected_deadlines = {method: deadline_for_method(method) for method in POST_INITIALIZE_METHODS}

        self.assertEqual(observation["method_deadlines"], expected_deadlines)
        self.assertEqual(set(observation["before_deadline_expired"]), set(POST_INITIALIZE_METHODS))
        self.assertEqual(set(observation["at_deadline_expired"]), set(POST_INITIALIZE_METHODS))
        self.assertFalse(any(observation["before_deadline_expired"].values()))
        self.assertTrue(all(observation["at_deadline_expired"].values()))
        self.assertEqual(
            observation["at_deadline_faults"],
            {method: REQUEST_TIMEOUT for method in POST_INITIALIZE_METHODS},
        )
        self.assertEqual(
            observation["unresolved_request_ids"],
            {method: f"{method}:deadline-probe" for method in POST_INITIALIZE_METHODS},
        )
        self.assertEqual(
            observation["automatic_retry"],
            {method: False for method in POST_INITIALIZE_METHODS},
        )

    def test_handshake_deadline_probe_is_both_edge_and_closes_on_expiry(self) -> None:
        observation = build_deadline_evidence(self.protocol)["observation"]["handshake"]

        self.assertEqual(observation["deadline_ms"], HANDSHAKE_DEADLINE_MS)
        self.assertFalse(observation["before_deadline_expired"])
        self.assertTrue(observation["at_deadline_expired"])
        self.assertEqual(observation["at_deadline_fault"], HANDSHAKE_TIMEOUT)
        self.assertTrue(observation["should_close"])

    def test_build_claim_still_gaps_deadline_rows_and_deferred_universal_row(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertLessEqual(DEADLINE_KEYS, gap_keys)
        self.assertLessEqual(DEFERRED_DEADLINE_KEYS, gap_keys)

    def test_deadline_ledger_is_scoped_disjoint_from_existing_ledgers(self) -> None:
        deadline = build_deadline_evidence(self.protocol)
        transport = build_transport_evidence(self.protocol)
        admission = build_admission_evidence(self.protocol)
        manifest = build_manifest_evidence(self.protocol)
        cancellation = build_request_policy_cancellation_evidence(self.protocol)

        deadline_keys = {clause["clause_key"] for clause in deadline["clauses"]}
        for name, artifact in (
            ("transport", transport),
            ("admission", admission),
            ("manifest", manifest),
            ("cancellation", cancellation),
        ):
            with self.subTest(name=name):
                other_keys = {clause["clause_key"] for clause in artifact["clauses"]}
                self.assertFalse(deadline_keys & other_keys)
                self.assertFalse(other_keys & deadline_keys)

    def test_clause_text_drift_fails_closed_for_request_and_handshake_rows(self) -> None:
        replacements = (
            (
                "Handshake expiry MUST return `HANDSHAKE_TIMEOUT`",
                "Handshake expiry MUST produce `HANDSHAKE_TIMEOUT`",
            ),
            (
                "Request expiry MUST return\n   `REQUEST_TIMEOUT`",
                "Request expiry MUST produce\n   `REQUEST_TIMEOUT`",
            ),
        )
        for old, new in replacements:
            with self.subTest(old=old):
                changed = self.protocol.replace(old, new)
                self.assertNotEqual(changed, self.protocol)
                with self.assertRaisesRegex(
                    DeadlineEvidenceFailure,
                    "missing deadline clause|stale deadline clause",
                ):
                    build_deadline_evidence(changed)

    def test_deadline_module_and_claim_module_remain_disjoint(self) -> None:
        deadline_tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        claim_tree = ast.parse(CLAIM_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(deadline_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")
        for node in ast.walk(claim_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_deadline_evidence" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_deadline_evidence")


if __name__ == "__main__":
    unittest.main()
