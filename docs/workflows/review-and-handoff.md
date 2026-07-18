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
- `design_skills_used`
- `impeccable_commands_used`
- `impeccable_detect_result`
- `browser_validation_desktop`
- `browser_validation_mobile`
- `operator_visual_feedback_requested`
- `design_doc_update_decision`

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
in `commit-push-prs.md`. Batch related findings locally. The same independent
reviewer may re-review only when the amendment stays inside the accepted
contract and changed-file set, and must inspect the batched delta, affected
invariants, and complete base-to-head coherence at the new exact head. Payments,
auth, permissions, schema/migrations, irreversible writes, a new product flow,
or any newly touched file require a new cold reviewer.

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
- `done`: reviewed and accepted

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
9. accepted tasks move to `Tasks/done`

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

For PR-review wait heartbeats, follow `commit-push-prs.md`: the repeated review
loop is the orchestrator's manual branch-diff review before commit/PR and after
any review-fix patch. When the operator has authorized the merge path, merge
from the current thread only after the exact current head has green required
checks, the PR is mergeable with clean merge state, the independent exact-head
review is clean, and the full current comment/review/thread payload has no
actionable finding. The GitHub Codex signal is clean when either the latest
`chatgpt-codex-connector` review/comment explicitly covers that exact OID with
no actionable issues or the watcher observed the connector's eyes-to-`+1`
(`thumbs-up`) transition on the latest head, the `+1` postdates that head, and no
subsequent push occurred. Either signal is terminal for the bot wait on that
head: report the exact verdict or attributable reaction lifecycle with its
timestamps immediately and do not wait out the remainder of the 15-minute
fallback. If neither terminal signal exists and no bot review is actually
pending, the resettable 15-minute settle is the fallback. For fallback
purposes, a pending/request state with no connector artifact for that full
resettable window is no longer actually pending; report and escalate the stuck
review, but do not let it extend the fallback indefinitely. Eyes or another
non-terminal reaction does not block the fallback once review is no longer
pending. Any push invalidates the prior signal and restarts the clock for the
new head.

If GitHub Codex comments on the PR, fix the pointed issue, rerun the manual
branch-diff review and required checks, then evaluate the new exact head and its
automatic artifacts from scratch. Do not substitute a resolved older thread or
stale inline review-thread object for current-head evidence. Delete the
heartbeat before post-merge cleanup.

When the PR comment needs implementer action, route it through the mailbox and
doorbell immediately instead of leaving the PR-wait heartbeat to poll in silence.
The packet must name the PR, review thread/comment, current head SHA, exact
finding, and required fix scope. If the wait cannot progress because the
implementer has not acknowledged, the next heartbeat escalates by doorbell with
the blocker rather than waiting for operator discovery.

When a persistent queue-runner heartbeat is active, each task-specific wait must
update `autonomous-loop.json` before it waits and again before it resumes. This
keeps one authoritative loop state instead of several stale heartbeats making
conflicting decisions.

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
