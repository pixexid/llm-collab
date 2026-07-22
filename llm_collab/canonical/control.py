"""Gated canonical control surfaces.

These helpers are library-only control surfaces.  They do not make canonical
rows current authority: every append path requires an exact project declaration,
an explicit environment gate, and an explicit per-call opt-in.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from llm_collab.compatibility.projection import (
    UNAUTHENTICATED_PROVENANCE,
    project_legacy_manifest_provenance_v2,
)
from llm_collab.ledger.store import (
    CanonicalIntegrityError,
    LedgerStore,
    _canonical_registry_revision,
    _canonical_scope,
)

from .delivery import append_receipt, project_delivery_v1, project_receipt_v1


CANONICAL_CONTROL_ENV = "LLM_COLLAB_CANONICAL_CONTROL"
CANONICAL_CONTROL_ENABLED = "enabled"
DEAD_LETTER_STATES = frozenset(
    {"ambiguous", "rejected_before_acceptance", "deferred_busy", "pull_pending"}
)
ACKNOWLEDGMENT_STATES = frozenset({"accepted", "completed"})


class CanonicalControlError(PermissionError):
    """Raised when a canonical control write is not explicitly admitted."""


def _canonical_writes_declared(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    registry_revision: str,
) -> bool:
    if scope_kind != "project":
        return False
    try:
        snapshot = store.get_project_snapshot(
            workspace_id=workspace_id,
            project_id=scope_identity,
            registry_revision=registry_revision,
        )
    except ValueError:
        return False
    if snapshot is None:
        return False
    try:
        payload = json.loads(snapshot["snapshot_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("canonical_writes") is True


def require_canonical_write_gate(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    registry_revision: str,
    allow_canonical_write: bool,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Fail closed unless all canonical-write control conjuncts are exact."""
    workspace, kind, identity = _canonical_scope(workspace_id, scope_kind, scope_identity)
    revision = _canonical_registry_revision(registry_revision, "registry_revision")
    environment = os.environ if environ is None else environ

    declaration_enabled = _canonical_writes_declared(
        store,
        workspace_id=workspace,
        scope_kind=kind,
        scope_identity=identity,
        registry_revision=revision,
    )
    if not declaration_enabled:
        raise CanonicalControlError("canonical write declaration is not enabled")

    environment_enabled = environment.get(CANONICAL_CONTROL_ENV) == CANONICAL_CONTROL_ENABLED
    if not environment_enabled:
        raise CanonicalControlError("canonical write environment gate is not enabled")

    call_enabled = allow_canonical_write is True
    if not call_enabled:
        raise CanonicalControlError("canonical write call opt-in is not enabled")


def _append_gated_receipt(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    registry_revision: str,
    allow_canonical_write: bool,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    evidence: Mapping[str, object],
    session_ref_id: str | None,
    created_at_utc: str,
    environ: Mapping[str, str] | None,
) -> tuple[str, bool]:
    require_canonical_write_gate(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        registry_revision=registry_revision,
        allow_canonical_write=allow_canonical_write,
        environ=environ,
    )
    return append_receipt(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        evidence=evidence,
        session_ref_id=session_ref_id,
        created_at_utc=created_at_utc,
    )


def append_acknowledgment_receipt(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    registry_revision: str,
    allow_canonical_write: bool,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    evidence: Mapping[str, object],
    session_ref_id: str,
    created_at_utc: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """Append an authoritative terminal acknowledgment receipt through the gate."""
    if evidence.get("state") not in ACKNOWLEDGMENT_STATES:
        raise CanonicalIntegrityError("acknowledgment receipt state is not terminal")
    return _append_gated_receipt(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        registry_revision=registry_revision,
        allow_canonical_write=allow_canonical_write,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        evidence=evidence,
        session_ref_id=session_ref_id,
        created_at_utc=created_at_utc,
        environ=environ,
    )


def append_dead_letter_receipt(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    registry_revision: str,
    allow_canonical_write: bool,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    evidence: Mapping[str, object],
    created_at_utc: str,
    session_ref_id: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, bool]:
    """Append a reconciliation/dead-letter receipt using the existing vocabulary."""
    if evidence.get("state") not in DEAD_LETTER_STATES:
        raise CanonicalIntegrityError("dead-letter receipt state is not in the closed vocabulary")
    return _append_gated_receipt(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        registry_revision=registry_revision,
        allow_canonical_write=allow_canonical_write,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        evidence=evidence,
        session_ref_id=session_ref_id,
        created_at_utc=created_at_utc,
        environ=environ,
    )


def inspect_delivery(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
) -> dict[str, object]:
    """Return a read-only delivery inspection view through receipt integrity checks."""
    return project_delivery_v1(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
    )


def inspect_receipt(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    receipt_id: str,
) -> dict[str, object]:
    """Return a read-only receipt inspection view through evidence integrity checks."""
    return project_receipt_v1(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        receipt_id=receipt_id,
    )


def inspect_legacy_manifest_provenance(
    store: LedgerStore,
    *,
    workspace_id: str,
    manifest_id: str,
) -> dict[str, object]:
    """Return labelled v6 provenance without authenticating caller-asserted identity."""
    projection = project_legacy_manifest_provenance_v2(
        store,
        workspace_id=workspace_id,
        manifest_id=manifest_id,
    )
    label = projection.get("publication", {}).get("provenance_label")  # type: ignore[union-attr]
    if label != UNAUTHENTICATED_PROVENANCE:
        raise CanonicalIntegrityError("legacy manifest provenance label is missing")
    return {
        "projection_kind": "canonical_control_legacy_manifest_provenance_v1",
        "authority": "read_only_inspection",
        "provenance_label": dict(UNAUTHENTICATED_PROVENANCE),
        "manifest_provenance": projection,
    }
