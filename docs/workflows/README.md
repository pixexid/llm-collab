# Workflows

Universal collaboration workflows that apply across projects.

Use these docs as defaults, then layer project-specific overrides from local `{project_state_root}/{project_id}/`.

Only `projects/_example/` is intended to be tracked in the open-source repo. Real project directories are local runtime state and should normally live outside the Git checkout via `project_state_root` in `collab.config.json`.

Recommended read order:

1. `session-startup.md`
2. `claude-code-desktop-computer-use-bridge.md` — the canonical agent-to-agent
   comms reference (bidirectional Computer-Use doorbell + `llm-collab` mailbox);
   read whenever desktop-app agents need to notify each other
3. `session-autobridge-runbook.md` — provisional safety-fuse only (polling is no
   longer the primary wake; see the doorbell doc)
4. `thread-event-runner-rfc.md` — Phase 1 architecture/threat contract for a
   planned durable event runner; no runner or exact-thread dispatcher is
   implemented by this RFC
5. `task-intake-and-delegation.md`
6. `review-and-handoff.md`
7. `isolated-worktrees.md`
8. `commit-push-prs.md`
9. `github-projects.md`
