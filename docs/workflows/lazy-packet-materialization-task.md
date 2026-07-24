# Option A task draft: lazy single-packet materialization

## Status

Draft for Claude stamp. Do not implement from this document until it is stamped
or restamped.

This is a receive-time compatibility bridge from one already-selected legacy
`Chats/` packet into the existing canonical message, delivery, and bound-attempt
rows. It is not a backfill, migration, cutover, inbox projection, read-state
rewrite, or new receive-claim authority.

## Proposed allowlist for the implementation slice

- `bin/_session_autobridge.py`
- `llm_collab/canonical/legacy_packet_materialization.py`
- `tests/test_session_autobridge.py`
- `tests/test_collabd_canonical.py`

`bin/_helpers.py`, `bin/watch_inbox.py`, schema migrations, `projects.json`,
runtime state, and inbox read-state storage stay out of scope. If the
implementation requires any of those files, stop and restamp before changing
them.

## Placement in the receive path

Materialization runs only after Slice 1 exact session selection succeeds:

1. `matching_unread_messages(session)` filters by exact project and chat.
2. `message_targets_session(session, message)` must return
   `(True, "explicit_target_match")`.
3. `binding_scoped_message_matches_session(session, message)` must have proven
   the session repo intersection, binding id, and binding generation when an
   exact runtime target requires them.
4. Only then may the implementation materialize the selected legacy packet and
   create a bound canonical delivery attempt.
5. Only after the bound-attempt step succeeds may the existing legacy read path
   mark the packet read.

The implementation must not materialize packets discovered only by
agent-scoped/broadcast fallback. It must not materialize nonmatching packets and
must not mark any packet read as a materialization side effect.

## Exact identity-derivation scheme

The immutable packet input is the exact packet file selected in step 2 above.
The canonical helper must derive all canonical identity from these immutable
facts:

- `packet_relpath`: the normalized path relative to the workspace root, under
  `Chats/`, with no absolute-path, `..`, symlink-re-resolve, or inbox-index
  authority.
- `packet_sha256`: SHA-256 of the exact packet bytes on disk before parsing.
- `packet_body`: the exact packet bytes. The canonical body is the durable
  packet, not the already-stripped message body.
- parsed frontmatter from those same bytes, used only after it agrees with the
  already-selected session: `project_id`, `chat_id`, `from`/`sender_agent_id`,
  `to`, `title`, `priority`, `tags`, `related_task`, `repo_targets`,
  `path_targets`, `sent_utc`, `target_session_id`, `target_binding_id`, and
  `target_binding_generation`.

The helper feeds `create_or_return_equivalent(...)` as follows:

- `workspace_id`: from the configured ledger/workspace root, not from the
  packet.
- `scope_kind`: `"project"`.
- `scope_identity`: the selected packet's `project_id`; it must equal the
  selected session's `project_id`.
- `sender_agent_id`: `"agent_" + sender`, where `sender` is
  `sender_agent_id` if present, otherwise `from`. If the derived value is not a
  canonical agent identifier, fail closed.
- `recipients`: exactly one item, `"agent_" + to`, and it must match
  `"agent_" + session["agent_id"]`. If either value is not a canonical agent
  identifier, fail closed.
- `body`: `packet_body`.
- `dedupe_key`: `legacy-packet:` plus the SHA-256 of
  `frame(packet_relpath) || frame(packet_sha256)`. The implementation should use
  one local framing helper equivalent to the canonical length-prefix discipline,
  not string concatenation.
- `registry_revision`: the current exact project registry revision admitted for
  this canonical write. This is stored and validated, but not relied on for
  message identity because `_derive_message_id(...)` does not include registry
  revision.
- `created_at_utc`: `sent_utc` from the packet, after UTC timestamp validation;
  missing or invalid timestamps fail closed.
- `title`: packet title, or a deterministic fallback derived from
  `packet_relpath`.
- `ttl_seconds`: `0`.
- `ack_policy`: `"none"`.
- `reply_to_message_id`: `None`.
- `priority`: packet priority if it is in the canonical vocabulary, otherwise
  fail closed. The implementation must not silently remap malformed priority
  text to `"normal"`.
- `tags`: packet tags plus a scoped marker such as
  `legacy_packet_materialized`; tags must remain data, not authority.
- `chat_link`: `chat_id`.
- `task_link`: `related_task` when present.
- `artifacts`: sorted data-only references:
  - `("chat", chat_id)`
  - `("path", packet_relpath)`
  - one `("repo", repo)` entry for each packet `repo_targets` member
  - one `("path", path)` entry for each packet `path_targets` member
  - `("task", related_task)` when present

`create_or_return_equivalent(...)` then derives `message_id` from the existing
canonical fields, including the packet bytes' body hash and the packet locator
dedupe key. Re-materializing the same packet feeds the same arguments and must
return `(same_message_id, False)` without new message/body/child rows.

The helper feeds `create_deliveries(...)` and the store's `_derive_delivery_id`
through the existing public wrapper:

- `workspace_id`, `scope_kind`, `scope_identity`, and `message_id`: the same
  values returned from message materialization.
- `routes`: exactly one route,
  `("agent_" + session["agent_id"], endpoint_id)`.
- `endpoint_id`: the endpoint from the resolved active conversation binding.
  It must agree with the selected session's canonical binding endpoint; if the
  selected legacy session record cannot prove that endpoint, stop/fail closed
  rather than inventing one from runtime metadata.
- `now_epoch_ms` and `created_at_utc`: deterministic receive-time values passed
  consistently within the single materialization attempt. A repeat with the same
  route must return the existing `delivery_id` because `_derive_delivery_id(...)`
  is based only on `(workspace_id, scope_kind, scope_identity, message_id,
  recipient_agent_id, endpoint_id)`.

Finally, the helper calls `create_bound_attempt(...)`:

- `message_id` and `delivery_id`: the existing or newly created canonical rows.
- `attempt_index`: deterministic for the selected packet/route. The first
  receive materialization uses `0`; retry after a crash between bind and
  mark-read must reuse `0` and return the existing frozen attempt. New attempt
  indexes are out of scope for this slice.
- `conversation_id`: packet `chat_id`.
- `participant_id`: the selected session's canonical conversation participant.
  The proposed deterministic legacy mapping is `"participant_" +
  session["agent_id"]`; the store's participant lookup must still prove that
  this participant maps to the recipient agent. If the implementation discovers
  that real selected sessions use a different stored participant id and there is
  no already-reviewed way to recover it, stop for restamp instead of falling
  back to raw `agent_id` or latest/frontmost session state.

`create_bound_attempt(...)` is the claim seam. It validates the participant,
resolves the binding inside the store transaction, checks endpoint agreement,
and freezes `(binding_id, generation)`. If it returns unresolved, the packet
stays unread and pending.

## Acceptance criteria

Claude's six adjudication conditions carry over verbatim:

1. Canonical ids derive deterministically from the immutable packet, path plus
   content hash, never minted per call. Materialize twice returns exactly one
   row set with identical ids.
2. Strict order: materialize -> `create_bound_canonical_delivery_attempt(...)`
   -> only then legacy mark-read. Bind failure leaves the packet unread and
   unconsumed. Include the crash-between-bind-and-mark-read recovery test:
   re-run is idempotent, has no duplicate rows, and the packet ends consistent.
3. Fail closed on unprovable packet path, project, chat, recipient, or repo
   scope, leaving the packet pending. Reuse
   `CONVERSATION_BINDING_RESOLUTION_REASONS`; no parallel reason set.
4. No bulk backfill and no cutover: one packet, at receive time. Existing
   unread packets are not mass-materialized.
5. Materialization must not mutate unread/read state as a side effect.
6. Mark-read remains consumption, never acceptance. Acceptance stays canonical
   receipt evidence.

## Intended mutation table

| Guard | Mutation | Expected failure |
|---|---|---|
| Deterministic message identity | Replace the packet-relpath+sha dedupe key with a per-call timestamp or UUID | Materialize-twice test sees duplicate message rows or changed `message_id` |
| Packet bytes are the body | Hash only stripped body text instead of exact packet bytes | Packet content mutation test returns the same body/message when it must diverge or conflict |
| Delivery idempotency | Add receive time to `_derive_delivery_id` inputs or route materialization arguments | Materialize-twice test sees duplicate delivery rows |
| Bound attempt idempotency | Increment `attempt_index` on retry after bind-before-mark-read crash | Crash-recovery test sees duplicate attempts or a second freeze |
| Exact selection ordering | Call materialization before `message_targets_session` / binding-scope success | Nonmatching binding or generation fixture creates canonical rows |
| Scope proof | Treat missing `project_id`, `chat_id`, recipient, repo target, endpoint, binding id, or generation as wildcard | Unprovable-scope fixtures are materialized instead of left unread |
| Reason vocabulary | Introduce a local skip/materialization reason not in `CONVERSATION_BINDING_RESOLUTION_REASONS` | Reason-vocabulary test fails |
| Read-state boundary | Let materialization call `mark_messages_read` or edit inbox JSON | Direct helper test observes read/unread mutation without legacy consumption |
| Mark-read ordering | Mark read after runtime success but before bound-attempt success | Bind-failure fixture is removed from unread |
| Mark-read is not acceptance | Append or project an accepted/completed receipt during materialization | Receipt-count/projection test sees acceptance evidence for a merely consumed packet |
| No backfill/cutover | Iterate all unread messages and materialize non-selected items | Existing-unread fixture gets canonical rows without exact receive selection |
| Existing guard durability | Broaden any sanctioned runtime consumer allowlist beyond this exact slice | Negative consumer fixture stops failing closed |

## STOP trigger assessment

No STOP trigger is known to fire in this draft:

- No durable packet-to-canonical link table or column is proposed. The link is
  represented by existing canonical message identity, data-only artifacts, and
  the dedupe key derived from immutable `packet_relpath` plus `packet_sha256`.
- No schema migration is proposed.
- No broad canonical ingest change is proposed; the helper materializes one
  selected packet at receive time.
- No backfill, cutover, inbox projection, or read-state rewrite is proposed.
- Option B is not required by the inspected seams. If implementation proves that
  the selected packet cannot produce an endpoint-backed canonical delivery and
  bound attempt without schema or a second claim authority, the slice must stop
  for restamp.

## Verification plan

- Focused canonical tests:
  `python3.11 -m unittest tests.test_collabd_canonical`
- Focused autobridge tests:
  `python3.11 -m unittest tests.test_session_autobridge`
- Feature/consumer guards if an import allowlist changes:
  `python3.11 -m unittest tests.test_standalone_feature_declarations`
- Full suite from the configured root before merge:
  `python3.11 -m unittest discover -s tests`
