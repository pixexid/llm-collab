# Standalone Agent Session Bus Architecture

## Status and authority

This document is the normative architecture contract for the standalone
`llm-collab` agent session bus. It freezes vocabulary, ownership, identity,
evidence, registry, safety, and compatibility boundaries. It does not implement
or enable a daemon, ledger, adapter, transport, workflow pack, feature flag, or
mutation path.

The key words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** describe future
standalone requirements unless a paragraph is explicitly labeled **Landed**.
The status labels used here are:

- **Landed** — behavior present on `llm-collab` `main` at
  `c4ade47c5086371d35e79370a5ec815282d51097`.
- **Frozen decision** — normative input to Phase 0 schemas and protocols, but
  not yet runtime behavior.
- **Future requirement** — required in a named later phase.
- **Non-goal** — deliberately not promised by this architecture.

The following sources remain authoritative within their boundaries:

1. The [Thread Event Runner RFC](workflows/thread-event-runner-rfc.md) is the
   frozen safety floor for exact identity, trusted registries, transactional
   observation, leases and fencing, ambiguous acceptance, quarantine, and
   default-off dispatch. This architecture generalizes that contract; it does
   not weaken it.
2. The [current v2 schema reference](schema-reference.md) and current code are
   the operational authority until a later migration gate changes ownership.
3. This document is the standalone architecture authority.
4. The [rebaselined implementation plan](standalone-agent-session-bus-plan.md)
   is the phase, issue, review, and rollout authority.

Pull request
[`#115`](https://github.com/pixexid/llm-collab/pull/115) was closed unmerged at
`b0217a6cace67cfa159186c1dcfc692cae658ca9`. Neither that branch nor its prose
is accepted authority. This document derives from current `main`, the frozen
RFC, issues
[`#85`](https://github.com/pixexid/llm-collab/issues/85) and
[`#88`](https://github.com/pixexid/llm-collab/issues/88), and the accepted
TASK-240A0F rebaseline.

## Product boundary

The standalone product is a local-first agent session bus. It coordinates
durable communication among logical agents and exact runtime sessions without
claiming universal remote control.

```text
operator CLI / local UI
          |
     llm-collabd
          |
  +-------+-------------------+
  |       |                   |
ledger  router          session registry
  |       |                   |
  +--- runtime adapters ------+
  |       |                   |
managed  native             UI/manual
runtime  attached           attached
          |
   transport adapters
          |
    workflow packs
```

The architecture separates three capabilities that current integrations can
easily conflate:

1. **Persist intent** — durably record one canonical message.
2. **Observe state** — discover that a message, event, or runtime state exists.
3. **Activate a session** — inject or point to work in one exact native
   session.

Persistence does not prove observation. Observation does not prove injection.
Injection does not prove acceptance or completion.

A minimal installation must eventually support local durable collaboration
without GitHub, PM2, Supabase, worktrees, product-specific design policy, a
hosted service, or a native session injection hook.

## Architectural invariants

Every later schema, protocol, adapter, transport, workflow pack, and migration
MUST preserve these invariants:

1. Canonical intent is durable before any activation attempt.
2. `Workspace`, `Agent`, `Endpoint`, and `SessionRef` are distinct objects.
3. Project-aware operations use exact registered project identity. Missing,
   empty, or `null` project identity is never a wildcard.
4. Repository relationships come from a typed registry, not prose.
5. Capability and evidence quality are explicit.
6. Ambiguous delivery is non-success and is never blindly retried.
7. Untrusted payloads cannot select executable code, capabilities, local paths,
   endpoints, sessions, or workflow handlers.
8. One transactional control ledger owns claims, attempts, receipts, leases,
   fencing, reconciliation, and unresolved evidence.
9. Current v2 state remains authoritative until an explicit migration and
   cutover gate transfers ownership.
10. Every mutation surface starts disabled and rolls back without discarding
    canonical intent or unresolved evidence.

## Component ownership

### Core

The core owns only project-independent session-bus mechanics:

- workspace and exact scope identity;
- logical agents, endpoints, and session references;
- registered repositories and typed repository relationships;
- canonical message metadata and body references;
- recipients, deliveries, attempts, receipts, and acknowledgments;
- capability and evidence quality;
- observations, checkpoints, deduplication, leases, fencing, retries,
  quarantine, dead letters, and audit;
- trusted registry lookup and policy evaluation;
- migration/import records and compatibility projections;
- the local control API and CLI contract.

The core MUST NOT contain Amiga, Supabase, Impeccable, GitHub Projects, a
mandatory worktree workflow, or another product's paths and policy.

### Runtime adapters

A runtime adapter owns host-specific session behavior:

- discover and inspect native sessions;
- create or bind a session when the host supports it;
- report host capabilities and constraints;
- observe host state and events;
- deliver, steer, interrupt, or defer work only when authorized;
- return structured host evidence;
- surface approvals, errors, and completion.

An adapter does not own canonical intent, routing policy, delivery state, or
workflow policy. It MUST NOT turn renderer visibility into runtime acceptance.

### Transport adapters

A transport adapter carries canonical envelopes between installations. It may
own transport identity mapping, encrypted carriage, ingress cursors, replay
protection, offline retry, and transport receipts.

A transport payload MUST NOT select a local command, adapter implementation,
capability profile, handler, filesystem path, `Endpoint`, or `SessionRef`.
Runtime routing remains local to the receiving installation.

### Workflow packs

Workflow packs may own optional orchestration:

- generic task lifecycle and refinement gates;
- GitHub issues, project queues, and release closure;
- worktree creation, preflight, review, merge, and cleanup;
- design/UI evidence;
- database policy;
- repository-specific runbooks and acceptance.

A workflow pack consumes core identities and evidence. It MUST NOT bypass
canonical messages, exact session binding, delivery evidence, workspace/project
isolation, registry validation, or ambiguity handling.

## Core object model

### Workspace

A `Workspace` is the top-level routing and security namespace. Every standalone
object belongs to exactly one `workspace_id`.

A standalone record is either explicitly workspace-scoped or bound to one
exact registered project. The Phase 0 schemas MUST represent that distinction
with a discriminator; they MUST NOT infer workspace scope from a missing or
`null` `project_id`.

Current v2 messages, chats, tasks, queues, runtime bindings, and project-aware
commands remain exact-project-scoped. Introducing `workspace_id` does not
create an unscoped compatibility path.

### Registered project and repository reference

A project is a registered workflow/routing namespace inside a workspace. A
repository reference is the exact pair:

```text
(project_id, repo_id)
```

Both components resolve through the same trusted workspace registry revision.
A path, repository display name, GitHub slug, task claim, or cwd is not a
repository ID. Canonical paths are resolved only after the exact registry
reference succeeds.

### Agent

An `Agent` is one logical collaborator. It carries stable logical identity and
role metadata, not a native process, application, thread, bundle ID, cwd, or
wake strategy.

One `Agent` may own zero or more `Endpoint` objects. Multiple agents MUST NOT
silently share an endpoint identity.

### Endpoint

An `Endpoint` is one installed runtime or explicit human/pull surface capable
of acting for one agent. It binds:

- one exact `agent_id`;
- one trusted adapter registration and revision;
- one trust class such as `managed`, `native_attached`, `ui_attached`,
  `pull_only`, or `human`;
- declared capability constraints;
- platform and configuration references that contain no copied secret.

An endpoint is not a session. One endpoint may expose multiple simultaneous
native sessions.

### SessionRef

A `SessionRef` binds one exact native session to one endpoint. It includes:

- one exact `workspace_id`;
- an explicit scope classification and, when project-aware, one exact
  `project_id`;
- one exact `endpoint_id`;
- one exact native session identifier;
- exact registered repository and canonical cwd evidence when the session
  claims repository binding;
- evidence source, revision, integrity, and observation metadata.

Display names, window indices, sidebar order, app titles, "latest", prefix
matches, and caller-supplied cwd claims are not authoritative session identity.
An adapter that cannot prove an exact binding MUST expose a weaker route, such
as `ui_attached` or `pull_only`, rather than invent a `SessionRef`.

### Message, Delivery, Receipt, and Acknowledgment

A `Message` is one immutable communication intent with one canonical ID and
body reference. Fan-out does not duplicate that intent.

A `Delivery` is one route for one message to one endpoint and, when known, one
exact session. Fan-out creates separate deliveries.

A `Receipt` is immutable structured evidence about one delivery or attempt. It
records source, quality, revision, correlation, and the exact state it proves.
A process exit code alone is not a receipt.

An `Acknowledgment` is a recipient assertion such as `seen`, `accepted`,
`blocked`, `completed`, or `rejected`. It is stronger than UI inference only
when it is bound to the canonical message and an exact authenticated
`SessionRef`. Acknowledgment still does not erase contradictory or unresolved
host evidence.

## Typed repository relationship authority

### Frozen decision

The current `projects.json.repos` map registers repository IDs and paths but
declares no relationship among repositories. Any docs-pairing rule therefore
needs a new typed registry authority.

A repository relationship MUST contain:

- an immutable `relationship_id`;
- an exact `relationship_type`;
- one exact `source` repository reference;
- one exact `target` repository reference;
- the registry revision that validated both endpoints;
- lifecycle/revision metadata suitable for auditable replacement.

The first required relationship type is `documentation_companion`. It is
**directed**:

```text
source implementation repository -> target documentation repository
```

Direction is semantic. An inverse relationship is not implied. A reverse rule
requires its own registry entry or a future relationship type whose contract
explicitly defines symmetry.

The registry permits zero or more directed `documentation_companion`
relationships per source, and one target may serve multiple sources. The exact
tuple `(relationship_type, source, target)` MUST be unique within one registry
revision.

A consumer that needs one documentation pair MUST name the exact
`relationship_id`. A convenience lookup by source and type is valid only when
it returns exactly one active relationship:

- zero matches → `relationship_missing`, fail closed;
- more than one match → `relationship_ambiguous`, fail closed;
- unresolved source or target endpoint → invalid registry, fail closed;
- stale registry revision → revision mismatch, fail closed.

The consumer MUST NOT choose the first, latest, nearest-path, same-name, or
prose-mentioned repository. A task body, issue, runbook sentence, folder
layout, commit message, or reviewer assumption cannot create registry
authority.

The relationship type and lookup behavior are project-neutral. They contain no
Amiga-specific repository name, path, direction, or fallback. Project-specific
selection is data in the trusted registry.

## Durable intent and delivery evidence

### Durable intent

The canonical message and recipient intent MUST commit before a runtime API,
native plugin, AX action, notification, transport send, or human relay is
attempted. A doorbell contains at most a bounded pointer to that intent. It is
never the source of truth.

A crash after intent commit and before activation leaves pending durable work.
A crash after a possible external acceptance leaves an ambiguous attempt that
must be reconciled; it does not recreate intent or authorize a blind resend.

### Capability quality

Each endpoint capability reports one of:

- `unsupported` — the endpoint cannot provide the capability;
- `best_effort` — the endpoint may attempt or observe it but cannot prove the
  native semantic result;
- `authoritative` — the endpoint can return source-specific proof meeting the
  frozen contract and revision.

Capability quality is a ceiling, not a delivery result. An endpoint declaring
authoritative support must still return evidence for each attempt.

### Evidence quality and state

Every state claim carries evidence source, quality, adapter/profile revision,
correlation identifier, and observation time. Evidence MUST prove only the
state it actually observed:

```text
persisted -> routed -> injected -> visible -> accepted
          -> processing -> acknowledged -> completed
```

Missing stages remain unknown. In particular:

- visible UI is not native acceptance;
- recipient busy is not proof that a pointer entered the intended session;
- inbox consumption proves durable packet consumption, not AX delivery;
- a shell exit status is not authoritative acceptance without a protocol
  contract that says so;
- `best_effort` evidence cannot encode authoritative `accepted` or
  `completed`.

`ambiguous` means external acceptance may have occurred. It is non-terminal
with respect to reconciliation and is never success. It MUST NOT auto-retry,
fall through to another session, or become completed from elapsed time,
process state, renderer state, or operator narrative.

Only authoritative not-accepted evidence may permit a bounded retry. Only
authoritative accepted/completed evidence or an exact-session acknowledgment
may advance the corresponding claim.

## Retired-evidence compatibility authority

### Frozen decision

Compatibility with a retired evidence form is a closed-set migration decision,
not an age heuristic.

Each cutoff policy revision defines an objective membership boundary and owns
one immutable, sealed legacy-evidence manifest. The boundary MUST be an
authoritative immutable source snapshot, ledger sequence/checkpoint,
content-addressed repository revision, or equivalent sealed observation record.
A task timestamp or a wall-clock cutoff by itself is not a membership boundary.

Each allowed entry is keyed by the exact tuple:

```text
(
  canonical_locator,
  content_hash,
  evidence_form_version,
  cutoff_policy_revision
)
```

The entry also records authoritative provenance:

- the trusted observer/importer identity and implementation revision;
- the immutable observation/import transaction ID;
- the source registry/project/workspace identity;
- the exact bytes or a canonical byte derivation used for the hash;
- the authority's observation/import record and integrity evidence;
- timestamps as audit metadata, never as sole eligibility proof.

An importer running after the cutoff may read only the immutable source
snapshot named by that boundary and must prove that the exact bytes were in it.
It cannot import the current mutable path merely because the path or task
predates the cutoff.

A collection draft has no compatibility authority. Publishing a policy revision
atomically seals its complete manifest, and the sealed manifest is immutable. A
policy revision cannot silently reopen or append to its legacy set. A new
policy requires a new reviewed revision and must not make new retired-form
artifacts look old.

To accept retired evidence, a compatibility reader MUST:

1. canonicalize the locator under the versioned locator rules;
2. hash the actual bytes under the recorded algorithm;
3. require an exact manifest entry for locator, hash, form version, and cutoff
   policy revision;
4. verify the authoritative provenance and manifest seal;
5. reject on any mismatch, missing entry, unknown form, or untrusted importer.

Task/container `created_utc`, task status, filesystem mtime, current container
age, path age, commit prose, and self-reported `produced_at_utc` are not cutoff
authority. An old open task that creates new retired-form evidence after the
cutoff fails because its exact locator/content-hash entry is absent. Replacing
bytes at a grandfathered locator also fails because the hash changes.

Retired evidence never becomes authoritative merely by import. Its manifest
entry authorizes compatibility parsing under the recorded support window; the
resulting claim still carries the evidence quality supported by that form.

## Trusted registries and untrusted data

Adapters, transports, handlers, capability profiles, repository relationships,
workflow packs, evidence importers, and compatibility policies are selected
from reviewed, versioned registries.

Untrusted event/message/transport data MUST NOT select or widen:

- executable commands, modules, adapters, or workflow handlers;
- tools, filesystem roots, network/UI permissions, or capability profiles;
- projects, repositories, endpoints, native sessions, or retry policy;
- feature flags, retention, lease, fencing, or reconciliation policy.

Registry configuration uses strict object boundaries. Unknown fields with
required semantics fail closed. Extension data is allowed only in an explicit,
bounded namespace that cannot alter routing, trust, or execution semantics.

Native capability restrictions must be enforced and attested by the runtime.
Prompts are not a security boundary. Trusted adapter code runs with the local
user's authority and therefore remains privileged reviewed code.

## Transactional ledger and concurrency safety

### Frozen safety inheritance

The standalone ledger generalizes, but MUST preserve, the Thread Event Runner
requirements:

- SQLite WAL mode, foreign keys, bounded busy timeout, transactional
  migrations, and backup before migration;
- one writer service;
- transactional observation/checkpoint/deduplication;
- immutable message, revision, attempt, and receipt identity;
- one lease per exact target with a monotonic fence token;
- compare-and-swap claim and completion;
- no database transaction held across filesystem, network, subprocess, AX, or
  runtime calls;
- target and semantic-work quarantine when acceptance is unknown;
- bounded coalescing, retries, retention, diagnostics, and dead letters;
- unresolved state and evidence never deleted by age or rollback.

The Thread Event Runner's Codex-specific exact tuple remains mandatory for a
Codex exact-session adapter:

```text
(project_id, runtime_home_id, runtime_home_realpath, native_thread_id)
```

The standalone `Endpoint`/`SessionRef` model may add identity; it MUST NOT
remove, wildcard, prefix-match, or infer any member of a host-specific exact
identity contract.

## Current v2 activation identity mapping

### Landed

Current `main` defines a non-mutating activation identity foundation in
`bin/_activation_identity.py`. An activation packet binds exactly:

```text
(project, chat, task, worktree, branch, target_agent)
```

The sender:

- requires every field to be present, non-blank, single-line, and
  serialization-stable;
- expands and strictly resolves an existing absolute worktree directory once;
- serializes the canonical path.

The receiver/classifier:

- requires `activation: true` and the complete identity;
- requires the serialized `to` value to be a string byte-exactly equal to the
  claiming target agent;
- requires identity values to remain strings after frontmatter parsing;
- checks the serialized worktree's absolute lexical canonical form;
- does not re-resolve the worktree against current filesystem, cwd, or home;
- treats any activation-shaped partial or invalid packet as `malformed`, never
  as an ordinary message.

This is current behavior and vocabulary, not authority to copy an unmerged
branch. The future standalone compatibility/import contract MUST preserve those
semantics for v2 activation packets.

The helper does not itself grant or enforce a writer lease. Current activation
lease/runtime integration remains separately owned by Amiga issues `#1571` and
`#1572`; future standalone lease authority belongs to the ledger and exact
`SessionRef` model after its proof gates. No S1 document activates either path.

## Migration, flags, and rollback posture

### Frozen decisions

Phase 0 is contract-only. Current v2 files, commands, mailbox behavior, PM2
watchers, session-autobridge state, and AX wrappers remain operational sources
of truth.

Later migration proceeds by:

1. back up current state;
2. create new state without rewriting legacy sources;
3. import with provenance;
4. run observation-only comparison;
5. enable one canonical-write or adapter surface at a time;
6. preserve compatibility projections through a documented support window;
7. retire one old owner only after reconciliation proves one new owner.

Daemon observation, canonical writes, runtime dispatch, AX v2, and remote
transport each require an independent, inert, default-off declaration in S3.
Omitted and explicit-false declarations preserve current behavior. A broader
flag never bypasses registry, identity, evidence, or phase gates.

Rollback disables mutation first, stops new claims, reconciles possible
in-flight acceptance, preserves canonical intent and unresolved evidence, and
returns pending work to a safe pull/manual posture. Rollback never marks
pending work completed and never discards an ambiguity quarantine.

## Explicit non-goals and unsupported guarantees

This architecture does not promise:

- unsolicited exact injection into an arbitrary user-opened Codex CLI/Desktop
  session;
- native thread identity or acceptance from AX, window titles, renderer
  visibility, CDP selection, or busy indicators;
- shared-client Codex App Server co-presence until the supported runtime proves
  it;
- arbitrary dynamic plugins or payload-selected shell commands;
- a hosted control plane;
- a mandatory GitHub, PM2, Supabase, worktree, or product-policy dependency;
- Phase 0 implementation of a daemon, database, adapter supervisor, transport,
  workflow pack, migration, or feature consumer.

Unsupported hosts degrade to durable pull/manual delivery. That is a valid
route, not a failure to invent authority.

## Phase 0 handoff obligations

S1 freezes this architecture and the rebaselined plan. It does not freeze exact
JSON field spelling beyond the named concepts and invariants.

S2 must express these decisions in strict JSON Schema 2020-12 contracts,
including:

- discriminated workspace-only versus exact-project scope;
- exact repository references and typed relationships;
- capability/evidence constraints;
- v2 activation identity;
- the sealed retired-evidence manifest/import authority.

S3 must freeze adapter, transport, and workflow protocols; the v2 compatibility
decision record; and inert default-off feature declarations. No Phase 0 slice
may consume those declarations from runtime code.
