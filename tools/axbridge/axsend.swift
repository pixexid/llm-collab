// axsend — focus-independent AX bridge for ringing agent app composers.
//
// Uses the macOS Accessibility API (AXUIElement) to read an app's UI tree,
// set text in its composer, and press its send button WITHOUT raising the
// window or stealing keyboard focus from whatever the operator is doing.
//
// Build:  swiftc -O axsend.swift -o axsend
// Trust:  the process running this (Terminal/the binary) must be enabled in
//         System Settings > Privacy & Security > Accessibility.
//
// Commands:
//   axsend tree   --app <name> [--max-depth N] [--editable-only]
//   axsend ring   --app <name> --text "<msg>" [--submit] [--window-index N]
//   axsend check
//
// "ring" sets the value of the deepest/last editable text element (the
// composer) and, with --submit, presses the nearest following AXButton whose
// title/description looks like a send control.

import Cocoa
import ApplicationServices
import CoreGraphics

// Post a Return keypress directly to a process (no focus steal, no window raise)
// — the universal "submit" for chat composers whose Send button ignores AXPress.
func postReturnKey(pid: pid_t, command: Bool = false) {
    let src = CGEventSource(stateID: .combinedSessionState)
    let down = CGEvent(keyboardEventSource: src, virtualKey: 0x24, keyDown: true)  // 0x24 = Return
    let up = CGEvent(keyboardEventSource: src, virtualKey: 0x24, keyDown: false)
    // Cmd+Return is the submit gesture for code-editor composers (ZCode et al.)
    // where a plain Return inserts a newline instead of sending.
    if command { down?.flags = .maskCommand; up?.flags = .maskCommand }
    down?.postToPid(pid)
    up?.postToPid(pid)
}

// Type text as real key events into a process (no focus steal). For composers
// that reject AXValue writes (Electron code-editors: ZCode, Antigravity), this
// is the only way to populate the field. keyboardSetUnicodeString lets us post
// arbitrary characters without keycode mapping; postToPid targets the bg app.
func typeUnicode(pid: pid_t, _ text: String) {
    let src = CGEventSource(stateID: .combinedSessionState)
    for scalar in text.unicodeScalars {
        var chars = Array(String(scalar).utf16)
        if let down = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: true) {
            down.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
            down.postToPid(pid)
        }
        if let up = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: false) {
            up.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
            up.postToPid(pid)
        }
        usleep(2000)  // small gap so editors process each input event
    }
}

// Select-all + delete via key events — to clear an editor composer that ignores
// AXValue writes before typing a fresh message.
func selectAllAndDelete(pid: pid_t) {
    let src = CGEventSource(stateID: .combinedSessionState)
    func key(_ kc: CGKeyCode, _ flags: CGEventFlags = []) {
        let d = CGEvent(keyboardEventSource: src, virtualKey: kc, keyDown: true); d?.flags = flags; d?.postToPid(pid)
        let u = CGEvent(keyboardEventSource: src, virtualKey: kc, keyDown: false); u?.flags = flags; u?.postToPid(pid)
        usleep(15_000)
    }
    // A cold Electron composer (ZCode/Antigravity) ignores the first key chord
    // until a key event has woken it — a cold Cmd+A then no-ops and the Backspace
    // deletes a single char (leaving a partial draft that concatenates with the
    // next message). Wake focus with a benign cursor move first, then clear with
    // TWO select-all strategies (different editors honor different ones), each
    // followed by delete. Idempotent on an already-empty field.
    key(0x7C)                                  // Right arrow — wake the field, no content change
    usleep(40_000)
    key(0x00, .maskCommand)                    // Cmd+A (select all)
    key(0x33)                                  // Backspace (delete selection)
    usleep(20_000)
    key(0x7D, .maskCommand)                    // Cmd+Down — cursor to absolute end
    key(0x7E, [.maskCommand, .maskShift])      // Cmd+Shift+Up — select to start
    key(0x33)                                  // Backspace (delete selection)
    usleep(20_000)
}

// Set the composer text: AXValue if the field accepts it, else key-event typing
// (Electron code-editors like ZCode/Antigravity reject AXValue). Returns false
// only if neither path put the text in.
func setComposerText(_ composer: AXUIElement, pid: pid_t, _ text: String) -> Bool {
    AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(80_000)
    AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, text as CFString)
    usleep(90_000)
    func current() -> String { str(composer, kAXValueAttribute) ?? "" }
    if text.isEmpty {
        // Clearing a (possibly stuck) draft. Don't gate on the AXValue readback:
        // Electron composers (ZCode/Antigravity) don't reflect the draft through
        // AXValue, so `current().isEmpty` is true even when a draft is stuck —
        // which would skip the clear AND falsely report success. Always issue the
        // key-event select-all+delete (best effort) and report success; verify
        // separately with `axsend confirm` (the AXValue readback can't be trusted
        // for these apps).
        selectAllAndDelete(pid: pid)
        usleep(120_000)
        return true
    }
    func has() -> Bool { current().contains(String(text.prefix(20))) }
    if has() { return true }
    // AXValue rejected — clear any draft and type as real key events.
    selectAllAndDelete(pid: pid)
    typeUnicode(pid: pid, text)
    usleep(120_000)
    if has() { return true }
    // Electron composers (ZCode/Antigravity) accept key events but do NOT
    // reflect the typed text back through AXValue, so has() stays false even
    // though the text is visibly in the field. Returning false here is the bug
    // that bit us: the caller treats it as "could not put text", aborts before
    // submitting, and LEAVES the just-typed keystrokes stuck in the composer.
    // Trust the keystrokes instead — the submit step's messageLanded check is
    // the real proof, and a genuine type failure is caught there (nothing
    // lands) and the draft is cleared on the failure path.
    return true
}

func cmdType(app: String, text: String, submit: Bool, verify: Bool) -> Int32 {
    guard AXIsProcessTrusted() else {
        FileHandle.standardError.write("AX not trusted; run `axsend check`.\n".data(using: .utf8)!); return 2
    }
    guard let (el, pid) = appElement(named: app), let win = windows(el).first else {
        FileHandle.standardError.write("app/window not found: \(app)\n".data(using: .utf8)!); return 1
    }
    guard let composer = findComposer(win) else {
        FileHandle.standardError.write("no composer found\n".data(using: .utf8)!); return 3
    }
    if isProcessing(win) {
        FileHandle.standardError.write("target busy — not typing.\n".data(using: .utf8)!); return 8
    }
    // Focus the composer so the key events land in it, clear any stale draft
    // (e.g. a stray newline left by a prior failed submit), then type.
    AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    usleep(120_000)
    if !text.isEmpty { selectAllAndDelete(pid: pid) }
    typeUnicode(pid: pid, text)
    print("typed \(text.count) chars into \(app)")
    var method = ""
    if submit {
        usleep(120_000)
        if isProcessing(win) {
            FileHandle.standardError.write("became busy before submit — skipped.\n".data(using: .utf8)!); return 8
        }
        func landed() -> Bool {
            Thread.sleep(forTimeInterval: 1.0)
            let fresh = windows(appElement(named: app)?.0 ?? el).first ?? win
            return messageLanded(fresh, sentText: text)
        }
        // Cmd+Return first — code-editor composers (ZCode) treat plain Return as
        // a newline, not a send. Fall back to plain Return for Enter-to-send apps.
        postReturnKey(pid: pid, command: true)
        if landed() { method = "cmd-return" }
        if method.isEmpty {
            AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            postReturnKey(pid: pid)
            if landed() { method = "key-return" }
        }
    }
    if verify {
        if submit {
            if !method.isEmpty { print("VERIFIED: submitted via \(method)") }
            else {
                // No method landed — clear the just-typed text so a failed send
                // never leaves a stuck draft in the composer.
                selectAllAndDelete(pid: pid)
                FileHandle.standardError.write("WARN: not landed after cmd-return + key-return — cleared the draft\n".data(using: .utf8)!); return 7
            }
        } else {
            Thread.sleep(forTimeInterval: 1.2)
            let fresh = windows(appElement(named: app)?.0 ?? el).first ?? win
            let v = findComposer(fresh).flatMap { str($0, kAXValueAttribute) } ?? ""
            if v.contains(String(text.prefix(20))) { print("VERIFIED: text is in the composer") }
            else { FileHandle.standardError.write("WARN: typed text not visible in composer\n".data(using: .utf8)!); return 7 }
        }
    }
    return 0
}

// MARK: - AX helpers

func attr(_ el: AXUIElement, _ key: String) -> AnyObject? {
    var value: AnyObject?
    let err = AXUIElementCopyAttributeValue(el, key as CFString, &value)
    return err == .success ? value : nil
}

func str(_ el: AXUIElement, _ key: String) -> String? {
    attr(el, key) as? String
}

func children(_ el: AXUIElement) -> [AXUIElement] {
    (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

func role(_ el: AXUIElement) -> String { str(el, kAXRoleAttribute) ?? "" }

func label(_ el: AXUIElement) -> String {
    // Best human-readable identifier for a control.
    for k in [kAXTitleAttribute, kAXDescriptionAttribute, "AXPlaceholderValue",
              "AXHelp", kAXValueAttribute] {
        if let s = str(el, k), !s.isEmpty { return s }
    }
    return ""
}

let editableRoles: Set<String> = ["AXTextArea", "AXTextField", "AXComboBox"]

func isEditable(_ el: AXUIElement) -> Bool {
    if editableRoles.contains(role(el)) { return true }
    // contenteditable web nodes surface as AXTextArea via Chromium, but some
    // Electron composers expose AXGroup with a settable AXValue.
    var settable = DarwinBoolean(false)
    if AXUIElementIsAttributeSettable(el, kAXValueAttribute as CFString, &settable) == .success {
        return settable.boolValue && (attr(el, kAXValueAttribute) != nil)
    }
    return false
}

func sendButtonScore(_ el: AXUIElement) -> Int {
    guard role(el) == "AXButton" else { return 0 }
    let t = label(el).lowercased()
    if t.contains("send") || t.contains("submit") { return 3 }
    if t.isEmpty { return 1 } // unlabeled icon button near the composer — likely send
    return 0
}

// MARK: - App lookup

// Electron/Chromium apps hide their web accessibility tree until a client
// opts in. Setting AXManualAccessibility (what VoiceOver does) wakes it up so
// kAXWindowsAttribute and the composer become visible — without focus.
func enableManualAX(_ appEl: AXUIElement) {
    AXUIElementSetAttributeValue(appEl, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    AXUIElementSetAttributeValue(appEl, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
}

func appElement(named name: String) -> (AXUIElement, pid_t)? {
    let target = name.lowercased()
    // An app can have several processes sharing a name (GPU/helper/menu-extra).
    // Match candidates, then prefer a regular (Dock-icon) app that actually
    // exposes windows — the dock/menu-extra helpers report 0 windows.
    let matches = NSWorkspace.shared.runningApplications.filter { app in
        let local = (app.localizedName ?? "").lowercased()
        let bundle = (app.bundleIdentifier ?? "").lowercased()
        return local == target || bundle == target || local.contains(target) || bundle.contains(target)
    }
    let ranked = matches.sorted { a, b in
        let ar = a.activationPolicy == .regular ? 0 : 1
        let br = b.activationPolicy == .regular ? 0 : 1
        return ar < br
    }
    for app in ranked {
        let el = AXUIElementCreateApplication(app.processIdentifier)
        enableManualAX(el)
        if windows(el).count > 0 { return (el, app.processIdentifier) }
    }
    // Fall back to the best-ranked match even if windows aren't visible yet.
    if let app = ranked.first {
        let el = AXUIElementCreateApplication(app.processIdentifier)
        enableManualAX(el)
        return (el, app.processIdentifier)
    }
    return nil
}

func windows(_ appEl: AXUIElement) -> [AXUIElement] {
    (attr(appEl, kAXWindowsAttribute) as? [AXUIElement]) ?? []
}

// MARK: - Tree walk

func walk(_ el: AXUIElement, depth: Int, maxDepth: Int, editableOnly: Bool,
          collect: inout [(AXUIElement, Int)]) {
    let r = role(el)
    let show = !editableOnly || isEditable(el) || r == "AXButton"
    if show { collect.append((el, depth)) }
    if depth >= maxDepth { return }
    for c in children(el) {
        walk(c, depth: depth + 1, maxDepth: maxDepth, editableOnly: editableOnly, collect: &collect)
    }
}

func flatten(_ el: AXUIElement, _ maxDepth: Int = 40) -> [AXUIElement] {
    var out: [AXUIElement] = []
    func rec(_ e: AXUIElement, _ d: Int) {
        out.append(e)
        if d >= maxDepth { return }
        for c in children(e) { rec(c, d + 1) }
    }
    rec(el, 0)
    return out
}

// MARK: - Commands

func cmdAttrs(app: String) -> Int32 {
    guard let (el, pid) = appElement(named: app) else {
        FileHandle.standardError.write("app not found: \(app)\n".data(using: .utf8)!)
        return 1
    }
    var names: CFArray?
    AXUIElementCopyAttributeNames(el, &names)
    let attrNames = (names as? [String]) ?? []
    print("# app=\(app) pid=\(pid)")
    print("attributes: \(attrNames.joined(separator: ", "))")
    for n in attrNames {
        if let v = attr(el, n) {
            let arr = v as? [AXUIElement]
            let desc = arr != nil ? "[\(arr!.count) elements]" : String(describing: v).prefix(60)
            print("  \(n) = \(desc)")
        }
    }
    let kids = children(el)
    print("AXChildren roles: \(kids.map { role($0) }.joined(separator: ", "))")
    return 0
}

func frame(_ el: AXUIElement) -> (x: Double, y: Double, w: Double, h: Double)? {
    guard let posV = attr(el, kAXPositionAttribute), let sizeV = attr(el, kAXSizeAttribute) else { return nil }
    var p = CGPoint.zero; var s = CGSize.zero
    AXValueGetValue(posV as! AXValue, .cgPoint, &p)
    AXValueGetValue(sizeV as! AXValue, .cgSize, &s)
    return (Double(p.x), Double(p.y), Double(s.width), Double(s.height))
}

func subrole(_ el: AXUIElement) -> String { str(el, kAXSubroleAttribute) ?? "" }

func cmdButtons(app: String) -> Int32 {
    guard let (el, _) = appElement(named: app) else {
        FileHandle.standardError.write("app not found: \(app)\n".data(using: .utf8)!); return 1
    }
    for (i, w) in windows(el).enumerated() {
        let all = flatten(w)
        let composerY = all.last(where: { isEditable($0) && !(str($0, "AXPlaceholderValue") ?? "").isEmpty })
            .flatMap { frame($0)?.y }
        print("## window[\(i)] composerY=\(composerY.map { String(format: "%.0f", $0) } ?? "?")")
        for e in all where role(e) == "AXButton" {
            let f = frame(e)
            let fs = f.map { String(format: "x=%.0f y=%.0f w=%.0f h=%.0f", $0.x, $0.y, $0.w, $0.h) } ?? "no-frame"
            print("  AXButton sub=\"\(subrole(e))\" label=\"\(label(e).prefix(24))\" \(fs)")
        }
    }
    return 0
}

// Conversation message texts (AXStaticText) and processing state, read straight
// from the AX tree — the reliable post-send check (the composer reading empty
// does NOT prove a send landed).
func conversationTexts(_ win: AXUIElement) -> [String] {
    flatten(win).filter { role($0) == "AXStaticText" }
        .compactMap { str($0, kAXValueAttribute) }
        .filter { !$0.isEmpty }
}

func isProcessing(_ win: AXUIElement) -> Bool {
    // The generating-state "stop" button: labeled exactly "stop" (Codex) or
    // "stop generating"/"stop streaming"/"stop response" (other agents), and it
    // sits in the composer's column. A loose contains("stop") substring match
    // falsely tripped on a sidebar chat titled "Stop booking generation loop"
    // (row button "Open Stop booking generation loop") — reporting a phantom
    // Stop button forever and gating EVERY submit to that app. Match the real
    // control by label shape + composer-column position instead.
    let composerX = findComposer(win).flatMap { frame($0)?.x }
    return flatten(win).contains { el in
        guard role(el) == "AXButton" else { return false }
        let l = label(el).lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        // Generating-state controls differ per app:
        //  - Codex / Claude / ZCode: a button labeled exactly "stop" (or
        //    "stop generating|streaming|response").
        //  - Antigravity (Gemini): NO "stop" label — instead a "Thinking for Ns"
        //    indicator and a "Cancel (⌃C)" interrupt button, both only present
        //    while generating ("Thought for Ns" / no Cancel = done).
        let isStop = l == "stop" || l.hasPrefix("stop generating")
            || l.hasPrefix("stop streaming") || l.hasPrefix("stop response")
        let isGenerating = l.hasPrefix("thinking for") || l.hasPrefix("cancel (⌃c") || l.hasPrefix("cancel (^c")
        guard isStop || isGenerating else { return false }
        // Real control lives with the composer; reject anything far to its left
        // (sidebar/chrome). Fall through to label-only if no composer found.
        if let cx = composerX, let f = frame(el), f.x < cx - 60 { return false }
        return true
    }
}

func findComposer(_ win: AXUIElement) -> AXUIElement? {
    let all = flatten(win)
    func placeholder(_ e: AXUIElement) -> String { str(e, "AXPlaceholderValue") ?? "" }
    return all.last(where: { role($0) == "AXTextArea" })
        ?? all.last(where: { role($0) == "AXTextField" })
        ?? all.last(where: { isEditable($0) && !placeholder($0).isEmpty })
        ?? all.last(where: { isEditable($0) })
}

func messageLanded(_ win: AXUIElement, sentText: String) -> Bool {
    // Match a longer prefix (40) so a short, non-unique opening can't collide with
    // a stale older turn. NOTE: standalone `confirm` has no before/after baseline,
    // so it's a best-effort re-check — the authoritative freshness-aware verify is
    // in `ring --submit` (it counts NEW turns against a pre-send baseline).
    let needle = String(sentText.prefix(40))
    guard !needle.isEmpty else { return true }
    let composer = findComposer(win)
    // NECESSARY: the text must have LEFT the composer. If the composer still
    // holds it, the send did not submit (recipient was busy / press no-op) — no
    // amount of conversation-text matching can override a stuck draft.
    if let c = composer, let v = str(c, kAXValueAttribute), v.contains(needle) {
        return false
    }
    // SUFFICIENT: it now appears as a real conversation message, ABOVE the
    // composer (the draft, if echoed, renders at/below the composer top).
    let composerTop = composer.flatMap { frame($0)?.y }
    return flatten(win).contains { e in
        guard role(e) == "AXStaticText",
              let v = str(e, kAXValueAttribute), v.contains(needle) else { return false }
        if let top = composerTop, let f = frame(e) { return f.y < top - 4 }
        return true
    }
}

func cmdState(app: String) -> Int32 {
    guard let (el, _) = appElement(named: app) else {
        FileHandle.standardError.write("app not found: \(app)\n".data(using: .utf8)!); return 1
    }
    guard let win = windows(el).first else { print("no windows"); return 1 }
    print("processing: \(isProcessing(win) ? "YES (Stop button present)" : "no")")
    // The chat column shares the composer's x band; side panels (changes/diff,
    // sidebar) sit far left/right of it. Filter the display to that band so
    // recent messages are real conversation, not UI chrome.
    // Real chat-message text renders with a non-trivial width/height; collapsed
    // side-panel chrome (changes/diff/sidebar) reports zero or tiny frames.
    let msgs = flatten(win).filter { role($0) == "AXStaticText" }.compactMap { e -> String? in
        guard let v = str(e, kAXValueAttribute), v.count >= 6 else { return nil }
        guard let f = frame(e), f.w >= 60, f.h >= 12 else { return nil }
        return v
    }
    print("recent messages:")
    for t in msgs.suffix(6) {
        print("  • \(t.replacingOccurrences(of: "\n", with: " ").prefix(90))")
    }
    return 0
}

func cmdCheck() -> Int32 {
    let trusted = AXIsProcessTrusted()
    if trusted {
        print("AX trusted: YES")
        return 0
    }
    print("AX trusted: NO")
    print("Enable the controlling process in System Settings > Privacy & Security > Accessibility.")
    return 2
}

func cmdTree(app: String, maxDepth: Int, editableOnly: Bool) -> Int32 {
    guard let (el, pid) = appElement(named: app) else {
        FileHandle.standardError.write("app not found: \(app)\n".data(using: .utf8)!)
        return 1
    }
    print("# app=\(app) pid=\(pid) windows=\(windows(el).count)")
    for (i, w) in windows(el).enumerated() {
        print("## window[\(i)] title=\(str(w, kAXTitleAttribute) ?? "")")
        var items: [(AXUIElement, Int)] = []
        walk(w, depth: 0, maxDepth: maxDepth, editableOnly: editableOnly, collect: &items)
        for (e, d) in items {
            let v = (str(e, kAXValueAttribute) ?? "").prefix(40).replacingOccurrences(of: "\n", with: "⏎")
            let edit = isEditable(e) ? " [EDITABLE]" : ""
            let sb = sendButtonScore(e) > 0 ? " [SEND?\(sendButtonScore(e))]" : ""
            print(String(repeating: "  ", count: d) + "\(role(e)) \"\(label(e).prefix(40))\" val=\"\(v)\"\(edit)\(sb)")
        }
    }
    return 0
}

func cmdRing(app: String, text: String, submit: Bool, windowIndex: Int, dryRun: Bool, verify: Bool) -> Int32 {
    guard AXIsProcessTrusted() else {
        FileHandle.standardError.write("AX not trusted; run `axsend check`.\n".data(using: .utf8)!)
        return 2
    }
    guard let (el, pid) = appElement(named: app) else {
        FileHandle.standardError.write("app not found: \(app)\n".data(using: .utf8)!)
        return 1
    }
    let wins = windows(el)
    guard !wins.isEmpty else {
        FileHandle.standardError.write("no windows for \(app)\n".data(using: .utf8)!)
        return 1
    }
    let win = wins[max(0, min(windowIndex, wins.count - 1))]
    let all = flatten(win)

    // Composer selection, best-first. A real AXTextArea/AXTextField is the most
    // reliable (works even when it already holds text and shows no placeholder);
    // placeholder-bearing node and last-editable are looser fallbacks.
    // Role priority: a real AXTextArea/AXTextField is the visible composer;
    // AXComboBox is often an autocomplete wrapper (ZCode), so try it only after.
    // Within a role, prefer the node that carries a placeholder (the input).
    func placeholder(_ e: AXUIElement) -> String { str(e, "AXPlaceholderValue") ?? "" }
    func pick(_ r: String) -> AXUIElement? {
        all.last(where: { role($0) == r && !placeholder($0).isEmpty }) ?? all.last(where: { role($0) == r })
    }
    let composer = pick("AXTextArea") ?? pick("AXTextField") ?? pick("AXComboBox")
        ?? all.last(where: { isEditable($0) && !placeholder($0).isEmpty })
        ?? all.last(where: { isEditable($0) })
    guard let composer = composer else {
        FileHandle.standardError.write("no editable composer found in window\n".data(using: .utf8)!)
        return 3
    }
    // AXValue if the field accepts it, else key-event typing (Electron editors
    // like ZCode/Antigravity reject AXValue and need real keystrokes).
    if !setComposerText(composer, pid: pid, text) {
        FileHandle.standardError.write("could not put text in composer (AXValue rejected and key-typing failed)\n".data(using: .utf8)!)
        return 4
    }
    print("composer set (role=\(role(composer)))")

    if submit {
        // Prefer the best-scoring send button that comes AFTER the composer.
        // Geometry-based send-button pick: the send arrow sits in the composer's
        // own toolbar row (same vertical band, just below the text). Take the
        // RIGHTMOST button in that band, excluding window controls and the known
        // non-send toolbar controls. This avoids grabbing a sidebar/window button.
        let windowControls: Set<String> = ["AXCloseButton", "AXMinimizeButton", "AXZoomButton",
                                            "AXFullScreenButton", "AXToolbarButton"]
        let nonSendLabels = ["add files", "custom", "medium", "dictate", "model", "attach",
                             "more", "agent", "branch", "environment",
                             // side-effecting controls that must never be pressed as "send"
                             "voice", "record", "memo", "mic", "microphone", "audio",
                             "image", "photo", "camera", "screenshot", "settings"]
        let buttons = all.filter { role($0) == "AXButton" }
        var button: AXUIElement? = nil
        if let cf = frame(composer) {
            let bandTop = cf.y - 12
            let bandBottom = cf.y + cf.h + 90
            let inBand = buttons.filter { b in
                guard !windowControls.contains(subrole(b)) else { return false }
                guard let bf = frame(b), bf.y >= bandTop, bf.y <= bandBottom else { return false }
                let lbl = label(b).lowercased()
                return !nonSendLabels.contains(where: { lbl.contains($0) })
            }
            // Prefer an unlabeled icon button (the send arrow); else rightmost.
            let preferred = inBand.filter { label($0).isEmpty }
            button = (preferred.isEmpty ? inBand : preferred)
                .max { (frame($0)?.x ?? -1) < (frame($1)?.x ?? -1) }
        }
        // Fallback: explicitly send/submit-labeled button anywhere.
        if button == nil {
            button = buttons.first { label($0).lowercased().contains("send") || label($0).lowercased().contains("submit") }
        }
        // Only press the resolved button if it is a CONFIDENT send target: an
        // unlabeled icon button (the send arrow) or one labeled send/submit. A
        // labeled non-send button (e.g. "Record voice memo") is never pressed —
        // we fall straight to AXConfirm/key-return, which is side-effect-free.
        let buttonOK: Bool = {
            guard let b = button else { return false }
            let l = label(b).lowercased()
            return l.isEmpty || l.contains("send") || l.contains("submit")
        }()
        let bf = button.flatMap(frame).map { String(format: "x=%.0f y=%.0f", $0.x, $0.y) } ?? "none"
        let tgt = button.map { "label=\"\(label($0).prefix(20))\" sub=\"\(subrole($0))\" \(bf)" } ?? "no button"
        if dryRun {
            print(buttonOK
                ? "DRY-RUN send target: \(tgt) — will press, then AXConfirm/key-return (not pressed)"
                : "DRY-RUN no confident send button (resolved: \(tgt)) — will submit via AXConfirm/key-return only (not pressed)")
            return 0
        }
        // Sending to a BUSY recipient is allowed and SAFE: the app queues the
        // message (it renders when the current turn ends) — queueing is insurance
        // that the receiver gets it without the sender polling for idle, and it
        // does NOT corrupt the running turn (only a forced steer would). So we do
        // NOT abort on busy. The submit cascade below classifies the result: a FRESH
        // turn with our text → DELIVERED; no fresh turn while busy → QUEUED
        // (UNCONFIRMED — busy alone can't prove the text entered THIS thread vs a new
        // one). A first successful submit empties the composer, so the remaining
        // cascade methods can't re-queue a duplicate. Sender discipline: don't re-ring
        // the same message repeatedly; the mailbox packet is the durable record.
        // Submit via multiple mechanisms — some composers (Claude Desktop) ignore
        // AXPress on the Send button. Try in order, verifying after each; stop at
        // the first that actually lands the message as a real turn.
        // Freshness baseline: count the real conversation turns that ALREADY
        // contain this text BEFORE we submit. A delivery is confirmed only when a
        // NEW turn appears (the count increases) — so a stale older turn with the
        // same text can't false-confirm, and the retry can't be fooled by a copy
        // it (or a prior attempt) already sent.
        let needle = String(text.prefix(40))
        func turnsWithNeedle(_ w: AXUIElement) -> Int {
            guard !needle.isEmpty else { return 0 }
            let top = findComposer(w).flatMap { frame($0)?.y }
            return flatten(w).filter { e in
                guard role(e) == "AXStaticText", let v = str(e, kAXValueAttribute),
                      v.contains(needle), let f = frame(e) else { return false }
                if let t = top { return f.y < t - 4 }   // above the composer = a real turn, not the draft
                return true
            }.count
        }
        let baseline = turnsWithNeedle(win)
        func deliveredFresh() -> Bool {
            Thread.sleep(forTimeInterval: 1.0)
            let w = windows(appElement(named: app)?.0 ?? el).first ?? win
            return turnsWithNeedle(w) > baseline
        }
        func keyReturn(command: Bool = false) {
            AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            postReturnKey(pid: pid, command: command)
        }
        // NOTE: there is intentionally NO fire-and-forget path. The old `--no-verify`
        // single-press did ONE plain Return, which on a code-editor composer
        // (ZCode/Antigravity) inserts a NEWLINE instead of sending and silently
        // strands the text. `--submit` ALWAYS runs the enforced verify+retry below,
        // so a send can never be left unconfirmed or stuck. (`verify` is ignored.)
        // Enforced verify + auto-retry. Verification is NOT a separate step a
        // worker can forget: the submit cascade runs, EACH method is confirmed by
        // landed() (the text appears as a real conversation turn), and if no method
        // lands the draft is cleared and the WHOLE cascade retries. `ring --submit`
        // returns 0 ONLY on a confirmed delivery, non-zero (7) after all attempts.
        var method = ""
        var queued = false
        let maxAttempts = 3
        func busyNow() -> Bool { isProcessing(windows(appElement(named: app)?.0 ?? el).first ?? win) }
        attempts: for attempt in 1...maxAttempts {
            if attempt > 1 {
                // A prior submit may have landed just after our check — re-confirm a
                // FRESH turn before resending so we never double-send.
                if deliveredFresh() { method = "confirmed on re-check"; break attempts }
                // If the recipient is now busy, the prior submit was ACCEPTED and is
                // queued (renders when the current turn ends) — never retry into a
                // busy agent (that double-sends). Report queued.
                if busyNow() { queued = true; break attempts }
                _ = setComposerText(composer, pid: pid, text)   // repopulate cleanly (clear + retype)
            }
            // 1. confident send button
            if buttonOK, let b = button {
                AXUIElementPerformAction(b, kAXPressAction as CFString)
                if deliveredFresh() { method = "button-press"; break attempts }
            }
            // 2. AXConfirm on the composer
            AXUIElementPerformAction(composer, kAXConfirmAction as CFString)
            if deliveredFresh() { method = "composer-confirm"; break attempts }
            // 3. Cmd+Return (code-editor composers — plain Return inserts a newline)
            keyReturn(command: true)
            if deliveredFresh() { method = "cmd-return"; break attempts }
            // 4. plain Return (Enter-to-send composers)
            keyReturn()
            if deliveredFresh() { method = "key-return"; break attempts }
            // No FRESH turn this attempt. If the recipient is now busy, our submit
            // was accepted + queued — stop (retrying would double-send).
            if busyNow() { queued = true; break attempts }
            // NEVER blind-resend (the 3x-duplicate bug, operator-caught twice
            // 2026-07-11: on a non-chat/landing screen the submit IS accepted
            // where this window can't see it, deliveredFresh() stays false, and
            // each retry landed another copy). Only retry when the composer
            // VERIFIABLY still holds our text — a readable leftover proves the
            // submit was ignored, so retrying can't duplicate.
            let leftover = str(composer, kAXValueAttribute) ?? ""
            if leftover.contains(String(text.prefix(20))) {
                selectAllAndDelete(pid: pid)
            } else {
                // Composer readback is blank/unreadable. On Electron composers
                // AXValue is blank even when a draft is stuck, so we CANNOT tell
                // "submit accepted" from "submit no-op with the text still stuck".
                // Do NOT resend (anti-duplicate) and do NOT set `queued` — a
                // QUEUED (UNCONFIRMED) print makes bin/axsend-ensure exit 0 without
                // running the follow-up confirm, silently losing a handoff that
                // never landed (bot P1, PR #74). Break and fall through to the
                // NOT-DELIVERED path: the final-settle deliveredFresh() check
                // below still promotes a real late delivery to VERIFIED, and a
                // genuine no-op returns non-zero so the wrapper/caller runs
                // `axsend confirm` (the conversation-turn check is the only
                // reliable signal for these apps).
                break attempts
            }
        }
        // ZCode can accept the submit and start processing before its sent-message
        // AXStaticText is visible to the one-second per-method checks above.
        // Give the conversation tree one last chance to expose the fresh turn
        // before classifying the delivery as queued or failed.
        for _ in 0..<4 {
            if deliveredFresh() {
                method = "confirmed on final settle"
                break
            }
        }
        if !method.isEmpty {
            // A FRESH turn with our text appeared above the composer → the message
            // is DELIVERED (a visible conversation turn), full stop. Do NOT call this
            // "queued": a delivered turn is not queued. busyNow() here only means the
            // recipient has STARTED processing the message we just delivered (normal)
            // — OR that the send spawned a NEW thread that is now running (the
            // landing-screen hazard). Either way the text was submitted as a turn, so
            // reporting "QUEUED behind its current run" was wrong feedback (the bug
            // the operator caught 2026-06-22).
            print("VERIFIED: submitted via \(method) — delivered as a conversation turn.")
            return 0
        }
        if queued {
            // Recipient went busy during the cascade but NO fresh turn rendered. The
            // message was likely accepted and queued behind the current run — BUT
            // busy-ness alone cannot confirm it entered THIS thread's queue: a send
            // into a new-task/landing composer also makes the app busy on a brand new
            // thread. Report this honestly as UNCONFIRMED rather than a queued
            // success; the mailbox packet is the real record. Verify with `axsend
            // confirm`, and do NOT blindly resend (risks a duplicate or new thread).
            print("QUEUED (UNCONFIRMED): recipient went busy with no visible turn — likely queued behind its run, but axsend cannot confirm it landed in THIS thread vs a new one. Verify with `axsend confirm`; do not resend.")
            return 0
        }
        FileHandle.standardError.write("WARN: NOT DELIVERED after \(maxAttempts) attempts (button-press, composer-confirm, cmd-return, key-return each, draft cleared between) and recipient is idle — likely on a non-chat screen. Re-ring; check with `axsend confirm`.\n".data(using: .utf8)!)
        return 7
    }
    return 0
}

// Read-only delivery feedback — confirm whether a message actually sent WITHOUT
// needing a screenshot/computer-use. Call after `ring` (or anytime) with the
// sent text (a prefix is fine). Verdict + exit code:
//   delivered (0): the text appears as a conversation turn above the composer
//   stuck (7):     the text is still sitting in the composer (NOT sent)
//   absent (8):    the text is in neither (wrong window, or never typed)
// Electron composers (ZCode/Antigravity) don't expose the draft via AXValue, so
// "stuck" also matches the draft rendered as static text at/below the composer.
func cmdConfirm(app: String, text: String, windowIndex: Int) -> Int32 {
    guard AXIsProcessTrusted() else {
        FileHandle.standardError.write("AX not trusted; run `axsend check`.\n".data(using: .utf8)!); return 2
    }
    guard let (el, _) = appElement(named: app) else {
        FileHandle.standardError.write("app not found: \(app)\n".data(using: .utf8)!); return 1
    }
    let wins = windows(el)
    guard !wins.isEmpty else {
        FileHandle.standardError.write("no windows for \(app)\n".data(using: .utf8)!); return 1
    }
    let win = wins[max(0, min(windowIndex, wins.count - 1))]
    let needle = String(text.prefix(40))
    guard !needle.isEmpty else { print("nothing to confirm (empty text)"); return 0 }

    // Only the DELIVERED signal is reliable on Electron: a sent message renders
    // as a real conversation turn ABOVE the composer (AX-readable), whereas the
    // composer's own draft/empty state is NOT reliably readable (AXValue is blank
    // and the subtree keeps stale cached static-text nodes that false-positive a
    // "stuck draft"). So report delivered vs not — and recovery for not-delivered
    // is always the same: re-ring (the ring reliably clears any draft + resends).
    let proc = isProcessing(win)
    if messageLanded(win, sentText: text) {
        print("delivered: text appears as a sent message\(proc ? "; recipient is processing" : "")")
        return 0
    }
    FileHandle.standardError.write("not delivered: text is not a sent message (it's a draft or was never typed). Recover by re-ringing with the message — the ring reliably clears any stuck draft and resends — then confirm again.\n".data(using: .utf8)!)
    return 7
}

// MARK: - Arg parsing

func argValue(_ key: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: key), i + 1 < a.count else { return nil }
    return a[i + 1]
}
func hasFlag(_ key: String) -> Bool { CommandLine.arguments.contains(key) }

let args = CommandLine.arguments
guard args.count >= 2 else {
    print("usage: axsend <check|tree|state|ring|type|confirm> [...]")
    print("  confirm --app <app> --text <sent-text>   read-only: delivered? (exit 0 delivered / 7 not delivered)")
    exit(64)
}
switch args[1] {
case "attrs":
    guard let app = argValue("--app") else { print("--app required"); exit(64) }
    exit(cmdAttrs(app: app))
case "buttons":
    guard let app = argValue("--app") else { print("--app required"); exit(64) }
    exit(cmdButtons(app: app))
case "state":
    guard let app = argValue("--app") else { print("--app required"); exit(64) }
    exit(cmdState(app: app))
case "check":
    exit(cmdCheck())
case "tree":
    guard let app = argValue("--app") else { print("--app required"); exit(64) }
    let depth = Int(argValue("--max-depth") ?? "30") ?? 30
    exit(cmdTree(app: app, maxDepth: depth, editableOnly: hasFlag("--editable-only")))
case "ring":
    guard let app = argValue("--app"), let text = argValue("--text") else {
        print("--app and --text required"); exit(64)
    }
    exit(cmdRing(app: app, text: text, submit: hasFlag("--submit"),
                 windowIndex: Int(argValue("--window-index") ?? "0") ?? 0, dryRun: hasFlag("--dry-run"),
                 // Verify is ALWAYS enforced for --submit (auto-retry until a confirmed
                 // or queued delivery); there is no fire-and-forget opt-out.
                 verify: true))
case "type":
    guard let app = argValue("--app"), let text = argValue("--text") else {
        print("--app and --text required"); exit(64)
    }
    exit(cmdType(app: app, text: text, submit: hasFlag("--submit"), verify: hasFlag("--verify")))
case "confirm":
    guard let app = argValue("--app"), let text = argValue("--text") else {
        print("--app and --text required"); exit(64)
    }
    exit(cmdConfirm(app: app, text: text, windowIndex: Int(argValue("--window-index") ?? "0") ?? 0))
default:
    print("unknown command: \(args[1])"); exit(64)
}
