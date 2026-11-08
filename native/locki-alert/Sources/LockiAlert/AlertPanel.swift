import AppKit
import SwiftUI
import LockiAlertCore

final class KeyablePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// Owns the pre-created full-display panel, shows/hides it, and logs proof of where it actually is.
final class AlertPanelController: NSObject {
    let panel: KeyablePanel
    let model = AlertModel()
    private let log: JSONLog
    private let snapshotPath: String?
    private var previousApp: NSRunningApplication?
    private var showCount = 0
    private struct Pending { let id: String; let receiptMono: UInt64; let receiptWall: Int64; let orderMono: UInt64; var reported: Bool }
    private var pending: Pending?
    /// Called once per show when the window server reports the panel visible (or after a 400 ms timeout).
    var onVisible: ((String, Bool) -> Void)?

    static func primaryFrame() -> NSRect {
        NSScreen.screens.first?.frame ?? NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
    }
    static func scale(for frame: NSRect) -> CGFloat { max(0.72, min(1.2, frame.height / 1000)) }

    init(level: NSWindow.Level, behavior: NSWindow.CollectionBehavior, log: JSONLog, snapshotPath: String?) {
        self.log = log
        self.snapshotPath = snapshotPath
        let frame = Self.primaryFrame()
        panel = KeyablePanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        super.init()
        panel.isOpaque = true
        panel.backgroundColor = .black
        panel.hasShadow = false
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true          // NOTE: this resets level to .floating, so level is set after it
        panel.level = level
        panel.collectionBehavior = behavior
        panel.becomesKeyOnlyIfNeeded = false
        panel.worksWhenModal = true
        panel.isReleasedWhenClosed = false
        panel.isMovable = false
        panel.animationBehavior = .none
        panel.title = "LOCKI Alert"
        model.scale = Self.scale(for: frame)
        let host = NSHostingView(rootView: AlertRootView(model: model))
        host.frame = NSRect(origin: .zero, size: frame.size)
        panel.contentView = host
        panel.setFrame(frame, display: false)
        NotificationCenter.default.addObserver(self, selector: #selector(occlusionChanged),
                                               name: NSWindow.didChangeOcclusionStateNotification, object: panel)
        // Warm the SwiftUI graph and layout off screen so the first real show is fast.
        model.presentation = Presentation(record: RehearsalSample.record(nowMs: wallMs()), sessionId: nil,
                                          workstationId: nil, rehearsalBanner: true, localSample: true)
        host.layoutSubtreeIfNeeded()
        panel.displayIfNeeded()
        model.presentation = nil
    }

    var isShowing: Bool { panel.isVisible }

    func present(_ p: Presentation, receiptMono: UInt64, receiptWall: Int64) {
        showCount += 1
        let shot = snapshotPath?.replacingOccurrences(of: "{n}", with: String(showCount))
        let front = NSWorkspace.shared.frontmostApplication
        if let front, front.processIdentifier != getpid() { previousApp = front }
        let frame = Self.primaryFrame()
        if panel.frame != frame { panel.setFrame(frame, display: false); model.scale = Self.scale(for: frame) }
        model.notice = nil
        model.appeared = false
        model.presentation = p
        panel.alphaValue = 1
        panel.orderFrontRegardless()
        panel.makeKey()
        let orderMono = monoNs()
        NSApp.activate()                                 // cooperative activation (macOS 14+)
        let activeAfterCooperative = NSApp.isActive
        pending = Pending(id: p.record.id, receiptMono: receiptMono, receiptWall: receiptWall, orderMono: orderMono, reported: false)
        DispatchQueue.main.async { withAnimation(.easeOut(duration: 0.18)) { self.model.appeared = true } }

        var f = proof()
        f["id"] = p.record.id
        f["mode"] = p.localSample ? "local_rehearsal" : (p.record.live ? "live" : "backend_rehearsal")
        f["rehearsal_banner"] = p.rehearsalBanner
        f["test_banner"] = model.testBanner ?? NSNull()
        f["critical_verified"] = p.record.criticalVerified
        f["received_at_ms"] = receiptWall
        f["shown_at_ms"] = wallMs()
        f["receipt_to_order_front_ms"] = msBetween(receiptMono, orderMono)
        f["latency_vs_detected_ms"] = p.record.detectedAtMs.map { wallMs() - $0 } ?? NSNull()
        f["frontmost_before"] = appInfo(front)
        f["activate_cooperative_is_active"] = activeAfterCooperative
        log.write("shown", f)

        if !occlusionVisible { scheduleVisibleTimeout() } else { reportVisible(source: "already_visible") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
            guard let self, self.panel.isVisible else { return }
            var used = "cooperative"
            if !NSApp.isActive {
                NSApp.activate(ignoringOtherApps: true)      // deprecated fallback; result logged
                used = "ignoringOtherApps_fallback"
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                guard let self, self.panel.isVisible else { return }
                var s = self.proof()
                s["id"] = p.record.id
                s["activation_path"] = used
                s["frontmost_before"] = self.appInfo(front)
                self.log.write("shown_settled", s)
                if let path = shot { MainActor.assumeIsolated { self.writeSnapshot(p, to: path) } }
            }
        }
    }

    func update(_ p: Presentation) { model.presentation = p }

    /// `restoreFocus`: re-activate the app that was frontmost before the panel showed (every hide except handoff).
    func hide(reason: String, restoreFocus: Bool) {
        let wasVisible = panel.isVisible
        panel.orderOut(nil)
        model.presentation = nil
        model.notice = nil
        pending = nil
        var f: [String: Any] = ["reason": reason, "was_visible": wasVisible]
        if restoreFocus, let prev = previousApp, !prev.isTerminated {
            NSApp.yieldActivation(to: prev)
            let ok = prev.activate(from: NSRunningApplication.current, options: [])
            f["restore_to"] = appInfo(prev)
            f["restore_activate_result"] = ok
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
                guard let self else { return }
                let now = NSWorkspace.shared.frontmostApplication
                self.log.write("focus_restored", ["expected": self.appInfo(prev), "frontmost_now": self.appInfo(now),
                                                  "ok": now?.processIdentifier == prev.processIdentifier])
            }
        }
        previousApp = nil
        log.write("hidden", f)
    }

    // MARK: visibility + proof

    private var occlusionVisible: Bool { panel.occlusionState.contains(.visible) }

    @objc private func occlusionChanged() {
        if occlusionVisible { reportVisible(source: "occlusion_notification") }
    }

    private func scheduleVisibleTimeout() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.reportVisible(source: "timeout_400ms") }
    }

    private func reportVisible(source: String) {
        guard var pnd = pending, !pnd.reported else { return }
        pnd.reported = true
        pending = pnd
        let now = monoNs()
        let vis = occlusionVisible
        log.write("visible", ["id": pnd.id, "source": source, "occlusion_visible": vis,
                              "receipt_to_visible_ms": msBetween(pnd.receiptMono, now),
                              "order_front_to_visible_ms": msBetween(pnd.orderMono, now)])
        onVisible?(pnd.id, vis)
    }

    func appInfo(_ app: NSRunningApplication?) -> Any {
        guard let app else { return NSNull() }
        return ["bundle": app.bundleIdentifier ?? "?", "name": app.localizedName ?? "?", "pid": Int(app.processIdentifier)]
    }

    func proof() -> [String: Any] {
        var d: [String: Any] = [
            "is_active": NSApp.isActive,
            "is_key_window": panel.isKeyWindow,
            "occlusion_visible": occlusionVisible,
            "is_visible": panel.isVisible,
            "on_active_space": panel.isOnActiveSpace,
            "window_number": panel.windowNumber,
            "level": panel.level.rawValue,
            "collection_behavior": Int(panel.collectionBehavior.rawValue),
            "frame": [Int(panel.frame.minX), Int(panel.frame.minY), Int(panel.frame.width), Int(panel.frame.height)],
            "frontmost_now": appInfo(NSWorkspace.shared.frontmostApplication),
        ]
        d["cg"] = cgStacking()
        return d
    }

    /// Front-to-back on-screen window list; is our panel above every other app's window?
    func cgStacking() -> [String: Any] {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] else {
            return ["error": "CGWindowListCopyWindowInfo returned nil"]
        }
        let me = panel.windowNumber
        let myPid = Int(getpid())
        guard let idx = list.firstIndex(where: { ($0[kCGWindowNumber as String] as? Int) == me }) else {
            return ["listed": false, "on_screen_count": list.count]
        }
        let screen = Self.primaryFrame()
        let screenCG = CGRect(x: 0, y: 0, width: screen.width, height: screen.height)
        func describe(_ w: [String: Any]) -> [String: Any] {
            let pid = w[kCGWindowOwnerPID as String] as? Int ?? -1
            var b = CGRect.zero
            if let bd = w[kCGWindowBounds as String] as? NSDictionary,
               let r = CGRect(dictionaryRepresentation: bd as CFDictionary) { b = r }
            let alpha = w[kCGWindowAlpha as String] as? Double ?? 1
            let regular = NSRunningApplication(processIdentifier: pid_t(pid))?.activationPolicy == .regular
            return ["owner": w[kCGWindowOwnerName as String] as? String ?? "?", "pid": pid,
                    "layer": w[kCGWindowLayer as String] as? Int ?? 0, "alpha": alpha,
                    "size": [Int(b.width), Int(b.height)], "regular_app": regular,
                    "overlaps_panel": alpha > 0.01 && b.width * b.height > 4 && b.intersects(screenCG)]
        }
        let above = list[..<idx].filter { ($0[kCGWindowOwnerPID as String] as? Int) != myPid }.map(describe)
        let appAbove = above.filter { ($0["regular_app"] as? Bool) == true && ($0["overlaps_panel"] as? Bool) == true }
        let below = list[(idx + 1)...].filter { ($0[kCGWindowOwnerPID as String] as? Int) != myPid }.map(describe)
        let appBelow = below.filter { ($0["regular_app"] as? Bool) == true && ($0["layer"] as? Int) == 0 }
        let appsBelow = Array(Set(appBelow.compactMap { $0["owner"] as? String })).sorted()
        return ["listed": true, "panel_index": idx, "on_screen_count": list.count,
                "panel_layer": list[idx][kCGWindowLayer as String] as? Int ?? -1,
                "windows_above": above, "other_app_windows_above": appAbove.count,
                "above_every_other_app_window": appAbove.isEmpty,
                "regular_app_windows_below": appBelow.count, "apps_below": appsBelow]
    }

    /// Self-render of the SwiftUI panel content via ImageRenderer. Not a screen capture; proves layout only.
    @MainActor private func writeSnapshot(_ p: Presentation, to path: String) {
        let m = AlertModel()
        m.presentation = p
        m.appeared = true
        m.scale = model.scale
        m.testBanner = model.testBanner
        let size = panel.frame.size
        let renderer = ImageRenderer(content: AlertRootView(model: m).frame(width: size.width, height: size.height))
        renderer.scale = 2
        guard let img = renderer.nsImage, let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else {
            log.write("snapshot", ["path": path, "ok": false]); return
        }
        let ok = (try? png.write(to: URL(fileURLWithPath: path))) != nil
        log.write("snapshot", ["path": path, "ok": ok, "kind": "ImageRenderer self-render (not a screen capture)"])
    }
}
