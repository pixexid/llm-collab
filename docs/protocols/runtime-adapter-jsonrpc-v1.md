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
legal. Every legal `RequestId`, its mandatory response echo, and the mandatory
`error.data.request_id` echo MUST serialize inside `MAX_MESSAGE_BYTES`; optional
`correlation_id` or `evidence_refs` members MUST be omitted if including them
would exceed that bound.

For every inbound complete frame, the receiver MUST inspect the raw member
sequence of every JSON object at every depth before an ordinary object parser
may collapse those members into a map. Member names are compared as decoded
JSON string values, so alternate escape spellings of the same name are the same
member name. If any top-level, `params`, `result`, schema, `error`, `data`, or
other nested object repeats a member name, the entire frame is duplicate-bearing
and MUST fail only at P1 as `PARSE_ERROR`. It MUST perform no action, and no later
stage may replace or supplement that classification. No `RequestId` is
recoverable from a duplicate-bearing frame, even if one visible top-level `id`
member appears exactly once.

P1 always precedes direction classification. A size, physical-framing, invalid-
JSON, or duplicate-member failure keeps its sole P1 classification and cannot
be reclassified from an apparent request or response shape. Only after P1
succeeds MUST the receiver apply this exhaustive P2 direction matrix:

| Sender and receiver | JSON-RPC form | Sole P2 direction outcome |
|---|---|---|
| Host to adapter | Request | Direction-valid. The adapter validates the closed request envelope, `RequestId`, and method, then continues through P3-P9. |
| Adapter to host | Response | Direction-valid. The host validates the closed response envelope and exact request correlation, then continues through P3-P9. |
| Adapter to host | Request | Prohibited adapter request: host-local `INVALID_REQUEST`, no JSON-RPC response or error, no action, no canonical-state advance, connection close, and adapter quarantine under Clause 12. |
| Host to adapter | Response | Prohibited host response: adapter-local `INVALID_REQUEST`, no JSON-RPC response or error, no action, no operation-state advance, a local direction-fault record, and connection close. The host MUST treat that close as its own outbound protocol fault and MUST NOT quarantine or blame the adapter solely for the host-originated wrong-direction frame. |

For matrix selection only, response form takes precedence: any top-level
`result` or `error` member selects response form, even when a top-level `method`
member is also present. Only when neither top-level `result` nor `error` is
present does a top-level `method` select request form. The two prohibited rows
are selected before validating their `id`, correlation, method, `result`,
`error`, or method-specific content. A parsed object containing none of
`method`, `result`, and `error` follows the direction-valid expected envelope
path for its sender and fails that closed envelope deterministically. These
rules are exhaustive; implementations MUST NOT invent a fifth direction case.
A both-family host-to-adapter object is therefore only prohibited response
form. A both-family adapter-to-host object is direction-valid response form,
but its additional `method` fails the closed response envelope at P2 as
host-local `INVALID_REQUEST`, with no response, action, or canonical-state
advance and with the existing invalid-output and quarantine rules.

There is exactly one exception to non-null response correlation. When the
adapter receives a direction-valid host-to-adapter inbound would-be request but
cannot recover a valid `RequestId` because the JSON text does not parse, any
object repeats a member name, or the parsed request envelope/id is invalid other
than the absent-id notification below, it MUST emit exactly
`{"jsonrpc":"2.0","id":null,"error":...}`. The matching closed `error.data`
object MUST set `request_id` to JSON `null`; `PARSE_ERROR` is required for
invalid JSON syntax or a repeated member name, and `INVALID_REQUEST` is required
for duplicate-free parsed JSON that is not a valid request envelope/id. No other
response or `error.data.request_id` may be null. This exception does not make a
null request id legal. If an otherwise valid direction-valid host-to-adapter
request omits `id`, it is a prohibited JSON-RPC notification: the adapter MUST
classify it only as adapter-local P2 `INVALID_REQUEST`, execute no action, send
no response, record the local notification fault, and close the connection.
The host MUST record its own outbound protocol fault and MUST NOT quarantine,
blame, or require operator release for the adapter solely because the adapter
correctly closed. The absent-id notification MUST NOT reach the null-
correlation response path. If a duplicate-free malformed direction-valid host-
to-adapter request instead contains a recoverable valid `RequestId`, the
adapter MUST use the normal error response with that exact id. Neither the
null-correlation exception nor this malformed-request response rule applies to
a duplicate-free host-to-adapter JSON-RPC response, including one that also
contains `method`.
That frame takes only the prohibited-host-response row above, even when its
`id` is null, missing, invalid, uncorrelated, or otherwise malformed.

Every object described in this section is closed. Missing, additional, or
mistyped request- or response-envelope members are `INVALID_REQUEST`. Missing,
additional, or mistyped `params` members or request-embedded schema objects are
`INVALID_PARAMS`. The adapter MUST validate the complete request before
performing an action, and the host MUST validate the complete response before
advancing canonical state. A malformed direction-valid host-to-adapter request
performs no action and receives the single response allowed by the rules above.
A malformed adapter response or result is a host-local protocol failure: the
host MUST classify and record the single failure code selected by the ordered
pipeline below, advance no canonical state, preserve possibly accepted work as
unresolved, and quarantine where Clauses 10 or 12 require it. A
duplicate-bearing adapter output is specifically a host-local P1 `PARSE_ERROR`;
the host MUST send no response, perform no action, advance no canonical state,
and quarantine the adapter under Clause 12. After P1 succeeds, any
adapter-to-host JSON-RPC request is prohibited regardless of whether it carries
a valid `RequestId`: it is a host-local P2 `INVALID_REQUEST`, and the host MUST
send no response, perform no action, advance no canonical state, close the
connection, and quarantine the adapter. The host MUST NOT send a JSON-RPC
response or error in response to any adapter output. After P1 succeeds, any
host-to-adapter JSON-RPC response is prohibited regardless of its correlation
or content: it is adapter-local P2 `INVALID_REQUEST`; the adapter MUST send no
response or error, perform no action, advance no operation state, record the
local direction fault, and close the connection. The host MUST record that
close as its own outbound protocol fault and MUST NOT quarantine or attribute
the direction violation to the adapter solely because the adapter closed as
required. A direction-valid host-to-adapter request with an unknown method
returns `METHOD_NOT_FOUND` without action. No implementation may repair an
invalid object by dropping members, applying defaults, coercing types,
inferring identity, or selecting a fallback.

Every inbound host-to-adapter frame and every adapter output MUST pass the
following exhaustive validation pipeline in order. Validation stops at the
first failing step; that step alone selects one response code for a
direction-valid host-to-adapter request or one host-local failure/quarantine
code for any adapter output. Later failures MUST NOT replace, supplement, or
combine with it.

- **P1 — frame and parse.** Validate message size, physical UTF-8 line framing,
  raw duplicate member names in every object at every depth, and then ordinary
  JSON object parsing, in that order. Use `MESSAGE_TOO_LARGE` for the Clause 2
  byte bound, `INVALID_FRAMING` for a physical framing failure, and
  `PARSE_ERROR` for invalid JSON syntax or any repeated member name after a
  complete frame is available. A duplicate-bearing frame yields no recoverable
  request id and cannot reach P2.
- **P2 — envelope, id, method, and direction.** After P1 succeeds, apply the
  exhaustive four-row direction matrix above before id, correlation, method,
  result, error, or method-specific validation. Top-level `result` or `error`
  presence selects response form before `method` presence is considered;
  otherwise `method` selects request form, and the absence of all three follows
  the sender's direction-valid expected envelope path. A host-to-adapter request
  and adapter-to-host response are the only direction-valid forms. An
  adapter-to-host request is host-local `INVALID_REQUEST` with no response and
  the close/quarantine behavior above. A host-to-adapter response is
  adapter-local `INVALID_REQUEST` with no response, action, or operation-state
  advance; the adapter records the direction fault and closes, and the host
  records its own outbound fault without adapter quarantine or blame solely for
  that frame. A direction-valid host request that omits `id` is the adapter-
  local notification fault above and cannot use null correlation; an adapter-
  to-host request, including one with an absent `id`, remains the host-local
  prohibited-adapter-request fault with close and quarantine. For the two
  direction-valid forms, validate the
  closed envelope, request id/correlation, and method; use `INVALID_REQUEST` for
  another invalid envelope/id and `METHOD_NOT_FOUND` for an otherwise valid
  host request naming a method outside the closed six.
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
- **P5 — session target.** For a session method, validate only the already
  shape-valid `SessionRefV1`'s exact trusted registry/session identity and
  binding proof under Clause 7: workspace and discriminated scope, endpoint,
  session-ref and native-session identities, optional repository binding,
  exact-session-binding evidence integrity/kind/quality/authority kind and
  subjects, and adapter authority identity/implementation revision. P5 MUST
  NOT inspect either `authority.capability_profile_*` field, a trusted
  capability-profile revision, the action relation, or any `CapabilitySetV1`
  entry, quality, constraints, or attestation. Any P5 failure is only
  `INVALID_SESSION_REF`.
- **P6 — capability authority.** Validate only the exact endpoint-bound
  `CapabilitySetV1` and trusted profile, the deterministic session-action
  relation, and every relevant `StateEvidenceV1` capability-profile
  id/revision, entry, quality ceiling, constraints, and attestation under
  Clause 6. Any P6 failure is only `CAPABILITY_NOT_DECLARED`, including a
  capability-profile failure inside an otherwise identity-valid
  `SessionRefV1`. P6 MUST NOT reclassify that failure as
  `INVALID_SESSION_REF`.
- **P7 — delivery and reconciliation identity.** Validate `DeliveryV1`,
  `ReceiptV1`, non-capability evidence/canonical-ledger cross-identities, and
  reconciliation truth.
  After a valid session and capability, mismatched
  delivery/message/attempt/endpoint/session/evidence identities are
  `INVALID_DELIVERY`. For reconciliation, absent authoritative observation or
  contradictory, ambiguous, or non-authoritative evidence that leaves truth
  unresolved is `RECONCILIATION_REQUIRED`.
- **P8 — admission state.** Apply Clause 2's total, delivery, and reserved-
  control capacity bounds and Clauses 11, 12, and 15's health, quarantine,
  recovery-only, and shutdown gates. Shutdown blocks later work first. When an
  adapter is quarantined, `ADAPTER_QUARANTINED` takes precedence over
  `ADAPTER_UNHEALTHY`; without the one exact operator-authorized recovery
  connection, either state blocks normal work. On that recovery connection,
  only the exact methods and recorded attempts allowed by Clause 12 may reach
  capacity admission; any other request receives the applicable adapter-state
  error. An otherwise admissible request that exceeds its delivery or control
  pool receives `TOO_MANY_IN_FLIGHT`. No request is queued, no pool borrows from
  the other, and no runtime action begins before this step passes.
- **P9 — execution.** A subsequently observed handshake/request timeout,
  cancellation, reconciliation uncertainty, stderr overflow, redaction
  failure, or otherwise internal failure uses only its corresponding Clause 13
  code. Such execution failures do not reopen earlier validation steps.

The P5/P6 boundary is exhaustive and non-overlapping:

| Validation fact | Pipeline stage | Sole classification |
|---|---|---|
| `SessionRefV1` registry identity; workspace/scope/project presence; endpoint, session-ref, native-session, or repository binding; binding-evidence integrity/kind/authoritative quality/authority kind/subject; or binding-evidence adapter identity/implementation revision | P5 | `INVALID_SESSION_REF` |
| Session-method relation lookup; exact capability-set/profile binding; or any carried `StateEvidenceV1.authority.capability_profile_id`/`capability_profile_revision`, selected entry, quality ceiling, constraints, or attestation | P6 | `CAPABILITY_NOT_DECLARED` |
| `DeliveryV1`/`ReceiptV1` canonical or non-capability evidence cross-identity after P6 succeeds | P7 | `INVALID_DELIVERY` or, only for unresolved reconciliation truth, `RECONCILIATION_REQUIRED` |

A record can fail more than one fact, but ordered validation reports only the
first row reached. In particular, a shape-valid, identity-valid session whose
binding evidence names a missing or stale capability profile passes P5 and
fails only P6 as `CAPABILITY_NOT_DECLARED`; it can never also produce
`INVALID_SESSION_REF`.

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
  original delivery request's exact bounded `RequestId` scalar. Its success
  result contains exactly `original_request_id`, equal in JSON type and value to
  the params member, and `status`, whose string constant is `"cancelled"`.
- `runtime.reconcile` params contain exactly `session_ref`,
  `original_request_id`, `delivery_id`, and `attempt_id`. `session_ref` is one
  complete `SessionRefV1`; `original_request_id` is the unresolved request's
  exact bounded `RequestId` scalar; and the last two values use the exact S2
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
   `RequestId` is either a nonempty JSON string whose decoded value is 1-256
   UTF-8 bytes or a safe integer in
   [-9,007,199,254,740,991, 9,007,199,254,740,991], unique among in-flight
   requests on that connection. This exact scalar and bound also apply to every
   carried `original_request_id`; neither side may coerce, truncate, hash, or
   substitute it. The string bound guarantees that the required top-level id
   echo and mandatory `error.data.request_id` echo fit within
   `MAX_MESSAGE_BYTES`; implementations MUST still measure the complete encoded
   response before writing it. Every direction-valid host-to-adapter
   `initialize` and `runtime.*` invocation MUST carry a `RequestId`. An omitted
   `id` in such a request is a prohibited notification; an empty string, a
   string over 256 UTF-8 bytes, a non-safe integer, `null`, or another JSON type
   is an invalid id. Either case MUST execute no action. The absent-id host
   notification is adapter-local P2 `INVALID_REQUEST`: the adapter sends no
   response, records the local fault, and closes, while the host records its own
   outbound protocol fault and MUST NOT quarantine, blame, or require release
   for the adapter solely for that correct close. It cannot reach null
   correlation. A present invalid or oversized id follows the P2 null-
   correlation `INVALID_REQUEST` exception above unless a different valid
   `RequestId` is recoverable, which V1 never infers. A carried
   `original_request_id` that violates the same scalar is instead
   `INVALID_PARAMS` at P3 before action.

   Physical framing is validated before JSON grammar. Invalid UTF-8 or EOF
   before the required terminating `\n` is `INVALID_FRAMING`; when that failure
   prevents recovery of a complete request and valid `RequestId`, the receiver
   closes without a response rather than widening the null-correlation
   exception. Once a complete UTF-8 line is available, the receiver MUST inspect
   every raw object-member sequence before ordinary object parsing. Any repeated
   decoded member name at any depth, including inside `params`, `result`, a
   schema object, `error`, or `data`, makes the complete frame only
   `PARSE_ERROR`; no visible `id` is recoverable, no action occurs, and later
   stages cannot replace the classification. All invalid JSON syntax, including
   an empty line, is also `PARSE_ERROR`; valid duplicate-free non-object JSON is
   `INVALID_REQUEST`. For direction-valid host-to-adapter input, either P1
   `PARSE_ERROR` case uses the null-id response above. A duplicate-bearing
   adapter output is instead a host-local `PARSE_ERROR`: the host sends no
   response, advances no canonical state, and quarantines the adapter. An
   unescaped raw newline in a JSON string terminates that physical frame, so the
   incomplete JSON line is `PARSE_ERROR` and the following line is validated
   independently. The receiver MUST NOT reinterpret stderr, concatenate
   adjacent lines, or answer malformed adapter output; adapter-output failures
   are recorded locally under the same code and direction rules above.

   After P1 succeeds, an adapter-to-host object in JSON-RPC request form is
   always prohibited direction, whether its `id` is valid, invalid, null, or
   absent. The host MUST classify it only as host-local P2 `INVALID_REQUEST`,
   send no JSON-RPC response, perform no action, advance no canonical state,
   close the connection, and quarantine the adapter. It MUST NOT reinterpret an
   absent-id wrong-direction request as a direction-valid notification or use a
   valid id to originate a response.

   After P1 succeeds, a host-to-adapter object in JSON-RPC response form is
   always prohibited direction, including when it also contains `method`,
   whether its `id` is valid, invalid, null, absent, or uncorrelated and whether
   `result` or `error` would otherwise be valid. The adapter MUST classify it
   only as adapter-local P2
   `INVALID_REQUEST`, send no JSON-RPC response or error, perform no action,
   advance no operation state, record the local direction fault, and close the
   connection. It MUST NOT apply the notification, null-correlation, or
   malformed-request response rules. The host MUST treat the resulting close as
   its own outbound protocol fault and MUST NOT quarantine, require release for,
   or blame the adapter solely for correctly closing on that host-originated
   wrong-direction frame.

2. **Size bounds (normative).** `MAX_MESSAGE_BYTES` is 1,048,576 bytes,
   including the JSON bytes but excluding the terminating `\n`.
   `MAX_IN_FLIGHT_REQUESTS` remains 32 total post-initialize requests per adapter
   connection. `MAX_IN_FLIGHT_DELIVERIES` is 28 and applies only to
   `runtime.deliver`. `MAX_IN_FLIGHT_CONTROL_REQUESTS` is 4 and is a reserved
   shared pool for `runtime.cancel`, `runtime.reconcile`, `runtime.health`, and
   `runtime.shutdown`. Deliveries MUST NOT consume a control slot, controls MUST
   NOT consume a delivery slot, deliveries MUST never exceed 28, controls MUST
   never exceed four, and their sum MUST never exceed 32. Thus 28 deliveries
   plus four controls is admissible; a 29th delivery or fifth control receives
   `TOO_MANY_IN_FLIGHT` even if the other pool has unused slots. The sole
   `initialize` handshake request is serialized before normal work, does not
   compete for either post-initialize pool, and cannot overlap another method.
   The same pools and bounds apply on the one recovery connection in Clause 12,
   where only its permitted post-initialize control methods are admissible.
   The reader MUST stop buffering a frame after
   `MAX_MESSAGE_BYTES + 1` bytes. An oversized frame MUST receive
   `MESSAGE_TOO_LARGE` when a response is possible and be
   discarded without unbounded buffering. A request above the in-flight limit
   applicable to its pool or above the total limit MUST receive
   `TOO_MANY_IN_FLIGHT`; it MUST NOT be queued in a host or adapter buffer and
   MUST begin no action.

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
   An operator-authorized recovery connection under Clause 12 MUST perform this
   same exact trusted handshake. Recovery authorization cannot supply, replace,
   relax, or bypass any initialize, manifest, registry, endpoint, capability-
   set, version, deadline, or echo validation.

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
   independent revisions against the trusted registry and profile.

   For a session-method invocation, successful P6 authorization requires the
   reviewed repository-trusted local registry/profile to contain exactly one
   versioned session-action relation for that exact invoked key
   (`EndpointV1.capability_set_id`, exact `CapabilitySetV1.revision`, invoked
   session method), where the method component is exactly one of
   `runtime.deliver`, `runtime.cancel`, or `runtime.reconcile`. Each relation
   row MUST select exactly one capability token that occurs exactly once in
   that exact `CapabilitySetV1`. The selected entry MUST be non-`unsupported`;
   its frozen S2 quality and attestation requirements MUST hold, and the
   invocation MUST satisfy every declared constraint. The key's capability-set
   id and revision are the exact values already bound at initialization. The
   relation and its selected token are trusted host-local registry/profile
   authority, never a JSON-RPC, S2 schema, request, result, transport,
   workflow-pack, or other caller field.

   A trusted profile MAY omit a relation for any or all of the three session
   methods. Zero rows for a method is a valid unsupported endpoint
   configuration and MUST NOT fail initialization; an invocation of that absent
   method nevertheless fails at P6 as `CAPABILITY_NOT_DECLARED` before action.
   Initialization does not require rows for uninvoked session methods.

   P6 MUST fail as `CAPABILITY_NOT_DECLARED` before action when the exact key
   has zero or multiple rows; the row is stale, unregistered, caller-supplied,
   or bound to another set/revision; the selected token is absent or duplicated
   in the exact set; or the selected entry is unsupported or fails quality,
   constraints, or attestation validation. There is no default row, prefix
   match, method-name equality, capability-name convention, or fallback.
   Multiple session methods MAY select the same existing capability token only
   through distinct explicit reviewed rows for their three distinct keys. This
   protocol therefore freezes deterministic selection without creating a
   universal capability-name vocabulary or reinterpreting S2 capability
   identities.

   Action authorization and evidence authority are independent. For every
   `StateEvidenceV1` carried by the applicable `SessionRefV1`, `DeliveryV1`, or
   `ReceiptV1`, P6 MUST separately treat
   (`authority.capability_profile_id`,
   `authority.capability_profile_revision`) as the evidence's own exact profile
   key. The id MUST occur exactly once as an existing capability token in the
   initialized `CapabilitySetV1`; the revision MUST equal that exact set's
   `revision`; the pair MUST be registered in the trusted local profile for the
   endpoint adapter; and the entry's non-`unsupported` quality ceiling,
   constraints, and adapter-bound attestation MUST satisfy the frozen S2
   validator, including authoritative requirements for exact-session binding
   and positive delivery truth. Missing, duplicate, stale, cross-set/revision,
   unregistered, unsupported, quality-escalating, constraint-violating, or
   unattested evidence profiles fail only at P6 as
   `CAPABILITY_NOT_DECLARED`.

   The session-action relation selects permission to invoke the method; an
   evidence profile selects authority for that evidence. A session-binding
   evidence profile MAY differ from the session action's selected capability,
   and the host MUST NOT infer equality from the method, evidence kind, or
   shared adapter. Likewise, the `runtime.reconcile` action selection and the
   returned `ReceiptV1` evidence profile are validated separately. Equality may
   be required only by a separate explicit reviewed local coupling relation;
   the action-selection row alone does not imply it.

   For `SessionRefV1` evidence, adapter `authority.identity` and
   `authority.implementation_revision` were validated exclusively at P5. For
   `DeliveryV1` and `ReceiptV1`, adapter/evidence cross-identities remain P7
   checks. P6 validates the selected capability entry's attestation
   `source_id`/`source_revision` against `EndpointV1.adapter_name`/
   `adapter_revision`; it MUST NOT use capability-profile fields to reopen P5
   or use action selection to bypass P7.

   Connection-scoped health and shutdown remain bound to the exact initialized
   endpoint, manifest, and capability set without accepting a session selector
   and without any session-action relation or product-capability lookup. A P6
   request failure receives `CAPABILITY_NOT_DECLARED`; a P6 response/result
   failure is host-local and receives no response. The host MUST NOT fall back
   to another method, capability, endpoint, capability set, project, workspace,
   session, relation row, or evidence profile.

7. **Exact session binding (normative).** The post-initialize session methods
   `runtime.deliver`, `runtime.cancel`, and `runtime.reconcile` MUST carry one
   complete `SessionRefV1`. P3 MUST first validate its closed schema shape. At
   P5, the host MUST then validate only its exact trusted registry/session
   identity against the initialized `EndpointV1`: `workspace_id`, complete
   discriminated scope, `endpoint_id`, `session_ref_id`, `native_session_id`,
   and `repository_binding` when present. A project-scoped session MUST contain
   the endpoint's exact non-null `scope.project_id`, and any repository binding
   MUST contain that same project. A workspace-scoped session MUST omit
   `scope.project_id` and MUST NOT carry a repository binding.

   P5 MUST also validate that the embedded binding evidence has valid frozen S2
   integrity; exactly matches the session's workspace and scope; has
   `evidence_kind: "exact_session_binding"`, `quality: "authoritative"`, and
   `authority_kind` of `native_runtime` or `trusted_adapter`; names exactly the
   session's endpoint, session-ref, native-session, and optional repository
   binding in its subject; and has `authority.identity` and
   `authority.implementation_revision` exactly equal to
   `EndpointV1.adapter_name` and `EndpointV1.adapter_revision`.

   P5 MUST NOT validate or classify
   `authority.capability_profile_id`,
   `authority.capability_profile_revision`, the trusted capability-profile
   revision, the session-action relation, or any `CapabilitySetV1` entry,
   quality, constraints, or attestation. Those fields and authorities are
   validated exclusively at P6 under Clause 6. Therefore an otherwise
   identity-valid session whose binding evidence names a missing, stale,
   mismatched, unsupported, or otherwise invalid capability profile is
   `CAPABILITY_NOT_DECLARED`, never `INVALID_SESSION_REF`.

   Wildcards, prefixes, display names, window order, `latest`, inferred cwd,
   inferred project, null identity, scope downgrade, and project substitution
   are prohibited. A missing, mismatched, stale, inferred, or non-authoritative
   P5 session identity or binding fact MUST return only
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
   MUST agree. At P6 the `runtime.deliver` action relation, session-binding
   evidence profile, delivery evidence profile, and returned receipt evidence
   profile MUST each pass their independent Clause 6 validation; none is
   inferred from or required to equal another. After params/schema, session,
   and capability validation pass, a cross-identity request or result is
   `INVALID_DELIVERY` at P7. A request failure receives that error; an adapter
   result failure is host-local, receives no response, preserves possible
   acceptance as unresolved, and MUST NOT advance canonical delivery state.

9. **Cancellation (normative).** The `runtime.cancel` method MUST name the exact
   original delivery request through the closed `original_request_id` param,
   use its own distinct request `id` under Clause 1, and carry the same exact
   `SessionRefV1` as the original request. At P6 its exact
   `runtime.cancel` action relation and the session-binding evidence profile
   MUST validate independently under Clause 6. The cancel invocation's only
   success result is the closed `{original_request_id, status:"cancelled"}`
   object; it is not a `REQUEST_CANCELLED` error. After that success, the
   original pending delivery request MUST terminate with the
   `REQUEST_CANCELLED` JSON-RPC error using the original request's id.
   Cancellation is idempotent: repeated cancellation of a request
   authoritatively cancelled before external acceptance MUST return the same
   cancel success result, while the original request remains terminally
   cancelled. Cancellation MUST NOT claim success when acceptance may have
   occurred; that cancel invocation MUST return `RECONCILIATION_REQUIRED`,
   preserve the original delivery and attempt as unresolved, and prohibit
   retry until reconciliation determines authoritative not-accepted evidence.
   Invalid cancel params return `INVALID_PARAMS` before action. A missing,
   additional, or mistyped cancel result member is host-local
   `INVALID_REQUEST` at P3, receives no response, advances no cancellation or
   delivery state, and leaves possible acceptance unresolved.
   `RECONCILIATION_REQUIRED` is reserved for a validly shaped cancel response
   or execution outcome that establishes that acceptance may have occurred.

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
    observations represented by that valid receipt and evidence. P6 MUST
    independently validate the `runtime.reconcile` action relation and every
    relevant session/receipt evidence profile; it MUST NOT infer that the
    returned evidence profile equals the action's selected capability. A
    missing, additional, or mistyped reconciliation result member is host-local
    `INVALID_REQUEST` at P3; an embedded `ReceiptV1` shape failure is host-local
    `INVALID_PARAMS` at P3. A validly shaped receipt whose capability profile,
    revision, quality, constraints, or attestation fails is host-local
    `CAPABILITY_NOT_DECLARED` at P6. Only after P6 succeeds does a delivery,
    attempt, endpoint, session, or other canonical-ledger identity mismatch
    become host-local `INVALID_DELIVERY` at P7. Only a validly shaped,
    capability-valid, identity-matching receipt whose authoritative observation
    is absent, ambiguous, non-authoritative, or contradictory is host-local
    `RECONCILIATION_REQUIRED` at P7. Each such response failure receives no
    response, advances no canonical state, and keeps the attempt unresolved or
    quarantined. A reconciliation request with invalid params returns
    `INVALID_PARAMS` before action. Neither side may synthesize success, choose
    another session, or blindly resend.

    When Clause 12 admits a recovery connection for a quarantined or unhealthy
    adapter, `runtime.reconcile` is admissible only when its exact
    `original_request_id`, delivery id, attempt id, session, endpoint, workspace,
    and scope identify one unresolved attempt explicitly named in the governing
    quarantine or unhealthy record. The request remains subject to every P3-P7
    validation and the four-slot control pool; operator authorization supplies
    no identity, capability, evidence, or outcome authority. After P3-P7
    succeeds, a reconciliation that is unrelated to or does not exactly match a
    named record attempt receives `ADAPTER_QUARANTINED`, or
    `ADAPTER_UNHEALTHY` when quarantine is not also active, at P8 and performs
    no action. A valid recovery reconciliation resolves only its named attempt
    and does not clear quarantine or unhealthy state.

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
    `ADAPTER_UNHEALTHY` to new work. The host MUST create or update an operator-
    visible unhealthy record for the exact adapter, manifest, profile, endpoint,
    reason, timestamps, and every unresolved attempt eligible for recovery,
    using the exact identities required by Clause 12. It MUST stop assigning
    normal requests until Clause 12's recovery and separate explicit operator
    release complete. When quarantine is not also active, blocked normal work
    receives `ADAPTER_UNHEALTHY`; only the single operator-authorized recovery
    connection may admit the bounded recovery controls. Successful recovery
    health responses contribute to the required fresh healthy sequence but MUST
    NOT automatically clear unhealthy or quarantine state. An invalid health
    request returns `INVALID_PARAMS` before action. A missing,
    additional, or mistyped health result member is host-local
    `INVALID_REQUEST` at P3, receives no response, advances no health state, and
    counts as one failed response. An identity or independent-revision mismatch
    is classified only at its later pipeline stage and also counts as one
    failed response; the threshold may make future work `ADAPTER_UNHEALTHY`,
    but it does not replace the current response's first-failure code.

12. **Quarantine and recovery (normative).** The host MUST quarantine an adapter
    for unsupported version drift, trusted-manifest mismatch, capability or
    exact-session contract violation, a duplicate-bearing adapter output, a
    prohibited adapter-to-host request including one with an absent `id`, stderr
    overflow, redaction failure, repeated closed-envelope, result-shape, or
    other invalid protocol output, or unresolved possible acceptance. The
    health failure threshold creates Clause 11's unhealthy state; an
    independent quarantine trigger may make both states active. Quarantine MUST
    create an operator-visible record
    containing adapter id, manifest and adapter revisions, initialized endpoint
    and capability-set identities/revisions, reason code, correlation ids,
    affected session ids, and the exact original request, delivery, and attempt
    ids for every unresolved attempt when applicable, bounded stderr byte and
    truncation counts when applicable, evidence references, and timestamps after
    redaction. Quarantine and unhealthy state MUST NOT auto-clear on reconnect,
    process restart, successful reconciliation, handshake, or health response.

    While either state is active, ordinary connection and request admission is
    closed. An operator MAY explicitly authorize one recovery-only connection
    tied to one exact quarantine or unhealthy record and its exact adapter,
    manifest, profile, endpoint, workspace, and scope. The host MUST admit at
    most one such connection; it MUST reject and terminate any concurrent or
    not-separately-authorized replacement recovery connection under the
    applicable adapter-state error, with `ADAPTER_QUARANTINED` taking precedence
    when both states apply. A replacement after connection loss requires a new
    explicit operator authorization and still cannot coexist with another
    recovery connection. The operator authorization is admission authority only:
    it is not a release, capability grant, identity source, evidence source,
    reconciliation outcome, or second canonical ledger.

    The recovery connection MUST first complete only the exact trusted
    `initialize` exchange from Clause 4. That serialized handshake does not
    consume a post-initialize control slot. After it succeeds, the only
    admissible methods are `runtime.reconcile` for an unresolved attempt
    explicitly named in the governing record, `runtime.health`, and
    `runtime.shutdown`. They share Clause 2's four reserved control slots and
    remain subject to P3-P7, deadlines, and every ordinary closed-envelope rule.
    After P3-P7 succeeds, `runtime.deliver`, `runtime.cancel`, unrelated
    reconciliation, and any other normal work remain blocked with
    `ADAPTER_QUARANTINED`, or `ADAPTER_UNHEALTHY` when quarantine is not also
    active. No blocked request is queued, no delivery slot becomes a control
    slot, and recovery success does not admit normal work.

    Final release is a separate explicit operator action. It is legal only
    after the exact manifest and capability profile, redacted diagnostics, and
    bounded-diagnostic behavior are reviewed; every unresolved attempt named in
    the governing record is reconciled to authoritative truth; the recovery
    connection completes a fresh exact handshake; and three consecutive valid
    `runtime.health` responses form a fresh healthy sequence at
    `HEALTH_INTERVAL_MS`. Until that action, applicable requests continue to
    receive `ADAPTER_QUARANTINED` or `ADAPTER_UNHEALTHY`.

    A connection close caused only by a duplicate-free prohibited host-to-
    adapter response or a direction-valid host request missing `id` is not
    adapter-originated quarantine evidence. The host MUST record its own
    outbound protocol fault and MUST NOT quarantine, require operator release
    for, or assign either fault to the adapter solely because the adapter made
    the required close; independent adapter-originated evidence may still
    satisfy this clause.

13. **Structured errors (normative).** Every direction-valid host-to-adapter
    request failure for which JSON-RPC permits a response MUST use the exact
    closed error envelope and error-data shape above and one numeric code from
    the following closed enumeration. Every adapter output failure MUST be
    recorded locally under the same enumeration and ordered pipeline, but the
    host MUST NOT send a response to that output. In particular, a
    duplicate-bearing adapter output is host-local P1 `PARSE_ERROR`, while a
    duplicate-free prohibited adapter-to-host request is host-local P2
    `INVALID_REQUEST` regardless of id validity. A duplicate-free prohibited
    host-to-adapter response is instead adapter-local P2 `INVALID_REQUEST`
    regardless of id or correlation validity; the adapter records that code and
    closes but sends no JSON-RPC response or error, and the host records its own
    outbound fault rather than an adapter fault. The same adapter-local P2
    `INVALID_REQUEST`, no-response, local-record, and host-owned-fault behavior
    applies to a direction-valid host request missing `id`; it cannot use the
    null-correlation error envelope. `error.data.retryable` MUST be the exact
    JSON boolean in the selected row. The `message`, `data.name`, and
    `data.retryable` values MUST all match that row exactly. The first failing
    pipeline step is the sole classifier; free-form strings, stderr, exit
    status, later validation failures, or host-specific exceptions are not
    substitutes for or additions to the selected code.

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
    `ADAPTER_UNHEALTHY` normal work may be retried only after Clause 12's fresh
    handshake, exact reconciliation, healthy sequence, and separate operator
    release. `REQUEST_TIMEOUT` and `RECONCILIATION_REQUIRED` are not resend
    permissions and require reconciliation rather than retry.
    `STDERR_LIMIT_EXCEEDED` and `ADAPTER_QUARANTINED` remain non-retryable
    pending explicit operator release. Operator-authorized recovery admission
    for the exact Clause 12 controls is not a retry permission for a blocked
    normal request and does not change any table literal. Every other `false`
    row remains non-retryable under its defining clause.

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
    binding, and it remains an admissible four-slot control on the one recovery
    connection in Clause 12. Its params MUST be exactly `{}`. On a valid
    request, the host and adapter MUST enter shutdown before returning exactly
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
    capability grant, session-action relation/key/selected token, evidence-
    profile registration, adapter-id alias, session selection by pattern, or
    any session action not backed by the exact trusted P6 relation and declared
    capability. A caller-supplied mapping or evidence authority is not made
    trusted merely because its values match a registry row. The
    mandatory connection-scoped health and shutdown controls are selected by
    the host under the successfully initialized protocol, not by caller input
    and not by a product-capability grant. The host MUST reject each prohibited
    session-action relation/key/selection or evidence-profile registration only
    as `CAPABILITY_NOT_DECLARED` at P6. It MUST reject each other prohibited
    input as `UNTRUSTED_MANIFEST_INPUT`, `INVALID_SESSION_REF`, or
    `CAPABILITY_NOT_DECLARED` at its ordered stage, before spawn or dispatch.
    It MUST NOT sanitize the input into authority, merge it with trusted
    configuration, invoke a shell, broaden a capability, or select a fallback
    runtime action.
