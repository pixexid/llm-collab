# Standalone Agent Session Bus Implementation Plan

## Status and program authority

- **Epic:** [`#85`](https://github.com/pixexid/llm-collab/issues/85)
- **Phase 0 issue:** [`#88`](https://github.com/pixexid/llm-collab/issues/88)
- **Accepted rebaseline:** TASK-240A0F
- **Current implementation base:**
  `c4ade47c5086371d35e79370a5ec815282d51097`
- **Rebaselined:** 2026-07-19

This is the repository-tracked implementation plan for the standalone
agent-session-bus program. It supersedes the sequencing and current-state
claims in the 2026-07-16 Downloads plan while preserving that plan's product
intent.

The [standalone architecture](standalone-agent-session-bus-architecture.md) is
the normative vocabulary, ownership, identity, evidence, registry, and safety
contract. The
[Thread Event Runner RFC](workflows/thread-event-runner-rfc.md) remains the
frozen safety floor. Current v2 code and the
[schema reference](schema-reference.md) remain operational authority until a
later migration gate transfers ownership.

This document uses:

- **Landed fact** — verified on the current base.
- **Frozen decision** — Phase 0 contract input, not runtime behavior.
- **Planned** — future implementation behind its dependency and rollout gates.
- **Non-goal** — excluded from the named phase.

No phase becomes active merely because it is described here.

## Current-main rebaseline

### Landed facts

- GH-87 landed through PR
  [`#116`](https://github.com/pixexid/llm-collab/pull/116) at
  `6b9760df6b0ddf8f7028c1930f5645a4feca6247`. Project-scoped
  `inbox.py --mark-all-read` no longer silently clears other projects.
- GH-86 landed through PR
  [`#117`](https://github.com/pixexid/llm-collab/pull/117) at the current base.
  `refine_task.py` rejects hollow task contracts rather than stamping
  placeholder, truncated, duplicate-section, or label-only planning.
- The configured-root S1 baseline gate runs 337 tests with
  `python3.11 -m unittest discover -s tests`; all pass on the current base.
- Current activation identity is the exact tuple
  `(project, chat, task, worktree, branch, target_agent)` with sender-only
  canonical worktree resolution, byte-exact receiver identity, and
  malformed-never-downgrades classification in
  `bin/_activation_identity.py`.
- Current v2 messages, chats, tasks, queues, and project-aware command
  boundaries require exact project identity. One agent's physical inbox is a
  cross-project collection of message pointers: reads may use an exact
  `--project` filter or span the collection, while destructive
  `--mark-all-read` requires exact `--project` or explicit `--all-projects`.
  Missing, empty, or `null` message project IDs never belong to a requested
  project.
- Current session-autobridge records remain a known legacy scope gap:
  `project_id` and `chat_id` are optional, and some lookup paths accept
  unscoped records. Such records are provenance/import evidence only, never an
  active standalone workspace-scoped record or exact-project `SessionRef`
  authority; no `workspace_id` compatibility wildcard or scope downgrade is
  allowed.
- `projects.json.repos` registers repository IDs and paths but has no typed
  sibling/documentation-companion relationship.
- Current task/container time and evidence self-reported timestamps cannot
  objectively prove that retired evidence existed before a compatibility
  cutoff.
- Amiga GH-1571 and GH-1572 remain open owners of v2 activation lease authority
  and runtime integration. P1 must not race those writers.
- Amiga PR #1579 was narrowed to app-only scope and squash-merged at
  `e31872053314faab09f0f9fee936c965d561f6ef`; it supplies no universal
  `llm-collab` schema or evidence authority.
- `llm-collab` is not yet registered as its own runtime project. The temporary
  `project_id: amiga` on the local planning/implementation tasks is coordination
  plumbing only; exact `repo_targets` and paths carry this lane's repository
  scope. Phase 0 must not inherit Amiga UI, DB, release, queue, desktop-bridge,
  or routing policy.

PR [`#115`](https://github.com/pixexid/llm-collab/pull/115) was closed unmerged
at `b0217a6cace67cfa159186c1dcfc692cae658ca9`. Its branch and wording are not
accepted inputs. Universal repository-relationship and evidence-provenance work
belongs to this program.

### Changes from the 2026-07-16 source plan

| Source-plan assumption | 2026-07-19 disposition |
|---|---|
| Phase 0 could begin from the original epic baseline. | Rebased after the GH-86/GH-87 prerequisite merges on `c4ade47`. |
| One broad Phase 0 implementation sequence. | Frozen as three separately reviewed slices: S1 architecture/plan, S2 schemas/tests, S3 protocols/compatibility/inert flags. |
| Project configuration could be described as declaring sibling docs. | Rejected. A typed registry relationship with exact registered repository endpoints is required. |
| Artifact/task age could support retired-form compatibility. | Rejected. Only an exact entry in a sealed, cutoff-policy-bound, content-addressed legacy manifest with authoritative import/observation provenance qualifies. |
| Session identity started from the earlier session-autobridge vocabulary. | Rebased to include the current-main exact activation tuple and malformed-never-downgrades behavior as v2 migration input. |
| Later work could begin from the original linear phase diagram alone. | Rebased to the actual GH-88–GH-104 dependency graph and explicit external writer gates. |
| One planning or implementation context could continue across the program. | Each write lane receives one fresh implementation worker, and each initial PR-ready head receives one fresh context-isolated reviewer. In-contract repair rounds reuse that reviewer under the bounded amendment rules in `docs/workflows/commit-push-prs.md`; a new cold reviewer is required only for the boundary-crossing amendments defined there. |

## Program outcome

The completed program provides a local-first session bus that:

1. persists canonical intent before any activation;
2. separates logical agents, installed endpoints, and exact native sessions;
3. routes by explicit capability and evidence quality;
4. records delivery attempts, acknowledgments, ambiguity, reconciliation, and
   dead letters transactionally;
5. uses one local daemon rather than one polling process per agent;
6. supports managed and native runtime adapters without promising universal
   injection;
7. retains AX as a first-party best-effort macOS adapter;
8. degrades unsupported routes to durable pull/manual work without message
   loss;
9. optionally carries canonical messages between machines through an encrypted
   transport;
10. extracts GitHub, tasks, queues, worktrees, design, and database policy into
    optional workflow packs;
11. remains inspectable, recoverable, and usable without a hosted service.

## Non-negotiable program gates

- Preserve durable intent before activation.
- Preserve exact workspace/project/repository/endpoint/session identity.
- Preserve the frozen Thread Event Runner trusted-registry, transactional,
  lease/fence, quarantine, ambiguity, and default-off requirements.
- Never infer authoritative acceptance from renderer, AX, busy, notification,
  or exit-code evidence.
- Never allow untrusted data to select code, capabilities, local paths,
  endpoints, sessions, or handlers.
- Keep all new mutation independently default off until its phase proof passes.
- Preserve current v2 behavior through import/projection and explicit cutover;
  no flag day.
- Use one writer per lane, an isolated worktree, a checkpoint commit, and a
  separate exact-head reviewer.
- Preserve unrelated local state and untracked `Evidence/`.
- Use disposable sessions for live dispatch tests, never the operator's active
  production task.

## Phase 0 — contract freeze

Phase 0 is GH-88. It changes documentation, schemas, tests/fixtures, and inert
declarations only. It does not change runtime behavior.

### S1 — architecture and repository-tracked plan

**Owner:** GH-88 S1 documentation lane

**Allowed files:**

- `docs/standalone-agent-session-bus-architecture.md`
- `docs/standalone-agent-session-bus-plan.md`
- README link text

**Frozen outputs:**

- core/runtime-adapter/transport-adapter/workflow-pack ownership;
- `Workspace`/`Agent`/`Endpoint`/`SessionRef` distinctions;
- exact project scoping and explicit workspace-only discrimination;
- durable intent, capability/evidence quality, and ambiguous non-success;
- typed `documentation_companion` repository relationship authority;
- sealed content-addressed retired-evidence compatibility authority;
- current v2 activation identity mapping;
- S1/S2/S3 sequencing and the complete issue graph.

**Non-goals:** schemas, protocols, dependency files, tests, feature
declarations, runtime code, state, queues, issue mutation, AX, PM2, or
`Evidence/`.

**Gate:** documentation/path/link assertions, `git diff --check`, full current
test suite, one checkpoint commit, and a fresh independent exact-head review.

### S2 — schema catalog, fixtures, tests, and dev validator

**Dependency:** S1 merged.

**Planned files:**

- `schemas/standalone/v1/index.json`;
- JSON Schema 2020-12 documents for `WorkspaceV1`, `AgentV1`, `EndpointV1`,
  `SessionRefV1`, `MessageV1`, `DeliveryV1`, `ReceiptV1`,
  `CapabilitySetV1`, `StateEvidenceV1`, and `EventEnvelopeV1`;
- valid/invalid fixtures beneath `tests/fixtures/standalone/v1/`;
- `tests/test_standalone_contract_schemas.py`;
- one dev/test-only validator dependency declaration.

**Required contract coverage:**

- explicit scope discriminator and no missing/null project wildcard;
- exact repository references and directed typed relationships;
- strict semantic object boundaries and bounded extensions;
- evidence source/quality/revision/correlation;
- best-effort evidence cannot claim authoritative acceptance;
- distinct ambiguous, deferred, rejected, pull-pending, accepted, and completed
  outcomes;
- current-main activation identity and malformed-never-downgrades fixtures;
- sealed retired-evidence manifest/import records keyed by canonical locator,
  content hash, form version, cutoff policy revision, objective immutable
  source boundary, and authoritative provenance.

The validator must support Python 3.10+, JSON Schema 2020-12, and registry
resolution. It is not a runtime dependency.

**Gate:** schema meta-validation, positive/negative fixtures, focused tests,
full suite, diff check, no runtime import, checkpoint commit, and fresh
exact-head review.

### S3 — protocols, compatibility, and inert flags

**Dependency:** S2 merged.

**Planned files:**

- `docs/protocols/runtime-adapter-jsonrpc-v1.md`;
- `docs/protocols/transport-adapter-boundary-v1.md`;
- `docs/protocols/workflow-pack-boundary-v1.md`;
- `docs/migration/standalone-v1-compatibility.md`;
- one inert declaration for daemon observation, canonical writes, runtime
  dispatch, AX v2, and remote transport.

**Required protocol decisions:**

- bounded JSON-RPC 2.0 over stdio, handshake/version negotiation, trusted
  manifest lookup, capabilities, exact session binding, delivery,
  cancellation, reconciliation, health, quarantine, errors, and redaction;
- transport may carry canonical envelopes but cannot select runtime actions;
- workflow packs may own project policy but cannot bypass core identity or
  evidence;
- a field/source decision matrix for v2 configuration, `Chats/`, inboxes,
  tasks, queues, session autobridge, PM2, AX, and current commands;
- feature declarations are data only, independently default off, and unread by
  current runtime paths;
- omitted and explicit-false declarations preserve current command behavior.

**Gate:** protocol/schema cross-reference checks, compatibility completeness,
runtime no-consumption proof, full suite, diff check, checkpoint commit, and
fresh exact-head review.

### Phase 0 rollback

Revert the Phase 0 documentation, schemas, fixtures/tests, dependency
declaration, and inert declaration. No process stop, state rewrite, database
repair, or runtime cleanup is required because Phase 0 consumes none of them.

## Current issue graph

All program issues are open at this rebaseline unless marked otherwise.

| Issue | Phase | Minimum dependency | Owning outcome |
|---|---|---|---|
| [#85](https://github.com/pixexid/llm-collab/issues/85) | Epic | — | Program acceptance and child tracking. |
| [#88](https://github.com/pixexid/llm-collab/issues/88) | P0 | GH-86 and GH-87 landed | S1/S2/S3 contract freeze; no runtime mutation. |
| [#90](https://github.com/pixexid/llm-collab/issues/90) | P1 | #88 merged; Amiga #1571/#1572 merged or explicitly descoped/superseded | Observation-only daemon and ledger. |
| [#91](https://github.com/pixexid/llm-collab/issues/91) | P2 | P1 merged; #88 schemas/protocols frozen | Canonical messages/receipts and v2 importer/projections. |
| [#92](https://github.com/pixexid/llm-collab/issues/92) | P3 | P2 merged; runtime-adapter V1 frozen | Adapter supervisor, SDK, and conformance kit. |
| [#93](https://github.com/pixexid/llm-collab/issues/93) | P4A | P3 merged | Managed Codex read-only/session adapter. |
| [#94](https://github.com/pixexid/llm-collab/issues/94) | P4B | P4A merged; P1 ledger proof gates complete | Feature-gated exact Codex delivery and reconciliation. |
| [#95](https://github.com/pixexid/llm-collab/issues/95) | P5A | #92 merged | Claude native attached-session adapter. |
| [#96](https://github.com/pixexid/llm-collab/issues/96) | P5B | #92 merged | pi native attached-session adapter. |
| [#97](https://github.com/pixexid/llm-collab/issues/97) | P5C | #92 merged | OpenCode native attached-session adapter. |
| [#98](https://github.com/pixexid/llm-collab/issues/98) | P6 | #91 and #92 merged; #77 safety frozen | AX Doorbell v2 profiles and honest evidence. |
| [#99](https://github.com/pixexid/llm-collab/issues/99) | P7 | #91 and #92 merged | Optional encrypted remote transport. |
| [#100](https://github.com/pixexid/llm-collab/issues/100) | P8A | #91 merged; canonical message contracts stable | Generic task/workflow pack boundary. |
| [#101](https://github.com/pixexid/llm-collab/issues/101) | P8B | P8A merged | GitHub, queue, worktree, review, and release packs. |
| [#102](https://github.com/pixexid/llm-collab/issues/102) | P8C | P8A merged | Design and database policy packs. |
| [#103](https://github.com/pixexid/llm-collab/issues/103) | P9A | P1–P3 and selected adapter/workflow state contracts stable | Dashboard/TUI and operational controls. |
| [#104](https://github.com/pixexid/llm-collab/issues/104) | P9B | All required P1–P9A capabilities merged and reviewed | Packaging, migration, backup, restore, and 1.0 release. |

The minimum dependency graph is:

```text
GH-88 S1 -> S2 -> S3
                  |
                GH-90 -> GH-91 -> GH-92
                  |        |        |
                  |        |        +-> GH-93 -> GH-94
                  |        |        +-> GH-95
                  |        |        +-> GH-96
                  |        |        +-> GH-97
                  |        +----------> GH-100 -> GH-101
                  |                         \----> GH-102
                  +-------------+-------> GH-103
                                |
                  GH-91 + GH-92 +-> GH-98
                                +-> GH-99

all required P1-P9A capabilities -> GH-104
```

This graph permits planning and path-disjoint implementation in parallel only
after the named minimum contracts stabilize. It does not waive one-writer,
queue-order, migration-owner, or exact-head review gates.

## Overlap and ownership disposition

The rebaseline classifies every overlapping v2 issue so the standalone program
does not create duplicate mechanisms.

| Issue | Classification | Disposition |
|---|---|---|
| llm-collab #29 | `close-as-superseded` | Closed. Current queue-runner state, stop conditions, loop modes, and bounded review wait are later packaged by P8B. |
| llm-collab #31 | `close-as-superseded` | Closed. Current independent exact-head review supersedes the old formulation; P8B packages it. |
| llm-collab #75 | `parallel-safe` | Project-specific direct-app UI policy remains separate and maps to P8C. |
| llm-collab #82 | `parallel-safe` | Project-specific production DB classification remains separate and maps to P8C. |
| llm-collab #86 | `fix-before-P0` | Closed through PR #117 before S1. P8A preserves the substantive hollow-contract rejection. |
| llm-collab #87 | `fix-before-P0` | Closed through PR #116 before S1. P2 preserves safe project-scoped inbox semantics. |
| llm-collab #89 | `parallel-safe` | Canonical release-lifecycle enforcement remains its own owner and later maps to P8B. |
| Amiga #1564 | `parallel-safe` | Release-watcher correctness remains project work and later maps to P8B. |
| Amiga #1565 | `close-as-superseded` | Closed as duplicate; exact-SHA persisted release verdict requirements transfer to #89/P8B. |
| Amiga #1566 | `map-to-later-phase` | Exact root-Codex AX targeting and honest outcomes belong to P6; no second AX mechanism. |
| Amiga #1571 | `parallel-safe` with P1 gate | Owns current v2 activation lease/fence work; must settle before P1 and becomes P4B import/retirement input. |
| Amiga #1572 | `parallel-safe` with P1/P2 gate | Owns current v2 inbox/dispatch integration; must settle before P1 and informs P2 hold/refusal compatibility. |

Duplicate-mechanism guards:

- one active lease/fence authority;
- one release-closure enforcement contract;
- one AX targeting/profile mechanism;
- one registry authority for repository relationships;
- one sealed authority for grandfathered evidence.

## Phase 1 — observation-only daemon and ledger

**Issue:** GH-90

**Goal:** introduce one transactional local service without waking any runtime.

### Planned deliverables

- SQLite migration framework with WAL, foreign keys, bounded busy timeout,
  private permissions, integrity checks, and backup;
- single-instance daemon and local Unix control socket;
- daemon start/stop/status/logs and doctor surfaces;
- filesystem events plus periodic reconciliation for mailbox/chat observation;
- transactional observations, checkpoints, dedupe, project/workspace
  isolation, bounded retention, and audit;
- import of current activation/session-autobridge records as provenance only;
- all dispatch paths structurally disabled.

### Activation gate

Amiga #1571/#1572 must merge or be explicitly descoped/superseded with one
remaining owner. P1 may not write active v2 lease/inbox/autobridge state while
those lanes own it.

### Acceptance and rollback

Prove duplicate observation suppression, concurrent scan safety, crash recovery,
project isolation, sleep/wake cursor correctness, retention of unresolved
state, and zero runtime/AX/GitHub/app mutation. Rollback stops observation and
leaves the ledger as read-only evidence; current v2 owners remain unchanged.

## Phase 2 — canonical messages, receipts, and v2 compatibility

**Issue:** GH-91

**Goal:** give each communication intent one immutable ID/body and make v2
files explicit import/projection surfaces.

### Planned deliverables

- canonical `message_id`, body reference/hash, recipients, dedupe key, reply,
  TTL, acknowledgment policy, and artifact references;
- separate delivery, attempt, receipt, and acknowledgment records;
- `Chats/` and inbox views as compatibility projections when canonical writes
  are enabled;
- idempotent import of paired v2 files with original-file preservation;
- exact project-scoped command compatibility for `deliver.py`, `inbox.py`,
  `new_chat.py`, and aliases;
- acknowledgment, delivery inspection/reconciliation, and dead-letter
  controls;
- immutable import provenance and rollback.

Retired-form import is accepted only through the sealed manifest authority
frozen in P0. Current `created_utc`, filesystem mtime, or self-reported
production time never grants compatibility. V2 activation packets retain the
settled malformed and hold/refusal semantics from current main and Amiga
#1572.

### Acceptance and rollback

Prove one logical intent per dedupe key, fan-out without body duplication,
idempotent/reversible imports, preserved task/chat links, safe cross-project
isolation, and rollback to direct-file compatibility without message loss.

## Phase 3 — adapter supervisor and conformance

**Issue:** GH-92

**Goal:** make runtime support a bounded protocol implementation, not a core
rewrite.

### Planned deliverables

- out-of-process JSON-RPC adapter supervisor;
- trusted manifest and version validation;
- startup/request/output limits, restart backoff, redacted logs, health, and
  version-drift quarantine;
- capability negotiation;
- manual/pull-only reference adapter;
- deterministic fake runtime;
- adapter SDK, authoring guide, replay fixtures, and conformance suite.

### Acceptance and rollback

Conformance covers unsupported, exact success, best-effort visibility,
authoritative busy, rejection-before-acceptance, ambiguity-after-possible-
acceptance, timeout, crash, duplicate event, session disappearance, and version
mismatch. A bad adapter cannot corrupt canonical intent or widen authority.
Rollback disables the adapter/supervisor and leaves messages pull-pending.

## Phase 4 — managed Codex

### P4A read-only/session adapter

**Issue:** GH-93

**Goal:** observe and register exact managed Codex sessions without sending.

Planned work includes supported local App Server connection, protocol/version
probing, explicit client initialization, thread start/list/read/resume/status,
loaded-session discovery, approvals/errors/background state, and authoritative
cwd/project evidence in exact `SessionRef` objects.

P4A does not claim co-presence with a separately owned Codex TUI/Desktop
renderer and does not deliver a message.

### P4B exact delivery and reconciliation

**Issue:** GH-94

**Goal:** enable exact managed delivery only after the ledger and host proof
gates pass.

Planned work includes a stable message correlation/idempotency token, one
per-session lease/fence, authoritative busy deferral, policy-gated steering,
acceptance reconciliation, crash recovery, and distinct host versus renderer
receipts.

The settled v2 activation authority from Amiga #1571/#1572 is an explicit
import-and-retirement input. P4B must prove there is one lease/fence owner after
cutover.

Production delivery remains off until supported-version tests prove exact
runtime home/project/session binding, authoritative busy, request
acceptance/rejection, idempotency or queryable reconciliation, and no duplicate
turn after restart. Ambiguous acceptance remains quarantined without retry.

Rollback disables new sends first, reconciles in-flight attempts, preserves
quarantine, and converts pending work to pull/manual.

## Phase 5 — native attached-session adapters

**Issues:** GH-95 Claude, GH-96 pi, GH-97 OpenCode

**Goal:** provide automatic receive only where the host exposes a safe,
session-bound API.

Each adapter is separately packaged and owns only native binding, session
injection, and host state. Portable inbox following remains core/transport
behavior. Each adapter must support version probing, exact session mapping,
message-ID acknowledgment, clean disable/removal, drift quarantine, and the
shared conformance suite.

Unsupported Codex CLI, Antigravity, Copilot, or another host remains
`pull_pending` until a supported hook exists. No adapter may infer a hook from a
window, process, or local history file.

Rollback removes or disables one adapter without marking pending work
completed or affecting other hosts.

## Phase 6 — AX Doorbell v2

**Issue:** GH-98

**Goal:** retain AX as a first-party `ui_attached` adapter with externalized,
validated profiles and honest structured evidence.

### Planned deliverables

- versioned profile manifests for verified Codex, Claude, and ZCode surfaces;
- compiled trusted mutation/send resolution;
- probe, record, dry-run, state, and confirm controls;
- sanitized AX-tree fixtures, app-version/profile health, and drift quarantine;
- structured `VERIFIED`, `AMBIGUOUS`, and `NOT_DELIVERED` outcomes;
- explicit absent/starting/idle/processing/approval/rate-limit/disconnected/
  error/ambiguous state;
- preserved fail-closed composer identity, foreign-web exclusion, frozen-window
  identity, anti-duplicate behavior, and visible-turn checks from #77.

P6 absorbs Amiga #1566 requirements: exact root-Codex task targeting, no
composer, multiple/ambiguous task handling, target-correct but send-disabled
state, intrinsic newline/placeholder emptiness, and post-submit loss of
verification identity. It does not create a second selector/profile mechanism.

AX evidence is never native session authority. Recipient busy, a submitted UI
action, or generic exit zero cannot become exact acceptance without native or
exact-session acknowledgment evidence.

Rollback quarantines the changed profile and returns work to pull/manual; it
does not discard the durable packet or re-ring an ambiguous attempt.

## Phase 7 — optional encrypted transport

**Issue:** GH-99

**Goal:** exchange canonical messages across machines without inventing a new
cryptographic protocol.

Planned direction is an optional `retalk` transport with practical
`agent-talk` interoperability: verified contact fingerprints, canonical
message-ID mapping, encrypted carriage, untrusted relay, replay protection,
offline outbox retry, transport receipts, and local runtime routing.

Remote payloads cannot select local commands, capabilities, adapters, paths,
or sessions. A transport outage must not block local collaboration. Minimal
local installation has no retalk dependency.

Rollback disables remote ingress/egress while retaining canonical outbox and
receipt evidence for reconciliation.

## Phase 8 — workflow-pack extraction

### P8A generic task/workflow boundary

**Issue:** GH-100

**Goal:** extract generic task lifecycle without weakening current activation
quality gates.

P8A preserves the substantive GH-86 behavior: placeholder summary/acceptance/
verification, duplicate canonical sections, truncation, and label-only risk
analysis cannot earn a refinement stamp. Concise real tasks and explicit
trivial-task `skip_refinement` remain valid.

### P8B GitHub, queue, worktree, review, and release packs

**Issue:** GH-101

**Goal:** make developer orchestration optional while preserving current full
behavior.

P8B includes issue/project integration, ordered queues, isolated worktrees,
preflight, checkpoint, exact-head review, merge/cleanup, and release closure.
It consumes typed repository relationships for docs-pair enforcement. It
preserves GH-89 as release-lifecycle owner, exact-SHA persisted verdict
requirements transferred from Amiga #1565, and watcher correctness from Amiga
#1564.

### P8C design and database policy packs

**Issue:** GH-102

**Goal:** isolate project-specific UI/design and production database contracts.

P8C absorbs the substantive policies from llm-collab #75/#82 without making
Amiga, Impeccable, Supabase, design sources, DB refs, or tool names core
defaults.

### Acceptance and rollback

Installation profiles become:

```text
minimal    core + daemon + manual/local-file route
developer  minimal + generic tasks + git/worktrees
full       developer + GitHub/queue + selected policy packs
```

Minimal messaging works without GitHub, PM2, Supabase, or worktrees. The full
profile reproduces current project behavior. A disabled pack cannot bypass or
break canonical messaging. Rollback re-enables the compatibility owner only
after one-owner reconciliation.

## Phase 9 — operations and 1.0

### P9A dashboard/TUI and controls

**Issue:** GH-103

**Goal:** expose agents, endpoints, exact sessions, delivery evidence, state,
leases, retries, dead letters, adapter health, pause/resume/cancel/reconcile,
human takeover, and safe "open in host" controls.

The UI must visibly distinguish authoritative, best-effort, ambiguous, and
pull-only routes. It cannot turn a display action into delivery evidence.

### P9B packaging, migration, backup, and release

**Issue:** GH-104

**Goal:** provide supported installation, service lifecycle, upgrade, restore,
diagnostics, incident response, and 1.0 gates.

Planned work includes launchd, systemd user service, a later Windows
service/named-pipe design, init/doctor/migrate/backup/restore/uninstall,
packaging choice, redacted diagnostics, repair tooling, compatibility reports,
migration/rollback runbooks, and the complete live host matrix.

P9B preserves project-scoped release-closure configuration and exact-merge-SHA
operational evidence. No project inherits another project's workflow, branch,
jobs, or smoke labels.

Release requires all required P1–P9A capabilities merged, independently
reviewed, migration/rollback exercised, legacy v2 import proven, and unresolved
ambiguity preserved across upgrade and restore.

## Testing strategy

### Contract and unit tests

- schema/catalog/reference integrity;
- invalid IDs and unknown semantic fields;
- scope/project/repository/session mismatch;
- typed relationship missing/ambiguous/stale lookup;
- sealed legacy-manifest provenance and hash mismatch;
- state transitions and capability/evidence contradictions;
- dedupe, idempotency, routing precedence, and migration transformations.

### Integration tests

- daemon, SQLite, and local socket;
- filesystem observation plus reconciliation;
- adapter lifecycle and bounded protocol;
- crash around external send and receipt commit;
- busy deferral/coalescing and acknowledgment binding;
- dead-letter recovery and legacy import/export;
- exact one-owner migration for leases, canonical writes, and projections.

### Fault tests

- process kill at every transaction boundary;
- malformed/hostile adapter output;
- locked/corrupt database and failed migration restore;
- partial/corrupt message file;
- clock changes and sleep/wake;
- host/profile version drift;
- transport disconnect and uncertain runtime acceptance;
- stale lease/fence and duplicate source event.

### Adapter conformance

Every adapter covers unsupported, exact success where supported, best-effort
visibility, authoritative busy, rejected-before-acceptance,
ambiguous-after-possible-acceptance, timeout, restart, duplicate event,
session disappearance, and version mismatch.

### Live validation

Live tests use disposable sessions. Expected route quality is:

| Host | Route | Expected quality |
|---|---|---|
| Managed Codex App Server | managed | authoritative only after exact proof gates |
| Codex Desktop | AX/UI attached or pull | best effort plus acknowledgment |
| Claude Code | native attached | automatic receive when bound |
| Claude Desktop | native bridge if supported, otherwise AX/pull | capability-specific; no universal claim |
| pi | native attached | automatic receive when bound |
| OpenCode | native attached | automatic receive when bound |
| ZCode | AX/UI attached | best effort |
| Unprofiled host | pull/manual | no false support claim |

## Security and trust plan

- Registry changes are privileged reviewed code/data changes.
- No arbitrary shell command is stored as event-selected execution.
- Message/event/transport bodies are bounded untrusted data.
- Database, socket, backup, and diagnostics permissions are user-private.
- Secrets remain references and are redacted from logs, ledger, and diagnostics.
- Exact workspace/project/repository/session ownership is checked on every
  project-aware query and mutation.
- Filesystem adapters require approved roots and race-resistant no-follow
  traversal where the platform supports it; otherwise they stay disabled.
- Native capability restrictions are runtime-enforced and attested.
- AX profiles are selectors/data, not arbitrary executable scripts.
- Remote identity requires explicit contact verification.
- Every mutation records actor, correlation, adapter/profile revision,
  evidence, and result.

## Migration and rollback sequence

For each mutable surface:

1. capture a verified backup and current owner;
2. create new state without rewriting the old source;
3. import through versioned provenance;
4. run observation-only comparison;
5. prove exact identity, dedupe, and no cross-project match;
6. pause the old writer;
7. reconcile in-flight and ambiguous work;
8. enable one new writer behind one scoped flag;
9. validate a quiet period and compatibility projections;
10. retire the old owner only when ownership is unambiguous.

Rollback reverses ownership, not evidence:

1. disable new mutation and claims;
2. wait for or reconcile in-flight attempts;
3. preserve canonical intent, attempts, receipts, and quarantine;
4. return pending work to pull/manual;
5. restore the previous owner only when duplicate execution is impossible;
6. leave the new ledger read-only for audit.

Schema rollback restores a verified pre-migration backup; it does not attempt an
in-place downgrade.

## Worker, review, and publication discipline

Every implementation slice follows this sequence:

1. refresh and freeze exact current `main`;
2. create a new task with real implementation-risk analysis and Claude
   refinement;
3. Codex accepts the contract and provisions one isolated worktree/branch;
4. activate one fresh implementation worker for that lane;
5. limit writes to the exact path contract;
6. run focused verification, the full suite, and `git diff --check`;
7. create exactly one local checkpoint commit;
8. activate one fresh context-isolated reviewer against the exact checkpoint
   SHA;
9. repair findings in a separately controlled writer turn and re-review at the
   new exact head, reusing the same reviewer for in-contract amendments; at
   most 2 review-fix cycles follow the initial review (3 when the contract
   scope includes payments, auth, permissions, schema/migrations, or
   irreversible writes; docs-only lanes with a proven zero-consumer scan always
   cap at 2), after which exactly one terminal disposition is mandatory:
   merge at the current head with `risk-accepted-followup` (open findings move
   to a new issue), `descope`, `split`, or a durable operator escalation;
10. publish a focused ready-for-review PR with scope, non-goals, compatibility,
    security, verification, migration, and rollback evidence;
11. merge only the reviewed exact head and reconcile task/issue state.

A worker/reviewer task is never reused for a later phase or a different write
lane; within its own lane's review-fix cycles, reviewer reuse follows the
bounded amendment rules above. One worker owns
one write lane. Review and planning may run in parallel when read-only; separate
implementation writers run in parallel only with recorded path/state/resource
non-overlap and merge order.

No worker may push, open a PR, merge, mutate runtime state, activate AX/ZCode,
or edit GitHub unless that action is explicitly assigned. Untracked
`Evidence/` remains untouched.

## Definition of phase and program completion

A phase is complete only when:

- all named deliverables and unhappy-path/rollback gates pass;
- focused and full configured tests pass at the exact head;
- schema/protocol/compatibility impact is recorded;
- security and migration ownership are explicit;
- an independent reviewer accepts the exact head — fresh and context-isolated
  for the initial PR-ready head, reused per the bounded amendment rules in
  `docs/workflows/commit-push-prs.md` for in-contract amended heads;
- the issue, task, and release state reflect the real result.

The epic is complete only when the minimal standalone profile works, supported
adapters pass conformance and live matrices, optional workflow packs reproduce
current full behavior, existing v2 workspaces migrate with provenance and
rollback, and the operational release gates pass. A happy path, UI visibility,
or one successful dispatch is insufficient.
