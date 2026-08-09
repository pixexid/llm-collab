#!/usr/bin/env python3
"""Record the resolved (provider, model, reasoning_level) triple for one BB thread.

GH-630 first scope / GH-617. The bb ``exec-tracking`` plugin's ``thread.created``
handler resolves the executed profile via ``bb.sdk.threads.defaultExecutionOptions``
and passes the *resolved primitive values* here as CLI args; this script is the
write authority. Two design points are load-bearing:

- **Resolved values, never a mutable reference.** GH-617's whole point is that
  storing a preset name lets editing the preset retroactively rewrite what a
  historical dispatch resolved to. The handler snapshots the resolved triple to
  strings before invoking this script, and this script serializes them
  immediately — no object reference survives that could later change meaning.

- **Records, never gates.** A ``thread.created`` handler is fire-and-forget with
  no veto hook (GH-630 probe). Enforcement stays at our CLI call sites; this only
  records what ran.

State lives at ``<workspace_root>/records/executed-triples/<project_id>.jsonl``:
git-tracked and diffable (JSONL, one record per line, canonical compact keys so
unchanged rows stay byte-identical across writes), and project-scoped by the
``project_id`` path segment so two projects never collide (Project Boundary rule).
The volatile ``project_state_root`` (``projects/``) is gitignored by design for
inbox/queue state, so a durable attribution record cannot live there and be
diffable; this dedicated tracked directory is the smallest path change that
satisfies both. Moving it is a one-line edit in :func:`record_path`.

A row is written for *every* ``thread.created`` this script is invoked for,
including one whose profile could not be resolved: the failure is recorded
explicitly (``status: "unresolved"`` + typed ``failure_reason``) so an absent row
and a failed resolution are distinguishable — never silently omitted.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPT_DIR)]
from _python_runtime import require_python  # noqa: E402

require_python()

from _helpers import find_workspace_root, utc_iso, write_file_durably  # noqa: E402
from _bounded_io import (  # noqa: E402
    ReadBudget,
    UnreadableFile,
    active_read_budget,
    read_regular_file_bounded,
)

ROOT: Path = find_workspace_root()

# A runaway record log is an accident (a writer loop, a corrupt append), not real
# data. AGENTS.md "Bounded work fails closed and never truncates": begin the
# budget at the earliest untrusted parse boundary, keep it cumulative within one
# run, raise on exceed with no partial state. The read below charges this budget;
# exceeding it raises before any write, so no partial rewrite lands.
RECORD_FILE_BUDGET_BYTES = 8 * 1024 * 1024  # 8 MiB

# A single safe filesystem path segment: project_id forms the record filename, so
# reject anything that could escape records/executed-triples/ (slash, traversal,
# NUL, empty). project_id is our own config, but it reaches a path, so bound it.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

RESOLVED = "resolved"
UNRESOLVED = "unresolved"

# Typed failure reasons. An absent row and a failed resolution must be
# distinguishable, so each shape a resolution can fail in gets a stable label.
REASON_NOT_RESOLVED = "profile_not_resolved"  # defaultExecutionOptions returned null
REASON_RESOLUTION_ERROR = "profile_resolution_error"  # defaultExecutionOptions threw
REASON_INCOMPLETE = "profile_incomplete"  # resolved object missing a required field


def record_path(project_id: str) -> Path:
    """Git-tracked, project-scoped JSONL path for one project's executed triples."""
    return ROOT / "records" / "executed-triples" / f"{project_id}.jsonl"


def _lock_path(project_id: str) -> Path:
    return record_path(project_id).with_suffix(".jsonl.lock")


@contextlib.contextmanager
def _write_lock(project_id: str):
    """Serialize the read-modify-write upsert on one project's record file.

    thread.created can fire for different threads near-simultaneously, and each
    spawns its own child running this script against the SAME project file. One
    exclusive flock per file makes each upsert atomic against the others, so a
    concurrent writer cannot lose another's row. ponytail: per-file lock, as
    narrow as the contention (all writers to one project file)."""
    lock = _lock_path(project_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _reject_nul(label: str, value: str) -> str:
    if "\x00" in value:
        raise SystemExit(f"--{label} contains an embedded NUL; refusing to record it")
    return value


def _load_existing(path: Path) -> list[dict]:
    """Read and parse the JSONL log under one cumulative budget. Fails closed.

    A malformed or shape-invalid record line is corruption in our own append-only
    log, not a truncation to swallow: raise so it is surfaced and recoverable from
    git, never silently dropped (a dropped line is an attribution that vanishes)."""
    budget = ReadBudget(RECORD_FILE_BUDGET_BYTES, label="executed-triples record log")
    try:
        with active_read_budget(budget):
            raw = read_regular_file_bounded(path, RECORD_FILE_BUDGET_BYTES)
    except FileNotFoundError:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SystemExit(f"{path}: not valid UTF-8 ({error}); refusing to rewrite a corrupt log") from error
    rows: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path}:{lineno}: malformed JSON record ({error}); refusing to drop it silently") from error
        if not isinstance(obj, dict) or "thread_id" not in obj or "status" not in obj:
            raise SystemExit(f"{path}:{lineno}: record missing thread_id/status; refusing to rewrite a corrupt log")
        rows.append(obj)
    return rows


def _serialize(row: dict) -> str:
    # Canonical compact form with sorted keys: a row that did not change serializes
    # to identical bytes across rewrites, so a commit diff shows only the row that
    # actually changed (the upserted one), not spurious churn across the file.
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _upsert(path: Path, rows: list[dict], new_row: dict) -> None:
    """Replace any existing row for this thread_id, then append. One row per thread."""
    kept = [row for row in rows if row.get("thread_id") != new_row["thread_id"]]
    kept.append(new_row)
    content = "".join(_serialize(row) + "\n" for row in kept)
    write_file_durably(path, content)


def _build_resolved_row(project_id: str, thread_id: str, provider: str | None,
                        model: str, reasoning_level: str, source: str) -> dict:
    return {
        "thread_id": thread_id,
        "project_id": project_id,
        "provider": provider,
        "model": model,
        "reasoning_level": reasoning_level,
        "source": source,
        "status": RESOLVED,
        "recorded_at": utc_iso(),
    }


def _build_unresolved_row(project_id: str, thread_id: str, provider: str | None,
                          reason: str, detail: str | None = None) -> dict:
    row = {
        "thread_id": thread_id,
        "project_id": project_id,
        "provider": provider,
        "status": UNRESOLVED,
        "failure_reason": reason,
        "recorded_at": utc_iso(),
    }
    if detail:
        row["failure_detail"] = detail[:400]
    return row


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--project", required=True, help="llm-collab project_id (scopes the record file)")
    result.add_argument("--thread-id", required=True, help="BB thread id whose profile executed")
    result.add_argument("--provider", default=None, help="providerId from the thread.created event DTO (may be omitted)")
    result.add_argument("--model", help="resolved model (resolved case)")
    result.add_argument("--reasoning-level", help="resolved reasoning level (resolved case)")
    result.add_argument("--source", help="resolved execution source / provenance (resolved case)")
    result.add_argument("--unresolved", metavar="REASON", help="record a typed resolution failure instead of a resolved triple")
    result.add_argument("--failure-detail", default=None, help="short detail for an unresolved row (e.g. the resolution error)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    project_id = args.project.strip()
    if not _PROJECT_ID_RE.match(project_id):
        raise SystemExit(f"--project {args.project!r} is not a safe single path segment")
    thread_id = _reject_nul("thread-id", args.thread_id)
    if not thread_id.strip():
        raise SystemExit("--thread-id is empty")

    provider = _reject_nul("provider", args.provider) if args.provider else None

    path = record_path(project_id)

    if args.unresolved:
        row = _build_unresolved_row(project_id, thread_id, provider, args.unresolved, args.failure_detail)
    else:
        missing = [name for name, value in (("model", args.model), ("reasoning-level", args.reasoning_level), ("source", args.source)) if not value]
        if missing:
            raise SystemExit(f"resolved case requires --model/--reasoning-level/--source together; missing: {', '.join(missing)}")
        row = _build_resolved_row(
            project_id,
            thread_id,
            provider,
            _reject_nul("model", args.model),
            _reject_nul("reasoning-level", args.reasoning_level),
            _reject_nul("source", args.source),
        )

    with _write_lock(project_id):
        rows = _load_existing(path)
        _upsert(path, rows, row)

    rel = path.relative_to(ROOT) if _is_relative_to(path, ROOT) else path
    print(f"recorded {row['status']} {thread_id} -> {rel}")
    return 0


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
