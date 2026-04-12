# Task Intake And Delegation

## Goal

One owner, one scope, one verification plan.

## Intake order

1. pass startup/preflight
2. create or identify the chat
3. read the project queue artifact when the project maintains one
4. create/update the task
5. update the queue when owner/order/dependency/activation state changes
6. provision branch/worktree first when the lane is isolated-worker implementation
7. assign one owner
8. send one clear delegation message
9. move task to `in_progress`
10. then request activation relay
11. then begin implementation

## Canonical ordered queue

If the project defines a canonical queue artifact, treat it as the ordered source of truth for
remaining issue-sized lanes.

- keep the queue outside chat threads
- read it during fresh-session recovery before selecting the next lane
- update it whenever lane order, owner, queue state, dependency state, or task status changes
- when the queue has a generated Markdown view, regenerate it after JSON edits
- if `claim_task.py --status in_progress` targets a queued lane that is not `ready`, the transition should fail unless an explicit queue-override flag is used

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
- explicit checkpoint-commit requirement for worker-owned implementation lanes
- required handoff evidence for acceptance:
  - checkpoint commit SHA
  - assigned branch confirmation
  - `git status --short --untracked-files=all`
  - disposition of any remaining tracked or untracked files

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
