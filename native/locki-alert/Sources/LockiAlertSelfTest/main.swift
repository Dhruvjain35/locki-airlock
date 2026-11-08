import Foundation
import LockiAlertCore

// Self-test for the LOCKI Alert state machine. Run: swift run -c release locki-alert-selftest
// (or ~/.locki/alert/locki-alert-selftest after build.sh). Exit 0 only if every case passes.

nonisolated(unsafe) var passed = 0
nonisolated(unsafe) var failed: [String] = []
nonisolated(unsafe) var current = ""

func check(_ cond: @autoclosure () -> Bool, _ what: String, line: Int = #line) {
    if !cond() { failed.append("\(current): \(what) (line \(line))") }
}

func test(_ name: String, _ body: () throws -> Void) {
    current = name
    let before = failed.count
    do { try body() } catch { failed.append("\(name): threw \(error)") }
    if failed.count == before { passed += 1; print("PASS  \(name)") } else { print("FAIL  \(name)") }
}

let T0: Int64 = 1_762_600_000_000

func rec(id: String = "usb:evt-1", live: Bool = true, rehearsal: Bool = false, fresh: Bool = true,
         ack: Int64? = nil, verified: Bool? = true, mode: String = "CRITICAL",
         lock: String = "not_locked") -> TakeoverRecord {
    TakeoverRecord(id: id, live: live, rehearsal: rehearsal, trigger: "unknown_usb", detectedAtMs: T0 - 300,
                   fresh: fresh, localMode: mode, localModeVerified: verified,
                   headline: "Unauthorized device activity detected", sentence: "s", sentenceSource: "usb_only",
                   evidence: [EvidenceItem(kind: "usb", label: "Unknown USB device connected")],
                   response: ["lock": ResponseItem(state: lock, detail: "Dry run")],
                   incidentId: nil, acknowledgedAtMs: ack)
}

func dec(_ r: TakeoverRecord?, armed: Bool = true, session: String? = "session-A") -> TakeoverDecision {
    TakeoverDecision(nowMs: T0, armed: armed, sessionId: session, workstationId: "areeb-mbp",
                     active: r != nil, takeover: r)
}

func shows(_ cmds: [Command]) -> [Presentation] {
    cmds.compactMap { if case .show(let p) = $0 { return p } else { return nil } }
}
func hides(_ cmds: [Command]) -> [HideReason] {
    cmds.compactMap { if case .hide(let r, _) = $0 { return r } else { return nil } }
}
func reports(_ cmds: [Command]) -> [Report] {
    cmds.compactMap { if case .report(let r) = $0 { return r } else { return nil } }
}

let tmpDir = FileManager.default.temporaryDirectory.appendingPathComponent("locki-alert-selftest-\(getpid())")
try? FileManager.default.createDirectory(at: tmpDir, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: tmpDir) }

// 1. show threshold
test("live + fresh + unacked shows exactly once") {
    var m = AlertStateMachine()
    let c = m.handle(.decision(dec(rec())), nowMs: T0)
    check(shows(c).count == 1, "one show")
    check(shows(c).first?.rehearsalBanner == false, "live record has no rehearsal banner")
    check(m.visible?.record.id == "usb:evt-1", "visible id")
}
test("not live (fixture, rehearsal=false) never shows") {
    var m = AlertStateMachine()
    check(shows(m.handle(.decision(dec(rec(live: false, rehearsal: false))), nowMs: T0)).isEmpty, "no show")
    check(m.visible == nil, "nothing visible")
}
test("acknowledged on backend (acknowledged_at_ms set) never shows") {
    var m = AlertStateMachine()
    check(shows(m.handle(.decision(dec(rec(ack: T0 - 10))), nowMs: T0)).isEmpty, "no show")
}
test("stale (fresh=false) never shows, even live and unacked") {
    var m = AlertStateMachine()
    check(shows(m.handle(.decision(dec(rec(fresh: false))), nowMs: T0)).isEmpty, "no show")
    check(shows(m.handle(.decision(dec(rec(fresh: false))), nowMs: T0 + 200)).isEmpty, "still no show")
}
test("disarmed decision carrying a record never shows") {
    var m = AlertStateMachine()
    check(shows(m.handle(.decision(dec(rec(), armed: false)), nowMs: T0)).isEmpty, "no show")
}

// 2. rehearsal requires the banner
test("rehearsal record (live=false, rehearsal=true) shows ONLY with the REHEARSAL banner") {
    var m = AlertStateMachine()
    let p = shows(m.handle(.decision(dec(rec(id: "rehearsal:fx-1", live: false, rehearsal: true))), nowMs: T0))
    check(p.count == 1, "one show")
    check(p.first?.rehearsalBanner == true, "banner flag set")
}
test("invariant: no presentation of a live=false record without the banner (exhaustive flags)") {
    for live in [false, true] { for reh in [false, true] { for fresh in [false, true] {
        var m = AlertStateMachine()
        for p in shows(m.handle(.decision(dec(rec(id: "x:\(live)\(reh)\(fresh)", live: live, rehearsal: reh, fresh: fresh))), nowMs: T0)) {
            check(p.record.live || p.rehearsalBanner, "live=\(live) rehearsal=\(reh) fresh=\(fresh) shown without banner")
        }
    } } }
}
test("local sample (--rehearsal) always has the banner and is never reported or acked") {
    var m = AlertStateMachine()
    let c = m.handle(.rehearsalSample(RehearsalSample.record(nowMs: T0)), nowMs: T0)
    check(shows(c).first?.rehearsalBanner == true, "banner")
    check(m.handle(.panelShown(id: m.visible!.record.id, visible: true), nowMs: T0 + 50).isEmpty, "no shown report")
    let d = m.handle(.dismiss(detail: nil), nowMs: T0 + 1000)
    check(hides(d) == [.dismissed], "hidden")
    check(reports(d).isEmpty, "no dismissed report")
    check(!d.contains { if case .persistAck = $0 { return true } else { return false } }, "no ack persisted")
}

// 3. one show per id
test("one show per id: 50 identical polls produce one show") {
    var m = AlertStateMachine()
    var total = 0
    for i in 0..<50 { total += shows(m.handle(.decision(dec(rec())), nowMs: T0 + Int64(i) * 200)).count }
    check(total == 1, "total shows \(total)")
}
test("same id after dismiss in the same run never re-shows") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    _ = m.handle(.dismiss(detail: nil), nowMs: T0 + 2000)
    check(shows(m.handle(.decision(dec(rec())), nowMs: T0 + 2200)).isEmpty, "no re-show")
}
test("same id after handoff never re-shows; handoff reports view and persists") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    let c = m.handle(.handoff(view: "explain", dashboardReachable: true), nowMs: T0 + 1500)
    check(hides(c) == [.handoff], "hidden on handoff")
    check(reports(c).first?.event == "handoff" && reports(c).first?.view == "explain", "handoff report")
    check(shows(m.handle(.decision(dec(rec())), nowMs: T0 + 1700)).isEmpty, "no re-show")
}
test("handoff with no reachable dashboard keeps the panel up and still reports") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    let c = m.handle(.handoff(view: "evidence", dashboardReachable: false), nowMs: T0 + 1500)
    check(hides(c).isEmpty, "not hidden")
    check(m.visible != nil, "still visible")
    check(reports(c).first?.view == "evidence", "reported")
}
test("shown report carries latency = shown wall ms - detected_at_ms") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    let r = reports(m.handle(.panelShown(id: "usb:evt-1", visible: true), nowMs: T0 + 40))
    check(r.count == 1 && r[0].event == "shown", "one shown report")
    check(r.first?.latencyMs == 340, "latency \(String(describing: r.first?.latencyMs)) == 340")
    check(r.first?.visible == true, "visible flag")
}

// 4. ack persisted across restart
test("ack persisted across restart (file round trip)") {
    let url = tmpDir.appendingPathComponent("alert-acks.json")
    var m1 = AlertStateMachine(acks: AckStore.load(from: url))
    _ = m1.handle(.decision(dec(rec())), nowMs: T0)
    let c = m1.handle(.dismiss(detail: nil), nowMs: T0 + 3000)
    check(c.contains(.persistAck(id: "usb:evt-1", event: "dismissed")), "persist command")
    try m1.acks.save(to: url)
    var m2 = AlertStateMachine(acks: AckStore.load(from: url))   // "restart"
    check(m2.acks.contains("usb:evt-1"), "loaded ack")
    check(shows(m2.handle(.decision(dec(rec())), nowMs: T0 + 5000)).isEmpty, "no replay after restart")
    var perm = stat(); stat(url.path, &perm)
    check(perm.st_mode & 0o777 == 0o600, "ack file mode 0600")
}
test("ack store caps at 200 newest ids; corrupt file loads empty") {
    var s = AckStore()
    for i in 0..<250 { s.insert("id-\(i)", event: "dismissed", atMs: Int64(i)) }
    check(s.entries.count == 200, "count 200")
    check(!s.contains("id-49") && s.contains("id-50") && s.contains("id-249"), "oldest evicted")
    let bad = tmpDir.appendingPathComponent("bad.json")
    try Data("{not json".utf8).write(to: bad)
    check(AckStore.load(from: bad).entries.isEmpty, "corrupt => empty")
}

// 5. reconnect does not re-show
test("backend drop while showing keeps the record with the connection-lost line; reconnect same id = update, not show") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    let lost = m.handle(.failure(.unreachable("refused")), nowMs: T0 + 1000)
    check(hides(lost).isEmpty, "not hidden")
    if case .update(let p)? = lost.first { check(p.connectionLostAtMs == T0 + 1000, "lost stamp") } else { check(false, "update emitted") }
    check(m.handle(.failure(.unreachable("refused")), nowMs: T0 + 1200).isEmpty, "no repeat update")
    let back = m.handle(.decision(dec(rec())), nowMs: T0 + 5000)
    check(shows(back).isEmpty, "no second show")
    if case .update(let p)? = back.first { check(p.connectionLostAtMs == nil, "lost line cleared") } else { check(false, "update on reconnect") }
}
test("reconnect after dismiss: backend restarted and lost the ack -> still no re-show") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    _ = m.handle(.dismiss(detail: nil), nowMs: T0 + 1000)
    _ = m.handle(.failure(.unreachable("refused")), nowMs: T0 + 2000)
    check(shows(m.handle(.decision(dec(rec(ack: nil))), nowMs: T0 + 6000)).isEmpty, "no re-show")
}

// 6. disarm / session change / acknowledged elsewhere close
test("disarm closes the panel") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    check(hides(m.handle(.decision(dec(nil, armed: false, session: nil)), nowMs: T0 + 400)) == [.disarmed], "hide disarmed")
    check(m.visible == nil, "nothing visible")
}
test("session change closes the panel") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    check(hides(m.handle(.decision(dec(nil, session: "session-B")), nowMs: T0 + 400)) == [.sessionChanged], "hide session change")
}
test("acknowledged elsewhere (acknowledged_at_ms becomes non-null) hides and persists the ack") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec())), nowMs: T0)
    let c = m.handle(.decision(dec(rec(ack: T0 + 900))), nowMs: T0 + 1000)
    check(hides(c) == [.acknowledgedElsewhere], "hidden")
    check(c.contains(.persistAck(id: "usb:evt-1", event: "acknowledged_elsewhere")), "ack persisted")
    check(m.acks.contains("usb:evt-1"), "ack in store")
}
test("response states update in place without a new show") {
    var m = AlertStateMachine()
    _ = m.handle(.decision(dec(rec(lock: "not_locked"))), nowMs: T0)
    let c = m.handle(.decision(dec(rec(lock: "dry_run"))), nowMs: T0 + 200)
    check(shows(c).isEmpty, "no show")
    if case .update(let p)? = c.first { check(p.record.response["lock"]?.state == "dry_run", "updated") } else { check(false, "update") }
}

// 7. backend 404 / down while hidden shows nothing
test("backend 404 (no native support) while hidden shows nothing") {
    var m = AlertStateMachine()
    for i in 0..<10 { check(m.handle(.failure(.notSupported), nowMs: T0 + Int64(i) * 200).isEmpty, "no commands") }
    check(m.visible == nil, "nothing visible")
    check(m.connection == .failing(.notSupported, sinceMs: T0), "connection state records 404")
}
test("backend down / 401 / bad payload while hidden shows nothing") {
    var m = AlertStateMachine()
    for f in [TransportFailure.unreachable("timeout"), .unauthorized, .forbidden, .badStatus(500), .badPayload("x")] {
        check(m.handle(.failure(f), nowMs: T0).isEmpty, "no commands for \(f)")
    }
    check(m.visible == nil, "nothing visible")
}

// 8. decoding and display rules
test("decodes the contract JSON (ints or floats, unknown keys ignored); unverified -> server-confirmed kicker") {
    let json = """
    {"now_ms": 1762600000000.0, "armed": true, "session_id": "session-5acd", "workstation_id": null, "active": true,
     "takeover": {"id": "incident:abc", "live": true, "rehearsal": false, "trigger": "correlated_incident",
       "detected_at_ms": 1762599999000, "fresh": true, "local_mode": "WATCHFUL", "local_mode_verified": false,
       "headline": "H", "sentence": "S", "sentence_source": "spacetimedb_summary",
       "evidence": [{"kind": "person", "label": "Person at desk", "detail": "d", "at_ms": 1762599995000, "source": "remote_camera", "event_id": "e1"},
                    {"kind": "scope", "label": "Workstation and session", "detail": "x", "at_ms": null, "source": "local"}],
       "response": {"audio": {"state": "played", "detail": "Kokoro"}, "lock": {"state": "dry_run", "detail": "Dry run"}, "future_row": {"state": "x"}},
       "incident_id": "incident:abc", "acknowledged_at_ms": null, "shown_at_ms": null, "shown_latency_ms": null, "new_field": 1},
     "blocked": [], "native": {"connected": true, "last_poll_ms": 1, "pid": 2, "version": "1"}, "handoff": null}
    """
    let d = try TakeoverDecision.decode(Data(json.utf8))
    check(d.nowMs == 1_762_600_000_000, "float now_ms")
    check(d.takeover?.evidence.count == 2 && d.takeover?.evidence[1].atMs == nil, "evidence")
    check(d.takeover?.criticalVerified == false, "not verified")
    check(Display.kicker(for: d.takeover!) == "Server-confirmed incident · local CRITICAL not verified", "kicker text")
    check(Display.orderedResponseKeys(d.takeover!.response) == ["audio", "lock", "future_row"], "row order")
}
test("decision without armed is a bad payload (never treated as disarm or as a record)") {
    check((try? TakeoverDecision.decode(Data(#"{"takeover": null}"#.utf8))) == nil, "throws")
}
test("lock row says Locked only for confirmed") {
    for s in ["not_locked", "dry_run", "requested", "executed", "refused", "failed"] {
        check(!Display.stateLabel("lock", s).lowercased().hasPrefix("locked"), "\(s) must not read as locked")
    }
    check(Display.stateLabel("lock", "confirmed") == "Locked (confirmed)", "confirmed")
}

print("")
print("selftest: \(passed) passed, \(failed.count) failed")
for f in failed { print("  - \(f)") }
exit(failed.isEmpty ? 0 : 1)
