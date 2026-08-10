# Starting And Running A Collab Thread

## Goal

Everything a worker needs to join a collaboration, exchange packets, and get work
reviewed — in the order you actually need it, with the failure modes that have
really happened.

`session-startup.md` covers the *environment*. This covers the *collaboration*.

## Choose the worker surface

BB is the normal path for provider-backed worker assignments. Use
[`bb-workers.md`](bb-workers.md) as the single authority for worker spawn,
isolation, communication, inspection, and completion proof; do not reproduce
its commands here. A BB worker is not registered in `agents.json`, bound to an
llm-collab session, receipt-bearing, or a canonical-bus participant. Its
orchestrator remains the integration point and communicates with it through BB.

The former Codex Thread Coordination worker route is dormant. It is the legacy
`codex -> codex` self-target path, and the Codex app is no longer a worker
surface. Do not create or drive workers with `read_thread` or
`send_message_to_thread`. If legacy `deliver.py` output reports
`thread_coordination_required`, treat that banner as compatibility output and
use the BB workflow above; retiring the live banner is a separate code change.

Stop here and use that workflow for an ordinary worker assignment. Continue
below only when the work explicitly requires a first-class durable-mailbox
participant with its own project/chat/agent coordinates.

The cold start below applies only to those first-class mailbox participants.

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

**A new collab session is new for _everyone_.** It is not the reuse of whatever
binding happened to be lying around. Each worker starts a fresh native session and
registers *its own* — the initiator never discovers or registers a co-worker's
session, because a guessed id binds the wrong thread. The initiator registers
only itself and hands each co-worker an exact setup prompt for the part only they
can do. Freshness is a convention each worker follows: ordinary registration
REFUSES a registration whose native id already backs a dispatchable lease
(active, or the default `parked` when unexpired) in a different `(project_id,
chat_id)` scope — the same key exact dispatch resolves by, so two projects that
reuse one `chat_id` are also refused (GH-468). Start a fresh native session per
scope and deactivate an old lease before reusing its native session.

**The one-command path** does the initiator's share and prints those prompts:

```bash
# From the deployed runtime or a fresh origin/main worktree — NEVER a parked/dirty
# operator checkout (it refuses if this checkout is behind origin/main, so a
# co-worker cannot be pointed at stale code).
python bin/new_collab_session.py \
  --project demo-app --title "Checkout refactor" \
  --me codex --my-runtime-session-id 019f9452-... --my-runtime-family codex_app \
  --with claude:claude_app --repo-target app
#  -> creates the chat, registers ONLY codex's own session, prints codex's own
#     pickup command (branched by wake channel), and a setup prompt for claude.
```

Co-worker families are explicit (`agent:family`), never guessed. This helper
supports the discover-runtime families only — `codex_app`, `claude_app`,
`gemini_cli`. Pi workers (glmpi/relay/kimi) use `worker.py start-livecraft-pi` and a
human-relay (zcode) has no native session, so both are refused here rather than
misbound.

Share each printed prompt with its worker. The initiator's own pickup command —
and each co-worker's — is **branched by that agent's wake channel**: a
watcher-backed worker — claude, gemini, and Codex alike — arms its own inbox
watcher, and routine exact-session dispatch wakes it. For OpenAI-model work,
focus is BB until the Codex app reaches parity with the Claude app and BB. AX
applies only when the task needs a Codex-app-only tool that BB cannot reach;
`deliver.py` printing a command does not satisfy that condition. Whether an
allowed command can land is a runtime property checked for that attempt, never
a standing process or window-count fact. See the
[`AGENTS.md` standing routing rule](../../AGENTS.md#bb-worker-surface). **Do your own pickup
step** (the helper prints yours first); a packet you never see is a packet you
never answer.

The manual steps below are exactly what that helper automates; run them by hand
only when you need to diverge from the defaults.

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

#    A managed PM2-backed worker must already have its dispatching watcher.
#    STOP on failure and complete pm2-log-rotation.md before registering:
python bin/pm2_watchers.py status --agent codex

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

For a workspace's first watcher-enabled bootstrap, complete the canonical
[PM2 Log Rotation workflow](pm2-log-rotation.md) first. The command below may
restart the agent's PM2 watcher.

```bash
<runtime_root>/bin/llm-collab current_runtime.py --agent <agent_id>
```

Use the deployed runtime root (normally
`~/.local/share/llm-collab/runtime/main`), never a parked source checkout.

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
export LLM_COLLAB_READER_RUNTIME_FAMILY=<native-runtime-family>  # (GH-468) reader adopts its real family
<runtime_root>/bin/llm-collab watch_inbox.py \
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
agent with a previously verified Livecraft profile in that project, start and bind
it with one command:

```bash
bin/llm-collab worker.py start-livecraft-pi \
  --agent <agent-id> \
  --project <project-id> \
  --chat <CHAT-ID> \
  --repo-target <repo-id>
```

For the first profile in a project, provide the profile and exact Pi runtime
home explicitly instead of fabricating history:

```bash
bin/llm-collab worker.py start-livecraft-pi \
  --agent <agent-id> --project <project-id> --chat <CHAT-ID> \
  --repo-target <repo-id> \
  --provider <provider-id> --model <model-id> --thinking <level> \
  --runtime-home <absolute-pi-runtime-home>
```

`start-livecraft-pi` checks the shared Livecraft backend, creates a fresh native
session, restores the agent's pinned provider/model/thinking profile, verifies
the exact project/repository scope and native fingerprint, and waits for the
worker's starter handshake before registering the canonical binding. The
Livecraft host owns background wakes; the worker does not start a foreground
event monitor. It fails closed instead of guessing when the profile is
ambiguous, corrupt, unreadable, or newer than the last complete fingerprint.
The explicit first-profile form is the only zero-history exception.

The returned `verified=true` proves the canonical binding was created. Complete
setup with one disposable durable packet targeted through that binding; require
the worker to read it from `inbox.py` and reply through the same chat. The packet
is the work authority; the monitor event is only a wake pointer.

Nobody sends native IDs during ordinary collaboration. Senders use
`--project`, `--chat`, `--to`, and `--repo-targets`; `deliver.py` resolves the
exact active binding. A session switch, fork, reload, replacement, or app
restart invalidates the old session-owned monitor; start a fresh session rather
than reusing it.

Codex is watcher-backed like every other worker: routine exact-session dispatch
is its wake. For OpenAI-model interaction, use BB unless the task needs a
Codex-app-only tool that BB cannot reach. Only then may the conditional AX
procedure in `session-autobridge-runbook.md` apply, and only when `deliver.py`
prints the command. Its landing capability is checked live for that attempt.

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
export LLM_COLLAB_READER_RUNTIME_FAMILY=<native-runtime-family>  # (GH-468) reader adopts its real family
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
declares a repo scope and your packet declares none, `deliver.py` refuses before
the durable write — the recipient never sees it and no stranded packet remains.

This is not hypothetical: on 2026-07-25 it silently dropped **27 consecutive
packets** over eleven hours, and the lane only kept working because GitHub PR
comments were carrying the conversation. `deliver.py` now exits 2 with a typed
routing refusal naming both scopes. The fix is to declare the scope.

Prefer `--body-file` over inline text: long bodies and shell quoting do not mix.

For a Codex recipient, `deliver.py` may also print an `AX DOORBELL REQUIRED`
block. That output does not authorize AX by itself. If the task does not need a
Codex-app-only tool that BB cannot reach, use BB. If it does, run only the exact
printed command, and only when it is printed. Landing capability is a live
runtime property rather than a standing window-count claim.
A bound recipient reports `autobridge_ready: true` with
`watcher_pickup_ready: false`, and that is the routine success case. An unbound
watcher-backed recipient is refused before write. Exit 0 means the durable packet
was admitted; an admitted unbound broadcast always reports
`routing_mode: broadcast` explicitly.

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
a passing local verify, silence, or a low review tier.

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
| `deliver.py` exits 2 with `delivery_refused: true` | no exact target, AX fallback, or documented broadcast route was admitted | repair the binding or declare the right `--repo-targets`; no packet was written |
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
| `deliver.py` refuses an unroutable packet up front | Exit 2 with `delivery_refused: true`, `durable_write: false`, and a typed reason. Previously it wrote a packet that no exact-session watcher could read. |
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
