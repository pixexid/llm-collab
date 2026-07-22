"""Tests for the inert Runtime Adapter V1 conformance harness."""

from __future__ import annotations

import ast
import copy
import inspect
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_conformance import (
    ConformanceFailure,
    FakeAdapter,
    LedgerRow,
    build_clause_ledger,
    classify_direction,
    dumps_frame,
    extract_clause_occurrences,
    load_json_frame,
    validate_clause_ledger,
    validate_request,
    validate_response,
)


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_conformance.py"
TEST_PATH = Path(__file__)


def request(method: str, params: dict, request_id: str = "r1") -> str:
    return dumps_frame({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def sample_params(method: str) -> dict:
    if method == "initialize":
        return {
            "requested_protocol_version": "1.0",
            "adapter_id": "adapter",
            "adapter_revision": "rev",
            "manifest_id": "manifest",
            "manifest_revision": "mrev",
            "endpoint": {"endpoint_id": "endpoint"},
        }
    if method == "runtime.deliver":
        return {"session_ref": {"session_ref_id": "s"}, "delivery": {"delivery_id": "d"}}
    if method in {"runtime.cancel", "runtime.reconcile"}:
        return {
            "session_ref": {"session_ref_id": "s"},
            "original_request_id": "orig",
            "delivery_id": "d",
            "attempt_id": "a",
        }
    return {}


class RuntimeAdapterConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_clause_extractor_is_pinned_and_complete(self) -> None:
        first = extract_clause_occurrences(self.protocol)
        second = extract_clause_occurrences(self.protocol)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 154)
        self.assertEqual(len({clause.clause_key for clause in first}), 154)
        self.assertEqual(sum(clause.keyword == "SHALL" for clause in first), 0)
        self.assertEqual(sum(clause.keyword == "MUST NOT" for clause in first), 34)
        self.assertEqual(sum(clause.keyword == "MUST" for clause in first), 120)

    def test_harness_outputs_are_structurally_repeatable(self) -> None:
        def run_harness_once() -> tuple:
            adapter = FakeAdapter({"runtime.health": {"status": "healthy"}})
            outcome = adapter.handle(request("runtime.health", {}))
            return (
                extract_clause_occurrences(self.protocol),
                build_clause_ledger(self.protocol),
                outcome,
                tuple(adapter.observed_frames),
            )

        self.assertEqual(run_harness_once(), run_harness_once())

    def test_clause_ledger_is_bijective_and_uses_j7_rules(self) -> None:
        clauses = extract_clause_occurrences(self.protocol)
        ledger = build_clause_ledger(self.protocol)
        validate_clause_ledger(clauses, ledger, implementing_child="P3a", stamped_claims={})
        owner_counts: dict[tuple[str, ...], int] = {}
        for row in ledger:
            key = tuple(sorted(row.owners))
            owner_counts[key] = owner_counts.get(key, 0) + 1
        self.assertEqual(sum(owner_counts.values()), 154)
        self.assertEqual(owner_counts[("P3e-redact", "P3e-state")], 6)
        self.assertEqual(owner_counts[("P3b", "P3c")], 2)

    def test_ledger_source_line_is_advisory_only(self) -> None:
        clauses = extract_clause_occurrences(self.protocol)
        ledger = list(build_clause_ledger(self.protocol))
        changed = [
            LedgerRow(
                clause_key=row.clause_key,
                text_sha256=row.text_sha256,
                classification=row.classification,
                owners=row.owners,
                reason=row.reason,
                covered_by=row.covered_by,
                claim_refs=row.claim_refs,
                source_line=999_999,
            )
            for row in ledger
        ]
        validate_clause_ledger(clauses, changed, implementing_child="P3a", stamped_claims={})

    def test_ledger_validator_handles_iterables_and_duplicate_keys(self) -> None:
        clauses = extract_clause_occurrences(self.protocol)
        ledger = build_clause_ledger(self.protocol)
        validate_clause_ledger(
            (clause for clause in clauses),
            (row for row in ledger),
            implementing_child="P3a",
            stamped_claims={},
        )
        with self.assertRaisesRegex(ConformanceFailure, "ledger-duplicate"):
            validate_clause_ledger(
                clauses,
                (*ledger, ledger[0]),
                implementing_child="P3a",
                stamped_claims={},
            )

    def test_ledger_validator_requires_stamped_claims(self) -> None:
        parameter = inspect.signature(validate_clause_ledger).parameters["stamped_claims"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            validate_clause_ledger(
                extract_clause_occurrences(self.protocol),
                build_clause_ledger(self.protocol),
                implementing_child="P3a",
            )

    def test_ledger_fails_escape_hatches_and_silent_rehome(self) -> None:
        clauses = extract_clause_occurrences(self.protocol)
        row = build_clause_ledger(self.protocol)[0]
        with self.assertRaisesRegex(ConformanceFailure, "ledger-deferred-to-self"):
            validate_clause_ledger(
                clauses,
                [
                    *(build_clause_ledger(self.protocol)[1:]),
                    LedgerRow(
                        row.clause_key,
                        row.text_sha256,
                        "deferred",
                        frozenset(("P3a",)),
                        reason="later",
                    ),
                ],
                implementing_child="P3a",
                stamped_claims={},
            )
        with self.assertRaisesRegex(ConformanceFailure, "ledger-actionable-untestable"):
            validate_clause_ledger(
                clauses,
                [
                    *(build_clause_ledger(self.protocol)[1:]),
                    LedgerRow(
                        row.clause_key,
                        row.text_sha256,
                        "not_mechanically_testable",
                        frozenset(("P3f",)),
                        reason="manual only",
                    ),
                ],
                implementing_child="P3a",
                stamped_claims={},
            )
        with self.assertRaisesRegex(ConformanceFailure, "ledger-silent-rehome"):
            validate_clause_ledger(
                clauses,
                build_clause_ledger(self.protocol),
                implementing_child="P3a",
                stamped_claims={"P3b": set()},
            )

    def test_closed_method_set_all_six_methods(self) -> None:
        for method in (
            "initialize",
            "runtime.deliver",
            "runtime.cancel",
            "runtime.reconcile",
            "runtime.health",
            "runtime.shutdown",
        ):
            frame = load_json_frame(request(method, sample_params(method)))
            self.assertEqual(validate_request(frame)[1], method)
        with self.assertRaisesRegex(ConformanceFailure, "closed-method-set"):
            validate_request(load_json_frame(request("runtime.unknown", {})))

    def test_duplicate_members_batch_notifications_and_id_rejected(self) -> None:
        with self.assertRaisesRegex(ConformanceFailure, "duplicate-member"):
            load_json_frame('{"jsonrpc":"2.0","id":"a","id":"b","method":"runtime.health","params":{}}')
        with self.assertRaisesRegex(ConformanceFailure, "batch-rejected"):
            classify_direction("host", "adapter", load_json_frame("[]"))
        with self.assertRaisesRegex(ConformanceFailure, "notification-rejected"):
            validate_request(load_json_frame('{"jsonrpc":"2.0","method":"runtime.health","params":{}}'))
        with self.assertRaisesRegex(ConformanceFailure, "request-id"):
            validate_request(load_json_frame('{"jsonrpc":"2.0","id":null,"method":"runtime.health","params":{}}'))

    def test_direction_matrix_is_exhaustive_and_response_precedence_holds(self) -> None:
        host_request = load_json_frame(request("runtime.health", {}))
        adapter_response = load_json_frame('{"jsonrpc":"2.0","id":"r1","result":{}}')
        adapter_request = load_json_frame(request("runtime.health", {}))
        host_response = load_json_frame('{"jsonrpc":"2.0","id":"r1","result":{},"method":"runtime.health"}')
        self.assertTrue(classify_direction("host", "adapter", host_request).direction_valid)
        self.assertTrue(classify_direction("adapter", "host", adapter_response).direction_valid)
        self.assertTrue(classify_direction("adapter", "host", adapter_request).should_quarantine)
        self.assertFalse(classify_direction("host", "adapter", host_response).should_quarantine)
        self.assertEqual(classify_direction("host", "adapter", host_response).form, "response")

    def test_error_response_data_schema_is_closed(self) -> None:
        with self.assertRaisesRegex(ConformanceFailure, "closed-error"):
            validate_response(
                {
                    "jsonrpc": "2.0",
                    "id": "r1",
                    "error": {
                        "code": -32600,
                        "message": "INVALID_REQUEST",
                        "data": {
                            "name": "INVALID_REQUEST",
                            "retryable": False,
                            "request_id": "r1",
                            "extra": "not allowed",
                        },
                    },
                },
                "r1",
            )

    def test_fake_adapter_is_deterministic_and_table_driven(self) -> None:
        adapter = FakeAdapter({"runtime.health": {"status": "healthy"}})
        frame = request("runtime.health", {})
        first = adapter.handle(frame)
        second = adapter.handle(frame)
        self.assertEqual(first, second)
        self.assertEqual(len(adapter.observed_frames), 2)
        validate_response(load_json_frame(first.response or ""), "r1")
        self.assertEqual(adapter.handle(request("runtime.nope", {})).fault, "METHOD_NOT_FOUND")
        self.assertIsNone(
            adapter.handle('{"jsonrpc":"2.0","method":"runtime.health","params":{}}').response
        )

    def test_nonconforming_variants_fail_and_name_clause_family(self) -> None:
        valid = load_json_frame(request("runtime.health", {}))
        variants = {
            "closed-method-set": lambda: validate_request(
                {**copy.deepcopy(valid), "method": "runtime.extra"}
            ),
            "closed-params": lambda: validate_request(
                {**copy.deepcopy(valid), "params": {"extra": True}}
            ),
            "closed-response": lambda: validate_response(
                {"jsonrpc": "2.0", "id": "r1", "result": {}, "error": {}},
                "r1",
            ),
            "direction-matrix": lambda: classify_direction("nobody", "adapter", valid),
        }
        for clause, operation in variants.items():
            with self.subTest(clause=clause), self.assertRaises(ConformanceFailure) as caught:
                operation()
            self.assertIn(clause, caught.exception.clause)

    def test_no_forbidden_runtime_or_authority_imports(self) -> None:
        forbidden_roots = {
            "os",
            "subprocess",
            "time",
            "threading",
            "random",
            "socket",
            "pathlib",
        }
        forbidden_llm_collab = {
            "canonical",
            "ledger",
            "compatibility",
            "daemon",
        }
        for path in (MODULE_PATH, TEST_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        if path == MODULE_PATH:
                            self.assertNotIn(root, forbidden_roots)
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if path == MODULE_PATH:
                        parts = module.split(".") if module else ()
                        root = parts[0] if parts else ""
                        self.assertNotIn(root, forbidden_roots)
                        self.assertFalse(set(parts) & forbidden_llm_collab)
                        for alias in node.names:
                            self.assertNotIn(alias.name.split(".", 1)[0], forbidden_llm_collab)

    def test_no_bin_consumer_imports_conformance_module(self) -> None:
        for path in (ROOT / "bin").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(alias.name == "llm_collab.runtime_adapter_conformance" for alias in node.names),
                        path,
                    )
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "llm_collab.runtime_adapter_conformance", path)
                    if node.module == "llm_collab":
                        self.assertFalse(
                            any(alias.name == "runtime_adapter_conformance" for alias in node.names),
                            path,
                        )


if __name__ == "__main__":
    unittest.main()
