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

## Read before acting

1. collaboration inbox
2. active task board
3. project-level instructions (`projects/<project_id>/...` when present)
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

## Claude desktop rule

Treat the Claude desktop app as a human-driven UI surface, not a safe programmatic thread-creation target.

Important distinction:

- Claude desktop app visible sidebar threads are backed by app-managed Electron state under:
  - `~/Library/Application Support/Claude/IndexedDB/...`
  - `~/Library/Application Support/Claude/Session Storage/...`
- Claude CLI/project sessions are backed by:
  - `~/.claude/projects/<project-slug>/<sessionId>.jsonl`
  - `~/.claude/projects/<project-slug>/sessions-index.json`

These stores are not interchangeable. A CLI-created project session may persist on disk without appearing in the desktop app sidebar.

Operational rule:

- do not claim that `llm-collab` can safely create a brand new Claude desktop app thread
- do not synthesize desktop-visible Claude threads by writing local app cache/index files
- use Claude desktop as an operator-facing UI
- use `Chats/` messages as the transport of record

Safest workflow for Claude desktop today:

1. operator or Claude opens an existing visible desktop thread manually
2. `llm-collab` delivers the task into `Chats/`
3. Claude reads/processes from that known thread or from the collab inbox
4. if a concrete visible thread must be targeted, bind only an already-existing thread/session id

For programmatic runtime targeting, prefer Claude CLI/project sessions over fresh desktop-sidebar thread creation.
