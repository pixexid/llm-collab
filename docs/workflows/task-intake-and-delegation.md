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

BB is the normal provider-backed worker surface. Follow
[`bb-workers.md`](bb-workers.md) for isolation, spawn, communication,
inspection, and completion proof rather than copying its commands here. A BB
worker is not an llm-collab participant or mailbox author; the orchestrator
remains the integration point.

For Amiga work, use at most one Codex-managed internal subagent for a task. Do
not stack several Codex-managed subagents on the same implementation lane.
External collaborators do not count against that internal subagent limit.
For Amiga work, do not stack several BB workers on the same implementation
lane.

The former Codex Thread Coordination implementation route is dormant. Its
superseded instruction is retained for audit only: "Codex-owned implementation
should use managed Codex Thread Coordination workers by default." Do not follow
that instruction; use BB for the implementation worker. Native subagents remain
focused local support lanes for review, repo mapping, docs sync, verification,
and recovery. `cdx2` is disabled legacy routing for Amiga; use it only when the
operator explicitly re-enables cdx2 for one specific task.

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
6. classify the review tier; for Tier A, write the lane contract before any branch
7. update the queue when owner/order/dependency/activation state changes
8. provision branch/worktree when the lane is isolated-worker implementation
9. assign one implementation owner
10. send one clear delegation message
11. move task to `in_progress` (gated — requires `refined_by: claude` or `skip_refinement: true`)
12. then activate the assigned worker directly through its approved transport
13. then begin implementation

For a BB worker, step 8 uses `bb-workers.md` to obtain and verify the managed
worktree. Step 10 freezes the delegation without starting the worker; step 12
starts it through BB only after the `in_progress` gate passes.
For a Claude-side manual lane or another lane not owned by BB, step 8 uses
[`isolated-worktrees.md` → Choose the isolation owner](isolated-worktrees.md#choose-the-isolation-owner).
That section routes BB-managed lanes back to `bb-workers.md`; the hand-managed
local-worktree and mailbox activation sequence does not apply to a BB thread.

Dormant Codex Thread Coordination mechanics are retained below for audit only.
They must not be executed:

1. create one managed Codex thread for the task/worktree/branch
2. inspect progress with `read_thread`
3. send only focused unblocks with `send_message_to_thread`
4. record progress, blockers, evidence, and handoff state back to the
   `llm-collab` task/chat

Thread Coordination was an execution surface, not a queue source of truth.
It has been superseded by BB.

BB is an execution surface, not a queue source of truth. If a result needs a
durable llm-collab record, the orchestrator reads and verifies the BB result,
then authors that record under its own registered identity. Do not make the BB
worker impersonate a bus participant.

## Lane WIP limit

At most **two writing lanes** are active at once across the workspace. A writing
lane has a designated writer, a branch, and a deliverable that changes files or
other persistent project artifacts. It is active from that assignment until its
PR merges or its terminal disposition is recorded.

A read-only lane delivers evidence or advice only, with no implementation branch
and no designated writer; audits, probes, scoping, and reviews do not count
against the cap. If its assignment authorizes a write, classify it as a writing
lane before activation.

The scarce resource is **orchestrator verification capacity**, not worktrees.
Two is the judgment of how many writing lanes one orchestrator can independently
verify without rubber-stamping; rubber-stamping makes a clean verdict stop
meaning anything. Before activating a third writing lane, one of the two must
ship, merge-with-followups, or be closed by the lane owner and release-gate
worker.

[One-writer-per-lane](../../AGENTS.md#one-writer-per-lane) is a separate,
unchanged rule. It did the concrete collision-prevention work by keeping PRs
#605 and #607 off the same file.

Fragments spawned by a capped PR (descope/split/backend-first children) do not
enter the board automatically: they go to triage, and each is activated only
when a writing-lane slot is free and — for Tier A — it carries the lane contract
its parent lacked. A split, successor PR, or new child issue is not progress
unless it shortens the path to `main`; count it as the same lane until it proves
otherwise.

## Canonical ordered queue

If the project defines a canonical queue artifact, treat it as the ordered source of truth for
remaining issue-sized lanes.

- keep the queue outside chat threads
- read it during fresh-session recovery before selecting the next lane
- update task mirrors and GitHub issue state, then run `python3 bin/project_issue_queue.py reconcile --project <project_id> --write` to refresh the runtime queue projection
- configure any project-specific class ordering with the ordered `github.backlog.priority_labels` list in that project's `projects.json` entry; missing or empty keeps issue-number order, and reconciled lanes expose the matched label and rank
- do not hand-edit queue state to clear blockers or materialize lanes unless repairing a reconcile failure with an explicit note
- if `claim_task.py --status in_progress` targets a queued lane that is not `ready`, the transition should fail unless an explicit queue-override flag is used

### Issue state labels

The `ENFORCED`, `CHECKED-CONVENTION`, and `JUDGMENT` classifications below
apply only to this subsection; no prose elsewhere in this document is
classified by implication.

- **ENFORCED — queue eligibility.** `epic` and `state:parked` are a
  non-removable exclusion floor at reconciliation and exact-issue activation.
  Project configuration may add exclusions but cannot remove the floor.
- **ENFORCED — state schema at execution boundaries.** An open issue must carry
  exactly one of `state:active`, `state:blocked`, or `state:parked`. Missing,
  multiple, and unknown `state:*` labels make reconciliation invalid and refuse
  activation. The exact activation check separately requires an open,
  non-pull-request record before classifying its labels; closed issues and pull
  request numbers produce distinct typed refusals. `epic` is orthogonal and
  never substitutes for a state label.
- **CHECKED-CONVENTION — proactive label hygiene.** Run
  `python3.11 bin/audit_issue_states.py --project "$COLLAB_PROJECT_ID"` at
  orchestrator succession and during the parked sweep. The queue and activation
  gates remain the safety controls if this audit is missed.
- **ENFORCED — blocked state.** `state:blocked` adds
  `github:state:blocked` to queue blockers and refuses new activation. It never
  clears a blocker owned by task or dependency evidence, remains blocked across
  local task-status transitions, and `state:active` clears only the GitHub-owned
  blocker after fresh reconciliation.
- **JUDGMENT — parking quality and disposition.** The parked listing is review
  input. No command decides whether a trigger is meaningful or fired, or
  whether close-or-recommit is the right decision.

## Autonomous queue loop

When the operator gives standing instructions to keep processing tasks, the
orchestrator should run a persistent queue loop instead of treating each PR or
worker wait as the end of the thread. Record the loop with:

```bash
python3 bin/autonomous_loop.py start --project <project_id> --agent codex --mode next_lane
```

Each loop pass must recover all live coordination inputs before deciding the
next action:

This recurring recovery assumes the initial watcher enablement in
[`Session Startup`](session-startup.md#bootstrap-first), including its canonical
PM2 rotation gate, is already complete.

- `current_runtime.py --agent codex`
- `inbox.py --me codex --project <project_id> --limit 5 --peek` or the
  project-approved unread check
- canonical issue/design queue validation
- active task mirrors and worker checkpoint status
- active PR review state, merge state, branch freshness, and the full reviewed
  artifact set (`commit-push-prs.md#reviewed-artifact-set`)

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
message, and use the approved worker bridge. For OpenAI-model interaction use BB
unless the task needs a Codex-app-only tool that BB cannot reach. Only after
that condition is met may a Codex recipient use the AX command printed by
`deliver.py`; the printed command does not itself satisfy the condition. A
supported `ax_attended_only` target reports
`ax_attended_recovery_required` instead — route control to Codex-attended
recovery, never a routine ring. A terminal-only
CLI worker needs a dispatchable runtime session. Every watcher-backed worker, Codex
included, is woken by its durable packet and its own watcher **whenever its
binding dispatches** (`autobridge_ready: true`); only when `deliver.py` reports
`ax_doorbell_required: true` instead does a Codex packet take the AX fallback.

Do not read `watcher_pickup_ready` as the signal for that. It is retained for
compatibility but is false for new admitted deliveries. A healthy bound delivery
reports `autobridge_ready: true` with `watcher_pickup_ready: false`; an unbound
watcher-backed recipient is refused before write. The only admitted unbound route
is an explicit `routing_mode: broadcast` for the operator or a watcher-disabled
human/human relay. Treat a typed routing refusal as a configuration blocker,
record its exact reason, and do not misreport it as routine operator relay.

For a BB worker, the approved bridge above is BB itself: update the task or
issue if scope changed, then communicate through `bb-workers.md`. The mailbox
and wake details above apply only to an explicitly registered first-class
participant.

Contract v12's predicate remains `wake_fallback_allowed = not autobridge_ready
and not dispatch_scope_refused`. AX is not a routine lane or a BB transport,
and whether an offered doorbell can land is a live runtime property, never a
standing process or window-count fact. Follow `session-autobridge-runbook.md`
for receipt and recovery details.

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
- `claim_task.py` runs each project's complete registered `preflight_command` argv
  without appending cross-project flags; Amiga's browser-skip tokens, when required,
  belong in the Amiga registry entry.
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

## Done transition authority

New `done` transitions are release closure, not a worker completion shortcut.
The source status must be exactly `review`; `open`, `in_progress`, and
`blocked` refuse before task or queue mutation. The configured project
`release_gate_agent` supplies `--released-by`, and it must be an enabled exact
agent match. This identity check deters accidental or misrouted closure but is
not authentication.

Objective evidence is separate and cannot be replaced by the actor name. Pass
one strict JSON object through `--release-evidence` with a full 40-hex
`merge_sha`, terminal disposition, optional positive integer `run_id`, and
optional non-empty `note`. A `success` disposition requires `run_id` and a live
transition-time evaluation by `deploy_release_watch.py` using the configured
exact-SHA workflow/event/branch/jobs/smoke authority. The caller's run ID must
equal the evaluator-selected run ID. A watcher packet, saved artifact, stale
run, or run for another SHA is candidate context only and never a shortcut.

Projects without complete `release_closure` configuration fail `success`
closed. An absent or empty (`{}`) closure still permits an honest structured
`non-production` or `risk-accepted-followup` disposition. In contrast, any
truthy `release_closure` must be a complete valid closure contract: malformed
truthy configuration refuses all three verdicts before evaluator, task, or
queue mutation.

For a GitHub-enabled project with a configured repository, an honest
non-success record preserves that repository identity. Only the GitHub-disabled
case binds `repository: null`. Both forms omit caller-provided run IDs; only
transition-time evaluation can make a run ID authoritative.

Post-merge release handling has one strict order:

1. Evaluate the exact merge SHA through the configured release authority.
2. After terminal success or an explicit honest non-success disposition, move
   the task from `review` to `done`.
3. Only after the `done` transition succeeds, run post-merge cleanup.

A raw `PENDING`, `MISSING`, `FAILURE`, or `CANCELLED` evaluation is not itself a
terminal disposition. It preserves the task in `review` and preserves the
implementation lane; do not promote the task or clean the lane. Historical
tasks already marked `done` are grandfathered because the gate applies only to
new transitions. If the gate itself must be rolled back, revert its code/config
change; never bypass a refusal by moving or editing the task manually.

Inside step 2, `claim_task.py` first validates the target-state task contract at
stage `done`, before its release evaluator or any task/activity/queue mutation.
`success`, `non-production`, and `risk-accepted-followup` cannot bypass missing
shared-database evidence. `post_merge_cleanup.py` verifies already-closed task
state; it is not an alternate done-transition authority.

For UI/UX lanes, also require:
- `ui_ux_lane: true`
- `ui_ux_mode: implementation | docs_only`
- `required_design_docs`
- `required_design_skills` from the exact project's `ui_ux.required_design_skills` (or the exact Amiga fallback)
- `design_doc_update_review_required: true`
- for `ui_ux_mode: implementation`: `design_thinking_polish_budget_loc` and at least 2 `design_thinking_polish_seeds`

When the exact project's required design skills include `impeccable`, also require
`impeccable_commands_required`, `impeccable_required: true`, and
`impeccable_antipatterns_enforced: true`. Otherwise those Impeccable-specific
fields are false/empty and are not a gate.

For DB lanes, also require:
- `db_impact: none | local-schema-only | shared-supabase-required`
- `db_impact_detection`
- `db_impact_detection_reasons`
- for `shared-supabase-required`: `db_project_ref` and `db_required_surfaces`

If the task's exact project enables strict boolean
`db.production_schema_guard: true`, assignment/review/PR/done validation also
refuses schema work classified as `none`. `local-schema-only` is limited to
disposable development/test schema that will never reach shared or production
and requires `db_local_schema_only_exception: dev-only-non-production`,
`db_local_schema_only_exception_approved_by: operator` (or `supervisor`,
per the operator grant of 2026-08-10), and a non-empty
`db_local_schema_only_exception_reason`. Concrete `db/migrations/**` and exact
`db/schema.sql` paths cannot be hidden by `manual_false`; documentation-only
body matches can. Missing/false guard values preserve existing behavior, while
a present non-boolean fails closed at the exact project registry entry.

Use the contract helper instead of hand-editing guesses:

```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab task_contract.py sync --task TASK-xxxxxx --write
```

If a lane should be forced on/off instead of auto-detected:

```bash
/Users/pixexid/Projects/llm-collab/bin/llm-collab task_contract.py sync --task TASK-xxxxxx --ui-ux-lane true --write
```

DB clarification:
- the dev-only `local-schema-only` exception is a classification exception, not
  an evidence bypass; if the lane is `shared-supabase-required`, all existing
  ref, surface, migration, apply, assertion, advisor, and runtime evidence still
  applies
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

## Delegation is a frozen task, not a message

A question and a task delegation are both valid — different acts with different
replies — and the failure is mixing them or misreading which one came back. Ask
any worker a question freely; it answers. Delegate a task and the worker executes
it and reports the result. Most idle standoffs trace to sending a question,
getting an answer, and recording it as if work were underway — so nothing was
built and the orchestrator waits for work it never assigned.

**Orchestrator side**

- **A delegation is a frozen, bounded work order.** Exact scope, the exact
  deliverable the worker returns (a PR head, an activation, named files), and the
  definition of done. No open question — the worker can execute it end to end
  without coming back to ask.
  The definition of done names the terminal action that publishes or records
  the deliverable. State exactly: `Do not end the turn until the deliverable and
  terminal action are both done.`
- **A question is fine; a question is not a delegation.** "Which will you start?",
  "does this look right?" ask for an answer, not a deliverable. Ask them — but
  never inside a delegation packet, and never record the answer as "the task is
  underway." A retry of a question must never be logged as a task.
- **One packet, one act.** Do not fold a question, prioritization,
  ownership-confirmation, and a work order into one message.
- **State acceptance explicitly.** For each delegation name who owns the next
  action, what artifact proves it started (branch/head/diff — not an "on it"),
  and when you re-drive.
- **An acknowledgement is not a deliverable — prove the claim, not the activity.**
  Confirm the artifact exists before reporting progress; an "ACK / read-only / no
  work started" reply is not work in progress. Track what you actually sent, not
  what you meant, and never report a worker "on <task>" when you only asked it
  about <task>.
- **Idle is not completion.** A BB thread becomes idle whenever its turn ends.
  Every worker and reviewer instead pushes one `DONE|BLOCKED` tell to the exact
  orchestrator thread with summary, exact head, and evidence. Inspect the named
  deliverable and terminal action once after that report or an abnormal watcher
  event before treating the assignment as finished; never poll live workers.
- **Disposition belongs where the gate reads it.** A worker adjudicating a
  review finding posts the outcome on the exact review thread or gate artifact.
  A BB result or mailbox packet alone does not close that discussion.

**Worker side (the mirror — a mixed or unclear packet dies here in one turn)**

- **Self-label an acknowledgement.** A reply that starts no work says so:
  `ACK only — no work started`. Cheaper and surer than making the orchestrator
  infer it from timing or artifacts.
- **Never guess a mixed packet.** If a packet folds a question into a task, answer
  the question and state "no executable task" — do not build half of it.
- **A frozen task can still hit real ambiguity.** On a genuine mid-execution
  blocker, send one narrow question flagged as a `BLOCKER` (not progress), then
  pause — don't guess in order to stay "frozen."
- **Silence is never a valid state.** If you are blocked, finished, or cannot
  proceed, say so in a durable packet.
  For a BB worker, the equivalent is one direct terminal `DONE|BLOCKED` BB tell
  to the exact orchestrator thread rather than an llm-collab packet.
- **Finish both promised outcomes.** Do not end the turn until the delegated
  deliverable and terminal action are both done. Acknowledgement, partial work,
  or an idle turn is not a substitute.

**Liveness (event-driven for BB workers)**

- **For a BB worker, do not poll.** Normal completion pushes its terminal tell;
  the worker-lifecycle watcher points only on `error`, `stopping`, or
  disappearance of a previously active worker. After either event, read bounded
  evidence once and inspect the artifact before deciding whether to re-drive.
  Normal `idle`, liveness, marker refresh, and unchanged state remain silent.
- **Confirm the worker is alive before treating silence as waiting.** A worker
  with no autonomous loop (a terminal-app or CLI worker, e.g. Codex) ends every
  turn awaiting its next wake. For OpenAI-model interaction that wake is through
  BB unless the task needs a Codex-app-only tool that BB cannot reach; only then
  may the `deliver.py`-selected doorbell apply. A stopped or lease-expired
  session, or a stopped watcher, hears a re-drive as silence forever. The
  orchestrator confirms the session is active (or
  reactivates it) and that the live harness or acceptance receipt actually saw
  the packet — AX `VERIFIED` alone is not that proof — before re-driving. An
  open lane with nobody acting is a defect, and the orchestrator that owns the
  lane is who checks.
  This bullet applies to an explicitly registered first-class mailbox
  participant, not a BB worker.
- **Then keep the loop alive — through the recipient's own transport.**
  Re-driving is a *new durable packet* plus only the wake action `deliver.py`
  reports for that recipient — never a hand-chosen ring. For a watcher-backed
  recipient whose binding dispatches (`autobridge_ready: true`) — Claude, the Pi
  workers, and Codex alike — deliver durably and stop; its watcher owns pickup,
  so never ring it. For OpenAI-model interaction, use BB unless the task needs a
  Codex-app-only tool that BB cannot reach. Only when that condition is true and
  `deliver.py` reports `ax_doorbell_required: true` does the alternate path
  apply: run exactly the AX command it prints, and never re-ring a `QUEUED
  (UNCONFIRMED)` attempt. `VERIFIED` proves only that a turn rendered in the
  lagging app UI, not delivery to a working harness or a reply path. Re-driving
  a question just yields another answer. See the
  [`AGENTS.md` standing routing rule](../../AGENTS.md#bb-worker-surface) and
  `## Delegation message requirements`.
  This bullet applies to an explicitly registered first-class mailbox
  participant; a BB worker receives its follow-up through BB.

## Delegation message requirements

- exact goal
- files/modules in scope
- docs to read first
- success criteria
- explicit non-goals
- task id
- verification commands
- handoff format (files changed, commands run, verification result, blocker/ready)
- terminal action
- exact terminal `bb thread tell <orchestrator-thread>` format carrying
  `DONE|BLOCKED`, summary, exact head, and evidence
- implementation publication boundary: worker commits and pushes; orchestrator
  verifies the pushed head and opens the PR
- the exact instruction not to end the turn until the deliverable and terminal
  action are both done
- for review findings, the exact gate artifact where the disposition must be posted

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
- the exact project's required design-skill list
- the D8 design-thinking-in-details budget and seeded polish vectors from the task contract
- the exact browser-validation expectation
- the requirement for a handoff `Design-thinking pass` section with at least 3 findings and dispositions
- the requirement to record UI evidence back onto the task contract before moving to `review`

When that exact project's required design skills include `impeccable`, the brief
must additionally name the planned Impeccable steering commands, the requirement
to enforce Impeccable curated anti-patterns, and the mandatory
`pnpm ui:impeccable:detect -- <paths>` step.

For `shared-supabase-required` lanes, the delegation brief must also name:
- the required `db_impact` classification and shared project ref
- the requirement to use both Supabase CLI and `supabase_amiga` MCP surfaces
- the required shared-project apply + schema assertion step
- the requirement to record DB evidence back onto the task contract before moving to `review`
- the worker-account MCP preflight and the exact fallback/remediation path for
  access-control failures

For an exact project whose required design skills include `impeccable`, record
Impeccable evidence with:

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

This is an operating rule, not a claim that BB enforces queue readiness.
Do not activate workers that are not ready to start. Activation is queue-owner
controlled and happens only after the gates pass — not by asking the operator
to relay. The queue owner starts a BB worker through `bb-workers.md`; an
explicitly registered first-class participant uses the mailbox and only the
wake action `deliver.py` authorizes.

- Activate only workers in `in_progress` state that should execute now.
- A worker is not ready to start until its required branch/worktree already exists when isolated mode is expected.
- For queued workers, update task ownership/status and keep instructions in task/chat, but do not ring/activate them yet.
- When a queued BB worker becomes ready, start its frozen assignment through BB.
- When a queued first-class participant becomes ready, use `deliver.py` and
  only the wake action its result authorizes.

Required activation-state wording:

- single activation: `activate <worker> now`
- parallel activation: `activate <worker-a> + <worker-b> now in parallel`
- queue-only instruction: `do not activate yet; waiting on <condition>`

These describe the queue-owner's recorded activation intent; they are not an
enforcement control. The worker is then activated directly through its actual
transport, not through an operator paste/relay.

Never ring/activate multiple workers without explicit activation order.
If order is sequential, activate only the first and wait until the trigger condition is met before activating the next.
