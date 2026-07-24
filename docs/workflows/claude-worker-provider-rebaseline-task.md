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
