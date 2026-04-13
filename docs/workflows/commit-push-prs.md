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

## Post-merge

After merge:

1. fast-forward local `main`
2. run targeted post-merge smoke only when the merge is browser-relevant
3. update the project queue artifact when lane ordering/state changes
4. clean stale branches/worktrees
5. mark local task done

Do not idle the active thread just to wait for asynchronous deploy automation if local post-merge
work is already complete. Treat deploy as a later checkpoint unless it has actually failed or a new
production-impacting merge would stack on top of an unresolved deploy state.

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
