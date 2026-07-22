"""Append-only Runtime Adapter quarantine and recovery state store.

This module owns only Clause 12 durable state. It stores already-redacted
adapter records in a caller-supplied SQLite database, folds appended events on
read, and never imports or touches canonical, ledger, inbox, registry, daemon,
queue, process, scheduler, or project-state surfaces.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_collab.runtime_adapter_redaction import RedactedDocument


SCHEMA_VERSION = 1
FRESH_HEALTHY_SEQUENCE_LENGTH = 3  # Clause 12: valid responses required before release.
EVENT_QUARANTINE_OPENED = "quarantine_opened"
EVENT_RECOVERY_AUTHORIZED = "recovery_authorized"
EVENT_ATTEMPT_RECONCILED = "attempt_reconciled"
EVENT_FRESH_HANDSHAKE = "fresh_handshake"
EVENT_VALID_HEALTH = "valid_health"
EVENT_RELEASED = "released"
EVENT_KINDS = frozenset(
    (
        EVENT_QUARANTINE_OPENED,
        EVENT_RECOVERY_AUTHORIZED,
        EVENT_ATTEMPT_RECONCILED,
        EVENT_FRESH_HANDSHAKE,
        EVENT_VALID_HEALTH,
        EVENT_RELEASED,
    )
)
_IDENTITY_FIELDS = (
    "adapter_id",
    "adapter_revision",
    "manifest_id",
    "manifest_revision",
    "profile_id",
    "endpoint_id",
    "workspace_id",
    "scope_identity",
)
_PROJECT_IDENTITY_FIELD = "project_id"
_OCCURRENCE_FIELD = "request_id"
_RECORD_ID_PREFIX = "adapter_record_"
_HEX = frozenset("0123456789abcdef")


class AdapterStateIntegrityError(RuntimeError):
    """Raised when persisted adapter-state rows fail closed on read."""


class AdapterStateStoreError(RuntimeError):
    """Raised when a requested adapter-state store is absent or uninitialized."""


@dataclass(frozen=True)
class AdapterRecordState:
    record_id: str
    opened: bool
    recovery_authorized: bool
    unresolved_attempts: tuple[str, ...]
    reconciled_attempts: tuple[str, ...]
    fresh_handshake: bool
    valid_health_count: int
    release_event_seen: bool
    released: bool
    event_count: int


def initialize_store(db_path: str | Path) -> None:
    """Create or migrate the independent adapter-state SQLite database."""

    with _connect(db_path) as conn:
        _ensure_schema(conn)


def record_quarantine_opened(db_path: str | Path, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_QUARANTINE_OPENED, redacted)


def record_recovery_authorized(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_RECOVERY_AUTHORIZED, redacted, record_id=record_id)


def record_attempt_reconciled(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_ATTEMPT_RECONCILED, redacted, record_id=record_id)


def record_fresh_handshake(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_FRESH_HANDSHAKE, redacted, record_id=record_id)


def record_valid_health(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_VALID_HEALTH, redacted, record_id=record_id)


def record_release(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_RELEASED, redacted, record_id=record_id)


def record_id_for(redacted: RedactedDocument) -> str:
    payload = _payload(redacted)
    identity = _record_identity(payload)
    encoded = _canonical_json(identity)
    return _RECORD_ID_PREFIX + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_record(db_path: str | Path, record_id: str) -> AdapterRecordState:
    _require_record_id(record_id)
    path = Path(db_path)
    if not path.exists():
        raise AdapterStateStoreError("adapter-state store is not initialized")
    with sqlite3.connect(path) as conn:
        _require_schema(conn)
        rows = conn.execute(
            """
            SELECT event_sequence, event_kind, payload_json, payload_sha256
            FROM runtime_adapter_events
            WHERE record_id = ?
            ORDER BY event_sequence
            """,
            (record_id,),
        ).fetchall()
    return _fold(record_id, rows)


def _append_event(
    db_path: str | Path,
    event_kind: str,
    redacted: RedactedDocument,
    *,
    record_id: str | None = None,
) -> str:
    if event_kind not in EVENT_KINDS:
        raise ValueError("unsupported adapter-state event kind")
    payload = _payload(redacted)
    if event_kind == EVENT_QUARANTINE_OPENED:
        if record_id is not None:
            raise ValueError("quarantine_opened derives its record_id from the occurrence")
        record_id = record_id_for(redacted)
    elif record_id is None:
        raise ValueError("adapter-state event requires a record_id")
    _require_record_id(record_id)
    payload_json = _canonical_json(payload)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        if event_kind != EVENT_QUARANTINE_OPENED:
            _require_matching_record_identity(conn, record_id, payload)
        conn.execute(
            """
            INSERT INTO runtime_adapter_events
                (record_id, event_kind, payload_json, payload_sha256)
            VALUES (?, ?, ?, ?)
            """,
            (record_id, event_kind, payload_json, payload_sha256),
        )
    return record_id


def _connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_adapter_events (
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT NOT NULL CHECK(
                instr(record_id, char(0)) = 0
                AND length(record_id) = length('adapter_record_') + 64
                AND substr(record_id, 1, length('adapter_record_')) = 'adapter_record_'
                AND substr(record_id, length('adapter_record_') + 1) NOT GLOB '*[^0-9a-f]*'
            ),
            event_kind TEXT NOT NULL CHECK(event_kind IN (
                'quarantine_opened',
                'recovery_authorized',
                'attempt_reconciled',
                'fresh_handshake',
                'valid_health',
                'released'
            )),
            payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
            payload_sha256 TEXT NOT NULL CHECK(
                instr(payload_sha256, char(0)) = 0
                AND length(payload_sha256) = 64
                AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            append_time_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(
                instr(append_time_utc, char(0)) = 0
                AND length(append_time_utc) > 0
            )
        );

        CREATE TRIGGER IF NOT EXISTS runtime_adapter_events_no_update
        BEFORE UPDATE ON runtime_adapter_events
        BEGIN
            SELECT RAISE(ABORT, 'runtime_adapter_events is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS runtime_adapter_events_no_delete
        BEFORE DELETE ON runtime_adapter_events
        BEGIN
            SELECT RAISE(ABORT, 'runtime_adapter_events is append-only');
        END;
        """
    )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _require_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise AdapterStateStoreError("adapter-state store is not initialized")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_adapter_events'"
    ).fetchone()
    if table is None:
        raise AdapterStateStoreError("adapter-state store is not initialized")


def _fold(record_id: str, rows: list[sqlite3.Row] | list[tuple[Any, ...]]) -> AdapterRecordState:
    opened = False
    recovery_authorized = False
    unresolved_attempts: set[str] = set()
    reconciled_attempts: set[str] = set()
    fresh_handshake = False
    valid_health_count = 0
    release_event_seen = False
    release_accepted = False
    event_count = 0
    seen: set[tuple[str, str]] = set()
    for event_sequence, event_kind, payload_json, payload_sha256 in rows:
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
            raise AdapterStateIntegrityError("adapter-state payload digest mismatch")
        payload = json.loads(payload_json)
        attempt = _attempt_key(payload)
        dedupe_key = _event_identity(event_sequence, event_kind, attempt)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        event_count += 1
        if event_kind == EVENT_QUARANTINE_OPENED:
            opened = True
            if attempt is not None:
                unresolved_attempts.add(attempt)
        elif event_kind == EVENT_RECOVERY_AUTHORIZED:
            if opened:
                recovery_authorized = True
                fresh_handshake = False
                valid_health_count = 0
        elif event_kind == EVENT_ATTEMPT_RECONCILED:
            if fresh_handshake and attempt is not None:
                reconciled_attempts.add(attempt)
                unresolved_attempts.discard(attempt)
        elif event_kind == EVENT_FRESH_HANDSHAKE:
            if recovery_authorized:
                fresh_handshake = True
                valid_health_count = 0
        elif event_kind == EVENT_VALID_HEALTH:
            if fresh_handshake and attempt is not None:
                valid_health_count += 1
        elif event_kind == EVENT_RELEASED:
            release_event_seen = True
            if (
                opened
                and recovery_authorized
                and not unresolved_attempts
                and fresh_handshake
                and valid_health_count >= FRESH_HEALTHY_SEQUENCE_LENGTH
            ):
                release_accepted = True
    return AdapterRecordState(
        record_id=record_id,
        opened=opened,
        recovery_authorized=recovery_authorized,
        unresolved_attempts=tuple(sorted(unresolved_attempts)),
        reconciled_attempts=tuple(sorted(reconciled_attempts)),
        fresh_handshake=fresh_handshake,
        valid_health_count=valid_health_count,
        release_event_seen=release_event_seen,
        released=release_accepted,
        event_count=event_count,
    )


def _payload(redacted: RedactedDocument) -> dict[str, Any]:
    if not isinstance(redacted, RedactedDocument):
        raise TypeError("adapter-state writes require RedactedDocument")
    return redacted.as_dict()


def _record_identity(payload: dict[str, Any]) -> dict[str, Any]:
    identity = _base_identity(payload)
    identity[_OCCURRENCE_FIELD] = _required_identity(payload, _OCCURRENCE_FIELD)
    return identity


def _base_identity(payload: dict[str, Any]) -> dict[str, Any]:
    identity = {field: _required_identity(payload, field) for field in _IDENTITY_FIELDS}
    scope = identity["scope_identity"]
    project = _scope_project_id(scope)
    if project is not None:
        identity[_PROJECT_IDENTITY_FIELD] = _required_identity(payload, _PROJECT_IDENTITY_FIELD)
        if identity[_PROJECT_IDENTITY_FIELD] != project:
            raise ValueError("adapter-state project_id must match scope_identity")
    elif _has_scope_segment(scope, "workspace"):
        if payload.get(_PROJECT_IDENTITY_FIELD) is not None:
            raise ValueError("workspace-scope adapter-state records must not carry project_id")
    else:
        raise ValueError("adapter-state scope_identity must denote project or workspace scope")
    return identity


def _require_matching_record_identity(
    conn: sqlite3.Connection, record_id: str, payload: dict[str, Any]
) -> None:
    row = conn.execute(
        """
        SELECT payload_json, payload_sha256
        FROM runtime_adapter_events
        WHERE record_id = ? AND event_kind = ?
        ORDER BY event_sequence
        LIMIT 1
        """,
        (record_id, EVENT_QUARANTINE_OPENED),
    ).fetchone()
    if row is None:
        raise AdapterStateStoreError("adapter-state record is not initialized")
    payload_json, payload_sha256 = row
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
        raise AdapterStateIntegrityError("adapter-state payload digest mismatch")
    if _base_identity(json.loads(payload_json)) != _base_identity(payload):
        raise ValueError("adapter-state event identity does not match record")


def _required_identity(payload: dict[str, Any], field: str) -> Any:
    if field not in payload or payload[field] is None:
        raise ValueError(f"redacted record missing required identity field: {field}")
    return payload[field]


def _require_record_id(record_id: str) -> None:
    if not isinstance(record_id, str):
        raise ValueError("invalid adapter-state record_id")
    suffix = record_id.removeprefix(_RECORD_ID_PREFIX)
    if (
        suffix == record_id
        or len(suffix) != 64
        or any(char not in _HEX for char in suffix)
    ):
        raise ValueError("invalid adapter-state record_id")


def _scope_project_id(scope_identity: str) -> str | None:
    values = [
        segment.removeprefix("project:")
        for segment in scope_identity.split("|")
        if segment.startswith("project:")
    ]
    if len(values) > 1 or any(not value for value in values):
        raise ValueError("adapter-state scope_identity has invalid project scope")
    return values[0] if values else None


def _has_scope_segment(scope_identity: str, kind: str) -> bool:
    prefix = f"{kind}:"
    return any(segment.startswith(prefix) and segment != prefix for segment in scope_identity.split("|"))


def _attempt_key(payload: dict[str, Any]) -> str | None:
    request_id = payload.get("request_id")
    if request_id is None:
        return None
    return _canonical_json({"request_id": request_id})


def _event_identity(event_sequence: int, event_kind: str, attempt: str | None) -> tuple[str, str]:
    if event_kind == EVENT_QUARANTINE_OPENED:
        return (event_kind, "record")
    if event_kind in {EVENT_RECOVERY_AUTHORIZED, EVENT_FRESH_HANDSHAKE}:
        return (event_kind, attempt or f"missing-{event_kind}-request")
    if event_kind == EVENT_RELEASED:
        return (event_kind, attempt or "missing-release-request")
    if event_kind == EVENT_ATTEMPT_RECONCILED:
        return (event_kind, attempt or "missing-attempt")
    if event_kind == EVENT_VALID_HEALTH:
        return (event_kind, attempt or "missing-health-request")
    return (event_kind, str(event_sequence))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
