# bb-plugin-exec-tracking

Custom bb plugin — the **task/execution-tracking** plugin (GH-630). This checkout's
first capability is the **thread-creation-defaults recorder** (GH-617 / GH-695 P1-B):
on `thread.created` it records the thread's creation-time **default**
`(provider, model, reasoning_level)` options into project-scoped state.

> ⚠️ **These are DEFAULTS, not executed evidence (GH-695 P1-B).** The recorder reads
> `bb.sdk.threads.defaultExecutionOptions`, a creation-time defaults lookup. If a
> thread's first turn uses an override, the default is what gets recorded — so every
> row self-describes as `evidence: "creation_defaults"` and must never be mistaken
> for what executed. The authoritative executed-evidence source is the `execution`
> block on the thread's `client/turn/requested` event (the surface
> `llm_collab/bb_client.py` validates the requested profile against). That event IS
> reachable from a plugin via `bb.sdk.threads.events.list` / `events.wait` (the SDK
> RPC analog of `thread log --json`), but it is **not** a plugin lifecycle event
> (`bb.events.on` exposes only the six thread transitions: created/active/idle/
> failed/archived/deleted), and at `thread.created` the spawn turn has not been
> requested yet — so sourcing from it means turning the creation-time snapshot into a
> bounded async wait that discriminates the spawn turn from later tells, whose
> pre-1.0 SDK semantics cannot be verified without a live server. Sourcing executed
> evidence from `client/turn/requested` is a **tracked re-scope (GH-695 P1-B)**; this
> slice's honest fix is to stop calling defaults "executed".

## Where it lives (two-plugin structure)

Custom bb plugins live as siblings under `bb-plugins/`:

- `bb-plugins/exec-tracking/` — **this plugin**. Thread creation-default provenance
  (this recorder) and, later, task/execution tracking carrying the gaps the builtin
  `tasks` could not supply.
- `bb-plugins/fan-out/` — a **future** orchestration/fan-out plugin (per-agent
  worktree isolation, fail-closed behaviour), added as a new sibling later.

Each plugin owns its own identity, its own state file, and shares **no mutable
module state** with the other, so plugin 2 can be added beside this one without
touching it. No shared framework is built up front.

## What it does (first capability)

On `thread.created`, `server.ts`:

1. reads the thread's creation-time default options in-process via
   `bb.sdk.threads.defaultExecutionOptions({ threadId })` (loopback RPC, `await`-ed,
   never blocks the event loop);
2. snapshots the resolved values to primitives (never a mutable reference —
   GH-617's whole point);
3. hands them to a child running `bin/record_thread_defaults.py`
   (`unref()` — the handler never waits on the write). Spawn errors and nonzero
   exits are logged **asynchronously** via the child's `error`/`close` events, so a
   recorder failure is never indistinguishable from an event that never happened.

An options object that cannot be resolved is recorded as an explicit `unresolved`
row, so an absent row and a failed resolution are distinguishable. The plugin
**records, never gates**: `thread.created` handlers have no veto hook, so enforcement
stays at the CLI call sites.

## Project scope and registry binding

`thread.created` is server-wide, so the recorder refuses to mis-attribute:

- **Registry binding.** `--project` must be an EXACT registered llm-collab project
  in `projects.json`, matched RAW — whitespace variants are rejected, not repaired;
  an unregistered or padded id is refused (it would reproduce the builtin `tasks`
  self-declared-project defect). Refusal is observable (nonzero exit → warn).
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
preset is visible rather than invisible. Each row stores the **resolved** values,
never a preset name.

## State

`{project_state_root}/{project_id}/thread-creation-defaults.jsonl` — the project
state root the Project Boundary rule owns, **not** a second invented runtime-state
root. Execution provenance is runtime state, not the git-backed/diffable task state
a later slice carries (GH-630 lists git-backed task state separately), so it is not
git-tracked here. The writer is the authority: bounded read, exclusive flock,
output-size check before the atomic temp+rename write, fail-closed on
budget/corruption, project-id scope-check on every loaded row, and
immutable-once-resolved rows (see above). The file name says what the rows are
(creation-time defaults), never "executed".

## Typecheck gate (GH-695 P1-A)

Path installs load `server.ts` directly as TypeScript with **no build step**, and
`bb plugin build` uses esbuild, which strips types without checking them — so an
undeclared identifier crashes at runtime instead of being caught at build. This
plugin therefore ships a real TypeScript gate:

- `tsconfig.json` maps `@bb/plugin-sdk` to the committed `types/bb-plugin-sdk.d.ts`
  (written by `bb plugin types .`) and sets `noEmit` + `strict`.
- `npm run typecheck` (`tsc -p tsconfig.json`) type-checks `server.ts` against the
  SDK declarations. It **fails on an undeclared identifier** (`TS2304`), which is
  the defect that crashed the handler when `STDOUT_CAP` / `INFO_MARKERS` were used
  without being declared.

`skipLibCheck` is `true` so the gate does not re-check the SDK's own bundled
declaration (bb's responsibility); `server.ts` itself is fully checked. Regenerate
the declarations after an SDK bump with `bb plugin types .`.

## Operator install + config (not done here)

Installation is a separate operator step. This directory only builds and tests the
code. **Run the typecheck first** — it is the gate that stops an undeclared
identifier reaching a live server:

```sh
cd bb-plugins/exec-tracking
bb plugin types .          # (re)write types/bb-plugin-sdk.d.ts for this bb
npm install                # install typescript + @types/node (devDependencies)
npm run typecheck          # FAILS on an undeclared identifier before you install
bb plugin install .        # operator step — only after typecheck is green
bb plugin config exec-tracking set checkoutPath /path/to/llm-collab
bb plugin config exec-tracking set pythonPath  /abs/path/to/python3.11   # server PATH is narrow
bb plugin config exec-tracking set projectId   amiga                     # must be registered in projects.json
bb plugin reload exec-tracking
```

`checkoutPath` and `pythonPath` must be set before any row is recorded; until then
the handler logs a warning and records nothing (rather than guess a path).
`projectId` must exist in that checkout's `projects.json` with a `bb.project_id`
matching the bb project its threads spawn under.
