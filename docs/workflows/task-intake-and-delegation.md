# Task Intake And Delegation

## Goal

One owner, one scope, one verification plan.

## Intake order

1. pass startup/preflight
2. create or identify the chat
3. create/update the task
4. provision branch/worktree first when the lane is isolated-worker implementation
5. assign one owner
6. send one clear delegation message
7. move task to `in_progress`
8. then request activation relay
9. then begin implementation

## Preflight gate split

- task-claim preflight (`claim_task.py` to `in_progress` or `review`) is a tooling/env gate, not a browser gate
- `claim_task.py` appends `--browser-check skip` when it runs project preflight
- browser validation should run later only for runtime/UI-impact lanes

## Required task fields

- `task_id`
- `title`
- `status`
- `owner`
- `created_by`
- `requested_by`
- `priority`
- `project_id`
- `related_chat`
- `related_paths`

## Delegation message requirements

- exact goal
- files/modules in scope
- docs to read first
- success criteria
- explicit non-goals
- task id
- verification commands
- handoff format (files changed, commands run, verification result, blocker/ready)

When isolated worktrees are used, include:
- exact worktree path
- branch
- base ref and base SHA
- allowed workspace
- checkpoint commit expectation

For worker-owned isolated lanes, those values must be provisioned and verified by the orchestrator before relay.
Do not phrase a planned branch/worktree as already assigned.

## Activation rule

When multiple workers are involved, state activation order explicitly:

- who should act now
- who should wait
- what condition triggers next activation

## Relay enforcement (hard rule)

Do not request operator relay for workers that are not ready to start.

- Send relay only for workers in `in_progress` state that should execute now.
- A worker is not ready to start until its required branch/worktree already exists when isolated mode is expected.
- For queued workers, update task ownership/status and keep instructions in task/chat, but do not request activation relay yet.
- When a queued worker becomes ready, send a single activation message and then request relay.

Required operator instruction format:

- single activation: `activate <worker> now`
- parallel activation: `activate <worker-a> + <worker-b> now in parallel`
- queue-only instruction: `do not activate yet; waiting on <condition>`

Never dump multiple relay prompts without explicit activation order.
If order is sequential, provide only the first relay and wait until the trigger condition is met before requesting the next.
