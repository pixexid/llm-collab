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

### Recovery after a Claude Code update (GH-135)

Claude Code installs under a version-numbered path
(`~/Library/Application Support/Claude/claude-code/<version>/claude.app/...`).
An update deletes the old tree, but the post-update auto-restart deliberately
keeps background tasks alive — so the surviving process runs from a bundle that
no longer exists and target app AX calls fail for the rest of that process's
life. Diagnose by comparing the running path against what is installed, then
probe Claude itself with a real app-targeted AX read:

```bash
LLM_COLLAB_ROOT="$(git rev-parse --show-toplevel)"
ps -eo pid,comm | grep 'claude-code/.*/claude.app'
ls ~/Library/Application\ Support/Claude/claude-code/
"$LLM_COLLAB_ROOT/bin/axsend-ensure" tree --app Claude --editable-only
```

Only a **full quit + reopen** recovers it; a restart cannot, because the
stranded process is the one it is protecting. Re-approving in System Settings
is normally unnecessary when controller AX trust is still intact; the target
app process is the stale part.

Claude cannot perform this recovery itself: quitting the app terminates the
session issuing the command. So Claude asks Codex, in the durable mailbox
packet Codex is already waiting on (the doorbell is dead by definition at that
point, so there is no ring to send), and Codex runs:

```bash
LLM_COLLAB_ROOT="$(git rev-parse --show-toplevel)"
osascript -e 'tell application "Claude" to quit'
while pgrep -f 'claude-code/.*/claude\.app/Contents/MacOS/claude' >/dev/null; do sleep 1; done
open -a Claude
sleep 15 && "$LLM_COLLAB_ROOT/bin/axsend-ensure" tree --app Claude --editable-only
```

Reopening is not the end of the recovery. The app lands on a **new task**
screen; the old session survives with its thread intact but is not resumed, so
the lane stalls silently with AX working and nobody driving it. Codex must
reselect it with computer-use. Select the exact `cliSessionId` named in the
`AX_BLOCKED` packet; resolve the title only as the on-screen label to click:

```bash
CLAUDE_CLI_SESSION_ID='<id from AX_BLOCKED packet>' python3 - <<'PY'
import glob, json, os, sys

target = os.environ["CLAUDE_CLI_SESSION_ID"]
best = None
for path in glob.glob(os.path.expanduser(
        '~/Library/Application Support/Claude/claude-code-sessions/*/*/*.json')):
    try:
        data = json.load(open(path))
    except Exception:
        continue
    if data.get('cliSessionId') == target and not data.get('isArchived'):
        best = data
        break
if best is None:
    sys.exit(f"no unarchived Claude session found for {target}")
print(best['title'], '|', best['cliSessionId'])
PY
# then: computer-use -> click the session row matching that title and id
```

If the packet lacks a `cliSessionId`, fail closed and ask for a new
`AX_BLOCKED` packet. Do not fall back to "most recent workspace session"; that
can select another worker's thread. The title is only the label to click.
Use the operator-approved UI reselection path for this attended recovery; note
that screenshot Computer Use may raise the target window and take keyboard
focus. The AX sidebar-nav used for Codex's own chats does not cover Claude's
app.

The older workspace-scoped fallback is diagnostic only, useful for identifying
which Claude sessions exist when preparing a corrected packet:

```bash
WORKSPACE_CWD="$(pwd)" python3 - <<'PY'
import json, glob, os
best = None
workspace = os.environ["WORKSPACE_CWD"]
for p in glob.glob(os.path.expanduser(
        '~/Library/Application Support/Claude/claude-code-sessions/*/*/*.json')):
    try:
        d = json.load(open(p))
    except Exception:
        continue
    if d.get('cwd') == workspace and not d.get('isArchived'):
        if best is None or d.get('lastActivityAt', 0) > best.get('lastActivityAt', 0):
            best = d
if best is None:
    raise SystemExit(f"no unarchived Claude session found for cwd {workspace}")
print(best['title'], '|', best['cliSessionId'])
PY
```

Claude keeps its thread and memory across this, so no state is lost. It still
states its title and `cliSessionId` in the `AX_BLOCKED` packet so Codex can
verify it reselected the right window, and Codex still replies into the
originating chat to hand the lane back.

## Usage

Before any routine `ring`, prove through readable `AXValue` that the native
composer is empty. A busy recipient alone is not a hold after that proof, so
one pointer may queue behind the active turn. A non-empty, unreadable,
unprovable, or `AXValue`-opaque composer means hold and enter attended recovery;
never infer empty or perform a blind ring.

This proof gate is ENFORCED by the binary (GH-1547): routine `ring` computes a
composer-safety decision BEFORE any mutation and refuses with **exit 11** when
the target profile is `AXValue`-opaque (ZCode, unknown apps), the `AXValue`
read fails, or a readable composer holds a draft. A refusal performs no clear,
select-all, delete, typing, or submit. Key-event typing exists only behind the
explicit `--attended` flag (`ring --attended`, and the `type` command which is
attended-only), which prints a loud warning and is valid only inside a
Codex-supervised attended-recovery turn. The sender-side readable
empty-composer proof remains good practice for queue discipline, but the
binary no longer relies on it for safety.

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

# Only after a non-zero/not-delivered result, retry once — and ONLY when the
# target composer is proven readable and empty (GH-1547): a routine ring refuses
# with exit 11 on an opaque profile, an unreadable AXValue, or a remaining
# draft. For any refused state, hold and request Codex-attended recovery
# (`--attended`, supervised) instead of retrying. Never re-ring either exit-0 result.
bin/axsend ring  --app Codex --submit --verify --text "[from claude] ..."

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

**Electron apps (ZCode/Antigravity) — opaque composer rule:** when these
composers do not reflect draft state through readable `AXValue`, their empty
state cannot be proved. They are therefore routine hold-and-recovery/attended
paths by definition: do not infer empty from a blank value, do not key-type a
blind ring, and do not make an exception because the recipient is busy.
`axsend confirm` checks the conversation after a ring; it cannot prove the
composer was empty before one. After attended recovery establishes a safe send,
`VERIFIED` exit 0 confirms delivery and `QUEUED (UNCONFIRMED)` exit 0 remains
unresolved and must not be re-rung. Only a non-zero/not-delivered result whose
absence is confirmed on the same target permits one re-ring.

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
- **Busy-safe queueing:** after readable `AXValue` proves the native composer is
  empty, `ring` is allowed while a distinct external collaborator is busy. A
  visible `Stop`, `Running`, or processing state alone is not an idle-wait
  requirement. Submit exactly one message. A non-empty, unreadable, unprovable,
  or `AXValue`-opaque composer means hold and attended recovery; never infer
  empty. Also hold when the same pointer is already queued, and never stack or
  re-ring that pointer behind the running turn. `VERIFIED` confirms delivery;
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
| **Codex** | `AXTextArea` "Ask for follow-up changes" or "Do anything" (bundle `com.openai.codex`, localized app name may be `ChatGPT`) | send-arrow `AXPress` (same chat web area) | ✅ resolves + confirmed delivery |
| **Claude Desktop** | `AXTextArea` "Prompt" | `key-return` fallback | ✅ resolves, no regression |
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
to one verified AX ring only when the native composer is again provably empty;
an `AXValue`-opaque composer remains on the attended recovery path.
