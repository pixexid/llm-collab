"""Tests for the pure P6 capability authority binding layer."""

from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_capability import (
    CAPABILITY_NOT_DECLARED,
    CapabilityAuthorityError,
    TrustedCapabilityAuthorityRegistry,
    method_requires_product_capability,
)
from llm_collab.runtime_adapter_manifest import TrustedManifestRegistry
from llm_collab.runtime_adapter_requests import (
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_capability.py"


class RuntimeAdapterCapabilityAuthorityTests(unittest.TestCase):
    def test_bind_initialized_accepts_exact_trusted_capability_set(self) -> None:
        registry = TrustedCapabilityAuthorityRegistry(_authority_records())
        resolved = _resolved_adapter()
        initialized = _initialized_result()

        bound = registry.bind_initialized(resolved=resolved, initialized=initialized)

        self.assertEqual(bound.adapter_id, "adapter_alpha")
        self.assertEqual(bound.adapter_revision, "adapter_rev1")
        self.assertEqual(bound.manifest_id, "manifest_alpha")
        self.assertEqual(bound.manifest_revision, "manifest_rev1")
        self.assertEqual(bound.endpoint["capability_set_id"], "caps_alpha")
        self.assertEqual(bound.capability_set["revision"], "cap_rev1")
        self.assertEqual(
            registry.require_capability_entry(bound, "runtime.deliver.observe_only")["capability"],
            "runtime.deliver.observe_only",
        )

    def test_adapter_declared_divergence_from_trusted_registry_fails_closed(self) -> None:
        registry = TrustedCapabilityAuthorityRegistry(_authority_records())
        resolved = _resolved_adapter()
        cases = (
            ("wrong set id", ("capability_set", "capability_set_id"), "caps_other"),
            ("wrong revision", ("capability_set", "revision"), "cap_rev_other"),
            ("wrong scope", ("capability_set", "scope"), {"kind": "project", "project_id": "amiga"}),
            ("wrong capability entry", ("capability_set", "capabilities", 0, "capability"), "runtime.other"),
        )
        for name, path, value in cases:
            initialized = _initialized_result()
            _set_nested(initialized, path, value)
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    registry.bind_initialized(resolved=resolved, initialized=initialized)
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_bind_uses_single_resolved_endpoint_source_not_caller_endpoint_members(self) -> None:
        registry = TrustedCapabilityAuthorityRegistry(_authority_records())
        resolved = _resolved_adapter()
        initialized = _initialized_result()
        initialized["endpoint"]["endpoint_id"] = "caller_endpoint"

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.bind_initialized(resolved=resolved, initialized=initialized)

        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_bind_requires_both_resolved_manifest_and_trusted_registry_endpoint_sources(self) -> None:
        trusted_records = _authority_records()

        initialized_from_trusted = _initialized_result()
        resolved_other = _resolved_adapter(endpoint_id="endpoint_other")
        with self.subTest("trusted initialization cannot bypass resolved manifest"):
            with self.assertRaises(CapabilityAuthorityError) as caught:
                TrustedCapabilityAuthorityRegistry(trusted_records).bind_initialized(
                    resolved=resolved_other,
                    initialized=initialized_from_trusted,
                )
            self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

        trusted_other = _authority_records(endpoint_id="endpoint_other")
        initialized_from_resolved = _initialized_result()
        resolved = _resolved_adapter()
        with self.subTest("resolved initialization cannot bypass trusted registry"):
            with self.assertRaises(CapabilityAuthorityError) as caught:
                TrustedCapabilityAuthorityRegistry(trusted_other).bind_initialized(
                    resolved=resolved,
                    initialized=initialized_from_resolved,
                )
            self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_constructor_rejects_unknown_fields_and_deepcopies_trusted_input(self) -> None:
        records = _authority_records()
        with_unknown = copy.deepcopy(records)
        with_unknown["adapter_alpha"]["shell"] = True

        with self.assertRaises(CapabilityAuthorityError):
            TrustedCapabilityAuthorityRegistry(with_unknown)

        registry = TrustedCapabilityAuthorityRegistry(records)
        records["adapter_alpha"]["capability_set"]["capability_set_id"] = "caps_mutated"
        records["adapter_alpha"]["capability_set"]["capabilities"][0]["capability"] = "runtime.mutated"

        bound = registry.bind_initialized(resolved=_resolved_adapter(), initialized=_initialized_result())
        self.assertEqual(bound.capability_set["capability_set_id"], "caps_alpha")
        self.assertEqual(bound.capability_set["capabilities"][0]["capability"], "runtime.deliver.observe_only")
        with self.assertRaises(TypeError):
            bound.capability_set["capability_set_id"] = "caps_mutated"  # type: ignore[index]

    def test_capability_set_revision_is_independent_from_adapter_and_manifest_revisions(self) -> None:
        records = _authority_records(capability_set_revision="cap_profile_rev_different")
        registry = TrustedCapabilityAuthorityRegistry(records)
        resolved = _resolved_adapter()
        initialized = _initialized_result(capability_set_revision="cap_profile_rev_different")

        bound = registry.bind_initialized(resolved=resolved, initialized=initialized)

        self.assertEqual(bound.adapter_revision, "adapter_rev1")
        self.assertEqual(bound.manifest_revision, "manifest_rev1")
        self.assertEqual(bound.capability_set["revision"], "cap_profile_rev_different")
        self.assertNotEqual(bound.capability_set["revision"], bound.adapter_revision)
        self.assertNotEqual(bound.capability_set["revision"], bound.manifest_revision)

    def test_attestation_and_selected_unsupported_entry_fail_closed(self) -> None:
        cases = (
            ("source id", ("capability_set", "capabilities", 0, "evidence", "source_id"), "adapter_other"),
            ("source revision", ("capability_set", "capabilities", 0, "evidence", "source_revision"), "rev_other"),
            ("malformed attestation", ("capability_set", "capabilities", 0, "evidence", "source_id"), None),
        )
        for name, path, value in cases:
            records = _authority_records()
            _set_nested(records["adapter_alpha"], path, value)
            registry = TrustedCapabilityAuthorityRegistry(records)
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError):
                    registry.bind_initialized(resolved=_resolved_adapter(), initialized=_initialized_result_from_record(records))

        records = _authority_records()
        records["adapter_alpha"]["capability_set"]["capabilities"].append(
            {"capability": "runtime.disabled", "quality": "unsupported"}
        )
        initialized = _initialized_result_from_record(records)
        bound = TrustedCapabilityAuthorityRegistry(records).bind_initialized(
            resolved=_resolved_adapter(),
            initialized=initialized,
        )
        with self.assertRaises(CapabilityAuthorityError):
            TrustedCapabilityAuthorityRegistry(records).require_capability_entry(bound, "runtime.disabled")

    def test_duplicate_selected_capability_token_fails_closed(self) -> None:
        records = _authority_records()
        duplicate = copy.deepcopy(records["adapter_alpha"]["capability_set"]["capabilities"][0])
        records["adapter_alpha"]["capability_set"]["capabilities"].append(duplicate)
        bound = TrustedCapabilityAuthorityRegistry(records).bind_initialized(
            resolved=_resolved_adapter(),
            initialized=_initialized_result_from_record(records),
        )

        with self.assertRaises(CapabilityAuthorityError) as caught:
            TrustedCapabilityAuthorityRegistry(records).require_capability_entry(bound, "runtime.deliver.observe_only")

        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_health_and_shutdown_are_protocol_controls_not_product_capabilities(self) -> None:
        records = _authority_records()
        capabilities = records["adapter_alpha"]["capability_set"]["capabilities"]
        self.assertFalse(any(entry["capability"] in {METHOD_HEALTH, METHOD_SHUTDOWN} for entry in capabilities))

        bound = TrustedCapabilityAuthorityRegistry(records).bind_initialized(
            resolved=_resolved_adapter(),
            initialized=_initialized_result(),
        )

        self.assertEqual(bound.capability_set["capability_set_id"], "caps_alpha")
        self.assertFalse(method_requires_product_capability(METHOD_HEALTH))
        self.assertFalse(method_requires_product_capability(METHOD_SHUTDOWN))
        self.assertTrue(method_requires_product_capability(METHOD_DELIVER))
        self.assertTrue(method_requires_product_capability(METHOD_CANCEL))
        self.assertTrue(method_requires_product_capability(METHOD_RECONCILE))

    def test_project_scope_requires_exact_non_null_project_id(self) -> None:
        records = _authority_records(scope={"kind": "project", "project_id": "amiga"})
        bound = TrustedCapabilityAuthorityRegistry(records).bind_initialized(
            resolved=_resolved_adapter(scope={"kind": "project", "project_id": "amiga"}),
            initialized=_initialized_result(scope={"kind": "project", "project_id": "amiga"}),
        )
        self.assertEqual(bound.endpoint["scope"], {"kind": "project", "project_id": "amiga"})

        bad_records = _authority_records(scope={"kind": "project", "project_id": ""})
        with self.assertRaises(CapabilityAuthorityError):
            TrustedCapabilityAuthorityRegistry(bad_records)

    def test_capability_module_uses_no_live_runtime_or_evidence_surfaces(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        forbidden_modules = {
            "datetime",
            "os",
            "sqlite3",
            "subprocess",
            "threading",
            "time",
            "llm_collab.runtime_adapter_claim",
            "llm_collab.runtime_adapter_lifecycle_evidence",
            "llm_collab.runtime_adapter_supervisor",
            "llm_collab.runtime_adapter_state",
            "llm_collab.canonical",
            "llm_collab.daemon",
            "llm_collab.inbox",
            "llm_collab.project_issue_queue",
            "llm_collab.registry",
        }
        forbidden_calls = {"open", "Popen", "run", "sleep", "Thread"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_modules)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, forbidden_modules)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, forbidden_calls)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_calls)


def _authority_records(
    *,
    capability_set_revision: str = "cap_rev1",
    scope: dict[str, object] | None = None,
    endpoint_id: str = "endpoint_alpha",
) -> dict[str, dict[str, object]]:
    active_scope = scope or {"kind": "workspace"}
    return {
        "adapter_alpha": {
            "adapter_id": "adapter_alpha",
            "endpoint": _endpoint(scope=active_scope, endpoint_id=endpoint_id),
            "capability_set": _capability_set(revision=capability_set_revision, scope=active_scope),
        }
    }


def _manifest(
    scope: dict[str, object] | None = None,
    *,
    endpoint_id: str = "endpoint_alpha",
) -> dict[str, dict[str, object]]:
    return {
        "adapter_alpha": {
            "adapter_id": "adapter_alpha",
            "adapter_revision": "adapter_rev1",
            "manifest_id": "manifest_alpha",
            "manifest_revision": "manifest_rev1",
            "endpoint": _endpoint(scope=scope or {"kind": "workspace"}, endpoint_id=endpoint_id),
            "executable": "/trusted/bin/adapter-alpha",
            "argv": ["adapter-alpha", "--stdio"],
            "working_directory": "/trusted/work",
            "environment": {"SAFE": "1"},
            "environment_allowlist": ["SAFE"],
        }
    }


def _resolved_adapter(
    scope: dict[str, object] | None = None,
    *,
    endpoint_id: str = "endpoint_alpha",
):
    return TrustedManifestRegistry(_manifest(scope, endpoint_id=endpoint_id)).resolve("adapter_alpha")


def _initialized_result(
    *,
    capability_set_revision: str = "cap_rev1",
    scope: dict[str, object] | None = None,
) -> dict[str, object]:
    active_scope = scope or {"kind": "workspace"}
    return {
        "negotiated_protocol_version": 1,
        "adapter_id": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "manifest_id": "manifest_alpha",
        "manifest_revision": "manifest_rev1",
        "endpoint": _endpoint(scope=active_scope),
        "capability_set": _capability_set(revision=capability_set_revision, scope=active_scope),
    }


def _initialized_result_from_record(records: dict[str, dict[str, object]]) -> dict[str, object]:
    record = records["adapter_alpha"]
    return {
        "negotiated_protocol_version": 1,
        "adapter_id": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "manifest_id": "manifest_alpha",
        "manifest_revision": "manifest_rev1",
        "endpoint": copy.deepcopy(record["endpoint"]),
        "capability_set": copy.deepcopy(record["capability_set"]),
    }


def _endpoint(scope: dict[str, object], *, endpoint_id: str = "endpoint_alpha") -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": copy.deepcopy(scope),
        "endpoint_id": endpoint_id,
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


def _capability_set(*, revision: str, scope: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": copy.deepcopy(scope),
        "capability_set_id": "caps_alpha",
        "revision": revision,
        "capabilities": [
            _capability("runtime.deliver.observe_only"),
            _capability("runtime.reconcile.observe_only"),
        ],
    }


def _capability(token: str) -> dict[str, object]:
    return {
        "capability": token,
        "quality": "authoritative",
        "constraints": {"access_mode": "observe_only"},
        "evidence": {
            "evidence_kind": "profile_attestation",
            "source_id": "adapter_alpha",
            "source_revision": "adapter_rev1",
            "integrity": "sha256:" + ("1" * 64),
        },
    }


def _set_nested(document, path, value) -> None:
    current = document
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


if __name__ == "__main__":
    unittest.main()
