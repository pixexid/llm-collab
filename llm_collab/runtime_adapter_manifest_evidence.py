"""Deterministic manifest launch-security evidence for Runtime Adapter JSON-RPC V1.

This module is intentionally separate from ``runtime_adapter_claim`` and from
wire/transport/admission evidence. It exercises the real trusted-manifest
resolver and statically audits the real stdio supervisor spawn boundary.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from llm_collab import runtime_adapter_supervisor
from llm_collab.runtime_adapter_conformance import extract_clause_occurrences
from llm_collab.runtime_adapter_manifest import (
    ManifestResolutionError,
    ResolvedAdapter,
    TrustedManifestRegistry,
    UNTRUSTED_MANIFEST_INPUT,
    validate_initialized_identity,
)


ARTIFACT_LABEL = "manifest_launch_security"
MANIFEST_SECURITY_EVIDENCED = "manifest_security_evidenced"
_ADAPTER_ID = "adapter_a"


class ManifestEvidenceFailure(AssertionError):
    """Raised when manifest launch-security evidence cannot be built honestly."""


@dataclass(frozen=True)
class ManifestClauseRef:
    clause_key: str
    text_sha256: str


@dataclass(frozen=True)
class SupervisorSpawnObservation:
    popen_call_count: int
    shell_keyword_false: bool
    args_are_resolved_argv: bool
    shell_true_count: int
    forbidden_spawn_helpers: tuple[str, ...]


@dataclass(frozen=True)
class ManifestObservation:
    resolved_adapter_id: str
    resolved_adapter_revision: str
    resolved_manifest_id: str
    resolved_manifest_revision: str
    resolved_executable: str
    resolved_argv: tuple[str, ...]
    resolved_working_directory: str
    resolved_environment: Mapping[str, str]
    unknown_adapter_fault: str
    rejected_field_faults: Mapping[str, str]
    rejection_outputs_resolved_adapter: bool
    initialized_identity_valid: bool
    initialized_identity_mismatch_faults: Mapping[str, str]
    initialized_identity_mismatch_mutated_source: bool
    supervisor_spawn: SupervisorSpawnObservation


_MANIFEST_REFS: tuple[ManifestClauseRef, ...] = (
    ManifestClauseRef(
        "Cc15ecf086a5b.1",
        "c15ecf086a5bbfed48f31cc4342c46423e98301ebf174cc27c84a2201664ef45",
    ),
    ManifestClauseRef(
        "Cc02b8dfb1bfa.1",
        "c02b8dfb1bfa773454c38e8ed092567a9f82c3e46bed7433864107afdb5099c6",
    ),
    ManifestClauseRef(
        "Cc02b8dfb1bfa.2",
        "c02b8dfb1bfa773454c38e8ed092567a9f82c3e46bed7433864107afdb5099c6",
    ),
    ManifestClauseRef(
        "Ca183987f3efe.1",
        "a183987f3efe736c00a8ebc2a2b089fde5504479cea407b896b84a52cd51d241",
    ),
    ManifestClauseRef(
        "Ca183987f3efe.2",
        "a183987f3efe736c00a8ebc2a2b089fde5504479cea407b896b84a52cd51d241",
    ),
    ManifestClauseRef(
        "C4c2db37e63d2.1",
        "4c2db37e63d2c71f9bc6f2123d64741fece054c235018c50cbc6b465dbf687df",
    ),
    ManifestClauseRef(
        "C4c2db37e63d2.2",
        "4c2db37e63d2c71f9bc6f2123d64741fece054c235018c50cbc6b465dbf687df",
    ),
    ManifestClauseRef(
        "C4c2db37e63d2.3",
        "4c2db37e63d2c71f9bc6f2123d64741fece054c235018c50cbc6b465dbf687df",
    ),
)


def build_manifest_evidence(protocol_text: str) -> Mapping[str, object]:
    """Return deterministic C05 manifest launch-security evidence."""

    _validate_clause_refs(protocol_text)
    observation = _manifest_observation()
    _validate_observation(observation)
    return {
        "schema_version": 1,
        "protocol": "runtime-adapter-jsonrpc-v1",
        "artifact_label": ARTIFACT_LABEL,
        "evidence_kind": "manifest_launch_security",
        "claim": MANIFEST_SECURITY_EVIDENCED,
        "clauses": tuple(
            {
                "clause_key": ref.clause_key,
                "text_sha256": ref.text_sha256,
                "state": MANIFEST_SECURITY_EVIDENCED,
                "evidence": ARTIFACT_LABEL,
            }
            for ref in _MANIFEST_REFS
        ),
        "observation": {
            "resolved_adapter_id": observation.resolved_adapter_id,
            "resolved_adapter_revision": observation.resolved_adapter_revision,
            "resolved_manifest_id": observation.resolved_manifest_id,
            "resolved_manifest_revision": observation.resolved_manifest_revision,
            "resolved_executable": observation.resolved_executable,
            "resolved_argv": observation.resolved_argv,
            "resolved_working_directory": observation.resolved_working_directory,
            "resolved_environment": dict(observation.resolved_environment),
            "unknown_adapter_fault": observation.unknown_adapter_fault,
            "rejected_field_faults": dict(observation.rejected_field_faults),
            "rejection_outputs_resolved_adapter": observation.rejection_outputs_resolved_adapter,
            "initialized_identity_valid": observation.initialized_identity_valid,
            "initialized_identity_mismatch_faults": dict(observation.initialized_identity_mismatch_faults),
            "initialized_identity_mismatch_mutated_source": observation.initialized_identity_mismatch_mutated_source,
            "supervisor_spawn": {
                "popen_call_count": observation.supervisor_spawn.popen_call_count,
                "shell_keyword_false": observation.supervisor_spawn.shell_keyword_false,
                "args_are_resolved_argv": observation.supervisor_spawn.args_are_resolved_argv,
                "shell_true_count": observation.supervisor_spawn.shell_true_count,
                "forbidden_spawn_helpers": observation.supervisor_spawn.forbidden_spawn_helpers,
            },
        },
    }


def _validate_clause_refs(protocol_text: str) -> None:
    live = {clause.clause_key: clause for clause in extract_clause_occurrences(protocol_text)}
    for ref in _MANIFEST_REFS:
        clause = live.get(ref.clause_key)
        if clause is None:
            raise ManifestEvidenceFailure(f"missing manifest clause: {ref.clause_key}")
        if clause.text_sha256 != ref.text_sha256:
            raise ManifestEvidenceFailure(f"stale manifest clause: {ref.clause_key}")


def _manifest_observation() -> ManifestObservation:
    resolved = TrustedManifestRegistry(_trusted_manifest()).resolve(_ADAPTER_ID)
    unknown_adapter_fault = _rejection_fault(lambda: TrustedManifestRegistry(_trusted_manifest()).resolve("adapter_missing"))
    rejected_field_faults = {name: _rejection_fault(probe) for name, probe in _rejection_probes().items()}
    initialized_identity_valid = _valid_initialized_identity_passes(resolved)
    mismatch_faults, mismatch_mutated_source = _initialized_identity_mismatch_faults(resolved)
    return ManifestObservation(
        resolved_adapter_id=resolved.adapter_id,
        resolved_adapter_revision=resolved.adapter_revision,
        resolved_manifest_id=resolved.manifest_id,
        resolved_manifest_revision=resolved.manifest_revision,
        resolved_executable=resolved.executable,
        resolved_argv=resolved.argv,
        resolved_working_directory=resolved.working_directory,
        resolved_environment=dict(resolved.environment),
        unknown_adapter_fault=unknown_adapter_fault,
        rejected_field_faults=rejected_field_faults,
        rejection_outputs_resolved_adapter=False,
        initialized_identity_valid=initialized_identity_valid,
        initialized_identity_mismatch_faults=mismatch_faults,
        initialized_identity_mismatch_mutated_source=mismatch_mutated_source,
        supervisor_spawn=_supervisor_spawn_observation(),
    )


def _trusted_manifest() -> dict[str, dict[str, Any]]:
    return {
        _ADAPTER_ID: {
            "adapter_id": _ADAPTER_ID,
            "adapter_revision": "rev_1",
            "manifest_id": "manifest_a",
            "manifest_revision": "manifest_rev_1",
            "endpoint": {
                "endpoint_id": "endpoint_a",
                "adapter_name": _ADAPTER_ID,
                "adapter_revision": "rev_1",
            },
            "executable": "/trusted/bin/adapter-a",
            "argv": ["adapter-a", "--stdio"],
            "working_directory": "/trusted/work",
            "environment": {"SAFE": "1"},
            "environment_allowlist": ["SAFE"],
        }
    }


def _rejection_probes() -> Mapping[str, Callable[[], ResolvedAdapter]]:
    return {
        "executable": lambda: _resolve_changed(lambda manifest: manifest[_ADAPTER_ID].__setitem__("executable", "")),
        "argv": lambda: _resolve_changed(lambda manifest: manifest[_ADAPTER_ID].__setitem__("argv", [])),
        "working_directory": lambda: _resolve_changed(
            lambda manifest: manifest[_ADAPTER_ID].__setitem__("working_directory", "")
        ),
        "environment_outside_allowlist": lambda: _resolve_changed(
            lambda manifest: manifest[_ADAPTER_ID]["environment"].__setitem__("SECRET", "1")
        ),
        "shell": lambda: _resolve_changed(lambda manifest: manifest[_ADAPTER_ID].__setitem__("shell", "/bin/sh")),
        "manifest_path": lambda: _resolve_changed(
            lambda manifest: manifest[_ADAPTER_ID].__setitem__("manifest_path", "/tmp/manifest.json")
        ),
        "adapter_alias": lambda: _resolve_changed(lambda manifest: manifest[_ADAPTER_ID].__setitem__("adapter_alias", "alias")),
    }


def _resolve_changed(mutate: Callable[[dict[str, dict[str, Any]]], None]) -> ResolvedAdapter:
    manifest = copy.deepcopy(_trusted_manifest())
    mutate(manifest)
    return TrustedManifestRegistry(manifest).resolve(_ADAPTER_ID)


def _rejection_fault(operation: Callable[[], object]) -> str:
    try:
        resolved = operation()
    except ManifestResolutionError as error:
        if error.code != UNTRUSTED_MANIFEST_INPUT:
            raise ManifestEvidenceFailure("manifest rejection used the wrong fault code") from error
        return error.code
    raise ManifestEvidenceFailure(f"manifest rejection produced a ResolvedAdapter: {resolved!r}")


def _matching_initialized_identity() -> dict[str, Any]:
    manifest = _trusted_manifest()[_ADAPTER_ID]
    return {
        "adapter_id": manifest["adapter_id"],
        "adapter_revision": manifest["adapter_revision"],
        "manifest_id": manifest["manifest_id"],
        "manifest_revision": manifest["manifest_revision"],
        "endpoint": copy.deepcopy(manifest["endpoint"]),
    }


def _valid_initialized_identity_passes(resolved: ResolvedAdapter) -> bool:
    validate_initialized_identity(resolved, _matching_initialized_identity())
    return True


def _initialized_identity_mismatch_faults(resolved: ResolvedAdapter) -> tuple[Mapping[str, str], bool]:
    faults: dict[str, str] = {}
    mutated_source = False
    for name, mutate in _initialized_identity_mismatch_mutations().items():
        initialized = _matching_initialized_identity()
        mutate(initialized)
        before = copy.deepcopy(initialized)
        faults[name] = _rejection_fault(lambda initialized=initialized: validate_initialized_identity(resolved, initialized))
        if initialized != before:
            mutated_source = True
    return faults, mutated_source


def _initialized_identity_mismatch_mutations() -> Mapping[str, Callable[[dict[str, Any]], None]]:
    return {
        "adapter_id": lambda initialized: initialized.__setitem__("adapter_id", "adapter_alias"),
        "adapter_revision": lambda initialized: initialized.__setitem__("adapter_revision", "rev_2"),
        "manifest_id": lambda initialized: initialized.__setitem__("manifest_id", "manifest_b"),
        "manifest_revision": lambda initialized: initialized.__setitem__("manifest_revision", "manifest_rev_2"),
        "endpoint.adapter_name": lambda initialized: initialized["endpoint"].__setitem__("adapter_name", "adapter_alias"),
        "endpoint.adapter_revision": lambda initialized: initialized["endpoint"].__setitem__("adapter_revision", "rev_2"),
    }


def _supervisor_spawn_observation() -> SupervisorSpawnObservation:
    tree = ast.parse(_supervisor_source_path().read_text(encoding="utf-8"))
    popen_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "Popen"
    ]
    shell_true_count = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword)
        and node.arg == "shell"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    )
    forbidden_spawn_helpers = tuple(sorted(_forbidden_spawn_helpers(tree)))
    if len(popen_calls) != 1:
        raise ManifestEvidenceFailure("supervisor must contain exactly one subprocess.Popen call")
    call = popen_calls[0]
    shell_keywords = [kw for kw in call.keywords if kw.arg == "shell"]
    shell_keyword_false = (
        len(shell_keywords) == 1
        and isinstance(shell_keywords[0].value, ast.Constant)
        and shell_keywords[0].value.value is False
    )
    args_are_resolved_argv = _is_self_resolved_argv(call.args[0]) if call.args else False
    return SupervisorSpawnObservation(
        popen_call_count=len(popen_calls),
        shell_keyword_false=shell_keyword_false,
        args_are_resolved_argv=args_are_resolved_argv,
        shell_true_count=shell_true_count,
        forbidden_spawn_helpers=forbidden_spawn_helpers,
    )


def _supervisor_source_path() -> Path:
    module_file = runtime_adapter_supervisor.__file__
    if module_file is None:
        raise ManifestEvidenceFailure("runtime_adapter_supervisor has no source path")
    return Path(module_file)


def _is_self_resolved_argv(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "argv"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_resolved"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


def _forbidden_spawn_helpers(tree: ast.AST) -> set[str]:
    forbidden: set[str] = set()
    helper_names = {"run", "call", "check_call", "check_output"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "subprocess" and func.attr in helper_names:
                    forbidden.add(f"subprocess.{func.attr}")
                if func.value.id == "os" and func.attr in {"system", "popen"}:
                    forbidden.add(f"os.{func.attr}")
            if isinstance(func, ast.Name) and func.id in {"system", "popen"}:
                forbidden.add(func.id)
    return forbidden


def _validate_observation(observation: ManifestObservation) -> None:
    expected = _trusted_manifest()[_ADAPTER_ID]
    if observation.resolved_adapter_id != _ADAPTER_ID:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong adapter")
    if observation.resolved_adapter_revision != expected["adapter_revision"]:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong adapter revision")
    if observation.resolved_manifest_id != expected["manifest_id"]:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong manifest id")
    if observation.resolved_manifest_revision != expected["manifest_revision"]:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong manifest revision")
    if observation.resolved_executable != expected["executable"]:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong executable")
    if observation.resolved_argv != tuple(expected["argv"]):
        raise ManifestEvidenceFailure("valid manifest resolved the wrong argv")
    if observation.resolved_working_directory != expected["working_directory"]:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong working directory")
    if dict(observation.resolved_environment) != expected["environment"]:
        raise ManifestEvidenceFailure("valid manifest resolved the wrong environment")
    if observation.unknown_adapter_fault != UNTRUSTED_MANIFEST_INPUT:
        raise ManifestEvidenceFailure("unknown adapter id did not fail closed")
    if set(observation.rejected_field_faults) != set(_rejection_probes()):
        raise ManifestEvidenceFailure("manifest field-class rejection coverage drifted")
    if any(fault != UNTRUSTED_MANIFEST_INPUT for fault in observation.rejected_field_faults.values()):
        raise ManifestEvidenceFailure("manifest field-class rejection used the wrong fault")
    if observation.rejection_outputs_resolved_adapter:
        raise ManifestEvidenceFailure("manifest rejection produced a resolved adapter")
    if not observation.initialized_identity_valid:
        raise ManifestEvidenceFailure("matching initialized identity did not validate")
    if set(observation.initialized_identity_mismatch_faults) != set(_initialized_identity_mismatch_mutations()):
        raise ManifestEvidenceFailure("initialized identity mismatch coverage drifted")
    if any(fault != UNTRUSTED_MANIFEST_INPUT for fault in observation.initialized_identity_mismatch_faults.values()):
        raise ManifestEvidenceFailure("initialized identity mismatch used the wrong fault")
    if observation.initialized_identity_mismatch_mutated_source:
        raise ManifestEvidenceFailure("initialized identity mismatch validation mutated the source")
    spawn = observation.supervisor_spawn
    if spawn.popen_call_count != 1:
        raise ManifestEvidenceFailure("supervisor Popen count drifted")
    if not spawn.shell_keyword_false:
        raise ManifestEvidenceFailure("supervisor Popen must pass shell=False")
    if not spawn.args_are_resolved_argv:
        raise ManifestEvidenceFailure("supervisor Popen must execute the resolved argv sequence")
    if spawn.shell_true_count:
        raise ManifestEvidenceFailure("supervisor contains shell=True")
    if spawn.forbidden_spawn_helpers:
        raise ManifestEvidenceFailure("supervisor contains alternate shell/spawn helpers")
