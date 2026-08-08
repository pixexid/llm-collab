# Session Autobridge Runbook

> **Current BB routing status: dormant.** The current worker fleet routes through
> BB. Session autobridge dispatch and its AX fallback are not BB transports and
> are not used to reach BB workers, whose threads are not `llm-collab`
> participants, session bindings, watcher recipients, or receipt-bearing
> endpoints. Use
> [`bb-workers.md`](bb-workers.md) for current BB operations; none of the AX lane
> applies to BB. The durable-packet and receipt reasoning below still applies
> when the orchestrator itself authors a collab packet after verifying a BB
> result, not to the BB worker.
>
> The mechanics remain here because watcher-backed recipients may return. The
> watcher recovery sequence in
> [`AGENTS.md`](../../AGENTS.md#one-writer-per-lane)—read the lifecycle log,
> reconcile what it names, restart cleanly, prove one fresh probe receipt, then
> consider the stranded packet—remains the proven repair path; the diagnostic
> mechanics below support it. Contract v12's fallback predicate is unchanged
> and remains the authority for whether `deliver.py` offers a doorbell. When it
> does, run the exact command it prints or follow the live AX capability check in
> [`bb-workers.md`](bb-workers.md) and read its current `windows` count. Never
> infer reachability from `pgrep -x Codex`: it can return no match while the
> surface is live inside `ChatGPT.app`.

Session autobridge lets a worker bind the current runtime thread to a
project/chat so future messages can be routed to that parked worker session.

Use it to reduce short manual relays. Do not use it to make workers fully
autonomous.

## Status: the primary wake (contract v12)

Routine exact-session dispatch through session autobridge is **the** routine wake
for every watcher-backed recipient. Bounded polling and heartbeat observation are
a separate thing and remain a safety-fuse; do not read the limits on those as
limits on dispatch.

Current `deliver.py` gives a matching dispatchable session autobridge precedence
and suppresses `ax_doorbell_required` for that packet. Only Codex may receive
the busy-safe **bidirectional AX doorbell**, and only through the exact command
printed by `deliver.py`. Every watcher-backed worker, Codex included, is woken
by its durable packet and its own watcher; AX is reached only when that dispatch
is unavailable. See
`claude-code-desktop-computer-use-bridge.md`. **GH-470: composer content and
`AXValue` readability are never a sender-side hold, and neither is a busy/running
recipient.** The recipient never types into its own composer, so any value there
— readable non-empty, readable empty, readable nil, or an opaque profile whose
value cannot be read — is stray; the ring clears and overrides it, and AX send
takes preference over any composer content, **including the operator actively
typing**. Ring even when the recipient is busy; the doorbell queues. Do not wait
for an empty composer and do not route a non-empty/unreadable composer to
attended recovery — that stranded the sender indefinitely. A routine `ring`
fails closed **only** on a genuine targeting or operation failure: no or
ambiguous native composer target, an **unrecognized** composer profile (unknown
target identity — some editable field resolved but it cannot be confirmed as the
right target), an AX-trust failure, a clear/type/submit failure, or post-submit
identity loss. Distinguish opaque `AXValue` **content** (proceed and override)
from an unresolved/unknown **target** (fail closed). `VERIFIED` exit 0 confirms
delivery. `QUEUED (UNCONFIRMED)` exit 0 does not prove exact-thread delivery:
preserve the durable mailbox packet, record the unconfirmed blocker/follow-up,
and never re-ring. The idle input gate applies only to attended
screenshot/keyboard Computer Use fallback, not to AX `ring`. Computer Use is
the recovery path when AX cannot safely resolve the native composer target.
`llm-collab` remains the durable mailbox. Routine/continuous polling is
**deprecated** as the primary wake — it wastes tokens and a heartbeat set on
guessed timing can fire into changed context.

PM2/heartbeat **polling and observation** — not session autobridge dispatch,
which is the primary wake — survive only as a bounded,
**provisional/experimental safety-fuse**, on trial, with hard constraints:

- only when a doorbell attempt is blocked, or a worker is visibly running and a
  handoff is expected
- for collab-loop waits such as PR review, bot-review comments, inbox replies, and
  doorbell handoffs, Claude owns the ongoing monitor; Codex should hand the
  watch to Claude instead of keeping an in-thread heartbeat alive
- task-scoped: tied to one specific task/worktree/branch and its chat
- auto-deletes on handoff/ack/blocker; must not outlive its task/chat
- never the primary path, never a standing always-on watcher
- one monitor per purpose; clear stale prior monitors before creating a new one
- must be fixed or removed if it misbehaves on real tasks

If the safety-fuse causes stale-context or duplicate-wake issues in practice,
remove it and rely on exact-session dispatch plus the mailbox-drain self-heal.

## Safety Defaults

- Prefer `notify` or `auto-read` over `auto-reply`.
- Keep one registered session per active worker/chat unless intentionally
  superseding an old one.
- Amiga `cdx2` is a disabled legacy human-relay worker by default. Do not
  activate `cdx2` for new Amiga implementation work unless the operator
  explicitly re-enables it for that specific task.
- When a human-relay implementation worker is explicitly enabled for a task,
  create a fresh chat, task, and session binding for that task. Reuse a
  registered session only for the same task context, blocker repair, or
  review-fix loop.
- Keep operator-visible chat notes enabled; autobridge activity must stay
  visible in `Chats/`.
- Do not target an active operator thread for retry tests.
- Treat Claude desktop as a human-visible UI, not as a
  `session_autobridge.py` runtime target. **Claude has exactly one routine wake
  path: the durable collab packet, picked up by an exact-session background
  inbox watcher streamed through a persistent native Monitor owned by that
  Claude task.** The Monitor surfaces each event without waiting for the watcher
  command to exit or re-arming after a turn. An ordinary background task or
  agent-wide PM2 watcher is not that proof. The poller leaves the activation
  unclaimed for the native watcher. Do not claim a PM2 watcher, CLI resume, or
  filesystem write reached the app.
- A binding whose canonical `agent_id` is `claude` must use `notify` mode.
  Only that identity suppresses `claude -p` / `claude --resume`; its background
  inbox watcher owns pickup.
- Workers must not synthesize a second routine wake path for Claude: no AX
  ring, `claude --resume`, `claude -p`, app reload, replacement thread, or
  repeated Computer Use typing. One attended Computer Use or AX interaction is
  allowed only to install or repair the exact-session watcher and prove a
  disposable packet wakes that task. Stop UI steering immediately after that
  proof.
- For Claude-owned collaboration lanes, inspect the visible Claude app before
  treating inbox or queue state as final: if Claude is visibly asking a related
  question, waiting for direction, or reporting Read/Agent/tool errors, that is
  diagnosis, and the answer goes back as a durable packet like any other.
- If Claude is stale, idle with no durable progress, or repeatedly erroring, its
  exact-session watcher is not picking up. Preserve the durable packet and
  repair or reinstall that watcher from an attended worker, then prove pickup
  with one disposable exact-session packet. Escalate to the operator only when
  the watcher cannot be repaired without credentials, an irreversible action,
  or a scope decision.

## Activate A Session

From the collaboration repo:

```bash
python3 bin/session_autobridge.py publish-current \
  --session SESSION-codex-amiga-dispatch \
  --agent codex \
  --runtime-family codex_app \
  --project amiga \
  --chat CHAT-xxxx \
  --mode auto-read \
  --status parked \
  --wake-strategy runtime_trigger \
  --ttl-seconds 3600
```

Use the current chat id, not `last`, for durable bindings. If the runtime cannot
be discovered automatically, register the session explicitly:

```bash
python3 bin/session_autobridge.py register \
  --session SESSION-codex-amiga-dispatch \
  --agent codex \
  --project amiga \
  --chat CHAT-xxxx \
  --mode auto-read \
  --status parked \
  --wake-strategy runtime_trigger \
  --runtime-family codex_app \
  --runtime-session-id <runtime-thread-id> \
  --runtime-session-source manual
```

## Activation Leases

Activation packets are writer grants only after the assigned worker claims the
exact activation lease. The claim is scoped to the packet identity
`project/chat/task/worktree/branch/target-agent` and to the worker's registered
session. The `--project` value must be registered in `projects.json` before
lease claim/show/assert/release construct, read, or write the lease identity.
The session must be live and exactly bound to the same agent, project, and chat;
null project/chat bindings are unbound and refuse rather than acting as
wildcards. A registered session is live only when its status is `active` or
`parked` and its session `lease_expires_utc` has not expired.

Claim with a runtime identity or live process id:

```bash
python3 bin/session_autobridge.py lease-claim \
  --project amiga \
  --chat CHAT-xxxx \
  --task TASK-xxxxxx \
  --worktree /absolute/canonical/worktree \
  --branch codex/example \
  --target-agent claude \
  --session SESSION-claude-amiga \
  --claimant-runtime-id <runtime-thread-id> \
  --json
```

Before mutating the assigned worktree, assert the current owner and fence:

```bash
python3 bin/session_autobridge.py lease-assert \
  --project amiga \
  --chat CHAT-xxxx \
  --task TASK-xxxxxx \
  --worktree /absolute/canonical/worktree \
  --branch codex/example \
  --target-agent claude \
  --session SESSION-claude-amiga \
  --fence-token <token-from-claim> \
  --claimant-runtime-id <runtime-thread-id> \
  --json
```

Claim, assert, and release require claimant identity from the current caller:
`--claimant-runtime-id`, a reader runtime environment variable, or a live
positive `--owner-pid`. The registered session record and stored lease record
are never proof that the current process is the lease holder.

Release when the task is done or superseded:

```bash
python3 bin/session_autobridge.py lease-release \
  --project amiga \
  --chat CHAT-xxxx \
  --task TASK-xxxxxx \
  --worktree /absolute/canonical/worktree \
  --branch codex/example \
  --target-agent claude \
  --session SESSION-claude-amiga \
  --fence-token <token-from-claim> \
  --claimant-runtime-id <runtime-thread-id> \
  --json
```

Refusals return exit 75 with JSON naming the reason and current owner where
known. Do not mutate the worktree after a refused claim/assert, and do not
treat a refused release as cleanup evidence. Takeover is explicit: use
`--takeover` when replacing an expired activation lease, session-expired owner,
or provably dead owner; those leases are never idempotently reclaimed and always
mint a new fence. A live, unexpired same-session/same-runtime runtime-only
reclaim may refresh TTL with the same fence. PID `0` and negative PIDs are
invalid claimant identities; a positive explicit `--owner-pid` that is provably
dead refuses with `owner_pid_not_live`; unknown liveness fails closed. Claims
resolve the requested worktree once under a nonblocking global grant lock and
refuse symlink aliases of an already-active real worktree, while identity
classification remains byte-exact and filesystem-independent. Grant-lock
contention returns bounded `claim_in_progress`. Released and expired
same-realpath lease records do not block a new identity claim. Malformed
activation lease JSON fails closed with `corrupt_lease_state`; the refusal names
only the bad lease filename, field, and reason, never file contents. Active,
unexpired lease records are structurally invalid unless `worktree_realpath`,
`lease_key`, `owner_session_id`, and `status` are all present non-null strings.
During alias enumeration, active unexpired lease records are also invalid unless
the payload `lease_key` matches the filename-derived key and the payload
`identity` hashes back to that same key; this semantic binding is required by
the runtime activation gate.
Claim, assert, and release route existing-lease authority through one shared
validation entry point covering structural validity, lease-key and identity
binding, session liveness and binding, claimant runtime/PID binding, PID
liveness, fence, and lease expiry.

For activation packets delivered by `deliver.py --activation`, the packet body
contains the exact claim command:

```bash
python3 bin/inbox.py --me <agent> --project <project> --chat <chat> --packet <packet-name>
```

`--packet` searches the recipient's read+unread inbox union and must match
exactly one packet. Ambiguous or missing selectors exit 75 before any inbox
mutation. The first successful reader claims the lease, prints the owner/fence,
and only then marks the packet read. A later reader exits 75 with the current
owner while leaving the packet state unchanged. `--mark-all-read` may clear
ordinary stale mail and missing pointers, but it holds existing activation
packets.

Runtime dispatch performs the same activation classification before any
loop-protection skip or processed-message mutation. Malformed activation
packets and concurrent-loss refusals remain unread/unprocessed. A successful
dispatch carries the activation identity and fence into the runtime payload and
resume prompt. Dispatcher claims bind both runtime id and dispatcher process
pid. Each protected filesystem/process boundary then runs inside a lease-held
mutation guard: acquire the per-identity claim lock, validate exact
owner/runtime/pid/fence, perform the mutation while the lock is held, then
release. Protected boundaries include turn summaries, runtime triggers, relay
prompts, UI refreshes, loop-protection processed writes, and final
processed-message writes. A one-time preflight assertion is not enough.
Stale-fence refusal at the boundary stops the packet and leaves it unprocessed;
a dead predecessor process can be replaced only by a successor claim with a
newer fence.

Activation cleanup is deliberately conservative. Before a claim, stale recurring
pollers for the same activation identity are audited. PM2 `jlist` PIDs are
preserved as authoritative watcher processes. Unregistered matching pollers must
be terminated and verified gone; report-only, unverified, or audit-unavailable
results refuse the claim. Matching a chat-id poller also requires `--me` for the
target agent, so a different agent watching the same chat is never terminated.
Tests must use `LLM_COLLAB_PS_FIXTURE`; fixture cleanup simulates termination
and never signals real PIDs.

## Inspect Bindings

Show the registered session:

```bash
python3 bin/session_autobridge.py show \
  --session SESSION-codex-amiga-dispatch \
  --json
```

Show the canonical project/chat/agent binding:

```bash
python3 bin/session_autobridge.py show-binding \
  --project amiga \
  --chat CHAT-xxxx \
  --agent codex \
  --json
```

Inspect inbox queue state:

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("agents/codex/inbox.json")
print(json.dumps(json.loads(path.read_text()), indent=2))
PY
```

## Send To A Bound Worker

Use `deliver.py` as usual. If a binding exists, `deliver.py` resolves
`target_session_id` automatically:

```bash
python3 bin/deliver.py \
  --chat CHAT-xxxx \
  --from codex \
  --to <enabled-human-relay-worker> \
  --project amiga \
  --title "Review watcher retry fix" \
  --body-file /tmp/message.md
```

If no dispatchable target or valid Codex AX fallback exists, `deliver.py`
refuses before writing the chat/inbox message. The only unbound exception is an
explicit `routing_mode: broadcast` for the operator or a watcher-disabled
human/human relay.

Do not use `--chat last` for a new implementation task unless you have just
created and verified that the latest chat is the task's dedicated chat. A new
task must not be carried into a previous worker thread just because the old
binding can still receive messages.

## Watch Or Run One Dispatch Pass

PM2 watcher:

If the managed watcher is not already online, enable it through the canonical
[PM2 Log Rotation workflow](pm2-log-rotation.md) before continuing. This
runbook does not carry a second start procedure.

```bash
<runtime_root>/bin/llm-collab pm2_watchers.py status --agent codex
<runtime_root>/bin/llm-collab pm2_watchers.py logs --agent codex --lines 100
```

Manual one-shot watcher:

```bash
<runtime_root>/bin/llm-collab watch_inbox.py --me codex --max-polls 1 --json
```

For Codex, manual and PM2 watcher runs default to:

- `LLM_COLLAB_CODEX_UI_REFRESH_METHOD=cdp`
- `LLM_COLLAB_CODEX_CDP_PORT=9223`

## Current Retry Behavior (No Busy Queue)

Session autobridge currently has no authoritative Codex busy/idle check, no
inbox `queued` field, and no `autobridge_deferred_busy` event. Do not rely on it
to protect a running target from a stacked or ambiguous `turn/start`.

The implemented behavior is narrower:

- a runtime trigger that reports nonzero emits `autobridge_failed`
- the matching message remains under `unread`
- later watcher passes consider the unread message again
- a later successful runtime result emits `autobridge_consumed` and moves the
  message to `read`
- activation packets add a lease gate and mutation-time fence assertions, but
  they do not create a general busy queue

### Settled diagnostic failures

An `append_event` failure after packet settlement does not change the action
outcome. Existing per-action events such as `autobridge_consumed` and
`autobridge_wake_signaled` carry a `diagnostic_errors` list. The default text
log keeps `ts`, `event`, and `detail` in its scan-friendly prefix, then appends
every other event field in one compact `data={...}` JSON object. The first
payload for an event identity, and every changed payload after it, stays
complete. An identical repeat for the same project, agent, session, chat, and
message identity stays detail-only. The process remembers at most 256 recent
identities, so interleaved sessions remain independent without creating an
unbounded cache. No event taxonomy or field allow-list is involved: action,
refusal, error, diagnostic, and unknown events expose new structured fields by
default. A settled relay, notify, or manual no-op has no ordinary per-action
event, so it emits
`autobridge_diagnostic_error` with `effective_action` and `diagnostic_errors`
instead.

Treat either form as a diagnostic-log repair, not a dispatch retry: inspect each
entry's `operation`, `error_type`, and `detail`, repair the session event-log
writer, then follow the watcher recovery sequence linked at the top of this
runbook. The action is already settled, so do not resend the packet merely
because its diagnostic write failed.

Because a transport failure may occur after runtime acceptance, an automatic
retry is not a proven exactly-once contract. Use disposable sessions for tests
and do not target an active operator thread. Transactional busy deferral,
coalescing, and broad ambiguous-delivery reconciliation belong to the planned
[Thread Event Runner](thread-event-runner-rfc.md), not to autobridge dispatch as
it stands today.

If a message is intentionally abandoned, clear it explicitly by marking it read:

```bash
python3 bin/inbox.py --me codex --all-projects --mark-all-read
```

Use this only when the unread set is known to be stale. For a single stale
message, edit `agents/<agent>/inbox.json` carefully or write a small local
maintenance script that moves that exact path from `unread` to `read`.

## Deactivate A Session

Stop a session when leaving a thread, replacing a worker, or ending a test:

```bash
python3 bin/session_autobridge.py deactivate \
  --session SESSION-codex-amiga-dispatch \
  --status stopped
```

Supersede an old session with a known replacement:

```bash
python3 bin/session_autobridge.py deactivate \
  --session SESSION-codex-amiga-dispatch \
  --status superseded \
  --superseded-by SESSION-codex-amiga-dispatch-2
```

## Minimum Proof Before Relying On A Watcher

Run the automated suite:

```bash
python3 -m py_compile \
  bin/_helpers.py \
  bin/_session_autobridge.py \
  bin/deliver.py \
  bin/inbox.py \
  bin/session_autobridge.py \
  <runtime_root>/bin/watch_inbox.py \
  tests/test_session_autobridge.py

python3 -m unittest tests.test_session_autobridge
```

For a real-worker session, also prove:

- `deliver.py` resolves the expected `target_session_id`
- watcher emits `autobridge_dispatch`
- a known pre-acceptance runtime failure emits `autobridge_failed` and leaves the
  message unread
- a later known-success pass emits `autobridge_consumed`
- `unread` is empty and the message is present in `read` after consumption
- a chat note records the pickup/dispatch event for the operator

This proof does not establish busy deferral or safe retry after ambiguous
runtime acceptance.
