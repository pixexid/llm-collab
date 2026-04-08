# Isolated Worker Worktrees

## Goal

Prevent overlapping edits by isolating worker implementation in per-task worktrees.

## Branch model

- worker branch: task implementation only
- review branch: orchestrator integration and preview gate
- main branch: post-merge only

## Lifecycle

1. create worktree for task/agent
2. implement in assigned worktree only
3. run required verification in that worktree
4. hand off for orchestrator review
5. mark integrated after acceptance
6. retire/remove worktree after integration

## Required metadata

Track on each worktree entry:

- task id
- agent
- repo
- worktree path
- branch
- base ref
- base sha
- integrated state
- retired state

## Guardrails

- refuse removal of dirty worktree unless explicitly forced
- refuse retirement before integration unless explicitly forced
- workers do not push or open PRs

