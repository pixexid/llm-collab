# Desktop Computer-Use Doorbell (Agent-to-Agent Comms)

This is the agent-to-agent communication workflow for collaborators running in
dedicated desktop apps (e.g. Claude in `/Applications/Claude.app`, Codex in
`/Applications/Codex.app`). It is intentionally separate from `claude --resume`,
`claude -p`, and other CLI/session-file flows.

> Claude may ring Codex after writing a durable packet. Codex never rings
> Claude: Claude is woken by its durable packet and the Claude app's own
> background inbox watcher. The heartbeat survives only as a bounded,
> provisional **safety-fuse** (see below), not as the primary path.

The AX doorbell is **Codex-only**: only a Codex/ChatGPT app is ever an AX ring
target, and only as the wake for a durable `deliver.py` packet, using exactly the
command `deliver.py` prints. Every other worker (Claude, ZCode, …) is woken by
its durable packet and its own background watcher — never by AX. The durable
mailbox is the sole agent-to-agent channel; AX is a wake mechanism for a
delivered Codex packet, not a separate messaging path. It does not apply to
`codex -> codex`, root self-handoffs, or managed Codex workers.

## One channel: the durable mailbox (AX is only a Codex wake)

There is a single agent-to-agent channel: the **llm-collab durable mailbox**
(`deliver.py`). Everything — task delegation, handoffs, status changes,
implementation discussion, feedback before opening an issue/task, a decision
between workers, quick coordination — goes through it as a durable packet, read
at the worker's own pace from the inbox. There is no separate ephemeral "direct
AX" messaging channel: **AX is only the wake** for a durable Codex packet (the
exact command `deliver.py` prints), never a place to put content that isn't also
in the mailbox.

**Ask the right worker, not the operator.** Engineering / plan / decision
questions go to **Codex (or the worker who owns that area) as a durable packet**,
not to the operator. The operator decides product, visual/UX, scope, and business
calls — not technical implementation. Routing eng decisions to the operator
stalls the work (they can't adjudicate them).

**Queueing to a busy recipient is SAFE, unconditionally, for Codex.** Only a
Codex recipient may receive an AX doorbell, using exactly the command
`deliver.py` prints. For Codex, composer content and `AXValue`
readability/opacity are NEVER a hold, and a busy/running recipient is NOT a
hold either: the ring clears and overrides whatever is in the composer and
sends (Codex never types into its own composer, so any content there is
stray; AX send takes preference over any composer content, including the
operator actively typing). Do NOT prove the composer empty first, and do NOT
wait/poll for Codex to be idle merely because `Stop`, `Running`, or processing
is visible; if Codex is busy, the message queues and is processed when its
current turn ends.
Queueing is *insurance* that the receiver gets the message even if it already
saw it on another channel; it does **not** corrupt the running turn (only a
forced steer would), and queued messages are cancelable on the receiver side.
A routine ring fails closed only on a genuine targeting/operation failure: no
or ambiguous native composer target, a non-Codex or unrecognized profile, an
AX-trust failure, a clear/type/submit failure, or post-submit identity loss —
that ambiguous/unresolved-target case, not composer content, is what enters
recovery. Never stack or re-ring the same pointer; if it is already queued,
one copy is enough.
Receiver discipline: when not running, read queued messages and ignore a queued
copy already handled from the inbox. This policy does not authorize a Codex
self-doorbell or AX delivery to any non-Codex worker.

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
for Codex. ZCode or Antigravity may originate a ring to Codex only; neither is
an AX target. Busy alone is not a hold after composer safety is actually
proved.

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
$AX state --app Codex                                   # read the reply
```

For delivery, run only the complete command printed by `deliver.py`; never
hand-author a ring command.

Rules: deliver the durable packet with `deliver.py` FIRST (mailbox = truth);
then ring once, even when the recipient is busy. Do not prove the composer
empty first — for Codex, composer content and `AXValue` readability/opacity
are never a hold, so the ring overrides and sends; only a genuine targeting
failure (no/ambiguous target, unrecognized profile, AX-trust failure) enters
recovery. `VERIFIED` exit 0 is confirmed;
`QUEUED (UNCONFIRMED)` exit 0 must be recorded and followed up without re-ringing
or claiming exact-thread delivery. Use `--dry-run` first on any new app and
`--verify` (non-zero means the text did not land; a post-send empty composer is
not proof of delivery). The idle input gate applies only to attended screenshot/keyboard
Computer Use fallback. Any collaborator may run the exact command printed by
`deliver.py` to ring the canonical Codex recipient. No other recipient is an
AX target, and Codex must not target itself. Needs the running process enabled in
Privacy & Security → Accessibility. Falls back to screenshot Computer Use only if
AX fails for an external-app target that is a valid ring target at all — never
for Claude. Full reference:
`tools/axbridge/README.md` and the Claude Code `ax-doorbell` skill.

**Per-app routine target safety:**

| App | Composer write | Submit | Status |
|-----|----------------|--------|--------|
| Codex | `AXValue` | send-arrow `AXPress` | ✅ proven — content/opacity never a hold (GH-470) |
| Claude Desktop | — | — | ⛔ never a routine target: durable packet + its own background inbox watcher |
| ZCode | `AXValue`-opaque | attended recovery only | ⛔ never a routine AX target (non-Codex profile); attended recovery only |
| Antigravity (Gemini) | unreadable/unproven | attended recovery only | ⛔ never a routine AX target (non-Codex, unresolved profile); attended recovery only |

This target-side hold is ENFORCED (GH-1547). The registry marks ZCode and
Antigravity `ax_attended_only`; `deliver.py` never emits a routine AX doorbell
for them and instead prints an explicit ATTENDED RECOVERY REQUIRED instruction
that routes control to Codex (never a silent mailbox-only fallback). The
`axsend` binary independently refuses a routine `ring` against an unrecognized
or opaque-profile **target** with exit 11 before any mutation; `--attended`
(and the attended-only `type` command) unlock key-event typing solely inside a
Codex-supervised recovery turn, with a loud warning.

Routine `ring` is Codex-only. For a resolved Codex composer, content and
`AXValue` readability/opacity are NEVER a hold: the ring clears and overrides
whatever is there and sends, and a busy/running recipient does not require an
idle wait — the doorbell queues instead. A rejected write, opaque profile, or
unresolved/ambiguous **target** — never composer content — is what does not
authorize a routine ring: hold and use the attended recovery path. Low-level
key-event typing may be used only within that recovery path after target
safety is established; it is never a blind-send escape hatch. The doorbell
works between supported distinct app identities; that capability does not make
it a Codex-to-Codex transport.

## GH-135 update-survival decision and recovery evidence

**Selected decision: option 3 — documented runbook.** The current accepted
mechanism is operator recovery after a real app update strands the running app
on a deleted versioned bundle path. No stable symlink/alias or stable helper is
selected in the current implementation, and this runbook does not authorize one.

### Confirmed version-path/TCC incident and real-update recovery

- Claude Code continued running from the versioned `2.1.209` path after that
  version tree had been deleted and only `2.1.215` remained on disk.
- GH-127A surfaced the resulting failed AX trust state as `[ax] DOWN`.
- The durable mailbox remained the source of truth while the doorbell was
  unavailable.
- A second real app update on 2026-07-21/22 reproduced the same failure class:
  the running process stayed pinned to the deleted `2.1.215` bundle while only
  `2.1.217` existed on disk, and `axsend check` reported `AX trusted: NO`.
- The app's post-update auto-restart is not sufficient recovery for this class;
  it can leave background tasks running in the stranded process. Use a **full
  app quit + reopen**, not an auto-restart, before retrying AX.
- After full quit + reopen, `axsend check` returned `AX trusted: YES`
  immediately and a live ring delivered. Accessibility re-approval was not
  required.
- If AX trust is still down after a full quit + reopen, only then move on to
  operator TCC/Accessibility repair.

Draft/composer-targeting failures, including the GH-1547 target-side hold, are
a different failure class and are not TCC update-survival evidence.

## One channel (the mailbox) plus a wake (the doorbell)

- **Mailbox = `llm-collab` (the single durable channel / source of truth).**
  Every task, handoff, blocker, clarification, decision, and piece of evidence is
  a file written with `deliver.py`. Nothing load-bearing lives only in an app's
  visible thread. This is the message.
- **Doorbell = AX (the wake only, to Codex only — not a second channel).** When
  `deliver.py` reports `ax_doorbell_required`, run its exact printed command to
  send one short, sender-tagged pointer to the
  durable packet. Screenshot/keyboard Computer Use is a fallback only when AX is
  unavailable and the target path is explicitly configured for desktop bridging.

The mailbox is the record; the doorbell is the notification. A doorbell with no
corresponding mailbox packet is not valid for task-grade work.

The doorbell exists for a worker that has no background event pickup of its own —
Codex today. **Claude is not rung.** Its runtime carries its own inbox watcher,
so the durable packet is the whole wake path; a Claude packet that is not being
picked up is a watcher or binding defect to report, not a reason to reach for AX.
See `session-autobridge-runbook.md`.

## Sender identifier convention

Because a Codex AX wake ring types a pointer into Codex's app, every such ring
must carry a sender identifier so the recipient can attribute it (the message
itself is always the durable mailbox packet). The operator never tags their own
messages.

- `[BRIDGE <8-char-uuid-prefix>] ...` — a durable bridge pointer routed via
  `llm-collab`.
- `[from <agent>] ...` or `[<agent> doorbell] ...` — the inline tag on a Codex
  AX **wake ring** whose content is a pointer to a durable `llm-collab` packet
  (the ring carries no frontmatter, so the tag disambiguates the sender). The
  ring is not itself the message — the packet in the mailbox is.
- **Untagged plain text is operator-origin by convention.** Treat a tagged
  message as peer-agent coordination; the work is always the durable packet —
  read and reply through the mailbox, never treat the ring pointer as the message.

## Ringing the doorbell

For task-grade work, in order:

1. Write the durable instruction/handoff with `deliver.py` to `Chats/` and the
   recipient's `agents/<agent>/inbox.json`.
2. If the recipient is non-Codex, stop: its background watcher owns pickup.
   If sender and recipient are both `codex`, stop app routing and use Thread
   Coordination (`read_thread` / `send_message_to_thread`). Otherwise, only
   when `deliver.py` reports `ax_doorbell_required`, run its exact printed
   command. Do not prove the composer empty first: composer content and
   `AXValue` readability/opacity are never a hold for Codex, and the ring
   overrides and sends. Ring even if the recipient is busy — the one-line
   pointer queues behind the current turn. Only a genuine targeting failure
   (no/ambiguous target, unrecognized profile, AX-trust failure,
   clear/type/submit failure, post-submit identity loss) means hold and
   recovery. The ring result must be
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
   for an external collaborator app when AX is unavailable or unsafe. In
   that fallback path, pass the idle input gate before typing.
5. Record the ring result in your own thread and, when relevant, in the mailbox.

Every agent-to-agent message — including ad-hoc coordination — goes through the
durable mailbox; it is never optional. AX targets Codex only and is only the wake
for a durable Codex packet; the idle gate applies only to attended
screenshot/keyboard Computer Use fallback for an external collaborator app.

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
- Recipient still generating / shows `Stop` / creating a worktree: one AX ring
  is allowed unconditionally and may queue the pointer behind the active turn
  — do not wait for or prove an empty composer first; composer content and
  `AXValue` readability/opacity are never a hold for Codex. Only a genuine
  targeting failure (no/ambiguous target, unrecognized profile, AX-trust
  failure) means hold and enter recovery. If the result is
  `QUEUED (UNCONFIRMED)`, preserve the mailbox/follow-up, never re-ring, and do
  not claim exact-thread delivery. Do not use screenshot/keyboard Computer Use
  until the idle input gate passes.
- Computer Use cannot see the prompt/transcript: the Computer Use fallback is
  paused; record the blocker and retry that fallback after repair. AX may still
  ring once whenever it can resolve the native composer TARGET — it does not need
  to prove an empty composer, and composer content/`AXValue` readability are not
  a hold (the ring clears and overrides). Do not fall back to CLI.
- App-visible thread changed: re-read app state and confirm project/title before
  sending anything.
- Safety-fuse heartbeat times out: record `timed_out`, delete the heartbeat, and
  leave the mailbox message visibly unresolved rather than inventing a response.
