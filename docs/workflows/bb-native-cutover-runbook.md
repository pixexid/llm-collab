# Manual BB-Native Orchestrator Cutover

## Goal and authority

Move an orchestrator role from an app session to a BB role thread without
changing who holds authority, creating a second project-state store, or treating
a BB thread as the durable control plane.

This document is the sole tracked manual role-cutover procedure and owns the
frozen role-thread brief below. It is a bridge until the role lease and capacity
controller replaces manual promotion. It does not automate promotion, leases,
capacity, the message bus, plugins, or live role-state changes.

[`Orchestrator Sessions`](orchestrator-sessions.md) continues to own session
operation, watcher coverage, succession, supervisor routing, and bb updates.
[`BB Workers`](bb-workers.md) owns provisioning, the sanctioned assignment-spawn
path, profile controls, BB communication, and completion inspection.
[`Task Intake And Delegation`](task-intake-and-delegation.md) owns queue
activation, one-writer isolation, delegation, and the writing-lane cap. The
normal verification, review, and publication gates remain in
[`Commit, Push, And PR Workflow`](commit-push-prs.md).

## Role model

```text
logical role -> qualified profile -> active BB thread + environment -> capacity slot -> execution evidence
```

`orchestrator:<project_id>` is the logical role. A BB thread is a replaceable
execution instance for one generation of that role. Never silently change the
model inside a live authority thread. A profile change creates a new assignment,
thread, execution record, and epoch.

Resolve `<runtime_root>` through the deployed-current-runtime rule in
[`Session Startup`](session-startup.md#bootstrap-first). Shared `llm-collab`
tools and contract documents come from that absolute runtime root. Role-authored
product-repository edits occur only in an authorized writing lane; read-only
diff, SHA, and test verification occurs in each delegated lane's separately
verified worktree.

## Cutover procedure

1. Bring the project's own handoff at
   `{project_state_root}/<project_id>/orchestrator-handoff.md` current before
   promotion. Follow the ordered handoff and watcher teardown in
   [`Orchestrator Sessions`](orchestrator-sessions.md#succession-protocol).
2. Provision the worktree through the exact project's configured native BB
   lifecycle, using only the `NO-WRITE WORKTREE PREFLIGHT` turn defined by
   [`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree). Provisioning is
   not an assignment: do not use
   `<runtime_root>/bin/llm-collab bb_spawn.py --new-environment worktree`, which
   deliberately refuses. Wait for the provisioning thread to become idle, then
   apply that workflow's exact environment, path, branch, base, and clean-tree
   checks before attaching the role.
3. Attach one **writing**, explicitly non-authoritative candidate to the verified
   environment with a profile-only first turn. Do not include the frozen brief
   or any task content yet. Ordered succession has stopped the
   predecessor's watchers, while only the activated successor may start its own,
   so this one spawn uses the recorded watcher-gap override:

   ```bash
   <runtime_root>/bin/llm-collab bb_spawn.py \
     --assignment-kind writing \
     --allow-stale-watchers \
     --collab-project <project_id> \
     --repo-target <repo_id> \
     --environment <verified_environment_id> \
     --base-sha <base_sha> \
     --provider <provider> \
     --model <model> \
     --reasoning-level <reasoning_level> \
     --permission-mode full \
     --title "Candidate orchestrator:<project_id> epoch <epoch>" \
     --json
   ```

   `--allow-stale-watchers` is recorded in this assignment and authorizes only
   this succession-gap spawn; it authorizes no other spawn against stale,
   absent, foreign-owned, or unverifiable watcher coverage. The candidate's
   launcher injects the task-free profile-only prompt; it accepts no caller
   prompt. The candidate's initial turn may load and verify context but must not
   claim authority or start protected work.
4. Require `<runtime_root>/bin/llm-collab bb_spawn.py` to return the same
   verified environment ID, then archive the provisioning thread immediately
   through the procedure in
   [`BB Workers`](bb-workers.md#spawn-in-an-isolated-worktree). Do this before
   demoting the predecessor or writing the generation record.
5. Require the candidate's live profile-only reply, then read the executed provider, model,
   and reasoning level from its execution event. Requested flags and declared
   defaults are not execution evidence; a missing or mismatched event refuses
   promotion. Only after that exact event proof, send the frozen task-bearing
   brief below to the same thread. The candidate assignment itself supplies the
   exact-profile proof, so do not create a second probe assignment or let the
   first turn inspect the brief.
6. Demote the predecessor in writing. It becomes driver/reviewer: it may advise,
   steer through BB, and review, but it may not issue approvals or start
   protected work.
7. Record the new generation in the project's existing
   `{project_state_root}/<project_id>/role-generation.md`, preserving the current
   approved record shape and its `handoff_sha256`. This runbook does not define a
   second schema or resolver; the program work tracked by GH-784 owns that shared
   authority seam.
8. Send the activation tell to the same candidate thread through the configured
   native BB command established by `BB Workers`:

   ```bash
   "${bb_cmd[@]}" thread tell <role_thread_id> \
     "ACTIVATE orchestrator:<project_id> epoch <epoch>. Re-read <role_generation_path> and begin only if it names this exact thread, project, role, and epoch. Start or reconcile successor watcher coverage before any other protected work." \
     --mode steer
   ```

   The generation write alone is not activation evidence. Authority begins only
   after the tell's exact input is accepted and the candidate re-reads an exact
   matching record.

   If execution is interrupted after the generation write, read bounded JSON
   pages of the exact candidate's thread log through the end. Match the
   activation's exact `client/turn/requested` event (`source: tell`, exact input,
   and `requestId`) to a `turn/input/accepted` event whose
   `data.clientRequestId` equals that `requestId`. If that acceptance exists,
   never resend; continue inspecting the accepted turn. Only when the
   complete readable log proves that no activation input was accepted —
   including interruption before invocation or a proven pre-acceptance failure
   — may the operator send one recovery tell with the same exact epoch and
   thread guard. If the log is unreadable or incomplete, or acceptance is
   ambiguous, remain in degraded safe mode and escalate. Never guess or send
   more than one recovery tell.
9. On activation, the successor first re-reads its generation and then starts or
   reconciles the standard project watcher set from
   [`Orchestrator Sessions`](orchestrator-sessions.md#standard-watcher-set).
   It performs no other protected work until successor-owned marker coverage is
   established.
10. After the exact generation match, accepted activation input, and
    successor-owned watcher coverage are all proven, record terminal disposition
    `promoted` for the candidate bootstrap writing assignment in the own-project
    handoff. This is an assignment disposition, not a role-state field or second
    record. It closes the bootstrap writing lane. The continuing orchestrator
    authority thread is then outside lane WIP, while every code, documentation,
    or fixture deliverable remains a delegated writing lane until its own
    terminal disposition.

Re-running bootstrap is reconciliation, not promotion. Re-read the live epoch,
verify the installed bb version, check watcher coverage by project-scoped
markers, and recheck the task board and canonical queue. If the recorded
generation already holds the role, do not create another authority holder,
watcher set, or project-state store. A fresh foreign-session watcher marker
blocks succession exactly as specified by
[`Orchestrator Sessions`](orchestrator-sessions.md#successor-bootstrap).
A generation record alone never proves activation; recover the activation input
through step 8 before treating the recorded candidate as authoritative.

## Promotion and refusal

A candidate is eligible only when its exact profile is live, qualified for the
role, not hard-excluded, within available capacity, able to load the exact
project context, and confirmed by its execution event.

The hard exclusions in [`BB Workers`](bb-workers.md) apply to every text-bearing
assignment, including read-only probes. Link to that authority; do not copy its
current model list into a generation record or brief.

A promoted-away thread must not regain authority. Before any protected action,
an old thread that resumes after a restart, retry, or account change reads the
current project generation. A higher epoch retires it to advice-only status.

When no qualified candidate is available, enter degraded safe mode:

- already-approved bounded workers may finish;
- read-only inspection and decision-packet preparation may continue;
- no new protected, irreversible, or policy-changing work starts;
- no worker self-promotes; and
- emit one operator alert.

Improvised authority is never the fallback.

## Authority and project boundary

BB owns execution, threads, environments, and panels. `llm-collab` owns roles,
qualification, routing policy, assignments, one-writer guarantees, approval
provenance, leases and epochs, handoffs, capacity state, project isolation, and
audit history. No plugin database may become a competing authority. The durable
mailbox remains the message ledger; a BB thread is not an `agents.json`
collaborator and never authors a packet as another agent.

The role may write its own project state at
`{project_state_root}/<project_id>/`, including its handoff and role-generation
files. It must never write another project's state, repositories, Tasks, or
queues. Resolve every project-aware read and write from the role's exact
`project_id`; do not infer ownership from a checkout, thread, or recent record.

## Watcher wake status

Watcher tells are minimal pointer-only events:

- a watcher event points the role at canonical state; it does not carry task or
  message authority;
- the liveness marker remains owned by the session that launched the watcher;
- the tell target resolves the current role generation when the event fires;
  and
- normal worker completion arrives directly from the worker as one terminal
  `DONE|BLOCKED` tell; lifecycle tells are only for `error`, `stopping`, or
  disappearance of a previously active worker;
- PR/review/check timeline changes and deployed-version drift wake the role;
- normal `idle`, liveness, marker refresh, `WATCHER LIVE`, and unchanged
  baselines remain silent; and
- identical semantic events coalesce until recovery or state change, while a
  confirmed tell failure advances neither canonical watcher baseline, marker,
  nor dedupe state.

The orchestrator never polls live workers. It performs one bounded evidence read
after a specific terminal report or watcher event.

## First actions after promotion

1. Re-read the generation record named by the activation tell and confirm that
   its project, role, epoch, and thread all identify this successor. A mismatch
   leaves the candidate non-authoritative.
2. Start or reconcile the standard watcher set immediately and establish
   successor-owned marker coverage before any other protected work.
3. After activation acceptance, exact generation match, and successor watcher
   coverage are proven, record the candidate bootstrap assignment's terminal
   `promoted` disposition in the own-project handoff.
4. Re-verify the handoff's environment deltas, installed bb version, project
   task board, and canonical queue, then continue through the session workflow.

## Frozen role-thread brief

Copy the template verbatim, replace angle-bracket placeholders, and do not add,
remove, rename, or combine assignment sections. The brief has exactly seven
sections.

```markdown
## TASK

Join as the non-authoritative candidate for `orchestrator:<project_id>` epoch
`<epoch>` and continue the project from `<handoff_path>`. This is the first
task-bearing turn and is sent only after the preceding profile-only turn's BB
execution event proved `<provider> / <model> / <reasoning_level>`. Read and
verify context, report readiness directly to `<cutover_thread_id>`, and stop. Do
not claim the role or start protected work. Authority begins only after the exact
activation input is accepted in a later turn; then re-read
`<role_generation_path>` and proceed only when its project, role, epoch, and
thread identify this exact candidate.

After activation, own orchestration, queue decisions, independent verification,
review judgments, and contract-reserved work. Workers and reviewers push one
terminal `DONE|BLOCKED` tell to this exact orchestrator thread; do not poll live workers.
Perform one bounded evidence read after that report or a watcher abnormal
pointer, then verify the promised artifact. Before becoming idle, recheck the
project task board and canonical queue and start all work that is ready under
the lane gates.

## EXPECTED OUTCOME

The candidate remains non-authoritative until its exact activation input is
accepted and it re-reads an exact matching generation. The exact project then
keeps one current orchestrator authority generation. After activation
acceptance, exact generation match, and successor watcher coverage are proven,
the bootstrap writing assignment ends with terminal disposition `promoted`;
the continuing authority thread is not a writing lane. Successor watcher
coverage is established before other protected work; live workers are driven
through their terminal actions; startable queue work is not left idle;
completed output is independently verified; and handoff, queue, review, and
release state remain project-exact.

## REQUIRED SKILLS

- Load Ponytail at full intensity before every task.
- Load each skill required by the exact delegated lane before dispatch.
- Treat `<runtime_root>/AGENTS.md` and its Required Reading as the worker
  contract.

## REQUIRED TOOLS

- Use `<runtime_root>/bin/llm-collab bb_spawn.py` for every code,
  documentation, and fixture assignment.
- Use the exact project's configured BB executable for tells and bounded read
  surfaces; never substitute PATH `bb`.
- Use the project's canonical task board, queue, GitHub, verification, and
  collaboration tools through the workflows that own them.

## MUST DO

- During the initial non-authoritative turn, read
  `<runtime_root>/AGENTS.md`, its Required Reading, and the handoff in full,
  starting with environment deltas. Verify the proposed epoch, installed bb
  version, watcher state, task board, queue, and verified product worktree;
  report readiness without starting protected work.
- On the activation tell, re-read the generation record and act only if its
  project, role, epoch, and thread identify this exact candidate. Start or
  reconcile successor watcher coverage immediately, before any other protected
  work.
- After the activation input is accepted, the generation matches exactly, and
  successor-owned watcher coverage is proven, record terminal disposition
  `promoted` for this bootstrap writing assignment in `<handoff_path>`. This
  closes only the bootstrap lane; the continuing authority thread is outside
  lane WIP, and every code, documentation, or fixture deliverable remains a
  delegated writing lane.
- Delegate all code, documentation, and fixture implementation through
  `<runtime_root>/bin/llm-collab bb_spawn.py`, using the same seven assignment
  headings in the same order. Keep only contract-reserved orchestration and
  judgment, plus a truly single-line mechanical own-project-state edit, in this
  thread.
- Put one exact terminal action in every worker and reviewer brief:
  `bb thread tell <role_thread_id> "DONE|BLOCKED <assignment> | summary=<one line> | head=<exact SHA or none> | evidence=<exact artifacts and checks>"`.
  Workers commit and push only; they never open the PR. This role independently
  verifies the pushed head and owns PR creation, review, merge, and disposition.
- Use `<runtime_root>` for shared `llm-collab` tools and contract documents. Do
  not make role-authored product-repository edits outside an authorized writing
  lane. Perform read-only diff, SHA, and test verification in each delegated
  lane's separately verified worktree; read access grants no writer authority.
- Keep one writer per lane. When two writing items are startable under the
  canonical queue and non-overlap gates, run both concurrently. The two-lane cap
  is a ceiling: never manufacture a second lane or exceed the cap.
- Treat every terminal worker report as a draft until you verify its exact diff
  and SHA, diagnostics, focused tests, and promised terminal action directly.
  Read evidence once after that report or a watcher event; never run live-worker
  `wait`/`show`/`output` loops.
- Execute the failover plan in order: reconcile, epic, five scoped issues in PR
  sequence, this manual cutover, then only PR 1 for role descriptor and authority
  provenance, building on GH-784, GH-783, and GH-565.
- Keep the supervisor on Fable for compact, event-driven high-value decisions.
  Routine orchestration stays off Anthropic: Luna MAX first while GLM-5.2 MAX is
  pending one passing requalification; use Kimi K3 high for complex work, Sol
  high only for the hardest work, Opus medium only as an explicit emergency
  orchestrator/worker tier, and Fable only for the supervisor. GPT-5 Pro receives
  only orchestrator-prepared complex definitions with artifacts already on
  GitHub, one send and one check.
- Apply two-key supervisor-plus-orchestrator authority with recorded reasoning,
  a revert path, and a distinct provider in the concurrence loop. Reserve only
  credentials/accounts, real spend, legal commitments, stated product direction,
  and destructive-irreversible actions for the operator.
- Treat harness and model as separate: BB is the surface; use AX only for tools
  available solely in the Codex app. Verify every spawned session from its BB
  execution record, never a chip, requested profile, remembered default, or
  self-report. Keep supervisor token use event-driven and delegate large reads.
- Keep Amiga focused on customer-facing SEO output; the portable-Postgres DB
  migration remains deferred.
- Write only this role's own project state under
  `{project_state_root}/<project_id>/`, including its own handoff and
  role-generation files.

## MUST NOT DO

- Do not write another project's state, repositories, Tasks, or queues.
- Do not claim authority or start protected work during the initial candidate
  turn, before the exact activation input is accepted, or after a
  generation-record mismatch.
- Do not treat a generation record alone as activation evidence, resend an
  accepted or ambiguous activation input, or send more than one recovery tell.
- Do not make role-authored product edits outside an authorized writing lane or
  turn read-only verification access into writer authority.
- Do not create a second role schema, resolver, store, queue, lease, capacity
  mechanism, or message ledger.
- Do not treat BB thread state, a watcher tell, `idle`, or wait exit 0 as task
  completion or authority evidence.
- Do not poll live workers, let a worker open a PR, omit provider/model/reasoning
  flags, or send task content before an execution event proves the exact triple.
- Do not let a worker self-promote, silently switch the profile inside an
  authority thread, or accept a worker summary without inspecting its artifacts.
- Do not leave startable queue work undispatched merely because live workers are
  legitimately quiet between pushed events.

## CONTEXT

- Project: `<project_id>`
- Role: `orchestrator:<project_id>`
- Epoch: `<epoch>`
- Runtime root: `<runtime_root>`
- Product worktree: `<product_worktree>`
- Repository target: `<repo_id>`
- Handoff: `<handoff_path>`
- Role generation record: `<role_generation_path>`
- Candidate thread: `<role_thread_id>`
- Cutover driver thread: `<cutover_thread_id>`
- Brief file: `<brief_file>`
- Exact profile: `<provider> / <model> / <reasoning_level>`
- Read first: `<runtime_root>/AGENTS.md`,
  `<runtime_root>/docs/workflows/session-startup.md`,
  `<runtime_root>/docs/workflows/orchestrator-sessions.md`,
  `<runtime_root>/docs/workflows/bb-native-cutover-runbook.md`,
  `<runtime_root>/docs/workflows/bb-workers.md`, and
  `<runtime_root>/docs/workflows/task-intake-and-delegation.md`.
```
