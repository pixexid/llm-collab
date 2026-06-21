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
    let aDown = CGEvent(keyboardEventSource: src, virtualKey: 0x00, keyDown: true); aDown?.flags = .maskCommand; aDown?.postToPid(pid)
    let aUp = CGEvent(keyboardEventSource: src, virtualKey: 0x00, keyDown: false); aUp?.flags = .maskCommand; aUp?.postToPid(pid)
    usleep(30_000)
    let dDown = CGEvent(keyboardEventSource: src, virtualKey: 0x33, keyDown: true); dDown?.postToPid(pid)  // 0x33 = Delete
    let dUp = CGEvent(keyboardEventSource: src, virtualKey: 0x33, keyDown: false); dUp?.postToPid(pid)
    usleep(30_000)
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
        // Clearing a (possibly stuck) draft. The AXValue "" write above clears
        // fields that accept it; Electron composers (ZCode/Antigravity) reject
        // it and keep the stale draft, so confirm it actually emptied and fall
        // back to a real key-event select-all+delete when it didn't. Returning
        // success on the unverified AXValue write would leave the stuck draft.
        if current().isEmpty { return true }
        selectAllAndDelete(pid: pid)
        usleep(120_000)
        return current().isEmpty
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
        guard l == "stop" || l.hasPrefix("stop generating")
            || l.hasPrefix("stop streaming") || l.hasPrefix("stop response") else { return false }
        // Real stop button lives with the composer; reject anything far to its
        // left (sidebar/chrome). Fall through to label-only if no composer found.
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
    let needle = String(sentText.prefix(30))
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
        // Re-check idle RIGHT before pressing. Chat apps (e.g. Claude Desktop)
        // won't submit while busy — pressing Send just leaves a stuck draft and
        // flips to a Stop button. If the target went busy since the composer was
        // set, abort instead of leaving a stuck draft.
        let preWin = windows(appElement(named: app)?.0 ?? el).first ?? win
        if isProcessing(preWin) {
            FileHandle.standardError.write("target became busy before send — not submitting (would leave a stuck draft). Re-ring when idle; clear the draft with `ring --text \"\"`.\n".data(using: .utf8)!)
            return 8
        }
        // Submit via multiple mechanisms — some composers (Claude Desktop) ignore
        // AXPress on the Send button. Try in order, verifying after each; stop at
        // the first that actually lands the message as a real turn.
        func landed() -> Bool {
            Thread.sleep(forTimeInterval: 1.0)
            let fresh = windows(appElement(named: app)?.0 ?? el).first ?? win
            return messageLanded(fresh, sentText: text)
        }
        func keyReturn(command: Bool = false) {
            AXUIElementSetAttributeValue(composer, kAXFocusedAttribute as CFString, kCFBooleanTrue)
            postReturnKey(pid: pid, command: command)
        }
        // Without --verify we can't tell which mechanism worked, so do the single
        // safest: press a confident send button, else key-return.
        if !verify {
            if buttonOK, let b = button {
                AXUIElementPerformAction(b, kAXPressAction as CFString)
                print("send pressed (\(tgt)) [no --verify]")
            } else {
                keyReturn()
                print("submitted via key-return (no confident button) [no --verify]")
            }
            return 0
        }
        var method = ""
        // 1. Press the resolved Send button ONLY if it's a confident send target.
        if buttonOK, let b = button {
            AXUIElementPerformAction(b, kAXPressAction as CFString)
            if landed() { method = "button-press" }
        }
        // 2. AXConfirm on the composer (text fields that submit on confirm).
        if method.isEmpty {
            AXUIElementPerformAction(composer, kAXConfirmAction as CFString)
            if landed() { method = "composer-confirm" }
        }
        // 3. Cmd+Return — submit gesture for code-editor composers (ZCode et al.)
        //    where a plain Return inserts a NEWLINE instead of sending (and would
        //    pollute the draft). Must come before plain Return for those apps.
        if method.isEmpty {
            keyReturn(command: true)
            if landed() { method = "cmd-return" }
        }
        // 4. Focus the composer + post a plain Return to the app's pid (no focus
        //    steal). The submit for Enter-to-send composers (Claude Desktop).
        if method.isEmpty {
            keyReturn()
            if landed() { method = "key-return" }
        }

        if verify {
            if !method.isEmpty {
                let fresh = windows(appElement(named: app)?.0 ?? el).first ?? win
                print("VERIFIED: submitted via \(method)\(isProcessing(fresh) ? "; recipient is processing" : "")")
            } else {
                // No method landed — the text is still sitting unsent in the
                // composer. Clear it so a failed doorbell never leaves a stuck
                // draft that pollutes the recipient's next input.
                selectAllAndDelete(pid: pid)
                FileHandle.standardError.write("WARN: not landed after button-press, composer-confirm, cmd-return, and key-return — send did not submit; cleared the draft\n".data(using: .utf8)!)
                return 7
            }
        }
    }
    return 0
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
    print("usage: axsend <check|tree|ring> [...]")
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
                 windowIndex: Int(argValue("--window-index") ?? "0") ?? 0, dryRun: hasFlag("--dry-run"), verify: hasFlag("--verify")))
case "type":
    guard let app = argValue("--app"), let text = argValue("--text") else {
        print("--app and --text required"); exit(64)
    }
    exit(cmdType(app: app, text: text, submit: hasFlag("--submit"), verify: hasFlag("--verify")))
default:
    print("unknown command: \(args[1])"); exit(64)
}
