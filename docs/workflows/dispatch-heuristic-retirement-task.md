# Dispatch heuristic retirement task

## Status

Draft for Claude stamp. Do not implement from this document until it is stamped
or restamped.

This is a design-only slice to remove heuristic runtime selection from
send-side mutation/dispatch paths. It is not a Codex-worker delivery pilot, a
worker cutover, a schema migration, or a new dispatch capability.

## Problem

Receive-side routing is now exact:

- Slice 1 made runtime receive selection binding-scoped.
- Option A, merged at `5020bf0c7a65657f3f06c87ad412a36098d65c24`, materializes
  one selected legacy `Chats/` packet into canonical message, delivery, and
  bound-attempt rows before runtime consumption.

Send-side dispatch can still choose a runtime session heuristically:

- `bin/deliver.py` calls `find_dispatchable_target_session(...)` with
  `require_exact_scope=args.activation`, so non-activation sends may select the
  first dispatchable session whose project/chat fields are missing or loosely
  compatible.
- `discover_claude_runtime_session(...)` chooses the newest Claude
  `sessions-index.json` entry by `fileMtime`.
- `discover_gemini_runtime_session(...)` chooses the newest Gemini
  `session-*.json` by filesystem mtime.
- `discover_codex_runtime_session(...)` chooses the newest Codex app session
  from `session_index.jsonl` (last object) or the newest history fallback, then
  can feed `register_session(...)` through publish mutation paths.

The task is to make those heuristic choices unreachable from mutation or
dispatch paths whenever exact runtime routing is required.

## Boundary

Do not start or enable Codex-worker delivery or pilot mutation. Issue #94 stays
blocked on #271. This slice may refuse unsafe runtime dispatch; it must not add
worker dispatch semantics to route around that refusal.

If implementation cannot express the gate as a pure refusal over existing
session/binding records, stop for restamp.

## Proposed implementation shape

### 1. Exact dispatch target gate

Add one small resolver in `bin/_session_autobridge.py`, conceptually:

```text
resolve_exact_dispatch_target(project_id, chat_id, agent_id)
```

It should:

1. Load the existing autobridge binding record with
   `load_binding(project_id, chat_id, agent_id)`.
2. Require binding `project_id`, `chat_id`, and `agent_id` to match the request.
3. Require both binding `session_id` and `runtime_session_id`.
4. Find dispatchable session records for that same `agent_id`.
5. Keep only sessions whose:
   - `session_id` equals the binding `session_id`;
   - `project_id` equals the requested project;
   - `chat_id` equals the requested chat;
   - `session_target_ids(session)` contains the binding `runtime_session_id`;
   - `session_is_dispatchable(session)` is true.
6. Return exactly one session. Zero or more than one is a refusal.

This resolver must not fall back to:

- missing/null project or chat fields;
- first session returned by directory order;
- `thread_pairs` as dispatch authority;
- mtime/newest runtime discovery;
- AX or desktop bridge as a way to call a runtime path anyway.

### 2. `deliver.py` placement

In `bin/deliver.py`, replace the current autobridge target lookup:

```python
find_dispatchable_target_session(..., require_exact_scope=args.activation)
```

with the exact resolver above for every non-`thread_coordination_required`
delivery where autobridge readiness is being considered.

Important ordering:

1. Validate chat/project.
2. Resolve sender session hints as today for packet frontmatter. Do not write a
   recipient runtime target hint unless the exact dispatch target gate succeeds.
3. Before setting `autobridge_ready`, call the exact dispatch target gate.
4. If it returns a session, set `autobridge_ready=true` and use the binding's
   runtime session id as the target session id.
5. If it refuses, keep the durable packet send path available but report
   `autobridge_ready=false` plus the refusal reason. Do not mark the runtime
   path ready and do not call `find_dispatchable_target_session(...)` as a
   fallback.

Durable mailbox delivery is not acceptance and not runtime dispatch. A refusal
here means "no exact autobridge wake", not "do not write the packet".

### 3. Runtime discovery mutation gate

Keep runtime discovery as a read-only diagnostic. It is useful for humans to see
what the legacy heuristic would have found, but it must not publish or bind that
heuristic result.

Make them unreachable from mutation paths:

- `bin/session_autobridge.py publish-current` must refuse for `codex_app`,
  `claude_app`, and `gemini_cli` if it would call heuristic discovery to
  create/update a session binding.
- `bin/inbox.py --publish-session` must make the same refusal for all three
  families.
- `bin/session_autobridge.py discover-runtime` may still call discovery because
  it only reports data and does not publish a binding.
- Exact binding through `bin/session_autobridge.py register
  --runtime-session-id ...` remains allowed because it does not infer identity
  from newest/mtime discovery.

This gate refuses only heuristic-fed publish mutation. It must leave open:

- read-only `session_autobridge.py discover-runtime`;
- exact bind via `session_autobridge.py register --runtime-session-id`.

## Refusal reason strings

Use a small fixed vocabulary. Do not add parallel free-form reason sets.

| Reason | Meaning |
|---|---|
| `exact_binding_required` | No binding record exists for the exact project/chat/agent, or the binding lacks `session_id` / `runtime_session_id`. |
| `exact_binding_not_dispatchable` | The bound session exists but is stopped, expired, superseded, or otherwise not dispatchable. |
| `exact_binding_ambiguous` | More than one dispatchable session claims the same exact binding identity. |
| `exact_binding_mismatch` | Binding payload fields disagree with the requested project/chat/agent or the matched session. |
| `heuristic_runtime_discovery_refused` | A mutation path tried to publish a Codex/Claude/Gemini runtime binding from newest/mtime discovery. |

`deliver.py` should expose these as an autobridge refusal/blocker field in its
JSON and human output. It should not silently downgrade the refusal to
`activation_unavailable` without preserving the exact reason.

## Function reachability

| Function | After this slice |
|---|---|
| `find_dispatchable_target_session(...)` | Retained for diagnostics and legacy read-only/tests, but unreachable from `deliver.py` autobridge readiness. If a future mutation caller needs it, that caller must explicitly justify why first-match is safe. |
| `discover_codex_runtime_session(...)` | Retained for `session_autobridge.py discover-runtime`; unreachable from `publish-current` and `inbox.py --publish-session`. |
| `discover_claude_runtime_session(...)` | Retained for `session_autobridge.py discover-runtime`; unreachable from `publish-current` and `inbox.py --publish-session`. |
| `discover_gemini_runtime_session(...)` | Retained for `session_autobridge.py discover-runtime`; unreachable from `publish-current` and `inbox.py --publish-session`. |

Proof should be structural:

- an AST or source guard that `bin/deliver.py` no longer calls
  `find_dispatchable_target_session`;
- tests that monkeypatch `find_dispatchable_target_session` to raise and prove
  a deliver autobridge path still succeeds/fails by the exact resolver;
- tests that monkeypatch Codex/Claude/Gemini discovery to raise and prove publish
  mutation paths refuse before calling them;
- tests that `session_autobridge.py discover-runtime` still calls discovery and remains
  read-only.

## Mutation table

| Guard | Mutation | Expected failing test |
|---|---|---|
| Exact binding required | Delete the binding load and let a wildcard session satisfy delivery | Non-activation deliver with only a wildcard session must report `exact_binding_required`, not `autobridge_ready=true`. |
| Exact project/chat | Allow missing or foreign `project_id` / `chat_id` on the session | Deliver with a foreign-chat bound session must fail with `exact_binding_mismatch` or `exact_binding_not_dispatchable`. |
| Bound session identity | Ignore binding `session_id` and match only runtime id | Test with a stale binding session id and a live same-runtime-looking session must refuse. |
| Runtime id identity | Ignore binding `runtime_session_id` and match only session id | Test with matching session id but different runtime id must refuse. |
| Single match | Return the first exact-looking session when two exist | Ambiguous duplicate fixture must fail with `exact_binding_ambiguous`. |
| Dispatchability | Skip `session_is_dispatchable` | Stopped/expired bound session fixture must fail with `exact_binding_not_dispatchable`. |
| Deliver callsite | Reintroduce `find_dispatchable_target_session(...)` in `deliver.py` | AST/source guard fails; monkeypatched first-match helper raises. |
| Thread-pair authority | Use `resolve_thread_pair_session_id(...)` as dispatch authority | Thread-pair-only fixture must not produce `autobridge_ready=true`. |
| Codex publish heuristic | Let `publish-current --runtime-family codex_app` call newest `session_index.jsonl` / history discovery | Publish mutation test must fail unless it returns `heuristic_runtime_discovery_refused`. |
| Claude publish heuristic | Let `publish-current --runtime-family claude_app` call newest `fileMtime` discovery | Publish mutation test must fail unless it returns `heuristic_runtime_discovery_refused`. |
| Gemini publish heuristic | Let `inbox.py --publish-session --runtime-family gemini_cli` call newest mtime discovery | Publish mutation test must fail unless it returns `heuristic_runtime_discovery_refused`. |
| Diagnostic retention | Remove read-only `session_autobridge.py discover-runtime` | Diagnostic test fails; this slice retires mutation reachability, not the diagnostic command. |

## Verification plan

Focused tests should stay in `tests/test_session_autobridge.py`:

- exact-binding deliver success;
- no-binding refusal;
- wildcard/first-match refusal;
- thread-pair-only refusal;
- stale binding/session mismatch refusal;
- duplicate exact binding refusal;
- stopped/expired bound session refusal;
- Codex/Claude/Gemini publish mutation refusal;
- read-only discovery still reports.

Also run the existing standalone feature/consumer guard if implementation
changes imports or `bin/` runtime consumers.

Full verification before merge:

```bash
python3.11 -m unittest tests.test_session_autobridge
python3.11 -m unittest discover -s tests
```

For isolated worktrees without `collab.config.json`, run the full suite from the
configured root with the implementation worktree first on the import path, as
used by the Option A slice.

## STOP-trigger assessment

No STOP trigger is known for this design:

- Existing binding files already contain `project_id`, `chat_id`, `agent_id`,
  `session_id`, and `runtime_session_id`.
- Existing session files already contain dispatch status and runtime metadata.
- No schema migration is required.
- No canonical worker delivery or #94 pilot behavior is introduced.
- The slice removes unsafe runtime readiness rather than adding a new runtime
  delivery path.

STOP and restamp if implementation discovers that:

- exact binding cannot be proven from existing binding/session records;
- durable mailbox delivery would have to fail entirely instead of refusing only
  autobridge readiness;
- a schema/table/generation field is needed;
- Codex-worker delivery semantics are needed to express the gate;
- heuristic discovery must remain in a mutation path for compatibility.

## Slice size

This fits one small implementation slice:

- one exact resolver/helper in `bin/_session_autobridge.py`;
- one `bin/deliver.py` callsite change;
- two publish-mutation refusals in `bin/session_autobridge.py` and `bin/inbox.py`;
- focused tests in `tests/test_session_autobridge.py`.

No migration is proposed. If the publish-mutation refusals prove separable
during review, they may split into a follow-up, but the deliver autobridge gate
is the first cut because it directly removes the unsafe dispatch path.
