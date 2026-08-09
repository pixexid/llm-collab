# bb-plugin-exec-tracking

Custom bb plugin — the **task/execution-tracking** plugin (GH-630). This checkout's
first capability is the **creation-defaults recorder** (GH-617 / GH-695): on
`thread.created` it records the thread's creation-time default `(provider, model,
reasoning_level)` options — and ONLY those. A turn-derived result is refused
observably and deferred to the `client/turn/requested` re-scope (GH-695 P1-B).

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

## Records ONLY creation-time defaults (GH-695 head 3)

`defaultExecutionOptions` resolves options and tags them with a `source` naming
which client phase the values came from. Only `client/thread/start` is a
creation-time default. The committed SDK declaration
(`resolvedThreadExecutionOptionsSchema`) also permits two turn-derived sources —
`client/turn/requested` (the authoritative executed-evidence source, the
`execution` block `llm_collab/bb_client.py` validates against) and
`client/turn/start` — but those are OUT OF THIS ARTIFACT'S CONTRACT: the recorder
**refuses them observably** (exit 0 + `ignored out_of_contract` marker naming the
source) and writes no row. An absent or unrecognised `source` is refused the same
way — a row the artifact cannot classify is not half-admitted.

| `source`                | outcome                                              |
|-------------------------|------------------------------------------------------|
| `client/thread/start`   | **recorded** — `evidence: creation_defaults`         |
| `client/turn/requested` | refused observably (`ignored out_of_contract`)        |
| `client/turn/start`     | refused observably (`ignored out_of_contract`)        |
| absent / unrecognised   | refused observably (`ignored out_of_contract`)        |

So the store name (`thread-creation-defaults.jsonl`) is true — every row in it
really is a creation default — and a consumer selecting the artifact by its
documented contract cannot misclassify a turn row. The fire-and-forget handler can
sometimes already see `client/turn/requested` (if the spawn turn advances before
its loopback RPC finishes), but recording that into an artifact named for creation
defaults would let the container make a claim its contents can violate, so it is
refused. Deterministic sourcing from `client/turn/requested` (via
`bb.sdk.threads.events.wait`, the SDK RPC analog of `thread log --json`) is the
tracked re-scope (GH-695 P1-B); `bb.events.on` exposes only the six thread
transitions (created/active/idle/failed/archived/deleted), so a lifecycle-event
recorder is not available. This slice does not build source precedence — it records
one thing and refuses the rest.

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
reasoning_level)` — `source` is excluded because every stored row has
`source: client/thread/start` by construction (turn sources are refused before
storage), so it never varies between rows and carries no identity. If the contract
ever re-admits turn sources, `source` must return to identity; without it a turn
row sharing `(provider, model, reasoning_level)` with a creation-defaults row
would no-op and be silently discarded rather than surfaced as a conflict. Each row
stores the **resolved** values, never a preset name.

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
