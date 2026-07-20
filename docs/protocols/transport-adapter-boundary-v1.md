# Transport Adapter Boundary V1

## Status

This is a contract-only boundary for future transport adapters. It creates no
transport process, network listener, route, declaration consumer, or local
runtime authority.

## Normative clauses

1. **Allowed carriage.** A transport MAY carry a canonical event envelope that
   conforms exactly to S2 `EventEnvelopeV1`, a canonical message that conforms
   exactly to S2 `MessageV1`, and transport-level evidence that conforms exactly
   to S2 `StateEvidenceV1`. Those schema objects remain separate authorities;
   transport metadata MUST NOT add fields to them or convert transport receipt
   into runtime acceptance. A non-conforming object MUST be rejected before
   canonical import or local routing, with a redacted transport-policy failure
   record; it MUST NOT be partially imported.

2. **Local runtime command is unselectable.** A transport payload MUST NOT name,
   embed, alias, or derive a local runtime command. This defers to the S1 rule
   that untrusted data cannot select executable code and to the bounded-data
   boundary of S2 `EventEnvelopeV1`. If attempted, the receiver MUST reject the
   entire payload as a transport-policy violation before command lookup or
   execution and record the offending field name after redaction.

3. **Adapter is unselectable.** A transport payload MUST NOT select an adapter,
   adapter implementation, adapter id, adapter version, or adapter revision.
   This defers to the S1 trusted-registry rule and the S2 `EndpointV1` trusted
   adapter binding. If attempted, the receiver MUST reject the entire payload
   before registry resolution and MUST NOT substitute a default or first
   adapter.

4. **Handler is unselectable.** A transport payload MUST NOT select a local
   handler, workflow handler, module, plugin, or function. This defers to the S1
   prohibition on payload-selected handlers and the S2 `EventEnvelopeV1`
   authority-key exclusion. If attempted, the receiver MUST reject the entire
   payload before handler dispatch and MUST NOT normalize, alias, or silently
   ignore the selection while processing the remainder.

5. **Capability profile is unselectable.** A transport payload MUST NOT select,
   grant, widen, or revise a capability profile or capability set. This defers
   to S1 capability quality and trusted-registry rules and S2
   `CapabilitySetV1`. If attempted, the receiver MUST reject the entire payload
   before capability evaluation; it MUST NOT intersect the untrusted request
   with a local profile and continue.

6. **Filesystem path is unselectable.** A transport payload MUST NOT select a
   local file, directory, repository path, working directory, runtime home, or
   filesystem root. This defers to the S1 exact registered repository rule and
   S2 `WorkspaceV1` registry references. If attempted, the receiver MUST reject
   the entire payload before path resolution or filesystem access and MUST NOT
   treat path similarity, cwd, or display text as registry identity.

7. **Endpoint is unselectable.** A transport payload MUST NOT select, alias, or
   infer a local endpoint. This defers to the S1 distinction between `Agent`,
   `Endpoint`, and `SessionRef` and to S2 `EndpointV1`. If attempted, the
   receiver MUST reject the entire payload before endpoint lookup and MUST NOT
   route to the first, latest, default, or sole endpoint.

8. **Session is unselectable.** A transport payload MUST NOT select, pattern-
   match, prefix-match, alias, or infer a local session. This defers to S1 exact
   session binding and S2 `SessionRefV1`. If attempted, the receiver MUST reject
   the entire payload before session lookup and MUST NOT fall back to `latest`,
   a visible window, a cwd match, or another session.

9. **Identity claims are untrusted.** Workspace, project, repository, agent,
   contact, endpoint, and session identity asserted by a transport are
   untrusted input. Before import, the receiver MUST re-validate every claim
   against the exact S1 identity and trusted-registry rules and the relevant S2
   `WorkspaceV1`, `AgentV1`, `EndpointV1`, `SessionRefV1`, and `MessageV1`
   fields. A missing, `null`, unknown, stale, cross-workspace, cross-project, or
   contradictory identity MUST reject the entire payload; it MUST NOT become a
   wildcard, compatibility default, or new registry authority.

10. **Transport evidence remains transport evidence.** A transport-generated
    `StateEvidenceV1` MAY prove carriage, ingress, egress, replay rejection, or
    remote contact receipt only to the quality its trusted transport profile
    supports. It MUST NOT prove local injection, acceptance, processing, or
    completion. An evidence claim that exceeds that authority MUST be rejected
    and recorded as a policy violation; local `DeliveryV1` or `ReceiptV1` state
    MUST NOT advance from it.
