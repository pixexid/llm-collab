# bb-plugin-exec-tracking

Custom bb plugin — the **task/execution-tracking** plugin (GH-630). This checkout's
first capability is the **executed-options recorder** (GH-617 / GH-695): on
`thread.created` it records the thread's resolved `(provider, model,
reasoning_level)` options into project-scoped state, labelled by where they came
from.

> **Operator procedure (build, typecheck, install, config) lives in
> [`docs/workflows/exec-tracking-plugin.md`](../../docs/workflows/exec-tracking-plugin.md).**
> It is the single source of truth; this README links to it rather than restating a
> command sequence that can go stale (AGENTS.md "this file is the source of truth").

## Where it lives (two-plugin structure)

Custom bb plugins live as siblings under `bb-plugins/`:

- `bb-plugins/exec-tracking/` — **this plugin**. Resolved execution options (this
  recorder) and, later, task/execution tracking carrying the gaps the builtin
  `tasks` could not supply.
- `bb-plugins/fan-out/` — a **future** orchestration/fan-out plugin (per-agent
  worktree isolation, fail-closed behaviour), added as a new sibling later.

Each plugin owns its own identity, its own state file, and shares **no mutable
module state** with the other, so plugin 2 can be added beside this one without
touching it. No shared framework is built up front.

## What it does (first capability)

On `thread.created`, `server.ts`:

1. reads the thread's resolved execution options in-process via
   `bb.sdk.threads.defaultExecutionOptions({ threadId })` (loopback RPC, `await`-ed,
   never blocks the event loop);
2. snapshots the resolved values **and their `source`** to primitives (never a
   mutable reference — GH-617's whole point);
3. hands them to a child running `bin/record_thread_defaults.py`
   (`unref()` — the handler never waits on the write). Spawn errors and nonzero
   exits are logged **asynchronously** via the child's `error`/`close` events, so a
   recorder failure is never indistinguishable from an event that never happened.

An options object that cannot be resolved is recorded as an explicit `unresolved`
row, so an absent row and a failed resolution are distinguishable. The plugin
**records, never gates**: `thread.created` handlers have no veto hook, so
enforcement stays at the CLI call sites.

## The evidence label follows the value, not the handler (GH-695 P1-A)

`defaultExecutionOptions` resolves options and tags them with a `source` naming
which client phase they came from. The handler is fire-and-forget: at
`thread.created` the `source` is usually `client/thread/start` (creation-time
defaults), but if the spawn turn advances before the handler's awaited settings
lookup and loopback RPC finish, the result can carry `client/turn/requested` — the
authoritative executed-evidence source. The recorder therefore DERIVES each row's
`evidence` label from the `source` the SDK actually reports; it never assumes a
label from which handler is running. An unconditional label would durably and
immutably misclassify a turn-derived snapshot (or vice versa) — the same lie GH-617
exists to prevent, in either direction.

The `source` values the committed SDK declaration (`resolvedThreadExecutionOptionsSchema`)
permits, and the label each gets:

| `source`                  | `evidence`          | meaning |
|---------------------------|---------------------|---------|
| `client/thread/start`     | `creation_defaults` | the thread's creation-time default options — NOT what executed if a turn overrode them |
| `client/turn/requested`   | `turn_requested`    | the authoritative executed-evidence source (the `execution` block `llm_collab/bb_client.py` validates against); sometimes already reachable via this race |
| `client/turn/start`       | `turn_start`        | a turn phase; turn-derived, not a creation-time default and not the authoritative `client/turn/requested` source |

An absent or unrecognised `source` is labelled `unknown` — NEVER `creation_defaults`.
Guessing a specific claim is the defect this labelling exists to prevent. A
dedicated `bb.sdk.threads.events.wait`-based recorder that sources
`client/turn/requested` deterministically (rather than via this race) remains a
tracked re-scope (GH-695 P1-B); `bb.events.on` exposes only the six thread
transitions (created/active/idle/failed/archived/deleted), so a lifecycle-event
recorder is not available.

## Project scope and registry binding

`thread.created` is server-wide, so the recorder refuses to mis-attribute:

- **Registry binding.** `--project` must be an EXACT registered llm-collab project
  in `projects.json`, matched RAW — whitespace variants are rejected, not repaired;
  an unregistered or padded id is refused. Refusal is observable (nonzero exit → warn).
- **Scope match, raw not normalized (GH-695 P2-D).** The thread's bb project
  (`thread.projectId`) must exactly match the configured project's `bb.project_id`
  scope, and that registry value is matched RAW: a padded `bb.project_id` is
  **rejected** at both the recorder and `spawn_gate` (never stripped), so the two
  authorities enforce the same scope. A thread for another llm-collab project is
  **ignored observably** (exit 0 + `ignored scope_mismatch` → info log) rather than
  recorded under the wrong file.
- **Every loaded row is scope-checked (GH-695 P2-C).** The loader validates
  `project_id` on every row read from disk and fails closed on a missing or
  mismatched value, so a cross-project row is never silently reused.

## Immutability once resolved

Provenance is immutable once resolved (GH-617). A `thread.created` re-fire never
overwrites a resolved row: the only legal write against an existing row is
`unresolved → resolved` (the record completing). A re-fire of the SAME resolved
triple is a no-op; a re-fire of a DIFFERENT resolved triple (or `resolved →
unresolved`) keeps the first and emits a `conflict` marker (info log) so a changed
preset is visible rather than invisible. Identity is `(provider, model,
reasoning_level, source)`, so a re-fire that advances the `source` (e.g.
`client/thread/start` → `client/turn/requested`) is a conflict, not a no-op — the
first record is kept and the change is surfaced. Each row stores the **resolved**
values, never a preset name.

## State

`{project_state_root}/{project_id}/thread-creation-defaults.jsonl` — the project
state root the Project Boundary rule owns, **not** a second invented runtime-state
root. Execution provenance is runtime state, not the git-backed/diffable task state
a later slice carries (GH-630 lists git-backed task state separately), so it is not
git-tracked here. The writer is the authority: bounded read, exclusive flock,
output-size check before the atomic temp+rename write, fail-closed on
budget/corruption, project-id scope-check on every loaded row, and
immutable-once-resolved rows (see above).

## Build, typecheck, install, config

See **[`docs/workflows/exec-tracking-plugin.md`](../../docs/workflows/exec-tracking-plugin.md)**.
The package ships a TypeScript gate (`tsconfig.json` + a `typecheck` script) that
fails on an undeclared identifier before install — the procedure (including the
mandatory pre-install `npm run typecheck`) is documented there, not restated here.
