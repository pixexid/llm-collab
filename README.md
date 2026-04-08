# llm-collab

A file-based multi-agent collaboration workspace for LLMs, AI agents, and CLI tools.

Zero infrastructure. Git-native. Human-inspectable. Works offline.

---

## What is this?

`llm-collab` is a **template workspace** that lets multiple LLM instances (Claude Code, Codex, Gemini, custom agents) collaborate on shared projects through structured file-based messaging, task tracking, and identity management.

No servers. No databases. No external services required. Every message, task, and state change is a file you can read, diff, and commit.

## Key capabilities

- **Multi-agent messaging** — agents send/receive messages via `Chats/` threads with per-agent inbox pointers
- **Task tracking** — full lifecycle from `open → in_progress → blocked → review → done`
- **Identity isolation** — each agent has a dedicated `identity.md`, `memory.md`, and `inbox.json`; multi-account same-model agents (e.g. Codex + CDX2) are fully disambiguated
- **Human relay handoff** — when sending to a human-relay agent, the system auto-generates a ready-to-paste activation prompt for the operator
- **Multi-project support** — messages and tasks carry `project_id`; a single workspace coordinates across many repos
- **PM2 watchers** — optional background polling with desktop notifications
- **Git worktrees** — optional per-agent isolated branches for parallel implementation
- **Memory snippets** — auto-generated collab-awareness snippets for Claude Code, Codex, and any LLM

## Architecture in one diagram

```
llm-collab/
├── collab.config.json      workspace settings
├── agents.json             agent roster + activation config
├── projects.json           project registry (repos, preflight, github)
│
├── agents/
│   ├── {agent_id}/
│   │   ├── identity.md     WHO this agent is (first thing read at bootstrap)
│   │   ├── memory.md       persistent memory for this agent
│   │   └── inbox.json      pointer index of unread messages
│
├── Chats/                  canonical message threads (full content)
│   └── {date}_{title}__{CHAT-id}/
│       ├── meta.json
│       ├── overview.md
│       ├── {ts}_to-{agent}_{slug}.md
│       └── {ts}_from-{agent}_{slug}.md
│
├── Tasks/
│   ├── active/             open, in_progress, blocked, review
│   ├── backlog/            planned but not started
│   └── done/               completed
│
├── bin/                    CLI scripts
├── pm2/                    PM2 ecosystem config
├── scripts/                setup utilities
└── docs/                   documentation
```

## Quickstart

### 1. Clone the template

```bash
git clone https://github.com/pixexid/llm-collab ~/Projects/_collab
cd ~/Projects/_collab
```

### 2. Initialize your workspace

```bash
python3 scripts/init.py
```

The init script will ask you to:
- Name your workspace
- Set your projects root path
- Define your agents (identities, roles, activation types)
- Register your projects (repos, preflight commands, GitHub integration)

It then generates `collab.config.json`, `agents.json`, `projects.json`, and `agents/{id}/identity.md` + `agents/{id}/memory.md` for each agent.

### 3. Bootstrap each agent session

At the start of every LLM session, run:

```bash
python3 bin/session_bootstrap.py --agent <your_agent_id>
```

This outputs your `identity.md` first (so you know who you are), then shows your unread inbox.

### 4. Generate memory snippets for your LLM tools

```bash
# For Claude Code
python3 bin/init_agent_memory.py --agent claude --target claude-code --write

# For Codex
python3 bin/init_agent_memory.py --agent codex --target codex

# Universal (copy/paste into any LLM)
python3 bin/init_agent_memory.py --agent orchestrator --target generic
```

### 5. Create a chat and start messaging

```bash
# Create a project-scoped chat thread
python3 bin/new_chat.py --title "Implement checkout flow" --project my-app

# Send a message
python3 bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to worker \
  --project my-app \
  --title "Implement the checkout API endpoint" \
  --body-file brief.md

# Read inbox
python3 bin/inbox.py --me worker --project my-app
```

## Core concepts

### Identity isolation

Each agent has a dedicated directory under `agents/{id}/`:

| File | Purpose |
|------|---------|
| `identity.md` | Tells the LLM who it is; read first at every bootstrap |
| `memory.md` | Persistent memory owned by this agent |
| `inbox.json` | Pointer index to unread messages in `Chats/` |

This prevents the most common multi-agent failure mode: an agent reading messages meant for a different identity, or not knowing which identity to assume.

### Human relay agents

When you have two accounts of the same LLM (e.g. two Codex accounts), configure the second as `activation.type: "human_relay"`. When any agent sends a message to a human-relay agent, `deliver.py` automatically prints a ready-to-paste activation prompt:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠  Worker requires human relay.
   Share this prompt with the operator to activate them:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are Worker (worker). Read only messages addressed to 'worker'.

Bootstrap your session by running:
  python3 /path/to/_collab/bin/session_bootstrap.py --agent worker

Then read your inbox and execute your latest task.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Message routing

Messages live in `Chats/` (permanent record). Agent inboxes (`agents/{id}/inbox.json`) hold lightweight pointers — no content duplication, no symlinks. Reading the inbox loads content from `Chats/` on demand.

### Multi-project support

Every message and task carries a `project_id`. A single workspace can coordinate work across multiple repos:

```bash
python3 bin/inbox.py --me orchestrator --project my-app
python3 bin/task_board.py --project docs-site
```

## Command reference

| Command | Purpose |
|---------|---------|
| `session_bootstrap.py --agent <id>` | Initialize session, print identity, show inbox |
| `inbox.py --me <id>` | List unread messages |
| `deliver.py --from <id> --to <id> --chat last --title "..."` | Send a message |
| `new_chat.py --title "..." --project <id>` | Create a chat thread |
| `new_task.py --title "..." --created-by <id>` | Create a task |
| `claim_task.py --task TASK-xxx --owner <id> --status in_progress` | Claim/update a task |
| `task_board.py` | List all tasks |
| `reindex.py` | Regenerate `Index/index.md` |
| `check_github_task_mirrors.py --project <id>` | Detect GitHub issue/task mirror drift |
| `report_github_project_task_sync.py --project <id>` | Generate GitHub Project/task alignment report |
| `pm2_watchers.py start --all` | Start background inbox watchers |
| `worktree_ctl.py create --task TASK-xxx --agent <id> --repo ../my-app` | Create isolated git worktree |
| `init_agent_memory.py --agent <id> --target generic` | Generate LLM memory snippet |

Full reference: [docs/schema-reference.md](docs/schema-reference.md)

Note: when `claim_task.py` transitions a task to `in_progress` or `review`, it runs project preflight with browser checks skipped (`--browser-check skip`). Browser checks stay lane-gated for runtime/UI changes.

Legacy migration aliases:
- `msg.py` -> `deliver.py`
- `watcher_ctl.py` -> `pm2_watchers.py`

For `human_relay` recipients, `deliver.py` prints a first-time onboarding relay prompt (docs + memory update instructions) only once, then switches to short “check inbox” relay prompts after awareness is recorded in local runtime state.

### Activation-gated relay policy

Use relay prompts only when a worker should start immediately.

- Do not request relay for queued/not-ready workers.
- If multiple workers are queued, provide only the relay for the worker that should act now.
- For sequential lanes, wait for the trigger condition before requesting the next relay.
- For parallel-safe lanes, explicitly say: `activate <worker-a> + <worker-b> now in parallel`.

## What this is NOT

- Not a real-time chat system (async file-based)
- Not a hosted service (runs entirely on your machine)
- Not opinionated about which LLMs you use (any model, any CLI)
- Not ACP/A2A compliant (deliberately simpler; see [docs/acp-comparison.md](docs/acp-comparison.md) if curious)

## Requirements

- Python 3.9+
- Git (for worktree features)
- PM2 (optional, for background watchers): `npm install -g pm2`

## Local Safety Guards

This repo ships a local pre-commit hook at `.githooks/pre-commit` that blocks commits of runtime workspace state and common sensitive file patterns.

Enable it locally:

```bash
git config core.hooksPath .githooks
```

## License

MIT
