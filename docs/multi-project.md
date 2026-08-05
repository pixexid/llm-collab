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

### Review-policy enrollment

Each repository's incident-derived rules live in that repository's `AGENTS.md`.
This inventory records the enrollment owner and audit outcome; zero rules is valid.

| Repository | Owner | Incident-derived rules | Where they live |
|---|---|---|---|
| `llm-collab` | codex | 3 | root `AGENTS.md` → `## Code Review Rules` |
| `amiga` | codex | 2 — paid/provider idempotency and replace-lock authority; public Supabase RPC role execution | `amiga` `AGENTS.md` → `### Code Review Rules` (merged in `pixexid/amiga#1584`) |
| `amiga_house_cleaning_company_docs` | codex | 0 — content repository, no executable surface | n/a |
| `nuvyr_app` | kimi | 0 — no adjudicated incident yet | n/a |

Enrollment ownership is an explicit governance decision; it is not inferred from a
project's release-gate worker. Do not restate another repository's rules here.

#### Rule usefulness and noise re-check

Recorded 2026-07-28 against the first representative run under manual-only review:
PRs #340, #342, #345 and #348, sixteen connector findings across nine requested
exact-head reviews.

Every independently reproduced finding was real, and none was rule noise, so no
rule is removed. Findings clustered in Tier A families the rules already name, and
several rounds found test artefacts masking production behaviour, independently
confirming #306's "test-only is not intrinsically low risk" rule.

Re-check after the next comparable run; remove any rule that starts producing
findings nobody acts on.

### Onboarding a new project

1. Add a `projects.json` entry with `id`, `display_name`, `repos`,
   `default_branch_base`, `preflight_command`, and `github`. Add
   `ui_ux.required_design_docs` or `db.*` only when applicable.
2. Initialize `{project_state_root}/{project_id}/` through queue reconciliation,
   then add a project README that records the coordination chat, roles, and
   routing policy.
3. Add product-repository instructions such as `AGENTS.md` and worker-specific
   files. For skill-capable agents, point onboarding at the in-repo
   `skills/llm-collab-join/SKILL.md`; keep project-local `CLAUDE.md` or generic
   markdown only as thin fallback pointers, and keep the exact checkout /
   `--project <id>` binding in the product repo's own instructions.
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
      "ui_ux": {
        "required_design_docs": ["/absolute/path/to/my-app/DESIGN.md"],
        "required_design_skills": ["impeccable"]
      },
      "db": {
        "production_schema_guard": false,
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

`preflight_command` is the complete argv for that project. Shared workflow callers
execute the registered list exactly; project-specific flags belong in that project's
registration and are never appended by a generic helper. For example, an Amiga
registration that skips browser checks during task claims includes
`"--browser-check", "skip"` in its own command list. Other projects keep their
commands unchanged, including shell commands whose `-lc` argument already consumes
the command string.

For UI/UX projects, set `ui_ux.required_design_docs` to the project's own
canonical design sources. Set `ui_ux.required_design_skills` when the project
requires a particular design-skill family; the task-contract helper uses that
exact list. If the field is absent, only the exact `amiga` project retains the
legacy `[impeccable]` fallback; non-Amiga projects inherit no design skill.
Impeccable-specific booleans, commands, and evidence are required only when
`impeccable` appears in that exact list. The helper prepends configured documents to UI/UX tasks and removes the Amiga
design-doc default from non-Amiga projects. Additional task-specific design
sources remain allowed.

Set `db.shared_supabase_project_ref` and `db.required_surfaces` only for projects
that use the shared-Supabase task contract. Non-Amiga projects never inherit
Amiga's project ref or MCP surface names; an unconfigured database lane must
provide both values explicitly at task level.

Projects that can ship DDL to a shared or production database may opt into the
generic strict boolean `db.production_schema_guard`. Missing or `false` is
default-off compatibility behavior; a present non-boolean fails closed. When
enabled, schema changes cannot be classified as `none`, and
`local-schema-only` means disposable development/test schema that will never be
applied to a shared or production database. That exception requires the exact
`dev-only-non-production` value, `operator` approver, and a non-empty reason.
It never replaces the existing `shared-supabase-required` evidence. Resolution
uses only the task's exact registered `project_id`; missing, empty, null,
unknown, or foreign IDs never inherit another project's guard, ref, or tool
surfaces.

`claude_desktop_bridge` no longer selects a wake path. Every non-Codex worker
with `watcher_enabled: true` is woken by its durable packet and its own watcher
in every project and registration shape. Only the Codex recipient may use the
project-independent AX doorbell, and only through the exact command printed by
`deliver.py`; a worker without either route needs a dispatchable runtime session.

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

Use `skills/llm-collab-join/SKILL.md` and
`docs/workflows/collab-thread-quickstart.md` for current durable-mailbox send
examples. This guide does not duplicate `deliver.py` command families because
chat selection and repo-target rules are contract-sensitive.

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
```

For project-scoped delegation examples, use the join skill plus
`docs/workflows/collab-thread-quickstart.md` instead of copied `deliver.py`
walkthroughs here.

---

## Notes

- `new_chat.py` and `deliver.py` require `--project`, and each chat has a single `project_id` in `meta.json`.
- New and active task files must carry `project_id`; project-aware queues,
  reports, and adapters exclude unscoped tasks. Use explicit migration tooling
  to backfill legacy data.
- For single-project setups, pass that project ID consistently instead of leaving fields null.
