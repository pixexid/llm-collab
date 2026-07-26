# Starting And Running A Collab Thread

## Goal

Everything a worker needs to join a collaboration, exchange packets, and get work
reviewed — in the order you actually need it, with the failure modes that have
really happened.

`session-startup.md` covers the *environment*. This covers the *collaboration*.

## 0. Cold start — the operator asks two workers to collaborate

Collaboration does not start by itself. A worker asked to "work with zcode on the
checkout refactor" is, until these steps run, working alone: it has no chat to write
to and no way for anyone to reach it.

The sequence below was run end to end against a scratch workspace; the commands are
copied from that run, not composed from the help text.

**The operator names the work and the collaborator. Everything below is worker work** —
including registering the project. Do not hand setup back to the operator; if the
project or an agent is missing, that is yours to create.

**If the project is not registered yet**, follow
[`AGENTS.md` → Adding A Project](../../AGENTS.md#adding-a-project) before anything else.
It covers `projects.json`, the local state directory, repository-level guidance, and —
for a GitHub-backed project — materializing and validating the issue queue. An
unregistered project is refused by the tooling rather than guessed at, so this is not
optional. Missing agents go in `agents.json` the same way.

**Then, once per collaboration:**

```bash
# 1. create the chat — this is the work stream both workers will share
python bin/new_chat.py --project demo-app --title "Checkout refactor"
#    -> chat_id: CHAT-2F8529C5
```

```bash
# 2. EACH worker publishes its own runtime session against that chat.
#    First it reads its own thread id. This is read-only:
python bin/session_autobridge.py discover-runtime --runtime-family codex_app --json
#    -> {"session_id": "019f9452-...", "home": "/Users/you/.codex", ...}

#    Then it registers that EXACT id:
python bin/session_autobridge.py register   --session SESSION-CODEX-DEMO --agent codex   --project demo-app --chat CHAT-2F8529C5 --repo-target app   --mode auto-read --status active --wake-strategy runtime_trigger   --runtime-family codex_app   --runtime-session-id 019f9452-...   --runtime-home /Users/you/.codex   --runtime-session-source first_read
```

`publish-current` looks like the shortcut for this and **will refuse**
(`heuristic_runtime_discovery_refused`). Guessing which thread belongs to which worker
was retired deliberately: a wrong guess binds someone else's thread. Read the id, then
register it explicitly.

Repeat step 2 for the other worker, with its own `--runtime-family`
(`codex_app`, `claude_app`, `gemini_cli`) and its own session id.

**That is the whole "sharing a session id" question.** Nobody pastes an id to anybody.
Registration publishes it to the shared registry; `deliver.py` resolves the recipient's
binding automatically, and every packet carries both `Sender Session` and
`Target Session` so the record shows exactly which threads spoke. `codex_stream.py`
reads the same binding to attach to a peer's App Server.

```bash
# 3. now they can talk
python bin/deliver.py --project demo-app --chat CHAT-2F8529C5   --from codex --to zcode --repo-targets app   --title "Splitting the checkout work" --body-file /tmp/msg.md
#    -> "resolved_target_session_id": "zcode-thread-001", "autobridge_ready": true
```

### Two things that bite here

- **`discover-runtime` returns the most recently indexed session for that family**, not
  provably the caller's own. Check the `session_id` it prints against the thread you are
  actually in before registering it; registering the wrong one binds someone else's
  thread and every later refusal will point at the wrong cause.
- **Registration is per chat.** A worker registered on one chat is unreachable on
  another. Adding a second work stream means a second chat and a second registration.

## 1. Bootstrap

```bash
python bin/session_bootstrap.py --agent <agent_id>
```

Starts your inbox watcher and prints your identity, current project, and recent
mail. Run it once per session, before anything else.

## 2. Know your three coordinates

Every packet is addressed by **project**, **chat**, and **agent**. Get these wrong
and the message goes nowhere useful:

- `project_id` — from `projects.json`. Must be registered; an unregistered project
  is refused rather than guessed.
- `chat_id` — the conversation (`CHAT-XXXXXXXX`). One work stream per chat.
- `agent_id` — you, and your counterpart.

A worker often runs several concurrent sessions in different chats. **Reply in the
chat the request arrived on**; do not default every reply to the thread you happen
to be watching.

## 3. Read your inbox

```bash
python bin/inbox.py --me <agent_id> --project <project_id> --chat <CHAT-ID> --limit 5
```

Scope it. `--chat` matches a substring, and an unscoped read across a busy
workspace will surface another project's traffic.

## 4. Send a packet

```bash
python bin/deliver.py \
  --project <project_id> --chat <CHAT-ID> \
  --from <you> --to <them> \
  --repo-targets <repo-id> \
  --title "..." --body-file /tmp/message.md
```

**`--repo-targets` is not optional in practice.** If the recipient's session
declares a repo scope and your packet declares none, the packet is written to the
mailbox and then **refused at dispatch** — the recipient never sees it.

This is not hypothetical: on 2026-07-25 it silently dropped **27 consecutive
packets** over eleven hours, and the lane only kept working because GitHub PR
comments were carrying the conversation. `deliver.py` now prints a loud
`DURABLE WRITE OK — RUNTIME DISPATCH REFUSED` banner naming both scopes when this
happens, but the fix is to declare the scope.

Prefer `--body-file` over inline text: long bodies and shell quoting do not mix.

## 5. Verify it actually dispatched

`autobridge_ready: true` means a target session was resolved. It is **not** proof
of delivery. Confirm:

```bash
pm2 logs llm-collab-<recipient> --lines 5 --nostream | grep -E "new_message|refused"
```

`new_message` means it dispatched. `autobridge_repo_scope_refused` means step 4
went wrong.

## 6. Watch a peer work, live

```bash
python bin/codex_stream.py --agent <agent_id> --project <project_id> \
  --chat <CHAT-ID> --seconds 90
```

Streams that worker's real thread — its reasoning, the commands it runs, and its
reply as it types. This is the fastest way to see whether a peer is working,
stalled, or waiting on you, and it is read-only: it answers no server request and
sends no input.

`--chat last` selects the newest binding. `--raw` prints every notification as
JSON. `--seconds` bounds the run.

## 7. Getting reviewed

**Codex code review is manual only.** Automatic review is off; nothing arrives
unless someone asks. **Do not wait for a review nobody requested.**

Whether you must request one is decided by the Tier A/B/C rule in
[`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository).
Short version: credentials, authority, money, input we do not control, shared
contract changes, concurrency and partial state, migrations, and anything that can
weaken proof of those — **must** be requested. Formatting, comments and additive
tests must not.

Issue one *initial* request, on the head you believe is final:

```
@codex review for <every Tier A family the diff touches> at <exact head SHA>
```

State the SHA. A connector `+1` counts as CLEAN only while the head still equals the
SHA the request named, so a request without one cannot be satisfied by a reaction.

An amendment stales the review; request again on the new final head. Any finding
that arrives is adjudicated in writing whatever the tier — including a finding whose
thread you resolve, since the merge checklist only reads unresolved threads.

The one-initial-request limit is not a ban on ever asking twice. If the connector
silently drops your request and neither a verdict nor a reaction ever arrives, the
**single request-anchored re-trigger** in
[`commit-push-prs.md`](commit-push-prs.md) is the explicit exemption and the only
recovery — without it a Tier A head would sit pending forever.

## 8. When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| Peer never replies; their thread is idle | packet refused at dispatch | check the watcher log (step 5); re-send with `--repo-targets` |
| `deliver.py` prints `DURABLE WRITE OK — RUNTIME DISPATCH REFUSED` | packet scope not a subset of the recipient's | declare the right `--repo-targets` |
| `blocker: the recipient's binding could not be READ` | the binding file exists but is unreadable | repair or remove that binding; nothing will wake them until then |
| Waiting a long time for a bot review | nobody requested one | see step 7; auto review is off |
| `codex_stream` refuses with "not registered in projects.json" | project not registered, or wrong id | register it, or correct the id |
| `codex_stream` refuses naming two different homes | binding and session disagree on `CODEX_HOME` | re-register the session; the pair is torn |
| Your reply reached the wrong session | replied in a different chat than the request | reply in the originating chat |

## 9. Behaviour that changed recently

If your instructions predate these, they are wrong in ways that look like the tool being
broken. Each line is a merged change and the symptom it produces.

| Change | What you see if you did not know |
|---|---|
| Review is **manual only** | You wait indefinitely for a bot review that will never arrive. See step 7. |
| `deliver.py` refuses an out-of-scope packet up front | `autobridge_ready: false` with `route_ambiguous`, and a loud banner. Previously it reported success and dropped the packet silently. |
| New field `binding_unreadable_blocker` in `deliver.py` output | `true` means the recipient's binding exists but could not be **read**. Every wake flag is false and nothing will reach them until it is repaired or removed — this is the one refusal with no automatic fallback. |
| `codex_stream.py` exists | You can watch a peer's thread live instead of guessing whether it is working. Step 6. |
| `codex_stream` has **no** `--runtime-home` | The flag was removed: the home decides which App Server you attach to, so it comes from the validated binding and never from an argument. `session_autobridge register --runtime-home` is unaffected. |
| `codex_stream --thread` asserts rather than bypasses | It now requires `--agent` and `--project` and refuses if the named thread is not the one the binding resolves to. |
| `publish-current` refuses | `heuristic_runtime_discovery_refused`. Use `discover-runtime` then `register --runtime-session-id`. Step 0. |
| Registration writes the **binding before** the session | Chosen for its failure mode; see the comment in `session_autobridge.py`. |

### If you call the shared helpers directly

`load_binding()` raises **`BindingUnreadable`** for an oversized or I/O-failed binding —
deliberately *not* `FileNotFoundError`, which every caller treats as "no such binding".
Catch it, or it propagates. `read_regular_file_bounded()` raises **`UnreadableFile`** for
the same reasons plus a non-regular path. Both exist so a present-but-unreadable record
is never reported as an absent one, which sends whoever is debugging to the wrong cause.

## 10. Ending cleanly

Leave the lane so the next worker can pick it up:

- every request you received is answered, or has a written status
- work in flight is on a branch and pushed, never an uncommitted working tree —
  a clean-tree sweep will discard it
- anything you decided that is not obvious from the diff is written down in the
  packet or the PR, not only in your own memory

## See also

- `session-startup.md` — environment, tooling currency, preflight
- `commit-push-prs.md` — branch, commit, PR and merge gates
- `review-and-handoff.md` — review flow and terminal signals
- `../multi-project.md` — projects, repos, and boundaries
