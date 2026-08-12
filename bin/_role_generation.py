"""Read the active project orchestrator role record, fail closed."""

from __future__ import annotations

import json
import re

from _bounded_io import read_regular_file_bounded
from _helpers import project_state_dir

MAX_ROLE_GENERATION_BYTES = 64 * 1024
ROLE_RECORD_PATTERN = re.compile(
    r"^```json[ \t]*\r?\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL
)


class RoleGenerationError(RuntimeError):
    pass


def current_orchestrator_thread_id(project_id: str) -> str:
    path = project_state_dir(project_id) / "role-generation.md"
    try:
        text = read_regular_file_bounded(path, MAX_ROLE_GENERATION_BYTES).decode("utf-8")
    except Exception as error:
        raise RoleGenerationError(f"cannot read role generation record {path}: {error}") from error
    records = ROLE_RECORD_PATTERN.findall(text)
    if len(records) != 1:
        raise RoleGenerationError(
            "role generation record must contain exactly one fenced JSON record"
        )

    def reject_duplicates(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate JSON object member")
        return value

    try:
        record = json.loads(records[0], object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, ValueError) as error:
        raise RoleGenerationError(f"role generation record is malformed JSON: {error}") from error
    expected_role = f"orchestrator:{project_id}"
    if not isinstance(record, dict):
        raise RoleGenerationError("role generation record is not a JSON object")
    if record.get("role_id") != expected_role:
        raise RoleGenerationError(f"role generation record does not name {expected_role!r}")
    if record.get("scope") != {"kind": "project", "project_id": project_id}:
        raise RoleGenerationError("role generation record does not have exact project scope")
    if record.get("status") != "active":
        raise RoleGenerationError("role generation record is not active")
    epoch = record.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise RoleGenerationError("role generation record epoch is not a positive integer")
    thread_id = record.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id or thread_id != thread_id.strip():
        raise RoleGenerationError(
            "role generation record thread_id is not non-empty unpadded text"
        )
    return thread_id
