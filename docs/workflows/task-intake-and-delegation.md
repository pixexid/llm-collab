# Task Intake And Delegation

## Goal

One implementation owner, one scope, one verification plan.

### Roles (worker-agnostic)

Describe responsibilities as roles, not hardcoded people. The same agent may hold
different roles on different lanes, and assignment follows task fit, current
context, and operator direction.

- **Queue owner / status mutator** — owns the canonical queue order, activation,
  and status transitions. In Amiga this defaults to **Codex** because the queue
  tooling and status mutation currently run there; this is a tooling constraint,
  not a hierarchy. If the tooling later supports another owner, the role moves
  with the tooling.
- **Planner / refiner** — owns spec, acceptance criteria, risk analysis, and
  phase/sequencing. The refinement gate requires `refined_by: claude` (see
  Planning And Acceptance Gate).
- **Implementer** — owns the diff in the assigned worktree. Roles are per-lane:
  by skill, backend lanes lean Codex and frontend/UI-UX lanes lean Claude, but
  either agent may implement either side when the task fits. There is exactly one
  writer per lane/worktree.
- **Reviewer / gate** — independent acceptance. The implementer never solely
  approves their own lane; the other agent reviews it. Planning-phase cross-review
  is mandatory for non-trivial lanes (a bad plan is the highest-cost failure) and
  a pre-merge second-eyes pass is mandatory on implementation.

For Amiga work, use at most one Codex-managed internal subagent for a task. Do
not stack several Codex-managed subagents on the same implementation lane.
External collaborators do not count against that internal subagent limit.
Codex-owned implementation should use managed Codex Thread Coordination workers
by default. Native subagents are focused local support lanes for review, repo
mapping, docs sync, verification, and recovery. `cdx2` is disabled legacy
routing for Amiga; use it only when the operator explicitly re-enables cdx2 for
one specific task.

The one-writer rule still applies: do not make two agents (or an agent and a
Codex-managed internal subagent) implementation writers for the same files in the
same task. Other workers on that lane are reviewers/advisors unless scopes are
explicitly disjoint.

## Intake order

1. pass startup/preflight
2. create or identify the chat
3. read the project queue artifact when the project maintains one
4. create/update the task
5. run the Claude planning/refinement gate for non-trivial tasks (see
   Refinement Gate below)
6. update the queue when owner/order/dependency/activation state changes
7. provision branch/worktree first when the lane is isolated-worker implementation
8. assign one implementation owner
9. send one clear delegation message
10. move task to `in_progress` (gated — requires `refined_by: claude` or `skip_refinement: true`)
11. then activate the assigned worker directly through the approved mailbox + doorbell path
12. then begin implementation

For Codex-owned implementation, the implementation owner is a managed Codex
Thread Coordination worker. Use Thread Coordination when that owner needs a
durable background Codex thread:

1. create one managed Codex thread for the task/worktree/branch
2. inspect progress with `read_thread`
3. send only focused unblocks with `send_message_to_thread`
4. record progress, blockers, evidence, and handoff state back to the
   `llm-collab` task/chat

Thread Coordination is an execution surface, not a queue source of truth.

## Canonical ordered queue

If the project defines a canonical queue artifact, treat it as the ordered source of truth for
remaining issue-sized lanes.

- keep the queue outside chat threads
- read it during fresh-session recovery before selecting the next lane
- update task mirrors and GitHub issue state, then run `python3 bin/project_issue_queue.py reconcile --project <project_id> --write` to refresh the runtime queue projection
- do not hand-edit queue state to clear blockers or materialize lanes unless repairing a reconcile failure with an explicit note
- if `claim_task.py --status in_progress` targets a queued lane that is not `ready`, the transition should fail unless an explicit queue-override flag is used

## Autonomous queue loop

When the operator gives standing instructions to keep processing tasks, the
orchestrator should run a persistent queue loop instead of treating each PR or
worker wait as the end of the thread. Record the loop with:

```bash
python3 bin/autonomous_loop.py start --project <project_id> --agent codex --mode next_lane
```

Each loop pass must recover all live coordination inputs before deciding the
next action:

- `session_bootstrap.py --agent codex`
- `inbox.py --me codex --project <project_id> --limit 5 --peek` or the
  project-approved unread check
- canonical issue/design queue validation
- `project_design_queue.py bridge-status --project <project_id> --json` when a
  design/Claude Desktop lane may be active
- active task mirrors and worker checkpoint status
- active PR checks, merge state, full comments/reviews/threads, and branch
  freshness

The loop may stop only when one of the recorded stop conditions is true:

- `operator_interrupt`: the operator changes direction or asks to stop
- `queue_empty`: queue validation confirms no remaining ready/active/review lane
- `true_external_blocker`: the next step needs unavailable credentials, product
  direction, destructive approval, or an unreachable required UI

Do not stop on these states:

- a worker reported `blocked`, if Codex can update the task, issue, branch, PR,
  brief, or verification failure and continue
- a PR is green but waiting for a GitHub Codex review artifact that may never
  arrive, after local/orchestrator review and the configured PR heartbeat policy
  are satisfied
- a workflow/process diff exists; classify it into its own PR or an explicit
  bundle before the next lane

`llm-collab` messages are part of the loop, not a side channel. Before sending a
worker follow-up, update the task/issue if scope changed, write one consolidated
message, and use the approved worker bridge. For any `cli_session` worker,
including Claude, use the AX command printed by `deliver.py`. Use Computer Use
only when `deliver.py` reports the project-configured non-CLI
`desktop_bridge_required` fallback. Record a failed bridge precisely; do not use
routine operator relay as the first fallback.

There should be one active queue-runner heartbeat for a project loop. A
task-specific heartbeat may exist only as a child wait for Claude, a worker
handoff, or a PR review/check state. Child heartbeats must name the current
task/PR and update or delete themselves when the loop mode changes, so stale
heartbeats cannot collide with the queue runner.

### Design-first lane precedence

Design-first work uses the canonical runtime queue with a design `lane_type`.
Do not create a second backlog in `design-queue.json`; a local empty design
queue is not proof that GitHub-backed work is empty.

Before activating code implementation:

- run `python3 bin/project_issue_queue.py reconcile --project <project_id> --write`; if it reports `needs_materialization`, duplicate mirrors, DRIFT, or `backlog unknown`, repair/report that queue state before activation
- run `python3 bin/project_issue_queue.py validate --project <project_id>` and treat DRIFT or unknown GitHub backlog state as a blocker
- keep only the earliest unblocked design dependency in `ready`; backend or runtime implementation lanes stay `queued` or `blocked` until their design dependency is done
- use `lane_type` values such as `design`, `design-surface-spec`, `design-handoff`, or `design-template` to filter design views from the single issue queue
- keep legacy `project_design_queue.py` usage limited to existing design-queue migrations and Claude Desktop bridge metadata until those projects are converted
- when migrating an existing `design-queue.json`, copy active design lanes into `issue-queue.json`, preserve their `lane_type` and dependencies, validate the single queue, then archive the old design queue

For design lanes that depend on accepted surface specs or handoffs that may not
yet be on the default branch, add a machine-readable materialization gate to the
task frontmatter before activation:

```yaml
dependency_materialization_gate: true
required_dependency_artifacts: ["design/surfaces/notifications.md", "design/handoff/notifications-HANDOFF.md"]
```

When such a lane is `ready`, `active`, or `review`, `project_design_queue.py
validate` checks the assigned `worktree` for those files. A missing file is an
activation/base-branch blocker, not a product gap for the worker to rediscover
or recreate.

If a broad issue mixes design and code, split it into a design task first and create the implementation task only after the design handoff is accepted.

## Preflight gate split

- task-claim preflight (`claim_task.py` to `in_progress` or `review`) is a tooling/env gate, not a browser gate
- `claim_task.py` appends `--browser-check skip` when it runs project preflight
- browser validation should run later only for runtime/UI-impact lanes

## Python Runtime

Use `/Users/pixexid/Projects/llm-collab/bin/llm-collab <script>.py ...` for
llm-collab task, inbox, queue, and contract commands. The launcher resolves a
Python 3.10+ interpreter before running the target script, which avoids macOS
environments where bare `python3` or `#!/usr/bin/env python3` can resolve to the
system Python 3.9. Direct script entrypoints also fail fast with a clear version
message if they are run under an incompatible interpreter.

## Planning And Acceptance Gate

Claude is the designated planning/refinement collaborator for non-trivial
tasks. `claim_task.py` blocks any `open → in_progress` transition unless the
task frontmatter contains `refined_by: claude` or `skip_refinement: true`.
When Claude both creates and plans a task, `claim_task.py` also requires
`accepted_by: codex` before activation.

The gate is a machine contract, not a requirement to open a separate refinement
thread. Prefer the Claude thread that already holds the relevant context:

- use the same Claude thread for the same task, same surface, blocker repair,
  review-fix loop, or continuation of the same planning chain
- ask Claude to create or update the task, GitHub issue, acceptance criteria,
  and risk analysis directly when that thread has the needed context; use
  `new_task.py`, never hand-author a task file
- set `refined_by: claude` from any real Claude planning/refinement pass, even
  when it happened inside the existing context-holding thread
- open a fresh Claude thread only for a genuinely new context, a full/corrupted
  thread, a needed cold-read independence check, or a task that cannot safely
  continue in the old thread
- keep Codex as the independent acceptance gate: Codex validates queue state,
  de-duplicates scope, checks blockers/frontmatter, and controls status
  transitions

**Standard flow (non-trivial tasks):**
1. Orchestrator creates task with `new_task.py` (status: `open`, `refined_by: null`)
2. Orchestrator fills or requests enough context for the task's `## Implementation Risk Analysis` section; this is required for Codex-created tasks too, not only Claude refinement
3. Orchestrator sends or records the planning/refinement request in the
   context-holding Claude chat when one exists; otherwise create a fresh Claude
   chat with task ID, file path, research docs, GH issue, and the required
   implementation-risk checklist
4. Claude reviews current files/topology, patches or authors the task and GH issue, completes `## Implementation Risk Analysis`, then runs:
   ```bash
   /Users/pixexid/Projects/llm-collab/bin/llm-collab plan_task.py --task TASK-... --note "..."
   ```
   `refine_task.py` remains the same validation path and may still be used.
5. Claude replies in the linked chat confirming refinement is done and calls out
   any cross-surface context it used
6. Orchestrator confirms `refined_by: claude` in the frontmatter and checks the risk analysis for unresolved blockers
7. If `created_by: claude` and `refined_by: claude`, Codex performs an independent acceptance read and activates with `claim_task.py --accepted-by codex`; otherwise Codex proceeds to activation normally

## Worker-owned follow-up capture

When a worker discovers new implementation scope, parity gaps, design-doc drift,
DB follow-up, or tooling repairs from direct rendered/code context, the worker
who found the gap owns the first durable capture. Do not route rich findings
through a short chat note and ask the orchestrator to reconstruct them later.

For Claude UI/UX and D8 lanes, this is mandatory:

- Claude creates or updates the GitHub issue and local task mirror from its own
  context via `new_task.py`, then links both from the active task and handoff.
- If an existing issue/task already owns the gap, Claude links it and records
  the disposition instead of creating a duplicate.
- If Claude lacks a required credential or command capability, Claude writes a
  complete issue/task draft artifact with title, body, labels, dependencies,
  evidence, acceptance gates, and queue placement recommendation, then hands
  that artifact to Codex for mechanical creation only.
- Codex validates the created/drafted issue/task against the source evidence,
  queue order, and task-contract gates before activation. For Claude-authored
  and Claude-planned tasks, Codex records `accepted_by: codex` during activation
  only after that read. Codex may discuss or request corrections from Claude,
  but should not be the first author of Claude's detailed finding unless Claude
  is blocked.

Every created follow-up must preserve the original evidence trail: source task,
source chat, affected route/component/state, D8 finding/disposition, browser or
DB evidence, operator feedback status, and whether the follow-up blocks later
route work.

**Implementation Risk Analysis (hard gate):**

Every non-trivial task must carry a completed `## Implementation Risk Analysis` section before it can be marked planned/refined or activated. `plan_task.py`/`refine_task.py` refuses to set `refined_by: claude`, and `claim_task.py --status in_progress` refuses activation, unless the section exists and these labels have real values:

- `Current file/topology reviewed:` exact files/directories inspected and whether the task plan matches the current repo shape
- `Scope split decision:` keep as one lane, split now, or explicitly defer a sub-lane; include why
- `Estimated diff/risk:` expected diff size, risky surfaces, and reviewability concerns
- `Verification/browser/sign-off plan:` concrete verification, browser, DB, UI, or operator sign-off mechanics
- `Open decisions/blockers:` decisions that must be resolved before activation, or `none`

This requirement applies in two places:

- Codex/orchestrator task creation must include the section with enough initial assessment that Claude can verify or correct it.
- Claude planning/refinement must validate and complete the section before marking the task refined.

For UI/UX implementation lanes, refinement must also seed D8 design-thinking-in-details work:

- frontmatter `design_thinking_polish_budget_loc`: positive integer, usually ~10–20% of the expected implementation LOC
- frontmatter `design_thinking_polish_seeds`: at least 2 surface-specific vectors
- risk-analysis line `Design thinking in details — polish-pass budget:`
- risk-analysis line `Design thinking in details — polish vectors:`

Docs-only UI/UX lanes do not need D8 pass items unless they also change rendered UI, but they still need an explicit browser-validation skip reason in the review evidence.

Do not hide implementation risks in chat only. If a risk changes lane size, acceptance criteria, activation order, worker ownership, or sign-off mechanics, update the task contract before activation.

**Bypass (trivial/hotfix tasks only):**
```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab new_task.py \
  --title "..." --created-by codex --project amiga --skip-refinement
```
Sets `skip_refinement: true` at creation. Use only for tasks with obvious, single-file scope where a spec review adds no value.

**Verify planning/refinement status:**
```bash
grep refined_by /Users/pixexid/Projects/llm-collab/Tasks/active/<task-file>.md
```

**Accept a Claude-authored and Claude-planned task for activation:**
```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab claim_task.py \
  --task TASK-... \
  --owner claude \
  --status in_progress \
  --accepted-by codex \
  --accepted-note "Reviewed source evidence, queue order, blockers, and task contract"
```

Use `--allow-self-plan` only for an explicitly approved solo recovery case; the
override is logged in the task frontmatter.

## Required task fields

- `task_id`
- `title`
- `status`
- `owner`
- `created_by`
- `requested_by`
- `priority`
- `project_id`
- `related_chat`
- `related_paths`
- `skip_refinement` (bool — set at creation; `false` by default)
- `refined_by` (null until claude marks it)
- `refined_at` (null until claude marks it)
- `planning_mode` (`authored` when Claude created the task, `refined` when Claude refined another agent's task)
- `accepted_by` (required as `codex` before activation only when `created_by: claude` and `refined_by: claude`)
- `accepted_at`
- `## Implementation Risk Analysis` body section with the required labels above for every non-trivial task

For UI/UX lanes, also require:
- `ui_ux_lane: true`
- `ui_ux_mode: implementation | docs_only`
- `required_design_docs`
- `required_design_skills`
- `impeccable_commands_required`
- `impeccable_required: true`
- `impeccable_antipatterns_enforced: true`
- `design_doc_update_review_required: true`
- for `ui_ux_mode: implementation`: `design_thinking_polish_budget_loc` and at least 2 `design_thinking_polish_seeds`

For DB lanes, also require:
- `db_impact: none | local-schema-only | shared-supabase-required`
- `db_impact_detection`
- `db_impact_detection_reasons`
- for `shared-supabase-required`: `db_project_ref` and `db_required_surfaces`

Use the contract helper instead of hand-editing guesses:

```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab task_contract.py sync --task TASK-xxxxxx --write
```

If a lane should be forced on/off instead of auto-detected:

```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab task_contract.py sync --task TASK-xxxxxx --ui-ux-lane true --write
```

DB clarification:
- if a lane touches the Amiga shared Supabase schema or depends on shared DB state, do not treat a separate “local DB” as the acceptance database
- the acceptance database is the shared/live Amiga Supabase project
- workers must use the CLI + `supabase_amiga` MCP workflow instead of guessing from migration files alone
- Supabase MCP privileges are account-scoped. Preflight the owning worker's own
  `supabase_amiga.get_project`, safe read-only `execute_sql`, and required
  `get_advisors` call before handoff for `shared-supabase-required` work.
- if that worker receives a Supabase access-control error, record it and stop;
  the remediation is operator/admin Supabase project or org access for that
  account. Do not silently continue with a service-role key.
- fallback order for DB proof is privileged `supabase_amiga` MCP, then linked
  Supabase CLI when local auth or `SUPABASE_DB_PASSWORD` is configured, then
  explicitly recorded service-role read-only assertions as the last resort.

## Delegation message requirements

- exact goal
- files/modules in scope
- docs to read first
- success criteria
- explicit non-goals
- task id
- verification commands
- handoff format (files changed, commands run, verification result, blocker/ready)

When isolated worktrees are used, include:
- exact worktree path
- branch
- base ref and base SHA
- allowed workspace
- explicit checkpoint-commit requirement for worker-owned implementation lanes
- required handoff evidence for acceptance:
  - checkpoint commit SHA
  - assigned branch confirmation
  - `git status --short --untracked-files=all`
  - disposition of any remaining tracked or untracked files

For worker-owned isolated lanes, those values must be provisioned and verified by the orchestrator before worker activation.
Do not phrase a planned branch/worktree as already assigned.

For UI/UX implementation lanes, the delegation brief must also name:
- required design docs to read first, including `DESIGN.md`
- required Impeccable-family skill usage (`required_design_skills: [impeccable]`)
- planned Impeccable steering commands for the lane
- the requirement to enforce Impeccable curated anti-patterns
- the D8 design-thinking-in-details budget and seeded polish vectors from the task contract
- the mandatory `pnpm ui:impeccable:detect -- <paths>` step
- the exact browser-validation expectation
- the requirement for a handoff `Design-thinking pass` section with at least 3 findings and dispositions
- the requirement to record UI evidence back onto the task contract before moving to `review`

For `shared-supabase-required` lanes, the delegation brief must also name:
- the required `db_impact` classification and shared project ref
- the requirement to use both Supabase CLI and `supabase_amiga` MCP surfaces
- the required shared-project apply + schema assertion step
- the requirement to record DB evidence back onto the task contract before moving to `review`
- the worker-account MCP preflight and the exact fallback/remediation path for
  access-control failures

Canonical UI evidence recording command:

```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab task_contract.py record-ui-evidence \
  --task TASK-xxxxxx \
  --design-docs-read /Users/pixexid/Projects/amiga/docs/ui_ux/DESIGN.md \
  --design-skills-used impeccable \
  --impeccable-commands-used /impeccable\ craft,/polish \
  --impeccable-detect-result "pass: pnpm ui:impeccable:detect -- src/routes/app/bookings.index.tsx" \
  --browser-validation-desktop "pass: /app/bookings desktop" \
  --browser-validation-mobile "pass: 393px no overflow" \
  --operator-visual-feedback-requested true \
  --design-doc-update-decision "reviewed; no DESIGN.md diff required"
```

Canonical DB evidence recording command:

```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab task_contract.py record-db-evidence \
  --task TASK-xxxxxx \
  --db-impact shared-supabase-required \
  --db-project-ref wbqjeasgxakubqcutgjt \
  --db-migration-files db/migrations/20260417_example.sql \
  --db-apply-result "pass: supabase db push --linked" \
  --db-schema-assertion "pass: execute_sql confirmed expected shape" \
  --db-advisors-result "pass: get_advisors returned no blocking advisors" \
  --db-runtime-validation "pass: exercised affected route against shared Supabase"
```

## Activation rule

When multiple workers are involved, state activation order explicitly:

- who should act now
- who should wait
- what condition triggers next activation

## Activation enforcement (hard rule)

Do not activate workers that are not ready to start. Activation is queue-owner
controlled and happens by directly activating the assigned worker through the
approved mailbox + doorbell path after the gates pass — not by asking the
operator to relay.

- Activate only workers in `in_progress` state that should execute now.
- A worker is not ready to start until its required branch/worktree already exists when isolated mode is expected.
- For queued workers, update task ownership/status and keep instructions in task/chat, but do not ring/activate them yet.
- When a queued worker becomes ready, send one activation message (mailbox packet) and ring it via the doorbell.

Required activation-state wording:

- single activation: `activate <worker> now`
- parallel activation: `activate <worker-a> + <worker-b> now in parallel`
- queue-only instruction: `do not activate yet; waiting on <condition>`

These describe the queue-owner's recorded activation intent; the worker is then
activated directly via mailbox + doorbell, not via an operator paste/relay.

Never ring/activate multiple workers without explicit activation order.
If order is sequential, activate only the first and wait until the trigger condition is met before activating the next.
