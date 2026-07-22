import ast
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from types import MappingProxyType

from llm_collab import runtime_adapter_redaction as redaction
from llm_collab.runtime_adapter_redaction import (
    REDACTION_FAILURE,
    RedactedDocument,
    RedactionFailure,
    redact_document,
)
from llm_collab.runtime_adapter_conformance import METHODS
from llm_collab.runtime_adapter_supervisor import MAX_STDERR_BYTES_PER_CONNECTION


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_redaction.py"
PROTOCOL_PATH = ROOT / "docs" / "protocols" / "runtime-adapter-jsonrpc-v1.md"


class RuntimeAdapterRedactionTests(unittest.TestCase):
    def test_allowlisted_fields_are_returned_in_distinct_frozen_wrapper(self):
        result = redact_document(
            {
                "fault": "STDERR_LIMIT_EXCEEDED",
                "method": "runtime.deliver",
                "stderr": {"prefix": b"adapter stderr", "total_bytes": 14, "truncated": False},
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        self.assertIsInstance(result.payload, MappingProxyType)
        self.assertEqual(
            result.as_dict(),
            {
                "fault": "STDERR_LIMIT_EXCEEDED",
                "method": "runtime.deliver",
                "stderr": {
                    "total_bytes": 14,
                    "retained_bytes": 14,
                    "truncated": False,
                },
            },
        )
        with self.assertRaises(TypeError):
            result.payload["fault"] = "mutated"

    def test_unlisted_keys_and_drop_only_fields_do_not_reach_output(self):
        result = redact_document(
            {
                "fault": "REQUEST_TIMEOUT",
                "authorization": "Bearer secret",
                "raw_payload": {"token": "secret"},
                "configuration_ref": {"resolved": "/Users/pixexid/secret"},
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        payload = result.as_dict()
        self.assertEqual(payload, {"fault": "REQUEST_TIMEOUT"})
        self.assertNotIn("authorization", payload)
        self.assertNotIn("raw_payload", payload)
        self.assertNotIn("configuration_ref", payload)

    def test_unreducible_input_fails_closed_without_partial_value(self):
        result = redact_document({"fault": "REQUEST_TIMEOUT", "diagnostic": ["not", "object"]})

        self.assertIsInstance(result, RedactionFailure)
        self.assertEqual(result.fault, REDACTION_FAILURE)
        self.assertNotIsInstance(result, RedactedDocument)

    def test_non_object_root_fails_closed_without_traceback(self):
        result = redact_document(["not", "object"])

        self.assertIsInstance(result, RedactionFailure)
        self.assertEqual(result.fault, REDACTION_FAILURE)
        self.assertEqual(result.reason, "non_mapping_root")

    def test_mapping_traversal_errors_become_redaction_failure(self):
        class BadMapping(dict):
            def items(self):
                raise RuntimeError("contains secret-token")

        result = redact_document(BadMapping({"fault": "REQUEST_TIMEOUT"}))

        self.assertIsInstance(result, RedactionFailure)
        self.assertEqual(result.fault, REDACTION_FAILURE)
        self.assertEqual(result.reason, "unexpected_redaction_exception")
        self.assertNotIn("secret-token", result.reason)

    def test_unexpected_exception_class_name_does_not_reach_failure_reason(self):
        class Authorization_Bearer_secret_token(Exception):
            pass

        class BadMapping(dict):
            def items(self):
                raise Authorization_Bearer_secret_token()

        result = redact_document(BadMapping({"fault": "REQUEST_TIMEOUT"}))

        self.assertIsInstance(result, RedactionFailure)
        self.assertEqual(result.reason, "unexpected_redaction_exception")
        self.assertNotIn("Authorization", result.reason)
        self.assertNotIn("secret", result.reason)

    def test_public_redaction_error_text_does_not_reach_failure_reason(self):
        class BadMapping(dict):
            def items(self):
                raise redaction.RedactionError("Authorization: Bearer secret-token")

        result = redact_document(BadMapping({"fault": "REQUEST_TIMEOUT"}))

        self.assertIsInstance(result, RedactionFailure)
        self.assertEqual(result.fault, REDACTION_FAILURE)
        self.assertEqual(result.reason, redaction.REDACTION_REASON_GENERIC)
        self.assertNotIn("Authorization", result.reason)
        self.assertNotIn("secret-token", result.reason)

    def test_redacted_document_cannot_be_directly_constructed_by_consumer(self):
        with self.assertRaises(TypeError):
            RedactedDocument({"fault": "REQUEST_TIMEOUT"})

    def test_redacted_document_payload_cannot_be_reassigned(self):
        result = redact_document({"fault": "REQUEST_TIMEOUT"})

        self.assertIsInstance(result, RedactedDocument)
        with self.assertRaises(TypeError):
            result._payload = {"raw_payload": "Authorization: Bearer secret-token"}
        self.assertEqual(result.as_dict(), {"fault": "REQUEST_TIMEOUT"})

    def test_stderr_shape_is_prefix_counts_and_truncation_marker_only(self):
        result = redact_document(
            {
                "stderr": {
                    "prefix": b"a" * 10,
                    "total_bytes": MAX_STDERR_BYTES_PER_CONNECTION + 5,
                    "truncated": True,
                }
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        stderr = result.as_dict()["stderr"]
        self.assertEqual(set(stderr), {"total_bytes", "retained_bytes", "truncated"})
        self.assertEqual(stderr["retained_bytes"], 10)
        self.assertEqual(stderr["total_bytes"], MAX_STDERR_BYTES_PER_CONNECTION + 5)
        self.assertIs(stderr["truncated"], True)
        self.assertNotIn("tail", stderr)
        self.assertNotIn("discarded_digest", stderr)

    def test_stderr_tail_or_digest_fields_fail_closed(self):
        for extra in ("tail", "discarded_digest"):
            with self.subTest(extra=extra):
                result = redact_document(
                    {"stderr": {"prefix": b"prefix", "total_bytes": 10, "truncated": True, extra: "x"}}
                )
                self.assertIsInstance(result, RedactionFailure)

    def test_stderr_truncation_marker_is_behavioral_not_constant_only(self):
        kept = b"x" * 5
        result = redact_document(
            {"stderr": {"prefix": kept, "total_bytes": len(kept) + 1, "truncated": False}}
        )

        self.assertIsInstance(result, RedactedDocument)
        stderr = result.as_dict()["stderr"]
        self.assertEqual(stderr["retained_bytes"], 5)
        self.assertEqual(stderr["total_bytes"], 6)
        self.assertIs(stderr["truncated"], True)

    def test_stderr_prefix_never_persists_raw_secret_text(self):
        secret = "Authorization: Bearer secret-token"
        result = redact_document(
            {"stderr": {"prefix": secret, "total_bytes": len(secret), "truncated": False}}
        )

        self.assertIsInstance(result, RedactedDocument)
        stderr = result.as_dict()["stderr"]
        self.assertNotIn("prefix", stderr)
        self.assertEqual(stderr["retained_bytes"], len(secret))

    def test_oversized_stderr_prefix_is_bounded_without_traceback(self):
        raw = b"x" * (MAX_STDERR_BYTES_PER_CONNECTION + 1)
        result = redact_document(
            {"stderr": {"prefix": raw, "total_bytes": len(raw), "truncated": True}}
        )

        self.assertIsInstance(result, RedactedDocument)
        stderr = result.as_dict()["stderr"]
        self.assertEqual(set(stderr), {"total_bytes", "retained_bytes", "truncated"})
        self.assertEqual(stderr["retained_bytes"], MAX_STDERR_BYTES_PER_CONNECTION)
        self.assertEqual(stderr["total_bytes"], MAX_STDERR_BYTES_PER_CONNECTION + 1)
        self.assertIs(stderr["truncated"], True)

    def test_stderr_total_bytes_validates_original_prefix_before_capping(self):
        raw = b"x" * (MAX_STDERR_BYTES_PER_CONNECTION + 1)
        result = redact_document(
            {
                "stderr": {
                    "prefix": raw,
                    "total_bytes": MAX_STDERR_BYTES_PER_CONNECTION,
                    "truncated": False,
                }
            }
        )

        self.assertIsInstance(result, RedactionFailure)

    def test_diagnostic_free_text_fields_are_dropped_not_placeholdered(self):
        result = redact_document(
            {
                "fault": "ADAPTER_UNHEALTHY",
                "diagnostic": {
                    "detail": "Authorization: Bearer secret-token",
                    "reason": "Cookie: session=secret",
                    "code": -9,
                },
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        diagnostic = result.as_dict()["diagnostic"]
        self.assertNotIn("detail", diagnostic)
        self.assertNotIn("reason", diagnostic)
        self.assertEqual(diagnostic["code"], -9)

    def test_identifier_hash_is_stable_across_processes(self):
        identifier = "NativeSessionId-0123456789abcdef-0123456789abcdef"
        first = redact_document({"native_session_id": identifier})
        self.assertIsInstance(first, RedactedDocument)
        code = textwrap.dedent(
            f"""
            from llm_collab.runtime_adapter_redaction import redact_document
            print(redact_document({{"native_session_id": {identifier!r}}}).as_dict()["native_session_id"])
            """
        )
        second = subprocess.check_output(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            text=True,
        ).strip()

        self.assertEqual(first.as_dict()["native_session_id"], second)
        self.assertRegex(second, r"^sha256:[0-9a-f]{64}$")

    def test_short_allowlisted_identifier_is_hashed_by_static_field_rule(self):
        result = redact_document({"native_session_id": "short-session-id", "fault": "REQUEST_TIMEOUT"})

        self.assertIsInstance(result, RedactedDocument)
        payload = result.as_dict()
        self.assertEqual(payload["fault"], "REQUEST_TIMEOUT")
        self.assertRegex(payload["native_session_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(payload["native_session_id"], "short-session-id")

    def test_schema_identity_references_and_request_ids_remain_exact(self):
        result = redact_document(
            {
                "adapter_id": "adapter.alpha",
                "request_id": "req-string-1",
                "workspace_id": "ws_alpha",
                "fault": "REQUEST_TIMEOUT",
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        payload = result.as_dict()
        self.assertEqual(payload["adapter_id"], "adapter.alpha")
        self.assertEqual(payload["request_id"], "req-string-1")
        self.assertEqual(payload["workspace_id"], "ws_alpha")
        self.assertEqual(payload["fault"], "REQUEST_TIMEOUT")

    def test_identity_fields_reject_bytes_instead_of_decoding_secret_text(self):
        for field in ("adapter_id", "workspace_id", "request_id", "native_session_id"):
            with self.subTest(field=field):
                result = redact_document(
                    {"fault": "REQUEST_TIMEOUT", field: b"Authorization: Bearer secret-token"}
                )
                self.assertIsInstance(result, RedactionFailure)
                self.assertNotIn("Authorization", result.reason)
                self.assertNotIn("secret-token", result.reason)

    def test_numeric_request_id_survives_exactly(self):
        result = redact_document({"request_id": -1, "fault": "REQUEST_TIMEOUT"})

        self.assertIsInstance(result, RedactedDocument)
        self.assertEqual(result.as_dict()["request_id"], -1)

    def test_unproven_string_values_drop_field_not_document(self):
        result = redact_document(
            {
                "fault": "Authorization: Bearer secret-token",
                "method": "Authorization: Bearer secret-token",
                "kind": "Authorization: Bearer secret-token",
                "diagnostic": {
                    "fault": "Authorization: Bearer secret-token",
                    "code": "Authorization: Bearer secret-token",
                    "detail": "secret text",
                    "reason": "secret text",
                },
                "adapter_id": "adapter.alpha",
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        self.assertEqual(result.as_dict(), {"adapter_id": "adapter.alpha"})

    def test_protocol_fault_literals_are_retained_by_closed_allowlist(self):
        protocol_faults = _protocol_fault_literals()
        self.assertGreaterEqual(len(protocol_faults), 23)
        missing = sorted(protocol_faults - redaction._FAULT_VALUES)
        self.assertEqual(missing, [])

        for fault in sorted(protocol_faults):
            with self.subTest(fault=fault):
                result = redact_document({"fault": fault})
                self.assertIsInstance(result, RedactedDocument)
                self.assertEqual(result.as_dict(), {"fault": fault})

    def test_protocol_method_literals_are_retained_by_closed_allowlist(self):
        self.assertTrue(METHODS)
        missing = sorted(METHODS - redaction._METHOD_VALUES)
        self.assertEqual(missing, [])

        for method in sorted(METHODS):
            with self.subTest(method=method):
                result = redact_document({"method": method})
                self.assertIsInstance(result, RedactedDocument)
                self.assertEqual(result.as_dict(), {"method": method})

    def test_kind_is_not_an_advertised_supported_field(self):
        result = redact_document({"fault": "ADAPTER_UNHEALTHY", "kind": "quarantine"})

        self.assertIsInstance(result, RedactedDocument)
        self.assertEqual(result.as_dict(), {"fault": "ADAPTER_UNHEALTHY"})
        self.assertNotIn("kind", redaction._SAFE_TOP_LEVEL_FIELDS)

    def test_malformed_non_string_closed_literal_still_fails_closed(self):
        for document in (
            {"fault": 1},
            {"method": 1},
        ):
            with self.subTest(document=document):
                result = redact_document(document)
                self.assertIsInstance(result, RedactionFailure)

    def test_non_amiga_project_derived_values_follow_same_shared_contract(self):
        result = redact_document(
            {
                "project_id": "nuvyr",
                "project_slug": "nuvyr",
                "workspace_id": "ws_nuvyr",
                "fault": "REQUEST_TIMEOUT",
            }
        )

        self.assertIsInstance(result, RedactedDocument)
        payload = result.as_dict()
        self.assertEqual(payload["project_id"], "nuvyr")
        self.assertEqual(payload["workspace_id"], "ws_nuvyr")
        self.assertNotIn("project_slug", payload)

    def test_drop_only_low_entropy_fields_are_still_removed_by_schema(self):
        result = redact_document({"project_slug": "amiga", "fault": "REQUEST_TIMEOUT"})

        self.assertIsInstance(result, RedactedDocument)
        self.assertEqual(result.as_dict(), {"fault": "REQUEST_TIMEOUT"})

    def test_negative_diagnostic_numbers_survive_redaction(self):
        result = redact_document(
            {"fault": "ADAPTER_UNHEALTHY", "diagnostic": {"code": -9, "detail": -1.5, "reason": -2}}
        )

        self.assertIsInstance(result, RedactedDocument)
        self.assertEqual(
            result.as_dict(),
            {
                "fault": "ADAPTER_UNHEALTHY",
                "diagnostic": {"code": -9, "detail": -1.5, "reason": -2},
            },
        )

    def test_nan_and_infinity_still_fail_closed(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                result = redact_document({"diagnostic": {"code": value}})
                self.assertIsInstance(result, RedactionFailure)

    def test_depth_cap_fails_closed(self):
        nested = {"detail": "ok"}
        for _ in range(redaction.MAX_REDACTION_DEPTH + 1):
            nested = {"context": nested}

        result = redact_document({"diagnostic": nested})

        self.assertIsInstance(result, RedactionFailure)

    def test_cycle_safe_failure_value_instead_of_traceback_or_hang(self):
        cyclic = {"fault": "REQUEST_TIMEOUT"}
        cyclic["diagnostic"] = cyclic

        result = redact_document(cyclic)

        self.assertIsInstance(result, RedactionFailure)
        self.assertEqual(result.fault, REDACTION_FAILURE)
        self.assertEqual(result.reason, "cyclic_input")

    def test_redaction_failure_reason_is_closed_vocabulary(self):
        class ForeignRedactionError(dict):
            def items(self):
                raise redaction.RedactionError("Authorization: Bearer secret-token")

        class ForeignException(dict):
            def items(self):
                raise RuntimeError("Authorization: Bearer secret-token")

        too_deep = {"detail": "x"}
        for _ in range(redaction.MAX_REDACTION_DEPTH + 1):
            too_deep = {"context": too_deep}

        cases = (
            (["not", "object"], "non_mapping_root"),
            (ForeignRedactionError(), redaction.REDACTION_REASON_GENERIC),
            (ForeignException(), "unexpected_redaction_exception"),
            ({"fault": "\ud800"}, "surrogate_bearing_string"),
            ({"fault": "REQUEST\x00TIMEOUT"}, "nul_bearing_string"),
            ({"diagnostic": {"context": []}}, "mapping_field_not_object"),
            ({"diagnostic": too_deep}, "redaction_depth_exceeded"),
            ({"stderr": {"prefix": b"abc", "total_bytes": 2, "truncated": False}}, "stderr_prefix_too_long_for_total"),
            ({"stderr": {"prefix": b"abc", "total_bytes": 3, "truncated": True}}, "stderr_truncation_without_discard"),
            ({"stderr": {"prefix": b"abc", "total_bytes": 3, "truncated": False, "tail": "x"}}, "stderr_unsupported_field"),
            ({"stderr": {"prefix": object(), "total_bytes": 3, "truncated": False}}, "prefix_not_bytes_or_string"),
        )
        for document, expected_reason in cases:
            with self.subTest(document=document):
                result = redact_document(document)
                self.assertIsInstance(result, RedactionFailure)
                self.assertEqual(result.reason, expected_reason)
                self.assertIn(result.reason, redaction._REDACTION_REASON_CODES)

    def test_all_redaction_failure_literal_reasons_are_closed_vocabulary(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        literals: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RedactionFailure"
            ):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                literals.append((node.lineno, node.args[0].value))

        self.assertTrue(literals)
        outside = [
            (lineno, reason)
            for lineno, reason in literals
            if reason not in redaction._REDACTION_REASON_CODES
        ]
        self.assertEqual(outside, [])

    def test_nul_bearing_string_does_not_pass_intact(self):
        scalar = redact_document({"fault": "REQUEST\x00TIMEOUT"})
        self.assertIsInstance(scalar, RedactionFailure)

        stderr = redact_document({"stderr": {"prefix": b"abc\x00def", "total_bytes": 7, "truncated": False}})
        self.assertIsInstance(stderr, RedactedDocument)
        self.assertNotIn("prefix", stderr.as_dict()["stderr"])

    def test_unpaired_surrogate_fails_closed_before_persistence(self):
        result = redact_document({"fault": "\ud800"})
        self.assertIsInstance(result, RedactionFailure)

        stderr = redact_document({"stderr": {"prefix": "\ud800", "total_bytes": 1, "truncated": False}})
        self.assertIsInstance(stderr, RedactionFailure)

    def test_invalid_bytes_are_handled_explicitly(self):
        result = redact_document({"stderr": {"prefix": b"\xff", "total_bytes": 3, "truncated": True}})

        self.assertIsInstance(result, RedactedDocument)
        self.assertNotIn("prefix", result.as_dict()["stderr"])

    def test_no_bin_consumer_imports_redaction_module(self):
        for path in (ROOT / "bin").rglob("*"):
            if not path.is_file() or path.name == "llm-collab":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            self.assertFalse(_imports_module(tree, "llm_collab.runtime_adapter_redaction"), path)

    def test_redaction_module_has_no_persistence_or_runtime_imports(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        forbidden = {
            "llm_collab.canonical",
            "llm_collab.compatibility",
            "llm_collab.daemon",
            "llm_collab.inbox",
            "llm_collab.ledger",
            "llm_collab.project_issue_queue",
            "llm_collab.runtime_adapter_lifecycle",
            "llm_collab.runtime_adapter_manifest",
            "llm_collab.runtime_adapter_requests",
            "llm_collab.task_contract",
        }
        imports = _imported_modules(tree)
        for module in forbidden:
            self.assertNotIn(module, imports)
        forbidden_stdlib = {"os", "pathlib", "random", "secrets", "socket", "subprocess", "threading", "time"}
        self.assertTrue(forbidden_stdlib.isdisjoint(imports))

    def test_direction_is_one_way_from_redaction_to_supervisor(self):
        self.assertTrue(_imports_module(ast.parse(MODULE_PATH.read_text()), "llm_collab.runtime_adapter_supervisor"))
        for relative in (
            "llm_collab/runtime_adapter_supervisor.py",
            "llm_collab/runtime_adapter_requests.py",
            "llm_collab/runtime_adapter_lifecycle.py",
        ):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
            self.assertFalse(_imports_module(tree, "llm_collab.runtime_adapter_redaction"), relative)


def _imported_modules(tree):
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imports_module(tree, module_name):
    return any(
        module == module_name or module.startswith(module_name + ".")
        for module in _imported_modules(tree)
    )


def _protocol_fault_literals():
    protocol = PROTOCOL_PATH.read_text(encoding="utf-8")
    return frozenset(
        match.group(1)
        for match in re.finditer(r"^\s*\|\s*-\d+\s*\|\s*`([A-Z_]+)`\s*\|", protocol, re.M)
    )


if __name__ == "__main__":
    unittest.main()
