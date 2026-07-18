# Multi-Project Support

A single `llm-collab` workspace can coordinate agent work across multiple code repositories and projects simultaneously. This document explains how project targeting works and how to use it effectively.

---

## Scoping principles

`llm-collab` is a multi-project runtime. Project-scoped is the default;
universal is the exception.

- **Project-scoped:** messages, chats, tasks, queues, worktrees,
  design/product sources, verification commands, database contracts, GitHub
  repositories, runbooks, and runtime state under
  `{project_state_root}/{project_id}/`. Every message and task carries a
  `project_id`; every project-aware inbox, task, and queue operation passes
  `--project <id>`.
- **Universal only when project-independent by construction:** agent identities
  and activation types in `agents.json`, mailbox and doorbell mechanics, the
  seven-section executor packet, task lifecycle states, and the one-writer-per-
  lane rule.
- **No cross-project inheritance:** anything a contract injects or validates —
  including design docs, database refs, tool surfaces, and preflight commands —
  resolves from that project's `projects.json` entry or is stated explicitly
  at task level. Hardcoding one project's value in `bin/` is a defect.

### Onboarding a new project

1. Add a `projects.json` entry with `id`, `display_name`, `repos`,
   `default_branch_base`, `preflight_command`, and `github`. Add
   `ui_ux.required_design_docs`, `db.*`, or `claude_desktop_bridge` only when
   applicable.
2. Initialize `{project_state_root}/{project_id}/` through queue reconciliation,
   then add a project README that records the coordination chat, roles, and
   routing policy.
3. Add product-repository instructions such as `AGENTS.md`, worker-specific
   files, and a collaboration skill that bind agents to the exact checkout and
   `--project <id>`.
4. Run that project's inbox, task, and queue checks. Sync a representative task
   contract and confirm that no other project's defaults appear.

### Changing `llm-collab`

Workflow and tooling changes are first-class deliverables, not side effects of
product work:

- keep one writer for the change lane; if another writer is active in this
  checkout, yield and coordinate through the mailbox;
- keep project values out of `bin/`; use `projects.json` configuration, with an
  explicit legacy fallback only when backward compatibility requires it;
- update `docs/schema-reference.md` and focused tests with contract changes;
- run the full suite with Python 3.10 or newer:
  `python3.11 -m unittest discover -s tests`.

---

## The model

Every message and task carries a `project_id` that identifies which project the communication is about. This allows:

- Filtering inbox to a specific project: `inbox.py --project my-app`
- Filtering task board: `task_board.py --project my-api`
- Scoping worktrees to specific repos
- Associating GitHub issues with the right project

The workspace itself is project-agnostic — it coordinates work, not a specific codebase.

Project-specific policy should live under `{project_state_root}/{project_id}/` and override the universal defaults in `docs/workflows/`.

The public repository tracks only `projects/_example/`. Real project directories are runtime-local and should normally live outside the Git checkout via `project_state_root` in `collab.config.json`, so queue state, customer context, repository paths, worker routing, memory templates, and operational runbooks do not leak into the open-source repo or disappear during merges that delete tracked paths.

---

## Registering projects

Edit `projects.json` (or regenerate with `python scripts/init.py`):

```json
{
  "projects": [
    {
      "id": "my-app",
      "display_name": "My Application",
      "repos": {
        "app": "my-app",
        "api": "my-app-api"
      },
      "default_branch_base": "main",
      "preflight_command": ["pnpm", "preflight", "--json"],
      "claude_desktop_bridge": false,
      "ui_ux": {
        "required_design_docs": ["/absolute/path/to/my-app/DESIGN.md"]
      },
      "db": {
        "shared_supabase_project_ref": "project-ref",
        "required_surfaces": ["supabase_my_app.execute_sql", "supabase CLI"]
      },
      "github": {
        "enabled": true,
        "repo": "owner/my-app",
        "project_number": 1,
        "backlog": {
          "exclude_labels": ["type:epic", "wontfix", "duplicate", "invalid", "question", "status:deferred"],
          "require_any_label": []
        }
      }
    },
    {
      "id": "docs",
      "display_name": "Docs Site",
      "repos": {
        "site": "docs-site"
      },
      "default_branch_base": "main",
      "preflight_command": null,
      "github": {
        "enabled": false
      }
    }
  ]
}
```

Repo paths are relative to `projects_root` (from `collab.config.json`). Project runtime state, such as queues and local runbooks, is separate and resolves from `project_state_root`.

For UI/UX projects, set `ui_ux.required_design_docs` to the project's own
canonical design sources. The task-contract helper prepends those documents to
UI/UX tasks and removes the Amiga default from non-Amiga projects. Additional
task-specific design sources remain allowed.

Set `db.shared_supabase_project_ref` and `db.required_surfaces` only for projects
that use the shared-Supabase task contract. Non-Amiga projects never inherit
Amiga's project ref or MCP surface names; an unconfigured database lane must
provide both values explicitly at task level.

`claude_desktop_bridge` is an opt-in fallback for Claude targets that are not
configured as CLI sessions. A CLI-session worker uses the project-independent AX
doorbell only when its agent entry explicitly sets `activation.ax_app` AND
`ax_attended_only` is not `true` (an opaque-composer target instead reports
`ax_attended_recovery_required` and routes to Codex-attended recovery); otherwise
it needs a dispatchable runtime session.

### Project state root

Set `project_state_root` to a directory outside this repository:

```json
{
  "projects_root": "/Users/you/Projects",
  "project_state_root": "/Users/you/.local/share/llm-collab/projects"
}
```

Tools that read or write local project state use `{project_state_root}/{project_id}/`.
For example, `python bin/project_issue_queue.py show --project my-app` reads:

```text
/Users/you/.local/share/llm-collab/projects/my-app/issue-queue.json
```

Use the in-repo `projects/_example/` directory only as a template. Do not store
real project queues, customer notes, routing policy, or memory templates under
the public checkout unless you intentionally want Git branch switches and pulls
to manage those files.

---

## Sending project-scoped messages

```bash
# Scoped to a project
python bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to worker \
  --title "Implement checkout flow" \
  --project my-app \
  --repo-targets app,api \
  --path-targets "src/routes/checkout.ts,src/types/order.ts"

# Meta/planning work still attaches to its owning registered project
python bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to researcher \
  --project my-app \
  --title "Research caching strategies"
```

### Filtering inbox by project

```bash
# Only messages related to my-app
python bin/inbox.py --me worker --project my-app

# All projects
python bin/inbox.py --me worker
```

---

## Creating project-scoped tasks

```bash
python bin/new_task.py \
  --title "Fix authentication middleware" \
  --created-by orchestrator \
  --owner worker \
  --project my-app \
  --repo-targets app \
  --path-targets "src/middleware/auth.ts" \
  --priority high
```

### Filtering task board by project

```bash
python bin/task_board.py --project my-app
python bin/task_board.py --project docs
python bin/task_board.py  # all projects
```

---

## Creating worktrees for a specific project repo

When using git worktrees for isolation, reference the project's repo by path:

```bash
# Using the repo path directly
python bin/worktree_ctl.py create \
  --task TASK-ABC123 \
  --agent worker \
  --repo my-app

# Using absolute path
python bin/worktree_ctl.py create \
  --task TASK-ABC123 \
  --agent worker \
  --repo /Users/you/Projects/my-app
```

The worktree is created at `{repo}-worktrees/{agent}/{task-slug}/` by default.

---

## Project directory layout example

```
~/Projects/
├── _collab/                    ← this workspace
│   ├── collab.config.json     ← project_state_root points outside this tree
│   ├── projects.json
│   ├── agents/
│   ├── Chats/
│   │   ├── 2026-04-07_my-app-sprint-1__CHAT-xxx/
│   │   └── 2026-04-07_docs-redesign__CHAT-yyy/
│   └── Tasks/
│       ├── active/
│       │   ├── 2026-04-07_fix-auth__TASK-aaa.md        ← project_id: my-app
│       │   └── 2026-04-07_redesign-header__TASK-bbb.md ← project_id: docs
│       └── done/
│
├── my-app/                     ← project repo
├── my-app-api/                 ← project repo
├── my-app-worktrees/           ← created by worktree_ctl.py
│   └── worker/
│       └── t-aaa-fix-auth/
└── docs-site/                  ← project repo

~/.local/share/llm-collab/projects/
└── my-app/
    ├── issue-queue.json        ← canonical local runtime execution cache
    ├── issue-queue.md
    ├── design-queue.json       ← deprecated legacy design queue, migrate to issue-queue lane_type
    ├── design-queue.md
    ├── roles-and-routing.md
    ├── runbooks/
    └── memory-templates/
```

---

## Typical multi-project workflow

```bash
# Orchestrator morning standup: review all projects
python bin/task_board.py
python bin/inbox.py --me orchestrator

# Focus on my-app
python bin/task_board.py --project my-app --status in_progress
python bin/inbox.py --me orchestrator --project my-app

# Delegate a my-app task to worker
python bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to worker \
  --project my-app \
  --title "Fix the broken auth middleware"

# Delegate a docs task to researcher
python bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to researcher \
  --project docs \
  --title "Research headless CMS options"
```

---

## Notes

- `new_chat.py` and `deliver.py` require `--project`, and each chat has a single `project_id` in `meta.json`.
- New and active task files must carry `project_id`; project-aware queues,
  reports, and adapters exclude unscoped tasks. Use explicit migration tooling
  to backfill legacy data.
- For single-project setups, pass that project ID consistently instead of leaving fields null.
