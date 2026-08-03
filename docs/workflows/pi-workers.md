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

## Two hosts coexist — the binding endpoint is authority

There are two Pi hosts, and both are valid:

- **Pi-Web** (`endpoint_pi_web_local`) — the existing host. Loopback manager at
  `http://127.0.0.1:8504`.
- **Livecraft** (`endpoint_pi_livecraft_local`) — the newer host. Manager at
  `127.0.0.1:43120`, backend at `http://127.0.0.1:43121`, UI at `http://127.0.0.1:5173`.

**Livecraft is the preferred host for new Pi provisioning** where its API is available.
It is **not** the universal host: Pi-Web is not deprecated, and existing Pi-Web workers
are not bulk-migrated. Each worker's endpoint is whatever its binding record says —
treat that as the single authority. Migrate a worker only through an explicit
per-worker replacement/cutover (with reconciliation and rollback), one worker at a
time, never in bulk just to make things uniform.

> Livecraft is the *preferred new* host, not the *proven production provisioning* path.
> Its end-to-end wake/drain was proven live (GH-486 — a real `to-glmpi` durable packet
> woke a real Livecraft-hosted `glmpi`, which drained with exact-session `--acknowledge`
> and replied `from-glmpi`). But a production first-start/spawn path on Livecraft is not
> yet implemented — see *Provisioning* below.

## Ponytail and other defaults are runtime-global

Every Pi worker on a given Pi runtime home inherits the same defaults, because they are
set once for the runtime, not per worker or per host. In `~/.pi/agent/settings.json`,
`packages` includes `npm:@dietrichgebert/ponytail` alongside `npm:pi-event-monitor` and
the Pi skills. So:

- **All Pi workers get ponytail by default** — glmpi, relay, kimi, and any future Pi
  worker, on Livecraft or Pi-Web alike. There is nothing to wire per worker or per host.
- The event monitor and Pi skills are inherited the same way.

If you ever need a worker *without* a runtime-global default, that is a separate Pi
runtime home, not a per-worker toggle.

## Provisioning a new Pi worker

Pi-Web has a working manual first-start; Livecraft does not yet. Know which host you are
provisioning on before you promise a new worker to a session:

- **Pi-Web: manual first-start exists.** `bin/worker.py start-pi` (→
  `worker_rotate_pi.py:start_pi`, called with `supersedes=None`) provisions a fresh
  Pi-Web native session for an existing agent: it resolves the agent's profile and repo
  cwd, provisions and binds one native session, verifies the returned native id and the
  provider/model/thinking fingerprint, bootstraps the worker, and registers the binding
  as active. `bin/worker.py rotate-pi` handles rotating a context-bloated worker to a
  fresh session (`--supersedes-session`). Both are Pi-Web-specific and
  operator-invoked — not something a worker triggers autonomously.
- **Livecraft: no spawn path yet.** The Livecraft `glmpi` used in GH-486 was
  hand-provisioned: create the native session (Livecraft `POST /api/sessions`),
  bootstrap it through the real Pi RPC command path until it returns its marker, then
  register the binding through the canonical reserve/consume path. There is no
  `start-pi`/`rotate-pi` equivalent for Livecraft.
- **A Livecraft first-start/spawn tool is planned** (GH-94), reusing the same
  create/verify/bootstrap/register discipline the Pi-Web path already proves. When it
  lands it must: validate exact project/worker/chat/repo/cwd scope; reserve the binding;
  create exactly one native session; verify the returned native id and
  provider/model/thinking fingerprint; bootstrap the worker; and register the binding
  **only after** the bootstrap marker succeeds. Any mismatch leaves the durable packet
  pull-pending and creates **no** binding.
- **The Livecraft path stays gated.** Production mutation is off by default. A pilot
  requires `runtime_dispatch` plus the exact-thread dispatch subscription and the
  canonical-write / current-authority gates, plus an explicit disposable or allowlisted
  worker/project scope — no single generic flag enables it.
- Do not add a provider abstraction spanning both hosts until both actually need one.

## Current worker hosts

- `glmpi` — a Livecraft binding exists and is proven (GH-486); earlier bindings were on
  Pi-Web. Read the specific binding you intend to use.
- `relay`, `kimi` — remain on their Pi-Web bindings. Leave them there until each is next
  needed, then migrate that one worker through an explicit replacement that preserves its
  history, unread packets, and in-flight evidence.

## See also

- [`session-autobridge-runbook.md`](session-autobridge-runbook.md) — binding activate /
  inspect / deactivate, and the minimum proof before relying on a watcher.
- [`claude-code-desktop-computer-use-bridge.md`](claude-code-desktop-computer-use-bridge.md)
  — agent-to-agent wake/doorbell reference.
