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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from llm_collab.runtime_adapter_conformance import (
    validate_delivery_scalar,
    validate_request_id_scalar,
    validate_s2_token,
)
from llm_collab.runtime_adapter_redaction import RedactedDocument


SCHEMA_VERSION = 2
FRESH_HEALTHY_SEQUENCE_LENGTH = 3  # Clause 12: valid responses required before release.
EVENT_QUARANTINE_OPENED = "quarantine_opened"
EVENT_RECOVERY_AUTHORIZED = "recovery_authorized"
EVENT_ATTEMPT_RECONCILED = "attempt_reconciled"
EVENT_FRESH_HANDSHAKE = "fresh_handshake"
EVENT_HEALTH_FAILED = "health_failed"
EVENT_VALID_HEALTH = "valid_health"
EVENT_RELEASE_REVIEWED = "release_reviewed"
EVENT_RELEASED = "released"
EVENT_KINDS = frozenset(
    (
        EVENT_QUARANTINE_OPENED,
        EVENT_RECOVERY_AUTHORIZED,
        EVENT_ATTEMPT_RECONCILED,
        EVENT_FRESH_HANDSHAKE,
        EVENT_HEALTH_FAILED,
        EVENT_VALID_HEALTH,
        EVENT_RELEASE_REVIEWED,
        EVENT_RELEASED,
    )
)
_IDENTITY_FIELDS = (
    "adapter_id",
    "adapter_revision",
    "manifest_id",
    "manifest_revision",
    "profile_id",
    "capability_set_id",
    "capability_set_revision",
    "endpoint_id",
    "workspace_id",
    "scope_identity",
)
_PROJECT_IDENTITY_FIELD = "project_id"
_OCCURRENCE_FIELD = "occurrence_at_utc"
_INCIDENT_FIELD = "incident_id"
_ATTEMPTS_FIELD = "attempts"
_REVIEW_FIELD = "review_references"
_RECORD_ID_PREFIX = "adapter_record_"
_HEX = frozenset("0123456789abcdef")


class AdapterStateIntegrityError(RuntimeError):
    """Raised when persisted adapter-state rows fail closed on read."""


class AdapterStateStoreError(RuntimeError):
    """Raised when a requested adapter-state store is absent or uninitialized."""


# Bounded-work fail-closed cap on one record's stored journal; matches the
# repository's existing MAX_PENDING_EVENTS magnitude (bin/codex_stream.py).
MAX_ADAPTER_STATE_EVENTS = 4096


@dataclass(frozen=True)
class AdapterRecordState:
    record_id: str
    incident_id: str
    opened: bool
    recovery_authorized: bool
    unresolved_attempts: tuple[str, ...]
    reconciled_attempts: tuple[str, ...]
    unresolved_attempt_tuples: tuple[tuple[Any, Any, Any], ...]
    reconciled_attempt_tuples: tuple[tuple[Any, Any, Any], ...]
    fresh_handshake: bool
    valid_health_count: int
    health_failure_count: int
    release_reviewed: bool
    release_event_seen: bool
    released: bool
    event_count: int
    # The exact stored event journal — (event_sequence, event_kind,
    # append_time_utc) for every row, including duplicates the fold dedupes —
    # exposed as recorded (GH-214). Never reconstructed or inferred.
    # (event_sequence, event_kind, producer occurrence timestamp)
    journal: tuple[tuple[int, str, str], ...] = ()


def initialize_store(db_path: str | Path) -> None:
    """Create or migrate the independent adapter-state SQLite database."""

    with _connect(db_path) as conn:
        _ensure_schema(conn)


def record_quarantine_opened(
    db_path: str | Path,
    incident_id: str | RedactedDocument,
    redacted: RedactedDocument = None,
) -> str:
    # The explicit incident_id form is the producer API. The redacted-only form
    # remains a narrow compatibility shim for existing evidence fixtures; it
    # reads only the producer-supplied incident_id and never derives one.
    if redacted is None:
        if not isinstance(incident_id, RedactedDocument):
            raise TypeError("record_quarantine_opened requires incident_id and RedactedDocument")
        redacted = incident_id
        incident_id = _required_incident_id(_payload(redacted))
    if not isinstance(incident_id, str):
        raise TypeError("incident_id must be text")
    return _append_event(db_path, EVENT_QUARANTINE_OPENED, redacted, incident_id=incident_id)


def record_recovery_authorized(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_RECOVERY_AUTHORIZED, redacted, record_id=record_id)


def record_attempt_reconciled(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_ATTEMPT_RECONCILED, redacted, record_id=record_id)


def record_fresh_handshake(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_FRESH_HANDSHAKE, redacted, record_id=record_id)


def record_valid_health(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_VALID_HEALTH, redacted, record_id=record_id)


def record_health_failed(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_HEALTH_FAILED, redacted, record_id=record_id)


def record_release_reviewed(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_RELEASE_REVIEWED, redacted, record_id=record_id)


def record_release(db_path: str | Path, record_id: str, redacted: RedactedDocument) -> str:
    return _append_event(db_path, EVENT_RELEASED, redacted, record_id=record_id)


def record_id_for(redacted: RedactedDocument) -> str:
    payload = _payload(redacted)
    incident_id = _required_incident_id(payload)
    return _RECORD_ID_PREFIX + hashlib.sha256(incident_id.encode("utf-8")).hexdigest()


def read_record(db_path: str | Path, record_id: str) -> AdapterRecordState:
    _require_record_id(record_id)
    path = Path(db_path)
    if not path.exists():
        raise AdapterStateStoreError("adapter-state store is not initialized")
    with sqlite3.connect(path) as conn:
        _require_schema(conn)
        rows = conn.execute(
            """
            SELECT event_sequence, event_kind, payload_json, payload_sha256, append_time_utc,
                   event_schema_version
            FROM runtime_adapter_events
            WHERE record_id = ?
            ORDER BY event_sequence
            LIMIT ?
            """,
            (record_id, MAX_ADAPTER_STATE_EVENTS + 1),
        ).fetchall()
    if len(rows) > MAX_ADAPTER_STATE_EVENTS:
        raise AdapterStateIntegrityError(
            f"adapter-state record exceeds {MAX_ADAPTER_STATE_EVENTS} stored events; "
            "refusing an unbounded journal read"
        )
    journal = tuple(
        (
            _stored_int(event_sequence, "event_sequence"),
            _stored_text(event_kind, "event_kind"),
            _stored_text(json.loads(payload_json).get(_OCCURRENCE_FIELD), _OCCURRENCE_FIELD),
        )
        for event_sequence, event_kind, payload_json, _payload_sha256, _append_time_utc, _schema_version
        in rows
    )
    return replace(_fold(record_id, [(*row[:4], row[5]) for row in rows]), journal=journal)


def _stored_text(value: object, name: str) -> str:
    """Require a stored TEXT value; SQLite may hand back a BLOB from a
    TEXT-affinity column, and str() would fabricate a b'...' literal."""
    if not isinstance(value, str):
        raise AdapterStateIntegrityError(f"adapter-state {name} is not stored text")
    return value


def _stored_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdapterStateIntegrityError(f"adapter-state {name} is not a stored integer")
    return value


def _append_event(
    db_path: str | Path,
    event_kind: str,
    redacted: RedactedDocument,
    *,
    record_id: str | None = None,
    incident_id: str | None = None,
) -> str:
    if event_kind not in EVENT_KINDS:
        raise ValueError("unsupported adapter-state event kind")
    payload = _payload(redacted)
    _validate_event_payload(event_kind, payload)
    payload_incident_id = _required_incident_id(payload)
    if event_kind == EVENT_QUARANTINE_OPENED:
        if record_id is not None or incident_id is None:
            raise ValueError("quarantine_opened requires a producer incident_id")
        if payload_incident_id != incident_id:
            raise ValueError("producer incident_id does not match redacted incident_id")
        record_id = record_id_for(redacted)
    elif record_id is None:
        raise ValueError("adapter-state event requires a record_id")
    _require_record_id(record_id)
    payload_json = _canonical_json(payload)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM runtime_adapter_events WHERE record_id = ? LIMIT 1", (record_id,)
        ).fetchone()
        if event_kind != EVENT_QUARANTINE_OPENED or existing is not None:
            _require_matching_record_identity(conn, record_id, payload)
        has_release_event = conn.execute(
            "SELECT 1 FROM runtime_adapter_events WHERE record_id = ? AND event_kind = ? LIMIT 1",
            (record_id, EVENT_RELEASED),
        ).fetchone()
        if has_release_event is not None and _fold_record_rows(conn, record_id).released:
            raise ValueError("released adapter incident rejects later evidence; create a new incident_id")
        conn.execute(
            """
            INSERT INTO runtime_adapter_events
                (record_id, event_kind, payload_json, payload_sha256, event_schema_version)
            VALUES (?, ?, ?, ?, ?)
            """,
            (record_id, event_kind, payload_json, payload_sha256, SCHEMA_VERSION),
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


_EVENTS_TABLE_SQL = """
CREATE TABLE runtime_adapter_events (
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
        'health_failed',
        'valid_health',
        'release_reviewed',
        'released'
    )),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_sha256 TEXT NOT NULL CHECK(
        instr(payload_sha256, char(0)) = 0
        AND length(payload_sha256) = 64
        AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    event_schema_version INTEGER NOT NULL DEFAULT 2 CHECK(event_schema_version IN (1, 2)),
    append_time_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP CHECK(
        instr(append_time_utc, char(0)) = 0
        AND length(append_time_utc) > 0
    )
)
"""


def _create_triggers(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TRIGGER runtime_adapter_events_no_update
        BEFORE UPDATE ON runtime_adapter_events
        BEGIN
            SELECT RAISE(ABORT, 'runtime_adapter_events is append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER runtime_adapter_events_no_delete
        BEFORE DELETE ON runtime_adapter_events
        BEGIN
            SELECT RAISE(ABORT, 'runtime_adapter_events is append-only');
        END
        """
    )


def _ensure_schema(conn: sqlite3.Connection) -> None:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_adapter_events'"
    ).fetchone()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if table_exists is None:
        if version not in (0, SCHEMA_VERSION):
            raise AdapterStateStoreError("adapter-state store schema version is unsupported")
        conn.execute(_EVENTS_TABLE_SQL)
        _create_triggers(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return
    if version == SCHEMA_VERSION:
        return
    if version != 1:
        raise AdapterStateStoreError("adapter-state store schema version is unsupported")
    _migrate_schema_v1_to_v2(conn)


def _migrate_schema_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Rebuild the one existing table transactionally, preserving every row."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        _preflight_v1_records(conn)
        conn.execute("DROP TRIGGER IF EXISTS runtime_adapter_events_no_update")
        conn.execute("DROP TRIGGER IF EXISTS runtime_adapter_events_no_delete")
        conn.execute("ALTER TABLE runtime_adapter_events RENAME TO runtime_adapter_events_v1")
        conn.execute(_EVENTS_TABLE_SQL)
        conn.execute(
            """
            INSERT INTO runtime_adapter_events
                (event_sequence, record_id, event_kind, payload_json, payload_sha256,
                 event_schema_version, append_time_utc)
            SELECT event_sequence, record_id, event_kind, payload_json, payload_sha256,
                   1, append_time_utc
            FROM runtime_adapter_events_v1
            ORDER BY event_sequence
            """
        )
        conn.execute("DROP TABLE runtime_adapter_events_v1")
        _create_triggers(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _preflight_v1_records(conn: sqlite3.Connection) -> None:
    current_record_id: str | None = None
    current_rows: list[tuple[Any, ...]] = []
    for row in conn.execute(
        """
        SELECT record_id, event_sequence, event_kind, payload_json, payload_sha256
        FROM runtime_adapter_events
        ORDER BY record_id, event_sequence
        """
    ):
        record_id = row[0]
        if current_record_id is not None and record_id != current_record_id:
            _preflight_v1_record(current_record_id, current_rows)
            current_rows = []
        current_record_id = record_id
        current_rows.append(tuple(row[1:]))
        if len(current_rows) > MAX_ADAPTER_STATE_EVENTS:
            raise AdapterStateStoreError(
                f"adapter-state migration blocked for v1 record {record_id}: "
                f"more than {MAX_ADAPTER_STATE_EVENTS} events"
            )
    if current_record_id is not None:
        _preflight_v1_record(current_record_id, current_rows)


def _preflight_v1_record(record_id: str, rows: list[tuple[Any, ...]]) -> None:
    state = _fold(record_id, [(*row, 1) for row in rows])
    if state.released:
        return
    try:
        _required_incident_id({_INCIDENT_FIELD: state.incident_id})
    except ValueError as error:
        raise AdapterStateStoreError(
            f"adapter-state migration blocked for open v1 record {record_id}: "
            f"incident identity is not writable under v2 ({state.incident_id!r})"
        ) from error

    blocked: set[tuple[Any, Any, Any]] = set()
    for attempt in state.unresolved_attempt_tuples:
        attempt_payload = {
            "attempts": [{
                "original_request_id": attempt[0],
                "delivery_id": attempt[1],
                "attempt_id": attempt[2],
            }]
        }
        try:
            _attempt_tuples(attempt_payload, schema_version=SCHEMA_VERSION)
        except (TypeError, ValueError):
            blocked.add(attempt)
    if blocked:
        raise AdapterStateStoreError(
            f"adapter-state migration blocked for open v1 record {record_id}: "
            "unresolved legacy attempt tuples are not writable under v2 "
            f"{_canonical_json(sorted(blocked))}"
        )


def _require_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise AdapterStateStoreError("adapter-state store is not initialized")
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_adapter_events'"
    ).fetchone()
    if table is None:
        raise AdapterStateStoreError("adapter-state store is not initialized")


def _fold_record_rows(conn: sqlite3.Connection, record_id: str) -> AdapterRecordState:
    rows = conn.execute(
        """
        SELECT event_sequence, event_kind, payload_json, payload_sha256, event_schema_version
        FROM runtime_adapter_events
        WHERE record_id = ?
        ORDER BY event_sequence
        LIMIT ?
        """,
        (record_id, MAX_ADAPTER_STATE_EVENTS + 1),
    ).fetchall()
    if len(rows) > MAX_ADAPTER_STATE_EVENTS:
        raise AdapterStateIntegrityError(
            f"adapter-state record exceeds {MAX_ADAPTER_STATE_EVENTS} stored events"
        )
    return _fold(record_id, rows)


def _fold(record_id: str, rows: list[sqlite3.Row] | list[tuple[Any, ...]]) -> AdapterRecordState:
    opened = False
    recovery_authorized = False
    incident_id = ""
    unresolved: set[tuple[Any, Any, Any]] = set()
    reconciled: set[tuple[Any, Any, Any]] = set()
    fresh_handshake = False
    valid_health_count = 0
    health_failure_count = 0
    release_reviewed = False
    release_event_seen = False
    release_accepted = False
    event_count = 0
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        event_sequence, event_kind, payload_json, payload_sha256 = row[:4]
        schema_version = row[4] if len(row) >= 5 else SCHEMA_VERSION
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
            raise AdapterStateIntegrityError("adapter-state payload digest mismatch")
        if schema_version not in (1, SCHEMA_VERSION):
            raise AdapterStateIntegrityError("adapter-state event schema version is invalid")
        try:
            payload = json.loads(payload_json)
            _validate_event_payload(event_kind, payload, schema_version=schema_version)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AdapterStateIntegrityError("adapter-state event payload is invalid") from error
        if not incident_id:
            incident_id = payload[_INCIDENT_FIELD]
        if payload[_INCIDENT_FIELD] != incident_id:
            raise AdapterStateIntegrityError("adapter-state incident identity mismatch")
        attempts = set(_attempt_tuples(payload, schema_version=schema_version))
        occurrence = payload[_OCCURRENCE_FIELD]
        dedupe_key = (event_kind, occurrence, _canonical_json(sorted(attempts)))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        event_count += 1
        if event_kind == EVENT_QUARANTINE_OPENED:
            opened = True
            unresolved.update(attempts)
        elif event_kind == EVENT_RECOVERY_AUTHORIZED:
            if opened:
                recovery_authorized = True
                release_reviewed = False
                fresh_handshake = False
                valid_health_count = 0
        elif event_kind == EVENT_ATTEMPT_RECONCILED:
            if fresh_handshake:
                reconciled.update(attempts)
                unresolved.difference_update(attempts)
        elif event_kind == EVENT_FRESH_HANDSHAKE:
            if recovery_authorized:
                fresh_handshake = True
                valid_health_count = 0
        elif event_kind == EVENT_HEALTH_FAILED:
            health_failure_count += 1
            fresh_handshake = False
            valid_health_count = 0
        elif event_kind == EVENT_VALID_HEALTH:
            if fresh_handshake:
                valid_health_count += 1
        elif event_kind == EVENT_RELEASE_REVIEWED:
            if _review_matches_incident(payload, schema_version=schema_version):
                release_reviewed = True
        elif event_kind == EVENT_RELEASED:
            release_event_seen = True
            if (
                opened
                and recovery_authorized
                and (release_reviewed or schema_version == 1)
                and not unresolved
                and fresh_handshake
                and valid_health_count >= FRESH_HEALTHY_SEQUENCE_LENGTH
            ):
                release_accepted = True
    return AdapterRecordState(
        record_id=record_id,
        incident_id=incident_id,
        opened=opened,
        recovery_authorized=recovery_authorized,
        unresolved_attempts=tuple(sorted(_legacy_attempt_label(item) for item in unresolved)),
        reconciled_attempts=tuple(sorted(_legacy_attempt_label(item) for item in reconciled)),
        unresolved_attempt_tuples=tuple(sorted(unresolved)),
        reconciled_attempt_tuples=tuple(sorted(reconciled)),
        fresh_handshake=fresh_handshake,
        valid_health_count=valid_health_count,
        health_failure_count=health_failure_count,
        release_reviewed=release_reviewed,
        release_event_seen=release_event_seen,
        released=release_accepted,
        event_count=event_count,
    )


def _payload(redacted: RedactedDocument) -> dict[str, Any]:
    if not isinstance(redacted, RedactedDocument):
        raise TypeError("adapter-state writes require RedactedDocument")
    return redacted.as_dict()


def _record_identity(payload: dict[str, Any]) -> dict[str, Any]:
    return {_INCIDENT_FIELD: _required_incident_id(payload), **_base_identity(payload)}


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


def _validate_event_payload(
    event_kind: str, payload: Any, *, schema_version: int = SCHEMA_VERSION
) -> None:
    if not isinstance(payload, dict):
        raise ValueError("adapter-state payload must be an object")
    if schema_version not in (1, SCHEMA_VERSION):
        raise ValueError("adapter-state event schema version is invalid")
    if schema_version == 1:
        _legacy_incident_id(payload)
    else:
        _required_incident_id(payload)
    occurrence = _required_identity(payload, _OCCURRENCE_FIELD)
    if not isinstance(occurrence, str) or not occurrence.endswith("Z"):
        raise ValueError("adapter-state occurrence timestamp is invalid")
    _base_identity(payload)
    attempts = payload.get(_ATTEMPTS_FIELD)
    if not isinstance(attempts, list):
        raise ValueError("adapter-state attempts are required")
    _attempt_tuples(payload, schema_version=schema_version)
    if event_kind == EVENT_ATTEMPT_RECONCILED and len(attempts) != 1:
        raise ValueError("attempt_reconciled requires exactly one attempt")
    if event_kind == EVENT_HEALTH_FAILED and not payload.get("health_failure_reason"):
        raise ValueError("health_failed requires a failure reason")
    if event_kind == EVENT_RELEASE_REVIEWED:
        if not payload.get(_REVIEW_FIELD) or not _review_matches_incident(payload, schema_version=schema_version):
            raise ValueError("release_reviewed requires matching review references")


def _legacy_incident_id(payload: dict[str, Any]) -> str:
    value = payload.get(_INCIDENT_FIELD)
    if not isinstance(value, str) or not value or len(value) > 128 or _unsafe_text(value):
        raise ValueError("legacy adapter-state incident_id is invalid")
    return value


def _unsafe_text(value: str) -> bool:
    return "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _required_incident_id(payload: dict[str, Any]) -> str:
    value = payload.get(_INCIDENT_FIELD)
    try:
        return validate_s2_token(value)
    except ValueError as error:
        raise ValueError("redacted record requires a valid incident_id") from error


def _attempt_tuples(
    payload: dict[str, Any], *, schema_version: int = SCHEMA_VERSION
) -> tuple[tuple[Any, Any, Any], ...]:
    attempts = payload.get(_ATTEMPTS_FIELD)
    if not isinstance(attempts, list):
        raise ValueError("adapter-state attempts are required")
    result: list[tuple[Any, Any, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != {
            "original_request_id", "delivery_id", "attempt_id"
        }:
            raise ValueError("adapter-state attempt tuple is incomplete")
        original = attempt["original_request_id"]
        delivery = attempt["delivery_id"]
        attempt_id = attempt["attempt_id"]
        try:
            if schema_version == 1:
                original = _legacy_request_id(original)
                delivery = _legacy_delivery_scalar(delivery, "delivery_id")
                attempt_id = _legacy_delivery_scalar(attempt_id, "attempt_id")
            else:
                original = validate_request_id_scalar(original)
                delivery = validate_delivery_scalar(delivery, "delivery_id")
                attempt_id = validate_delivery_scalar(attempt_id, "attempt_id")
        except ValueError as error:
            raise ValueError("adapter-state attempt tuple scalar is invalid") from error
        value = (original, delivery, attempt_id)
        if value in result:
            raise ValueError("adapter-state attempt tuple is duplicated")
        result.append(value)
    return tuple(result)


def _legacy_request_id(value: Any) -> str | int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("legacy RequestId is invalid")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError("legacy RequestId is invalid")
    if isinstance(value, str) and _unsafe_text(value):
        raise ValueError("legacy RequestId is invalid")
    return value


def _legacy_delivery_scalar(value: Any, field: str) -> str:
    prefix = "delivery_" if field == "delivery_id" else "attempt_"
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 128 or _unsafe_text(value):
        raise ValueError("legacy DeliveryV1 scalar is invalid")
    return value


def _review_matches_incident(
    payload: dict[str, Any], *, schema_version: int = SCHEMA_VERSION
) -> bool:
    refs = payload.get(_REVIEW_FIELD)
    if not isinstance(refs, list) or not refs:
        return False
    return all(
        isinstance(ref, dict)
        and set(ref) == {
            "manifest_id", "manifest_revision", "capability_set_id",
            "capability_set_revision", "diagnostic_id",
        }
        and ref.get("manifest_id") == payload.get("manifest_id")
        and ref.get("manifest_revision") == payload.get("manifest_revision")
        and ref.get("capability_set_id") == payload.get("capability_set_id")
        and ref.get("capability_set_revision") == payload.get("capability_set_revision")
        and (
            _valid_s2_token(ref.get("diagnostic_id"))
            if schema_version == SCHEMA_VERSION
            else _legacy_valid_identity(ref.get("diagnostic_id"))
        )
        for ref in refs
    )


def _legacy_valid_identity(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 128 and not _unsafe_text(value)


def _valid_s2_token(value: Any) -> bool:
    try:
        validate_s2_token(value)
    except ValueError:
        return False
    return True


def _legacy_attempt_label(attempt: tuple[Any, Any, Any]) -> str:
    return _canonical_json({"request_id": attempt[0]})


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
    if _record_identity(json.loads(payload_json)) != _record_identity(payload):
        raise ValueError("adapter-state event identity does not match record")


def _required_identity(payload: dict[str, Any], field: str) -> Any:
    if field not in payload or payload[field] is None:
        raise ValueError(f"redacted record missing required identity field: {field}")
    return payload[field]


def _require_record_id(record_id: str) -> None:
    if not isinstance(record_id, str):
        raise ValueError("invalid adapter-state record_id")
    suffix = record_id.removeprefix(_RECORD_ID_PREFIX)
    if suffix == record_id or len(suffix) != 64 or any(char not in _HEX for char in suffix):
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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
