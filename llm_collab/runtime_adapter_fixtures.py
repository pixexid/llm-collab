"""Spec-derived Runtime Adapter JSON-RPC V1 replay fixtures.

This module is deliberately inert. It defines replay data and validation for
that data; it does not start adapters, touch persistent state, or publish any
conformance claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from llm_collab.runtime_adapter_conformance import (
    ConformanceFailure,
    classify_direction,
    extract_clause_occurrences,
    protocol_error_codes,
    validate_endpoint_v1,
    validate_request,
    validate_response,
    validate_session_ref_v1,
)


POLARITY_CONFORMING = "conforming"
POLARITY_VIOLATING = "violating"
POLARITIES = frozenset((POLARITY_CONFORMING, POLARITY_VIOLATING))
NO_STATE_CHANGE = "no_state_change"
_SCOPE = MappingProxyType({"kind": "workspace"})
_AUTHORITY = MappingProxyType(
    {
        "authority_kind": "trusted_adapter",
        "identity": "adapter_alpha",
        "implementation_revision": "adapter_rev1",
        "capability_profile_id": "runtime_profile",
        "capability_profile_revision": "cap_rev1",
    }
)
_SESSION_EVIDENCE = MappingProxyType(
    {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": _SCOPE,
        "evidence_id": "evidence_session_binding",
        "evidence_kind": "exact_session_binding",
        "quality": "authoritative",
        "state": "visible",
        "authority": _AUTHORITY,
        "subject": MappingProxyType(
            {
                "endpoint_id": "endpoint_alpha",
                "session_ref_id": "session_alpha",
                "native_session_id": "native-session-alpha",
            }
        ),
        "correlation_id": "corr_session",
        "observed_at_utc": "2026-07-22T00:00:00Z",
        "integrity": "sha256:5e9511607616c2bd4683720897634cb4096eb48cbf5a0eaa0f3ef14ecdbdcb8f",
    }
)
_SESSION_REF = MappingProxyType(
    {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": _SCOPE,
        "session_ref_id": "session_alpha",
        "endpoint_id": "endpoint_alpha",
        "native_session_id": "native-session-alpha",
        "evidence": _SESSION_EVIDENCE,
    }
)
_RECEIPT_EVIDENCE = MappingProxyType(
    {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": _SCOPE,
        "evidence_id": "evidence_attempt_alpha",
        "evidence_kind": "native_delivery_state",
        "quality": "authoritative",
        "state": "completed",
        "authority": _AUTHORITY,
        "subject": MappingProxyType(
            {
                "message_id": "msg_alpha",
                "delivery_id": "delivery_alpha",
                "attempt_id": "attempt_alpha",
                "endpoint_id": "endpoint_alpha",
                "session_ref_id": "session_alpha",
            }
        ),
        "correlation_id": "corr_attempt_alpha",
        "observed_at_utc": "2026-07-22T00:00:00Z",
        "integrity": "sha256:33180ea9457e5e05bf9f13d2ae76275cc8abe10da89ba955ed942305ad0a3f90",
    }
)
_ENDPOINT = MappingProxyType(
    {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": _SCOPE,
        "endpoint_id": "endpoint_alpha",
        "agent_id": "agent_alpha",
        "adapter_name": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "trust_class": "managed",
        "capability_set_id": "caps_alpha",
        "platform": MappingProxyType({"os": "other", "architecture": "test"}),
        "configuration_ref": MappingProxyType(
            {
                "registry_id": "registry_alpha",
                "revision": "registry_rev1",
                "reference": "reference_alpha",
            }
        ),
    }
)
_CAPABILITY_SET = MappingProxyType(
    {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": _SCOPE,
        "capability_set_id": "caps_alpha",
        "revision": "cap_rev1",
        "capabilities": (
            MappingProxyType({"capability": "runtime.health", "quality": "unsupported"}),
            MappingProxyType({"capability": "runtime.reconcile", "quality": "authoritative"}),
            MappingProxyType({"capability": "runtime_profile", "quality": "authoritative"}),
        ),
    }
)
_INITIALIZE_PARAMS = MappingProxyType(
    {
        "requested_protocol_version": "1.0",
        "adapter_id": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "manifest_id": "manifest_alpha",
        "manifest_revision": "manifest_rev1",
        "endpoint": _ENDPOINT,
    }
)
_INITIALIZE_RESULT = MappingProxyType(
    {
        "negotiated_protocol_version": "1.0",
        "adapter_id": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "manifest_id": "manifest_alpha",
        "manifest_revision": "manifest_rev1",
        "endpoint": _ENDPOINT,
        "capability_set": _CAPABILITY_SET,
    }
)
_RECEIPT = MappingProxyType(
    {
        "schema_version": 1,
        "workspace_id": "ws_alpha",
        "scope": _SCOPE,
        "receipt_id": "receipt_attempt_alpha",
        "message_id": "msg_alpha",
        "delivery_id": "delivery_alpha",
        "attempt_id": "attempt_alpha",
        "endpoint_id": "endpoint_alpha",
        "session_ref_id": "session_alpha",
        "state": "completed",
        "evidence": _RECEIPT_EVIDENCE,
    }
)
_HEALTH_RESULT = MappingProxyType(
    {
        "status": "healthy",
        "negotiated_protocol_version": "1.0",
        "adapter_id": "adapter_alpha",
        "adapter_revision": "adapter_rev1",
        "manifest_id": "manifest_alpha",
        "manifest_revision": "manifest_rev1",
        "endpoint_id": "endpoint_alpha",
        "workspace_id": "ws_alpha",
        "scope_kind": "workspace",
        "capability_set_id": "caps_alpha",
        "capability_set_revision": "cap_rev1",
    }
)
_SHUTDOWN_RESULT = MappingProxyType({"status": "shutdown_started"})


@dataclass(frozen=True)
class ClauseReference:
    clause_key: str
    text_sha256: str
    polarity: str
    non_classifying: bool = False


@dataclass(frozen=True)
class TraceFrame:
    sender: str
    receiver: str
    frame: Mapping[str, Any]


@dataclass(frozen=True)
class ExpectedResult:
    method: str
    result: Mapping[str, Any]
    state_effect: str


@dataclass(frozen=True)
class ExpectedRefusal:
    error_name: str
    error_code: int
    state_effect: str
    response_emitted: bool
    accepted: bool = False
    closes_connection: bool = False


@dataclass(frozen=True)
class RuntimeAdapterFixture:
    fixture_id: str
    polarity: str
    clause_refs: tuple[ClauseReference, ...]
    trace: tuple[TraceFrame, ...]
    expectation: ExpectedResult | ExpectedRefusal


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _request(method: str, params: Mapping[str, Any], request_id: str) -> Mapping[str, Any]:
    return _freeze({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def _host_response(request_id: str) -> Mapping[str, Any]:
    return _freeze({"jsonrpc": "2.0", "id": request_id, "result": {"status": "unexpected"}})


def _response(request_id: str, result: Mapping[str, Any]) -> Mapping[str, Any]:
    return _freeze({"jsonrpc": "2.0", "id": request_id, "result": result})


def _reconcile_trace(session_ref: Mapping[str, Any], request_id: str = "reconcile-1") -> TraceFrame:
    return TraceFrame(
        "host",
        "adapter",
        _request(
            "runtime.reconcile",
            {
                "session_ref": session_ref,
                "original_request_id": "deliver-1",
                "delivery_id": "delivery_alpha",
                "attempt_id": "attempt_alpha",
            },
            request_id,
        ),
    )


def _session_ref_with(mutator) -> Mapping[str, Any]:
    value = _thaw(_SESSION_REF)
    mutator(value)
    return _freeze(value)


def _session_ref_with_binding_capability_profile(
    capability_profile_id: str = "runtime_profile_other",
    capability_profile_revision: str = "cap_rev_other",
) -> Mapping[str, Any]:
    value = _thaw(_SESSION_REF)
    authority = value["evidence"]["authority"]
    authority["capability_profile_id"] = capability_profile_id
    authority["capability_profile_revision"] = capability_profile_revision
    value["evidence"] = _with_integrity(value["evidence"])
    return _freeze(value)


def _canonical_digest_without_integrity(value: Mapping[str, Any]) -> str:
    import hashlib
    import json

    material = _thaw(value)
    material.pop("integrity", None)
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _with_integrity(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _thaw(evidence)
    value["integrity"] = _canonical_digest_without_integrity(value)
    return _freeze(value)


def _project_scope(project_id: str = "project_alpha") -> Mapping[str, Any]:
    return _freeze({"kind": "project", "project_id": project_id})


def _project_repository_binding(project_id: str = "project_alpha") -> Mapping[str, Any]:
    return _freeze({"project_id": project_id, "repo_id": "repo_alpha", "canonical_cwd": "/repo"})


def _project_endpoint() -> Mapping[str, Any]:
    value = _thaw(_ENDPOINT)
    value["scope"] = _project_scope()
    return _freeze(value)


def _project_capability_set() -> Mapping[str, Any]:
    value = _thaw(_CAPABILITY_SET)
    value["scope"] = _project_scope()
    return _freeze(value)


def _project_initialize_trace() -> tuple[TraceFrame, TraceFrame]:
    params = _thaw(_INITIALIZE_PARAMS)
    params["endpoint"] = _project_endpoint()
    result = _thaw(_INITIALIZE_RESULT)
    result["endpoint"] = _project_endpoint()
    result["capability_set"] = _project_capability_set()
    return (
        TraceFrame("host", "adapter", _request("initialize", _freeze(params), "initialize-project")),
        TraceFrame("adapter", "host", _response("initialize-project", _freeze(result))),
    )


def _project_session_ref(
    *,
    scope_project_id: str = "project_alpha",
    repository_project_id: str = "project_alpha",
) -> Mapping[str, Any]:
    scope = _project_scope(scope_project_id)
    repository_binding = _project_repository_binding(repository_project_id)
    value = _thaw(_SESSION_REF)
    value["scope"] = scope
    value["repository_binding"] = repository_binding
    value["evidence"]["scope"] = scope
    value["evidence"]["subject"]["repository_binding"] = repository_binding
    value["evidence"] = _with_integrity(value["evidence"])
    return _freeze(value)


def _project_receipt() -> Mapping[str, Any]:
    value = _thaw(_RECEIPT)
    value["scope"] = _project_scope()
    value["evidence"]["scope"] = _project_scope()
    value["evidence"] = _with_integrity(value["evidence"])
    return _freeze(value)


def _initialize_trace() -> tuple[TraceFrame, TraceFrame]:
    return (
        TraceFrame("host", "adapter", _request("initialize", _INITIALIZE_PARAMS, "initialize-1")),
        TraceFrame("adapter", "host", _response("initialize-1", _INITIALIZE_RESULT)),
    )


FIXTURES: tuple[RuntimeAdapterFixture, ...] = (
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-health-request",
        polarity=POLARITY_CONFORMING,
        clause_refs=(
            ClauseReference(
                clause_key="C45acb2959726.1",
                text_sha256="45acb2959726b90f0cb7cc42d2825e8d80971c663143653f0db0bc8673ed9d18",
                polarity=POLARITY_CONFORMING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            TraceFrame("host", "adapter", _request("runtime.health", {}, "health-1")),
        ),
        expectation=ExpectedResult(
            method="runtime.health",
            result=_HEALTH_RESULT,
            state_effect="health_observed",
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-reconcile-request",
        polarity=POLARITY_CONFORMING,
        clause_refs=(
            ClauseReference(
                clause_key="Cc919c73efc96.1",
                text_sha256="c919c73efc96589ba76befb12f5abaec52ff36f6f8c624ecb3379459dc2306e5",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="Cf346f21afa1b.1",
                text_sha256="f346f21afa1bb5c9410cfcb2c4eabcc032a97ba7a1df5e3c5fca24fc2dda884e",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C39aa248f4dd8.1",
                text_sha256="39aa248f4dd89bd10348302ffac0f507163e35506fa5d581189692a942985426",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C5097ad6c480d.1",
                text_sha256="5097ad6c480d38de824999d4fe41c22204b4e35dc46b366b95649e7691ff6039",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C81987c71b9d0.1",
                text_sha256="81987c71b9d07010e889ecf4a4799aa761d83b71d2230a3d6dff1bbd8ed9e040",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C9e388d863ed5.1",
                text_sha256="9e388d863ed5b5506df04fcc0fc29e0bb29dc85fc4bc2954bf0eccbbaa56b2ff",
                polarity=POLARITY_CONFORMING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            _reconcile_trace(_SESSION_REF),
        ),
        expectation=ExpectedResult(
            method="runtime.reconcile",
            result=_RECEIPT,
            state_effect="attempt_reconciled",
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-reconcile-accepts-varied-binding-capability-profile",
        polarity=POLARITY_CONFORMING,
        clause_refs=(
            ClauseReference(
                clause_key="C9a07be32fe6b.1",
                text_sha256="9a07be32fe6bcbd721ebbaca64ce1aa6fa1c3987550b78c411e840f56fe0fa06",
                polarity=POLARITY_CONFORMING,
                non_classifying=True,
            ),
        ),
        trace=(
            *_initialize_trace(),
            _reconcile_trace(
                _session_ref_with_binding_capability_profile(),
                "reconcile-varied-binding-capability-profile",
            ),
        ),
        expectation=ExpectedResult(
            method="runtime.reconcile",
            result=_RECEIPT,
            state_effect="attempt_reconciled",
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-reconcile-rejects-workspace-mismatch",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C72337c3bc58e.1",
                text_sha256="72337c3bc58e165f1cbf09a77ed0f9aa1d67e29f19ded1286522b16a8d2e5fed",
                polarity=POLARITY_VIOLATING,
            ),
            ClauseReference(
                clause_key="C72337c3bc58e.2",
                text_sha256="72337c3bc58e165f1cbf09a77ed0f9aa1d67e29f19ded1286522b16a8d2e5fed",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            _reconcile_trace(
                _session_ref_with(lambda value: value.__setitem__("workspace_id", "ws_other")),
                "reconcile-workspace-mismatch",
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_SESSION_REF",
            error_code=-32008,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-reconcile-rejects-workspace-repository-binding",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C81987c71b9d0.2",
                text_sha256="81987c71b9d07010e889ecf4a4799aa761d83b71d2230a3d6dff1bbd8ed9e040",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            _reconcile_trace(
                _session_ref_with(
                    lambda value: value.__setitem__(
                        "repository_binding",
                        {"project_id": "amiga", "repo_id": "llm-collab", "canonical_cwd": "/repo"},
                    )
                ),
                "reconcile-repository-binding",
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_SESSION_REF",
            error_code=-32008,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-reconcile-rejects-stale-binding-evidence",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C9e388d863ed5.1",
                text_sha256="9e388d863ed5b5506df04fcc0fc29e0bb29dc85fc4bc2954bf0eccbbaa56b2ff",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            _reconcile_trace(
                _session_ref_with(lambda value: value["evidence"].__setitem__("integrity", "sha256:" + "0" * 64)),
                "reconcile-stale-evidence",
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_SESSION_REF",
            error_code=-32008,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-reconcile-rejects-session-ref-schema-drift",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C930c3ccd59a0.1",
                text_sha256="930c3ccd59a0f29e02b46317f30aa3130821fbc53ade72e31aeee680e80a5fd3",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            _reconcile_trace(
                _session_ref_with(lambda value: value.__setitem__("extensions", {"trace": "optional"})),
                "reconcile-session-ref-schema-drift",
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_PARAMS",
            error_code=-32602,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-project-reconcile-request",
        polarity=POLARITY_CONFORMING,
        clause_refs=(
            ClauseReference(
                clause_key="C8ed901b43824.1",
                text_sha256="8ed901b43824181e067e6559b1a7412fc15861d6ae8d54a0b89b127d26ff6799",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C8ed901b43824.2",
                text_sha256="8ed901b43824181e067e6559b1a7412fc15861d6ae8d54a0b89b127d26ff6799",
                polarity=POLARITY_CONFORMING,
            ),
        ),
        trace=(
            *_project_initialize_trace(),
            _reconcile_trace(_project_session_ref(), "reconcile-project"),
        ),
        expectation=ExpectedResult(
            method="runtime.reconcile",
            result=_project_receipt(),
            state_effect="attempt_reconciled",
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-project-reconcile-rejects-scope-mismatch",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C8ed901b43824.1",
                text_sha256="8ed901b43824181e067e6559b1a7412fc15861d6ae8d54a0b89b127d26ff6799",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_project_initialize_trace(),
            _reconcile_trace(
                _project_session_ref(scope_project_id="project_other"),
                "reconcile-project-scope-mismatch",
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_SESSION_REF",
            error_code=-32008,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-project-reconcile-rejects-repository-project-mismatch",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C8ed901b43824.2",
                text_sha256="8ed901b43824181e067e6559b1a7412fc15861d6ae8d54a0b89b127d26ff6799",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_project_initialize_trace(),
            _reconcile_trace(
                _project_session_ref(repository_project_id="project_other"),
                "reconcile-project-repository-mismatch",
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_SESSION_REF",
            error_code=-32008,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-shutdown-success",
        polarity=POLARITY_CONFORMING,
        clause_refs=(
            ClauseReference(
                clause_key="Ce0c84af21a71.1",
                text_sha256="e0c84af21a718d576bf429d33d11b9f67216b1def5f1613fde168a1cdd6baf81",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C43b913cc99f1.1",
                text_sha256="43b913cc99f16fbc7c95db683f62d6592ab081445ef00375ff36ca85f3d2b017",
                polarity=POLARITY_CONFORMING,
            ),
            ClauseReference(
                clause_key="C78f267e558da.1",
                text_sha256="78f267e558dad3c464eaf76f96525bb7479753d80f728dd67e55ef3f692fa7a8",
                polarity=POLARITY_CONFORMING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            TraceFrame("host", "adapter", _request("runtime.shutdown", {}, "shutdown-success")),
        ),
        expectation=ExpectedResult(
            method="runtime.shutdown",
            result=_SHUTDOWN_RESULT,
            state_effect="shutdown_started",
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-post-shutdown-health-refusal",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C78f267e558da.2",
                text_sha256="78f267e558dad3c464eaf76f96525bb7479753d80f728dd67e55ef3f692fa7a8",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            TraceFrame("host", "adapter", _request("runtime.shutdown", {}, "shutdown-before-health")),
            TraceFrame("host", "adapter", _request("runtime.health", {}, "health-after-shutdown")),
        ),
        expectation=ExpectedRefusal(
            error_name="SHUTDOWN_IN_PROGRESS",
            error_code=-32016,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-shutdown-rejects-session-selector",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="Ce0c84af21a71.2",
                text_sha256="e0c84af21a718d576bf429d33d11b9f67216b1def5f1613fde168a1cdd6baf81",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            TraceFrame(
                "host",
                "adapter",
                _request(
                    "runtime.shutdown",
                    {"session_ref": {"session_ref_id": "session-1"}},
                    "shutdown-1",
                ),
            ),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_PARAMS",
            error_code=-32602,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=False,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-host-response-is-direction-fault",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="Cc4e88d8bcf02.1",
                text_sha256="c4e88d8bcf027faa7f029a3977e8ad920331084cd0d265598f9e64fac4098752",
                polarity=POLARITY_VIOLATING,
            ),
            ClauseReference(
                clause_key="C1f287966eb2e.1",
                text_sha256="1f287966eb2e1643dad839084dc6cab69f11d2097de7668be1b7dc07b696094b",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(TraceFrame("host", "adapter", _host_response("response-1")),),
        expectation=ExpectedRefusal(
            error_name="INVALID_REQUEST",
            error_code=-32600,
            state_effect=NO_STATE_CHANGE,
            response_emitted=False,
            closes_connection=True,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-absent-id-runtime-request-closes",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C27ebf7697043.1",
                text_sha256="27ebf76970432d441d1de1e3bf8ea83cc85e8e665c881bd87cae217fb09c3d5a",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            TraceFrame("host", "adapter", _freeze({"jsonrpc": "2.0", "method": "runtime.health", "params": {}})),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_REQUEST",
            error_code=-32600,
            state_effect=NO_STATE_CHANGE,
            response_emitted=False,
            closes_connection=True,
        ),
    ),
    RuntimeAdapterFixture(
        fixture_id="runtime-adapter-null-id-runtime-request-refuses",
        polarity=POLARITY_VIOLATING,
        clause_refs=(
            ClauseReference(
                clause_key="C8f215da97f3e.1",
                text_sha256="8f215da97f3e799ba14db5c115c0badf29445a9a098a5782ec5c681f32ba4e47",
                polarity=POLARITY_VIOLATING,
            ),
        ),
        trace=(
            *_initialize_trace(),
            TraceFrame("host", "adapter", _freeze({"jsonrpc": "2.0", "id": None, "method": "runtime.health", "params": {}})),
        ),
        expectation=ExpectedRefusal(
            error_name="INVALID_REQUEST",
            error_code=-32600,
            state_effect=NO_STATE_CHANGE,
            response_emitted=True,
            closes_connection=True,
        ),
    ),
)


def _direction_valid_request(trace: TraceFrame) -> tuple[bool, Any | None, str | None]:
    frame = _thaw(trace.frame)
    try:
        outcome = classify_direction(trace.sender, trace.receiver, frame)
        if not outcome.direction_valid:
            return False, None, None
        if trace.sender == "host" and trace.receiver == "adapter":
            request_id, method, params = validate_request(frame)
            _validate_request_params(method, params)
            return True, request_id, method
        return True, None, None
    except ConformanceFailure:
        return False, None, None


def _request_would_be_accepted(trace: TraceFrame) -> bool:
    frame = _thaw(trace.frame)
    try:
        outcome = classify_direction(trace.sender, trace.receiver, frame)
        if not outcome.direction_valid:
            return False
        if trace.sender == "host" and trace.receiver == "adapter":
            _request_id, method, params = validate_request(frame)
            _validate_request_params(method, params)
            return True
        return True
    except ConformanceFailure:
        return False


def _derived_refusal(trace: TraceFrame, error_codes: Mapping[str, int]) -> tuple[str, int, bool, bool] | None:
    frame = _thaw(trace.frame)
    try:
        outcome = classify_direction(trace.sender, trace.receiver, frame)
    except ConformanceFailure:
        return None
    if not outcome.direction_valid:
        if outcome.fault is None:
            return None
        return (
            outcome.fault,
            error_codes[outcome.fault],
            bool(outcome.send_response),
            bool(outcome.should_close),
        )
    if trace.sender == "host" and trace.receiver == "adapter":
        try:
            _request_id, method, params = validate_request(frame)
            _validate_request_params(method, params)
        except ConformanceFailure as error:
            if error.clause == "closed-method-set":
                name = "METHOD_NOT_FOUND"
            elif error.clause == "notification-rejected":
                return ("INVALID_REQUEST", error_codes["INVALID_REQUEST"], False, True)
            elif error.clause == "request-id":
                return ("INVALID_REQUEST", error_codes["INVALID_REQUEST"], True, True)
            elif error.clause == "fixture-session-ref-identity":
                name = "INVALID_SESSION_REF"
            elif error.clause in {"closed-params", "fixture-request-params", "fixture-session-ref"}:
                name = "INVALID_PARAMS"
            else:
                name = "INVALID_REQUEST"
            return (name, error_codes[name], True, False)
        return None
    return None


def _validate_conforming_trace(fixture: RuntimeAdapterFixture) -> tuple[str, ...]:
    requests: dict[Any, str] = {}
    methods: list[str] = []
    initialized = False
    initialize_request_id: Any | None = None
    for trace in fixture.trace:
        frame = _thaw(trace.frame)
        try:
            outcome = classify_direction(trace.sender, trace.receiver, frame)
            if not outcome.direction_valid:
                raise ConformanceFailure("fixture-conforming-trace", fixture.fixture_id)
            if trace.sender == "host" and trace.receiver == "adapter":
                request_id, method, params = validate_request(frame)
                if method == "initialize":
                    if methods:
                        raise ConformanceFailure("fixture-conforming-trace", fixture.fixture_id)
                    initialize_request_id = request_id
                elif not initialized:
                    raise ConformanceFailure("fixture-conforming-trace", fixture.fixture_id)
                _validate_request_params(method, params)
                requests[request_id] = method
                methods.append(method)
            elif trace.sender == "adapter" and trace.receiver == "host":
                response_id = frame.get("id") if isinstance(frame, Mapping) else None
                if response_id not in requests:
                    raise ConformanceFailure("fixture-conforming-trace", fixture.fixture_id)
                validate_response(frame, response_id)
                if response_id == initialize_request_id:
                    if "result" not in frame:
                        raise ConformanceFailure("fixture-conforming-trace", fixture.fixture_id)
                    _validate_initialize_result(frame["result"])
                    initialized = True
        except ConformanceFailure as error:
            raise ConformanceFailure("fixture-conforming-trace", fixture.fixture_id) from error
    return tuple(methods)


def _is_closed_mapping(value: Any, keys: set[str]) -> bool:
    return isinstance(value, Mapping) and set(value) == keys


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_scope(value: Any) -> bool:
    return (
        _is_closed_mapping(value, {"kind"})
        and value["kind"] == "workspace"
    ) or (
        _is_closed_mapping(value, {"kind", "project_id"})
        and value["kind"] == "project"
        and _is_nonempty_string(value["project_id"])
    )


def _validate_schema_version(value: Mapping[str, Any], label: str) -> None:
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ConformanceFailure("fixture-result-shape", label)


def _validate_initialize_result(result: Any) -> None:
    required = {
        "negotiated_protocol_version",
        "adapter_id",
        "adapter_revision",
        "manifest_id",
        "manifest_revision",
        "endpoint",
        "capability_set",
    }
    if not _is_closed_mapping(result, required):
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    if result["negotiated_protocol_version"] != "1.0":
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    for key in ("adapter_id", "adapter_revision", "manifest_id", "manifest_revision"):
        if not _is_nonempty_string(result[key]):
            raise ConformanceFailure("fixture-conforming-trace", "initialize")
    endpoint = result["endpoint"]
    try:
        validate_endpoint_v1(endpoint)
    except Exception as error:
        raise ConformanceFailure("fixture-conforming-trace", "initialize") from error
    for key in (
        "workspace_id",
        "endpoint_id",
        "agent_id",
        "adapter_name",
        "adapter_revision",
        "trust_class",
        "capability_set_id",
    ):
        if not _is_nonempty_string(endpoint[key]):
            raise ConformanceFailure("fixture-conforming-trace", "initialize")
    if not _is_scope(endpoint["scope"]):
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    if not _is_closed_mapping(endpoint["platform"], {"os", "architecture"}):
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    if not _is_closed_mapping(endpoint["configuration_ref"], {"registry_id", "revision", "reference"}):
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    capability_set = result["capability_set"]
    if not _is_closed_mapping(
        capability_set,
        {"schema_version", "workspace_id", "scope", "capability_set_id", "revision", "capabilities"},
    ):
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    _validate_schema_version(capability_set, "initialize")
    for key in ("workspace_id", "capability_set_id", "revision"):
        if not _is_nonempty_string(capability_set[key]):
            raise ConformanceFailure("fixture-conforming-trace", "initialize")
    if not _is_scope(capability_set["scope"]):
        raise ConformanceFailure("fixture-conforming-trace", "initialize")
    if not isinstance(capability_set["capabilities"], (list, tuple)) or not capability_set["capabilities"]:
        raise ConformanceFailure("fixture-conforming-trace", "initialize")


def _initialize_result_matches_request(params: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    if params.get("requested_protocol_version") != result.get("negotiated_protocol_version"):
        return False
    for key in ("adapter_id", "adapter_revision", "manifest_id", "manifest_revision"):
        if params.get(key) != result.get(key):
            return False
    endpoint = result.get("endpoint")
    capability_set = result.get("capability_set")
    if not isinstance(endpoint, Mapping) or not isinstance(capability_set, Mapping):
        return False
    return (
        params.get("endpoint") == endpoint
        and endpoint.get("adapter_name") == result.get("adapter_id")
        and endpoint.get("adapter_revision") == result.get("adapter_revision")
        and endpoint.get("workspace_id") == capability_set.get("workspace_id")
        and endpoint.get("scope") == capability_set.get("scope")
        and endpoint.get("capability_set_id") == capability_set.get("capability_set_id")
    )


def _validate_session_ref(value: Any) -> None:
    try:
        value = validate_session_ref_v1(value)
    except Exception as error:
        raise ConformanceFailure("fixture-session-ref", "session_ref") from error
    expected_endpoint = _project_endpoint() if value["scope"].get("kind") == "project" else _ENDPOINT
    if value["workspace_id"] != expected_endpoint["workspace_id"]:
        raise ConformanceFailure("fixture-session-ref-identity", "workspace")
    if value["scope"] != expected_endpoint["scope"]:
        raise ConformanceFailure("fixture-session-ref-identity", "scope")
    repository_binding = value.get("repository_binding")
    if value["scope"]["kind"] == "workspace" and repository_binding is not None:
        raise ConformanceFailure("fixture-session-ref-identity", "repository_binding")
    if value["scope"]["kind"] == "project" and repository_binding is not None:
        if repository_binding["project_id"] != value["scope"]["project_id"]:
            raise ConformanceFailure("fixture-session-ref-identity", "repository_binding")
    if value["endpoint_id"] != expected_endpoint["endpoint_id"]:
        raise ConformanceFailure("fixture-session-ref-identity", "endpoint")
    evidence = value["evidence"]
    if evidence["workspace_id"] != value["workspace_id"] or evidence["scope"] != value["scope"]:
        raise ConformanceFailure("fixture-session-ref-identity", "session evidence scope")
    if evidence["evidence_kind"] != "exact_session_binding" or evidence["quality"] != "authoritative":
        raise ConformanceFailure("fixture-session-ref-identity", "session evidence")
    subject = evidence["subject"]
    subject_keys = {"endpoint_id", "session_ref_id", "native_session_id"}
    if repository_binding is not None:
        subject_keys.add("repository_binding")
    if not _is_closed_mapping(subject, subject_keys):
        raise ConformanceFailure("fixture-session-ref-identity", "session subject")
    if subject["endpoint_id"] != value["endpoint_id"] or subject["session_ref_id"] != value["session_ref_id"]:
        raise ConformanceFailure("fixture-session-ref-identity", "session subject mismatch")
    if subject["native_session_id"] != value["native_session_id"]:
        raise ConformanceFailure("fixture-session-ref-identity", "native session mismatch")
    if repository_binding is not None and subject["repository_binding"] != repository_binding:
        raise ConformanceFailure("fixture-session-ref-identity", "repository binding mismatch")
    try:
        _validate_evidence_integrity(evidence)
    except ConformanceFailure as error:
        raise ConformanceFailure("fixture-session-ref-identity", "session evidence integrity") from error


def _validate_request_params(method: str, params: Mapping[str, Any]) -> None:
    if method == "runtime.reconcile":
        _validate_session_ref(params["session_ref"])
    if method in {"runtime.cancel", "runtime.reconcile"}:
        if not all(isinstance(params[key], str) and params[key] for key in ("original_request_id", "delivery_id", "attempt_id")):
            raise ConformanceFailure("fixture-request-params", method)


def _validate_health_result(result: Mapping[str, Any]) -> None:
    base_required = {
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
    }
    required = set(base_required)
    if isinstance(result, Mapping) and result.get("scope_kind") == "project":
        required.add("project_id")
    if not _is_closed_mapping(result, required):
        raise ConformanceFailure("fixture-result-shape", "runtime.health")
    if result["status"] != "healthy" or result["negotiated_protocol_version"] != "1.0":
        raise ConformanceFailure("fixture-result-shape", "runtime.health")
    if result["scope_kind"] not in {"workspace", "project"}:
        raise ConformanceFailure("fixture-result-shape", "runtime.health")
    if result["scope_kind"] == "workspace" and "project_id" in result:
        raise ConformanceFailure("fixture-result-shape", "runtime.health")
    for key in base_required - {"status", "negotiated_protocol_version", "scope_kind"}:
        if not isinstance(result[key], str) or not result[key]:
            raise ConformanceFailure("fixture-result-shape", "runtime.health")
    if "project_id" in result and (not isinstance(result["project_id"], str) or not result["project_id"]):
        raise ConformanceFailure("fixture-result-shape", "runtime.health")


def _validate_shutdown_result(result: Mapping[str, Any]) -> None:
    if not _is_closed_mapping(result, {"status"}) or result["status"] != "shutdown_started":
        raise ConformanceFailure("fixture-result-shape", "runtime.shutdown")


def _validate_receipt_result(result: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "workspace_id",
        "scope",
        "receipt_id",
        "message_id",
        "delivery_id",
        "attempt_id",
        "endpoint_id",
        "session_ref_id",
        "state",
        "evidence",
    }
    if not _is_closed_mapping(result, required):
        raise ConformanceFailure("fixture-result-shape", "runtime.reconcile")
    _validate_schema_version(result, "runtime.reconcile")
    if result["state"] not in {"accepted", "completed", "rejected_before_acceptance"}:
        raise ConformanceFailure("fixture-result-shape", "runtime.reconcile")
    session_ref = params["session_ref"]
    if result["workspace_id"] != session_ref["workspace_id"] or result["scope"] != session_ref["scope"]:
        raise ConformanceFailure("fixture-result-identity", "runtime.reconcile")
    if result["delivery_id"] != params["delivery_id"] or result["attempt_id"] != params["attempt_id"]:
        raise ConformanceFailure("fixture-result-identity", "runtime.reconcile")
    if result["endpoint_id"] != session_ref["endpoint_id"] or result["session_ref_id"] != session_ref["session_ref_id"]:
        raise ConformanceFailure("fixture-result-identity", "runtime.reconcile")
    evidence = result["evidence"]
    if not _is_closed_mapping(
        evidence,
        {
            "schema_version",
            "workspace_id",
            "scope",
            "evidence_id",
            "evidence_kind",
            "quality",
            "state",
            "authority",
            "subject",
            "correlation_id",
            "observed_at_utc",
            "integrity",
        },
    ):
        raise ConformanceFailure("fixture-result-shape", "runtime.reconcile")
    _validate_schema_version(evidence, "runtime.reconcile")
    if evidence["quality"] != "authoritative" or evidence["state"] != result["state"]:
        raise ConformanceFailure("fixture-result-shape", "runtime.reconcile")
    subject = evidence["subject"]
    if not _is_closed_mapping(subject, {"message_id", "delivery_id", "attempt_id", "endpoint_id", "session_ref_id"}):
        raise ConformanceFailure("fixture-result-shape", "runtime.reconcile")
    for key in ("message_id", "delivery_id", "attempt_id", "endpoint_id", "session_ref_id"):
        if subject[key] != result[key]:
            raise ConformanceFailure("fixture-result-identity", "runtime.reconcile")
    _validate_evidence_integrity(result["evidence"])


def _validate_result_shape(fixture: RuntimeAdapterFixture, methods: tuple[str, ...]) -> None:
    expectation = fixture.expectation
    if not isinstance(expectation, ExpectedResult):
        raise ConformanceFailure("fixture-conforming-expectation", fixture.fixture_id)
    if expectation.method not in methods:
        raise ConformanceFailure("fixture-result-method", fixture.fixture_id)
    if not isinstance(expectation.result, Mapping):
        raise ConformanceFailure("fixture-result-shape", expectation.method)
    if expectation.method == "runtime.health":
        _validate_health_result(expectation.result)
    elif expectation.method == "runtime.shutdown":
        _validate_shutdown_result(expectation.result)
    elif expectation.method == "runtime.reconcile":
        params = None
        for trace in fixture.trace:
            frame = _thaw(trace.frame)
            if (
                isinstance(frame, Mapping)
                and frame.get("method") == "runtime.reconcile"
                and isinstance(frame.get("params"), Mapping)
            ):
                params = frame["params"]
                break
        if params is None:
            raise ConformanceFailure("fixture-result-method", fixture.fixture_id)
        _validate_receipt_result(expectation.result, params)
    else:
        raise ConformanceFailure("fixture-result-method", fixture.fixture_id)


def _validate_evidence_integrity(evidence: Mapping[str, Any]) -> None:
    if evidence.get("integrity") != _canonical_digest_without_integrity(evidence):
        raise ConformanceFailure("fixture-evidence-integrity", "state evidence")


def _stateful_post_shutdown_refusal(
    fixture: RuntimeAdapterFixture,
    expected: tuple[str, int, bool, bool],
) -> tuple[str, int, bool, bool] | None:
    if expected != ("SHUTDOWN_IN_PROGRESS", -32016, True, False):
        return None
    if len(fixture.trace) < 4:
        return None
    initialize_response = fixture.trace[1]
    initialize_response_frame = _thaw(initialize_response.frame)
    host_frames = [
        _thaw(trace.frame)
        for trace in fixture.trace
        if trace.sender == "host" and trace.receiver == "adapter"
    ]
    if len(host_frames) < 3:
        return None
    shutdown = host_frames[-2]
    final = host_frames[-1]
    try:
        initialize_request_id, initialize_method, initialize_params = validate_request(host_frames[0])
        if initialize_method != "initialize":
            return None
        if initialize_response.sender != "adapter" or initialize_response.receiver != "host":
            return None
        validate_response(initialize_response_frame, initialize_request_id)
        if "result" not in initialize_response_frame:
            return None
        _validate_initialize_result(initialize_response_frame["result"])
        if not _initialize_result_matches_request(initialize_params, initialize_response_frame["result"]):
            return None
        _shutdown_request_id, shutdown_method, shutdown_params = validate_request(shutdown)
        if shutdown_method != "runtime.shutdown" or shutdown_params != {}:
            return None
        _final_request_id, final_method, final_params = validate_request(final)
        if final_method != "runtime.health" or final_params != {}:
            return None
        if (
            fixture.trace[-1].sender != "host"
            or fixture.trace[-1].receiver != "adapter"
            or _thaw(fixture.trace[-1].frame) != final
        ):
            return None
    except ConformanceFailure:
        return None
    return expected


def _validate_clause_refs(
    fixture: RuntimeAdapterFixture,
    live_by_key: Mapping[str, Any],
) -> None:
    if not fixture.clause_refs:
        raise ConformanceFailure("fixture-clause-ref", fixture.fixture_id)
    for ref in fixture.clause_refs:
        if ref.polarity != fixture.polarity:
            raise ConformanceFailure("fixture-polarity-ref", fixture.fixture_id)
        if ref.polarity not in POLARITIES:
            raise ConformanceFailure("fixture-polarity", fixture.fixture_id)
        live = live_by_key.get(ref.clause_key)
        if live is None:
            raise ConformanceFailure("fixture-clause-key", ref.clause_key)
        if ref.non_classifying and ref.polarity != POLARITY_CONFORMING:
            raise ConformanceFailure("fixture-non-classifying-polarity", fixture.fixture_id)
        if ref.non_classifying and live.keyword != "MUST NOT":
            raise ConformanceFailure("fixture-non-classifying-keyword", ref.clause_key)
        if ref.text_sha256 != live.text_sha256:
            raise ConformanceFailure("fixture-text-hash", ref.clause_key)


def _validate_expectation(fixture: RuntimeAdapterFixture, error_codes: Mapping[str, int]) -> None:
    if fixture.polarity == POLARITY_CONFORMING:
        if not isinstance(fixture.expectation, ExpectedResult):
            raise ConformanceFailure("fixture-conforming-expectation", fixture.fixture_id)
        if not fixture.expectation.method or fixture.expectation.state_effect == NO_STATE_CHANGE:
            raise ConformanceFailure("fixture-conforming-expectation", fixture.fixture_id)
        _validate_result_shape(fixture, _validate_conforming_trace(fixture))
        return

    if fixture.polarity != POLARITY_VIOLATING:
        raise ConformanceFailure("fixture-polarity", fixture.fixture_id)
    if not isinstance(fixture.expectation, ExpectedRefusal):
        raise ConformanceFailure("fixture-violating-expectation", fixture.fixture_id)
    if fixture.expectation.accepted:
        raise ConformanceFailure("fixture-violating-accepted", fixture.fixture_id)
    if fixture.expectation.state_effect != NO_STATE_CHANGE:
        raise ConformanceFailure("fixture-violating-state-effect", fixture.fixture_id)
    if fixture.expectation.error_name in {"ANY_ERROR", "*", ""}:
        raise ConformanceFailure("fixture-violating-refusal", fixture.fixture_id)
    if not isinstance(fixture.expectation.error_code, int):
        raise ConformanceFailure("fixture-violating-refusal", fixture.fixture_id)
    if error_codes.get(fixture.expectation.error_name) != fixture.expectation.error_code:
        raise ConformanceFailure("fixture-violating-refusal", fixture.fixture_id)
    expected = (
        fixture.expectation.error_name,
        fixture.expectation.error_code,
        fixture.expectation.response_emitted,
        fixture.expectation.closes_connection,
    )
    stateful_refusal = _stateful_post_shutdown_refusal(fixture, expected)
    if all(_request_would_be_accepted(frame) for frame in fixture.trace) and stateful_refusal is None:
        raise ConformanceFailure("fixture-violating-trace", fixture.fixture_id)
    derived = tuple(
        item
        for item in (
            *(_derived_refusal(frame, error_codes) for frame in fixture.trace),
            stateful_refusal,
        )
        if item is not None
    )
    if not derived or expected not in derived:
        raise ConformanceFailure("fixture-violating-refusal", fixture.fixture_id)


def validate_fixtures(
    protocol_text: str,
    fixtures: Iterable[RuntimeAdapterFixture] = FIXTURES,
) -> tuple[RuntimeAdapterFixture, ...]:
    """Validate replay fixtures against the live protocol extractor."""

    live_by_key = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    try:
        error_codes = protocol_error_codes(protocol_text)
    except ConformanceFailure as error:
        raise ConformanceFailure("fixture-error-codes", error.message) from error
    checked = tuple(fixtures)
    if not checked:
        raise ConformanceFailure("fixture-empty", "no fixtures")
    for fixture in checked:
        if fixture.polarity not in POLARITIES:
            raise ConformanceFailure("fixture-polarity", fixture.fixture_id)
        if not fixture.trace:
            raise ConformanceFailure("fixture-trace", fixture.fixture_id)
        _validate_clause_refs(fixture, live_by_key)
        _validate_expectation(fixture, error_codes)
    return checked
