# bb-plugin-exec-tracking

Custom bb plugin — the **task/execution-tracking** plugin (GH-630). This checkout's
first capability is the **executed-triple recorder** (GH-710 / GH-617): on
`thread.created` it records the `(provider, model, reasoning_level)` triple that
actually ran — and ONLY executed evidence. A non-executed result is refused
observably.

> **Operator procedure (build, typecheck, install, config) lives in
> [`docs/workflows/exec-tracking-plugin.md`](../../docs/workflows/exec-tracking-plugin.md).**
> It is the single source of truth; this README links to it rather than restating a
> command sequence that can go stale (AGENTS.md "this file is the source of truth").

## The artifact's definition (this README owns it)

The artifact is **executed-triple evidence**: which `(provider, model,
reasoning_level)` triple actually ran on each thread. That is the invariant the
plugin exists to serve — it is what makes the frozen-triple rule auditable.
"Creation-time defaults" was a **proxy** for that invariant, chosen before GH-706
established the proxy is unobservable: no surface on this SDK carries
creation-time execution options (`client/thread/start`'s payload has none, and an
idle fork emits `client/turn/requested` *first*, so there is no pre-turn window).

The accepted source is `client/turn/requested` **because the repository already
decided it is the proof**, not because it is the source we can get:
`llm_collab/bb_client.py:21-24` states that the spawn envelope carries no model
and no reasoning level, so argv alone cannot prove which profile actually ran,
and that the authoritative record is the `execution` block on the thread's
`client/turn/requested` event — the block `BbClient` validates the requested
profile against (`SPAWN_EVENT_TYPE` at `bb_client.py:67`, `_execution_evidence()`
at `bb_client.py:633`). This recorder accepts the same source that authority
accepts; GH-710 resolved the two-authorities defect in favor of `bb_client.py`.

`docs/workflows/exec-tracking-plugin.md` links here for this definition rather
than restating it.

## Where it lives (two-plugin structure)

Custom bb plugins live as siblings under `bb-plugins/`:

- `bb-plugins/exec-tracking/` — **this plugin**. Executed-triple evidence (this
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
3. hands them to a child running `bin/record_executed_triples.py`
   (`unref()` — the handler never waits on the write). Spawn errors and nonzero
   exits are logged **asynchronously** via the child's `error`/`close` events, so a
   recorder failure is never indistinguishable from an event that never happened.

An options object that cannot be resolved is recorded as an explicit `unresolved`
row, so an absent row and a failed resolution are distinguishable. The plugin
**records, never gates**: `thread.created` handlers have no veto hook, so
enforcement stays at the CLI call sites.

## Records ONLY executed-triple evidence (GH-710)

`defaultExecutionOptions` resolves options and tags them with a `source` naming
which client phase the values came from. The gate admits ONE named source — it is
a definition, not a removed check:

| `source`                | outcome                                                          |
|-------------------------|------------------------------------------------------------------|
| `client/turn/requested` | **recorded** — `evidence: executed`                               |
| `client/thread/start`   | refused observably, own reason (`ignored thread_start_not_executed`) |
| `client/turn/start`     | refused observably (`ignored out_of_contract`)                    |
| absent / unrecognised   | refused observably (`ignored out_of_contract`)                    |

`client/thread/start` is handled **explicitly**, not folded into the unrecognised
refusal: on this SDK its payload carries no execution options (GH-706), so it is
not executed evidence — but if it ever IS seen carrying them, the distinct
`thread_start_not_executed` marker surfaces that change instead of silently
dropping it. The fire-and-forget handler usually still sees `client/thread/start`
at `thread.created` (the spawn turn has not advanced when its loopback RPC
finishes), in which case no row is written; when the turn has advanced, the result
carries `client/turn/requested` and the executed triple records. Deterministic
turn-event sourcing (via `bb.sdk.threads.events.wait`, the SDK RPC analog of
`thread log --json`) remains out of scope: `bb.events.on` exposes only the six
thread transitions (created/active/idle/failed/archived/deleted), so a
lifecycle-event recorder is not available.

So the store name (`thread-executed-triples.jsonl`) is true — every row in it
really is the triple that executed — and a consumer selecting the artifact by its
documented contract cannot misclassify a non-executed row.

## Project scope and registry binding

`thread.created` is server-wide, so the recorder refuses to mis-attribute:

- **One registry authority.** The Python recorder resolves `thread.projectId`
  against every registered project carrying a `bb` block. The plugin performs no
  second ownership lookup and has no project setting. A project without a `bb`
  block remains outside recorder coverage.
- **Repo-target-aware scope match.** The thread's bb project may match any native
  placement resolved from a candidate project's exact repo-target mappings,
  with the single `bb.project_id` fallback when the mapping is absent. The
  [canonical BB mapping schema](../../docs/multi-project.md#registering-projects)
  owns validation. No match is **ignored observably** (exit 0 +
  `ignored unknown_thread_project` → info log) and writes nowhere.
- **Ambiguity and malformed candidates fail closed.** Duplicate native ids refuse
  and name every colliding project. A malformed `bb` block or invalid/padded
  native id refuses rather than silently disappearing from the candidate set.
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
`source: client/turn/requested` by construction (other sources are refused before
storage), so it never varies between rows and carries no identity. If the contract
ever admits a second source, `source` must return to identity; without it a row
sharing `(provider, model, reasoning_level)` from a different source would no-op
and be silently discarded rather than surfaced as a conflict. Each row
stores the **resolved** values, never a preset name.

## State

`{project_state_root}/{project_id}/thread-executed-triples.jsonl` — the project
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
