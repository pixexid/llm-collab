# Review And Handoff

## Worker completion contract

Handoff replies should include:

- agent identity
- files changed
- commands run
- verification result
- known risks/open questions
- task readiness (`review` or `blocked`)

For worker-owned isolated-worktree implementation lanes, handoff replies must also include:

- checkpoint commit SHA
- assigned branch confirmation
- `git status --short --untracked-files=all`
- disposition of any remaining tracked or untracked files

For UI/UX lanes, handoff replies and the linked task contract must also include:
- `design_docs_read`
- `design_skills_used` matching the exact project's required design skills
- `browser_validation_desktop`
- `browser_validation_mobile`
- `operator_visual_feedback_requested`
- `design_doc_update_decision`

When the exact project's required design skills include `impeccable`, also include
`impeccable_commands_used` and `impeccable_detect_result`. These
Impeccable-specific fields are not required for projects that do not select that
skill.

For UI/UX implementation lanes, handoff replies must also include a `Design-thinking pass`
section and the linked task contract must record at least 3 `design_thinking_pass_items`.
Each item must include:

- `finding`
- `disposition`: `shipped`, `deferred`, or `out_of_scope`
- optional `evidence`

Docs-only UI/UX lanes skip D8 pass items unless they also change rendered UI.

For `shared-supabase-required` lanes, handoff replies and the linked task contract must also include:
- `db_project_ref`
- `db_migration_files` when schema change is involved
- `db_apply_result`
- `db_schema_assertion`
- `db_advisors_result` for schema-change lanes
- `db_runtime_validation`

## Independent review gate (cross-review)

The implementer never solely approves their own lane. Review is a separate role
from implementation (see role model in `task-intake-and-delegation.md`).

- **Planning-phase cross-review is mandatory for non-trivial lanes.** The agent
  that did not author the plan reviews the spec/AC/risk analysis before
  activation. A bad plan is the highest-cost failure, so this is the most
  important gate.
- **Pre-PR second-eyes is mandatory** on implementation. The reviewing agent
  inspects the actual diff (and rendered/DB evidence where relevant) before the
  lane is treated as PR-ready/accepted. The review artifact must name reviewer,
  implementer, base ref or merge base, final head SHA, result, mechanism/source,
  and any re-review disposition.
- **The initial PR-ready head requires one independent cold full-diff review**
  from a context-isolated reviewer. Identity separation, fresh-context
  attestation, complete changed-file coverage, and exact-head binding are not
  relaxed by later amendment handling.
- Cross-review is symmetric: each agent reviews the other's lanes. The Amiga
  queue-owner default (Codex) still records status transitions and the
  acceptance read, but "reviewer" is a role either agent fills depending on who
  implemented the lane.
- Codex normally reviews Claude-authored implementation lanes through the
  collab/doorbell loop and opens the PR after a clean result. Claude, Gemini, or
  another independent reviewer reviews Codex-authored lanes. Codex may still
  run the PR opener for a Codex-authored lane, but the recorded pre-PR review
  result and notes must come from the independent reviewer.
- If Codex is unavailable, Claude may obtain the review through the Codex
  MCP/review surface. Record that fallback artifact in the linked chat before
  PR creation. GitHub Codex PR review is a post-PR backstop and PR-wait signal,
  not the pre-PR review of record.

For amended heads, follow the canonical bounded-amendment and convergence rules
in `commit-push-prs.md`. Batch related findings locally and run the required
local exact-head verification. The first connector pass remains the PR's one
external review; do not add another model or bot review.

Count finding rounds by family, where same-family means the same file or the
same named invariant/mechanism across files. The second round makes the family
hot; before a third same-family amended head, the queue-owner/release-gate role
must record one durable disposition:
`contract-clarified`, `descope`, `split`, `backend-first`, or
`risk-accepted-followup`. Only `contract-clarified` continues in-lane, after the
corrected invariant is written into the task/spec. Current-head actionable bot
findings still block, and the final merge gate remains a full-diff attestation
bound to the exact current head.

## Task status guide

- `open`: created, not started
- `in_progress`: actively owned
- `blocked`: cannot progress without external input
- `review`: ready for orchestrator review
- `done`: reviewed, accepted, and closed by the configured release gate with a
  persisted terminal disposition

In an autonomous queue loop, `blocked` is not a default stop state. First decide
whether the blocker is actionable:

- if Codex can fix the brief, task contract, queue metadata, branch conflict,
  PR body, review comment, or verification failure, switch the loop to
  `fix_loop`, make the smallest correction, rerun the relevant gates, and
  return to the normal lane state
- if a worker needs a corrected instruction, update the task/issue first, then
  send one consolidated `llm-collab` message through the approved bridge path
- stop only for a true external blocker, such as missing credentials, operator
  product direction, unavailable required UI, or a destructive decision that
  cannot be inferred safely

## Handoff flow

1. worker creates the required checkpoint commit when the lane is an isolated-worktree implementation task
2. worker updates task status (`review` or `blocked`)
3. worker replies in the same task-linked chat
4. orchestrator verifies
5. for worker-owned isolated-worktree implementation lanes, orchestrator runs a dirty-worktree acceptance gate before acceptance:
   - verify the assigned branch matches the task contract
   - capture `git status --short --untracked-files=all`
   - confirm the checkpoint commit exists on that branch
   - record the disposition of any remaining files in task/chat notes
6. if the worktree is still dirty, orchestrator blocks acceptance unless the worker adds the missing checkpoint commit, explains why specific files must remain dirty, or the orchestrator records an explicit waiver with the reason
7. orchestrator either blocks, reassigns, or accepts
8. if the project maintains a canonical queue artifact, orchestrator updates queue state/order before selecting the next lane
9. after release closure, the configured release-gate agent performs the
   evidence-gated `review -> done` transition; only then does the task move to
   `Tasks/done`

## Parallel queue operation

Do not reduce the collaboration loop to one worker implementing while everyone
else waits. The orchestrator should keep safe parallel work moving:

- one authoritative writer per implementation lane
- one branch and isolated worktree per writer
- read-only planning, repo mapping, review, docs-sync, and release-guard work in
  parallel with active implementation when it can unblock future lanes
- multiple implementation writers only after a recorded non-overlap check for
  routes/surfaces, file sets, shared utilities, API/data/schema ownership,
  generated artifacts, validation resources, and merge order
- no parallel implementation when two lanes touch the same route, component
  family, DB table/migration, API contract, or generated artifact unless the
  task contracts explicitly split ownership and sequencing

Queue order still matters. If a later lane is safe to implement out of order,
use the queue override path with the non-overlap evidence. If that evidence is
missing, run read-only prep instead of parking the worker.

Hard rule for UI/UX lanes:
- `claim_task.py --status review` should fail if the task contract is missing the required UI evidence
- `claim_task.py --status review` should fail for UI/UX implementation lanes if the D8 design-thinking pass is missing or has fewer than 3 findings
- PR/review gating should fail again if the same task still does not satisfy the UI contract

Hard rule for shared-Supabase lanes:
- `claim_task.py --status review` should fail if the task contract is missing the required DB evidence
- migration files in git do not count as acceptance without shared-project apply + assertion

For a project with strict boolean `db.production_schema_guard: true`, review and
PR validation also reject detected schema work classified as `none`.
`local-schema-only` means disposable development/test schema that will never
reach shared or production and requires the exact operator-approved
`dev-only-non-production` exception plus a non-empty reason. Concrete
`db/migrations/**` and exact `db/schema.sql` paths force detection despite
`manual_false`; prose-only documentation matches may still be overridden. The
exception never waives the existing shared-database evidence above.

When the last queued lane moves to `done`, archive the final queue snapshot and keep the canonical
queue path in an explicit empty state instead of deleting it.

## Thread-boundary handoff rule

Stay in the active orchestrator thread by default after merge/local cleanup. Do
not create a self-handoff only because an issue merged, a task moved to `done`,
or the queue advanced. Continue in-thread unless the operator asks for a fresh
session/handoff, context safety requires a boundary, or the current agent cannot
continue safely.

If context must continue in a fresh orchestrator thread, send a self-handoff message before ending context. Include:

- task/issue identifiers
- related chat path
- branch/worktree state
- files/docs to read first
- current state and next concrete action

Do not use a thread-boundary handoff as a substitute for preserving workflow
changes. If workflow docs, repo instructions, skills, queue scripts, bridge
runtime docs, or agent memory changed during the lane, classify and persist
those changes before starting the next lane or ending the thread.

For PR-review wait heartbeats, follow `commit-push-prs.md`: the manual
branch-diff review happens once before the initial PR-ready head. Amended heads
receive local exact-head verification; the first connector pass remains the
PR's only bot review. Merge
from the current thread only after the exact current head has green required
checks, the PR is mergeable with clean merge state, the mandatory one-pass
connector review gate is complete, required local exact-head verification is clean, and
[the reviewed artifact set](commit-push-prs.md#reviewed-artifact-set) has no
actionable finding. Reviews start automatically for opened or ready PRs, and
every PR waits for that first pass. The Tier A/B/C rule in
[`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository),
which is the only place that defines it. The completion cases, including a clean
first pass on a prior OID followed by complete local verification of the amended
head, are defined only in the canonical terminal list in `commit-push-prs.md`.
The GitHub Codex gate is complete when the latest
`chatgpt-codex-connector` review/comment explicitly covers that exact OID with
no actionable issues, or a connector-authored `+1` (`thumbs-up`) sits on the exact
manual-review request comment while the head still equals the SHA that request
named, or the connector completed an exact-head review and every thread it
initiated has a thread-linked disposition accepted by the lane owner and
release-gate worker. A bare `eyes` reaction is accepted-and-in-progress, never
a verdict. Each outcome is terminal for the bot wait on that head. Their
post-signal handling differs. A terminal outcome stops waiting for further
artifacts only; it does not waive the handling below:

- A head-named clean connector verdict is not merge-immediate. Hold an
  approximately five-minute mandatory post-clean settle, then perform a full
  re-read of [the reviewed artifact set](commit-push-prs.md#reviewed-artifact-set)
  because the connector can emit multiple review artifacts for one head. A
  reaction counts
  only on the latest, unedited request artifact -- GitHub keeps reactions across an
  edit, so an edited request comment can still carry a `+1` left for an older head.
- A connector-authored `+1` on the exact manual-review request comment is
  terminal CLEAN once all six checks hold (actor, that request comment, the
  requested SHA, the current head, that this request is the latest for this head,
  and that it has not been edited since the reaction was left). It receives **the same approximately
  five-minute post-clean settle and full re-read as a text verdict**. The
  rationale for accepting a reaction-only CLEAN at Tier A rests on that settle
  plus adjudication, so exempting it from the settle would remove the evidence
  the rule depends on. Required CI, mergeability, independent review, and full
  inspection of [the reviewed artifact set](commit-push-prs.md#reviewed-artifact-set) still apply.
- A disposed-review completion receives the same approximately five-minute
  settle and full artifact re-read. Any new or unadjudicated finding cancels
  that completion.

Whether a missing automatic trigger needs the one manual fallback request is the Tier A/B/C rule in
[`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository),
which is the only place that defines it.

For first-pass silence, follow the canonical
[First-pass precedence](commit-push-prs.md#first-pass-precedence).
Tier A may issue exactly one fallback request when the automatic trigger did not
start. No retry or elapsed-time disposition replaces the mandatory first pass.

**No silence fallback exists.** No automatic artifact, eyes-only artifact, or
prior-head artifact is a pass. Tier A may repair a missing trigger with its one
fallback request; every other silent or stalled case remains blocked on review
infrastructure.

If GitHub Codex comments on the PR, every finding is adjudicated in writing —
which is not the same as accepted. Two paths, and which one applies depends on
whether the code changes:

- **Fix it.** Repair the pointed issue and rerun required local exact-head
  verification and checks. Do not request a second bot or model review.
- **Reject it.** A finding that is wrong, out of scope, or already handled is
  answered with a written disposition posted on that thread, naming the head it
  was judged at, and accepted by the lane owner and release-gate worker. No code
  changes means no amended head and no new request: the completed exact-head
  review plus that disposition is sufficient evidence.

Requiring a fix for every finding left an invalid one with no legal move — a
worker had to either make an unwarranted change or stall — and the governing
contract asks for adjudication, not compliance.

The first pass is the PR's only bot pass. A fix push does not require another
one; its findings remain the review record while local exact-head verification
proves the repair. Do not substitute a resolved thread with no written
disposition for that evidence. Delete the heartbeat before post-merge cleanup.

When the PR comment needs implementer action, route it through the mailbox and
doorbell immediately instead of leaving the PR-wait heartbeat to poll in silence.
The packet must name the PR, review thread/comment, current head SHA, exact
finding, head status, and required fix scope. If the packet is a writer
activation, send it with `deliver.py --activation` and tell the worker to use
the embedded `inbox.py --packet` claim command. The worker must not start from a
generic inbox read, because activation authority is granted only after the exact
packet lease claim succeeds and the returned fence is carried to later
mutation-time assertions. If the wait cannot progress because the
implementer has not acknowledged, the next heartbeat escalates by doorbell with
the blocker rather than waiting for operator discovery.

When a persistent queue-runner heartbeat is active, each task-specific wait must
update `autonomous-loop.json` before it waits and again before it resumes. This
keeps one authoritative loop state instead of several stale heartbeats making
conflicting decisions.

## Release Closure Gate (GH-1524)

After a production-affecting merge, release closure requires the exact-merge-SHA
deploy gate in `commit-push-prs.md` ("Release closure does not end at merge"):
run `bin/deploy_release_watch.py --project <project-id> --merge-sha <sha>`;
only terminal deploy+smoke success for that exact SHA closes a production
release, and failure/cancelled/missing each get one durable packet + one
doorbell with the configured release-gate agent holding the terminal
disposition.

The final task transition is mechanically restricted to `review -> done` and
requires `--released-by` equal to the enabled project `release_gate_agent` plus
one strict JSON `--release-evidence` object. Actor identity is workflow
deterrence, not authentication. For `verdict=success`, `claim_task.py`
re-evaluates the supplied SHA at transition time through the same
`deploy_release_watch.py` project config and requires both terminal `SUCCESS`
and exact equality with its selected run ID. Caller claims, watcher artifacts,
green different-SHA runs, stale runs, pending/failing/cancelled runs, missing
jobs/smoke steps, and missing configuration all refuse before task write/move
or queue mutation.

Projects without `release_closure` cannot claim `success`; use an explicit
structured `non-production` or `risk-accepted-followup` disposition when
truthful. Those honest non-success dispositions also remain available when
GitHub is disabled: the record persists `repository: null`, no evaluator runs,
and no caller-provided run ID is treated as authoritative. The normalized
record persists project/task/repository/SHA identity,
configured workflow, authoritative run ID when present, production-impact
disposition, terminal verdict, actor, evaluation time, and optional note.
Historical `done` tasks remain grandfathered. Rollback means reverting the
gate/config change, never hand-editing around a refusal or treating saved
evidence as a shortcut.

For every new done transition, `claim_task.py` validates the target-state task
contract at stage `done` before invoking the release evaluator. All three
dispositions (`success`, `non-production`, and `risk-accepted-followup`) refuse
missing shared-database evidence before task write/move, activity append, queue
mutation, or cleanup.

## Post-merge Cleanup Gate

After a merge, the orchestrator must run the executable cleanup gate before the
queue runner leaves `post_merge`:

```bash
python3 bin/post_merge_cleanup.py \
  --project amiga \
  --apply \
  --remove-plain-dirs \
  --discard-disposable-dirty \
  --fail-on-blockers
```

This gate is intentionally broader than `git branch --merged`: it inspects the
project worktree root, registered git worktrees, stale branch refs, done-task
mirrors, disposable generated dirt, and plain leftover directories. If it
reports blockers, the active thread must either fix them or record why they are
intentionally deferred before moving to the next lane.

Cleanup is verification/application after authoritative closure only.
`post_merge_cleanup.py` does not validate or perform a `review -> done`
transition and cannot substitute for `claim_task.py` contract and release gates.
