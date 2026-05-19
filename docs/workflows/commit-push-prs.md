# Commit, Push, And PR Workflow

## Goal

No lane is PR-ready until local validation and required metadata are complete.

## Hard rules

- workers do not push
- do not push directly to `main`
- do not open PRs without linked tracking context (issue/task)
- require full local verification for the affected surface before PR

## Suggested branch layers

- worker branch: implementation
- review branch: integration + preview gate
- main: merge target

## Review-branch gate

Before PR creation, run project-required verification on the review branch.
Run browser/smoke checks only when the lane touches browser-relevant behavior.
Use the project's primary browser path first, and run fallback browser tooling only if the primary path fails.
Do not create the review branch from a worker lane until the worker handoff acceptance gate has already passed, including branch verification, checkpoint-commit verification, and `git status --short --untracked-files=all`.

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
- GitHub Codex review/comments are consumed when they appear
- GitHub Codex review must not become an infinite wait when no new review
  artifact appears after the review request
- if the connector fails, stays silent, or only reacts positively, the
  orchestrator may proceed after inspecting the full PR comment/review state
  and confirming there is no current actionable feedback

If the PR is waiting only for remote checks or remote review state, keep it open
and create or update a Codex heartbeat attached to the current thread with a
6-minute cadence. Each heartbeat must re-check the PR checks, review state,
review threads/comments, review-request reactions, and merge state.

When the operator has authorized the merge path for the PR or PR class, the
heartbeat may complete the wait after it verifies the latest head has green
required checks, clean `mergeStateStatus`, local/orchestrator review completed,
and no unresolved current review feedback. Treat the current GitHub Codex review
signal as clean when either:

- the latest top-level `chatgpt-codex-connector` review/comment for the current
  head reports no actionable or major issues, or
- the connector reacted positively to the latest operator `@codex review`
  request for the current head and no new inline/top-level actionable comments
  were created after that request.

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
reaction. If review feedback lands, fix or respond to it, push the update, and
keep the heartbeat active until rerun checks, merge state, and review signals are
clean. Delete the PR-wait heartbeat immediately after the merge, then continue
normal post-merge cleanup in the same Codex thread.

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
4. clean stale branches/worktrees
5. mark local task done

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
