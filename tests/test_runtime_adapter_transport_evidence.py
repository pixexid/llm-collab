"""Tests for deterministic Runtime Adapter transport evidence."""

from __future__ import annotations

import ast
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from llm_collab import runtime_adapter_reference
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_conformance import DirectionOutcome
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
BOUNDED_READ_KEYS = {"C9614292c6ab1.1", "C3dc535246440.1"}
STREAM_SEPARATION_KEYS = {"C00951376f21f.1", "C27e614c40ce1.1"}
C01_FRAMING_KEYS = {
    "C9ef1a548fb74.1",
    "C1c16ee3a9a20.1",
    "C01e408ef2020.1",
    "Cbddfb728b470.1",
    "C15ce93ba85cf.1",
}
TRANSPORT_KEYS = BOUNDED_READ_KEYS | STREAM_SEPARATION_KEYS | C01_FRAMING_KEYS


class RuntimeAdapterTransportEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_transport_evidence_is_distinct_and_covers_its_rows(self) -> None:
        artifact = build_transport_evidence(self.protocol)

        self.assertEqual(artifact["artifact_label"], ARTIFACT_LABEL)
        self.assertEqual(artifact["evidence_kind"], "transport_raw_reader")
        self.assertEqual(artifact["claim"], TRANSPORT_EVIDENCED)
        self.assertNotEqual(artifact["claim"], "exercised_conforming")
        self.assertEqual({clause["clause_key"] for clause in artifact["clauses"]}, TRANSPORT_KEYS)
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

    def test_stream_separation_probe_is_non_vacuous(self) -> None:
        artifact = build_transport_evidence(self.protocol)
        observation = artifact["observation"]

        self.assertGreaterEqual(observation["normal_stdout_frames"], 2)
        self.assertGreaterEqual(observation["normal_stdout_successes"], 1)
        self.assertGreaterEqual(observation["normal_stdout_errors"], 1)
        self.assertEqual(observation["fault_stdout_frames"], 1)
        self.assertGreater(observation["fault_stderr_bytes"], 0)

    def test_c01_framing_probe_uses_real_framing_direction_and_writer_surfaces(self) -> None:
        c01 = build_transport_evidence(self.protocol)["observation"]["c01_framing"]

        self.assertEqual(c01["first_frame_id"], "line-1")
        self.assertEqual(c01["second_frame_id"], "line-2")
        self.assertEqual(c01["embedded_newline_fault"], "parse-json")
        self.assertEqual(c01["duplicate_member_fault"], "duplicate-member")
        self.assertEqual(c01["absent_id_adapter_request_fault"], "INVALID_REQUEST")
        self.assertTrue(c01["absent_id_adapter_request_quarantines"])
        self.assertEqual(c01["oversized_response_error_name"], "MESSAGE_TOO_LARGE")
        self.assertLessEqual(c01["oversized_response_bytes"], MAX_MESSAGE_BYTES + 1)

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

    def test_build_claim_still_gaps_transport_rows(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        self.assertLessEqual(TRANSPORT_KEYS, {gap["clause_key"] for gap in result.gaps})

    def test_clause_text_drift_fails_closed(self) -> None:
        changed = self.protocol.replace(
            "The reader MUST stop buffering a frame after",
            "The reader MUST cease buffering a frame after",
        )

        with self.assertRaisesRegex(TransportEvidenceFailure, "missing transport clause|stale transport clause"):
            build_transport_evidence(changed)

    def test_stream_separation_clause_text_drift_fails_closed(self) -> None:
        replacements = (
            (
                "Standard output MUST contain protocol frames only.",
                "Standard output MUST contain only protocol frames.",
            ),
            (
                "Standard error is for\n   diagnostics only and MUST NOT contain protocol responses.",
                "Standard error is for\n   diagnostics only and MUST NOT carry protocol responses.",
            ),
        )
        for old, new in replacements:
            with self.subTest(old=old):
                changed = self.protocol.replace(old, new)
                with self.assertRaisesRegex(TransportEvidenceFailure, "missing transport clause|stale transport clause"):
                    build_transport_evidence(changed)

    def test_c01_clause_text_drift_fails_closed(self) -> None:
        replacements = (
            (
                "The connection MUST use JSON-RPC 2.0 over stdio,\n   with exactly one UTF-8 encoded JSON object per line",
                "The connection MUST use JSON-RPC 2.0 over stdio,\n   with exactly one UTF-8 encoded JSON value per line",
            ),
            (
                "JSON strings MUST escape newlines; an embedded raw newline ends the frame.",
                "JSON strings MUST encode newlines; an embedded raw newline ends the frame.",
            ),
            (
                "the receiver MUST inspect\n   every raw object-member sequence before ordinary object parsing",
                "the receiver MUST inspect\n   every object-member sequence before ordinary object parsing",
            ),
            (
                "It MUST NOT reinterpret an\n   absent-id wrong-direction request as a direction-valid notification",
                "It MUST NOT treat an\n   absent-id wrong-direction request as a direction-valid notification",
            ),
            (
                "implementations MUST still measure the complete encoded\n   response before writing it",
                "implementations MUST still size the complete encoded\n   response before writing it",
            ),
        )
        for old, new in replacements:
            with self.subTest(old=old):
                changed = self.protocol.replace(old, new)
                self.assertNotEqual(changed, self.protocol)
                with self.assertRaisesRegex(TransportEvidenceFailure, "missing transport clause|stale transport clause"):
                    build_transport_evidence(changed)

    def test_c01_framing_rows_are_scoped_disjoint_from_prior_transport_rows(self) -> None:
        self.assertFalse(C01_FRAMING_KEYS & BOUNDED_READ_KEYS)
        self.assertFalse(C01_FRAMING_KEYS & STREAM_SEPARATION_KEYS)

    def test_real_c01_transport_component_mutations_kill_evidence(self) -> None:
        original_classify_direction = _transport_module().classify_direction

        def unbounded_readline(stdin, deadline_at):
            return stdin.readline()

        def json_loads_without_duplicate_rejection(raw):
            return json.loads(raw)

        def fail_open_direction(sender, receiver, payload):
            outcome = original_classify_direction(sender, receiver, payload)
            if sender == "adapter" and receiver == "host" and outcome.form == "request":
                return DirectionOutcome(
                    direction_valid=True,
                    form=outcome.form,
                    fault=None,
                    should_close=False,
                    should_quarantine=False,
                    send_response=False,
                )
            return outcome

        def unbounded_write_response(response, stdout, *, enforce_bound=True):
            payload = response if isinstance(response, bytes) else response.encode("utf-8")
            stdout.write(payload + b"\n")
            stdout.flush()

        mutations = (
            (
                mock.patch.object(runtime_adapter_reference, "_readline_before_deadline", unbounded_readline),
                "newline frame reader",
            ),
            (mock.patch.object(_transport_module(), "load_json_frame", json_loads_without_duplicate_rejection), "JSON parser"),
            (
                mock.patch.object(_transport_module(), "classify_direction", fail_open_direction),
                "adapter-to-host direction classifier",
            ),
            (
                mock.patch.object(runtime_adapter_reference, "_write_response", unbounded_write_response),
                "complete response writer bound",
            ),
        )
        for patcher, name in mutations:
            with self.subTest(name=name), patcher:
                with self.assertRaises(TransportEvidenceFailure):
                    build_transport_evidence(self.protocol)

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


def _transport_module():
    import importlib

    return importlib.import_module("llm_collab.runtime_adapter_transport_evidence")


if __name__ == "__main__":
    unittest.main()
