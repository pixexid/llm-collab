# Exec-Tracking Plugin

## Authority

This workflow is the single source of truth for building, type-checking,
installing, and configuring the `exec-tracking` bb plugin
(`bb-plugins/exec-tracking/`). The plugin's own `README.md` and any other
branch-local documentation LINK HERE instead of copying the procedure — a restated
command sequence is a cached copy that goes stale without telling anyone (the
AGENTS.md "this file is the source of truth" rule, and the 27-packet incident it
cites). If a flag, a type-generation command, or an install step changes, it
changes here once.

Its design, project scope, and immutability semantics are documented in
`bb-plugins/exec-tracking/README.md`. That README also owns the artifact's
definition — **executed-triple evidence**: which `(provider, model,
reasoning_level)` triple actually ran, and which event source is authoritative
for it. This doc links to that definition rather than restating it, and owns
only the operator procedure.

## Why a typecheck gate is required

Path installs load `server.ts` directly as TypeScript with **no build step**, and
`bb plugin build` uses esbuild, which strips types without checking them — so an
undeclared identifier crashes at runtime instead of being caught at build (that is
the GH-695 P1-A defect). The package therefore ships a real TypeScript gate
(`tsconfig.json`, `strict` + `noEmit`, mapping `@bb/plugin-sdk` to the committed
`types/bb-plugin-sdk.d.ts`) exposed as the `typecheck` script. It fails on an
undeclared identifier (`TS2304`) before the plugin reaches a live server.
`skipLibCheck` is `true` so the gate does not re-check the SDK's own bundled
declaration (bb's responsibility); `server.ts` itself is fully checked.

## Procedure

Run from the plugin directory. The typecheck MUST pass before install.

```sh
cd bb-plugins/exec-tracking
bb plugin types .          # (re)write types/bb-plugin-sdk.d.ts for this bb
npm install                # install typescript + @types/node (devDependencies)
npm test                   # SQLite concurrency, persistence, retry, and isolation
npm run typecheck          # FAILS on an undeclared identifier before you install
bb plugin install --yes .  # operator step — only after typecheck is green
bb plugin config exec-tracking set checkoutPath /path/to/llm-collab
bb plugin config exec-tracking set pythonPath  /abs/path/to/python3.11   # server PATH is narrow
bb plugin reload exec-tracking
```

`checkoutPath` and `pythonPath` must be set before a row is recorded or a wake is
resolved; until then the handler logs a warning and writes/sends nothing rather
than guess a path.
The plugin has no project setting: its Python child resolves each native thread
project against every registered project carrying a `bb` block, so the registry
lookup exists in one place. In each covered project's workspace `projects.json`
entry, set the native bb project id (the `projectId` reported by bb itself, for
example `bb thread show <thread-id> --json`), not the llm-collab registry slug:

```json
"bb": {
  "project_id": "<native-bb-project-id>"
}
```

Without a `bb` block, that project remains outside recorder coverage and its
native thread id is ignored with a reason. Leave out `enabled`: setting
`enabled: true` also activates bootstrap, continuation, and the spawn gate.

After an SDK bump, regenerate the declarations with `bb plugin types .` and
re-run `npm run typecheck` before reinstalling.

## Silent-wake activation gate

`exec-tracking` also owns abnormal native thread wakes and the CLI ingress used
by the `pr-artifacts` and `heartbeat` host watchers. Its per-plugin SQLite row is
dedupe state only: project, current role thread, producer family, semantic
digest, pending bit, and reservation token. It is not a task queue or role
authority. The role target is resolved from the registered native-project map
and `role-generation.md` each time an event fires.

Plugin `running` state is the native abnormal-wake liveness signal. Do not add a
third host marker. A running path install does not automatically follow checkout
changes, so only the orchestrator reloads the plugin, and only after independent
review of the draft.

Before merge, the orchestrator creates a dedicated **visible** probe thread with
the repository-configured BB executable, verifies the returned thread
`visibility` is `visible`, sends the fixed pointer with every input part marked
`agent-only` and `queue-if-active`, then verifies that the send added no
user-facing row and changed neither unread nor attention state. Never aim this
probe at an operator or authority thread. If BB renders even an empty row, stop
as `BLOCKED` unless the alternative acceptance branch is proven literally: one
atomic pending wake total for the probe role thread across event count, plugin
reload, and both native and CLI producers.
