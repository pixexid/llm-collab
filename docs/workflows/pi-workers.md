# Pi Workers: Hosting, Configuration, And Provisioning

## Goal

How the Pi-runtime implementation workers (`glmpi`/Glim, `relay`/Relay, `kimi`/Kimi,
and any future Pi worker) are hosted, what they inherit by default, and how a new one
is provisioned. This covers the *Pi worker* specifically; the binding lifecycle it
rides on is in [`session-autobridge-runbook.md`](session-autobridge-runbook.md) — read
that for activate/inspect/deactivate mechanics rather than duplicating them here.

## What a Pi worker is

In `agents.json`, `glmpi`/`relay`/`kimi` are `cli_session` workers with
`watcher_enabled: true`. That definition is **host-agnostic on purpose** — it names the
worker and says it has an event watcher, and nothing about *where* the Pi session runs.
The host and endpoint live in the worker's exact **canonical binding record**, not in
`agents.json`. To learn which host a worker is actually on, read its binding
(`session_autobridge.py show-binding`), never `agents.json`.

## Livecraft is the sole Pi host — the binding remains authority

Pi workers use the machine-wide **Livecraft** host. Do not start another host or create a
second per-project host. The service is shared across projects and workers; each
worker still gets an exact project/chat/agent/repository binding, and that binding is
the authority for the native session identity.

- Manager: `127.0.0.1:43120`
- Backend: `http://127.0.0.1:43121`
- UI: `http://127.0.0.1:43122`

The frontend port is pinned to 43122 via `PI_LIVECRAFT_FRONTEND_PORT` and Vite
`strictPort: true` in the local pi-livecraft checkout. A taken port fails loudly
instead of silently drifting to another port.

Livecraft is the only supported Pi host. Existing legacy bindings must be replaced
explicitly, preserving their history, unread packets, and in-flight evidence.

> Livecraft end-to-end wake/drain was proven live (GH-486 — a real `to-glmpi` durable packet woke
> a real Livecraft-hosted `glmpi`, which drained with exact-session `--acknowledge` and
> replied `from-glmpi`).

## Ponytail and other defaults are runtime-global

Every Pi worker on a given Pi runtime home inherits the same defaults, because they are
set once for the runtime, not per worker or per host. In `~/.pi/agent/settings.json`,
`packages` includes `npm:@dietrichgebert/ponytail` alongside `npm:pi-event-monitor` and
the Pi skills. So:

- **All Pi workers get ponytail by default** — glmpi, relay, kimi, and any future Pi
  worker on Livecraft. There is nothing to wire per worker or per host.
- The event monitor and Pi skills are inherited the same way.

If you ever need a worker *without* a runtime-global default, that is a separate Pi
runtime home, not a per-worker toggle.

## Machine-wide service and health gate

The canonical user service is `com.pixexid.pi-livecraft`, installed as
`$HOME/Library/LaunchAgents/com.pixexid.pi-livecraft.plist`. Its stable checkout is
`$HOME/Projects/pi-livecraft`, it runs with Node 24, and `npm run dev` owns the
manager, backend, and frontend as one supervised process group. `RunAtLoad` and
`KeepAlive` recover a process-group exit; they do not override a backend that still
reports `managerConnected: false`.

Use these commands for deterministic inspection/recovery (never GUI-poke the app):

```bash
export PATH="$HOME/.nvm/versions/node/v24.18.1/bin:$PATH"
launchctl print "gui/$(id -u)/com.pixexid.pi-livecraft"
curl --fail-with-body --max-time 2 http://127.0.0.1:43121/api/health
launchctl kickstart -k "gui/$(id -u)/com.pixexid.pi-livecraft"
```

Health is ready only when the endpoint returns HTTP 200 with
`{"ok":true,"managerConnected":true}`. HTTP 503 with
`managerConnected:false` is a global unhealthy state; do not kick the service because
that can interrupt active sessions. A connection refusal is restartable only when both
43120 and 43121 are unused, and the worker gate performs at most one serialized
LaunchAgent kick followed by a bounded health wait.

Both `bin/worker.py start-livecraft-pi` and `bin/livecraft_wake.py` run this gate before
creating a native session or sending a command. A failed gate leaves native session
creation and wake delivery untouched and reports the recovery condition clearly.

## Provisioning a new Pi worker

- **Livecraft first-start:** `bin/worker.py start-livecraft-pi` drives
  Livecraft `POST /api/sessions`, sets model/thinking through Pi RPC, verifies the exact
  native id/cwd/fingerprint, waits for a bootstrap marker from the snapshot API, then
  registers through the canonical reserve/consume path. A mismatch or missing marker
  creates no binding; Livecraft has no delete endpoint, so failed cleanup can only issue
  native `abort`.
- **Single-repository projects:** when the project's `repos` map has exactly one key,
  `--repo-target` is optional:

  ```bash
  bin/llm-collab worker.py start-livecraft-pi \
    --agent <agent-id> --project <project-id> --chat <CHAT-ID>
  ```

  Multi-repository projects still require the repository key. A missing or invalid
  key fails before native session creation and lists the valid keys.
- **The first-start flow validates exact scope.** It validates exact
  project/worker/chat/repo/cwd scope, creates exactly one native session, verifies its
  returned identity and provider/model/thinking fingerprint, and registers only after
  the bootstrap marker succeeds. The worker bootstrap does not install a foreground
  event monitor; the Livecraft host owns background wakes.
- **The Livecraft path is production by default.** Without `--disposable`, the command
  requires a current project snapshot with `canonical_writes: true` and resolves the
  stored worker profile plus the active starter binding. Pass explicit provider/model/
  thinking/runtime-home values only when overriding that profile. `--disposable` selects
  the separately gated pilot path, which still requires the valid `runtime_dispatch`
  declaration, exact-thread environment gate, `LLM_COLLAB_CANONICAL_CONTROL=enabled`,
  an exact `--pilot-scope <project>/<agent>`, and the explicit confirmation.

## Current worker hosts

- Every active worker must have a Livecraft binding. Replace a stale legacy binding
  explicitly, preserving its history, unread packets, and in-flight evidence.

## See also

- [`session-autobridge-runbook.md`](session-autobridge-runbook.md) — binding activate /
  inspect / deactivate, and the minimum proof before relying on a watcher.
- [`claude-code-desktop-computer-use-bridge.md`](claude-code-desktop-computer-use-bridge.md)
  — agent-to-agent wake/doorbell reference.
