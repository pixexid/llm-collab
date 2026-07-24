"""Lazy materialization for one selected legacy Chats packet."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path

from llm_collab.canonical.delivery import create_bound_attempt, create_deliveries
from llm_collab.canonical.messages import create_or_return_equivalent
from llm_collab.ledger.store import (
    CONVERSATION_BINDING_RESOLUTION_REASONS,
    LedgerStore,
    _parse_legacy_frontmatter,
    _string_list,
)


ROUTE_AMBIGUOUS = "route_ambiguous"


@dataclass(frozen=True)
class LegacyPacketMaterializationRefused(Exception):
    reason: str
    detail: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}"


def materialize_selected_legacy_packet(
    store: LedgerStore,
    *,
    workspace_root: Path,
    session: Mapping[str, object],
    message: Mapping[str, object],
) -> dict[str, object]:
    """Append canonical rows for one already-selected legacy packet."""
    relpath, packet = _selected_packet(workspace_root, message)
    packet_sha256 = hashlib.sha256(packet).hexdigest()
    frontmatter, _body = _parse_legacy_frontmatter(packet)
    project_id = _required_text(frontmatter.get("project_id"), "project_id")
    chat_id = _required_text(frontmatter.get("chat_id"), "chat_id")
    agent_id = _required_text(session.get("agent_id"), "session.agent_id")
    sender = _required_text(
        frontmatter.get("sender_agent_id", frontmatter.get("from")),
        "sender_agent_id",
    )
    recipient = _required_text(frontmatter.get("to"), "to")
    if project_id != session.get("project_id") or chat_id != session.get("chat_id"):
        _refuse("packet project/chat does not match selected session")
    if recipient != agent_id:
        _refuse("packet recipient does not match selected session")
    _require_target_session(session, frontmatter)
    target_binding_id, target_generation = _require_binding(session, frontmatter)
    _require_repo_scope(session, frontmatter)

    registry_revision = _latest_project_registry_revision(
        store,
        workspace_id=store.paths.workspace_id,
        project_id=project_id,
    )
    sent_utc = _required_text(frontmatter.get("sent_utc"), "sent_utc")
    sent_epoch_ms = _epoch_ms(sent_utc)
    title = _required_text(frontmatter.get("title"), "title")
    priority = _required_text(frontmatter.get("priority"), "priority")
    related_task = frontmatter.get("related_task")
    task_link = related_task if isinstance(related_task, str) and related_task else None
    repo_targets = _string_list(frontmatter.get("repo_targets"), "repo_targets")
    path_targets = _string_list(frontmatter.get("path_targets"), "path_targets")
    tags = (*_string_list(frontmatter.get("tags"), "tags"), "legacy_packet_materialized")
    artifacts = [
        ("chat", chat_id),
        ("path", relpath),
        *(("repo", repo) for repo in repo_targets),
        *(("path", path) for path in path_targets),
    ]
    if task_link is not None:
        artifacts.append(("task", task_link))

    participant_id = "participant_" + agent_id
    resolved = store.resolve_conversation_binding(
        workspace_id=store.paths.workspace_id,
        scope_kind="project",
        scope_identity=project_id,
        conversation_id=chat_id,
        participant_id=participant_id,
        expected_binding_id=target_binding_id,
        expected_generation=target_generation,
    )
    if not resolved["resolved"]:
        return _unresolved(resolved.get("reason"))
    endpoint_id = _required_text(resolved.get("endpoint_id"), "endpoint_id")
    session_endpoint_id = session.get("endpoint_id")
    if session_endpoint_id is not None and session_endpoint_id != endpoint_id:
        _refuse("selected session endpoint does not match resolved binding")

    message_id, message_created = create_or_return_equivalent(
        store,
        workspace_id=store.paths.workspace_id,
        scope_kind="project",
        scope_identity=project_id,
        sender_agent_id="agent_" + sender,
        dedupe_key=_dedupe_key(relpath, packet_sha256),
        body=packet,
        recipients=("agent_" + recipient,),
        registry_revision=registry_revision,
        created_at_utc=sent_utc,
        title=title,
        ttl_seconds=0,
        ack_policy="none",
        priority=priority,
        tags=tags,
        chat_link=chat_id,
        task_link=task_link,
        artifacts=artifacts,
    )
    ((delivery_id, delivery_created),) = create_deliveries(
        store,
        workspace_id=store.paths.workspace_id,
        scope_kind="project",
        scope_identity=project_id,
        message_id=message_id,
        routes=(("agent_" + recipient, endpoint_id),),
        now_epoch_ms=sent_epoch_ms,
        created_at_utc=sent_utc,
    )
    attempt = create_bound_attempt(
        store,
        workspace_id=store.paths.workspace_id,
        scope_kind="project",
        scope_identity=project_id,
        message_id=message_id,
        delivery_id=delivery_id,
        attempt_index=0,
        attempt_epoch_ms=sent_epoch_ms,
        created_at_utc=sent_utc,
        conversation_id=chat_id,
        participant_id=participant_id,
    )
    return {
        **attempt,
        "message_id": message_id,
        "message_created": message_created,
        "delivery_id": delivery_id,
        "delivery_created": delivery_created,
        "packet_sha256": packet_sha256,
        "packet_relpath": relpath,
    }


def _selected_packet(workspace_root: Path, message: Mapping[str, object]) -> tuple[str, bytes]:
    raw_path = _required_text(message.get("path"), "message.path")
    rel = Path(raw_path)
    if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 3:
        _refuse("packet path is not a closed Chats packet path")
    if rel.parts[0] != "Chats" or not rel.parts[2].endswith(".md"):
        _refuse("packet path is not a Chats markdown packet")
    current = workspace_root.resolve(strict=True)
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            _refuse("packet path contains a symlink")
    if not current.is_file():
        _refuse("packet path does not exist")
    return rel.as_posix(), current.read_bytes()


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        _refuse(f"{name} is missing")
    return value


def _require_target_session(
    session: Mapping[str, object],
    frontmatter: Mapping[str, object],
) -> None:
    target = _required_text(frontmatter.get("target_session_id"), "target_session_id")
    runtime = session.get("runtime")
    session_ids = {_required_text(session.get("session_id"), "session.session_id")}
    if isinstance(runtime, Mapping) and runtime.get("session_id"):
        session_ids.add(str(runtime["session_id"]))
    if target not in session_ids:
        _refuse("target_session_id does not match selected session")


def _require_binding(
    session: Mapping[str, object],
    frontmatter: Mapping[str, object],
) -> tuple[str, int]:
    target_binding = frontmatter.get("target_binding_id", frontmatter.get("binding_id"))
    session_binding = session.get("binding_id", session.get("conversation_binding_id"))
    if not target_binding or not session_binding or target_binding != session_binding:
        _refuse("binding id is unprovable")
    target_generation = frontmatter.get(
        "target_binding_generation",
        frontmatter.get("binding_generation"),
    )
    session_generation = session.get("binding_generation", session.get("generation"))
    if target_generation != session_generation:
        _refuse("binding generation is unprovable")
    if isinstance(target_generation, bool) or not isinstance(target_generation, int):
        _refuse("binding generation is malformed")
    return str(target_binding), target_generation


def _require_repo_scope(
    session: Mapping[str, object],
    frontmatter: Mapping[str, object],
) -> None:
    session_repos = _string_list(session.get("repo_targets"), "session.repo_targets")
    packet_repos = _string_list(frontmatter.get("repo_targets"), "repo_targets")
    if not session_repos or not packet_repos or not (set(session_repos) & set(packet_repos)):
        _refuse("repo scope is unprovable")


def _latest_project_registry_revision(
    store: LedgerStore,
    *,
    workspace_id: str,
    project_id: str,
) -> str:
    row = store._connection.execute(
        """
        SELECT p.registry_revision
        FROM project_registry_snapshots AS p
        JOIN workspace_registry_snapshots AS w
          ON w.workspace_id = p.workspace_id
         AND w.registry_revision = p.registry_revision
        WHERE p.workspace_id = ? AND p.project_id = ?
        ORDER BY w.captured_at_utc DESC, p.registry_revision DESC
        LIMIT 1
        """,
        (workspace_id, project_id),
    ).fetchone()
    if row is None:
        _refuse("registry revision is unprovable")
    return str(row[0])


def _epoch_ms(timestamp: str) -> int:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyPacketMaterializationRefused(ROUTE_AMBIGUOUS, "sent_utc is malformed") from exc
    if parsed.tzinfo is None:
        _refuse("sent_utc is timezone-naive")
    return int(parsed.timestamp() * 1000)


def _dedupe_key(relpath: str, packet_sha256: str) -> str:
    return "legacy-packet:" + hashlib.sha256(
        _frame(relpath) + _frame(packet_sha256)
    ).hexdigest()


def _frame(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return b"\x01" + len(encoded).to_bytes(8, "big") + encoded


def _unresolved(reason: object) -> dict[str, object]:
    if reason not in CONVERSATION_BINDING_RESOLUTION_REASONS:
        raise RuntimeError("unknown conversation binding resolution reason")
    return {
        "created": False,
        "resolved": False,
        "reason": reason,
        "binding_id": None,
        "generation": None,
    }


def _refuse(detail: str) -> None:
    raise LegacyPacketMaterializationRefused(ROUTE_AMBIGUOUS, detail)
