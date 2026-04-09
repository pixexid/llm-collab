# Isolated Worker Worktrees

## Goal

Prevent overlapping edits by isolating worker implementation in per-task worktrees.

## Branch model

- worker branch: task implementation only
- review branch: orchestrator integration and preview gate
- main branch: post-merge only

## Lifecycle

1. orchestrator creates branch/worktree for task/agent
2. implement in assigned worktree only
3. run required verification in that worktree
4. create the required worker checkpoint commit on the assigned branch before handoff
5. hand off for orchestrator review with branch/status evidence
6. mark integrated after acceptance
7. retire/remove worktree and branch after integration

## Provisioning ownership

- For worker-owned implementation lanes, the orchestrator provisions the branch and worktree before requesting worker activation.
- Do not activate a worker on a merely planned lane.
- The orchestrator must verify the lane exists with local git/worktree state before relay.
- Use planned wording only before create:
  - `planned branch`
  - `planned worktree`
- Use assigned wording only after create:
  - `assigned branch`
  - `assigned worktree`

Minimum pre-activation gate:

1. create the worker branch/worktree
2. verify branch exists
3. verify worktree exists
4. record exact branch/worktree/base metadata in task/chat
5. only then request operator relay

## Provisioning ownership

- For worker-owned implementation lanes, the orchestrator provisions the branch and worktree before requesting worker activation.
- Do not activate a worker on a merely planned lane.
- The orchestrator must verify the lane exists with local git/worktree state before relay.
- Use planned wording only before create:
  - `planned branch`
  - `planned worktree`
- Use assigned wording only after create:
  - `assigned branch`
  - `assigned worktree`

Minimum pre-activation gate:

1. create the worker branch/worktree
2. verify branch exists
3. verify worktree exists
4. record exact branch/worktree/base metadata in task/chat
5. only then request operator relay

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
- worker-owned isolated implementation lanes require a checkpoint commit before the task can move to `review`
- orchestrator acceptance must include branch verification plus `git status --short --untracked-files=all` before integrating a worker slice
- after merge, remove retired worker worktrees before deleting their local branches
- after local cleanup, prune stale worktree metadata
