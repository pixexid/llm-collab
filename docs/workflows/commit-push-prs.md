# Commit, Push, And PR Workflow

## Goal

No lane is PR-ready until local validation and required metadata are complete.

## Hard rules

- never push directly to `main`; only the release-gate role merges to `main`
- the implementer role may create/commit on its assigned task branch and, when
  granted git/PR authority, push that branch and open a PR for its own lane —
  but only within the assigned worktree and under the safeguards below. Any
  worker (including Claude) may hold this authority per the role model in
  `task-intake-and-delegation.md`; it is not reserved to one named agent.
- the merge/release gate stays with the queue-owner/release-gate role (Codex by
  default in Amiga, a tooling constraint): independent review, merge-state
  inspection, and the merge itself are not performed by the implementer on their
  own lane
- do not open PRs without linked tracking context (issue/task)
- require full local verification for the affected surface before PR
- commit only on the assigned worktree branch; verify
  `git branch --show-current` before each commit; out-of-scope work becomes a
  separate task/branch/PR so no shared repo/branch is left dirty

## Suggested branch layers

- worker branch: implementation
- review branch: integration + preview gate
- main: merge target

## Review-branch gate

Before PR creation, run project-required verification on the review branch.
Run browser/smoke checks only when the lane touches browser-relevant behavior.
Use the project's primary browser path first, and run fallback browser tooling only if the primary path fails.
Do not create the review branch from a worker lane until the worker handoff acceptance gate has already passed, including branch verification, checkpoint-commit verification, and `git status --short --untracked-files=all`.
For Amiga, this means invoking the repo-local `amiga-pre-pr-review` skill before
commit, push, or PR.

Before committing or opening a PR, an independent reviewer must manually review
the final branch diff against the target branch or merge base. The reviewer is
not the implementer. Treat this as the primary code-review gate:

- review against the merge base or target branch, not just the last edited file
- check correctness, regressions, missing verification, contract drift, and
  workflow/process consistency
- classify findings as blocker, follow-up, or note
- fix all blockers before commit when the fix is local and bounded
- rerun the affected verification after fixes
- only then commit, push, and open the PR

This manual branch-diff review is the repeated review loop. GitHub Codex PR
review is an external safety net, not the mechanism that should discover routine
issues for the first time.

The standard Amiga mechanism is collab/doorbell review: the implementer sends
the final branch, base ref or merge base, final head SHA, scope, and verification
evidence in the linked chat; the reviewer returns findings in the same durable
chat. Codex normally reviews Claude-authored lanes and opens the PR after a
clean result. Claude, Gemini, or another independent reviewer reviews
Codex-authored lanes; Codex may still run the PR opener, but the review result
and notes must come from the independent reviewer. If Codex is unavailable,
Claude may use the Codex MCP/review surface as a fallback, but the review
artifact must still be recorded in llm-collab with reviewer, implementer, base
or merge base, final head SHA, result, mechanism/source, and re-review
disposition. The Codex app `/code review` UI is operator-facing and should not
be documented as an agent-callable requirement.

For Amiga, `pnpm pr:open` is the mechanical bypass guard for this workflow. It
requires `--review-result` and `--review-notes` for PR creation/editing. Allowed
results are `clean`, `clean-after-fixes`, `blocked`, `skipped (docs-only)`, and
`skipped (non-runtime)`; `blocked` cannot open a ready PR. Skip values require a
clear rationale in the notes. The opener rejects self-review notes and rejects
notes whose `Head SHA:` does not match the current branch head, so a stale clean
review cannot cover later commits.

### Bounded amendment review and convergence

One independent cold full-diff review by a context-isolated reviewer is
mandatory before the initial PR-ready head. Implementer/reviewer identity
separation, fresh-context attestation, full changed-file coverage, and exact
live-head SHA binding remain mandatory.

Batch related findings locally into one reviewed amendment instead of producing
one pushed head per micro-fix. When an amendment stays inside both the accepted
contract and its changed-file set, the same independent reviewer may inspect the
batched delta, the affected invariants, and the resulting full-diff coherence at
the new exact head. The resulting attestation must still cover the complete
base-to-head diff at that exact head; a delta-only review is never sufficient.

Create a new cold context-isolated reviewer for the complete amended diff when
the amendment touches payments, auth, permissions, schema or migrations,
irreversible writes, a new product flow, or any file outside the accepted
contract's changed-file set. These boundaries override the same-reviewer
allowance.

Apply a convergence circuit breaker per finding family:

- A finding belongs to the same family when it is anchored to the same file or
  concerns the same named invariant/mechanism across files.
- When a second finding round lands in one family, the queue-owner/release-gate
  role names the family and marks it hot. One already-drafted round-two
  amendment may finish.
- Before any third same-family amended head, record exactly one durable
  disposition in the task and, once a PR exists, in a PR comment:
  `contract-clarified`, `descope`, `split`, `backend-first`, or
  `risk-accepted-followup`.
- Only `contract-clarified` permits continued work in the same lane, and it
  requires updating the task/spec with the corrected invariant before the third
  head. `contract-clarified` may be used at most once per family per PR; a
  second same-family disposition must be one of the terminal values.
- Same-file anchoring counts mechanically: two finding rounds whose findings
  touch the same file are the same family regardless of which named invariants
  they cite. Orchestrator judgment applies only to grouping cross-file
  invariant findings.

Hard cycle cap, independent of family counting:

- A review-fix cycle is one finding round plus its amendment, regardless of
  reviewer freshness: same-reviewer re-reviews under the bounded amendment
  allowance consume cycles exactly like fresh cold reviews.
- The cycle counter is per task/lane, not per PR: it starts at the initial
  cold review — including the pre-PR collab/doorbell review loop — and carries
  into the PR once one exists. Opening the PR never resets the count.
- After the initial cold review, at most 2 review-fix cycles are permitted per
  lane; 3 when the contract scope includes payments, auth, permissions,
  schema/migrations, or irreversible writes.
- Docs-only lanes whose no-consumer scan proves zero runtime consumers always
  cap at 2 cycles: residual prose ambiguity in an unconsumed document is a
  follow-up issue, never another cycle.
- At the cap, inspect the exact current head. Only when actionable findings
  remain open at the capped head is exactly one terminal action required
  before any further amendment: merge at the current head with
  `risk-accepted-followup` (open findings move to a new issue), `descope`,
  `split`, `backend-first`, or a durable operator escalation packet. A capped
  head with zero open actionable findings and a clean exact-head re-review
  follows the normal merge gate with no convergence-disposition label.
  "No further amendment" bars content changes only; the publication steps the
  chosen disposition itself requires — pushing the already-reviewed head,
  opening its PR, and merging — remain permitted, so a lane that caps during
  the pre-PR loop can still land via `risk-accepted-followup`. Starting another
  review cycle past the cap is a process violation.
- A cap disposition never waives the PR Review Wait Gate. The cap bars another
  fix cycle, not waiting: the capped exact head must still pass the complete
  gate below, including its exact-head signal model, post-clean guard, and
  resettable fallback, before merge.
- Reaching the applicable cap requires an operator-visible escalation message
  recorded independently, whether or not open findings require a terminal
  disposition. When open findings do require a terminal disposition, record
  the escalation alongside it. Spending more than 2 hours of wall-clock time
  in the review-fix state requires escalation before the next cycle; a lane
  found past its cap is a process violation that must also be escalated.

When a project supports structured review notes, the disposition may be
recorded as the optional line `Convergence-disposition: <value>` and must use
exactly one of the five values above.

One final exact-head full-diff gate is mandatory before merge, and actionable
automated-review findings on the current head remain blocking. After a pushed
amendment, stale review-attestation CI is an expected transitional state rather
than evidence that product verification failed. Refresh the PR body only after
the amended head passes its required review.

Rely on the automatic GitHub Codex review flow for ready PRs. Consume the
automatic PR review/comment/reaction when it appears. If no automatic artifact
appears, use the opportunistic policy below.

## PR requirements

Include:

- linked issue/task
- verification summary
- risk notes
- docs-sync confirmation when behavior contracts changed

## PR Review Wait Gate

Do not merge a PR from green CI alone. A merge is allowed only after the
orchestrator has inspected:

- GitHub Actions checks on the latest head SHA
- `mergeStateStatus`
- top-level PR reviews and review bodies
- nested review threads and inline comments
- any requested changes or review replies after follow-up commits

Do not idle on review while `mergeStateStatus` is dirty. A dirty merge state is
an active blocker: refresh the branch against the target base, resolve conflicts,
rerun verification, push, and then request/inspect review again.

### GitHub Codex review policy

Use `local_required_github_codex_opportunistic` as the default queue-runner
policy:

- the orchestrator's local review and required project gates are mandatory
- automatic GitHub Codex review/comments are consumed when they appear
- a clean `chatgpt-codex-connector` review/comment that explicitly covers the
  exact current OID is terminal for that head
- a connector `+1` (`thumbs-up`) is terminal only when it postdates the latest
  head, no subsequent push occurred, and the watcher observed the connector's
  eyes-to-`+1` lifecycle on that head. Timestamp alone or a `+1` on an older or
  unrelated artifact is not head-attributable
- these are the only two exact-head terminal signal models; no other review,
  comment, or reaction artifact is terminal
- a head-named clean connector verdict is not merge-immediate. Hold an
  approximately five-minute post-clean settle, then perform a full re-read of
  reviews, review threads, and reactions before merge because the connector
  can emit multiple reviews for the same head
- when a re-review was explicitly requested, that re-review supersedes older
  same-head clean artifacts for the clean-verdict path. Only the explicit
  re-review verdict can satisfy that path, and it receives the same
  approximately five-minute post-clean settle and full re-read
- report the exact verdict or the latest-head eyes-to-`+1` transition with its
  timestamps and confirm that no later push occurred
- any push creates a new head and invalidates every prior verdict and reaction
  lifecycle. Start or reset the 15-minute fallback clock at the later of the
  final push to that head and the time that head becomes reviewable: the PR is
  open, any draft is marked ready, and the PR is visible for review. An explicit
  review request is NOT required for a head to be reviewable; absence of an
  explicit request neither pre-expires the fallback nor extends it (see the
  absent-request variant below)
- GitHub Codex silence must not become an infinite wait: the resettable
  15-minute settle is the fallback only for the three named
  no-terminal-artifact variants enumerated below, when no bot review is
  actually pending. An explicitly requested review does not enter this ageing
  rule; follow the canonical requested-review precedence below
- after every review-fix push, evaluate the new exact head under the same rule:
  a clean exact-head verdict or head-attributable connector `+1` is terminal; if
  neither terminal signal exists and no bot review is actually pending under
  the ageing rule above,
  heartbeat inspections may observe the wait but must not merge before the
  resettable 15-minute settle measured from the later of the final push and that
  head becoming reviewable
- neither a bot verdict nor a reaction waives required CI, mergeability, the
  independent exact-head review, or full comment/review/thread inspection

The resettable fallback above handles three named no-terminal-artifact variants
explicitly, so none of them silently extends the wait:

- **No explicit review request.** The reviewability clock starts at the later of
  the final push and the head becoming reviewable even when no explicit review
  request exists on the PR (the PR is open, any draft is marked ready, and review
  visibility exists for that head). Absence of an explicit request does not
  pre-expire the fallback, and it does not extend it indefinitely either.
- **Eyes-only current-head artifact.** This fallback variant applies only when
  no explicit review request is outstanding. A current-head non-terminal
  `eyes` reaction is not blocking once no review is actually pending; it does
  not restart or suppress the resettable fallback, and it is not itself a
  terminal signal.
- **Prior-head artifacts only.** Any push creates a new head and invalidates every
  prior verdict and reaction lifecycle; a prior-head `Codex Review:` body or
  `eyes`/`+1` reaction is not head-attributable for the current head and is
  ignored for terminal-signal purposes. The resettable fallback runs its clock on
  the current head, never on a stale-head artifact.

#### Explicit requested-review precedence

An explicitly requested review remains pending until its roughly 30–35-minute
clock expires unless one of the two exact-head terminal signals arrives; it
never ages into the 15-minute fallback. Anchor each clock to the corresponding
explicit request artifact's GitHub `created_at`, never to the latest push or
the time the head became reviewable. A current-head `eyes` reaction alone is
non-terminal: it does not exit requested-review precedence, reset that request's
clock, or move the PR into the eyes-only fallback.

When the initial request's clock expires without a terminal signal, treat that
request as silently dropped and issue exactly one `@codex review` re-trigger.
The re-trigger is the sole automatic retry and starts its own 30–35-minute clock
at its GitHub `created_at`. If that clock also expires without a terminal
signal, do not re-trigger again. The PR remains unmergeable until a
human/operator records an explicit disposition bound to the exact current
head. The disposition must state exactly one of these outcomes: merge of that
exact head is authorized despite the absent connector terminal signal; or that
exact head must not merge and remains blocked or is closed. An ambiguous note,
a disposition not bound to the current head, or an older-head disposition does
not lift the merge block. Any later push invalidates the disposition and
restarts exact-head evaluation.

An exact-current-head merge authorization lifts only the missing
connector-signal subgate caused by the silently dropped requested review. It is
not a connector terminal signal, is not a third automated terminal-signal
model, and creates no fallback path. It does not waive independent exact-head
review, green required checks, mergeability, the full
comment/review/thread/reaction reread, unresolved-feedback handling, or
project/operator auto-merge authority. If a connector clean signal later
arrives, its signal-specific settle and reread still apply normally; the
operator authorization does not masquerade as that signal or inherit its
handling. Report and escalate the stuck review at each expiry. The existing
PR-wait heartbeat observes these clocks; neither the re-trigger nor the
operator authorization is a new terminal signal or watcher mechanism. The
longer request-anchored timer exists because a dropped request is
indistinguishable from a review that is still processing, unlike the
absent-request variant, where there is nothing to drop.

If the PR is waiting only for remote checks or remote review state, keep it open
and create or update a Codex heartbeat attached to the current thread with a
6-minute cadence. Each heartbeat must re-check the PR checks, review state,
review threads/comments, automatic connector reactions, and merge state.

PR-wait heartbeats are a safety-fuse, not the primary routing path. When a
heartbeat or queue owner finds actionable PR feedback that needs the implementer
to change their branch, it must send a durable mailbox packet and inspect the
`deliver.py` result. If `autobridge_ready: true`, the current Phase 1 route is
session autobridge and no AX doorbell was requested. If
`ax_doorbell_required: true`, first prove the native composer is empty, then
ring the implementer once with AX even if busy. A non-empty, unreadable,
unprovable, or `AXValue`-opaque composer means hold and enter recovery.
`VERIFIED` exit 0 confirms delivery; `QUEUED (UNCONFIRMED)` exit 0 preserves the
mailbox/blocker follow-up but is not exact-thread delivery proof and must not be
re-rung. The idle input gate applies only if attended screenshot/keyboard
Computer Use is needed as fallback. Do not silently wait for the next heartbeat
or depend on the operator to notice the PR comment.

When the operator has authorized the merge path for the PR or PR class, the
heartbeat may complete the wait after it verifies the exact current head has
green required checks, the PR is mergeable with clean `mergeStateStatus`, the
independent exact-head review is clean, and the full current comment, review,
inline-comment, and thread payload has no actionable finding. Treat the GitHub
Codex review signal as clean when either:

- the latest top-level `chatgpt-codex-connector` review/comment explicitly
  covers the exact current OID and reports no actionable or major issues, or
- the watcher observed the connector's eyes-to-`+1` (`thumbs-up`) transition on
  the latest head, the `+1` postdates that head, and no subsequent push occurred.
  That attributable reaction is terminal for the bot wait on that head when the
  required gates above remain clean; do not wait out the remainder of the
  15-minute fallback.

For the clean-verdict path, do not merge immediately after the first
head-named clean artifact. Observe the approximately five-minute post-clean
settle and then re-read all reviews, review threads, and reactions. When an
explicit re-review was requested for the same head, ignore older same-head
clean artifacts for this path and apply the same settle and re-read to the
explicit re-review verdict.

If neither a terminal GitHub Codex verdict nor a head-attributable connector
`+1` exists for the exact current head, use the fallback only for the three
named no-terminal-artifact variants above and only after at least 15 minutes
have elapsed since the later of the final push to that head and the
reviewability timestamp for that head (PR open, marked ready when applicable,
and visible for review). Commit age cannot pre-expire the fallback before
automatic review can begin. Eyes or another non-terminal reaction does not
restart or suppress that fallback after review is no longer pending. Any push
resets this clock. Explicit requested-review silence follows
[Explicit requested-review precedence](#explicit-requested-review-precedence)
instead of this fallback. Proceed only when all of these are true:

- the independent exact-head review found no actionable issues
- required checks are green on the latest head
- the PR is mergeable and `mergeStateStatus` is clean
- full PR comments, review bodies, review threads, and inline comments contain
  no unresolved actionable feedback for the current head
- the project/operator has authorized auto-merge for this PR or queue class

Read current review bodies and reactions directly. Do not infer the current
result from stale inline review-thread objects alone. The watcher must report
the exact current-head verdict or the latest-head eyes-to-`+1` lifecycle with
its timestamps and confirm that no later push occurred. Either terminal signal
stops the heartbeat from waiting for further artifacts or a fallback timeout;
it does not waive post-signal handling. For a head-named clean verdict, the
approximately five-minute post-clean settle and full review/thread/reaction
re-read remain mandatory before merge. If review feedback lands, fix or respond
to it, push the update, rerun the manual branch-diff review and required
local/CI checks, then evaluate the new exact head from scratch before
continuing toward merge.

If the wait cannot self-progress because checks stalled, review state is
ambiguous, or the implementer has not acknowledged a routed review-fix request,
the heartbeat must escalate by doorbell with the exact blocker and next action.
Delete or rewrite any PR-wait heartbeat that misses this escalation path.


Keep the heartbeat active until rerun checks, merge state, and current PR
comments/reviews are clean. Delete the PR-wait heartbeat immediately after the
merge, then continue normal post-merge cleanup in the same Codex thread.

## Autonomous Queue Runner State

For unattended or standing-instruction loops, record the current loop mode in:

```bash
python3 bin/autonomous_loop.py start --project <project_id> --agent codex --mode next_lane
```

The state file lives at:

```text
{project_state_root}/<project_id>/autonomous-loop.json
```

Use it to distinguish these states:

- `next_lane`: recover inbox/queues/PRs and activate the next safe lane
- `worker_wait`: a worker is active; check inbox and bridge status without
  interrupting visible work
- `acceptance`: worker checkpoint is ready for dirty-worktree/task-contract
  acceptance
- `pr_wait`: PR is open; re-check checks/reviews/comments/merge state
- `fix_loop`: blocker exists but can be fixed by Codex or re-delegation
- `post_merge`: merge completed; run cleanup and return to queue recovery
- `queue_empty`: no remaining lane after validation

There should be one queue-runner heartbeat per project loop. Task-specific
heartbeats are subordinate: create them only for a concrete wait such as Claude
Desktop, a worker handoff, or a PR, and delete/update them when the main
queue-runner state moves. Do not leave a stale task heartbeat competing with the
persistent queue runner.
## Post-merge

After merge:

1. fast-forward local `main`
2. run targeted post-merge smoke only when the merge is browser-relevant
3. evaluate the exact merge SHA through the project's configured release
   authority
4. only after terminal success or an explicit honest non-success disposition,
   move the local task from `review` to `done`
5. after the `review → done` transition succeeds, perform any required project
   queue refresh for lane-ordering or state changes
6. only then run the branch/worktree cleanup gate in applying mode

`PENDING`, `MISSING`, `FAILURE`, or `CANCELLED` stops this sequence: preserve
the task in `review` and preserve the implementation lane. Do not apply cleanup
or advance the queue runner beyond `post_merge`.

For a production-affecting merge, the `review → done` transition waits for the
release-closure gate below ("Release closure does not end at merge"): terminal
deploy+smoke success for the exact merge SHA, or an explicit Codex disposition
on a non-success. Never mark done with the release outcome unknown or red. A
docs-only or otherwise non-production-impacting merge exits via an explicit
scope disposition (recorded as such); a skipped deploy is never called deploy
success.

Within the `review → done` command, the target-state task contract is validated
at stage `done` before the release evaluator and before activity/task/queue
mutation. For projects with `db.production_schema_guard: true` this blocks `none` or
unapproved `local-schema-only` classification and preserves every required
`shared-supabase-required` evidence field across `success`, `non-production`,
and `risk-accepted-followup` dispositions. Existing done history remains
grandfathered. Post-merge cleanup is verification/application only, never an
alternate transition path.

For `llm-collab` itself, the shared local checkout is part of the shipped
workflow. After a PR that changes workflow docs, scripts, gates, skills, agent
routing, or queue behavior merges, fast-forward the canonical local checkout
that future sessions read before starting or handing off more work. For Amiga,
that checkout is `/Users/pixexid/Projects/llm-collab`.

If tracked local dirt blocks the fast-forward, stash or move the tracked dirt
aside with an explicit note. Do not leave the shared checkout behind
`origin/main` just because GitHub is up to date. Project-private untracked files
may remain untracked; do not delete or commit them just to sync tracked workflow
files.

The cleanup gate is:

```bash
python3 bin/post_merge_cleanup.py \
  --project <project_id> \
  --apply \
  --remove-plain-dirs \
  --discard-disposable-dirty \
  --fail-on-blockers
```

Run it from `/Users/pixexid/Projects/llm-collab`. For Amiga this command scans
the app repo and `/Users/pixexid/Projects/amiga-worktrees`, not only branch refs
visible from `/Users/pixexid/Projects/amiga`. The queue runner must not clear
`post_merge`, return to `idle`, or activate the next lane until this command has
either:

- removed all safe stale worktrees, stale branches, and disposable plain
  directories; or
- reported every deferred dirty/active item with a concrete reason in the
  current thread or task notes.

Stay in the same Codex thread after merge/local cleanup by default. Do not send
a `codex -> codex` self-handoff or force a fresh `check inbox` thread unless the
operator explicitly asks for a new session/handoff or context safety requires a
thread boundary.

Workflow/process edits are first-class deliverables, not disposable local dirt.
If an orchestrator edits repo instructions, skills, workflow docs, queue
scripts, agent memory, or bridge/runtime instructions while fixing a process
failure, it must classify that diff before starting the next lane:

- own PR
- explicitly bundled into the current task PR
- intentionally abandoned or kept local with operator approval

Run `git status --short --branch --untracked-files=all` in each touched repo,
commit/push/open the PR for changes that should persist, and record any
intentional local remainder. Do not assume future merge cleanup will preserve
uncommitted workflow edits.

## Release closure does not end at merge (GH-1524)

A **production-affecting** merged PR is not a closed release until the **main
production deploy for the exact merge SHA** — including its post-deploy smoke —
reaches terminal success. Docs-only merges intentionally skip the heavy deploy
job in Amiga's `deploy.yml`; a skipped deploy is a no-op run, never
"deploy+smoke success", and such merges are outside this gate's scope.
The df55a282 incident proved the gap: a post-deploy smoke failure (run
29537490993) sat unnoticed for hours because nothing consumed the deploy
signal, and a later unrelated green deploy looked like cover.

The gate, enforced with `bin/deploy_release_watch.py`:

```bash
bin/deploy_release_watch.py --project amiga --merge-sha <full-merge-sha> [--wait]
```

The repo, base branch, workflow, and required job/smoke-step evidence come from
the project's `release_closure` object in `projects.json` (project boundary:
job/step names are project-specific and never live in shared `bin/`). A project
without that config fails closed with exit 64.

- **Exact-SHA correlation is absolute.** A deploy run for a different or
  earlier SHA never satisfies this merge's closure, no matter how green.
- **Only the automatic run counts**: the project's configured
  `release_closure.trigger_event` on its configured `default_branch_base`
  (Amiga: `push` on `main`). A same-SHA run under any different event or
  branch is non-authoritative and never satisfies — or supersedes — the
  configured automatic run's outcome.
- **Success = deploy AND post-deploy smoke terminal success** for that exact
  SHA, proven by POSITIVE evidence: every job named in the project's
  `release_closure.required_jobs` present and successful (a skipped required
  job = not a release) and every configured `required_smoke_steps` present and
  successful inside the configured `smoke_job`. All names come from the
  project's `projects.json` `release_closure` — no project inherits another's
  labels. Empty or partial run evidence fails closed.
- **`FAILURE` / `CANCELLED` / `MISSING` are each actionable**: the watcher
  sends ONE durable llm-collab packet plus ONE doorbell ring. A missing run is
  a distinct alarm, never silence and never a pass.
- **On any non-success the task is NOT done**: closure is blocked until Codex
  records a terminal disposition. Preserve the run id and logs
  (`gh run view <id> --log-failed`); **no blind retry or redeploy** as the
  reflex response.
- **Ownership:** Claude is the ongoing main-deploy watcher; Codex is the
  terminal task/release closer.

Do not idle the active thread just to wait for asynchronous deploy automation if local post-merge
work is already complete. Treat deploy as a later checkpoint unless it has actually failed or a new
production-impacting merge would stack on top of an unresolved deploy state.

For `llm-collab` itself, refreshing `main` after every merge is mandatory before
new coordination work starts from a persistent checkout. Workers and new
sessions must not keep using an old feature branch as the collaboration runtime
after its changes merge.

Use:

```bash
git fetch origin main
git switch main
git pull --ff-only origin main
git status --short --branch --untracked-files=all
```

Do not delete or commit project-private untracked files during this refresh.
Examples include `.secrets/`, local runtime state, generated worker memory
templates, and project-local config examples. They should remain local unless a
separate reviewed task explicitly promotes them into the open-source repo.

## Branch/worktree cleanup contract

Post-merge cleanup is required, not optional.

- remove merged `codex/review/*` branches (local and remote)
- remove stale worker branches only when their lane is verified complete
- remove stale worktrees only when their lane is verified complete
- keep only active worktrees and one intentional root parking branch (or `main`)

Do not treat `merged` as sufficient evidence that a worker branch/worktree is disposable.

Worker branches/worktrees are deletion candidates only when all of the following are true:

1. the related PR is merged or the related issue is closed
2. the related local task mirror has an exact `project_id` match for the
   cleanup command's `--project`; a missing, empty, null, or foreign project ID
   is not a task match
3. the related local task mirror is `done`, not `open`, `in_progress`, `blocked`, or `review`
4. the branch is not the active branch of any existing worktree
5. the worktree is clean enough to discard (`git status --short --untracked-files=all`)
6. the branch tip is merged into `main` or patch-equivalent to a merged commit on `main`
7. no active chat/task/brief still points to that branch/worktree as the implementation lane

If any one of those checks fails, defer cleanup.

Safe cleanup order:

1. fetch/prune refs (`git fetch --all --prune`)
2. split cleanup candidates into:
   - safe now: merged review branches and worker lanes whose task is `done`
   - defer: any branch/worktree still referenced by an active task/chat or still carrying non-disposable files
3. verify each candidate worktree is disposable (`git status --short --untracked-files=all`)
4. verify each candidate branch is merged or patch-equivalent to `main`
5. remove stale worktrees first (`git worktree remove [--force] <path>`)
6. prune stale worktree metadata (`git worktree prune`)
7. delete stale local branches (`git branch -d` or `-D` when explicitly safe)
8. delete stale remote branches (`git push origin --delete <branch...>`)

Do not keep merged branch clutter; clean branch lists are required for reliable lane selection.

The manual sequence above is the policy model. The executable gate above is the
required loop mechanism. If the gate reports `ok_to_clear_post_merge: false`,
the queue runner is still in `post_merge` or `fix_loop`, not `next_lane`.
