# axsend — focus-independent AX doorbell bridge

Rings another agent app's composer (Codex, ZCode, Claude Desktop) using the
macOS Accessibility API (AXUIElement) — **no screenshots, no window raising, no
focus stealing**. Built because screenshot-based computer-use grabs focus while
the operator is working and misroutes keystrokes across overlapping windows.

## Build

```bash
cd tools/axbridge && swiftc -O axsend.swift -o axsend
# symlinked at ../../bin/axsend
```

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

# Set + press send. Verify is ENFORCED by default: it confirms the text landed as
# a NEW conversation turn (freshness baseline — no stale-match), auto-retries the
# whole cascade if not, and exits 0 ONLY on a confirmed (or queued) delivery.
bin/axsend ring  --app Codex --submit --text "[from claude] ..."

# Feedback WITHOUT a screenshot — did the message actually send? Call after any
# ring (or anytime). This is the reliable check; DO NOT use computer-use to verify.
#   exit 0 delivered | exit 7 not-delivered (draft or never typed)
bin/axsend confirm --app Codex --text "[from claude] ..."

# Only after a non-zero/not-delivered result, retry once — the ring clears the old
# draft + retypes + resends (verified on Electron). Never re-ring exit-0 queued/delivered.
bin/axsend ring  --app Codex --submit --verify --text "[from claude] ..."
# (`ring --text ""` now reliably clears Electron drafts too: it wakes focus then
#  runs two select-all strategies. One retry is the all-in-one recovery.)

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
delivered vs not, not a draft state. Exit 0 from the original ring means
delivered/queued and must not be re-rung. After a non-zero/not-delivered result,
retry once; do not repeatedly re-ring.

`--app` matches by localized name or bundle id (substring ok). `--window-index N`
targets a specific window (default 0).

## How it targets things

- **Right process:** an app has several same-named processes (GPU/helper/menu-extra).
  axsend prefers `activationPolicy == .regular` with `windows > 0` — the dock-extra
  helper reports `AXTitle = com.apple.dock.external.extra.arm64` and 0 windows.
- **Electron wake-up:** sets `AXManualAccessibility` + `AXEnhancedUserInterface` on
  the app element so Chromium exposes its web tree.
- **Composer:** the editable node with a non-empty `AXPlaceholderValue` (the real
  `AXTextArea`), not a wrapper `AXGroup` (setting `AXValue` on a wrapper no-ops).
- **Send button:** geometry-based — the rightmost `AXButton` in the composer's
  own toolbar band (same vertical zone), excluding window controls
  (close/minimize/zoom) and known non-send controls. Document-order heuristics
  are unsafe: they once grabbed the window minimize button. Always
  `ring --submit --dry-run` on a new app first to print the resolved target.
- **Submit (multi-mechanism):** some composers ignore `AXPress` on the Send
  button. `ring --submit` tries, in order, verifying after each: (1) `AXPress`
  the Send button, (2) `AXConfirm` on the composer, (3) focus the composer and
  post a real **Return key to the app's PID** (`CGEventPostToPid` — no focus
  steal). Stops at the first that actually lands.
- **`--verify` (honest):** requires the text to have **left the composer** AND
  appear as a conversation message above it. A stuck draft can never
  false-positive. It returns 0 for delivered/queued and `7` when the message did
  not land; a busy recipient is not an AX ring failure.
- **Busy-safe queueing:** `ring` is allowed while the recipient is busy. It
  submits one message, exits 0 when delivered or queued, and must not be
  repeatedly re-rung. `tree`/`state` are optional diagnostics, not AX ring idle
  gates. The idle input gate applies only to attended screenshot/keyboard
  Computer Use fallback.

## Per-app support matrix (validated 2026-06-21)

`ring` populates the composer via `AXValue` if the field accepts it, else falls
back to **key-event typing** (`CGEventPostToPid` + `keyboardSetUnicodeString`, no
focus steal) for Electron code-editor composers that reject `AXValue`. Submit then
tries the send button, `AXConfirm`, and a posted Return.

| App | Composer write | Submit | Status |
|-----|----------------|--------|--------|
| **Codex** | `AXValue` | send-arrow `AXPress` | ✅ proven bidirectional |
| **Claude Desktop** | `AXValue` | `key-return` | ✅ proven (received Codex ring) |
| **ZCode** | **key-typing** (rejects `AXValue`) | "Send" button | ✅ proven (replied to a typed ring) |
| **Antigravity (Gemini)** | **key-typing** (rejects `AXValue`) | `key-return` | ✅ typed + submitted |

Both ZCode and Antigravity are Electron apps with code-editor-style composers
(Monaco/CodeMirror) that silently reject programmatic `AXValue` writes — the
key-event typing path is what makes the doorbell work for them.

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
