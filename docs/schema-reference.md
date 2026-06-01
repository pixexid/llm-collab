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
  "project_state_root": "~/.local/share/llm-collab/projects",
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
| `project_state_root` | string | Optional path for local project runtime state such as queues, runbooks, roles, and memory templates. Defaults to `<workspace_root>/projects` for backward compatibility. Prefer a path outside the Git checkout. |
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
        "app": "my-app",
        "api": "my-app-api"
      },
      "default_branch_base": "main",
      "preflight_command": ["pnpm", "preflight", "--json"],
      "github": {
        "enabled": true,
        "repo": "owner/my-app",
        "project_number": 1,
        "backlog": {
          "exclude_labels": ["type:epic", "wontfix", "duplicate", "invalid", "question", "status:deferred"],
          "require_any_label": []
        }
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
| `github.backlog.exclude_labels` | string[] | Open issue labels excluded from the executable backlog. Defaults include `type:epic`, terminal labels, and `status:deferred`. |
| `github.backlog.require_any_label` | string[] | Optional label or wildcard patterns that an open issue must match to be backlog-eligible. Empty means every non-excluded open issue is eligible. |

When GitHub integration is enabled, open GitHub issues are the source of truth for whether project work remains. Runtime queues may order and assign work, but `issue-queue.json` is empty only when the eligible GitHub backlog is empty.

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
design_thinking_polish_budget_loc: 80
design_thinking_polish_seeds: [map rail coupling, empty route row treatment]
design_thinking_pass_items: []
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
| `dependency_materialization_gate` | bool | For queued design lanes, require accepted dependency artifacts to be present in the assigned worktree before activation/review |
| `required_dependency_artifacts` | string[] | Repo-relative or absolute artifact paths that `project_design_queue.py validate` checks when the lane is `ready`, `active`, or `review` |
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
| `design_thinking_polish_budget_loc` | int or null | UI/UX implementation lanes only: refinement-time D8 polish budget, roughly 10–20% of the implementation LOC estimate |
| `design_thinking_polish_seeds` | string[] | UI/UX implementation lanes only: at least 2 surface-specific D8 polish vectors seeded during refinement |
| `design_thinking_pass_items` | object[] | UI/UX implementation lanes only: review/PR evidence for the D8 pass; at least 3 items with `finding`, `disposition`, and optional `evidence` |
| `design_docs_read` | string[] | Design docs the worker explicitly confirms were read |
| `design_skills_used` | string[] | Design skills actually used during the lane; for Amiga UI/UX lanes this must be `[impeccable]` |
| `impeccable_commands_used` | string[] | Impeccable commands actually used during the lane |
| `impeccable_detect_result` | string or null | Captured `impeccable detect` evidence summary |
| `browser_validation_desktop` | string or null | Worker-owned desktop browser validation evidence |
| `browser_validation_mobile` | string or null | Worker-owned mobile browser validation evidence |
| `operator_visual_feedback_requested` | bool | Whether the operator was explicitly asked for visual review feedback |
| `design_doc_update_decision` | string or null | Same-session DESIGN.md or linked UI-doc review/update decision |

`design_thinking_pass_items` entries use:

```yaml
- finding: "Map marker click feels detached from the opened popover"
  disposition: shipped
  evidence: "Added 220ms marker ring expansion before popover"
```

Allowed dispositions: `shipped`, `deferred`, `out_of_scope`.

Non-trivial tasks must also include a completed body section named `## Implementation Risk Analysis` before `plan_task.py`/`refine_task.py` can set `refined_by: claude`. The section is a planning/refinement gate, not optional prose. Required labels:

- `Current file/topology reviewed:`
- `Scope split decision:`
- `Estimated diff/risk:`
- `Verification/browser/sign-off plan:`
- `Open decisions/blockers:`

When Claude both creates and plans a task (`created_by: claude` and
`refined_by: claude`), activation also requires `accepted_by: codex` and
`accepted_at` before `claim_task.py --status in_progress` succeeds. That
acceptance is Codex's independent read of the task/issue, source evidence,
queue order, blockers, and task contract.

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

## Implementation Risk Analysis

- Current file/topology reviewed: concrete files/directories inspected
- Scope split decision: one lane, split now, or defer with reason
- Estimated diff/risk: expected diff size and risky surfaces
- Verification/browser/sign-off plan: exact evidence and review mechanics
- Open decisions/blockers: blockers before activation, or none

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

## project_state_root/{project_id}/issue-queue.json

Optional per-project artifact for canonical ordered issue queues.

Use this when a project needs one durable ordered list of remaining issue-sized lanes that survives
chat/session turnover.

```json
{
  "schema_version": 1,
  "artifact_type": "ordered_issue_queue",
  "project_id": "my-app",
  "last_updated_utc": "2026-01-01T00:00:00+00:00",
  "source_issue": 1,
  "source_task": "TASK-EXAMPLE",
  "completed_recently": [
    { "issue": 100, "task_id": "TASK-DONE1", "owner": "worker", "status": "done" }
  ],
  "lanes": [
    {
      "order": 1,
      "issue": 101,
      "task_id": "TASK-READY1",
      "title": "first ready implementation lane",
      "owner": "worker",
      "task_status": "pending",
      "queue_state": "ready",
      "tier": 1,
      "depends_on": [],
      "blocked_by": [],
      "notes": "Replace with local project queue details."
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
- when the final lane completes, archive the last queue snapshot to `{project_state_root}/{project_id}/history/`
- keep `{project_state_root}/{project_id}/issue-queue.json` and `.md` present, but treat `lanes: []` as valid only when `project_issue_queue.py validate` confirms the eligible GitHub backlog is also empty
- do not keep real project queue files as tracked files in this repo; only `projects/_example/` belongs in the public checkout

---

## project_state_root/{project_id}/design-queue.json

Deprecated per-project artifact for older design-first queue workflows.

Do not use `design-queue.json` as a second backlog source. New design-first work should live in the single `issue-queue.json` as lanes whose `lane_type` identifies the design scope. GitHub-backed backlog validation remains the authority for whether work exists; design views and bridge helpers may filter or annotate the single issue queue, but they must not create an independent `lanes: []` state.

```json
{
  "schema_version": 2,
  "artifact_type": "ordered_design_queue",
  "project_id": "my-app",
  "last_updated_utc": "2026-01-01T00:00:00+00:00",
  "mode": "design-only-until-empty",
  "completed_recently": [
    { "issue": 100, "task_id": "TASK-DONE1", "title": "accepted surface spec", "owner": "designer", "status": "done" }
  ],
  "lanes": [
    {
      "order": 1,
      "phase": "design-refresh D1",
      "issue": 101,
      "task_id": "TASK-DESIGN1",
      "title": "customer status tracker design",
      "owner": "designer",
      "task_status": "open",
      "queue_state": "ready",
      "lane_type": "design-layout-plus-template-spec",
      "repo_scope": "design/surfaces/customer.md",
      "depends_on": [],
      "blocked_by": [],
      "notes": "Design contract only; implementation follows from accepted handoff."
    }
  ]
}
```

### Design Queue Rules

- prefer `issue-queue.json` with `lane_type: design*` for new design, shaping, surface-spec, handoff, parity, stale-issue audit, and template-design lanes
- validate backlog emptiness with `python3 bin/project_issue_queue.py validate --project <project_id>`; do not trust a design queue's empty state as proof that work is done
- keep `project_design_queue.py` for legacy design-queue inspection and Claude Desktop bridge metadata while projects migrate to single-queue design lanes
- for lanes depending on accepted-but-not-yet-main design outputs, set `dependency_materialization_gate: true` and `required_dependency_artifacts` in the task mirror; validation fails ready/active/review lanes when the assigned worktree lacks those artifacts
- when migrating an old design queue, copy active design lanes into `issue-queue.json`, preserve their `lane_type`, then archive the legacy design queue

---

## project_state_root/{project_id}/claude-desktop-bridge-state.json

Runtime-local state for the Claude desktop Computer Use bridge.

This file is outside the public checkout when `project_state_root` points to a
local runtime directory. It records recent Computer Use capture/accessibility
timeouts for the current ready design lane so unattended heartbeats can apply a
cooldown instead of repeatedly spending long tool timeouts on the same blocked
desktop state.

```json
{
  "project_id": "my-app",
  "updated_utc": "2026-01-01T00:00:00+00:00",
  "computer_use_timeouts": {
    "TASK-DESIGN1": {
      "last_timeout_utc": "2026-01-01T00:00:00+00:00",
      "timeout_count": 1,
      "reason": "Computer Use get_app_state timed out after 120s",
      "issue": 101,
      "bridge_thread_uuid": "00000000-0000-0000-0000-000000000000",
      "worktree": "/path/to/worktree",
      "branch": "codex/claude/task-design1"
    }
  }
}
```

### Bridge state fields

| Field | Type | Description |
|-------|------|-------------|
| `project_id` | string | Project identifier |
| `updated_utc` | string | Last write timestamp |
| `computer_use_timeouts` | object | Map of task id to the latest idle-time Computer Use timeout blocker |
| `last_timeout_utc` | string | Timestamp used to calculate the retry cooldown |
| `timeout_count` | int | Consecutive timeout count for the task; repeated failures increase the retry cooldown |
| `reason` | string | Short operator-readable blocker reason |
| `issue` | int | GitHub issue number for the ready lane |
| `bridge_thread_uuid` | string or null | Claude desktop bridge UUID for audit/binding |
| `worktree` | string or null | Assigned implementation/design worktree |
| `branch` | string or null | Assigned branch |

Use `python3 bin/project_design_queue.py record-computer-use-timeout --project
<project_id> --reason "..."` to update this state. Use `python3
bin/project_design_queue.py bridge-status --project <project_id> --json` to read
the current classification. When it reports
`computer-use-cooldown-no-durable-progress`, keep checking durable evidence but
do not call Computer Use again until the cooldown expires.

Repeated timeouts use exponential backoff from 30 minutes up to a 2-hour cap.
This prevents unattended loops from spending a 120-second Computer Use timeout
on every heartbeat when Claude Desktop accessibility/capture is blocked.
`bridge-status --json` also reports `recommended_next_check_seconds` and
`recommended_next_check_minutes` inside `computer_use_blocker`; heartbeat loops
should use that value as their next wake cadence while no durable progress is
visible.

---

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
- Claude Desktop recipients are not normal relay recipients when Computer Use is
  available. For those sends, `deliver.py` reports `desktop_bridge_required`
  and a one-line bridge prompt for Codex to send through Computer Use. The
  operator wake path is a last resort; if Computer Use is blocked, record a
  blocker, keep the heartbeat active, and retry through Codex/Computer Use or
  repair tooling before asking the operator to relay.
