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

Before committing or opening a PR, the orchestrator must manually review the
final branch diff against the target branch or merge base. Treat this as the
primary code-review gate:

- review against the merge base or target branch, not just the last edited file
- check correctness, regressions, missing verification, contract drift, and
  workflow/process consistency
- fix actionable findings before commit when the fix is local and bounded
- rerun the affected verification after fixes
- only then commit, push, and open the PR

This manual branch-diff review is the repeated review loop. GitHub Codex PR
review is an external safety net, not the mechanism that should discover routine
issues for the first time.

Do not manually comment `@codex review` when opening a PR. If GitHub Codex is
enabled for the repository, consume the automatic PR review/comment/reaction
when it appears. If no automatic artifact appears, use the opportunistic policy
below instead of creating a manual review request.

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
- GitHub Codex review must not become an infinite wait when no new review
  artifact appears after PR creation
- do not request another GitHub Codex review after a narrow fix that directly
  addresses a PR comment and does not materially expand the diff; the
  current-head GitHub Codex artifact requirement is waived only for that narrow
  review-fix commit
- if the connector fails, stays silent, or only reacts positively, the
  orchestrator may proceed after inspecting the full PR comment/review state
  and confirming there is no current actionable feedback

If the PR is waiting only for remote checks or remote review state, keep it open
and create or update a Codex heartbeat attached to the current thread with a
6-minute cadence. Each heartbeat must re-check the PR checks, review state,
review threads/comments, automatic connector reactions, and merge state.

When the operator has authorized the merge path for the PR or PR class, the
heartbeat may complete the wait after it verifies the latest head has green
required checks, clean `mergeStateStatus`, local/orchestrator review completed,
and no unresolved current review feedback. Treat the GitHub Codex review signal
as clean when either:

- before any review-fix commit, the latest top-level
  `chatgpt-codex-connector` review/comment for the reviewed head reports no
  actionable or major issues, or
- the connector reacted positively to the PR or latest reviewed head and no new
  inline/top-level actionable comments were created after that signal.

If no new GitHub Codex review artifact appears, do not wait forever. After at
least one heartbeat cycle, proceed when all of these are true:

- local/orchestrator review found no actionable issues
- required checks are green on the latest head
- `mergeStateStatus` is clean
- full PR comments, review bodies, review threads, and inline comments contain
  no unresolved actionable feedback for the current head
- the project/operator has authorized auto-merge for this PR or queue class

Read current review bodies and reactions directly. Do not infer the current
result from stale inline review-thread objects alone, and do not keep a heartbeat
waiting indefinitely for a comment when the connector signaled clean review via
reaction. If review feedback lands, fix or respond to it, push the update, rerun
the manual branch-diff review and required local/CI checks, treat the resolved
review thread plus current PR state as the GitHub Codex signal, then continue
toward merge without asking GitHub Codex for another review when the fix is
narrow.

Request another GitHub Codex review only when the follow-up materially changes
the PR, for example:

- broad refactor or new behavior beyond the reviewed comment
- new files or surface area not covered by the prior review
- DB, API, auth, security, payment, deployment, or workflow semantics changed
- merge-conflict resolution changed code meaning
- CI failure required non-trivial edits

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
3. update the project queue artifact when lane ordering/state changes
4. run the executable branch/worktree cleanup gate
5. mark local task done

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
2. the related local task mirror is `done`, not `open`, `in_progress`, `blocked`, or `review`
3. the branch is not the active branch of any existing worktree
4. the worktree is clean enough to discard (`git status --short --untracked-files=all`)
5. the branch tip is merged into `main` or patch-equivalent to a merged commit on `main`
6. no active chat/task/brief still points to that branch/worktree as the implementation lane

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
