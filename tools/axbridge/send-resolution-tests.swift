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

if failures == 0 { print("\nALL PASS (send-resolution)"); exit(0) }
else { print("\n\(failures) FAILURE(S)"); exit(1) }
}
}
