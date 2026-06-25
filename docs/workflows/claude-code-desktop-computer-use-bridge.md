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

## Communication tiers: llm-collab vs direct ax (choose by PURPOSE)

Two channels, picked by what the message IS — not by convenience:

- **llm-collab (durable / work channel)** — task delegation, handoffs, status
  changes, anything a worker must **act on as work**, long context, and anything
  that must **survive a thread/context loss**. It is the mailbox of record; read
  at the worker's own pace from the inbox. If it needs to be remembered,
  recovered, or done-as-work → it goes here.
- **Direct ax doorbell (real-time / thinking channel)** — discuss an
  implementation plan, expand/explore ideas, get **feedback before opening an
  issue or task** (don't open issues with zero prior planning/research), resolve
  a **decision between workers**, quick coordination. Ephemeral — NOT in the
  mailbox, so never put a task/decision/handoff *only* in a direct ax msg.

**Ask the right worker, not the operator.** Engineering / plan / decision
questions go to **Codex (or the worker who owns that area) over the direct
channel**, not to the operator. The operator decides product, visual/UX, scope,
and business calls — not technical implementation. Routing eng decisions to the
operator stalls the work (they can't adjudicate them).

**Queueing to a busy recipient is SAFE and expected.** Do NOT wait/poll for the
other agent to be idle — ring them; if they're busy the message queues and is
processed when their current turn ends. Queueing is *insurance* that the receiver
gets the message even if it already saw it on another channel; it does **not**
corrupt the running turn (only a forced steer would), and queued msgs are
cancelable on the receiver side. Every worker can queue, in every direction.
Sender discipline: **don't re-ring the same message repeatedly** (one queue is
enough). Receiver discipline: when not running, read queued msgs; ignore a queued
copy you already handled from the inbox while running.

**Verification is enforced, not optional.** `ring --submit` verifies by default
(exit 0 = delivered or queued; non-zero = not delivered, re-ring). NEVER use
computer-use/screenshots to check whether a doorbell sent — that's the ring's
exit code (and `axsend confirm` for a later re-check). Validated bidirectionally
2026-06-21: Claude ⇄ Codex ⇄ ZCode ⇄ Antigravity.

## Preferred doorbell transport: `axsend` (AX bridge, no focus steal)

Ring the other agent with the **Accessibility-API bridge** rather than
screenshot computer-use. Screenshot tools must raise the target window and steal
the operator's keyboard focus, and on overlapping windows they misroute
keystrokes. `axsend` sets the composer text and presses send through AXUIElement
**without raising the window or touching focus**, and reads replies without a
screenshot.

```bash
AX=/Users/pixexid/Projects/llm-collab/bin/axsend-ensure   # auto-builds if needed
$AX state --app Codex                                   # idle-gate: processing? + recent msgs
$AX ring  --app Codex --submit --dry-run --text "x"     # confirm send target (new app/window)
$AX ring  --app Codex --submit --verify --text "[from <me>] <pointer to durable packet>"
$AX state --app Codex                                   # read the reply
```

Rules: deliver the durable packet with `deliver.py` FIRST (mailbox = truth);
idle-gate before every ring; `--dry-run` first on any new app; `--verify` (returns
non-zero if the text didn't land — empty composer is NOT proof of send). Any agent
(Codex, Claude, Gemini, ZCode) can call `axsend-ensure` via shell. Needs the
running process enabled in Privacy & Security → Accessibility. Falls back to
screenshot computer-use only if AX fails for a target. Full reference:
`tools/axbridge/README.md` and the Claude Code `ax-doorbell` skill.

**Validated across all four agent apps (2026-06-21):**

| App | Composer write | Submit | Status |
|-----|----------------|--------|--------|
| Codex | `AXValue` | send-arrow `AXPress` | ✅ proven bidirectional |
| Claude Desktop | `AXValue` | `key-return` | ✅ proven |
| ZCode | key-event typing | "Send" button | ✅ proven (replied to a typed ring) |
| Antigravity (Gemini) | key-event typing | `key-return` | ✅ typed + submitted |

`ring` adapts automatically: it writes via `AXValue`, and if the field rejects it
(ZCode/Antigravity are Electron code-editor composers that silently drop `AXValue`
writes) it falls back to real **key-event typing** (`CGEventPostToPid` +
`keyboardSetUnicodeString`, no focus steal). So the doorbell is universal across
every agent desktop app — no screenshots, no focus theft, in either direction.

## Two channels: mailbox + doorbell

- **Mailbox = `llm-collab` (durable source of truth).** Every task, handoff,
  blocker, clarification, decision, and piece of evidence is a file written with
  `deliver.py`. Nothing load-bearing lives only in an app's visible thread.
- **Doorbell = Computer Use (immediate, event-driven nudge).** The moment an
  agent finishes a task, hits a blocker, needs a clarification, or completes a
  handoff, it uses Computer Use to bring the other agent's desktop app to front
  and type one short, sender-tagged pointer to the durable packet. This wakes the
  recipient *now* instead of waiting on a scheduled check.

The mailbox is the record; the doorbell is the notification. A doorbell with no
corresponding mailbox packet is not valid for task-grade work.

## Sender identifier convention

Because agents now type directly into each other's apps, every agent-to-agent
message must carry a sender identifier so the recipient can attribute it. The
operator never tags their own messages.

- `[BRIDGE <8-char-uuid-prefix>] ...` — a durable bridge pointer routed via
  `llm-collab`.
- `[from <agent>] ...` or `[<agent> doorbell] ...` — a direct Computer-Use ring
  (there is no message frontmatter on a direct ring, so the inline tag is what
  disambiguates).
- **Untagged plain text is operator-origin by convention.** Treat a tagged
  message as peer-agent coordination; reply/hand back through the durable
  mailbox, not only the ring.

## Ringing the doorbell

For task-grade work, in order:

1. Write the durable instruction/handoff with `deliver.py` to `Chats/` and the
   recipient's `agents/<agent>/inbox.json`.
2. Ring the recipient with `axsend ring --submit --verify --text "<pointer>"`
   even if the recipient is busy. The busy case is expected: the one-line pointer
   queues behind the current turn and reinforces delivery of the durable packet.
   Use exactly **one short, sender-tagged, one-line pointer** to the exact
   inbox/chat/message path as the `--text` value. Full context stays in the
   durable packet, never in the visible prompt.
4. Treat exit 0 (`delivered`, `confirmed`, or `queued`) as a successful doorbell.
   If `axsend` reports not delivered, run `axsend confirm`; if still absent,
   retry once or record the exact AX blocker in the mailbox.
5. Use screenshot/keyboard Computer Use only as fallback when `axsend` is
   unavailable. In that fallback path, pass the idle input gate before typing.
6. Record the ring result in your own thread and, when relevant, in the mailbox.

For non-task ad-hoc chat, the mailbox is optional, but a sender tag is still
required. Prefer `axsend`; the idle gate applies only to screenshot/keyboard
Computer Use fallback.

## Idle input gate (Computer Use fallback only)

Never type over an active turn. Before sending, confirm ALL of:

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
is proven only after Computer Use inspects the visible target thread/prompt and
the idle gate passes. CPU-busy is not proof of lane progress — pair it with
durable lane evidence before describing progress.

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
- Recipient still generating / shows `Stop` / creating a worktree: do not
  interrupt; the idle gate forbids the ring.
- Computer Use cannot see the prompt/transcript: the doorbell is paused; record
  the blocker (for the safety-fuse path, `record-computer-use-timeout` applies a
  cooldown) and retry via Computer Use after repair. Do not fall back to CLI.
- App-visible thread changed: re-read app state and confirm project/title before
  sending anything.
- Safety-fuse heartbeat times out: record `timed_out`, delete the heartbeat, and
  leave the mailbox message visibly unresolved rather than inventing a response.
