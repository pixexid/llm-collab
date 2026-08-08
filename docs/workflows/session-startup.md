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
through BB. BB changes workspace acquisition, not project preflight: after
`bb-workers.md` provides the verified managed worktree, the worker reads the
supplied task, project, and repository context and completes
[`Required preflight`](#required-preflight) before editing. The orchestrator
drains the collaboration inbox and performs any exact identity-bearing read
that requires `--me`; the BB worker has no collaboration identity and must not
impersonate one. The first-class bootstrap, binding, and watcher sections apply
only when a worker is explicitly being enrolled as a durable-mailbox
participant.

## Bootstrap first

In a new workspace, complete the canonical
[PM2 Log Rotation workflow](pm2-log-rotation.md) before the first
watcher-enabled bootstrap. Bootstrap may restart the agent's PM2 watcher;
subsequent session starts use the already-configured rotation setup.

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

### Restarted first-class sessions

A restarted session owns reporting its binding drift because only it knows its
new native runtime id. Bootstrap detects and reports an exact mismatch but never
mutates or refuses; when several active scopes could be peers, it reports the
ambiguity without naming a supersession target. There is currently no
self-service repair command for a non-Pi canonical binding: ordinary
`register --supersedes-session` updates the session and file binding but not the
canonical ledger, so bootstrap deliberately does not print that ineffective and
potentially destructive command. If either the session scan or canonical ledger
read cannot complete, bootstrap continues but reports the drift check as
unavailable rather than claiming the binding is clear.

Rebinding does not recover packets already addressed to the dead runtime id.
Those packets stay invisible to exact reads because the exact-read target filter
skips non-matching ids rather than refusing. Find the drift-window mail directly:

```bash
grep -R -- "target_session_id: <dead-id>" Chats/
```

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
   clean tracked state, PM2 availability, and both old/new workspace names. Before
   any mutation, refuse if a process the ecosystem does not declare is executing
   the deployed runtime's own `bin/watch_inbox.py` — a live dispatcher outside the
   fence — so code replacement never happens under it and rollback is never
   reached for that condition.
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

The managed PM2 watcher has one canonical invocation shape, generated by
`pm2/ecosystem.config.cjs`:

```text
<configured-python> <runtime_root>/bin/watch_inbox.py --me <agent_id> \
  --poll-seconds <configured-seconds> --skip-existing [--notify]
```

`--notify` is present when notifications are enabled. The PM2 watcher is a real
agent-wide dispatcher, not a notification-only operator surface: it intentionally
has neither `--session` nor `--no-autobridge`, and it requires each candidate
session's exact canonical binding before dispatch. Notifications are additive;
they do not change that role.

This is a configuration contract, not a claim about live PM2 state. The deploy
transaction above reads the current ecosystem and fails unless every managed
process's live script and arguments match it, and also refuses when a process
the ecosystem does not declare is running the deployed runtime's own
`bin/watch_inbox.py`, refusing before the fence so no code replacement happens
under a live dispatcher — so an undeclared watcher cannot report conformance
green.
The match is on the property (a process executing that script), never a PM2 name
pattern, so unrelated entries on this host are not implicated. Use that check
whenever current runtime conformance matters; do not derive or preserve a second
invocation.

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

An enrolled first-class participant follows all four items below under its own
identity. For a BB assignment, the orchestrator owns item 1 and any item 2 read
that requires a collaboration identity, then supplies the relevant task/board
context in the delegation. The BB worker follows that supplied context plus
items 3-4; it never supplies another participant to an identity-bearing read.

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

## Collab-loop monitor ownership

Claude owns ongoing collab-loop monitoring for PR/review status, bot-review
comments, inbox replies, and doorbell handoffs. Codex should usually check live
state once while actively gating/reviewing, then hand any continuing watch to
Claude through the durable mailbox.

Use a Codex heartbeat only for a genuinely Codex-side wait or when Claude cannot
own the watch. Before creating one, clear any stale monitor for the same target.
Keep one monitor per purpose, and delete or update it as soon as the purpose is
served.

For BB lanes, the orchestrator uses BB read surfaces only for worker completion
and stall inspection per `bb-workers.md`; GitHub review status and bot-review
comments remain on GitHub, and this section governs enrolled first-class
participants.

## Claude Desktop Rule

BB workers do not enter this path. The former use of the Codex app as a worker
surface is dormant; use `bb-workers.md` for worker assignments. The mechanics
below remain live for explicitly registered first-class mailbox participants
and attended recovery, and do not authorize provisioning a Codex-app worker.

Claude has one wake path in every registration and project shape: the durable
`Chats/` packet, picked up by the Claude app's own background inbox watcher.
Neither `activation.ax_app` nor `claude_desktop_bridge` changes that — `deliver.py`
excludes Claude from the AX doorbell selector, and the Computer Use fallback that
`claude_desktop_bridge` used to select is removed.

Important distinction:

- Claude desktop app visible sidebar threads are backed by app-managed Electron state under:
  - `~/Library/Application Support/Claude/IndexedDB/...`
  - `~/Library/Application Support/Claude/Session Storage/...`
- Claude CLI/project sessions are backed by:
  - `~/.claude/projects/<project-slug>/<sessionId>.jsonl`
  - `~/.claude/projects/<project-slug>/sessions-index.json`

These stores are not interchangeable. A CLI-created project session may persist on disk without appearing in the desktop app sidebar.

Operational rule:

- do not claim that `llm-collab`, PM2, or Claude CLI can safely create a brand
  new Claude desktop app thread
- do not synthesize desktop-visible Claude threads by writing local app cache/index files
- use `Chats/` messages as the transport of record (the durable mailbox)
- routine exact-session dispatch is the wake for every watcher-backed worker,
  Codex included. Only when no matching dispatchable session autobridge exists
  and `deliver.py` reports `ax_doorbell_required: true` does Codex fall back to
  the **bidirectional AX doorbell** (see
  `claude-code-desktop-computer-use-bridge.md`); terminal-only sessions require a
  dispatchable runtime binding
- every worker other than a Codex on that fallback path is woken by the durable
  packet and its own watcher alone, never by AX. Preserve the packet and let the
  watcher own pickup. See
  `session-autobridge-runbook.md` for the full rule
- attended Computer Use is fallback/recovery when AX cannot safely target or
  verify the native composer; it is never the universal first path, and it is
  never a path to Claude — the project-configured non-CLI Claude Desktop bridge
  is removed, and `deliver.py` no longer reports `desktop_bridge_required`

Current Phase 1 routing gives a matching dispatchable session autobridge
precedence. When `deliver.py` reports `autobridge_ready: true`, it intentionally
suppresses `ax_doorbell_required`; that packet
uses the separately documented session-autobridge path and its retry limitations.
Do not describe AX as primary for that packet, and **never deactivate a working
binding to obtain an AX wake** — that removes the routine dispatch v12 requires
in order to reach a fallback. If `deliver.py` does not print an AX command, the
answer is to repair or diagnose dispatch, not to disable it.

Contract v12's fallback predicate is unchanged:
`wake_fallback_allowed = not autobridge_ready and not
dispatch_scope_refused`. AX is not a routine worker lane or a BB transport.
Whether an offered doorbell can land is a runtime property checked live for
that attempt; never infer it from process state or record a window count as a
standing capability. `autobridge_ready: true` proves send-time routability, not
delivery, so require the receipt or recipient evidence named by
`session-autobridge-runbook.md`.

Safest task-grade workflow for desktop-app agents:

1. `llm-collab` delivers the task into `Chats/` with `deliver.py`
   - `autobridge_ready: true` takes precedence and means no AX doorbell was
     requested for this packet
   - for Codex, `ax_doorbell_required` means the sender runs exactly the command
     printed by `deliver.py`; it is
     not a manual operator relay request
   - `watcher_pickup_ready` is retained for compatibility but is false for new
     admitted deliveries; watcher-backed recipients require an exact binding
   - `desktop_bridge_required` is always false: the Claude Computer Use fallback
     it selected is removed, and Claude is woken by its own background watcher
   - `delivery_refused: true` with `durable_write: false` means no exact target,
     AX fallback, or documented broadcast route was admitted; repair the route
     and send again
   - `routing_mode: broadcast` is the explicit signal for the only admitted
     unbound case: the operator or a watcher-disabled human/human relay
2. when `ax_doorbell_required` is true, the sender rings once with the printed AX
   command. Do not prove the composer empty first: for Codex, composer content
   and `AXValue` readability/opacity are never a hold, and a busy recipient is
   not a hold either — the ring clears and overrides whatever is in the
   composer and sends, and queues behind the recipient's active turn. Only a
   genuine targeting/operation failure (no or ambiguous native composer target,
   a non-Codex or unrecognized profile, an AX-trust failure, a
   clear/type/submit failure, or post-submit identity loss) means hold and
   recovery. `VERIFIED` exit 0 confirms
   delivery. `QUEUED (UNCONFIRMED)` exit 0 does
   not prove the pointer entered the intended thread: preserve the mailbox
   packet, record the unconfirmed blocker/follow-up, never re-ring it, and do not
   claim exact-thread delivery without a later `axsend confirm` or explicit
   recipient evidence that the pointer appeared in the native thread. Inbox
   consumption proves durable packet delivery only, not AX-thread delivery. A
   running/processing state alone does not block the ring after composer-safety
   proof
3. the sender sends exactly one short sender-tagged wake prompt that points the
   recipient to the exact `llm-collab` inbox/chat/message path. Do not paste full
   task context, acceptance criteria, or multi-paragraph briefs into the app; the
   durable `Chats/` packet is the source of truth. The prompt must be one line,
   under roughly 240 characters, and never contain newline-split bridge details.
   The recipient drains its full unread inbox after the ring.
4. if AX resolves an embedded preview/web field or cannot deliver/confirm, stop
   sending but preserve the packet. In an attended Codex turn, use Computer Use
   plus `bin/axsend-ensure tree --app <app> --editable-only` to remove/blank the
   competing field, select the correct window, and clear probes. Resume routine
   AX only after verifying the native composer **target** identity is resolved
   and unambiguous — composer content/`AXValue` readability is never the gate.
   An opaque or unresolvable target remains on attended recovery; use one
   idle-gated Computer Use send as the bounded fallback. Never turn one
   targeting incident into a standing AX-disabled rule.

A Claude packet that is not picked up is a watcher or binding defect: preserve
the durable packet, record the observed blocker, and report it for repair or
operator attention. Codex does not wake Claude through Computer Use, AX, or an
app restart — coarse Claude bridge health checks and recording an
accessibility/capture blocker are diagnosis, not a wake. Keep or create the heartbeat and record
`observed_state`, `expected_outcome`, `why_not_done`, and
`next_unlock_action`. Do not ask the operator to relay, paste, click, or
manually wake Claude, and do not reach for Computer Use to do it either: repair
the binding or the watcher, or keep monitoring and report the blocker.

For programmatic runtime targeting that does not require visible desktop state,
use a separate non-desktop adapter. For Claude desktop work, do not use
`claude -p`, `claude --resume`, or `~/.claude/projects` as the bridge.
