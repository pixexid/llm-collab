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
        "watcher_enabled": true,
        "ax_app": "Codex"
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
| `ax_app` | string | no | For AX-capable `cli_session` agents: localized macOS app name or bundle ID used by `axsend`. Omit for terminal-only sessions. |
| `base_model` | string | no | For `human_relay`: which LLM this maps to (informational) |
| `identity_note` | string | no | For `human_relay`: shown in handoff prompt to disambiguate identity |

### Activation types

| Type | Watcher | Handoff prompt | Use for |
|------|---------|----------------|---------|
| `cli_session` | optional | no | LLM CLIs with persistent sessions; direct AX wake requires `ax_app` |
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
      "claude_desktop_bridge": false,
      "ui_ux": {
        "required_design_docs": ["/absolute/path/to/project/DESIGN.md"]
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
| `claude_desktop_bridge` | bool | Optional Claude Desktop fallback for a non-CLI Claude target. CLI-session workers use AX first. |
| `ui_ux.required_design_docs` | string[] | Optional project-specific design sources prepended to every UI/UX task contract. Non-Amiga projects must configure these or provide explicit task-level design docs. |
| `db.shared_supabase_project_ref` | string | Optional shared Supabase project ref required by database-impact task contracts. Non-Amiga projects do not inherit Amiga's ref. |
| `db.required_surfaces` | string[] | Optional project-specific CLI or MCP surfaces required by shared-database task contracts. |
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
| `project_id` | string | Registered project this message belongs to |
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
required_design_docs: [/absolute/path/to/my-app/DESIGN.md]
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
| `project_id` | string | Registered project this task belongs to |
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

## Planned State/thread_event_runner/runner.sqlite3 (not implemented)

**Phase 1 contract only.** The repository does not currently create this
database, run a Thread Event Runner daemon, or provide the delivery guarantees
described below. See the
[Thread Event Runner RFC](workflows/thread-event-runner-rfc.md) for the full
architecture, state machines, threat review, and rollout gates.

The planned ledger is a fresh SQLite database, independent of
`State/session_autobridge/`. Every connection requires WAL mode, foreign keys,
a 5-second busy timeout, and full synchronous writes:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = FULL;
```

The directory is planned as mode `0700` and database-related files as `0600` on
POSIX systems. All four identity fields below are non-null and exact:

```text
(project_id, runtime_home_id, runtime_home_realpath, native_thread_id)
```

`runtime_home_id` is the SHA-256 of the canonical absolute
`runtime_home_realpath`. Missing, empty, `null`, prefix, display-name, and
"latest" matches are invalid. Legacy wildcard matching is allowed only in an
explicit migration tool and cannot become an active subscription.

### Planned subscription record

Logical `subscriptions` fields:

```json
{
  "subscription_id": "UUID",
  "name": "mailbox handoff watch",
  "project_id": "my-app",
  "runtime_home_id": "sha256-hex",
  "runtime_home_realpath": "/absolute/path/to/CODEX_HOME",
  "native_thread_id": "native-thread-id",
  "agent_id": "orchestrator",
  "chat_id": "CHAT-A1B2C3D4",
  "task_id": "TASK-ABC123",
  "adapter_name": "mailbox",
  "adapter_version": "1",
  "handler_name": "action-first-thread-note",
  "handler_version": "1",
  "capability_profile_id": "observe.mailbox",
  "config_json": {},
  "revision": 1,
  "state": "active",
  "next_run_utc": "2026-01-01T00:00:00Z",
  "expires_at_utc": null,
  "created_at_utc": "2026-01-01T00:00:00Z",
  "updated_at_utc": "2026-01-01T00:00:00Z"
}
```

Planned subscription states are `active`, `paused`, `cancelling`, `cancelled`,
`expired`, and `error`. Adapter, handler, capability profile, and exact target
identity come from trusted registries and are immutable. Mutable updates require
the expected revision and atomically increment it. Retargeting or changing a
source kind requires cancel plus create.

### Planned event record

Adapters return a bounded, data-only envelope. The runner adds subscription,
identity, revision, receive timestamp, and hash after validation:

```json
{
  "schema_version": 1,
  "adapter_name": "mailbox",
  "adapter_version": "1",
  "event_type": "message_pointer_changed",
  "source_event_id": "adapter-stable-id",
  "source_cursor": "opaque-bounded-cursor",
  "observed_at_utc": "2026-01-01T00:00:00Z",
  "source_time_utc": null,
  "subject": "bounded display text",
  "coalescing_key": "mailbox:orchestrator:CHAT-A1B2C3D4",
  "observed_state": "unread_message_present",
  "expected_outcome": "message_acknowledged",
  "why_not_done": "target_thread_not_yet_checked",
  "next_unlock_action": "wake_origin_thread_when_delivery_is_enabled",
  "severity": "actionable",
  "payload": {}
}
```

The planned serialized envelope limit is 64 KiB. `subject` and
`coalescing_key` are each limited to 256 UTF-8 bytes. Payloads cannot select or
override a project, runtime home, thread, adapter, handler, capability, command,
tool, URL, module, path root, lease, retry policy, retention, or feature flag.

Logical `events` rows reference the subscription and revision, store canonical
envelope/hash/classification, and enforce source-event/hash uniqueness according
to the registered adapter policy. Valid classifications are `observed`, `quiet`,
`actionable`, `coalesced`, `delivery_created`, and `invalid`. A quiet observation
updates the compact checkpoint/snapshot but creates no delivery or repeated
operator notification.

### Planned delivery record

```json
{
  "delivery_id": "UUID",
  "subscription_id": "UUID",
  "subscription_revision": 1,
  "project_id": "my-app",
  "runtime_home_id": "sha256-hex",
  "runtime_home_realpath": "/absolute/path/to/CODEX_HOME",
  "native_thread_id": "native-thread-id",
  "handler_name": "action-first-thread-note",
  "coalescing_key": "mailbox:orchestrator:CHAT-A1B2C3D4",
  "state": "pending",
  "first_event_id": 1,
  "last_event_id": 1,
  "event_count": 1,
  "attempt_count": 0,
  "next_attempt_utc": "2026-01-01T00:00:00Z",
  "expires_at_utc": "2026-01-02T00:00:00Z",
  "lease_owner": null,
  "lease_fence": null,
  "attempt_token": null,
  "cancel_requested": false
}
```

Planned delivery states are `pending`, `leased`, `deferred_busy`,
`dispatching`, `retry_wait`, `reconciling`, `delivered`, `cancelled`, `obsolete`,
`expired`, and `dead_letter`. Busy deferral consumes no retry attempt.
`reconciling` means runtime acceptance is unknown and MUST NOT auto-retry.

Only open deliveries with the same exact identity, subscription/revision,
handler, and normalized coalescing key may merge. A leased, dispatching,
reconciling, or terminal delivery never accepts new events; those events create
or join a successor delivery.

Each external attempt has a unique attempt token and a numbered
`delivery_attempts` row. An attempt records `not_accepted`, `accepted`, or
`acceptance_unknown` plus bounded response/error evidence. Only authoritative
`not_accepted` evidence permits automatic retry. Default planned retry policy is
5 attempts, exponential backoff with full jitter, 5-second base, and 15-minute
cap, bounded by delivery expiry.

### Planned lease records

`thread_leases` has one row per exact identity tuple. `subscription_leases` has
one row per subscription. Both records contain:

```json
{
  "owner_instance_id": "runner-instance-UUID",
  "fence": 42,
  "acquired_at_utc": "2026-01-01T00:00:00Z",
  "renewed_at_utc": "2026-01-01T00:00:10Z",
  "expires_at_utc": "2026-01-01T00:00:30Z"
}
```

Every acquisition after expiry increments the monotonic fence. Claim,
pre-send authorization, result commit, retry scheduling, and lease release use
compare-and-swap checks on owner, fence, attempt token, subscription revision,
and expected delivery state. A stale owner cannot complete or release newer
work. No database transaction remains open during source I/O, sleeps,
subprocesses, AX, network, or app-server calls.

### Planned supporting tables and transaction rule

The ledger also requires `runner_instances`, `source_checkpoints`,
`delivery_attempts`, `dead_letters`, `audit_log`, and `schema_migrations`, all
with foreign keys. Observation inserts/deduplicates the event, updates or
creates its coalesced delivery, advances the checkpoint, and writes audit state
in one `BEGIN IMMEDIATE` transaction. Delivery claim and fenced result commit
are separate short transactions around external work.

The exact-thread dispatcher is not part of Phase 2 and remains feature-disabled
until authoritative thread busy state and `turn/start` acceptance/idempotency
plus restart reconciliation are integration-proven. Phase 2 is limited to this
SQLite ledger and read-only timer/filesystem/mailbox observation.

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

### Parallel lane policy

The ordered queue is an activation contract, not a requirement to keep only one
worker alive. Projects may run multiple active lanes when the orchestrator
records why the work is parallel-safe.

- Keep one writer per task, branch, and isolated worktree.
- Use read-only mapper, planner, reviewer, docs-sync, and release-guard workers
  freely while an implementation writer is active.
- Activate more than one implementation writer only after recording a
  non-overlap check in the task activity note or lane notes. The check should
  cover routes/surfaces, file sets, shared utilities, API/data/schema ownership,
  generated artifacts, validation resources such as ports/browser profiles, and
  intended merge order.
- If a queued lane needs an out-of-order implementation claim, use
  `claim_task.py --allow-queue-override` only with that non-overlap evidence.
- If the non-overlap check is unclear, keep the later lane in planning/review
  prep instead of starting a second writer.

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
- `deliver.py` prepends first-time onboarding to the recipient's durable mailbox
  packet before marking awareness, so AX and runtime-dispatched workers receive
  the same setup contract as human-relay recipients
- `human_relay` recipients also receive the onboarding in the printed handoff
  prompt; later deliveries omit it once awareness is tracked locally
- AX-capable `cli_session` workers configure `activation.ax_app`. For those
  sends, `deliver.py` reports `ax_doorbell_required` and prints the
  `axsend-ensure ring --submit --verify` command the sender should run.
- A terminal-only `cli_session` needs a dispatchable runtime session. Without
  either transport, `deliver.py` reports `activation_unavailable` instead of
  silently requesting operator relay.
- A project may enable `claude_desktop_bridge` for a non-CLI Claude target. Only
  that fallback reports `desktop_bridge_required` and uses Computer Use; it does
  not override AX routing for a Claude agent registered as `cli_session`.
