# PM2 Watcher Adapter (Optional)

PM2-backed watchers run `watch_inbox.py` as a persistent background process per agent, polling for new messages and sending desktop notifications.

PM2 is entirely optional. You can check your inbox manually at any time with:
```bash
python bin/inbox.py --me <agent_id>
```

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

`pm2/ecosystem.config.cjs` reads `agents.json` dynamically and generates one PM2 app per agent where `activation.watcher_enabled: true` and type is not `human_relay` or `human`.

App naming: `{workspace_name}-{agent_id}` (workspace_name from `collab.config.json`)

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

## Busy-session behavior

Autobridge delivery does not interrupt a busy Codex session.

If a targeted Codex runtime thread is already active:

- the watcher emits `autobridge_deferred_busy`
- the message remains in `unread`
- the inbox records a `queued` entry with session metadata
- the next watcher pass retries that queued message first for the same target session

When the target session returns to idle, the next watcher pass drains the queued message and emits `autobridge_consumed`.

This applies to:

- PM2-backed watchers
- one-shot/manual `watch_inbox.py` runs

---

## Claude Desktop Constraint

PM2/watcher automation must not be treated as the controller for the Claude
desktop app. PM2 can watch `llm-collab` inbox files and dispatch shell/runtime
adapters, but it cannot call Codex Computer Use tools.

Current safe assumption:

- Codex app visible refresh is automatable
- Claude desktop app interaction is automatable only from a live Codex turn using
  Computer Use
- Claude desktop fresh sidebar thread creation is safe only when Computer Use
  generates a UUID plus short title, clicks `New session`, sends the first
  visible prompt beginning with `[BRIDGE <8-char-uuid-prefix>] <short title>`,
  and verifies the new sidebar title/local URL

Why:

- Claude desktop visible threads depend on app-managed Electron storage under:
  - `~/Library/Application Support/Claude/IndexedDB/...`
  - `~/Library/Application Support/Claude/Session Storage/...`
- Claude CLI/project sessions live under:
  - `~/.claude/projects/<project-slug>/...`
- Writing the CLI/project session store does not guarantee that a new thread appears in the desktop app sidebar

Watcher policy for Claude:

- safe: record durable work in `llm-collab` and notify that a Claude desktop
  bridge plan is needed
- safe: let a Codex heartbeat drive Claude desktop through Computer Use
- safe: if Computer Use cannot inspect or send, keep the Codex heartbeat active,
  try reasonable app-control recovery paths, and record a blocker for
  Codex/Computer Use retry or tooling repair
- unsafe: claim a PM2 watcher created a new app-visible desktop thread
- unsafe by default: synthesize sidebar visibility by writing app cache/index files directly
- unsafe: use `claude -p`, `claude --resume`, or `~/.claude/projects` as proof
  that the visible desktop thread changed
- unsafe: ask the operator to wake Claude or paste the bridge prompt before
  Codex has exhausted Computer Use/app-control recovery

If an operator needs Claude desktop visibility, the recommended flow is:

1. write the task/message to `Chats/` with `deliver.py`
2. create a Codex-side bridge plan and heartbeat
3. have the heartbeat wake Codex, not Claude
4. Codex uses Computer Use to open/select/create the Claude desktop thread and
   sends the bounded prompt
5. while Claude is running, the heartbeat observes only
6. once Claude is idle/awaiting input, Codex reads the visible response and
   records it back into the Codex thread and `llm-collab`
7. delete the heartbeat when the response is recorded, blocked, timed out, or no
   longer needed

---

## Disposable queue test

Use a disposable Codex thread for queue validation. Do not target an active operator thread.

Canonical flow:

1. register or refresh a disposable Codex autobridge session
2. start a long-running turn on that disposable Codex thread
3. send a targeted worker message to that exact Codex runtime session
4. run one watcher pass while the thread is busy
5. confirm `autobridge_deferred_busy` and that the message remains in `unread` plus `queued`
6. wait for the disposable thread to return to idle
7. run a second watcher pass
8. confirm `autobridge_consumed`, `unread: []`, and `queued: []`

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
