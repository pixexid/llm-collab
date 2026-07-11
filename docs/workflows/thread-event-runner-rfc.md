# Thread Event Runner RFC

## Status

**Phase 1 architecture and threat/safety contract. No runner described here is
implemented yet.** Normative planned behavior uses **MUST**, **MUST NOT**,
**SHOULD**, and **MAY**. The confirmed sections under "Evidence classification"
describe the Phase 1 baseline; all other runner schemas, states, commands,
feature flags, and guarantees are planned contracts for later phases.

Source continuity:

- GitHub issue `pixexid/llm-collab#79` and its Phase 1 freeze comment
- Amiga `CHAT-D0397480`, especially the evidence-tagged Claude background-task
  architecture packet from 2026-07-11
- the current session-autobridge RFC, runbook, implementation, and tests

## Purpose

The Thread Event Runner is a planned local daemon and ledger for durable,
project-scoped event subscriptions that can eventually wake one exact Codex
thread. It is not another inbox watcher and it is not an unbounded command
scheduler.

The runner must make these properties mechanical:

- exact, non-null project, Codex runtime-home, and native thread identity;
- transactional observation, checkpointing, deduplication, and delivery claims;
- one active delivery per target thread, protected by leases and fencing;
- quiet healthy states, bounded coalescing, retry, retention, and dead letters;
- trusted code selecting capabilities and actions, never event payloads;
- explicit management, cancellation, migration, and rollback;
- no exact-thread turn until authoritative busy and acceptance/idempotency
  behavior is proven by integration tests.

## Evidence classification

### Confirmed Claude behavior

The following is confirmed only as observable behavior of the Claude harness
used in `CHAT-D0397480`; it is not a claim about Claude Desktop internals:

- A thread can create a monitor or background command. Output lines and process
  completion become harness task notifications in the originating thread.
- Harness-tracked completion can re-invoke that thread across an idle gap.
- Multiple tasks can run concurrently under distinct task IDs. The task surface
  can list, inspect, read output from, and stop them.
- The adapter logic owns polling/subscription cadence, state diffing, and
  deduplication. The observed harness does not provide cross-restart persistence,
  per-thread leases, transactional dedupe, or a dead-letter ledger.
- The observed tasks are recreated per session and stopped at handover.

### Claude inferences that are not contract facts

The evidence does not establish whether Claude's notification path is owned by
an Electron renderer, main process, local agent, or server. It does not establish
the persistence format, internal concurrency algorithm, or product-level
acceptance semantics. The runner must not copy an inferred topology.

### Confirmed current Codex/llm-collab behavior

At the Phase 1 baseline:

- Session autobridge stores session records as JSON and appends per-session
  JSONL event logs under `State/session_autobridge/`.
- Its Codex adapter can select an app-server process by exact `CODEX_HOME`, call
  `thread/resume`, then call `turn/start` for an exact native thread ID and wait
  for terminal notifications.
- The adapter does not first obtain an authoritative busy/idle result. It has no
  proven runtime idempotency key or reconciliation query for a `turn/start`
  whose acceptance is uncertain.
- `processed_messages` is written back to a session JSON file after a runtime
  result reports success. Observation, external dispatch, inbox mutation, and
  processed marking are not one transaction; overlapping manual/watcher passes
  can race.
- A session lease expiry limits whether a record is considered dispatchable,
  but there is no mutually exclusive per-thread lease, monotonic fence token, or
  compare-and-swap completion guard.
- Project/chat filters on autobridge sessions are optional. Some lookup paths
  accept an unscoped session as a match. This is incompatible with the runner's
  exact-project contract.
- `allowed_actions` is stored and forwarded in runtime payloads, but it is not
  an enforced adapter/handler capability profile.
- A failed runtime trigger remains unread and is retried on later watcher
  passes. There is no inbox `queued` field and no implementation that emits
  `autobridge_deferred_busy`. Those names in older docs were planned behavior,
  not current behavior.
- `pm2/ecosystem.config.cjs` currently creates watchers for every roster entry
  with `activation.watcher_enabled: true`; it does not exclude `human` or
  `human_relay` types.

### Product-level gaps

Local code cannot safely infer these contracts:

- an authoritative app-server thread busy/idle state;
- whether `turn/start` was rejected before acceptance, accepted exactly once,
  or accepted before a transport failure;
- a runtime-enforced idempotency key or queryable client delivery token;
- a stable reconciliation query for an accepted turn after runner restart.

The exact-thread dispatcher MUST remain feature-disabled until these behaviors
are integration-proven against the supported Codex runtime. A successful
happy-path `thread/resume` plus `turn/start` call is not sufficient proof.

## Scope and non-goals

The MVP architecture covers durable subscriptions, read-only observation, a
transactional ledger, local inspection, and a gated exact-thread dispatcher.
Initial adapter families are timer, filesystem, durable mailbox/chat, process
completion, GitHub state, AX handoff safety, and project queue/task/worktree
state. Only the first three are authorized for Phase 2, and their Phase 2
operation is read-only.

The runner does not:

- replace `Chats/`, tasks, queues, or project registries as sources of truth;
- execute a shell command supplied by an event, subscription payload, or chat;
- accept arbitrary modules, handlers, paths, URLs, tools, or target threads;
- create a general remote-code-execution scheduler;
- mutate AX, applications, GitHub, queues, or worktrees in Phase 2;
- replace Codex app automations or share ownership of their schedules;
- make session autobridge records the new transactional ledger;
- promise native Codex UI integration.

## Architecture

### Components and trust boundaries

1. **Management CLI/API** validates operator-authored subscription operations.
   It resolves projects and runtime homes before writing the ledger.
2. **Trusted adapter registry** maps a fixed adapter name to reviewed code, its
   configuration schema, source capabilities, payload limits, and cursor rules.
3. **Adapters** observe an approved source and return bounded untrusted event
   data. An adapter cannot select a handler, capability profile, or target.
4. **Trusted handler registry** maps a fixed handler name to reviewed
   classification/coalescing code and a fixed capability profile.
5. **SQLite ledger** is the source of truth for subscriptions, checkpoints,
   observations, delivery state, leases, attempts, audit, and dead letters.
6. **Dispatcher** is a separately gated component. It may use only the target
   and capability profile already frozen on the subscription.
7. **Compactor** applies bounded retention without deleting active or unresolved
   state.

Adapters and handlers are code registrations, not executable strings in the
database. The initial registry MUST be an in-repository allowlist. Dynamic
imports, entry-point discovery, arbitrary shell commands, and payload-selected
plugins are forbidden.

### Exact identity

Every subscription and delivery MUST bind this immutable tuple:

```text
(project_id, runtime_home_id, runtime_home_realpath, native_thread_id)
```

- `project_id` is a non-empty registered project ID and matches by exact string.
- `runtime_home_realpath` is the canonical absolute `CODEX_HOME` after symlink
  resolution. It must exist and must not be a parent/prefix wildcard.
- `runtime_home_id` is `sha256(utf8(runtime_home_realpath))`, stored alongside
  the path so equality is auditable rather than hash-only.
- `native_thread_id` is the runtime's exact thread identifier, not an agent ID,
  chat ID, "latest thread", or display title.

All four database columns are `NOT NULL`. Empty strings are invalid. Missing,
`null`, legacy wildcard, prefix, display-name, and "latest binding" matches MUST
fail closed. `chat_id`, `task_id`, and `agent_id` are optional audit metadata;
when present, they must resolve within the same exact project and cannot change
the target identity.

Before any future dispatch, the dispatcher MUST prove that the selected
app-server process has the exact canonical runtime home and that the resumed
thread ID equals `native_thread_id`. A subscription update cannot retarget the
identity tuple; retargeting requires cancel plus create.

## Trusted registry and capability model

Each adapter registration freezes:

- `adapter_name` and implementation version;
- strict configuration schema with unknown fields rejected;
- read roots or source namespaces it may observe;
- maximum event bytes, maximum events per poll, and poll timeout;
- cursor/checkpoint type and normalization rules;
- whether the adapter may use filesystem, clock, mailbox, process, network, or
  AX read capability.

Each handler registration freezes:

- `handler_name` and implementation version;
- accepted adapter/event types;
- classification and coalescing policy;
- maximum prompt/diagnostic bytes;
- capability profile ID;
- whether delivery is allowed at all in the current rollout phase.

Capability profiles are reviewed constants such as
`observe.timer`, `observe.filesystem`, `observe.mailbox`, and a future
`dispatch.codex_exact_thread`. A subscription may request only a registry-valid
adapter/handler/profile combination. The event envelope cannot add or widen a
capability.

Filesystem configurations MUST use canonical approved roots, reject traversal
and NUL bytes, and define symlink policy. Phase 2 filesystem observation is
restricted to metadata and bounded file content under explicit roots. Mailbox
observation parses only exact-project durable message paths and treats message
body/frontmatter as untrusted data.

## Bounded event envelope

The adapter returns a data-only envelope. The planned logical schema is:

```json
{
  "schema_version": 1,
  "adapter_name": "mailbox",
  "adapter_version": "1",
  "event_type": "message_pointer_changed",
  "source_event_id": "adapter-stable-id",
  "source_cursor": "opaque-bounded-cursor",
  "observed_at_utc": "2026-01-01T00:00:00Z",
  "source_time_utc": null,
  "subject": "bounded-display-label",
  "coalescing_key": "mailbox:codex:CHAT-...",
  "observed_state": "unread_message_present",
  "expected_outcome": "message_acknowledged",
  "why_not_done": "target_thread_not_yet_checked",
  "next_unlock_action": "wake_origin_thread_when_delivery_is_enabled",
  "severity": "actionable",
  "payload": {}
}
```

The runner supplies `subscription_id`, revision, exact identity, receive time,
and content hash after validation. An adapter/event MUST NOT supply or override:

- project, runtime home, native target thread, chat, task, or agent routing;
- adapter or handler implementation names;
- capability profile, command, executable, module, tool, URL, or environment;
- lease, retry, retention, feature flag, or delivery state;
- arbitrary absolute paths outside the registered adapter configuration.

The default maximum serialized envelope is 64 KiB, maximum `subject` is 256
UTF-8 bytes, and maximum coalescing key is 256 bytes. Payloads are canonicalized
before hashing. Oversize, malformed, unknown-field, invalid-encoding, and schema
violations fail closed and create a bounded diagnostic; they are never
truncated into a different semantic event.

## Planned SQLite ledger

### Location and connection contract

The new ledger is independent of session autobridge:

```text
State/thread_event_runner/runner.sqlite3
```

`State/` is gitignored runtime state. The directory MUST be mode `0700` and the
database, WAL, shared-memory, backup, and export files MUST be mode `0600` where
the platform supports POSIX permissions.

Every connection MUST apply and verify:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = FULL;
```

The busy timeout is bounded to 5 seconds; lock contention is an observable
runner error, not permission to wait forever. Schema version is recorded in
both `PRAGMA user_version` and a migration table. A mismatch fails startup
before adapters or dispatch run.

### Planned tables

This is a logical contract; column spelling may change only through a reviewed
schema migration that preserves the stated constraints.

| Table | Required purpose and constraints |
|---|---|
| `runner_instances` | Stable instance UUID, start/heartbeat/stop timestamps, version; used as lease owner identity. |
| `subscriptions` | UUID primary key; immutable exact identity tuple; trusted adapter/handler/profile; validated config JSON; positive revision; state; schedule; expiry; timestamps. Unique active name within exact identity. |
| `source_checkpoints` | One row per subscription/revision with opaque bounded cursor, normalized observed snapshot/hash, and last observation time; foreign key with cascade. |
| `events` | Monotonic ID; subscription/revision; stable source event ID; canonical envelope/hash; classification; coalescing key; timestamps. Unique `(subscription_id, revision, source_event_id)` and `(subscription_id, revision, envelope_hash)` as adapter-policy permits. |
| `deliveries` | UUID; exact identity copied and foreign-keyed to subscription; revision; handler; coalescing key; state; event range/count; due/expiry; retry count; lease/fence; cancellation/update marker. At most one open coalescing bucket per subscription revision/key. |
| `delivery_attempts` | UUID attempt token; delivery; fence; numbered attempt; started/finished time; acceptance classification; bounded response/error; unique `(delivery_id, attempt_number)` and unique attempt token. |
| `thread_leases` | One row per exact identity; owner instance; monotonic fence integer; acquired/renewed/expires timestamps. Exact identity is the primary key. |
| `subscription_leases` | One row per subscription; owner instance, monotonic fence, expiry; prevents concurrent polling/checkpoint advancement. |
| `dead_letters` | One row per terminal failed delivery or invalid observation; reason code, actionable diagnostic, first/last failure, acknowledgement, retained tombstone metadata. |
| `audit_log` | Append-only state transitions and management operations with actor, object, from/to state, revision/fence, reason code, and bounded metadata. |
| `schema_migrations` | Applied version, checksum, time, tool version, and backup reference. |

Foreign keys are mandatory. Orphaned events, attempts, leases, or dead letters
are corruption and MUST stop the affected operation. SQLite integrity and
foreign-key checks are required before migration, after unclean shutdown
recovery, and before a ledger is accepted after restore.

### Transaction boundaries

All write paths use short `BEGIN IMMEDIATE` transactions. No filesystem read,
network call, subprocess wait, app-server request, sleep, or AX operation may
occur while a database transaction is open.

**Subscription management transaction**

1. Resolve and validate exact identity plus registry entries outside the write
   transaction.
2. Inside one transaction, compare the expected revision, write the new state
   or revision, mark superseded deliveries, and append audit.
3. Commit before returning success.

**Observation transaction**

1. Poll under a valid subscription lease outside the transaction.
2. Begin the transaction and re-check subscription state, revision, lease owner,
   and fence.
3. Insert the deduplicated event, classify it, update or create an eligible
   coalesced delivery, advance the source checkpoint, and append audit/state
   transition records.
4. Commit all of those changes together. A cursor is never advanced without the
   corresponding event/dedup decision being durable.

**Delivery claim transaction**

1. Expire stale leases by timestamp, acquire the exact-thread lease, and
   increment its fence.
2. Atomically compare-and-swap one eligible delivery to `leased`, store owner,
   fence, and a fresh attempt token, then append audit.
3. Commit before any runtime inspection or dispatch.

**Pre-send authorization transaction**

After authoritative idle inspection and immediately before a future external
send, re-check subscription revision/state, delivery state, cancellation marker,
lease owner/expiry/fence, and feature flag. Compare-and-swap `leased` to
`dispatching` and commit. Cancellation before this boundary prevents the send.

**Attempt result transaction**

Persist the attempt result and compare-and-swap the delivery using the same
owner, fence, and attempt token. A stale worker cannot complete, retry, or
release a newer owner's delivery. Release or renew the thread lease in the same
transaction.

## State machines

### Subscription

```text
active <-> paused
active|paused -> cancelling -> cancelled
active|paused -> expired
active|paused -> error
error -> paused|active        (explicit operator recovery)
```

- Only `active` subscriptions may poll.
- `paused` preserves the checkpoint but creates no observations or deliveries.
- `expired` and `cancelled` are terminal. Resumption creates a new subscription
  or explicit new revision according to management policy.
- `error` is quiet after one actionable diagnostic and bounded health signal;
  it does not spin.

### Event

```text
observed -> quiet | actionable | invalid
actionable -> coalesced | delivery_created
quiet|coalesced|delivery_created|invalid -> retained -> compacted
```

`quiet` means the event advanced a checkpoint/snapshot but produced no thread
wake, notification, or repeated audit line. `invalid` creates only a bounded
diagnostic/dead letter according to reason.

### Delivery

```text
pending -> leased -> deferred_busy -> pending
pending -> leased -> dispatching -> delivered
leased -> retry_wait -> pending
dispatching -> retry_wait -> pending
dispatching -> reconciling
pending|leased|deferred_busy|retry_wait -> cancelled|obsolete|expired
pending|leased|dispatching|retry_wait|reconciling -> dead_letter
```

- `deferred_busy` does not increment retry count.
- `reconciling` is sticky and operator-visible. It never auto-transitions to a
  new attempt without authoritative evidence that the prior turn was not
  accepted.
- `obsolete` means a subscription update superseded work before the dispatch
  acceptance boundary.
- `delivered`, `cancelled`, `obsolete`, `expired`, and `dead_letter` are terminal.

## Quiet, coalescing, and retry behavior

Handlers classify every valid event as `quiet` or `actionable` and populate the
action-first fields `observed_state`, `expected_outcome`, `why_not_done`, and
`next_unlock_action`. A healthy/running/non-actionable observation MUST remain
quiet. The runner records a compact latest snapshot and metrics, not a new
notification or unbounded repeated event log.

Coalescing is allowed only for `pending`, `deferred_busy`, or `retry_wait`
deliveries with the same exact identity, subscription ID and revision, handler,
and normalized coalescing key. Coalescing updates the latest bounded state,
first/last event IDs, event count, severity, and due time according to the
registered handler policy. It never changes target/capabilities and never folds
into `leased`, `dispatching`, `reconciling`, or terminal work. An event arriving
after a delivery is leased creates or joins a successor bucket.

Retries apply only when the runner has authoritative evidence that dispatch was
not accepted. Default planned policy is 5 attempts with exponential backoff and
full jitter: base 5 seconds, cap 15 minutes, and absolute delivery expiry.
Capability violations, identity mismatches, invalid registry combinations, and
malformed envelopes fail closed without retry. Busy deferral uses its own
bounded recheck cadence and consumes no retry attempt.

After the retry/expiry bound, the delivery becomes `dead_letter` with an
operator-actionable reason, exact identity, subscription/revision, event range,
attempt history, and next recovery action. Dead-letter inspection and explicit
acknowledgement are management operations; acknowledgement never replays work.
A replay creates a new delivery with a new idempotency token after the operator
confirms the prior attempt was not accepted.

## Ambiguous delivery reconciliation

A transport timeout, disconnect, process crash, or malformed response after the
future `turn/start` request might mean the turn started. Such an attempt is
`acceptance_unknown`, and its delivery MUST enter `reconciling`.

The runner MUST NOT auto-retry an ambiguous attempt. Reconciliation may mark it
`delivered` only from authoritative runtime evidence tied to the unique attempt
token/turn ID, or may create a replacement only from authoritative evidence
that the attempt was not accepted. If the runtime exposes neither proof, the
delivery remains operator-visible and requires explicit disposition.

This is also the dispatcher rollout gate: exact-thread dispatch remains off
until integration tests prove authoritative busy state plus `turn/start`
acceptance/idempotency and restart reconciliation. Local heuristics, renderer
state, process CPU, absence of a `turn/started` notification, and timeout alone
are not authoritative.

## Update and cancellation semantics

- Every update requires the caller's expected revision and atomically increments
  it. Lost updates fail with a conflict.
- Adapter/handler/profile, target identity, and source kind are immutable.
  Changing them requires cancel plus create.
- Schedule, expiry, quiet/coalescing thresholds, and validated adapter options
  may update only if the registry declares them mutable.
- Each cursor-affecting update declares `preserve_cursor`, `start_now`, or an
  adapter-approved explicit replay point. Silent replay is forbidden.
- An update marks old-revision open deliveries `obsolete`. A delivery already
  beyond the pre-send authorization boundary is recorded to completion or
  reconciliation, but cannot schedule follow-up work for the new revision.
- Cancel atomically moves the subscription to `cancelling`, prevents new polls,
  cancels unleased work, and marks leased work cancel-requested. Before the
  pre-send boundary it must stop. After that boundary it follows delivered or
  ambiguous reconciliation rules because external work cannot be recalled.
- `cancelled` is reached only when no old-revision delivery is leased,
  dispatching, or reconciling.

## Retention and compaction

Planned defaults are intentionally bounded and configurable only within
operator-approved limits:

| Data | Default retention |
|---|---|
| Redundant quiet event bodies | 24 hours; latest snapshot/checkpoint retained |
| Delivered/cancelled/obsolete event bodies | 30 days |
| Delivery attempt detail and routine audit metadata | 90 days |
| Dead-letter payload/attempt detail | 180 days |
| Terminal/dead-letter tombstones and management audit | 365 days |

Active subscriptions, current checkpoints, open deliveries, unresolved
`reconciling` work, unexpired leases, and schema migration history are never
removed by age. Compaction deletes in batches of at most 1,000 rows per
transaction, preserves hashes/reason/timestamps in tombstones, and yields
between batches. WAL checkpointing and incremental vacuum run only in an idle
maintenance window; no full `VACUUM` runs during observation or delivery.

The planned default database size budget is 512 MiB. The soft limit is 80% of
that budget; the hard limit is 100%. At the soft limit the runner drops
redundant quiet payload bodies while preserving checkpoints and health counters.
At the hard limit it pauses observation, emits one actionable storage
diagnostic, and requires compaction/operator recovery; it must not delete
unresolved delivery evidence to keep running.

## Threat review

| Threat | Required mitigation |
|---|---|
| Event payload selects a command/tool/module | Payloads are data-only; trusted registries select fixed code and capability profiles. |
| Project or thread confusion | Non-null exact identity tuple, exact registry lookup, no null/prefix/latest compatibility. |
| Wrong Codex account/runtime | Canonical `CODEX_HOME` realpath plus fingerprint and exact app-server process match before dispatch. |
| Duplicate observers or crash recovery | Subscription/thread leases, monotonic fencing, compare-and-swap transitions, transactional checkpoints. |
| Turn stacked into a busy thread | Authoritative busy check required; busy defers quietly without consuming retry budget. Dispatcher disabled until proven. |
| Ambiguous `turn/start` acceptance | Unique attempt token, `reconciling` state, no automatic retry, authoritative reconciliation only. |
| Cancel/update races | Expected revision, pre-send authorization boundary, stale-revision obsolescence, fenced completion. |
| Filesystem traversal/symlink escape | Canonical registered roots, strict relative paths, explicit symlink policy, bounded reads. |
| Mailbox/chat content injection | Exact-project path validation; frontmatter/body remain untrusted event data and cannot widen routing/capabilities. |
| Database tampering/corruption | Private permissions, foreign keys, integrity checks, migration checksums/backups, fail-closed startup. |
| Disk/log amplification | Quiet-state suppression, envelope limits, batch compaction, retention windows, database size budget. |
| Clock rollback/sleep/wake | Persist wall-clock deadlines and recompute timers on wake; use monotonic time for in-process durations; dedupe overdue timer occurrence IDs. |
| Poison source or adapter loop | Poll/event/byte limits, timeouts, adapter health backoff, one actionable error, dead letter or subscription error. |
| Capability drift after deploy | Persist adapter/handler versions and profile ID; pause incompatible subscriptions until explicitly migrated. |
| Split ownership with automations/legacy watcher | Separate stores and lifecycle, explicit cutover, one owner per source/target subscription. |
| Local hostile user with same account | Out of scope for strong isolation; document local-user trust and fail on permissive state-file modes. |

The runner protects against malformed/untrusted observed data and accidental
concurrency. It does not sandbox trusted adapter code from the local OS account.
Adding registry code is therefore a privileged code change requiring normal
review and tests.

## Management surface

The later CLI/API must support create, list, inspect, pause, resume, update,
cancel, dead-letter acknowledge/replay, compact, integrity-check, export, and
status. Inspection must show:

- exact target identity and subscription revision;
- trusted adapter, handler, capability profile, and versions;
- state, expiry, next poll/run, last checkpoint/event, and quiet snapshot;
- open/coalesced delivery, lease owner/fence/expiry, retry count, and next retry;
- cancellation/update status and any ambiguous/dead-letter diagnostic;
- feature flags that currently permit observation or delivery.

Management output must redact bounded payload fields by default. JSON output is
required for automation, but mutation commands require explicit IDs and
expected revisions; ambiguous names or "last" are forbidden.

## Boundary with Codex app automations

Codex app automations remain an app-owned product surface with their own
schedules, thread behavior, lifecycle, UI, and storage. Runner subscriptions
remain locally owned ledger records for observing external state. The runner
MUST NOT read, write, import, mirror, pause, cancel, or claim app automation
state, including `automation.toml` metadata.

An automation may explicitly invoke the future runner CLI as an operator action,
but the resulting subscription has a new runner ID and independent lifecycle.
Likewise, a runner event may eventually wake a thread but cannot create or
mutate an app automation. If both surfaces watch the same condition, they are
independent producers; operators must choose one owner or accept distinct,
visible dedupe namespaces. No shared lease or silent cross-cancellation exists.

Use app automations for app-managed scheduled prompts. Use the runner for
durable local observation, coalescing, and future exact-thread recovery from an
external condition.

## Migration and rollback

Session autobridge is migration input, not the new ledger.

- Phase 2 creates a fresh database and does not import `processed_messages`,
  JSONL events, lease expiry, or implied queue state as transactional truth.
- An optional later migration command may read an autobridge binding only as an
  operator-reviewed candidate. It must require exact non-null project, canonical
  runtime home, native thread ID, trusted registry mapping, and a chosen initial
  cursor. Invalid/ambiguous records are reported, never wildcard-backfilled.
- Shadow observation may run beside a legacy dispatcher only when runner
  delivery is disabled. The same source/target must never have two dispatch
  owners.
- Per-subscription cutover order is: pause legacy dispatch, establish and audit
  the runner checkpoint, verify no legacy in-flight action, enable the reviewed
  runner phase flag, then observe before any delivery flag changes.
- Rollback disables delivery first, stops new claims, waits for or explicitly
  reconciles in-flight work, stops the runner, and leaves the ledger read-only
  for evidence. Legacy dispatch resumes only after ownership is unambiguous.
- Schema migration takes a verified backup, uses a checksum/versioned migration,
  runs in an exclusive maintenance window, and performs integrity/foreign-key
  checks before commit. Failed migration restores the backup. Runtime rollback
  does not perform an in-place schema downgrade.

## Phases and feature flags

All flags default off. Flags narrow the trusted registry; they never authorize
unregistered code or weaken identity/capability checks.

| Phase | Planned scope | Required flag posture |
|---|---|---|
| 1 | This architecture/threat contract only | No runtime flags or processes. |
| 2 | SQLite ledger; management/status; read-only timer, filesystem, and mailbox observation; quiet/coalescing simulation; no thread wake | `THREAD_EVENT_RUNNER_ENABLED=1`, `THREAD_EVENT_RUNNER_OBSERVE=1`, allowlist limited to `timer,filesystem,mailbox`; `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD=0`. |
| 3 | Exact-thread dispatcher plus its mandatory lease, fencing, retry, dead-letter, busy-deferral, and reconciliation substrate, developed as one disabled safety slice | Dispatch flag remains off while any part of the pre-pilot gate is incomplete. Only after the entire gate passes may one disposable exact subscription opt in. |
| 4 | Broader multi-runner, crash, sleep/wake, clock-change, load, and runtime-upgrade hardening | No project pilot or default-on rollout until the broader fault-injection gate passes. |
| 5 | Process, GitHub, queue/task/worktree, and AX safety adapters | Each adapter has a separate allowlist/feature flag and reviewed capability profile; AX mutation is not implied by observation. |
| 6 | Project pilots, observability, legacy cutover, documented rollback | Opt in one project/runtime/thread at a time; no workspace-wide wildcard enablement. |

Phase 2 is the next authorized implementation phase: **SQLite plus read-only
timer/filesystem/mailbox observation only**. It must not extract or activate
exact-thread dispatch, start PM2 state, mutate AX/apps, or claim busy deferral
works.

## Proof gates for later phases

Before Phase 2 handoff:

- schema migration, foreign key, WAL/restart, observation/checkpoint atomicity,
  dedupe, quiet-state, coalescing simulation, cancellation/update, retention,
  project isolation, sleep/wake, and clock-change tests pass;
- capability and path violations fail closed;
- no code path calls app-server `turn/start`.

Before any Phase 3 exact-thread pilot:

- a disposable target proves an authoritative busy state and no turn is sent
  while busy;
- N equivalent events while busy produce exactly one successor delivery;
- the runtime accepts a stable idempotency/client token or exposes equivalent
  authoritative acceptance and reconciliation evidence;
- crash before send, during request, after acceptance, and before result commit
  each have tested, non-duplicating outcomes;
- lease expiry and stale-fence completion cannot produce a second wake;
- update/cancel races cannot target an old revision;
- exact project/runtime-home/thread isolation is tested with at least two
  projects and two runtime homes;
- ambiguous acceptance remains `reconciling` and does not auto-retry.

Phase 3 implementation order does not weaken this gate: the dispatcher may be
built alongside the lease/fence/reconciliation substrate, but its feature flag
stays off and no disposable turn is sent until every item above passes. Phase 4
then broadens fault and load coverage before any project pilot.

Until all of those are true, the safe operational model remains the durable
mailbox plus currently approved doorbell/worker paths. This RFC authorizes no
new background process or delivery behavior by itself.
