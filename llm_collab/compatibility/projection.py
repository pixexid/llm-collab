"""Read-only compatibility projections from canonical ledger rows.

These helpers are deliberately library-only. They expose enough v2-shaped data
for compatibility readers without making canonical rows a current-authority
transport surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from llm_collab.ledger.paths import validate_project_id, validate_workspace_id
from llm_collab.ledger.store import (
    CanonicalIntegrityError,
    LedgerStore,
    _canonical_agent_id,
    _canonical_manifest_id,
    _canonical_message_id,
)


UNAUTHENTICATED_PROVENANCE = {
    "trust": "caller_asserted_unauthenticated",
    "authority": "not_authenticated",
}


def _agent_without_prefix(agent_id: object) -> str:
    agent = _canonical_agent_id(agent_id, "agent_id")
    return agent.removeprefix("agent_")


def _canonical_message_locator(
    *,
    workspace_id: str,
    project_id: str,
    message_id: str,
) -> str:
    return f"canonical://{workspace_id}/project/{project_id}/messages/{message_id}"


def _append_if_present(target: dict[str, object], key: str, value: object) -> None:
    if value is not None:
        target[key] = value


def _require_no_nulls(value: object, *, path: str = "projection") -> None:
    if value is None:
        raise CanonicalIntegrityError(f"{path} contains an unsupported null")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require_no_nulls(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_no_nulls(item, path=f"{path}[{index}]")


def _project_artifacts(artifacts: Iterable[tuple[str, str]]) -> dict[str, object]:
    repos: list[str] = []
    paths: list[str] = []
    extras: list[dict[str, str]] = []
    for kind, ref in artifacts:
        if kind == "repo":
            repos.append(ref)
        elif kind == "path":
            paths.append(ref)
        elif kind not in {"chat", "task"}:
            extras.append({"kind": kind, "ref": ref})
    result: dict[str, object] = {}
    if repos:
        result["repo_targets"] = repos
    if paths:
        result["path_targets"] = paths
    if extras:
        result["artifact_refs"] = extras
    return result


def project_chat_packet_v2(
    store: LedgerStore,
    *,
    workspace_id: str,
    project_id: str,
    message_id: str,
) -> dict[str, object]:
    """Project one canonical message into an honest v2-compatible packet shape."""
    workspace = validate_workspace_id(workspace_id)
    project = validate_project_id(project_id)
    identifier = _canonical_message_id(message_id, "message_id")
    message = store.read_canonical_message(
        workspace_id=workspace,
        scope_kind="project",
        scope_identity=project,
        message_id=identifier,
    )
    if message is None:
        raise KeyError(identifier)
    body = message["body"]
    if not isinstance(body, bytes):
        raise CanonicalIntegrityError("canonical message body is not bytes")
    try:
        body_text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalIntegrityError(
            "canonical message body cannot be projected as v2 UTF-8 text"
        ) from exc
    recipients = [_agent_without_prefix(agent) for agent in message["recipients"]]  # type: ignore[index]
    frontmatter: dict[str, object] = {
        "from": _agent_without_prefix(message["sender_agent_id"]),
        "sender_agent_id": message["sender_agent_id"],
        "to": recipients[0] if len(recipients) == 1 else recipients,
        "title": message["title"],
        "priority": message["priority"],
        "tags": list(message["tags"]),  # type: ignore[arg-type]
        "project_id": project,
        "sent_utc": message["created_at_utc"],
        "canonical_projection": {
            "projection_kind": "v2_chat_packet",
            "message_id": identifier,
            "body_sha256": message["body_sha256"],
            "authority": "read_only_projection",
            "lossy_fields_omitted": [
                "sender_session_id",
                "target_session_id",
                "supersedes_session_id",
            ],
        },
    }
    _append_if_present(frontmatter, "chat_id", message["chat_link"])
    _append_if_present(frontmatter, "related_task", message["task_link"])
    frontmatter.update(_project_artifacts(message["artifacts"]))  # type: ignore[arg-type]
    projected = {"frontmatter": frontmatter, "body": body_text}
    _require_no_nulls(projected)
    return projected


def project_inbox_pointers_v2(
    store: LedgerStore,
    *,
    workspace_id: str,
    project_id: str,
    recipient_agent_id: str,
) -> dict[str, object]:
    """Project canonical delivery routes into an inbox-like read-only pointer view."""
    workspace = validate_workspace_id(workspace_id)
    project = validate_project_id(project_id)
    recipient = _canonical_agent_id(recipient_agent_id, "recipient_agent_id")
    store.canonical_preflight(write=False)
    rows = store._connection.execute(
        "SELECT d.message_id, d.delivery_id, d.endpoint_id, m.chat_link, m.task_link, "
        "m.created_at_utc, m.title "
        "FROM canonical_deliveries AS d JOIN canonical_messages AS m "
        "ON m.workspace_id = d.workspace_id "
        "AND m.scope_kind = d.scope_kind "
        "AND m.scope_identity = d.scope_identity "
        "AND m.message_id = d.message_id "
        "WHERE d.workspace_id = ? AND d.scope_kind = 'project' "
        "AND d.scope_identity = ? AND d.recipient_agent_id = ? "
        "ORDER BY m.created_at_utc, d.message_id, d.delivery_id",
        (workspace, project, recipient),
    ).fetchall()
    pointers = []
    for message_id, delivery_id, endpoint_id, chat_link, task_link, created_at_utc, title in rows:
        pointer: dict[str, object] = {
            "locator": _canonical_message_locator(
                workspace_id=workspace,
                project_id=project,
                message_id=message_id,
            ),
            "message_id": message_id,
            "delivery_id": delivery_id,
            "endpoint_id": endpoint_id,
            "title": title,
            "sent_utc": created_at_utc,
            "read_state": "not_projected",
            "acknowledgment": "not_inferred",
            "authority": "read_only_route_projection",
        }
        _append_if_present(pointer, "chat_id", chat_link)
        _append_if_present(pointer, "related_task", task_link)
        pointers.append(pointer)
    projected = {
        "projection_kind": "v2_inbox_pointers",
        "workspace_id": workspace,
        "project_id": project,
        "agent": _agent_without_prefix(recipient),
        "recipient_agent_id": recipient,
        "read_state_authority": "not_projected",
        "acknowledgment_authority": "not_inferred",
        "pointers": pointers,
    }
    _require_no_nulls(projected)
    return projected


def project_legacy_manifest_provenance_v2(
    store: LedgerStore,
    *,
    workspace_id: str,
    manifest_id: str,
) -> dict[str, object]:
    """Project v6 legacy import provenance with in-band unauthenticated labels."""
    workspace = validate_workspace_id(workspace_id)
    manifest = _canonical_manifest_id(manifest_id, "manifest_id")
    store.canonical_preflight(write=False)
    row = store._connection.execute(
        "SELECT manifest_seal, publisher_identity, publisher_revision, "
        "publication_transaction_id, provenance_id, source_registry_revision, "
        "cutoff_policy_revision, source_boundary_kind, source_boundary_identity, "
        "imported_at_utc "
        "FROM legacy_import_manifests WHERE workspace_id = ? AND manifest_id = ?",
        (workspace, manifest),
    ).fetchone()
    if row is None:
        raise KeyError(manifest)
    (
        manifest_seal,
        publisher_identity,
        publisher_revision,
        publication_transaction_id,
        provenance_id,
        source_registry_revision,
        cutoff_policy_revision,
        source_boundary_kind,
        source_boundary_identity,
        imported_at_utc,
    ) = row
    entries = [
        {
            "entry_integrity": entry_integrity,
            "canonical_locator": canonical_locator,
            "evidence_form_version": evidence_form_version,
            "content_hash": content_hash,
            "byte_size": byte_size,
            "source_workspace_id": source_workspace_id,
            "source_project_id": source_project_id,
            "source_registry_revision": entry_source_registry_revision,
            "cutoff_policy_revision": entry_cutoff_policy_revision,
            "provenance_label": dict(UNAUTHENTICATED_PROVENANCE),
        }
        for (
            entry_integrity,
            canonical_locator,
            evidence_form_version,
            content_hash,
            byte_size,
            source_workspace_id,
            source_project_id,
            entry_source_registry_revision,
            entry_cutoff_policy_revision,
        ) in store._connection.execute(
            "SELECT entry_integrity, canonical_locator, evidence_form_version, "
            "content_hash, byte_size, source_workspace_id, source_project_id, "
            "source_registry_revision, cutoff_policy_revision "
            "FROM legacy_import_manifest_entries "
            "WHERE workspace_id = ? AND manifest_id = ? ORDER BY canonical_locator",
            (workspace, manifest),
        )
    ]
    records = []
    for entry_integrity, record_kind, scope_kind, scope_identity, message_id in store._connection.execute(
            "SELECT entry_integrity, record_kind, scope_kind, scope_identity, message_id "
            "FROM legacy_import_records "
            "WHERE workspace_id = ? AND manifest_id = ? ORDER BY entry_integrity, record_kind",
            (workspace, manifest),
    ):
        record = {
            "entry_integrity": entry_integrity,
            "record_kind": record_kind,
        }
        _append_if_present(record, "scope_kind", scope_kind)
        _append_if_present(record, "scope_identity", scope_identity)
        _append_if_present(record, "message_id", message_id)
        records.append(record)
    projected = {
        "projection_kind": "v6_legacy_import_provenance",
        "workspace_id": workspace,
        "manifest_id": manifest,
        "manifest_seal": manifest_seal,
        "publication": {
            "source_registry_revision": source_registry_revision,
            "cutoff_policy_revision": cutoff_policy_revision,
            "publisher": {
                "identity": publisher_identity,
                "revision": publisher_revision,
                "provenance_label": dict(UNAUTHENTICATED_PROVENANCE),
            },
            "publication_transaction_id": publication_transaction_id,
            "provenance_id": provenance_id,
            "source_boundary": {
                "kind": source_boundary_kind,
                "identity": source_boundary_identity,
                "provenance_label": dict(UNAUTHENTICATED_PROVENANCE),
            },
            "provenance_label": dict(UNAUTHENTICATED_PROVENANCE),
        },
        "entries": entries,
        "records": records,
        "imported_at_utc": imported_at_utc,
    }
    _require_no_nulls(projected)
    return projected
