# Session Lifecycle Protocol V1

## Status and authority

This document freezes Session Lifecycle Protocol V1 for Phase 3.5. The v8
storage foundation and pure read-only binding resolver have landed, but they do
not add runtime behavior, delivery authority, or production activation.

Session Lifecycle Protocol V1 is orthogonal to
[Runtime Adapter JSON-RPC Protocol V1](runtime-adapter-jsonrpc-v1.md). Runtime
Adapter JSON-RPC V1 remains closed. Lifecycle providers create, attach, inspect,
heartbeat, and retire native sessions; runtime adapter manifests authorize
post-initialize delivery after a binding has already been established.

## Identity model

The protocol operates on a conversation participant, not on an agent name, app
window, renderer thread, or raw chat value. A participant is addressed only by:

```text
(workspace_id, scope_kind, scope_identity, conversation_id, participant_id)
```

`conversation_id` reuses the existing durable `chat_id` value, but `chat_id` is
not globally unique. The same `conversation_id` in two projects or scopes names
two different participants. `conversation_id` alone MUST NOT route delivery,
resolve a binding, or select a native session.

The provider returns or verifies a `SessionRefV1`. The core derives every
`ConversationBindingV1.binding_id` and generation from storage state. Callers
MUST NOT submit a binding ID or generation as authority.

## Lifecycle provider registry

A lifecycle provider registry entry is trusted configuration for one provider
implementation and revision. It is separate from runtime adapter manifests.

The registry entry defines:

- provider ID and revision;
- supported host and trust class;
- supported operations such as reserve, attach, inspect, heartbeat, retire, and
  presentation-only UI open;
- challenge algorithm and TTL bounds;
- repository/cwd evidence requirements for project-scoped sessions;
- whether the provider may create a fresh native session, attach an existing
  one, or only inspect.

Untrusted payloads, remote messages, adapter output, window titles, cwd claims,
display labels, "latest", sidebar order, and AX state cannot select a lifecycle
provider.

## Binding lifecycle

The binding state vocabulary is closed:

| State | Meaning |
|---|---|
| `reserved` | A participant slot exists, but no native session is yet verified. |
| `registering` | A one-time challenge or attach/create proof is in progress. |
| `active` | The participant generation is bound to one exact verified `SessionRefV1`. |
| `draining` | The binding no longer accepts new mutation work; predecessor work is reconciling. |
| `unverified` | The native session may exist, but current liveness or identity cannot be proven. |
| `superseded` | A newer generation owns new work for the same participant. |
| `retired` | The binding was intentionally closed and cannot be reactivated. |
| `quarantined` | Contradictory or unsafe evidence requires explicit recovery. |

Restart, provider drift, missing heartbeat, unknown liveness, changed repository
evidence, changed native session identity, or stale registry revision MUST fail
closed as `unverified` or `quarantined`. The core MUST NOT pick a replacement
session.

## Storage-enforced invariants

Storage, not caller discipline, enforces:

1. one active mutation-capable binding for one compound participant key;
2. one mutation owner for one exact native session;
3. monotonic generation per participant;
4. derived `binding_id` and generation;
5. no wildcard, legacy-unscoped, missing, empty, or `null` scope match;
6. no resolution by `conversation_id` alone;
7. stale generations refusing rather than resolving the newer active binding.

The same-`conversation_id`-across-projects case is mandatory regression
coverage for the storage child.

## Challenge and attach flow

An attach or create flow uses a one-time bounded challenge tied to:

- workspace ID;
- scope kind and scope identity;
- conversation ID;
- participant ID;
- endpoint ID;
- provider ID and revision;
- native session identity;
- repository ID and canonical cwd evidence when project-scoped.

The provider may report native facts, but the core validates them against the
trusted registry and derives the binding. A stale challenge, duplicate
challenge, mismatched scope, foreign project, changed cwd evidence, or ambiguous
native session fails closed.

## Dispatch freeze

Before a mutation-capable dispatch, the router resolves and freezes
`(binding_id, generation)` for the participant. The pure resolver returns only a
binding reference and one of the closed non-send reasons
`waiting_for_session`, `route_ambiguous`, `session_unverified`,
`adapter_quarantined`, `pull_pending`, or `stale_generation`; it does not
fabricate `SessionRefV1` evidence from v8 storage rows. The dispatch attempt
stays bound to that exact generation. If a rebind or restart occurs before the
attempt is resolved, the attempt is not retargeted to the newer generation.

Pending work may transfer during explicit audited rebind/handoff only when the
work has not been attempted and no possible native acceptance exists.
Attempted, ambiguous, accepted, completed, or otherwise unresolved work remains
owned by the predecessor binding until reconciled.

## Rebind, handoff, and rollback

Rebind and handoff are explicit audited transitions. They record actor, reason,
predecessor binding, successor binding, transferred pending work, preserved
predecessor work, and verification evidence. They cannot erase receipts,
quarantine, or contradictory host evidence.

Rollback disables binding resolution for new mutation work first. It preserves
canonical messages, delivery attempts, receipts, binding records, and binding
audit. Existing unresolved work returns to pull/manual or stays quarantined; it
is never silently retargeted.

## Non-authorities

The following are never lifecycle or routing authority:

- `conversation_id` without workspace and scope;
- `agent_id` without participant identity;
- app/window/sidebar/display labels;
- renderer visibility or busy state;
- AX success, queued, or composer state;
- caller-supplied cwd or repository strings;
- a runtime adapter manifest without a lifecycle-provider registry entry;
- a lifecycle-provider entry without post-initialize runtime-adapter authority.

## Child acceptance matrix

Later implementation children must cover at least:

| Area | Required proof |
|---|---|
| Version selection | Next storage version mechanically matches current migration guards before writing. |
| Compound identity | Same `conversation_id` in two projects/scopes produces distinct participants. |
| Uniqueness | Two active mutation bindings for one participant are rejected. |
| Native ownership | Two mutation owners for one exact native session are rejected. |
| Generation | Stale generation lookup refuses instead of resolving the newer binding. |
| Restart | Restart or unknown liveness moves to `unverified` and blocks mutation routing. |
| Registry separation | Lifecycle-provider and runtime-adapter authorities cannot substitute for each other. |
| Rebind | Only never-attempted/no-possible-acceptance pending work transfers. |
| Rollback | New mutation routing stops without data loss or retargeting. |
