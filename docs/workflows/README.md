# Workflows

Universal collaboration workflows that apply across projects.

Use these docs as defaults, then layer project-specific overrides from local `{project_state_root}/{project_id}/`.

Only `projects/_example/` is intended to be tracked in the open-source repo. Real project directories are local runtime state and should normally live outside the Git checkout via `project_state_root` in `collab.config.json`.

Recommended read order:

1. `session-startup.md`
2. `claude-code-desktop-computer-use-bridge.md` when Claude desktop app
   interaction or unattended Claude responses are expected
3. `session-autobridge-runbook.md`
4. `task-intake-and-delegation.md`
5. `review-and-handoff.md`
6. `isolated-worktrees.md`
7. `commit-push-prs.md`
8. `github-projects.md`
