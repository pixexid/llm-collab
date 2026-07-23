"""Tests for the Runtime Adapter JSON-RPC V1 reference adapter subject."""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import json
import sys
import unittest
from pathlib import Path
from types import MappingProxyType

import llm_collab.runtime_adapter_conformance as conformance
from llm_collab.runtime_adapter_conformance import (
    ERROR_CODES,
    ConformanceFailure,
    classify_direction,
    load_json_frame,
    protocol_error_codes,
    validate_response,
)
from llm_collab.runtime_adapter_manifest import TrustedManifestRegistry
from llm_collab.runtime_adapter_reference import (
    FAULT_ABSENT_ID_REQUEST,
    FAULT_CLOSED_ENVELOPE,
    FAULT_DUPLICATE_OUTPUT,
    FAULT_INVALID_FRAMING,
    FAULT_OVERSIZED_MESSAGE,
    FAULT_PROHIBITED_ADAPTER_REQUEST,
    FAULT_RESULT_SHAPE,
    FAULT_STDERR_OVERFLOW,
    ReferenceAdapter,
    MAX_MESSAGE_BYTES,
    main,
    serve,
)
from llm_collab.runtime_adapter_supervisor import StdioSupervisor


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_reference.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"
ENDPOINT = {
    "schema_version": 1,
    "workspace_id": "ws_alpha",
    "scope": {"kind": "workspace"},
    "endpoint_id": "endpoint_alpha",
    "agent_id": "agent_alpha",
    "adapter_name": "adapter_alpha",
    "adapter_revision": "adapter_rev1",
    "trust_class": "managed",
    "capability_set_id": "caps_alpha",
    "platform": {"os": "other", "architecture": "test"},
    "configuration_ref": {
        "registry_id": "registry_alpha",
        "revision": "registry_rev1",
        "reference": "reference_alpha",
    },
}


def manifest(*, inject: str | None = None) -> dict:
    argv = [sys.executable, "-m", "llm_collab.runtime_adapter_reference"]
    if inject is not None:
        argv.extend(["--inject", inject])
    return {
        "adapter_alpha": {
            "adapter_id": "adapter_alpha",
            "adapter_revision": "adapter_rev1",
            "manifest_id": "manifest_alpha",
            "manifest_revision": "manifest_rev1",
            "endpoint": ENDPOINT,
            "executable": sys.executable,
            "argv": argv,
            "working_directory": str(ROOT),
            "environment": {"PYTHONUNBUFFERED": "1"},
            "environment_allowlist": ["PYTHONUNBUFFERED"],
        }
    }


def resolved_adapter(*, inject: str | None = None):
    return TrustedManifestRegistry(manifest(inject=inject)).resolve("adapter_alpha")


def frame(method: str, params: dict, request_id: str) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )


def initialize_frame(request_id: str = "initialize-1") -> str:
    return frame(
        "initialize",
        {
            "requested_protocol_version": "1.0",
            "adapter_id": "adapter_alpha",
            "adapter_revision": "adapter_rev1",
            "manifest_id": "manifest_alpha",
            "manifest_revision": "manifest_rev1",
            "endpoint": ENDPOINT,
        },
        request_id,
    )


def canonical_digest(value: dict) -> str:
    material = dict(value)
    material.pop("integrity", None)
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    return "sha256:" + hashlib.sha256(raw).hexdigest()


def session_ref() -> dict:
    evidence = {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "evidence_id": "evidence_session_alpha",
        "evidence_kind": "exact_session_binding",
        "quality": "authoritative",
        "state": "visible",
        "authority": {
            "authority_kind": "trusted_adapter",
            "identity": "adapter_alpha",
            "implementation_revision": "adapter_rev1",
            "capability_profile_id": "runtime_profile",
            "capability_profile_revision": "cap_rev1",
        },
        "subject": {
            "endpoint_id": "endpoint_alpha",
            "session_ref_id": "session_alpha",
            "native_session_id": "native-session-alpha",
        },
        "correlation_id": "corr_session_alpha",
        "observed_at_utc": "2026-07-22T00:00:00Z",
    }
    evidence["integrity"] = canonical_digest(evidence)
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "session_ref_id": "session_alpha",
        "endpoint_id": "endpoint_alpha",
        "native_session_id": "native-session-alpha",
        "evidence": evidence,
    }


class RuntimeAdapterReferenceTests(unittest.TestCase):
    def test_default_adapter_is_inert_and_spawnable_via_module(self) -> None:
        self.assertIsNone(ReferenceAdapter().fault_injection)
        with StdioSupervisor(resolved_adapter()) as supervisor:
            initialized = supervisor.request(initialize_frame(), timeout_seconds=5)
            self.assertIsNone(initialized.fault)
            init_obj = json.loads(initialized.response or "{}")
            validate_response(init_obj, "initialize-1")
            self.assertEqual(init_obj["result"]["negotiated_protocol_version"], "1.0")
            self.assertEqual(init_obj["result"]["endpoint"]["endpoint_id"], "endpoint_alpha")
            self.assertEqual(
                set(init_obj["result"]["capability_set"]),
                {"schema_version", "workspace_id", "scope", "capability_set_id", "revision", "capabilities"},
            )
            self.assertEqual(init_obj["result"]["capability_set"]["capability_set_id"], "caps_alpha")
            self.assertEqual(init_obj["result"]["capability_set"]["capabilities"][0]["quality"], "unsupported")
            self.assertIn(
                {"capability": "runtime_profile", "quality": "authoritative"},
                init_obj["result"]["capability_set"]["capabilities"],
            )
            self.assertIn(
                {"capability": "runtime.reconcile", "quality": "authoritative"},
                init_obj["result"]["capability_set"]["capabilities"],
            )

            health = supervisor.request(frame("runtime.health", {}, "health-1"), timeout_seconds=5)
            self.assertIsNone(health.fault)
            health_obj = json.loads(health.response or "{}")
            validate_response(health_obj, "health-1")
            self.assertEqual(
                set(health_obj["result"]),
                {
                    "status",
                    "negotiated_protocol_version",
                    "adapter_id",
                    "adapter_revision",
                    "manifest_id",
                    "manifest_revision",
                    "endpoint_id",
                    "workspace_id",
                    "scope_kind",
                    "capability_set_id",
                    "capability_set_revision",
                },
            )
            self.assertEqual(health_obj["result"]["status"], "healthy")

    def test_default_adapter_rejects_post_initialize_method_before_successful_initialize(self) -> None:
        adapter = ReferenceAdapter()
        response = json.loads(adapter.handle_text(frame("runtime.health", {}, "health-1")) or "{}")

        self.assertEqual(response["error"]["data"]["name"], "INITIALIZE_REQUIRED")
        self.assertEqual(response["error"]["code"], -32005)

    def test_default_adapter_rejects_bad_initialize_without_partial_binding(self) -> None:
        adapter = ReferenceAdapter()
        bad = json.loads(initialize_frame())
        bad["params"]["requested_protocol_version"] = "2.0"
        rejected = json.loads(adapter.handle_text(json.dumps(bad)) or "{}")
        self.assertEqual(rejected["error"]["data"]["name"], "UNSUPPORTED_PROTOCOL_VERSION")

        self.assertIsNone(adapter.handle_text(initialize_frame()))

    def test_unknown_method_returns_method_not_found_without_action(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(adapter.handle_text(frame("runtime.unknown", {}, "unknown-1")) or "{}")

        self.assertEqual(response["error"]["data"]["name"], "METHOD_NOT_FOUND")
        self.assertEqual(response["error"]["code"], -32601)

    def test_error_codes_match_protocol_table(self) -> None:
        protocol_codes = protocol_error_codes(PROTOCOL_PATH.read_text(encoding="utf-8"))

        self.assertEqual(len(protocol_codes), 23)
        self.assertIs(ERROR_CODES, protocol_codes)
        self.assertEqual(ERROR_CODES["INITIALIZE_REQUIRED"], -32005)

    def test_reference_error_lookup_uses_shared_catalog_at_call_time(self) -> None:
        original = conformance.ERROR_CODES
        changed = dict(original)
        changed["INITIALIZE_REQUIRED"] = -32099
        conformance.ERROR_CODES = MappingProxyType(changed)
        try:
            adapter = ReferenceAdapter()
            response = json.loads(adapter.handle_text(frame("runtime.health", {}, "health-1")) or "{}")
        finally:
            conformance.ERROR_CODES = original

        self.assertEqual(response["error"]["data"]["name"], "INITIALIZE_REQUIRED")
        self.assertEqual(response["error"]["code"], -32099)

    def test_duplicate_bearing_input_returns_parse_error_without_initializing(self) -> None:
        adapter = ReferenceAdapter()
        duplicate = (
            '{"jsonrpc":"2.0","id":"initialize-1","method":"initialize","params":{'
            '"requested_protocol_version":"1.0",'
            '"adapter_id":"wrong",'
            '"adapter_id":"adapter_alpha",'
            '"adapter_revision":"adapter_rev1",'
            '"manifest_id":"manifest_alpha",'
            '"manifest_revision":"manifest_rev1",'
            f'"endpoint":{json.dumps(ENDPOINT, sort_keys=True, separators=(",", ":"))}'
            "}}"
        )
        response = json.loads(adapter.handle_text(duplicate) or "{}")

        self.assertEqual(response["error"]["data"]["name"], "PARSE_ERROR")
        self.assertIsNone(response["id"])
        self.assertIsNone(adapter.handle_text(frame("runtime.health", {}, "health-1")))

    def test_non_finite_json_tokens_are_parse_errors_before_dispatch(self) -> None:
        adapter = ReferenceAdapter()
        response = json.loads(
            adapter.handle_text('{"jsonrpc":"2.0","id":"initialize-1","method":"initialize","params":{"x":NaN}}')
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "PARSE_ERROR")
        self.assertIsNone(response["id"])
        self.assertIsNone(adapter.handle_text(initialize_frame()))

    def test_parser_depth_failure_is_parse_error(self) -> None:
        adapter = ReferenceAdapter()
        response = json.loads(adapter.handle_text("[" * 10_000 + "]" * 10_000) or "{}")

        self.assertEqual(response["error"]["data"]["name"], "PARSE_ERROR")
        self.assertIsNone(response["id"])

    def test_host_to_adapter_response_frame_closes_without_response(self) -> None:
        adapter = ReferenceAdapter()

        self.assertIsNone(adapter.handle_text('{"jsonrpc":"2.0","id":"r1","result":{}}'))
        self.assertIsNone(adapter.handle_text(initialize_frame()))

    def test_idless_malformed_envelope_returns_invalid_request(self) -> None:
        adapter = ReferenceAdapter()
        response = json.loads(adapter.handle_text('{"jsonrpc":"2.0","params":{}}') or "{}")

        self.assertEqual(response["error"]["data"]["name"], "INVALID_REQUEST")
        self.assertIsNone(response["id"])

    def test_invalid_request_ids_use_null_correlation_without_action(self) -> None:
        cases = ("", "\ud800", 1.5, 2**53, None, True)
        for request_id in cases:
            with self.subTest(request_id=request_id):
                adapter = ReferenceAdapter()
                payload = json.loads(initialize_frame())
                payload["id"] = request_id
                response = json.loads(adapter.handle_text(json.dumps(payload)) or "{}")
                self.assertIsNone(response["id"])
                self.assertEqual(response["error"]["data"]["name"], "INVALID_REQUEST")
                self.assertIsNone(adapter.handle_text(frame("runtime.health", {}, "health-1")))

    def test_non_initialize_first_request_terminates_connection(self) -> None:
        adapter = ReferenceAdapter()
        response = json.loads(adapter.handle_text(frame("runtime.health", {}, "health-1")) or "{}")

        self.assertEqual(response["error"]["data"]["name"], "INITIALIZE_REQUIRED")
        self.assertIsNone(adapter.handle_text(initialize_frame()))

    def test_second_initialize_is_rejected_after_successful_handshake(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(adapter.handle_text(initialize_frame("initialize-2")) or "{}")

        self.assertEqual(response["error"]["data"]["name"], "INVALID_REQUEST")

    def test_explicit_duplicate_output_fault_is_host_classified_not_self_labelled(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_DUPLICATE_OUTPUT)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        self.assertIsNone(outcome.fault)
        self.assertNotIn("violation", outcome.response or "")
        with self.assertRaisesRegex(ConformanceFailure, "duplicate-member"):
            load_json_frame(outcome.response or "")

    def test_explicit_prohibited_adapter_request_fault_is_host_classified(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_PROHIBITED_ADAPTER_REQUEST)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        obj = load_json_frame(outcome.response or "")
        direction = classify_direction("adapter", "host", obj)
        self.assertFalse(direction.direction_valid)
        self.assertEqual(direction.fault, "INVALID_REQUEST")
        self.assertTrue(direction.should_close)
        self.assertTrue(direction.should_quarantine)

    def test_explicit_absent_id_request_fault_is_distinct_data(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_ABSENT_ID_REQUEST)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        obj = load_json_frame(outcome.response or "")
        self.assertNotIn("id", obj)
        direction = classify_direction("adapter", "host", obj)
        self.assertFalse(direction.direction_valid)
        self.assertEqual(direction.fault, "INVALID_REQUEST")

    def test_explicit_invalid_framing_fault_is_supervisor_classified(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_INVALID_FRAMING)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        self.assertEqual(outcome.fault, "INVALID_FRAMING")
        self.assertTrue(outcome.should_close)

    def test_explicit_oversized_message_fault_is_supervisor_classified(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_OVERSIZED_MESSAGE)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        self.assertEqual(outcome.fault, "MESSAGE_TOO_LARGE")
        self.assertTrue(outcome.should_close)

    def test_explicit_stderr_overflow_fault_is_supervisor_classified(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_STDERR_OVERFLOW)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        self.assertEqual(outcome.fault, "STDERR_LIMIT_EXCEEDED")
        self.assertTrue(outcome.should_close)
        self.assertTrue(outcome.stderr_truncated)

    def test_explicit_result_shape_fault_emits_bad_result_without_self_classification(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_RESULT_SHAPE)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        obj = load_json_frame(outcome.response or "")
        validate_response(obj, "fault")
        self.assertEqual(obj["result"], {"status": "healthy"})
        self.assertNotIn("fault", obj["result"])
        self.assertNotIn("violation", obj["result"])

    def test_explicit_closed_envelope_fault_is_host_classified(self) -> None:
        with StdioSupervisor(resolved_adapter(inject=FAULT_CLOSED_ENVELOPE)) as supervisor:
            outcome = supervisor.request(initialize_frame(), timeout_seconds=5)
        obj = load_json_frame(outcome.response or "")
        with self.assertRaisesRegex(ConformanceFailure, "closed-response"):
            validate_response(obj, "fault")

    def test_reconcile_returns_identity_bound_receipt(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        bound_session_ref = session_ref()
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": bound_session_ref,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        receipt = response["result"]
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["workspace_id"], "ws_alpha")
        self.assertEqual(receipt["message_id"], "msg_alpha")
        self.assertEqual(receipt["delivery_id"], "delivery_alpha")
        self.assertEqual(receipt["attempt_id"], "attempt_alpha")
        self.assertEqual(receipt["endpoint_id"], "endpoint_alpha")
        self.assertEqual(receipt["session_ref_id"], "session_alpha")
        self.assertEqual(receipt["evidence"]["subject"]["message_id"], "msg_alpha")
        self.assertEqual(receipt["evidence"]["authority"]["capability_profile_id"], "runtime_profile")
        self.assertTrue(receipt["evidence"]["integrity"].startswith("sha256:"))

    def test_reconcile_accepts_session_ref_extensions(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        extended = session_ref()
        extended["extensions"] = {"x_note_trace": "optional"}
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": extended,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["result"]["message_id"], "msg_alpha")

    def test_reconcile_rejects_session_ref_extension_schema_drift(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        extended = session_ref()
        extended["extensions"] = {"trace": "optional"}
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": extended,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")

    def test_reconcile_accepts_safe_integer_original_request_id(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": session_ref(),
                        "original_request_id": 42,
                        "delivery_id": "delivery_numeric",
                        "attempt_id": "attempt_numeric",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["result"]["message_id"], "msg_numeric")

    def test_reconcile_refuses_unknown_original_request_instead_of_fabricating_message_id(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": session_ref(),
                        "original_request_id": "unknown-request",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_PARAMS")

    def test_reconcile_refuses_known_original_request_with_mismatched_delivery_identity(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        for field, value in {
            "delivery_id": "delivery_beta",
            "attempt_id": "attempt_beta",
        }.items():
            with self.subTest(field=field):
                params = {
                    "session_ref": session_ref(),
                    "original_request_id": "deliver-1",
                    "delivery_id": "delivery_alpha",
                    "attempt_id": "attempt_alpha",
                }
                params[field] = value
                response = json.loads(
                    adapter.handle_text(frame("runtime.reconcile", params, f"reconcile-{field}")) or "{}"
                )

                self.assertEqual(response["error"]["data"]["name"], "INVALID_DELIVERY")
                self.assertEqual(response["error"]["code"], -32009)

    def test_reconcile_keeps_malformed_delivery_identity_as_invalid_params(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": session_ref(),
                        "original_request_id": "deliver-1",
                        "delivery_id": [],
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-malformed-delivery",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_PARAMS")

    def test_reconcile_rejects_known_original_with_mismatched_session_identity(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        mismatched = session_ref()
        mismatched["session_ref_id"] = "session_beta"
        mismatched["native_session_id"] = "native-session-beta"
        mismatched["evidence"]["subject"]["session_ref_id"] = "session_beta"
        mismatched["evidence"]["subject"]["native_session_id"] = "native-session-beta"
        mismatched["evidence"]["integrity"] = canonical_digest(mismatched["evidence"])
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": mismatched,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-session-mismatch",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")
        self.assertEqual(response["error"]["code"], -32008)

    def test_reconcile_rejects_incomplete_session_ref_without_scope_mutation(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": {
                            "workspace_id": "ws_other",
                            "scope": {"kind": "project", "project_id": "foreign"},
                            "endpoint_id": "endpoint_alpha",
                            "session_ref_id": "session_alpha",
                        },
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )
        self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")
        self.assertEqual(response["error"]["code"], -32008)
        health = json.loads(adapter.handle_text(frame("runtime.health", {}, "health-1")) or "{}")
        self.assertEqual(health["result"]["scope_kind"], "workspace")

    def test_reconcile_rejects_project_scope_for_workspace_endpoint(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        foreign_scope = session_ref()
        foreign_scope["scope"] = {"kind": "project", "project_id": "other_project"}
        foreign_scope["evidence"]["scope"] = foreign_scope["scope"]
        foreign_scope["evidence"]["integrity"] = canonical_digest(foreign_scope["evidence"])
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": foreign_scope,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")
        health = json.loads(adapter.handle_text(frame("runtime.health", {}, "health-1")) or "{}")
        self.assertEqual(health["result"]["scope_kind"], "workspace")

    def test_reconcile_rejects_malformed_session_evidence_tokens_before_receipt(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        malformed = session_ref()
        malformed["evidence"]["correlation_id"] = []
        malformed["evidence"]["integrity"] = canonical_digest(malformed["evidence"])
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": malformed,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")

    def test_reconcile_rejects_repository_binding_on_workspace_endpoint_as_session_identity(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        bound = session_ref()
        bound["repository_binding"] = {"project_id": "other_project", "repo_id": "repo_alpha"}
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": bound,
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_alpha",
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )

        self.assertEqual(response["error"]["data"]["name"], "INVALID_SESSION_REF")

    def test_shutdown_returns_exact_success_shape(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(adapter.handle_text(frame("runtime.shutdown", {}, "shutdown-1")) or "{}")

        self.assertEqual(response["result"], {"status": "shutdown_started"})
        later = json.loads(adapter.handle_text(frame("runtime.health", {}, "health-after-shutdown")) or "{}")
        self.assertEqual(later["error"]["data"]["name"], "SHUTDOWN_IN_PROGRESS")
        handshake = json.loads(adapter.handle_text(initialize_frame("initialize-after-shutdown")) or "{}")
        self.assertEqual(handshake["error"]["data"]["name"], "SHUTDOWN_IN_PROGRESS")

    def test_serve_exits_after_shutdown_ack(self) -> None:
        payload = (initialize_frame() + "\n" + frame("runtime.shutdown", {}, "shutdown-1") + "\n").encode("utf-8")
        stdout = io.BytesIO()
        result = serve(
            adapter=ReferenceAdapter(),
            stdin=io.BytesIO(payload),
            stdout=stdout,
            stderr=io.BytesIO(),
        )
        responses = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines()]

        self.assertEqual(result, 0)
        self.assertEqual([response["id"] for response in responses], ["initialize-1", "shutdown-1"])
        self.assertEqual(responses[-1]["result"], {"status": "shutdown_started"})

    def test_serve_bounds_stdin_and_classifies_physical_framing_failures(self) -> None:
        cases = {
            "too-large": (b"x" * (MAX_MESSAGE_BYTES + 2), "MESSAGE_TOO_LARGE"),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name):
                stdout = io.BytesIO()
                result = serve(
                    adapter=ReferenceAdapter(),
                    stdin=io.BytesIO(payload),
                    stdout=stdout,
                    stderr=io.BytesIO(),
                )
                self.assertEqual(result, 0)
                response = json.loads(stdout.getvalue().decode("utf-8"))
                self.assertEqual(response["error"]["data"]["name"], expected)

    def test_serve_closes_without_response_on_unrecoverable_invalid_framing(self) -> None:
        for name, payload in {
            "missing-newline": initialize_frame().encode("utf-8"),
            "non-utf8": b"\xff\n",
        }.items():
            with self.subTest(name=name):
                stdout = io.BytesIO()
                result = serve(
                    adapter=ReferenceAdapter(),
                    stdin=io.BytesIO(payload),
                    stdout=stdout,
                    stderr=io.BytesIO(),
                )
                self.assertEqual(result, 0)
                self.assertEqual(stdout.getvalue(), b"")

    def test_reconcile_input_that_would_oversize_response_is_rejected(self) -> None:
        adapter = ReferenceAdapter()
        adapter.handle_text(initialize_frame())
        response = json.loads(
            adapter.handle_text(
                frame(
                    "runtime.reconcile",
                    {
                        "session_ref": session_ref(),
                        "original_request_id": "deliver-1",
                        "delivery_id": "delivery_" + ("x" * 600_000),
                        "attempt_id": "attempt_alpha",
                    },
                    "reconcile-1",
                )
            )
            or "{}"
        )
        self.assertEqual(response["error"]["data"]["name"], "INVALID_PARAMS")

    def test_adapter_imports_only_allowed_protocol_constants_from_host_modules(self) -> None:
        allowed_from = {
            "llm_collab.runtime_adapter_conformance": {
                "ERROR_CODES",
                "JSONRPC_VERSION",
                "NEGOTIATED_PROTOCOL_VERSION",
                "error_code",
                "validate_endpoint_v1",
                "validate_session_ref_v1",
            },
            "llm_collab.runtime_adapter_requests": {
                "METHOD_CANCEL",
                "METHOD_DELIVER",
                "METHOD_HEALTH",
                "METHOD_RECONCILE",
                "METHOD_SHUTDOWN",
            },
        }
        forbidden_modules = {
            "llm_collab.runtime_adapter_lifecycle",
            "llm_collab.runtime_adapter_redaction",
            "llm_collab.runtime_adapter_state",
            "llm_collab.runtime_adapter_supervisor",
            "llm_collab.runtime_adapter_fixtures",
            "llm_collab.canonical",
            "llm_collab.ledger",
            "llm_collab.compatibility",
            "llm_collab.inbox",
            "llm_collab.daemon",
            "llm_collab.registry",
            "llm_collab.project_issue_queue",
            "llm_collab.manifest_provenance",
        }
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("llm_collab."), alias.name)
                    self.assertNotIn(alias.name, {"os", "pathlib", "subprocess"})
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn(module, forbidden_modules)
                if module.startswith("llm_collab."):
                    self.assertIn(module, allowed_from)
                    self.assertLessEqual({alias.name for alias in node.names}, allowed_from[module])

    def test_no_environment_or_filesystem_configuration_reads(self) -> None:
        forbidden_calls = {"open", "exec", "eval", "compile"}
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name in {"os", "pathlib"} for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, {"os", "pathlib"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.assertFalse(node.value.id == "sys" and node.attr == "path")

    def test_module_has_main_guard_but_no_bin_consumer(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', source)
        for path in (ROOT / "bin").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(alias.name == "llm_collab.runtime_adapter_reference" for alias in node.names),
                        path,
                    )
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "llm_collab.runtime_adapter_reference", path)
                    if node.module == "llm_collab":
                        self.assertFalse(
                            any(alias.name == "runtime_adapter_reference" for alias in node.names),
                            path,
                        )

    def test_resolved_adapter_is_spawn_source_not_raw_process_arguments(self) -> None:
        resolved = resolved_adapter()
        self.assertEqual(resolved.argv[:3], (sys.executable, "-m", "llm_collab.runtime_adapter_reference"))
        raw_execution_inputs = {
            "executable",
            "path",
            "env",
            "environment",
            "working_directory",
            "workdir",
            "shell",
            "manifest_path",
            "adapter_alias",
        }
        self.assertFalse(set(inspect.signature(ReferenceAdapter.__init__).parameters) & raw_execution_inputs)
        self.assertFalse(set(inspect.signature(main).parameters) & raw_execution_inputs)


if __name__ == "__main__":
    unittest.main()
