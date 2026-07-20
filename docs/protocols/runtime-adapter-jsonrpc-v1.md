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

## Closed V1 wire objects

The V1 wire surface contains exactly six methods: `initialize`,
`runtime.deliver`, `runtime.cancel`, `runtime.reconcile`, `runtime.health`, and
`runtime.shutdown`. A V1 request object has exactly the four members below and
no others:

```json
{
  "jsonrpc": "2.0",
  "id": "request-id",
  "method": "runtime.health",
  "params": {}
}
```

`jsonrpc` MUST be the string constant `"2.0"`. `id` MUST satisfy the
`RequestId` rule in Clause 1. `method` MUST be one of the six exact method
names above. `params` MUST be the method-specific closed object defined below;
positional params are prohibited. A success response has exactly `jsonrpc`,
the request's JSON-type-and-value-exact `id`, and `result`. An error response
has exactly `jsonrpc`, the same exact `id`, and `error`. A response MUST contain
exactly one of `result` and `error`, never both and never neither.

The `error` object has exactly `code`, `message`, and `data`. `code` MUST be one
numeric value from Clause 13; `message` MUST be that row's exact symbolic name.
`data` is a closed object requiring `name`, `retryable`, and `request_id`, where
`name` and `retryable` match the same Clause 13 row and `request_id` equals the
response `id`. `data` MAY additionally contain `correlation_id`, using the S2
token scalar, and `evidence_refs`, an array of at most 32 redacted
`StateEvidenceV1.evidence_id` strings. No other error or error-data member is
legal. A V1 response is emitted only when a valid `RequestId` is recoverable;
otherwise the receiver follows Clause 1's close behavior and quarantines only
where Clause 1 or Clause 12 requires it, rather than fabricating a `null`
correlation.

Every object described in this section is closed. Missing, additional, or
mistyped request- or response-envelope members are `INVALID_REQUEST`. Missing,
additional, or mistyped `params` members are `INVALID_PARAMS`. The receiver
MUST validate the complete request before performing an action and the complete
response before advancing canonical state. A malformed request performs no
action. A malformed result is a protocol violation, advances no canonical
state, preserves possibly accepted work as unresolved, and triggers the
method-specific failure and quarantine rules below. Unknown methods return
`METHOD_NOT_FOUND` without action. No implementation may repair an invalid
object by dropping members, applying defaults, coercing types, inferring
identity, or selecting a fallback.

The method-specific shapes are:

- `initialize` params contain exactly `requested_protocol_version`,
  `adapter_id`, `adapter_revision`, `manifest_id`, `manifest_revision`, and
  `endpoint`. `requested_protocol_version` is a string; the only successfully
  negotiable V1 value is `"1.0"`. The four adapter/manifest values use the S2
  token scalar. `endpoint` is one complete `EndpointV1` copied from the exact
  trusted registry record by the host, not supplied or overridden by an
  untrusted caller. Its success result contains exactly
  `negotiated_protocol_version`, `adapter_id`, `adapter_revision`,
  `manifest_id`, `manifest_revision`, `endpoint`, and `capability_set`.
  `negotiated_protocol_version` is the string constant `"1.0"`; the four
  identity/revision scalars and the complete `EndpointV1` MUST exactly equal
  the request; `capability_set` is one complete `CapabilitySetV1` satisfying
  Clauses 4 and 6.
- `runtime.deliver` params contain exactly `session_ref` and `delivery`, whose
  values are one complete `SessionRefV1` and one complete `DeliveryV1`. Its
  success result is directly one complete `ReceiptV1`, with no result wrapper.
- `runtime.cancel` params contain exactly `session_ref` and
  `original_request_id`, whose values are one complete `SessionRefV1` and the
  original delivery request's exact `RequestId`. Its success result contains
  exactly `original_request_id`, equal in JSON type and value to the params
  member, and `status`, whose string constant is `"cancelled"`.
- `runtime.reconcile` params contain exactly `session_ref`,
  `original_request_id`, `delivery_id`, and `attempt_id`. `session_ref` is one
  complete `SessionRefV1`; `original_request_id` is the unresolved request's
  exact `RequestId`; and the last two values use the exact S2
  `DeliveryV1.delivery_id` and `DeliveryV1.attempt_id` scalar definitions. Its
  success result is directly one complete identity-matching `ReceiptV1`, with
  no result wrapper.
- `runtime.health` params are exactly the empty object `{}`. Its result is a
  closed scalar object requiring `status`, `negotiated_protocol_version`,
  `adapter_id`, `adapter_revision`, `manifest_id`, `manifest_revision`,
  `endpoint_id`, `workspace_id`, `scope_kind`, `capability_set_id`, and
  `capability_set_revision`. `status` is the string constant `"healthy"`;
  `negotiated_protocol_version` is `"1.0"`; `scope_kind` is exactly
  `"workspace"` or `"project"`; and every identity/revision value uses the
  corresponding S2 scalar and exactly echoes the initialized binding. A
  project-scoped result additionally requires exactly one `project_id` using
  the S2 token scalar; a workspace-scoped result forbids `project_id`.
- `runtime.shutdown` params are exactly the empty object `{}`. Its success
  result is exactly `{"status":"shutdown_started"}`.

## Normative clauses

1. **Framing (normative).** The connection MUST use JSON-RPC 2.0 over stdio,
   with exactly one UTF-8 encoded JSON object per line, terminated by one `\n`.
   JSON strings MUST escape newlines; an embedded raw newline ends the frame.
   Standard output MUST contain protocol frames only. Standard error is for
   diagnostics only and MUST NOT contain protocol responses.
   `MAX_STDERR_BYTES_PER_CONNECTION` is 65,536 bytes, counted cumulatively from
   process start through stderr EOF. The host MUST continuously drain stderr,
   independently of stdout and request processing, until process exit or hard
   kill so the pipe cannot deadlock. It MUST retain no more than
   `MAX_STDERR_BYTES_PER_CONNECTION`; after the first excess byte it MUST mark
   the retained diagnostic as truncated, discard all overflow while continuing
   to drain, fail each affected operation with `STDERR_LIMIT_EXCEEDED` when a
   response is possible, preserve any possibly accepted delivery as unresolved
   for reconciliation, and quarantine the adapter pending explicit release.
   Every `initialize` and `runtime.*` invocation MUST be a JSON-RPC request with
   a non-null `id` that is either a JSON string or a safe integer in
   [-9,007,199,254,740,991, 9,007,199,254,740,991], unique among in-flight
   requests on that connection. An omitted `id` is a prohibited notification;
   a notification or invalid id MUST execute no action. The receiver MUST return
   `INVALID_REQUEST` when a correlatable response is possible, and for a
   notification, where JSON-RPC permits no response, it MUST close the
   connection and quarantine the adapter rather than claim an error was
   delivered. On invalid UTF-8, non-object JSON, a missing terminator, an
   embedded raw newline, or non-protocol standard output, the receiver MUST
   return `INVALID_FRAMING` when a response is possible and otherwise close the
   connection; it MUST NOT reinterpret stderr or concatenate adjacent lines
   into a request.

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
   direction MUST be the closed `initialize` exchange above. The host MUST
   construct its params only after trusted manifest and exact registry lookup;
   an `initialize` notification is prohibited. V1 requires
   `requested_protocol_version` and `negotiated_protocol_version` to be the
   unambiguous string constant `"1.0"`. The response MUST echo the exact
   trusted adapter, manifest, and `EndpointV1` identities and revisions and
   return the exact bound `CapabilitySetV1`. Before initialization succeeds,
   no other method is legal. An unknown or unsupported major version MUST return
   `UNSUPPORTED_PROTOCOL_VERSION` and terminate the connection; a non-
   `initialize` first method MUST return `INITIALIZE_REQUIRED` and terminate the
   connection. V1 performs no implicit minor-version coercion or downgrade; a
   future minor must define its own explicit compatible negotiation contract.
   Missing, extra, or mistyped initialize params MUST return `INVALID_PARAMS`;
   a malformed result MUST surface `INVALID_REQUEST`. Stale or non-echoing
   trusted identities use the narrower errors in Clauses 5 and 6. Every such
   failure occurs before initialization and terminates or quarantines the
   connection as applicable; it MUST NOT create a partial initialized binding.

5. **Trusted manifest lookup (normative).** The host MUST resolve the adapter
   executable, immutable argument vector, working directory, environment
   allowlist, adapter id, and manifest revision exclusively from a reviewed
   repository-trusted manifest keyed by the exact adapter id. The host MUST
   execute the resolved program directly and MUST NOT invoke a shell. A caller-
   supplied executable path, argv member, environment entry, working directory,
   shell string, manifest path, or adapter-id alias MUST be rejected as
   `UNTRUSTED_MANIFEST_INPUT`; the process MUST NOT be spawned from that input.
   The `initialize` adapter and manifest identity/revision members MUST come from
   that same lookup. `adapter_id` MUST equal the trusted manifest key and the
   initialized `EndpointV1.adapter_name`; `adapter_revision` MUST equal
   `EndpointV1.adapter_revision`; and `manifest_id` and `manifest_revision` MUST
   equal the selected manifest record. A request or result that aliases,
   substitutes, or mismatches any of those values is
   `UNTRUSTED_MANIFEST_INPUT`, performs no runtime action, and creates no
   initialized state.

6. **Capability negotiation (normative).** The adapter MUST declare its
   capabilities in the initialization `CapabilitySetV1`, including quality,
   constraints, evidence, and revision. Initialization succeeds only when the
   request and result carry the same exact trusted `EndpointV1`,
   `EndpointV1.capability_set_id` equals
   `CapabilitySetV1.capability_set_id`, their `workspace_id` values are equal,
   their complete discriminated `scope` objects are equal, and
   `CapabilitySetV1.revision`, `EndpointV1.adapter_revision`, and the initialize
   `adapter_revision` are equal. For project scope, both scope objects MUST
   contain the same non-null `project_id`; for workspace scope, both MUST omit
   `project_id`. `runtime.health` and `runtime.shutdown` are mandatory V1
   protocol control methods authorized by that successful exact initialization;
   they are not product capabilities and MUST NOT be required to appear in
   `CapabilitySetV1.capabilities`.

   Before any post-initialize invocation, the host MUST revalidate the exact
   initialized adapter, manifest, endpoint, and capability-set identities and
   revisions against the trusted registry and profile. For a session action,
   the trusted local profile MUST authorize it through the exact applicable
   non-`unsupported` entries already present in `CapabilitySetV1`, and the
   invocation MUST satisfy their declared constraints. This protocol does not
   create a capability-name namespace, require a JSON-RPC method name to equal
   a capability name, or reinterpret capability identities such as those
   frozen by S2. Before a session method, the
   `SessionRefV1.endpoint_id`, `workspace_id`, and complete scope discriminator
   MUST also match the initialized `EndpointV1` and `CapabilitySetV1`.
   Connection-scoped health and shutdown remain bound to that initialized
   endpoint without accepting a session selector and without a product-
   capability lookup. An undeclared, unsupported, stale, cross-workspace,
   cross-project, cross-endpoint, cross-capability-set, or constraint-violating
   session action or result MUST return `CAPABILITY_NOT_DECLARED` before action.
   A session identity mismatch additionally fails as `INVALID_SESSION_REF`.
   The host MUST NOT fall back to another method, capability, endpoint,
   capability set, project, workspace, or session.

7. **Exact session binding (normative).** The post-initialize session methods
   `runtime.deliver`, `runtime.cancel`, and `runtime.reconcile` MUST carry one
   complete `SessionRefV1`, and the host MUST validate it against the trusted
   registry, the initialized `EndpointV1`, the negotiated `CapabilitySetV1`,
   and the initialized adapter revision before invoking the adapter. A
   project-scoped `SessionRefV1` MUST contain the exact non-null
   `scope.project_id` present in the endpoint and capability-set registry
   records; if `repository_binding` is present, its `project_id` MUST also
   equal that value. A workspace-scoped `SessionRefV1` MUST omit
   `scope.project_id`, and its endpoint and capability set MUST also be
   workspace-scoped with `project_id` absent. In both cases, `workspace_id`,
   the complete scope object, `endpoint_id`, registry record, and adapter
   revision MUST match exactly. Wildcards, prefixes, display names, window
   order, `latest`, inferred cwd, inferred project, null identity, scope
   downgrade, and project substitution are prohibited. A missing, mismatched,
   stale, inferred, or non-authoritative session reference MUST return
   `INVALID_SESSION_REF`; the adapter MUST NOT choose a replacement session or
   infer a project. `runtime.health` and `runtime.shutdown` are
   connection/initialized-endpoint scoped and MUST follow their no-session
   rules in Clauses 11 and 15.

8. **Delivery (normative).** The `runtime.deliver` method MUST receive exactly
   the closed params above: `session_ref`, whose value is a `SessionRefV1`, and
   `delivery`, whose value is a `DeliveryV1`. Its successful result MUST be
   directly one `ReceiptV1`. The embedded schema objects MUST validate without
   added, removed, renamed, or adapter-private fields, and their workspace,
   scope, message, delivery, attempt, endpoint, session, and evidence identities
   MUST agree. A malformed or cross-identity request or result MUST return
   `INVALID_DELIVERY`; it MUST NOT advance canonical delivery state.

9. **Cancellation (normative).** The `runtime.cancel` method MUST name the exact
   original delivery request through the closed `original_request_id` param,
   use its own distinct request `id` under Clause 1, and carry the same exact
   `SessionRefV1` as the original request. The cancel invocation's only success
   result is the closed `{original_request_id, status:"cancelled"}` object; it
   is not a `REQUEST_CANCELLED` error. After that success, the original pending
   delivery request MUST terminate with the `REQUEST_CANCELLED` JSON-RPC error
   using the original request's id. Cancellation is idempotent: repeated
   cancellation of a request authoritatively cancelled before external
   acceptance MUST return the same cancel success result, while the original
   request remains terminally cancelled. Cancellation MUST NOT claim success
   when acceptance may have occurred; that cancel invocation MUST return
   `RECONCILIATION_REQUIRED`, preserve the original delivery and attempt as
   unresolved, and prohibit retry until reconciliation determines
   authoritative not-accepted evidence. Invalid cancel params return
   `INVALID_PARAMS` before action; a malformed cancel result advances no
   cancellation or delivery state, leaves possible acceptance unresolved, and
   surfaces `RECONCILIATION_REQUIRED`.

10. **Reconciliation (normative).** After adapter restart, connection loss, or
    any possibly accepted request without a committed result, the host MUST
    invoke `runtime.reconcile` with the closed params above: its own request id,
    the exact original JSON-RPC request id being reconciled, the exact
    `SessionRefV1`, and the outstanding delivery and attempt identities. The
    only success result is directly one valid `ReceiptV1` whose workspace,
    scope, delivery id, attempt id, endpoint id, session reference id, and
    embedded `StateEvidenceV1` identities match the request and core ledger.
    The receipt MUST resolve the attempt with authoritative evidence as
    `accepted`, `completed`, or `rejected_before_acceptance`; an `ambiguous`,
    `pull_pending`, `deferred_busy`, or other non-resolving state is not a
    reconciliation success.

    The core ledger remains authoritative for canonical intent, delivery
    identity, and unresolved state; the adapter is authoritative only for host
    observations represented by that valid receipt and evidence. A missing,
    additional, mistyped, identity-mismatched, non-authoritative, or
    contradictory reconciliation result MUST surface
    `RECONCILIATION_REQUIRED`, advance no canonical state, and keep the attempt
    unresolved or quarantined. Invalid params return `INVALID_PARAMS` before
    action. Neither side may synthesize success, choose another session, or
    blindly resend.

11. **Health (normative).** The host MUST call `runtime.health` every
    `HEALTH_INTERVAL_MS`, fixed at 10,000 milliseconds, using exactly `{}`
    params. Health is connection/initialized-endpoint scoped: it MUST NOT carry
    `SessionRefV1`, `session_ref`, a native-session id, or any other session
    selector, and it is legal immediately after successful initialization
    before native-session discovery or binding. A valid response MUST have the
    closed scalar result shape above, MUST exactly identify the negotiated
    protocol, initialized adapter, trusted manifest, endpoint, workspace and
    complete scope discriminator, and capability-set identity/revisions, and
    MUST arrive inside `REQUEST_DEADLINE_MS`. For project scope the exact
    initialized `project_id` is required; for workspace scope `project_id` is
    forbidden. Three consecutive missed, malformed, unhealthy-status,
    identity-mismatched, or revision-mismatched health responses
    (`HEALTH_FAILURE_THRESHOLD = 3`) move the adapter out of service and return
    `ADAPTER_UNHEALTHY` to new work. The host MUST stop assigning requests until
    a new initialization and any required operator release have completed. An
    invalid health request returns `INVALID_PARAMS` before action; an invalid
    health result advances no health state and counts as one failed response.

12. **Quarantine (normative).** The host MUST quarantine an adapter for
    unsupported version drift, trusted-manifest mismatch, capability or exact-
    session contract violation, a prohibited notification, stderr overflow,
    redaction failure, repeated closed-envelope, result-shape, or other invalid
    protocol output, unresolved possible acceptance, or the health failure
    threshold. Quarantine MUST create an operator-visible record containing
    adapter id, manifest and adapter revisions, initialized endpoint and
    capability-set identities/revisions, reason code, correlation ids, affected
    session and attempt ids when applicable, bounded stderr byte and truncation
    counts when applicable, evidence references, and timestamps after
    redaction. Quarantine MUST NOT auto-clear on reconnect or process restart.
    Release requires an explicit operator action after the manifest/profile and
    bounded-diagnostic behavior are reviewed, unresolved deliveries are
    reconciled, and a fresh handshake and connection-scoped health sequence
    succeed; otherwise requests receive `ADAPTER_QUARANTINED`.

13. **Structured errors (normative).** Every failure MUST use a JSON-RPC error
    with the exact closed envelope and error-data shape above and a numeric code
    from the following closed enumeration. The `message`, `data.name`, and
    `data.retryable` values MUST exactly match the selected row. Response-
    envelope and method-result violations use `INVALID_REQUEST` unless Clauses
    4 through 11 require a narrower method-specific error; params-shape
    violations use `INVALID_PARAMS`; unknown methods use `METHOD_NOT_FOUND`;
    embedded schema or identity violations use their required narrower error.
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
    | -32017 | `STDERR_LIMIT_EXCEEDED` | no; explicit release required |

    Any numeric code outside this list is a protocol violation. The host MUST
    record it and quarantine the adapter rather than guessing retryability.

14. **Redaction (normative).** Before any log, evidence file, diagnostic,
    quarantine record, or persistent protocol trace is written, both sides MUST
    redact credentials, authorization headers, cookies, API keys, tokens,
    environment values, message-body bytes, caller-provided raw payloads,
    `configuration_ref` resolution data, local user/home paths, and native
    session identifiers except for approved stable hashes or schema identity
    references. The bounded retained stderr prefix is subject to the same rule;
    discarded stderr overflow MUST NOT be reconstructed or persisted, and only
    redacted bounded diagnostics, byte counts, and a truncation marker may enter
    the quarantine record. Redaction MUST happen before persistence, not at read
    time. If required redaction cannot be proven, persistence MUST stop, the
    request MUST return `REDACTION_FAILURE`, and the adapter MUST be
    quarantined.

15. **Shutdown (normative).** `runtime.shutdown` is connection-scoped, MUST use
    the request id required by Clause 1, and MUST NOT carry a `SessionRefV1`,
    `session_ref`, or any other session selector. It is legal immediately after
    successful initialization, including before native-session discovery or
    binding. Its params MUST be exactly `{}`. On a valid request, the host and
    adapter MUST enter shutdown before returning exactly
    `{"status":"shutdown_started"}`; they MUST stop admitting new requests and
    return `SHUTDOWN_IN_PROGRESS` for later work. They MUST cancel work
    authoritatively known not to have been accepted, preserve and reconcile
    uncertain work, and drain remaining in-flight requests for
    `SHUTDOWN_DRAIN_MS = 10,000`. The adapter then MUST flush protocol output
    and exit. At `SHUTDOWN_HARD_KILL_MS = 15,000` from shutdown start, the host
    MUST terminate a still-running process while continuing to drain stderr to
    EOF. A hard kill MUST leave possibly accepted attempts unresolved or
    quarantined; it MUST NOT mark them cancelled, accepted, or completed without
    authoritative evidence. Missing, extra, or mistyped shutdown params return
    `INVALID_PARAMS` and MUST NOT begin shutdown. A malformed shutdown result
    MUST NOT be treated as proof that graceful shutdown began.

16. **Caller-input prohibitions (normative).** A caller MUST NEVER supply or
    override an executable path, argv, working directory, environment,
    capability grant, adapter-id alias, session selection by pattern, or any
    session action not backed by an applicable declared capability. The
    mandatory connection-scoped health and shutdown controls are selected by
    the host under the successfully initialized protocol, not by caller input
    and not by a product-capability grant. The host MUST reject each prohibited
    input as `UNTRUSTED_MANIFEST_INPUT`, `INVALID_SESSION_REF`, or
    `CAPABILITY_NOT_DECLARED` as applicable, before spawn or dispatch. It MUST
    NOT sanitize the input into authority, merge it with trusted configuration,
    invoke a shell, broaden a capability, or select a fallback runtime action.
