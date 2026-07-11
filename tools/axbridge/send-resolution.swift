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
