# Schema Reference

All schemas use version 2 (introduced in llm-collab v1.0).

---

## collab.config.json

Workspace-level settings. Created by `scripts/init.py`. Gitignored.

```json
{
  "workspace_name": "my-collab",
  "schema_version": 2,
  "projects_root": "/path/to/your/projects",
  "default_tags": [],
  "branch_pattern": "collab/{agent}/{task_slug}",
  "poll_interval_seconds": 15,
  "notifications_enabled": true,
  "notifications_platform": "auto"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `workspace_name` | string | Used as PM2 app prefix and in memory snippets |
| `schema_version` | int | Must be `2` |
| `projects_root` | string | Absolute path to directory containing project repos |
| `default_tags` | string[] | Default tags applied to messages (empty = no tags) |
| `branch_pattern` | string | Template for git branch names. Variables: `{agent}`, `{task_slug}`, `{orchestrator}` |
| `poll_interval_seconds` | int | Seconds between inbox polls in background watchers |
| `notifications_enabled` | bool | Whether to send desktop notifications |
| `notifications_platform` | string | `"auto"`, `"macos"`, `"linux"`, `"none"` |

---

## agents.json

Agent roster. Created by `scripts/init.py`. Gitignored.

```json
{
  "agents": [
    {
      "id": "orchestrator",
      "display_name": "Orchestrator",
      "role": "primary_orchestrator",
      "notes": "Plans work and delegates to workers.",
      "activation": {
        "type": "cli_session",
        "watcher_enabled": true
      }
    },
    {
      "id": "worker",
      "display_name": "Worker",
      "role": "implementation",
      "activation": {
        "type": "human_relay",
        "watcher_enabled": false,
        "base_model": "codex",
        "identity_note": "You are Worker (worker). Read only messages addressed to 'worker'."
      }
    }
  ]
}
```

### Agent fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier. Lowercase, alphanumeric + hyphens. |
| `display_name` | string | no | Human-readable name |
| `role` | string | no | Functional role label |
| `notes` | string | no | Free-text description |
| `activation` | object | yes | How this agent is started |

### activation object

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `"cli_session"`, `"human_relay"`, `"human"`, `"api_trigger"` |
| `watcher_enabled` | bool | no | Whether PM2 should manage a background watcher for this agent |
| `base_model` | string | no | For `human_relay`: which LLM this maps to (informational) |
| `identity_note` | string | no | For `human_relay`: shown in handoff prompt to disambiguate identity |

### Activation types

| Type | Watcher | Handoff prompt | Use for |
|------|---------|----------------|---------|
| `cli_session` | optional | no | LLM CLIs with persistent sessions |
| `human_relay` | no | **yes** | Second account of same LLM, or manual sessions |
| `human` | no | no | Human operators |
| `api_trigger` | no | no | Webhook-triggered agents |

---

## projects.json

Project registry. Created by `scripts/init.py`. Gitignored.

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
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique project identifier. Referenced in `project_id` fields. |
| `display_name` | string | Human-readable name |
| `repos` | object | Map of repo ID → path (relative to `projects_root` or absolute) |
| `default_branch_base` | string | Default git ref for worktree creation |
| `preflight_command` | string[] or null | Command to validate before task integration |
| `github.enabled` | bool | Whether GitHub integration is active |
| `github.repo` | string | `owner/repo` format |
| `github.project_number` | int | GitHub Projects board number |

---

## Message file schema

**Location**: `Chats/{date}_{title}__{CHAT-id}/{ts}_{direction}-{agent}_{slug}.md`

**Filename patterns**:
- `{ts}_to-{recipient}_{slug}.md` — recipient's copy
- `{ts}_from-{sender}_{slug}.md` — sender's copy (sent record)

Both files have identical content.

### Frontmatter

```yaml
---
chat_id: CHAT-A1B2C3D4
from: orchestrator
to: worker
title: Implement the checkout API endpoint
priority: high
tags: []
project_id: my-app
related_task: TASK-ABC123
repo_targets: [app, api]
path_targets: [src/routes/checkout.ts]
sent_utc: 2026-04-07T10:00:00+00:00
---
```

| Field | Type | Description |
|-------|------|-------------|
| `chat_id` | string | `CHAT-{8hex}` — identifies the thread |
| `from` | string | Sender agent ID |
| `to` | string | Recipient agent ID |
| `title` | string | Short semantic title |
| `priority` | string | `low`, `normal`, `high`, `urgent` |
| `tags` | string[] | Custom labels |
| `project_id` | string or null | Project this message relates to |
| `related_task` | string or null | `TASK-{id}` cross-reference |
| `repo_targets` | string[] | Which repos are in scope |
| `path_targets` | string[] | File/directory scope |
| `sent_utc` | string | ISO 8601 timestamp |

### Body

Free-form markdown. Recommended structure:

```markdown
## Context

Brief description of the situation.

## Request

What you need the recipient to do.

## Action Checklist

- [ ] Step 1
- [ ] Step 2

## Deliverables

- Description of expected output
```

---

## Task file schema

**Location**: `Tasks/{folder}/{date}_{slug}__{TASK-id}.md`

**Folder mapping**:
- `active/` — status: `open`, `in_progress`, `blocked`, `review`
- `backlog/` — status: `open` (planned, not started)
- `done/` — status: `done`

### Frontmatter

```yaml
---
task_id: TASK-ABC123
title: Implement user authentication
status: in_progress
owner: worker
created_by: orchestrator
requested_by: operator
created_utc: 2026-04-07T10:00:00+00:00
priority: high
project_id: my-app
related_chat: CHAT-A1B2C3D4
related_paths: [src/auth/, src/middleware/]
repo_targets: [app]
depends_on: [TASK-XYZ789]
branch: collab/worker/t-abc123-implement-auth
ui_ux_lane: true
ui_ux_mode: implementation
ui_ux_detection: auto
ui_ux_detection_reasons: [src/routes/app/bookings.index.tsx]
required_design_docs: [/Users/pixexid/Projects/amiga/docs/ui_ux/DESIGN.md]
required_design_skills: [impeccable]
impeccable_commands_required: [/impeccable craft, /polish]
impeccable_required: true
impeccable_antipatterns_enforced: true
design_doc_update_review_required: true
design_docs_read: []
design_skills_used: []
impeccable_commands_used: []
impeccable_detect_result: null
browser_validation_desktop: null
browser_validation_mobile: null
operator_visual_feedback_requested: false
design_doc_update_decision: null
---
```

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | `TASK-{6hex}` |
| `title` | string | Task title |
| `status` | string | `open`, `in_progress`, `blocked`, `review`, `done` |
| `owner` | string | Agent ID or `unassigned` |
| `created_by` | string | Agent ID who created the task |
| `requested_by` | string | Who requested the work |
| `created_utc` | string | ISO 8601 timestamp |
| `priority` | string | `low`, `normal`, `high`, `urgent` |
| `project_id` | string or null | Project this task belongs to |
| `related_chat` | string or null | `CHAT-{id}` cross-reference |
| `related_paths` | string[] | File/directory paths involved |
| `repo_targets` | string[] | Repos in scope |
| `depends_on` | string[] | `TASK-{id}` blockers |
| `branch` | string or null | Git branch for this task |
| `ui_ux_lane` | bool | Whether the lane is subject to the UI/UX workflow contract |
| `ui_ux_mode` | string | `implementation`, `docs_only`, or `none` |
| `ui_ux_detection` | string | `auto`, `manual_true`, or `manual_false` |
| `ui_ux_detection_reasons` | string[] | Why the lane was auto-flagged |
| `required_design_docs` | string[] | UI docs the worker must read before starting |
| `required_design_skills` | string[] | Design skill family expected for the lane; for Amiga UI/UX lanes this must be `[impeccable]` |
| `impeccable_commands_required` | string[] | Planned Impeccable steering commands for the lane |
| `impeccable_required` | bool | Whether `impeccable detect` is mandatory |
| `impeccable_antipatterns_enforced` | bool | Whether Impeccable curated anti-patterns are treated as a hard guardrail |
| `design_doc_update_review_required` | bool | Whether the lane must record a DESIGN.md review/update decision |
| `design_docs_read` | string[] | Design docs the worker explicitly confirms were read |
| `design_skills_used` | string[] | Design skills actually used during the lane; for Amiga UI/UX lanes this must be `[impeccable]` |
| `impeccable_commands_used` | string[] | Impeccable commands actually used during the lane |
| `impeccable_detect_result` | string or null | Captured `impeccable detect` evidence summary |
| `browser_validation_desktop` | string or null | Worker-owned desktop browser validation evidence |
| `browser_validation_mobile` | string or null | Worker-owned mobile browser validation evidence |
| `operator_visual_feedback_requested` | bool | Whether the operator was explicitly asked for visual review feedback |
| `design_doc_update_decision` | string or null | Same-session DESIGN.md or linked UI-doc review/update decision |

### Body

```markdown
# Task title

## Summary

Context and motivation.

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2

## Verification Plan

- [ ] How to verify it works

## Notes

## Activity Log

- 2026-04-07T10:00:00+00:00 | orchestrator | Task created
- 2026-04-07T10:05:00+00:00 | worker | Status → in_progress, owner → worker
```

---

## agents/{id}/inbox.json

Per-agent pointer index. Gitignored (runtime state).

```json
{
  "agent": "worker",
  "updated_utc": "2026-04-07T10:05:00+00:00",
  "unread": [
    "Chats/2026-04-07_checkout-flow__CHAT-A1B2C3D4/2026-04-07T10-00-00_to-worker_checkout-brief.md"
  ],
  "read": [
    "Chats/2026-03-15_auth-task__CHAT-B5E6F7G8/2026-03-15T09-00-00_to-worker_auth-spec.md"
  ]
}
```

All paths are relative to workspace root.

---

## agents/{id}/identity.md

Generated by `scripts/init.py`. Gitignored (contains local paths).

Contains:
1. Agent identity statement (explicitly scoped to this agent ID)
2. Role description
3. Workspace path
4. Bootstrap command
5. Key commands reference
6. Active projects list
7. Other agents list
8. Behavioral instructions

---

## projects/{project_id}/issue-queue.json

Optional per-project artifact for canonical ordered issue queues.

Use this when a project needs one durable ordered list of remaining issue-sized lanes that survives
chat/session turnover.

```json
{
  "schema_version": 1,
  "artifact_type": "ordered_issue_queue",
  "project_id": "amiga",
  "last_updated_utc": "2026-04-12T16:47:13+00:00",
  "source_issue": 257,
  "source_task": "TASK-A3AEFF",
  "completed_recently": [
    { "issue": 232, "task_id": "TASK-B0D14F", "owner": "cdx2", "status": "done" }
  ],
  "lanes": [
    {
      "order": 1,
      "issue": 233,
      "task_id": "TASK-48C9F9",
      "title": "validate transitions against real drive time not zone buckets",
      "owner": "cdx2",
      "task_status": "pending",
      "queue_state": "ready",
      "tier": 2,
      "depends_on": [],
      "blocked_by": [],
      "notes": "Current next lane after GH-232."
    }
  ]
}
```

### Queue fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | int | Queue schema version |
| `artifact_type` | string | `ordered_issue_queue` |
| `project_id` | string | Project identifier |
| `last_updated_utc` | string | ISO 8601 timestamp |
| `source_issue` | int | Workflow issue tracking this queue contract |
| `source_task` | string | Local task mirror tracking this queue contract |
| `completed_recently` | object[] | Optional recently-finished reference lanes |
| `lanes` | object[] | Ordered remaining issue-sized lanes |

### Lane fields

| Field | Type | Description |
|-------|------|-------------|
| `order` | int | Canonical queue order; contiguous starting at 1 |
| `issue` | int | GitHub issue number |
| `task_id` | string | Local task mirror id |
| `title` | string | Short lane title |
| `owner` | string | Assigned collaborator |
| `task_status` | string | Mirror task status (`pending`, `in_progress`, `review`, etc.) |
| `queue_state` | string | Queue position state such as `ready`, `queued`, or `blocked` |
| `tier` | int | Optional rollout tier |
| `depends_on` | string[] | Task-level dependencies |
| `blocked_by` | string[] | Explicit blocker references |
| `notes` | string | Short operator/orchestrator note |

### Operational rules

- the canonical queue path should remain stable even when no lanes remain
- when the final lane completes, archive the last queue snapshot to `projects/{project_id}/history/`
- keep `projects/{project_id}/issue-queue.json` and `.md` present with `lanes: []` so fresh sessions see an explicit empty queue instead of a missing file

Read first by `session_bootstrap.py` so the LLM immediately knows its identity.

---

## Chat meta.json

```json
{
  "chat_id": "CHAT-A1B2C3D4",
  "title": "Implement checkout flow",
  "project_id": "my-app",
  "created_utc": "2026-04-07T10:00:00+00:00"
}
```

---

## State/worktrees.json

Registry of all created git worktrees.

```json
[
  {
    "task_id": "TASK-ABC123",
    "agent": "worker",
    "repo": "/path/to/my-app",
    "worktree_path": "/path/to/my-app-worktrees/worker/t-abc123-implement-auth",
    "branch": "collab/worker/t-abc123-implement-auth",
    "base_ref": "main",
    "base_sha": "abc123def456",
    "integrated": false,
    "retired": false,
    "created_utc": "2026-04-07T10:00:00+00:00"
  }
]
```

---

## State/awareness.json

Local runtime state used to avoid repeating first-time collaboration onboarding instructions to the same recipient.

```json
{
  "version": 1,
  "agents": {
    "worker": {
      "aware": true,
      "updated_utc": "2026-04-08T03:00:00+00:00",
      "source": "onboarding_message",
      "message_path": "Chats/.../2026-04-08T03-00-00_to-worker_intro.md"
    }
  }
}
```

Notes:
- this file is runtime-only and gitignored (`State/`)
- `deliver.py` uses it to print first-time onboarding relay prompts for `human_relay` recipients, then avoid repeating those long prompts
