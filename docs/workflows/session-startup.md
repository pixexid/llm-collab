# Session Startup

## Goal

Start from a known-good environment before claiming or editing work.

## Bootstrap first

```bash
<runtime_root>/bin/llm-collab current_runtime.py --agent <agent_id>
```

`<runtime_root>` is the deployed runtime (normally
`~/.local/share/llm-collab/runtime/main`), not a parked or lane checkout. Source
checkouts may be dirty; they are never session or watcher launch roots.

The launcher fetches `origin/main`, verifies ancestry and the contract marker, then
invokes the repository-local bootstrap. Use `<runtime_root>/bin/llm-collab
current_runtime.py --check` to report the verified heads without starting a
session or watcher.

For an interactive collab worker, startup is not complete until the exact
native session watcher and its target/sibling probes pass. Follow
[`collab-thread-quickstart.md` → Bootstrap](collab-thread-quickstart.md#1-bootstrap).
Do not treat the agent-wide watcher reported by `session_bootstrap.py` as that
proof.

## Keep The Tooling Current

`llm-collab` is the shared coordination tool. Keep the parked operator checkout
out of the runtime path. Refresh the deployed runtime from a fresh isolated
source worktree:

Safe refresh flow:

```bash
<source_worktree>/bin/llm-collab deploy_runtime.py \
  --source <source_worktree>
```

The deploy command fetches and validates only the named source before resetting
the deployed runtime's tracked files. It leaves runtime-state symlinks and
source-checkout files untouched. If the source is stale or its contract differs
from `origin/main`, it refuses before changing the target.

Do not use these commands against a parked or dirty operator checkout:

- `git switch main`
- `git pull --ff-only origin main`

Untracked/gitignored files normally persist across branch switches. Git blocks
the switch instead of silently overwriting untracked files that conflict with
tracked files on the target branch. This is intentional: project-local secrets,
runtime state, worker memory templates, and operator/private config should stay
local in this open-source repo.

Real project runtime state should not depend on that Git behavior. Configure
`project_state_root` in `collab.config.json` to a directory outside the
`llm-collab` checkout, such as:

```json
{
  "project_state_root": "~/.local/share/llm-collab/projects"
}
```

Queues, project runbooks, roles/routing files, and memory templates then live at
`{project_state_root}/{project_id}/`. After any merge or branch switch, verify
the active queue from that external state root:

```bash
python bin/project_issue_queue.py show --project <project_id>
```

Do not copy real `projects/{project_id}` directories back into the public repo
as tracked files. The in-repo `projects/_example/` directory is only a template.

## Read before acting

1. collaboration inbox
2. active task board
3. project-level instructions (`{project_state_root}/<project_id>/...` when present locally)
4. repo-specific contributing/agent guidance

## Required preflight

Do not claim tasks or edit code until the active checkout is healthy.

Typical preflight checks:

- dependencies installed
- environment files present/readable
- project build/test command surface usable
- GitHub access usable (if this lane needs GitHub)
- browser/runtime validation path usable (if this lane needs it)

If any item fails: stop, fix environment, re-run checks.

## Session-autobridge validation rule

When validating worker wake/resume behavior, do not target the active operator thread.

Use a disposable worker session instead, especially for Codex app tests:

1. bind or refresh a disposable worker session
2. if testing failure retry, use a bounded adapter that is known to return
   nonzero before runtime acceptance on its first pass
3. send the routed message to the disposable target session
4. inspect watcher/inbox state

Current retry acceptance (not busy-queue protection):

- a known pre-acceptance runtime failure emits `autobridge_failed` and leaves
  the message in `unread`
- a later known-success watcher pass emits `autobridge_consumed` and moves the
  message to `read`

Session autobridge currently has no authoritative Codex busy/idle check, no
inbox `queued` field, and no `autobridge_deferred_busy` event. Do not use a
running operator thread to test retries or describe this behavior as safe busy
deferral. The planned transactional contract is in
`thread-event-runner-rfc.md`; exact-thread delivery remains disabled there
until busy and turn-acceptance/idempotency behavior is integration-proven.

For Codex manual watcher checks, `watch_inbox.py` should behave the same as the PM2 watcher by default:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

## Collab-loop monitor ownership

Claude owns ongoing collab-loop monitoring for PR/CI status, bot-review
comments, inbox replies, and doorbell handoffs. Codex should usually check live
state once while actively gating/reviewing, then hand any continuing watch to
Claude through the durable mailbox.

Use a Codex heartbeat only for a genuinely Codex-side wait or when Claude cannot
own the watch. Before creating one, clear any stale monitor for the same target.
Keep one monitor per purpose, and delete or update it as soon as the purpose is
served.

## Claude Desktop Rule

Claude has one wake path in every registration and project shape: the durable
`Chats/` packet, picked up by the Claude app's own background inbox watcher.
Neither `activation.ax_app` nor `claude_desktop_bridge` changes that — `deliver.py`
excludes Claude from the AX doorbell selector, and the Computer Use fallback that
`claude_desktop_bridge` used to select is removed.

Important distinction:

- Claude desktop app visible sidebar threads are backed by app-managed Electron state under:
  - `~/Library/Application Support/Claude/IndexedDB/...`
  - `~/Library/Application Support/Claude/Session Storage/...`
- Claude CLI/project sessions are backed by:
  - `~/.claude/projects/<project-slug>/<sessionId>.jsonl`
  - `~/.claude/projects/<project-slug>/sessions-index.json`

These stores are not interchangeable. A CLI-created project session may persist on disk without appearing in the desktop app sidebar.

Operational rule:

- do not claim that `llm-collab`, PM2, or Claude CLI can safely create a brand
  new Claude desktop app thread
- do not synthesize desktop-visible Claude threads by writing local app cache/index files
- use `Chats/` messages as the transport of record (the durable mailbox)
- when no matching dispatchable session autobridge exists and `deliver.py`
  reports `ax_doorbell_required: true`, the primary wake for Codex is the
  **bidirectional AX doorbell** (see
  `claude-code-desktop-computer-use-bridge.md`); terminal-only sessions require a
  dispatchable runtime binding
- every non-Codex worker with a background inbox watcher is woken by the durable
  packet and that watcher alone, never by AX. Preserve the packet and let the
  watcher own pickup. See
  `session-autobridge-runbook.md` for the full rule
- attended Computer Use is fallback/recovery when AX cannot safely target or
  verify the native composer; it is never the universal first path, and it is
  never a path to Claude — the project-configured non-CLI Claude Desktop bridge
  is removed, and `deliver.py` no longer reports `desktop_bridge_required`

Current Phase 1 routing gives a matching dispatchable session autobridge
precedence. When `deliver.py` reports `autobridge_ready: true`, it intentionally
suppresses both `ax_doorbell_required` and `desktop_bridge_required`; that packet
uses the separately documented session-autobridge path and its retry limitations.
Do not describe AX as primary for that packet. If the workflow requires AX as
the primary wake, avoid registering the matching dispatchable autobridge or
deactivate it before calling `deliver.py` (see
`session-autobridge-runbook.md#deactivate-a-session`), then verify the delivery
result actually reports `ax_doorbell_required: true`.

Safest task-grade workflow for desktop-app agents:

1. `llm-collab` delivers the task into `Chats/` with `deliver.py`
   - `autobridge_ready: true` takes precedence and means no AX doorbell was
     requested for this packet
   - for Codex, `ax_doorbell_required` means the sender runs exactly the command
     printed by `deliver.py`; it is
     not a manual operator relay request
   - `watcher_pickup_ready` means the durable packet is ready and the
     non-Codex recipient's watcher owns pickup
   - `desktop_bridge_required` is always false: the Claude Computer Use fallback
     it selected is removed, and Claude is woken by its own background watcher
   - `activation_unavailable` means the durable packet exists but neither a
     dispatchable runtime nor an explicit wake transport is configured
2. when `ax_doorbell_required` is true, the sender rings once with the printed AX
   command only after the native composer is provably empty. Once proven empty,
   a busy recipient may queue the one-line pointer behind its active turn. A
   non-empty draft or unreadable, unprovable, or `AXValue`-opaque composer state
   means hold and recovery—never infer empty. `VERIFIED` exit 0 confirms
   delivery. `QUEUED (UNCONFIRMED)` exit 0 does
   not prove the pointer entered the intended thread: preserve the mailbox
   packet, record the unconfirmed blocker/follow-up, never re-ring it, and do not
   claim exact-thread delivery without a later `axsend confirm` or explicit
   recipient evidence that the pointer appeared in the native thread. Inbox
   consumption proves durable packet delivery only, not AX-thread delivery. A
   running/processing state alone does not block the ring after composer-safety
   proof
3. the sender sends exactly one short sender-tagged wake prompt that points the
   recipient to the exact `llm-collab` inbox/chat/message path. Do not paste full
   task context, acceptance criteria, or multi-paragraph briefs into the app; the
   durable `Chats/` packet is the source of truth. The prompt must be one line,
   under roughly 240 characters, and never contain newline-split bridge details.
   The recipient drains its full unread inbox after the ring.
4. if AX resolves an embedded preview/web field or cannot deliver/confirm, stop
   sending but preserve the packet. In an attended Codex turn, use Computer Use
   plus `bin/axsend-ensure tree --app <app> --editable-only` to remove/blank the
   competing field, select the correct window, and clear probes. Resume routine
   AX only after verifying both the native composer identity and its provably
   empty state through readable `AXValue`. An unreadable, unprovable, or
   `AXValue`-opaque composer remains on attended recovery; use one idle-gated
   Computer Use send as the bounded fallback. Never turn one targeting incident
   into a standing AX-disabled rule.

A Claude packet that is not picked up is a watcher or binding defect: preserve
the durable packet, record the observed blocker, and report it for repair or
operator attention. Codex does not wake Claude through Computer Use, AX, or an
app restart — coarse Claude bridge health checks and recording an
accessibility/capture blocker are diagnosis, not a wake. Keep or create the heartbeat and record
`observed_state`, `expected_outcome`, `why_not_done`, and
`next_unlock_action`. Do not ask the operator to relay, paste, click, or
manually wake Claude, and do not reach for Computer Use to do it either: repair
the binding or the watcher, or keep monitoring and report the blocker.

For programmatic runtime targeting that does not require visible desktop state,
use a separate non-desktop adapter. For Claude desktop work, do not use
`claude -p`, `claude --resume`, or `~/.claude/projects` as the bridge.
