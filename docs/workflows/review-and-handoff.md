# Review And Handoff

## Worker completion contract

Handoff replies should include:

- agent identity
- files changed
- commands run
- verification result
- known risks/open questions
- task readiness (`review` or `blocked`)

For worker-owned isolated-worktree implementation lanes, handoff replies must also include:

- checkpoint commit SHA
- assigned branch confirmation
- `git status --short --untracked-files=all`
- disposition of any remaining tracked or untracked files

For UI/UX lanes, handoff replies and the linked task contract must also include:
- `design_docs_read`
- `design_skills_used`
- `impeccable_detect_result`
- `browser_validation_desktop`
- `browser_validation_mobile`
- `operator_visual_feedback_requested`
- `design_doc_update_decision`

For `shared-supabase-required` lanes, handoff replies and the linked task contract must also include:
- `db_project_ref`
- `db_migration_files` when schema change is involved
- `db_apply_result`
- `db_schema_assertion`
- `db_advisors_result` for schema-change lanes
- `db_runtime_validation`

## Task status guide

- `open`: created, not started
- `in_progress`: actively owned
- `blocked`: cannot progress without external input
- `review`: ready for orchestrator review
- `done`: reviewed and accepted

## Handoff flow

1. worker creates the required checkpoint commit when the lane is an isolated-worktree implementation task
2. worker updates task status (`review` or `blocked`)
3. worker replies in the same task-linked chat
4. orchestrator verifies
5. for worker-owned isolated-worktree implementation lanes, orchestrator runs a dirty-worktree acceptance gate before acceptance:
   - verify the assigned branch matches the task contract
   - capture `git status --short --untracked-files=all`
   - confirm the checkpoint commit exists on that branch
   - record the disposition of any remaining files in task/chat notes
6. if the worktree is still dirty, orchestrator blocks acceptance unless the worker adds the missing checkpoint commit, explains why specific files must remain dirty, or the orchestrator records an explicit waiver with the reason
7. orchestrator either blocks, reassigns, or accepts
8. if the project maintains a canonical queue artifact, orchestrator updates queue state/order before selecting the next lane
9. accepted tasks move to `Tasks/done`

Hard rule for UI/UX lanes:
- `claim_task.py --status review` should fail if the task contract is missing the required UI evidence
- PR/review gating should fail again if the same task still does not satisfy the UI contract

Hard rule for shared-Supabase lanes:
- `claim_task.py --status review` should fail if the task contract is missing the required DB evidence
- migration files in git do not count as acceptance without shared-project apply + assertion

When the last queued lane moves to `done`, archive the final queue snapshot and keep the canonical
queue path in an explicit empty state instead of deleting it.

## Thread-boundary handoff rule

If context must continue in a fresh orchestrator thread, send a self-handoff message before ending context. Include:

- task/issue identifiers
- related chat path
- branch/worktree state
- files/docs to read first
- current state and next concrete action
