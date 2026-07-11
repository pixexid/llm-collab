# Session Autobridge Runbook

Session autobridge lets a worker bind the current runtime thread to a
project/chat so future messages can be routed to that parked worker session.

Use it to reduce short manual relays. Do not use it to make workers fully
autonomous.

## Status: provisional safety-fuse, not the primary wake

The primary agent-to-agent wake mechanism is now the **bidirectional Computer-Use
doorbell** (see `claude-code-desktop-computer-use-bridge.md`): whichever agent
finishes work or needs something rings the other immediately, with `llm-collab`
as the durable mailbox. Routine/continuous polling is **deprecated** as the
primary wake — it wastes tokens and a heartbeat set on guessed timing can fire
into changed context.

Session autobridge and PM2/heartbeat polling survive only as a bounded,
**provisional/experimental safety-fuse**, on trial, with hard constraints:

- only when a doorbell attempt is blocked, or a worker is visibly running and a
  handoff is expected
- for collab-loop waits such as PR/CI, bot-review comments, inbox replies, and
  doorbell handoffs, Claude owns the ongoing monitor; Codex should hand the
  watch to Claude instead of keeping an in-thread heartbeat alive
- task-scoped: tied to one specific task/worktree/branch and its chat
- auto-deletes on handoff/ack/blocker; must not outlive its task/chat
- never the primary path, never a standing always-on watcher
- one monitor per purpose; clear stale prior monitors before creating a new one
- must be fixed or removed if it misbehaves on real tasks

If the safety-fuse causes stale-context or duplicate-wake issues in practice,
remove it and rely on the doorbell + mailbox-drain self-heal.

## Safety Defaults

- Prefer `notify` or `auto-read` over `auto-reply`.
- Keep one registered session per active worker/chat unless intentionally
  superseding an old one.
- Amiga `cdx2` is a disabled legacy human-relay worker by default. Do not
  activate `cdx2` for new Amiga implementation work unless the operator
  explicitly re-enables it for that specific task.
- When a human-relay implementation worker is explicitly enabled for a task,
  create a fresh chat, task, and session binding for that task. Reuse a
  registered session only for the same task context, blocker repair, or
  review-fix loop.
- Keep operator-visible chat notes enabled; autobridge activity must stay
  visible in `Chats/`.
- Do not target an active operator thread for queue/busy tests.
- Treat Claude desktop as a human-visible UI controlled through Computer Use,
  not as a `session_autobridge.py` runtime target. Fresh Claude desktop threads
  can be created only by visible app interaction: generate a UUID plus short
  title, click `New session`, send the first prompt beginning with
  `[BRIDGE <8-char-uuid-prefix>] <short title>`, then verify the sidebar title
  and `local_*` URL. Do not claim a PM2 watcher, CLI resume, or filesystem write
  created a desktop-visible thread.
- For Claude-owned collaboration lanes, inspect the visible Claude app before
  treating inbox or queue state as final. If Claude is visibly asking a related
  question, waiting for direction, or reporting Read/Agent/tool errors, Codex
  must answer or unblock it in that same visible thread when safe; do not wait
  for a final inbox handoff while Claude is blocked in the app.
- If Claude is stale, idle with no durable progress, or repeatedly erroring,
  first try to wake or repair the same thread with a durable unblock packet plus
  one short bridge after the idle gate passes. Restart or reopen Claude only
  from an attended Codex recovery turn or after explicit operator instruction;
  unattended heartbeats must notify with the observed blocker instead of
  interrupting or restarting Claude. Create a new Claude thread only when the
  current thread is full, unrecoverably corrupted, repeatedly loses tool access,
  or still cannot continue after attended recovery; include a full continuity
  packet for the same task.

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
  --to <enabled-human-relay-worker> \
  --project amiga \
  --title "Review watcher retry fix" \
  --body-file /tmp/message.md
```

If no dispatchable target exists, the command still writes the chat/inbox
message and prints the manual relay prompt.

Do not use `--chat last` for a new implementation task unless you have just
created and verified that the latest chat is the task's dedicated chat. A new
task must not be carried into a previous worker thread just because the old
binding can still receive messages.

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

## Current Retry Behavior (No Busy Queue)

Session autobridge currently has no authoritative Codex busy/idle check, no
inbox `queued` field, and no `autobridge_deferred_busy` event. Do not rely on it
to protect a running target from a stacked or ambiguous `turn/start`.

The implemented behavior is narrower:

- a runtime trigger that reports nonzero emits `autobridge_failed`
- the matching message remains under `unread`
- later watcher passes consider the unread message again
- a later successful runtime result emits `autobridge_consumed` and moves the
  message to `read`

Because a transport failure may occur after runtime acceptance, an automatic
retry is not a proven exactly-once contract. Use disposable sessions for tests
and do not target an active operator thread. Transactional busy deferral,
coalescing, leases/fencing, and ambiguous-delivery reconciliation belong to the
planned [Thread Event Runner](thread-event-runner-rfc.md), not this provisional
autobridge.

If a message is intentionally abandoned, clear it explicitly by marking it read:

```bash
python3 bin/inbox.py --me codex --mark-all-read
```

Use this only when the unread set is known to be stale. For a single stale
message, edit `agents/<agent>/inbox.json` carefully or write a small local
maintenance script that moves that exact path from `unread` to `read`.

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
- a known pre-acceptance runtime failure emits `autobridge_failed` and leaves the
  message unread
- a later known-success pass emits `autobridge_consumed`
- `unread` is empty and the message is present in `read` after consumption
- a chat note records the pickup/dispatch event for the operator

This proof does not establish busy deferral or safe retry after ambiguous
runtime acceptance.
