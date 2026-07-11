# llm-collab

`llm-collab` is a file-based, multi-agent coordination runtime for work across
multiple projects and repositories.

The core mailbox and task system uses local files—no application server or
database is required. GitHub, PM2, macOS Accessibility doorbells, and desktop
notifications are optional adapters for teams that need them.

## Current state

- **Project-scoped core** — new chats, messages, and tasks require a registered
  `project_id`; delivery rejects chat/project mismatches; queues and task
  contracts require exact project matches.
- **Durable mailbox** — `Chats/` is the transport of record, while per-agent
  inboxes store lightweight pointers to unread and read messages.
- **Ordered execution queues** — GitHub issues can act as the backlog source of
  truth, with project-local `issue-queue.json` files serving as the runtime
  execution cache. The separate design queue is a legacy migration surface.
- **Task activation gates** — non-trivial tasks require implementation-risk
  analysis and Claude refinement before they can move to `in_progress`.
  Claude-authored and Claude-planned tasks also require Codex acceptance.
- **Isolated implementation lanes** — worktree metadata, checkpoint commits,
  independent review, project preflight, and post-merge cleanup are supported
  as mechanical gates.
- **Explicit activation transports** — runtime sessions, AX-capable desktop
  apps, project-configured Claude Desktop fallback, and human relay are distinct
  activation paths. Missing transports report `activation_unavailable`.
- **Local project state** — real queues, runbooks, routing policy, and memory
  templates live under `{project_state_root}/{project_id}/`, normally outside
  this public Git checkout.
- **Separate scheduled-work surfaces** — Codex app automations remain app-owned.
  The proposed Thread Event Runner is a separate, currently unimplemented local
  subscription/ledger design; it does not own or mirror app automation state.

Project-scoped is the default; universal behavior is the exception. Read
[Multi-Project Support](docs/multi-project.md#scoping-principles) before adding
a project or changing shared tooling.

## Agent entrypoint

Automated workers must read [`AGENTS.md`](AGENTS.md) before operating a project
lane or changing this shared runtime. It defines mandatory project boundaries,
the new-project setup gate, shared-checkout safety, and verification rules.

Each product repository should also provide its own `AGENTS.md` or equivalent
worker instructions that bind collaboration commands to the exact checkout and
`--project <id>`.

## Repository and runtime layout

Tracked template and tooling:

```text
llm-collab/
├── AGENTS.md                 shared worker contract
├── README.md
├── bin/                      collaboration commands and launcher
├── scripts/                  initialization and migration utilities
├── docs/                     schemas, adapters, and workflow runbooks
├── tests/                    Python unit and integration tests
├── examples/                 public configuration examples
├── projects/_example/        public project-state template
├── pm2/                      optional watcher configuration
└── .githooks/                local commit safety guard
```

Workspace-local data, normally gitignored:

```text
llm-collab/
├── collab.config.json        workspace paths and runtime settings
├── agents.json               collaborator identities and activation config
├── projects.json             registered project contracts
├── agents/{agent_id}/        identity, memory, and inbox pointers
├── Chats/                    durable message threads
├── Tasks/                    active, backlog, and completed task mirrors
├── State/                    runtime bindings and watcher state
├── Index/                    generated indexes and reports
└── Logs/                     local process logs
```

Project-local operational state should live outside the checkout:

```text
~/.local/share/llm-collab/projects/
└── {project_id}/
    ├── issue-queue.json
    ├── issue-queue.md
    ├── roles-and-routing.md
    ├── runbooks/
    └── memory-templates/
```

## Requirements

- Python 3.10+
- Git for repository and worktree operations
- GitHub CLI (`gh`) only for GitHub-backed projects
- PM2 only for background inbox watchers
- macOS Accessibility permission only for AX doorbells

Use `bin/llm-collab` for collaboration commands. It selects a compatible Python
runtime even when the system `python3` is older. Use Python 3.10 or newer
directly for initialization and test discovery.

## Quickstart

### 1. Clone and initialize a new workspace

```bash
git clone https://github.com/pixexid/llm-collab ~/Projects/llm-collab
cd ~/Projects/llm-collab
python3.11 scripts/init.py
```

Initialization creates local `collab.config.json`, `agents.json`,
`projects.json`, and per-agent identity, memory, and inbox files. It asks for:

- the projects root and external project-state root;
- collaborator IDs, roles, and activation types;
- project repositories, preflight commands, and optional GitHub integration.

After initialization, add optional project contracts such as
`ui_ux.required_design_docs`, `db.shared_supabase_project_ref`,
`db.required_surfaces`, and `claude_desktop_bridge` directly to that project's
`projects.json` entry.

`scripts/init.py` reinitializes the workspace. To add a project to an existing
workspace, edit `projects.json` instead of rerunning initialization.

### 2. Bootstrap each agent

```bash
bin/llm-collab session_bootstrap.py --agent orchestrator
bin/llm-collab inbox.py --me orchestrator --project my-app --limit 5 --peek
```

Bootstrap prints the agent identity, unread inbox summary, queue recovery state,
and watcher status. Use `--peek` when inspecting messages without marking them
read.

### 3. Create a project-scoped chat and task

```bash
bin/llm-collab new_chat.py \
  --title "Implement checkout flow" \
  --project my-app

bin/llm-collab new_task.py \
  --title "Implement checkout API" \
  --created-by orchestrator \
  --owner worker \
  --project my-app \
  --repo-targets app \
  --path-targets src/routes/checkout.py
```

For a non-trivial task, complete its `## Implementation Risk Analysis`, then
run the planning/refinement and acceptance workflow described in
[Task Intake and Delegation](docs/workflows/task-intake-and-delegation.md)
before moving it to `in_progress`.

### 4. Deliver the durable task packet

```bash
bin/llm-collab deliver.py \
  --chat last \
  --from orchestrator \
  --to worker \
  --project my-app \
  --related-task TASK-... \
  --title "Implement checkout API" \
  --body-file brief.md
```

`deliver.py` writes the packet before reporting how the recipient can be
activated. The mailbox packet is the source of truth; a doorbell is only a wake
signal.

## Project boundaries

| Scope | Source | Examples |
|---|---|---|
| Universal | `agents.json` and shared workflow contracts | collaborator identity, activation type, task lifecycle, mailbox mechanics |
| Project registry | `projects.json` | repositories, GitHub repo, preflight, design docs, DB refs, tool surfaces |
| Project runtime | `{project_state_root}/{project_id}/` | queue, routing policy, runbooks, project memory templates |
| Task-specific | task frontmatter and body | owner, worktree, touched paths, evidence, explicit contract overrides |

The core enforces these boundaries:

- `new_chat.py`, `new_task.py`, and `deliver.py` require a registered project;
- a message cannot be delivered into a chat owned by another project;
- task-contract validation rejects missing or unknown projects;
- queue reconciliation and validation use exact task/project matches;
- non-Amiga projects do not inherit Amiga design docs, database refs, or MCP
  surfaces;
- AX routing uses explicit `activation.ax_app`, not an agent display name.

Amiga compatibility remains explicitly gated to `project_id == "amiga"`; it is
not a workspace default.

### GitHub adapter isolation

GitHub mirror and report commands always require exact task/project matches.
The older `--strict-project` option remains accepted as a deprecated no-op so
existing automation keeps working:

```bash
bin/llm-collab check_github_task_mirrors.py \
  --project my-app

bin/llm-collab report_github_project_task_sync.py \
  --project my-app
```

The generated report defaults to
`{project_state_root}/my-app/github-project-task-sync.md`, so one project cannot
overwrite another project's report.

## Adding a project to an existing workspace

1. Add a unique project entry to `projects.json` with `id`, `display_name`,
   `repos`, `default_branch_base`, `preflight_command`, and `github`.
2. Add project-specific `ui_ux`, `db`, and `claude_desktop_bridge` values only
   when applicable. Never copy another project's paths, refs, or tool names.
3. Create `{project_state_root}/{project_id}/` and add local routing/runbook
   overrides there. Do not commit real project state under `projects/`.
4. Add product-repository worker instructions that use the exact checkout and
   `--project <id>`.
5. For a GitHub-backed project, materialize and validate the queue:

   ```bash
   bin/llm-collab project_issue_queue.py reconcile --project <id> --write
   bin/llm-collab project_issue_queue.py validate --project <id>
   ```

   Projects without GitHub integration can use the local task board without a
   GitHub-backed issue queue.
6. Create a representative task, sync its contract, and validate assignment:

   ```bash
   bin/llm-collab task_contract.py sync --task TASK-... --write
   bin/llm-collab task_contract.py validate \
     --task TASK-... \
     --stage assignment
   ```

7. Confirm that the task, generated guidance, queue, and runtime state contain
   no paths, database refs, tool surfaces, or policies from another project.

See [Multi-Project Support](docs/multi-project.md) for the complete project
schema and examples.

## Tasks, queues, and activation

### Task lifecycle and planning gate

Tasks move through `open → in_progress → blocked/review → done`.

- Non-trivial tasks require a completed implementation-risk analysis.
- `plan_task.py` and `refine_task.py` record Claude planning/refinement.
- `claim_task.py` blocks `in_progress` unless the task has
  `refined_by: claude` or an explicit trivial-task `skip_refinement: true`.
- A task both created and planned by Claude requires `accepted_by: codex`
  before activation.
- Queue order, project preflight, UI/UX evidence, and database evidence can add
  further transition gates.

### Queue model

For GitHub-backed projects:

- open eligible GitHub issues are the backlog source of truth;
- `{project_state_root}/{project_id}/issue-queue.json` is the runtime execution
  cache for order, ownership, dependencies, and lane type;
- `project_issue_queue.py reconcile` refreshes that projection;
- `project_issue_queue.py validate` checks task mirrors, ordering, dependencies,
  and GitHub backlog consistency;
- new design work uses design `lane_type` values in the issue queue;
- `design-queue.json` is retained only for legacy migration and bridge metadata.

### Activation model

| Activation | Behavior |
|---|---|
| Dispatchable runtime session | Message can be routed to the bound runtime session |
| `cli_session` with `activation.ax_app` | `deliver.py` prints an AX doorbell command |
| Terminal-only `cli_session` | Requires a dispatchable runtime session |
| Project-configured non-CLI Claude fallback | Reports `desktop_bridge_required` |
| `human_relay` | Prints a human handoff prompt |
| Missing transport | Reports `activation_unavailable` with a reason |

See [Session Startup](docs/workflows/session-startup.md) and the
[desktop-app doorbell workflow](docs/workflows/claude-code-desktop-computer-use-bridge.md)
for operational safety rules.

### Thread Event Runner versus app automations

The [Thread Event Runner RFC](docs/workflows/thread-event-runner-rfc.md) defines
a Phase 1 architecture and threat contract for durable local event
subscriptions. It does **not** add a daemon, database, PM2 process, adapter, or
exact-thread dispatch behavior in the current release.

Codex app automations remain owned by the Codex app, including their schedules,
thread behavior, lifecycle, UI, and storage. A future runner subscription would
be an independently managed local record for observing external state. Neither
surface may silently import, pause, cancel, deduplicate, or take ownership of
the other.

## Command reference

Prefix collaboration commands with `bin/llm-collab`:

| Command | Purpose |
|---|---|
| `session_bootstrap.py --agent <id>` | Recover identity, inbox, queue, and watcher state |
| `inbox.py --me <id> --project <project>` | Read project-filtered messages |
| `new_chat.py --title "..." --project <project>` | Create a project-owned chat |
| `new_task.py --title "..." --created-by <id> --project <project>` | Create a project-owned task |
| `deliver.py --chat <chat> --from <id> --to <id> --project <project> --title "..."` | Write a durable message and report activation requirements |
| `task_board.py --project <project>` | List project tasks |
| `plan_task.py --task <task>` / `refine_task.py --task <task>` | Record Claude planning/refinement |
| `claim_task.py --task <task> --owner <id> --status <status>` | Apply gated ownership/status transitions |
| `task_contract.py sync/validate ...` | Sync and validate UI/UX and database task contracts |
| `project_issue_queue.py reconcile/validate --project <project>` | Refresh or validate a GitHub-backed execution queue |
| `worktree_ctl.py create/list/preflight/...` | Manage isolated implementation worktrees |
| `pm2_watchers.py start/status/logs ...` | Manage optional inbox watchers |
| `autonomous_loop.py start/update/show/clear --project <project>` | Record persistent queue-runner state |
| `post_merge_cleanup.py --project <project> ...` | Audit or clean integrated worktrees and branches |
| `init_agent_memory.py --agent <id> --target <target>` | Generate collaboration-aware worker guidance |
| `check_github_task_mirrors.py --project <project>` | Audit exact-project GitHub issue/task mirrors |
| `report_github_project_task_sync.py --project <project>` | Write a project-local GitHub Project/task report |

Legacy aliases remain available for migration:

- `msg.py` → `deliver.py`
- `watcher_ctl.py` → `pm2_watchers.py`

## Git and local-state safety

Runtime configuration, live chats/tasks, agent memory, queues, logs, and secrets
are gitignored by default. Public examples live under `examples/` and
`projects/_example/`.

This repository includes `.githooks/pre-commit`, which blocks common secrets and
runtime workspace state. Enable it with:

```bash
git config core.hooksPath .githooks
```

Before switching or pulling in a persistent shared checkout, inspect:

```bash
git status --short --branch --untracked-files=all
```

Do not discard another lane's tracked changes or untracked local files.

## Verification

Run the full suite with Python 3.10 or newer:

```bash
python3.11 -m unittest discover -s tests
git diff --check
```

## Documentation map

- [Getting Started](docs/getting-started.md)
- [Multi-Project Support](docs/multi-project.md)
- [Schema Reference](docs/schema-reference.md)
- [Identity System](docs/identity-system.md)
- [Workflow index](docs/workflows/README.md)
- [Thread Event Runner RFC](docs/workflows/thread-event-runner-rfc.md)
- [GitHub adapter](docs/adapters/github.md)
- [PM2 adapter](docs/adapters/pm2.md)
- [Migration from Amiga](docs/migration/from-amiga.md)
- [ACP comparison](docs/acp-comparison.md)

## What this is not

- a hosted service or real-time chat system;
- a replacement for project-specific repository instructions;
- a universal store for product-specific paths, credentials, or policy;
- ACP/A2A compliance—the design is deliberately simpler.

## License

MIT
