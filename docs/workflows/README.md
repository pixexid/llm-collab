# Workflows

Universal collaboration workflows that apply across projects.

Use these docs as defaults, then layer project-specific overrides from local `{project_state_root}/{project_id}/`.

Only `projects/_example/` is intended to be tracked in the open-source repo. Real project directories are local runtime state and should normally live outside the Git checkout via `project_state_root` in `collab.config.json`.

Recommended read order:

1. `session-startup.md`
2. `claude-code-desktop-computer-use-bridge.md` — the canonical agent-to-agent
   comms reference (Codex-only Computer-Use doorbell + bidirectional `llm-collab`
   mailbox); read whenever desktop-app agents need to notify each other
3. `session-autobridge-runbook.md` — routine exact-session dispatch, the primary
   wake for every watcher-backed recipient (contract v12); bounded
   polling/heartbeat observation remains a safety-fuse only
4. `thread-event-runner-rfc.md` — Phase 1 architecture/threat contract for a
   planned durable event runner; no runner or exact-thread dispatcher is
   implemented by this RFC
5. `observation-global-cadence-rfc.md` — design contract for the GH-179 global
   observation cadence budget, GH-183 pinned-root precondition, and GH-181 audit
   accounting before scheduler implementation
6. `pi-workers.md` — how Pi-runtime workers (glmpi/relay/kimi) use the shared
   Livecraft host, what they inherit by default (ponytail is runtime-global),
   and how the production first-start binding is gated
7. `task-intake-and-delegation.md`
8. `review-and-handoff.md`
9. `isolated-worktrees.md`
10. `commit-push-prs.md`
11. `github-projects.md`
