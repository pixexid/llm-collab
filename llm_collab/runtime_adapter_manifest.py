"""Trusted manifest resolver for Runtime Adapter JSON-RPC V1.

This module resolves adapter execution facts from an already-reviewed manifest
mapping. It intentionally cannot spawn a process or read runtime/project state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


UNTRUSTED_MANIFEST_INPUT = "UNTRUSTED_MANIFEST_INPUT"
_MANIFEST_FIELDS = frozenset(
    {
        "adapter_id",
        "adapter_revision",
        "manifest_id",
        "manifest_revision",
        "endpoint",
        "executable",
        "argv",
        "working_directory",
        "environment",
        "environment_allowlist",
    }
)


class ManifestResolutionError(ValueError):
    """Raised when Clause 5 trusted-manifest resolution fails."""

    code = UNTRUSTED_MANIFEST_INPUT


@dataclass(frozen=True)
class ResolvedAdapter:
    adapter_id: str
    adapter_revision: str
    manifest_id: str
    manifest_revision: str
    endpoint: Mapping[str, Any]
    executable: str
    argv: tuple[str, ...]
    working_directory: str
    environment: Mapping[str, str]


class TrustedManifestRegistry:
    """Reviewed manifest records; resolving from it accepts only adapter id."""

    def __init__(self, manifests: Mapping[str, Mapping[str, Any]]):
        if not isinstance(manifests, Mapping):
            _reject("manifest registry must be a mapping")
        self._manifests = MappingProxyType(copy.deepcopy(dict(manifests)))

    def resolve(self, adapter_id: str) -> ResolvedAdapter:
        return _resolve_adapter_manifest(adapter_id, self._manifests)


def _reject(message: str) -> None:
    raise ManifestResolutionError(message)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        _reject(f"{name} must be a non-empty string")
    return value


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        _reject(f"{name} must be a non-empty sequence")
    out = tuple(value)
    if not all(isinstance(item, str) and item for item in out):
        _reject(f"{name} entries must be non-empty strings")
    return out


def _string_mapping(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        _reject(f"{name} must be a mapping")
    out = dict(value)
    if not all(isinstance(key, str) and key and isinstance(val, str) for key, val in out.items()):
        _reject(f"{name} entries must be string:string")
    return out


def _manifest(adapter_id: str, manifests: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    if not isinstance(manifests, Mapping):
        _reject("manifest registry must be a mapping")
    try:
        manifest = manifests[adapter_id]
    except KeyError as error:
        raise ManifestResolutionError("adapter_id is not in the trusted manifest registry") from error
    if not isinstance(manifest, Mapping):
        _reject("manifest record must be a mapping")
    return manifest


def _identity_matches(
    *,
    trusted_key: str,
    adapter_id: str,
    adapter_revision: str,
    manifest_id: str,
    manifest_revision: str,
    endpoint: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> bool:
    return (
        adapter_id == trusted_key
        and adapter_id == endpoint.get("adapter_name")
        and adapter_revision == endpoint.get("adapter_revision")
        and manifest_id == manifest.get("manifest_id")
        and manifest_revision == manifest.get("manifest_revision")
    )


def _require_identity(
    *,
    trusted_key: str,
    adapter_id: str,
    adapter_revision: str,
    manifest_id: str,
    manifest_revision: str,
    endpoint: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    if not _identity_matches(
        trusted_key=trusted_key,
        adapter_id=adapter_id,
        adapter_revision=adapter_revision,
        manifest_id=manifest_id,
        manifest_revision=manifest_revision,
        endpoint=endpoint,
        manifest=manifest,
    ):
        _reject("adapter, endpoint, and manifest identities must match exactly")


def _resolve_adapter_manifest(adapter_id: str, manifests: Mapping[str, Mapping[str, Any]]) -> ResolvedAdapter:
    """Resolve one trusted adapter manifest by exact adapter id.

    Caller-provided executable, argv, environment, working directory, shell, and
    manifest path inputs are unrepresentable in TrustedManifestRegistry.resolve.
    """

    trusted_key = _string(adapter_id, "adapter_id")
    manifest = _manifest(trusted_key, manifests)
    unknown_manifest_fields = set(manifest) - _MANIFEST_FIELDS
    if unknown_manifest_fields:
        _reject("manifest record contains fields outside the trusted schema")
    endpoint = manifest.get("endpoint")
    if not isinstance(endpoint, Mapping):
        _reject("endpoint must be a mapping")

    resolved_adapter_id = _string(manifest.get("adapter_id"), "adapter_id")
    adapter_revision = _string(manifest.get("adapter_revision"), "adapter_revision")
    manifest_id = _string(manifest.get("manifest_id"), "manifest_id")
    manifest_revision = _string(manifest.get("manifest_revision"), "manifest_revision")
    executable = _string(manifest.get("executable"), "executable")
    argv = _string_tuple(manifest.get("argv"), "argv")
    working_directory = _string(manifest.get("working_directory"), "working_directory")
    environment = _string_mapping(manifest.get("environment", {}), "environment")
    environment_allowlist = frozenset(_string_tuple(manifest.get("environment_allowlist"), "environment_allowlist"))
    unknown = set(environment) - environment_allowlist
    if unknown:
        _reject("environment contains keys outside the manifest allowlist")

    _require_identity(
        trusted_key=trusted_key,
        adapter_id=resolved_adapter_id,
        adapter_revision=adapter_revision,
        manifest_id=manifest_id,
        manifest_revision=manifest_revision,
        endpoint=endpoint,
        manifest=manifest,
    )

    return ResolvedAdapter(
        adapter_id=resolved_adapter_id,
        adapter_revision=adapter_revision,
        manifest_id=manifest_id,
        manifest_revision=manifest_revision,
        endpoint=MappingProxyType(dict(endpoint)),
        executable=executable,
        argv=argv,
        working_directory=working_directory,
        environment=MappingProxyType(environment),
    )


def validate_initialized_identity(resolved: ResolvedAdapter, initialized: Mapping[str, Any]) -> None:
    """Verify initialize request/result identity fields against one resolved manifest."""

    if not isinstance(initialized, Mapping):
        _reject("initialized identity must be a mapping")
    endpoint = initialized.get("endpoint")
    if not isinstance(endpoint, Mapping):
        _reject("initialized endpoint must be a mapping")
    _require_identity(
        trusted_key=resolved.adapter_id,
        adapter_id=_string(initialized.get("adapter_id"), "adapter_id"),
        adapter_revision=_string(initialized.get("adapter_revision"), "adapter_revision"),
        manifest_id=_string(initialized.get("manifest_id"), "manifest_id"),
        manifest_revision=_string(initialized.get("manifest_revision"), "manifest_revision"),
        endpoint=endpoint,
        manifest={
            "manifest_id": resolved.manifest_id,
            "manifest_revision": resolved.manifest_revision,
        },
    )
