# Observation global cadence scheduler RFC

Status: design draft for GH-179 / GH-181 / GH-183. This document is inert:
it does not enable observation, dispatch, product runtime mutation, or queue
mutation.

## Problem

The implemented GH-90 observation daemon runs one bounded pass for each
registered project. Each project pass may scan 2000 source entries, write 500
new observations, prune 500 resolved observations, and append reconciliation
and retention audit rows. A workspace with N registered projects can therefore
perform N times that work during one 30-second daemon cadence.

Three follow-up issues touch the same seam:

- GH-183 requires one pinned workspace-root identity for a cadence, so project
  scans cannot reopen or re-resolve different roots inside the same pass.
- GH-179 requires one global per-cadence scheduler and budget shared by all
  projects.
- GH-181 requires audit writes and retention/prune work to stay inside the same
  bounded cadence model instead of growing as a separate unbounded side effect.

The implementation must therefore change `ObservationEngine.reconcile()` once,
around a single pinned-root scheduler seam. It must not land three independent
partial schedulers.

## Non-goals

- No production observation enablement. The existing declaration and environment
  gates remain necessary and default-off.
- No dispatcher, delivery, canonical write, runtime adapter, Amiga product
  queue, or registry mutation.
- No all-project transaction. Each project keeps its own atomic observation
  checkpoint transaction; a failure in one project cannot roll back committed
  work for another project.
- No compatibility path for missing, empty, null, or foreign project identity.

## Implementation order

1. Land this reviewed RFC.
2. Land GH-183 root pinning as the structural precondition. `reconcile()` must
   open and verify one workspace-root identity for the cadence, then pass that
   pinned root to every project scan. Per-project scans must not reopen the
   workspace root by pathname.
3. Land GH-179 scheduling over that pinned-root seam.
4. Land any remaining GH-181 retention/compaction work against the same budget
   model. Audit rows written by reconciliation and retention are counted by the
   GH-179 maintenance budget from the first scheduler implementation.

## Fixed cadence budget

One daemon cadence has these workspace-wide budgets for the fixed
`chats_mailbox` source:

| Budget | Value | Counts | Does not count |
| --- | ---: | --- | --- |
| scan entries | 2000 | candidate directory entries read by mailbox scanning across every project | registry snapshot reads |
| observation writes | 500 | inserted `observations` rows across every project | duplicate candidates that insert no row |
| maintenance writes | 500 | `observation_checkpoints` upserts, `observation_audit` rows, deleted resolved observation rows, and scheduler cursor updates | read-only diagnostics |

The split preserves the existing single-project observation capacity of 500
new observations while adding a separate global bound for checkpoint, audit,
retention, and scheduler metadata writes. It also makes GH-181 explicit: a
reconciliation audit row and a retention audit row consume maintenance budget;
they are not free per-project side effects.

The scheduler must stop before starting a project if the remaining budget cannot
cover the minimum safe project attempt:

- one checkpoint upsert;
- one reconciliation audit row;
- one scheduler cursor update.

Prune work is optional per project. If maintenance budget remains after the
reconciliation transaction and scheduler cursor update, the engine may prune up
to the remaining maintenance budget minus one retention-audit row. If that
budget is not available, retention is deferred to a later cadence.

## Durable scheduler cursor

The existing `observation_checkpoints` table is project-scoped. It records
where one project/source/revision scan resumes inside that project. It is not a
cross-project scheduler.

The implementation must add a v7 ledger migration with a separate cross-project
cursor table:

```sql
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
```

The row is absent when the registry snapshot has no projects. `next_project_id`
is deliberately not a foreign key to a project row because the cursor must
survive project removal long enough to reconcile its position in the next
registry snapshot.

Store API additions:

- `observation_scheduler_cursor(workspace_id, source_id) -> str | None`
  returns the last durable `next_project_id`, or `None` when no cursor row
  exists.
- `advance_observation_scheduler_cursor(workspace_id, source_id,
  registry_revision, next_project_id, updated_at_utc)` validates bounded text,
  requires a recorded workspace registry snapshot, writes one cursor row, and
  consumes one maintenance-budget unit in the scheduler accounting.

The cursor is advanced only after the current project transaction has committed.
If the daemon crashes after a project commits but before the scheduler cursor
advances, the next cadence may revisit that project. Observation deduplication
and per-project checkpoint atomicity make that safe. The cursor must never
advance before the project commit it is meant to follow.

## Project ordering and fairness

For each registry snapshot, scheduler order is `sorted(snapshot.project_ids)`.
The order is independent of `projects.json` file ordering, so unrelated file
reordering cannot change fairness.

Cursor reconciliation:

1. If the durable `next_project_id` is present in the sorted project list, start
   there.
2. If it is absent, start at the first project id greater than the cursor value.
3. If no project id is greater, wrap to the first project.
4. If the project list is empty, do no observation work and write no scheduler
   cursor row.

Per cadence, the scheduler visits each project at most once, in ring order from
the reconciled start. A project may use all remaining scan, observation-write,
or maintenance budget, but after its committed attempt the scheduler cursor
advances to the next project in ring order. If budget exhaustion stops the
cadence, the following cadence resumes at that next project, not at the project
that just consumed the budget.

This is a rotating-start, resume-front policy. A large project receives
successive bounded chunks through its own `observation_checkpoints` cursor, but
it cannot permanently monopolize the first position. With a finite project set
and enough budget to attempt at least one project per cadence, every project is
attempted within a bounded number of cadences.

## Pinned-root scheduler seam

The scheduler implementation must run under one pinned root object for the
entire cadence:

```text
read registry snapshot
record snapshot
open and verify one workspace-root identity
load/reconcile scheduler cursor
for each selected project in ring order:
    read that project's observation checkpoint
    scan the mailbox through the pinned root
    commit that project's observation checkpoint and reconciliation audit
    optionally commit retention/prune work within maintenance budget
    advance the scheduler cursor after committed project work
return bounded diagnostics
```

The pinned root must be represented by an actual opened directory identity, not
a pathname-only value. The GH-183 implementation must make every mailbox source
open relative to that pinned root and fail closed if the root cannot be opened
or verified safely.

## Crash and restart behavior

- Registry snapshots are recorded before scheduler work.
- A project observation transaction remains atomic for candidates, checkpoint,
  and reconciliation audit.
- Retention/prune work may commit separately after the observation transaction;
  if it does not run, it is retried later.
- Scheduler cursor advancement occurs after the project work it follows. A crash
  before cursor advancement may repeat one project; it must not skip uncommitted
  work.
- Registry revision changes do not invalidate per-project checkpoints. They
  create a new project/source/revision observation scope, while the scheduler
  reconciles the cross-project cursor by stable project id ordering.

## Required implementation tests

The implementation slices following this RFC must prove these properties:

1. One cadence across multiple projects never exceeds 2000 scanned entries,
   500 inserted observation rows, or 500 maintenance writes.
2. Reconciliation and retention audit rows consume maintenance budget.
3. If maintenance budget cannot cover the minimum safe project attempt, the
   scheduler stops before scanning that project.
4. A project that exhausts the remaining budget advances the cross-project
   cursor to the next project after its committed attempt.
5. Repeated cadences make progress for every project in a finite project set.
6. Adding, removing, or reordering projects reconciles the durable cursor by
   project id without resetting fairness to the first project unnecessarily.
7. A simulated crash after project commit but before scheduler cursor update
   may repeat that project and must not duplicate observations.
8. A simulated root swap between projects is caught by the pinned-root seam, and
   no project scan reopens the workspace root by pathname.
9. Per-project transactions remain isolated: a failing project does not roll
   back earlier committed project work and does not commit partial work for the
   failing project.

## Review gates

Implementation review must reject:

- a scheduler that opens or resolves the workspace root per project;
- a global budget that omits prune or audit rows;
- a cursor keyed only by position/index;
- a cursor advance before the corresponding project commit;
- one all-project SQLite transaction;
- any observation enablement or runtime/product queue mutation bundled with the
  scheduler.
