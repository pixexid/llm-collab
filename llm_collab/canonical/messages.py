"""Inert canonical message intent and content-addressed body storage."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from llm_collab.ledger.paths import validate_project_id, validate_workspace_id
from llm_collab.ledger.store import CanonicalConflictError, LedgerStore


_AGENT_ID = re.compile(r"agent_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{64}\Z")
_REGISTRY_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACK_POLICIES = frozenset({"none", "required"})
_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
_ARTIFACT_KINDS = frozenset({"chat", "task", "repo", "path", "branch", "worktree"})


class CanonicalIntegrityError(RuntimeError):
    pass


def _bounded_text(value: object, name: str, maximum: int) -> str:
    try:
        encoded_size = len(value.encode("utf-8")) if isinstance(value, str) else -1
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or encoded_size > maximum
    ):
        raise ValueError(f"{name} must be non-empty NUL-free text of at most {maximum} bytes")
    return value


def _optional_text(value: object, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, name, maximum)


def _utc_timestamp(value: object, name: str) -> str:
    timestamp = _bounded_text(value, name, 128)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc).isoformat()


def _agent_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _AGENT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a MessageV1 agent_ identifier")
    return value


def _message_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _MESSAGE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical msg_ identifier")
    return value


def _scope(workspace_id: object, scope_kind: object, scope_identity: object) -> tuple[str, str, str]:
    workspace = validate_workspace_id(workspace_id)  # type: ignore[arg-type]
    if scope_kind == "workspace":
        if scope_identity != "workspace":
            raise ValueError("workspace scope identity must be workspace")
        return workspace, "workspace", "workspace"
    if scope_kind == "project":
        return workspace, "project", validate_project_id(scope_identity)  # type: ignore[arg-type]
    raise ValueError("scope_kind must be workspace or project")


def _normalized_recipients(values: Iterable[object]) -> tuple[str, ...]:
    recipients = tuple(sorted({_agent_id(value, "recipient_agent_id") for value in values}))
    if not recipients or len(recipients) > 256:
        raise ValueError("recipients must contain between 1 and 256 distinct agents")
    return recipients


def _normalized_artifacts(values: Iterable[object]) -> tuple[tuple[str, str], ...]:
    artifacts = set()
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("each artifact must be an (artifact_kind, artifact_ref) pair")
        kind, reference = value
        if not isinstance(kind, str) or kind not in _ARTIFACT_KINDS:
            raise ValueError("artifact_kind is not in the closed vocabulary")
        # Artifact references are bounded data only; P2a never resolves or executes them.
        artifacts.add((kind, _bounded_text(reference, "artifact_ref", 4096)))
    if len(artifacts) > 256:
        raise ValueError("artifacts may contain at most 256 distinct references")
    return tuple(sorted(artifacts))


def _normalized_tags(values: Iterable[object]) -> tuple[str, ...]:
    tags = tuple(sorted({_bounded_text(value, "tag", 128) for value in values}))
    if len(tags) > 64:
        raise ValueError("tags may contain at most 64 distinct values")
    return tags


def _frame(value: str | None) -> bytes:
    if value is None:
        return b"\x00"
    encoded = value.encode("utf-8")
    return b"\x01" + len(encoded).to_bytes(8, "big") + encoded


def _sequence(values: Iterable[str]) -> bytes:
    items = tuple(values)
    return b"\x02" + len(items).to_bytes(8, "big") + b"".join(_frame(item) for item in items)


def _artifact_sequence(values: Iterable[tuple[str, str]]) -> bytes:
    items = tuple(values)
    return b"\x03" + len(items).to_bytes(8, "big") + b"".join(
        _frame(kind) + _frame(reference) for kind, reference in items
    )


def _derive_message_id(
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    sender_agent_id: str,
    dedupe_key: str,
    body_sha256: str,
    recipients: tuple[str, ...],
    reply_to_message_id: str | None,
    ttl_seconds: int,
    ack_policy: str,
    artifacts: tuple[tuple[str, str], ...],
    title: str,
    priority: str,
    tags: tuple[str, ...],
    chat_link: str | None,
    task_link: str | None,
) -> str:
    canonical_intent = b"".join(
        (
            _frame(workspace_id),
            _frame(scope_kind),
            _frame(scope_identity),
            _frame(sender_agent_id),
            _frame(dedupe_key),
            _frame(body_sha256),
            _sequence(recipients),
            _frame(reply_to_message_id),
            _frame(str(ttl_seconds)),
            _frame(ack_policy),
            _artifact_sequence(artifacts),
            _frame(title),
            _frame(priority),
            _sequence(tags),
            _frame(chat_link),
            _frame(task_link),
        )
    )
    return "msg_" + hashlib.sha256(canonical_intent).hexdigest()


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
    store.canonical_preflight(write=True)
    workspace, kind, identity = _scope(workspace_id, scope_kind, scope_identity)
    sender = _agent_id(sender_agent_id, "sender_agent_id")
    dedupe = _bounded_text(dedupe_key, "dedupe_key", 256)
    if not isinstance(body, bytes) or len(body) > 1048576:
        raise ValueError("body must be bytes of at most 1048576 bytes")
    normalized_recipients = _normalized_recipients(recipients)
    if not isinstance(registry_revision, str) or _REGISTRY_REVISION.fullmatch(registry_revision) is None:
        raise ValueError("registry_revision must be sha256:<lowercase hex>")
    created = _utc_timestamp(created_at_utc, "created_at_utc")
    normalized_title = _bounded_text(title, "title", 512)
    reply = None if reply_to_message_id is None else _message_id(
        reply_to_message_id, "reply_to_message_id"
    )
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 0 <= ttl_seconds <= 31536000:
        raise ValueError("ttl_seconds must be an integer between 0 and 31536000")
    if not isinstance(ack_policy, str) or ack_policy not in _ACK_POLICIES:
        raise ValueError("ack_policy is not in the closed vocabulary")
    normalized_artifacts = _normalized_artifacts(artifacts)
    if not isinstance(priority, str) or priority not in _PRIORITIES:
        raise ValueError("priority is not in the closed vocabulary")
    normalized_tags = _normalized_tags(tags)
    normalized_chat = _optional_text(chat_link, "chat_link", 256)
    normalized_task = _optional_text(task_link, "task_link", 256)
    body_sha256 = hashlib.sha256(body).hexdigest()
    message_id = _derive_message_id(
        workspace_id=workspace,
        scope_kind=kind,
        scope_identity=identity,
        sender_agent_id=sender,
        dedupe_key=dedupe,
        body_sha256=body_sha256,
        recipients=normalized_recipients,
        reply_to_message_id=reply,
        ttl_seconds=ttl_seconds,
        ack_policy=ack_policy,
        artifacts=normalized_artifacts,
        title=normalized_title,
        priority=priority,
        tags=normalized_tags,
        chat_link=normalized_chat,
        task_link=normalized_task,
    )
    message = {
        "workspace_id": workspace,
        "scope_kind": kind,
        "scope_identity": identity,
        "message_id": message_id,
        "sender_agent_id": sender,
        "dedupe_key": dedupe,
        "body_sha256": body_sha256,
        "reply_to_message_id": reply,
        "ttl_seconds": ttl_seconds,
        "ack_policy": ack_policy,
        "title": normalized_title,
        "priority": priority,
        "chat_link": normalized_chat,
        "task_link": normalized_task,
        "registry_revision": registry_revision,
        "project_id": identity if kind == "project" else None,
        "created_at_utc": created,
    }
    was_created = store.create_canonical_message(
        message=message,
        recipients=normalized_recipients,
        artifacts=normalized_artifacts,
        tags=normalized_tags,
        body=body,
    )
    return message_id, was_created


def read_message(
    store: LedgerStore,
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
) -> dict[str, object]:
    """Read one exact-scoped message and verify its body bytes every time."""
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
    body = message["body"]
    body_sha256 = message["body_sha256"]
    byte_size = message["byte_size"]
    if (
        not isinstance(body, bytes)
        or len(body) != byte_size
        or hashlib.sha256(body).hexdigest() != body_sha256
    ):
        raise CanonicalIntegrityError("canonical body failed size or SHA-256 verification")
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
