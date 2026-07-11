# Session Startup

## Goal

Start from a known-good environment before claiming or editing work.

## Bootstrap first

```bash
cd <workspace_root>
python bin/session_bootstrap.py --agent <agent_id>
```

## Keep The Tooling Current

`llm-collab` is the shared coordination tool. Before using a persistent checkout
for inbox, task, queue, watcher, or delivery work, make sure the checkout is on
the latest `main`.

Safe refresh flow:

```bash
git fetch origin main
git status --short --branch --untracked-files=all
git switch main
git pull --ff-only origin main
git status --short --branch --untracked-files=all
```

If tracked or staged changes block `git switch main`, stop and classify them
before proceeding:

- merged/superseded feature-branch edits: discard only after confirming they are
  already on `origin/main`
- active task edits: keep working in that task branch or commit/stash before
  switching
- project-private or generated local files: keep them untracked; do not commit
  them just to make the checkout clean

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
2. if testing queue protection, make that disposable session busy first
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

Route Claude according to its registered activation type, explicit doorbell
capability, and active project configuration. A Claude agent registered as
`cli_session` uses AX only when `activation.ax_app` is configured. A project may
set `claude_desktop_bridge: true` as a fallback only when its Claude target is
not a CLI session.

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
- the primary agent-to-agent wake for AX-capable `cli_session` workers is the
  **bidirectional AX doorbell** (see
  `claude-code-desktop-computer-use-bridge.md`); terminal-only sessions require a
  dispatchable runtime binding
- screenshot/keyboard Computer Use is a fallback for a project-configured
  non-CLI Claude Desktop bridge, never the universal default

Safest task-grade workflow for desktop-app agents:

1. `llm-collab` delivers the task into `Chats/` with `deliver.py`
   - for an AX-capable `cli_session` worker, including Claude when configured,
     `ax_doorbell_required` means the
     sender rings the worker with the printed `axsend-ensure ring` command; it is
     not a manual operator relay request
   - `desktop_bridge_required` is the project-configured non-CLI Claude fallback
   - `activation_unavailable` means the durable packet exists but neither a
     dispatchable runtime nor an explicit wake transport is configured
2. the sender rings once with the printed AX command; a busy recipient may queue
   the one-line pointer behind its active turn
3. the sender sends exactly one short sender-tagged wake prompt that points the
   recipient to the exact `llm-collab` inbox/chat/message path. Do not paste full
   task context, acceptance criteria, or multi-paragraph briefs into the app; the
   durable `Chats/` packet is the source of truth. The prompt must be one line,
   under roughly 240 characters, and never contain newline-split bridge details.
   The recipient drains its full unread inbox after the ring.
4. if AX cannot deliver or confirm, record the exact blocker; use Computer Use
   only for an explicitly configured desktop-bridge fallback

Only after `desktop_bridge_required`, Codex owns the visible Claude Desktop wake
through Computer Use. If Computer Use is unavailable, blocked by a non-idle
Claude state, cannot inspect the app, or fails to send, Codex must keep
ownership of the Claude app path: bring Claude to front by bundle id, use
Computer Use app inspection/click/type, run coarse Claude bridge health checks,
wait/retry via heartbeat when Claude is busy, and record any
accessibility/capture blocker. Keep or create the heartbeat and record
`observed_state`, `expected_outcome`, `why_not_done`, and
`next_unlock_action`. Do not ask the operator to relay, paste, click, or
manually wake Claude; repair Computer Use/app access or continue monitoring the
Claude app until Codex can inspect and act safely.

For programmatic runtime targeting that does not require visible desktop state,
use a separate non-desktop adapter. For Claude desktop work, do not use
`claude -p`, `claude --resume`, or `~/.claude/projects` as the bridge.
