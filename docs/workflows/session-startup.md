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

Queue-protection acceptance for Codex:

- busy target session: message must stay in `unread` and appear in `queued`
- idle target session on the next watcher pass: message must drain from `queued` and move to `read`

For Codex manual watcher checks, `watch_inbox.py` should behave the same as the PM2 watcher by default:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

## Claude Desktop Rule

Treat the Claude desktop app as a human-driven UI surface that Codex may drive
only through Computer Use. Do not treat Claude desktop as a CLI/project-session
target.

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
- use Claude desktop as an operator-facing UI controlled through Computer Use
- use `Chats/` messages as the transport of record
- when any Claude action or response is expected while the operator is away,
  Codex must plan a Codex-side heartbeat that wakes Codex and inspects Claude
  desktop through Computer Use

Safest task-grade workflow for Claude desktop:

1. `llm-collab` delivers the task into `Chats/` with `deliver.py`
   - for Claude Desktop, `desktop_bridge_required` means Codex must continue to
     Computer Use; it is not a manual operator relay request
2. Codex uses Computer Use to open/select the Claude desktop thread, or creates a
   new visible thread by generating a UUID plus short title, clicking
   `New session`, and sending one one-line wake prompt
3. Codex sends a visible prompt only after the Claude idle input gate passes:
   active row not `Running`, empty focused composer, no visible `Stop`, and no
   visible queued messages with `Remove queued message`
4. Codex types exactly one short visible wake prompt that points Claude to the
   exact `llm-collab` inbox/chat/message path. Do not paste full task context,
   acceptance criteria, or multi-paragraph implementation briefs into Claude
   Desktop; the durable `Chats/` packet is the source of truth. The prompt must
   be one line, under roughly 240 characters, and never contain newline-split
   bridge details. If more detail is needed, update the durable chat packet and
   wake Claude with the same one-line pointer only after Claude is idle.
5. Codex creates a heartbeat only while a Claude response is expected
6. each heartbeat checks Claude desktop through Computer Use; if Claude is
   running, it waits; if Claude is idle/awaiting input, it reads and records the
   response
7. Codex deletes the heartbeat when the response is recorded, blocked, timed
   out, or no longer needed

After `desktop_bridge_required`, Codex owns the visible Claude Desktop wake
through Computer Use. If Computer Use is unavailable, blocked by a non-idle
Claude state, cannot inspect the app, or fails to send, Codex must try
reasonable app-control recovery paths first: bring Claude to front by bundle id,
use Computer Use app inspection/click/type, run coarse Claude bridge health
checks, wait/retry via heartbeat when Claude is busy, and record any
accessibility/capture blocker. Keep or create the heartbeat and record
`observed_state`, `expected_outcome`, `why_not_done`, and
`next_unlock_action`. Operator manual wake is a last-resort fallback only after
those Codex/Computer Use attempts fail or require an operator-only
permission/tooling decision; include the exact failed attempts and the one-line
bridge prompt.

For programmatic runtime targeting that does not require visible desktop state,
use a separate non-desktop adapter. For Claude desktop work, do not use
`claude -p`, `claude --resume`, or `~/.claude/projects` as the bridge.
