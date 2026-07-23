"""Inert normalized Codex session observation."""

from __future__ import annotations

import os
from typing import Any, Mapping

from llm_collab.codex_runtime_home import RuntimeHomeIdentity
from llm_collab.codex_session_ref import RepositoryBinding, SessionAuthority, build_session_ref


CLOSED_OBSERVATION_STATES = frozenset(("idle", "busy", "awaiting_approval", "error", "background_terminal"))
RENDERER_EVIDENCE_KEYS = frozenset(("display_title", "latest", "renderer_visible", "sidebar_index", "window_title"))
OBSERVED_FACT_KEYS = frozenset(
    (
        "host_session_id",
        "endpoint_id",
        "project_id",
        "canonical_cwd",
        "authority_identity",
        "implementation_revision",
        "capability_profile_id",
        "capability_profile_revision",
    )
)


class CodexSessionObservationError(ValueError):
    """Raised when a normalized Codex session observation is not authoritative."""


def build_observed_session_ref(
    *,
    workspace_id: str,
    scope: Mapping[str, Any],
    endpoint_id: str,
    native_session_id: str,
    runtime_home: RuntimeHomeIdentity,
    authority: SessionAuthority,
    observed_at_utc: str,
    correlation_id: str,
    observation_state: str,
    repository_binding: RepositoryBinding | None = None,
    observed: Mapping[str, Any] | None = None,
    expected_session_ref_id: str | None = None,
    expected_evidence_integrity: str | None = None,
) -> Mapping[str, Any]:
    if observation_state not in CLOSED_OBSERVATION_STATES:
        raise CodexSessionObservationError("unknown observation state")
    if observed is not None:
        _validate_observed_facts(
            observed,
            scope=scope,
            endpoint_id=endpoint_id,
            native_session_id=native_session_id,
            authority=authority,
            repository_binding=repository_binding,
        )

    return build_session_ref(
        workspace_id=workspace_id,
        scope=scope,
        endpoint_id=endpoint_id,
        native_session_id=native_session_id,
        runtime_home=runtime_home,
        authority=authority,
        observed_at_utc=observed_at_utc,
        correlation_id=correlation_id,
        repository_binding=repository_binding,
        expected_session_ref_id=expected_session_ref_id,
        expected_evidence_integrity=expected_evidence_integrity,
    )


def _validate_observed_facts(
    observed: Mapping[str, Any],
    *,
    scope: Mapping[str, Any],
    endpoint_id: str,
    native_session_id: str,
    authority: SessionAuthority,
    repository_binding: RepositoryBinding | None,
) -> None:
    if not isinstance(observed, Mapping):
        raise CodexSessionObservationError("observed facts must be a mapping")
    keys = set(observed)
    if RENDERER_EVIDENCE_KEYS & keys:
        raise CodexSessionObservationError("renderer evidence is not session authority")
    unknown = keys - OBSERVED_FACT_KEYS
    if unknown:
        raise CodexSessionObservationError("unknown observed fact")
    expected = {
        "host_session_id": native_session_id,
        "endpoint_id": endpoint_id,
        "authority_identity": authority.identity,
        "implementation_revision": authority.implementation_revision,
        "capability_profile_id": authority.capability_profile_id,
        "capability_profile_revision": authority.capability_profile_revision,
    }
    if scope.get("kind") == "project":
        expected["project_id"] = scope.get("project_id")
    if repository_binding is not None:
        expected["canonical_cwd"] = os.path.realpath(repository_binding.cwd)
    for key, value in expected.items():
        if key in observed and observed[key] != value:
            raise CodexSessionObservationError(f"{key} mismatch")
