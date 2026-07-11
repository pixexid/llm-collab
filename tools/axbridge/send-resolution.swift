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

// The native chat composer's identity. `title` carries its non-value identity
// (AXTitle / AXDescription / AXPlaceholderValue / AXHelp — whichever the app
// exposes "Prompt" on); `placeholder` carries the value/placeholder text
// ("Type / for commands"). The composer is identified STRICTLY by this identity
// — NOT by web-area membership: Claude/Codex are Electron apps whose whole UI is
// under an AXWebArea, so the native composer itself reports inWebArea=true. The
// "Prompt" identity is the strict proof (R3 item 1) and precisely excludes every
// other editable — browser chrome (Page URL) and embedded form fields (a "Name",
// "Enter your address", etc.) — because none of them carry it.
func editableIsNativePrompt(_ e: EditableInfo) -> Bool {
    if editableIsBrowserChrome(e) { return false }
    let id = e.title.lowercased()
    let val = e.placeholder.lowercased()
    if id == "prompt" || id.hasPrefix("prompt ") || id.hasSuffix(" prompt") { return true }
    return id.contains("type / for commands") || val.contains("type / for commands")
}

private let nativeEditableRoles: Set<String> = ["AXTextArea", "AXTextField", "AXComboBox"]

// The embedded Browser pane's address bar is a native (non-web) AXTextField but
// is NEVER the chat composer. Exclude it (and any URL field) so a window that
// only holds a Browser pane is not mistaken for a chat window.
func editableIsBrowserChrome(_ e: EditableInfo) -> Bool {
    let t = e.title.lowercased()
    return t == "page url" || t.contains("url") || t == "address"
}

// A window holds a usable native composer if it has the Prompt identity, or a
// non-web native text field that is not browser chrome.
func windowHasNativeComposer(_ eds: [EditableInfo]) -> Bool {
    eds.contains(where: editableIsNativePrompt)
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
// not clamped (R3 item 2). Auto is PROMPT-ONLY: exactly one native Prompt window
// -> that window; >1 -> ambiguous; zero -> none (never window 0 or a generic
// editable, R3 items 1+4). The SAME resolver drives ring/state/confirm/type and
// every post-send refresh so verification never drifts to another window.
// Composer strictness within the chosen window (Prompt-only) is enforced by
// windowComposer/promptComposer at the AX layer.
func pickConversationWindow(_ windows: [[EditableInfo]], preferIndex: Int?) -> ConvWindowPick {
    guard !windows.isEmpty else { return .none }
    if let idx = preferIndex {
        guard idx >= 0 && idx < windows.count else { return .invalidIndex }
        return .index(idx)
    }
    let promptWindows = windows.indices.filter { windows[$0].contains(where: editableIsNativePrompt) }
    if promptWindows.count > 1 { return .ambiguous }
    if let i = promptWindows.first { return .index(i) }
    return .none
}
