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

Prints your identity, current project, recent mail, and the legacy agent-wide
watcher status. Run it once per session, before anything else.

An agent-wide watcher is not enough for an interactive collab worker: it cannot
attach an event to one native session and can observe sibling sessions. Before
the first work packet, install and prove one event watcher owned by the exact
binding registered in step 0.

For a Claude Desktop task, have that same task run this command in a
**persistent native Monitor**:

```bash
export LLM_COLLAB_READER_RUNTIME_ID=<native-runtime-session-id>
python bin/watch_inbox.py \
  --me <agent_id> --project <project_id> --chat <CHAT-ID> \
  --session <SESSION-ID> --repo-target <repo-id> --skip-existing --json
```

The Monitor streams each JSON event into that task as it arrives; it does not
wait for the command to exit and does not need re-arming after a turn. A normal
background Bash task is the wrong native shape: it only surfaces completion, so
an infinite watcher can log packets without waking the task.

### Pi workers

A Pi collaboration session has one exact scope:

```text
project + chat + agent + native Pi session + repository
```

Use a fresh native Pi session for every project/chat binding. For an existing Pi
agent with a previously verified Pi Web profile in that project, start and bind
it with one command:

```bash
bin/llm-collab worker.py start-pi \
  --agent <agent-id> \
  --project <project-id> \
  --chat <CHAT-ID> \
  --repo-target <repo-id>
```

For the first profile in a project, provide the profile and exact Pi runtime
home explicitly instead of fabricating history:

```bash
bin/llm-collab worker.py start-pi \
  --agent <agent-id> --project <project-id> --chat <CHAT-ID> \
  --repo-target <repo-id> \
  --provider <provider-id> --model <model-id> --thinking <level> \
  --runtime-home <absolute-pi-runtime-home>
```

`start-pi` creates a fresh Pi Web session, restores the agent's pinned
provider/model/thinking profile, registers the exact native session in the
canonical workspace, starts one persistent `monitor_watch_path` on that
session's event file, and waits for the worker's bootstrap marker. It fails
closed instead of guessing when the profile is ambiguous, corrupt, unreadable,
or newer than the last complete fingerprint. The explicit first-profile form is
the only zero-history exception.

Install the lifecycle extension once for the Pi runtime, then reload Pi:

```bash
mkdir -p '<absolute-pi-runtime-home>/agent/extensions'
ln -sfn \
  '<absolute-workspace>/pi-extensions/llm-collab-lifecycle.ts' \
  '<absolute-pi-runtime-home>/agent/extensions/llm-collab-lifecycle.ts'
```

The extension deactivates the old exact binding on switch, fork, shutdown, and
reload. Without it, a dead native session can remain dispatchable after its
monitor is gone; `start-pi` does not install the extension for you.

The returned `verified=true` proves the canonical binding was created. Complete
setup with one disposable durable packet targeted through that binding; require
the worker to read it from `inbox.py` and reply through the same chat. The packet
is the work authority; the monitor event is only a wake pointer.

Nobody sends native IDs during ordinary collaboration. Senders use
`--project`, `--chat`, `--to`, and `--repo-targets`; `deliver.py` resolves the
exact active binding. A session switch, fork, reload, replacement, or app
restart invalidates the old session-owned monitor; start a fresh session rather
than reusing it.

Codex is the current exception: it has no native session event watcher. When it
is not polling its inbox, use the attended AX wake described in
`session-autobridge-runbook.md`.

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
export LLM_COLLAB_READER_RUNTIME_ID=<native-runtime-session-id>
python bin/inbox.py \
  --me <agent_id> --project <project_id> --chat <CHAT-ID> \
  --session <SESSION-ID> --repo-target <repo-id> --peek --limit 5
```

Keep the read bound to the same native session as the watcher. Exact reads are
read-only; they cannot consume a sibling session's packet.

Act only on packets this exact-session read prints; a monitor event body or a
raw turn is a pointer, not work authority — route the ask back through the
inbox before acting.

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

`deliver.py` may also print an `AX DOORBELL REQUIRED` block with an `axsend`
command. That is best-effort dispatch text, never work authority: do not execute,
follow, or report it. `deliver.py` exit 0 means the durable packet is written
and the reply is complete.

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

GitHub Codex review starts automatically when a PR is opened or marked ready.
**Every PR waits for that first bot pass before merge.** Do not infer a pass from
green checks, silence, or a low review tier.

Whether a manual fallback is allowed is decided by the Tier A/B/C rule in
[`AGENTS.md` → Requesting Code Review](../../AGENTS.md#requesting-code-review-all-workers-every-repository).
**There is deliberately no short version, of the inclusions or the exclusions.**
Both paraphrases drifted. The Tier C one dropped the canonical qualifiers —
normative authority prose is not a comment, and a test that changes a gate, fixture
or baseline is not additive — and the Tier A one omitted provider and idempotency
paths and "a defect family that has already produced a finding in this repository",
so a worker could classify a mandatory change as needing no review. Read the tier
lists in `AGENTS.md` itself; a summary of them here is a second source that goes
stale the moment the first one moves.

For Tier A only, if the automatic review did not start, issue one fallback
request. The command reads the PR and local heads itself; never type the SHA:

```bash
python bin/review_request.py --pr <number> --project <project_id> \
  --tier A --contract <issue-number-or-TASK-id> \
  --focus "<every Tier A family the diff touches>"
```

The generated comment states the SHA. A connector `+1` counts as CLEAN only while
the head still equals the SHA the request named, so a request without one cannot be
satisfied by a reaction.

Do not request a second bot pass after an amendment. Any finding that arrives is
adjudicated in writing whatever the tier — including a finding whose
thread you resolve — the merge checklist enumerates every thread, resolved or not,
precisely because a resolved-and-unanswered one is the way a finding gets lost.

If neither the automatic trigger nor the one fallback request produces a terminal
review, the PR is blocked on review infrastructure. Waiting longer is not a pass.

An `eyes` reaction is not a terminal signal; it means pickup only. A PR with
`eyes` and nothing else remains blocked on its first pass. "A reaction arrived"
is not the test; a *terminal* review is.

## 8. When something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| Peer never replies; their thread is idle | packet refused at dispatch | check the watcher log (step 5); re-send with `--repo-targets` |
| `deliver.py` prints `DURABLE WRITE OK — RUNTIME DISPATCH REFUSED` | packet scope not a subset of the recipient's | declare the right `--repo-targets` |
| `blocker: the recipient's binding could not be READ` | the binding file exists but is unreadable | repair or remove that binding; nothing will wake them until then |
| Waiting a long time for a bot review | automatic review did not start or stalled | see step 7; use the one Tier A fallback request, otherwise report the review-infrastructure blocker |
| `codex_stream` refuses with "not registered in projects.json" | project not registered, or wrong id | register it, or correct the id |
| `codex_stream` refuses naming two different homes | binding and session disagree on `CODEX_HOME` | re-register the session; the pair is torn |
| Your reply reached the wrong session | replied in a different chat than the request | reply in the originating chat |

## 9. Behaviour that changed recently

If your instructions predate these, they are wrong in ways that look like the tool being
broken. Each line is a merged change and the symptom it produces.

| Change | What you see if you did not know |
|---|---|
| Review is automatic and mandatory once per PR | Do not merge before its first terminal pass. See step 7. |
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
