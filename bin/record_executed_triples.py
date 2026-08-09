#!/usr/bin/env python3
"""Record the executed (provider, model, reasoning_level) triple for one BB thread.

GH-710 / GH-630 first scope / GH-617. The bb ``exec-tracking`` plugin's
``thread.created`` handler reads ``bb.sdk.threads.defaultExecutionOptions`` and
passes the *resolved primitive values* + their ``source`` here as CLI args; this
script is the write authority.

**This artifact records executed-triple evidence: which triple actually ran.**
That is the invariant the plugin exists to serve — it is what makes the
frozen-triple rule auditable. "Creation-time defaults" was a *proxy* for that
invariant, chosen before GH-706 established the proxy is unobservable: no surface
on this SDK carries creation-time execution options. The artifact is therefore
defined by the repository's existing authority for this decision:
``llm_collab/bb_client.py:21-24`` states that the spawn envelope carries no model
and no reasoning level, so argv alone cannot prove which profile actually ran,
and that **the authoritative record is the ``execution`` block on the thread's
``client/turn/requested`` event** — the block ``BbClient`` itself validates the
requested profile against (``SPAWN_EVENT_TYPE`` at ``bb_client.py:67``,
``_execution_evidence()`` at ``bb_client.py:633``). This recorder accepts the
same source that authority accepts, so the repository has ONE authority for
"which triple ran", not two. This is a definition, not a sampling convenience:
``client/turn/requested`` is accepted because ``bb_client.py`` already decided it
is the proof, not because it is the one source we can get.

The gate stays a gate — it admits ONE named source:

- ``client/turn/requested`` — **accepted**. The executed-evidence source above.
  The row records with ``evidence: executed`` and the resolved triple.
- ``client/thread/start`` — **refused with its own distinct reason**
  (``ignored thread_start_not_executed``). On this SDK its payload carries no
  execution options (GH-706), so it cannot be executed evidence. It is handled
  EXPLICITLY, not folded into the unrecognised refusal: if ``client/thread/start``
  ever appears carrying execution options, that distinct marker surfaces it
  instead of silently dropping it.
- anything else (``client/turn/start``, absent, or unrecognised) — **refused**
  via ``ignored out_of_contract``, source named. Admitting one named source does
  not remove the refusal for everything else.

Why the artifact was redefined rather than its gate widened: GH-695 head 3
refused turn-derived sources so an artifact documented as *creation defaults*
could not quietly fill with executed evidence. That refusal was correct for an
artifact so named — and it is exactly why this change renames the artifact
(``thread-creation-defaults.jsonl`` must not survive holding turn-derived rows)
rather than loosening the old check. Under the new definition the accepted
source is correct by definition, not by exception.

Load-bearing design points (unchanged from the recorder's first slice):

- **Provenance is immutable once resolved.** GH-617's whole point is that storing
  a reference lets a later edit retroactively change what a historical dispatch
  resolved to; *replacing* a resolved row on a re-fire does the same thing by a
  different route. So a row that is already ``resolved`` is never overwritten. The
  only legal write against an existing row is ``unresolved -> resolved`` (the
  record completing). A re-fire of the SAME resolved triple is a no-op; a re-fire
  of a DIFFERENT resolved triple (or resolved -> unresolved) keeps the first and
  surfaces a ``conflict`` marker so a changed preset is visible, not invisible.

- **Resolved values, never a mutable reference.** Even on the completing write,
  the row stores the resolved primitive values, never a preset name.

- **Records, never gates.** A ``thread.created`` handler is fire-and-forget with
  no veto hook (GH-630 probe). Enforcement stays at our CLI call sites.

- **Exact identifiers, never normalized.** ``--project`` is matched against
  ``projects.json`` RAW — whitespace variants are rejected, not repaired, because
  repairing operator configuration silently is how a typo becomes authoritative
  state (GH-630 review, N2). The registry ``bb.project_id`` scope is likewise
  matched RAW: a padded ``bb.project_id`` is REJECTED, never stripped, so the
  recorder and ``spawn_gate`` enforce the same scope (GH-695 P2-D).

- **Project-scoped, registry-bound, and every loaded row is scope-checked.**
  ``--project`` must be an EXACT registered llm-collab project; the thread's bb
  project (``--thread-project``) must exactly match that project's ``bb.project_id``
  scope. Every row loaded from disk is validated against the requested
  ``project_id`` and a cross-project row fails closed rather than being silently
  reused (GH-695 P2-C).

- **Fail closed, observably.** Every refusal exits nonzero with a message on
  stderr so the plugin's async close-handler can log it; a silent refusal is
  indistinguishable from an event that never happened (GH-630 review, F5/N3).

State lives at ``{project_state_root}/{project_id}/thread-executed-triples.jsonl``
— the project state root the Project Boundary rule owns. The file name says what
the rows are (the triples that executed), never "creation defaults".
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPT_DIR)]
from _python_runtime import require_python  # noqa: E402

require_python()

from _helpers import (  # noqa: E402
    get_project,
    project_state_dir,
    utc_iso,
    write_file_durably,
)
from _bounded_io import (  # noqa: E402
    ReadBudget,
    UnreadableFile,
    active_read_budget,
    read_regular_file_bounded,
)

# A runaway record log is an accident (a writer loop, a corrupt append), not real
# data. AGENTS.md "Bounded work fails closed and never truncates": begin the
# budget at the earliest untrusted parse boundary and raise on exceed with no
# partial state. The budget bounds BOTH the read (an already-oversized log is
# refused) and the write (a write that would cross the boundary is refused BEFORE
# replacing the file, so a just-under-budget log plus one row refuses without
# landing oversized state — a wedge is worse than a refusal). GH-630 review F4.
RECORD_FILE_BUDGET_BYTES = 8 * 1024 * 1024  # 8 MiB

RESOLVED = "resolved"
UNRESOLVED = "unresolved"

# GH-710: this artifact records executed-triple evidence — which triple actually
# ran. The accepted source is ``client/turn/requested`` because
# ``llm_collab/bb_client.py:21-24`` names its ``execution`` block the
# authoritative record of the profile bb actually ran (the block ``BbClient``
# validates against); the recorder aligns with that authority rather than
# dissenting from it. ``client/thread/start`` is NOT executed evidence — its
# payload carries no execution options on this SDK (GH-706) — and is refused
# with its own distinct reason so a future ``thread/start`` that DOES carry
# options is surfaced, not silently dropped. Every other source is refused as
# out of contract: the gate admits one named source, it is not removed.
EVIDENCE_EXECUTED = "executed"   # the one evidence this artifact stores

# The one ``source`` this artifact records. main() refuses any other source
# before a row is built, so _build_resolved_row is only ever reached with this
# source.
_SOURCE_TURN_REQUESTED = "client/turn/requested"

# ``client/thread/start`` is handled EXPLICITLY (GH-710 acceptance): it is not
# unrecognised — it is a known creation-phase signal that carries no execution
# options on this SDK — so it gets its own refusal marker, distinct from
# ``out_of_contract``. If it ever carries execution options, this marker is the
# tripwire that makes the change visible.
_SOURCE_THREAD_START = "client/thread/start"

# Fields that define a resolved triple's identity. Two resolved rows for the same
# thread are the SAME triple iff all of these match; otherwise the second is a
# conflicting re-resolution and the first is kept (N1).
#
# GH-710: ``source`` remains EXCLUDED. Every stored resolved row has
# ``source == client/turn/requested`` by construction (main() refuses every other
# source), so ``source`` never varies between stored rows and carries no identity.
# If the contract ever admits a second source, ``source`` MUST return to identity
# -- without it, a row with the same (provider, model, reasoning_level) from a
# different source would no-op against the stored row and be silently discarded
# rather than surfaced as a conflict.
RESOLVED_IDENTITY_FIELDS = ("provider", "model", "reasoning_level")

# Typed failure reasons. An absent row and a failed resolution must be
# distinguishable, so each shape a resolution can fail in gets a stable label.
REASON_NOT_RESOLVED = "profile_not_resolved"  # defaultExecutionOptions returned null
REASON_RESOLUTION_ERROR = "profile_resolution_error"  # defaultExecutionOptions threw
REASON_INCOMPLETE = "profile_incomplete"  # resolved object missing a required field


def record_path(project_id: str) -> Path:
    """Project-scoped JSONL path under the configured project_state_root."""
    return project_state_dir(project_id) / "thread-executed-triples.jsonl"


def _lock_path(project_id: str) -> Path:
    path = record_path(project_id)
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def _write_lock(project_id: str):
    """Serialize the read-modify-write on one project's record file.

    thread.created can fire for different threads near-simultaneously, and each
    spawns its own child running this script against the SAME project file. One
    exclusive flock per file makes each decision+write atomic against the others.
    ponytail: per-file lock, as narrow as the contention (all writers to one file)."""
    lock = _lock_path(project_id)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
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


def _resolve_native_bb_project(entry: object) -> str:
    """The bb project this llm-collab project spawns under (the scope for --thread-project).

    Mirrors spawn_gate's ``bb.get("project_id", project_id)`` so the recorder and
    the spawner enforce the SAME scope. Matched RAW — a padded ``bb.project_id`` is
    REJECTED, never stripped: the recorder and spawn_gate must agree, and repairing
    a padded registry value in one authority but not the other is how they diverged
    (GH-695 P2-D). ``.strip()`` is used ONLY to test for empty/padded, never to
    transform the returned value."""
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
        raise SystemExit("registered project has no valid id; refusing to record")
    bb = entry.get("bb")
    native = bb.get("project_id", entry["id"]) if isinstance(bb, dict) else entry["id"]
    if not isinstance(native, str) or not native.strip():
        raise SystemExit(f"project {entry['id']!r} has no valid bb.project_id; refusing to record")
    if native != native.strip():
        raise SystemExit(
            f"project {entry['id']!r} bb.project_id {native!r} has surrounding whitespace; "
            "refusing to record (match raw, reject padded — GH-695 P2-D)"
        )
    return native


def _load_existing(path: Path, project_id: str) -> list[dict]:
    """Read and parse the JSONL log under one cumulative budget. Fails closed.

    Every loaded row is validated against the requested ``project_id`` (GH-695
    P2-C): a row whose ``project_id`` is missing or belongs to another project
    fails closed rather than being silently reused — a matching ``thread_id`` in a
    cross-project row must not make the recorder report no-op/conflict and preserve
    the wrong project's data.

    A malformed or shape-invalid record line is corruption in our own append-only
    log, not a truncation to swallow: raise so it is surfaced and recoverable,
    never silently dropped (a dropped line is an attribution that vanishes)."""
    budget = ReadBudget(RECORD_FILE_BUDGET_BYTES, label="thread-executed-triples record log")
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
        # GH-695 P2-C: validate project_id on every loaded row, fail closed. The
        # loader used to match on thread_id/status only, so a cross-project row was
        # accepted and a matching thread id preserved it.
        row_project = obj.get("project_id")
        if row_project != project_id:
            raise SystemExit(
                f"{path}:{lineno}: record project_id {row_project!r} != requested {project_id!r}; "
                "refusing to load a cross-project row (GH-695 P2-C)"
            )
        rows.append(obj)
    return rows


def _serialize(row: dict) -> str:
    # Canonical compact form with sorted keys: a row that did not change serializes
    # to identical bytes, so a re-write does not churn unchanged rows.
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _same_resolved(existing: dict, new_row: dict) -> bool:
    """Two resolved rows are the SAME triple iff every identity field matches."""
    if existing.get("status") != RESOLVED or new_row.get("status") != RESOLVED:
        return False
    return all(existing.get(f) == new_row.get(f) for f in RESOLVED_IDENTITY_FIELDS)


def _diff_summary(existing: dict, new_row: dict) -> str:
    for field in RESOLVED_IDENTITY_FIELDS:
        if existing.get(field) != new_row.get(field):
            return f"{field}: {existing.get(field)!r} -> {new_row.get(field)!r}"
    return f"status: {existing.get('status')!r} -> {new_row.get('status')!r}"


def _write_bounded(path: Path, rows: list[dict], thread_id: str) -> None:
    """Serialize + atomically replace, but only if the output stays within budget.

    The output-size check happens BEFORE the atomic replace, so a boundary-crossing
    write refuses without landing oversized state (F4). The file is left untouched
    and remains readable for later invocations."""
    content = "".join(_serialize(row) + "\n" for row in rows)
    encoded = content.encode("utf-8")
    if len(encoded) > RECORD_FILE_BUDGET_BYTES:
        raise SystemExit(
            f"recording thread {thread_id} would grow {path} to "
            f"{len(encoded)} bytes over the {RECORD_FILE_BUDGET_BYTES}-byte budget; "
            "refusing without writing (a wedge is worse than a refusal)"
        )
    write_file_durably(path, content)


def _build_resolved_row(project_id: str, thread_id: str, provider: str | None,
                        model: str, reasoning_level: str, source: str) -> dict:
    return {
        "thread_id": thread_id,
        "project_id": project_id,
        # GH-710: main() refuses any source other than client/turn/requested
        # before this row is built, so every stored row IS the triple that
        # executed and the label states exactly that. The source is verified
        # upstream, not assumed here.
        "evidence": EVIDENCE_EXECUTED,
        "provider": provider,
        "model": model,
        "reasoning_level": reasoning_level,
        "source": source,
        "status": RESOLVED,
        "recorded_at": utc_iso(),
    }


def _build_unresolved_row(project_id: str, thread_id: str, provider: str | None,
                          reason: str, detail: str | None = None) -> dict:
    # An unresolved row records a FAILED resolution -- no value was read, so there
    # is no source to derive an evidence label from and no ``evidence`` field. It
    # carries ``failure_reason`` instead. (Setting evidence="executed" here would
    # be the assume-the-label defect: a failed resolution executed nothing.)
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
    result.add_argument("--project", required=True, help="registered llm-collab project_id (matched RAW; scopes the record file)")
    result.add_argument("--thread-id", required=True, help="BB thread id whose executed triple is recorded")
    result.add_argument("--thread-project", required=True, help="bb projectId from the thread.created event DTO (scope match)")
    result.add_argument("--provider", default=None, help="providerId from the thread.created event DTO (may be omitted)")
    result.add_argument("--model", help="executed model (resolved case)")
    result.add_argument("--reasoning-level", help="executed reasoning level (resolved case)")
    result.add_argument(
        "--source",
        help="the SDK-reported source the resolved options came from; only "
        "client/turn/requested is recorded (the executed-evidence source "
        "bb_client.py:21-24 names authoritative). client/thread/start is refused "
        "with its own distinct reason; any other source is refused observably "
        "as out of contract. No refusal writes a row",
    )
    result.add_argument("--unresolved", metavar="REASON", help="record a typed resolution failure instead of a resolved triple")
    result.add_argument("--failure-detail", default=None, help="short detail for an unresolved row (e.g. the resolution error)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    # N2: identifiers are matched RAW — a whitespace variant is rejected by the
    # registry/scope check, never repaired. `.strip()` is used ONLY to test for an
    # empty value; the raw value is what flows downstream.
    project_id = _reject_nul("project", args.project)  # RAW; registry enforces exactness (N2)
    thread_id = _reject_nul("thread-id", args.thread_id)
    if not thread_id.strip():
        raise SystemExit("--thread-id is empty")
    thread_project = _reject_nul("thread-project", args.thread_project)
    if not thread_project.strip():
        raise SystemExit("--thread-project is empty")
    provider = _reject_nul("provider", args.provider) if args.provider else None

    # F3: exact registry match BEFORE the lock or any record. An unregistered id
    # must not create a phantom authoritative file. Refusal is observable.
    entry = get_project(project_id)
    if entry is None:
        raise SystemExit(f"project {project_id!r} is not registered in projects.json; refusing to record")

    # F1 + GH-695 P2-D: the thread's bb project must exactly match this project's
    # scope, matched RAW (a padded bb.project_id is rejected at resolution above).
    # A thread for another llm-collab project is IGNORED observably (exit 0 +
    # stdout marker) rather than mis-attributed. The plugin captures this marker
    # and logs it (N3).
    native = _resolve_native_bb_project(entry)
    if thread_project != native:
        print(f"ignored scope_mismatch {thread_id}: thread project {thread_project!r} != {project_id!r} scope {native!r}")
        return 0

    path = record_path(project_id)

    if args.unresolved:
        row = _build_unresolved_row(project_id, thread_id, provider, args.unresolved, args.failure_detail)
    else:
        missing = [name for name, value in (("model", args.model), ("reasoning-level", args.reasoning_level), ("source", args.source)) if not value]
        if missing:
            raise SystemExit(f"resolved case requires --model/--reasoning-level/--source together; missing: {', '.join(missing)}")
        # GH-710: record ONLY executed-triple evidence (source ==
        # client/turn/requested, the source llm_collab/bb_client.py:21-24 names
        # the authoritative record of the profile bb actually ran).
        #
        # client/thread/start is handled EXPLICITLY, with its own distinct
        # refusal: on this SDK its payload carries no execution options (GH-706),
        # so it is not executed evidence — but if it ever IS seen carrying
        # options, the distinct marker surfaces that instead of silently dropping
        # it. Every other source is unrecognised to this artifact and refused as
        # out of contract. Both refusals are observable (exit 0 + an ignored
        # marker naming the source) and write no row; silence here would repeat
        # the finding the GH-630 review already fixed (F5/N3). This admits ONE
        # named source; it does not remove the gate.
        if args.source == _SOURCE_THREAD_START:
            print(
                f"ignored thread_start_not_executed {thread_id}: client/thread/start carries "
                "no execution options on this SDK (GH-706) and is not executed evidence; "
                "if it ever carries them, this marker is the tripwire — not recorded"
            )
            return 0
        if args.source != _SOURCE_TURN_REQUESTED:
            print(
                f"ignored out_of_contract {thread_id}: source {args.source!r} is not the "
                f"executed-evidence source ({_SOURCE_TURN_REQUESTED}); not recorded"
            )
            return 0
        row = _build_resolved_row(
            project_id,
            thread_id,
            provider,
            _reject_nul("model", args.model),
            _reject_nul("reasoning-level", args.reasoning_level),
            _reject_nul("source", args.source),
        )

    try:
        with _write_lock(project_id):
            # GH-695 P2-C: pass project_id so every loaded row is scope-checked.
            rows = _load_existing(path, project_id)
            existing = next((r for r in rows if r.get("thread_id") == thread_id), None)

            if existing is None or existing.get("status") == UNRESOLVED:
                # No prior row, or a pending failure completing/refining: write.
                # Unresolved is not established provenance, so replacing it is the
                # legal ``unresolved -> resolved`` completion (N1).
                kept = []
                replaced = False
                for r in rows:
                    if r.get("thread_id") == thread_id:
                        kept.append(row)
                        replaced = True
                    else:
                        kept.append(r)
                if not replaced:
                    kept.append(row)
                _write_bounded(path, kept, thread_id)
                print(f"recorded {row['status']} {thread_id} -> {path}")
            elif _same_resolved(existing, row):
                # Resolved already, and the re-fire is the SAME triple: a no-op.
                # Do not rewrite or duplicate (N1).
                print(f"noop {thread_id}: identical resolved triple; provenance immutable")
            else:
                # Resolved already, and the re-fire DIFFERS (a changed preset, or a
                # resolved -> unresolved regression). Keep the first — provenance is
                # immutable once resolved — and surface the conflict so the change is
                # visible rather than invisible (N1). No write.
                print(
                    f"conflict {thread_id}: kept resolved provenance; rejected re-resolution "
                    f"({_diff_summary(existing, row)}); provenance is immutable once resolved"
                )
    except UnreadableFile as error:
        raise SystemExit(str(error)) from error

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
