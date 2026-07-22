"""SQLite storage foundation for the inert observation ledger."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from .paths import LedgerPaths, validate_project_id, validate_registry_token, validate_workspace_id


SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5_000
SYNCHRONOUS_FULL = 2
MIGRATION_TOOL_VERSION = "llm-collab-ledger/1"
SAFE_SQLITE_BACKPORTS = {(3, 44, 6), (3, 50, 7)}
V1_TABLES = frozenset(
    {
        "schema_migrations",
        "workspace_registry_snapshots",
        "project_registry_snapshots",
        "observation_source_registry_snapshots",
        "daemon_instances",
    }
)
V2_TABLES = V1_TABLES | frozenset(
    {
        "observations",
        "observation_checkpoints",
        "observation_audit",
    }
)
V3_TABLES = V2_TABLES | frozenset({"legacy_provenance_imports"})
V4_TABLES = V3_TABLES | frozenset(
    {
        "canonical_bodies",
        "canonical_messages",
        "canonical_message_recipients",
        "canonical_message_artifacts",
        "canonical_message_tags",
    }
)


class SQLiteSafetyError(RuntimeError):
    pass


class WriterAlreadyOpenError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


class CanonicalConflictError(RuntimeError):
    pass


def _linked_sqlite_version_info() -> Sequence[int]:
    return sqlite3.sqlite_version_info


def _validate_sqlite_version(raw: Sequence[int]) -> tuple[int, int, int]:
    if len(raw) < 3 or any(isinstance(item, bool) or not isinstance(item, int) for item in raw[:3]):
        raise SQLiteSafetyError("SQLite WAL safety version must contain three integers")
    version = tuple(raw[:3])
    if version not in SAFE_SQLITE_BACKPORTS and version < (3, 51, 3):
        rendered = ".".join(str(item) for item in version)
        raise SQLiteSafetyError(
            f"SQLite {rendered} is unsafe for WAL: the WAL-reset corruption safety fix "
            "requires exactly 3.44.6, 3.50.7, or 3.51.3 and newer"
        )
    return version


def require_safe_sqlite() -> tuple[int, int, int]:
    return _validate_sqlite_version(_linked_sqlite_version_info())


V1_SQL = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        migration_checksum TEXT NOT NULL CHECK (length(migration_checksum) > 0),
        applied_at_utc TEXT NOT NULL CHECK (length(applied_at_utc) > 0),
        tool_version TEXT NOT NULL CHECK (length(tool_version) > 0),
        backup_reference TEXT NOT NULL CHECK (length(backup_reference) > 0)
    ) STRICT
    """,
    """
    CREATE TABLE workspace_registry_snapshots (
        workspace_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        registry_source_sha256 TEXT NOT NULL,
        captured_at_utc TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, registry_revision),
        CHECK (registry_revision = 'sha256:' || registry_source_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE project_registry_snapshots (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, project_id, registry_revision),
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE observation_source_registry_snapshots (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, project_id, source_id, registry_revision),
        FOREIGN KEY (workspace_id, project_id, registry_revision)
            REFERENCES project_registry_snapshots (workspace_id, project_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE daemon_instances (
        workspace_id TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        registry_revision TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        stopped_at_utc TEXT,
        metadata_json TEXT NOT NULL,
        PRIMARY KEY (workspace_id, instance_id),
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
)
V1_MIGRATION_CHECKSUM = "sha256:ce236daff444f736e01f3666ed44baf1c3ba17e81215fedb638276aff76b01c7"
V1_SCHEMA_FINGERPRINT = "sha256:26a856329406e45d22a8fbecdbd769d9c632acae3652d8c72438d228de7cfca2"

V2_SQL = (
    """
    CREATE TABLE observations (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        source_id TEXT NOT NULL CHECK (source_id = 'chats_mailbox'),
        registry_revision TEXT NOT NULL,
        dedupe_key TEXT NOT NULL
            CHECK (
                instr(dedupe_key, char(0)) = 0
                AND length(CAST(dedupe_key AS BLOB)) = 64
                AND dedupe_key NOT GLOB '*[^0-9a-f]*'
            ),
        path TEXT NOT NULL
            CHECK (
                length(path) > 0
                AND substr(path, 1, 1) != '/'
                AND substr(path, -1, 1) != '/'
                AND instr(path, '//') = 0
                AND instr(path, '\\') = 0
                AND instr(path, char(0)) = 0
                AND path != '.'
                AND path != '..'
                AND path NOT LIKE '../%'
                AND path NOT LIKE '%/../%'
                AND path NOT LIKE '%/..'
                AND path NOT LIKE './%'
                AND path NOT LIKE '%/./%'
                AND path NOT LIKE '%/.'
            ),
        content_sha256 TEXT NOT NULL
            CHECK (
                instr(content_sha256, char(0)) = 0
                AND length(CAST(content_sha256 AS BLOB)) = 64
                AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        byte_size INTEGER NOT NULL
            CHECK (typeof(byte_size) = 'integer' AND byte_size >= 0),
        mtime_ns INTEGER NOT NULL
            CHECK (
                typeof(mtime_ns) = 'integer'
                AND mtime_ns BETWEEN -9223372036854775808 AND 9223372036854775807
            ),
        resolution_state TEXT NOT NULL DEFAULT 'unresolved'
            CHECK (resolution_state IN ('unresolved', 'resolved')),
        observed_at_utc TEXT NOT NULL
            CHECK (
                instr(observed_at_utc, char(0)) = 0
                AND length(CAST(observed_at_utc AS BLOB)) > 0
            ),
        resolved_at_utc TEXT,
        PRIMARY KEY (
            workspace_id, project_id, source_id, registry_revision, dedupe_key
        ),
        FOREIGN KEY (workspace_id, project_id, source_id, registry_revision)
            REFERENCES observation_source_registry_snapshots
                (workspace_id, project_id, source_id, registry_revision)
            ON DELETE RESTRICT,
        CHECK (
            (resolution_state = 'unresolved' AND resolved_at_utc IS NULL)
            OR
            (
                resolution_state = 'resolved'
                AND resolved_at_utc IS NOT NULL
                AND instr(resolved_at_utc, char(0)) = 0
                AND length(CAST(resolved_at_utc AS BLOB)) > 0
            )
        )
    ) STRICT
    """,
    """
    CREATE TABLE observation_checkpoints (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        source_id TEXT NOT NULL CHECK (source_id = 'chats_mailbox'),
        registry_revision TEXT NOT NULL,
        cursor TEXT NOT NULL,
        scanned_count INTEGER NOT NULL
            CHECK (typeof(scanned_count) = 'integer' AND scanned_count BETWEEN 0 AND 2000),
        written_count INTEGER NOT NULL
            CHECK (typeof(written_count) = 'integer' AND written_count BETWEEN 0 AND 500),
        updated_at_utc TEXT NOT NULL
            CHECK (
                instr(updated_at_utc, char(0)) = 0
                AND length(CAST(updated_at_utc AS BLOB)) > 0
            ),
        PRIMARY KEY (workspace_id, project_id, source_id, registry_revision),
        FOREIGN KEY (workspace_id, project_id, source_id, registry_revision)
            REFERENCES observation_source_registry_snapshots
                (workspace_id, project_id, source_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE observation_audit (
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        source_id TEXT NOT NULL CHECK (source_id = 'chats_mailbox'),
        registry_revision TEXT NOT NULL,
        audit_id INTEGER NOT NULL CHECK (typeof(audit_id) = 'integer' AND audit_id > 0),
        action TEXT NOT NULL CHECK (action IN ('reconcile', 'resolve', 'retention')),
        result TEXT NOT NULL CHECK (result = 'committed'),
        occurred_at_utc TEXT NOT NULL
            CHECK (
                instr(occurred_at_utc, char(0)) = 0
                AND length(CAST(occurred_at_utc AS BLOB)) > 0
            ),
        detail_json TEXT NOT NULL
            CHECK (
                instr(detail_json, char(0)) = 0
                AND length(CAST(detail_json AS BLOB)) BETWEEN 2 AND 4096
            ),
        PRIMARY KEY (
            workspace_id, project_id, source_id, registry_revision, audit_id
        ),
        FOREIGN KEY (workspace_id, project_id, source_id, registry_revision)
            REFERENCES observation_source_registry_snapshots
                (workspace_id, project_id, source_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
)

V2_MIGRATION_CHECKSUM = "sha256:338a5d526b6fdea47af667c469897fd38d97a4a2dc8caf90dc5d62c067610e36"
V2_SCHEMA_FINGERPRINT = "sha256:805aa5ae43c31d85dbe9a84590050b701ddc69cfe1dd225e9c6e67afbd889a7c"

V3_SQL = (
    """
    CREATE TABLE legacy_provenance_imports (
        workspace_id TEXT NOT NULL
            CHECK (
                instr(workspace_id, char(0)) = 0
                AND length(CAST(workspace_id AS BLOB)) > 0
            ),
        registry_revision TEXT NOT NULL
            CHECK (
                instr(registry_revision, char(0)) = 0
                AND length(CAST(registry_revision AS BLOB)) = 71
                AND substr(registry_revision, 1, 7) = 'sha256:'
                AND substr(registry_revision, 8) NOT GLOB '*[^0-9a-f]*'
            ),
        scope_kind TEXT NOT NULL
            CHECK (scope_kind IN ('exact_project', 'legacy_unscoped')),
        scope_identity TEXT NOT NULL,
        project_id TEXT
            CHECK (
                project_id IS NULL
                OR (
                    instr(project_id, char(0)) = 0
                    AND length(CAST(project_id AS BLOB)) > 0
                )
            ),
        source_family TEXT NOT NULL CHECK (source_family = 'session_autobridge'),
        record_kind TEXT NOT NULL CHECK (record_kind IN ('session', 'activation_lease')),
        source_locator TEXT NOT NULL
            CHECK (
                instr(source_locator, char(0)) = 0
                AND length(CAST(source_locator AS BLOB)) BETWEEN 1 AND 4096
                AND substr(source_locator, 1, 1) != '/'
                AND substr(source_locator, -1, 1) != '/'
                AND instr(source_locator, '//') = 0
                AND instr(source_locator, '\\') = 0
                AND source_locator != '.'
                AND source_locator != '..'
                AND source_locator NOT LIKE '../%'
                AND source_locator NOT LIKE '%/../%'
                AND source_locator NOT LIKE '%/..'
                AND source_locator NOT LIKE './%'
                AND source_locator NOT LIKE '%/./%'
                AND source_locator NOT LIKE '%/.'
            ),
        content_sha256 TEXT NOT NULL
            CHECK (
                instr(content_sha256, char(0)) = 0
                AND length(CAST(content_sha256 AS BLOB)) = 64
                AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        byte_size INTEGER NOT NULL
            CHECK (typeof(byte_size) = 'integer' AND byte_size BETWEEN 0 AND 1048576),
        observed_at_utc TEXT NOT NULL
            CHECK (
                instr(observed_at_utc, char(0)) = 0
                AND length(CAST(observed_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        imported_at_utc TEXT NOT NULL
            CHECK (
                instr(imported_at_utc, char(0)) = 0
                AND length(CAST(imported_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        import_transaction_id TEXT NOT NULL
            CHECK (
                instr(import_transaction_id, char(0)) = 0
                AND length(CAST(import_transaction_id AS BLOB)) = 32
                AND import_transaction_id NOT GLOB '*[^0-9a-f]*'
            ),
        import_revision TEXT NOT NULL CHECK (import_revision = 'legacy-provenance/1'),
        PRIMARY KEY (
            workspace_id,
            registry_revision,
            scope_kind,
            scope_identity,
            source_family,
            record_kind,
            source_locator,
            content_sha256,
            import_revision
        ),
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT,
        FOREIGN KEY (workspace_id, project_id, registry_revision)
            REFERENCES project_registry_snapshots (workspace_id, project_id, registry_revision)
            ON DELETE RESTRICT,
        CHECK (
            (
                scope_kind = 'exact_project'
                AND project_id IS NOT NULL
                AND scope_identity = project_id
            )
            OR
            (
                scope_kind = 'legacy_unscoped'
                AND project_id IS NULL
                AND scope_identity = 'legacy_unscoped'
            )
        )
    ) STRICT
    """,
    """
    CREATE TRIGGER legacy_provenance_imports_no_update
    BEFORE UPDATE ON legacy_provenance_imports
    BEGIN
        SELECT RAISE(ABORT, 'legacy provenance is append-only');
    END
    """,
    """
    CREATE TRIGGER legacy_provenance_imports_no_delete
    BEFORE DELETE ON legacy_provenance_imports
    BEGIN
        SELECT RAISE(ABORT, 'legacy provenance is append-only');
    END
    """,
)
V3_MIGRATION_CHECKSUM = "sha256:1b8380593b73695bf8824425b58eda7c94f51fc0937f07dbcbd1786a6e5d467b"
V3_SCHEMA_FINGERPRINT = "sha256:88e59c9be91df366c03985f99f8b3db1c68382b4846612c0334fd15cc505e673"

# V4 hardens the resultant schema_migrations table with triggers. Released
# fingerprints remain byte-exact because each is rebuilt from its own released SQL only.
V4_SQL = (
    """
    CREATE TABLE canonical_bodies (
        workspace_id TEXT NOT NULL
            CHECK (
                instr(workspace_id, char(0)) = 0
                AND length(CAST(workspace_id AS BLOB)) BETWEEN 6 AND 131
                AND substr(workspace_id, 1, 3) = 'ws_'
                AND substr(workspace_id, 4, 1) GLOB '[A-Za-z0-9]'
                AND substr(workspace_id, 4) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        body_sha256 TEXT NOT NULL
            CHECK (
                instr(body_sha256, char(0)) = 0
                AND length(CAST(body_sha256 AS BLOB)) = 64
                AND body_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        byte_size INTEGER NOT NULL
            CHECK (typeof(byte_size) = 'integer' AND byte_size BETWEEN 0 AND 1048576),
        body BLOB NOT NULL
            CHECK (typeof(body) = 'blob' AND length(body) = byte_size),
        created_at_utc TEXT NOT NULL
            CHECK (
                instr(created_at_utc, char(0)) = 0
                AND length(CAST(created_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, body_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE canonical_messages (
        workspace_id TEXT NOT NULL
            CHECK (
                instr(workspace_id, char(0)) = 0
                AND length(CAST(workspace_id AS BLOB)) BETWEEN 6 AND 131
                AND substr(workspace_id, 1, 3) = 'ws_'
                AND substr(workspace_id, 4, 1) GLOB '[A-Za-z0-9]'
                AND substr(workspace_id, 4) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        scope_kind TEXT NOT NULL CHECK (scope_kind IN ('workspace', 'project')),
        scope_identity TEXT NOT NULL
            CHECK (
                instr(scope_identity, char(0)) = 0
                AND length(CAST(scope_identity AS BLOB)) BETWEEN 1 AND 128
            ),
        message_id TEXT NOT NULL
            CHECK (
                instr(message_id, char(0)) = 0
                AND length(CAST(message_id AS BLOB)) = 68
                AND substr(message_id, 1, 4) = 'msg_'
                AND substr(message_id, 5) NOT GLOB '*[^0-9a-f]*'
            ),
        sender_agent_id TEXT NOT NULL
            CHECK (
                instr(sender_agent_id, char(0)) = 0
                AND length(CAST(sender_agent_id AS BLOB)) BETWEEN 9 AND 134
                AND substr(sender_agent_id, 1, 6) = 'agent_'
                AND substr(sender_agent_id, 7, 1) GLOB '[A-Za-z0-9]'
                AND substr(sender_agent_id, 7) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        dedupe_key TEXT NOT NULL
            CHECK (
                instr(dedupe_key, char(0)) = 0
                AND length(CAST(dedupe_key AS BLOB)) BETWEEN 1 AND 256
            ),
        body_sha256 TEXT NOT NULL
            CHECK (
                instr(body_sha256, char(0)) = 0
                AND length(CAST(body_sha256 AS BLOB)) = 64
                AND body_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        reply_to_message_id TEXT
            CHECK (
                reply_to_message_id IS NULL
                OR (
                    instr(reply_to_message_id, char(0)) = 0
                    AND length(CAST(reply_to_message_id AS BLOB)) = 68
                    AND substr(reply_to_message_id, 1, 4) = 'msg_'
                    AND substr(reply_to_message_id, 5) NOT GLOB '*[^0-9a-f]*'
                )
            ),
        ttl_seconds INTEGER NOT NULL
            CHECK (typeof(ttl_seconds) = 'integer' AND ttl_seconds BETWEEN 0 AND 31536000),
        ack_policy TEXT NOT NULL CHECK (ack_policy IN ('none', 'required')),
        title TEXT NOT NULL
            CHECK (
                instr(title, char(0)) = 0
                AND length(CAST(title AS BLOB)) BETWEEN 1 AND 512
            ),
        priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
        chat_link TEXT
            CHECK (
                chat_link IS NULL
                OR (
                    instr(chat_link, char(0)) = 0
                    AND length(CAST(chat_link AS BLOB)) BETWEEN 1 AND 256
                )
            ),
        task_link TEXT
            CHECK (
                task_link IS NULL
                OR (
                    instr(task_link, char(0)) = 0
                    AND length(CAST(task_link AS BLOB)) BETWEEN 1 AND 256
                )
            ),
        registry_revision TEXT NOT NULL
            CHECK (
                instr(registry_revision, char(0)) = 0
                AND length(CAST(registry_revision AS BLOB)) = 71
                AND substr(registry_revision, 1, 7) = 'sha256:'
                AND substr(registry_revision, 8) NOT GLOB '*[^0-9a-f]*'
            ),
        project_id TEXT
            CHECK (
                project_id IS NULL
                OR (
                    instr(project_id, char(0)) = 0
                    AND length(CAST(project_id AS BLOB)) BETWEEN 1 AND 128
                    AND substr(project_id, 1, 1) GLOB '[A-Za-z]'
                    AND project_id NOT GLOB '*[^A-Za-z0-9._-]*'
                )
            ),
        created_at_utc TEXT NOT NULL
            CHECK (
                instr(created_at_utc, char(0)) = 0
                AND length(CAST(created_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, scope_kind, scope_identity, message_id),
        UNIQUE (workspace_id, scope_kind, scope_identity, sender_agent_id, dedupe_key),
        FOREIGN KEY (workspace_id, body_sha256)
            REFERENCES canonical_bodies (workspace_id, body_sha256)
            ON DELETE RESTRICT,
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT,
        FOREIGN KEY (workspace_id, project_id, registry_revision)
            REFERENCES project_registry_snapshots (workspace_id, project_id, registry_revision)
            ON DELETE RESTRICT,
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, reply_to_message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT,
        CHECK (
            (
                scope_kind = 'project'
                AND project_id IS NOT NULL
                AND scope_identity = project_id
            )
            OR
            (
                scope_kind = 'workspace'
                AND project_id IS NULL
                AND scope_identity = 'workspace'
            )
        )
    ) STRICT
    """,
    """
    CREATE TABLE canonical_message_recipients (
        workspace_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_identity TEXT NOT NULL,
        message_id TEXT NOT NULL,
        recipient_agent_id TEXT NOT NULL
            CHECK (
                instr(recipient_agent_id, char(0)) = 0
                AND length(CAST(recipient_agent_id AS BLOB)) BETWEEN 9 AND 134
                AND substr(recipient_agent_id, 1, 6) = 'agent_'
                AND substr(recipient_agent_id, 7, 1) GLOB '[A-Za-z0-9]'
                AND substr(recipient_agent_id, 7) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        PRIMARY KEY (
            workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id
        ),
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE canonical_message_artifacts (
        workspace_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_identity TEXT NOT NULL,
        message_id TEXT NOT NULL,
        artifact_kind TEXT NOT NULL
            CHECK (artifact_kind IN ('chat', 'task', 'repo', 'path', 'branch', 'worktree')),
        artifact_ref TEXT NOT NULL
            CHECK (
                instr(artifact_ref, char(0)) = 0
                AND length(CAST(artifact_ref AS BLOB)) BETWEEN 1 AND 4096
            ),
        PRIMARY KEY (
            workspace_id, scope_kind, scope_identity, message_id, artifact_kind, artifact_ref
        ),
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE canonical_message_tags (
        workspace_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_identity TEXT NOT NULL,
        message_id TEXT NOT NULL,
        tag TEXT NOT NULL
            CHECK (
                instr(tag, char(0)) = 0
                AND length(CAST(tag AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, scope_kind, scope_identity, message_id, tag),
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TRIGGER canonical_bodies_no_update
    BEFORE UPDATE ON canonical_bodies
    BEGIN
        SELECT RAISE(ABORT, 'canonical bodies are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_bodies_no_delete
    BEFORE DELETE ON canonical_bodies
    BEGIN
        SELECT RAISE(ABORT, 'canonical bodies are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_messages_no_update
    BEFORE UPDATE ON canonical_messages
    BEGIN
        SELECT RAISE(ABORT, 'canonical messages are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_messages_no_delete
    BEFORE DELETE ON canonical_messages
    BEGIN
        SELECT RAISE(ABORT, 'canonical messages are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_recipients_no_update
    BEFORE UPDATE ON canonical_message_recipients
    BEGIN
        SELECT RAISE(ABORT, 'canonical recipients are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_recipients_no_delete
    BEFORE DELETE ON canonical_message_recipients
    BEGIN
        SELECT RAISE(ABORT, 'canonical recipients are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_artifacts_no_update
    BEFORE UPDATE ON canonical_message_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'canonical artifacts are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_artifacts_no_delete
    BEFORE DELETE ON canonical_message_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'canonical artifacts are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_tags_no_update
    BEFORE UPDATE ON canonical_message_tags
    BEGIN
        SELECT RAISE(ABORT, 'canonical tags are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_tags_no_delete
    BEFORE DELETE ON canonical_message_tags
    BEGIN
        SELECT RAISE(ABORT, 'canonical tags are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_message_recipients_count_cap
    BEFORE INSERT ON canonical_message_recipients
    WHEN (
        SELECT count(*) FROM canonical_message_recipients
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    ) >= 256
    BEGIN
        SELECT RAISE(ABORT, 'canonical recipient count exceeds 256');
    END
    """,
    """
    CREATE TRIGGER canonical_message_artifacts_count_cap
    BEFORE INSERT ON canonical_message_artifacts
    WHEN (
        SELECT count(*) FROM canonical_message_artifacts
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    ) >= 256
    BEGIN
        SELECT RAISE(ABORT, 'canonical artifact count exceeds 256');
    END
    """,
    """
    CREATE TRIGGER canonical_message_tags_count_cap
    BEFORE INSERT ON canonical_message_tags
    WHEN (
        SELECT count(*) FROM canonical_message_tags
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    ) >= 64
    BEGIN
        SELECT RAISE(ABORT, 'canonical tag count exceeds 64');
    END
    """,
    """
    CREATE TRIGGER schema_migrations_no_nul_insert
    BEFORE INSERT ON schema_migrations
    WHEN instr(NEW.migration_checksum, char(0)) != 0
      OR instr(NEW.applied_at_utc, char(0)) != 0
      OR instr(NEW.tool_version, char(0)) != 0
      OR instr(NEW.backup_reference, char(0)) != 0
    BEGIN
        SELECT RAISE(ABORT, 'schema migration metadata contains NUL');
    END
    """,
    """
    CREATE TRIGGER schema_migrations_no_nul_update
    BEFORE UPDATE ON schema_migrations
    WHEN instr(NEW.migration_checksum, char(0)) != 0
      OR instr(NEW.applied_at_utc, char(0)) != 0
      OR instr(NEW.tool_version, char(0)) != 0
      OR instr(NEW.backup_reference, char(0)) != 0
    BEGIN
        SELECT RAISE(ABORT, 'schema migration metadata contains NUL');
    END
    """,
)
V4_MIGRATION_CHECKSUM = "sha256:d45b8f84c18a93fe9fe69658794307ab6d451b06edef5245e8d4d7e305d247c6"
V4_SCHEMA_FINGERPRINT = "sha256:c5ed09f78345ba35338635db76cc6e24c528d19137acab301990a859157febff"
MIGRATIONS = ((1, V1_SQL), (2, V2_SQL), (3, V3_SQL), (4, V4_SQL))


def _migration_checksum(statements: Sequence[str]) -> str:
    encoded = "\x00".join(statements).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _v1_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in V1_SQL:
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _v2_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in (*V1_SQL, *V2_SQL):
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _v3_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL):
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _v4_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *V4_SQL):
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _OwnedWriterLock:
    """Own one flock fd and release it exactly once, including on abandonment."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class _PinnedFile:
    """Own one no-follow descriptor and its immutable file identity."""

    def __init__(self, fd: int, identity: tuple[int, int]) -> None:
        self._fd = fd
        self.identity = identity

    def fchmod(self, mode: int) -> None:
        if self._fd is None:
            raise SQLiteSafetyError("pinned SQLite file is closed")
        os.fchmod(self._fd, mode)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def _close_connection_and_pin(
    connection: sqlite3.Connection | None, pin: _PinnedFile | None
) -> None:
    try:
        if connection is not None:
            connection.close()
    finally:
        if pin is not None:
            pin.close()


def _stable_fd_record(fd: int, path_reader: Callable[[int], str]) -> tuple[int, int, int, str]:
    before = os.fstat(fd)
    reported_path = path_reader(fd)
    after = os.fstat(fd)
    before_identity = (before.st_dev, before.st_ino, before.st_mode)
    after_identity = (after.st_dev, after.st_ino, after.st_mode)
    if before_identity != after_identity or not reported_path:
        raise OSError("file descriptor changed during inspection")
    return before.st_dev, before.st_ino, before.st_mode, reported_path


def _linux_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    root = "/proc/self/fd"
    records = {}
    for name in os.listdir(root):
        try:
            fd = int(name)
            records[fd] = _stable_fd_record(fd, lambda value: os.readlink(f"{root}/{value}"))
        except (OSError, ValueError):
            continue
    return records


def _darwin_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    root = "/dev/fd"
    f_getpath = getattr(fcntl, "F_GETPATH", None)
    if f_getpath is None:
        raise SQLiteSafetyError("SQLite connection identity proof is unsupported: fcntl.F_GETPATH unavailable")

    def get_path(fd: int) -> str:
        value = fcntl.fcntl(fd, f_getpath, b"\0" * 1024)
        return value.split(b"\0", 1)[0].decode("utf-8")

    records = {}
    for name in os.listdir(root):
        try:
            fd = int(name)
            records[fd] = _stable_fd_record(fd, get_path)
        except (OSError, UnicodeError, ValueError):
            continue
    return records


def _connection_fd_snapshot() -> dict[int, tuple[int, int, int, str]]:
    if sys.platform.startswith("linux"):
        return _linux_fd_snapshot()
    if sys.platform == "darwin":
        return _darwin_fd_snapshot()
    raise SQLiteSafetyError(f"SQLite connection identity proof is unsupported on {sys.platform}")


def _reported_path_matches(reported_path: str, database: Path) -> bool:
    if reported_path.endswith(" (deleted)"):
        reported_path = reported_path[: -len(" (deleted)")]
    return os.path.realpath(reported_path) == os.path.realpath(database)


class LedgerStore:
    """One thread-bound writer/checkpointer or query-only reader connection."""

    def __init__(
        self,
        paths: LedgerPaths,
        connection: sqlite3.Connection,
        database_pin: _PinnedFile,
        *,
        read_only: bool,
        writer_lock: _OwnedWriterLock | None,
        clock: Callable[[], datetime],
    ) -> None:
        self.paths = paths
        self._connection = connection
        self._database_pin = database_pin
        self._read_only = read_only
        self._writer_lock = writer_lock
        self._clock = clock
        self._thread_id = threading.get_ident()
        self._closed = False

    @classmethod
    def open_writer(
        cls,
        paths: LedgerPaths,
        *,
        clock: Callable[[], datetime] = _utc_now,
        migrations: Sequence[tuple[int, Sequence[str]]] = MIGRATIONS,
    ) -> "LedgerStore":
        require_safe_sqlite()
        paths.ensure_directories()
        cls._preflight_sqlite_files(paths.ledger)
        writer_lock = cls._acquire_writer_lock(paths)
        connection = None
        database_pin = None
        try:
            connection, database_pin = cls._open_verified_connection(
                paths.ledger,
                read_only=False,
                create=True,
                timeout=BUSY_TIMEOUT_MS / 1_000,
            )
            cls._validate_schema_or_empty(connection, paths)
            paths.assert_contained()
            cls._configure(connection, writer=True)
            store = cls(
                paths,
                connection,
                database_pin,
                read_only=False,
                writer_lock=writer_lock,
                clock=clock,
            )
            store._secure_sqlite_files(paths.ledger, main_pin=database_pin)
            store._migrate(migrations)
            store._validate_schema(connection, paths)
            store._secure_sqlite_files(paths.ledger, main_pin=database_pin)
            return store
        except BaseException:
            try:
                _close_connection_and_pin(connection, database_pin)
            finally:
                writer_lock.close()
            raise

    @classmethod
    def open_reader(
        cls,
        paths: LedgerPaths,
    ) -> "LedgerStore":
        require_safe_sqlite()
        paths.assert_contained()
        cls._preflight_sqlite_files(paths.ledger)
        if not paths.ledger.is_file():
            raise FileNotFoundError(paths.ledger)
        connection, database_pin = cls._open_verified_connection(
            paths.ledger,
            read_only=True,
            timeout=BUSY_TIMEOUT_MS / 1_000,
        )
        try:
            cls._validate_schema(connection, paths)
            cls._configure(connection, writer=False)
            return cls(
                paths,
                connection,
                database_pin,
                read_only=True,
                writer_lock=None,
                clock=_utc_now,
            )
        except BaseException:
            _close_connection_and_pin(connection, database_pin)
            raise

    @staticmethod
    def _pin_regular_file(
        path: Path,
        *,
        writable: bool,
        create: bool = False,
        exclusive: bool = False,
    ) -> _PinnedFile:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise SQLiteSafetyError("O_NOFOLLOW is required for SQLite file safety")
        flags = (os.O_RDWR if writable else os.O_RDONLY) | nofollow
        flags |= getattr(os, "O_CLOEXEC", 0)
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        directory_flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            try:
                fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
            except IsADirectoryError as exc:
                raise SQLiteSafetyError(f"refusing non-regular SQLite file: {path}") from exc
        finally:
            os.close(directory_fd)
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                raise SQLiteSafetyError(f"refusing non-regular SQLite file: {path}")
            return _PinnedFile(fd, (status.st_dev, status.st_ino))
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def _open_verified_connection(
        cls,
        path: Path,
        *,
        read_only: bool,
        create: bool = False,
        exclusive: bool = False,
        timeout: float = BUSY_TIMEOUT_MS / 1_000,
    ) -> tuple[sqlite3.Connection, _PinnedFile]:
        pin = cls._pin_regular_file(
            path,
            writable=not read_only,
            create=create,
            exclusive=exclusive,
        )
        connection = None
        try:
            before = _connection_fd_snapshot()
            connection = sqlite3.connect(
                path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                uri=True,
                timeout=timeout,
                isolation_level=None,
            )
            after = _connection_fd_snapshot()
            opened_regular_files = [
                record
                for fd, record in after.items()
                if fd not in before
                and stat.S_ISREG(record[2])
                and _reported_path_matches(record[3], path)
            ]
            if len(opened_regular_files) != 1:
                raise SQLiteSafetyError(
                    "SQLite main-database descriptor proof is unavailable or ambiguous"
                )
            actual = opened_regular_files[0]
            if (actual[0], actual[1]) != pin.identity:
                raise SQLiteSafetyError("SQLite opened a different file than the no-follow pin")
            if not read_only:
                pin.fchmod(0o600)
            return connection, pin
        except BaseException:
            _close_connection_and_pin(connection, pin)
            raise

    @staticmethod
    def _acquire_writer_lock(paths: LedgerPaths) -> _OwnedWriterLock:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_fd = os.open(paths.lock, flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise WriterAlreadyOpenError(f"writer/checkpointer already owns {paths.ledger}") from exc
        return _OwnedWriterLock(lock_fd)

    @staticmethod
    def _configure(connection: sqlite3.Connection, *, writer: bool) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if writer:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise SQLiteSafetyError(f"SQLite refused required WAL mode: {mode}")
        elif connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise SQLiteSafetyError("ledger is not in required WAL mode")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA query_only = {'OFF' if writer else 'ON'}")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SQLiteSafetyError("SQLite refused required foreign_keys enforcement")
        if connection.execute("PRAGMA busy_timeout").fetchone()[0] != BUSY_TIMEOUT_MS:
            raise SQLiteSafetyError("SQLite refused the bounded busy timeout")
        if connection.execute("PRAGMA synchronous").fetchone()[0] != SYNCHRONOUS_FULL:
            raise SQLiteSafetyError("SQLite refused required synchronous FULL durability")
        if not writer and connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise SQLiteSafetyError("SQLite refused query-only reader mode")

    @classmethod
    def _validate_schema_or_empty(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Allow only a truly empty database to enter the migration path."""
        try:
            claimed = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = cls._table_names(connection)
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger is corrupt or unreadable") from exc
        if claimed == 0 and not tables:
            return
        if claimed == 1:
            cls._validate_released_v1(connection, paths)
            return
        if claimed == 2:
            cls._validate_released_v2(connection, paths)
            return
        if claimed == 3:
            cls._validate_released_v3(connection, paths)
            return
        cls._validate_schema(connection, paths)

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection, paths: LedgerPaths) -> None:
        """Require the exact latest schema; query-only readers never accept v1."""
        try:
            cls._validate_database_health(connection)
            claimed = connection.execute("PRAGMA user_version").fetchone()[0]
            if claimed != SCHEMA_VERSION:
                raise MigrationError(
                    f"unsupported ledger schema version {claimed}; expected {SCHEMA_VERSION}"
                )
            if _migration_checksum(V1_SQL) != V1_MIGRATION_CHECKSUM:
                raise MigrationError("released v1 migration checksum is incoherent")
            if _v1_schema_fingerprint_from_sql() != V1_SCHEMA_FINGERPRINT:
                raise MigrationError("released v1 schema fingerprint is incoherent")
            if _migration_checksum(V2_SQL) != V2_MIGRATION_CHECKSUM:
                raise MigrationError("released v2 migration checksum is incoherent")
            if _v2_schema_fingerprint_from_sql() != V2_SCHEMA_FINGERPRINT:
                raise MigrationError("released v2 schema fingerprint is incoherent")
            if _migration_checksum(V3_SQL) != V3_MIGRATION_CHECKSUM:
                raise MigrationError("released v3 migration checksum is incoherent")
            if _v3_schema_fingerprint_from_sql() != V3_SCHEMA_FINGERPRINT:
                raise MigrationError("released v3 schema fingerprint is incoherent")
            if _migration_checksum(V4_SQL) != V4_MIGRATION_CHECKSUM:
                raise MigrationError("released v4 migration checksum is incoherent")
            if _v4_schema_fingerprint_from_sql() != V4_SCHEMA_FINGERPRINT:
                raise MigrationError("released v4 schema fingerprint is incoherent")
            rows = cls._migration_rows(connection)
            if [row[0] for row in rows] != [1, 2, 3, 4]:
                raise MigrationError("ledger migration metadata is incoherent")
            cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
            cls._validate_migration_row(rows[1], V2_MIGRATION_CHECKSUM, 1, paths)
            cls._validate_migration_row(rows[2], V3_MIGRATION_CHECKSUM, 2, paths)
            cls._validate_migration_row(rows[3], V4_MIGRATION_CHECKSUM, 3, paths)
            actual_tables = cls._table_names(connection)
            if actual_tables != V4_TABLES:
                raise MigrationError(
                    "ledger v4 table set is incoherent: "
                    f"missing={sorted(V4_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V4_TABLES)}"
                )
            if _schema_fingerprint(connection) != V4_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v4 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    @classmethod
    def _validate_released_v3(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Accept only the exact released v3 long enough for a writer migration."""
        try:
            cls._validate_database_health(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 3:
                raise MigrationError("ledger is not released schema v3")
            released = (
                (V1_SQL, V1_MIGRATION_CHECKSUM, V1_SCHEMA_FINGERPRINT, _v1_schema_fingerprint_from_sql),
                (V2_SQL, V2_MIGRATION_CHECKSUM, V2_SCHEMA_FINGERPRINT, _v2_schema_fingerprint_from_sql),
                (V3_SQL, V3_MIGRATION_CHECKSUM, V3_SCHEMA_FINGERPRINT, _v3_schema_fingerprint_from_sql),
            )
            for statements, checksum, fingerprint, fingerprint_from_sql in released:
                if _migration_checksum(statements) != checksum:
                    raise MigrationError("released migration checksum is incoherent")
                if fingerprint_from_sql() != fingerprint:
                    raise MigrationError("released schema fingerprint is incoherent")
            rows = cls._migration_rows(connection)
            if [row[0] for row in rows] != [1, 2, 3]:
                raise MigrationError("ledger migration metadata is incoherent")
            cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
            cls._validate_migration_row(rows[1], V2_MIGRATION_CHECKSUM, 1, paths)
            cls._validate_migration_row(rows[2], V3_MIGRATION_CHECKSUM, 2, paths)
            actual_tables = cls._table_names(connection)
            if actual_tables != V3_TABLES:
                raise MigrationError(
                    "ledger v3 table set is incoherent: "
                    f"missing={sorted(V3_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V3_TABLES)}"
                )
            if _schema_fingerprint(connection) != V3_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v3 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    @classmethod
    def _validate_released_v2(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Accept only the exact released v2 long enough for a writer migration."""
        try:
            cls._validate_database_health(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise MigrationError("ledger is not released schema v2")
            if _migration_checksum(V1_SQL) != V1_MIGRATION_CHECKSUM:
                raise MigrationError("released v1 migration checksum is incoherent")
            if _v1_schema_fingerprint_from_sql() != V1_SCHEMA_FINGERPRINT:
                raise MigrationError("released v1 schema fingerprint is incoherent")
            if _migration_checksum(V2_SQL) != V2_MIGRATION_CHECKSUM:
                raise MigrationError("released v2 migration checksum is incoherent")
            if _v2_schema_fingerprint_from_sql() != V2_SCHEMA_FINGERPRINT:
                raise MigrationError("released v2 schema fingerprint is incoherent")
            rows = cls._migration_rows(connection)
            if [row[0] for row in rows] != [1, 2]:
                raise MigrationError("ledger migration metadata is incoherent")
            cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
            cls._validate_migration_row(rows[1], V2_MIGRATION_CHECKSUM, 1, paths)
            actual_tables = cls._table_names(connection)
            if actual_tables != V2_TABLES:
                raise MigrationError(
                    "ledger v2 table set is incoherent: "
                    f"missing={sorted(V2_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V2_TABLES)}"
                )
            if _schema_fingerprint(connection) != V2_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v2 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    @classmethod
    def _validate_released_v1(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Accept only the exact released v1 long enough for a writer migration."""
        try:
            cls._validate_database_health(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise MigrationError("ledger is not released schema v1")
            if _migration_checksum(V1_SQL) != V1_MIGRATION_CHECKSUM:
                raise MigrationError("released v1 migration checksum is incoherent")
            if _v1_schema_fingerprint_from_sql() != V1_SCHEMA_FINGERPRINT:
                raise MigrationError("released v1 schema fingerprint is incoherent")
            rows = cls._migration_rows(connection)
            if [row[0] for row in rows] != [1]:
                raise MigrationError("ledger migration metadata is incoherent")
            cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
            actual_tables = cls._table_names(connection)
            if actual_tables != V1_TABLES:
                raise MigrationError(
                    "ledger v1 table set is incoherent: "
                    f"missing={sorted(V1_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V1_TABLES)}"
                )
            if _schema_fingerprint(connection) != V1_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v1 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    @staticmethod
    def _validate_database_health(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("ledger failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MigrationError("ledger failed foreign_key_check")

    @staticmethod
    def _migration_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
        return connection.execute(
            "SELECT version, migration_checksum, applied_at_utc, tool_version, backup_reference "
            "FROM schema_migrations ORDER BY version"
        ).fetchall()

    @classmethod
    def _validate_migration_row(
        cls,
        row: tuple[object, ...],
        expected_checksum: str,
        prior_version: int,
        paths: LedgerPaths,
    ) -> None:
        _version, checksum, applied_at, tool_version, backup_reference = row
        backup_match = (
            re.fullmatch(
                rf"ledger-{prior_version}-(\d{{8}}T\d{{12}}Z)\.sqlite3",
                backup_reference,
            )
            if isinstance(backup_reference, str)
            else None
        )
        if (
            checksum != expected_checksum
            or not isinstance(applied_at, str)
            or not applied_at
            or tool_version != MIGRATION_TOOL_VERSION
            or backup_match is None
        ):
            raise MigrationError("ledger migration metadata is incoherent")
        backup_time = datetime.strptime(
            backup_match.group(1), "%Y%m%dT%H%M%S%fZ"
        ).replace(tzinfo=timezone.utc)
        if applied_at != backup_time.isoformat():
            raise MigrationError("ledger migration metadata is incoherent")
        backup = paths.backups / backup_reference
        if backup.is_symlink() or not backup.is_file():
            raise MigrationError("ledger migration backup reference is incoherent")
        cls._verify_database(backup)

    def _migrate(self, migrations: Sequence[tuple[int, Sequence[str]]]) -> None:
        current = self.schema_version()
        for version, statements in migrations:
            if version <= current:
                continue
            if version != current + 1:
                raise MigrationError(f"migration gap: expected {current + 1}, got {version}")
            backup = self._backup_before_migration(current)
            backup_stamp = re.fullmatch(
                r"ledger-\d+-(\d{8}T\d{12}Z)\.sqlite3", backup.name
            )
            if backup_stamp is None:
                raise MigrationError("generated migration backup identity is incoherent")
            applied_at = datetime.strptime(
                backup_stamp.group(1), "%Y%m%dT%H%M%S%fZ"
            ).replace(tzinfo=timezone.utc).isoformat()
            try:
                checksum = _migration_checksum(statements)
                expected_checksum = {
                    1: V1_MIGRATION_CHECKSUM,
                    2: V2_MIGRATION_CHECKSUM,
                    3: V3_MIGRATION_CHECKSUM,
                    4: V4_MIGRATION_CHECKSUM,
                }.get(version)
                if expected_checksum is None or checksum != expected_checksum:
                    raise MigrationError(f"migration {version} does not match its released checksum")
                self._connection.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations"
                    "(version, migration_checksum, applied_at_utc, tool_version, backup_reference) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (version, checksum, applied_at, MIGRATION_TOOL_VERSION, backup.name),
                )
                self._connection.execute(f"PRAGMA user_version = {version}")
                if self._connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MigrationError(f"migration {version} failed integrity_check")
                if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise MigrationError(f"migration {version} failed foreign_key_check")
                expected_fingerprint = {
                    1: V1_SCHEMA_FINGERPRINT,
                    2: V2_SCHEMA_FINGERPRINT,
                    3: V3_SCHEMA_FINGERPRINT,
                    4: V4_SCHEMA_FINGERPRINT,
                }[version]
                if _schema_fingerprint(self._connection) != expected_fingerprint:
                    raise MigrationError(f"migration {version} produced an incoherent schema")
                self._connection.execute("COMMIT")
            except BaseException as exc:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                self._restore_from_backup(backup)
                raise MigrationError(f"migration {version} failed; the verified backup was restored") from exc
            current = version

    @staticmethod
    def _sqlite_files(database: Path) -> tuple[Path, Path, Path, Path]:
        return (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
            database.with_name(database.name + "-journal"),
        )

    @classmethod
    def _preflight_sqlite_files(cls, database: Path) -> None:
        for path in cls._sqlite_files(database):
            if path.is_symlink():
                raise SQLiteSafetyError(f"refusing symlinked SQLite artifact: {path}")

    @classmethod
    def _secure_sqlite_files(
        cls, database: Path, *, main_pin: _PinnedFile | None = None
    ) -> None:
        for path in cls._sqlite_files(database):
            try:
                pin = cls._pin_regular_file(path, writable=True)
            except FileNotFoundError:
                continue
            try:
                if path == database and main_pin is not None and pin.identity != main_pin.identity:
                    raise SQLiteSafetyError("SQLite database pathname no longer matches its pin")
                pin.fchmod(0o600)
            finally:
                pin.close()

    def _backup_before_migration(self, schema_version: int) -> Path:
        if self._connection.in_transaction:
            raise MigrationError("refusing backup while a database transaction is active")
        stamp = self._clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.paths.backup_path(schema_version, stamp)
        self._preflight_sqlite_files(backup)
        destination, destination_pin = self._open_verified_connection(
            backup,
            read_only=False,
            create=True,
            exclusive=True,
        )
        try:
            self._connection.backup(destination)
            destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination.execute("PRAGMA journal_mode = DELETE")
        finally:
            _close_connection_and_pin(destination, destination_pin)
        self._secure_sqlite_files(backup)
        self._verify_database(backup)
        return backup

    def _restore_from_backup(self, backup: Path) -> None:
        if self._connection.in_transaction:
            raise MigrationError("refusing restore while a database transaction is active")
        source, source_pin = self._open_verified_connection(backup, read_only=True)
        try:
            source.backup(self._connection)
        finally:
            _close_connection_and_pin(source, source_pin)
        self._database_pin.fchmod(0o600)
        self._secure_sqlite_files(self.paths.ledger, main_pin=self._database_pin)
        if self.integrity_check() != "ok":
            raise MigrationError("restored ledger failed integrity_check")

    @classmethod
    def _verify_database(cls, path: Path) -> None:
        connection, pin = cls._open_verified_connection(path, read_only=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_failure = connection.execute("PRAGMA foreign_key_check").fetchone()
        finally:
            _close_connection_and_pin(connection, pin)
        if result != "ok":
            raise MigrationError(f"backup failed integrity_check: {result}")
        if foreign_key_failure is not None:
            raise MigrationError(f"backup failed foreign_key_check: {foreign_key_failure}")

    def _ensure_thread(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise sqlite3.ProgrammingError("ledger connections may not be reused across threads")
        if self._closed:
            raise sqlite3.ProgrammingError("ledger connection is closed")

    def schema_version(self) -> int:
        self._ensure_thread()
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def integrity_check(self) -> str:
        self._ensure_thread()
        return self._connection.execute("PRAGMA integrity_check").fetchone()[0]

    def canonical_preflight(self, *, write: bool) -> None:
        """Reject an unusable canonical operation before normalization or SQL work."""
        self._ensure_thread()
        if self._connection.in_transaction:
            raise RuntimeError("canonical operations require no open transaction")
        if write and self._read_only:
            raise PermissionError("query-only readers cannot create canonical messages")

    def read_canonical_message(
        self,
        *,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
    ) -> dict[str, object] | None:
        """Read one message and body only through its exact scope tuple."""
        self.canonical_preflight(write=False)
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        return self._read_canonical_message(
            workspace_id, scope_kind, scope_identity, message_id
        )

    def create_canonical_message(
        self,
        *,
        message: Mapping[str, object],
        recipients: Sequence[str],
        artifacts: Sequence[tuple[str, str]],
        tags: Sequence[str],
        body: bytes,
    ) -> bool:
        """Atomically append one normalized canonical intent, or return its equivalent."""
        self.canonical_preflight(write=True)
        workspace_id = str(message["workspace_id"])
        scope_kind = str(message["scope_kind"])
        scope_identity = str(message["scope_identity"])
        message_id = str(message["message_id"])
        sender_agent_id = str(message["sender_agent_id"])
        dedupe_key = str(message["dedupe_key"])
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)

        candidate_rows = self._connection.execute(
            "SELECT workspace_id, scope_kind, scope_identity, message_id "
            "FROM canonical_messages WHERE workspace_id = ? AND "
            "(message_id = ? OR (scope_kind = ? AND scope_identity = ? "
            "AND sender_agent_id = ? AND dedupe_key = ?))",
            (
                workspace_id,
                message_id,
                scope_kind,
                scope_identity,
                sender_agent_id,
                dedupe_key,
            ),
        ).fetchall()
        equivalent = {
            "workspace_id": workspace_id,
            "scope_kind": scope_kind,
            "scope_identity": scope_identity,
            "message_id": message_id,
            "sender_agent_id": sender_agent_id,
            "dedupe_key": dedupe_key,
            "body_sha256": message["body_sha256"],
            "reply_to_message_id": message["reply_to_message_id"],
            "ttl_seconds": message["ttl_seconds"],
            "ack_policy": message["ack_policy"],
            "title": message["title"],
            "priority": message["priority"],
            "chat_link": message["chat_link"],
            "task_link": message["task_link"],
            "recipients": tuple(recipients),
            "artifacts": tuple(artifacts),
            "tags": tuple(tags),
            "byte_size": len(body),
            "body": body,
        }
        for row in candidate_rows:
            existing = self._read_canonical_message(*row)
            if existing is not None and all(
                existing[key] == value for key, value in equivalent.items()
            ):
                return False
            raise CanonicalConflictError(
                "canonical message identity or dedupe namespace conflicts with different intent"
            )

        body_row = self._connection.execute(
            "SELECT byte_size, body FROM canonical_bodies "
            "WHERE workspace_id = ? AND body_sha256 = ?",
            (workspace_id, message["body_sha256"]),
        ).fetchone()
        if body_row is not None and body_row != (len(body), body):
            raise CanonicalConflictError("canonical body hash conflicts with different bytes")

        project_id = message["project_id"]
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if body_row is None:
                self._connection.execute(
                    "INSERT INTO canonical_bodies "
                    "(workspace_id, body_sha256, byte_size, body, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        message["body_sha256"],
                        len(body),
                        body,
                        message["created_at_utc"],
                    ),
                )
            self._connection.execute(
                "INSERT INTO canonical_messages "
                "(workspace_id, scope_kind, scope_identity, message_id, sender_agent_id, "
                "dedupe_key, body_sha256, reply_to_message_id, ttl_seconds, ack_policy, "
                "title, priority, chat_link, task_link, registry_revision, project_id, "
                "created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    scope_kind,
                    scope_identity,
                    message_id,
                    sender_agent_id,
                    dedupe_key,
                    message["body_sha256"],
                    message["reply_to_message_id"],
                    message["ttl_seconds"],
                    message["ack_policy"],
                    message["title"],
                    message["priority"],
                    message["chat_link"],
                    message["task_link"],
                    message["registry_revision"],
                    project_id,
                    message["created_at_utc"],
                ),
            )
            prefix = (workspace_id, scope_kind, scope_identity, message_id)
            self._connection.executemany(
                "INSERT INTO canonical_message_recipients "
                "(workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ((*prefix, recipient) for recipient in recipients),
            )
            self._connection.executemany(
                "INSERT INTO canonical_message_artifacts "
                "(workspace_id, scope_kind, scope_identity, message_id, artifact_kind, "
                "artifact_ref) VALUES (?, ?, ?, ?, ?, ?)",
                ((*prefix, kind, reference) for kind, reference in artifacts),
            )
            self._connection.executemany(
                "INSERT INTO canonical_message_tags "
                "(workspace_id, scope_kind, scope_identity, message_id, tag) "
                "VALUES (?, ?, ?, ?, ?)",
                ((*prefix, tag) for tag in tags),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return True

    def _read_canonical_message(
        self,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
    ) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT m.workspace_id, m.scope_kind, m.scope_identity, m.message_id, "
            "m.sender_agent_id, m.dedupe_key, m.body_sha256, m.reply_to_message_id, "
            "m.ttl_seconds, m.ack_policy, m.title, m.priority, m.chat_link, m.task_link, "
            "m.registry_revision, m.project_id, m.created_at_utc, b.byte_size, b.body "
            "FROM canonical_messages AS m JOIN canonical_bodies AS b "
            "ON b.workspace_id = m.workspace_id AND b.body_sha256 = m.body_sha256 "
            "WHERE m.workspace_id = ? AND m.scope_kind = ? AND m.scope_identity = ? "
            "AND m.message_id = ?",
            (workspace_id, scope_kind, scope_identity, message_id),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "workspace_id",
            "scope_kind",
            "scope_identity",
            "message_id",
            "sender_agent_id",
            "dedupe_key",
            "body_sha256",
            "reply_to_message_id",
            "ttl_seconds",
            "ack_policy",
            "title",
            "priority",
            "chat_link",
            "task_link",
            "registry_revision",
            "project_id",
            "created_at_utc",
            "byte_size",
            "body",
        )
        result = dict(zip(keys, row))
        prefix = (workspace_id, scope_kind, scope_identity, message_id)
        result["recipients"] = tuple(
            item[0]
            for item in self._connection.execute(
                "SELECT recipient_agent_id FROM canonical_message_recipients "
                "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
                "AND message_id = ? ORDER BY recipient_agent_id",
                prefix,
            )
        )
        result["artifacts"] = tuple(
            (item[0], item[1])
            for item in self._connection.execute(
                "SELECT artifact_kind, artifact_ref FROM canonical_message_artifacts "
                "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
                "AND message_id = ? ORDER BY artifact_kind, artifact_ref",
                prefix,
            )
        )
        result["tags"] = tuple(
            item[0]
            for item in self._connection.execute(
                "SELECT tag FROM canonical_message_tags WHERE workspace_id = ? "
                "AND scope_kind = ? AND scope_identity = ? AND message_id = ? ORDER BY tag",
                prefix,
            )
        )
        return result

    def _validate_canonical_scope(
        self, workspace_id: str, scope_kind: str, scope_identity: str
    ) -> None:
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if scope_kind == "workspace":
            if scope_identity != "workspace":
                raise ValueError("workspace scope identity must be workspace")
        elif scope_kind == "project":
            validate_project_id(scope_identity)
        else:
            raise ValueError("scope_kind must be workspace or project")

    def record_registry_snapshot(
        self,
        *,
        workspace_id: str,
        registry_revision: str,
        registry_source_sha256: str,
        captured_at_utc: str,
        workspace_snapshot_json: str,
        project_snapshots: Mapping[str, str],
        source_snapshots: Mapping[str, Mapping[str, str]],
    ) -> None:
        """Persist one immutable, fully scoped registry snapshot."""
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot write registry snapshots")
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", registry_revision):
            raise ValueError("registry_revision must be sha256:<lowercase hex>")
        if registry_revision != f"sha256:{registry_source_sha256}":
            raise ValueError("registry revision and exact-source hash differ")
        if not isinstance(captured_at_utc, str) or not captured_at_utc:
            raise ValueError("captured_at_utc is required")
        workspace_snapshot = self._require_json_object(
            workspace_snapshot_json, "workspace snapshot"
        )
        if workspace_snapshot.get("workspace_id") != workspace_id:
            raise ValueError("workspace snapshot workspace_id does not match this ledger")
        project_entries = workspace_snapshot.get("projects")
        if not isinstance(project_entries, list) or not project_entries:
            raise ValueError("workspace snapshot projects must be a non-empty list")
        workspace_project_ids = []
        for entry in project_entries:
            workspace_project_ids.append(
                self._registry_project_identity(
                    entry, "workspace snapshot project", allow_string=True
                )
            )
        if len(workspace_project_ids) != len(set(workspace_project_ids)):
            raise ValueError("workspace snapshot projects must be duplicate-free")
        if set(workspace_project_ids) != set(project_snapshots):
            raise ValueError("workspace snapshot projects do not match project snapshots")
        projects = []
        for project_id, snapshot in project_snapshots.items():
            validate_project_id(project_id)
            project_snapshot = self._require_json_object(
                snapshot, f"project {project_id} snapshot"
            )
            if self._registry_project_identity(
                project_snapshot, f"project {project_id} snapshot", allow_string=False
            ) != project_id:
                raise ValueError(f"project {project_id} snapshot identity does not match its key")
            projects.append((workspace_id, project_id, registry_revision, snapshot))
        sources = []
        for project_id, project_sources in source_snapshots.items():
            validate_project_id(project_id)
            if project_id not in project_snapshots:
                raise ValueError(f"source snapshot project {project_id!r} is absent from this revision")
            for source_id, snapshot in project_sources.items():
                validate_registry_token(source_id, "source_id")
                self._require_json_object(snapshot, f"source {source_id} snapshot")
                sources.append((workspace_id, project_id, source_id, registry_revision, snapshot))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "INSERT INTO workspace_registry_snapshots "
                "(workspace_id, registry_revision, registry_source_sha256, captured_at_utc, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    registry_revision,
                    registry_source_sha256,
                    captured_at_utc,
                    workspace_snapshot_json,
                ),
            )
            self._connection.executemany(
                "INSERT INTO project_registry_snapshots "
                "(workspace_id, project_id, registry_revision, snapshot_json) VALUES (?, ?, ?, ?)",
                projects,
            )
            self._connection.executemany(
                "INSERT INTO observation_source_registry_snapshots "
                "(workspace_id, project_id, source_id, registry_revision, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?)",
                sources,
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _require_json_object(serialized: str, name: str) -> dict[str, object]:
        try:
            parsed = json.loads(serialized)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{name} must be serialized JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{name} must serialize one object")
        return parsed

    @staticmethod
    def _registry_project_identity(
        value: object, name: str, *, allow_string: bool
    ) -> str:
        if allow_string and isinstance(value, str):
            return validate_project_id(value)
        if not isinstance(value, dict):
            raise ValueError(f"{name} must carry an exact project identity")
        aliases = [value[key] for key in ("project_id", "id") if key in value]
        if not aliases or any(alias != aliases[0] for alias in aliases[1:]):
            raise ValueError(f"{name} has missing or conflicting project identity aliases")
        return validate_project_id(aliases[0])

    def get_project_snapshot(
        self,
        *,
        workspace_id: str,
        project_id: str,
        registry_revision: str,
    ) -> dict[str, str] | None:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        row = self._connection.execute(
            "SELECT workspace_id, project_id, registry_revision, snapshot_json "
            "FROM project_registry_snapshots "
            "WHERE workspace_id = ? AND project_id = ? AND registry_revision = ?",
            (workspace_id, project_id, registry_revision),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(("workspace_id", "project_id", "registry_revision", "snapshot_json"), row))

    def get_source_snapshots(
        self,
        *,
        workspace_id: str,
        project_id: str,
        registry_revision: str,
    ) -> list[dict[str, str]]:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        rows = self._connection.execute(
            "SELECT workspace_id, project_id, source_id, registry_revision, snapshot_json "
            "FROM observation_source_registry_snapshots "
            "WHERE workspace_id = ? AND project_id = ? AND registry_revision = ? ORDER BY source_id",
            (workspace_id, project_id, registry_revision),
        ).fetchall()
        keys = ("workspace_id", "project_id", "source_id", "registry_revision", "snapshot_json")
        return [dict(zip(keys, row)) for row in rows]

    def has_registry_snapshot(self, *, workspace_id: str, registry_revision: str) -> bool:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        return (
            self._connection.execute(
                "SELECT 1 FROM workspace_registry_snapshots "
                "WHERE workspace_id = ? AND registry_revision = ?",
                (workspace_id, registry_revision),
            ).fetchone()
            is not None
        )

    def registered_project_ids(
        self, *, workspace_id: str, registry_revision: str
    ) -> frozenset[str]:
        """Return the exact registered projects for one immutable registry snapshot."""
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", registry_revision):
            raise ValueError("registry_revision must be sha256:<lowercase hex>")
        if not self.has_registry_snapshot(
            workspace_id=workspace_id, registry_revision=registry_revision
        ):
            raise ValueError("registry snapshot is absent")
        rows = self._connection.execute(
            "SELECT project_id FROM project_registry_snapshots "
            "WHERE workspace_id = ? AND registry_revision = ? ORDER BY project_id",
            (workspace_id, registry_revision),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def legacy_import_preflight(
        self, *, workspace_id: str, registry_revision: str
    ) -> frozenset[str]:
        """Prove writer and transaction readiness before any legacy filesystem read."""
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot import legacy provenance")
        if self._connection.in_transaction:
            raise RuntimeError("refusing legacy import while a transaction is active")
        return self.registered_project_ids(
            workspace_id=workspace_id, registry_revision=registry_revision
        )

    def import_legacy_provenance(
        self,
        *,
        workspace_id: str,
        registry_revision: str,
        import_transaction_id: str,
        import_revision: str,
        imported_at_utc: str,
        records: Sequence[Mapping[str, object]],
        failpoint: Callable[[str], None] | None = None,
    ) -> int:
        """Atomically append one already-collected hash-only provenance batch."""
        projects = self.legacy_import_preflight(
            workspace_id=workspace_id, registry_revision=registry_revision
        )
        if re.fullmatch(r"[0-9a-f]{32}", import_transaction_id) is None:
            raise ValueError("import_transaction_id must be 32 lowercase hexadecimal characters")
        if import_revision != "legacy-provenance/1":
            raise ValueError("import_revision must be legacy-provenance/1")
        if (
            not isinstance(imported_at_utc, str)
            or not imported_at_utc
            or "\x00" in imported_at_utc
            or len(imported_at_utc.encode("utf-8")) > 128
        ):
            raise ValueError("imported_at_utc must be bounded text")
        if len(records) > 5_000:
            raise ValueError("one import may contain at most 5000 records")

        required = {
            "source_family",
            "record_kind",
            "source_locator",
            "content_sha256",
            "byte_size",
            "observed_at_utc",
            "scope_kind",
            "project_id",
        }
        normalized = []
        prefixes = {
            "session": "State/session_autobridge/sessions/",
            "activation_lease": "State/session_autobridge/activation_leases/",
        }
        for record in records:
            if not isinstance(record, Mapping) or set(record) != required:
                raise ValueError("legacy provenance record has an invalid field set")
            if record["source_family"] != "session_autobridge":
                raise ValueError("legacy provenance source family is closed")
            record_kind = record["record_kind"]
            if record_kind not in prefixes:
                raise ValueError("legacy provenance record kind is closed")
            locator = record["source_locator"]
            prefix = prefixes[record_kind]
            if not isinstance(locator, str):
                raise ValueError("legacy provenance source locator must be text")
            filename = locator[len(prefix) :] if locator.startswith(prefix) else ""
            if (
                not filename.endswith(".json")
                or "/" in filename
                or "\\" in filename
                or "\x00" in locator
                or len(locator.encode("utf-8")) > 4_096
            ):
                raise ValueError("legacy provenance source locator is outside the closed set")
            content_sha256 = self._require_lower_hex(
                record["content_sha256"], "content_sha256"
            )
            byte_size = record["byte_size"]
            if (
                isinstance(byte_size, bool)
                or not isinstance(byte_size, int)
                or not 0 <= byte_size <= 1_048_576
            ):
                raise ValueError("legacy provenance byte_size is outside its fixed bound")
            observed_at_utc = record["observed_at_utc"]
            if (
                not isinstance(observed_at_utc, str)
                or not observed_at_utc
                or "\x00" in observed_at_utc
                or len(observed_at_utc.encode("utf-8")) > 128
            ):
                raise ValueError("observed_at_utc must be bounded text")
            scope_kind = record["scope_kind"]
            project_id = record["project_id"]
            if scope_kind == "exact_project":
                if not isinstance(project_id, str) or project_id not in projects:
                    raise ValueError("exact-project provenance must bind a registered project")
                scope_identity = project_id
            elif scope_kind == "legacy_unscoped":
                if project_id is not None:
                    raise ValueError("legacy-unscoped provenance cannot carry a project")
                scope_identity = "legacy_unscoped"
            else:
                raise ValueError("legacy provenance scope kind is closed")
            normalized.append(
                (
                    workspace_id,
                    registry_revision,
                    scope_kind,
                    scope_identity,
                    project_id,
                    "session_autobridge",
                    record_kind,
                    locator,
                    content_sha256,
                    byte_size,
                    observed_at_utc,
                    imported_at_utc,
                    import_transaction_id,
                    import_revision,
                )
            )

        inserted = 0
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for values in normalized:
                inserted += self._connection.execute(
                    "INSERT INTO legacy_provenance_imports "
                    "(workspace_id, registry_revision, scope_kind, scope_identity, project_id, "
                    "source_family, record_kind, source_locator, content_sha256, byte_size, "
                    "observed_at_utc, imported_at_utc, import_transaction_id, import_revision) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    values,
                ).rowcount
            if failpoint is not None:
                failpoint("after_provenance")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return inserted

    def get_legacy_provenance(
        self,
        *,
        workspace_id: str,
        project_id: str,
        registry_revision: str,
    ) -> list[dict[str, object]]:
        """Return exact-project provenance; legacy-unscoped rows are never projected."""
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        rows = self._connection.execute(
            "SELECT source_family, record_kind, source_locator, content_sha256, byte_size, "
            "observed_at_utc, imported_at_utc, import_transaction_id, import_revision "
            "FROM legacy_provenance_imports WHERE workspace_id = ? AND project_id = ? "
            "AND registry_revision = ? AND scope_kind = 'exact_project' "
            "ORDER BY record_kind, source_locator, content_sha256",
            (workspace_id, project_id, registry_revision),
        ).fetchall()
        keys = (
            "source_family",
            "record_kind",
            "source_locator",
            "content_sha256",
            "byte_size",
            "observed_at_utc",
            "imported_at_utc",
            "import_transaction_id",
            "import_revision",
        )
        return [dict(zip(keys, row)) for row in rows]

    def observation_checkpoint_cursor(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_id: str,
        registry_revision: str,
    ) -> str:
        self._ensure_thread()
        self._validate_observation_scope(
            workspace_id, project_id, source_id, registry_revision
        )
        row = self._connection.execute(
            "SELECT cursor FROM observation_checkpoints "
            "WHERE workspace_id = ? AND project_id = ? AND source_id = ? "
            "AND registry_revision = ?",
            (workspace_id, project_id, source_id, registry_revision),
        ).fetchone()
        return "" if row is None else row[0]

    def reconcile_observations(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_id: str,
        registry_revision: str,
        candidates: Sequence[Mapping[str, object]],
        next_cursor: str,
        scanned_count: int,
        observed_at_utc: str,
        write_limit: int = 500,
        failpoint: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """Atomically dedupe observations, advance one scoped cursor, and audit."""
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot reconcile observations")
        self._validate_observation_scope(
            workspace_id, project_id, source_id, registry_revision
        )
        if (
            not isinstance(observed_at_utc, str)
            or not observed_at_utc
            or "\x00" in observed_at_utc
        ):
            raise ValueError("observed_at_utc is required")
        if (
            not isinstance(next_cursor, str)
            or "\x00" in next_cursor
            or len(next_cursor.encode("utf-8")) > 4096
        ):
            raise ValueError("checkpoint cursor must be bounded text")
        if (
            isinstance(scanned_count, bool)
            or not isinstance(scanned_count, int)
            or not 0 <= scanned_count <= 2_000
        ):
            raise ValueError("one reconciliation may scan at most 2000 source entries")
        if len(candidates) > scanned_count:
            raise ValueError("observation candidates exceed the scanned source count")
        if (
            isinstance(write_limit, bool)
            or not isinstance(write_limit, int)
            or not 1 <= write_limit <= 500
        ):
            raise ValueError("one reconciliation may write at most 500 new observations")
        normalized = [self._validate_observation_candidate(item) for item in candidates]

        processed = scanned_count
        written = 0
        cursor = next_cursor
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for candidate in normalized:
                result = self._connection.execute(
                    "INSERT INTO observations "
                    "(workspace_id, project_id, source_id, registry_revision, dedupe_key, path, "
                    "content_sha256, byte_size, mtime_ns, resolution_state, observed_at_utc, "
                    "resolved_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved', ?, NULL) "
                    "ON CONFLICT (workspace_id, project_id, source_id, registry_revision, dedupe_key) "
                    "DO NOTHING",
                    (
                        workspace_id,
                        project_id,
                        source_id,
                        registry_revision,
                        candidate["dedupe_key"],
                        candidate["path"],
                        candidate["content_sha256"],
                        candidate["byte_size"],
                        candidate["mtime_ns"],
                        observed_at_utc,
                    ),
                )
                written += result.rowcount
                if written == write_limit:
                    cursor = candidate["scan_cursor"]
                    processed = candidate["scan_count"]
                    break
            if failpoint is not None:
                failpoint("after_observations")
            self._connection.execute(
                "INSERT INTO observation_checkpoints "
                "(workspace_id, project_id, source_id, registry_revision, cursor, scanned_count, "
                "written_count, updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, project_id, source_id, registry_revision) DO UPDATE SET "
                "cursor = excluded.cursor, scanned_count = excluded.scanned_count, "
                "written_count = excluded.written_count, updated_at_utc = excluded.updated_at_utc",
                (
                    workspace_id,
                    project_id,
                    source_id,
                    registry_revision,
                    cursor,
                    processed,
                    written,
                    observed_at_utc,
                ),
            )
            if failpoint is not None:
                failpoint("after_checkpoint")
            self._insert_observation_audit(
                workspace_id=workspace_id,
                project_id=project_id,
                source_id=source_id,
                registry_revision=registry_revision,
                action="reconcile",
                occurred_at_utc=observed_at_utc,
                detail={
                    "cursor_bytes": len(cursor.encode("utf-8")),
                    "cursor_incomplete": bool(cursor),
                    "cursor_sha256": hashlib.sha256(cursor.encode("utf-8")).hexdigest(),
                    "scanned": processed,
                    "written": written,
                },
            )
            if failpoint is not None:
                failpoint("after_audit")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return {"cursor": cursor, "scanned": processed, "written": written}

    def resolve_observation(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_id: str,
        registry_revision: str,
        dedupe_key: str,
        resolved_at_utc: str,
    ) -> bool:
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot resolve observations")
        self._validate_observation_scope(
            workspace_id, project_id, source_id, registry_revision
        )
        self._require_lower_hex(dedupe_key, "dedupe_key")
        if (
            not isinstance(resolved_at_utc, str)
            or not resolved_at_utc
            or "\x00" in resolved_at_utc
        ):
            raise ValueError("resolved_at_utc is required")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            changed = self._connection.execute(
                "UPDATE observations SET resolution_state = 'resolved', resolved_at_utc = ? "
                "WHERE workspace_id = ? AND project_id = ? AND source_id = ? "
                "AND registry_revision = ? AND dedupe_key = ? AND resolution_state = 'unresolved'",
                (
                    resolved_at_utc,
                    workspace_id,
                    project_id,
                    source_id,
                    registry_revision,
                    dedupe_key,
                ),
            ).rowcount
            if changed:
                self._insert_observation_audit(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    source_id=source_id,
                    registry_revision=registry_revision,
                    action="resolve",
                    occurred_at_utc=resolved_at_utc,
                    detail={"dedupe_key": dedupe_key},
                )
            self._connection.execute("COMMIT")
            return bool(changed)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def prune_resolved_observations(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_id: str,
        registry_revision: str,
        resolved_before_utc: str,
        occurred_at_utc: str,
        limit: int = 500,
    ) -> int:
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot prune observations")
        self._validate_observation_scope(
            workspace_id, project_id, source_id, registry_revision
        )
        if not isinstance(resolved_before_utc, str) or not resolved_before_utc:
            raise ValueError("resolved_before_utc is required")
        if (
            not isinstance(occurred_at_utc, str)
            or not occurred_at_utc
            or "\x00" in occurred_at_utc
        ):
            raise ValueError("occurred_at_utc is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("retention limit must be between 1 and 500")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            removed = self._connection.execute(
                "DELETE FROM observations WHERE rowid IN ("
                "SELECT rowid FROM observations WHERE workspace_id = ? AND project_id = ? "
                "AND source_id = ? AND registry_revision = ? AND resolution_state = 'resolved' "
                "AND resolved_at_utc < ? ORDER BY resolved_at_utc LIMIT ?)",
                (
                    workspace_id,
                    project_id,
                    source_id,
                    registry_revision,
                    resolved_before_utc,
                    limit,
                ),
            ).rowcount
            self._insert_observation_audit(
                workspace_id=workspace_id,
                project_id=project_id,
                source_id=source_id,
                registry_revision=registry_revision,
                action="retention",
                occurred_at_utc=occurred_at_utc,
                detail={"removed": removed},
            )
            self._connection.execute("COMMIT")
            return removed
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def observation_diagnostics(
        self,
        *,
        workspace_id: str,
        integrity: str | None = None,
        group_limit: int = 50,
        audit_limit: int = 200,
    ) -> dict[str, object]:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if group_limit != 50 or audit_limit != 200:
            raise ValueError("diagnostic limits are fixed at 50 groups and 200 audit rows")
        group_rows = self._connection.execute(
            "SELECT project_id, source_id, registry_revision, resolution_state, count(*) "
            "FROM observations WHERE workspace_id = ? "
            "GROUP BY project_id, source_id, registry_revision, resolution_state "
            "ORDER BY project_id, source_id, registry_revision, resolution_state LIMIT 51",
            (workspace_id,),
        ).fetchall()
        audit_rows = self._connection.execute(
            "SELECT audit_id, project_id, source_id, registry_revision, action, result, "
            "occurred_at_utc, detail_json FROM observation_audit WHERE workspace_id = ? "
            "ORDER BY occurred_at_utc DESC, project_id, source_id, registry_revision, "
            "audit_id DESC LIMIT 201",
            (workspace_id,),
        ).fetchall()
        group_keys = (
            "project_id",
            "source_id",
            "registry_revision",
            "resolution_state",
            "count",
        )
        audit_keys = (
            "audit_id",
            "project_id",
            "source_id",
            "registry_revision",
            "action",
            "result",
            "occurred_at_utc",
            "detail_json",
        )
        groups, groups_byte_truncated = self._bounded_diagnostic_rows(
            group_rows[:50], group_keys, byte_limit=12 * 1024
        )
        audit, audit_byte_truncated = self._bounded_diagnostic_rows(
            audit_rows[:200], audit_keys, byte_limit=24 * 1024
        )
        integrity_result = self.integrity_check() if integrity is None else integrity
        return {
            "schema_version": self.schema_version(),
            "integrity": integrity_result,
            "groups": groups,
            "groups_returned": len(groups),
            "groups_truncated": len(group_rows) > 50 or groups_byte_truncated,
            "audit": audit,
            "audit_returned": len(audit),
            "audit_truncated": len(audit_rows) > 200 or audit_byte_truncated,
        }

    @staticmethod
    def _bounded_diagnostic_rows(
        rows: Sequence[Sequence[object]],
        keys: Sequence[str],
        *,
        byte_limit: int,
    ) -> tuple[list[dict[str, object]], bool]:
        result: list[dict[str, object]] = []
        used = 2
        for row in rows:
            item = dict(zip(keys, row))
            encoded = json.dumps(
                item, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            additional = len(encoded) + (1 if result else 0)
            if used + additional > byte_limit:
                return result, True
            result.append(item)
            used += additional
        return result, False

    def _validate_observation_scope(
        self,
        workspace_id: str,
        project_id: str,
        source_id: str,
        registry_revision: str,
    ) -> None:
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        if source_id != "chats_mailbox":
            raise ValueError("source_id must be chats_mailbox")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", registry_revision):
            raise ValueError("registry_revision must be sha256:<lowercase hex>")

    def _insert_observation_audit(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_id: str,
        registry_revision: str,
        action: str,
        occurred_at_utc: str,
        detail: Mapping[str, object],
    ) -> None:
        """Append bounded metadata only; callers cannot supply raw source content."""
        if action == "reconcile":
            if set(detail) != {
                "cursor_bytes",
                "cursor_incomplete",
                "cursor_sha256",
                "scanned",
                "written",
            }:
                raise ValueError("reconcile audit detail has an invalid field set")
            cursor_bytes = detail["cursor_bytes"]
            cursor_incomplete = detail["cursor_incomplete"]
            cursor_sha256 = detail["cursor_sha256"]
            scanned = detail["scanned"]
            written = detail["written"]
            if type(cursor_bytes) is not int or not 0 <= cursor_bytes <= 4_096:
                raise ValueError("reconcile audit cursor length is invalid")
            if type(cursor_incomplete) is not bool:
                raise ValueError("reconcile audit cursor state is invalid")
            self._require_lower_hex(cursor_sha256, "cursor_sha256")
            if type(scanned) is not int or not 0 <= scanned <= 2_000:
                raise ValueError("reconcile audit scanned count is invalid")
            if type(written) is not int or not 0 <= written <= 500:
                raise ValueError("reconcile audit written count is invalid")
        elif action == "resolve":
            if set(detail) != {"dedupe_key"}:
                raise ValueError("resolve audit detail has an invalid field set")
            self._require_lower_hex(detail["dedupe_key"], "dedupe_key")
        elif action == "retention":
            if (
                set(detail) != {"removed"}
                or type(detail["removed"]) is not int
                or not 0 <= detail["removed"] <= 500
            ):
                raise ValueError("retention audit detail has an invalid field set")
        else:
            raise ValueError("observation audit action is invalid")
        detail_json = json.dumps(detail, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        if not 2 <= len(detail_json) <= 4_096:
            raise ValueError("observation audit detail exceeds its fixed bound")
        self._connection.execute(
            "INSERT INTO observation_audit "
            "(workspace_id, project_id, source_id, registry_revision, audit_id, action, "
            "result, occurred_at_utc, detail_json) "
            "SELECT ?, ?, ?, ?, coalesce(max(audit_id), 0) + 1, ?, 'committed', ?, ? "
            "FROM observation_audit WHERE workspace_id = ? AND project_id = ? "
            "AND source_id = ? AND registry_revision = ?",
            (
                workspace_id,
                project_id,
                source_id,
                registry_revision,
                action,
                occurred_at_utc,
                detail_json,
                workspace_id,
                project_id,
                source_id,
                registry_revision,
            ),
        )

    @classmethod
    def _validate_observation_candidate(
        cls, candidate: Mapping[str, object]
    ) -> dict[str, object]:
        required = {
            "dedupe_key",
            "path",
            "content_sha256",
            "byte_size",
            "mtime_ns",
            "scan_cursor",
            "scan_count",
        }
        if not isinstance(candidate, Mapping) or set(candidate) != required:
            raise ValueError("observation candidate has an invalid field set")
        dedupe_key = cls._require_lower_hex(candidate["dedupe_key"], "dedupe_key")
        content_sha256 = cls._require_lower_hex(
            candidate["content_sha256"], "content_sha256"
        )
        path = candidate["path"]
        if not isinstance(path, str):
            raise ValueError("observation path must be text")
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("observation path must be normalized workspace-relative evidence")
        byte_size = candidate["byte_size"]
        mtime_ns = candidate["mtime_ns"]
        scan_cursor = candidate["scan_cursor"]
        scan_count = candidate["scan_count"]
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise ValueError("byte_size must be a nonnegative integer")
        if (
            isinstance(mtime_ns, bool)
            or not isinstance(mtime_ns, int)
            or not -(1 << 63) <= mtime_ns <= (1 << 63) - 1
        ):
            raise ValueError("mtime_ns must be a signed 64-bit integer")
        if (
            not isinstance(scan_cursor, str)
            or "\x00" in scan_cursor
            or len(scan_cursor.encode("utf-8")) > 4096
        ):
            raise ValueError("candidate scan cursor must be bounded text")
        if (
            isinstance(scan_count, bool)
            or not isinstance(scan_count, int)
            or not 1 <= scan_count <= 2_000
        ):
            raise ValueError("candidate scan count must be between 1 and 2000")
        return {
            "dedupe_key": dedupe_key,
            "path": path,
            "content_sha256": content_sha256,
            "byte_size": byte_size,
            "mtime_ns": mtime_ns,
            "scan_cursor": scan_cursor,
            "scan_count": scan_count,
        }

    @staticmethod
    def _require_lower_hex(value: object, name: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
        return value

    def checkpoint(self) -> tuple[int, int, int]:
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("only the writer connection may checkpoint")
        result = tuple(self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        self._secure_sqlite_files(self.paths.ledger, main_pin=self._database_pin)
        return result

    @property
    def owns_writer_lock(self) -> bool:
        self._ensure_thread()
        return not self._read_only and self._writer_lock is not None and not self._closed

    def close(self) -> None:
        if threading.get_ident() != self._thread_id:
            raise sqlite3.ProgrammingError("ledger connections may not be reused across threads")
        if self._closed:
            return
        try:
            _close_connection_and_pin(self._connection, self._database_pin)
        finally:
            try:
                if self._writer_lock is not None:
                    self._writer_lock.close()
            finally:
                self._writer_lock = None
                self._closed = True

    def __enter__(self) -> "LedgerStore":
        self._ensure_thread()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
