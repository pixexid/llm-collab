// Deterministic regression tests for issue #77 send-button resolution.
// Compile:  swiftc -O send-resolution.swift send-resolution-tests.swift -o /tmp/axsend-tests && /tmp/axsend-tests
// (also run by tools/axbridge/test.sh)
//
// Covers the exact #77 failure: with an embedded Amiga /design preview open, a
// Browser pane "Run <server>" button sits near the composer band and was being
// resolved as the send target. These fixtures assert the pure picker never
// selects a web/Run control and correctly finds the native send arrow.

import Foundation

@main
enum SendResolutionTests {
static func main() {
var failures = 0
func check(_ cond: Bool, _ name: String) {
    if cond { print("ok   - \(name)") }
    else { print("FAIL - \(name)"); failures += 1 }
}

// Composer band anchor used across fixtures.
let cy = 700.0, ch = 40.0   // band ≈ [688, 830]

// 1) The #77 repro: a Browser "Run <server>" button in-band + the native send
//    arrow. The Run button is inWebArea; must pick the arrow, never the Run.
do {
    let cands = [
        SendButtonCandidate(label: "Run gh1162fe-app", subrole: "", x: 900, y: 760, inWebArea: true),
        SendButtonCandidate(label: "", subrole: "", x: 520, y: 812, inWebArea: false), // send arrow
    ]
    let idx = pickSendButtonIndex(cands, composerY: cy, composerH: ch)
    check(idx == 1, "#77 repro: picks native send arrow, not Browser Run button")
}

// 2) Even if a Run button were NOT flagged inWebArea (defense in depth), the
//    label denylist excludes it.
do {
    let cands = [
        SendButtonCandidate(label: "Run gh1162fe-app", subrole: "", x: 900, y: 760, inWebArea: false),
        SendButtonCandidate(label: "", subrole: "", x: 520, y: 812, inWebArea: false),
    ]
    let idx = pickSendButtonIndex(cands, composerY: cy, composerH: ch)
    check(idx == 1, "Run label denylisted even when not flagged web-area")
}

// 3) All candidates are web/Run controls -> nil (fall back to key-return, never
//    press a wrong control).
do {
    let cands = [
        SendButtonCandidate(label: "Run app", subrole: "", x: 900, y: 760, inWebArea: true),
        SendButtonCandidate(label: "Stop", subrole: "", x: 940, y: 760, inWebArea: true),
        SendButtonCandidate(label: "Reload", subrole: "", x: 980, y: 760, inWebArea: true),
    ]
    check(pickSendButtonIndex(cands, composerY: cy, composerH: ch) == nil,
          "only web/Run controls -> nil (no confident send button)")
}

// 4) Prefer the unlabeled icon arrow over a labeled non-arrow, and take the
//    rightmost among unlabeled.
do {
    let cands = [
        SendButtonCandidate(label: "Attach", subrole: "", x: 480, y: 812, inWebArea: false),
        SendButtonCandidate(label: "", subrole: "", x: 500, y: 812, inWebArea: false),
        SendButtonCandidate(label: "", subrole: "", x: 560, y: 812, inWebArea: false), // rightmost arrow
    ]
    check(pickSendButtonIndex(cands, composerY: cy, composerH: ch) == 2,
          "prefers rightmost unlabeled send arrow")
}

// 5) Out-of-band buttons (e.g. a sidebar/window control far above) are ignored.
do {
    let cands = [
        SendButtonCandidate(label: "", subrole: "", x: 560, y: 120, inWebArea: false),  // far above band
        SendButtonCandidate(label: "", subrole: "AXCloseButton", x: 20, y: 812, inWebArea: false),
        SendButtonCandidate(label: "", subrole: "", x: 540, y: 812, inWebArea: false),  // in-band arrow
    ]
    check(pickSendButtonIndex(cands, composerY: cy, composerH: ch) == 2,
          "ignores out-of-band + window-control buttons")
}

// 6) A field-bearing localhost form's submit button (inWebArea) is never picked
//    even if unlabeled and in-band.
do {
    let cands = [
        SendButtonCandidate(label: "", subrole: "", x: 600, y: 800, inWebArea: true),  // web form submit
        SendButtonCandidate(label: "", subrole: "", x: 520, y: 812, inWebArea: false), // native arrow
    ]
    check(pickSendButtonIndex(cands, composerY: cy, composerH: ch) == 1,
          "web form submit button (in-band, unlabeled) excluded")
}

// 7) Confidence gate: unlabeled/send/submit are confident; anything else isn't.
check(sendResolutionIsConfident(""), "confident: unlabeled")
check(sendResolutionIsConfident("Send"), "confident: Send")
check(sendResolutionIsConfident("Submit message"), "confident: Submit")
check(!sendResolutionIsConfident("Run app"), "not confident: Run app")
check(!sendResolutionIsConfident("Record voice memo"), "not confident: Record voice memo")

// --- Multi-window conversation-window selection (issue #77 / PR78 review) ---
// Window 0 = auxiliary Browser/preview: a "Page URL" field + a web form input.
// Window 1 = native chat: the "Prompt" composer. Every path (ring/verify/busy/
// confirm/state) must select window 1 and ignore window 0.
let auxWindow0: [EditableInfo] = [
    EditableInfo(role: "AXTextField", title: "Page URL", placeholder: "", inWebArea: false),
    EditableInfo(role: "AXTextArea", title: "", placeholder: "Full name", inWebArea: true),
    EditableInfo(role: "AXTextArea", title: "", placeholder: "Gate codes, parking...", inWebArea: true),
]
let chatWindow1: [EditableInfo] = [
    EditableInfo(role: "AXTextArea", title: "Prompt", placeholder: "Type / for commands", inWebArea: false),
]

// 8) Auto-selection picks the native chat window (1), never the aux window (0).
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: nil) == .index(1),
      "auto: selects native Prompt window 1, ignores aux window 0")

// 9) Order-independent: native window first still resolves to it.
check(pickConversationWindow([chatWindow1, auxWindow0], preferIndex: nil) == .index(0),
      "auto: native window found regardless of order")

// 10) Aux window alone has NO native composer (Page URL + web inputs excluded).
check(!windowHasNativeComposer(auxWindow0), "aux window has no native composer")
check(windowHasNativeComposer(chatWindow1), "chat window has native composer")

// 11) Explicit index wins — and explicit 0 is honored (distinct from unset/nil).
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: 0) == .index(0),
      "explicit index 0 honored (not treated as unset)")
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: 1) == .index(1),
      "explicit index 1 honored")
// R3 item 2: out-of-range / negative explicit indices are REJECTED, never clamped.
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: 2) == .invalidIndex,
      "explicit out-of-range index -> invalidIndex (not clamped)")
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: -1) == .invalidIndex,
      "explicit negative index -> invalidIndex")

// 12) R3 item 4: auto with no proven native Prompt -> none (never window 0).
check(pickConversationWindow([auxWindow0], preferIndex: nil) == .none,
      "auto, no Prompt anywhere -> none (never window 0)")
check(pickConversationWindow([], preferIndex: nil) == .none,
      "no windows -> none")

// 13) Page URL field is not mistaken for a native composer.
let urlOnly: [EditableInfo] = [EditableInfo(role: "AXTextField", title: "Page URL", placeholder: "", inWebArea: false)]
check(!windowHasNativeComposer(urlOnly), "Page URL field alone is not a native composer")

// R3 item 1: a generic native Name/Search field is NOT a chat composer -> auto
// resolves to none (mutating paths must not write into it).
let nameSearchWindow: [EditableInfo] = [
    EditableInfo(role: "AXTextField", title: "Name", placeholder: "Your name", inWebArea: false),
    EditableInfo(role: "AXTextField", title: "Search", placeholder: "Search", inWebArea: false),
]
check(pickConversationWindow([nameSearchWindow], preferIndex: nil) == .none,
      "auto: window with only Name/Search fields -> none (Prompt-only)")
check(pickConversationWindow([nameSearchWindow, chatWindow1], preferIndex: nil) == .index(1),
      "auto: picks the Prompt window over a Name/Search window")

// --- PR78 R2 safety cases ---
// 14) Two native Prompt windows in AUTO mode -> ambiguous (fail closed).
check(pickConversationWindow([chatWindow1, chatWindow1], preferIndex: nil) == .ambiguous,
      "auto + two Prompt windows -> ambiguous (fail closed)")
// 15) Reordered windows: aux then two chats -> still ambiguous in auto.
check(pickConversationWindow([auxWindow0, chatWindow1, chatWindow1], preferIndex: nil) == .ambiguous,
      "auto + multiple Prompt windows (any order) -> ambiguous")
// 16) Explicit index disambiguates even with two Prompt windows.
check(pickConversationWindow([chatWindow1, chatWindow1], preferIndex: 1) == .index(1),
      "explicit index disambiguates two Prompt windows")
// 17) One Prompt + aux is NOT ambiguous (single credible composer).
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: nil) == .index(1),
      "single Prompt window is not ambiguous")
// 18) Page-URL-only window (Browser) has no native composer -> type/ring must
//     fail closed rather than write into Page URL.
check(!windowHasNativeComposer(urlOnly), "Browser-only (Page URL) window: no native composer for mutating paths")

// --- PR78 R4: per-app composer identity (Codex / ZCode) ---
// Live evidence 2026-07-11: Codex has TWO windows both titled "ChatGPT" — an
// avatar-overlay shell (no composer) and the active chat (AXTextArea identity
// "Ask for follow-up changes", value may prefix a ⏎). ZCode uses the same
// composer identity. Claude's "Prompt" identity must be unaffected.
let codexOverlay: [EditableInfo] = [
    // avatar-overlay shell: no composer, maybe stray non-composer editables
    EditableInfo(role: "AXTextField", title: "Search", placeholder: "", inWebArea: true),
]
let codexChat: [EditableInfo] = [
    EditableInfo(role: "AXTextArea", title: "Ask for follow-up changes",
                 placeholder: "⏎Ask for follow-up changes", inWebArea: true),
]

// R4-1: Codex two same-title windows -> only the chat window (index 1) resolves.
check(pickConversationWindow([codexOverlay, codexChat], preferIndex: nil, profile: .codex) == .index(1),
      "R4: Codex overlay+chat -> picks the chat window with the follow-up composer")
// R4-2: reordered (chat first) still resolves by identity, not position.
check(pickConversationWindow([codexChat, codexOverlay], preferIndex: nil, profile: .codex) == .index(0),
      "R4: Codex chat-first -> picks index 0 by composer identity")
// R4-3: missing Codex composer (both overlay/shell) -> none (fail closed).
check(pickConversationWindow([codexOverlay, codexOverlay], preferIndex: nil, profile: .codex) == .none,
      "R4: Codex with no follow-up composer -> none (fail closed, never window 0)")
// R4-4: duplicate Codex composer windows in auto -> ambiguous (fail closed).
check(pickConversationWindow([codexChat, codexChat], preferIndex: nil, profile: .codex) == .ambiguous,
      "R4: two Codex composer windows -> ambiguous (fail closed)")
// R4-5: explicit index disambiguates duplicate Codex composers.
check(pickConversationWindow([codexChat, codexChat], preferIndex: 0, profile: .codex) == .index(0),
      "R4: explicit index disambiguates two Codex composer windows")
// R4-6: ZCode uses the same follow-up composer identity.
let zcodeChat: [EditableInfo] = [
    EditableInfo(role: "AXTextArea", title: "Ask for follow-up changes", placeholder: "", inWebArea: true),
]
check(pickConversationWindow([codexOverlay, zcodeChat], preferIndex: nil, profile: .zcode) == .index(1),
      "R4: ZCode follow-up composer resolves")
check(windowHasNativeComposer(zcodeChat, .zcode), "R4: ZCode window has a native composer")
// R4-7: cross-profile isolation — Claude's "Prompt" is NOT a Codex composer, and
// the Codex "Ask for follow-up changes" is NOT a Claude composer. Each profile
// only matches its own identity (fail closed otherwise).
check(pickConversationWindow([chatWindow1], preferIndex: nil, profile: .codex) == .none,
      "R4: a Claude Prompt window is not a Codex composer -> none under .codex")
check(pickConversationWindow([codexChat], preferIndex: nil, profile: .claude) == .none,
      "R4: a Codex follow-up window is not a Claude composer -> none under .claude")
check(!editableIsNativeComposer(EditableInfo(role: "AXButton", title: "Ask for follow-up changes", placeholder: "", inWebArea: true), .codex),
      "R4: a same-named non-AXTextArea (button) is not the Codex composer")
// R4-8: Claude Prompt path is unchanged under the default profile.
check(pickConversationWindow([auxWindow0, chatWindow1], preferIndex: nil, profile: .claude) == .index(1),
      "R4: Claude .claude profile still resolves the Prompt window (no regression)")

if failures == 0 { print("\nALL PASS (send-resolution)"); exit(0) }
else { print("\n\(failures) FAILURE(S)"); exit(1) }
}
}
