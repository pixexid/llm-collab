# Claude Code Desktop Computer Use Bridge

This is the unattended bridge workflow for Claude Code in the Claude desktop
app. It is intentionally separate from `claude --resume`, `claude -p`, and
other Claude Code CLI flows.

## Goal

Let Codex and Claude Code desktop exchange bounded messages while the operator
is away, with `llm-collab` preserving the durable task/message record and Codex
driving the visible Claude desktop app through Computer Use.

Claude desktop does not wake itself from `llm-collab`. If a Claude action or
response is expected, Codex must use Computer Use to interact with the visible
Claude desktop prompt, exactly as the operator would.

## Transport Choice

For task-grade work, use this order:

1. Use `deliver.py` to write the instruction to `Chats/` and Claude's
   `agents/claude/inbox.json`.
2. Create or update a Claude desktop bridge plan in the active Codex thread.
3. Codex uses Computer Use against `/Applications/Claude.app`.
4. Codex opens/selects the intended Claude desktop thread, or creates a new one
   by generating a UUID plus short title, clicking `New session`, and sending
   the first prompt with the bridge title line.
5. Codex types exactly one short one-line bridge prompt into the visible Claude
   prompt. For task-grade work, full context stays in the durable
   `llm-collab` packet; the visible prompt only points Claude to the exact
   inbox/chat/message path.
6. A Codex heartbeat keeps waking this Codex thread while Claude is expected to
   answer.
7. On each heartbeat, Codex inspects the Claude app with Computer Use. If Claude
   is running, Codex observes only. If Claude is idle/awaiting input, Codex
   reads the visible response, records it in this Codex thread and optionally in
   `llm-collab`, then stops or schedules the next explicit turn.

For non-task chat with Claude, `llm-collab` is optional. Computer Use is still
mandatory whenever a Claude response is expected and the operator will not be
present to read it.

`llm-collab` is the transport and audit log, not the wake mechanism. It does not
notify the current Codex thread, and it does not make Claude desktop read its
inbox. The active wake mechanism is a Codex heartbeat attached to the current
Codex thread.

When `deliver.py --to claude --project amiga` reports
`desktop_bridge_required`, Codex should continue to Computer Use in the same
thread. This is not an operator relay request. Do not print or act on a manual
operator relay for Claude Desktop unless Computer Use has already failed or is
blocked.

Use shell commands for `llm-collab` filesystem checks and message recording.
Use Computer Use only for Claude desktop interaction.

Do not use `claude --resume`, `claude -p`, or `~/.claude/projects/...` as the
desktop bridge. Those are CLI/project-session surfaces and do not prove the
operator-visible Claude Code desktop thread changed.

## Health Diagnostics

If Computer Use cannot inspect Claude desktop, do not switch to CLI/session-file
bridging. Record the blocker and gather only coarse shell diagnostics:

```bash
python3 bin/claude_desktop_bridge_health.py --json
```

This helper reports whether Claude appears to be running, frontmost, visible,
holding power assertions, and whether the main Claude process appears busy by
CPU. It deliberately does not read Claude message content, prompt state, thread
titles, URLs, or local session stores. A healthy shell report is not proof that
the bridge is usable; the bridge is usable only after `get_app_state` succeeds
through Computer Use and the visible target thread/prompt can be inspected.

Manual operator wake is the fallback path only. If Computer Use is unavailable,
cannot inspect Claude, cannot pass the idle input gate, or a send attempt fails,
report the exact blocker to the operator and provide the same one-line bridge
prompt. Do not fall back to Claude CLI, Electron store writes, or
`~/.claude/projects`.

For unattended heartbeats, run this helper before calling Computer Use. If it
reports busy Claude CPU, treat Claude as mid-turn and skip Computer Use for that
wakeup; report the coarse active status instead. The local-agent process count
is context, not a blocker by itself, because idle Claude desktop sessions can
leave helper processes resident. When the helper says Claude is visible/frontmost
but not busy, Computer Use should be tried next. If Computer Use still times out,
treat the lane as blocked on Computer Use accessibility/capture. Do not
interrupt or restart Claude from a heartbeat. Keep the heartbeat active only
when there is a concrete pending Claude relay; otherwise stop it and record the
blocker.

CPU-busy Claude is not proof that the target lane is progressing. For
task-grade work, pair the health signal with durable lane evidence before
describing progress:

- Codex inbox handoff or chat note from Claude
- task mirror status/activity update
- assigned worktree dirty status or checkpoint commit after activation

`status: in_progress` by itself is not durable progress. It only proves the lane
was activated. Treat it as progress only when it is paired with a new handoff,
blocked/review/done state, worktree dirt, or a checkpoint that did not exist on
the previous wakeup.

If the activation packet remains unread, Codex has no handoff, and the assigned
worktree is unchanged, report "no durable lane progress yet" even if Claude
desktop is CPU-busy.

## Desktop Thread Facts

Computer Use can drive Claude desktop at the visible UI level. A live smoke test
proved this sequence:

- click `New session`
- type a bounded exact-token prompt
- press/send the prompt
- observe a new sidebar row titled `Bridge lifecycle smoke test`
- observe the visible URL change to
  `claude.ai/epitaxy/local_ff7039ba-10da-450a-943c-99c73744cb72`
- read the exact response `CLAUDE_DESKTOP_THREAD_CREATED_OK`
- wait until the sidebar state settles to `Idle` and the prompt is available

A Claude desktop thread does not become a useful bridge target until the first
prompt has been sent and the resulting visible thread title/local URL have been
observed. The `local_*` id is a desktop UI target only. Do not convert it to a
CLI session id.

After a send, Claude may show transitional states such as `Creating worktree...`.
The bridge must treat `Running`, a visible `Stop` button, or any setup/progress
text as "do not send another prompt".

## Heartbeat Prompt

Only Codex needs a heartbeat. Claude desktop does not need a separate heartbeat:
Claude runs after prompt input, then waits for the next visible prompt.

Use a Codex thread heartbeat rather than a PM2 process for this bridge, because
PM2 cannot call Computer Use tools.

Heartbeat instructions:

```text
Use shell only for llm-collab inbox checks and relay recording. Use Computer Use
only for Claude desktop interaction. Do not use Claude Code CLI, `claude -p`,
`claude --resume`, or `~/.claude/projects` as the bridge.

Peek inboxes first:
- Set `<llm-collab-checkout>` to the active llm-collab checkout path.
- Set `<project-id>` to the collaboration project id for this task.
- cd <llm-collab-checkout>
- python3 bin/inbox.py --me codex --project <project-id> --peek --limit 5
- python3 bin/inbox.py --me claude --project <project-id> --peek --limit 5

Run `python3 bin/claude_desktop_bridge_health.py --json`. If it reports busy
Claude CPU, report the coarse active status and wait for the next heartbeat
without calling Computer Use. Otherwise inspect `/Applications/Claude.app` with
Computer Use. If Claude visibly shows `Stop`, is creating a worktree, or is
otherwise mid-turn, report the visible status briefly and wait for the next
heartbeat. If Claude is idle/awaiting input and there is a prepared outbound
prompt in this Codex thread, type only that prompt into the visible Claude
desktop prompt and send it. On later heartbeats, read the latest visible Claude
response from the accessibility tree and relay the actionable result back into
this Codex thread and, when relevant, into llm-collab with deliver.py or a chat
note.

If Claude is stopped, errored, or the prompt field is unavailable, report that in
this Codex thread and do not use the CLI fallback.
```

There is no always-on active heartbeat by default. Create a Codex heartbeat only
when Claude has been prompted or will be prompted and a response is expected.
Delete it as soon as the planned response is recorded, blocked, or no longer
needed.

## Cadence Policy

Set the heartbeat cadence per relay, not once forever:

- Short response expected: use a tight minute-level heartbeat.
- Long implementation or verification task: use a slower heartbeat and include
  what state counts as a useful update.
- Waiting only for final handoff: use a moderate heartbeat and notify only when
  Claude is awaiting input with a new response, blocked, or errored.
- Bridge no longer needed: delete the heartbeat immediately.

For each outbound Claude desktop message, the sender should also set or update
the heartbeat with the expected response window and the specific thread/action
context. Otherwise the watcher can only report that Claude is idle or awaiting
input; it cannot infer the next safe prompt.

The heartbeat only sends to Claude when this Codex thread contains an explicit
outbound directive. This prevents a watcher from accidentally starting the next
Amiga queue lane or interrupting a running Claude turn.

For ordered design queues, the heartbeat should not stay permanently hardcoded
to one issue. At the start of each wakeup, compute the current ready lane:

```bash
python3 bin/project_design_queue.py ready-context --project <project-id> --json
```

For the full heartbeat decision, prefer the consolidated status command:

```bash
python3 bin/project_design_queue.py bridge-status --project <project-id> --json
```

This combines the current ready lane, activation packet read/unread state, Codex
handoff inbox state, task mirror status, assigned worktree dirty/head state, and
coarse Claude Desktop health. Use its `classification` first:

- `durable-progress-visible`: verify handoff/task/worktree evidence before touching Claude desktop
- `cpu-busy-no-durable-progress`: report coarse active status plus "no durable lane progress yet"; do not call Computer Use on that wakeup, and also report any active `computer_use_blocker`
- `idle-no-durable-progress`: try Computer Use once; if `get_app_state` times out, record a Computer Use capture/accessibility blocker
- `computer-use-cooldown-no-durable-progress`: a recent idle-timeout blocker is still cooling down; report the blocker and do not call Computer Use
- `missing-bridge-metadata`: repair metadata before touching Claude desktop
- `queue-empty` or `no-ready-lane`: stop the relay and report queue state

After an `idle-no-durable-progress` Computer Use timeout, record the blocker:

```bash
python3 bin/project_design_queue.py record-computer-use-timeout --project <project-id> --reason "Computer Use get_app_state timed out after 120s"
```

This writes runtime-local state under the project state root and makes
`bridge-status` report a cooldown classification for the same task until the
retry window expires. Continue checking for durable progress during cooldown.
Repeated timeouts increase the cooldown from 30 minutes up to a 2-hour cap, so
an unattended loop keeps watching durable evidence without repeatedly spending a
full Computer Use timeout on the same blocked desktop state.
When `computer_use_blocker.active` is true, prefer its
`recommended_next_check_minutes` value for the next heartbeat cadence instead of
continuing a short fixed interval.

Use the returned `task_id`, `worktree`, `branch`, `bridge_thread_uuid`,
`bridge_visible_prefix`, `claude_desktop_thread_title`, and
`claude_activation_message_path` as the live target. If
`bridge_metadata_complete` is false, stop before touching Claude desktop and
record the missing bridge metadata. If only bridge naming fields are missing,
prepare them with:

```bash
python3 bin/project_design_queue.py ensure-bridge-metadata --project <project-id> --all-active
```

This only writes UUID/title/sidebar-prefix metadata to task mirrors. It does
not create worktrees, send `llm-collab` messages, or activate queued lanes. A
lane is not relay-ready until `ready-context` reports no missing metadata,
including worktree, branch, and activation message path. After a lane is
accepted and `claim_task.py` advances the queue, run `ready-context` again and
update the heartbeat target for the newly ready lane.

When the ready lane is bridge-complete, generate the exact bounded prompt to
type into Claude desktop with:

```bash
python3 bin/project_design_queue.py desktop-prompt --project <project-id>
```

The heartbeat may type only this generated prompt, or an explicit operator
directive, into the visible Claude prompt. Do not reconstruct the prompt by hand
from stale heartbeat text.

## Operating Plan

The bridge is useful only when an outbound message creates an expectation that
Claude will answer. Every Claude desktop relay needs a small plan packet before
the heartbeat is created or updated.

Plan packet fields:

- `bridge_goal`: why Claude is being contacted.
- `transport`: `llm-collab` for task-grade work, `direct-ui` for ad hoc chat.
- `collab_message_path`: exact `Chats/...` path when `llm-collab` is used.
- `target_thread`: continue current Claude desktop thread or create a new one.
- `bridge_thread_uuid`: UUID generated before creating a new Claude desktop
  thread.
- `short_thread_title`: short human title that should appear in the Claude
  sidebar.
- `expected_response`: exact signal that means Claude answered usefully.
- `expected_window`: short, medium, or long wait.
- `heartbeat_cadence`: the concrete poll interval for this relay.
- `timeout_action`: what Codex should do if Claude stays busy or silent.
- `recording_target`: where to persist the answer, if not only this Codex
  thread.
- `stop_condition`: when to delete the heartbeat.

Default cadence mapping:

- `short`: 1-2 minutes for quick review, ack, or exact-token checks.
- `medium`: 5-10 minutes for investigation or small patch work.
- `long`: 15-30 minutes for implementation, verification, or browser work.

Task-grade outbound lifecycle:

1. Send the durable instruction with `deliver.py`.
2. Create or update the bridge plan in the Codex thread.
3. Prepare an explicit Claude desktop prompt. Use a short one-line "check this
   exact inbox/chat/message" prompt. Full task context belongs in the durable
   `llm-collab` packet, not in Claude Desktop visible prompt text.
4. Use Computer Use to create/select the Claude desktop thread and send exactly
   one one-line wake prompt after the idle input gate passes.
5. Create or update the Codex heartbeat with the expected response window.
6. Heartbeat watches the visible Claude desktop state.
7. Once Claude is idle/awaiting input, Codex reads the visible response and
   records it in this thread and in `llm-collab` when required.
8. Delete the heartbeat unless a specific follow-up prompt is already planned.

No active plan means no send. In that state a heartbeat may report app/inbox
status, but it must not infer what Claude should do next.

## New Thread Naming

Every new Claude desktop thread must start with a unique bridge id and a short
title. This prevents the sidebar from filling with indistinguishable titles such
as `Check inbox messages` or `Check and review inbox messages`.

Before clicking `New session`, generate:

- `bridge_thread_uuid`: a full UUID for durable binding and audit
- `short_thread_title`: a compact label for the Claude sidebar, for example
  `GH-361 review workflow`

Send the first visible prompt in this format, only after the
Claude idle input gate passes:

- active sidebar row is not `Running`
- composer is empty and focused
- no visible `Stop` button
- no visible queued messages with `Remove queued message`

If any item fails, do not type into Claude Desktop. Wait for the next heartbeat
or record a blocker. Adding a prompt while Claude is running can fragment the
message into queued chunks and confuse the active turn.

Use this compact one-line prompt format:

```text
[BRIDGE <8-char-uuid-prefix>] Read <collab_message_path or latest CHAT-id packet> and execute it.
```

Keep the visible prompt under roughly 240 characters. Use the same
`bridge_thread_uuid` in the bridge plan and any later `llm-collab` note. The
full UUID, chat id, message path, task body, acceptance criteria, and gates must
live in the durable `llm-collab` packet or bridge record, not in Claude Desktop
visible prompt text.

Do not paste task bodies, acceptance criteria, long implementation briefs, or
multi-paragraph context into Claude Desktop for task-grade work. Put that
content in the durable `llm-collab` `Chats/` packet and wake Claude with only
the bridge pointer.

Never send task-grade Claude Desktop prompts with newlines or split one bridge
across multiple visible messages. If the app shows multiple queued Codex
fragments or queued remove affordances after sending, remove the queued
fragments if safe, record the bridge as failed, and do not stack replacement
prompts.

## Completion Detection

Do not treat a visible answer alone as complete. A Claude turn is settled only
when all of these are true:

- the visible response needed by the plan is present
- the sidebar row is no longer `Running`
- the prompt is available
- the Send button is disabled only because the prompt is empty
- there is no visible `Stop` control for the active turn

Do not require Claude to end with bridge protocol tokens. Claude should write
the normal task result, blocker, question, or handoff content needed for the
work. Codex decides the heartbeat state by reading that visible response plus
the settled UI state, then recording the meaningful content into `llm-collab`
when needed.

## Thread Selection

Codex side:

- Continue the current Codex thread when the operator wants the bridge watcher to
  stay active and report Claude state here.
- Stay in the current Codex thread after an Amiga issue is merged/cleaned up by
  default. Start a fresh Codex thread only when the operator explicitly asks for
  a fresh session/handoff, context safety requires a boundary, or Codex cannot
  safely continue in the current thread.
- Before starting a new lane or ending the current thread, preserve any workflow
  docs, repo instructions, skills, queue scripts, bridge docs, or agent-memory
  edits by committing/PR'ing them, explicitly bundling them into the next lane,
  or abandoning them with operator approval.

Claude side:

- Continue the current Claude desktop thread only when the visible project/title
  match the active task, or when the outbound directive explicitly says to
  continue it.
- Create a new Claude desktop thread only by using Computer Use: click
  `New session`, select/confirm the intended project/worktree controls, type the
  first prompt with `[BRIDGE <8-char-uuid-prefix>] <short_thread_title>`, and
  send it. Then read back the visible project, title, and `local_*` URL before
  binding the thread to a plan.
- If the visible Claude thread is unrelated, busy, or ambiguous, the heartbeat
  reports the blocker in Codex and does not send anything.

Do not create a Claude desktop thread by writing Electron stores, IndexedDB,
`~/.claude/projects`, or CLI/project-session files.

## Operator Safety

- Do not paste secrets, credentials, persona passwords, or private browser data
  into Claude desktop.
- Do not start unrelated Amiga queue lanes from the desktop bridge.
- If a prompt is a product implementation instruction, it must still name the
  issue/task, worktree, allowed files, and verification expectations.
- Keep every relay visible in the Codex thread; no hidden desktop-only state.
- Use one active Claude desktop controller heartbeat at a time unless a future
  implementation adds explicit per-thread UI locking. Multiple plans may exist,
  but only one heartbeat should drive the visible Claude app.

## Failure Modes

- If Claude desktop is idle/awaiting input, Codex can safely read the last answer
  and decide whether an explicit next prompt exists.
- If Claude desktop is still generating, shows `Stop`, or is creating a
  worktree, Codex should not interrupt it.
- If Computer Use cannot see the prompt or transcript, the bridge is paused; do
  not fall back to Claude CLI. For idle-time Computer Use timeouts, record the
  blocker with `project_design_queue.py record-computer-use-timeout` so later
  heartbeats apply the cooldown classification while still checking durable
  progress.
- If the app-visible thread changes, re-read the app state and confirm the
  project/title before sending anything.
- If a heartbeat times out, record `timed_out`, delete the heartbeat, and leave
  the `llm-collab` message unread or visibly unresolved rather than inventing a
  response.
