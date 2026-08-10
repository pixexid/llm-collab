# axsend — focus-independent AX doorbell bridge

> **Conditional Codex-app procedure.** For OpenAI-model interaction, focus is
> BB until the Codex app reaches parity with the Claude app and BB. Use this
> tool only when the task needs a Codex-app-only tool that BB cannot reach;
> `deliver.py` printing an AX command does not satisfy that condition. See the
> [`AGENTS.md` standing routing rule](../../AGENTS.md#bb-worker-surface).

Rings another agent app's composer (Codex or ZCode) using the
macOS Accessibility API (AXUIElement) — **no screenshots, no window raising, no
focus stealing**. Built because screenshot-based computer-use grabs focus while
the operator is working and misroutes keystrokes across overlapping windows.

After the app-only-tool condition is met, AX is a doorbell between distinct
collaborator app identities. External workers such as Claude and ZCode may ring
root Codex. Claude-targeted delivery is its durable packet plus its own
background inbox watcher. Never use AX for `codex -> codex`, a root
self-handoff, or a managed Codex worker: use Codex Thread Coordination
(`read_thread` / `send_message_to_thread`) instead. Native subagents use native
subagent coordination, not an app doorbell. `deliver.py` persists a sender-aware
`autobridge_skip` guard on a `codex -> codex` packet so PM2 or manual inbox
watchers cannot later turn that durable history into a runtime wake.

## Build

```bash
cd tools/axbridge && ./build.sh        # rebuilds only when source is newer; symlinks ../../bin/axsend
# or explicitly (two sources, library mode — required since PR78 R4):
swiftc -O -parse-as-library axsend.swift send-resolution.swift -o axsend
```

The pure `send-resolution.swift` module is a separate source; a single-file
`swiftc axsend.swift` no longer compiles. Run tests with `./test.sh`.

## Permission

The process that runs `axsend` must be enabled in
**System Settings → Privacy & Security → Accessibility**. Check with:

```bash
bin/axsend check        # -> "AX trusted: YES"
```

## Usage

These commands apply only after the task satisfies the app-only-tool condition.
Routine `ring` targets Codex only. For Codex, composer **content** and
`AXValue` readability/opacity are never a sender-side hold, and neither is a
busy/running recipient: Codex never types into its own composer, so any value
there — readable non-empty, readable empty, readable nil, or unreadable — is
stray, and the ring clears and overrides it before sending. Ring even when the
recipient is busy; the doorbell queues. Do not prove the composer empty first
and do not route a non-empty/unreadable Codex composer to attended recovery —
that stranded the sender indefinitely (GH-470).

Routine `ring` still fails closed on a genuine targeting or operation failure
(GH-1547 target-safety decision): no or ambiguous native composer target, an
unrecognized or opaque-profile target it cannot safely confirm (ZCode, unknown
apps), an AX-trust failure, a clear/type/submit failure, or post-submit
identity loss. **Exit 11** refuses before any mutation in those target cases;
it no longer fires on a resolvable Codex composer merely for holding a draft
or being unreadable — GH-470 overrides and sends through that instead. Key-event
typing exists only behind the explicit `--attended` flag (`ring --attended`,
and the `type` command which is attended-only), which prints a loud warning and
is valid only inside a Codex-supervised attended-recovery turn for a genuinely
opaque/unresolved target.

```bash
# Inspect an app's tree to find the composer + send button
bin/axsend-ensure tree --app Codex --editable-only

# Dump the app element's raw attributes (debugging which process is real)
bin/axsend attrs --app Codex

# Optional targeting diagnostic; this is not an AX ring idle gate
bin/axsend-ensure tree --app Codex --editable-only | grep AXTextArea

# Set composer text only (draft, no send)
bin/axsend ring  --app Codex --text "hello"

# See which button send would press WITHOUT pressing (do this on any new app)
bin/axsend ring  --app Codex --submit --dry-run --text "x"

# Set + press send. Verification is ENFORCED by default. Exit 0 is either
# VERIFIED or QUEUED (UNCONFIRMED); classify the output as documented below.
bin/axsend ring  --app Codex --submit --text "[from claude] ..."

# Feedback WITHOUT a screenshot — did the app turn render? Call after any ring
# (or anytime). This does not establish a working harness or reply path.
#   exit 0 app turn present | exit 7 app turn absent (draft or never typed)
bin/axsend confirm --app Codex --text "[from claude] ..."

# Only after a non-zero/not-delivered result, retry once — and ONLY when the
# target composer resolves cleanly (GH-1547): a routine ring refuses with exit
# 11 only on a genuine targeting failure (no/ambiguous composer, an
# unrecognized/opaque-profile target, an AX-trust failure) — a resolvable
# Codex composer's content/readability is never a refusal reason (GH-470). For
# a refused target, hold and request Codex-attended recovery (`--attended`,
# supervised) instead of retrying. Never re-ring either exit-0 result.
bin/axsend ring  --app Codex --submit --verify --text "[from claude] ..."

# Post-send / anytime: is the recipient processing, and what are recent messages
# (including their reply)?
bin/axsend state --app Codex
```

Exit codes: `ring --verify` returns 7 if the sent text isn't found in the
conversation after the press (treat as "did not render"; the draft is cleared so
nothing is left stuck). `confirm` returns 0 app-turn-present / 7 app-turn-absent.
`ring --submit` (verify default) exits 0 with either `VERIFIED` or
`QUEUED (UNCONFIRMED)`. `VERIFIED` exit 0 confirms only that a turn rendered in
the lagging app UI; it does not establish delivery to a working harness or a
reply path.
Queued-unconfirmed means the recipient became busy during submit, but it does
not prove the message entered the intended thread. Never resend it; preserve the
mailbox packet, record the unconfirmed blocker/follow-up, and do not claim
exact-thread delivery until later confirmation or recipient evidence. Exit 7
means not delivered after bounded internal attempts while idle. `--submit`
returns 5 if no send button resolved, 6 if the press failed.

`bin/axsend-ensure` preserves the queued-unconfirmed exit-0 result. It runs the
additional standalone `confirm` only after a visibly verified ring; a queued
turn is not yet present in the conversation tree, so immediate post-confirming
would create a false failure and invite an unsafe duplicate send. The caller
must record queued-unconfirmed as unresolved and follow up without re-ringing.

**Non-Codex apps (ZCode/Antigravity) are not routine ring targets:** the routine
AX doorbell is Codex-only. ZCode is a relay and Antigravity is an
opaque/undriveable **target** (`ax_attended_only`), so neither takes a routine
ring — they route to Codex-attended recovery / their own channel. This is a
TARGET-resolution rule, not an empty-composer rule (GH-470): composer content and
`AXValue` readability are never a hold for a *resolvable* Codex composer — a
routine ring clears and overrides whatever is there and sends, and busy is not a
hold. `axsend confirm` checks the conversation after a ring. `VERIFIED` exit 0
confirms only that a turn rendered in the lagging app UI; it does not establish
delivery to a working harness or a reply path. `QUEUED (UNCONFIRMED)` exit 0
remains unresolved and must not be re-rung.

`--app` matches by localized name or bundle id (substring ok). `--window-index N`
targets a specific window. It is OPTIONAL: when ABSENT the resolver is in AUTO
mode (nil) and picks the one window carrying the app's native composer (failing
closed on none/ambiguous); an explicit index — including `0` — is honored and, if
out of range, REJECTED (not clamped). Absent is not the same as `0`.

## How it targets things

- **Right process:** an app has several same-named processes (GPU/helper/menu-extra).
  axsend prefers `activationPolicy == .regular` with `windows > 0` — the dock-extra
  helper reports `AXTitle = com.apple.dock.external.extra.arm64` and 0 windows.
  If multiple regular matching processes expose windows (for example two Codex
  accounts), axsend fails closed with their PIDs instead of selecting by launch
  order and risking delivery to the wrong account.
- **Electron wake-up:** sets `AXManualAccessibility` + `AXEnhancedUserInterface` on
  the app element so Chromium exposes its web tree.
- **Composer (PR78 R4/R5 — app-profile identity):** the composer is identified by
  its app-specific field identity, NOT web-area membership (Electron renders the
  native composer inside an `AXWebArea`). `profileFor(--app)` selects the profile:
  Claude = `AXTextArea` identity **"Prompt"**; ZCode and older Codex builds use
  **"Ask for follow-up changes"**; updated Codex Desktop builds use **"Do
  anything"** while exposing localized app name `ChatGPT` and bundle
  `com.openai.codex`. Codex's same-title windows disambiguate by composer
  identity, not window title. An UNRECOGNIZED
  app resolves to `.unknown` and FAILS CLOSED — it never silently inherits
  Claude's matching. Every path (ring/state/type/confirm + each post-send refresh)
  re-resolves by this identity and fails closed on loss (no stale-window fallback).
  A resolver failure reports `no proven chat window/composer`; it does not imply
  that the application exposes zero AX windows. Use `tree --app ...` when the
  distinction between app-window enumeration and composer identity matters.
- **Send button:** geometry-based — the rightmost unlabeled `AXButton` (the send
  arrow) in the composer's own toolbar band, scoped to the composer's `composerPane`
  and to the composer's OWN chat `AXWebArea`. A button in a DIFFERENT (foreign)
  web area — an embedded Browser/preview pane's Run/Stop — is excluded (R5: the
  real Electron send arrow lives in the same chat web area as the composer, so a
  blanket "exclude all web-area buttons" wrongly removed it). Window controls
  (close/minimize/zoom) and known non-send labels are excluded. Always
  `ring --submit --dry-run` on a new app first to print the resolved target.
- **Submit (multi-mechanism):** some composers ignore `AXPress` on the Send
  button. `ring --submit` tries, in order, verifying after each: (1) `AXPress`
  the Send button, (2) `AXConfirm` on the composer, (3) focus the composer and
  post a real **Return key to the app's PID** (`CGEventPostToPid` — no focus
  steal). Stops at the first that actually lands.
- **`--verify` (honest):** requires the text to have **left the composer** AND
  appear as a conversation message above it. A stuck draft can never
  false-positive. It returns 0 with either `VERIFIED` or
  `QUEUED (UNCONFIRMED)` and `7` when the message did not land. A busy recipient
  is not an AX ring failure, but queued-unconfirmed is not exact-thread delivery
  proof.
- **Busy-safe queueing:** `ring` targets Codex only and is allowed while Codex
  is busy regardless of composer content — content and `AXValue`
  readability/opacity are never a hold for Codex, and the ring clears and
  overrides whatever is there before sending. A visible `Stop`, `Running`, or
  processing state alone is not an idle-wait requirement. Submit exactly one
  message. Also hold when the same pointer is already queued, and never stack or
  re-ring that pointer behind the running turn. `VERIFIED` confirms only that
  an app turn rendered, not delivery to a working harness or a reply path;
  `QUEUED (UNCONFIRMED)` preserves the mailbox/follow-up but cannot be reported
  as exact-thread delivery. `tree`/`state` are optional diagnostics, not AX ring
  idle gates. The idle input gate applies only to attended screenshot/keyboard
  Computer Use fallback. This does not permit a Codex-to-Codex AX doorbell.

## Per-app support matrix (composer identity revalidated 2026-07-11, PR78 R4/R5)

`ring` populates the composer via `AXValue` if the field accepts it, else falls
back to **key-event typing** (`CGEventPostToPid` + `keyboardSetUnicodeString`, no
focus steal) for Electron code-editor composers that reject `AXValue`. Submit then
tries the send button, `AXConfirm`, and a posted Return.

| App | Composer identity | Submit | Status (2026-07-11) |
|-----|-------------------|--------|---------------------|
| **Codex** | `AXTextArea` "Ask for follow-up changes" or "Do anything" (bundle `com.openai.codex`, localized app name may be `ChatGPT`) | send-arrow `AXPress` (same chat web area) | ✅ resolves + app-turn verification |
| **Claude Desktop** | `AXTextArea` "Prompt" | — | ⛔ mutation refused; durable mailbox watcher only |
| **ZCode** | `AXTextArea` "Ask for follow-up changes"; draft state is `AXValue`-opaque | "Send" button | ⛔ routine ring REFUSES (exit 11, enforced) — Codex-attended recovery only (`--attended`) |
| **Antigravity / Gemini** | ❌ no profile yet → `.unknown` | — | ⛔ **FAILS CLOSED** — `.unknown` is opaque, routine ring REFUSES (exit 11); attended recovery only |

ZCode is an Electron code-editor composer that rejects programmatic `AXValue`
writes. The key-event typing path is available only within the attended recovery
path after composer safety is established; it does not authorize a routine
blind ring.

**Antigravity/Gemini (PR78 R5/R6):** no explicit composer-identity profile is
captured, so `profileFor` returns `.unknown` and resolution FAILS CLOSED rather
than silently reusing Claude's "Prompt" matching (which would drive a broken
doorbell). To support either app, inspect its live composer identity, add an
explicit `ComposerProfile` case + fixtures, and record live evidence. Routing is
aligned so no watcher attempts the unsupported doorbell: `gemini`'s
`activation.ax_app` was REMOVED (terminal-only `cli_session`), and `antigravity`
is a `human_relay` with `watcher_enabled: false` and no `ax_app` — so `deliver.py`
routes neither to an AX ring (regression: `tests/test_deliver_ax_routing.py`). The
durable mailbox remains their delivery channel.

Safety: `ring --submit` only presses a **confident** send button (unlabeled icon
or labeled send/submit), never a side-effecting control (e.g. Antigravity's
"Record voice memo"). Honest `--verify` returns 7 if nothing actually submits.

The `type` command exposes key-typing directly and is ATTENDED-ONLY (GH-1547):
`axsend type --app <name> --text "..." --attended [--submit] [--verify]` —
without `--attended` it refuses with exit 11 before touching anything.

## Diagnosing a resolver failure (2026-07-27)

Resolver failures are not verdicts on AX. `state` reports
`no proven chat window/composer`; `ring` reports
`no native chat composer found for <app> (auto mode requires a proven chat window)`.
Two ways to misread them were observed in one session:

**It can be transient.** Three consecutive `state` and
`ring --submit --dry-run --text "probe"` calls against Codex returned their
respective resolver failures, and `check`
reported `AX trusted: YES` the whole time. The same commands resolved the composer
immediately once the app window was brought into view. What changed was visibility
of the window, not trust, not the binary, and not the app (same pid throughout).
Before concluding the doorbell is unavailable, make the target window visible and
retry.

**`tree` cannot tell you whether the target composer holds a draft.** It lists
`AXTextArea` nodes from every thread the app has open, including background
threads whose composers legitimately hold drafts, and it truncates `AXValue` at
about 40 characters. A stale-looking fragment from some other thread's composer
reads exactly like a stuck draft in the one you are about to ring. Use
`ring --submit --dry-run --text "probe"` instead. GH-470: `--dry-run` is
side-effect-free — it resolves the send target from the composer's geometry and
does NOT populate, clear, or restore the composer, so it never disturbs an
existing draft (and a draft never causes a refusal). If you need the full text of
a draft, `tree` will not give it to you.

**For a non-Claude target, a failed mailbox wake says nothing about AX.**
`deliver.py` refusing with
`autobridge_refusal_reason: exact_binding_not_dispatchable` means the bound
session is not dispatchable. Check both `status` and `lease_expires_utc`; expiry
does not change `status`, and a stopped session can produce the same refusal
without an expired lease. The AX doorbell is an independent path and may be fine;
test it rather than assuming both are down. Canonical Claude is the exception:
its durable packet and background inbox watcher remain the only target-side path.

## Limits / next

- Validated on Codex. ZCode exposes a composer too; per-app send-button
  heuristics may need tuning (`tree` to inspect).
- Pairs with the llm-collab mailbox: `deliver.py` is the durable record, `axsend`
  is the doorbell nudge.

## Computer Use supervision

Only after the task satisfies the app-only-tool condition, AX is the
**fallback** doorbell between distinct external collaborator apps, including an
external worker ringing root Codex. Even then it is taken only when `deliver.py`
prints the command, because routine exact-session dispatch is the wake whenever
the recipient's binding dispatches (contract v12). Where it is printed it should
not be disabled or bypassed merely because an external desktop app needs
recovery. It is never a Codex-to-Codex or root-self transport.

Codex exclusively owns attended Computer Use control of external collaborator
desktop apps. Use that supervisory path when an external app requires visible
state inspection, navigation, thread creation or switching, usage-limit
handling, unsafe-composer recovery, or an unblock that the mailbox plus
`axsend state` cannot safely resolve. Do not use Computer Use to select or route
work to a Codex task. Other collaborators continue to use durable packets plus
AX and send Codex a durable intervention request instead of independently
driving another agent's desktop UI. These target-side Computer Use and AX
recovery clauses exclude canonical Claude; its mailbox watcher is the only wake
path. Inbound Claude-to-Codex AX remains permitted.

Computer Use is a serialized control and recovery plane, not a replacement
doorbell. Once Codex has restored a safe target/thread, wake routing resumes
under the [canonical contract-v12 rule](../../AGENTS.md#recent-contract-changes).
On the doorbell path it selects, composer content and `AXValue` readability
remain never a hold for Codex, so the ring overrides and sends regardless. An
opaque/unresolved **target** — a composer AX cannot safely confirm belongs to
the intended recipient — remains on the attended recovery path.
