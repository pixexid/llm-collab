# Standalone V1 Compatibility and Source Authority

## Status

This is a Phase 0 decision record. Current v2 files, commands, processes, and
local state remain authoritative. The standalone S2 schemas are data contracts,
not active owners, and S3 adds no importer, projection writer, declaration
reader, process, database, adapter, or mutation path.

## Current-to-standalone matrix

| Surface | Current authority (file/process/path on main) | Standalone v1 authority (S2 schema or "none yet") | Import/projection behavior | Activation gate (declaration + flag) | Rollback behavior | Open gap |
|---|---|---|---|---|---|---|
| `collab.config.json` workspace configuration | `collab.config.json` is the current v2 authority for the workspace name, repository and project-state roots, branch naming, watcher polling, and notification settings; `bin/_helpers.py` supplies the current `bin/` readers, `pm2/ecosystem.config.cjs` reads watcher settings directly, and `scripts/init.py` creates the file. | `WorkspaceV1` supplies future workspace and repository identity vocabulary only; none yet for host-local paths, branch, watcher, or notification settings, and no S2 schema is an active configuration owner. | Not imported or projected in Phase 0. Future work must keep host-local settings distinct from portable identity and transfer configuration ownership only through an explicit one-owner migration. | Any future activation requires both every relevant declaration plus its current authority and a separately reviewed one-owner configuration cutover; no S3 declaration alone can activate import, reads, writes, or ownership. | Disable the future configuration owner and return all reads to `collab.config.json` and its current readers only after duplicate writes are impossible; preserve imported records and source provenance. | A future cutover must define the portable-versus-host-local settings boundary and single owner; S3 does not resolve it. |
| `agents.json` v2 configuration | `agents.json` is the collaborator identity and activation configuration read by current `bin/` routing and `pm2/ecosystem.config.cjs`. | `AgentV1`, `EndpointV1`, and `CapabilitySetV1` model separated future identities and capabilities; none is an active registry owner. | Not imported in Phase 0. A future importer must preserve logical-agent identity, split endpoint configuration explicitly, and never infer shared endpoint authority. | No S3 declaration or current Thread Event Runner flag authorizes configuration import or ownership transfer. | Remove or disable the future importer and continue reading `agents.json`; do not discard imported provenance. | no gap for S3; a future migration still needs an explicit one-owner cutover contract. |
| `projects.json` registered-project contracts | `projects.json` is the current v2 registry authority for exact project IDs, repository maps and default branches, GitHub/backlog/release settings, preflight commands, and project-local UI/UX, database, tool, and bridge settings; `bin/_helpers.py` and current project-aware `bin/` commands read its exact entries. | `WorkspaceV1` supplies frozen future workspace, project, and repository identity vocabulary, but no S2 schema is an active registry owner; project-specific policy remains registry or future workflow-pack territory. | Not imported or projected in Phase 0. Future standalone identities may use the frozen `WorkspaceV1` vocabulary, but project-specific policy must remain registry/workflow-pack data and cannot become a workspace default or cross-project fallback in the shared runtime. | Registry activation requires a separately reviewed one-owner cutover and an exact, non-null registered-project match. Neither `canonical_writes` nor `runtime_dispatch` can grant registry authority, and no missing, empty, or `null` project fallback is permitted. | Disable future registry writes, reconcile one owner, and return reads to `projects.json` while preserving standalone records, source mappings, and provenance. | G1 applies: `projects.json` has no sibling-document relationship and none is inferred; see requirement G1 below. |
| `Chats/` | Paired Markdown message files under `Chats/` plus each chat `meta.json`; current `deliver.py`, `new_chat.py`, and inbox readers own writes and reads. | `MessageV1` is the future canonical intent contract; `DeliveryV1`, `ReceiptV1`, and `StateEvidenceV1` model route state and proof. | Phase 0 does not import. A future Phase 2 importer may create idempotent canonical records and compatibility projections while preserving original files and exact project links. | Future canonical ownership requires `canonical_writes` to be true and an additional reviewed current-authority flag or cutover gate; no such current flag exists, so S3 cannot activate it. | Disable canonical writes first and return commands to direct `Chats/` files after reconciliation; preserve canonical intent, attempts, receipts, and ambiguity evidence. | no gap for S3; import and projection mechanics remain Phase 2 work. |
| Agent inboxes (task label `State/inbox`; implemented path `agents/{id}/inbox.json`) | `agents/{id}/inbox.json` is the actual current per-agent unread and read pointer index; there is no current `State/inbox` authority. | `MessageV1` identifies canonical intent; none yet for the inbox compatibility projection itself. | Not imported in Phase 0. A future projection may recreate exact-project pointer views from canonical state, but the current file remains authoritative until cutover. | Future projection writes require `canonical_writes` plus a separately reviewed current-authority cutover gate; neither an omitted declaration nor declaration `false` changes current reads or mutations. | Disable projection writes, reconcile pointer state, and resume the current inbox index without losing canonical messages or unresolved delivery state. | no gap: this record makes the current implemented path explicit and grants no authority to the `State/inbox` label. |
| `Tasks/` | Markdown task mirrors under `Tasks/`, mutated by current task and contract commands, are current workflow authority. | none yet; task lifecycle belongs to a future workflow pack and is not one of the ten S2 core schemas. | Not imported or projected in Phase 0. A future workflow pack may map task links to canonical messages without making tasks canonical intent. | No declaration directly activates workflow packs. `canonical_writes` cannot enable task mutation by itself, and no current flag counterpart exists. | Disable the future pack and continue current task files after one-owner reconciliation; retain canonical message and evidence links. | G2: retired-form evidence age and provenance is unresolved; see requirement G2 below. |
| Queues | GitHub backlog eligibility plus `{project_state_root}/{project_id}/issue-queue.json` are current ordered-work authority; queue commands require exact project matching. | none yet; queue policy belongs to a future workflow pack, while `WorkspaceV1` supplies only trusted project and repository registry identity. | Not imported or projected in Phase 0. Future extraction must preserve order, dependencies, exact project scope, and one writer without creating a second queue authority. | No S3 declaration directly activates queue ownership. No flag may turn `canonical_writes` or `runtime_dispatch` into queue authority. | Disable the future queue pack and restore the prior queue owner only after duplicate activation is impossible; preserve audit and unresolved work. | G1: sibling-document pairing has no typed current project-schema authority; see requirement G1 below. |
| Session autobridge (`State/session_autobridge`) | `State/session_autobridge/` records plus `bin/_session_autobridge.py`, `bin/session_autobridge.py`, `deliver.py`, `inbox.py`, and `watch_inbox.py` are the current experimental authority. | `SessionRefV1` is the future exact-session identity and `StateEvidenceV1`, `DeliveryV1`, and `ReceiptV1` model proof; legacy records are provenance only. | Not imported in Phase 0. A future importer must treat missing or optional project and chat filters as provenance, never as workspace scope or exact-project `SessionRefV1` authority. | Future exact dispatch requires `runtime_dispatch` AND `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD` for one exact subscription. `THREAD_EVENT_RUNNER_TEST_DISPATCH_DISPOSABLE_RUNTIME` is deliberately not a declaration counterpart. | Disable new dispatch, reconcile possible acceptance, preserve quarantine, and return pending work to current pull/manual or settled legacy ownership. | Legacy optional scope filters remain a later import and cutover gap; S3 does not resolve or widen them. |
| PM2 (`pm2/ecosystem.config.cjs`) | `pm2/ecosystem.config.cjs`, `agents.json` watcher settings, PM2 saved process state, and `bin/watch_inbox.py` own current watcher materialization. | none yet for a process manager; future observation records may use `EventEnvelopeV1` and `StateEvidenceV1`. | Not imported, started, stopped, or reconciled in Phase 0. Existing or PM2-saved processes remain independent current state. | Future standalone observation requires `daemon_observation` AND both `THREAD_EVENT_RUNNER_ENABLED` and `THREAD_EVENT_RUNNER_OBSERVE`. S3 sets no flag and starts no process. | Stop the future standalone observer and leave its ledger read-only; current PM2 state changes only through its existing operator workflow. | no gap for S3; PM2 retirement or coexistence is a later migration decision. |
| AX (`tools/axbridge/`) | `tools/axbridge/` Swift sources and tests plus `bin/axsend-ensure` are the current AX doorbell implementation and evidence vocabulary. | `EndpointV1`, `CapabilitySetV1`, `StateEvidenceV1`, and `ReceiptV1` constrain a future `ui_attached` adapter; none is active. | Not imported, profiled, invoked, or changed in Phase 0. Future AX v2 profiles must be reviewed trusted registry data and AX evidence remains non-native unless exact acknowledgment proves more. | Future AX v2 requires `ax_v2` plus a separately reviewed AX activation gate; no current Thread Event Runner flag is its counterpart and S3 enables nothing. | Quarantine or disable the future AX v2 profile and return work to the current AX or pull/manual route without re-ringing ambiguous attempts. | no gap for S3; profile capture and conformance remain Phase 6 work. |
| Current `bin/` commands | Current scripts under `bin/` directly own v2 chat, inbox, task, queue, worktree, watcher, and delivery behavior according to their existing project-scoped contracts. | `MessageV1`, `DeliveryV1`, `ReceiptV1`, `SessionRefV1`, and `StateEvidenceV1` are future core contracts; none replaces a command in Phase 0. | No command reads the declaration or standalone protocols in S3. Future compatibility wrappers must preserve exact project scope and current command results until explicit cutover. | Current behavior is unchanged. Future canonical writes require `canonical_writes` plus a separate cutover gate; future exact delivery also requires `runtime_dispatch` AND `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD`. | Disable standalone mutation first, reconcile in-flight work, and return commands to direct v2 authority while preserving canonical intent and unresolved evidence. | no gap for S3; per-command import and projection semantics remain Phase 2 and workflow-pack work. |

## Required open gaps

1. **G1 — sibling-document pairing.** The `Queues` row `Open gap` cell points
   here. Current main has no explicit project-schema relationship that pairs
   sibling documents. Any future pairing rule requires a real typed schema or
   trusted registry authority with exact registered repository endpoints. It
   MUST NOT invent, imply, or hard-code a pairing convention from paths, names,
   task prose, issue prose, or repository proximity. Recording this gap does not
   create the future authority.

2. **G2 — retired-form evidence age.** The `Tasks/` row `Open gap` cell points
   here. `created_utc` alone is insufficient because an old open task can mint
   new evidence. Future retired-form compatibility MUST bind objective content
   age and provenance using the S2 sealed-manifest vocabulary: canonical
   locator, content hash, evidence form version, cutoff policy revision, and an
   immutable source boundary with trusted import and publication provenance.
   Current path age, task age, filesystem mtime, commit prose, and self-reported
   production time are not authority. Recording this gap is in scope; resolving
   it is not.

## Inert declaration semantics

The sole declaration is
`docs/protocols/standalone-v1-feature-declarations.json`, identified by
`https://llm-collab.dev/declarations/standalone/v1/feature-declarations.json`.
Its five independent JSON booleans are `daemon_observation`,
`canonical_writes`, `runtime_dispatch`, `ax_v2`, and `remote_transport`, and all
are committed `false`. Omission and explicit `false` are semantically
identical. Strings, integers, `null`, and other truthy-looking values are
invalid. The file grants nothing on its own, and S3 adds no reader.

Any future consumer must apply this narrowing rule:

```text
effective(capability) = current_env_authority(capability) AND declaration.features[capability]
```

The current flag mapping and precedence are frozen as follows:

- `THREAD_EVENT_RUNNER_ENABLED` and `THREAD_EVENT_RUNNER_OBSERVE` remain the
  flag pair authoritative for observation; `daemon_observation` is an
  additional necessary gate and can never enable observation alone.
- `THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD` remains authoritative and exact-
  subscription-scoped for production/project dispatch; `runtime_dispatch` is
  an additional necessary gate and cannot broaden it to workspace-wide scope.
- `THREAD_EVENT_RUNNER_TEST_DISPATCH_DISPOSABLE_RUNTIME` has no declaration
  counterpart deliberately. It is a disposable test-only path and MUST NOT be
  satisfiable by a committed declaration.
- `canonical_writes`, `ax_v2`, and `remote_transport` have no current flag
  counterpart. No existing flag implies them.

S3 does not replace, rename, deprecate, or remove any of the four current
Thread Event Runner flags.
