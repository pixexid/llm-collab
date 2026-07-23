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
| `watcher_enabled` | bool | no | Whether the current PM2 ecosystem should instantiate a background watcher. The ecosystem checks this flag only; it does not filter by activation `type`. |
| `ax_app` | string | no | For AX-capable `cli_session` agents: localized macOS app name or bundle ID used by `axsend`. Omit for terminal-only sessions. |
| `ax_attended_only` | bool | no | GH-1547: set `true` when the agent's composer is `AXValue`-opaque (emptiness unprovable — e.g. ZCode, Antigravity). `deliver.py` then never emits a routine AX doorbell and instead prints an ATTENDED RECOVERY REQUIRED instruction routing control to Codex; this supersedes `human_relay` operator routing for the flagged agent. Must agree with the `axsend` binary's composer opacity table (`tools/axbridge/send-resolution.swift`) — `tests/test_deliver_ax_routing.py` enforces the agreement. |
| `base_model` | string | no | For `human_relay`: which LLM this maps to (informational) |
| `identity_note` | string | no | For `human_relay`: shown in handoff prompt to disambiguate identity |

### Activation types

| Type | Watcher | Handoff prompt | Use for |
|------|---------|----------------|---------|
| `cli_session` | if enabled | no | LLM CLIs with persistent sessions; direct AX wake requires `ax_app` |
| `human_relay` | if enabled | **yes** | Second account of same LLM, or manual sessions; normally configured with watcher disabled |
| `human` | if enabled | no | Human operators; normally configured with watcher disabled |
| `api_trigger` | if enabled | no | Webhook-triggered agents; normally configured with watcher disabled |

The table describes current PM2 materialization, not a recommendation to enable
watchers for every type. Any entry with `watcher_enabled: true` is instantiated
on ecosystem start/reload. Existing or PM2-saved processes can outlive later
roster/config edits until explicitly reconciled and re-saved; see
[PM2 Watcher Adapter](adapters/pm2.md).

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
        "direct_app_only": false,
        "required_design_docs": ["/absolute/path/to/project/DESIGN.md"]
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
      },
      "release_gate_agent": "orchestrator",
      "release_closure": {
        "trigger_event": "push",
        "workflow": "deploy",
        "required_jobs": ["detect", "deploy"],
        "smoke_job": "deploy",
        "required_smoke_steps": ["Verify production hosts", "Verify production auth"]
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
| `ui_ux.direct_app_only` | bool | Optional, default-off direct-app gate. When `true`, every non-`done` task must avoid design/sandbox/spec/handoff/parity, bare-template, and template-design-only lane types, repository-root `design/**` targets, and dependency materialization of newly authored `design/**` artifacts. Explicit implementation lanes such as `template-implementation`, `src/design/**`, and read-only `required_design_docs` remain valid. Absolute related/dependency paths require a complete resolvable project `repos` mapping so repository-root scope can be evaluated. A present non-boolean value is a configuration error. |
| `ui_ux.required_design_docs` | string[] | Optional project-specific design sources prepended to every UI/UX task contract. Non-Amiga projects must configure these or provide explicit task-level design docs. |
| `db.production_schema_guard` | bool | Optional strict boolean, default `false`. When `true` for the task's exact project, assignment/review/PR/done validation rejects schema-changing tasks classified as `none`, restricts `local-schema-only` to the exact operator-approved dev-only exception, and treats concrete `db/migrations/**` or `db/schema.sql` paths as schema changes even after `manual_false`. A present non-boolean fails closed; projects never inherit another project's value. |
| `db.shared_supabase_project_ref` | string | Optional shared Supabase project ref required by database-impact task contracts. Non-Amiga projects do not inherit Amiga's ref. |
| `db.required_surfaces` | string[] | Optional project-specific CLI or MCP surfaces required by shared-database task contracts. |
| `github.enabled` | bool | Whether GitHub integration is active |
| `github.repo` | string | `owner/repo` format |
| `github.project_number` | int | GitHub Projects board number |
| `github.backlog.exclude_labels` | string[] | Open issue labels excluded from the executable backlog. Defaults include `type:epic`, terminal labels, and `status:deferred`. |
| `github.backlog.require_any_label` | string[] | Optional label or wildcard patterns that an open issue must match to be backlog-eligible. Empty means every non-excluded open issue is eligible. |
| `release_gate_agent` | string | Required, never-defaulted enabled agent ID that must equal `claim_task.py --released-by` for every new `review -> done` transition. `scripts/init.py` requires an explicit selection from collected enabled agents for new projects; existing registries require a manual local rollout. Missing or empty fails closed and must be repaired in that task project's `projects.json` entry. This is workflow deterrence and attribution, not authentication. |
| `release_closure.workflow` | string | Deploy workflow name for the exact-merge-SHA release gate (`bin/deploy_release_watch.py`). Required for the gate; never defaulted. |
| `release_closure.trigger_event` | string | The automatic event that runs the production deploy (`push` for Amiga; `workflow_run`/`merge_group` for projects triggered that way). Required; never defaulted — only runs with this event on `default_branch_base` count. |
| `release_closure.required_jobs` | string[] | Job names that must be present and `success` for release closure. Project-specific — no project inherits Amiga's labels. |
| `release_closure.smoke_job` | string | The required job carrying the post-deploy smoke steps. Must be one of `required_jobs`. |
| `release_closure.required_smoke_steps` | string[] | Step names inside `smoke_job` that must be present and `success`. Project-specific — no project inherits Amiga's labels. |

A project without a complete `release_closure` (plus `default_branch_base` and
an enabled `github.repo`) fails the release gate closed with exit 64; the gate
never guesses a branch, workflow, or evidence labels.

`release_gate_agent` and `release_closure` serve different purposes. The former
prevents accidental closure by the wrong workflow actor; only transition-time
`deploy_release_watch.py` evaluation supplies objective success authority.
Caller assertions, watcher packets, saved JSON artifacts, and green runs for a
different SHA are never evidence shortcuts. A project without
`release_closure` may record an explicit structured `non-production` or
`risk-accepted-followup` disposition, but cannot record `success`.
Likewise, `success` requires an enabled exact `github.repo`, while an honest
`non-production` or `risk-accepted-followup` disposition remains reachable for
a project with GitHub disabled. Such a record persists `repository: null` and
does not persist a caller-supplied `run_id`; a run ID becomes authoritative and
persistable only after successful transition-time evaluation.

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
| `activation` | bool (optional) | `true` marks a writer ACTIVATION packet. Emitted by `deliver.py --activation`, which atomically requires `related_task`, `worktree`, and `branch` (delivery fails otherwise); `worktree` must be absolute and is canonicalized before serialization. |
| `worktree` | string (activation only) | Absolute canonical assigned worktree; part of the activation identity |
| `branch` | string (activation only) | Assigned branch; part of the activation identity |
| `repo_targets` | string[] | Which repos are in scope |
| `path_targets` | string[] | File/directory scope |
| `sent_utc` | string | ISO 8601 timestamp |

An activation packet's identity is the exact canonical tuple
`(project, chat, task, worktree, branch, target-agent)`
(`bin/_activation_identity.py`); its body opens with a banner carrying the
absolute exact-packet claim command. A packet carrying any activation marker
(`activation`, `worktree`, `branch`) without the complete identity is
MALFORMED and consumers must fail closed — it is never an ordinary message.
Malformed includes: any activation marker without `activation: true` present
(the marker is mandatory and exactly boolean), falsy/coerced marker values,
relative or home-relative worktrees, absolute worktree spellings that are not
in canonical lexical form (dot/dotdot segments, duplicate separators,
non-root trailing separators, or a double-leading slash — the receiver
requires the serialized value to equal its lexical normal form before the
byte-exact comparison), a `to` field that is not a string exactly equal to
the claiming target agent, and non-string parsed identity values.
Verdicts are CWD/HOME/filesystem-time independent: the sender canonicalizes an
EXISTING directory exactly once before serialization, and receivers never
re-resolve — the serialized identity is compared byte-exact, so post-send path
creation or symlink replacement cannot move an identity. Lease authority uses
the same byte-exact identity for its record key, then resolves the claimed
worktree once under the claim lock only to reject active symlink/alias
collisions. Consumption gating and wake-path enforcement ship in the follow-on
lanes (GH-1572). Runtime consumption is active: the emitted claim command uses
`inbox.py --packet <basename>` and must select exactly one packet across the
recipient's read+unread inbox union. Ambiguous selectors, malformed activation
packets, and refused lease claims fail closed before any inbox read mutation.
`--mark-all-read` clears stale missing pointers and ordinary mail, but holds
existing activation-shaped packets for explicit claim or manual adjudication.

### Activation lease records

Activation lease authority stores one JSON record per exact activation identity
under `State/session_autobridge/activation_leases/{lease_key}.json`, with a
stable never-unlinked sibling `.lock` file used for `flock(LOCK_EX|LOCK_NB)`
per-identity claim contention and a stable never-unlinked global
`.claim-grant.lock` file also acquired with `flock(LOCK_EX|LOCK_NB)` to
serialize the authority-granting instant across distinct identity keys. Lock
contention returns bounded `claim_in_progress`; non-contention lock errors
re-raise. The global grant lock covers worktree realpath resolution, active
lease alias scan, and lease write so symlink aliases cannot race into two
active records. The record is written only by
`session_autobridge.py lease-claim`/`lease-release`; `lease-assert` is
read-only.

```json
{
  "identity": {
    "project": "amiga",
    "chat": "CHAT-A1B2C3D4",
    "task": "TASK-ABC123",
    "worktree": "/absolute/canonical/worktree",
    "branch": "codex/example",
    "target_agent": "claude"
  },
  "lease_key": "16hex",
  "owner_session_id": "SESSION-...",
  "owner_runtime_session_id": "runtime-thread-id",
  "owner_pid": 12345,
  "status": "active",
  "fence_token": 1,
  "claimed_utc": "2026-01-01T00:00:00+00:00",
  "lease_expires_utc": "2026-01-01T01:00:00+00:00",
  "previous_owner_session_id": null,
  "worktree_realpath": "/resolved/real/worktree",
  "updated_utc": "2026-01-01T00:00:00+00:00"
}
```

The lease CLI subcommands (`lease-claim`, `lease-show`, `lease-assert`, and
`lease-release`) require `--project` to name a registered `projects.json` entry
before they construct, read, or write an activation-lease identity.

`owner_session_id` must name a registered live autobridge session whose
`agent_id`, `project_id`, and `chat_id` exactly match the activation identity;
missing/null session bindings are unbound and refuse. A registered session is
live only when its status is in the live set (`active` or `parked`) and its
session `lease_expires_utc` has not expired. Claim, assert, and release use that
same registered-session liveness rule. A claim also requires a bound claimant
identity from the current caller: `--claimant-runtime-id`, a reader runtime
environment variable, or a live positive `--owner-pid`. Claim, assert, and
release never derive claimant identity from the session or lease record being
checked. PID `0` and negative PIDs are process-group selectors rather than
process identities and refuse with `invalid_owner_pid`. A positive explicit
`--owner-pid` that is provably dead refuses with `owner_pid_not_live`. Claims
with no runtime identity and no live pid refuse with
`claimant_identity_required`; an identity-less lease record is never valid.

Every ownership change increments `fence_token`. Refused claims are evaluated
before record writes and must leave the existing lease file byte-identical.
`lease-assert` requires `--fence-token` and verifies the owner session plus the
runtime/pid binding recorded at claim time; same-session assertions from a
different runtime or process refuse. `lease-release` applies the same
owner-session and claimant runtime/pid binding before it writes a released or
superseded status; a refused release leaves the lease file byte-identical.
Assert and release refuse stopped, superseded, or session-expired owner
sessions with `owner_session_not_live`. Expired activation leases do not assert;
the owner must reclaim to refresh TTL before mutating. Live, unexpired
same-session/same-runtime runtime-only reclaim refreshes TTL with the same
fence. Expired or provably dead leases are never idempotently reclaimed: every
identity combination requires explicit `--takeover` and writes a new
`fence_token`. Unknown liveness fails closed. Non-active or expired
same-realpath lease records do not block a new identity claim. Malformed
activation lease JSON fails closed with `corrupt_lease_state`; the refusal names
only the bad lease filename, field, and reason, never file contents. Active,
unexpired lease records are structurally invalid unless `worktree_realpath`,
`lease_key`, `owner_session_id`, and `status` are all present non-null strings.
During alias enumeration, active unexpired lease records are also invalid unless
the payload `lease_key` matches the filename-derived key and the payload
`identity` hashes back to that same key; this semantic binding is required by
the runtime activation gate.
Claim, assert, and release route existing-lease authority through one shared
validation entry point covering structural validity, lease-key and identity
binding, session liveness and binding, claimant runtime/PID binding, PID
liveness, fence, and lease expiry.

Runtime activation claims first audit stale recurring inbox pollers for the
same activation identity. PM2 `jlist` PIDs are the authoritative preserved set:
registered watcher PIDs are reported and preserved. Unregistered matching
pollers must be terminated with verified SIGTERM/SIGKILL proof, or the claim
refuses with a cleanup/audit reason. Test suites must use
`LLM_COLLAB_PS_FIXTURE` for process listings so fixture cleanup cannot signal a
real PID. Chat-id poller matching is target-agent-bound; a poller for another
agent that mentions the same chat id is never a match.

Session autobridge dispatch treats activation-shaped packets as activation
packets before loop-protection or processed-message mutations. A malformed
packet or concurrent claim loss stays unread/unprocessed. A successful dispatch
attaches the activation identity and fence to the runtime payload and resume
prompt. Dispatcher claims bind both runtime id and dispatcher process pid, so
two dispatcher processes sharing one registered runtime/session cannot both
idempotently wake the same packet. Each protected filesystem or process
mutation for an activation packet runs through a lease-held mutation guard that
acquires the per-identity claim lock, validates the exact owner/runtime/pid and
fence, holds that lock through the mutation, and then releases it. Protected
boundaries include turn summaries, runtime triggers, relay prompts, UI
refreshes, loop-protection processed writes, and ordinary processed-message
writes. A stale fence at any boundary stops that packet without marking it
processed; a dead predecessor process may be taken over only by minting a newer
fence.

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
release_evidence: null
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
| `direct_app_legacy_maintenance` | bool | For a project with `ui_ux.direct_app_only: true`, the first of three mandatory fields for an explicitly approved active legacy-design maintenance exception. Must be strict `true`; incomplete overrides fail closed. |
| `direct_app_legacy_maintenance_approved_by` | string or null | Second mandatory legacy-maintenance field. Must equal `operator`. |
| `direct_app_legacy_maintenance_reason` | string or null | Third mandatory legacy-maintenance field. Must be a non-empty reason. |
| `db_impact` | string | `none`, `local-schema-only`, or `shared-supabase-required`. `local-schema-only` means disposable development/test schema that will never be applied to a shared or production database. |
| `db_schema_change_detected` | bool | Whether the task changes schema. With the project production-schema guard enabled, concrete `db/migrations/**` and exact `db/schema.sql` paths force this to `true`. |
| `db_schema_change_detection` | string | `auto`, `manual_true`, or `manual_false`. Guarded concrete schema paths cannot be hidden by `manual_false`; body-only documentation matches can. |
| `db_local_schema_only_exception` | string or null | For a guarded schema change classified `local-schema-only`, must equal `dev-only-non-production`. |
| `db_local_schema_only_exception_approved_by` | string or null | For the guarded local-only exception, must equal `operator`. |
| `db_local_schema_only_exception_reason` | string or null | For the guarded local-only exception, must be a non-empty, non-whitespace reason. |
| `release_evidence` | object or null | Normalized closure record written only by a successful new `review -> done` transition. See below. |

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

### Production schema classification guard

Projects that ship database schema to a shared or production environment may
opt in with strict boolean `db.production_schema_guard: true`. Missing or
`false` preserves existing behavior. A configured non-boolean refuses
assignment, review, PR, and new done transitions and names the exact project
registry key to repair.

When enabled, a detected schema change cannot use `db_impact: none`.
`local-schema-only` is narrowly reserved for disposable development/test schema
that will never reach a shared or production database, and requires all three
exact task fields:

```yaml
db_local_schema_only_exception: dev-only-non-production
db_local_schema_only_exception_approved_by: operator
db_local_schema_only_exception_reason: "Why this schema is disposable and non-production"
```

The exception does not waive the `shared-supabase-required` contract. A shared
schema lane still needs the exact project ref/surfaces and, at review, PR, and
done, non-empty migration files, apply result, schema assertion, advisors
result, and runtime validation. Concrete `db/migrations/**` paths and the exact
`db/schema.sql` path force schema-change detection even when a task carries
`db_schema_change_detection: manual_false`; a body-only documentation keyword
match may still be manually overridden.

### Done transition and release evidence

Only a task whose current status is exactly `review` may newly transition to
`done`. The caller must supply:

```text
--released-by <project release_gate_agent>
--release-evidence '{"merge_sha":"<40-hex>","verdict":"success|risk-accepted-followup|non-production","run_id":123,"note":"optional"}'
```

The evidence must be exactly one JSON object containing only `merge_sha`,
`verdict`, optional `run_id`, and optional non-empty `note`. `run_id` is
required for `success` and, whenever present, is a positive strict integer
(booleans and floats are invalid). For `success`, `claim_task.py` evaluates the
exact SHA live through the same configured workflow/event/branch/job/smoke
authority as `deploy_release_watch.py`, requires terminal `SUCCESS`, and
requires the supplied run ID to equal the evaluator-selected run ID.

Before any release-evidence evaluator runs, `claim_task.py` validates a copy of
the task in its target `done` state at the done contract stage. A classification
or shared-database evidence refusal therefore happens before evaluator work,
activity append, task write/unlink/move, queue advancement, or cleanup, for all
three release dispositions. The persisted `release_evidence` object contains `project_id`, `task_id`,
`repository` (explicitly `null` when GitHub is disabled), `merge_sha`,
configured `workflow` when present, authoritative `run_id` only after a
successful objective evaluation, `production_impact`, `terminal_verdict`,
`released_by`, `evaluated_at`, and optional `note`. Refusals occur before any task write,
file move, or queue transition. Existing files already in `Tasks/done` are
grandfathered and are not retroactively revalidated.

Rollback is an ordinary code/config revert: revert the done-gate implementation
and remove or revert the registry key if necessary. Do not hand-edit a task
around a refusal; repair the missing project configuration or release evidence
and rerun the transition.

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

All inbox message paths above are relative to workspace root. Project runtime
state paths below resolve from `project_state_root` and may be outside the Git
checkout.

---

## Implemented workspace observation ledger

GH-90 implements a narrower observation foundation for the broader
[Thread Event Runner RFC](workflows/thread-event-runner-rfc.md). It does not
implement subscriptions, timers, canonical delivery, attempts, receipts,
leases, fences, retries, quarantine, dead letters, dispatch, or management of
those planned objects.

Fresh initialization generates an opaque `WorkspaceV1`-compatible
`workspace_id`. An existing workspace can add one without reinitialization:

```bash
python3.11 scripts/init.py --add-workspace-id
```

That command refuses to replace an existing identity, writes the original
bytes once to `collab.config.json.pre-workspace-id.bak` with mode `0600`, then
atomically replaces only `collab.config.json`. Configuration
`schema_version: 2` is independent of the ledger's `PRAGMA user_version`.

All artifacts for one workspace are derived, without path overrides, at:

```text
{project_state_root}/llm-collabd/{workspace_id}/
├── ledger.sqlite3
├── backups/ledger-{prior_version}-{utc_stamp}.sqlite3
├── daemon.lock
├── daemon.sock
└── logs/llm-collabd.jsonl
```

Directories are mode `0700`; the database, SQLite sidecars, backups, lock,
socket, and logs are mode `0600` where POSIX permissions apply. The encoded Unix
socket path is limited to 103 bytes. One non-blocking `flock` owner is the only
writer/checkpointer; readers are separate thread-bound, query-only connections.

Every connection applies and verifies WAL mode, foreign keys, a 5-second busy
timeout, and full synchronous writes:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = FULL;
```

SQLite must be exactly 3.44.6 or 3.50.7, or 3.51.3 and newer. Every open verifies
the actual SQLite main-database descriptor against a no-follow `(device,inode)`
pin and rejects symlinked/non-regular artifacts. Migrations are sequential and
recorded in both `PRAGMA user_version` and `schema_migrations` with released SQL
checksums, schema fingerprints, tool version, timestamp, and a verified backup
of the prior version. Startup requires exact table, migration-row, fingerprint,
`integrity_check`, and `foreign_key_check` agreement. A failed migration restores
its verified backup.

The private `project_state_root` is a trust boundary. No-follow traversal,
descriptor identity checks, fixed paths, and permissions are defense in depth;
Python's stdlib SQLite API cannot bind SQLite's separately opened `-wal` and
`-shm` sidecars to a retained ancestor directory descriptor. A same-UID actor
that can replace an ancestor of the private state root is outside the supported
threat model. The implementation does not claim WAL/SHM ancestor-swap
containment.

### Implemented ledger versions

The exact current ledger is `PRAGMA user_version = 8`
(`llm_collab.ledger.store.SCHEMA_VERSION == 8`):

| Version | Landed in | Tables, triggers, and constraints |
|---|---|
| v1 | P1a merge `b89d12d` | 5 tables: `schema_migrations`, `workspace_registry_snapshots`, `project_registry_snapshots`, `observation_source_registry_snapshots`, and `daemon_instances`. Registry rows are immutable snapshots keyed by exact `workspace_id`, `project_id`, `source_id`, and `registry_revision`. |
| v2 | P1c merge `6e3768e` | 3 tables: `observations`, `observation_checkpoints`, and `observation_audit`. Every row is composite-scoped by workspace, exact project, closed source `chats_mailbox`, and exact registry revision. Paths and hashes have database-level NUL, traversal, length, and lowercase-hex checks. |
| v3 | P1d merge `a235f89` | 1 table plus 2 triggers: append-only `legacy_provenance_imports`, including UPDATE/DELETE refusal triggers and exact-project versus `legacy_unscoped` scope constraints. |
| v4 | P2a merge `5673037` | 5 tables plus 18 triggers: content-addressed `canonical_bodies`, immutable `canonical_messages`, and normalized child tables for recipients, artifacts, and tags. Scope uses `(workspace_id, scope_kind, scope_identity)`, with project rows bound to exact registry snapshots and workspace rows bound to the workspace registry snapshot. Child collections are sealed after parent creation and count-capped. |
| v5 | P2b merge `1369eaf` | 4 tables plus 12 triggers: `canonical_evidence_bodies`, `canonical_deliveries`, `canonical_delivery_attempts`, and `canonical_delivery_receipts`. Attempts are TTL-bounded; receipts are append-only and carry closed state/evidence vocabularies. Terminal `accepted`/`completed` receipts require authoritative `native_delivery_state` or `exact_session_acknowledgment` evidence plus `session_ref_id`. |
| v6 | P2c merge `a42585b` | 3 tables plus 6 triggers: `legacy_import_manifests`, `legacy_import_manifest_entries`, and `legacy_import_records`. The import manifest is sealed, idempotent, append-only, and records exact locator/content-hash/source membership for legacy v2 packets and inbox indexes. Its publisher/provenance fields are caller-asserted and unauthenticated; GH-195 remains open. |
| v7 | GH-179 scheduler merge `62b1839` | 1 table: `observation_scheduler_cursors`. The cursor is keyed by exact workspace and closed source `chats_mailbox`, stores the next project ID for fair per-project observation scheduling, and is bound to the workspace registry snapshot. |
| v8 | GH-271 Child 2 | 4 tables plus 2 partial unique indexes and 1 trigger: `conversation_participants`, `lifecycle_provider_registry`, `conversation_bindings`, and `session_binding_challenges`. The storage is inert and adds no route authority. It enforces the compound participant key, a separate lifecycle-provider registry, one active/draining mutation binding per participant, one active/draining mutation owner per exact native-session owner tuple, and monotonic binding generation. |

The migration checksums and schema fingerprints are code authority in
`llm_collab/ledger/store.py` and are verified on open. Current values are:

| Version | Migration checksum | Schema fingerprint |
|---|---|---|
| v1 | `sha256:ce236daff444f736e01f3666ed44baf1c3ba17e81215fedb638276aff76b01c7` | `sha256:26a856329406e45d22a8fbecdbd769d9c632acae3652d8c72438d228de7cfca2` |
| v2 | `sha256:338a5d526b6fdea47af667c469897fd38d97a4a2dc8caf90dc5d62c067610e36` | `sha256:805aa5ae43c31d85dbe9a84590050b701ddc69cfe1dd225e9c6e67afbd889a7c` |
| v3 | `sha256:1b8380593b73695bf8824425b58eda7c94f51fc0937f07dbcbd1786a6e5d467b` | `sha256:88e59c9be91df366c03985f99f8b3db1c68382b4846612c0334fd15cc505e673` |
| v4 | `sha256:63f00990d9c3e01384d14d7613c961856ff48037504b1e0ada1f95b034cedf01` | `sha256:665e17152991c6c21cb8756a5d5720e35e3154d13a4a069b4c74440ed425b39e` |
| v5 | `sha256:d6498cf5728ec3d56c0d1360a065243d72384a0de50af55bead8054881bbd9b9` | `sha256:4495eab6339d339b770442d994b5878e0743d011917cc99b370991a793891a99` |
| v6 | `sha256:56e7ca2ba9eb0a8eb79079372abdc7a39c024977e71a40931b8b60a6acc33c00` | `sha256:eb8bc4ddd4348ce05874b91c63ce963c5bb3653636363b7437e2046900996d60` |
| v7 | `sha256:2de4a95aaf7f92fb436772b5cf4fede42db485ae464809b9a23f9c8ccc6dda03` | `sha256:3fd3ca002c8571ff90165da045929aedd520d2a891a8b95b2a36ba07569c32e1` |
| v8 | `sha256:437fe52450978b246b2a62fd5a0a0f08ddbf4f3f97501dafda0eb999e48580ff` | `sha256:9aefd9f214307d6645358444485b632dcbfc8c1a809a0c3708c369909abdaf3f` |

No subscription, timer, runtime-dispatch lease, fence, retry, or quarantine
table exists in v1 through v8. Dead-letter and reconciliation outcomes in
Phase 2 are receipts, not a separate table.

### Implemented Phase 2 canonical surfaces

Phase 2 canonical message and compatibility work is implemented but remains
default-off and non-authoritative for current v2 operations.

- **Message intent (P2a, merge `5673037`).** Library calls store canonical body
  bytes once, derive message identity from immutable intent, normalize
  recipients/artifacts/tags, and reject divergent duplicate dedupe keys.
- **Deliveries and receipts (P2b, merge `1369eaf`).** Library calls create
  delivery routes, attempts, and append-only receipts. Receipt states are
  `persisted`, `routed`, `injected`, `visible`, `accepted`, `processing`,
  `acknowledged`, `completed`, `rejected_before_acceptance`, `ambiguous`,
  `pull_pending`, and `deferred_busy`. `accepted` and `completed` require
  authoritative evidence and `session_ref_id`.
- **Sealed legacy import manifests (P2c, merge `a42585b`).** The importer
  records immutable source membership and hashes for legacy v2 chat packet and
  inbox-index inputs. The seal verifies integrity and closed membership only;
  publisher identity and manifest provenance remain
  `caller_asserted_unauthenticated` under GH-195.
- **Read-only compatibility projections (P2d, merge `df0fe31`).** Projection
  helpers expose honest v2-shaped packet, inbox-pointer, and legacy-provenance
  views without writing v2 files or fabricating unrepresentable fields.
- **Gated library controls (P2e, merge `cf50d239`).** Mutating control helpers
  are library-only and require one conjunctive gate:
  exact project `canonical_writes` is `true` in the admitting registry snapshot,
  `LLM_COLLAB_CANONICAL_CONTROL=enabled`, and per-call
  `allow_canonical_write=True`. `canonical_writes` alone is insufficient.

#### Not yet in Phase 2

Phase 2 does **not** deliver live delivery routing, inbox mark-read,
inbox consumption, inbox write projection, v2 ownership transfer, any command or
CLI cutover, authenticated manifest provenance, or `canonical_writes` enabled by
default.

### Implemented daemon and observation surface

The public command grammar is:

```text
bin/llm-collab daemon <start|stop|status|logs>
bin/llm-collab doctor
```

`bin/llm-collab daemon start --background` is also supported. The fixed
version-1 Unix-socket protocol accepts only
`{"version":1,"op":"status|logs|shutdown"}` with no
unknown or duplicate members. Requests are bounded to 4096 bytes, responses to
65536 bytes, and I/O to a 2-second deadline. The server proves the peer UID
before reading or parsing request bytes. Stale recovery unlinks only an
inode-stable, non-listening socket; it never unlinks a symlink or non-socket.

Observation is effective only when all three inputs are true:

1. the closed feature declaration is valid and declares
   its daemon-observation feature true;
2. `THREAD_EVENT_RUNNER_ENABLED=1`;
3. `THREAD_EVENT_RUNNER_OBSERVE=1`.

The semantics are AND, never OR. A missing, malformed, duplicate-member,
wrong-version, wrong-identity, unknown-feature, or non-boolean declaration sets
every declared feature false. With the gate off, the daemon still acquires the
writer lock and serves honest control diagnostics, but it does not open, create,
migrate, integrity-check, or observe the ledger.

The only implemented source is `chats_mailbox`: fixed `Chats/*/*.md` packets and
the `read`/`unread` packet pointers in `agents/*/inbox.json`. It stores bounded
path, hash, size, mtime, cursor, count, resolution, and audit metadata; it does
not store message bodies. Filesystem events only mark the engine dirty and
reduce latency. Periodic reconciliation every 30 seconds is the sole correctness
authority. For each project-scoped checkpoint, one pass scans at most 2000
candidate directory entries and atomically inserts at most 500 new observations
plus that checkpoint and its reconciliation audit row. It may then prune at most
500 resolved observations older than 30 days and write the matching retention
audit. Because the engine runs that work once per registered project, one
30-second cadence may perform up to N times those bounds for N projects. There
is no global per-cadence bound or fairness policy yet; open
[#179](https://github.com/pixexid/llm-collab/issues/179) owns that prerequisite.
Observation, checkpoint, and audit reads and writes require the exact workspace,
project, source, and registry revision. Provenance import is scoped by workspace
and registry revision; each row is either bound to one exact registered project
or is `legacy_unscoped` with a `NULL` project, and only exact-project rows appear
in exact-project projections. One workspace ledger may contain many projects.

### Implemented legacy provenance import

P1d imports only the closed current-v2 source families
`State/session_autobridge/sessions/*.json` and
`State/session_autobridge/activation_leases/*.json`. It records source family,
record kind, relative locator, SHA-256, byte size, timestamps, import revision,
and transaction ID. It never stores source JSON and no runtime, dispatcher,
lease, activation, inbox, or other consumer reads these rows.

Session scope comes only from a top-level `project_id`; activation-lease scope
comes only from `identity.project`. The claim must exactly match a project in
the immutable registry snapshot. Missing, malformed, duplicate-member,
non-RFC JSON, excessive-depth, unknown, or foreign claims become
`legacy_unscoped` and are never projected by exact-project reads. Collection is
bounded to **5000 directory entries total across both source directories** and
**1048576 bytes (1 MiB) per regular JSON file**. The entry budget counts every
directory entry before suffix filtering and fails closed rather than
truncating. No-follow, non-blocking file opens reject symlinks, FIFOs, devices,
sockets, and files that change during collection. The complete set is collected
and revalidated before one append-only transaction.

### Rollback

Rollback disables observation by clearing either environment gate or setting
the declaration false, then optionally stops the daemon. It preserves
unresolved observations, canonical rows, receipts, and imported provenance,
leaves the ledger available through query-only readers, and leaves current v2
writers and owners unchanged. To roll back Phase 2 operational use or a future
cutover attempt, keep `canonical_writes` false or unset, do not set
`LLM_COLLAB_CANONICAL_CONTROL=enabled`, and keep current `bin/` commands on v2
files. Those gates block P2e controls and current-authority activation; they are
not a process-wide kill switch for direct in-process `LedgerStore` or
`llm_collab.canonical` library calls. Rollback never performs an in-place schema
downgrade or destructive cleanup of canonical evidence. A migration failure may
restore the verified pre-migration backup automatically; an intentional rollback
keeps v1-v8 evidence intact for later reconciliation.

## Implemented Phase 3.5 conversation binding foundation

The v8 ledger adds the inert Phase 3.5 conversation binding storage foundation.
It adds no runtime code, commands, router, dispatcher, AX behavior, GitHub issue
mutation, or production delivery.

Phase 3.5 reuses the existing durable `chat_id` value as `conversation_id`, but
`chat_id` is not globally unique. The only authoritative participant key is:

```text
(workspace_id, scope_kind, scope_identity, conversation_id, participant_id)
```

`conversation_id` alone MUST NOT key a binding, route a delivery, resolve a
participant, or select a native session. A same-`chat_id` value in two different
projects or scopes is two different conversation addresses. Missing, empty, or
`null` scope components never act as wildcards or legacy backfills.

The storage shape intentionally has no `conversations` table. It adds only
participant and binding records plus the separate lifecycle-provider registry:

- `conversation_participants`, keyed by the compound participant address above;
- `conversation_bindings`, keyed by the compound participant address plus
  derived generation and derived `binding_id`;
- `session_binding_challenges`, for one-time, TTL-bounded attach/start proofs;
- `lifecycle_provider_registry`, a trusted registry for providers that create,
  attach, inspect, heartbeat, and retire native sessions.

The lifecycle-provider registry is distinct from runtime-adapter manifests.
Lifecycle providers establish and verify native session bindings. Runtime
adapter manifests authorize post-initialize delivery actions. A row from one
registry cannot substitute for the other.

The exact native-session owner tuple for v8 active/draining mutation-capable
bindings is:

```text
(workspace_id, provider_id, endpoint_id, session_ref_id, native_session_id, runtime_instance_id)
```

Storage constraints enforce:

- one active mutation-capable binding per compound participant key;
- one active/draining mutation owner per exact native-session owner tuple;
- monotonic generation per participant by trigger;
- a closed lifecycle vocabulary of `reserved`, `registering`, `active`,
  `draining`, `unverified`, `superseded`, `retired`, and `quarantined`;
- partial uniqueness that allows a `superseded`, `retired`, or `quarantined`
  binding to coexist with a later active generation;
- NUL-safe byte-length checks on every new text column.

The read-only resolver maps one full participant address to either one current
active mutation-capable binding reference or one closed non-send reason:
`waiting_for_session`, `route_ambiguous`, `session_unverified`,
`adapter_quarantined`, `pull_pending`, or `stale_generation`. It validates the
compound address before querying and never accepts `conversation_id`,
`agent_id`, endpoint, native session, window/frontmost/latest/sidebar, or caller
session values as alternate route authority.

Resolver success is a binding reference only. v8 rows contain exact
`endpoint_id`, `session_ref_id`, `native_session_id`, provider, runtime
instance, `binding_id`, and generation fields; they do not contain the evidence,
authority, runtime-home annotations, or optional repository binding required to
fabricate a complete `SessionRefV1` document.

Later Phase 3.5 children still own storage-derived write helpers, audited
rebind/handoff records, dispatch-time `(binding_id, generation)` persistence,
restart-to-`unverified` transitions, lifecycle-provider evidence assembly, and
any delivery-capable route consumption.

## Planned Thread Event Runner records

Everything below this heading remains a broader future contract. The exact
identity tuple for a future targeted subscription is:

```text
(project_id, runtime_home_id, runtime_home_realpath, native_thread_id)
```

`runtime_home_id` is the SHA-256 of the canonical absolute
`runtime_home_realpath`. It is a stable local namespace key, not an account
identity, credential, or authentication fingerprint. Missing, empty, `null`,
prefix, display-name, and "latest" matches are invalid. Legacy wildcard matching
is allowed only in an explicit migration tool and cannot become an active
subscription. Targeted subscription creation and delivery also require immutable
authoritative native project evidence: registered repo ID/realpath, native
thread cwd realpath, and evidence source/version/hash. That binding is verified
at create and re-verified before dispatch; missing evidence or runtime drift
fails closed.

### Planned subscription record

`subscriptions` is the lifecycle/current-head row:

```json
{
  "subscription_id": "UUID",
  "name": "mailbox handoff watch",
  "state": "active",
  "subscription_mode": "observation_only",
  "current_revision": 1,
  "created_at_utc": "2026-01-01T00:00:00Z",
  "updated_at_utc": "2026-01-01T00:00:00Z"
}
```

Subscription IDs are authoritative; names are display labels and are never
accepted as an ambiguous mutation target.

`subscription_revisions` contains the immutable frozen snapshot, keyed by
`(subscription_id, revision)`:

```json
{
  "subscription_id": "UUID",
  "revision": 1,
  "project_id": "my-app",
  "runtime_home_id": "sha256-hex",
  "runtime_home_realpath": "/absolute/path/to/CODEX_HOME",
  "native_thread_id": "native-thread-id",
  "project_repo_id": "app",
  "project_repo_realpath": "/absolute/path/to/my-app",
  "native_thread_cwd_realpath": "/absolute/path/to/my-app",
  "project_binding_evidence_source": "authoritative-runtime-api",
  "project_binding_evidence_version": "1",
  "project_binding_evidence_hash": "sha256-hex",
  "project_binding_verified_at_utc": "2026-01-01T00:00:00Z",
  "agent_id": "orchestrator",
  "chat_id": "CHAT-A1B2C3D4",
  "task_id": "TASK-ABC123",
  "adapter_name": "mailbox",
  "adapter_version": "1",
  "handler_name": "action-first-thread-note",
  "handler_version": "1",
  "capability_profile_id": "observe.mailbox",
  "capability_profile_version": "1",
  "dispatch_profile_id": null,
  "dispatch_profile_version": null,
  "source_identity_hash": "sha256-hex",
  "config_json": {},
  "schedule_json": {},
  "expires_at_utc": null,
  "created_at_utc": "2026-01-01T00:00:00Z"
}
```

Planned lifecycle states are `active`, `paused`, `cancelling`, `cancelled`,
`expiring`, `expired`, and `error`. The lifecycle row points to an immutable
revision with a deferred composite foreign key valid only inside the atomic
insert/head-advance transaction. Checkpoints, events, deliveries, and attempts
also carry `(subscription_id, subscription_revision)` and have composite foreign
keys to the same frozen snapshot. `subscription_mode` is immutable for the
lifetime of the subscription. An `observation_only` subscription requires null
dispatch-profile fields; a `delivery_capable` subscription freezes a non-null
trusted dispatch-profile identity/version at creation. That registry entry
binds the exact handler/capability versions, pointer-template version, allowed
adapter/event and target class, bounded coalescing/retry/expiry policy, and
runtime-attestation requirements. Adapter name/version, handler name/version,
capability-profile identity/version, dispatch-profile identity/version, exact
target, source kind,
registry-declared source identity, and project binding cannot be rewritten by
an ordinary revision. Mutable updates require the expected current revision,
may change only registry-declared mutable config or policy, insert a new snapshot
retaining mode and all frozen identities, and atomically advance the head. Any
mode, identity/version, retargeting, or source change requires cancel plus
create.

Phase 3 activation therefore cancels the Phase 2 `observation_only`
subscription and creates a separate `delivery_capable` subscription; it never
advances the old subscription to a delivery revision. The same revision-checked
management flow writes an audit-only `subscription_activation_links` row with
old/new subscription and revision IDs, actor/time, and exactly one operator-
selected continuity policy: `start_now` or a cursor/evidence record that the
new adapter registry explicitly approves for replay. The link shares no event,
projection, checkpoint, lineage, delivery, attempt, lease, retry, or quarantine
state. The old checkpoint remains evidence and is not implicitly copied.
Phase 2 events/projections remain on the cancelled subscription. The first
delivery state may arise only from a fresh adapter observation under the new
subscription; replay re-reads the source through the adapter, and a source that
cannot honor the selected cursor waits for a post-activation observation.

Pause freezes unleased open deliveries and makes them ineligible; a pre-send
lease returns to its prior state, while `dispatching`/`reconciling` work keeps
its quarantine through authoritative resolution. Expiry enters `expiring`,
expires unleased/pre-send work, and reaches terminal `expired` only after no
in-flight/quarantine remains. Error stops polls/claims and preserves unleased
work as ineligible until explicit revision-checked recovery while in-flight work
reconciles. Recovery compare-and-swaps the lifecycle row against the expected
current revision without mutating the immutable snapshot: `active` restores
eligibility and `paused` keeps it frozen. A config/policy change requires a new
revision. Cancellation cannot complete merely because a lease expired;
`cancelled` requires no leased, dispatching, reconciling, or
subscription-owned quarantine state.

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

Phase 2 filesystem configuration stores an approved root plus its device/inode
identity. Observations use relative names, directory-fd-anchored no-follow
traversal, and post-open root/final-fd verification. A platform without
equivalent primitives cannot enable the adapter; canonical path-string checks
alone are not accepted.

### Planned Phase 2 timer adapter record

Timer observation is planned and read-only; it cannot create a delivery or
dispatch in Phase 2. The immutable revision stores one explicit schedule kind.
A civil example is:

```json
{
  "schedule_kind": "civil_recurring",
  "frequency": "weekly",
  "weekdays": [1, 3, 5],
  "local_time": "09:30:00",
  "time_zone": "America/Los_Angeles",
  "tzdb_version": "2026a",
  "ambiguous_time_policy": "first",
  "nonexistent_time_policy": "skip",
  "missed_fire_policy": "coalesce",
  "max_lateness_seconds": 86400,
  "catch_up_limit": 4
}
```

Allowed kinds and bounds:

- `one_shot`: RFC 3339 `scheduled_at_utc` with `Z`, at most 5 years ahead
- `fixed_interval`: RFC 3339 `anchor_utc` with `Z` and integer
  `interval_seconds` from 60 through 31,536,000; occurrence instants derive from
  the anchor, not the last wake
- `civil_recurring`: `daily` or `weekly`, second-precision local time, explicit
  IANA zone and exact tzdb version, with ISO weekdays 1=Monday through 7=Sunday
  for weekly schedules; abbreviations/host-local defaults are invalid

The exact tzdb version must be loadable or the subscription enters `error`.
Changing it requires a new immutable revision and explicit cursor policy. The
source checkpoint stores last/next scheduled UTC, last occurrence ID, frozen
tzdb version, and bounded skip/coalesce counters. Occurrence IDs are deterministic
SHA-256 values over subscription ID, revision, schedule kind, scheduled UTC,
civil label, and fold index, and are unique in the event/checkpoint ledger.

Due decisions use wall-clock UTC; waits use monotonic time with at most a
60-second recheck. Restart, wake, and clock jumps recompute from frozen schedule
plus checkpoint. A backward clock never rewinds the checkpoint or re-emits an
occurrence.

`missed_fire_policy` is explicitly persisted as `skip`, `coalesce`, or
`catch_up`; omitted management input materializes `coalesce`. Lateness defaults
to 86,400 seconds and is bounded through 604,800. `catch_up_limit` defaults to 4
and is bounded from 1 through 16; it emits only the most recent eligible
occurrences, while older ones update a skipped counter. Coalesce emits one
first/last/count summary. One poll emits at most 16 individual events plus one
summary. A one-shot emits at most once inside the lateness window and otherwise
terminates quietly according to policy.

Civil schedules persist `ambiguous_time_policy: first|second|both|skip` (default
materialized as `first`) and `nonexistent_time_policy: skip|next_valid` (default
materialized as `skip`). `both` uses fold indexes 0/1 for distinct occurrence
IDs; `next_valid` retains the original civil label and chosen resolution in
evidence. All timer counters saturate at unsigned 64-bit maximum.

Logical `events` rows carry a composite foreign key to the immutable
subscription revision, store canonical envelope/hash/classification, and
enforce source-event/hash uniqueness according to the registered adapter policy.
Valid classifications are `observed`, `quiet`, `actionable`, `coalesced`,
`delivery_created`, and `invalid`; a retained `quiet` row represents a changed
semantic state, never an unchanged poll.

The registered semantic quiet projection excludes cursor, source/observation
timestamps, poll counters, latency, and other volatile metadata. If its hash is
unchanged, one observation transaction advances only the checkpoint, latest
time, and quiet counters; it creates no event, delivery, audit row, or operator
notification. Invalid diagnostics retain at most one row per
subscription/revision/reason per 15 minutes and 100 per subscription; excess
occurrences increment a counter only.

Phase 2 may also store an optional bounded `simulation_projections` row for an
event/revision. It contains only a projected action class, normalized
coalescing key/window estimate, count/bytes, timestamps, and expiry, with
`deliverable = false`. It has no lineage, delivery, attempt, lease, target,
quarantine, retry, acceptance, idempotency, or dispatch foreign key/state. It is
analytical evidence only: no claim or dispatch query may read it, and Phase 3
must never convert, copy, claim, or promote it. Retention may expire it without
affecting events or checkpoints.

### Planned delivery record

```json
{
  "delivery_id": "UUID",
  "lineage_id": "UUID",
  "lineage_generation": 1,
  "replaces_delivery_id": null,
  "subscription_id": "UUID",
  "subscription_revision": 1,
  "project_id": "my-app",
  "runtime_home_id": "sha256-hex",
  "runtime_home_realpath": "/absolute/path/to/CODEX_HOME",
  "native_thread_id": "native-thread-id",
  "handler_name": "action-first-thread-note",
  "coalescing_key": "mailbox:orchestrator:CHAT-A1B2C3D4",
  "state": "pending",
  "pre_lease_state": null,
  "first_event_id": 1,
  "last_event_id": 1,
  "event_count": 1,
  "coalesced_bytes": 1024,
  "coalescing_window_started_utc": "2026-01-01T00:00:00Z",
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

`delivery_lineages` stores one immutable semantic-work identity derived by the
ledger from project, trusted handler/action class, and origin event/dedupe
evidence. Callers cannot choose a new lineage for a replacement. The unique
origin mapping plus `replaces_delivery_id` forces retry, rebind, and replacement
deliveries to retain the same lineage and increment generation.

Only open deliveries with the same exact identity, subscription/revision,
handler, and normalized coalescing key may merge. A leased, dispatching,
reconciling, or terminal delivery never accepts new events; those events create
or join a successor delivery. Default coalescing window is 60 seconds with a
hard 15-minute maximum; each bucket is capped at 256 observations and 64 KiB of
canonical state. A full/sealed bucket creates a successor or bounded overflow
counter. While its target is quarantined, at most one successor per revision/key
is retained and further occurrences update a saturating 64-bit counter only.

Each external attempt has a unique attempt token and a numbered
`delivery_attempts` row that repeats subscription/revision and has composite
foreign keys to both the matching delivery and immutable subscription revision.
Each attempt also stores the immediately pre-dispatch authoritative
project/runtime binding and runtime capability attestation hashes/times. An
attempt records `not_accepted`, `accepted`, or `acceptance_unknown` plus bounded
response/error evidence. Only authoritative `not_accepted` evidence permits
automatic retry. Default planned retry policy is 5 attempts, exponential
backoff with full jitter, 5-second base, and 15-minute cap, bounded by delivery
expiry.

The dispatch prompt is a fixed trusted pointer-only template containing only
grammar-validated runner-owned IDs. No event envelope, subject, state, reason,
path, or chat content is interpolated. The runtime must enforce and attest the
frozen tool/filesystem/network/UI capability profile; prompt text is not a
security boundary. A trusted delivery reader exposes detail only as bounded,
UTF-8-validated, JSON-escaped canonical JSON under `untrusted_event`; it never
renders event bytes as instructions, markdown, shell, or tool names. Without
that runtime enforcement, delivery stays disabled.

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

`thread_quarantines` has one row per exact target while a delivery is
`dispatching`, `reconciling`, or dead-lettered with unresolved acceptance. It
stores the delivery, subscription/revision, attempt token, reason, and resolution
evidence. Every normal claim checks both this table and unresolved in-flight
delivery state before lease acquisition. Quarantine survives lease
expiry/release, runner restart, and subscription pause/error/cancel. Lease
renewal uses short fenced transactions during an external call; if renewal fails
after a possible send, the attempt becomes `acceptance_unknown` and quarantine
remains.

`lineage_quarantines` independently blocks the semantic work across target
changes. Every claim checks target and lineage quarantine plus unresolved
in-flight state. Retiring/rebinding a thread or runtime home never clears the
lineage. Authoritative terminal-completion evidence closes the lineage without
replacement; acceptance without terminal completion remains quarantined.
Ledger-stored authoritative `not_accepted` evidence may be consumed once to
create the next same-lineage generation. Without either terminal/not-accepted
evidence, the lineage remains blocked even if the old target is retired.

`reconciling` can become `delivered` from authoritative terminal-completion
evidence or `retry_wait`/a recorded replacement from authoritative not-accepted
evidence. Acceptance without terminal completion keeps both quarantines.
Operator dead-letter disposition keeps target and lineage quarantine and blocks
future turns. Dead-letter acknowledgement, target retire/rebind, or narrative
rationale never clears quarantine or permits a duplicate.

Dead letters are typed. `dispatch_failure` has no generic replay operation; a
replacement requires ledger-stored authoritative `not_accepted` evidence tied
to the exact attempt and remains in the same lineage. `acceptance_unknown` and
unresolved quarantine are never replayable. `invalid_observation` correction
uses one of two explicit operations:

- Same-subscription `reprocess-observation` requires a new immutable revision
  that changes only registry-declared mutable config or policy and retains
  subscription mode and the exact dispatch-profile, adapter, handler, and
  capability-profile identities/versions, target, source kind, and source
  identity.
- A subscription-mode, dispatch-profile, adapter, handler, capability-profile,
  version, target, source-kind, or source-identity change requires cancel plus
  create and `cross-subscription-reprocess`. That
  operation links the original dead letter and source subscription/revision to
  the new subscription/revision and requires trusted registry evidence that the
  new adapter can parse the bounded old evidence format. Without compatibility,
  the new subscription waits for a fresh source observation.

Both operations create a fresh observation linked to the dead letter and an
immutable `observation_reprocesses` record, then pass normal envelope validation,
classification, project/origin/event dedupe, and cursor/replay policy. For a
Phase 2 `observation_only` target, the transaction may write only the event,
checkpoint, bounded audit, and optional `simulation_projections` row; it MUST
NOT read or write `delivery_lineages` or any other dispatch table. Lineage
dedupe applies only when the target is a separately created Phase 3
`delivery_capable` subscription and the evidence becomes a fresh observation
under its frozen dispatch profile and active delivery flags. Neither operation
inherits delivery, attempt, retry, lineage, lease, acceptance,
target-quarantine, or lineage-quarantine state.

### Planned dead-letter record

```json
{
  "dead_letter_id": "UUID",
  "kind": "dispatch_failure",
  "subscription_id": "UUID",
  "subscription_revision": 1,
  "event_id": null,
  "delivery_id": "UUID",
  "lineage_id": "UUID",
  "acceptance": "acceptance_unknown",
  "authoritative_evidence_attempt_token": null,
  "authoritative_evidence_ref": null,
  "authoritative_evidence_hash": null,
  "target_quarantined": true,
  "lineage_quarantined": true,
  "reprocess_allowed": false,
  "reprocess_mode": null,
  "reprocess_target_subscription_id": null,
  "reprocess_target_revision": null,
  "acknowledged_at_utc": null
}
```

For a dispatch replacement, `acceptance` must be `not_accepted` and all three
authoritative evidence fields must resolve the matching immutable attempt; the
transaction consumes that evidence once and creates one next lineage generation.
For `invalid_observation`, delivery/lineage/acceptance fields are null.
`reprocess_allowed` becomes true only after the runner validates either a
same-subscription mutable-policy revision or a cancel/create replacement with
registry compatibility. `reprocess_mode` is then `same_subscription` or
`cross_subscription`, and the target fields identify the exact immutable
revision. The immutable `observation_reprocesses` row also stores the source
subscription/revision, dead-letter ID, compatibility evidence, resulting fresh
event ID, actor/time, and result; it never stores or inherits dispatch state.
Acknowledgement changes visibility only and never changes either eligibility or
quarantine.

### Planned supporting tables and transaction rule

The ledger also requires `runner_instances`, `subscription_revisions`,
`source_checkpoints`, `simulation_projections`, `delivery_lineages`,
`delivery_attempts`, `thread_quarantines`, `lineage_quarantines`,
`dead_letters`, `observation_reprocesses`, `subscription_activation_links`,
`audit_log`, and `schema_migrations`, all with their applicable foreign keys.
Every observation transaction inserts/deduplicates a changed event, classifies
it, advances the checkpoint, and writes bounded transition audit in one
`BEGIN IMMEDIATE` transaction. An unchanged semantic quiet observation advances
only checkpoint/counters without event/audit insertion.

The same transaction is phase-aware. In Phase 2 it may additionally write only
a permanently non-deliverable `simulation_projections` row; it MUST NOT read or
write `delivery_lineages`, `deliveries`, `delivery_attempts`, `thread_leases`,
`thread_quarantines`, or `lineage_quarantines`, nor create retry, lease,
acceptance, idempotency, or other later-promotable dispatch state. In Phase 3 or
later, lineage/delivery derivation requires a separately created
`delivery_capable` subscription with a frozen dispatch profile, the applicable
delivery flags, and a fresh adapter observation under that subscription. Phase
2 events/projections are never selected, converted, copied, claimed, migrated,
or promoted. `start_now` begins from the new checkpoint; an approved replay
must re-observe the source from the adapter-approved cursor under the new
subscription and pass normal validation, classification, project/origin/event
and lineage dedupe. Delivery claim,
pre-send/quarantine authorization, lease renewal, and fenced result commit are
separate short transactions around external work.

The exact-thread dispatcher is not part of Phase 2. A SQLite authorizer/trace
must prove Phase 2 observation and reprocessing do not read or write
`delivery_lineages` or any other dispatch table. Phase 2 proof also requires
every observation, checkpoint, and audit row and query for two registered
projects in one workspace ledger to remain isolated by exact workspace,
project, source, and registry revision. V3 provenance is instead scoped by
workspace and registry revision; each row is either bound to one exact
registered project or is `legacy_unscoped` with a `NULL` project, and
exact-project projections exclude legacy-unscoped rows. Phase 2 rejects
unregistered, empty, null, traversal-like, and foreign project scope,
arbitrary path overrides, and cross-project reads or writes. Proof also requires zero new
lineage, delivery, attempt, dispatch-lease, or target/lineage-quarantine rows
after timer/filesystem/mailbox observation and reprocessing, plus proof that
simulation rows have no claim/promotion path. Phase 3 first proves an
observation-only subscription cannot gain delivery capability by revision;
activation cancels it, creates a separate delivery-capable subscription with a
frozen dispatch profile, records only an audit successor link, and requires
explicit `start_now` or adapter-approved replay-cursor selection. It then proves
that only a fresh observation under the new subscription can create delivery
state before running non-send contract tests and using only the test-mode-only
`THREAD_EVENT_RUNNER_TEST_DISPATCH_DISPOSABLE_RUNTIME` gate for an isolated
fault matrix and one disposable subscription. Production/project
`THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD` remains off until the later project
pilot gate. Phase 2 is limited to this SQLite ledger and read-only
timer/filesystem/mailbox observation.

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
| `project_id` | string | Exact non-empty project identifier. Queue transition mutators refuse a missing, empty, null, or foreign value before changing lane, completion, archive, or synced state. |
| `last_updated_utc` | string | ISO 8601 timestamp |
| `source_issue` | int | Workflow issue tracking this queue contract |
| `source_task` | string | Local task mirror tracking this queue contract |
| `invalid_lanes` | object[] | Reconcile diagnostics for task mirrors rejected by project policy. A `ui_ux.direct_app_only` violation keeps the result non-OK and the lane blocked instead of ready. |
| `completed_recently` | object[] | Optional recently-finished reference lanes. Dependency reconciliation trusts only `status: done` entries from a queue whose exact `project_id` matches the requested project. |
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
| `depends_on` | string[] | Task-level dependencies. A dependency is satisfied only by exact-project `status: done` task evidence or exact-project persisted done history; global same-ID foreign/projectless mirrors are not authority. |
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
  sends — ONLY when `ax_attended_only` is not `true` — `deliver.py` reports
  `ax_doorbell_required` and prints the `bin/axsend-ensure ring --submit
  --verify` command (from the llm-collab checkout root) the sender should run.
  An `ax_attended_only: true` target (opaque composer, e.g. ZCode) instead
  reports `ax_attended_recovery_required` with the Codex-attended recovery
  instruction (GH-1547); no routine ring command is ever printed for it.
- `codex -> codex` is the sender-aware exception. `deliver.py` preserves the
  durable packet, reports `thread_coordination_required: true`, and suppresses
  dispatchable-runtime, AX, desktop-bridge, and operator-relay activation so a
  managed worker can be inspected with `read_thread` and unblocked with
  `send_message_to_thread`. Its message frontmatter records
  `autobridge_skip: true`, `autobridge_skip_reason: codex_self_target`, and a
  null `target_session_id`. Session dispatch excludes every `from: codex` /
  `to: codex` packet, including durable packets created before the flag existed,
  records a `codex_self_target_thread_coordination` skip, and leaves the packet
  unread.
- A terminal-only `cli_session` needs a dispatchable runtime session. Without
  either transport, `deliver.py` reports `activation_unavailable` instead of
  silently requesting operator relay.
- A project may enable `claude_desktop_bridge` for a non-CLI Claude target. Only
  that fallback reports `desktop_bridge_required` and uses Computer Use; it does
  not override AX routing for a Claude agent registered as `cli_session`.
