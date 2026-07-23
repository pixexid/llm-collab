"""Tests for deterministic Runtime Adapter transport evidence."""

from __future__ import annotations

import ast
import io
import json
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_reference import MAX_MESSAGE_BYTES, ReferenceAdapter, serve
from llm_collab.runtime_adapter_transport_evidence import (
    ARTIFACT_LABEL,
    TRANSPORT_EVIDENCED,
    TransportEvidenceFailure,
    _READLINE_LIMIT,
    build_transport_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_transport_evidence.py"
CLAIM_PATH = ROOT / "llm_collab" / "runtime_adapter_claim.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
BYTE_LENGTH_KEYS = {"C9614292c6ab1.1", "C3dc535246440.1"}


class RuntimeAdapterTransportEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_transport_evidence_is_distinct_and_covers_only_byte_length_rows(self) -> None:
        artifact = build_transport_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], "transport_raw_reader")
        self.assertEqual(artifact["claim"], TRANSPORT_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        self.assertEqual({clause["clause_key"] for clause in artifact["clauses"]}, BYTE_LENGTH_KEYS)
        self.assertTrue(
            all(clause["state"] == TRANSPORT_EVIDENCED and clause["evidence"] == ARTIFACT_LABEL for clause in artifact["clauses"])
        )

    def test_bounded_read_probe_uses_dwarf_input_and_consumes_at_most_readline_limit(self) -> None:
        artifact = build_transport_evidence(self.protocol)
        observation = artifact["observation"]

        self.assertGreaterEqual(observation["input_bytes"], MAX_MESSAGE_BYTES * 4)
        self.assertEqual(observation["readline_limit"], MAX_MESSAGE_BYTES + 2)
        self.assertLessEqual(observation["consumed_bytes"], MAX_MESSAGE_BYTES + 2)
        self.assertEqual(observation["stdout_error_name"], "MESSAGE_TOO_LARGE")
        self.assertEqual(observation["stdout_error_code"], -32001)

    def test_unbounded_readline_control_consumes_the_same_dwarf_input(self) -> None:
        raw = b"x" * (MAX_MESSAGE_BYTES * 4)
        bounded = io.BytesIO(raw)
        stdout = io.BytesIO()

        serve(adapter=ReferenceAdapter(), stdin=bounded, stdout=stdout, stderr=io.BytesIO())
        self.assertLessEqual(bounded.tell(), _READLINE_LIMIT)

        unbounded = io.BytesIO(raw)
        unbounded.readline()
        self.assertEqual(unbounded.tell(), len(raw))

    def test_length_boundary_is_strictly_greater_than_max_plus_one(self) -> None:
        not_too_large = io.BytesIO((b"x" * MAX_MESSAGE_BYTES) + b"\n")
        stdout = io.BytesIO()
        serve(adapter=ReferenceAdapter(), stdin=not_too_large, stdout=stdout, stderr=io.BytesIO())
        response = json.loads(stdout.getvalue().decode("utf-8"))
        self.assertNotEqual(response["error"]["data"]["name"], "MESSAGE_TOO_LARGE")

        too_large = io.BytesIO((b"x" * (MAX_MESSAGE_BYTES + 1)) + b"\n")
        stdout = io.BytesIO()
        serve(adapter=ReferenceAdapter(), stdin=too_large, stdout=stdout, stderr=io.BytesIO())
        response = json.loads(stdout.getvalue().decode("utf-8"))
        self.assertEqual(response["error"]["data"]["name"], "MESSAGE_TOO_LARGE")

    def test_build_claim_still_gaps_transport_byte_length_rows(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        self.assertLessEqual(BYTE_LENGTH_KEYS, {gap["clause_key"] for gap in result.gaps})

    def test_clause_text_drift_fails_closed(self) -> None:
        changed = self.protocol.replace(
            "The reader MUST stop buffering a frame after",
            "The reader MUST cease buffering a frame after",
        )

        with self.assertRaisesRegex(TransportEvidenceFailure, "missing transport clause|stale transport clause"):
            build_transport_evidence(changed)

    def test_transport_module_and_claim_module_remain_disjoint(self) -> None:
        transport_tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        claim_tree = ast.parse(CLAIM_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(transport_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_claim" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_claim")
        for node in ast.walk(claim_tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == "llm_collab.runtime_adapter_transport_evidence" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "llm_collab.runtime_adapter_transport_evidence")


if __name__ == "__main__":
    unittest.main()
