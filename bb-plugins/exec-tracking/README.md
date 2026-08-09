# bb-plugin-exec-tracking

Custom bb plugin — the **task/execution-tracking** plugin (GH-630). This checkout's
first capability is the **executed-triple recorder** (GH-617): on `thread.created`
it records the resolved `(provider, model, reasoning_level)` profile that executed
into project-scoped state.

## Where it lives (two-plugin structure)

Custom bb plugins live as siblings under `bb-plugins/`:

- `bb-plugins/exec-tracking/` — **this plugin**. Execution provenance (the triple
  recorder) and, later, task/execution tracking carrying the gaps the builtin
  `tasks` could not supply.
- `bb-plugins/fan-out/` — a **future** orchestration/fan-out plugin (per-agent
  worktree isolation, fail-closed behaviour), added as a new sibling later.

Each plugin owns its own identity, its own state file, and shares **no mutable
module state** with the other, so plugin 2 can be added beside this one without
touching it. No shared framework is built up front.

## What it does (first capability)

On `thread.created`, `server.ts`:

1. resolves the executed profile in-process via the only surface that exposes it —
   `bb.sdk.threads.defaultExecutionOptions({ threadId })` (loopback RPC, `await`-ed,
   never blocks the event loop);
2. snapshots the resolved values to primitives (never a mutable reference —
   GH-617's whole point);
3. hands them to a child running `bin/record_executed_triple.py`
   (`unref()` — the handler never waits on the write). Spawn errors and nonzero
   exits are logged **asynchronously** via the child's `error`/`close` events, so a
   recorder failure is never indistinguishable from an event that never happened.

A profile that cannot be resolved is recorded as an explicit `unresolved` row, so
an absent row and a failed resolution are distinguishable. The plugin **records,
never gates**: `thread.created` handlers have no veto hook, so enforcement stays at
the CLI call sites.

## Project scope and registry binding

`thread.created` is server-wide, so the recorder refuses to mis-attribute:

- **Registry binding.** `--project` must be an EXACT registered llm-collab project
  in `projects.json`, matched RAW — whitespace variants are rejected, not repaired;
  an unregistered or padded id is refused (it would reproduce the builtin `tasks`
  self-declared-project defect). Refusal is observable (nonzero exit → warn).
- **Scope match.** The thread's bb project (`thread.projectId`) must exactly match
  the configured project's `bb.project_id` scope (the same field `spawn_gate` uses).
  A thread for another llm-collab project is **ignored observably** (exit 0 +
  `ignored scope_mismatch` → info log) rather than recorded under the wrong file —
  mis-attribution would corrupt the exact provenance this plugin exists to preserve.

## Immutability once resolved

Provenance is immutable once resolved (GH-617). A `thread.created` re-fire never
overwrites a resolved row: the only legal write against an existing row is
`unresolved → resolved` (the record completing). A re-fire of the SAME resolved
triple is a no-op; a re-fire of a DIFFERENT resolved triple (or `resolved →
unresolved`) keeps the first and emits a `conflict` marker (info log) so a changed
preset is visible rather than invisible. Each row stores the **resolved** values,
never a preset name.

## State

`{project_state_root}/{project_id}/executed-triples.jsonl` — the project state root
the Project Boundary rule owns, **not** a second invented runtime-state root.
Execution provenance is runtime state, not the git-backed/diffable task state a
later slice carries (GH-630 lists git-backed task state separately), so it is not
git-tracked here. The writer is the authority: bounded read, exclusive flock,
output-size check before the atomic temp+rename write, fail-closed on
budget/corruption, and immutable-once-resolved rows (see above).

## Operator install + config (not done here)

Installation is a separate operator step. This directory only builds and tests the
code.

```sh
bb plugin install ./bb-plugins/exec-tracking   # operator step
bb plugin config exec-tracking set checkoutPath /path/to/llm-collab
bb plugin config exec-tracking set pythonPath  /abs/path/to/python3.11   # server PATH is narrow
bb plugin config exec-tracking set projectId   amiga                     # must be registered in projects.json
bb plugin reload exec-tracking
```

`checkoutPath` and `pythonPath` must be set before any triple is recorded; until
then the handler logs a warning and records nothing (rather than guess a path).
`projectId` must exist in that checkout's `projects.json` with a `bb.project_id`
matching the bb project its threads spawn under.
