# PM2 Watcher Adapter (Optional)

PM2-backed watchers run `watch_inbox.py` as a persistent background process per agent, polling for new messages and sending desktop notifications.

PM2 is entirely optional. You can check your inbox manually at any time with:
```bash
python bin/inbox.py --me <agent_id>
```

For Amiga collab-loop waits, Claude owns ongoing PR/CI, bot-review, inbox-reply,
and doorbell monitoring. Codex should prefer an attended one-shot check and hand
continuing watches to Claude instead of keeping a Codex thread heartbeat alive.
If Codex must create a monitor, use one monitor per purpose, clear stale prior
monitors first, and delete or update it as soon as its purpose is served.

Manual one-shot watcher runs use the same Codex refresh defaults as PM2:

```bash
python bin/watch_inbox.py --me codex --max-polls 1 --json
```

For Codex, both PM2 and manual watcher runs default to:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

---

## Requirements

```bash
npm install -g pm2
```

---

## How it works

`pm2/ecosystem.config.cjs` reads `agents.json` dynamically and generates one PM2
app per agent where `activation.watcher_enabled: true`. The current ecosystem
file does not filter by activation type. `human` and `human_relay` entries are
normally configured with watchers disabled, but setting their flag to `true`
will currently create a watcher.

App naming: `{workspace_name}-{agent_id}` (workspace_name from `collab.config.json`)

PM2 materializes this configuration when a process is started/reloaded; changing
`agents.json` does not automatically remove or reconfigure an already-running
process. A PM2 process saved before an agent was disabled/removed can also return
after reboot even though the current ecosystem would not create it.

After roster/watcher changes, compare `python bin/pm2_watchers.py status --all`
and `pm2 list` with current `watcher_enabled: true` entries. Stop/delete stale
named processes (`python bin/pm2_watchers.py delete --agent <id>` while the ID
remains in the roster, otherwise `pm2 delete <workspace>-<agent>`), start/ensure
the intended set, then run `pm2 save` so reboot state matches the reconciled
process list. Do not treat a healthy stale PM2 process as proof that current
routing policy authorizes it.

---

## Commands

```bash
# Start all watchers
python bin/pm2_watchers.py start --all

# Ensure a specific watcher is running (start if not)
python bin/pm2_watchers.py ensure --agent orchestrator

# Check status
python bin/pm2_watchers.py status --all
python bin/pm2_watchers.py status --agent orchestrator

# View logs
python bin/pm2_watchers.py logs --agent orchestrator
python bin/pm2_watchers.py logs --agent orchestrator --lines 100

# Stop
python bin/pm2_watchers.py stop --agent orchestrator
python bin/pm2_watchers.py stop --all

# Remove from PM2
python bin/pm2_watchers.py delete --all
```

---

## Notifications

`watch_inbox.py` detects the OS automatically:

| Platform | Notification method |
|----------|-------------------|
| macOS | `osascript` display notification |
| Linux | `notify-send` |
| Other | Silent (no-op) |

Notifications can be disabled globally: `"notifications_enabled": false` in `collab.config.json`.

---

## Current retry behavior (no busy queue)

The current session autobridge does **not** obtain an authoritative Codex thread
busy/idle state and does not implement a distinct busy queue. In particular:

- `agents/<agent>/inbox.json` has `unread` and `read`; it has no autobridge
  `queued` field
- the watcher does not emit `autobridge_deferred_busy`
- an attempted runtime trigger that returns nonzero emits `autobridge_failed`
  and leaves the message unread
- each later watcher pass considers unread messages again; a successful runtime
  result emits `autobridge_consumed` and moves the message to `read`

This retry shape applies to PM2-backed and one-shot/manual `watch_inbox.py`
runs. A failure is not proof that a busy runtime rejected the turn before
acceptance, so this behavior must not be described as safe busy deferral. Avoid
targeting a running operator thread.

The planned [Thread Event Runner](../workflows/thread-event-runner-rfc.md)
defines transactional busy deferral, coalescing, leases, and ambiguous-delivery
reconciliation. None of those guarantees are implemented by the current PM2
watcher.

---

## Desktop-app constraint and wake priority

PM2/watcher automation must not be treated as the controller for a desktop app.
PM2 can watch `llm-collab` inbox files and dispatch configured shell/runtime
adapters, but it cannot perform Codex Computer Use recovery.

Current safe ordering:

- first write the durable `llm-collab` packet
- for an AX-capable `cli_session` with `activation.ax_app`, use
  `axsend-ensure ring --submit --verify` once as the primary wake even when the
  recipient is busy; exit 0 means delivered/queued, so never repeatedly re-ring
- use attended Computer Use only as fallback/recovery when AX cannot safely
  inspect/target/send, or for an explicitly project-configured non-CLI desktop
  bridge; apply the idle input gate before this screenshot/keyboard fallback
- PM2/heartbeat remains a bounded observation safety-fuse, not the primary wake

Why:

- Claude desktop visible threads depend on app-managed Electron storage under:
  - `~/Library/Application Support/Claude/IndexedDB/...`
  - `~/Library/Application Support/Claude/Session Storage/...`
- Claude CLI/project sessions live under:
  - `~/.claude/projects/<project-slug>/...`
- Writing the CLI/project session store does not guarantee that a new thread appears in the desktop app sidebar

Watcher policy for desktop-app agents:

PM2/heartbeat is only the bounded, provisional safety-fuse described in
`session-autobridge-runbook.md`.

- primary: ring the registered AX app once, even while it is busy, with one short
  pointer to the durable packet; treat exit 0 as delivered/queued and do not
  repeatedly re-ring
- recovery: if AX targets an embedded preview/web field or cannot verify the
  native composer, preserve the packet, stop sending, and use an attended Codex
  turn with Computer Use plus
  `bin/axsend-ensure tree --app <app> --editable-only` to remove/blank the
  competing field and verify the real native prompt before resuming AX
- fallback: use Computer Use to send only when AX remains unavailable/unsafe or
  the project explicitly configured a non-CLI desktop bridge; apply the Computer
  Use idle input gate and one-line pointer rule
- never convert one AX targeting incident into a standing mailbox-only or
  AX-disabled policy
- unsafe: claim a PM2 watcher created a new app-visible desktop thread
- unsafe by default: synthesize sidebar visibility by writing app cache/index files directly
- unsafe: use `claude -p`, `claude --resume`, or `~/.claude/projects` as proof
  that the visible desktop thread changed
- unsafe: ask the operator to wake an agent or paste the bridge prompt before
  AX plus attended Computer Use/app-control recovery has been exhausted

If desktop visibility is needed, the recommended flow is:

1. write the task/message to `Chats/` with `deliver.py`
2. ring the recipient's registered app via AX once even if it is busy, with one
   short sender-tagged pointer to the durable packet; exit 0 means
   delivered/queued and must not be repeatedly re-rung
3. the recipient drains its unread inbox and acts; it rings back on handoff
4. if AX targets the wrong editable surface or cannot verify delivery, run the
   attended Computer Use recovery above, then retry AX once the real composer is
   verified; use Computer Use send only as the bounded fallback
5. only if the ring is blocked or a running worker's response is expected, create
   a bounded provisional safety-fuse heartbeat
6. while the target is running, the heartbeat observes only; delete it when the
   response is recorded, blocked, timed out, or no longer needed

---

## Disposable retry test

Use a disposable runtime adapter/session for retry validation. Do not target an
active operator thread and do not treat this as a busy-deferral test.

Current test shape:

1. register a disposable autobridge session with a bounded test adapter
2. deliver one message to that exact disposable target
3. make the adapter return nonzero on the first watcher pass
4. confirm `autobridge_failed` and that the message remains in `unread`
5. make the adapter return success on a later watcher pass
6. confirm `autobridge_consumed`, `unread: []`, and the message in `read`

This proves failure retry and eventual consumption only. Authoritative Codex
busy detection, coalescing, and no-duplicate delivery remain future runner
integration gates.

Useful inspection points:

- `agents/codex/inbox.json`
- `Logs/watchers/codex.pm2.out-1.log`
- `State/session_autobridge/events/<session>.jsonl`

---

## Survive system reboots

```bash
pm2 startup    # follow the printed instructions
pm2 save       # save current process list
```

After this, PM2 and all running watchers restart automatically on reboot.

---

## Log locations

Logs are written to `Logs/watchers/{agent}.pm2.{out,err}.log`.

These files are gitignored.
