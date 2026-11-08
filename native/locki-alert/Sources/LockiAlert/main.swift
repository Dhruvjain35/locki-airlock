import AppKit
import LockiAlertCore

let opts = Options.parse(CommandLine.arguments)
if opts.help { print(Options.usage); exit(0) }

// The token must never leave this Mac: only loopback backends are accepted.
guard let base = URL(string: opts.backend), let host = base.host?.lowercased(),
      ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host), base.scheme?.lowercased() == "http" else {
    FileHandle.standardError.write(Data("locki-alert: --backend must be http://127.0.0.1:<port> (loopback only), got \(opts.backend)\n".utf8))
    exit(2)
}

let paths = Paths(opts)

if opts.onceCheck {
    // Read-only status: one GET (records a heartbeat on a backend that supports it), no lock touched.
    var req = URLRequest(url: base.appendingPathComponent("api/security/takeover"))
    req.timeoutInterval = 1.5
    let token = paths.readToken()
    if let token { req.setValue(token, forHTTPHeaderField: "X-Locki-Alert-Token") }
    req.setValue(String(getpid()), forHTTPHeaderField: "X-Locki-Alert-Pid")
    req.setValue(kAppVersion, forHTTPHeaderField: "X-Locki-Alert-Version")
    let sem = DispatchSemaphore(value: 0)
    var status = 0
    var errText: String?
    var decision: TakeoverDecision?
    URLSession(configuration: .ephemeral).dataTask(with: req) { data, resp, err in
        status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        errText = err.map { ($0 as NSError).localizedDescription }
        if status == 200, let data { decision = try? TakeoverDecision.decode(data) }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 3)
    let message: String
    switch status {
    case 200: message = decision == nil ? "200 but the body does not match the TakeoverDecision contract" : "native alert endpoint OK"
    case 404: message = "backend has no native alert support yet"
    case 401: message = "token rejected (missing or rotated alert.token)"
    case 403: message = "refused (403)"
    case 0: message = "backend unreachable: \(errText ?? "no response")"
    default: message = "unexpected HTTP \(status)"
    }
    let owner = ownerChrome()
    var out: [String: Any] = [
        "app": "LOCKI Alert", "version": kAppVersion, "backend": base.absoluteString,
        "endpoint": "/api/security/takeover", "http_status": status, "native_support": status == 200 && decision != nil,
        "message": message, "token_file": paths.token.path, "token_present": token != nil,
        "state_dir": paths.stateDir.path, "running_instance_pid": SingleInstance.livePid(paths.pid).map { Int($0) } ?? NSNull(),
        "acks": AckStore.load(from: paths.acks).entries.count, "log": paths.log.path,
        "owner_dashboard_chrome_pid": owner.map { Int($0.processIdentifier) } ?? NSNull(),
        "dashboard_url_configured": paths.dashboardURL() != nil,
    ]
    if let d = decision {
        out["armed"] = d.armed
        out["active"] = d.active ?? NSNull()
        out["takeover_id"] = d.takeover?.id ?? NSNull()
        out["blocked"] = d.blocked
    }
    if let data = try? JSONSerialization.data(withJSONObject: out, options: [.sortedKeys, .prettyPrinted, .withoutEscapingSlashes]) {
        print(String(decoding: data, as: UTF8.self))
    }
    exit(0)
}

paths.ensureStateDir()
let instance = SingleInstance(lockURL: paths.lock, pidURL: paths.pid)
guard instance.acquire() else {
    let holder = SingleInstance.livePid(paths.pid).map { String($0) } ?? "unknown"
    print("{\"event\":\"already_running\",\"holder_pid\":\"\(holder)\",\"lock\":\"\(paths.lock.path)\"}")
    exit(0)
}

let log = JSONLog(url: paths.log)
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = AppController(opts: opts, paths: paths, log: log, instance: instance, base: base)
app.delegate = controller
app.run()
