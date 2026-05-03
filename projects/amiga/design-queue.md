# Amiga `/design` Queue

> Source: `design-queue.json`. This queue is for `/design` sandbox/spec work only.
> Do not use the older implementation `issue-queue.json` to choose design-only lanes.

- Last updated: `2026-05-03T04:00:00+00:00`
- Current mode: `/design` before `/app`
- Active lane: `GH-346` / `TASK-02B76E` / `claude`
- Next queued lane: `GH-347` / `TASK-180EDC` / `claude`

## Rule

Work through this queue before starting Phase 7 `/app` implementation lanes. These lanes produce design artifacts: surface specs, sandbox gap specs, parity/handoff preparation, and DESIGN.md sync when the sandbox/spec disagrees with the design contract.

Research-only outputs stay in the collaboration task record unless the operator explicitly decides a durable repo artifact is needed.

## Recently Completed

- `GH-338` / `TASK-2370B3` / done — Phase 6 R2.4 Dispatch Map view in `/design`
- `GH-340` / `TASK-130A01` / done — Phase 6 R3 handoff/parity templates and Operations surface specs
- `GH-342` / `TASK-86F310` / done — Mapbox vs OpenFreeMap research; recommendation kept in task record, no app-repo artifact
- `GH-345` / `TASK-50F143` / done — Dashboard surface spec merged in PR #349

## Remaining `/design` Queue

| Order | Phase | Issue | Task | Owner | Status | Queue | Lane Type | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 6 R3.2 | GH-346 | TASK-02B76E | claude | in_progress | active | design-surface-spec | Refined by Claude and activated after D8 merge. Clients surface spec from existing `/design/clients`; keep Client + Property redesign deferred. |
| 3 | 6 R3.3 | GH-347 | TASK-180EDC | claude | open | queued | design-surface-spec-or-gap-spec | Staff spec; if no `/design/staff` route exists, record the blocker honestly. |
| 4 | 6 R3.4 | GH-348 | TASK-3D1716 | claude | open | queued | design-surface-spec-or-gap-spec | Account spec; if no `/design/account` route exists, record the blocker honestly. |

## After This Queue

Do not jump directly to `/app` implementation until the accepted surface has:

- an approved surface spec under `design/surfaces/`
- an implementation handoff under `design/handoff/` when the implementation lane opens
- a parity target under `design/parity/`
- a GitHub issue and local task mirror for the implementation lane
