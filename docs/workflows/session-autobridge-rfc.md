# Session Autobridge RFC

## Status

Experimental spike. Not part of the default `llm-collab` workflow yet.

## Problem

`llm-collab` can already do three useful things:

- deliver messages into an agent inbox
- watch unread pointers in the background
- print a relay prompt when a recipient is `human_relay`

What it cannot do today is treat a parked worker session as a scoped runtime target. There is no session registry, no lease model, no normalized runtime session handle, and no bounded wake hook that can safely say "this exact parked session should do one action now."

That leaves a gap for short back-and-forth loops where the operator currently has to relay every wake-up manually.

## Goals

- introduce a session-scoped source of truth instead of overloading `agents.json`
- let a parked session declare its mode, runtime identity, and wake strategy
- prove whether one bounded non-interactive wake path is viable
- degrade cleanly when the target session cannot actually be auto-woken
- keep the scope narrow enough that the spike can be tested safely
- make same-agent multi-session traffic unambiguous for sender, receiver, and self-handoff flows

## Non-goals

- no full autonomous reply system
- no global autobridge behavior for every agent
- no silent wake-up path for parked interactive `cli_session` terminals
- no attempt to auto-attach to Codex or Claude interactive sessions without an explicit runtime hook

## Proposed State Model

Store experimental session records under:

- `State/session_autobridge/sessions/<session-id>.json`

Each session tracks:

- `session_id`
- `agent_id`
- `supersedes_session_id`
- optional `project_id` and `chat_id` filters
- `mode`: `manual | notify | auto-read | auto-reply`
- `status`: `active | parked | stopping | stopped | superseded`
- `wake_strategy`: `none | notify | relay | runtime_trigger`
- lease metadata: owner and expiry
- runtime identity:
  - `runtime.family`
  - `runtime.session_id`
  - `runtime.session_source`
- optional bounded runtime command for `runtime_trigger`
- a per-session processed-message ledger so one inbox item does not fire twice

Each message may also carry session-scoped routing hints:

- `sender_agent_id`
- `sender_session_id`
- `target_session_id`
- `supersedes_session_id`

This keeps two distinct `codex` sessions from being conflated and lets `codex -> codex` traffic target a different live runtime explicitly.

Dispatcher artifacts live under:

- `State/session_autobridge/events/<session-id>.jsonl`
- `State/session_autobridge/prompts/<session-id>/...`

Canonical runtime bindings live under:

- `State/session_autobridge/bindings/<project-id>/<chat-id>/<agent-id>.json`

The binding is the shared lookup used by senders. It prevents "latest local app
session" from being treated as authoritative when the same worker has multiple
open sessions.

Canonical paired-thread state lives under:

- `State/session_autobridge/thread_pairs/<project-id>/<chat-id>/<agent-a>__<agent-b>.json`

This store is chat-scoped and unordered by direction. It records the latest
known live session id for each participant in a two-worker conversation so
reverse replies can target the paired sender thread instead of falling back to
"latest binding" or the currently active orchestrator thread.

## Current Experimental Command Surface

- Discover the current runtime session from local app/CLI state:
  - `python3 bin/session_autobridge.py discover-runtime --runtime-family codex_app`
  - `python3 bin/session_autobridge.py discover-runtime --runtime-family claude_app --project-path /path/to/project`
  - `python3 bin/session_autobridge.py discover-runtime --runtime-family gemini_cli`
- Publish the current runtime session into a parked `llm-collab` session lease:
  - `python3 bin/session_autobridge.py publish-current --session SESSION-... --agent codex --runtime-family codex_app --project amiga`
- Publish on first inbox read:
  - `python3 bin/inbox.py --me codex --project amiga --publish-session --session SESSION-... --runtime-family codex_app`

Discovery sources currently used by the spike:

- Codex app: `~/.codex/session_index.jsonl` with `history.jsonl` fallback
- Claude app: `~/.claude/projects/<project-slug>/sessions-index.json`
- Gemini: `~/.gemini/tmp/**/chats/session-*.json`

For Codex app sessions, the preferred dispatch path is the Codex App Server:

- The dispatcher first looks for `LLM_COLLAB_CODEX_APP_SERVER_URL`.
- If no URL is configured, it scans running `codex app-server --listen ws://...`
  processes whose environment includes the exact target `CODEX_HOME` value.
  Exact matching matters: `/Users/name/.codex` must not match
  `/Users/name/.codex-app-account2`.
- When found, it calls `thread/resume`, resolves the current default model via
  `model/list`, then calls `turn/start` for the target `runtime.session_id`.
- This path is preferred over `codex exec resume` because it emits the normal
  `turn/started`, `item/...`, and `turn/completed` notification stream that an
  already-open Codex renderer can observe.

After a successful app-runtime dispatch, the spike may still attempt a visible UI
refresh as a fallback:

- Codex app: no destructive refresh by default. The only non-relaunch refresh
  adapter is `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`, which requires the
  target Codex account to have been launched with `--remote-debugging-port`.
  The CDP adapter uses the DevTools protocol, not `Command-R`: it invalidates
  the thread cache, reloads the renderer page, waits for the sidebar row with
  `data-app-action-sidebar-thread-id=<session_id>`, then selects that row so the
  operator sees the updated real app thread.
  Without that port the dispatcher records a clear `ui_refresh_result` failure
  instead of pretending the open thread refreshed.
- `Command-R` is not a supported Codex refresh path. It does not refresh app
  threads.
- `codex://threads/<id>` can target the right account/thread, but it does not
  invalidate an already-open thread cache.
- Account relaunch is kept as an explicit fallback with
  `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=relaunch_account` only.
- Claude app: activate Claude and invoke `View > Reload This Page`
- Gemini CLI: no UI refresh hook

Set `LLM_COLLAB_UI_REFRESH=0` to disable this behavior. The refresh hook is
best-effort and is recorded as `ui_refresh_result` in the session event log.
Codex account relaunch is intentionally not the default because closing a live
worker window is visible and can disrupt long-running interactive work. The
app-server path is the primary non-relaunch execution route; CDP is the only
known non-relaunch renderer refresh route.

### Bidirectional Codex App Setup

Codex-to-cdx2 and cdx2-to-Codex use the same contract. Each visible Codex app
account that should receive auto-bridged work needs three runtime facts:

- a published runtime binding for the receiving `agent_id`, `project_id`, and
  `chat_id`
- a `codex app-server --listen ws://...` process running with the receiver's
  exact `CODEX_HOME`
- a visible Codex app renderer launched with a receiver-specific
  `--remote-debugging-port`

The sender does not infer these from its own app. It resolves the receiver's
binding, dispatches the receiver's registered autobridge session, and sets the
receiver's CDP port for the UI refresh step.

Example cdx2 receiver:

```bash
CODEX_HOME=/Users/pixexid/.codex-app-account2 \
  /Applications/Codex.app/Contents/Resources/codex app-server \
  --listen ws://127.0.0.1:8765 \
  --ws-auth capability-token \
  --ws-token-file /path/to/cdx2-token

open -n -a /Applications/Codex.app --args \
  --user-data-dir="/Users/pixexid/Library/Application Support/Codex Account 2" \
  --remote-debugging-port=9224
```

Example Codex receiver:

```bash
CODEX_HOME=/Users/pixexid/.codex \
  /Applications/Codex.app/Contents/Resources/codex app-server \
  --listen ws://127.0.0.1:8767 \
  --ws-auth capability-token \
  --ws-token-file /path/to/codex-token

CODEX_HOME=/Users/pixexid/.codex open -n -a /Applications/Codex.app \
  "codex://threads/<codex-runtime-session-id>" \
  --args --remote-debugging-port=9223
```

Sender-side dispatch then uses the receiver's CDP port:

```bash
LLM_COLLAB_CODEX_MODEL=gpt-5.4 \
LLM_COLLAB_UI_REFRESH=1 \
LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp \
LLM_COLLAB_CODEX_CDP_PORT=9223 \
python3 bin/session_autobridge.py dispatch --session SESSION-CODEX-...
```

Live proof from this spike:

- Codex -> cdx2 used cdx2 runtime session
  `019dbb4c-ac68-7f10-8332-77ea314a137f`, app-server
  `ws://100.91.98.28:8765`, CDP `9224`, and rendered
  `CDX2_CDP_REPEAT_REFRESH_OK`.
- cdx2 -> Codex should not target the active orchestrator thread for reverse
  tests. Sending test prompts into the live operator thread contaminates that
  session's working context.
- The safe reverse-test shape is a dedicated disposable Codex thread created by
  first real input, then bound as the reverse-test target session.
- First real input can be created by the sender. That means the sender can open
  the receiver-side worker thread, seed it with the first real message, bind the
  resulting runtime session id, and immediately treat it as the paired receiver
  thread for future turns.
- For visible Codex CDP refresh, that disposable thread also needs to live under
  the same project surface the receiving Codex renderer is currently showing.
  In practice, a disposable thread created under `/Users/pixexid/Projects/amiga`
  refreshed correctly because its sidebar row existed in the visible Amiga
  thread list; a disposable thread created under another cwd did not provide a
  selectable sidebar row and therefore could not prove visible refresh.
- A successful reverse proof used disposable Codex runtime session
  `019dc2ba-f467-70b3-9748-499eeab8a55d`, app-server
  `ws://127.0.0.1:8767`, CDP `9223`, and rendered
  `CODEX_REVERSE_AMIGA_DISPOSABLE_REFRESH_OK`.
- These runtime session ids are example evidence only, not durable contract
  values.

## Wake Strategy Rules

### `runtime_trigger`

Use only when the parked session has an explicit bounded runtime adapter. For
Codex app sessions, that adapter may be the Codex App Server. For command-based
adapters, the dispatcher passes a JSON payload through stdin and
message/session metadata through environment variables, then waits for a single
exit code.

This is the only spike path that proves a parked non-interactive session can do one bounded action.

The runtime command is not the primary identity. It is only an adapter detail layered on top of:

- `agent_id`
- `session_id`
- `runtime.family`
- `runtime.session_id`

### `relay`

Use when the target is `human_relay` or when the requested wake path cannot be honored directly. The dispatcher writes a concrete relay artifact with the exact prompt the operator should paste into the target worker session.

This is the strongest safe fallback for parked human-relay sessions.

### `notify`

Record that the message matched the parked session but do not attempt to wake anything. This is the safe fallback for parked `cli_session` agents that have no session handle or runtime hook.

### `none`

Session is registered but should not be woken automatically.

## Loop Prevention

The spike keeps loop prevention intentionally narrow:

- a message is dispatched at most once per registered session
- dispatcher-generated messages can be tagged in frontmatter later with `autobridge_session_id` / `autobridge_hops`
- the current dispatcher already reserves those checks so recursive autobridge traffic can be ignored once message-writing exists
- if `target_session_id` is present, only the matching registered session may act on it
- if a sender shows up with the same `agent_id` but a different `sender_session_id`, the receiver can treat that as a distinct worker runtime instead of merging state mentally

## Paired-Thread Routing

The default conversation shape for visible app-to-app collaboration is a paired
thread, not a single mirrored thread.

- sender opens or targets a receiver thread on the other app
- receiver works in that receiver thread
- receiver replies back to the sender's originating thread
- sender continues by replying to the remembered receiver thread

Every routed message should carry both ids when they are known:

- `sender_session_id`: the live sender-side thread/session id
- `target_session_id`: the live receiver-side thread/session id

Delivery should resolve follow-up ids in this order:

1. explicit ids on the send command
2. remembered paired-thread state for the same project/chat/agent pair
3. canonical per-agent runtime binding as the fallback

When a sender later emits the same `agent_id` with a different
`sender_session_id`, the paired-thread state should update that side of the
conversation immediately. That lets receivers treat the new message as a new
worker session instead of silently attaching it to stale history.

## What The Spike Proves

The spike is meant to answer two questions separately.

1. Can a parked session be mapped to a bounded action target at all?
   Answer: yes, but only when there is an explicit runtime adapter.

2. Can parked interactive sessions be woken universally?
   Answer: no, not with the current `llm-collab` model. Existing `cli_session` and `human_relay` flows do not expose a generic runtime wake handle.

## Recommended Rollout

1. Keep this registry and dispatcher experimental.
2. Require workers to publish runtime session identity on bootstrap or first inbox read.
3. Use `runtime_trigger` only for explicit non-interactive adapters.
4. Keep runtime bindings scoped by project, chat, and agent before resolving follow-up targets.
5. Treat `human_relay` downgrade-to-relay as the supported fallback when no runtime binding exists.
6. Keep post-dispatch UI refresh best-effort and visible to the operator.
7. Do not add true auto-reply message generation until loop controls and audit rules are stronger.
8. If future runtimes expose a real session wake API, integrate it as another adapter instead of changing inbox semantics.
