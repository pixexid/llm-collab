"""Exact-byte, fail-closed registry snapshots for observation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from llm_collab.ledger.paths import validate_project_id, validate_workspace_id

from .gate import read_exact_nofollow


SOURCE_ID = "chats_mailbox"
SOURCE_PATHS = ("Chats/*/*.md", "agents/*/inbox.json")


class RegistryError(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise RegistryError("duplicate projects.json member")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise RegistryError(f"non-JSON numeric constant: {value}")


@dataclass(frozen=True)
class RegistrySnapshot:
    workspace_id: str
    registry_revision: str
    registry_source_sha256: str
    captured_at_utc: str
    workspace_snapshot_json: str
    project_snapshots: dict[str, str]
    source_snapshots: dict[str, dict[str, str]]

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.project_snapshots))

    def record(self, store: object) -> None:
        if store.has_registry_snapshot(
            workspace_id=self.workspace_id,
            registry_revision=self.registry_revision,
        ):
            return
        store.record_registry_snapshot(
            workspace_id=self.workspace_id,
            registry_revision=self.registry_revision,
            registry_source_sha256=self.registry_source_sha256,
            captured_at_utc=self.captured_at_utc,
            workspace_snapshot_json=self.workspace_snapshot_json,
            project_snapshots=self.project_snapshots,
            source_snapshots=self.source_snapshots,
        )


def read_registry_snapshot(
    path: Path,
    *,
    workspace_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> RegistrySnapshot:
    """Validate the complete registry before returning any persistable rows."""
    validate_workspace_id(workspace_id)
    raw = read_exact_nofollow(path, maximum_bytes=16 * 1024 * 1024)
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RegistryError) as exc:
        raise RegistryError("projects.json must be duplicate-free UTF-8 JSON") from exc
    if not isinstance(parsed, dict) or "projects" not in parsed:
        raise RegistryError("projects.json must contain a projects array")
    declared_workspace = parsed.get("workspace_id", workspace_id)
    if declared_workspace != workspace_id:
        raise RegistryError("projects.json workspace_id does not match this ledger")
    entries = parsed["projects"]
    if not isinstance(entries, list) or not entries:
        raise RegistryError("projects.json projects must be a non-empty array")

    projects: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryError("each projects.json project must be one object")
        aliases = [entry[key] for key in ("id", "project_id") if key in entry]
        if not aliases or any(alias != aliases[0] for alias in aliases[1:]):
            raise RegistryError("project identity is missing, null, or conflicting")
        try:
            project_id = validate_project_id(aliases[0])
        except ValueError as exc:
            raise RegistryError("project identity is invalid") from exc
        if project_id in projects:
            raise RegistryError("project identities must be unique")
        projects[project_id] = json.dumps(
            entry, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )

    digest = hashlib.sha256(raw).hexdigest()
    captured = clock().astimezone(timezone.utc).isoformat()
    workspace_snapshot = json.dumps(
        {
            "workspace_id": workspace_id,
            "projects": sorted(projects),
            "projects_json_exact_utf8": raw.decode("utf-8"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    source_snapshot = json.dumps(
        {
            "source_id": SOURCE_ID,
            "path_patterns": list(SOURCE_PATHS),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return RegistrySnapshot(
        workspace_id=workspace_id,
        registry_revision=f"sha256:{digest}",
        registry_source_sha256=digest,
        captured_at_utc=captured,
        workspace_snapshot_json=workspace_snapshot,
        project_snapshots=projects,
        source_snapshots={
            project_id: {SOURCE_ID: source_snapshot} for project_id in projects
        },
    )
