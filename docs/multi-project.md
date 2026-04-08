# Multi-Project Support

A single `llm-collab` workspace can coordinate agent work across multiple code repositories and projects simultaneously. This document explains how project targeting works and how to use it effectively.

---

## The model

Every message and task carries a `project_id` that identifies which project the communication is about. This allows:

- Filtering inbox to a specific project: `inbox.py --project my-app`
- Filtering task board: `task_board.py --project my-api`
- Scoping worktrees to specific repos
- Associating GitHub issues with the right project

The workspace itself is project-agnostic — it coordinates work, not a specific codebase.

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
        "app": "../my-app",
        "api": "../my-app-api"
      },
      "default_branch_base": "main",
      "preflight_command": ["pnpm", "preflight", "--json"],
      "github": {
        "enabled": true,
        "repo": "owner/my-app",
        "project_number": 1
      }
    },
    {
      "id": "docs",
      "display_name": "Docs Site",
      "repos": {
        "site": "../docs-site"
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

Repo paths are relative to `projects_root` (from `collab.config.json`).

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

# Not project-specific (e.g. meta/planning discussions)
python bin/deliver.py \
  --chat last \
  --from orchestrator \
  --to researcher \
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
  --repo ../my-app

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
│   ├── collab.config.json
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

- `project_id` is optional on messages and tasks. A workspace can mix project-scoped and non-scoped communication.
- If you only have one project, you can leave `project_id` null on everything and the system works fine.
- Chat threads are not project-scoped at the thread level (one thread can have messages about multiple projects). Project filtering is done per-message via the frontmatter field.
