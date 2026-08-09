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

The plugin records ONLY the thread's creation-time default execution options for
BB threads on `thread.created` (source `client/thread/start`); turn-derived
sources are refused observably and deferred to the `client/turn/requested` re-scope
(GH-695 P1-B). Its design, project scope, and immutability semantics are documented
in `bb-plugins/exec-tracking/README.md`. This doc owns only the operator procedure.

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

After an SDK bump, regenerate the declarations with `bb plugin types .` and
re-run `npm run typecheck` before reinstalling.
