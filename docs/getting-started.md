# Getting Started

This guide walks you through setting up a fresh `llm-collab` workspace from scratch.

## Prerequisites

- Python 3.10+ (`bin/llm-collab` selects a compatible interpreter for
  collaboration commands; direct test discovery must also use Python 3.10+)
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
| `cli_session` | LLM CLI has a persistent session; optionally configure an AX app/bundle target for direct doorbells |
| `human_relay` | Human must start a new LLM session and paste the handoff prompt |
| `api_trigger` | External webhook triggers the agent (advanced) |

**For multi-account same-model setups** (e.g. two Codex accounts):
- Name them distinctly: `codex` and `codex2`
- Set `codex2` as `human_relay` with `base_model: codex`
- Add a clear `identity_note`: `"You are Codex2 (codex2). Read only messages addressed to 'codex2'."`
- For task ownership, start each new human-relay task in a fresh chat and
  runtime session. Reuse the existing session only for the same task's blocker,
  review-fix, or final handoff loop.

### Projects

Register each code project. Provide:
- A short ID (e.g. `my-app`)
- Repo paths (relative to `projects_root`, e.g. `my-app`)
- Optional preflight command (e.g. `pnpm preflight --json`)
- Optional GitHub integration
- An explicit release-gate agent selected from the enabled agents just defined

Init generates:
- `collab.config.json`
- `agents.json`
- `projects.json` (if projects added)
- `agents/{id}/identity.md` for each LLM agent
- `agents/{id}/memory.md` for each LLM agent
- a per-agent inbox pointer file for each LLM agent

Project-specific overrides can live under:

- `{project_state_root}/{project_id}/roles-and-routing.md`
- `{project_state_root}/{project_id}/runbooks/`
- `{project_state_root}/{project_id}/memory-templates/`

Set `project_state_root` in `collab.config.json` to keep real project state outside the Git checkout, for example `~/.local/share/llm-collab/projects`. Use `projects/_example/` as the public template; do not commit customer, company, repository, queue, task, worker, or operational state from a real project into this open-source repo.

`scripts/init.py` writes the selected agent as each new project's required
`release_gate_agent`; blank, unknown, or disabled selections are rejected.
Existing workspaces are not rewritten by a code upgrade: add the key manually
to each existing local `projects.json` entry, choosing a known enabled agent,
before attempting any new `review -> done` transition.

## Step 3: Point agents at the join skill

The primary agent entry path is the in-repo join skill:

- `skills/llm-collab-join/SKILL.md`

For skill-capable runtimes, generate a thin memory pointer to that skill plus
bootstrap. Keep generic markdown and project `CLAUDE.md` only as thin fallback
pointers for runtimes without installable skills.

```bash
# Claude Code: writes a thin pointer to ~/.claude/projects/.../memory/
python bin/init_agent_memory.py --agent claude --target claude-code --write

# Codex: prints a thin pointer to copy into Codex memory
python bin/init_agent_memory.py --agent codex --target codex

# Any LLM: universal markdown fallback pointer
python bin/init_agent_memory.py --agent orchestrator --target generic

# Inject a thin fallback pointer into a project's CLAUDE.md
python bin/init_agent_memory.py --agent claude --target claude-md \
  --project-path ~/Projects/my-app --write
```

## Step 4: Bootstrap agent sessions

At the start of **every LLM session**, run the bootstrap command:

```bash
~/.local/share/llm-collab/runtime/main/bin/llm-collab current_runtime.py --agent <id>
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

Watchers provide background wake behavior. Treat `skills/llm-collab-join/SKILL.md`
and the canonical workflow docs as the current authority for mailbox, wake, and
handoff behavior rather than copying inbox or deliver command families here.

## Step 6: Start your first collaboration lane

Use the initial project setup commands above, then move to the canonical
workflow docs for lane operation:

- `skills/llm-collab-join/SKILL.md`
- `docs/workflows/collab-thread-quickstart.md`
- `docs/workflows/task-intake-and-delegation.md`
- `docs/workflows/session-startup.md`

Those sources own the current send, inbox, task-claim, and human-relay
procedures. This getting-started guide does not duplicate them.

## Troubleshooting

**Task claim triggered browser checks unexpectedly**
`claim_task.py` now forces preflight `--browser-check skip` for `in_progress`/`review` transitions. Browser checks should run in preview/review gates for runtime/UI changes only.

**Worker says the branch/worktree does not exist**
For isolated implementation lanes, branch/worktree provisioning is orchestrator-owned. The worker should treat a missing lane as a blocker and report it immediately instead of inventing local lane state unless the task explicitly says self-provision is allowed.

**"collab.config.json not found"**
Run `python scripts/init.py` from the workspace root.

**"Queue file not found" after pulling latest main**
Check `project_state_root` in `collab.config.json`. Real queues should live at `{project_state_root}/{project_id}/issue-queue.json`, not under the tracked public checkout. If you previously kept `projects/{project_id}` inside the repo, copy it to the configured external state root and rerun `python bin/project_issue_queue.py show --project <project_id>`.

**"Unknown agent: X"**
Check `agents.json` — the ID must match exactly (case-sensitive).

**Messages not appearing in inbox**
Return to `skills/llm-collab-join/SKILL.md` and `docs/workflows/collab-thread-quickstart.md` for the current mailbox and wake path instead of inspecting runtime pointer files directly.

**PM2 watchers not starting**
Install PM2: `npm install -g pm2`. Check logs: `python bin/pm2_watchers.py logs --agent <id>`.

**Identity file missing**
Re-run `python scripts/init.py` — it will skip existing config and only generate missing agent files.

**Codex app shell cannot find pnpm**
Set a project local environment in Codex app so PATH includes your Node bin directory (for example `/opt/homebrew/bin` or `~/.local/node-v*/bin`).
