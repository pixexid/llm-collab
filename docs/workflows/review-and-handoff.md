# Review And Handoff

## Worker completion contract

Handoff replies should include:

- agent identity
- files changed
- commands run
- verification result
- known risks/open questions
- task readiness (`review` or `blocked`)

## Task status guide

- `open`: created, not started
- `in_progress`: actively owned
- `blocked`: cannot progress without external input
- `review`: ready for orchestrator review
- `done`: reviewed and accepted

## Handoff flow

1. worker updates task status (`review` or `blocked`)
2. worker replies in the same task-linked chat
3. orchestrator verifies
4. orchestrator either blocks, reassigns, or accepts
5. accepted tasks move to `Tasks/done`

## Thread-boundary handoff rule

If context must continue in a fresh orchestrator thread, send a self-handoff message before ending context. Include:

- task/issue identifiers
- related chat path
- branch/worktree state
- files/docs to read first
- current state and next concrete action

