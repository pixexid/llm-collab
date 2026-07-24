# Claude worker provider rebaseline task

This is a design-only refinement for issue 95. It does not authorize Claude
provider implementation, GitHub issue edits, AX operation, new dispatch
surfaces, or live session mutation.

## Current main facts

- Session lifecycle storage is at `SCHEMA_VERSION == 11`. The v8-v11 children
  added compound participant bindings, challenge/consume, delivery freeze,
  zero-transfer rebind, legacy autobridge provenance import, and read-only
  operator inspection.
- The lifecycle resolver addresses participants only by the compound tuple
  `(workspace_id, scope_kind, scope_identity, conversation_id, participant_id)`.
  It returns a binding reference plus a closed reason from
  `CONVERSATION_BINDING_RESOLUTION_REASONS`; it does not fabricate
  `SessionRefV1`.
- Dispatch heuristic retirement removed the publish/deliver first-match and
  newest-session mutation paths. Runtime receive still uses the legacy
  autobridge watcher as the receive mechanism.
- The receive-side legacy packet materialization seam exists and must remain
  ordered after exact session selection and before read-state mutation.
- `watch_inbox.py` still maintains read state at the agent inbox level through
  `mark_messages_read(agent_id, paths)`. Therefore provider work must prove
  that only binding-selected packets reach that function.

## Goal

Rebaseline issue 95 into dependency-ordered children for a Claude worker
provider over the existing lifecycle binding authority. The provider must bind
one Claude-attached native session to one participant tuple and generation
before that session can receive, acknowledge, or send mutation-capable work.

Worker identity is a projection from the participant/binding record. It must
not be stored as a separate `worker_id`, and it must not become a second routing
authority.

## Non-goals

- No automatic Claude Desktop session mutation.
- No AX, Buzz, generic composer injection, newest/frontmost/sidebar session
  selection, or fallback to a generic `claude` inbox.
- No new schema unless a later stamped child proves the current v11 authority
  cannot represent the provider requirement.
- No change to GitHub issue text or queue state.
- No read-state side effects before exact binding selection and materialized
  claim/receipt boundaries.

## Proposed child split

1. **Provider inventory and boundary audit.** Document the current Claude
   watcher, session publication, materialization, processed-message, and
   read-state paths. Prove the first code child's allowlist and identify every
   existing function that may consume or mark a packet.
2. **Claude attached-session registration design.** Define how a Claude session
   obtains a lifecycle challenge, proves one native session, and creates one
   active binding for the compound participant tuple. The provider must
   re-attest against trusted project root input on resume/restart.
3. **Watcher selection and receive claim.** Cut over the watcher to require the
   exact active binding and generation before dispatch. Nonmatching,
   unresolved, missing-binding, foreign-project, stale-generation, or
   ambiguous packets stay unread. Materialization and bound-attempt creation
   happen before read-state mutation.
4. **Outbound identity.** A Claude send path derives sender identity from the
   local active binding. Callers cannot select a raw target session, raw worker,
   newest session, or alternate participant. The sender's binding and generation
   are recorded in the canonical delivery/receipt boundary.
5. **Resume, restart, compaction, fork, replace, and retire.** Define lifecycle
   transitions for Claude sessions without retargeting attempted work. Rebind
   must preserve prior generation evidence and never silently transfer frozen
   attempts.
6. **Disposable-session conformance.** Add bounded live/read-only or fake-provider
   conformance proving a disposable Claude-attached session cannot receive or
   send outside its active binding.

The recommended first child is child 1 only. It is read-only/design work and
exists to pin the implementation seam before provider authority is introduced.

## Child 1 receive/read-state inventory

Child 1 is read-only. It freezes the real files the first code child must
inspect before changing Claude receive behavior.

### Watcher entry points

| File:line | Function | Receive/read-state role |
| --- | --- | --- |
| `bin/watch_inbox.py:79` | `autobridge_session_ids(agent_id)` | Enumerates every legacy session with the same `agent_id`. This is still agent-scoped, so exact binding filters must happen after enumeration and before consumption. |
| `bin/watch_inbox.py:95` | `dispatch_autobridge(agent_id, json_output)` | Dispatches every enumerated session, collects successful runtime-trigger paths, and is the only background watcher caller of `mark_messages_read`. |
| `bin/watch_inbox.py:145` | `mark_messages_read(agent_id, sorted(set(consumed_paths)))` | Moves agent-inbox read state after any session reports a successful runtime trigger. This is the residual issue 141 hazard: read state is keyed by agent, not by binding. |
| `bin/watch_inbox.py:150` | `main()` | Poll loop that loads the agent inbox, computes new unread paths, optionally notifies, and calls `dispatch_autobridge` when unread paths exist. |

### Agent inbox helpers

| File:line | Function | Receive/read-state role |
| --- | --- | --- |
| `bin/_helpers.py:489` | `load_agent_inbox(agent_id)` | Reads one shared inbox document for the logical agent. It has no project, worker, binding, or generation partition. |
| `bin/_helpers.py:503` | `add_to_inbox(agent_id, message_path)` | Adds durable packet paths to the recipient's shared unread list. This is enqueue authority, not receive acceptance. |
| `bin/_helpers.py:512` | `mark_messages_read(agent_id, paths)` | Removes paths from that shared unread list and appends them to shared read state. A code child must prove only binding-selected, materialized, claimed paths reach this helper. |
| `bin/_helpers.py:522` | `get_unread_messages(agent_id)` | Expands shared unread paths into parsed packet frontmatter/body for matching. Filtering after this point cannot make the underlying inbox partitioned. |

### Session autobridge selection and local processed state

| File:line | Function | Receive/read-state role |
| --- | --- | --- |
| `bin/_session_autobridge.py:93` | `load_session(session_id)` | Loads one legacy parked session record. |
| `bin/_session_autobridge.py:100` | `iter_sessions(agent_id=None)` | Lists legacy session records; with `agent_id`, it remains logical-agent scoped. |
| `bin/_session_autobridge.py:217` | `save_session(payload)` | Persists the session-local processed-message list and runtime metadata. |
| `bin/_session_autobridge.py:265` | `append_event(session_id, event)` | Persists dispatch, skip, refusal, and mutation-boundary events. Event append is evidence only; it is not receive acceptance. |
| `bin/_session_autobridge.py:539` | `binding_scoped_message_matches_session(session, message)` | Applies repo-target, target binding-id, and target-generation checks when exact receive targeting is required. |
| `bin/_session_autobridge.py:641` | `message_targets_session(session, message)` | Routes explicit target-session packets, refuses missing targets for exact-receive sessions, and otherwise still returns `broadcast_or_agent_scoped`. |
| `bin/_session_autobridge.py:658` | `matching_unread_messages(session)` | Reads the shared agent inbox and filters by project, chat, and `message_targets_session`. |
| `bin/_session_autobridge.py:674` | `processed_messages(session)` | Reads the legacy session-local processed-path set. This is duplicate suppression only, not canonical acceptance. |
| `bin/_session_autobridge.py:690` | `processed_message_blocks_dispatch(session, message, seen)` | Allows binding-scoped packets to re-enter materialization while blocking already-processed noncanonical packets. |
| `bin/_session_autobridge.py:696` | `mark_message_processed(session, message_path)` | Moves session-local processed state through `save_session`. This does not move the shared agent inbox read state. |

### Materialization, trigger, and consume ordering

| File:line | Function | Receive/read-state role |
| --- | --- | --- |
| `bin/_session_autobridge.py:704` | `materialize_selected_runtime_packet(session, message)` | Opens the canonical ledger and calls legacy packet materialization for the selected packet. It returns a closed resolver reason on refusal. |
| `bin/_session_autobridge.py:1316` | `execute_runtime_trigger(session, message)` | Runs the selected runtime trigger after materialization and operator-turn summary boundaries. It is transport execution evidence, not acceptance. |
| `bin/_session_autobridge.py:1914` | `dispatch_session(session_id)` | Main receive pipeline: session dispatchability, matching, activation claim, loop protection, materialization, runtime trigger, UI refresh, and session-local processed marking. |
| `bin/_session_autobridge.py:2000` | `if action == "runtime_trigger"` | Runtime-trigger branch. When materialization is needed, it happens before operator summary and trigger execution. |
| `bin/_session_autobridge.py:2002` | `message_needs_canonical_materialization(session, message)` | Decides whether binding-targeted runtime packets must pass canonical materialization before trigger. |
| `bin/_session_autobridge.py:2003` | `activation_fenced_mutation(... boundary="canonical_receive_materialization" ...)` | Mutation boundary for materialization. Refusal appends an event and continues without runtime trigger. |
| `bin/_session_autobridge.py:2112` | `mark_after_event` block | Session-local processed marking happens after the dispatch event; activation messages use a fenced mutation. |

### Heuristic discovery and dispatch-adjacent seams

| File:line | Function | Receive/read-state role |
| --- | --- | --- |
| `bin/_session_autobridge.py:385` | `discover_codex_runtime_session()` | Read-only/newest-style diagnostic discovery. It must not become receive/send authority. |
| `bin/_session_autobridge.py:416` | `discover_claude_runtime_session(project_path=None)` | Read-only/newest-style Claude discovery by project session index and file mtime. It must not bind or route mutation-capable receive. |
| `bin/_session_autobridge.py:452` | `discover_gemini_runtime_session(project_path=None)` | Read-only/newest-style diagnostic discovery. It must not bind or route mutation-capable receive. |
| `bin/_session_autobridge.py:475` | `discover_runtime_session(runtime_family, project_path=None)` | Dispatcher for the read-only discovery helpers. |
| `bin/_session_autobridge.py:567` | `find_dispatchable_target_session(...)` | Legacy first-match helper retained for diagnostics/tests. A future Claude mutation path must not call it. |
| `bin/_session_autobridge.py:594` | `resolve_exact_dispatch_target(project_id, chat_id, agent_id)` | Exact binding-file resolver used by deliver/autobridge readiness. It is not the v11 lifecycle resolver, but it is the current nonheuristic dispatch-adjacent seam. |
| `bin/inbox.py:137` | `publish_runtime_session_if_requested(args)` | Refuses heuristic runtime discovery families for session publishing and points users to read-only diagnostics or exact registration. |
| `bin/inbox.py:386` | `mark_all_read(args)` | Manual command path that can move shared agent-inbox read state. It is not watcher acceptance and must stay outside automatic Claude receive. |
| `bin/inbox.py:544` | `if consume: mark_messages_read(args.me, shown_paths)` | Manual consume path that marks displayed paths read. It must not be confused with runtime receive acceptance. |
| `bin/deliver.py:398` | `resolve_exact_dispatch_target(...)` | Sender-side check for exact autobridge target before delivery readiness. |
| `bin/deliver.py:505` | `add_to_inbox(args.recipient, to_path)` | Sender-side enqueue into the shared recipient inbox. It persists intent only. |
| `bin/project_design_queue.py:722` | `activation_packet_state(context)` | Read-only status reporting over the unpartitioned `claude` inbox. It reads `unread`/`read` state but does not consume or mark paths. |
| `bin/project_design_queue.py:734` | `unread_messages_from(agent, sender=..., project_id=...)` | Read-only design-queue status helper over an agent inbox. It filters unread paths for display only and does not move read state. |

### Invariant for the first code child

For Claude receive, a packet may reach `bin/_helpers.py:512`
`mark_messages_read(agent_id, paths)` only after all of these are true for that
exact path:

1. the watcher selected one exact active participant binding and generation;
2. the packet matched that binding and was not a broadcast, generic agent, stale
   generation, foreign project, missing-binding, or ambiguous route;
3. legacy packet materialization resolved through canonical rows;
4. the receive claim/bound attempt was created or idempotently returned;
5. the runtime write completed; and
6. no unresolved refusal reason from `CONVERSATION_BINDING_RESOLUTION_REASONS`
   was produced.

If any condition is false, the packet remains unread in the shared agent inbox.
Session-local processed state may suppress legacy duplicate handling, but it
must not be treated as canonical acceptance or shared read-state authority.

## Child 2 Claude attached-session registration design

Child 2 is design-only. It freezes the registration contract for a Claude
attached session, but it does not implement a provider, mutate a Claude session,
open a composer, or create a live channel.

### Registration object model

Claude registration composes existing lifecycle authority. It must not add a
stored worker table or a second routing key:

```text
worker projection
  = (workspace_id, scope_kind, scope_identity, conversation_id, participant_id)
    + active binding_id/generation
    + provider_id/endpoint_id/native_session_id/runtime_instance_id
```

The Claude provider uses `llm_collab.session_lifecycle` only as the authority
surface:

- `LifecycleSubject` names the compound participant tuple plus
  `agent_id`, `endpoint_id`, `native_session_id`, and `runtime_instance_id`;
- `SessionLifecycleCore.reserve(...)` creates a bounded one-time challenge;
- `SessionLifecycleCore.consume(...)` validates the challenge, re-attests the
  native session, and lets storage derive `binding_id` and generation;
- `heartbeat(...)`, `mark_restart_unverified(...)`, `retire(...)`,
  `rebind(...)`, and `inspect(...)` are later lifecycle operations, not initial
  receive authority.

The provider registry remains distinct from runtime-adapter manifests. A
lifecycle provider may prove that one Claude native session is bound to one
participant. It does not authorize post-initialize runtime adapter methods and
does not make AX/UI state authoritative.

### Proposed registration flow

1. The daemon creates a registration challenge for the exact participant tuple
   and intended Claude endpoint. The challenge is TTL-bounded and one-time.
2. The Claude-attached side obtains the challenge through an explicit provider
   hook or session-local channel. The hook/channel must provide exact native
   session evidence and must not rely on newest, frontmost, sidebar, window
   title, mtime, or generic `claude` inbox state.
3. The daemon validates the attestation against trusted project/root input on
   every reserve/consume/restart path. It validates before challenge consumption
   or binding mutation.
4. Storage consumes the challenge and creates the binding atomically. A forced
   mid-operation failure leaves the challenge pending and creates no binding.
5. Storage derives `binding_id` and generation. Caller-provided binding or
   generation values are ignored or refused; they never become authority.
6. The resulting binding starts as the only route authority for later watcher
   receive and outbound identity children. Child 2 itself does not dispatch or
   mark any packet read.

### Required proof shape for the first registration code child

| Guard | Expected proof |
| --- | --- |
| Exact participant tuple | Same `conversation_id` in two projects/scopes creates distinct bindings; `conversation_id` alone cannot resolve. |
| Native-session owner tuple | The same Claude native session cannot be active for two mutation-capable participants; two distinct native sessions can bind independently. |
| Challenge tuple binding | Wrong workspace, scope, conversation, participant, endpoint, native session, runtime instance, provider, challenge id, or token fails closed and preserves pending state. |
| TTL and replay | Expired, replayed, or duplicate challenge consumption is refused without creating or retargeting a binding. |
| Atomic consume+bind | Forced failure between consume and bind leaves no consumed challenge and no binding. |
| Trusted-root revalidation | Project/root/cwd evidence is checked against trusted registry input on reserve, consume, heartbeat, and restart re-attestation; no cached repo/cwd copy becomes authority. |
| Derived identity | Caller-provided `binding_id` or generation cannot change storage-derived values. |
| No heuristic registration | `discover_claude_runtime_session(...)`, mtime/newest discovery, frontmost UI, AX, and generic inbox state cannot register or bind a mutation-capable worker. |
| Inertness | Child 2 code does not call watcher dispatch, runtime trigger, `mark_messages_read`, `add_to_inbox`, AX, Computer Use, subprocess, socket, or wall-clock-derived token sources. |

Each proof must isolate one guard. A broad happy-path fixture is insufficient.

### Child 2 allowlist proposal

The expected implementation allowlist is:

- `llm_collab/session_lifecycle.py`, only if a Claude-specific provider can be
  modeled through the existing lifecycle core without adding a new authority;
- `llm_collab/ledger/store.py`, only if existing v11 challenge/binding methods
  need a narrowly-scoped helper for the Claude provider;
- focused lifecycle/store tests;
- this design doc and `docs/protocols/session-lifecycle-v1.md` for contract
  wording.

If registration needs a new schema version, a new provider transport module, a
daemon/CLI surface, or any live Claude hook/channel, stop and restamp before
implementation.

## First code child acceptance table

| Guard | Expected proof |
| --- | --- |
| Same agent, two projects | A packet for project B is not consumed by the project A binding and remains unread. |
| Same project, two workers | A packet for worker B is not consumed by worker A and remains unread. |
| Stale generation | The resolver returns `stale_generation`; dispatch and read-state mutation do not run. |
| Missing binding | The packet stays unread and no legacy fallback consumes it. |
| Mark-read is not acceptance | `mark_messages_read` is called only for paths that passed exact binding selection, materialization, and claim. |
| Duplicate watcher event | Replaying the same filesystem/inbox event produces the same canonical rows and does not duplicate an attempt. |
| Forged sender identity | Sender fields not derived from the active binding are refused before outbound delivery. |
| Heuristic routes | First-match, newest, frontmost, sidebar, generic agent inbox, and broadcast fallbacks cannot feed a mutation-capable Claude receive/send path. |

Each guard needs one independent mutation or structural test. Broad fixtures that
mask multiple guards are not enough.

## Stop conditions

Stop and return a design packet for review if any child requires one of these:

- a schema migration beyond v11;
- a second claim, receipt, worker, or routing authority;
- a durable packet-to-canonical link that cannot be represented by current
  canonical message, delivery, attempt, receipt, and lifecycle rows;
- a Claude hook/channel that cannot prove exact native session identity;
- production AX, Computer Use, UI composer, or live-session mutation;
- a path where read-state would move before binding selection and claim.

## Verification for this design slice

- `git diff --check`
- Source grep for the named seams in `bin/_session_autobridge.py`,
  `bin/watch_inbox.py`, `bin/_helpers.py`, `llm_collab/session_lifecycle.py`,
  and `docs/schema-reference.md`
- Release-gate verification remains responsible for the final merge decision.
  The first code child must run focused watcher/lifecycle/canonical tests plus
  the configured-root suite before merge.
