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
3. clean stale branches/worktrees
4. mark local task done

## Branch/worktree cleanup contract

Post-merge cleanup is required, not optional.

- remove merged `codex/review/*` branches (local and remote)
- remove stale worker branches that were tied to completed lanes (for example `codex/cdx2/*`, `codex/claude/*`)
- remove stale worktrees for those branches
- keep only active worktrees and one intentional root parking branch (or `main`)

Safe cleanup order:

1. fetch/prune refs (`git fetch --all --prune`)
2. verify each candidate worktree is disposable (`git status --short --untracked-files=all`)
3. remove stale worktrees first (`git worktree remove [--force] <path>`)
4. prune stale worktree metadata (`git worktree prune`)
5. delete stale local branches (`git branch -d` or `-D` when explicitly safe)
6. delete stale remote branches (`git push origin --delete <branch...>`)

Do not keep merged branch clutter; clean branch lists are required for reliable lane selection.
