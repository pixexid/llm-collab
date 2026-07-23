"""Tests for the pure P6 capability authority binding layer."""

from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_capability import (
    BoundCapabilityContext,
    CAPABILITY_NOT_DECLARED,
    CapabilityAuthorityError,
    CapabilityDecision,
    EvidenceProfileDecision,
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

    def test_validate_request_authority_derives_exact_action_decision(self) -> None:
        registry, bound = _bound_authority()

        decision = registry.validate_request_authority(bound, METHOD_DELIVER)

        self.assertEqual(
            decision,
            CapabilityDecision(
                method=METHOD_DELIVER,
                selected_capability="runtime.deliver.observe_only",
                selected_quality="authoritative",
            ),
        )
        with self.assertRaises(Exception):
            decision.selected_capability = "runtime.other"  # type: ignore[misc]

    def test_request_authority_rejects_relation_schema_drift(self) -> None:
        cases = (
            ("unknown field", lambda relation: relation.__setitem__("caller_relation", True)),
            ("missing field", lambda relation: relation.pop("selected_capability")),
            ("bad capability-set id", lambda relation: relation.__setitem__("capability_set_id", "")),
            ("bad method", lambda relation: relation.__setitem__("method", METHOD_HEALTH)),
            ("bad relation type", lambda _relation: []),
        )
        for name, mutate in cases:
            records = _authority_records()
            if name == "bad relation type":
                records["adapter_alpha"]["session_action_relations"][0] = []
            else:
                mutate(records["adapter_alpha"]["session_action_relations"][0])
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    TrustedCapabilityAuthorityRegistry(records)
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_request_authority_relation_cardinality_and_key_fail_closed(self) -> None:
        cases = (
            ("zero", []),
            ("duplicate", [_action_relation(METHOD_DELIVER), _action_relation(METHOD_DELIVER)]),
            ("stale set", [_action_relation(METHOD_DELIVER, capability_set_id="caps_other")]),
            ("stale revision", [_action_relation(METHOD_DELIVER, capability_set_revision="cap_rev_other")]),
            ("wrong method", [_action_relation(METHOD_RECONCILE)]),
        )
        for name, relations in cases:
            records = _authority_records(session_action_relations=relations)
            registry, bound = _bound_authority(records)
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    registry.validate_request_authority(bound, METHOD_DELIVER)
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_request_authority_rejects_controls_and_caller_supplied_authority(self) -> None:
        registry, bound = _bound_authority()

        for method in (METHOD_HEALTH, METHOD_SHUTDOWN, "runtime.unknown"):
            with self.subTest(method=method):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    registry.validate_request_authority(bound, method)
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_request_authority(
                bound,
                METHOD_DELIVER,
                caller_authority_fields={"selected_capability": "runtime.deliver.observe_only"},
            )
        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_request_authority_selected_capability_guards_are_independent(self) -> None:
        cases = (
            (
                "absent selected token",
                lambda records: records["adapter_alpha"]["session_action_relations"][0].__setitem__(
                    "selected_capability", "runtime.absent"
                ),
            ),
            (
                "duplicate selected token",
                lambda records: records["adapter_alpha"]["capability_set"]["capabilities"].append(
                    copy.deepcopy(records["adapter_alpha"]["capability_set"]["capabilities"][0])
                ),
            ),
            (
                "quality mismatch",
                lambda records: records["adapter_alpha"]["session_action_relations"][0].__setitem__(
                    "required_quality", "best_effort"
                ),
            ),
        )
        for name, mutate in cases:
            records = _authority_records()
            mutate(records)
            registry, bound = _bound_authority(records)
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    registry.validate_request_authority(bound, METHOD_DELIVER)
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_request_authority_rejects_unsupported_selected_capability_quality(self) -> None:
        records = _authority_records()
        records["adapter_alpha"]["session_action_relations"][0]["selected_capability"] = "runtime.disabled"
        records["adapter_alpha"]["session_action_relations"][0]["required_quality"] = "unsupported"
        registry, bound = _bound_authority(records)
        forged_capability_set = _thaw(bound.capability_set)
        unsupported = _capability("runtime.disabled")
        unsupported["quality"] = "unsupported"
        forged_capability_set["capabilities"].append(unsupported)
        forged_bound = BoundCapabilityContext(
            adapter_id=bound.adapter_id,
            adapter_revision=bound.adapter_revision,
            manifest_id=bound.manifest_id,
            manifest_revision=bound.manifest_revision,
            endpoint=bound.endpoint,
            capability_set=forged_capability_set,
        )

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_request_authority(forged_bound, METHOD_DELIVER)

        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_request_authority_revalidates_selected_attestation_on_bound_context(self) -> None:
        registry, bound = _bound_authority()
        forged_capability_set = _thaw(bound.capability_set)
        forged_entries = [copy.deepcopy(entry) for entry in forged_capability_set["capabilities"]]
        forged_entries[0]["evidence"]["source_id"] = "adapter_other"
        forged_capability_set["capabilities"] = forged_entries
        forged_bound = BoundCapabilityContext(
            adapter_id=bound.adapter_id,
            adapter_revision=bound.adapter_revision,
            manifest_id=bound.manifest_id,
            manifest_revision=bound.manifest_revision,
            endpoint=bound.endpoint,
            capability_set=forged_capability_set,
        )

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_request_authority(forged_bound, METHOD_DELIVER)

        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_request_authority_has_no_method_or_prefix_fallback(self) -> None:
        records = _authority_records(session_action_relations=[])
        records["adapter_alpha"]["capability_set"]["capabilities"].append(_capability(METHOD_DELIVER))
        registry, bound = _bound_authority(records)

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_request_authority(bound, METHOD_DELIVER)

        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_evidence_profile_authority_derives_exact_profile_decision(self) -> None:
        registry, bound = _bound_authority()

        decision = registry.validate_evidence_profile_authority(bound, _state_evidence())

        self.assertEqual(
            decision,
            EvidenceProfileDecision(
                capability_profile_id="runtime_profile",
                capability_profile_revision="cap_rev1",
                evidence_kind="exact_session_binding",
                evidence_quality="authoritative",
            ),
        )
        with self.assertRaises(Exception):
            decision.capability_profile_id = "runtime.other"  # type: ignore[misc]

    def test_evidence_profile_schema_and_registration_cardinality_fail_closed(self) -> None:
        cases = (
            ("unknown field", [_evidence_profile(extra={"caller_profile": True})]),
            ("missing field", [{"capability_profile_id": "runtime_profile"}]),
            ("bad id", [_evidence_profile(capability_profile_id="")]),
            ("bad revision", [_evidence_profile(capability_profile_revision="")]),
            ("bad row type", [[]]),
            ("zero", []),
            ("duplicate", [_evidence_profile(), _evidence_profile()]),
            ("stale revision", [_evidence_profile(capability_profile_revision="cap_rev_other")]),
        )
        for name, profiles in cases:
            records = _authority_records(evidence_profiles=profiles)
            with self.subTest(name=name):
                if name in {"unknown field", "missing field", "bad id", "bad revision", "bad row type"}:
                    with self.assertRaises(CapabilityAuthorityError) as caught:
                        TrustedCapabilityAuthorityRegistry(records)
                else:
                    registry, bound = _bound_authority(records)
                    with self.assertRaises(CapabilityAuthorityError) as caught:
                        registry.validate_evidence_profile_authority(bound, _state_evidence())
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_evidence_profile_capability_token_cardinality_fail_closed(self) -> None:
        cases = (
            (
                "absent profile token",
                lambda bound: BoundCapabilityContext(
                    adapter_id=bound.adapter_id,
                    adapter_revision=bound.adapter_revision,
                    manifest_id=bound.manifest_id,
                    manifest_revision=bound.manifest_revision,
                    endpoint=bound.endpoint,
                    capability_set={
                        **_thaw(bound.capability_set),
                        "capabilities": [
                            entry
                            for entry in _thaw(bound.capability_set)["capabilities"]
                            if entry["capability"] != "runtime_profile"
                        ],
                    },
                ),
            ),
            (
                "duplicate profile token",
                lambda bound: BoundCapabilityContext(
                    adapter_id=bound.adapter_id,
                    adapter_revision=bound.adapter_revision,
                    manifest_id=bound.manifest_id,
                    manifest_revision=bound.manifest_revision,
                    endpoint=bound.endpoint,
                    capability_set={
                        **_thaw(bound.capability_set),
                        "capabilities": _thaw(bound.capability_set)["capabilities"]
                        + [copy.deepcopy(_capability("runtime_profile"))],
                    },
                ),
            ),
        )
        registry, bound = _bound_authority()
        for name, forge in cases:
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    registry.validate_evidence_profile_authority(forge(bound), _state_evidence())
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_evidence_profile_rejects_unsupported_or_bad_attestation_independently(self) -> None:
        cases = (
            (
                "unsupported",
                lambda entry: entry.update({"quality": "unsupported"}),
            ),
            ("source id", lambda entry: entry["evidence"].__setitem__("source_id", "adapter_other")),
            ("source revision", lambda entry: entry["evidence"].__setitem__("source_revision", "rev_other")),
        )
        registry, bound = _bound_authority()
        for name, mutate in cases:
            forged_set = _thaw(bound.capability_set)
            for entry in forged_set["capabilities"]:
                if entry["capability"] == "runtime_profile":
                    mutate(entry)
            forged_bound = BoundCapabilityContext(
                adapter_id=bound.adapter_id,
                adapter_revision=bound.adapter_revision,
                manifest_id=bound.manifest_id,
                manifest_revision=bound.manifest_revision,
                endpoint=bound.endpoint,
                capability_set=forged_set,
            )
            with self.subTest(name=name):
                with self.assertRaises(CapabilityAuthorityError) as caught:
                    registry.validate_evidence_profile_authority(forged_bound, _state_evidence())
                self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_evidence_profile_quality_ceiling_is_directional(self) -> None:
        best_effort_records = _authority_records(profile_quality="best_effort")
        best_effort_registry, best_effort_bound = _bound_authority(best_effort_records)
        authoritative_registry, authoritative_bound = _bound_authority()

        with self.assertRaises(CapabilityAuthorityError) as caught:
            best_effort_registry.validate_evidence_profile_authority(best_effort_bound, _state_evidence(quality="authoritative"))
        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

        decision = best_effort_registry.validate_evidence_profile_authority(
            best_effort_bound,
            _state_evidence(quality="best_effort"),
        )
        self.assertEqual(decision.evidence_quality, "best_effort")

        decision = authoritative_registry.validate_evidence_profile_authority(
            authoritative_bound,
            _state_evidence(quality="best_effort"),
        )
        self.assertEqual(decision.evidence_quality, "best_effort")

    def test_evidence_profile_independent_from_action_relation_no_inference(self) -> None:
        registry, bound = _bound_authority()

        action_decision = registry.validate_request_authority(bound, METHOD_DELIVER)
        profile_decision = registry.validate_evidence_profile_authority(bound, _state_evidence())

        self.assertEqual(action_decision.selected_capability, "runtime.deliver.observe_only")
        self.assertEqual(profile_decision.capability_profile_id, "runtime_profile")
        self.assertNotEqual(action_decision.selected_capability, profile_decision.capability_profile_id)

        records = _authority_records(evidence_profiles=[])
        registry, bound = _bound_authority(records)
        action_decision = registry.validate_request_authority(bound, METHOD_DELIVER)
        self.assertEqual(action_decision.selected_capability, "runtime.deliver.observe_only")
        action_named_evidence = _state_evidence(capability_profile_id=action_decision.selected_capability)
        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_evidence_profile_authority(bound, action_named_evidence)
        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_evidence_profile_validates_each_carried_evidence_call_separately(self) -> None:
        registry, bound = _bound_authority()

        session_decision = registry.validate_evidence_profile_authority(
            bound,
            _state_evidence(evidence_kind="exact_session_binding", evidence_id="evidence_session_alpha"),
        )
        delivery_decision = registry.validate_evidence_profile_authority(
            bound,
            _state_evidence(evidence_kind="native_delivery_state", evidence_id="evidence_delivery_alpha"),
        )
        receipt_decision = registry.validate_evidence_profile_authority(
            bound,
            _state_evidence(evidence_kind="exact_session_acknowledgment", evidence_id="evidence_receipt_alpha"),
        )
        self.assertEqual(
            [session_decision.evidence_kind, delivery_decision.evidence_kind, receipt_decision.evidence_kind],
            ["exact_session_binding", "native_delivery_state", "exact_session_acknowledgment"],
        )

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_evidence_profile_authority(
                bound,
                _state_evidence(capability_profile_id="receipt_profile", evidence_kind="exact_session_acknowledgment"),
            )
        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

    def test_evidence_profile_boundary_keeps_p5_and_p7_out_of_child_d(self) -> None:
        registry, bound = _bound_authority()

        stale_registry, stale_bound = _bound_authority(
            _authority_records(evidence_profiles=[_evidence_profile(capability_profile_revision="cap_rev_other")])
        )
        stale_profile = _state_evidence(capability_profile_revision="cap_rev_other")
        with self.assertRaises(CapabilityAuthorityError) as caught:
            stale_registry.validate_evidence_profile_authority(stale_bound, stale_profile)
        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

        tampered_integrity = _state_evidence()
        tampered_integrity["integrity"] = "sha256:" + ("0" * 64)
        decision = registry.validate_evidence_profile_authority(bound, tampered_integrity)
        self.assertEqual(decision.capability_profile_id, "runtime_profile")

    def test_evidence_profile_rejects_caller_supplied_authority_fields_before_selection(self) -> None:
        registry, bound = _bound_authority()

        with self.assertRaises(CapabilityAuthorityError) as caught:
            registry.validate_evidence_profile_authority(
                bound,
                _state_evidence(),
                caller_authority_fields={"capability_profile_id": "runtime_profile"},
            )

        self.assertEqual(caught.exception.code, CAPABILITY_NOT_DECLARED)

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
    session_action_relations: list[dict[str, object]] | None = None,
    evidence_profiles: list[dict[str, object]] | None = None,
    profile_quality: str = "authoritative",
) -> dict[str, dict[str, object]]:
    active_scope = scope or {"kind": "workspace"}
    return {
        "adapter_alpha": {
            "adapter_id": "adapter_alpha",
            "endpoint": _endpoint(scope=active_scope, endpoint_id=endpoint_id),
            "capability_set": _capability_set(
                revision=capability_set_revision,
                scope=active_scope,
                profile_quality=profile_quality,
            ),
            "session_action_relations": copy.deepcopy(
                session_action_relations
                if session_action_relations is not None
                else [
                    _action_relation(METHOD_DELIVER),
                    _action_relation(METHOD_CANCEL, selected_capability="runtime.cancel.observe_only"),
                    _action_relation(METHOD_RECONCILE, selected_capability="runtime.reconcile.observe_only"),
                ]
            ),
            "evidence_profiles": copy.deepcopy(
                evidence_profiles if evidence_profiles is not None else [_evidence_profile()]
            ),
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


def _capability_set(*, revision: str, scope: dict[str, object], profile_quality: str = "authoritative") -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": copy.deepcopy(scope),
        "capability_set_id": "caps_alpha",
        "revision": revision,
        "capabilities": [
            _capability("runtime.deliver.observe_only"),
            _capability("runtime.cancel.observe_only"),
            _capability("runtime.reconcile.observe_only"),
            _capability("runtime_profile", quality=profile_quality),
        ],
    }


def _capability(token: str, *, quality: str = "authoritative") -> dict[str, object]:
    return {
        "capability": token,
        "quality": quality,
        "constraints": {"access_mode": "observe_only"},
        "evidence": {
            "evidence_kind": "profile_attestation",
            "source_id": "adapter_alpha",
            "source_revision": "adapter_rev1",
            "integrity": "sha256:" + ("1" * 64),
        },
    }


def _evidence_profile(
    *,
    capability_profile_id: str = "runtime_profile",
    capability_profile_revision: str = "cap_rev1",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    profile = {
        "capability_profile_id": capability_profile_id,
        "capability_profile_revision": capability_profile_revision,
    }
    if extra:
        profile.update(extra)
    return profile


def _state_evidence(
    *,
    capability_profile_id: str = "runtime_profile",
    capability_profile_revision: str = "cap_rev1",
    quality: str = "authoritative",
    evidence_kind: str = "exact_session_binding",
    evidence_id: str = "evidence_session_alpha",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": {"kind": "workspace"},
        "evidence_id": evidence_id,
        "evidence_kind": evidence_kind,
        "quality": quality,
        "state": "visible",
        "authority": {
            "authority_kind": "trusted_adapter",
            "identity": "adapter_alpha",
            "implementation_revision": "adapter_rev1",
            "capability_profile_id": capability_profile_id,
            "capability_profile_revision": capability_profile_revision,
        },
        "subject": {
            "endpoint_id": "endpoint_alpha",
            "session_ref_id": "session_alpha",
            "native_session_id": "native-session-alpha",
        },
        "correlation_id": "corr_session_alpha",
        "observed_at_utc": "2026-07-23T00:00:00Z",
        "integrity": "sha256:" + ("2" * 64),
    }


def _action_relation(
    method: str,
    *,
    selected_capability: str = "runtime.deliver.observe_only",
    capability_set_id: str = "caps_alpha",
    capability_set_revision: str = "cap_rev1",
    required_quality: str = "authoritative",
) -> dict[str, object]:
    return {
        "capability_set_id": capability_set_id,
        "capability_set_revision": capability_set_revision,
        "method": method,
        "selected_capability": selected_capability,
        "required_quality": required_quality,
    }


def _bound_authority(records: dict[str, dict[str, object]] | None = None):
    active_records = records or _authority_records()
    registry = TrustedCapabilityAuthorityRegistry(active_records)
    bound = registry.bind_initialized(
        resolved=_resolved_adapter(),
        initialized=_initialized_result_from_record(active_records),
    )
    return registry, bound


def _thaw(value):
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _set_nested(document, path, value) -> None:
    current = document
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


if __name__ == "__main__":
    unittest.main()
