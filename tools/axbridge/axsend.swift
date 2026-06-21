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
    flatten(win).contains { role($0) == "AXButton" && label($0).lowercased().contains("stop") }
}

func messageLanded(_ win: AXUIElement, sentText: String) -> Bool {
    let needle = String(sentText.prefix(30))
    guard !needle.isEmpty else { return true }
    return conversationTexts(win).contains { $0.contains(needle) }
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
    guard let (el, _) = appElement(named: app) else {
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
    let setErr = AXUIElementSetAttributeValue(composer, kAXValueAttribute as CFString, text as CFString)
    if setErr != .success {
        FileHandle.standardError.write("set value failed: \(setErr.rawValue)\n".data(using: .utf8)!)
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
                             "more", "agent", "branch", "environment"]
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
        guard let button = button else {
            FileHandle.standardError.write("composer set but no send button found; submit skipped\n".data(using: .utf8)!)
            return 5
        }
        let bf = frame(button).map { String(format: "x=%.0f y=%.0f", $0.x, $0.y) } ?? "?"
        let tgt = "label=\"\(label(button).prefix(20))\" sub=\"\(subrole(button))\" \(bf)"
        if dryRun {
            print("DRY-RUN send target: \(tgt) (not pressed)")
            return 0
        }
        let pressErr = AXUIElementPerformAction(button, kAXPressAction as CFString)
        if pressErr != .success {
            FileHandle.standardError.write("press failed: \(pressErr.rawValue)\n".data(using: .utf8)!)
            return 6
        }
        print("send pressed (\(tgt))")

        if verify {
            // capture_after: confirm the text actually landed in the conversation
            // (composer clearing is NOT proof). Re-fetch the window fresh.
            Thread.sleep(forTimeInterval: 1.2)
            let freshWin = windows(appElement(named: app)?.0 ?? el).first ?? win
            if messageLanded(freshWin, sentText: text) {
                print("VERIFIED: message present in conversation\(isProcessing(freshWin) ? "; recipient is processing" : "")")
            } else {
                FileHandle.standardError.write("WARN: sent text not found in conversation — send may not have landed\n".data(using: .utf8)!)
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
default:
    print("unknown command: \(args[1])"); exit(64)
}
