# Session Autobridge RFC

## Status

Experimental spike. Not part of the default `llm-collab` workflow yet.

## Problem

`llm-collab` can already do three useful things:

- deliver messages into an agent inbox
- watch unread pointers in the background
- print a relay prompt when a recipient is `human_relay`

What it cannot do today is treat a parked worker session as a scoped runtime target. There is no session registry, no lease model, and no bounded wake hook that can safely say "this exact parked session should do one action now."

That leaves a gap for short back-and-forth loops where the operator currently has to relay every wake-up manually.

## Goals

- introduce a session-scoped source of truth instead of overloading `agents.json`
- let a parked session declare its mode and wake strategy
- prove whether one bounded non-interactive wake path is viable
- degrade cleanly when the target session cannot actually be auto-woken
- keep the scope narrow enough that the spike can be tested safely

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
- optional `project_id` and `chat_id` filters
- `mode`: `manual | notify | auto-read | auto-reply`
- `status`: `active | parked | stopping | stopped | superseded`
- `wake_strategy`: `none | notify | relay | runtime_trigger`
- lease metadata: owner and expiry
- optional bounded runtime command for `runtime_trigger`
- a per-session processed-message ledger so one inbox item does not fire twice

Dispatcher artifacts live under:

- `State/session_autobridge/events/<session-id>.jsonl`
- `State/session_autobridge/prompts/<session-id>/...`

## Wake Strategy Rules

### `runtime_trigger`

Use only when the parked session has an explicit bounded runtime command. The dispatcher passes a JSON payload through stdin and message/session metadata through environment variables, then waits for a single exit code.

This is the only spike path that proves a parked non-interactive session can do one bounded action.

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

## What The Spike Proves

The spike is meant to answer two questions separately.

1. Can a parked session be mapped to a bounded action target at all?
   Answer: yes, but only when there is an explicit runtime adapter.

2. Can parked interactive sessions be woken universally?
   Answer: no, not with the current `llm-collab` model. Existing `cli_session` and `human_relay` flows do not expose a generic runtime wake handle.

## Recommended Rollout

1. Keep this registry and dispatcher experimental.
2. Use `runtime_trigger` only for explicit non-interactive adapters.
3. Treat `human_relay` downgrade-to-relay as the supported fallback.
4. Do not add true auto-reply message generation until loop controls and audit rules are stronger.
5. If future runtimes expose a real session wake API, integrate it as another adapter instead of changing inbox semantics.
