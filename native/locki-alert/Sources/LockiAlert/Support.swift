import AppKit
import CoreText
import Foundation
import LockiAlertCore

let kAppVersion = "1"

func wallMs() -> Int64 { Int64((Date().timeIntervalSince1970 * 1000).rounded()) }
func monoNs() -> UInt64 { DispatchTime.now().uptimeNanoseconds }
func msBetween(_ a: UInt64, _ b: UInt64) -> Double { (Double(b) - Double(a)) / 1_000_000 }

// MARK: CLI

struct Options {
    var backend = ProcessInfo.processInfo.environment["LOCKI_ALERT_BACKEND"] ?? "http://127.0.0.1:8077"
    var tokenFile: String? = nil
    var stateDir: String? = nil
    var logFile: String? = nil
    var rehearsal = false
    var delay: Double = 0
    var autoDismiss: Double? = nil
    var onceCheck = false
    var level = "screensaver"
    var behavior = "canJoinAllSpaces,fullScreenAuxiliary,stationary,ignoresCycle"
    var snapshot: String? = nil
    var statusItem = true
    var pollMs: Int = 200
    var testBanner: String? = nil
    var autoAction = "dismiss"
    var help = false

    static let usage = """
    LOCKI Alert \(kAppVersion): native takeover alert for LOCKI AIRLOCK (renders what the backend decides).

    Usage: locki-alert [options]
      --backend URL        loopback backend base URL (default http://127.0.0.1:8077, env LOCKI_ALERT_BACKEND)
      --token-file PATH    alert token file (default $LOCKI_ALERT_TOKEN_FILE or ~/.locki/run/alert.token)
      --state-dir DIR      lock, pid and ack files (default ~/.locki/run)
      --log PATH           JSON-lines log (default ~/.locki/logs/alert.log)
      --poll-ms N          poll interval in ms (default 200)
      --rehearsal          show a local REHEARSAL sample; no backend contact at all; exits when it closes
      --delay N            seconds before the rehearsal sample shows (default 0)
      --auto-dismiss N     act on a shown panel after N seconds (test aid; default action dismiss, reported as dismissed)
      --auto-action A      what --auto-dismiss does: dismiss (default) | explain | evidence | stop (handoff test)
      --test-banner TEXT   amber banner with TEXT on every panel this process shows (live or not)
      --level NAME|INT     window level: screensaver (default) | shielding | statusbar | popup | mainmenu | floating | <int>
      --behavior LIST      collection behavior, comma list (default canJoinAllSpaces,fullScreenAuxiliary,stationary,ignoresCycle)
      --snapshot PATH      after each show, write a PNG self-render of the panel view, {n} = show number (NOT a screen capture)
      --no-status-item     no "LOCKI" menu bar item
      --once-check         print JSON status (backend support, token, instance, acks) and exit
      --help
    """

    static func parse(_ argv: [String]) -> Options {
        var o = Options()
        var i = 1
        func next() -> String? { i += 1; return i < argv.count ? argv[i] : nil }
        while i < argv.count {
            let a = argv[i]
            switch a {
            case "--backend": if let v = next() { o.backend = v }
            case "--token-file": o.tokenFile = next()
            case "--state-dir": o.stateDir = next()
            case "--log": o.logFile = next()
            case "--poll-ms": if let v = next(), let n = Int(v), n >= 50 { o.pollMs = n }
            case "--rehearsal": o.rehearsal = true
            case "--delay": if let v = next(), let n = Double(v) { o.delay = max(0, n) }
            case "--auto-dismiss": if let v = next(), let n = Double(v) { o.autoDismiss = max(0.5, n) }
            case "--level": if let v = next() { o.level = v }
            case "--behavior": if let v = next() { o.behavior = v }
            case "--snapshot": o.snapshot = next()
            case "--test-banner": o.testBanner = next()
            case "--auto-action": if let v = next(), ["dismiss", "explain", "evidence", "stop"].contains(v) { o.autoAction = v }
            case "--no-status-item": o.statusItem = false
            case "--once-check": o.onceCheck = true
            case "--help", "-h": o.help = true
            default:
                // Ignore LaunchServices noise such as -psn_0_12345 / -NSDocumentRevisionsDebugMode.
                if !a.hasPrefix("-psn") && !a.hasPrefix("-NS") && !a.hasPrefix("-Apple") {
                    FileHandle.standardError.write(Data("locki-alert: ignoring unknown argument \(a)\n".utf8))
                }
            }
            i += 1
        }
        return o
    }

    var windowLevel: NSWindow.Level {
        switch level.lowercased() {
        case "screensaver": return .screenSaver
        case "shielding": return NSWindow.Level(rawValue: Int(CGShieldingWindowLevel()))
        case "statusbar": return .statusBar
        case "popup": return .popUpMenu
        case "mainmenu": return .mainMenu
        case "floating": return .floating
        default: return NSWindow.Level(rawValue: Int(level) ?? NSWindow.Level.screenSaver.rawValue)
        }
    }

    var collectionBehavior: NSWindow.CollectionBehavior {
        var b: NSWindow.CollectionBehavior = []
        for raw in behavior.split(separator: ",") {
            switch raw.trimmingCharacters(in: .whitespaces) {
            case "canJoinAllSpaces": b.insert(.canJoinAllSpaces)
            case "moveToActiveSpace": b.insert(.moveToActiveSpace)
            case "fullScreenAuxiliary": b.insert(.fullScreenAuxiliary)
            case "fullScreenNone": b.insert(.fullScreenNone)
            case "stationary": b.insert(.stationary)
            case "ignoresCycle": b.insert(.ignoresCycle)
            case "transient": b.insert(.transient)
            case "managed": b.insert(.managed)
            case "canJoinAllApplications":
                if #available(macOS 13.0, *) { b.insert(.canJoinAllApplications) }
            default: break
            }
        }
        return b
    }
}

// MARK: paths

struct Paths {
    let stateDir: URL
    let lock: URL
    let pid: URL
    let acks: URL
    let token: URL
    let log: URL
    let launcherEnv: URL

    init(_ o: Options) {
        let home = URL(fileURLWithPath: NSHomeDirectory())
        func expand(_ p: String) -> URL { URL(fileURLWithPath: (p as NSString).expandingTildeInPath) }
        stateDir = o.stateDir.map(expand) ?? home.appendingPathComponent(".locki/run")
        lock = stateDir.appendingPathComponent("alert.lock")
        pid = stateDir.appendingPathComponent("alert.pid")
        acks = stateDir.appendingPathComponent("alert-acks.json")
        if let t = o.tokenFile { token = expand(t) }
        else if let env = ProcessInfo.processInfo.environment["LOCKI_ALERT_TOKEN_FILE"], !env.isEmpty { token = expand(env) }
        else { token = home.appendingPathComponent(".locki/run/alert.token") }
        log = o.logFile.map(expand) ?? home.appendingPathComponent(".locki/logs/alert.log")
        launcherEnv = home.appendingPathComponent(".locki/launcher.env")
    }

    func ensureStateDir() {
        try? FileManager.default.createDirectory(at: stateDir, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
    }

    func readToken() -> String? {
        guard let s = try? String(contentsOf: token, encoding: .utf8) else { return nil }
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    /// Only LOCKI_DASHBOARD_URL is read from launcher.env; every other line is ignored and never logged.
    func dashboardURL() -> URL? {
        guard let text = try? String(contentsOf: launcherEnv, encoding: .utf8) else { return nil }
        for raw in text.split(whereSeparator: \.isNewline) {
            var line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("export ") { line = String(line.dropFirst(7)) }
            guard line.hasPrefix("LOCKI_DASHBOARD_URL=") else { continue }
            var v = String(line.dropFirst("LOCKI_DASHBOARD_URL=".count)).trimmingCharacters(in: .whitespaces)
            if v.count >= 2, let f = v.first, let l = v.last, (f == "\"" && l == "\"") || (f == "'" && l == "'") {
                v = String(v.dropFirst().dropLast())
            }
            if let u = URL(string: v), let scheme = u.scheme?.lowercased(), scheme == "http" || scheme == "https" { return u }
        }
        return nil
    }
}

// MARK: JSON-lines log

final class JSONLog {
    let url: URL
    private var handle: FileHandle?
    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()

    init(url: URL) {
        self.url = url
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        handle = try? FileHandle(forWritingTo: url)
        _ = try? handle?.seekToEnd()
    }

    func write(_ event: String, _ fields: [String: Any] = [:]) {
        var d = fields
        d["event"] = event
        d["ts"] = iso.string(from: Date())
        d["wall_ms"] = wallMs()
        d["pid"] = Int(getpid())
        guard JSONSerialization.isValidJSONObject(d),
              let data = try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys, .withoutEscapingSlashes])
        else { return }
        var line = data; line.append(0x0A)
        handle?.write(line)
        FileHandle.standardError.write(line)
    }
}

// MARK: single instance (flock + pid file)

final class SingleInstance {
    let lockURL: URL
    let pidURL: URL
    private var fd: Int32 = -1

    init(lockURL: URL, pidURL: URL) { self.lockURL = lockURL; self.pidURL = pidURL }

    func acquire() -> Bool {
        fd = open(lockURL.path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return false }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 { close(fd); fd = -1; return false }
        let tmp = pidURL.deletingLastPathComponent().appendingPathComponent(".alert.pid.\(getpid())")
        if (try? Data("\(getpid())\n".utf8).write(to: tmp)) != nil {
            chmod(tmp.path, 0o600)
            rename(tmp.path, pidURL.path)
        }
        return true
    }

    func release() {
        if let s = try? String(contentsOf: pidURL, encoding: .utf8),
           Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)) == getpid() {
            unlink(pidURL.path)
        }
        if fd >= 0 { flock(fd, LOCK_UN); close(fd); fd = -1 }
    }

    /// Pid from the pid file if that process is alive (no lock is touched, so it can never race a starting instance).
    static func livePid(_ pidURL: URL) -> Int32? {
        guard let s = try? String(contentsOf: pidURL, encoding: .utf8),
              let p = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)), p > 0 else { return nil }
        return kill(p, 0) == 0 || errno == EPERM ? p : nil
    }
}

// MARK: owner dashboard lookup

/// argv of a same-user process via sysctl(KERN_PROCARGS2). Read-only.
func processArguments(pid: pid_t) -> [String]? {
    var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
    var size = 0
    guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > 4 else { return nil }
    var buf = [UInt8](repeating: 0, count: size)
    guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0, size > 4 else { return nil }
    let argc = buf.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
    var i = 4
    while i < size && buf[i] != 0 { i += 1 }      // exec path
    while i < size && buf[i] == 0 { i += 1 }      // padding
    var args: [String] = []
    while args.count < Int(argc) && i < size {
        var j = i
        while j < size && buf[j] != 0 { j += 1 }
        args.append(String(decoding: buf[i..<j], as: UTF8.self))
        i = j + 1
    }
    return args
}

/// The owner's Chrome: a running Chrome whose argv has --user-data-dir=$HOME/.locki-owner-chrome.
func ownerChrome() -> NSRunningApplication? {
    let target = ((NSHomeDirectory() as NSString).appendingPathComponent(".locki-owner-chrome") as NSString).standardizingPath
    for app in NSWorkspace.shared.runningApplications where app.bundleIdentifier == "com.google.Chrome" {
        guard let args = processArguments(pid: app.processIdentifier) else { continue }
        for a in args where a.hasPrefix("--user-data-dir=") {
            let p = String(a.dropFirst("--user-data-dir=".count))
            if ((p as NSString).expandingTildeInPath as NSString).standardizingPath == target { return app }
        }
    }
    return nil
}

// MARK: fonts

enum FontBook {
    nonisolated(unsafe) static var archivo = false
    nonisolated(unsafe) static var note = "system (SF Pro)"

    /// Archivo at an explicit `wght` axis value. SwiftUI's .weight() cannot move a variable font's axis
    /// ("Unable to update Font Descriptor's weight"), so the variation is applied on the descriptor.
    static func archivoFont(_ size: CGFloat, wght: CGFloat) -> NSFont? {
        let variation = NSFontDescriptor.AttributeName(rawValue: kCTFontVariationAttribute as String)
        let desc = NSFontDescriptor(fontAttributes: [.name: "Archivo",
                                                     variation: [NSNumber(value: 0x7767_6874): NSNumber(value: Double(wght))]])
        return NSFont(descriptor: desc, size: size)
    }

    /// Registers the bundled Archivo variable font (copied by build.sh from frontend/node_modules). Falls back to SF Pro.
    static func register() {
        guard let url = Bundle.main.url(forResource: "archivo-latin-wght-normal", withExtension: "woff2") else {
            note = "system (SF Pro): Archivo not bundled"; return
        }
        var err: Unmanaged<CFError>?
        if CTFontManagerRegisterFontsForURL(url as CFURL, .process, &err) {
            let probe = NSFont(name: "Archivo", size: 20) ?? NSFont(name: "ArchivoRoman-Regular", size: 20)
            archivo = probe != nil
            note = archivo ? "Archivo (bundled woff2)" : "system (SF Pro): Archivo registered but not resolvable"
        } else {
            let why = err?.takeRetainedValue().localizedDescription ?? "unknown"
            note = "system (SF Pro): Archivo registration failed (\(why))"
        }
    }
}
