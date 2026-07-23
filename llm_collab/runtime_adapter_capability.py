"""Pure P6 capability authority for Runtime Adapter JSON-RPC V1.

The host-trusted registry is the authority. Adapter-declared initialized
capabilities are never authority by themselves; ``bind_initialized`` accepts
them only when the single validated initialize result exactly matches the
host-reviewed expected registry copy.

This module intentionally does not read files, spawn adapters, touch runtime or
project state, persist data, inspect sessions, publish evidence, or import the
claim engine. Storage/config integration is a later boundary; callers construct
``TrustedCapabilityAuthorityRegistry`` from already-reviewed local data.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from llm_collab.runtime_adapter_manifest import (
    ResolvedAdapter,
    validate_initialized_identity,
)
from llm_collab.runtime_adapter_requests import (
    METHOD_CANCEL,
    METHOD_DELIVER,
    METHOD_HEALTH,
    METHOD_RECONCILE,
    METHOD_SHUTDOWN,
)


CAPABILITY_NOT_DECLARED = "CAPABILITY_NOT_DECLARED"
SESSION_METHODS = frozenset((METHOD_DELIVER, METHOD_CANCEL, METHOD_RECONCILE))
PROTOCOL_CONTROL_METHODS = frozenset((METHOD_HEALTH, METHOD_SHUTDOWN))

_AUTHORITY_RECORD_FIELDS = frozenset(("adapter_id", "endpoint", "capability_set", "session_action_relations"))
_ENDPOINT_FIELDS = frozenset(
    (
        "schema_version",
        "workspace_id",
        "scope",
        "endpoint_id",
        "agent_id",
        "adapter_name",
        "adapter_revision",
        "trust_class",
        "capability_set_id",
        "platform",
        "configuration_ref",
    )
)
_CAPABILITY_SET_FIELDS = frozenset(
    ("schema_version", "workspace_id", "scope", "capability_set_id", "revision", "capabilities")
)
_CAPABILITY_FIELDS = frozenset(("capability", "quality", "constraints", "evidence"))
_UNSUPPORTED_CAPABILITY_FIELDS = frozenset(("capability", "quality"))
_ATTESTATION_FIELDS = frozenset(("evidence_kind", "source_id", "source_revision", "integrity"))
_SESSION_ACTION_RELATION_FIELDS = frozenset(
    ("capability_set_id", "capability_set_revision", "method", "selected_capability", "required_quality")
)


class CapabilityAuthorityError(ValueError):
    """Raised when P6 capability authority fails closed."""

    code = CAPABILITY_NOT_DECLARED


@dataclass(frozen=True)
class BoundCapabilityContext:
    adapter_id: str
    adapter_revision: str
    manifest_id: str
    manifest_revision: str
    endpoint: Mapping[str, Any]
    capability_set: Mapping[str, Any]


@dataclass(frozen=True)
class CapabilityDecision:
    method: str
    selected_capability: str
    selected_quality: str


class TrustedCapabilityAuthorityRegistry:
    """Host-reviewed P6 authority records, keyed by exact adapter id."""

    def __init__(self, records: Mapping[str, Mapping[str, Any]]):
        if not isinstance(records, Mapping):
            _reject("capability authority registry must be a mapping")
        frozen = copy.deepcopy(dict(records))
        for adapter_id, record in frozen.items():
            _validate_record_key(adapter_id)
            _validate_authority_record(adapter_id, record)
        self._records = MappingProxyType(frozen)

    def bind_initialized(self, *, resolved: ResolvedAdapter, initialized: Mapping[str, Any]) -> BoundCapabilityContext:
        """Bind one initialized adapter result to the host-trusted capability set."""

        if not isinstance(resolved, ResolvedAdapter):
            _reject("resolved adapter is required")
        if not isinstance(initialized, Mapping):
            _reject("initialized result must be a mapping")
        try:
            validate_initialized_identity(resolved, initialized)
        except Exception as error:
            raise CapabilityAuthorityError("initialized identity is not trusted") from error

        record = self._record(resolved.adapter_id)
        endpoint = _mapping(initialized.get("endpoint"), "initialized endpoint")
        capability_set = _mapping(initialized.get("capability_set"), "initialized capability_set")
        trusted_endpoint = _mapping(record.get("endpoint"), "trusted endpoint")
        trusted_capability_set = _mapping(record.get("capability_set"), "trusted capability_set")

        _require_exact_endpoint(resolved, endpoint, trusted_endpoint)
        _require_exact_capability_set(endpoint, capability_set, trusted_capability_set)
        _validate_capability_entries(endpoint, capability_set)

        return BoundCapabilityContext(
            adapter_id=resolved.adapter_id,
            adapter_revision=resolved.adapter_revision,
            manifest_id=resolved.manifest_id,
            manifest_revision=resolved.manifest_revision,
            endpoint=_freeze(endpoint),
            capability_set=_freeze(capability_set),
        )

    def require_capability_entry(self, bound: BoundCapabilityContext, capability: str) -> Mapping[str, Any]:
        """Return one non-unsupported, adapter-attested capability entry."""

        if not isinstance(bound, BoundCapabilityContext):
            _reject("bound capability context is required")
        entry = _select_capability(bound.capability_set, capability)
        _require_supported_entry(bound.endpoint, entry)
        return _freeze(entry)

    def validate_request_authority(
        self,
        bound: BoundCapabilityContext,
        method: str,
        *,
        caller_authority_fields: Mapping[str, Any] | None = None,
    ) -> CapabilityDecision:
        """Authorize one post-initialize session action from host-trusted relations."""

        if not isinstance(bound, BoundCapabilityContext):
            _reject("bound capability context is required")
        if caller_authority_fields is not None:
            _reject("caller-supplied authority fields are not trusted")
        if method not in SESSION_METHODS:
            _reject("method is not a product capability session action")
        record = self._record(bound.adapter_id)
        relation = _select_session_action_relation(record["session_action_relations"], bound, method)
        entry = _select_capability(bound.capability_set, relation["selected_capability"])
        _require_supported_entry(bound.endpoint, entry)
        if entry["quality"] != relation["required_quality"]:
            _reject("selected capability quality does not match trusted action relation")
        return CapabilityDecision(
            method=method,
            selected_capability=relation["selected_capability"],
            selected_quality=entry["quality"],
        )

    def _record(self, adapter_id: str) -> Mapping[str, Any]:
        try:
            return self._records[adapter_id]
        except KeyError as error:
            raise CapabilityAuthorityError("adapter has no trusted capability authority record") from error


def method_requires_product_capability(method: str) -> bool:
    """Return whether a method is a P6 product-capability session method."""

    return method in SESSION_METHODS


def _reject(message: str) -> None:
    raise CapabilityAuthorityError(message)


def _validate_record_key(value: Any) -> None:
    if not isinstance(value, str) or not value:
        _reject("authority registry keys must be non-empty adapter ids")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject(f"{name} must be a mapping")
    return value


def _validate_authority_record(adapter_id: str, record: Any) -> None:
    if not isinstance(record, Mapping):
        _reject("capability authority record must be a mapping")
    if set(record) != _AUTHORITY_RECORD_FIELDS:
        _reject("capability authority record contains fields outside the trusted schema")
    if record.get("adapter_id") != adapter_id:
        _reject("authority record adapter_id must match its trusted key")
    endpoint = _mapping(record.get("endpoint"), "trusted endpoint")
    capability_set = _mapping(record.get("capability_set"), "trusted capability_set")
    _validate_endpoint_shape(endpoint)
    _validate_capability_set_shape(capability_set)
    relations = record.get("session_action_relations")
    if not isinstance(relations, (list, tuple)):
        _reject("session action relations must be a sequence")
    for relation in relations:
        _validate_session_action_relation_shape(relation)


def _validate_endpoint_shape(endpoint: Mapping[str, Any]) -> None:
    if set(endpoint) != _ENDPOINT_FIELDS:
        _reject("endpoint contains fields outside the trusted schema")
    for key in ("workspace_id", "endpoint_id", "adapter_name", "adapter_revision", "capability_set_id"):
        if not isinstance(endpoint.get(key), str) or not endpoint.get(key):
            _reject(f"endpoint {key} must be a non-empty string")
    _validate_scope(endpoint.get("scope"))


def _validate_capability_set_shape(capability_set: Mapping[str, Any]) -> None:
    if set(capability_set) != _CAPABILITY_SET_FIELDS:
        _reject("CapabilitySetV1 contains fields outside the trusted schema")
    for key in ("workspace_id", "capability_set_id", "revision"):
        if not isinstance(capability_set.get(key), str) or not capability_set.get(key):
            _reject(f"CapabilitySetV1 {key} must be a non-empty string")
    _validate_scope(capability_set.get("scope"))
    capabilities = capability_set.get("capabilities")
    if not isinstance(capabilities, (list, tuple)):
        _reject("CapabilitySetV1 capabilities must be a sequence")


def _validate_scope(scope: Any) -> None:
    if not isinstance(scope, Mapping):
        _reject("scope must be a mapping")
    if scope.get("kind") == "workspace":
        if set(scope) != {"kind"}:
            _reject("workspace scope must omit project_id")
        return
    if scope.get("kind") == "project":
        if set(scope) != {"kind", "project_id"}:
            _reject("project scope must contain only kind and project_id")
        if not isinstance(scope.get("project_id"), str) or not scope.get("project_id"):
            _reject("project scope requires non-empty project_id")
        return
    _reject("scope kind must be workspace or project")


def _require_exact_endpoint(
    resolved: ResolvedAdapter,
    initialized_endpoint: Mapping[str, Any],
    trusted_endpoint: Mapping[str, Any],
) -> None:
    _validate_endpoint_shape(initialized_endpoint)
    if dict(initialized_endpoint) != dict(resolved.endpoint):
        _reject("initialized endpoint must come from the single resolved manifest")
    if dict(initialized_endpoint) != dict(trusted_endpoint):
        _reject("initialized endpoint diverges from trusted capability authority")


def _require_exact_capability_set(
    endpoint: Mapping[str, Any],
    initialized_capability_set: Mapping[str, Any],
    trusted_capability_set: Mapping[str, Any],
) -> None:
    _validate_capability_set_shape(initialized_capability_set)
    if dict(initialized_capability_set) != dict(trusted_capability_set):
        _reject("adapter-declared capability set diverges from trusted registry")
    if initialized_capability_set["workspace_id"] != endpoint["workspace_id"]:
        _reject("CapabilitySetV1 workspace must match endpoint")
    if dict(initialized_capability_set["scope"]) != dict(endpoint["scope"]):
        _reject("CapabilitySetV1 scope must match endpoint")
    if initialized_capability_set["capability_set_id"] != endpoint["capability_set_id"]:
        _reject("CapabilitySetV1 id must match endpoint")


def _validate_capability_entries(endpoint: Mapping[str, Any], capability_set: Mapping[str, Any]) -> None:
    capabilities = capability_set["capabilities"]
    for entry in capabilities:
        if not isinstance(entry, Mapping):
            _reject("capability entries must be mappings")
        quality = entry.get("quality")
        if quality == "unsupported":
            if set(entry) != _UNSUPPORTED_CAPABILITY_FIELDS:
                _reject("unsupported capability entry contains unsupported authority fields")
            _capability_token(entry.get("capability"))
            continue
        _require_supported_entry(endpoint, entry)


def _require_supported_entry(endpoint: Mapping[str, Any], entry: Mapping[str, Any]) -> None:
    if set(entry) != _CAPABILITY_FIELDS:
        _reject("supported capability entry contains fields outside the trusted schema")
    _capability_token(entry.get("capability"))
    if entry.get("quality") == "unsupported":
        _reject("selected capability is unsupported")
    if not isinstance(entry.get("quality"), str) or not entry.get("quality"):
        _reject("selected capability quality must be a non-empty string")
    if not isinstance(entry.get("constraints"), Mapping):
        _reject("selected capability constraints must be a mapping")
    evidence = _mapping(entry.get("evidence"), "capability attestation")
    if set(evidence) != _ATTESTATION_FIELDS:
        _reject("capability attestation contains fields outside the trusted schema")
    if evidence.get("evidence_kind") != "profile_attestation":
        _reject("capability attestation kind must be profile_attestation")
    if evidence.get("source_id") != endpoint["adapter_name"]:
        _reject("capability attestation source_id must match endpoint adapter")
    if evidence.get("source_revision") != endpoint["adapter_revision"]:
        _reject("capability attestation source_revision must match endpoint adapter revision")
    integrity = evidence.get("integrity")
    if (
        not isinstance(integrity, str)
        or len(integrity) != 71
        or not integrity.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in integrity[7:])
    ):
        _reject("capability attestation integrity must be a sha256 digest")


def _validate_session_action_relation_shape(relation: Any) -> None:
    if not isinstance(relation, Mapping):
        _reject("session action relation must be a mapping")
    if set(relation) != _SESSION_ACTION_RELATION_FIELDS:
        _reject("session action relation contains fields outside the trusted schema")
    for key in ("capability_set_id", "capability_set_revision", "selected_capability", "required_quality"):
        if not isinstance(relation.get(key), str) or not relation.get(key):
            _reject(f"session action relation {key} must be a non-empty string")
    if relation.get("method") not in SESSION_METHODS:
        _reject("session action relation method must be a product session method")


def _select_session_action_relation(
    relations: Any,
    bound: BoundCapabilityContext,
    method: str,
) -> Mapping[str, Any]:
    matches = []
    for relation in relations:
        _validate_session_action_relation_shape(relation)
        if (
            relation["capability_set_id"] == bound.endpoint["capability_set_id"]
            and relation["capability_set_revision"] == bound.capability_set["revision"]
            and relation["method"] == method
        ):
            matches.append(relation)
    if len(matches) != 1:
        _reject("session action relation must match exactly once")
    return matches[0]


def _select_capability(capability_set: Mapping[str, Any], capability: str) -> Mapping[str, Any]:
    token = _capability_token(capability)
    matches = [
        entry for entry in capability_set["capabilities"] if isinstance(entry, Mapping) and entry.get("capability") == token
    ]
    if len(matches) != 1:
        _reject("selected capability must occur exactly once")
    return matches[0]


def _capability_token(value: Any) -> str:
    if not isinstance(value, str) or not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        _reject("capability token must be a non-empty string without controls")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value
