# Session Startup

## Goal

Start from a known-good environment before claiming or editing work.

## Choose the worker surface first

BB is the normal worker fleet. For a provider-backed worker assignment, follow
[`bb-workers.md`](bb-workers.md) before starting any collaboration bootstrap. It
owns worker isolation, spawn, communication, inspection, and completion proof;
do not copy its commands here.

A BB worker is not an llm-collab participant: it has no `agents.json` identity,
exact-session binding, durable-mailbox authorship, or delivery receipt. The
orchestrator remains the integration point and communicates with that worker
through BB. Only workspace acquisition differs: after `bb-workers.md` provides
the verified managed worktree, every BB worker must still complete
[`Read before acting`](#read-before-acting) and
[`Required preflight`](#required-preflight) before editing. The first-class
bootstrap, binding, and watcher sections apply only when a worker is explicitly
being enrolled as a durable-mailbox participant.

## Bootstrap first

```bash
<runtime_root>/bin/llm-collab current_runtime.py --agent <agent_id>
```

`<runtime_root>` is the deployed runtime (normally
`~/.local/share/llm-collab/runtime/main`), not a parked or lane checkout. Source
checkouts may be dirty; they are never session or watcher launch roots.

The launcher fetches `origin/main`, verifies ancestry and the contract marker, then
invokes the repository-local bootstrap. Use `<runtime_root>/bin/llm-collab
current_runtime.py --check` to report the verified heads without starting a
session or watcher.

For an explicitly enrolled interactive collab worker, startup is not complete
until the exact native session watcher and its target/sibling probes pass. Follow
[`collab-thread-quickstart.md` → Bootstrap](collab-thread-quickstart.md#1-bootstrap).
Do not treat the agent-wide watcher reported by `session_bootstrap.py` as that
proof.

## Keep The Tooling Current

`llm-collab` is the shared coordination tool. Keep the parked operator checkout
out of the runtime path. Refresh the deployed runtime from a fresh isolated
source worktree:

Safe refresh flow:

```bash
<source_worktree>/bin/llm-collab deploy_runtime.py \
  --source <source_worktree>
```

The deploy command requires the named source to be an exact `origin/main`, then
performs one fenced deployment transaction:

1. Preflight the source and deployed target, including the target's current head,
   clean tracked state, PM2 availability, and both old/new workspace names.
2. Read `pm2 jlist` and stop every persistent PM2 app owned by either workspace
   name. The command verifies that no owned app remains live before changing the
   deployed tree.
3. Advance the deployed runtime only after that fence, then read the target's
   current `pm2/ecosystem.config.cjs` and stop/delete every owned PM2 app omitted
   from the new ecosystem.
4. Run `pm2 startOrRestart <target>/pm2/ecosystem.config.cjs --update-env`, verify
   the target HEAD, PM2 roster/status/cwd/script/args, and a non-streaming log probe
   for each app, then run `pm2 save` so removed apps do not return after reboot.

Any failure after the fence rolls the target back to its previous head and restores
the previous ecosystem/PM2 roster. If rollback or restoration cannot be verified,
the command fails loudly with both errors. It leaves runtime-state symlinks and
source-checkout files untouched; a stale source, contract mismatch, dirty target,
failed fence, or unverifiable PM2 state refuses before advancing the target.

Do not use these commands against a parked or dirty operator checkout:

- `git switch main`
- `git pull --ff-only origin main`

Untracked/gitignored files normally persist across branch switches. Git blocks
the switch instead of silently overwriting untracked files that conflict with
tracked files on the target branch. This is intentional: project-local secrets,
runtime state, worker memory templates, and operator/private config should stay
local in this open-source repo.

Real project runtime state should not depend on that Git behavior. Configure
`project_state_root` in `collab.config.json` to a directory outside the
`llm-collab` checkout, such as:

```json
{
  "project_state_root": "~/.local/share/llm-collab/projects"
}
```

Queues, project runbooks, roles/routing files, and memory templates then live at
`{project_state_root}/{project_id}/`. After any merge or branch switch, verify
the active queue from that external state root:

```bash
python bin/project_issue_queue.py show --project <project_id>
```

Do not copy real `projects/{project_id}` directories back into the public repo
as tracked files. The in-repo `projects/_example/` directory is only a template.

## Read before acting

1. collaboration inbox
2. active task board
3. project-level instructions (`{project_state_root}/<project_id>/...` when present locally)
4. repo-specific contributing/agent guidance

## Required preflight

Do not claim tasks or edit code until the active checkout is healthy.

Typical preflight checks:

- dependencies installed
- environment files present/readable
- project build/test command surface usable
- GitHub access usable (if this lane needs GitHub)
- browser/runtime validation path usable (if this lane needs it)

If any item fails: stop, fix environment, re-run checks.

## Session-autobridge validation rule

When validating worker wake/resume behavior, do not target the active operator thread.

Use a disposable worker session instead, especially for Codex app tests:

1. bind or refresh a disposable worker session
2. if testing failure retry, use a bounded adapter that is known to return
   nonzero before runtime acceptance on its first pass
3. send the routed message to the disposable target session
4. inspect watcher/inbox state

Current retry acceptance (not busy-queue protection):

- a known pre-acceptance runtime failure emits `autobridge_failed` and leaves
  the message in `unread`
- a later known-success watcher pass emits `autobridge_consumed` and moves the
  message to `read`

Session autobridge currently has no authoritative Codex busy/idle check, no
inbox `queued` field, and no `autobridge_deferred_busy` event. Do not use a
running operator thread to test retries or describe this behavior as safe busy
deferral. The planned transactional contract is in
`thread-event-runner-rfc.md`; exact-thread delivery remains disabled there
until busy and turn-acceptance/idempotency behavior is integration-proven.

For Codex manual watcher checks, `watch_inbox.py` should behave the same as the PM2 watcher by default:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

## First-class mailbox waits and wakes

For a registered llm-collab participant, `Chats/` remains the transport of
record. Follow [`session-autobridge-runbook.md`](session-autobridge-runbook.md)
for binding, watcher, dispatch, receipt, and recovery mechanics instead of
reproducing them in this entry point.

Contract v12's fallback predicate is unchanged:
`wake_fallback_allowed = not autobridge_ready and not
dispatch_scope_refused`. AX is a conditional Codex-only fallback that
`deliver.py` may offer under that predicate; it is not a routine worker lane or
a BB transport. Run only the exact command `deliver.py` prints. Whether the
offered doorbell can land is a runtime property that must be checked live for
that attempt—never infer it from process state or record a window count as a
standing capability.

`autobridge_ready: true` proves send-time routability, not delivery. Require the
receipt or recipient evidence named by the runbook. When no AX command is
printed, diagnose the binding, watcher, or dispatch path; do not invent a ring
or disable a working binding to force the fallback.
