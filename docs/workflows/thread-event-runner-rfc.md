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
- a stable reconciliation query for an accepted turn after runner restart;
- authoritative native thread metadata that binds its cwd/repository to the
  requested registered project at creation and immediately before dispatch;
- runtime-enforced tool, filesystem-root, network, and UI capability restriction
  for a runner-started turn, with an attestation of the applied profile.

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
  the path so equality is auditable rather than hash-only. This hash is only a
  stable local namespace key. It is **not** an account identity, credential, or
  authentication fingerprint.
- `native_thread_id` is the runtime's exact thread identifier, not an agent ID,
  chat ID, "latest thread", or display title.

All four database columns are `NOT NULL`. Empty strings are invalid. Missing,
`null`, legacy wildcard, prefix, display-name, and "latest binding" matches MUST
fail closed. `chat_id`, `task_id`, and `agent_id` are optional audit metadata;
when present, they must resolve within the same exact project and cannot change
the target identity.

At create time, the runner MUST resolve one repo ID and canonical repo path from
the exact `projects.json` entry, then obtain authoritative native thread
metadata from the supported runtime. The thread's authoritative cwd/repository
binding must be inside that exact registered repo, must not resolve to another
project, and must be stored as immutable revision evidence: repo ID and
realpath, native cwd realpath, evidence source/version/hash, and verification
time. A caller-supplied cwd or a local session-index guess is not authoritative.
If the runtime cannot supply a verifiable project/cwd binding, targeted
subscription creation fails closed. A future unbound observation-only record
would require a separate schema and is not part of this contract.

Immediately before any future dispatch, the runner MUST re-obtain the same
authoritative binding, prove that the selected authenticated app-server process
has the exact canonical runtime home, and prove that the resumed thread ID,
project repo, and native cwd still match the frozen revision. Drift moves the
delivery to a fail-closed diagnostic state without sending. Endpoint
authentication/process attestation is separate from `runtime_home_id`; the path
hash alone never authorizes a connection. A subscription update cannot retarget
the identity tuple or project binding; retargeting requires cancel plus create.

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

The dispatcher uses one fixed, trusted pointer-only prompt template. It may
render only canonical runner-owned identifiers such as `delivery_id`,
`subscription_id`, and `attempt_token`; it MUST NOT interpolate `subject`,
payload, observed state, reason text, source paths, chat content, or any other
event-controlled string. Rendered identifiers must pass their exact UUID/token
grammar, contain no control characters or newlines, and be serialized into the
JSON-RPC request by a structured encoder rather than shell/string
concatenation. Event detail is retrieved later through a trusted bounded reader
for that delivery ID. That reader returns canonical JSON with all strings UTF-8
validated and JSON-escaped under an explicit `untrusted_event` data field;
control/NUL bytes and unknown fields fail closed. It never renders event data as
system/developer instructions, markdown, shell, tool names, or prompt suffixes.

The capability profile must be enforced by the target runtime, not merely
described in the prompt. Before delivery is enabled, the runtime must restrict
the runner-started turn to the frozen tool set, filesystem roots, network/UI
permissions, and read/write mode and return authoritative evidence that the
profile was applied. If the supported runtime cannot enforce and attest those
limits, `dispatch.codex_exact_thread` remains disabled.

Capability profiles are reviewed constants such as
`observe.timer`, `observe.filesystem`, `observe.mailbox`, and a future
`dispatch.codex_exact_thread`. A subscription may request only a registry-valid
adapter/handler/profile combination. The event envelope cannot add or widen a
capability.

Filesystem configurations MUST use canonical approved roots, reject traversal
and NUL bytes, and prohibit symlink following. Path-string `realpath` checks are
not sufficient because they race. Each observation must open the registered
root directory, record and verify its device/inode identity, walk every relative
segment with directory-fd-anchored `openat`/equivalent and no-follow flags, and
post-open verify the final fd plus root identity before reading metadata or
bounded content. Events supply relative names only and the adapter reopens the
target through this anchored walk. If the platform cannot provide equivalent
fd-anchored traversal and post-open verification, the filesystem adapter fails
closed and stays disabled; it does not fall back to a path-only check. Mailbox
observation parses only exact-project durable message paths and treats message
body/frontmatter as untrusted data.

## Planned Phase 2 timer adapter contract

The Phase 2 timer adapter is read-only observation: it computes due occurrences
and writes bounded event/checkpoint state. It never dispatches a turn. The
immutable subscription revision MUST materialize one of these schedule kinds:

| Kind | Frozen fields and bounds |
|---|---|
| `one_shot` | `scheduled_at_utc` as RFC 3339 with `Z`; no implicit local time; at most 5 years after revision creation. |
| `fixed_interval` | `anchor_utc` with `Z` plus integer `interval_seconds` from 60 through 31,536,000. Occurrences are `anchor + n * interval`, not "last wake + interval". |
| `civil_recurring` | `frequency: daily|weekly`, second-precision `local_time`, explicit IANA `time_zone`, exact `tzdb_version`, and ISO weekdays 1=Monday through 7=Sunday for weekly schedules. Time-zone abbreviations, host-local defaults, and unversioned zone rules are invalid. |

The runner must be able to load the exact frozen tzdb version. Missing or
different zone rules move the subscription to `error` without producing an
occurrence. Adopting a new tzdb version requires a new immutable revision plus
an explicit cursor policy; it never silently changes an existing schedule.

Each real scheduled instant has the deterministic occurrence ID:

```text
sha256(subscription_id | revision | schedule_kind | scheduled_instant_utc |
       civil_local_label_or_empty | fold_index_or_zero)
```

`events` and checkpoints enforce uniqueness on that ID. Process restart,
duplicate polls, backward wall-clock movement, and sleep/wake cannot re-emit an
already recorded occurrence. Cursor state stores last occurrence ID and UTC
instant, next UTC instant, frozen tzdb version, and bounded skipped/coalesced
counters.

Wall-clock UTC determines whether a persisted occurrence is due. In-process
waiting uses a monotonic clock with a maximum 60-second recheck, never a single
unbounded wall-clock sleep. After restart, system wake, or detected wall-clock
jump, the adapter recomputes from the frozen schedule and checkpoint. If the
clock moves backward, it waits until a not-yet-recorded scheduled instant is due;
it never subtracts the clock delta from the checkpoint, generates a negative
sleep, or replays an old occurrence.

Every revision stores an explicit `missed_fire_policy`. Management materializes
`coalesce` when omitted; the database never stores an implicit value. It also
stores `max_lateness_seconds` (default 86,400; range 0 through 604,800):

- `skip`: advance the checkpoint through eligible missed occurrences and update
  a bounded skipped counter without an event.
- `coalesce`: emit one event containing count plus first/last scheduled UTC and
  occurrence IDs for eligible missed occurrences. Compute large counts
  arithmetically rather than materializing every instant.
- `catch_up`: emit only the most recent `catch_up_limit` eligible occurrences in
  chronological order; `catch_up_limit` defaults to 4 and is bounded from 1
  through 16. Older eligible occurrences increment the skipped counter without
  individual events.

Occurrences older than `max_lateness_seconds` always count as skipped, regardless
of policy. A missed one-shot emits at most once when within the lateness window
under `coalesce` or `catch_up`; `skip` or an exceeded window moves it to its
terminal checkpoint without an event. One timer poll emits at most 16 individual
events plus one coalesced summary, and all counters saturate at unsigned 64-bit
maximum.

For `civil_recurring`, the immutable revision also stores DST policies:

- `ambiguous_time_policy: first|second|both|skip` for a repeated local time;
  management materializes `first` by default. `both` produces two distinct UTC
  instants/occurrence IDs with fold indexes 0 and 1.
- `nonexistent_time_policy: skip|next_valid` for a spring-forward gap;
  management materializes `skip` by default. `next_valid` uses the first valid
  instant after the gap while retaining the original civil label and resolution
  policy in the occurrence evidence.

Timer adapter errors, missed-fire counters, and DST skips follow the same quiet
and bounded-diagnostic rules as other adapters. No clock, sleep, or DST outcome
may create a delivery in Phase 2 because exact-thread dispatch remains disabled.

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
| `subscriptions` | UUID primary key and lifecycle/current-head row only: name, state, `current_revision`, created/updated timestamps. It points to one immutable revision and does not carry mutable adapter/config snapshots; IDs, not names, are authoritative. |
| `subscription_revisions` | Immutable frozen snapshot with composite primary key `(subscription_id, revision)`: exact identity and project-binding evidence; trusted adapter/handler/profile versions; validated config; schedule, expiry, quiet/coalescing policy, and creation actor/time. An update inserts a new row; existing rows are never rewritten. |
| `source_checkpoints` | One row per subscription/revision with opaque bounded cursor, semantic quiet snapshot/hash, counters, and last observation time; composite foreign key to `subscription_revisions`. |
| `events` | Monotonic ID; subscription/revision; stable source event ID; canonical envelope/hash; classification; coalescing key; timestamps; composite foreign key to `subscription_revisions`. Unique `(subscription_id, revision, source_event_id)` and `(subscription_id, revision, envelope_hash)` as adapter-policy permits. |
| `delivery_lineages` | Immutable semantic-work identity derived by the ledger from project, trusted handler/action class, and origin event/dedupe evidence. Stores origin key and terminal/resolution status. Callers cannot choose, replace, or detach a lineage ID. |
| `deliveries` | UUID plus immutable `lineage_id`, lineage generation, optional `replaces_delivery_id`, and subscription/revision; exact identity copied from the frozen revision; handler; coalescing key/window; state and pre-lease state; event range/count/bytes; due/expiry; retry count; lease/fence; cancellation/update marker. Composite foreign key to `subscription_revisions` and foreign key to `delivery_lineages`; at most one open coalescing bucket per revision/key/window. |
| `delivery_attempts` | UUID attempt token plus delivery ID and subscription/revision; fence; numbered attempt; pre-dispatch project/runtime/capability attestation hashes and times; started/finished time; acceptance classification; bounded response/error. Composite foreign keys to both the matching `deliveries` identity/revision and `subscription_revisions`; unique `(delivery_id, attempt_number)` and unique attempt token. |
| `thread_leases` | One row per exact identity; owner instance; monotonic fence integer; acquired/renewed/expires timestamps. Exact identity is the primary key. |
| `thread_quarantines` | One row per exact identity while any delivery is `dispatching`, `reconciling`, or dead-lettered with unresolved acceptance. Stores delivery/attempt/revision, reason, created time, and resolution evidence. It survives lease expiry, release, runner restart, and subscription pause/cancel. |
| `lineage_quarantines` | One row per delivery lineage with ambiguous acceptance. Blocks every delivery/replacement in that lineage across subscription revisions, target threads, runtime homes, retire/rebind operations, and runner restarts until authoritative terminal-completion/not-accepted evidence resolves it. |
| `subscription_leases` | One row per subscription; owner instance, monotonic fence, expiry; prevents concurrent polling/checkpoint advancement. |
| `dead_letters` | Typed as `dispatch_failure` or `invalid_observation`; reason code, actionable diagnostic, first/last failure, acknowledgement, reprocessing eligibility, authoritative acceptance evidence reference, lineage/quarantine reference, and retained tombstone metadata. |
| `audit_log` | Append-only state transitions and management operations with actor, object, from/to state, revision/fence, reason code, and bounded metadata. |
| `schema_migrations` | Applied version, checksum, time, tool version, and backup reference. |

Foreign keys are mandatory. `subscriptions.current_revision` has a composite
foreign key back to its own `(subscription_id, revision)` row. Checkpoints,
events, deliveries, and attempts all carry `subscription_id` plus revision and
must resolve the same immutable snapshot; an attempt cannot point at a delivery
from another revision. A replacement delivery must carry its predecessor's
lineage ID, and the unique origin source/action mapping prevents recreating the
same semantic work under a fresh lineage. Orphaned events, attempts, leases,
lineages, quarantines, or dead
letters are corruption and MUST stop the affected operation. SQLite integrity
and foreign-key checks are required before migration, after unclean shutdown
recovery, and before a ledger is accepted after restore.

### Transaction boundaries

All write paths use short `BEGIN IMMEDIATE` transactions. No filesystem read,
network call, subprocess wait, app-server request, sleep, or AX operation may
occur while a database transaction is open.

**Subscription management transaction**

1. Resolve and validate exact identity plus registry entries outside the write
   transaction.
2. Inside one transaction, compare the expected current revision, insert the
   complete immutable `subscription_revisions` snapshot, advance the lifecycle
   row's current-head pointer, apply the frozen open-delivery disposition, and
   append audit. The current-head composite foreign key is deferred only until
   this transaction commits; it is never left dangling.
3. Commit before returning success.

**Observation transaction**

1. Poll under a valid subscription lease outside the transaction.
2. Begin the transaction and re-check subscription state, revision, lease owner,
   and fence.
3. Compute the registered semantic snapshot. If an unchanged quiet snapshot is
   observed, advance only the checkpoint, quiet counters, and last-observed time;
   do not insert an event or audit row. Otherwise insert/deduplicate the bounded
   event, classify it, resolve/create its ledger-derived lineage from the unique
   origin source/action key, update or create an eligible coalesced delivery,
   advance the source checkpoint, and append only the bounded transition audit.
4. Commit all of those changes together. A cursor is never advanced without the
   corresponding event/dedup decision being durable.

**Delivery claim transaction**

1. Check for any `thread_quarantines` row or unresolved exact-target in-flight
   delivery, then check the candidate's `lineage_quarantines` row and every
   unresolved delivery in that lineage. If any exists, claim fails regardless
   of target changes, lease expiry, or owner state. Only then expire a stale
   lease, acquire the exact-thread lease, and increment its fence.
2. Atomically compare-and-swap one eligible delivery to `leased`, store owner,
   fence, and a fresh attempt token, then append audit.
3. Commit before any runtime inspection or dispatch.

**Pre-send authorization transaction**

After authoritative idle, project/thread binding, endpoint identity, and
runtime capability-profile inspection and immediately before a future external
send, re-check the frozen subscription revision, lifecycle state, delivery
state, cancellation marker, lease owner/expiry/fence, and applicable test or
production flag. Compare-and-swap `leased` to `dispatching`, insert the exact
target's quarantine row and the delivery lineage quarantine, and commit before
constructing the fixed pointer-only request. Cancellation before this boundary
prevents the send. No external call occurs unless every attestation still
matches the immutable revision.

**Attempt result transaction**

Persist the attempt result and compare-and-swap the delivery using the same
owner, fence, and attempt token. A stale worker cannot complete, retry, or
release a newer owner's delivery. Authoritative terminal completion resolves
both quarantines and closes the lineage; authoritative acceptance without a
terminal result keeps both quarantines while the turn runs. Ledger-stored
authoritative not-accepted evidence resolves both quarantines and may authorize
exactly one next lineage generation. An unknown result moves the delivery to
`reconciling` and leaves both quarantines in place.

The owner renews the thread lease with short fenced transactions while an
external call is outstanding; no transaction spans the call. If renewal fails
before the request is sent, the worker must not send. If renewal fails after
send or send status is uncertain, the delivery becomes `reconciling` and the
quarantine survives the expired/released lease. Recovery may acquire a separate
fenced reconciliation lease, but normal delivery claims remain blocked.

## State machines

### Subscription

```text
active <-> paused
active|paused -> cancelling -> cancelled
active|paused -> expiring -> expired
active|paused -> error
error -> paused|active|cancelling   (explicit operator recovery)
```

- Only `active` subscriptions may poll.
- `paused` preserves the checkpoint and open unleased deliveries but creates no
  observations and makes those deliveries ineligible for claim.
- `expired` and `cancelled` are terminal. Resumption creates a new subscription
  or explicit new revision according to management policy.
- `error` is quiet after one actionable diagnostic and bounded health signal;
  it does not spin.

### Event

```text
observed -> unchanged_quiet (checkpoint/counters only; no event row)
observed -> changed_quiet | actionable | invalid
actionable -> coalesced | delivery_created
changed_quiet|coalesced|delivery_created|invalid -> retained -> compacted
```

`unchanged_quiet` advances a checkpoint/snapshot but produces no event row,
thread wake, notification, or audit line. `changed_quiet` may retain one bounded
semantic transition event without delivery. `invalid` creates only a bounded,
rate-limited diagnostic/dead letter according to reason.

### Delivery

```text
pending -> leased -> deferred_busy -> pending
pending -> leased -> dispatching -> delivered
leased -> retry_wait -> pending
dispatching -> retry_wait -> pending
dispatching -> reconciling
reconciling -> delivered
reconciling -> retry_wait -> pending
reconciling -> dead_letter (quarantine retained until safe disposition)
pending|leased|deferred_busy|retry_wait -> cancelled|obsolete|expired
pending|leased|dispatching|retry_wait|reconciling -> dead_letter
```

- `deferred_busy` does not increment retry count.
- `reconciling` is sticky and operator-visible. It never auto-transitions to a
  new attempt without authoritative evidence that the prior turn was not
  accepted.
- Every normal claim checks delivery state plus exact-target and delivery-lineage
  quarantine. Lease expiry, target retirement, rebind, or release never makes a
  `dispatching`, `reconciling`, or unresolved dead-letter lineage claimable.
- `obsolete` means a subscription update superseded work before the dispatch
  acceptance boundary.
- `delivered`, `cancelled`, `obsolete`, `expired`, and `dead_letter` are terminal
  delivery states. A terminal dead letter may still retain a target quarantine;
  terminal delivery state alone does not authorize another turn.

## Quiet, coalescing, and retry behavior

Handlers classify every valid event as `quiet` or `actionable` and populate the
action-first fields `observed_state`, `expected_outcome`, `why_not_done`, and
`next_unlock_action`. A healthy/running/non-actionable observation MUST remain
quiet. Each handler defines a canonical **semantic quiet projection** containing
only stable state needed to detect a meaningful transition. Cursor, observation
and source timestamps, poll/occurrence counters, adapter latency, and other
volatile metadata are excluded from its hash. When that semantic hash is
unchanged, the observation transaction advances the cursor, latest time, and
quiet counters without inserting an event or audit row. A changed semantic
state may create one bounded transition event. This keeps healthy polling from
becoming an event log.

Coalescing is allowed only for `pending`, `deferred_busy`, or `retry_wait`
deliveries with the same exact identity, subscription ID and revision, handler,
and normalized coalescing key. Coalescing updates the latest bounded state,
first/last event IDs, event count, severity, and due time according to the
registered handler policy. It never changes target/capabilities and never folds
into `leased`, `dispatching`, `reconciling`, or terminal work. An event arriving
after a delivery is leased creates or joins a successor bucket.

The default coalescing window is 60 seconds and a registry may raise it only to
a hard maximum of 15 minutes. One bucket accepts at most 256 observations and
64 KiB of canonical coalesced state. Reaching any window/count/byte bound seals
the bucket; later observations create a successor bucket or increment a bounded
overflow counter without retaining additional bodies. While the exact target is
quarantined, at most one successor bucket per revision/key is retained; further
occurrences increment a saturating 64-bit counter only. Invalid diagnostics are
rate-limited to one retained diagnostic per subscription/revision/reason per 15
minutes and at most 100 retained invalid diagnostics per subscription; excess
occurrences update a counter only.

Retries apply only when the runner has authoritative evidence that dispatch was
not accepted. Default planned policy is 5 attempts with exponential backoff and
full jitter: base 5 seconds, cap 15 minutes, and absolute delivery expiry.
Capability violations, identity mismatches, invalid registry combinations, and
malformed envelopes fail closed without retry. Busy deferral uses its own
bounded recheck cadence and consumes no retry attempt.

After the retry/expiry bound, the delivery becomes `dead_letter` with an
operator-actionable reason, exact identity, subscription/revision, event range,
attempt history, lineage, acceptance classification, quarantine state, and next
recovery action. A `dispatch_failure` dead letter has no generic replay path.
Acknowledgement never creates work or clears a target/lineage quarantine. A
replacement delivery is allowed only when the ledger already contains
authoritative `not_accepted` evidence tied to the exact attempt token and the
reconciliation transaction consumes that evidence while creating exactly one
next generation in the same lineage. Operator assertion, acknowledgement,
timeout, absence of output, or `acceptance_unknown` is insufficient.

An `invalid_observation` dead letter is a different state machine and never
inherits delivery retry/attempt semantics. After the adapter/config/handler is
corrected through a new immutable subscription revision, an explicit
`reprocess-observation` operation may parse the original bounded source evidence
into a new observation linked by `reprocesses_dead_letter_id`. It does not copy
delivery attempt count, does not create a delivery directly, cannot clear a
lineage quarantine, and must pass normal validation/classification/dedupe again.

## Ambiguous delivery reconciliation

A transport timeout, disconnect, process crash, or malformed response after the
future `turn/start` request might mean the turn started. Such an attempt is
`acceptance_unknown`, and its delivery MUST enter `reconciling`.

The runner MUST NOT auto-retry an ambiguous attempt. Reconciliation may mark it
`delivered` only from authoritative terminal-completion evidence tied to the
unique attempt token/turn ID. Evidence of acceptance without terminal completion
keeps the delivery/quarantines in progress. Authoritative evidence that the
request was not accepted may transition it to `retry_wait` or create a
replacement delivery that records
`replaces_delivery_id` and preserves the same `lineage_id`; the original attempt
history remains immutable. The replacement is claimable only after the
reconciliation transaction stores and consumes that exact not-accepted evidence
and resolves both quarantines.

If the runtime exposes neither proof, the delivery remains operator-visible and
the exact target remains quarantined across lease expiry, process restart,
subscription pause/error/cancel, and dead-letter retention. Explicit operator
dispositions are limited to:

- record authoritative terminal accepted/completed evidence and mark
  `delivered`;
- record authoritative not-accepted evidence and authorize one replacement;
- move the item to `dead_letter` while retaining the quarantine and blocking all
  later dispatch in the lineage.

Acknowledgement changes visibility only, and any generic replay request fails
closed. Retiring/rebinding the target does not clear lineage quarantine and
cannot move the same semantic work to another thread/runtime home. Only
ledger-stored authoritative terminal-completion evidence closes the lineage
without a replacement, or ledger-stored authoritative not-accepted evidence
permits one same-lineage next generation. A heuristic "probably failed"
disposition or narrative rationale is never sufficient.

This is also the dispatcher rollout gate: exact-thread dispatch remains off
until integration tests prove authoritative busy state plus `turn/start`
acceptance/idempotency and restart reconciliation. Local heuristics, renderer
state, process CPU, absence of a `turn/started` notification, and timeout alone
are not authoritative.

## Update and cancellation semantics

- Every update requires the caller's expected revision and atomically increments
  it by inserting a new immutable `subscription_revisions` row and advancing the
  lifecycle row's current-head pointer. Lost updates fail with a conflict; old
  snapshots remain queryable for every checkpoint/event/delivery/attempt.
- Adapter/handler/profile, target identity, and source kind are immutable.
  Changing them requires cancel plus create.
- Schedule, expiry, quiet/coalescing thresholds, and validated adapter options
  may update only if the registry declares them mutable.
- Each cursor-affecting update declares `preserve_cursor`, `start_now`, or an
  adapter-approved explicit replay point. Silent replay is forbidden.
- An update marks old-revision open deliveries `obsolete`. A delivery already
  beyond the pre-send authorization boundary is recorded to completion or
  reconciliation, but cannot schedule follow-up work for the new revision.
- Pause stops polling and new claims. Existing `pending`, `deferred_busy`, and
  `retry_wait` deliveries remain frozen and ineligible; a `leased` delivery that
  has not crossed pre-send authorization returns to its prior state. A
  `dispatching` or `reconciling` delivery continues only through fenced result or
  reconciliation handling and keeps the target quarantined.
- Expiry moves the subscription to `expiring`, stops polling/claims, and marks
  unleased work `expired`. Pre-send leased work becomes `expired` without a
  call. `dispatching`/`reconciling` work must reach an authoritative outcome or
  quarantined dead letter before the lifecycle row becomes terminal `expired`.
- Error stops polling and new claims after one bounded diagnostic. Unleased open
  work is preserved but ineligible until explicit revision-checked recovery;
  pre-send leases are released. In-flight/ambiguous work still follows fenced
  reconciliation and quarantine rules. Recovery compare-and-swaps only the
  lifecycle row against the expected current revision: recovery to `active`
  restores eligibility, recovery to `paused` keeps it frozen, and neither path
  mutates the frozen revision. A config/policy change requires a new revision.
- Cancel atomically moves the subscription to `cancelling`, prevents new polls,
  cancels unleased work, and marks leased work cancel-requested. Before the
  pre-send boundary it must stop. After that boundary it follows delivered or
  ambiguous reconciliation rules because external work cannot be recalled.
- `cancelled` is reached only when no old-revision delivery is leased,
  dispatching, or reconciling and no quarantine owned by that subscription
  remains unresolved. Lease expiry never completes cancellation.

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
`reconciling` work, target and lineage quarantines, unexpired leases, immutable
revision snapshots plus delivery/attempt/authoritative-evidence records
referenced by a quarantine, and schema migration history are never removed by
age. Unresolved quarantine overrides the ordinary dead-letter retention window.
Compaction deletes in batches of at most
1,000 rows per transaction, preserves hashes/reason/timestamps in tombstones,
and yields between batches. WAL checkpointing and incremental vacuum run only
in an idle
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
| Project or thread confusion | Non-null exact identity tuple plus authoritative native cwd/repo binding at create and pre-dispatch; no null/prefix/latest compatibility. |
| Wrong Codex account/runtime | Authenticated endpoint/process attestation plus exact canonical `CODEX_HOME`; path hash is only a namespace key, not authentication. |
| Duplicate observers or crash recovery | Immutable revision snapshots, subscription/thread leases, monotonic fencing, target and semantic-lineage quarantine beyond lease expiry/rebind, compare-and-swap transitions, transactional checkpoints. |
| Turn stacked into a busy thread | Authoritative busy check required; busy defers quietly without consuming retry budget. Dispatcher disabled until proven. |
| Ambiguous `turn/start` acceptance | Unique attempt token, `reconciling` state, target plus semantic-lineage quarantine across rebinds, no generic replay, ledger-stored authoritative reconciliation only. |
| Cancel/update races | Expected revision, pre-send authorization boundary, stale-revision obsolescence, fenced completion. |
| Prompt/content injection | Fixed pointer-only prompt; strict identifier grammar and structured encoding; event detail retrieved through a bounded trusted reader under runtime-enforced capabilities. |
| Filesystem traversal/symlink escape | Directory-fd-anchored no-follow traversal, root/final-fd identity verification, strict relative names, bounded reads; disable when unavailable. |
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
cancel, dead-letter acknowledge, typed `reprocess-observation`, evidence-gated
`replace-delivery`, compact, integrity-check, export, and status. There is no
generic dead-letter replay command. Inspection must show:

- exact target identity and subscription revision;
- frozen authoritative project/cwd binding and latest drift check;
- trusted adapter, handler, capability profile, and versions;
- state, expiry, next poll/run, last checkpoint/event, and quiet snapshot;
- open/coalesced delivery, lease owner/fence/expiry, retry count, and next retry;
- delivery lineage/generation, predecessor, typed dead-letter operation, and any
  ledger-stored authoritative acceptance evidence;
- cancellation/update status and any ambiguous/dead-letter diagnostic;
- target and lineage quarantine owner/reason/resolution evidence and runtime
  capability attestation;
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
  runtime home, native thread ID, authoritative native project/cwd binding,
  trusted registry/capability mapping, and a chosen initial cursor.
  Invalid/ambiguous records are reported, never wildcard-backfilled.
- Shadow observation may run beside a legacy dispatcher only when runner
  delivery is disabled. The same source/target must never have two dispatch
  owners.
- Per-subscription cutover order is: pause legacy dispatch, establish and audit
  the runner checkpoint, verify no legacy in-flight action, enable the reviewed
  runner phase flag, then observe before any delivery flag changes.
- Rollback disables delivery first, stops new claims, waits for or explicitly
  reconciles in-flight work, and preserves every unresolved target and lineage
  quarantine before stopping the runner and leaving the ledger read-only for
  evidence. Legacy dispatch resumes only after ownership is unambiguous and no
  unresolved runner lineage can collide with that semantic work.
- Schema migration takes a verified backup, uses a checksum/versioned migration,
  runs in an exclusive maintenance window, and performs integrity/foreign-key
  checks before commit. Failed migration restores the backup. Runtime rollback
  does not perform an in-place schema downgrade.

## Phases and feature flags

All flags default off. Flags narrow the trusted registry; they never authorize
unregistered code or weaken identity/capability checks.

`THREAD_EVENT_RUNNER_TEST_DISPATCH_DISPOSABLE_RUNTIME` is a separately named,
test-only escape hatch for breaking the dispatcher proof circularity. It is
valid only with an explicit test build/mode, a temporary ledger, an
authoritatively marked disposable runtime/thread, and the test adapter/profile
allowlist. It rejects normal project/runtime targets even when the environment
variable is set. It does not enable
`THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD`, which remains the production/project
gate.

| Phase | Planned scope | Required flag posture |
|---|---|---|
| 1 | This architecture/threat contract only | No runtime flags or processes. |
| 2 | SQLite ledger; management/status; read-only timer, filesystem, and mailbox observation; quiet/coalescing simulation; no thread wake | `THREAD_EVENT_RUNNER_ENABLED=1`, `THREAD_EVENT_RUNNER_OBSERVE=1`, allowlist limited to `timer,filesystem,mailbox`; `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD=0`. |
| 3 | Exact-thread dispatcher plus mandatory immutable revisions, project binding, pointer-only prompt, runtime capability enforcement, lease/quarantine/fencing, retry, dead-letter, busy-deferral, and reconciliation substrate | Production dispatch remains off. After non-send contract tests, use only `THREAD_EVENT_RUNNER_TEST_DISPATCH_DISPOSABLE_RUNTIME=1` for the isolated dispatch/fault matrix and then one disposable subscription. |
| 4 | Broader multi-runner, crash, sleep/wake, clock-change, load, runtime-upgrade, and quarantine-recovery hardening | Test-only disposable dispatch may continue; production/project dispatch remains off. |
| 5 | Process, GitHub, queue/task/worktree, and AX safety adapters | Each adapter has a separate allowlist/feature flag and reviewed capability profile; AX mutation is not implied by observation; production/project dispatch remains off. |
| 6 | Project pilots, observability, legacy cutover, documented rollback | After explicit approval, set `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD=1` for one exact subscription at a time; no workspace-wide wildcard enablement. |

Phase 2 is the next authorized implementation phase: **SQLite plus read-only
timer/filesystem/mailbox observation only**. It must not extract or activate
exact-thread dispatch, start PM2 state, mutate AX/apps, or claim busy deferral
works.

## Proof gates for later phases

Before Phase 2 handoff:

- schema migration, foreign key, WAL/restart, observation/checkpoint atomicity,
  dedupe, quiet-state, coalescing simulation, cancellation/update, retention,
  project isolation, deterministic timer occurrence IDs, all missed-fire/DST
  policies, unavailable tzdb, sleep/wake, and forward/backward clock tests pass;
- capability and path violations fail closed;
- no code path calls app-server `turn/start`.

Phase 3 validation order is mandatory:

1. Complete all non-send contract tests with both dispatch flags off.
2. In an isolated test runtime and temporary ledger, enable only
   `THREAD_EVENT_RUNNER_TEST_DISPATCH_DISPOSABLE_RUNTIME` and run the complete
   dispatch/fault matrix. No normal project target is accepted.
3. After that matrix is green, run one exact disposable subscription under the
   same test-only flag and inspect its full ledger/quarantine lifecycle.
4. Return the test flag to off, complete Phase 4 hardening, and only then
   consider one explicitly approved project pilot with
   `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD=1` scoped to one exact
   subscription.

Before the isolated dispatch/fault matrix or disposable subscription can pass:

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
- ambiguous acceptance remains `reconciling`, quarantines the target beyond
  lease expiry/restart, quarantines the semantic lineage across target
  retire/rebind, and does not auto-retry or generic-replay;
- dispatch dead letters require consumed ledger-stored `not_accepted` evidence
  for one same-lineage replacement, while invalid-observation reprocessing
  inherits no delivery attempt state and creates no delivery directly;
- create and pre-dispatch checks prove the authoritative native project/cwd
  binding and detect drift;
- the fixed pointer-only prompt contains no event-controlled bytes and the
  runtime attests the frozen tool/capability profile.

The test-only flag permits the integration evidence needed to evaluate this
matrix without enabling production/project dispatch. Failure at any step turns
both flags off and preserves the ledger for diagnosis. Project pilots are later
than the disposable subscription and require the Phase 4 hardening gate.

Until all of those are true, the safe operational model remains the durable
mailbox plus currently approved doorbell/worker paths. This RFC authorizes no
new background process or delivery behavior by itself.
