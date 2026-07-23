"""Inert SessionRefV1 assembly for managed Codex sessions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from llm_collab.codex_runtime_home import RuntimeHomeIdentity


SCHEMA_ID = "https://llm-collab.dev/schemas/standalone/v1/session-ref.schema.json"
CATALOG_ID = "https://llm-collab.dev/schemas/standalone/v1/index.json"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "standalone" / "v1"
RUNTIME_HOME_REALPATH_FIELD = "x_note_runtime_home_realpath"
RUNTIME_HOME_ID_FIELD = "x_note_runtime_home_id"

_VALIDATOR = None


class SessionRefError(ValueError):
    """Raised when a SessionRefV1 candidate cannot be assembled or validated."""


@dataclass(frozen=True)
class SessionAuthority:
    authority_kind: str
    identity: str
    implementation_revision: str
    capability_profile_id: str
    capability_profile_revision: str


@dataclass(frozen=True)
class RepositoryBinding:
    project_id: str
    repo_id: str
    repo_root: str | os.PathLike[str]
    cwd: str | os.PathLike[str]


def build_session_ref(
    *,
    workspace_id: str,
    scope: Mapping[str, Any],
    endpoint_id: str,
    native_session_id: str,
    runtime_home: RuntimeHomeIdentity,
    authority: SessionAuthority,
    observed_at_utc: str,
    correlation_id: str,
    repository_binding: RepositoryBinding | None = None,
    expected_session_ref_id: str | None = None,
    expected_evidence_integrity: str | None = None,
) -> Mapping[str, Any]:
    """Build and schema-validate one inert SessionRefV1 candidate.

    The caller supplies already-proven facts. Derived identifiers and integrity
    are checks only when expected values are passed.
    """

    if not isinstance(runtime_home, RuntimeHomeIdentity):
        raise SessionRefError("runtime_home must be a RuntimeHomeIdentity")
    scope_value = _closed_mapping(scope, "scope")
    repo_value = _repository_binding(repository_binding, scope_value)
    extensions = {
        RUNTIME_HOME_REALPATH_FIELD: runtime_home.runtime_home_realpath,
        RUNTIME_HOME_ID_FIELD: runtime_home.runtime_home_id,
    }
    subject = {
        "endpoint_id": endpoint_id,
        "native_session_id": native_session_id,
    }
    if repo_value is not None:
        subject["repository_binding"] = repo_value

    derivation_seed = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "scope": scope_value,
        "endpoint_id": endpoint_id,
        "native_session_id": native_session_id,
        "repository_binding": repo_value,
        "runtime_home": {
            "runtime_home_realpath": runtime_home.runtime_home_realpath,
            "runtime_home_id": runtime_home.runtime_home_id,
        },
        "authority": _authority_dict(authority),
    }
    session_ref_id = _session_id_for(derivation_seed)
    if expected_session_ref_id is not None and expected_session_ref_id != session_ref_id:
        raise SessionRefError("session_ref_id mismatch")

    subject["session_ref_id"] = session_ref_id
    subject = _subject_in_schema_order(subject)
    evidence = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "scope": deepcopy(scope_value),
        "evidence_id": _evidence_id_for(derivation_seed),
        "evidence_kind": "exact_session_binding",
        "quality": "authoritative",
        "state": "routed",
        "authority": _authority_dict(authority),
        "subject": subject,
        "correlation_id": correlation_id,
        "observed_at_utc": observed_at_utc,
    }
    evidence["integrity"] = _digest_without(evidence)
    if expected_evidence_integrity is not None and expected_evidence_integrity != evidence["integrity"]:
        raise SessionRefError("evidence integrity mismatch")

    candidate: dict[str, Any] = {
        "schema_version": 1,
        "workspace_id": workspace_id,
        "scope": scope_value,
        "session_ref_id": session_ref_id,
        "endpoint_id": endpoint_id,
        "native_session_id": native_session_id,
    }
    if repo_value is not None:
        candidate["repository_binding"] = repo_value
    candidate["evidence"] = evidence
    candidate["extensions"] = extensions
    validate_session_ref(
        candidate,
        runtime_home=runtime_home,
        authority=authority,
        repository_binding=repository_binding,
    )
    return deepcopy(candidate)


def validate_session_ref(
    candidate: Mapping[str, Any],
    *,
    runtime_home: RuntimeHomeIdentity,
    authority: SessionAuthority | None = None,
    repository_binding: RepositoryBinding | None = None,
) -> None:
    """Validate schema conformance and the semantic binding this producer owns."""

    if not isinstance(runtime_home, RuntimeHomeIdentity):
        raise SessionRefError("runtime_home must be a RuntimeHomeIdentity")
    document = _closed_mapping(candidate, "session_ref")
    try:
        _validator().validate(document)
    except Exception as error:
        raise SessionRefError("SessionRefV1 schema validation failed") from error
    extensions = document.get("extensions")
    if not isinstance(extensions, Mapping):
        raise SessionRefError("runtime-home annotations missing")
    if extensions.get(RUNTIME_HOME_REALPATH_FIELD) != runtime_home.runtime_home_realpath:
        raise SessionRefError("runtime_home_realpath mismatch")
    if extensions.get(RUNTIME_HOME_ID_FIELD) != runtime_home.runtime_home_id:
        raise SessionRefError("runtime_home_id mismatch")
    evidence = document["evidence"]
    if evidence.get("integrity") != _digest_without(evidence):
        raise SessionRefError("evidence integrity mismatch")
    if evidence.get("workspace_id") != document["workspace_id"] or evidence.get("scope") != document["scope"]:
        raise SessionRefError("evidence scope mismatch")
    if authority is not None and evidence.get("authority") != _authority_dict(authority):
        raise SessionRefError("authority mismatch")
    expected_subject = {
        "endpoint_id": document["endpoint_id"],
        "session_ref_id": document["session_ref_id"],
        "native_session_id": document["native_session_id"],
    }
    if "repository_binding" in document:
        expected_subject["repository_binding"] = document["repository_binding"]
    if repository_binding is not None:
        expected_repository = _repository_binding(repository_binding, document["scope"])
        if expected_repository != document.get("repository_binding"):
            raise SessionRefError("repository binding mismatch")
    if evidence.get("subject") != _subject_in_schema_order(expected_subject):
        raise SessionRefError("evidence subject mismatch")


def _validator():
    global _VALIDATOR
    if _VALIDATOR is not None:
        return _VALIDATOR
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError as error:
        raise SessionRefError("SessionRefV1 schema validator is unavailable") from error

    try:
        catalog = _load_json(SCHEMA_DIR / "index.json")
        schemas = [_load_json(path) for path in SCHEMA_DIR.glob("*.schema.json")]
        resources = [(schema["$id"], Resource.from_contents(schema)) for schema in schemas]
        resources.append(
            (
                catalog["catalog_id"],
                Resource.from_contents(catalog, default_specification=DRAFT202012),
            )
        )
        registry = Registry(retrieve=_no_network).with_resources(resources)
        schema = registry.contents(SCHEMA_ID)
        Draft202012Validator.check_schema(schema)
        _VALIDATOR = Draft202012Validator(
            schema,
            registry=registry,
            format_checker=FormatChecker(),
        )
    except Exception as error:
        raise SessionRefError("SessionRefV1 schema validator failed to load") from error
    return _VALIDATOR


def _no_network(uri: str):
    raise LookupError(f"offline standalone schema registry has no resource for {uri}")


def _load_json(path: Path) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _closed_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionRefError(f"{label} must be a mapping")
    try:
        return deepcopy(dict(value))
    except (RecursionError, TypeError) as error:
        raise SessionRefError(f"{label} must be a finite mapping") from error


def _authority_dict(authority: SessionAuthority) -> dict[str, str]:
    if not isinstance(authority, SessionAuthority):
        raise SessionRefError("authority must be a SessionAuthority")
    return {
        "authority_kind": authority.authority_kind,
        "identity": authority.identity,
        "implementation_revision": authority.implementation_revision,
        "capability_profile_id": authority.capability_profile_id,
        "capability_profile_revision": authority.capability_profile_revision,
    }


def _repository_binding(binding: RepositoryBinding | None, scope: Mapping[str, Any]) -> dict[str, str] | None:
    if binding is None:
        return None
    if not isinstance(binding, RepositoryBinding):
        raise SessionRefError("repository_binding must be a RepositoryBinding")
    root = _real_directory(binding.repo_root, "repository root")
    cwd = _real_directory(binding.cwd, "canonical cwd")
    try:
        if os.path.commonpath([root, cwd]) != root:
            raise SessionRefError("canonical cwd must be under repository root")
    except ValueError as error:
        raise SessionRefError("canonical cwd must be under repository root") from error
    if scope.get("kind") == "project" and binding.project_id != scope.get("project_id"):
        raise SessionRefError("repository project_id mismatch")
    return {
        "project_id": binding.project_id,
        "repo_id": binding.repo_id,
        "canonical_cwd": cwd,
    }


def _real_directory(value: str | os.PathLike[str], label: str) -> str:
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise SessionRefError(f"{label} must be an absolute directory path") from error
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise SessionRefError(f"{label} must be an absolute directory path")
    if not os.path.isabs(raw):
        raise SessionRefError(f"{label} must be absolute")
    real = os.path.realpath(raw)
    if not os.path.isdir(real):
        raise SessionRefError(f"{label} must be an existing directory")
    return real


def _session_id_for(seed: Mapping[str, Any]) -> str:
    return "session_" + _digest(seed)[:32]


def _evidence_id_for(seed: Mapping[str, Any]) -> str:
    return "evidence_" + _digest({"session_ref": seed})[:32]


def _digest_without(value: Mapping[str, Any], field: str = "integrity") -> str:
    material = deepcopy(dict(value))
    material.pop(field, None)
    return "sha256:" + _digest(material)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    _assert_strict_json(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _assert_strict_json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and any(_is_forbidden_text(char) for char in value):
            raise SessionRefError("strict JSON string contains forbidden scalar")
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -9007199254740991 or value > 9007199254740991:
            raise SessionRefError("strict JSON integer is out of range")
        return
    if isinstance(value, float):
        raise SessionRefError("strict JSON forbids floats")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SessionRefError("strict JSON object keys must be strings")
            _assert_strict_json(key)
            _assert_strict_json(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_strict_json(item)
        return
    raise SessionRefError("strict JSON value is unsupported")


def _is_forbidden_text(char: str) -> bool:
    code = ord(char)
    return code <= 0x1F or code in {0x7F, 0x85, 0x2028, 0x2029} or 0xD800 <= code <= 0xDFFF


def _subject_in_schema_order(subject: Mapping[str, Any]) -> dict[str, Any]:
    ordered = {
        "endpoint_id": subject["endpoint_id"],
        "session_ref_id": subject["session_ref_id"],
        "native_session_id": subject["native_session_id"],
    }
    if "repository_binding" in subject:
        ordered["repository_binding"] = subject["repository_binding"]
    return ordered
