"""Inert canonical delivery and receipt fan-out helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from llm_collab.ledger.store import (
    CanonicalIntegrityError,
    LedgerStore,
    _derive_attempt_id,
    _derive_delivery_id,
    _derive_receipt_id,
)


def create_deliveries(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    routes: Iterable[tuple[str, str]],
    now_epoch_ms: int,
    created_at_utc: str,
) -> tuple[tuple[str, bool], ...]:
    return store.create_canonical_deliveries(
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        routes=routes,
        now_epoch_ms=now_epoch_ms,
        created_at_utc=created_at_utc,
    )


def create_attempt(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
    attempt_index: int,
    attempt_epoch_ms: int,
    created_at_utc: str,
) -> tuple[str, bool]:
    return store.create_canonical_delivery_attempt(
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_index=attempt_index,
        attempt_epoch_ms=attempt_epoch_ms,
        created_at_utc=created_at_utc,
    )


def append_receipt(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    evidence: Mapping[str, object],
    session_ref_id: str | None = None,
    created_at_utc: str,
) -> tuple[str, bool]:
    return store.append_canonical_delivery_receipt(
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


def project_delivery_v1(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
) -> dict[str, object]:
    delivery = store.read_canonical_delivery(
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
    )
    if delivery is None:
        raise KeyError(delivery_id)
    if delivery["attempt_id"] is None or delivery["evidence"] is None:
        raise CanonicalIntegrityError(
            "canonical delivery has no receipt-backed DeliveryV1 projection"
        )
    scope = {"kind": delivery["scope_kind"]}
    if delivery["scope_kind"] == "project":
        scope["project_id"] = delivery["scope_identity"]
    evidence = delivery["evidence"]
    projected = {
        "schema_version": 1,
        "workspace_id": delivery["workspace_id"],
        "scope": scope,
        "delivery_id": delivery["delivery_id"],
        "message_id": delivery["message_id"],
        "attempt_id": delivery["attempt_id"],
        "endpoint_id": delivery["endpoint_id"],
        "outcome": delivery["outcome"],
        "evidence": evidence,
    }
    if delivery["session_ref_id"] is not None:
        projected["session_ref_id"] = delivery["session_ref_id"]
    return projected


def project_receipt_v1(
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
    receipt = store.read_canonical_receipt(
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_id=attempt_id,
        receipt_id=receipt_id,
    )
    if receipt is None:
        raise KeyError(receipt_id)
    scope = {"kind": receipt["scope_kind"]}
    if receipt["scope_kind"] == "project":
        scope["project_id"] = receipt["scope_identity"]
    projected = {
        "schema_version": 1,
        "workspace_id": receipt["workspace_id"],
        "scope": scope,
        "receipt_id": receipt["receipt_id"],
        "message_id": receipt["message_id"],
        "delivery_id": receipt["delivery_id"],
        "attempt_id": receipt["attempt_id"],
        "endpoint_id": receipt["evidence"]["subject"]["endpoint_id"],  # type: ignore[index]
        "state": receipt["state"],
        "evidence": receipt["evidence"],
    }
    if receipt["session_ref_id"] is not None:
        projected["session_ref_id"] = receipt["session_ref_id"]
    return projected
