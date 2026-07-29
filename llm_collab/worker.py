"""Read-only Worker composition over the canonical binding authority (GH-396).

A Worker is derived, never stored: one stable ``worker_id`` per conversation
participant, with the current binding reported through the existing operator
inspection seam. No provider mutation, no new tables, no second resolver.
"""

from __future__ import annotations

import hashlib

from llm_collab.ledger import LedgerStore, validate_project_id
from llm_collab.session_lifecycle import (
    FakeLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
)

# ponytail: the provider is never touched on the inspect path, but the existing
# operator-inspection seam requires one at construction.
_INSPECTION = SessionLifecycleCore(FakeLifecycleProvider())

# ponytail: the binding resolver reads only the compound key; these
# subject fields exist only because LifecycleSubject requires them.
_UNUSED = "unused_read_only"


class WorkerLookupError(LookupError):
    pass


MAX_PROJECT_WORKERS = 256


def _frame(value: str) -> bytes:
    # Same canonical length framing as llm_collab.ledger.store._frame; kept
    # local because exposing the store helper would widen its private API.
    encoded = value.encode("utf-8")
    return b"\x01" + len(encoded).to_bytes(8, "big") + encoded


def derive_worker_id(
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    conversation_id: str,
    participant_id: str,
) -> str:
    fields = (
        "worker-v1",
        workspace_id,
        scope_kind,
        scope_identity,
        conversation_id,
        participant_id,
    )
    digest = hashlib.sha256(b"".join(_frame(field) for field in fields))
    return "worker_" + digest.hexdigest()[:32]


def _participants(
    store: LedgerStore, *, workspace_id: str, project_id: str
) -> list[dict[str, str]]:
    rows = store._connection.execute(
        """
        SELECT scope_kind, scope_identity, conversation_id, participant_id, agent_id
        FROM conversation_participants
        WHERE workspace_id = ? AND scope_kind = 'project' AND scope_identity = ?
        ORDER BY conversation_id, participant_id
        LIMIT ?
        """,
        (workspace_id, validate_project_id(project_id), MAX_PROJECT_WORKERS + 1),
    ).fetchall()
    if len(rows) > MAX_PROJECT_WORKERS:
        raise WorkerLookupError(
            f"project {project_id} has more than {MAX_PROJECT_WORKERS} workers; "
            "refusing an unbounded scan"
        )
    keys = ("scope_kind", "scope_identity", "conversation_id", "participant_id", "agent_id")
    return [dict(zip(keys, row)) for row in rows]


def _compose(
    store: LedgerStore, *, workspace_id: str, row: dict[str, str]
) -> dict[str, object]:
    subject = LifecycleSubject(
        workspace_id=workspace_id,
        scope_kind=row["scope_kind"],
        scope_identity=row["scope_identity"],
        conversation_id=row["conversation_id"],
        participant_id=row["participant_id"],
        agent_id=row["agent_id"],
        endpoint_id=_UNUSED,
        native_session_id=_UNUSED,
        runtime_instance_id=_UNUSED,
    )
    inspection = _INSPECTION.inspect_for_operator(store, subject)
    return {
        "worker_id": derive_worker_id(
            workspace_id=workspace_id,
            scope_kind=row["scope_kind"],
            scope_identity=row["scope_identity"],
            conversation_id=row["conversation_id"],
            participant_id=row["participant_id"],
        ),
        "agent_id": row["agent_id"],
        **inspection,
    }


def list_workers(
    store: LedgerStore, *, workspace_id: str, project_id: str
) -> list[dict[str, object]]:
    return [
        _compose(store, workspace_id=workspace_id, row=row)
        for row in _participants(store, workspace_id=workspace_id, project_id=project_id)
    ]


def show_worker(
    store: LedgerStore, *, workspace_id: str, project_id: str, worker_id: str
) -> dict[str, object]:
    matches = [
        worker
        for worker in list_workers(store, workspace_id=workspace_id, project_id=project_id)
        if worker["worker_id"] == worker_id
    ]
    if not matches:
        raise WorkerLookupError(f"no worker {worker_id} in project {project_id}")
    if len(matches) > 1:
        raise WorkerLookupError(f"worker id {worker_id} is ambiguous in project {project_id}")
    return matches[0]


def _resolve_subject(
    store: LedgerStore, *, workspace_id: str, project_id: str, worker_id: str
) -> LifecycleSubject:
    matches = [
        row
        for row in _participants(store, workspace_id=workspace_id, project_id=project_id)
        if derive_worker_id(
            workspace_id=workspace_id,
            scope_kind=row["scope_kind"],
            scope_identity=row["scope_identity"],
            conversation_id=row["conversation_id"],
            participant_id=row["participant_id"],
        )
        == worker_id
    ]
    if not matches:
        raise WorkerLookupError(f"no worker {worker_id} in project {project_id}")
    if len(matches) > 1:
        raise WorkerLookupError(f"worker id {worker_id} is ambiguous in project {project_id}")
    row = matches[0]
    return LifecycleSubject(
        workspace_id=workspace_id,
        scope_kind=row["scope_kind"],
        scope_identity=row["scope_identity"],
        conversation_id=row["conversation_id"],
        participant_id=row["participant_id"],
        agent_id=row["agent_id"],
        endpoint_id=_UNUSED,
        native_session_id=_UNUSED,
        runtime_instance_id=_UNUSED,
    )


def retire_worker(
    store: LedgerStore, *, workspace_id: str, project_id: str, worker_id: str
) -> dict[str, object]:
    """Release the worker's active mutation ownership via the existing lifecycle
    retire authority. Refuses anything that is not the single active,
    mutation-capable binding; the binding row, generation, and session_ref are
    preserved (only ``state`` moves to ``retired``)."""

    subject = _resolve_subject(
        store, workspace_id=workspace_id, project_id=project_id, worker_id=worker_id
    )
    resolved = _INSPECTION.inspect(store, subject)
    if (
        not resolved.get("resolved")
        or resolved.get("state") != "active"
        or resolved.get("mutation_capable") != 1
    ):
        raise WorkerLookupError(
            f"worker {worker_id} is not an active mutation-capable binding "
            f"(state={resolved.get('state')!r}, reason={resolved.get('reason')!r}); "
            "refusing to retire"
        )
    # Snapshot the pre-retire identity: core.retire() leaves resolution at
    # pull_pending with no active binding fields, so the result is reported from
    # this snapshot, never a post-retire projection.
    retired = {
        "worker_id": worker_id,
        "agent_id": subject.agent_id,
        "conversation_id": subject.conversation_id,
        "participant_id": subject.participant_id,
        "binding_id": resolved["binding_id"],
        "generation": resolved["generation"],
        "session_ref_id": resolved["session_ref_id"],
    }
    _INSPECTION.retire(store, subject, resolved)
    retired["state"] = "retired"
    return retired
