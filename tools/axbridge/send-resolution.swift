// Pure send-button resolution — no AX, no I/O — so the issue #77 regression
// (send target escaping the composer into the embedded Browser pane's
// "Run <server>" controls) is deterministically unit-testable with synthetic
// candidates. axsend.swift builds `SendButtonCandidate`s from the live AX tree
// (already scoped to the composer's pane and web-area-excluded) and calls
// `pickSendButtonIndex`; the tests feed it multi-window / embedded-preview
// fixtures including Browser Run buttons and a field-bearing localhost form.

import Foundation

struct SendButtonCandidate {
    let label: String     // human label (title/desc/help), lowercased-agnostic
    let subrole: String   // AXSubrole (window controls carry these)
    let x: Double
    let y: Double
    let inWebArea: Bool    // descendant of an AXWebArea (Browser/preview content)
}

// Controls that are structurally never a send arrow.
let sendResolutionWindowControls: Set<String> = [
    "AXCloseButton", "AXMinimizeButton", "AXZoomButton",
    "AXFullScreenButton", "AXToolbarButton",
]

// Labels that must never be pressed as "send": composer toolbar affordances,
// side-effecting media controls, and embedded Browser/preview pane controls.
let sendResolutionNonSendLabels = [
    "add files", "custom", "medium", "dictate", "model", "attach",
    "more", "agent", "branch", "environment",
    "voice", "record", "memo", "mic", "microphone", "audio",
    "image", "photo", "camera", "screenshot", "settings",
    // embedded Browser/preview pane controls (defense in depth; pane-scoping
    // already excludes them structurally)
    "run", "stop", "reload", "refresh", "back", "forward", "url",
]

func sendResolutionIsNonSend(_ label: String) -> Bool {
    let l = label.lowercased()
    return sendResolutionNonSendLabels.contains { l.contains($0) }
}

// Pick the confident send button among candidates near the composer, or nil.
// Rules (all must hold): not in a web area, not a window control, within the
// composer's vertical band, not a non-send label. Prefer an unlabeled icon
// button (the send arrow); among the pool, take the rightmost.
func pickSendButtonIndex(_ cands: [SendButtonCandidate],
                         composerY: Double, composerH: Double) -> Int? {
    let bandTop = composerY - 12
    let bandBottom = composerY + composerH + 90
    let eligible = cands.indices.filter { i in
        let c = cands[i]
        if c.inWebArea { return false }
        if sendResolutionWindowControls.contains(c.subrole) { return false }
        if c.y < bandTop || c.y > bandBottom { return false }
        return !sendResolutionIsNonSend(c.label)
    }
    let unlabeled = eligible.filter { cands[$0].label.isEmpty }
    let pool = unlabeled.isEmpty ? eligible : unlabeled
    return pool.max { cands[$0].x < cands[$1].x }
}

// A resolved candidate is only pressed if it is a CONFIDENT send target: an
// unlabeled icon button or one labeled send/submit. Anything else falls through
// to AXConfirm/key-return (side-effect-free), never a wrong press.
func sendResolutionIsConfident(_ label: String) -> Bool {
    let l = label.lowercased()
    return l.isEmpty || l.contains("send") || l.contains("submit")
}

// One editable field, flattened from a window, for window-selection decisions.
struct EditableInfo {
    let role: String        // AXTextArea / AXTextField / AXComboBox / ...
    let title: String       // AXTitle or AXDescription
    let placeholder: String // AXPlaceholderValue
    let inWebArea: Bool     // descendant of an AXWebArea (Browser/preview content)
}

// PR78 R4: per-app composer identity. Claude/Codex/ZCode are Electron apps whose
// whole UI (incl. the native composer) is under an AXWebArea, so a composer is
// identified STRICTLY by its app-specific field identity — never by web-area
// membership. Live AX evidence (2026-07-11): Claude composer = AXTextArea
// identity "Prompt" (+ "Type / for commands"); Codex AND ZCode composer =
// AXTextArea identity "Ask for follow-up changes" (they are disambiguated by
// app/bundle upstream, not by this string). The DOM/CSS fingerprints Codex
// proposed (ProseMirror class, composer-surface-chrome ancestor) are NOT exposed
// via AXUIElement, so the readable field identity is the stable key.
enum ComposerProfile: Equatable {
    case claude
    case codex
    case zcode
}

// Is this editable THIS profile's native chat composer? Browser chrome (Page
// URL) and generic embedded form fields never match, because none carry the
// app's composer identity. Zero or multiple matches must fail closed upstream.
func editableIsNativeComposer(_ e: EditableInfo, _ profile: ComposerProfile) -> Bool {
    if editableIsBrowserChrome(e) { return false }
    let id = e.title.lowercased()
    let val = e.placeholder.lowercased()
    switch profile {
    case .claude:
        if id == "prompt" || id.hasPrefix("prompt ") || id.hasSuffix(" prompt") { return true }
        return id.contains("type / for commands") || val.contains("type / for commands")
    case .codex, .zcode:
        // Codex/ZCode composer: AXTextArea whose non-draft field identity is
        // "Ask for follow-up changes" (the value may prefix a ⏎ glyph). Require
        // the AXTextArea role so a same-named button/label can't match.
        guard e.role == "AXTextArea" else { return false }
        return id.contains("ask for follow-up changes") || val.contains("ask for follow-up changes")
    }
}

// Backward-compatible Claude alias (the pre-R4 Prompt-only identity).
func editableIsNativePrompt(_ e: EditableInfo) -> Bool {
    editableIsNativeComposer(e, .claude)
}

private let nativeEditableRoles: Set<String> = ["AXTextArea", "AXTextField", "AXComboBox"]

// The embedded Browser pane's address bar is a native (non-web) AXTextField but
// is NEVER the chat composer. Exclude it (and any URL field) so a window that
// only holds a Browser pane is not mistaken for a chat window.
func editableIsBrowserChrome(_ e: EditableInfo) -> Bool {
    let t = e.title.lowercased()
    return t == "page url" || t.contains("url") || t == "address"
}

// A window holds a usable native composer if it carries the profile's composer
// identity, or (looser fallback for display/tree only) a non-web native text
// field that is not browser chrome.
func windowHasNativeComposer(_ eds: [EditableInfo], _ profile: ComposerProfile = .claude) -> Bool {
    eds.contains { editableIsNativeComposer($0, profile) }
        || eds.contains { !$0.inWebArea && nativeEditableRoles.contains($0.role) && !editableIsBrowserChrome($0) }
}

// Result of conversation-window selection. Fails closed (PR78 R2+R3):
//  - `none`         : no PROVEN native Prompt composer in any window (auto). Never
//                     falls back to window 0 or a generic editable (R3 item 4).
//  - `ambiguous`    : auto mode, >1 native Prompt window (R2).
//  - `invalidIndex` : an explicit index is negative or >= window count. Rejected,
//                     never clamped (R3 item 2).
enum ConvWindowPick: Equatable {
    case index(Int)
    case none
    case ambiguous
    case invalidIndex
}

// Choose the conversation window across all app windows. Explicit index (when the
// caller passes one) always wins — auto/unset is a distinct nil, so an explicit
// index 0 is honored — but an out-of-range/negative explicit index is REJECTED,
// not clamped (R3 item 2). Auto is COMPOSER-IDENTITY-ONLY for the given app
// profile (R4): exactly one window carrying that profile's native composer
// identity -> that window; >1 -> ambiguous; zero -> none (never window 0 or a
// generic editable, R3 items 1+4). This is how Codex's two same-title "ChatGPT"
// windows disambiguate — only the chat window carries the "Ask for follow-up
// changes" composer; the avatar-overlay shell has none. The SAME resolver drives
// ring/state/confirm/type and every post-send refresh so verification never
// drifts to another window.
func pickConversationWindow(_ windows: [[EditableInfo]], preferIndex: Int?,
                            profile: ComposerProfile = .claude) -> ConvWindowPick {
    guard !windows.isEmpty else { return .none }
    if let idx = preferIndex {
        guard idx >= 0 && idx < windows.count else { return .invalidIndex }
        return .index(idx)
    }
    let composerWindows = windows.indices.filter { i in
        windows[i].contains { editableIsNativeComposer($0, profile) }
    }
    if composerWindows.count > 1 { return .ambiguous }
    if let i = composerWindows.first { return .index(i) }
    return .none
}
