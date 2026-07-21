# Desktop Computer-Use Doorbell (Agent-to-Agent Comms)

This is the agent-to-agent communication workflow for collaborators running in
dedicated desktop apps (e.g. Claude in `/Applications/Claude.app`, Codex in
`/Applications/Codex.app`). It is intentionally separate from `claude --resume`,
`claude -p`, and other CLI/session-file flows.

> Supersedes the earlier one-directional model in which only Codex drove Claude
> and the sole wake mechanism was a Codex heartbeat. The doorbell is now
> **bidirectional and event-driven**: whichever agent finishes a unit of work or
> needs something rings the other immediately. The heartbeat survives only as a
> bounded, provisional **safety-fuse** (see below), not as the primary path.

This desktop-app workflow applies only between distinct collaborator app
identities. External workers such as Claude and ZCode may ring root Codex, and
root Codex may ring those external apps. It does not apply to `codex -> codex`,
root self-handoffs, or managed Codex workers.

## Communication tiers: llm-collab vs direct ax (choose by PURPOSE)

Two channels, picked by what the message IS — not by convenience:

- **llm-collab (durable / work channel)** — task delegation, handoffs, status
  changes, anything a worker must **act on as work**, long context, and anything
  that must **survive a thread/context loss**. It is the mailbox of record; read
  at the worker's own pace from the inbox. If it needs to be remembered,
  recovered, or done-as-work → it goes here.
- **Direct ax doorbell (real-time / thinking channel)** — discuss with a
  distinct app collaborator an implementation plan, expand/explore ideas,
  get **feedback before opening an issue or task** (don't open issues with zero
  prior planning/research), resolve
  a **decision between workers**, quick coordination. Ephemeral — NOT in the
  mailbox, so never put a task/decision/handoff *only* in a direct ax msg.

**Ask the right worker, not the operator.** Engineering / plan / decision
questions go to **Codex (or the worker who owns that area) over the direct
channel**, not to the operator. The operator decides product, visual/UX, scope,
and business calls — not technical implementation. Routing eng decisions to the
operator stalls the work (they can't adjudicate them).

**Queueing to a busy recipient is SAFE after composer-safety proof.** A routine
AX ring requires a provably empty native composer. Once that proof exists, do
NOT wait/poll for the other agent to be idle merely because `Stop`, `Running`,
or processing is visible. Send one AX doorbell on a supported distinct-app
route; if the target is busy, the message queues and is processed when its
current turn ends.
Queueing is *insurance* that the receiver gets the message even if it already
saw it on another channel; it does **not** corrupt the running turn (only a
forced steer would), and queued messages are cancelable on the receiver side.
A non-empty draft or unreadable, unprovable, or `AXValue`-opaque composer state
means hold and enter recovery—never infer empty. The target's busy state alone
is not a hold condition after the empty-composer proof. An ambiguous or unsafe
target likewise enters recovery. Never stack or re-ring the same pointer; if it
is already queued, one copy is enough.
Receiver discipline: when not running, read queued messages and ignore a queued
copy already handled from the inbox. This policy does not authorize a Codex
self-doorbell.

**Verification is enforced, not optional.** `ring --submit` verifies by default.
Exit 0 with `VERIFIED` confirms a visible conversation turn. Exit 0 with
`QUEUED (UNCONFIRMED)` means only that the recipient became busy during submit;
it does not prove the pointer landed in the intended thread. Preserve the
durable mailbox packet, record the unconfirmed state and follow-up, never
re-ring that pointer, and do not claim exact-thread delivery until later
`axsend confirm` or explicit recipient evidence shows the pointer in the native
thread. Inbox consumption proves only durable packet delivery, not AX-thread
delivery. Exit 7/not-delivered requires `axsend confirm`; if it confirms the
pointer is absent on the same proven target, one re-ring is allowed. Never
re-ring an exit-0 queued/unconfirmed result or an identity-loss/ambiguous
result. Other non-zero results enter recovery. NEVER use Computer
Use/screenshots to verify an AX send. Routine target composers are validated
for Codex and Claude Desktop. ZCode or Antigravity may originate a ring to a
supported target, but when either app is the target its `AXValue`-opaque or
otherwise unprovable composer requires hold and attended recovery; never use a
blind key-typed AX ring. Busy alone is not a hold after composer safety is
actually proved.

## Managed Codex and native subagent routing

Managed Codex workers are not AX desktop-app recipients. Create the managed
thread through Codex Thread Coordination, inspect it with `read_thread`, and
send focused unblocks with `send_message_to_thread`. Keep the durable task/chat
state in `llm-collab`, but never ring a Codex composer for `codex -> codex`.
`deliver.py` preserves such a durable packet, suppresses app activation, and
reports `thread_coordination_required: true`. The packet carries the persistent
frontmatter guard `autobridge_skip: true` with
`autobridge_skip_reason: codex_self_target`, and omits `target_session_id`.
PM2 and manual `watch_inbox.py --me codex` dispatch both honor that sender-aware
guard, leave the packet unread for durable recovery, and never runtime-trigger
a Codex session from it. The watcher also excludes older `from: codex` /
`to: codex` packets created before the persistent flag existed. External
Claude/ZCode-to-Codex packets do not receive the guard and keep their normal AX
or registered-runtime activation behavior.

Native subagents use native subagent coordination for bounded local support.
Do not route them through AX or Computer Use. Attended Computer Use is reserved
for supervision and recovery of external collaborator desktop apps; it is not
a managed-Codex task selector, wake path, or transport.

## Preferred doorbell transport: `axsend` (AX bridge, no focus steal)

Ring the other agent with the **Accessibility-API bridge** rather than
screenshot computer-use. Screenshot tools must raise the target window and steal
the operator's keyboard focus, and on overlapping windows they misroute
keystrokes. `axsend` sets the composer text and presses send through AXUIElement
**without raising the window or touching focus**, and reads replies without a
screenshot.

```bash
AX=/Users/pixexid/Projects/llm-collab/bin/axsend-ensure   # auto-builds if needed
$AX state --app Codex                                   # optional status + recent messages
$AX ring  --app Codex --submit --dry-run --text "x"     # confirm send target (new app/window)
$AX ring  --app Codex --submit --verify --text "[from <me>] <pointer to durable packet>"
$AX state --app Codex                                   # read the reply
```

Rules: deliver the durable packet with `deliver.py` FIRST (mailbox = truth);
prove the native composer is empty, then ring once even when the recipient is
busy. An unreadable, unprovable, or `AXValue`-opaque composer enters recovery.
`VERIFIED` exit 0 is confirmed;
`QUEUED (UNCONFIRMED)` exit 0 must be recorded and followed up without re-ringing
or claiming exact-thread delivery. Use `--dry-run` first on any new app and
`--verify` (non-zero means the text did not land; a post-send empty composer is
not proof of delivery). The idle input gate applies only to attended screenshot/keyboard
Computer Use fallback. Any collaborator (Codex, Claude, Gemini, ZCode) can call
`axsend-ensure` via shell for a distinct-app route, but Codex must not target
itself or a managed Codex worker. Needs the running process enabled in Privacy &
Security → Accessibility. Falls back to screenshot Computer Use only if AX
fails for an external-app target. Full reference:
`tools/axbridge/README.md` and the Claude Code `ax-doorbell` skill.

**Per-app routine target safety:**

| App | Composer write | Submit | Status |
|-----|----------------|--------|--------|
| Codex | `AXValue` | send-arrow `AXPress` | ✅ proven bidirectional |
| Claude Desktop | `AXValue` | `key-return` | ✅ proven |
| ZCode | `AXValue`-opaque | attended recovery only | ⚠️ routine ring holds because emptiness is unprovable |
| Antigravity (Gemini) | unreadable/unproven | attended recovery only | ⚠️ routine AX target unsupported until composer safety is provable |

This target-side hold is ENFORCED (GH-1547). The registry marks ZCode and
Antigravity `ax_attended_only`; `deliver.py` never emits a routine AX doorbell
for them and instead prints an explicit ATTENDED RECOVERY REQUIRED instruction
that routes control to Codex (never a silent mailbox-only fallback). The
`axsend` binary independently refuses a routine `ring` against an opaque
composer with exit 11 before any mutation; `--attended` (and the attended-only
`type` command) unlock key-event typing solely inside a Codex-supervised
recovery turn, with a loud warning.

Routine `ring` requires readable `AXValue` proof that the native composer is
empty. A rejected write, opaque value, unreadable state, or otherwise unprovable
composer does not authorize automatic key-event typing: hold and use the
attended recovery path. Low-level key-event typing may be used only within that
recovery path after composer safety is established; it is never a blind-send
escape hatch. Once safety is proved, recipient busy state alone does not require
an idle wait. The doorbell works between supported distinct app identities;
that capability does not make it a Codex-to-Codex transport.

## GH-135 update-survival decision and recovery evidence

**Decision state: `PENDING REAL-UPDATE EVIDENCE`.** Neither a stable
symlink/alias nor a stable helper has been selected. The current evidence does
not establish that either candidate is feasible or infeasible, and a process
restart or relaunch alone is not update-survival proof. GH-135 requires evidence
from a real app update before the decision can change.

### Confirmed version-path/TCC incident

- Claude Code continued running from the versioned `2.1.209` path after that
  version tree had been deleted and only `2.1.215` remained on disk.
- GH-127A surfaced the resulting failed AX trust state as `[ax] DOWN`.
- The durable mailbox remained the source of truth while the doorbell was
  unavailable.
- Demonstrated recovery required a full app quit and relaunch, operator
  Accessibility re-approval, and a successful `tools/axbridge/axsend check`.

That sequence is recovery evidence for the observed incident, not proof that a
candidate survives an update. Draft/composer-targeting failures, including the
GH-1547 target-side hold, are a different failure class and are not TCC
update-survival evidence.

### Operator-run evidence matrix for a genuine update

For each row, the operator records the same before-and-after fields around one
genuine application update. A blank or ambiguous field leaves that candidate
undecided.

| Candidate or control | Before the real app update | After the real app update | Visibility and outcome |
|---|---|---|---|
| Current versioned-path control | App version; executable path; resolved path; AX trust | App version; executable path; resolved path; AX trust | GH-127A status; live-ring result; rollback result; recovery result |
| Stable symlink/alias candidate | App version; executable path; resolved path; AX trust | App version; executable path; resolved path; AX trust | GH-127A status; live-ring result; rollback result; recovery result |
| Stable-helper candidate | App version; executable path; resolved path; AX trust | App version; executable path; resolved path; AX trust | GH-127A status; live-ring result; rollback result; recovery result |

Installation, launchd or service-lifecycle changes, TCC/Accessibility changes,
the genuine app update, and live-ring validation are operator-owned. This
protocol does not authorize an agent to perform those actions, install a
candidate, or change targeting/session semantics. Any future implementation
must be scoped separately after the evidence supports a mechanism decision;
this runbook intentionally specifies no code, signing, or IPC design.

## Two channels: mailbox + doorbell

- **Mailbox = `llm-collab` (durable source of truth).** Every task, handoff,
  blocker, clarification, decision, and piece of evidence is a file written with
  `deliver.py`. Nothing load-bearing lives only in an app's visible thread.
- **Doorbell = AX (immediate, event-driven nudge).** The moment one participant
  in a distinct-app route finishes
  a task, hits a blocker, needs a clarification, or completes a handoff, it uses
  `bin/axsend-ensure ring --submit --verify` (run from the llm-collab checkout
  root, or use the exact absolute command `deliver.py` prints)
  to send one short, sender-tagged pointer to the
  durable packet. Screenshot/keyboard Computer Use is a fallback only when AX is
  unavailable and the target path is explicitly configured for desktop bridging.

The mailbox is the record; the doorbell is the notification. A doorbell with no
corresponding mailbox packet is not valid for task-grade work.

## Sender identifier convention

Because agents now type directly into each other's apps, every agent-to-agent
message must carry a sender identifier so the recipient can attribute it. The
operator never tags their own messages.

- `[BRIDGE <8-char-uuid-prefix>] ...` — a durable bridge pointer routed via
  `llm-collab`.
- `[from <agent>] ...` or `[<agent> doorbell] ...` — a direct AX or fallback ring
  (there is no message frontmatter on a direct ring, so the inline tag is what
  disambiguates).
- **Untagged plain text is operator-origin by convention.** Treat a tagged
  message as peer-agent coordination; reply/hand back through the durable
  mailbox, not only the ring.

## Ringing the doorbell

For task-grade work, in order:

1. Write the durable instruction/handoff with `deliver.py` to `Chats/` and the
   recipient's `agents/<agent>/inbox.json`.
2. If sender and recipient are both `codex`, stop app routing and use Thread
   Coordination (`read_thread` / `send_message_to_thread`). Otherwise ring the
   distinct external-app recipient with
   `bin/axsend-ensure ring --submit --verify --text "<pointer>"`
   (from the checkout root; or the exact absolute command `deliver.py` prints)
   only after the native
   composer is provably empty. Once proven empty, ring even if the recipient is
   busy. A non-empty draft or unreadable, unprovable, or `AXValue`-opaque
   composer state means hold and recovery—never infer empty. The one-line
   pointer may queue behind the current turn, but the ring result must be
   classified as described below. Use exactly **one short,
   sender-tagged, one-line pointer** to the exact inbox/chat/message path as the
   `--text` value. Full context stays in the durable packet, never in the
   visible prompt.
3. Classify exit 0 by output: `VERIFIED` confirms delivery;
   `QUEUED (UNCONFIRMED)` does not. For queued-unconfirmed, keep the mailbox
   packet unresolved, record the blocker/follow-up, never re-ring, and wait for
   later `axsend confirm` or explicit recipient evidence that the pointer
   appeared in the native thread. Inbox consumption proves the durable packet
   was consumed but not that the AX pointer landed. For exit 7/not-delivered,
   run `axsend confirm`; if it proves absence on the same target, re-ring once.
   Never re-ring queued/unconfirmed or identity-loss/ambiguous results; route
   other non-zero results into recovery.
4. Use screenshot/keyboard Computer Use only as attended fallback or recovery
   for an external collaborator app when `axsend` is unavailable or unsafe. In
   that fallback path, pass the idle input gate before typing.
5. Record the ring result in your own thread and, when relevant, in the mailbox.

For non-task ad-hoc chat, the mailbox is optional, but a sender tag is still
required. Prefer `axsend` only on a supported distinct-app route; the idle gate
applies only to attended screenshot/keyboard Computer Use fallback for an
external collaborator app.

## Idle input gate (Computer Use fallback only)

This gate applies only when attended screenshot/keyboard Computer Use must type
into an external collaborator app. It does not apply to the focus-independent
AX ring above: a visible `Stop` or processing state alone must not cause an AX
idle wait. Before a Computer Use fallback send, confirm ALL of:

- the recipient's active thread/sidebar row is not `Running`
- the composer is empty and focused
- no visible `Stop` button for an active turn
- no visible queued messages with a `Remove queued message` affordance
- no transitional setup text such as `Creating worktree...`

If any item fails, do not type. Wait briefly and re-check, or record the blocker
in the mailbox. Typing into a running turn can fragment the message into queued
chunks and corrupt the active turn.

If Computer Use cannot inspect the app at all (capture/accessibility blocked),
record the blocker in the mailbox and recover safely (bring app to front by
bundle id, retry, repair app permissions). Ask the operator to relay only when
app access is genuinely blocked and self-recovery has failed. Do not fall back to
`claude --resume`, `claude -p`, Electron-store writes, or `~/.claude/projects`.

## After a ring: drain the full inbox

A doorbell points at one packet, but the recipient should **drain its entire
unread inbox** on wake, not just read the referenced file:

```bash
python3 bin/inbox.py --me <agent> --project <project-id> --limit 5
```

Activation doorbells are the exception: follow the exact claim command embedded
in the durable packet body, which includes `--chat <chat-id> --packet
<packet-name>`. That command claims the activation lease before marking the
packet read. A refusal exits 75 and leaves the packet unresolved; do not treat a
generic inbox read as activation authority.

This makes a missed doorbell self-healing: the next ring surfaces any earlier
unread packets too. There is no routine polling backstop by default (see
safety-fuse).

## A doorbell is not an acceptance artifact

A ring (or a visible answer) never by itself means a lane is done or accepted.
Completion/handoff still requires durable evidence:

- a mailbox handoff or chat note from the implementer
- task mirror status/activity update
- assigned-worktree dirty status or a checkpoint commit after activation

`status: in_progress` alone is not durable progress; it only proves activation.

## Provisional safety-fuse (heartbeat) — experimental

Routine/continuous polling is **deprecated** as the primary wake mechanism
(token cost + stale-context risk). A bounded heartbeat survives only as a
provisional **safety-fuse**, on trial, with hard constraints:

- **Only** when a doorbell attempt is blocked, or a worker is visibly running and
  a handoff is expected.
- **Task-scoped**: tied to one specific task/worktree/branch and its chat.
- **Auto-deletes** on handoff/ack/blocker; must not outlive its task/chat.
- **Never the primary path**, never a standing always-on watcher.
- Must be fixed or removed if it misbehaves on real tasks.

When a safety-fuse heartbeat is active, the Codex-side tooling below is the
reference implementation; the same discipline applies symmetrically to any agent
running one.

### Health diagnostics (safety-fuse)

If Computer Use cannot inspect the other app, do not switch to CLI/session-file
bridging. Record the blocker and gather only coarse shell diagnostics, e.g.:

```bash
python3 bin/claude_desktop_bridge_health.py --json
```

This reports whether the app appears running, frontmost, visible, holding power
assertions, and whether its main process appears busy by CPU. It deliberately
does not read message content, prompt state, thread titles, URLs, or local
session stores. A healthy shell report is not proof the doorbell is usable; that
is proven by the AX ring exit result and, when needed, `axsend confirm`. A
healthy shell report is also not proof an attended Computer Use fallback is
safe; that requires visible target inspection and the idle input gate. CPU-busy
is not proof of lane progress — pair it with durable lane evidence before
describing progress.

### Cadence (safety-fuse only)

Set heartbeat cadence per relay, scoped to the expectation, and delete it the
moment the expected response is recorded, blocked, or no longer needed:

- short response expected: tight minute-level
- long implementation/verification: slower, with what state counts as a useful
  update
- waiting only for final handoff: moderate, notify only on awaiting-input/new
  response/blocked/errored
- no longer needed: delete immediately

A heartbeat sends only when an explicit outbound directive exists; it must never
start the next queue lane or interrupt a running turn on its own. For ordered
Amiga queues, recompute the ready lane each wakeup rather than hardcoding an
issue:

```bash
python3 bin/project_design_queue.py bridge-status --project <project-id> --json
```

Use its `classification` (e.g. `durable-progress-visible`,
`cpu-busy-no-durable-progress`, `idle-no-durable-progress`,
`computer-use-cooldown-no-durable-progress`, `missing-bridge-metadata`,
`queue-empty`/`no-ready-lane`) to decide whether to inspect the app, wait, or
stop. After an idle-timeout, record the cooldown:

```bash
python3 bin/project_design_queue.py record-computer-use-timeout --project <project-id> --reason "Computer Use get_app_state timed out"
```

## New thread naming

Every new desktop thread must start with a unique bridge id and a short title so
the sidebar does not fill with indistinguishable rows. Before clicking
`New session`, generate a `bridge_thread_uuid` (full UUID, durable binding/audit)
and a `short_thread_title` (compact sidebar label, e.g. `GH-361 review
workflow`). Send the first visible prompt only after the idle gate passes, in the
compact one-line form:

```text
[BRIDGE <8-char-uuid-prefix>] Read <collab_message_path or latest CHAT-id packet> and execute it.
```

Keep the visible prompt under ~240 characters. The full UUID, chat id, message
path, task body, acceptance criteria, and gates live in the durable mailbox
packet, not in visible prompt text. Never paste task bodies or multi-paragraph
briefs into the app for task-grade work, and never split one bridge across
multiple visible messages.

## Completion detection

A turn is settled only when ALL are true:

- the visible response needed by the plan is present
- the sidebar row is no longer `Running`
- the prompt is available
- the Send button is disabled only because the prompt is empty
- there is no visible `Stop` control for the active turn

Agents should write the normal task result, blocker, question, or handoff — no
required protocol tokens. The reader decides state from the visible response plus
the settled UI, then records the meaningful content into the mailbox.

## Operator safety

- Do not paste secrets, credentials, persona passwords, or private browser data
  into any agent app.
- Do not start unrelated Amiga queue lanes from the doorbell.
- A product-implementation ring must still name the issue/task, worktree, allowed
  files, and verification expectations in the durable packet.
- Keep every relay visible/durable; no hidden desktop-only state.
- Do not create desktop threads by writing Electron stores, IndexedDB,
  `~/.claude/projects`, or CLI/project-session files.

## Failure modes

- Recipient idle/awaiting input: safe to read the last answer and decide whether
  an explicit next ring exists.
- Recipient still generating / shows `Stop` / creating a worktree: after the
  native composer is provably empty, one AX ring is allowed and may queue the
  pointer behind the active turn. If composer state is unreadable, unprovable,
  or `AXValue`-opaque, hold and enter recovery. If the result is
  `QUEUED (UNCONFIRMED)`, preserve the mailbox/follow-up, never re-ring, and do
  not claim exact-thread delivery. Do not use screenshot/keyboard Computer Use
  until the idle input gate passes.
- Computer Use cannot see the prompt/transcript: the Computer Use fallback is
  paused; record the blocker (for the safety-fuse path,
  `record-computer-use-timeout` applies a cooldown) and retry that fallback
  after repair. AX may still ring once only when it can verify the native
  composer identity and prove its empty state through readable `AXValue`. Do
  not fall back to CLI.
- App-visible thread changed: re-read app state and confirm project/title before
  sending anything.
- Safety-fuse heartbeat times out: record `timed_out`, delete the heartbeat, and
  leave the mailbox message visibly unresolved rather than inventing a response.
