# Runtime Adapter JSON-RPC Protocol V1

## Status and schema authority

This document freezes the future standalone runtime-adapter boundary. It is a
contract only: it does not start a supervisor, register an adapter, consume a
feature declaration, or authorize a runtime action.

Schema names in this protocol refer exactly to the frozen catalog at
`schemas/standalone/v1/index.json`. In particular, session identity is
`SessionRefV1`, negotiated capability data is `CapabilitySetV1`, delivery state
is `DeliveryV1`, delivery evidence is `ReceiptV1`, and embedded proof is
`StateEvidenceV1`. Protocol wrappers must not add fields to those schema
objects.

## Normative clauses

1. **Framing (normative).** The connection MUST use JSON-RPC 2.0 over stdio,
   with exactly one UTF-8 encoded JSON object per line, terminated by one `\n`.
   JSON strings MUST escape newlines; an embedded raw newline ends the frame.
   Standard output MUST contain protocol frames only. Standard error is for
   bounded diagnostics only and MUST NOT contain protocol responses. On invalid
   UTF-8, non-object JSON, a missing terminator, an embedded raw newline, or
   non-protocol standard output, the receiver MUST return `INVALID_FRAMING`
   when a response is possible and otherwise close the connection; it MUST NOT
   reinterpret stderr or concatenate adjacent lines into a request.

2. **Size bounds (normative).** `MAX_MESSAGE_BYTES` is 1,048,576 bytes,
   including the JSON bytes but excluding the terminating `\n`.
   `MAX_IN_FLIGHT_REQUESTS` is 32 requests per adapter connection. The reader
   MUST stop buffering a frame after `MAX_MESSAGE_BYTES + 1` bytes. An oversized
   frame MUST receive `MESSAGE_TOO_LARGE` when a response is possible and be
   discarded without unbounded buffering. A request above the in-flight limit
   MUST receive `TOO_MANY_IN_FLIGHT`; it MUST NOT be queued in an unbounded host
   or adapter buffer.

3. **Time bounds (normative).** `REQUEST_DEADLINE_MS` is 30,000 milliseconds
   from receipt of a complete frame, and `HANDSHAKE_DEADLINE_MS` is 5,000
   milliseconds from process start. Every request MUST finish or enter an
   explicit reconciliation state before its deadline. Handshake expiry MUST
   return `HANDSHAKE_TIMEOUT` when possible and terminate the connection.
   Request expiry MUST return `REQUEST_TIMEOUT`; the host MUST treat possible
   external acceptance as unresolved and MUST NOT convert the timeout into a
   successful result or an automatic retry.

4. **Handshake and version negotiation (normative).** The first frame in each
   direction MUST be the `initialize` exchange. The request MUST carry the
   requested protocol version, whose V1 spelling is `1.0`, and the response
   MUST return the negotiated protocol version and the adapter's
   `CapabilitySetV1`. Before initialization succeeds, no other method is legal.
   An unknown or unsupported major version MUST return
   `UNSUPPORTED_PROTOCOL_VERSION` and terminate the connection; a non-
   `initialize` first method MUST return `INITIALIZE_REQUIRED` and terminate the
   connection. Minor versions MAY negotiate downward only within major version
   1 and only when both sides explicitly advertise the resulting minor.

5. **Trusted manifest lookup (normative).** The host MUST resolve the adapter
   executable, immutable argument vector, working directory, environment
   allowlist, adapter id, and manifest revision exclusively from a reviewed
   repository-trusted manifest keyed by the exact adapter id. The host MUST
   execute the resolved program directly and MUST NOT invoke a shell. A caller-
   supplied executable path, argv member, environment entry, working directory,
   shell string, manifest path, or adapter-id alias MUST be rejected as
   `UNTRUSTED_MANIFEST_INPUT`; the process MUST NOT be spawned from that input.

6. **Capability negotiation (normative).** The adapter MUST declare its
   capabilities in the initialization `CapabilitySetV1`, including quality,
   constraints, evidence, and revision. The host MAY invoke only a capability
   declared for the exact adapter revision and permitted by the trusted local
   profile. An undeclared, unsupported, stale-revision, or constraint-violating
   invocation MUST return `CAPABILITY_NOT_DECLARED`; the host MUST NOT fall back
   to another method, capability, endpoint, or session.

7. **Exact session binding (normative).** Every post-initialize request,
   including delivery, cancellation, reconciliation, health, and shutdown,
   MUST carry one complete `SessionRefV1`, and the host MUST validate it against
   the trusted registry and the initialized adapter revision. Wildcards,
   prefixes, display names, window order, `latest`, inferred cwd, inferred
   project, and omitted or `null` project identity are prohibited. A missing,
   mismatched, stale, inferred, or non-authoritative session reference MUST
   return `INVALID_SESSION_REF`; the adapter MUST NOT choose a replacement
   session.

8. **Delivery (normative).** The `runtime.deliver` method MUST receive exactly
   two protocol members: `session_ref`, whose value is a `SessionRefV1`, and
   `delivery`, whose value is a `DeliveryV1`. Its successful result MUST be one
   `ReceiptV1`. The embedded schema objects MUST validate without added,
   removed, renamed, or adapter-private fields, and their workspace, scope,
   message, delivery, attempt, endpoint, session, and evidence identities MUST
   agree. A malformed or cross-identity request or result MUST return
   `INVALID_DELIVERY`; it MUST NOT advance canonical delivery state.

9. **Cancellation (normative).** The `runtime.cancel` method MUST name the exact
   JSON-RPC request id being cancelled and carry the same `SessionRefV1` as the
   original request. Cancellation is idempotent: repeated cancellation of a
   request that was authoritatively cancelled before external acceptance MUST
   return the same `REQUEST_CANCELLED` terminal result. Cancellation MUST NOT
   claim success when acceptance may have occurred; that case MUST return
   `RECONCILIATION_REQUIRED`, preserve the attempt as unresolved, and prohibit
   retry until reconciliation determines authoritative not-accepted evidence.

10. **Reconciliation (normative).** After adapter restart, connection loss, or
    any possibly accepted request without a committed result, the host MUST
    invoke `runtime.reconcile` with the exact `SessionRefV1` and the outstanding
    delivery and attempt identities. The core ledger remains authoritative for
    canonical intent, delivery identity, and unresolved state; the adapter is
    authoritative only for host observations represented by valid `ReceiptV1`
    and `StateEvidenceV1` values. Missing or contradictory host evidence MUST
    return `RECONCILIATION_REQUIRED` and keep the attempt unresolved or
    quarantined; neither side may synthesize success, choose another session,
    or blindly resend.

11. **Health (normative).** The host MUST call `runtime.health` every
    `HEALTH_INTERVAL_MS`, fixed at 10,000 milliseconds, for an exact
    `SessionRefV1`. A valid response MUST identify the initialized adapter and
    manifest revisions and MUST arrive inside `REQUEST_DEADLINE_MS`. Three
    consecutive missed, invalid, or revision-mismatched health responses
    (`HEALTH_FAILURE_THRESHOLD = 3`) move the adapter out of service and return
    `ADAPTER_UNHEALTHY` to new work. The host MUST stop assigning requests until
    a new initialization and any required operator release have completed.

12. **Quarantine (normative).** The host MUST quarantine an adapter for
    unsupported version drift, trusted-manifest mismatch, capability or exact-
    session contract violation, redaction failure, repeated invalid protocol
    output, unresolved possible acceptance, or the health failure threshold.
    Quarantine MUST create an operator-visible record containing adapter id,
    manifest and adapter revisions, reason code, correlation ids, affected
    session and attempt ids, evidence references, and timestamps after
    redaction. Quarantine MUST NOT auto-clear on reconnect or process restart.
    Release requires an explicit operator action after the manifest/profile is
    reviewed, unresolved deliveries are reconciled, and a fresh handshake and
    health sequence succeed; otherwise requests receive `ADAPTER_QUARANTINED`.

13. **Structured errors (normative).** Every failure MUST use a JSON-RPC error
    whose numeric code is from the following closed enumeration. `message` is a
    stable diagnostic label, and bounded `data` MAY contain only the error name,
    retryability, request id, correlation id, and redacted evidence references.
    Free-form strings, stderr, exit status, or host-specific exceptions are not
    substitutes for these codes.

    | Code | Name | Retryable |
    |---:|---|---|
    | -32700 | `PARSE_ERROR` | no |
    | -32600 | `INVALID_REQUEST` | no |
    | -32601 | `METHOD_NOT_FOUND` | no |
    | -32602 | `INVALID_PARAMS` | no |
    | -32603 | `INTERNAL_ERROR` | no |
    | -32000 | `INVALID_FRAMING` | no |
    | -32001 | `MESSAGE_TOO_LARGE` | no |
    | -32002 | `TOO_MANY_IN_FLIGHT` | yes, only after capacity is available |
    | -32003 | `HANDSHAKE_TIMEOUT` | no |
    | -32004 | `UNSUPPORTED_PROTOCOL_VERSION` | no |
    | -32005 | `INITIALIZE_REQUIRED` | no |
    | -32006 | `UNTRUSTED_MANIFEST_INPUT` | no |
    | -32007 | `CAPABILITY_NOT_DECLARED` | no |
    | -32008 | `INVALID_SESSION_REF` | no |
    | -32009 | `INVALID_DELIVERY` | no |
    | -32010 | `REQUEST_TIMEOUT` | no; reconcile first |
    | -32011 | `REQUEST_CANCELLED` | no |
    | -32012 | `RECONCILIATION_REQUIRED` | no; reconcile rather than resend |
    | -32013 | `ADAPTER_UNHEALTHY` | yes, only after a fresh healthy initialization |
    | -32014 | `ADAPTER_QUARANTINED` | no; explicit release required |
    | -32015 | `REDACTION_FAILURE` | no |
    | -32016 | `SHUTDOWN_IN_PROGRESS` | no |

    Any numeric code outside this list is a protocol violation. The host MUST
    record it and quarantine the adapter rather than guessing retryability.

14. **Redaction (normative).** Before any log, evidence file, diagnostic,
    quarantine record, or persistent protocol trace is written, both sides MUST
    redact credentials, authorization headers, cookies, API keys, tokens,
    environment values, message-body bytes, caller-provided raw payloads,
    `configuration_ref` resolution data, local user/home paths, and native
    session identifiers except for approved stable hashes or schema identity
    references. Redaction MUST happen before persistence, not at read time. If
    required redaction cannot be proven, persistence MUST stop, the request MUST
    return `REDACTION_FAILURE`, and the adapter MUST be quarantined.

15. **Shutdown (normative).** On `runtime.shutdown`, the host MUST stop
    admitting new requests and return `SHUTDOWN_IN_PROGRESS` for later work. It
    MUST cancel work authoritatively known not to have been accepted, preserve
    and reconcile uncertain work, and drain remaining in-flight requests for
    `SHUTDOWN_DRAIN_MS = 10,000`. The adapter then MUST flush protocol output and
    exit. At `SHUTDOWN_HARD_KILL_MS = 15,000` from shutdown start, the host MUST
    terminate a still-running process. A hard kill MUST leave possibly accepted
    attempts unresolved or quarantined; it MUST NOT mark them cancelled,
    accepted, or completed without authoritative evidence.

16. **Caller-input prohibitions (normative).** A caller MUST NEVER supply or
    override an executable path, argv, working directory, environment,
    capability grant, adapter-id alias, session selection by pattern, or any
    runtime action not backed by a declared capability. The host MUST reject
    each such input as `UNTRUSTED_MANIFEST_INPUT`, `INVALID_SESSION_REF`, or
    `CAPABILITY_NOT_DECLARED` as applicable, before spawn or dispatch. It MUST
    NOT sanitize the input into authority, merge it with trusted configuration,
    invoke a shell, broaden a capability, or select a fallback runtime action.
