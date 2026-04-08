# PM2 Watcher Adapter (Optional)

PM2-backed watchers run `watch_inbox.py` as a persistent background process per agent, polling for new messages and sending desktop notifications.

PM2 is entirely optional. You can check your inbox manually at any time with:
```bash
python bin/inbox.py --me <agent_id>
```

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
