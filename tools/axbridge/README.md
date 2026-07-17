# axsend — focus-independent AX doorbell bridge

Rings another agent app's composer (Codex, ZCode, Claude Desktop) using the
macOS Accessibility API (AXUIElement) — **no screenshots, no window raising, no
focus stealing**. Built because screenshot-based computer-use grabs focus while
the operator is working and misroutes keystrokes across overlapping windows.

AX is a doorbell between distinct collaborator app identities. External
workers such as Claude and ZCode may ring root Codex, and root Codex may ring an
external worker. Never use AX for `codex -> codex`, a root self-handoff, or a
managed Codex worker: use Codex Thread Coordination (`read_thread` /
`send_message_to_thread`) instead. Native subagents use native subagent
coordination, not an app doorbell. `deliver.py` persists a sender-aware
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

# Feedback WITHOUT a screenshot — did the message actually send? Call after any
# ring (or anytime). This is the reliable check; DO NOT use computer-use to verify.
#   exit 0 delivered | exit 7 not-delivered (draft or never typed)
bin/axsend confirm --app Codex --text "[from claude] ..."

# Only after a non-zero/not-delivered result, retry once — the ring clears the old
# draft + retypes + resends (verified on Electron). Never re-ring either exit-0 result.
bin/axsend ring  --app Codex --submit --verify --text "[from claude] ..."
# (`ring --text ""` now reliably clears Electron drafts too: it wakes focus then
#  runs two select-all strategies. One retry is the all-in-one recovery.)

# Post-send / anytime: is the recipient processing, and what are recent messages
# (including their reply)?
bin/axsend state --app Codex
```

Exit codes: `ring --verify` returns 7 if the sent text isn't found in the
conversation after the press (treat as "did not land"; the draft is cleared so
nothing is left stuck). `confirm` returns 0 delivered / 7 not-delivered.
`ring --submit` (verify default) exits 0 with either `VERIFIED` or
`QUEUED (UNCONFIRMED)`. Only `VERIFIED` confirms a visible conversation turn.
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

**Electron apps (ZCode/Antigravity) — the verification rule:** these composers
accept key events but do NOT reflect text back through `AXValue`, so you cannot
read the draft/empty state via AX. NEVER trust a read-back of the composer, and
NEVER fall back to a screenshot to check — use `axsend confirm`, which checks the
*conversation* (the sent message appears as a real turn ABOVE the composer) — the
one signal that IS reliably AX-readable. The composer's own draft/empty state is
NOT reliable (blank AXValue + stale cached static-text nodes), so confirm reports
delivered vs not, not a draft state. `VERIFIED` exit 0 confirms delivery;
`QUEUED (UNCONFIRMED)` exit 0 remains unresolved but must not be re-rung. After a
non-zero/not-delivered result, retry once; do not repeatedly re-ring.

`--app` matches by localized name or bundle id (substring ok). `--window-index N`
targets a specific window. It is OPTIONAL: when ABSENT the resolver is in AUTO
mode (nil) and picks the one window carrying the app's native composer (failing
closed on none/ambiguous); an explicit index — including `0` — is honored and, if
out of range, REJECTED (not clamped). Absent is not the same as `0`.

## How it targets things

- **Right process:** an app has several same-named processes (GPU/helper/menu-extra).
  axsend prefers `activationPolicy == .regular` with `windows > 0` — the dock-extra
  helper reports `AXTitle = com.apple.dock.external.extra.arm64` and 0 windows.
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
- **Busy-safe queueing:** `ring` is allowed while a distinct external
  collaborator is busy. A visible `Stop`, `Running`, or processing state is not
  an idle-wait requirement. Submit exactly one message; block only when the
  composer is non-empty/unsafe or the same pointer is already queued, and never
  stack or re-ring that pointer behind the running turn. `VERIFIED` confirms
  delivery; `QUEUED (UNCONFIRMED)` preserves the mailbox/follow-up but cannot be
  reported as exact-thread delivery. `tree`/`state` are optional diagnostics,
  not AX ring idle gates. The idle input gate applies only to attended
  screenshot/keyboard Computer Use fallback. This does not permit a
  Codex-to-Codex AX doorbell.

## Per-app support matrix (composer identity revalidated 2026-07-11, PR78 R4/R5)

`ring` populates the composer via `AXValue` if the field accepts it, else falls
back to **key-event typing** (`CGEventPostToPid` + `keyboardSetUnicodeString`, no
focus steal) for Electron code-editor composers that reject `AXValue`. Submit then
tries the send button, `AXConfirm`, and a posted Return.

| App | Composer identity | Submit | Status (2026-07-11) |
|-----|-------------------|--------|---------------------|
| **Codex** | `AXTextArea` "Ask for follow-up changes" or "Do anything" (bundle `com.openai.codex`, localized app name may be `ChatGPT`) | send-arrow `AXPress` (same chat web area) | ✅ resolves + confirmed delivery |
| **Claude Desktop** | `AXTextArea` "Prompt" | `key-return` fallback | ✅ resolves, no regression |
| **ZCode** | `AXTextArea` "Ask for follow-up changes" | "Send" button | ✅ resolves; `Send` dry-run target |
| **Antigravity / Gemini** | ❌ no profile yet → `.unknown` | — | ⚠️ **FAILS CLOSED** — AX doorbell unsupported pending live composer-identity capture |

ZCode is an Electron code-editor composer that rejects programmatic `AXValue`
writes — the key-event typing path makes the doorbell work for it.

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

The `type` command exposes key-typing directly: `axsend type --app <name>
--text "..." [--submit] [--verify]`.

## Limits / next

- Validated on Codex. ZCode and Claude Desktop expose composers too; per-app send
  button heuristics may need tuning (`tree` to inspect).
- Pairs with the llm-collab mailbox: `deliver.py` is the durable record, `axsend`
  is the doorbell nudge.

## Computer Use supervision

AX remains the routine doorbell between distinct external collaborator apps,
including an external worker ringing root Codex. It is the normal transport
after a durable `deliver.py` packet for those routes and should not be disabled
or bypassed merely because an external desktop app needs recovery. It is never
a Codex-to-Codex or root-self transport.

Codex exclusively owns attended Computer Use control of external collaborator
desktop apps. Use that supervisory path when an external app requires visible
state inspection, navigation, thread creation or switching, usage-limit
handling, unsafe-composer recovery, or an unblock that the mailbox plus
`axsend state` cannot safely resolve. Do not use Computer Use to select or route
work to a Codex task. Other collaborators continue to use durable packets plus
AX and send Codex a durable intervention request instead of independently
driving another agent's desktop UI.

Computer Use is a serialized control and recovery plane, not a replacement
doorbell. Once Codex has restored a safe target/thread, normal delivery returns
to one verified AX ring.
