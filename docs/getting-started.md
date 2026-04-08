# Getting Started

This guide walks you through setting up a fresh `llm-collab` workspace from scratch.

## Prerequisites

- Python 3.9+
- Git
- PM2 (optional): `npm install -g pm2`

## Step 1: Clone and position the workspace

The workspace should live **alongside** your project repos, not inside them.

```
~/Projects/
├── my-app/              ← your project
├── my-api/              ← your project
└── _collab/             ← this workspace (cloned here)
```

```bash
git clone https://github.com/your-org/llm-collab ~/Projects/_collab
cd ~/Projects/_collab
```

## Step 2: Run init

```bash
python scripts/init.py
```

You will be asked to define:

### Workspace settings

- **Workspace name** — used as PM2 app prefix and in memory snippets (e.g. `my-collab`)
- **Projects root** — the directory containing your project repos (e.g. `~/Projects`)
- **Poll interval** — seconds between inbox checks for background watchers (default: 15)
- **Notifications** — whether to send desktop notifications on new messages

### Agents

At minimum you need:
1. A **human** agent (type: `human`) — the operator dispatching work
2. At least one **LLM** agent

For each LLM agent, choose an activation type:

| Type | When to use |
|------|------------|
| `cli_session` | LLM CLI is always open in a terminal; background watcher runs |
| `human_relay` | Human must start a new LLM session and paste the handoff prompt |
| `api_trigger` | External webhook triggers the agent (advanced) |

**For multi-account same-model setups** (e.g. two Codex accounts):
- Name them distinctly: `codex` and `codex2`
- Set `codex2` as `human_relay` with `base_model: codex`
- Add a clear `identity_note`: `"You are Codex2 (codex2). Read only messages addressed to 'codex2'."`

### Projects

Register each code project. Provide:
- A short ID (e.g. `my-app`)
- Repo paths (relative to `projects_root`, e.g. `../my-app`)
- Optional preflight command (e.g. `pnpm preflight --json`)
- Optional GitHub integration

Init generates:
- `collab.config.json`
- `agents.json`
- `projects.json` (if projects added)
- `agents/{id}/identity.md` for each LLM agent
- `agents/{id}/memory.md` for each LLM agent
- `agents/{id}/inbox.json` for each LLM agent

Project-specific overrides can live under:

- `projects/{project_id}/roles-and-routing.md`
- `projects/{project_id}/runbooks/`
- `projects/{project_id}/memory-templates/`

## Step 3: Generate memory snippets

Make each LLM agent aware of the workspace by generating a memory snippet.

```bash
# Claude Code: writes directly to ~/.claude/projects/.../memory/
python bin/init_agent_memory.py --agent claude --target claude-code --write

# Codex: prints snippet to copy into Codex memory
python bin/init_agent_memory.py --agent codex --target codex

# Any LLM: universal markdown block
python bin/init_agent_memory.py --agent orchestrator --target generic

# Inject into a project's CLAUDE.md
python bin/init_agent_memory.py --agent claude --target claude-md \
  --project-path ~/Projects/my-app --write
```

## Step 4: Bootstrap agent sessions

At the start of **every LLM session**, run the bootstrap command:

```bash
python bin/session_bootstrap.py --agent <id>
```

This:
1. Prints your `identity.md` — the LLM immediately knows who it is
2. Shows unread inbox
3. Starts the background watcher (for `cli_session` agents)

For `human_relay` agents: you paste the bootstrap command into the new LLM session. The system generates this command automatically when someone sends them a message.

## Step 5: Start background watchers (optional)

```bash
# Start all watcher-enabled agents
python bin/pm2_watchers.py start --all

# Check status
python bin/pm2_watchers.py status --all

# View logs
python bin/pm2_watchers.py logs --agent orchestrator
```

Watchers poll `agents/{id}/inbox.json` every N seconds and send desktop notifications when new messages arrive.

## Step 6: Create your first chat and task

```bash
# Create a chat thread
python bin/new_chat.py --title "Sprint planning" --project my-app

# Create a task
python bin/new_task.py \
  --title "Implement user authentication" \
  --created-by orchestrator \
  --owner worker \
  --project my-app \
  --priority high

# Send a message
echo "Please implement the auth flow per the spec in /docs/auth.md" | \
python bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to worker \
  --title "Auth implementation task"
```

For `human_relay` recipients, `deliver.py` prints a one-time onboarding relay prompt (read docs + update memory file) the first time they receive work. Later relays are short “check inbox” prompts once awareness is tracked locally.

## Daily workflow

### For an orchestrator agent

```bash
# Start of session
python bin/session_bootstrap.py --agent orchestrator

# Review inbox
python bin/inbox.py --me orchestrator

# Check task board
python bin/task_board.py --project my-app

# Delegate work
python bin/deliver.py --chat last --from orchestrator --to worker --title "..."

# Update task status
python bin/claim_task.py --task TASK-xxx --owner orchestrator --status review
```

### For a worker agent

```bash
# Start of session
python bin/session_bootstrap.py --agent worker

# Read inbox (full content)
python bin/inbox.py --me worker

# Claim task
python bin/claim_task.py --task TASK-xxx --owner worker --status in_progress

# Create isolated worktree (optional)
python bin/worktree_ctl.py create --task TASK-xxx --agent worker --repo ../my-app

# ... do the work ...

# Mark done and report back
python bin/claim_task.py --task TASK-xxx --owner worker --status done
python bin/deliver.py --chat last --from worker --to orchestrator \
  --title "Auth implementation complete" \
  --related-task TASK-xxx
```

### For a human-relay agent

The human operator receives the handoff prompt automatically when a message is sent to this agent. They paste it into a new LLM session:

```
You are Worker (worker). Read only messages addressed to 'worker'.

Bootstrap your session by running:
  python /path/to/_collab/bin/session_bootstrap.py --agent worker

Then read your inbox and execute your latest task.
```

The LLM runs the bootstrap command and immediately sees its identity and unread messages.

## Troubleshooting

**"collab.config.json not found"**
Run `python scripts/init.py` from the workspace root.

**"Unknown agent: X"**
Check `agents.json` — the ID must match exactly (case-sensitive).

**Messages not appearing in inbox**
Ensure `deliver.py` completed without errors. Check `agents/{id}/inbox.json` directly.

**PM2 watchers not starting**
Install PM2: `npm install -g pm2`. Check logs: `python bin/pm2_watchers.py logs --agent <id>`.

**Identity file missing**
Re-run `python scripts/init.py` — it will skip existing config and only generate missing agent files.
