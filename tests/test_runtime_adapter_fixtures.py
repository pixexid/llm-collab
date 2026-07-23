"""Tests for spec-derived Runtime Adapter JSON-RPC V1 replay fixtures."""

from __future__ import annotations

import ast
from dataclasses import fields, replace
import inspect
import io
import json
import unittest
from pathlib import Path
from types import MappingProxyType

import llm_collab.runtime_adapter_conformance as conformance
from llm_collab.runtime_adapter_conformance import ERROR_CODES, ConformanceFailure, error_response, protocol_error_codes
from llm_collab.runtime_adapter_claim import ClaimFailure, build_claim
from llm_collab.runtime_adapter_fixtures import (
    FIXTURES,
    _INITIALIZE_PARAMS,
    NO_STATE_CHANGE,
    POLARITY_CONFORMING,
    POLARITY_VIOLATING,
    ClauseReference,
    ExpectedRefusal,
    ExpectedResult,
    RuntimeAdapterFixture,
    TraceFrame,
    validate_fixtures,
)
from llm_collab.runtime_adapter_reference import ReferenceAdapter, serve


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

    def test_violating_fixtures_replay_against_reference_adapter(self) -> None:
        violating = [fixture for fixture in FIXTURES if fixture.polarity == POLARITY_VIOLATING]

        for fixture in violating:
            with self.subTest(fixture=fixture.fixture_id):
                adapter = ReferenceAdapter()
                responses: list[str | bytes | None] = []
                for trace in fixture.trace:
                    if trace.sender != "host" or trace.receiver != "adapter":
                        continue
                    frame = _thaw(trace.frame)
                    responses.append(adapter.handle_text(json.dumps(frame, sort_keys=True, separators=(",", ":"))))
                self.assertTrue(responses)
                self.assertTrue(_matches_refusal(fixture.expectation, responses[-1]))

    def test_c01_request_id_fixtures_are_named_and_replay(self) -> None:
        by_id = {fixture.fixture_id: fixture for fixture in FIXTURES}
        expected = {
            "runtime-adapter-absent-id-runtime-request-closes": (
                "C27ebf7697043.1",
                False,
                True,
            ),
            "runtime-adapter-null-id-runtime-request-refuses": (
                "C8f215da97f3e.1",
                True,
                True,
            ),
        }

        for fixture_id, (clause_key, response_emitted, closes_connection) in expected.items():
            with self.subTest(fixture=fixture_id):
                fixture = by_id[fixture_id]
                self.assertEqual({ref.clause_key for ref in fixture.clause_refs}, {clause_key})
                self.assertIsInstance(fixture.expectation, ExpectedRefusal)
                self.assertEqual(fixture.expectation.error_name, "INVALID_REQUEST")
                self.assertEqual(fixture.expectation.error_code, -32600)
                self.assertEqual(fixture.expectation.state_effect, NO_STATE_CHANGE)
                self.assertEqual(fixture.expectation.response_emitted, response_emitted)
                self.assertEqual(fixture.expectation.closes_connection, closes_connection)
                self.assertTrue(_replays_fixture(fixture))

    def test_c07_session_binding_fixtures_are_named_and_replay(self) -> None:
        by_id = {fixture.fixture_id: fixture for fixture in FIXTURES}
        expected = {
            "runtime-adapter-reconcile-request": {
                "C39aa248f4dd8.1",
                "C5097ad6c480d.1",
                "C81987c71b9d0.1",
                "C9e388d863ed5.1",
            },
            "runtime-adapter-reconcile-accepts-varied-binding-capability-profile": {
                "C9a07be32fe6b.1",
            },
            "runtime-adapter-reconcile-rejects-workspace-mismatch": {
                "C72337c3bc58e.1",
                "C72337c3bc58e.2",
            },
            "runtime-adapter-reconcile-rejects-workspace-repository-binding": {
                "C81987c71b9d0.2",
            },
            "runtime-adapter-reconcile-rejects-stale-binding-evidence": {
                "C9e388d863ed5.1",
            },
            "runtime-adapter-reconcile-rejects-session-ref-schema-drift": {
                "C930c3ccd59a0.1",
            },
            "runtime-adapter-project-reconcile-request": {
                "C8ed901b43824.1",
                "C8ed901b43824.2",
            },
            "runtime-adapter-project-reconcile-rejects-scope-mismatch": {
                "C8ed901b43824.1",
            },
            "runtime-adapter-project-reconcile-rejects-repository-project-mismatch": {
                "C8ed901b43824.2",
            },
        }
        deferred = set()
        referenced = {ref.clause_key for fixture in FIXTURES for ref in fixture.clause_refs}

        self.assertFalse(deferred & referenced)
        for fixture_id, clause_keys in expected.items():
            with self.subTest(fixture=fixture_id):
                fixture = by_id[fixture_id]
                self.assertLessEqual(clause_keys, {ref.clause_key for ref in fixture.clause_refs})
                self.assertTrue(_replays_fixture(fixture))
                if fixture.polarity == POLARITY_VIOLATING:
                    self.assertIsInstance(fixture.expectation, ExpectedRefusal)
                    if fixture_id == "runtime-adapter-reconcile-rejects-session-ref-schema-drift":
                        self.assertEqual(fixture.expectation.error_name, "INVALID_PARAMS")
                        self.assertEqual(fixture.expectation.error_code, -32602)
                        self.assertTrue(fixture.expectation.response_emitted)
                        self.assertFalse(fixture.expectation.closes_connection)
                    else:
                        self.assertEqual(fixture.expectation.error_name, "INVALID_SESSION_REF")
                        self.assertEqual(fixture.expectation.error_code, -32008)

    def test_c07_capability_profile_must_not_fixture_differs_from_initialized_profile(self) -> None:
        fixture = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-reconcile-accepts-varied-binding-capability-profile"
        )
        initialize = _thaw(fixture.trace[1].frame)["result"]
        session_ref = _thaw(fixture.trace[-1].frame)["params"]["session_ref"]
        authority = session_ref["evidence"]["authority"]

        self.assertEqual(initialize["capability_set"]["revision"], "cap_rev1")
        self.assertNotEqual(authority["capability_profile_id"], "runtime_profile")
        self.assertNotEqual(authority["capability_profile_revision"], initialize["capability_set"]["revision"])
        self.assertTrue(_replays_fixture(fixture))

    def test_c07_session_identity_mutations_replay_as_invalid_session_ref(self) -> None:
        base = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-reconcile-request")
        session = _thaw(base.trace[-1].frame)["params"]["session_ref"]
        mutations = {
            "workspace_id": lambda value: value.__setitem__("workspace_id", "ws_other"),
            "scope": lambda value: value.__setitem__("scope", {"kind": "project", "project_id": "amiga"}),
            "endpoint_id": lambda value: value.__setitem__("endpoint_id", "endpoint_other"),
            "native_session_id": lambda value: value.__setitem__("native_session_id", "native-other"),
            "repository_binding": lambda value: value.__setitem__(
                "repository_binding",
                {"project_id": "amiga", "repo_id": "llm-collab", "canonical_cwd": "/repo"},
            ),
            "evidence_integrity": lambda value: value["evidence"].__setitem__("integrity", "sha256:" + "0" * 64),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = _thaw(session)
                mutate(changed)
                response = _reconcile_response(changed)
                self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")
                self.assertEqual(response["error"]["code"], -32008)

    def test_c10_reconcile_receipt_fixture_resolves_with_authoritative_evidence(self) -> None:
        resolving_states = {"accepted", "completed", "rejected_before_acceptance"}
        fixture = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-reconcile-request")
        self.assertIn("C94e8b1a261f1.1", {ref.clause_key for ref in fixture.clause_refs})

        payload = json.loads(_last_replay_response(fixture) or "{}")
        receipt = payload["result"]
        evidence = receipt["evidence"]
        params = _thaw(fixture.trace[-1].frame)["params"]
        session_ref = params["session_ref"]

        self.assertIn(receipt["state"], resolving_states)
        self.assertEqual(evidence["quality"], "authoritative")
        self.assertEqual(evidence["state"], receipt["state"])
        self.assertEqual(receipt["workspace_id"], session_ref["workspace_id"])
        self.assertEqual(receipt["scope"], session_ref["scope"])
        self.assertEqual(receipt["delivery_id"], params["delivery_id"])
        self.assertEqual(receipt["attempt_id"], params["attempt_id"])
        self.assertEqual(receipt["endpoint_id"], session_ref["endpoint_id"])
        self.assertEqual(receipt["session_ref_id"], session_ref["session_ref_id"])
        self.assertEqual(evidence["subject"]["message_id"], receipt["message_id"])
        self.assertEqual(evidence["subject"]["delivery_id"], receipt["delivery_id"])
        self.assertEqual(evidence["subject"]["attempt_id"], receipt["attempt_id"])
        self.assertEqual(evidence["subject"]["endpoint_id"], receipt["endpoint_id"])
        self.assertEqual(evidence["subject"]["session_ref_id"], receipt["session_ref_id"])
        self.assertTrue(_replays_fixture(fixture))

    def test_c10_receipt_slice_reduces_its_own_base_gaps_by_one(self) -> None:
        covered_key = "C94e8b1a261f1.1"
        deferred_keys = {
            "Ce45ac56f0f07.1",
            "Ce45ac56f0f07.2",
            "Cd849c64f4310.1",
            "C0ed26afcfb8a.1",
        }
        result = build_claim(self.protocol)
        base_result = build_claim(
            self.protocol,
            fixtures=_fixtures_without_ref("runtime-adapter-reconcile-request", covered_key),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 1)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertNotIn(covered_key, gap_keys)
        self.assertLessEqual(deferred_keys, gap_keys)

    def test_c10_receipt_resolution_mutations_fail_closed(self) -> None:
        fixture = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-reconcile-request")
        self.assertIsInstance(fixture.expectation, ExpectedResult)

        state_mutations = ("ambiguous", "pull_pending", "deferred_busy")
        for state in state_mutations:
            with self.subTest(state=state):
                receipt = _thaw(fixture.expectation.result)
                receipt["state"] = state
                receipt["evidence"]["state"] = state
                mutated = replace(fixture, expectation=replace(fixture.expectation, result=receipt))
                with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
                    validate_fixtures(self.protocol, (mutated,))

        non_authoritative = _thaw(fixture.expectation.result)
        non_authoritative["evidence"]["quality"] = "observed"
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(
                self.protocol,
                (replace(fixture, expectation=replace(fixture.expectation, result=non_authoritative)),),
            )

        mismatched_evidence_state = _thaw(fixture.expectation.result)
        mismatched_evidence_state["evidence"]["state"] = "accepted"
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(
                self.protocol,
                (replace(fixture, expectation=replace(fixture.expectation, result=mismatched_evidence_state)),),
            )

        result = build_claim(
            self.protocol,
            fixtures=_fixtures_without_ref("runtime-adapter-reconcile-request", "C94e8b1a261f1.1"),
        )
        self.assertIsInstance(result, ClaimFailure)
        self.assertIn("C94e8b1a261f1.1", {gap["clause_key"] for gap in result.gaps})

    def test_c08_deliver_envelope_fixture_is_inert_and_identity_bound(self) -> None:
        fixture = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-deliver-envelope")
        self.assertEqual(
            {ref.clause_key for ref in fixture.clause_refs},
            {"Cb995426c8ed9.1", "C508d9faea31c.1", "Ca587a5325269.1", "Ca587a5325269.2"},
        )
        payload = json.loads(_last_replay_response(fixture) or "{}")
        receipt = payload["result"]
        params = _thaw(fixture.trace[-1].frame)["params"]
        session_ref = params["session_ref"]
        delivery = params["delivery"]
        evidence = receipt["evidence"]

        self.assertEqual(receipt["state"], "routed")
        self.assertEqual(evidence["quality"], "best_effort")
        self.assertEqual(evidence["state"], receipt["state"])
        self.assertEqual(receipt["workspace_id"], session_ref["workspace_id"])
        self.assertEqual(receipt["scope"], session_ref["scope"])
        self.assertEqual(receipt["message_id"], delivery["message_id"])
        self.assertEqual(receipt["delivery_id"], delivery["delivery_id"])
        self.assertEqual(receipt["attempt_id"], delivery["attempt_id"])
        self.assertEqual(receipt["endpoint_id"], session_ref["endpoint_id"])
        self.assertEqual(receipt["session_ref_id"], session_ref["session_ref_id"])
        for key in ("message_id", "delivery_id", "attempt_id", "endpoint_id", "session_ref_id"):
            self.assertEqual(evidence["subject"][key], receipt[key])
        self.assertTrue(_replays_fixture(fixture))

    def test_c08_deliver_oracle_violating_fixtures_replay(self) -> None:
        expected = {
            "runtime-adapter-deliver-rejects-missing-param": "INVALID_PARAMS",
            "runtime-adapter-deliver-rejects-extra-param": "INVALID_PARAMS",
            "runtime-adapter-deliver-rejects-wrong-typed-delivery-param": "INVALID_PARAMS",
            "runtime-adapter-deliver-rejects-session-ref-schema-drift": "INVALID_PARAMS",
            "runtime-adapter-deliver-rejects-delivery-schema-drift": "INVALID_PARAMS",
            "runtime-adapter-deliver-rejects-identity-drift": "INVALID_DELIVERY",
        }

        for fixture_id, error_name in expected.items():
            with self.subTest(fixture=fixture_id):
                fixture = next(fixture for fixture in FIXTURES if fixture.fixture_id == fixture_id)
                self.assertEqual(fixture.polarity, POLARITY_VIOLATING)
                self.assertIsInstance(fixture.expectation, ExpectedRefusal)
                self.assertEqual(fixture.expectation.error_name, error_name)
                self.assertTrue(_replays_fixture(fixture))

    def test_c08_deliver_slice_reduces_its_own_base_gaps_by_four(self) -> None:
        c08_keys = {"Cb995426c8ed9.1", "C508d9faea31c.1", "Ca587a5325269.1", "Ca587a5325269.2"}
        deferred_keys = {
            "Cddf6725ddfa4.1",
            "Cbfa7351a2ba5.1",
            "Cbfa7351a2ba5.2",
            "C3db5b5acb8d7.1",
            "Ce4dfe2af8d8d.1",
        }
        result = build_claim(self.protocol)
        base_result = build_claim(self.protocol, fixtures=_fixtures_without_clause_keys(c08_keys))

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 4)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertFalse(c08_keys & gap_keys)
        self.assertLessEqual(deferred_keys, gap_keys)

    def test_c08_deliver_result_mutations_fail_closed(self) -> None:
        fixture = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-deliver-envelope")
        self.assertIsInstance(fixture.expectation, ExpectedResult)

        wrapped = {"receipt": _thaw(fixture.expectation.result)}
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(
                self.protocol,
                (replace(fixture, expectation=replace(fixture.expectation, result=wrapped)),),
            )

        completed = _thaw(fixture.expectation.result)
        completed["state"] = "completed"
        completed["evidence"]["state"] = "completed"
        completed["evidence"]["quality"] = "authoritative"
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(
                self.protocol,
                (replace(fixture, expectation=replace(fixture.expectation, result=completed)),),
            )

        for clause_key in {"Cb995426c8ed9.1", "C508d9faea31c.1", "Ca587a5325269.1", "Ca587a5325269.2"}:
            with self.subTest(clause=clause_key):
                result = build_claim(self.protocol, fixtures=_fixtures_without_clause_keys({clause_key}))
                self.assertIsInstance(result, ClaimFailure)
                self.assertIn(clause_key, {gap["clause_key"] for gap in result.gaps})

    def test_fixture_batch_reduces_claim_gap_below_baseline_but_still_fails_closed(self) -> None:
        result = build_claim(self.protocol)

        self.assertIsInstance(result, ClaimFailure)
        self.assertLess(len(result.gaps), 137)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertNotIn("C930c3ccd59a0.1", gap_keys)
        self.assertNotIn("C8ed901b43824.1", gap_keys)
        self.assertNotIn("C8ed901b43824.2", gap_keys)
        self.assertNotIn("C9a07be32fe6b.1", gap_keys)
        self.assertNotIn("C358ebcd9608d.1", gap_keys)
        self.assertNotIn("C358ebcd9608d.2", gap_keys)
        self.assertNotIn("Cf12ffe8bf4a6.1", gap_keys)

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

    def test_c04_handshake_fixtures_cover_only_static_replayable_clauses(self) -> None:
        by_id = {fixture.fixture_id: fixture for fixture in FIXTURES}
        health = by_id["runtime-adapter-health-request"]
        expected = {
            "runtime-adapter-initialize-rejects-bad-params": ("C35ad6e99e97c.1", "INVALID_PARAMS", -32602, False),
            "runtime-adapter-initialize-rejects-unsupported-version": (
                "Cd29f1b437866.1",
                "UNSUPPORTED_PROTOCOL_VERSION",
                -32004,
                True,
            ),
            "runtime-adapter-non-initialize-first-method-refuses": (
                "Cd29f1b437866.2",
                "INITIALIZE_REQUIRED",
                -32005,
                True,
            ),
        }
        referenced = {ref.clause_key for fixture in FIXTURES for ref in fixture.clause_refs}

        self.assertLessEqual({"C6e51fab4b16d.1", "C1c1a8c844fde.1"}, {ref.clause_key for ref in health.clause_refs})
        self.assertTrue(_replays_fixture(health))
        _assert_initialize_result_echoes_request(self, health)
        for fixture_id, (clause_key, error_name, error_code, closes) in expected.items():
            with self.subTest(fixture=fixture_id):
                fixture = by_id[fixture_id]
                self.assertEqual(fixture.polarity, POLARITY_VIOLATING)
                self.assertIn(clause_key, {ref.clause_key for ref in fixture.clause_refs})
                self.assertIn("Cc1f582727d06.1", {ref.clause_key for ref in fixture.clause_refs})
                self.assertIsInstance(fixture.expectation, ExpectedRefusal)
                self.assertEqual(fixture.expectation.error_name, error_name)
                self.assertEqual(fixture.expectation.error_code, error_code)
                self.assertTrue(fixture.expectation.response_emitted)
                self.assertEqual(fixture.expectation.closes_connection, closes)
                self.assertTrue(_replays_fixture(fixture))
        self.assertNotIn("C587906f36ba3.1", referenced)
        self.assertNotIn("C1be9d6c85a83.1", referenced)

    def test_c04_handshake_no_partial_binding_and_termination_split(self) -> None:
        for name, frame, later_initialize_response in _handshake_failure_cases():
            with self.subTest(name=name):
                adapter = ReferenceAdapter()
                first_response = adapter.handle_text(json.dumps(frame, sort_keys=True, separators=(",", ":")))
                self.assertIsNotNone(first_response)
                first_payload = json.loads(first_response or "{}")
                second_response = adapter.handle_text(_valid_initialize_json("initialize-after-failure"))

                if name == "bad-params":
                    self.assertEqual(first_payload["error"]["data"]["name"], "INVALID_PARAMS")
                    self.assertIsInstance(second_response, str)
                    _assert_initialize_success(self, json.loads(second_response or "{}"))
                else:
                    self.assertEqual(first_payload["error"]["data"]["name"], later_initialize_response)
                    self.assertIsNone(second_response)

    def test_c04_handshake_slice_reduces_its_own_base_gaps_by_six(self) -> None:
        c04_keys = {
            "C6e51fab4b16d.1",
            "C1c1a8c844fde.1",
            "C35ad6e99e97c.1",
            "Cd29f1b437866.1",
            "Cd29f1b437866.2",
            "Cc1f582727d06.1",
        }
        deferred = {"C587906f36ba3.1", "C1be9d6c85a83.1"}
        result = build_claim(self.protocol)
        base_result = build_claim(
            self.protocol,
            fixtures=tuple(
                replace(
                    fixture,
                    clause_refs=tuple(ref for ref in fixture.clause_refs if ref.clause_key not in c04_keys),
                )
                if fixture.fixture_id == "runtime-adapter-health-request"
                else fixture
                for fixture in FIXTURES
                if not fixture.fixture_id.startswith("runtime-adapter-initialize-rejects-")
                and fixture.fixture_id != "runtime-adapter-non-initialize-first-method-refuses"
            ),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 6)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertFalse(c04_keys & gap_keys)
        self.assertLessEqual(deferred, gap_keys)

    def test_c04_handshake_mutations_fail_closed(self) -> None:
        health = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-health-request")
        drifted_response = _thaw(health.trace[1].frame)
        drifted_response["result"]["adapter_id"] = "adapter_other"

        with self.assertRaisesRegex(ConformanceFailure, "fixture-conforming-trace"):
            validate_fixtures(
                self.protocol,
                (replace(health, trace=(health.trace[0], TraceFrame("adapter", "host", drifted_response))),),
            )

        for name, mutate in _bad_initialize_param_mutations().items():
            with self.subTest(name=name):
                params = _thaw(_INITIALIZE_PARAMS)
                mutate(params)
                response = ReferenceAdapter().handle_text(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": f"initialize-{name}", "method": "initialize", "params": params},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                self.assertIsNotNone(response)
                self.assertEqual(json.loads(response or "{}")["error"]["data"]["name"], "INVALID_PARAMS")

        for fixture_id, clause_key in (
            ("runtime-adapter-health-request", "C6e51fab4b16d.1"),
            ("runtime-adapter-health-request", "C1c1a8c844fde.1"),
            ("runtime-adapter-initialize-rejects-bad-params", "C35ad6e99e97c.1"),
            ("runtime-adapter-initialize-rejects-unsupported-version", "Cd29f1b437866.1"),
            ("runtime-adapter-non-initialize-first-method-refuses", "Cd29f1b437866.2"),
            ("runtime-adapter-initialize-rejects-bad-params", "Cc1f582727d06.1"),
        ):
            with self.subTest(clause=clause_key):
                fixtures = tuple(
                    replace(
                        fixture,
                        clause_refs=tuple(ref for ref in fixture.clause_refs if ref.clause_key != clause_key),
                    )
                    if fixture.fixture_id == fixture_id or clause_key == "Cc1f582727d06.1"
                    else fixture
                    for fixture in FIXTURES
                )
                result = build_claim(self.protocol, fixtures=fixtures)
                self.assertIsInstance(result, ClaimFailure)
                self.assertIn(clause_key, {gap["clause_key"] for gap in result.gaps})

    def test_c06_initialize_identity_and_control_methods_are_replay_proven(self) -> None:
        by_id = {fixture.fixture_id: fixture for fixture in FIXTURES}
        health = by_id["runtime-adapter-health-request"]
        shutdown = by_id["runtime-adapter-shutdown-success"]
        refs = {ref.clause_key: ref for ref in health.clause_refs}

        self.assertFalse(refs["Ca25026199e82.1"].non_classifying)
        self.assertTrue(refs["Cfdd95bba6e3a.1"].non_classifying)
        self.assertTrue(refs["C741f42c5aa1e.1"].non_classifying)
        _assert_c06_initialize_identity_controls(self, health, shutdown)

    def test_c06_init_identity_slice_reduces_its_own_base_gaps_by_three(self) -> None:
        c06_keys = {"Ca25026199e82.1", "Cfdd95bba6e3a.1", "C741f42c5aa1e.1"}
        deferred = {
            "C4d3e4e331f8e.1",
            "C507960193aaf.1",
            "C5203ae51498d.1",
            "C05530aaf0297.1",
            "C44a06b005f56.1",
            "C8665d49fe212.1",
            "C8665d49fe212.2",
            "C8665d49fe212.3",
            "Cbc69b8dc81fc.1",
            "Cbc69b8dc81fc.2",
            "Cbc69b8dc81fc.3",
            "Cbc69b8dc81fc.4",
            "C991a6ee55456.1",
            "C468b7316502d.1",
            "C60fb22117077.1",
            "C01d5a7107389.1",
            "Ca7d929aaf1c6.1",
            "Ca7d929aaf1c6.2",
            "Cfb24d181976b.1",
        }
        result = build_claim(self.protocol)
        base_result = build_claim(
            self.protocol,
            fixtures=tuple(
                replace(
                    fixture,
                    clause_refs=tuple(ref for ref in fixture.clause_refs if ref.clause_key not in c06_keys),
                )
                if fixture.fixture_id == "runtime-adapter-health-request"
                else fixture
                for fixture in FIXTURES
            ),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 3)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertFalse(c06_keys & gap_keys)
        self.assertLessEqual(deferred, gap_keys)

    def test_c06_init_identity_mutations_fail_closed(self) -> None:
        by_id = {fixture.fixture_id: fixture for fixture in FIXTURES}
        health = by_id["runtime-adapter-health-request"]
        shutdown = by_id["runtime-adapter-shutdown-success"]

        for clause_key in ("Cfdd95bba6e3a.1", "C741f42c5aa1e.1"):
            with self.subTest(marker=clause_key):
                fixtures = tuple(
                    replace(
                        fixture,
                        clause_refs=tuple(
                            replace(ref, non_classifying=False) if ref.clause_key == clause_key else ref
                            for ref in fixture.clause_refs
                        ),
                    )
                    if fixture.fixture_id == "runtime-adapter-health-request"
                    else fixture
                    for fixture in FIXTURES
                )
                result = build_claim(self.protocol, fixtures=fixtures)
                self.assertIsInstance(result, ClaimFailure)
                self.assertIn(clause_key, {gap["clause_key"] for gap in result.gaps})

        equal_revision = _thaw(health.trace[1].frame)
        equal_revision["result"]["capability_set"]["revision"] = equal_revision["result"]["adapter_revision"]
        with self.assertRaises(AssertionError):
            _assert_c06_initialize_identity_controls(
                self,
                replace(health, trace=(health.trace[0], TraceFrame("adapter", "host", equal_revision), health.trace[2])),
                shutdown,
            )

        health_supported = _thaw(health.trace[1].frame)
        health_supported["result"]["capability_set"]["capabilities"][0]["quality"] = "authoritative"
        with self.assertRaises(AssertionError):
            _assert_c06_initialize_identity_controls(
                self,
                replace(
                    health,
                    trace=(health.trace[0], TraceFrame("adapter", "host", health_supported), health.trace[2]),
                ),
                shutdown,
            )

        shutdown_added = _thaw(health.trace[1].frame)
        shutdown_added["result"]["capability_set"]["capabilities"].append(
            {"capability": "runtime.shutdown", "quality": "authoritative"}
        )
        with self.assertRaises(AssertionError):
            _assert_c06_initialize_identity_controls(
                self,
                replace(health, trace=(health.trace[0], TraceFrame("adapter", "host", shutdown_added), health.trace[2])),
                shutdown,
            )

        for clause_key in ("Ca25026199e82.1", "Cfdd95bba6e3a.1", "C741f42c5aa1e.1"):
            with self.subTest(removed=clause_key):
                result = build_claim(
                    self.protocol,
                    fixtures=_fixtures_without_ref("runtime-adapter-health-request", clause_key),
                )
                self.assertIsInstance(result, ClaimFailure)
                self.assertIn(clause_key, {gap["clause_key"] for gap in result.gaps})

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

    def test_non_classifying_marker_is_only_for_conforming_must_not_refs(self) -> None:
        c9 = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-reconcile-accepts-varied-binding-capability-profile"
        )
        non_must_not = replace(
            FIXTURES[0],
            clause_refs=(replace(FIXTURES[0].clause_refs[0], non_classifying=True),),
        )
        violating_marker = replace(
            c9,
            polarity=POLARITY_VIOLATING,
            clause_refs=(replace(c9.clause_refs[0], polarity=POLARITY_VIOLATING),),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-non-classifying-keyword"):
            validate_fixtures(self.protocol, (non_must_not,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-non-classifying-polarity"):
            validate_fixtures(self.protocol, (violating_marker,))

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

    def test_shutdown_success_fixture_covers_only_positive_c15_success_clauses(self) -> None:
        shutdown = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-shutdown-success"
        )

        self.assertEqual(shutdown.polarity, POLARITY_CONFORMING)
        self.assertEqual(
            {ref.clause_key for ref in shutdown.clause_refs},
            {"Ce0c84af21a71.1", "C43b913cc99f1.1", "C78f267e558da.1"},
        )
        self.assertNotIn("C78f267e558da.2", {ref.clause_key for ref in shutdown.clause_refs})
        self.assertIsInstance(shutdown.expectation, ExpectedResult)
        self.assertEqual(shutdown.expectation.method, "runtime.shutdown")
        self.assertEqual(_thaw(shutdown.expectation.result), {"status": "shutdown_started"})
        self.assertEqual(shutdown.trace[0].frame["method"], "initialize")
        self.assertIn("result", shutdown.trace[1].frame)
        self.assertEqual(shutdown.trace[2].frame["method"], "runtime.shutdown")
        self.assertEqual(shutdown.trace[2].frame["params"], {})
        self.assertTrue(_replays_fixture(shutdown))

    def test_shutdown_success_fixture_reduces_its_own_base_gaps_by_three(self) -> None:
        fixtures_without_post_shutdown = tuple(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id != "runtime-adapter-post-shutdown-health-refusal"
        )
        result = build_claim(self.protocol, fixtures=fixtures_without_post_shutdown)
        base_result = build_claim(
            self.protocol,
            fixtures=tuple(
                fixture
                for fixture in fixtures_without_post_shutdown
                if fixture.fixture_id != "runtime-adapter-shutdown-success"
            ),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 3)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        for clause_key in ("Ce0c84af21a71.1", "C43b913cc99f1.1", "C78f267e558da.1"):
            self.assertNotIn(clause_key, gap_keys)
        self.assertIn("C78f267e558da.2", gap_keys)

    def test_shutdown_success_fixture_mutations_fail_closed(self) -> None:
        shutdown = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-shutdown-success"
        )
        request = _thaw(shutdown.trace[2].frame)
        request["params"] = {"reason": "test"}
        non_empty_params = replace(shutdown, trace=(*shutdown.trace[:2], TraceFrame("host", "adapter", request)))
        wrong_status = replace(
            shutdown,
            expectation=replace(shutdown.expectation, result={"status": "stopped"}),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-conforming-trace"):
            validate_fixtures(self.protocol, (non_empty_params,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(self.protocol, (wrong_status,))

        for clause_ref in shutdown.clause_refs:
            with self.subTest(clause=clause_ref.clause_key):
                removed_ref = replace(
                    shutdown,
                    clause_refs=tuple(ref for ref in shutdown.clause_refs if ref != clause_ref),
                )
                result = build_claim(
                    self.protocol,
                    fixtures=tuple(
                        removed_ref if fixture.fixture_id == shutdown.fixture_id else fixture
                        for fixture in FIXTURES
                    ),
                )
                self.assertIsInstance(result, ClaimFailure)
                self.assertIn(clause_ref.clause_key, {gap["clause_key"] for gap in result.gaps})

    def test_post_shutdown_refusal_fixture_covers_only_later_work_clause(self) -> None:
        fixture = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-post-shutdown-health-refusal"
        )

        self.assertEqual(fixture.polarity, POLARITY_VIOLATING)
        self.assertEqual({ref.clause_key for ref in fixture.clause_refs}, {"C78f267e558da.2"})
        self.assertIsInstance(fixture.expectation, ExpectedRefusal)
        self.assertEqual(fixture.expectation.error_name, "SHUTDOWN_IN_PROGRESS")
        self.assertEqual(fixture.expectation.error_code, -32016)
        self.assertEqual(fixture.expectation.state_effect, NO_STATE_CHANGE)
        self.assertTrue(fixture.expectation.response_emitted)
        self.assertFalse(fixture.expectation.closes_connection)
        self.assertEqual(fixture.trace[0].frame["method"], "initialize")
        self.assertIn("result", fixture.trace[1].frame)
        self.assertEqual(fixture.trace[2].frame["method"], "runtime.shutdown")
        self.assertEqual(fixture.trace[2].frame["params"], {})
        self.assertEqual(fixture.trace[3].frame["method"], "runtime.health")
        self.assertEqual(fixture.trace[3].frame["params"], {})
        self.assertTrue(_replays_fixture(fixture))
        self.assertTrue(_serves_fixture(fixture))

    def test_post_shutdown_refusal_fixture_reduces_its_own_base_gaps_by_one(self) -> None:
        result = build_claim(self.protocol)
        base_result = build_claim(
            self.protocol,
            fixtures=tuple(
                fixture
                for fixture in FIXTURES
                if fixture.fixture_id != "runtime-adapter-post-shutdown-health-refusal"
            ),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 1)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertNotIn("C78f267e558da.2", gap_keys)
        for clause_key in ("Ce0c84af21a71.1", "C43b913cc99f1.1", "C78f267e558da.1"):
            self.assertNotIn(clause_key, gap_keys)

    def test_post_shutdown_refusal_fixture_mutations_fail_closed(self) -> None:
        fixture = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-post-shutdown-health-refusal"
        )
        malformed_initialize_response = _thaw(fixture.trace[1].frame)
        malformed_initialize_response["result"] = {"status": "anything"}
        bad_initialize_response = replace(
            fixture,
            trace=(
                fixture.trace[0],
                TraceFrame("adapter", "host", malformed_initialize_response),
                *fixture.trace[2:],
            ),
        )
        mismatched_initialize_request = _thaw(fixture.trace[0].frame)
        mismatched_initialize_request["params"]["adapter_id"] = "adapter_other"
        bad_initialize_binding = replace(
            fixture,
            trace=(
                TraceFrame("host", "adapter", mismatched_initialize_request),
                *fixture.trace[1:],
            ),
        )
        without_shutdown = replace(fixture, trace=(*fixture.trace[:2], fixture.trace[3]))
        malformed_later = _thaw(fixture.trace[3].frame)
        malformed_later["params"] = {"unexpected": True}
        bad_later_params = replace(
            fixture,
            trace=(*fixture.trace[:3], TraceFrame("host", "adapter", malformed_later)),
        )
        peer_later_request = replace(
            fixture,
            trace=(*fixture.trace[:3], TraceFrame("adapter", "host", _thaw(fixture.trace[3].frame))),
        )

        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-trace"):
            validate_fixtures(self.protocol, (bad_initialize_response,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-trace"):
            validate_fixtures(self.protocol, (bad_initialize_binding,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-trace"):
            validate_fixtures(self.protocol, (without_shutdown,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating"):
            validate_fixtures(self.protocol, (peer_later_request,))
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-refusal"):
            validate_fixtures(self.protocol, (bad_later_params,))

        result = build_claim(
            self.protocol,
            fixtures=tuple(
                candidate
                for candidate in FIXTURES
                if candidate.fixture_id != fixture.fixture_id
            ),
        )
        self.assertIsInstance(result, ClaimFailure)
        self.assertIn("C78f267e558da.2", {gap["clause_key"] for gap in result.gaps})

    def test_c11_health_fixtures_cover_only_replay_proven_shape_and_selector_clauses(self) -> None:
        by_id = {fixture.fixture_id: fixture for fixture in FIXTURES}
        health = by_id["runtime-adapter-health-request"]
        selector = by_id["runtime-adapter-health-rejects-session-selector"]

        self.assertLessEqual(
            {"C45acb2959726.1", "C358ebcd9608d.1", "C358ebcd9608d.2"},
            {ref.clause_key for ref in health.clause_refs},
        )
        self.assertIsInstance(health.expectation, ExpectedResult)
        self.assertEqual(health.expectation.method, "runtime.health")
        self.assertEqual(_thaw(health.expectation.result)["status"], "healthy")
        self.assertEqual(_thaw(health.expectation.result)["adapter_id"], "adapter_alpha")
        self.assertTrue(_replays_fixture(health))

        self.assertEqual(selector.polarity, POLARITY_VIOLATING)
        self.assertIn("Cf12ffe8bf4a6.1", {ref.clause_key for ref in selector.clause_refs})
        self.assertIsInstance(selector.expectation, ExpectedRefusal)
        self.assertEqual(selector.expectation.error_name, "INVALID_PARAMS")
        self.assertEqual(selector.expectation.error_code, -32602)
        self.assertEqual(selector.expectation.state_effect, NO_STATE_CHANGE)
        self.assertTrue(selector.expectation.response_emitted)
        self.assertFalse(selector.expectation.closes_connection)
        self.assertEqual(selector.trace[2].frame["method"], "runtime.health")
        self.assertIn("session_ref", selector.trace[2].frame["params"])
        self.assertTrue(_replays_fixture(selector))

    def test_c11_health_slice_reduces_its_own_base_gaps_by_three(self) -> None:
        c11_keys = {"C358ebcd9608d.1", "C358ebcd9608d.2", "Cf12ffe8bf4a6.1"}
        selector = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-health-rejects-session-selector"
        )
        result = build_claim(self.protocol)
        base_result = build_claim(
            self.protocol,
            fixtures=tuple(
                fixture
                if fixture.fixture_id != "runtime-adapter-health-request"
                else replace(
                    fixture,
                    clause_refs=tuple(
                        ref
                        for ref in fixture.clause_refs
                        if ref.clause_key not in c11_keys
                    ),
                )
                for fixture in FIXTURES
                if fixture.fixture_id != "runtime-adapter-health-rejects-session-selector"
            )
            + (
                replace(
                    selector,
                    clause_refs=tuple(
                        ref for ref in selector.clause_refs if ref.clause_key != "Cf12ffe8bf4a6.1"
                    ),
                ),
            ),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 3)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        for clause_key in ("C358ebcd9608d.1", "C358ebcd9608d.2", "Cf12ffe8bf4a6.1"):
            self.assertNotIn(clause_key, gap_keys)
        for clause_key in (
            "C358ebcd9608d.3",
            "C2cd9421b9c86.1",
            "Cd5e98b5f64fa.1",
            "C4696f988cd35.1",
            "C947f9da5c155.1",
            "C810ab2059e2a.1",
            "Cacd7574f8bbf.1",
        ):
            self.assertIn(clause_key, gap_keys)

    def test_c11_health_fixture_mutations_fail_closed(self) -> None:
        health = next(fixture for fixture in FIXTURES if fixture.fixture_id == "runtime-adapter-health-request")
        selector = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-health-rejects-session-selector"
        )
        bad_shape = _thaw(health.expectation.result)
        bad_shape.pop("capability_set_revision")
        accepted_selector = _thaw(selector.trace[2].frame)
        accepted_selector["params"] = {}

        with self.assertRaisesRegex(ConformanceFailure, "fixture-result-shape"):
            validate_fixtures(
                self.protocol,
                (replace(health, expectation=replace(health.expectation, result=bad_shape)),),
            )
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-trace"):
            validate_fixtures(
                self.protocol,
                (replace(selector, trace=(*selector.trace[:2], TraceFrame("host", "adapter", accepted_selector))),),
            )
        with self.assertRaisesRegex(ConformanceFailure, "fixture-violating-refusal"):
            validate_fixtures(
                self.protocol,
                (replace(selector, expectation=replace(selector.expectation, error_name="INVALID_REQUEST")),),
            )

        for fixture_id, clause_key in (
            ("runtime-adapter-health-request", "C358ebcd9608d.1"),
            ("runtime-adapter-health-request", "C358ebcd9608d.2"),
            ("runtime-adapter-health-rejects-session-selector", "Cf12ffe8bf4a6.1"),
        ):
            with self.subTest(clause=clause_key):
                fixtures = tuple(
                    replace(
                        fixture,
                        clause_refs=tuple(ref for ref in fixture.clause_refs if ref.clause_key != clause_key),
                    )
                    if fixture.fixture_id == fixture_id
                    else fixture
                    for fixture in FIXTURES
                )
                result = build_claim(self.protocol, fixtures=fixtures)
                self.assertIsInstance(result, ClaimFailure)
                self.assertIn(clause_key, {gap["clause_key"] for gap in result.gaps})

    def test_c13_returned_error_envelopes_are_closed_for_every_response_refusal(self) -> None:
        retryable_by_name = _protocol_retryable_by_name(self.protocol)
        response_refusals = [
            fixture
            for fixture in FIXTURES
            if fixture.polarity == POLARITY_VIOLATING
            and isinstance(fixture.expectation, ExpectedRefusal)
            and fixture.expectation.response_emitted
        ]

        self.assertGreater(len(response_refusals), 1)
        for fixture in response_refusals:
            with self.subTest(fixture=fixture.fixture_id):
                response = _last_replay_response(fixture)
                self.assertIsInstance(response, str)
                _assert_closed_error_envelope(
                    self,
                    json.loads(response),
                    fixture.expectation,
                    retryable_by_name,
                )

    def test_c13_returned_error_slice_reduces_its_own_base_gaps_by_three(self) -> None:
        c13_keys = {"C6b6763d6addb.1", "Cd75a8b8bc595.1", "C1cef357011ab.1"}
        host_local_keys = {"C5d3edf690fb2.1", "C5d3edf690fb2.2", "C1ba88e813bab.1"}
        result = build_claim(self.protocol)
        base_result = build_claim(
            self.protocol,
            fixtures=tuple(
                replace(
                    fixture,
                    clause_refs=tuple(ref for ref in fixture.clause_refs if ref.clause_key not in c13_keys),
                )
                if fixture.fixture_id == "runtime-adapter-health-rejects-session-selector"
                else fixture
                for fixture in FIXTURES
            ),
        )

        self.assertIsInstance(result, ClaimFailure)
        self.assertIsInstance(base_result, ClaimFailure)
        self.assertEqual(len(base_result.gaps) - len(result.gaps), 3)
        gap_keys = {gap["clause_key"] for gap in result.gaps}
        self.assertFalse(c13_keys & gap_keys)
        self.assertLessEqual(host_local_keys, gap_keys)

    def test_c13_returned_error_envelope_mutations_fail_closed(self) -> None:
        fixture = next(
            fixture
            for fixture in FIXTURES
            if fixture.fixture_id == "runtime-adapter-health-rejects-session-selector"
        )
        payload = json.loads(_last_replay_response(fixture) or "{}")
        retryable_by_name = _protocol_retryable_by_name(self.protocol)
        mutations = {
            "wrong-message": lambda value: value["error"].__setitem__("message", "INVALID_REQUEST"),
            "wrong-name": lambda value: value["error"]["data"].__setitem__("name", "INVALID_REQUEST"),
            "string-retryable": lambda value: value["error"]["data"].__setitem__("retryable", "false"),
            "missing-data": lambda value: value["error"].pop("data"),
            "extra-error-member": lambda value: value["error"].__setitem__("extra", True),
            "mismatched-request-id": lambda value: value["error"]["data"].__setitem__("request_id", "other"),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = json.loads(json.dumps(payload))
                mutate(changed)
                with self.assertRaises(AssertionError):
                    _assert_closed_error_envelope(self, changed, fixture.expectation, retryable_by_name)

        result = build_claim(
            self.protocol,
            fixtures=tuple(
                replace(
                    candidate,
                    clause_refs=tuple(ref for ref in candidate.clause_refs if ref.clause_key != "C6b6763d6addb.1"),
                )
                if candidate.fixture_id == fixture.fixture_id
                else candidate
                for candidate in FIXTURES
            ),
        )
        self.assertIsInstance(result, ClaimFailure)
        self.assertIn("C6b6763d6addb.1", {gap["clause_key"] for gap in result.gaps})

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
        codes = protocol_error_codes(self.protocol)

        self.assertEqual(len(codes), 23)
        self.assertIs(codes, ERROR_CODES)
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
            validate_fixtures(without_table)

    def test_catalog_drift_breaks_fixture_validation(self) -> None:
        original = conformance.ERROR_CODES
        changed = dict(original)
        changed["INVALID_FRAMING"] = -32099
        conformance.ERROR_CODES = MappingProxyType(changed)
        try:
            with self.assertRaisesRegex(ConformanceFailure, "fixture-error-codes"):
                validate_fixtures(self.protocol)
        finally:
            conformance.ERROR_CODES = original

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


def _valid_initialize_json(request_id: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": _thaw(_INITIALIZE_PARAMS)},
        sort_keys=True,
        separators=(",", ":"),
    )


def _bad_initialize_param_mutations():
    return {
        "missing": lambda params: params.pop("manifest_revision"),
        "extra": lambda params: params.__setitem__("extra", True),
        "mistyped": lambda params: params.__setitem__("endpoint", {"endpoint_id": "endpoint_alpha"}),
    }


def _handshake_failure_cases():
    bad_params = _thaw(_INITIALIZE_PARAMS)
    bad_params.pop("manifest_revision")
    bad_version = _thaw(_INITIALIZE_PARAMS)
    bad_version["requested_protocol_version"] = "2.0"
    return (
        (
            "bad-params",
            {"jsonrpc": "2.0", "id": "initialize-bad-params", "method": "initialize", "params": bad_params},
            "INVALID_PARAMS",
        ),
        (
            "bad-version",
            {"jsonrpc": "2.0", "id": "initialize-bad-version", "method": "initialize", "params": bad_version},
            "UNSUPPORTED_PROTOCOL_VERSION",
        ),
        (
            "non-init-first",
            {"jsonrpc": "2.0", "id": "health-before-initialize", "method": "runtime.health", "params": {}},
            "INITIALIZE_REQUIRED",
        ),
    )


def _assert_initialize_result_echoes_request(testcase, fixture) -> None:
    request = _thaw(fixture.trace[0].frame)
    response = _thaw(fixture.trace[1].frame)
    testcase.assertEqual(request["method"], "initialize")
    testcase.assertEqual(response["id"], request["id"])
    _assert_initialize_success(testcase, response)
    result = response["result"]
    params = request["params"]
    testcase.assertEqual(result["negotiated_protocol_version"], params["requested_protocol_version"])
    for key in ("adapter_id", "adapter_revision", "manifest_id", "manifest_revision"):
        testcase.assertEqual(result[key], params[key])
    testcase.assertEqual(result["endpoint"], params["endpoint"])
    testcase.assertEqual(result["endpoint"]["capability_set_id"], result["capability_set"]["capability_set_id"])
    testcase.assertEqual(result["endpoint"]["workspace_id"], result["capability_set"]["workspace_id"])
    testcase.assertEqual(result["endpoint"]["scope"], result["capability_set"]["scope"])


def _assert_c06_initialize_identity_controls(testcase, health_fixture, shutdown_fixture) -> None:
    _assert_initialize_result_echoes_request(testcase, health_fixture)
    initialize = _thaw(health_fixture.trace[1].frame)["result"]
    endpoint = initialize["endpoint"]
    capability_set = initialize["capability_set"]
    capabilities = {entry["capability"]: entry for entry in capability_set["capabilities"]}

    testcase.assertEqual(initialize["adapter_id"], endpoint["adapter_name"])
    testcase.assertEqual(initialize["adapter_revision"], endpoint["adapter_revision"])
    testcase.assertNotEqual(capability_set["revision"], initialize["adapter_revision"])
    testcase.assertEqual(capabilities["runtime.health"]["quality"], "unsupported")
    testcase.assertNotIn("runtime.shutdown", capabilities)
    testcase.assertTrue(_replays_fixture(health_fixture))
    testcase.assertTrue(_replays_fixture(shutdown_fixture))


def _assert_initialize_success(testcase, payload) -> None:
    testcase.assertEqual(set(payload), {"jsonrpc", "id", "result"})
    result = payload["result"]
    testcase.assertEqual(
        set(result),
        {
            "negotiated_protocol_version",
            "adapter_id",
            "adapter_revision",
            "manifest_id",
            "manifest_revision",
            "endpoint",
            "capability_set",
        },
    )
    testcase.assertEqual(result["negotiated_protocol_version"], "1.0")


def _thaw(value):
    if hasattr(value, "items"):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _matches_refusal(expectation, response: str | bytes | None) -> bool:
    if not isinstance(expectation, ExpectedRefusal):
        return False
    if response is None:
        return not expectation.response_emitted and expectation.closes_connection
    payload = json.loads(response)
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    return error.get("code") == expectation.error_code and error.get("message") == expectation.error_name


def _replays_fixture(fixture) -> bool:
    response = _last_replay_response(fixture)
    if isinstance(fixture.expectation, ExpectedResult):
        if response is None:
            return False
        payload = json.loads(response or "{}")
        return payload.get("result") == _thaw(fixture.expectation.result)
    return _matches_refusal(fixture.expectation, response)


def _fixtures_without_ref(fixture_id: str, clause_key: str) -> tuple[RuntimeAdapterFixture, ...]:
    return tuple(
        replace(
            fixture,
            clause_refs=tuple(ref for ref in fixture.clause_refs if ref.clause_key != clause_key),
        )
        if fixture.fixture_id == fixture_id
        else fixture
        for fixture in FIXTURES
    )


def _fixtures_without_clause_keys(clause_keys: set[str]) -> tuple[RuntimeAdapterFixture, ...]:
    stripped: list[RuntimeAdapterFixture] = []
    for fixture in FIXTURES:
        refs = tuple(ref for ref in fixture.clause_refs if ref.clause_key not in clause_keys)
        if refs:
            stripped.append(replace(fixture, clause_refs=refs))
    return tuple(stripped)


def _last_replay_response(fixture):
    adapter = ReferenceAdapter()
    host_traces = [trace for trace in fixture.trace if trace.sender == "host" and trace.receiver == "adapter"]
    responses: list[str | bytes | None] = []
    for trace in host_traces:
        frame = _thaw(trace.frame)
        response = adapter.handle_text(json.dumps(frame, sort_keys=True, separators=(",", ":")))
        responses.append(response)
    if not responses:
        return None
    return responses[-1]


def _protocol_retryable_by_name(protocol: str) -> dict[str, bool]:
    rows: dict[str, bool] = {}
    for line in protocol.splitlines():
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) != 3 or not parts[0].startswith("-") or not parts[1].startswith("`"):
            continue
        rows[parts[1].strip("`")] = parts[2].strip("`") == "true"
    return rows


def _assert_closed_error_envelope(testcase, payload, expectation, retryable_by_name) -> None:
    testcase.assertIsInstance(expectation, ExpectedRefusal)
    testcase.assertEqual(set(payload), {"jsonrpc", "id", "error"})
    testcase.assertEqual(payload["jsonrpc"], "2.0")
    error = payload["error"]
    testcase.assertEqual(set(error), {"code", "message", "data"})
    testcase.assertEqual(error["code"], expectation.error_code)
    testcase.assertEqual(error["message"], expectation.error_name)
    data = error["data"]
    testcase.assertEqual(set(data), {"name", "retryable", "request_id"})
    testcase.assertEqual(data["name"], expectation.error_name)
    testcase.assertIs(data["retryable"], retryable_by_name[expectation.error_name])
    testcase.assertEqual(data["request_id"], payload["id"])


def _serves_fixture(fixture) -> bool:
    payload = b"\n".join(
        json.dumps(_thaw(trace.frame), sort_keys=True, separators=(",", ":")).encode("utf-8")
        for trace in fixture.trace
        if trace.sender == "host" and trace.receiver == "adapter"
    )
    stdout = io.BytesIO()
    result = serve(
        adapter=ReferenceAdapter(),
        stdin=io.BytesIO(payload + b"\n"),
        stdout=stdout,
        stderr=io.BytesIO(),
    )
    if result != 0:
        return False
    responses = [line for line in stdout.getvalue().splitlines()]
    if not responses:
        return False
    return _matches_refusal(fixture.expectation, responses[-1])


def _reconcile_response(session_ref):
    adapter = ReferenceAdapter()
    adapter.handle_text(json.dumps(_thaw(FIXTURES[0].trace[0].frame), sort_keys=True, separators=(",", ":")))
    response = adapter.handle_text(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "reconcile-mutated-session",
                "method": "runtime.reconcile",
                "params": {
                    "session_ref": session_ref,
                    "original_request_id": "deliver-1",
                    "delivery_id": "delivery_alpha",
                    "attempt_id": "attempt_alpha",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return json.loads(response or "{}")


if __name__ == "__main__":
    unittest.main()
