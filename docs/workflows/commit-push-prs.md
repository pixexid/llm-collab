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

Before PR creation, run project-required verification on the review branch, then run targeted smoke checks for changed flows.

## PR requirements

Include:

- linked issue/task
- verification summary
- risk notes
- docs-sync confirmation when behavior contracts changed

## Post-merge

After merge:

1. fast-forward local `main`
2. run targeted post-merge smoke
3. clean stale review branches/worktrees
4. mark local task done

