import AppKit
import LockiAlertCore

/// Glue: poll -> state machine -> panel / reports / acks. Everything runs on the main thread; URLSession
/// callbacks hop to main. The helper never listens on a port and holds no owner or investigator credentials.
final class AppController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let opts: Options
    let paths: Paths
    let log: JSONLog
    let instance: SingleInstance?
    let base: URL
    private var sm: AlertStateMachine
    private var panelCtl: AlertPanelController!
    private var token: String?
    private var timer: Timer?
    private var inFlight = false
    private var lastConnSummary = ""
    private var autoTimer: Timer?
    private var activity: NSObjectProtocol?
    private var statusItem: NSStatusItem?
    private var statusLine: NSMenuItem?
    private var signalSources: [DispatchSourceSignal] = []
    private var receipt: (mono: UInt64, wall: Int64) = (0, 0)
    private var polls = 0
    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 1.0
        c.timeoutIntervalForResource = 2.0
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        c.urlCache = nil
        c.httpCookieStorage = nil
        c.httpShouldSetCookies = false
        c.httpMaximumConnectionsPerHost = 2
        c.connectionProxyDictionary = [:]   // loopback only; never through a proxy
        return URLSession(configuration: c)
    }()

    init(opts: Options, paths: Paths, log: JSONLog, instance: SingleInstance?, base: URL) {
        self.opts = opts; self.paths = paths; self.log = log; self.instance = instance; self.base = base
        // A local rehearsal never reads or writes the ack file.
        self.sm = AlertStateMachine(acks: opts.rehearsal ? AckStore() : AckStore.load(from: paths.acks))
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // No App Nap: the 200 ms poll must keep its cadence while the helper has no visible window.
        activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep],
                                                         reason: "LOCKI security alert polling")
        FontBook.register()
        panelCtl = AlertPanelController(level: opts.windowLevel, behavior: opts.collectionBehavior, log: log,
                                        snapshotPath: opts.snapshot)
        panelCtl.model.testBanner = opts.testBanner
        panelCtl.model.onExplain = { [weak self] in self?.handoff("explain") }
        panelCtl.model.onEvidence = { [weak self] in self?.handoff("evidence") }
        panelCtl.model.onStop = { [weak self] in self?.handoff("stop") }
        panelCtl.model.onDismiss = { [weak self] in self?.dismiss(detail: nil) }
        panelCtl.onVisible = { [weak self] id, vis in
            guard let self else { return }
            self.run(self.sm.handle(.panelShown(id: id, visible: vis), nowMs: wallMs()))
        }
        installSignalHandlers()
        log.write("launch", [
            "version": kAppVersion, "mode": opts.rehearsal ? "local_rehearsal" : "backend",
            "backend": opts.rehearsal ? NSNull() : base.absoluteString as Any,
            "state_dir": paths.stateDir.path, "token_file": paths.token.path, "log": paths.log.path,
            "acks_loaded": sm.acks.entries.count, "font": FontBook.note,
            "level": opts.windowLevel.rawValue, "collection_behavior": opts.behavior,
            "screen": [Int(AlertPanelController.primaryFrame().width), Int(AlertPanelController.primaryFrame().height)],
            "test_banner": opts.testBanner ?? NSNull(), "bundle": Bundle.main.bundlePath,
        ])
        if opts.rehearsal {
            DispatchQueue.main.asyncAfter(deadline: .now() + opts.delay) { [weak self] in
                guard let self else { return }
                self.receipt = (monoNs(), wallMs())
                self.run(self.sm.handle(.rehearsalSample(RehearsalSample.record(nowMs: wallMs())), nowMs: wallMs()))
            }
            return
        }
        token = paths.readToken()
        if opts.statusItem { installStatusItem() }
        let t = Timer(timeInterval: Double(opts.pollMs) / 1000, repeats: true) { [weak self] _ in self?.poll() }
        t.tolerance = 0.025
        RunLoop.main.add(t, forMode: .common)
        timer = t
        poll()
    }

    func applicationWillTerminate(_ notification: Notification) { cleanup(reason: "terminate") }

    // MARK: polling

    private func headers(_ req: inout URLRequest) {
        if let token { req.setValue(token, forHTTPHeaderField: "X-Locki-Alert-Token") }
        req.setValue(String(getpid()), forHTTPHeaderField: "X-Locki-Alert-Pid")
        req.setValue(kAppVersion, forHTTPHeaderField: "X-Locki-Alert-Version")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
    }

    private func poll() {
        guard !inFlight else { return }
        inFlight = true
        polls += 1
        var req = URLRequest(url: base.appendingPathComponent("api/security/takeover"))
        req.httpMethod = "GET"
        req.timeoutInterval = 1.0
        headers(&req)
        session.dataTask(with: req) { [weak self] data, resp, err in
            let mono = monoNs(), wall = wallMs()
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            DispatchQueue.main.async { self?.onPollResult(data: data, code: code, err: err, mono: mono, wall: wall) }
        }.resume()
    }

    private func onPollResult(data: Data?, code: Int, err: Error?, mono: UInt64, wall: Int64) {
        inFlight = false
        let input: Input
        if let err {
            input = .failure(.unreachable((err as NSError).localizedDescription))
        } else {
            switch code {
            case 200:
                if let data, let d = try? TakeoverDecision.decode(data) { input = .decision(d) }
                else { input = .failure(.badPayload("JSON did not match the TakeoverDecision contract")) }
            case 401:
                token = paths.readToken()
                input = .failure(.unauthorized)
            case 403: input = .failure(.forbidden)
            case 404: input = .failure(.notSupported)
            default: input = .failure(.badStatus(code))
            }
        }
        receipt = (mono, wall)
        run(sm.handle(input, nowMs: wall))
        let summary = sm.connection.summary
        if summary != lastConnSummary {
            lastConnSummary = summary
            log.write("connection", ["state": summary, "http_status": code, "polls": polls,
                                     "token_present": token != nil])
            statusLine?.title = "LOCKI Alert: \(summary)"
            statusItem?.button?.title = sm.connection == .connected ? "LOCKI" : "LOCKI ·"
        }
    }

    // MARK: commands

    private func run(_ cmds: [Command]) { cmds.forEach(execute) }

    private func execute(_ c: Command) {
        switch c {
        case .show(let p):
            panelCtl.present(p, receiptMono: receipt.mono, receiptWall: receipt.wall)
            scheduleAuto()
        case .update(let p):
            panelCtl.update(p)
            log.write("updated", ["id": p.record.id, "connection_lost_at_ms": p.connectionLostAtMs ?? NSNull() as Any])
        case .hide(let reason, let id):
            autoTimer?.invalidate(); autoTimer = nil
            panelCtl.hide(reason: reason.rawValue, restoreFocus: reason != .handoff)
            log.write("hide", ["id": id, "reason": reason.rawValue])
            if opts.rehearsal {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { NSApp.terminate(nil) }
            }
        case .report(let r):
            post(r, retry: true)
        case .persistAck(let id, let event):
            do {
                try sm.acks.save(to: paths.acks)
                log.write("ack_persisted", ["id": id, "ack_event": event, "file": paths.acks.path, "count": sm.acks.entries.count])
            } catch {
                log.write("ack_persist_failed", ["id": id, "error": error.localizedDescription])
            }
        }
    }

    private func scheduleAuto() {
        guard let secs = opts.autoDismiss else { return }
        autoTimer?.invalidate()
        autoTimer = Timer.scheduledTimer(withTimeInterval: secs, repeats: false) { [weak self] _ in
            guard let self else { return }
            if self.opts.autoAction == "dismiss" { self.dismiss(detail: "auto-dismiss after \(secs) s (test flag)") }
            else { self.handoff(self.opts.autoAction) }
        }
    }

    private func post(_ r: Report, retry: Bool) {
        var req = URLRequest(url: base.appendingPathComponent("api/security/takeover/report"))
        req.httpMethod = "POST"
        req.timeoutInterval = 2.0
        headers(&req)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONEncoder().encode(r)
        session.dataTask(with: req) { [weak self] _, resp, err in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            DispatchQueue.main.async {
                guard let self else { return }
                self.log.write("report", ["takeover_id": r.takeoverId, "report_event": r.event, "view": r.view ?? NSNull() as Any,
                                          "latency_ms": r.latencyMs ?? NSNull() as Any, "http_status": code,
                                          "error": err.map { ($0 as NSError).localizedDescription } ?? NSNull() as Any])
                if code == 401 && retry {
                    self.token = self.paths.readToken()
                    self.post(r, retry: false)
                }
            }
        }.resume()
    }

    // MARK: user actions

    func dismiss(detail: String?) {
        run(sm.handle(.dismiss(detail: detail), nowMs: wallMs()))
    }

    private enum Dashboard { case chrome(NSRunningApplication), url(URL), none }

    private func resolveDashboard() -> Dashboard {
        if let app = ownerChrome() { return .chrome(app) }
        if let u = paths.dashboardURL() { return .url(u) }
        return .none
    }

    func handoff(_ view: String) {
        let target = resolveDashboard()
        var reachable = true
        if case .none = target { reachable = false }
        run(sm.handle(.handoff(view: view, dashboardReachable: reachable), nowMs: wallMs()))
        switch target {
        case .chrome(let app):
            // macOS 14+ cooperative activation: become active (the click is the user intent), then yield to Chrome.
            NSApp.activate()
            let selfActive = NSApp.isActive
            NSApp.yieldActivation(to: app)
            let ok = app.activate(from: NSRunningApplication.current, options: [.activateAllWindows])
            log.write("handoff", ["view": view, "target": "owner_chrome", "chrome_pid": Int(app.processIdentifier),
                                  "self_active_before_yield": selfActive, "activate_from_result": ok])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
                let front = NSWorkspace.shared.frontmostApplication
                self?.log.write("handoff_result", ["view": view, "frontmost_now": self?.panelCtl.appInfo(front) ?? NSNull(),
                                                   "owner_chrome_frontmost": front?.processIdentifier == app.processIdentifier])
            }
        case .url(let u):
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
            p.arguments = ["-na", "Google Chrome", "--args",
                           "--user-data-dir=\(NSHomeDirectory())/.locki-owner-chrome",
                           "--no-first-run", "--no-default-browser-check", u.absoluteString]
            let launched = (try? p.run()) != nil
            log.write("handoff", ["view": view, "target": "open_dashboard_url", "launched": launched, "url": u.absoluteString])
        case .none:
            panelCtl.model.notice = "Open the LOCKI dashboard"
            log.write("handoff", ["view": view, "target": "none",
                                  "notice": "Open the LOCKI dashboard (no owner Chrome running, no LOCKI_DASHBOARD_URL in launcher.env)"])
        }
    }

    // MARK: menu bar

    private func installStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "LOCKI ·"
        let menu = NSMenu()
        menu.delegate = self
        let line = NSMenuItem(title: "LOCKI Alert: connecting", action: nil, keyEquivalent: "")
        line.isEnabled = false
        menu.addItem(line)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Show last alert", action: #selector(menuShowLast), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Rehearsal alert", action: #selector(menuRehearsal), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit alert window", action: #selector(menuQuit), keyEquivalent: "").target = self
        item.menu = menu
        statusItem = item
        statusLine = line
    }

    func menuNeedsUpdate(_ menu: NSMenu) { statusLine?.title = "LOCKI Alert: \(sm.connection.summary)" }

    @objc private func menuShowLast() { receipt = (monoNs(), wallMs()); run(sm.handle(.reopenLast, nowMs: wallMs())) }
    @objc private func menuRehearsal() {
        receipt = (monoNs(), wallMs())
        run(sm.handle(.rehearsalSample(RehearsalSample.record(nowMs: wallMs())), nowMs: wallMs()))
    }
    @objc private func menuQuit() { NSApp.terminate(nil) }

    // MARK: lifecycle

    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { [weak self] in
                self?.cleanup(reason: sig == SIGTERM ? "SIGTERM" : (sig == SIGINT ? "SIGINT" : "SIGHUP"))
                exit(0)
            }
            src.resume()
            signalSources.append(src)
        }
    }

    private var cleaned = false
    private func cleanup(reason: String) {
        guard !cleaned else { return }
        cleaned = true
        timer?.invalidate()
        if panelCtl?.isShowing == true { panelCtl.hide(reason: "exit", restoreFocus: true) }
        log.write("exit", ["reason": reason, "polls": polls])
        instance?.release()
        if let activity { ProcessInfo.processInfo.endActivity(activity) }
    }
}
