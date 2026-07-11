# axsend — focus-independent AX doorbell bridge

Rings another agent app's composer (Codex, ZCode, Claude Desktop) using the
macOS Accessibility API (AXUIElement) — **no screenshots, no window raising, no
focus stealing**. Built because screenshot-based computer-use grabs focus while
the operator is working and misroutes keystrokes across overlapping windows.

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
bin/axsend tree  --app Codex --editable-only

# Dump the app element's raw attributes (debugging which process is real)
bin/axsend attrs --app Codex

# Idle-gate: empty AXTextArea value == composer is empty, safe to ring
bin/axsend tree  --app Codex --editable-only | grep AXTextArea

# Set composer text only (draft, no send)
bin/axsend ring  --app Codex --text "hello"

# See which button send would press WITHOUT pressing (do this on any new app)
bin/axsend ring  --app Codex --submit --dry-run --text "x"

# Set + press send. Verify is ENFORCED by default: it confirms the text landed as
# a NEW conversation turn (freshness baseline — no stale-match), auto-retries the
# whole cascade if not, and exits 0 ONLY on a confirmed (or queued) delivery.
bin/axsend ring  --app Codex --submit --text "[from claude] ..."

# Feedback WITHOUT a screenshot — did the message actually send? Call after any
# ring (or anytime). This is the reliable check; DO NOT use computer-use to verify.
#   exit 0 delivered | exit 7 not-delivered (draft or never typed)
bin/axsend confirm --app Codex --text "[from claude] ..."

# If confirm says "not-delivered": RE-RING with the message — the ring clears the old
# draft + retypes + resends (verified on Electron). Then confirm again.
bin/axsend ring  --app Codex --submit --verify --text "[from claude] ..."
# (`ring --text ""` now reliably clears Electron drafts too: it wakes focus then
#  runs two select-all strategies. Re-ring is the all-in-one recovery.)

# Post-send / anytime: is the recipient processing, and what are recent messages
# (including their reply)?
bin/axsend state --app Codex
```

Exit codes: `ring --verify` returns 7 if the sent text isn't found in the
conversation after the press (treat as "did not land"; the draft is cleared so
nothing is left stuck). `confirm` returns 0 delivered / 7 not-delivered. `ring --submit` (verify default) exits 0 on a confirmed delivery OR a QUEUED accept (recipient busy — message taken, will render when its turn ends; never resend), and 7 only after all retries fail with the recipient idle. `--submit` returns 5 if no send button resolved, 6 if the press failed.

`bin/axsend-ensure` preserves that queued exit-0 result. It runs the additional
standalone `confirm` only after a visibly verified ring; a queued turn is not yet
present in the conversation tree, so post-confirming it would create a false
failure and invite an unsafe duplicate send.

**Electron apps (ZCode/Antigravity) — the verification rule:** these composers
accept key events but do NOT reflect text back through `AXValue`, so you cannot
read the draft/empty state via AX. NEVER trust a read-back of the composer, and
NEVER fall back to a screenshot to check — use `axsend confirm`, which checks the
*conversation* (the sent message appears as a real turn ABOVE the composer) — the
one signal that IS reliably AX-readable. The composer's own draft/empty state is
NOT reliable (blank AXValue + stale cached static-text nodes), so confirm reports
delivered vs not, not a draft state. A handoff is done only when `confirm` reports
`delivered` (exit 0); if not-delivered, re-ring (it reliably clears + resends).

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
  Claude = `AXTextArea` identity **"Prompt"**; Codex/ZCode = `AXTextArea` identity
  **"Ask for follow-up changes"** (Codex's two same-title "ChatGPT" windows
  disambiguate by this composer identity, not the window title). An UNRECOGNIZED
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
  false-positive. Returns `7` if not landed, `8` if the target went busy before
  the press (so it never leaves a stuck draft).
- **Strict idle-gate:** `ring` re-checks for a Stop button immediately before
  pressing; aborts (`8`) if the target became busy. Apps won't submit while busy.

## Per-app support matrix (composer identity revalidated 2026-07-11, PR78 R4/R5)

`ring` populates the composer via `AXValue` if the field accepts it, else falls
back to **key-event typing** (`CGEventPostToPid` + `keyboardSetUnicodeString`, no
focus steal) for Electron code-editor composers that reject `AXValue`. Submit then
tries the send button, `AXConfirm`, and a posted Return.

| App | Composer identity | Submit | Status (2026-07-11) |
|-----|-------------------|--------|---------------------|
| **Codex** | `AXTextArea` "Ask for follow-up changes" (bundle `com.openai.codex`) | send-arrow `AXPress` (same chat web area) | ✅ resolves + confirmed delivery |
| **Claude Desktop** | `AXTextArea` "Prompt" | `key-return` fallback | ✅ resolves, no regression |
| **ZCode** | `AXTextArea` "Ask for follow-up changes" | "Send" button | ✅ resolves; `Send` dry-run target |
| **Antigravity / Gemini** | ❌ no profile yet → `.unknown` | — | ⚠️ **FAILS CLOSED** — AX doorbell unsupported pending live composer-identity capture |

ZCode is an Electron code-editor composer that rejects programmatic `AXValue`
writes — the key-event typing path makes the doorbell work for it.

**Antigravity/Gemini (PR78 R5):** no explicit composer-identity profile is
captured, so `profileFor` returns `.unknown` and resolution FAILS CLOSED rather
than silently reusing Claude's "Prompt" matching (which would drive a broken
doorbell). To support either app, inspect its live composer identity, add an
explicit `ComposerProfile` case + fixtures, and record live evidence. Until then
`agents.json` `ax_app` routing for `gemini`/`antigravity` will fail closed with a
clear "no native chat composer found" message — the durable mailbox remains the
delivery channel.

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
