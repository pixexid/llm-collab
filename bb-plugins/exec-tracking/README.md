# bb-plugin-exec-tracking

Custom bb plugin — the **task/execution-tracking** plugin (GH-630). This checkout's
first capability is the **executed-triple recorder** (GH-617): on `thread.created`
it records the resolved `(provider, model, reasoning_level)` profile that executed
into git-tracked state.

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
3. hands them to a detached child running `bin/record_executed_triple.py`
   (`stdio: "ignore"` + `unref()` — the handler never waits on the write).

A profile that cannot be resolved is recorded as an explicit `unresolved` row, so
an absent row and a failed resolution are distinguishable. The plugin **records,
never gates**: `thread.created` handlers have no veto hook, so enforcement stays at
the CLI call sites.

## State

Git-tracked JSONL, one record per line, project-scoped by path:

```
<checkout>/records/executed-triples/<project_id>.jsonl
```

The volatile `project_state_root` (`projects/`) is gitignored by design, so a
diffable attribution record lives in this dedicated tracked directory. See
`records/README.md`. Each row stores the **resolved** values, not a preset name.

## Operator install + config (not done here)

Installation is a separate operator step. This directory only builds and tests the
code.

```sh
bb plugin install ./bb-plugins/exec-tracking   # operator step
bb plugin config exec-tracking set checkoutPath /path/to/llm-collab
bb plugin config exec-tracking set pythonPath  /abs/path/to/python3.11   # server PATH is narrow
bb plugin config exec-tracking set projectId   llm-collab                # optional; default llm-collab
bb plugin reload exec-tracking
```

`checkoutPath` and `pythonPath` must be set before any triple is recorded; until
then the handler logs a warning and records nothing (rather than guess a path).
