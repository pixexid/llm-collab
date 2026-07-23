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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import LedgerPaths, validate_project_id, validate_registry_token, validate_workspace_id


SCHEMA_VERSION = 7
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
V5_TABLES = V4_TABLES | frozenset(
    {
        "canonical_evidence_bodies",
        "canonical_deliveries",
        "canonical_delivery_attempts",
        "canonical_delivery_receipts",
    }
)
V6_TABLES = V5_TABLES | frozenset(
    {
        "legacy_import_manifests",
        "legacy_import_manifest_entries",
        "legacy_import_records",
    }
)
V7_TABLES = V6_TABLES | frozenset({"observation_scheduler_cursors"})


class SQLiteSafetyError(RuntimeError):
    pass


class WriterAlreadyOpenError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


class CanonicalConflictError(RuntimeError):
    pass


class CanonicalIntegrityError(RuntimeError):
    pass


_CANONICAL_AGENT_ID = re.compile(r"agent_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_CANONICAL_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{64}\Z")
_CANONICAL_DELIVERY_ID = re.compile(r"delivery_[0-9a-f]{64}\Z")
_CANONICAL_ATTEMPT_ID = re.compile(r"attempt_[0-9a-f]{64}\Z")
_CANONICAL_RECEIPT_ID = re.compile(r"receipt_[0-9a-f]{64}\Z")
_CANONICAL_MANIFEST_ID = re.compile(r"manifest_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_CANONICAL_ENDPOINT_ID = re.compile(r"endpoint_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_CANONICAL_SESSION_REF_ID = re.compile(r"session_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_CANONICAL_REGISTRY_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANONICAL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}\Z")
_CANONICAL_EVIDENCE_ID = re.compile(r"evidence_[A-Za-z0-9][A-Za-z0-9_-]{2,127}\Z")
_CANONICAL_EXTENSION_KEY = re.compile(r"x_note_[A-Za-z][A-Za-z0-9_-]{0,55}\Z")
_CANONICAL_PATH = re.compile(
    r"/(?!/)(?!.*//)(?!.*(?:/\.{1,2})(?:/|$))(?!.*[\x00-\x1f\x7f\x85\u2028\u2029])"
    r"[^/](?:.*[^/])?\Z"
)
_CANONICAL_ACK_POLICIES = frozenset({"none", "required"})
_CANONICAL_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
_CANONICAL_ARTIFACT_KINDS = frozenset(
    {"chat", "task", "repo", "path", "branch", "worktree"}
)
_CANONICAL_EVIDENCE_KINDS = frozenset(
    {
        "adapter_observation",
        "native_delivery_state",
        "exact_session_acknowledgment",
        "exact_session_binding",
        "compatibility_import",
    }
)
_CANONICAL_EVIDENCE_QUALITIES = frozenset({"best_effort", "authoritative"})
_CANONICAL_TERMINAL_RECEIPT_STATES = frozenset({"accepted", "completed"})
_CANONICAL_TERMINAL_EVIDENCE_KINDS = frozenset(
    {"native_delivery_state", "exact_session_acknowledgment"}
)
_CANONICAL_TERMINAL_AUTHORITY_KINDS = frozenset({"native_runtime", "trusted_adapter"})
_CANONICAL_AUTHORITY_KINDS = _CANONICAL_TERMINAL_AUTHORITY_KINDS | frozenset(
    {"trusted_importer"}
)
_CANONICAL_RECEIPT_STATES = frozenset(
    {
        "persisted",
        "routed",
        "injected",
        "visible",
        "accepted",
        "processing",
        "acknowledged",
        "completed",
        "rejected_before_acceptance",
        "ambiguous",
        "pull_pending",
        "deferred_busy",
    }
)
_DELIVERY_OUTCOME_BY_STATE = {
    "completed": (100, "completed"),
    "accepted": (90, "accepted"),
    "rejected_before_acceptance": (80, "rejected_before_acceptance"),
    "ambiguous": (70, "ambiguous"),
    "pull_pending": (60, "pull_pending"),
    "deferred_busy": (50, "deferred_busy"),
    "acknowledged": (40, "pending"),
    "processing": (30, "pending"),
    "visible": (20, "pending"),
    "injected": (10, "pending"),
    "routed": (5, "pending"),
    "persisted": (0, "pending"),
}
_SAFE_JSON_INTEGER_MIN = -9_007_199_254_740_991
_SAFE_JSON_INTEGER_MAX = 9_007_199_254_740_991
_JSON_FORBIDDEN_CODEPOINTS = {0x7F, 0x85, 0x2028, 0x2029}


def _bounded_text(value: object, name: str, maximum: int) -> str:
    try:
        encoded_size = len(value.encode("utf-8")) if isinstance(value, str) else -1
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    if not isinstance(value, str) or not value or "\x00" in value or encoded_size > maximum:
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


def _canonical_agent_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_AGENT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a MessageV1 agent_ identifier")
    return value


def _canonical_message_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_MESSAGE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical msg_ identifier")
    return value


def _canonical_delivery_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_DELIVERY_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical delivery_ identifier")
    return value


def _canonical_attempt_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_ATTEMPT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical attempt_ identifier")
    return value


def _canonical_receipt_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_RECEIPT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical receipt_ identifier")
    return value


def _canonical_manifest_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_MANIFEST_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a manifest_ identifier")
    return value


def _canonical_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


def _canonical_token(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded token")
    return value


def _canonical_registry_revision(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_REGISTRY_REVISION.fullmatch(value) is None:
        raise ValueError(f"{name} must be sha256:<lowercase hex>")
    return value


def _canonical_path(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_PATH.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical absolute path")
    return value


def _legacy_import_dir_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError("O_NOFOLLOW is required for legacy v2 import")
    return (
        os.O_RDONLY
        | nofollow
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _legacy_import_file_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    nonblock = getattr(os, "O_NONBLOCK", None)
    if nofollow is None or nonblock is None:
        raise ValueError("O_NOFOLLOW and O_NONBLOCK are required for legacy v2 import")
    return os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)


def _legacy_file_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _legacy_dir_identity(status: os.stat_result) -> tuple[int, int, int]:
    return status.st_dev, status.st_ino, status.st_mode


def _canonical_endpoint_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_ENDPOINT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be an endpoint_ identifier")
    return value


def _optional_session_ref_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CANONICAL_SESSION_REF_ID.fullmatch(value) is None:
        raise ValueError("session_ref_id must be a session_ identifier")
    return value


def _canonical_scope(
    workspace_id: object, scope_kind: object, scope_identity: object
) -> tuple[str, str, str]:
    workspace = validate_workspace_id(workspace_id)  # type: ignore[arg-type]
    if scope_kind == "workspace":
        if scope_identity != "workspace":
            raise ValueError("workspace scope identity must be workspace")
        return workspace, "workspace", "workspace"
    if scope_kind == "project":
        return workspace, "project", validate_project_id(scope_identity)  # type: ignore[arg-type]
    raise ValueError("scope_kind must be workspace or project")


def _normalized_recipients(values: Iterable[object]) -> tuple[str, ...]:
    recipients = tuple(
        sorted({_canonical_agent_id(value, "recipient_agent_id") for value in values})
    )
    if not recipients or len(recipients) > 256:
        raise ValueError("recipients must contain between 1 and 256 distinct agents")
    return recipients


def _normalized_artifacts(values: Iterable[object]) -> tuple[tuple[str, str], ...]:
    artifacts = set()
    for value in values:
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("each artifact must be an (artifact_kind, artifact_ref) pair")
        kind, reference = value
        if not isinstance(kind, str) or kind not in _CANONICAL_ARTIFACT_KINDS:
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
    return b"\x02" + len(items).to_bytes(8, "big") + b"".join(
        _frame(item) for item in items
    )


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


def _derive_delivery_id(
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    recipient_agent_id: str,
    endpoint_id: str,
) -> str:
    return "delivery_" + hashlib.sha256(
        b"".join(
            (
                _frame(workspace_id),
                _frame(scope_kind),
                _frame(scope_identity),
                _frame(message_id),
                _frame(recipient_agent_id),
                _frame(endpoint_id),
            )
        )
    ).hexdigest()


def _derive_attempt_id(
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
    attempt_index: int,
) -> str:
    return "attempt_" + hashlib.sha256(
        b"".join(
            (
                _frame(workspace_id),
                _frame(scope_kind),
                _frame(scope_identity),
                _frame(message_id),
                _frame(delivery_id),
                _frame(str(attempt_index)),
            )
        )
    ).hexdigest()


def _derive_receipt_id(
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    evidence_sha256: str,
) -> str:
    return "receipt_" + hashlib.sha256(
        b"".join(
            (
                _frame(workspace_id),
                _frame(scope_kind),
                _frame(scope_identity),
                _frame(message_id),
                _frame(delivery_id),
                _frame(attempt_id),
                _frame(evidence_sha256),
            )
        )
    ).hexdigest()


def _validate_canonical_json_text(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("canonical JSON strings must be valid UTF-8") from exc
    for character in value:
        codepoint = ord(character)
        if (
            codepoint < 0x20
            or codepoint in _JSON_FORBIDDEN_CODEPOINTS
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            raise ValueError("canonical JSON strings must not contain controls or surrogates")


def _validate_canonical_json_value(value: object) -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not _SAFE_JSON_INTEGER_MIN <= value <= _SAFE_JSON_INTEGER_MAX:
            raise ValueError("canonical JSON integers must be in the safe range")
        return
    if isinstance(value, float):
        raise ValueError("canonical JSON does not admit floats")
    if isinstance(value, str):
        _validate_canonical_json_text(value)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            _validate_canonical_json_text(key)
            _validate_canonical_json_value(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            _validate_canonical_json_value(item)
        return
    raise ValueError("canonical JSON values must be objects, arrays, strings, integers, booleans, or null")


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    _validate_canonical_json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _integrity_without(value: Mapping[str, object], field: str = "integrity") -> str:
    projection = dict(value)
    projection.pop(field, None)
    return hashlib.sha256(_canonical_json_bytes(projection)).hexdigest()


def _normalize_manifest_entry(value: object) -> dict[str, object]:
    entry = dict(_mapping(value, "manifest entry"))
    locator = _canonical_path(entry.get("canonical_locator"), "canonical_locator")
    if isinstance(entry.get("byte_size"), bool) or not isinstance(entry.get("byte_size"), int):
        raise ValueError("byte_size must be an integer")
    byte_size = int(entry["byte_size"])
    if not 0 <= byte_size <= 1048576:
        raise ValueError("byte_size must be between 0 and 1048576")
    source_boundary = _mapping(entry.get("source_boundary"), "source_boundary")
    trusted_importer = _mapping(entry.get("trusted_importer"), "trusted_importer")
    normalized = {
        **entry,
        "canonical_locator": locator,
        "content_hash": _canonical_sha256(entry.get("content_hash"), "content_hash"),
        "byte_size": byte_size,
        "evidence_form_version": _canonical_token(entry.get("evidence_form_version"), "evidence_form_version"),
        "cutoff_policy_revision": _canonical_token(entry.get("cutoff_policy_revision"), "cutoff_policy_revision"),
        "source_workspace_id": validate_workspace_id(entry.get("source_workspace_id")),  # type: ignore[arg-type]
        "source_project_id": validate_project_id(entry.get("source_project_id")),  # type: ignore[arg-type]
        "source_registry_revision": _canonical_registry_revision(
            entry.get("source_registry_revision"), "source_registry_revision"
        ),
        "source_boundary": {
            "kind": source_boundary.get("kind"),
            "identity": _canonical_token(source_boundary.get("identity"), "source_boundary.identity"),
            "immutable": source_boundary.get("immutable"),
        },
        "trusted_importer": {
            "identity": _canonical_token(trusted_importer.get("identity"), "trusted_importer.identity"),
            "revision": _canonical_token(trusted_importer.get("revision"), "trusted_importer.revision"),
        },
        "transaction_id": _canonical_token(entry.get("transaction_id"), "transaction_id"),
        "provenance_id": _canonical_token(entry.get("provenance_id"), "provenance_id"),
    }
    if normalized["source_boundary"]["kind"] not in {
        "source_snapshot",
        "ledger_checkpoint",
        "content_addressed_revision",
        "sealed_observation",
    } or normalized["source_boundary"]["immutable"] is not True:
        raise ValueError("source_boundary must be an immutable closed-kind boundary")
    expected = _integrity_without(normalized)
    if entry.get("integrity") != expected:
        raise CanonicalIntegrityError("manifest entry integrity does not match")
    normalized["integrity"] = expected
    return normalized


def _normalize_legacy_manifest(value: Mapping[str, object]) -> dict[str, object]:
    manifest = dict(_mapping(value, "legacy manifest"))
    publication = dict(_mapping(manifest.get("publication"), "publication"))
    publisher = _mapping(publication.get("publisher"), "publisher")
    source_boundary = _mapping(publication.get("source_boundary"), "publication.source_boundary")
    entries = tuple(_normalize_manifest_entry(entry) for entry in manifest.get("entries", ()))
    if not 1 <= len(entries) <= 4096:
        raise ValueError("manifest entries must contain between 1 and 4096 entries")
    locators = [entry["canonical_locator"] for entry in entries]
    if len(set(locators)) != len(locators):
        raise ValueError("manifest entries must have duplicate-free canonical_locator values")
    normalized_publication = {
        **publication,
        "publisher": {
            "identity": _canonical_token(publisher.get("identity"), "publisher.identity"),
            "revision": _canonical_token(publisher.get("revision"), "publisher.revision"),
        },
        "publication_transaction_id": _canonical_token(
            publication.get("publication_transaction_id"), "publication_transaction_id"
        ),
        "provenance_id": _canonical_token(publication.get("provenance_id"), "publication.provenance_id"),
        "workspace_id": validate_workspace_id(publication.get("workspace_id")),  # type: ignore[arg-type]
        "project_id": validate_project_id(publication.get("project_id")),  # type: ignore[arg-type]
        "registry_revision": _canonical_registry_revision(
            publication.get("registry_revision"), "registry_revision"
        ),
        "cutoff_policy_revision": _canonical_token(
            publication.get("cutoff_policy_revision"), "publication.cutoff_policy_revision"
        ),
        "source_boundary": {
            "kind": source_boundary.get("kind"),
            "identity": _canonical_token(source_boundary.get("identity"), "publication.source_boundary.identity"),
            "immutable": source_boundary.get("immutable"),
        },
    }
    if normalized_publication["source_boundary"]["kind"] not in {
        "source_snapshot",
        "ledger_checkpoint",
        "content_addressed_revision",
        "sealed_observation",
    } or normalized_publication["source_boundary"]["immutable"] is not True:
        raise ValueError("publication source_boundary must be immutable")
    expected_publication = _integrity_without(normalized_publication)
    if publication.get("integrity") != expected_publication:
        raise CanonicalIntegrityError("publication integrity does not match")
    normalized_publication["integrity"] = expected_publication
    if manifest.get("sealed") is not True:
        raise ValueError("legacy manifest must be sealed")
    seal_projection = {
        "manifest_id": _canonical_manifest_id(manifest.get("manifest_id"), "manifest_id"),
        "cutoff_policy_revision": _canonical_token(manifest.get("cutoff_policy_revision"), "cutoff_policy_revision"),
        "entries": entries,
        "publication": normalized_publication,
    }
    seal = _mapping(manifest.get("seal"), "seal")
    if seal.get("algorithm") != "sha256":
        raise ValueError("legacy manifest seal algorithm must be sha256")
    expected_seal = hashlib.sha256(_canonical_json_bytes(seal_projection)).hexdigest()
    if seal.get("value") != expected_seal:
        raise CanonicalIntegrityError("legacy manifest seal does not match")
    return {
        **seal_projection,
        "sealed": True,
        "seal": {"algorithm": "sha256", "value": expected_seal},
    }


def _parse_legacy_frontmatter(raw: bytes) -> tuple[dict[str, object], bytes]:
    if not raw.startswith(b"---\n"):
        raise ValueError("legacy packet is missing frontmatter")
    end = raw.find(b"\n---\n", 4)
    if end == -1:
        raise ValueError("legacy packet frontmatter is not closed")
    try:
        frontmatter_text = raw[4:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("legacy packet frontmatter is not UTF-8") from exc
    body = raw[end + 5 :]
    frontmatter: dict[str, object] = {}
    for line in frontmatter_text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                inner = value[1:-1].strip()
                parsed = [
                    item.strip().strip('"').strip("'")
                    for item in inner.split(",")
                    if item.strip()
                ] if inner else []
            if not isinstance(parsed, list):
                raise ValueError("legacy packet list frontmatter is malformed")
            frontmatter[key] = parsed
        elif value.startswith("{") and value.endswith("}"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("legacy packet object frontmatter is malformed") from exc
            frontmatter[key] = parsed
        elif value.lower() == "null" or value == "":
            frontmatter[key] = None
        elif value.lower() == "true":
            frontmatter[key] = True
        elif value.lower() == "false":
            frontmatter[key] = False
        else:
            try:
                frontmatter[key] = int(value)
            except ValueError:
                frontmatter[key] = value
    return frontmatter, body


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(value)


def _legacy_v2_locator_kind(locator: str) -> str:
    parts = locator.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "Chats" and parts[2] == "meta.json":
        return "chat_meta"
    if len(parts) == 3 and parts[0] == "Chats" and parts[2].endswith(".md"):
        return "packet"
    if len(parts) == 3 and parts[0] == "agents" and parts[2] == "inbox.json":
        return "inbox_pointer"
    raise ValueError("legacy manifest locator is outside the closed v2 source set")


def _legacy_packet_name_parts(locator: str) -> tuple[str, str, str, str]:
    name = locator.strip("/").split("/")[-1]
    match = re.fullmatch(r"(.+)_(to|from)-([A-Za-z0-9_-]+)_(.+)\.md", name)
    if match is None:
        raise ValueError("legacy packet filename is malformed")
    stem, direction, agent, slug = match.groups()
    return stem, direction, agent, slug


def _normalize_evidence(value: Mapping[str, object]) -> tuple[bytes, str, str, str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("evidence must be a mapping")
    evidence = dict(value)
    state = evidence.get("state")
    quality = evidence.get("quality")
    evidence_kind = evidence.get("evidence_kind")
    if not isinstance(state, str) or state not in _CANONICAL_RECEIPT_STATES:
        raise ValueError("evidence state is not in the closed vocabulary")
    if not isinstance(quality, str) or quality not in _CANONICAL_EVIDENCE_QUALITIES:
        raise ValueError("evidence quality is not in the closed vocabulary")
    if not isinstance(evidence_kind, str) or evidence_kind not in _CANONICAL_EVIDENCE_KINDS:
        raise ValueError("evidence_kind is not in the closed vocabulary")
    integrity = evidence.get("integrity")
    projection = dict(evidence)
    projection.pop("integrity", None)
    expected = "sha256:" + hashlib.sha256(_canonical_json_bytes(projection)).hexdigest()
    if integrity != expected:
        raise CanonicalIntegrityError("state evidence integrity does not match its bytes")
    body = _canonical_json_bytes(evidence)
    if len(body) > 1048576:
        raise ValueError("evidence must be at most 1048576 bytes")
    return body, hashlib.sha256(body).hexdigest(), state, quality, evidence_kind


def _scope_projection(scope_kind: str, scope_identity: str) -> dict[str, str]:
    scope = {"kind": scope_kind}
    if scope_kind == "project":
        scope["project_id"] = scope_identity
    return scope


def _validate_state_evidence_core_schema(evidence: Mapping[str, object]) -> None:
    required = {
        "schema_version",
        "workspace_id",
        "scope",
        "evidence_id",
        "evidence_kind",
        "quality",
        "state",
        "authority",
        "subject",
        "correlation_id",
        "observed_at_utc",
        "integrity",
    }
    allowed = required | {"legacy_manifest", "legacy_import", "extensions"}
    if set(evidence) - allowed:
        raise CanonicalIntegrityError("canonical state evidence has unexpected fields")
    if not required <= set(evidence):
        raise CanonicalIntegrityError("canonical state evidence is missing required fields")
    if evidence.get("schema_version") != 1:
        raise CanonicalIntegrityError("canonical state evidence schema_version mismatch")
    if (
        not isinstance(evidence.get("evidence_id"), str)
        or _CANONICAL_EVIDENCE_ID.fullmatch(str(evidence["evidence_id"])) is None
    ):
        raise CanonicalIntegrityError("canonical state evidence_id is invalid")
    if (
        not isinstance(evidence.get("correlation_id"), str)
        or _CANONICAL_TOKEN.fullmatch(str(evidence["correlation_id"])) is None
    ):
        raise CanonicalIntegrityError("canonical state evidence correlation_id is invalid")
    _utc_timestamp(evidence.get("observed_at_utc"), "observed_at_utc")
    authority = evidence.get("authority")
    if not isinstance(authority, Mapping):
        raise CanonicalIntegrityError("canonical state evidence authority is invalid")
    required_authority = {
        "authority_kind",
        "identity",
        "implementation_revision",
        "capability_profile_id",
        "capability_profile_revision",
    }
    if set(authority) != required_authority:
        raise CanonicalIntegrityError("canonical state evidence authority fields are invalid")
    if authority.get("authority_kind") not in _CANONICAL_AUTHORITY_KINDS:
        raise CanonicalIntegrityError("canonical state evidence authority kind is invalid")
    for key in required_authority - {"authority_kind"}:
        if not isinstance(authority.get(key), str) or _CANONICAL_TOKEN.fullmatch(
            str(authority[key])
        ) is None:
            raise CanonicalIntegrityError("canonical state evidence authority token is invalid")
    subject = evidence.get("subject")
    if not isinstance(subject, Mapping):
        raise CanonicalIntegrityError("canonical receipt evidence subject is missing")
    allowed_subject = {
        "message_id",
        "delivery_id",
        "attempt_id",
        "endpoint_id",
        "session_ref_id",
        "native_session_id",
        "repository_binding",
        "legacy_locator",
    }
    if not subject or set(subject) - allowed_subject:
        raise CanonicalIntegrityError("canonical state evidence subject fields are invalid")
    for key, value in subject.items():
        if key in {"repository_binding"}:
            if not isinstance(value, Mapping):
                raise CanonicalIntegrityError(
                    "canonical state evidence repository binding is invalid"
                )
            continue
        if not isinstance(value, str) or not value:
            raise CanonicalIntegrityError("canonical state evidence subject value is invalid")
    if evidence.get("evidence_kind") == "exact_session_binding":
        if evidence.get("quality") != "authoritative":
            raise CanonicalIntegrityError(
                "canonical exact-session evidence must be authoritative"
            )
        if authority.get("authority_kind") not in _CANONICAL_TERMINAL_AUTHORITY_KINDS:
            raise CanonicalIntegrityError(
                "canonical exact-session evidence authority is not trusted"
            )
        for key in ("endpoint_id", "session_ref_id", "native_session_id"):
            if key not in subject:
                raise CanonicalIntegrityError(
                    "canonical exact-session evidence subject is incomplete"
                )
    if evidence.get("evidence_kind") == "compatibility_import":
        if evidence.get("quality") != "best_effort":
            raise CanonicalIntegrityError(
                "canonical compatibility-import evidence must be best effort"
            )
        if authority.get("authority_kind") != "trusted_importer":
            raise CanonicalIntegrityError(
                "canonical compatibility-import authority is invalid"
            )
        if "legacy_manifest" not in evidence or "legacy_import" not in evidence:
            raise CanonicalIntegrityError(
                "canonical compatibility-import evidence is missing legacy provenance"
            )
        if "legacy_locator" not in subject:
            raise CanonicalIntegrityError(
                "canonical compatibility-import subject is incomplete"
            )
    elif "legacy_manifest" in evidence or "legacy_import" in evidence:
        raise CanonicalIntegrityError(
            "canonical non-import evidence must not carry legacy provenance"
        )
    extensions = evidence.get("extensions")
    if extensions is not None:
        if not isinstance(extensions, Mapping) or len(extensions) > 8:
            raise CanonicalIntegrityError("canonical state evidence extensions are invalid")
        for key, value in extensions.items():
            if not isinstance(key, str) or _CANONICAL_EXTENSION_KEY.fullmatch(key) is None:
                raise CanonicalIntegrityError(
                    "canonical state evidence extension key is invalid"
                )
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                if not _SAFE_JSON_INTEGER_MIN <= value <= _SAFE_JSON_INTEGER_MAX:
                    raise CanonicalIntegrityError(
                        "canonical state evidence extension integer is invalid"
                    )
                continue
            if isinstance(value, str):
                if len(value) > 512:
                    raise CanonicalIntegrityError(
                        "canonical state evidence extension string is too long"
                    )
                _validate_canonical_json_text(value)
                continue
            raise CanonicalIntegrityError("canonical state evidence extension value is invalid")


def _validate_receipt_evidence_contract(
    evidence: Mapping[str, object],
    *,
    workspace_id: str,
    scope_kind: str,
    scope_identity: str,
    message_id: str,
    delivery_id: str,
    attempt_id: str,
    endpoint_id: str,
    session_ref_id: str | None,
) -> None:
    _validate_state_evidence_core_schema(evidence)
    if evidence.get("workspace_id") != workspace_id:
        raise CanonicalIntegrityError("canonical receipt evidence workspace mismatch")
    if evidence.get("scope") != _scope_projection(scope_kind, scope_identity):
        raise CanonicalIntegrityError("canonical receipt evidence scope mismatch")
    subject = evidence.get("subject")
    if not isinstance(subject, Mapping):
        raise CanonicalIntegrityError("canonical receipt evidence subject is missing")
    expected_subject = {
        "message_id": message_id,
        "delivery_id": delivery_id,
        "attempt_id": attempt_id,
        "endpoint_id": endpoint_id,
    }
    for key, value in expected_subject.items():
        if subject.get(key) != value:
            raise CanonicalIntegrityError("canonical receipt evidence subject mismatch")
    if subject.get("session_ref_id") != session_ref_id:
        raise CanonicalIntegrityError("canonical receipt evidence session mismatch")
    state = evidence.get("state")
    if state in _CANONICAL_TERMINAL_RECEIPT_STATES:
        if session_ref_id is None:
            raise CanonicalIntegrityError(
                "canonical terminal receipt evidence requires a session reference"
            )
        if evidence.get("quality") != "authoritative":
            raise CanonicalIntegrityError(
                "canonical terminal receipt evidence must be authoritative"
            )
        if evidence.get("evidence_kind") not in _CANONICAL_TERMINAL_EVIDENCE_KINDS:
            raise CanonicalIntegrityError(
                "canonical terminal receipt evidence kind is not authoritative"
            )
        authority = evidence.get("authority")
        if not isinstance(authority, Mapping) or authority.get(
            "authority_kind"
        ) not in _CANONICAL_TERMINAL_AUTHORITY_KINDS:
            raise CanonicalIntegrityError(
                "canonical terminal receipt evidence authority is not trusted"
            )


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
            DEFERRABLE INITIALLY DEFERRED
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
            DEFERRABLE INITIALLY DEFERRED
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
            DEFERRABLE INITIALLY DEFERRED
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
    CREATE TRIGGER canonical_message_recipients_sealed
    BEFORE INSERT ON canonical_message_recipients
    WHEN EXISTS (
        SELECT 1 FROM canonical_messages
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'canonical recipients are sealed');
    END
    """,
    """
    CREATE TRIGGER canonical_message_artifacts_sealed
    BEFORE INSERT ON canonical_message_artifacts
    WHEN EXISTS (
        SELECT 1 FROM canonical_messages
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'canonical artifacts are sealed');
    END
    """,
    """
    CREATE TRIGGER canonical_message_tags_sealed
    BEFORE INSERT ON canonical_message_tags
    WHEN EXISTS (
        SELECT 1 FROM canonical_messages
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'canonical tags are sealed');
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
V4_MIGRATION_CHECKSUM = "sha256:63f00990d9c3e01384d14d7613c961856ff48037504b1e0ada1f95b034cedf01"
V4_SCHEMA_FINGERPRINT = "sha256:665e17152991c6c21cb8756a5d5720e35e3154d13a4a069b4c74440ed425b39e"
V5_SQL = (
    """
    CREATE TABLE canonical_evidence_bodies (
        workspace_id TEXT NOT NULL
            CHECK (
                instr(workspace_id, char(0)) = 0
                AND length(CAST(workspace_id AS BLOB)) BETWEEN 6 AND 131
                AND substr(workspace_id, 1, 3) = 'ws_'
                AND substr(workspace_id, 4, 1) GLOB '[A-Za-z0-9]'
                AND substr(workspace_id, 4) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        evidence_sha256 TEXT NOT NULL
            CHECK (
                instr(evidence_sha256, char(0)) = 0
                AND length(CAST(evidence_sha256 AS BLOB)) = 64
                AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
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
        PRIMARY KEY (workspace_id, evidence_sha256)
    ) STRICT
    """,
    """
    CREATE TABLE canonical_deliveries (
        workspace_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_identity TEXT NOT NULL,
        message_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL
            CHECK (
                instr(delivery_id, char(0)) = 0
                AND length(CAST(delivery_id AS BLOB)) = 73
                AND substr(delivery_id, 1, 9) = 'delivery_'
                AND substr(delivery_id, 10) NOT GLOB '*[^0-9a-f]*'
            ),
        recipient_agent_id TEXT NOT NULL
            CHECK (
                instr(recipient_agent_id, char(0)) = 0
                AND length(CAST(recipient_agent_id AS BLOB)) BETWEEN 9 AND 134
                AND substr(recipient_agent_id, 1, 6) = 'agent_'
                AND substr(recipient_agent_id, 7, 1) GLOB '[A-Za-z0-9]'
                AND substr(recipient_agent_id, 7) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        endpoint_id TEXT NOT NULL
            CHECK (
                instr(endpoint_id, char(0)) = 0
                AND length(CAST(endpoint_id AS BLOB)) BETWEEN 12 AND 137
                AND substr(endpoint_id, 1, 9) = 'endpoint_'
                AND substr(endpoint_id, 10, 1) GLOB '[A-Za-z0-9]'
                AND substr(endpoint_id, 10) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        deadline_epoch_ms INTEGER NOT NULL
            CHECK (typeof(deadline_epoch_ms) = 'integer' AND deadline_epoch_ms >= 0),
        created_at_utc TEXT NOT NULL
            CHECK (
                instr(created_at_utc, char(0)) = 0
                AND length(CAST(created_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, scope_kind, scope_identity, message_id, delivery_id),
        UNIQUE (
            workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id, endpoint_id
        ),
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (
            workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id
        )
            REFERENCES canonical_message_recipients (
                workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id
            )
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE canonical_delivery_attempts (
        workspace_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_identity TEXT NOT NULL,
        message_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL
            CHECK (
                instr(attempt_id, char(0)) = 0
                AND length(CAST(attempt_id AS BLOB)) = 72
                AND substr(attempt_id, 1, 8) = 'attempt_'
                AND substr(attempt_id, 9) NOT GLOB '*[^0-9a-f]*'
            ),
        attempt_index INTEGER NOT NULL
            CHECK (typeof(attempt_index) = 'integer' AND attempt_index >= 0),
        attempt_epoch_ms INTEGER NOT NULL
            CHECK (typeof(attempt_epoch_ms) = 'integer' AND attempt_epoch_ms >= 0),
        created_at_utc TEXT NOT NULL
            CHECK (
                instr(created_at_utc, char(0)) = 0
                AND length(CAST(created_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, scope_kind, scope_identity, message_id, delivery_id, attempt_id),
        UNIQUE (workspace_id, scope_kind, scope_identity, message_id, delivery_id, attempt_index),
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id, delivery_id)
            REFERENCES canonical_deliveries (
                workspace_id, scope_kind, scope_identity, message_id, delivery_id
            )
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE canonical_delivery_receipts (
        workspace_id TEXT NOT NULL,
        scope_kind TEXT NOT NULL,
        scope_identity TEXT NOT NULL,
        message_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        receipt_id TEXT NOT NULL
            CHECK (
                instr(receipt_id, char(0)) = 0
                AND length(CAST(receipt_id AS BLOB)) = 72
                AND substr(receipt_id, 1, 8) = 'receipt_'
                AND substr(receipt_id, 9) NOT GLOB '*[^0-9a-f]*'
            ),
        evidence_sha256 TEXT NOT NULL
            CHECK (
                instr(evidence_sha256, char(0)) = 0
                AND length(CAST(evidence_sha256 AS BLOB)) = 64
                AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        state TEXT NOT NULL CHECK (
            state IN (
                'persisted', 'routed', 'injected', 'visible', 'accepted', 'processing',
                'acknowledged', 'completed', 'rejected_before_acceptance', 'ambiguous',
                'pull_pending', 'deferred_busy'
            )
        ),
        quality TEXT NOT NULL CHECK (quality IN ('best_effort', 'authoritative')),
        evidence_kind TEXT NOT NULL CHECK (
            evidence_kind IN (
                'adapter_observation', 'native_delivery_state',
                'exact_session_acknowledgment', 'exact_session_binding',
                'compatibility_import'
            )
        ),
        session_ref_id TEXT
            CHECK (
                session_ref_id IS NULL
                OR (
                    instr(session_ref_id, char(0)) = 0
                    AND length(CAST(session_ref_id AS BLOB)) BETWEEN 11 AND 136
                    AND substr(session_ref_id, 1, 8) = 'session_'
                    AND substr(session_ref_id, 9, 1) GLOB '[A-Za-z0-9]'
                    AND substr(session_ref_id, 9) NOT GLOB '*[^A-Za-z0-9_-]*'
                )
            ),
        created_at_utc TEXT NOT NULL
            CHECK (
                instr(created_at_utc, char(0)) = 0
                AND length(CAST(created_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (
            workspace_id, scope_kind, scope_identity, message_id, delivery_id, attempt_id, receipt_id
        ),
        FOREIGN KEY (workspace_id, evidence_sha256)
            REFERENCES canonical_evidence_bodies (workspace_id, evidence_sha256)
            ON DELETE RESTRICT,
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id, delivery_id, attempt_id)
            REFERENCES canonical_delivery_attempts (
                workspace_id, scope_kind, scope_identity, message_id, delivery_id, attempt_id
            )
            ON DELETE RESTRICT,
        CHECK (state NOT IN ('accepted', 'completed') OR session_ref_id IS NOT NULL)
    ) STRICT
    """,
    """
    CREATE TRIGGER canonical_evidence_bodies_no_update
    BEFORE UPDATE ON canonical_evidence_bodies
    BEGIN
        SELECT RAISE(ABORT, 'canonical evidence bodies are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_evidence_bodies_no_delete
    BEFORE DELETE ON canonical_evidence_bodies
    BEGIN
        SELECT RAISE(ABORT, 'canonical evidence bodies are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_deliveries_no_update
    BEFORE UPDATE ON canonical_deliveries
    BEGIN
        SELECT RAISE(ABORT, 'canonical deliveries are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_deliveries_no_delete
    BEFORE DELETE ON canonical_deliveries
    BEGIN
        SELECT RAISE(ABORT, 'canonical deliveries are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_attempts_no_update
    BEFORE UPDATE ON canonical_delivery_attempts
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery attempts are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_attempts_no_delete
    BEFORE DELETE ON canonical_delivery_attempts
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery attempts are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_receipts_no_update
    BEFORE UPDATE ON canonical_delivery_receipts
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery receipts are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_receipts_no_delete
    BEFORE DELETE ON canonical_delivery_receipts
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery receipts are append-only');
    END
    """,
    """
    CREATE TRIGGER canonical_deliveries_count_cap
    BEFORE INSERT ON canonical_deliveries
    WHEN (
        SELECT count(*) FROM canonical_deliveries
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
    ) >= 256
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery count exceeds 256');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_attempts_count_cap
    BEFORE INSERT ON canonical_delivery_attempts
    WHEN (
        SELECT count(*) FROM canonical_delivery_attempts
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
          AND delivery_id = NEW.delivery_id
    ) >= 64
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery attempt count exceeds 64');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_receipts_count_cap
    BEFORE INSERT ON canonical_delivery_receipts
    WHEN (
        SELECT count(*) FROM canonical_delivery_receipts
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
          AND delivery_id = NEW.delivery_id
          AND attempt_id = NEW.attempt_id
    ) >= 256
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery receipt count exceeds 256');
    END
    """,
    """
    CREATE TRIGGER canonical_delivery_attempts_not_expired
    BEFORE INSERT ON canonical_delivery_attempts
    WHEN EXISTS (
        SELECT 1 FROM canonical_deliveries
        WHERE workspace_id = NEW.workspace_id
          AND scope_kind = NEW.scope_kind
          AND scope_identity = NEW.scope_identity
          AND message_id = NEW.message_id
          AND delivery_id = NEW.delivery_id
          AND deadline_epoch_ms != 0
          AND NEW.attempt_epoch_ms >= deadline_epoch_ms
    )
    BEGIN
        SELECT RAISE(ABORT, 'canonical delivery attempt is expired');
    END
    """,
)
V5_MIGRATION_CHECKSUM = "sha256:d6498cf5728ec3d56c0d1360a065243d72384a0de50af55bead8054881bbd9b9"
V5_SCHEMA_FINGERPRINT = "sha256:4495eab6339d339b770442d994b5878e0743d011917cc99b370991a793891a99"
V6_SQL = (
    """
    CREATE TABLE legacy_import_manifests (
        workspace_id TEXT NOT NULL
            CHECK (
                instr(workspace_id, char(0)) = 0
                AND length(CAST(workspace_id AS BLOB)) BETWEEN 6 AND 131
                AND substr(workspace_id, 1, 3) = 'ws_'
                AND substr(workspace_id, 4, 1) GLOB '[A-Za-z0-9]'
                AND substr(workspace_id, 4) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        manifest_id TEXT NOT NULL
            CHECK (
                instr(manifest_id, char(0)) = 0
                AND length(CAST(manifest_id AS BLOB)) BETWEEN 12 AND 137
                AND substr(manifest_id, 1, 9) = 'manifest_'
                AND substr(manifest_id, 10, 1) GLOB '[A-Za-z0-9]'
                AND substr(manifest_id, 10) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        cutoff_policy_revision TEXT NOT NULL
            CHECK (
                instr(cutoff_policy_revision, char(0)) = 0
                AND length(CAST(cutoff_policy_revision AS BLOB)) BETWEEN 1 AND 128
            ),
        entry_count INTEGER NOT NULL
            CHECK (typeof(entry_count) = 'integer' AND entry_count BETWEEN 1 AND 4096),
        publisher_identity TEXT NOT NULL
            CHECK (
                instr(publisher_identity, char(0)) = 0
                AND length(CAST(publisher_identity AS BLOB)) BETWEEN 1 AND 128
            ),
        publisher_revision TEXT NOT NULL
            CHECK (
                instr(publisher_revision, char(0)) = 0
                AND length(CAST(publisher_revision AS BLOB)) BETWEEN 1 AND 128
            ),
        publication_transaction_id TEXT NOT NULL
            CHECK (
                instr(publication_transaction_id, char(0)) = 0
                AND length(CAST(publication_transaction_id AS BLOB)) BETWEEN 1 AND 128
            ),
        provenance_id TEXT NOT NULL
            CHECK (
                instr(provenance_id, char(0)) = 0
                AND length(CAST(provenance_id AS BLOB)) BETWEEN 1 AND 128
            ),
        source_registry_revision TEXT NOT NULL
            CHECK (
                instr(source_registry_revision, char(0)) = 0
                AND length(CAST(source_registry_revision AS BLOB)) = 71
                AND substr(source_registry_revision, 1, 7) = 'sha256:'
                AND substr(source_registry_revision, 8) NOT GLOB '*[^0-9a-f]*'
            ),
        source_boundary_kind TEXT NOT NULL
            CHECK (
                source_boundary_kind IN (
                    'source_snapshot', 'ledger_checkpoint',
                    'content_addressed_revision', 'sealed_observation'
                )
            ),
        source_boundary_identity TEXT NOT NULL
            CHECK (
                instr(source_boundary_identity, char(0)) = 0
                AND length(CAST(source_boundary_identity AS BLOB)) BETWEEN 1 AND 128
            ),
        manifest_seal TEXT NOT NULL
            CHECK (
                instr(manifest_seal, char(0)) = 0
                AND length(CAST(manifest_seal AS BLOB)) = 64
                AND manifest_seal NOT GLOB '*[^0-9a-f]*'
            ),
        imported_at_utc TEXT NOT NULL
            CHECK (
                instr(imported_at_utc, char(0)) = 0
                AND length(CAST(imported_at_utc AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, manifest_id),
        UNIQUE (workspace_id, manifest_seal),
        FOREIGN KEY (workspace_id, source_registry_revision)
            REFERENCES workspace_registry_snapshots (workspace_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE legacy_import_manifest_entries (
        workspace_id TEXT NOT NULL,
        manifest_id TEXT NOT NULL,
        entry_integrity TEXT NOT NULL
            CHECK (
                instr(entry_integrity, char(0)) = 0
                AND length(CAST(entry_integrity AS BLOB)) = 64
                AND entry_integrity NOT GLOB '*[^0-9a-f]*'
            ),
        canonical_locator TEXT NOT NULL
            CHECK (
                instr(canonical_locator, char(0)) = 0
                AND length(CAST(canonical_locator AS BLOB)) BETWEEN 2 AND 4096
                AND substr(canonical_locator, 1, 1) = '/'
                AND substr(canonical_locator, -1, 1) != '/'
                AND canonical_locator NOT GLOB '*//*'
                AND canonical_locator NOT GLOB '*/./*'
                AND canonical_locator NOT GLOB '*/../*'
                AND canonical_locator NOT GLOB '*/.'
                AND canonical_locator NOT GLOB '*/..'
            ),
        content_hash TEXT NOT NULL
            CHECK (
                instr(content_hash, char(0)) = 0
                AND length(CAST(content_hash AS BLOB)) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        byte_size INTEGER NOT NULL
            CHECK (typeof(byte_size) = 'integer' AND byte_size BETWEEN 0 AND 1048576),
        evidence_form_version TEXT NOT NULL
            CHECK (
                instr(evidence_form_version, char(0)) = 0
                AND length(CAST(evidence_form_version AS BLOB)) BETWEEN 1 AND 128
            ),
        cutoff_policy_revision TEXT NOT NULL
            CHECK (
                instr(cutoff_policy_revision, char(0)) = 0
                AND length(CAST(cutoff_policy_revision AS BLOB)) BETWEEN 1 AND 128
            ),
        source_workspace_id TEXT NOT NULL
            CHECK (
                instr(source_workspace_id, char(0)) = 0
                AND length(CAST(source_workspace_id AS BLOB)) BETWEEN 6 AND 131
                AND substr(source_workspace_id, 1, 3) = 'ws_'
                AND substr(source_workspace_id, 4, 1) GLOB '[A-Za-z0-9]'
                AND substr(source_workspace_id, 4) NOT GLOB '*[^A-Za-z0-9_-]*'
            ),
        source_project_id TEXT NOT NULL
            CHECK (
                instr(source_project_id, char(0)) = 0
                AND length(CAST(source_project_id AS BLOB)) BETWEEN 1 AND 128
                AND substr(source_project_id, 1, 1) GLOB '[A-Za-z]'
                AND source_project_id NOT GLOB '*[^A-Za-z0-9._-]*'
            ),
        source_registry_revision TEXT NOT NULL
            CHECK (
                instr(source_registry_revision, char(0)) = 0
                AND length(CAST(source_registry_revision AS BLOB)) = 71
                AND substr(source_registry_revision, 1, 7) = 'sha256:'
                AND substr(source_registry_revision, 8) NOT GLOB '*[^0-9a-f]*'
            ),
        transaction_id TEXT NOT NULL
            CHECK (
                instr(transaction_id, char(0)) = 0
                AND length(CAST(transaction_id AS BLOB)) BETWEEN 1 AND 128
            ),
        provenance_id TEXT NOT NULL
            CHECK (
                instr(provenance_id, char(0)) = 0
                AND length(CAST(provenance_id AS BLOB)) BETWEEN 1 AND 128
            ),
        PRIMARY KEY (workspace_id, manifest_id, entry_integrity),
        FOREIGN KEY (workspace_id, manifest_id)
            REFERENCES legacy_import_manifests (workspace_id, manifest_id)
            ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE legacy_import_records (
        workspace_id TEXT NOT NULL,
        manifest_id TEXT NOT NULL,
        entry_integrity TEXT NOT NULL,
        record_kind TEXT NOT NULL CHECK (record_kind IN ('message', 'inbox_pointer')),
        scope_kind TEXT CHECK (scope_kind IN ('workspace', 'project')),
        scope_identity TEXT
            CHECK (
                scope_identity IS NULL
                OR (
                    instr(scope_identity, char(0)) = 0
                    AND length(CAST(scope_identity AS BLOB)) BETWEEN 1 AND 128
                )
            ),
        message_id TEXT
            CHECK (
                message_id IS NULL
                OR (
                    instr(message_id, char(0)) = 0
                    AND length(CAST(message_id AS BLOB)) = 68
                    AND substr(message_id, 1, 4) = 'msg_'
                    AND substr(message_id, 5) NOT GLOB '*[^0-9a-f]*'
                )
            ),
        PRIMARY KEY (workspace_id, manifest_id, entry_integrity),
        FOREIGN KEY (workspace_id, manifest_id, entry_integrity)
            REFERENCES legacy_import_manifest_entries (workspace_id, manifest_id, entry_integrity)
            ON DELETE RESTRICT,
        FOREIGN KEY (workspace_id, scope_kind, scope_identity, message_id)
            REFERENCES canonical_messages (workspace_id, scope_kind, scope_identity, message_id)
            ON DELETE RESTRICT,
        CHECK (
            (
                record_kind = 'message'
                AND message_id IS NOT NULL
                AND scope_kind IS NOT NULL
                AND scope_identity IS NOT NULL
            )
            OR
            (
                record_kind = 'inbox_pointer'
                AND message_id IS NULL
                AND scope_kind IS NULL
                AND scope_identity IS NULL
            )
        )
    ) STRICT
    """,
    """
    CREATE TRIGGER legacy_import_manifests_no_update
    BEFORE UPDATE ON legacy_import_manifests
    BEGIN
        SELECT RAISE(ABORT, 'legacy import manifests are append-only');
    END
    """,
    """
    CREATE TRIGGER legacy_import_manifests_no_delete
    BEFORE DELETE ON legacy_import_manifests
    BEGIN
        SELECT RAISE(ABORT, 'legacy import manifests are append-only');
    END
    """,
    """
    CREATE TRIGGER legacy_import_manifest_entries_no_update
    BEFORE UPDATE ON legacy_import_manifest_entries
    BEGIN
        SELECT RAISE(ABORT, 'legacy import manifest entries are append-only');
    END
    """,
    """
    CREATE TRIGGER legacy_import_manifest_entries_no_delete
    BEFORE DELETE ON legacy_import_manifest_entries
    BEGIN
        SELECT RAISE(ABORT, 'legacy import manifest entries are append-only');
    END
    """,
    """
    CREATE TRIGGER legacy_import_records_no_update
    BEFORE UPDATE ON legacy_import_records
    BEGIN
        SELECT RAISE(ABORT, 'legacy import records are append-only');
    END
    """,
    """
    CREATE TRIGGER legacy_import_records_no_delete
    BEFORE DELETE ON legacy_import_records
    BEGIN
        SELECT RAISE(ABORT, 'legacy import records are append-only');
    END
    """,
)
V6_MIGRATION_CHECKSUM = "sha256:56e7ca2ba9eb0a8eb79079372abdc7a39c024977e71a40931b8b60a6acc33c00"
V6_SCHEMA_FINGERPRINT = "sha256:eb8bc4ddd4348ce05874b91c63ce963c5bb3653636363b7437e2046900996d60"
V7_SQL = (
    """
    CREATE TABLE observation_scheduler_cursors (
        workspace_id TEXT NOT NULL,
        source_id TEXT NOT NULL CHECK (source_id = 'chats_mailbox'),
        registry_revision TEXT NOT NULL,
        next_project_id TEXT NOT NULL
            CHECK (
                instr(next_project_id, char(0)) = 0
                AND length(CAST(next_project_id AS BLOB)) BETWEEN 1 AND 200
            ),
        updated_at_utc TEXT NOT NULL
            CHECK (
                instr(updated_at_utc, char(0)) = 0
                AND length(CAST(updated_at_utc AS BLOB)) > 0
            ),
        PRIMARY KEY (workspace_id, source_id),
        FOREIGN KEY (workspace_id, registry_revision)
            REFERENCES workspace_registry_snapshots
                (workspace_id, registry_revision)
            ON DELETE RESTRICT
    ) STRICT
    """,
)
V7_MIGRATION_CHECKSUM = "sha256:2de4a95aaf7f92fb436772b5cf4fede42db485ae464809b9a23f9c8ccc6dda03"
V7_SCHEMA_FINGERPRINT = "sha256:3fd3ca002c8571ff90165da045929aedd520d2a891a8b95b2a36ba07569c32e1"
MIGRATIONS = (
    (1, V1_SQL),
    (2, V2_SQL),
    (3, V3_SQL),
    (4, V4_SQL),
    (5, V5_SQL),
    (6, V6_SQL),
    (7, V7_SQL),
)


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


def _v5_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *V4_SQL, *V5_SQL):
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _v6_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *V4_SQL, *V5_SQL, *V6_SQL):
            connection.execute(statement)
        return _schema_fingerprint(connection)
    finally:
        connection.close()


def _v7_schema_fingerprint_from_sql() -> str:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for statement in (*V1_SQL, *V2_SQL, *V3_SQL, *V4_SQL, *V5_SQL, *V6_SQL, *V7_SQL):
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
        if claimed == 4:
            cls._validate_released_v4(connection, paths)
            return
        if claimed == 5:
            cls._validate_released_v5(connection, paths)
            return
        if claimed == 6:
            cls._validate_released_v6(connection, paths)
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
            if _migration_checksum(V5_SQL) != V5_MIGRATION_CHECKSUM:
                raise MigrationError("released v5 migration checksum is incoherent")
            if _v5_schema_fingerprint_from_sql() != V5_SCHEMA_FINGERPRINT:
                raise MigrationError("released v5 schema fingerprint is incoherent")
            if _migration_checksum(V6_SQL) != V6_MIGRATION_CHECKSUM:
                raise MigrationError("released v6 migration checksum is incoherent")
            if _v6_schema_fingerprint_from_sql() != V6_SCHEMA_FINGERPRINT:
                raise MigrationError("released v6 schema fingerprint is incoherent")
            if _migration_checksum(V7_SQL) != V7_MIGRATION_CHECKSUM:
                raise MigrationError("released v7 migration checksum is incoherent")
            if _v7_schema_fingerprint_from_sql() != V7_SCHEMA_FINGERPRINT:
                raise MigrationError("released v7 schema fingerprint is incoherent")
            rows = cls._migration_rows(connection)
            if [row[0] for row in rows] != [1, 2, 3, 4, 5, 6, 7]:
                raise MigrationError("ledger migration metadata is incoherent")
            cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
            cls._validate_migration_row(rows[1], V2_MIGRATION_CHECKSUM, 1, paths)
            cls._validate_migration_row(rows[2], V3_MIGRATION_CHECKSUM, 2, paths)
            cls._validate_migration_row(rows[3], V4_MIGRATION_CHECKSUM, 3, paths)
            cls._validate_migration_row(rows[4], V5_MIGRATION_CHECKSUM, 4, paths)
            cls._validate_migration_row(rows[5], V6_MIGRATION_CHECKSUM, 5, paths)
            cls._validate_migration_row(rows[6], V7_MIGRATION_CHECKSUM, 6, paths)
            actual_tables = cls._table_names(connection)
            if actual_tables != V7_TABLES:
                raise MigrationError(
                    "ledger v7 table set is incoherent: "
                    f"missing={sorted(V7_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V7_TABLES)}"
                )
            if _schema_fingerprint(connection) != V7_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v7 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    @classmethod
    def _validate_released_v6(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        cls._validate_database_health(connection)
        if connection.execute("PRAGMA user_version").fetchone()[0] != 6:
            raise MigrationError("released v6 ledger has an incoherent user_version")
        if cls._table_names(connection) != V6_TABLES:
            raise MigrationError("released v6 ledger table set is incoherent")
        if _schema_fingerprint(connection) != V6_SCHEMA_FINGERPRINT:
            raise MigrationError("released v6 ledger schema fingerprint is incoherent")
        rows = cls._migration_rows(connection)
        if [row[0] for row in rows] != [1, 2, 3, 4, 5, 6]:
            raise MigrationError("released v6 migration metadata is incoherent")
        cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
        cls._validate_migration_row(rows[1], V2_MIGRATION_CHECKSUM, 1, paths)
        cls._validate_migration_row(rows[2], V3_MIGRATION_CHECKSUM, 2, paths)
        cls._validate_migration_row(rows[3], V4_MIGRATION_CHECKSUM, 3, paths)
        cls._validate_migration_row(rows[4], V5_MIGRATION_CHECKSUM, 4, paths)
        cls._validate_migration_row(rows[5], V6_MIGRATION_CHECKSUM, 5, paths)

    @classmethod
    def _validate_released_v5(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Accept only the exact released v5 long enough for a writer migration."""
        try:
            cls._validate_database_health(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 5:
                raise MigrationError("ledger is not released schema v5")
            released = (
                (V1_SQL, V1_MIGRATION_CHECKSUM, V1_SCHEMA_FINGERPRINT, _v1_schema_fingerprint_from_sql),
                (V2_SQL, V2_MIGRATION_CHECKSUM, V2_SCHEMA_FINGERPRINT, _v2_schema_fingerprint_from_sql),
                (V3_SQL, V3_MIGRATION_CHECKSUM, V3_SCHEMA_FINGERPRINT, _v3_schema_fingerprint_from_sql),
                (V4_SQL, V4_MIGRATION_CHECKSUM, V4_SCHEMA_FINGERPRINT, _v4_schema_fingerprint_from_sql),
                (V5_SQL, V5_MIGRATION_CHECKSUM, V5_SCHEMA_FINGERPRINT, _v5_schema_fingerprint_from_sql),
            )
            for statements, checksum, fingerprint, fingerprint_from_sql in released:
                if _migration_checksum(statements) != checksum:
                    raise MigrationError("released migration checksum is incoherent")
                if fingerprint_from_sql() != fingerprint:
                    raise MigrationError("released schema fingerprint is incoherent")
            rows = cls._migration_rows(connection)
            if [row[0] for row in rows] != [1, 2, 3, 4, 5]:
                raise MigrationError("ledger migration metadata is incoherent")
            cls._validate_migration_row(rows[0], V1_MIGRATION_CHECKSUM, 0, paths)
            cls._validate_migration_row(rows[1], V2_MIGRATION_CHECKSUM, 1, paths)
            cls._validate_migration_row(rows[2], V3_MIGRATION_CHECKSUM, 2, paths)
            cls._validate_migration_row(rows[3], V4_MIGRATION_CHECKSUM, 3, paths)
            cls._validate_migration_row(rows[4], V5_MIGRATION_CHECKSUM, 4, paths)
            actual_tables = cls._table_names(connection)
            if actual_tables != V5_TABLES:
                raise MigrationError(
                    "ledger v5 table set is incoherent: "
                    f"missing={sorted(V5_TABLES - actual_tables)}, "
                    f"extra={sorted(actual_tables - V5_TABLES)}"
                )
            if _schema_fingerprint(connection) != V5_SCHEMA_FINGERPRINT:
                raise MigrationError("ledger v5 schema fingerprint is incoherent")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("ledger schema is corrupt or incoherent") from exc

    @classmethod
    def _validate_released_v4(
        cls, connection: sqlite3.Connection, paths: LedgerPaths
    ) -> None:
        """Accept only the exact released v4 long enough for a writer migration."""
        try:
            cls._validate_database_health(connection)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 4:
                raise MigrationError("ledger is not released schema v4")
            released = (
                (V1_SQL, V1_MIGRATION_CHECKSUM, V1_SCHEMA_FINGERPRINT, _v1_schema_fingerprint_from_sql),
                (V2_SQL, V2_MIGRATION_CHECKSUM, V2_SCHEMA_FINGERPRINT, _v2_schema_fingerprint_from_sql),
                (V3_SQL, V3_MIGRATION_CHECKSUM, V3_SCHEMA_FINGERPRINT, _v3_schema_fingerprint_from_sql),
                (V4_SQL, V4_MIGRATION_CHECKSUM, V4_SCHEMA_FINGERPRINT, _v4_schema_fingerprint_from_sql),
            )
            for statements, checksum, fingerprint, fingerprint_from_sql in released:
                if _migration_checksum(statements) != checksum:
                    raise MigrationError("released migration checksum is incoherent")
                if fingerprint_from_sql() != fingerprint:
                    raise MigrationError("released schema fingerprint is incoherent")
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
                    5: V5_MIGRATION_CHECKSUM,
                    6: V6_MIGRATION_CHECKSUM,
                    7: V7_MIGRATION_CHECKSUM,
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
                    5: V5_SCHEMA_FINGERPRINT,
                    6: V6_SCHEMA_FINGERPRINT,
                    7: V7_SCHEMA_FINGERPRINT,
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

    def _validate_legacy_manifest_provenance(
        self,
        *,
        workspace_id: str,
        registry_revision: str,
        normalized: Mapping[str, object],
        records: Sequence[tuple[object, ...]],
        source_project_ids: Mapping[str, tuple[object, bool]] | None,
    ) -> None:
        publication = normalized["publication"]  # type: ignore[index]
        entries = normalized["entries"]  # type: ignore[assignment]
        publication_workspace_id = str(publication["workspace_id"])  # type: ignore[index]
        publication_project_id = str(publication["project_id"])  # type: ignore[index]
        cutoff_policy_revision = str(normalized["cutoff_policy_revision"])
        if publication_workspace_id != workspace_id:
            raise ValueError("manifest provenance workspace_id must match the ledger workspace")
        if self.get_project_snapshot(
            workspace_id=workspace_id,
            project_id=publication_project_id,
            registry_revision=registry_revision,
        ) is None:
            raise ValueError("manifest provenance project_id references an unknown project")
        if str(publication["cutoff_policy_revision"]) != cutoff_policy_revision:  # type: ignore[index]
            raise ValueError("manifest provenance cutoff_policy_revision must match")
        entry_ids: set[str] = set()
        for entry in entries:  # type: ignore[union-attr]
            entry_id = str(entry["integrity"])
            entry_ids.add(entry_id)
            if str(entry["source_workspace_id"]) != publication_workspace_id:
                raise ValueError("manifest provenance source_workspace_id must match")
            if str(entry["source_project_id"]) != publication_project_id:
                raise ValueError("manifest provenance source_project_id must match")
            if str(entry["cutoff_policy_revision"]) != cutoff_policy_revision:
                raise ValueError("manifest provenance cutoff_policy_revision must match")
        for record in records:
            if record[3] == "message" and (record[4], record[5]) != ("project", publication_project_id):
                raise ValueError("legacy import message records must match manifest publication project_id")
        if source_project_ids is None:
            return
        required_source_entries = {
            str(entry["integrity"])
            for entry in entries  # type: ignore[union-attr]
            if entry["evidence_form_version"] in {"v2_packet", "v2_chat_meta"}
        }
        if not required_source_entries <= set(source_project_ids):
            raise ValueError("legacy source project_id must cover every project-declaring manifest entry")
        if not set(source_project_ids) <= entry_ids:
            raise ValueError("legacy source project_id references an unknown manifest entry")
        for value, _required in source_project_ids.values():
            try:
                source_project_id = validate_project_id(value)  # type: ignore[arg-type]
            except ValueError as exc:
                raise ValueError("legacy source project_id must match manifest provenance") from exc
            if source_project_id != publication_project_id:
                raise ValueError("legacy source project_id must match manifest provenance")

    def _prepare_legacy_import_rows(
        self,
        *,
        workspace_id: str,
        normalized: Mapping[str, object],
        records: Iterable[Mapping[str, object]],
        imported_at_utc: str,
        planned_messages: frozenset[tuple[str, str, str]] = frozenset(),
        source_project_ids: Mapping[str, tuple[object, bool]] | None = None,
    ) -> tuple[
        str,
        tuple[object, ...],
        tuple[tuple[object, ...], ...],
        tuple[tuple[object, ...], ...],
    ]:
        publication = normalized["publication"]  # type: ignore[index]
        manifest_id = str(normalized["manifest_id"])
        manifest_seal = str(normalized["seal"]["value"])  # type: ignore[index]
        entries = normalized["entries"]  # type: ignore[assignment]
        entry_ids = {str(entry["integrity"]) for entry in entries}  # type: ignore[index]
        recordless_entry_ids = {
            str(entry["integrity"])
            for entry in entries  # type: ignore[union-attr]
            if entry["evidence_form_version"] == "v2_chat_meta"
        }

        normalized_records = []
        for record in records:
            item = dict(_mapping(record, "legacy import record"))
            entry_integrity = _canonical_sha256(item.get("entry_integrity"), "entry_integrity")
            if entry_integrity not in entry_ids:
                raise ValueError("legacy import record references an unknown manifest entry")
            kind = item.get("record_kind")
            if kind == "message":
                _workspace, scope_kind, scope_identity = _canonical_scope(
                    workspace_id, item.get("scope_kind"), item.get("scope_identity")
                )
                message_id = _canonical_message_id(item.get("message_id"), "message_id")
                planned = (scope_kind, scope_identity, message_id) in planned_messages
                if (
                    not planned
                    and self._read_canonical_message(workspace_id, scope_kind, scope_identity, message_id) is None
                ):
                    raise CanonicalIntegrityError("legacy import record message is missing")
                normalized_records.append(
                    (workspace_id, manifest_id, entry_integrity, "message", scope_kind, scope_identity, message_id)
                )
            elif kind == "inbox_pointer":
                if any(item.get(name) is not None for name in ("scope_kind", "scope_identity", "message_id")):
                    raise ValueError("inbox pointer records must not carry canonical message identity")
                normalized_records.append(
                    (workspace_id, manifest_id, entry_integrity, "inbox_pointer", None, None, None)
                )
            else:
                raise ValueError("record_kind must be message or inbox_pointer")
        normalized_records = tuple(sorted(normalized_records, key=lambda row: row[2]))
        recorded_entry_ids = {str(row[2]) for row in normalized_records}
        if len(recorded_entry_ids) != len(normalized_records):
            raise ValueError("legacy import records must be duplicate-free by entry")
        if recorded_entry_ids | recordless_entry_ids != entry_ids:
            raise ValueError("legacy import records must cover every non-meta manifest entry exactly once")
        if recorded_entry_ids & recordless_entry_ids:
            raise ValueError("chat meta manifest entries must not carry output records")
        self._validate_legacy_manifest_provenance(
            workspace_id=workspace_id,
            registry_revision=str(publication["registry_revision"]),  # type: ignore[index]
            normalized=normalized,
            records=normalized_records,
            source_project_ids=source_project_ids,
        )

        expected_manifest = (
            workspace_id,
            manifest_id,
            str(normalized["cutoff_policy_revision"]),
            len(entries),
            publication["publisher"]["identity"],  # type: ignore[index]
            publication["publisher"]["revision"],  # type: ignore[index]
            publication["publication_transaction_id"],  # type: ignore[index]
            publication["provenance_id"],  # type: ignore[index]
            publication["registry_revision"],  # type: ignore[index]
            publication["source_boundary"]["kind"],  # type: ignore[index]
            publication["source_boundary"]["identity"],  # type: ignore[index]
            manifest_seal,
            imported_at_utc,
        )
        expected_entries = tuple(
            sorted(
                (
                    workspace_id,
                    manifest_id,
                    str(entry["integrity"]),
                    str(entry["canonical_locator"]),
                    str(entry["content_hash"]),
                    int(entry["byte_size"]),
                    str(entry["evidence_form_version"]),
                    str(entry["cutoff_policy_revision"]),
                    str(entry["source_workspace_id"]),
                    str(entry["source_project_id"]),
                    str(entry["source_registry_revision"]),
                    str(entry["transaction_id"]),
                    str(entry["provenance_id"]),
                )
                for entry in entries  # type: ignore[union-attr]
            )
        )
        return manifest_seal, expected_manifest, expected_entries, normalized_records

    def _existing_legacy_import_is_identical(
        self,
        *,
        workspace_id: str,
        manifest_id: str,
        manifest_seal: str,
        expected_manifest: tuple[object, ...],
        expected_entries: tuple[tuple[object, ...], ...],
        normalized_records: tuple[tuple[object, ...], ...],
    ) -> bool | None:
        existing = self._connection.execute(
            "SELECT manifest_seal FROM legacy_import_manifests "
            "WHERE workspace_id = ? AND manifest_id = ?",
            (workspace_id, manifest_id),
        ).fetchone()
        if existing is None:
            return None
        if existing[0] != manifest_seal:
            raise CanonicalConflictError("legacy manifest_id conflicts with a different seal")
        stored_manifest = self._connection.execute(
            "SELECT workspace_id, manifest_id, cutoff_policy_revision, entry_count, "
            "publisher_identity, publisher_revision, publication_transaction_id, "
            "provenance_id, source_registry_revision, source_boundary_kind, "
            "source_boundary_identity, manifest_seal "
            "FROM legacy_import_manifests WHERE workspace_id = ? AND manifest_id = ?",
            (workspace_id, manifest_id),
        ).fetchone()
        stored_entries = tuple(
            self._connection.execute(
                "SELECT workspace_id, manifest_id, entry_integrity, canonical_locator, "
                "content_hash, byte_size, evidence_form_version, cutoff_policy_revision, "
                "source_workspace_id, source_project_id, source_registry_revision, "
                "transaction_id, provenance_id FROM legacy_import_manifest_entries "
                "WHERE workspace_id = ? AND manifest_id = ? ORDER BY entry_integrity",
                (workspace_id, manifest_id),
            )
        )
        stored_records = tuple(
            self._connection.execute(
                "SELECT workspace_id, manifest_id, entry_integrity, record_kind, "
                "scope_kind, scope_identity, message_id FROM legacy_import_records "
                "WHERE workspace_id = ? AND manifest_id = ? ORDER BY entry_integrity",
                (workspace_id, manifest_id),
            )
        )
        if (
            stored_manifest != expected_manifest[:-1]
            or stored_entries != expected_entries
            or stored_records != normalized_records
        ):
            raise CanonicalConflictError("legacy manifest retry conflicts with different rows")
        return True

    def _append_legacy_import_rows(
        self,
        *,
        expected_manifest: tuple[object, ...],
        expected_entries: tuple[tuple[object, ...], ...],
        normalized_records: tuple[tuple[object, ...], ...],
    ) -> None:
        self._connection.execute(
            "INSERT INTO legacy_import_manifests "
            "(workspace_id, manifest_id, cutoff_policy_revision, entry_count, "
            "publisher_identity, publisher_revision, publication_transaction_id, "
            "provenance_id, source_registry_revision, source_boundary_kind, "
            "source_boundary_identity, manifest_seal, imported_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            expected_manifest,
        )
        self._connection.executemany(
            "INSERT INTO legacy_import_manifest_entries "
            "(workspace_id, manifest_id, entry_integrity, canonical_locator, "
            "content_hash, byte_size, evidence_form_version, cutoff_policy_revision, "
            "source_workspace_id, source_project_id, source_registry_revision, "
            "transaction_id, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            expected_entries,
        )
        self._connection.executemany(
            "INSERT INTO legacy_import_records "
            "(workspace_id, manifest_id, entry_integrity, record_kind, scope_kind, "
            "scope_identity, message_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            normalized_records,
        )

    def record_legacy_import_manifest(
        self,
        *,
        workspace_id: str,
        manifest: Mapping[str, object],
        records: Iterable[Mapping[str, object]],
        imported_at_utc: str,
    ) -> tuple[str, bool]:
        """Atomically seal one supplied legacy manifest and its import result rows."""
        self.canonical_preflight(write=True)
        workspace_id = validate_workspace_id(workspace_id)
        imported_at_utc = _utc_timestamp(imported_at_utc, "imported_at_utc")
        normalized = _normalize_legacy_manifest(manifest)
        manifest_id = str(normalized["manifest_id"])
        manifest_seal, expected_manifest, expected_entries, normalized_records = (
            self._prepare_legacy_import_rows(
                workspace_id=workspace_id,
                normalized=normalized,
                records=records,
                imported_at_utc=imported_at_utc,
            )
        )

        existing = self._existing_legacy_import_is_identical(
            workspace_id=workspace_id,
            manifest_id=manifest_id,
            manifest_seal=manifest_seal,
            expected_manifest=expected_manifest,
            expected_entries=expected_entries,
            normalized_records=normalized_records,
        )
        if existing:
            return manifest_seal, False

        if self._connection.execute(
            "SELECT 1 FROM legacy_import_manifests WHERE workspace_id = ? AND manifest_seal = ?",
            (workspace_id, manifest_seal),
        ).fetchone() is not None:
            raise CanonicalConflictError("legacy manifest seal already belongs to another manifest")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._append_legacy_import_rows(
                expected_manifest=expected_manifest,
                expected_entries=expected_entries,
                normalized_records=normalized_records,
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return manifest_seal, True

    def _read_legacy_v2_source_file(self, root_fd: int, locator: str) -> bytes:
        parts = locator.strip("/").split("/")
        if not parts or any(part in {"", ".", ".."} or "/" in part or "\x00" in part for part in parts):
            raise ValueError("legacy locator is unsafe")
        opened: list[int] = []
        parent_fd = root_fd
        try:
            for part in parts[:-1]:
                fd = os.open(part, _legacy_import_dir_flags(), dir_fd=parent_fd)
                opened.append(fd)
                parent_fd = fd
            name = parts[-1]
            fd = os.open(name, _legacy_import_file_flags(), dir_fd=parent_fd)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("legacy source is not a regular file")
                if before.st_size > 1048576:
                    raise ValueError("legacy source exceeds 1048576 bytes")
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = os.read(fd, min(65_536, 1048577 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 1048576:
                        raise ValueError("legacy source exceeds 1048576 bytes")
                after = os.fstat(fd)
                path_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    _legacy_file_identity(before) != _legacy_file_identity(after)
                    or _legacy_file_identity(path_status) != _legacy_file_identity(after)
                    or size != after.st_size
                ):
                    raise ValueError("legacy source changed during read")
                return b"".join(chunks)
            finally:
                os.close(fd)
        except OSError as exc:
            raise ValueError("refusing unsafe legacy source") from exc
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _count_legacy_chat_dir_entries(self, root_fd: int, locator: str) -> None:
        parts = locator.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "Chats":
            return
        opened: list[int] = []
        parent_fd = root_fd
        try:
            for part in parts[:2]:
                fd = os.open(part, _legacy_import_dir_flags(), dir_fd=parent_fd)
                opened.append(fd)
                parent_fd = fd
            count = 0
            with os.scandir(parent_fd) as entries:
                for _entry in entries:
                    count += 1
                    if count > 4096:
                        raise ValueError("legacy chat directory exceeds 4096 entries")
        except OSError as exc:
            raise ValueError("refusing unsafe legacy chat directory") from exc
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _read_legacy_v2_sources(
        self,
        *,
        workspace_root: str | os.PathLike[str],
        entries: Sequence[Mapping[str, object]],
    ) -> dict[str, bytes]:
        root = Path(workspace_root)
        if not root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        root_fd = os.open(root, _legacy_import_dir_flags())
        try:
            root_identity = _legacy_dir_identity(os.fstat(root_fd))
            sources: dict[str, bytes] = {}
            counted_chat_dirs: set[str] = set()
            cumulative = 0
            for entry in entries:
                locator = str(entry["canonical_locator"])
                kind = _legacy_v2_locator_kind(locator)
                expected_form = {
                    "chat_meta": "v2_chat_meta",
                    "packet": "v2_packet",
                    "inbox_pointer": "v2_inbox_index",
                }[kind]
                if entry["evidence_form_version"] != expected_form:
                    raise ValueError("legacy manifest entry evidence_form_version does not match locator kind")
                chat_dir = "/".join(locator.strip("/").split("/")[:2])
                if kind in {"chat_meta", "packet"} and chat_dir not in counted_chat_dirs:
                    self._count_legacy_chat_dir_entries(root_fd, locator)
                    counted_chat_dirs.add(chat_dir)
                raw = self._read_legacy_v2_source_file(root_fd, locator)
                if hashlib.sha256(raw).hexdigest() != entry["content_hash"]:
                    raise CanonicalIntegrityError("legacy source hash does not match manifest")
                if len(raw) != entry["byte_size"]:
                    raise CanonicalIntegrityError("legacy source byte_size does not match manifest")
                cumulative += len(raw)
                if cumulative > 67_108_864:
                    raise ValueError("legacy import exceeds 64 MiB cumulative source bytes")
                if locator in sources:
                    raise ValueError("legacy manifest contains duplicate locator")
                sources[locator] = raw
            if _legacy_dir_identity(os.fstat(root_fd)) != root_identity:
                raise ValueError("workspace root identity changed during legacy import")
            return sources
        finally:
            os.close(root_fd)

    def import_legacy_v2_manifest(
        self,
        *,
        workspace_root: str | os.PathLike[str],
        workspace_id: str,
        manifest: Mapping[str, object],
        registry_revision: str,
        imported_at_utc: str,
    ) -> tuple[str, bool]:
        """Read only caller-named v2 sources, then atomically append canonical messages and v6 provenance."""
        self.canonical_preflight(write=True)
        workspace_id = validate_workspace_id(workspace_id)
        if workspace_id != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        registry_revision = _canonical_registry_revision(registry_revision, "registry_revision")
        imported_at_utc = _utc_timestamp(imported_at_utc, "imported_at_utc")
        if not self.has_registry_snapshot(workspace_id=workspace_id, registry_revision=registry_revision):
            raise ValueError("registry snapshot is absent")
        normalized = _normalize_legacy_manifest(manifest)
        entries = normalized["entries"]  # type: ignore[assignment]
        sources = self._read_legacy_v2_sources(workspace_root=workspace_root, entries=entries)  # type: ignore[arg-type]

        packet_members: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
        records: list[dict[str, object]] = []
        prepared_messages: list[dict[str, object]] = []
        source_project_ids: dict[str, tuple[object, bool]] = {}
        inbox_pointer_count = 0
        entry_by_locator = {str(entry["canonical_locator"]): entry for entry in entries}  # type: ignore[union-attr]
        for locator, raw in sources.items():
            entry = entry_by_locator[locator]
            kind = _legacy_v2_locator_kind(locator)
            if kind == "chat_meta":
                try:
                    parsed_meta = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("legacy chat meta is not valid JSON") from exc
                if not isinstance(parsed_meta, dict):
                    raise ValueError("legacy chat meta must be a JSON object")
                source_project_ids[str(entry["integrity"])] = (parsed_meta.get("project_id"), True)
                continue
            if kind == "inbox_pointer":
                try:
                    inbox = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("legacy inbox pointer index is not valid JSON") from exc
                if not isinstance(inbox, dict):
                    raise ValueError("legacy inbox pointer index must be a JSON object")
                if "project_id" in inbox:
                    source_project_ids[str(entry["integrity"])] = (inbox.get("project_id"), False)
                for bucket in ("unread", "read"):
                    pointers = inbox.get(bucket, [])
                    if not isinstance(pointers, list) or any(not isinstance(item, str) for item in pointers):
                        raise ValueError("legacy inbox pointer lists must contain only strings")
                    inbox_pointer_count += len(pointers)
                    if inbox_pointer_count > 4096:
                        raise ValueError("legacy import exceeds 4096 inbox pointers")
                records.append({"entry_integrity": entry["integrity"], "record_kind": "inbox_pointer"})
                continue

            frontmatter, body = _parse_legacy_frontmatter(raw)
            source_project_ids[str(entry["integrity"])] = (frontmatter.get("project_id"), True)
            stem, direction, filename_agent, slug = _legacy_packet_name_parts(locator)
            sender = _bounded_text(frontmatter.get("from"), "from", 128)
            recipient = _bounded_text(frontmatter.get("to"), "to", 128)
            if direction == "to" and filename_agent != recipient:
                raise ValueError("legacy packet to-filename disagrees with frontmatter")
            if direction == "from" and filename_agent != sender:
                raise ValueError("legacy packet from-filename disagrees with frontmatter")
            chat_dir = "/".join(locator.strip("/").split("/")[:2])
            pair_key = (chat_dir, stem, slug, sender, recipient)
            member = packet_members.setdefault(pair_key, {})
            if direction in member:
                raise ValueError("legacy packet pair has duplicate direction")
            member[direction] = {
                "locator": locator,
                "entry_integrity": entry["integrity"],
                "frontmatter": frontmatter,
                "body": body,
                "raw": raw,
            }

        for pair_key, member in sorted(packet_members.items()):
            if set(member) != {"to", "from"}:
                raise ValueError("legacy packet pair is incomplete")
            to_member = member["to"]  # type: ignore[index]
            from_member = member["from"]  # type: ignore[index]
            if to_member["raw"] != from_member["raw"]:  # type: ignore[index]
                raise ValueError("legacy packet pair members differ")
            frontmatter = to_member["frontmatter"]  # type: ignore[assignment]
            body = to_member["body"]  # type: ignore[assignment]
            publication_project_id = str(normalized["publication"]["project_id"])  # type: ignore[index]
            scope_kind, scope_identity = "project", publication_project_id
            sender_agent_id = _canonical_agent_id("agent_" + str(frontmatter["from"]), "sender_agent_id")  # type: ignore[index]
            recipient_agent_id = _canonical_agent_id("agent_" + str(frontmatter["to"]), "recipient_agent_id")  # type: ignore[index]
            artifacts = []
            artifacts.extend(("repo", value) for value in _string_list(frontmatter.get("repo_targets"), "repo_targets"))  # type: ignore[union-attr]
            artifacts.extend(("path", value) for value in _string_list(frontmatter.get("path_targets"), "path_targets"))  # type: ignore[union-attr]
            if isinstance(frontmatter.get("chat_id"), str):  # type: ignore[union-attr]
                artifacts.append(("chat", frontmatter["chat_id"]))  # type: ignore[index]
            if isinstance(frontmatter.get("related_task"), str):  # type: ignore[union-attr]
                artifacts.append(("task", frontmatter["related_task"]))  # type: ignore[index]
            locator = str(to_member["locator"])  # type: ignore[index]
            prepared = self._prepare_canonical_message(
                workspace_id=workspace_id,
                scope_kind=scope_kind,
                scope_identity=scope_identity,
                sender_agent_id=sender_agent_id,
                dedupe_key="import:" + hashlib.sha256(locator.encode("utf-8")).hexdigest(),
                body=body,  # type: ignore[arg-type]
                recipients=(recipient_agent_id,),
                registry_revision=registry_revision,
                created_at_utc=frontmatter.get("sent_utc"),  # type: ignore[union-attr]
                title=frontmatter.get("title"),  # type: ignore[union-attr]
                ttl_seconds=0,
                ack_policy="none",
                artifacts=artifacts,
                priority=frontmatter.get("priority"),  # type: ignore[union-attr]
                tags=_string_list(frontmatter.get("tags"), "tags"),  # type: ignore[union-attr]
                chat_link=frontmatter.get("chat_id"),  # type: ignore[union-attr]
                task_link=frontmatter.get("related_task"),  # type: ignore[union-attr]
            )
            prepared_messages.append(prepared)
            for direction in ("to", "from"):
                records.append(
                    {
                        "entry_integrity": member[direction]["entry_integrity"],  # type: ignore[index]
                        "record_kind": "message",
                        "scope_kind": scope_kind,
                        "scope_identity": scope_identity,
                        "message_id": prepared["message_id"],
                    }
                )

        planned = frozenset(
            (str(item["scope_kind"]), str(item["scope_identity"]), str(item["message_id"]))
            for item in prepared_messages
        )
        manifest_id = str(normalized["manifest_id"])
        manifest_seal, expected_manifest, expected_entries, normalized_records = (
            self._prepare_legacy_import_rows(
                workspace_id=workspace_id,
                normalized=normalized,
                records=records,
                imported_at_utc=imported_at_utc,
                planned_messages=planned,
                source_project_ids=source_project_ids,
            )
        )
        existing = self._existing_legacy_import_is_identical(
            workspace_id=workspace_id,
            manifest_id=manifest_id,
            manifest_seal=manifest_seal,
            expected_manifest=expected_manifest,
            expected_entries=expected_entries,
            normalized_records=normalized_records,
        )
        if existing:
            return manifest_seal, False
        if self._connection.execute(
            "SELECT 1 FROM legacy_import_manifests WHERE workspace_id = ? AND manifest_seal = ?",
            (workspace_id, manifest_seal),
        ).fetchone() is not None:
            raise CanonicalConflictError("legacy manifest seal already belongs to another manifest")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for prepared in prepared_messages:
                self._append_prepared_canonical_message(prepared)
            self._append_legacy_import_rows(
                expected_manifest=expected_manifest,
                expected_entries=expected_entries,
                normalized_records=normalized_records,
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return manifest_seal, True

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
        """Normalize and atomically append one intent, or return its exact equivalent."""
        self.canonical_preflight(write=True)
        prepared = self._prepare_canonical_message(
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
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            message_id, created = self._append_prepared_canonical_message(prepared)
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return message_id, created

    def _prepare_canonical_message(
        self,
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
    ) -> dict[str, object]:
        workspace_id, scope_kind, scope_identity = _canonical_scope(
            workspace_id, scope_kind, scope_identity
        )
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        sender_agent_id = _canonical_agent_id(sender_agent_id, "sender_agent_id")
        dedupe_key = _bounded_text(dedupe_key, "dedupe_key", 256)
        if not isinstance(body, bytes) or len(body) > 1048576:
            raise ValueError("body must be bytes of at most 1048576 bytes")
        recipients = _normalized_recipients(recipients)
        if (
            not isinstance(registry_revision, str)
            or _CANONICAL_REGISTRY_REVISION.fullmatch(registry_revision) is None
        ):
            raise ValueError("registry_revision must be sha256:<lowercase hex>")
        created_at_utc = _utc_timestamp(created_at_utc, "created_at_utc")
        title = _bounded_text(title, "title", 512)
        reply_to_message_id = (
            None
            if reply_to_message_id is None
            else _canonical_message_id(reply_to_message_id, "reply_to_message_id")
        )
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 0 <= ttl_seconds <= 31536000
        ):
            raise ValueError("ttl_seconds must be an integer between 0 and 31536000")
        if not isinstance(ack_policy, str) or ack_policy not in _CANONICAL_ACK_POLICIES:
            raise ValueError("ack_policy is not in the closed vocabulary")
        artifacts = _normalized_artifacts(artifacts)
        if not isinstance(priority, str) or priority not in _CANONICAL_PRIORITIES:
            raise ValueError("priority is not in the closed vocabulary")
        tags = _normalized_tags(tags)
        chat_link = _optional_text(chat_link, "chat_link", 256)
        task_link = _optional_text(task_link, "task_link", 256)
        body_sha256 = hashlib.sha256(body).hexdigest()
        message_id = _derive_message_id(
            workspace_id=workspace_id,
            scope_kind=scope_kind,
            scope_identity=scope_identity,
            sender_agent_id=sender_agent_id,
            dedupe_key=dedupe_key,
            body_sha256=body_sha256,
            recipients=recipients,
            reply_to_message_id=reply_to_message_id,
            ttl_seconds=ttl_seconds,
            ack_policy=ack_policy,
            artifacts=artifacts,
            title=title,
            priority=priority,
            tags=tags,
            chat_link=chat_link,
            task_link=task_link,
        )
        return {
            "workspace_id": workspace_id,
            "scope_kind": scope_kind,
            "scope_identity": scope_identity,
            "message_id": message_id,
            "sender_agent_id": sender_agent_id,
            "dedupe_key": dedupe_key,
            "body_sha256": body_sha256,
            "reply_to_message_id": reply_to_message_id,
            "ttl_seconds": ttl_seconds,
            "ack_policy": ack_policy,
            "title": title,
            "priority": priority,
            "chat_link": chat_link,
            "task_link": task_link,
            "registry_revision": registry_revision,
            "created_at_utc": created_at_utc,
            "recipients": recipients,
            "artifacts": artifacts,
            "tags": tags,
            "byte_size": len(body),
            "body": body,
        }

    def _append_prepared_canonical_message(
        self, prepared: Mapping[str, object]
    ) -> tuple[str, bool]:
        workspace_id = str(prepared["workspace_id"])
        scope_kind = str(prepared["scope_kind"])
        scope_identity = str(prepared["scope_identity"])
        message_id = str(prepared["message_id"])
        sender_agent_id = str(prepared["sender_agent_id"])
        dedupe_key = str(prepared["dedupe_key"])
        body_sha256 = str(prepared["body_sha256"])
        body = prepared["body"]
        byte_size = int(prepared["byte_size"])
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
            key: prepared[key]
            for key in (
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
                "recipients",
                "artifacts",
                "tags",
                "byte_size",
                "body",
            )
        }
        found_conflict = False
        for row in candidate_rows:
            try:
                existing = self._read_canonical_message(*row)
            except CanonicalIntegrityError:
                found_conflict = True
                continue
            if existing is None or any(
                existing[key] != value for key, value in equivalent.items()
            ):
                found_conflict = True
        if found_conflict:
            raise CanonicalConflictError(
                "canonical message identity or dedupe namespace conflicts with different intent"
            )
        if candidate_rows:
            return message_id, False

        body_row = self._connection.execute(
            "SELECT byte_size, body FROM canonical_bodies "
            "WHERE workspace_id = ? AND body_sha256 = ?",
            (workspace_id, body_sha256),
        ).fetchone()
        if body_row is not None and body_row != (byte_size, body):
            raise CanonicalConflictError("canonical body hash conflicts with different bytes")

        project_id = scope_identity if scope_kind == "project" else None
        if body_row is None:
            self._connection.execute(
                "INSERT INTO canonical_bodies "
                "(workspace_id, body_sha256, byte_size, body, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    body_sha256,
                    byte_size,
                    body,
                    prepared["created_at_utc"],
                ),
            )
        prefix = (workspace_id, scope_kind, scope_identity, message_id)
        self._connection.executemany(
            "INSERT INTO canonical_message_recipients "
            "(workspace_id, scope_kind, scope_identity, message_id, recipient_agent_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ((*prefix, recipient) for recipient in prepared["recipients"]),  # type: ignore[union-attr]
        )
        self._connection.executemany(
            "INSERT INTO canonical_message_artifacts "
            "(workspace_id, scope_kind, scope_identity, message_id, artifact_kind, "
            "artifact_ref) VALUES (?, ?, ?, ?, ?, ?)",
            ((*prefix, kind, reference) for kind, reference in prepared["artifacts"]),  # type: ignore[union-attr]
        )
        self._connection.executemany(
            "INSERT INTO canonical_message_tags "
            "(workspace_id, scope_kind, scope_identity, message_id, tag) "
            "VALUES (?, ?, ?, ?, ?)",
            ((*prefix, tag) for tag in prepared["tags"]),  # type: ignore[union-attr]
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
                body_sha256,
                prepared["reply_to_message_id"],
                prepared["ttl_seconds"],
                prepared["ack_policy"],
                prepared["title"],
                prepared["priority"],
                prepared["chat_link"],
                prepared["task_link"],
                prepared["registry_revision"],
                project_id,
                prepared["created_at_utc"],
            ),
        )
        return message_id, True

    def create_canonical_deliveries(
        self,
        *,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
        routes: Iterable[tuple[str, str]],
        now_epoch_ms: int,
        created_at_utc: str,
    ) -> tuple[tuple[str, bool], ...]:
        """Append route-only delivery rows for existing message recipients."""
        self.canonical_preflight(write=True)
        workspace_id, scope_kind, scope_identity = _canonical_scope(
            workspace_id, scope_kind, scope_identity
        )
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        message_id = _canonical_message_id(message_id, "message_id")
        message = self._read_canonical_message(
            workspace_id, scope_kind, scope_identity, message_id
        )
        if message is None:
            raise KeyError(message_id)
        if (
            isinstance(now_epoch_ms, bool)
            or not isinstance(now_epoch_ms, int)
            or now_epoch_ms < 0
        ):
            raise ValueError("now_epoch_ms must be a non-negative integer")
        created_at_utc = _utc_timestamp(created_at_utc, "created_at_utc")
        ttl_seconds = message["ttl_seconds"]
        deadline_epoch_ms = (
            0 if ttl_seconds == 0 else now_epoch_ms + int(ttl_seconds) * 1000
        )
        normalized_routes = tuple(
            sorted(
                {
                    (
                        _canonical_agent_id(recipient, "recipient_agent_id"),
                        _canonical_endpoint_id(endpoint, "endpoint_id"),
                    )
                    for recipient, endpoint in routes
                }
            )
        )
        if not normalized_routes:
            raise ValueError("routes must contain at least one recipient endpoint")
        deliveries = []
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for recipient_agent_id, endpoint_id in normalized_routes:
                delivery_id = _derive_delivery_id(
                    workspace_id,
                    scope_kind,
                    scope_identity,
                    message_id,
                    recipient_agent_id,
                    endpoint_id,
                )
                row = self._connection.execute(
                    "SELECT recipient_agent_id, endpoint_id FROM canonical_deliveries "
                    "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
                    "AND message_id = ? AND delivery_id = ?",
                    (workspace_id, scope_kind, scope_identity, message_id, delivery_id),
                ).fetchone()
                if row is not None:
                    if row != (recipient_agent_id, endpoint_id):
                        raise CanonicalIntegrityError(
                            "canonical delivery_id conflicts with different route identity"
                        )
                    deliveries.append((delivery_id, False))
                    continue
                self._connection.execute(
                    "INSERT INTO canonical_deliveries "
                    "(workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
                    "recipient_agent_id, endpoint_id, deadline_epoch_ms, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        scope_kind,
                        scope_identity,
                        message_id,
                        delivery_id,
                        recipient_agent_id,
                        endpoint_id,
                        deadline_epoch_ms,
                        created_at_utc,
                    ),
                )
                deliveries.append((delivery_id, True))
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return tuple(deliveries)

    def create_canonical_delivery_attempt(
        self,
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
        self.canonical_preflight(write=True)
        workspace_id, scope_kind, scope_identity = _canonical_scope(
            workspace_id, scope_kind, scope_identity
        )
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        message_id = _canonical_message_id(message_id, "message_id")
        delivery_id = _canonical_delivery_id(delivery_id, "delivery_id")
        if (
            isinstance(attempt_index, bool)
            or not isinstance(attempt_index, int)
            or attempt_index < 0
        ):
            raise ValueError("attempt_index must be a non-negative integer")
        if (
            isinstance(attempt_epoch_ms, bool)
            or not isinstance(attempt_epoch_ms, int)
            or attempt_epoch_ms < 0
        ):
            raise ValueError("attempt_epoch_ms must be a non-negative integer")
        created_at_utc = _utc_timestamp(created_at_utc, "created_at_utc")
        attempt_id = _derive_attempt_id(
            workspace_id,
            scope_kind,
            scope_identity,
            message_id,
            delivery_id,
            attempt_index,
        )
        row = self._connection.execute(
            "SELECT attempt_epoch_ms, created_at_utc FROM canonical_delivery_attempts "
            "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
            "AND message_id = ? AND delivery_id = ? AND attempt_id = ?",
            (workspace_id, scope_kind, scope_identity, message_id, delivery_id, attempt_id),
        ).fetchone()
        if row is not None:
            if row != (attempt_epoch_ms, created_at_utc):
                raise CanonicalConflictError(
                    "canonical delivery attempt conflicts with different metadata"
                )
            return attempt_id, False
        self._connection.execute(
            "INSERT INTO canonical_delivery_attempts "
            "(workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
            "attempt_id, attempt_index, attempt_epoch_ms, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                attempt_id,
                attempt_index,
                attempt_epoch_ms,
                created_at_utc,
            ),
        )
        return attempt_id, True

    def append_canonical_delivery_receipt(
        self,
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
        self.canonical_preflight(write=True)
        workspace_id, scope_kind, scope_identity = _canonical_scope(
            workspace_id, scope_kind, scope_identity
        )
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        message_id = _canonical_message_id(message_id, "message_id")
        delivery_id = _canonical_delivery_id(delivery_id, "delivery_id")
        attempt_id = _canonical_attempt_id(attempt_id, "attempt_id")
        session_ref_id = _optional_session_ref_id(session_ref_id)
        created_at_utc = _utc_timestamp(created_at_utc, "created_at_utc")
        body, evidence_sha256, state, quality, evidence_kind = _normalize_evidence(evidence)
        attempt_context = self._connection.execute(
            "SELECT d.endpoint_id FROM canonical_delivery_attempts AS a "
            "JOIN canonical_deliveries AS d ON d.workspace_id = a.workspace_id "
            "AND d.scope_kind = a.scope_kind AND d.scope_identity = a.scope_identity "
            "AND d.message_id = a.message_id AND d.delivery_id = a.delivery_id "
            "WHERE a.workspace_id = ? AND a.scope_kind = ? AND a.scope_identity = ? "
            "AND a.message_id = ? AND a.delivery_id = ? AND a.attempt_id = ?",
            (
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                attempt_id,
            ),
        ).fetchone()
        if attempt_context is None:
            raise CanonicalIntegrityError("canonical delivery attempt is missing")
        _validate_receipt_evidence_contract(
            evidence,
            workspace_id=workspace_id,
            scope_kind=scope_kind,
            scope_identity=scope_identity,
            message_id=message_id,
            delivery_id=delivery_id,
            attempt_id=attempt_id,
            endpoint_id=str(attempt_context[0]),
            session_ref_id=session_ref_id,
        )
        receipt_id = _derive_receipt_id(
            workspace_id,
            scope_kind,
            scope_identity,
            message_id,
            delivery_id,
            attempt_id,
            evidence_sha256,
        )
        row = self._connection.execute(
            "SELECT state, quality, evidence_kind, session_ref_id, created_at_utc "
            "FROM canonical_delivery_receipts WHERE workspace_id = ? AND scope_kind = ? "
            "AND scope_identity = ? AND message_id = ? AND delivery_id = ? "
            "AND attempt_id = ? AND receipt_id = ?",
            (
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                attempt_id,
                receipt_id,
            ),
        ).fetchone()
        if row is not None:
            if row != (state, quality, evidence_kind, session_ref_id, created_at_utc):
                raise CanonicalConflictError(
                    "canonical delivery receipt conflicts with different metadata"
                )
            self._read_canonical_receipt(
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                attempt_id,
                receipt_id,
            )
            return receipt_id, False
        body_row = self._connection.execute(
            "SELECT byte_size, body FROM canonical_evidence_bodies "
            "WHERE workspace_id = ? AND evidence_sha256 = ?",
            (workspace_id, evidence_sha256),
        ).fetchone()
        if body_row is not None and body_row != (len(body), body):
            raise CanonicalConflictError(
                "canonical evidence hash conflicts with different bytes"
            )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if body_row is None:
                self._connection.execute(
                    "INSERT INTO canonical_evidence_bodies "
                    "(workspace_id, evidence_sha256, byte_size, body, created_at_utc) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (workspace_id, evidence_sha256, len(body), body, created_at_utc),
                )
            self._connection.execute(
                "INSERT INTO canonical_delivery_receipts "
                "(workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
                "attempt_id, receipt_id, evidence_sha256, state, quality, evidence_kind, "
                "session_ref_id, created_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id,
                    scope_kind,
                    scope_identity,
                    message_id,
                    delivery_id,
                    attempt_id,
                    receipt_id,
                    evidence_sha256,
                    state,
                    quality,
                    evidence_kind,
                    session_ref_id,
                    created_at_utc,
                ),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return receipt_id, True

    def read_canonical_delivery(
        self,
        *,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
        delivery_id: str,
    ) -> dict[str, object] | None:
        self.canonical_preflight(write=False)
        workspace_id, scope_kind, scope_identity = _canonical_scope(
            workspace_id, scope_kind, scope_identity
        )
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        return self._read_canonical_delivery(
            workspace_id,
            scope_kind,
            scope_identity,
            _canonical_message_id(message_id, "message_id"),
            _canonical_delivery_id(delivery_id, "delivery_id"),
        )

    def read_canonical_receipt(
        self,
        *,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
        delivery_id: str,
        attempt_id: str,
        receipt_id: str,
    ) -> dict[str, object] | None:
        self.canonical_preflight(write=False)
        workspace_id, scope_kind, scope_identity = _canonical_scope(
            workspace_id, scope_kind, scope_identity
        )
        self._validate_canonical_scope(workspace_id, scope_kind, scope_identity)
        return self._read_canonical_receipt(
            workspace_id,
            scope_kind,
            scope_identity,
            _canonical_message_id(message_id, "message_id"),
            _canonical_delivery_id(delivery_id, "delivery_id"),
            _canonical_attempt_id(attempt_id, "attempt_id"),
            _canonical_receipt_id(receipt_id, "receipt_id"),
        )

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
        body = result["body"]
        byte_size = result["byte_size"]
        body_sha256 = result["body_sha256"]
        if (
            not isinstance(body, bytes)
            or not isinstance(byte_size, int)
            or len(body) != byte_size
            or hashlib.sha256(body).hexdigest() != body_sha256
        ):
            raise CanonicalIntegrityError(
                "canonical body failed size or SHA-256 verification"
            )
        # The seal prevents post-publication appends; this detects a forged whole
        # message inserted through direct SQL in one transaction. Append-only rows
        # are never repaired here.
        derived_message_id = _derive_message_id(
            workspace_id=str(result["workspace_id"]),
            scope_kind=str(result["scope_kind"]),
            scope_identity=str(result["scope_identity"]),
            sender_agent_id=str(result["sender_agent_id"]),
            dedupe_key=str(result["dedupe_key"]),
            body_sha256=str(result["body_sha256"]),
            recipients=result["recipients"],  # type: ignore[arg-type]
            reply_to_message_id=result["reply_to_message_id"],  # type: ignore[arg-type]
            ttl_seconds=result["ttl_seconds"],  # type: ignore[arg-type]
            ack_policy=str(result["ack_policy"]),
            artifacts=result["artifacts"],  # type: ignore[arg-type]
            title=str(result["title"]),
            priority=str(result["priority"]),
            tags=result["tags"],  # type: ignore[arg-type]
            chat_link=result["chat_link"],  # type: ignore[arg-type]
            task_link=result["task_link"],  # type: ignore[arg-type]
        )
        if derived_message_id != result["message_id"]:
            raise CanonicalIntegrityError(
                "canonical message_id does not match its normalized immutable intent"
            )
        return result

    def _read_canonical_evidence_body(
        self, workspace_id: str, evidence_sha256: str
    ) -> tuple[dict[str, object], bytes]:
        row = self._connection.execute(
            "SELECT byte_size, body FROM canonical_evidence_bodies "
            "WHERE workspace_id = ? AND evidence_sha256 = ?",
            (workspace_id, evidence_sha256),
        ).fetchone()
        if row is None:
            raise CanonicalIntegrityError("canonical evidence body is missing")
        byte_size, body = row
        if (
            not isinstance(body, bytes)
            or not isinstance(byte_size, int)
            or len(body) != byte_size
            or hashlib.sha256(body).hexdigest() != evidence_sha256
        ):
            raise CanonicalIntegrityError(
                "canonical evidence failed size or SHA-256 verification"
            )
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CanonicalIntegrityError("canonical evidence is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise CanonicalIntegrityError("canonical evidence is not a JSON object")
        normalized_body, normalized_sha, _state, _quality, _kind = _normalize_evidence(parsed)
        if normalized_sha != evidence_sha256 or normalized_body != body:
            raise CanonicalIntegrityError(
                "canonical evidence bytes are not canonical for their integrity"
            )
        return parsed, body

    @staticmethod
    def _delivery_outcome(receipts: Iterable[dict[str, object]]) -> tuple[str, dict[str, object] | None]:
        best: tuple[int, str, dict[str, object]] | None = None
        for receipt in receipts:
            rank, outcome = _DELIVERY_OUTCOME_BY_STATE[str(receipt["state"])]
            candidate = (rank, str(receipt["receipt_id"]), receipt)
            if best is None or rank > best[0] or (rank == best[0] and candidate[1] < best[1]):
                best = candidate
        if best is None:
            return "pending", None
        return _DELIVERY_OUTCOME_BY_STATE[str(best[2]["state"])][1], best[2]

    def _read_canonical_receipt(
        self,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
        delivery_id: str,
        attempt_id: str,
        receipt_id: str,
    ) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
            "attempt_id, receipt_id, evidence_sha256, state, quality, evidence_kind, "
            "session_ref_id, created_at_utc FROM canonical_delivery_receipts "
            "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
            "AND message_id = ? AND delivery_id = ? AND attempt_id = ? AND receipt_id = ?",
            (
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                attempt_id,
                receipt_id,
            ),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "workspace_id",
            "scope_kind",
            "scope_identity",
            "message_id",
            "delivery_id",
            "attempt_id",
            "receipt_id",
            "evidence_sha256",
            "state",
            "quality",
            "evidence_kind",
            "session_ref_id",
            "created_at_utc",
        )
        result = dict(zip(keys, row))
        evidence, evidence_body = self._read_canonical_evidence_body(
            str(result["workspace_id"]), str(result["evidence_sha256"])
        )
        derived_receipt_id = _derive_receipt_id(
            str(result["workspace_id"]),
            str(result["scope_kind"]),
            str(result["scope_identity"]),
            str(result["message_id"]),
            str(result["delivery_id"]),
            str(result["attempt_id"]),
            str(result["evidence_sha256"]),
        )
        if derived_receipt_id != result["receipt_id"]:
            raise CanonicalIntegrityError(
                "canonical receipt_id does not match its immutable receipt tuple"
            )
        if (
            evidence.get("state") != result["state"]
            or evidence.get("quality") != result["quality"]
            or evidence.get("evidence_kind") != result["evidence_kind"]
        ):
            raise CanonicalIntegrityError(
                "canonical receipt fold columns do not match evidence bytes"
            )
        attempt = self._connection.execute(
            "SELECT d.recipient_agent_id, d.endpoint_id, a.attempt_index "
            "FROM canonical_delivery_attempts AS a "
            "JOIN canonical_deliveries AS d ON d.workspace_id = a.workspace_id "
            "AND d.scope_kind = a.scope_kind AND d.scope_identity = a.scope_identity "
            "AND d.message_id = a.message_id AND d.delivery_id = a.delivery_id "
            "WHERE a.workspace_id = ? AND a.scope_kind = ? AND a.scope_identity = ? "
            "AND a.message_id = ? AND a.delivery_id = ? AND a.attempt_id = ?",
            (
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                attempt_id,
            ),
        ).fetchone()
        if attempt is None:
            raise CanonicalIntegrityError("canonical receipt attempt is missing")
        expected_delivery_id = _derive_delivery_id(
            str(result["workspace_id"]),
            str(result["scope_kind"]),
            str(result["scope_identity"]),
            str(result["message_id"]),
            str(attempt[0]),
            str(attempt[1]),
        )
        if expected_delivery_id != result["delivery_id"]:
            raise CanonicalIntegrityError(
                "canonical delivery_id does not match its immutable route tuple"
            )
        expected_attempt_id = _derive_attempt_id(
            str(result["workspace_id"]),
            str(result["scope_kind"]),
            str(result["scope_identity"]),
            str(result["message_id"]),
            str(result["delivery_id"]),
            int(attempt[2]),
        )
        if expected_attempt_id != result["attempt_id"]:
            raise CanonicalIntegrityError(
                "canonical attempt_id does not match its immutable attempt tuple"
            )
        _validate_receipt_evidence_contract(
            evidence,
            workspace_id=str(result["workspace_id"]),
            scope_kind=str(result["scope_kind"]),
            scope_identity=str(result["scope_identity"]),
            message_id=str(result["message_id"]),
            delivery_id=str(result["delivery_id"]),
            attempt_id=str(result["attempt_id"]),
            endpoint_id=str(attempt[1]),
            session_ref_id=result["session_ref_id"],  # type: ignore[arg-type]
        )
        result["evidence"] = evidence
        result["evidence_body"] = evidence_body
        return result

    def _read_canonical_delivery(
        self,
        workspace_id: str,
        scope_kind: str,
        scope_identity: str,
        message_id: str,
        delivery_id: str,
    ) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT workspace_id, scope_kind, scope_identity, message_id, delivery_id, "
            "recipient_agent_id, endpoint_id, deadline_epoch_ms, created_at_utc "
            "FROM canonical_deliveries WHERE workspace_id = ? AND scope_kind = ? "
            "AND scope_identity = ? AND message_id = ? AND delivery_id = ?",
            (workspace_id, scope_kind, scope_identity, message_id, delivery_id),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "workspace_id",
            "scope_kind",
            "scope_identity",
            "message_id",
            "delivery_id",
            "recipient_agent_id",
            "endpoint_id",
            "deadline_epoch_ms",
            "created_at_utc",
        )
        result = dict(zip(keys, row))
        expected_delivery_id = _derive_delivery_id(
            str(result["workspace_id"]),
            str(result["scope_kind"]),
            str(result["scope_identity"]),
            str(result["message_id"]),
            str(result["recipient_agent_id"]),
            str(result["endpoint_id"]),
        )
        if expected_delivery_id != result["delivery_id"]:
            raise CanonicalIntegrityError(
                "canonical delivery_id does not match its immutable route tuple"
            )
        receipts = []
        for receipt_row in self._connection.execute(
            "SELECT attempt_id, receipt_id FROM canonical_delivery_receipts "
            "WHERE workspace_id = ? AND scope_kind = ? AND scope_identity = ? "
            "AND message_id = ? AND delivery_id = ? ORDER BY attempt_id, receipt_id",
            (workspace_id, scope_kind, scope_identity, message_id, delivery_id),
        ):
            receipt = self._read_canonical_receipt(
                workspace_id,
                scope_kind,
                scope_identity,
                message_id,
                delivery_id,
                receipt_row[0],
                receipt_row[1],
            )
            if receipt is not None:
                receipts.append(receipt)
        outcome, selected = self._delivery_outcome(receipts)
        result["outcome"] = outcome
        result["receipts"] = tuple(receipts)
        result["selected_receipt"] = selected
        result["attempt_id"] = selected["attempt_id"] if selected is not None else None
        result["evidence"] = selected["evidence"] if selected is not None else None
        result["session_ref_id"] = selected["session_ref_id"] if selected is not None else None
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

    def observation_scheduler_cursor(self, *, workspace_id: str, source_id: str) -> str | None:
        self._ensure_thread()
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if source_id != "chats_mailbox":
            raise ValueError("source_id must be chats_mailbox")
        row = self._connection.execute(
            "SELECT next_project_id FROM observation_scheduler_cursors "
            "WHERE workspace_id = ? AND source_id = ?",
            (workspace_id, source_id),
        ).fetchone()
        return None if row is None else row[0]

    def advance_observation_scheduler_cursor(
        self,
        *,
        workspace_id: str,
        source_id: str,
        registry_revision: str,
        next_project_id: str,
        updated_at_utc: str,
    ) -> None:
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot advance observation scheduler")
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if source_id != "chats_mailbox":
            raise ValueError("source_id must be chats_mailbox")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", registry_revision):
            raise ValueError("registry_revision must be sha256:<lowercase hex>")
        validate_project_id(next_project_id)
        if (
            not isinstance(updated_at_utc, str)
            or not updated_at_utc
            or "\x00" in updated_at_utc
        ):
            raise ValueError("updated_at_utc is required")
        if not self.has_registry_snapshot(
            workspace_id=workspace_id, registry_revision=registry_revision
        ):
            raise ValueError("registry snapshot is absent")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "INSERT INTO observation_scheduler_cursors "
                "(workspace_id, source_id, registry_revision, next_project_id, updated_at_utc) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (workspace_id, source_id) DO UPDATE SET "
                "registry_revision = excluded.registry_revision, "
                "next_project_id = excluded.next_project_id, "
                "updated_at_utc = excluded.updated_at_utc",
                (
                    workspace_id,
                    source_id,
                    registry_revision,
                    next_project_id,
                    updated_at_utc,
                ),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def clear_observation_scheduler_cursor(
        self, *, workspace_id: str, source_id: str
    ) -> None:
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot clear observation scheduler")
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        if source_id != "chats_mailbox":
            raise ValueError("source_id must be chats_mailbox")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "DELETE FROM observation_scheduler_cursors "
                "WHERE workspace_id = ? AND source_id = ?",
                (workspace_id, source_id),
            )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

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

    def prune_observation_audit(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_id: str,
        keep_latest: int,
        limit: int = 500,
    ) -> int:
        """Delete old audit rows beyond one project/source newest-N tail.

        This is intentionally not audited: writing an audit row for audit
        compaction would recreate the unbounded growth this retention policy
        exists to stop. Callers charge one maintenance unit per deleted row.
        """
        self._ensure_thread()
        if self._read_only:
            raise PermissionError("query-only readers cannot prune observation audit")
        if validate_workspace_id(workspace_id) != self.paths.workspace_id:
            raise ValueError("workspace_id does not own this ledger")
        validate_project_id(project_id)
        if source_id != "chats_mailbox":
            raise ValueError("source_id must be chats_mailbox")
        if (
            isinstance(keep_latest, bool)
            or not isinstance(keep_latest, int)
            or not 1 <= keep_latest <= 200
        ):
            raise ValueError("audit retention tail must be between 1 and 200 rows")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("audit retention prune limit must be between 1 and 500")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            removed = self._connection.execute(
                "DELETE FROM observation_audit WHERE rowid IN ("
                "SELECT rowid FROM observation_audit WHERE workspace_id = ? "
                "AND project_id = ? AND source_id = ? "
                "AND rowid NOT IN ("
                "SELECT rowid FROM observation_audit WHERE workspace_id = ? "
                "AND project_id = ? AND source_id = ? "
                "ORDER BY audit_id DESC, registry_revision DESC, rowid DESC LIMIT ?) "
                "ORDER BY audit_id ASC, registry_revision ASC, rowid ASC LIMIT ?)",
                (
                    workspace_id,
                    project_id,
                    source_id,
                    workspace_id,
                    project_id,
                    source_id,
                    keep_latest,
                    limit,
                ),
            ).rowcount
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
            "AND source_id = ?",
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
