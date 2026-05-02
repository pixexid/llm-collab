# Session Autobridge Runbook

Session autobridge lets a worker bind the current runtime thread to a
project/chat so future messages can be routed to that parked worker session.

Use it to reduce short manual relays. Do not use it to make workers fully
autonomous.

## Safety Defaults

- Prefer `notify` or `auto-read` over `auto-reply`.
- Keep one registered session per active worker/chat unless intentionally
  superseding an old one.
- Keep operator-visible chat notes enabled; autobridge activity must stay
  visible in `Chats/`.
- Do not target an active operator thread for queue/busy tests.
- Treat Claude desktop as an existing-thread, human-visible UI. Do not claim
  safe fresh Claude desktop sidebar thread creation.

## Activate A Session

From the collaboration repo:

```bash
python3 bin/session_autobridge.py publish-current \
  --session SESSION-codex-amiga-dispatch \
  --agent codex \
  --runtime-family codex_app \
  --project amiga \
  --chat CHAT-xxxx \
  --mode auto-read \
  --status parked \
  --wake-strategy runtime_trigger \
  --ttl-seconds 3600
```

Use the current chat id, not `last`, for durable bindings. If the runtime cannot
be discovered automatically, register the session explicitly:

```bash
python3 bin/session_autobridge.py register \
  --session SESSION-codex-amiga-dispatch \
  --agent codex \
  --project amiga \
  --chat CHAT-xxxx \
  --mode auto-read \
  --status parked \
  --wake-strategy runtime_trigger \
  --runtime-family codex_app \
  --runtime-session-id <runtime-thread-id> \
  --runtime-session-source manual
```

## Inspect Bindings

Show the registered session:

```bash
python3 bin/session_autobridge.py show \
  --session SESSION-codex-amiga-dispatch \
  --json
```

Show the canonical project/chat/agent binding:

```bash
python3 bin/session_autobridge.py show-binding \
  --project amiga \
  --chat CHAT-xxxx \
  --agent codex \
  --json
```

Inspect inbox queue state:

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("agents/codex/inbox.json")
print(json.dumps(json.loads(path.read_text()), indent=2))
PY
```

## Send To A Bound Worker

Use `deliver.py` as usual. If a binding exists, `deliver.py` resolves
`target_session_id` automatically:

```bash
python3 bin/deliver.py \
  --chat CHAT-xxxx \
  --from codex \
  --to cdx2 \
  --project amiga \
  --title "Review watcher retry fix" \
  --body-file /tmp/message.md
```

If no dispatchable target exists, the command still writes the chat/inbox
message and prints the manual relay prompt.

## Watch Or Run One Dispatch Pass

PM2 watcher:

```bash
python3 bin/pm2_watchers.py ensure --agent codex
python3 bin/pm2_watchers.py status --agent codex
python3 bin/pm2_watchers.py logs --agent codex --lines 100
```

Manual one-shot watcher:

```bash
python3 bin/watch_inbox.py --me codex --max-polls 1 --json
```

For Codex, manual and PM2 watcher runs default to:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

## Busy Or Queued Messages

When a targeted Codex runtime session is busy, the watcher must defer instead of
interrupting:

- message remains in `agents/<agent>/inbox.json` under `unread`
- message appears in `queued`
- watcher emits `autobridge_deferred_busy`
- later watcher passes retry while the message remains unread
- when the runtime becomes idle, watcher emits `autobridge_consumed` and moves
  the message to `read`

If a message is intentionally abandoned, clear it explicitly by marking it read:

```bash
python3 bin/inbox.py --me codex --mark-all-read
```

Use this only when the unread queue is known to be stale. For a single stale
message, edit `agents/<agent>/inbox.json` carefully or write a small local
maintenance script that moves that exact path from `unread` to `read` and
removes the matching `queued` entry.

## Deactivate A Session

Stop a session when leaving a thread, replacing a worker, or ending a test:

```bash
python3 bin/session_autobridge.py deactivate \
  --session SESSION-codex-amiga-dispatch \
  --status stopped
```

Supersede an old session with a known replacement:

```bash
python3 bin/session_autobridge.py deactivate \
  --session SESSION-codex-amiga-dispatch \
  --status superseded \
  --superseded-by SESSION-codex-amiga-dispatch-2
```

## Minimum Proof Before Relying On A Watcher

Run the automated suite:

```bash
python3 -m py_compile \
  bin/_helpers.py \
  bin/_session_autobridge.py \
  bin/deliver.py \
  bin/inbox.py \
  bin/session_autobridge.py \
  bin/watch_inbox.py \
  tests/test_session_autobridge.py

python3 -m unittest tests.test_session_autobridge
```

For a real-worker session, also prove:

- `deliver.py` resolves the expected `target_session_id`
- watcher emits `autobridge_dispatch`
- busy target emits `autobridge_deferred_busy`
- later idle pass emits `autobridge_consumed`
- `unread` and `queued` are empty after consumption
- a chat note records the pickup/dispatch event for the operator
