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
`runtime.shutdown`. On this closed connection the host is the JSON-RPC client:
it originates requests and handles responses. The adapter is the JSON-RPC
server: it handles requests and originates responses. A normal V1 request
object has exactly the four members below and no others:

```json
{
  "jsonrpc": "2.0",
  "id": "request-id",
  "method": "runtime.health",
  "params": {}
}
```

`jsonrpc` MUST be the string constant `"2.0"`. `id` MUST satisfy the non-null
`RequestId` rule in Clause 1. `method` MUST be one of the six exact method
names above. `params` MUST be the method-specific closed object defined below;
positional params are prohibited. A normal success response has exactly
`jsonrpc`, the request's JSON-type-and-value-exact non-null `id`, and `result`.
A normal error response has exactly `jsonrpc`, the same exact non-null `id`,
and `error`. A response MUST contain exactly one of `result` and `error`, never
both and never neither.

The `error` object has exactly `code`, `message`, and `data`. `code` MUST be one
numeric value from Clause 13; `message` MUST be that row's exact symbolic name.
`data` is a closed object requiring `name`, `retryable`, and `request_id`.
`retryable` MUST be a JSON boolean; `name` and `retryable` MUST match the same
Clause 13 row exactly, and `request_id` MUST equal the response `id` by JSON
type and value. `data` MAY additionally contain `correlation_id`, using the S2
token scalar, and `evidence_refs`, an array of at most 32 redacted
`StateEvidenceV1.evidence_id` strings. No other error or error-data member is
legal.

There is exactly one exception to non-null response correlation. When the
adapter receives an inbound would-be request but cannot recover a valid
`RequestId` because the JSON text does not parse or the parsed request
envelope/id is invalid, it MUST emit exactly
`{"jsonrpc":"2.0","id":null,"error":...}`. The matching closed `error.data`
object MUST set `request_id` to JSON `null`; `PARSE_ERROR` is required for
invalid JSON syntax and `INVALID_REQUEST` for parsed JSON that is not a valid
request envelope/id. No other response or `error.data.request_id` may be null.
This exception does not make a null request id legal. If an otherwise valid
request omits `id`, it is a prohibited JSON-RPC notification: the adapter MUST
execute no action, send no response, and apply Clause 1's close/quarantine
behavior. If a malformed request still contains a recoverable valid
`RequestId`, the adapter MUST use the normal error response with that exact id.

Every object described in this section is closed. Missing, additional, or
mistyped request- or response-envelope members are `INVALID_REQUEST`. Missing,
additional, or mistyped `params` members or request-embedded schema objects are
`INVALID_PARAMS`. The adapter MUST validate the complete request before
performing an action, and the host MUST validate the complete response before
advancing canonical state. A malformed request performs no action and receives
the single response allowed by the rules above. A malformed adapter response
or result is a host-local protocol failure: the host MUST classify and record
the single failure code selected by the ordered pipeline below, advance no
canonical state, preserve possibly accepted work as unresolved, and quarantine
where Clauses 10 or 12 require it. The host MUST NOT send a JSON-RPC response or
error in response to a response. Unknown request methods return
`METHOD_NOT_FOUND` without action. No implementation may repair an invalid
object by dropping members, applying defaults, coercing types, inferring
identity, or selecting a fallback.

Every inbound request and adapter response MUST pass the following exhaustive
validation pipeline in order. Validation stops at the first failing step; that
step alone selects one response code for a request or one host-local
failure/quarantine code for a response. Later failures MUST NOT replace,
supplement, or combine with it.

- **P1 — frame and parse.** Validate message size, physical UTF-8 line framing,
  and then JSON syntax, in that order. Use `MESSAGE_TOO_LARGE` for the Clause 2
  byte bound, `INVALID_FRAMING` for a physical framing failure, and
  `PARSE_ERROR` for invalid JSON syntax after a complete frame is available.
- **P2 — envelope, id, method, and direction.** Validate the closed
  request/response envelope, request id, method, and allowed direction. Use the
  notification no-response rule above, `INVALID_REQUEST` for an invalid
  envelope/id/direction, and `METHOD_NOT_FOUND` for an otherwise valid request
  naming a method outside the closed six.
- **P3 — params, result, and embedded schema shape.** Validate the
  method-specific closed params or result and every embedded S2 schema object's
  closed shape before evaluating identities. Request params or embedded-schema
  shape failures are `INVALID_PARAMS`; a missing, additional, or mistyped
  method-specific result member is host-local `INVALID_REQUEST`; and an
  embedded result-schema shape failure is host-local `INVALID_PARAMS`. A
  malformed top-level response was already classified as host-local
  `INVALID_REQUEST` at P2.
- **P4 — initialize and manifest.** Validate initialize ordering and version
  plus the exact trusted manifest/initialized binding. Use
  `INITIALIZE_REQUIRED`, `UNSUPPORTED_PROTOCOL_VERSION`, or
  `UNTRUSTED_MANIFEST_INPUT` as applicable.
- **P5 — session target.** For a session method, validate the complete
  `SessionRefV1` against the trusted registry, initialized endpoint, workspace,
  and discriminated scope. Any failure at this step is `INVALID_SESSION_REF`,
  even if the same input would later lack capability authority.
- **P6 — capability authority.** Validate the exact endpoint-bound
  `CapabilitySetV1`, trusted profile, and action authorization under Clause 6.
  Any failure at this step is `CAPABILITY_NOT_DECLARED`. Thus a valid session
  without the exact applicable capability authority is not reclassified as a
  session error.
- **P7 — delivery and reconciliation identity.** Validate `DeliveryV1`,
  `ReceiptV1`, canonical-ledger cross-identities, and reconciliation truth.
  After a valid session and capability, mismatched
  delivery/message/attempt/endpoint/session/evidence identities are
  `INVALID_DELIVERY`. For reconciliation, absent authoritative observation or
  contradictory, ambiguous, or non-authoritative evidence that leaves truth
  unresolved is `RECONCILIATION_REQUIRED`.
- **P8 — admission state.** Apply `TOO_MANY_IN_FLIGHT`,
  `ADAPTER_UNHEALTHY`, `ADAPTER_QUARANTINED`, or
  `SHUTDOWN_IN_PROGRESS`. No runtime action begins before this step passes.
- **P9 — execution.** A subsequently observed handshake/request timeout,
  cancellation, reconciliation uncertainty, stderr overflow, redaction
  failure, or otherwise internal failure uses only its corresponding Clause 13
  code. Such execution failures do not reopen earlier validation steps.

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
  corresponding S2 scalar and exactly echoes the initialized binding.
  `adapter_revision` echoes the adapter implementation revision;
  `capability_set_revision` separately echoes the capability-profile revision,
  with no equality required between them. A project-scoped result additionally
  requires exactly one `project_id` using the S2 token scalar; a
  workspace-scoped result forbids `project_id`.
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
   `RequestId` is a non-null JSON string or a safe integer in
   [-9,007,199,254,740,991, 9,007,199,254,740,991], unique among in-flight
   requests on that connection. Every `initialize` and `runtime.*` invocation
   MUST carry a `RequestId`. An omitted `id` is a prohibited notification; a
   notification or invalid id MUST execute no action. A notification receives
   no response and MUST close the connection and quarantine the adapter rather
   than claim an error was delivered. An invalid id follows the null-correlation
   `INVALID_REQUEST` exception above unless a different valid `RequestId` is
   recoverable, which V1 never infers.

   Physical framing is validated before JSON grammar. Invalid UTF-8 or EOF
   before the required terminating `\n` is `INVALID_FRAMING`; when that failure
   prevents recovery of a complete request and valid `RequestId`, the receiver
   closes without a response rather than widening the null-correlation
   exception. Once a complete UTF-8 line is available, all invalid JSON syntax,
   including an empty line, is `PARSE_ERROR`; valid non-object JSON is
   `INVALID_REQUEST`. An unescaped raw newline in a JSON string terminates that
   physical frame, so the incomplete JSON line is `PARSE_ERROR` and the
   following line is validated independently. The receiver MUST NOT reinterpret
   stderr, concatenate adjacent lines, or answer malformed adapter output;
   adapter-output failures are recorded locally under the same code and
   direction rules above.

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
   Missing, extra, or mistyped initialize params MUST return `INVALID_PARAMS`.
   A missing, additional, or mistyped initialize result member is host-local
   `INVALID_REQUEST`; an embedded `EndpointV1` or `CapabilitySetV1` shape
   failure is host-local `INVALID_PARAMS`. Neither receives a response. Only
   after those stages pass do stale or non-echoing trusted identities reach the
   narrower errors in Clauses 5 and 6. Every such failure occurs before
   initialization and terminates or quarantines the connection as applicable;
   it MUST NOT create a partial initialized binding.

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
   initialized state. A request failure receives that error; a result failure
   is host-local and receives no response.

6. **Capability negotiation (normative).** The adapter MUST declare its
   capabilities in the initialization `CapabilitySetV1`, including quality,
   constraints, evidence, and revision. The trusted registry MUST bind the
   exact initialized `EndpointV1.capability_set_id` to one exact complete
   `CapabilitySetV1` record, including its own `revision`, capabilities,
   quality, constraints, and attestations. Initialization succeeds only when
   the request and result carry the same exact trusted `EndpointV1`, the result
   carries that exact complete registry-bound `CapabilitySetV1`, their
   `workspace_id` values are equal, and their complete discriminated `scope`
   objects are equal. For project scope, both scope objects MUST contain the
   same non-null `project_id`; for workspace scope, both MUST omit
   `project_id`.

   Adapter implementation authority and capability-profile authority are
   independent. The initialize `adapter_id` and `adapter_revision` MUST equal
   `EndpointV1.adapter_name` and `EndpointV1.adapter_revision`, respectively.
   Every non-`unsupported` capability attestation's `source_id` and
   `source_revision` MUST equal that same endpoint adapter name and revision.
   Separately, `CapabilitySetV1.revision` is the trusted profile revision. V1
   MUST NOT require `CapabilitySetV1.revision` to equal
   `EndpointV1.adapter_revision`, the initialize `adapter_revision`, or any
   other implementation revision.

   `runtime.health` and `runtime.shutdown` are mandatory V1 protocol control
   methods authorized by that successful exact initialization; they are not
   product capabilities and MUST NOT be required to appear in
   `CapabilitySetV1.capabilities`.

   Before any post-initialize invocation, the host MUST revalidate the exact
   initialized adapter, manifest, endpoint, and capability-set identities and
   independent revisions against the trusted registry and profile. For a
   session action, the trusted local profile MUST authorize it through the
   exact applicable non-`unsupported` entry already present in
   `CapabilitySetV1`, and the invocation MUST satisfy its declared constraints.
   Every canonical `StateEvidenceV1` carried by the applicable
   `SessionRefV1`, `DeliveryV1`, or `ReceiptV1` MUST bind
   `authority.capability_profile_id` to that exact existing capability entry
   and MUST bind both the trusted profile revision and
   `authority.capability_profile_revision` to `CapabilitySetV1.revision`.
   Independently, its `authority.identity` and
   `authority.implementation_revision` MUST equal
   `EndpointV1.adapter_name` and `EndpointV1.adapter_revision`, and the
   applicable capability attestation MUST carry those same values as
   `source_id` and `source_revision`. This protocol does not create a
   capability-name namespace, require a JSON-RPC method name to equal a
   capability name, or reinterpret capability identities such as those frozen
   by S2. Before a session method, the
   `SessionRefV1.endpoint_id`, `workspace_id`, and complete scope discriminator
   MUST also match the initialized `EndpointV1` and `CapabilitySetV1`.
   Connection-scoped health and shutdown remain bound to that initialized
   endpoint without accepting a session selector and without a product-
   capability lookup. An undeclared, unsupported, stale, cross-workspace,
   cross-project, cross-endpoint, cross-capability-set, or constraint-violating
   session action or result is `CAPABILITY_NOT_DECLARED` at pipeline P6. A
   session identity mismatch fails earlier as `INVALID_SESSION_REF` at P5.
   Request failures receive the selected error; response/result failures are
   host-local and receive no response.
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
   revision MUST match exactly. The session evidence's
   `authority.identity`/`implementation_revision` MUST match the endpoint
   adapter authority, while its `authority.capability_profile_revision` and
   trusted profile revision MUST independently match the bound
   `CapabilitySetV1.revision`. Wildcards, prefixes, display names, window
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
   MUST agree. After params/schema, session, and capability validation pass, a
   cross-identity request or result is `INVALID_DELIVERY`. A request failure
   receives that error; an adapter result failure is host-local, receives no
   response, preserves possible acceptance as unresolved, and MUST NOT advance
   canonical delivery state.

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
   `INVALID_PARAMS` before action. A missing, additional, or mistyped cancel
   result member is host-local `INVALID_REQUEST` at P3, receives no response,
   advances no cancellation or delivery state, and leaves possible acceptance
   unresolved. `RECONCILIATION_REQUIRED` is reserved for a validly shaped
   cancel response or execution outcome that establishes that acceptance may
   have occurred.

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
    additional, or mistyped reconciliation result member is host-local
    `INVALID_REQUEST` at P3; an embedded `ReceiptV1` shape failure is host-local
    `INVALID_PARAMS` at P3. A validly shaped receipt whose delivery, attempt,
    endpoint, session, or other canonical-ledger identity mismatches is
    host-local `INVALID_DELIVERY` at P7. Only a validly shaped,
    identity-matching receipt whose authoritative observation is absent,
    ambiguous, non-authoritative, or contradictory is host-local
    `RECONCILIATION_REQUIRED` at P7. Each such response failure receives no
    response, advances no canonical state, and keeps the attempt unresolved or
    quarantined. A reconciliation request with invalid params returns
    `INVALID_PARAMS` before action. Neither side may synthesize success, choose
    another session, or blindly resend.

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
    invalid health request returns `INVALID_PARAMS` before action. A missing,
    additional, or mistyped health result member is host-local
    `INVALID_REQUEST` at P3, receives no response, advances no health state, and
    counts as one failed response. An identity or independent-revision mismatch
    is classified only at its later pipeline stage and also counts as one
    failed response; the threshold may make future work `ADAPTER_UNHEALTHY`,
    but it does not replace the current response's first-failure code.

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

13. **Structured errors (normative).** Every request failure for which JSON-RPC
    permits a response MUST use the exact closed error envelope and error-data
    shape above and one numeric code from the following closed enumeration.
    Every adapter response/result failure MUST be recorded locally under the
    same enumeration and ordered pipeline, but the host MUST NOT send a response
    to that response. `error.data.retryable` MUST be the exact JSON boolean in
    the selected row. The `message`, `data.name`, and `data.retryable` values
    MUST all match that row exactly. The first failing pipeline step is the sole
    classifier; free-form strings, stderr, exit status, later validation
    failures, or host-specific exceptions are not substitutes for or additions
    to the selected code.

    | Code | Name | Retryable |
    |---:|---|---|
    | -32700 | `PARSE_ERROR` | `false` |
    | -32600 | `INVALID_REQUEST` | `false` |
    | -32601 | `METHOD_NOT_FOUND` | `false` |
    | -32602 | `INVALID_PARAMS` | `false` |
    | -32603 | `INTERNAL_ERROR` | `false` |
    | -32000 | `INVALID_FRAMING` | `false` |
    | -32001 | `MESSAGE_TOO_LARGE` | `false` |
    | -32002 | `TOO_MANY_IN_FLIGHT` | `true` |
    | -32003 | `HANDSHAKE_TIMEOUT` | `false` |
    | -32004 | `UNSUPPORTED_PROTOCOL_VERSION` | `false` |
    | -32005 | `INITIALIZE_REQUIRED` | `false` |
    | -32006 | `UNTRUSTED_MANIFEST_INPUT` | `false` |
    | -32007 | `CAPABILITY_NOT_DECLARED` | `false` |
    | -32008 | `INVALID_SESSION_REF` | `false` |
    | -32009 | `INVALID_DELIVERY` | `false` |
    | -32010 | `REQUEST_TIMEOUT` | `false` |
    | -32011 | `REQUEST_CANCELLED` | `false` |
    | -32012 | `RECONCILIATION_REQUIRED` | `false` |
    | -32013 | `ADAPTER_UNHEALTHY` | `true` |
    | -32014 | `ADAPTER_QUARANTINED` | `false` |
    | -32015 | `REDACTION_FAILURE` | `false` |
    | -32016 | `SHUTDOWN_IN_PROGRESS` | `false` |
    | -32017 | `STDERR_LIMIT_EXCEEDED` | `false` |

    Any numeric code outside this list is a protocol violation. The host MUST
    record it and quarantine the adapter rather than guessing retryability.
    `TOO_MANY_IN_FLIGHT` may be retried only after capacity is available.
    `ADAPTER_UNHEALTHY` may be retried only after a fresh healthy
    initialization. `REQUEST_TIMEOUT` and `RECONCILIATION_REQUIRED` are not
    resend permissions and require reconciliation rather than retry.
    `STDERR_LIMIT_EXCEEDED` and `ADAPTER_QUARANTINED` remain non-retryable
    pending explicit operator release. Every other `false` row remains
    non-retryable under its defining clause.

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
    `INVALID_PARAMS` and MUST NOT begin shutdown. A missing, additional, or
    mistyped shutdown result member is host-local `INVALID_REQUEST` at P3,
    receives no response, and MUST NOT be treated as proof that graceful
    shutdown began.

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
