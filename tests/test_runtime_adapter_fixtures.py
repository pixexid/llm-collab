"""Tests for spec-derived Runtime Adapter JSON-RPC V1 replay fixtures."""

from __future__ import annotations

import ast
from dataclasses import fields, replace
import inspect
import json
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_conformance import ConformanceFailure, error_response
from llm_collab.runtime_adapter_fixtures import (
    FIXTURES,
    NO_STATE_CHANGE,
    POLARITY_CONFORMING,
    POLARITY_VIOLATING,
    ClauseReference,
    ExpectedRefusal,
    ExpectedResult,
    RuntimeAdapterFixture,
    TraceFrame,
    _protocol_error_codes,
    validate_fixtures,
)
from llm_collab.runtime_adapter_reference import ReferenceAdapter


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_fixtures.py"
CONFORMANCE_PATH = ROOT / "llm_collab" / "runtime_adapter_conformance.py"


class RuntimeAdapterFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = PROTOCOL_PATH.read_text(encoding="utf-8")

    def test_fixture_set_validates_against_live_clause_extractor(self) -> None:
        checked = validate_fixtures(self.protocol)

        self.assertEqual(checked, FIXTURES)
        self.assertTrue(any(fixture.polarity == POLARITY_CONFORMING for fixture in checked))
        self.assertTrue(any(fixture.polarity == POLARITY_VIOLATING for fixture in checked))

    def test_conforming_fixtures_replay_against_reference_adapter(self) -> None:
        conforming = [fixture for fixture in FIXTURES if fixture.polarity == POLARITY_CONFORMING]

        for fixture in conforming:
            with self.subTest(fixture=fixture.fixture_id):
                adapter = ReferenceAdapter()
                saw_expected = False
                for trace in fixture.trace:
                    if trace.sender != "host" or trace.receiver != "adapter":
                        continue
                    frame = _thaw(trace.frame)
                    response = adapter.handle_text(json.dumps(frame, sort_keys=True, separators=(",", ":")))
                    self.assertIsNotNone(response)
                    payload = json.loads(response)
                    self.assertNotIn("error", payload)
                    if frame["method"] == fixture.expectation.method:
                        self.assertEqual(payload["result"], _thaw(fixture.expectation.result))
                        saw_expected = True
                self.assertTrue(saw_expected)

    def test_old_three_key_initialize_endpoint_is_rejected(self) -> None:
        initialize = _thaw(FIXTURES[0].trace[0].frame)
        initialize["params"]["endpoint"] = {
            "endpoint_id": "endpoint_alpha",
            "adapter_name": "adapter_alpha",
            "adapter_revision": "adapter_rev1",
        }

        response = ReferenceAdapter().handle_text(json.dumps(initialize, sort_keys=True, separators=(",", ":")))

        self.assertIsNotNone(response)
        payload = json.loads(response)
        self.assertEqual(payload["error"]["data"]["name"], "INVALID_PARAMS")

    def test_unknown_clause_key_fails_closed(self) -> None:
        fixture = replace(
            FIXTURES[0],
            clause_refs=(
                replace(FIXTURES[0].clause_refs[0], clause_key="Cunknown.1"),
            ),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-clause-key"):
            validate_fixtures(self.protocol, (fixture,))

    def test_stale_clause_hash_fails_closed(self) -> None:
        fixture = replace(
            FIXTURES[0],
            clause_refs=(
                replace(FIXTURES[0].clause_refs[0], text_sha256="0" * 64),
            ),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-text-hash"):
            validate_fixtures(self.protocol, (fixture,))

    def test_each_fixture_ref_declares_matching_polarity(self) -> None:
        fixture = replace(
            FIXTURES[0],
            clause_refs=(
                replace(FIXTURES[0].clause_refs[0], polarity=POLARITY_VIOLATING),
            ),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-polarity-ref"):
            validate_fixtures(self.protocol, (fixture,))

    def test_violating_fixture_requires_exact_refusal(self) -> None:
        violating = next(fixture for fixture in FIXTURES if fixture.polarity == POLARITY_VIOLATING)
        self.assertIsInstance(violating.expectation, ExpectedRefusal)
        response_parameter = inspect.signature(ExpectedRefusal).parameters["response_emitted"]
        self.assertIs(response_parameter.default, inspect.Parameter.empty)
        generic = replace(
            violating,
            expectation=replace(violating.expectation, error_name="ANY_ERROR"),
        )
        missing_code = replace(
            violating,
            expectation=replace(violating.expectation, error_code=None),  # type: ignore[arg-type]
        )

        for fixture in (generic, missing_code):
            with self.subTest(fixture=fixture), self.assertRaisesRegex(
                ConformanceFailure,
                "fixture-violating-refusal",
            ):
                validate_fixtures(self.protocol, (fixture,))

    def test_violating_fixture_cannot_be_accepted_or_stateful(self) -> None:
        violating = next(fixture for fixture in FIXTURES if fixture.polarity == POLARITY_VIOLATING)
        self.assertIsInstance(violating.expectation, ExpectedRefusal)
        accepted = replace(violating, expectation=replace(violating.expectation, accepted=True))
        stateful = replace(violating, expectation=replace(violating.expectation, state_effect="mutated"))

        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-accepted"):
            validate_fixtures(self.protocol, (accepted,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-state-effect"):
            validate_fixtures(self.protocol, (stateful,))
        self.assertEqual(violating.expectation.state_effect, NO_STATE_CHANGE)

    def test_violating_fixture_distinguishes_returned_error_from_silent_close(self) -> None:
        returned_error = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-shutdown-rejects-session-selector"
        )
        silent_close = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-host-response-is-direction-fault"
        )
        self.assertIsInstance(returned_error.expectation, ExpectedRefusal)
        self.assertIsInstance(silent_close.expectation, ExpectedRefusal)
        self.assertTrue(returned_error.expectation.response_emitted)
        self.assertFalse(returned_error.expectation.closes_connection)
        self.assertFalse(silent_close.expectation.response_emitted)
        self.assertTrue(silent_close.expectation.closes_connection)

    def test_shutdown_refusal_fixture_is_post_initialize(self) -> None:
        shutdown = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-shutdown-rejects-session-selector"
        )

        self.assertEqual(shutdown.trace[0].frame["method"], "initialize")
        self.assertIn("result", shutdown.trace[1].frame)
        self.assertEqual(shutdown.trace[2].frame["method"], "runtime.shutdown")
        self.assertEqual(shutdown.trace[2].frame["id"], "shutdown-1")

    def test_violating_fixture_trace_must_really_be_rejected(self) -> None:
        violating = next(fixture for fixture in FIXTURES if fixture.polarity == POLARITY_VIOLATING)
        accepted_trace = (
            TraceFrame(
                "host",
                "adapter",
                {"jsonrpc": "2.0", "id": "health-accepted", "method": "runtime.health", "params": {}},
            ),
        )
        mutated = replace(violating, trace=accepted_trace)

        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-trace"):
            validate_fixtures(self.protocol, (mutated,))

    def test_conforming_fixture_trace_must_validate_as_request(self) -> None:
        conforming = next(fixture for fixture in FIXTURES if fixture.polarity == POLARITY_CONFORMING)
        invalid_trace = (
            TraceFrame(
                "host",
                "adapter",
                {"jsonrpc": "2.0", "id": "bad", "method": "runtime.health", "params": {"extra": True}},
            ),
        )
        mutated = replace(conforming, trace=invalid_trace)

        with self.assertRaisesRegex(ConformanceFailure, "fixture-conforming-trace"):
            validate_fixtures(self.protocol, (mutated,))

    def test_conforming_fixture_binds_method_and_result_shape(self) -> None:
        health = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-health-request")
        self.assertIsInstance(health.expectation, ExpectedResult)
        wrong_method = replace(health, expectation=replace(health.expectation, method="runtime.reconcile"))
        malformed_result = replace(
            health,
            expectation=replace(health.expectation, result={"status": "healthy"}),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-method"):
            validate_fixtures(self.protocol, (wrong_method,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(self.protocol, (malformed_result,))

    def test_conforming_reconcile_fixture_requires_complete_session_ref_and_receipt(self) -> None:
        reconcile = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-reconcile-request")
        self.assertIsInstance(reconcile.expectation, ExpectedResult)
        request = _thaw(reconcile.trace[0].frame)
        request["params"]["session_ref"] = {"session_ref_id": "session_alpha"}
        malformed_session = replace(reconcile, trace=(TraceFrame("host", "adapter", request),))
        bad_receipt = _thaw(reconcile.expectation.result)
        bad_receipt.pop("session_ref_id")
        malformed_receipt = replace(
            reconcile,
            expectation=replace(reconcile.expectation, result=bad_receipt),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-conforming-trace"):
            validate_fixtures(self.protocol, (malformed_session,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(self.protocol, (malformed_receipt,))

    def test_conforming_adapter_response_frames_are_validated(self) -> None:
        health = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-health-request")
        malformed_response = TraceFrame("adapter", "host", {"result": {}})
        mutated = replace(health, trace=(*health.trace, malformed_response))

        with self.assertRaisesRegex(ConformanceFailure, "fixture-conforming-trace"):
            validate_fixtures(self.protocol, (mutated,))

    def test_initialize_response_must_be_success_before_post_initialize_methods(self) -> None:
        health = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-health-request")
        failed_initialize = TraceFrame(
            "adapter",
            "host",
            json.loads(error_response("initialize-1", "INVALID_PARAMS")),
        )
        mutated = replace(health, trace=(health.trace[0], failed_initialize, *health.trace[2:]))

        with self.assertRaisesRegex(ConformanceFailure, "fixture-conforming-trace"):
            validate_fixtures(self.protocol, (mutated,))

    def test_reconcile_receipt_schema_version_is_v1(self) -> None:
        reconcile = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-reconcile-request")
        self.assertIsInstance(reconcile.expectation, ExpectedResult)
        bad_receipt = _thaw(reconcile.expectation.result)
        bad_receipt["schema_version"] = "bogus"
        mutated = replace(
            reconcile,
            expectation=replace(reconcile.expectation, result=bad_receipt),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(self.protocol, (mutated,))

    def test_refusal_name_and_code_pair_is_closed(self) -> None:
        violating = next(fixture for fixture in FIXTURES if fixture.polarity == POLARITY_VIOLATING)
        self.assertIsInstance(violating.expectation, ExpectedRefusal)
        wrong_code = replace(violating, expectation=replace(violating.expectation, error_code=999))
        wrong_name = replace(violating, expectation=replace(violating.expectation, error_name="WRONG_ERROR"))

        for fixture in (wrong_code, wrong_name):
            with self.subTest(fixture=fixture), self.assertRaisesRegex(
                ConformanceFailure,
                "fixture-violating-refusal",
            ):
                validate_fixtures(self.protocol, (fixture,))

    def test_refusal_codes_are_derived_from_protocol_table(self) -> None:
        codes = _protocol_error_codes(self.protocol)

        self.assertEqual(len(codes), 23)
        self.assertEqual(codes["REDACTION_FAILURE"], -32015)
        self.assertEqual(codes["TOO_MANY_IN_FLIGHT"], -32002)
        self.assertEqual(codes["ADAPTER_QUARANTINED"], -32014)
        self.assertEqual(codes["INVALID_FRAMING"], -32000)

        shutdown_refusal = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-shutdown-rejects-session-selector"
        )
        wrong_code = replace(shutdown_refusal, expectation=replace(shutdown_refusal.expectation, error_code=-32000))

        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-refusal"):
            validate_fixtures(self.protocol, (wrong_code,))

    def test_error_table_derivation_fails_closed_when_table_missing(self) -> None:
        without_table = self.protocol.replace("| -32015 | `REDACTION_FAILURE` | `false` |", "")

        with self.assertRaisesRegex(ConformanceFailure, "fixture-error-codes"):
            _protocol_error_codes(without_table)

    def test_no_human_text_expectation_field_exists(self) -> None:
        for expectation_type in (ExpectedRefusal,):
            self.assertNotIn("message", {field.name for field in fields(expectation_type)})
        self.assertNotIn('"message"', MODULE_PATH.read_text(encoding="utf-8"))

    def test_module_exports_no_coverage_value(self) -> None:
        module = __import__("llm_collab.runtime_adapter_fixtures", fromlist=["runtime_adapter_fixtures"])
        exported = set(getattr(module, "__all__", ())) | set(vars(module))

        self.assertFalse(any("coverage" in name.lower() for name in exported))
        self.assertFalse(any("covered" in name.lower() for name in exported))
        self.assertFalse(any("uncovered" in name.lower() for name in exported))

    def test_fixture_module_is_pure_data_and_no_reference_adapter_import(self) -> None:
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
            "inbox",
            "daemon",
            "registry",
            "project_issue_queue",
            "manifest_provenance",
            "runtime_adapter_reference",
            "runtime_adapter_supervisor",
        }
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    self.assertNotIn(root, forbidden_roots)
                    self.assertNotIn(alias.name, {"llm_collab.runtime_adapter_reference"})
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                parts = module.split(".") if module else ()
                root = parts[0] if parts else ""
                self.assertNotIn(root, forbidden_roots)
                self.assertFalse(set(parts) & forbidden_llm_collab)
                if module == "llm_collab":
                    for alias in node.names:
                        self.assertNotIn(alias.name, forbidden_llm_collab)

    def test_import_direction_allows_fixtures_to_read_conformance_only(self) -> None:
        fixture_imports = _module_imports(MODULE_PATH)
        conformance_imports = _module_imports(CONFORMANCE_PATH)

        self.assertIn("llm_collab.runtime_adapter_conformance", fixture_imports)
        self.assertNotIn("llm_collab.runtime_adapter_fixtures", conformance_imports)

    def test_no_bin_consumer_imports_fixture_module(self) -> None:
        for path in (ROOT / "bin").glob("*.py"):
            imports = _module_imports(path)
            self.assertNotIn("llm_collab.runtime_adapter_fixtures", imports, path)
            self.assertNotIn("llm_collab.runtime_adapter_fixtures.*", imports, path)


def _module_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.add(module)
            for alias in node.names:
                if module == "llm_collab":
                    imports.add(f"llm_collab.{alias.name}")
    return imports


def _thaw(value):
    if hasattr(value, "items"):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


if __name__ == "__main__":
    unittest.main()
