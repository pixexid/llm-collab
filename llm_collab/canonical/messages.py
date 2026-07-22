"""Inert canonical message intent and content-addressed body storage."""

from __future__ import annotations

from collections.abc import Iterable

from llm_collab.ledger.store import (
    CanonicalIntegrityError,
    LedgerStore,
    _canonical_message_id as _message_id,
    _canonical_scope as _scope,
    _derive_message_id,
)


def create_or_return_equivalent(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    sender_agent_id: str,
    dedupe_key: str,
    body: bytes,
    recipients: Iterable[str],
    registry_revision: str,
    created_at_utc: str,
    title: str,
    reply_to_message_id: str | None = None,
    ttl_seconds: int = 0,
    ack_policy: str = "none",
    artifacts: Iterable[tuple[str, str]] = (),
    priority: str = "normal",
    tags: Iterable[str] = (),
    chat_link: str | None = None,
    task_link: str | None = None,
) -> tuple[str, bool]:
    """Create one immutable intent atomically, or return its exact equivalent."""
    return store.create_canonical_message(
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        sender_agent_id=sender_agent_id,
        dedupe_key=dedupe_key,
        body=body,
        recipients=recipients,
        registry_revision=registry_revision,
        created_at_utc=created_at_utc,
        title=title,
        reply_to_message_id=reply_to_message_id,
        ttl_seconds=ttl_seconds,
        ack_policy=ack_policy,
        artifacts=artifacts,
        priority=priority,
        tags=tags,
        chat_link=chat_link,
        task_link=task_link,
    )


def read_message(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
) -> dict[str, object]:
    """Read one exact-scoped message through the store's integrity checks."""
    workspace, kind, identity = _scope(workspace_id, scope_kind, scope_identity)
    identifier = _message_id(message_id, "message_id")
    message = store.read_canonical_message(
        workspace_id=workspace,
        scope_kind=kind,
        scope_identity=identity,
        message_id=identifier,
    )
    if message is None:
        raise KeyError(identifier)
    body_sha256 = message["body_sha256"]
    message["body_ref"] = "body_" + str(body_sha256)
    return message


def project_message_v1(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
) -> dict[str, object]:
    """Project only the required frozen MessageV1 members."""
    message = read_message(
        store,
        workspace_id=workspace_id,
        scope_kind=scope_kind,
        scope_identity=scope_identity,
        message_id=message_id,
    )
    scope = {"kind": message["scope_kind"]}
    if message["scope_kind"] == "project":
        scope["project_id"] = message["scope_identity"]
    return {
        "schema_version": 1,
        "workspace_id": message["workspace_id"],
        "scope": scope,
        "message_id": message["message_id"],
        "body_ref": message["body_ref"],
        "recipients": list(message["recipients"]),
    }
