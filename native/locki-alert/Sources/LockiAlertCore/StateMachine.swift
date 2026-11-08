import Foundation

// The native helper's whole decision logic, pure and deterministic (no clocks, no IO).
// The app feeds it inputs (poll results, user actions) with the current wall time and executes
// the returned commands (show / update / hide the panel, POST a report, persist an ack).
//
// Show rule (contract): takeover.live && takeover.fresh && acknowledged_at_ms == nil
//   && id not in the persisted ack file && id not already shown/handed off this run.
// A live=false record is shown only when it carries rehearsal=true, and then ALWAYS with the
// amber REHEARSAL banner. Disarm or a session change closes the panel. A transport failure
// while hidden shows nothing; while showing it keeps the record and adds the amber
// "connection lost" line. Nothing here ever invents a record.

public enum TransportFailure: Equatable, Sendable {
    case unreachable(String)   // refused, timeout, DNS
    case notSupported          // 404: backend has no native alert support yet
    case unauthorized          // 401: token missing or rotated (app re-reads the token file)
    case forbidden             // 403
    case badStatus(Int)
    case badPayload(String)

    public var summary: String {
        switch self {
        case .unreachable(let why): return "backend unreachable (\(why))"
        case .notSupported: return "backend has no native alert support yet (404)"
        case .unauthorized: return "token rejected (401); re-reading alert.token"
        case .forbidden: return "request refused (403)"
        case .badStatus(let code): return "unexpected HTTP \(code)"
        case .badPayload(let why): return "unreadable decision (\(why))"
        }
    }
}

public enum ConnectionState: Equatable, Sendable {
    case starting
    case connected
    case failing(TransportFailure, sinceMs: Int64)

    public var summary: String {
        switch self {
        case .starting: return "connecting"
        case .connected: return "connected"
        case .failing(let f, _): return f.summary
        }
    }
}

public enum HideReason: String, Sendable {
    case disarmed
    case sessionChanged = "session_changed"
    case takeoverCleared = "takeover_cleared"
    case replaced
    case acknowledgedElsewhere = "acknowledged_elsewhere"
    case dismissed
    case handoff
}

public struct Presentation: Equatable, Sendable {
    public var record: TakeoverRecord
    public var sessionId: String?
    public var workstationId: String?
    public var active: Bool?
    /// Amber "REHEARSAL — sample, not a security event" banner. Always true for live=false records.
    public var rehearsalBanner: Bool
    /// Wall ms when the backend stopped answering while this record was on screen.
    public var connectionLostAtMs: Int64?
    /// Local sample (CLI --rehearsal or menu "Rehearsal alert"): never reported, never acked.
    public var localSample: Bool
    /// Re-opened by the user from the menu after it was dismissed/handed off.
    public var reopened: Bool

    public init(record: TakeoverRecord, sessionId: String?, workstationId: String?, active: Bool? = nil,
                rehearsalBanner: Bool, connectionLostAtMs: Int64? = nil,
                localSample: Bool = false, reopened: Bool = false) {
        self.record = record; self.sessionId = sessionId; self.workstationId = workstationId
        self.active = active; self.rehearsalBanner = rehearsalBanner
        self.connectionLostAtMs = connectionLostAtMs; self.localSample = localSample; self.reopened = reopened
    }
}

public struct Report: Equatable, Sendable, Codable {
    public var takeoverId: String
    public var event: String          // shown | dismissed | handoff
    public var view: String?          // explain | evidence | stop (handoff only)
    public var atMs: Int64
    public var latencyMs: Int64?
    public var visible: Bool?
    public var detail: String?

    enum CodingKeys: String, CodingKey {
        case event, view, visible, detail
        case takeoverId = "takeover_id", atMs = "at_ms", latencyMs = "latency_ms"
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(takeoverId, forKey: .takeoverId)
        try c.encode(event, forKey: .event)
        if let view { try c.encode(view, forKey: .view) }
        try c.encode(atMs, forKey: .atMs)
        try c.encode(latencyMs, forKey: .latencyMs)   // explicit null when unknown
        try c.encode(visible, forKey: .visible)
        try c.encode(detail, forKey: .detail)
    }
}

public enum Command: Equatable, Sendable {
    case show(Presentation)
    case update(Presentation)
    case hide(HideReason, id: String)
    case report(Report)
    case persistAck(id: String, event: String)
}

public enum Input: Equatable, Sendable {
    case decision(TakeoverDecision)
    case failure(TransportFailure)
    /// The app reports the panel is on screen; produces the "shown" report with latency.
    case panelShown(id: String, visible: Bool)
    case dismiss(detail: String?)
    /// `dashboardReachable == false` keeps the panel up (it shows "Open the LOCKI dashboard").
    case handoff(view: String, dashboardReachable: Bool)
    case rehearsalSample(TakeoverRecord)
    case reopenLast
}

public struct AlertStateMachine: Sendable {
    public private(set) var visible: Presentation?
    public private(set) var lastShown: Presentation?
    public private(set) var connection: ConnectionState = .starting
    public private(set) var acks: AckStore
    public private(set) var shownThisRun: Set<String> = []
    public private(set) var lastArmed: Bool?
    public private(set) var lastSessionId: String?

    public init(acks: AckStore = AckStore()) { self.acks = acks }

    public mutating func handle(_ input: Input, nowMs: Int64) -> [Command] {
        switch input {
        case .decision(let d): return onDecision(d, nowMs: nowMs)
        case .failure(let f): return onFailure(f, nowMs: nowMs)
        case .panelShown(let id, let vis): return onPanelShown(id: id, visible: vis, nowMs: nowMs)
        case .dismiss(let detail): return onDismiss(detail: detail, nowMs: nowMs)
        case .handoff(let view, let reachable): return onHandoff(view: view, reachable: reachable, nowMs: nowMs)
        case .rehearsalSample(let rec): return onSample(rec)
        case .reopenLast: return onReopen()
        }
    }

    // MARK: decision

    private func qualifies(_ t: TakeoverRecord, _ d: TakeoverDecision) -> Presentation? {
        guard t.fresh else { return nil }                       // stale never shows
        guard t.acknowledgedAtMs == nil else { return nil }     // acknowledged on the backend
        guard !acks.contains(t.id), !shownThisRun.contains(t.id) else { return nil }
        let banner: Bool
        if t.live { banner = false }
        else if t.rehearsal { banner = true }                   // rehearsal only ever with the banner
        else { return nil }                                     // fixture: never shown
        return Presentation(record: t, sessionId: d.sessionId, workstationId: d.workstationId,
                            active: d.active, rehearsalBanner: banner)
    }

    private mutating func present(_ p: Presentation) -> Command {
        visible = p
        lastShown = p
        shownThisRun.insert(p.record.id)
        return .show(p)
    }

    private mutating func onDecision(_ d: TakeoverDecision, nowMs: Int64) -> [Command] {
        var out: [Command] = []
        connection = .connected
        lastArmed = d.armed

        // A local sample stays until its own dismiss unless a real takeover qualifies.
        if let v = visible, v.localSample {
            if d.armed, let t = d.takeover, let p = qualifies(t, d) {
                out.append(.hide(.replaced, id: v.record.id))
                visible = nil
                out.append(present(p))
            }
            lastSessionId = d.armed ? d.sessionId : nil
            return out
        }

        guard d.armed else {
            if let v = visible { out.append(.hide(.disarmed, id: v.record.id)); visible = nil }
            lastSessionId = nil
            return out
        }
        if let v = visible, v.sessionId != d.sessionId {
            out.append(.hide(.sessionChanged, id: v.record.id)); visible = nil
        }
        lastSessionId = d.sessionId

        guard let t = d.takeover else {
            if let v = visible { out.append(.hide(.takeoverCleared, id: v.record.id)); visible = nil }
            return out
        }

        if let v = visible {
            if v.record.id == t.id {
                if t.acknowledgedAtMs != nil && !v.reopened {
                    acks.insert(t.id, event: "acknowledged_elsewhere", atMs: nowMs)
                    out.append(.persistAck(id: t.id, event: "acknowledged_elsewhere"))
                    out.append(.hide(.acknowledgedElsewhere, id: t.id))
                    visible = nil
                    return out
                }
                var nv = v
                nv.record = t
                nv.workstationId = d.workstationId
                nv.active = d.active
                nv.connectionLostAtMs = nil
                nv.rehearsalBanner = v.rehearsalBanner || !t.live
                if nv != v {
                    visible = nv
                    lastShown = nv
                    out.append(.update(nv))
                }
                return out
            }
            out.append(.hide(.replaced, id: v.record.id)); visible = nil
        }

        if let p = qualifies(t, d) { out.append(present(p)) }
        return out
    }

    // MARK: transport

    private mutating func onFailure(_ f: TransportFailure, nowMs: Int64) -> [Command] {
        if case .failing(_, let since) = connection {
            connection = .failing(f, sinceMs: since)
        } else {
            connection = .failing(f, sinceMs: nowMs)
        }
        guard var v = visible, !v.localSample else { return [] }   // hidden: nothing, ever
        if v.connectionLostAtMs == nil {
            v.connectionLostAtMs = nowMs
            visible = v
            return [.update(v)]
        }
        return []
    }

    // MARK: panel + user actions

    private func onPanelShown(id: String, visible vis: Bool, nowMs: Int64) -> [Command] {
        guard let v = visible, v.record.id == id, !v.localSample, !v.reopened else { return [] }
        let latency = v.record.detectedAtMs.map { nowMs - $0 }
        return [.report(Report(takeoverId: id, event: "shown", view: nil, atMs: nowMs, latencyMs: latency,
                               visible: vis, detail: v.rehearsalBanner ? "rehearsal banner shown" : nil))]
    }

    private mutating func onDismiss(detail: String?, nowMs: Int64) -> [Command] {
        guard let v = visible else { return [] }
        visible = nil
        var out: [Command] = [.hide(.dismissed, id: v.record.id)]
        if v.localSample || v.reopened { return out }
        acks.insert(v.record.id, event: "dismissed", atMs: nowMs)
        out.append(.persistAck(id: v.record.id, event: "dismissed"))
        out.append(.report(Report(takeoverId: v.record.id, event: "dismissed", view: nil, atMs: nowMs,
                                  latencyMs: nil, visible: true, detail: detail)))
        return out
    }

    private mutating func onHandoff(view: String, reachable: Bool, nowMs: Int64) -> [Command] {
        guard let v = visible else { return [] }
        var out: [Command] = []
        if reachable {
            visible = nil
            out.append(.hide(.handoff, id: v.record.id))
        }
        if v.localSample { return out }
        if !v.reopened && reachable {
            acks.insert(v.record.id, event: "handoff:\(view)", atMs: nowMs)
            out.append(.persistAck(id: v.record.id, event: "handoff:\(view)"))
        }
        out.append(.report(Report(takeoverId: v.record.id, event: "handoff", view: view, atMs: nowMs,
                                  latencyMs: nil, visible: true,
                                  detail: reachable ? nil : "owner dashboard not reachable from the helper")))
        return out
    }

    private mutating func onSample(_ rec: TakeoverRecord) -> [Command] {
        guard visible == nil else { return [] }       // never covers a real record
        let p = Presentation(record: rec, sessionId: nil, workstationId: nil, active: nil,
                             rehearsalBanner: true, localSample: true)
        visible = p
        return [.show(p)]
    }

    private mutating func onReopen() -> [Command] {
        guard visible == nil, var p = lastShown else { return [] }
        p.reopened = true
        if case .failing(_, let since) = connection, !p.localSample { p.connectionLostAtMs = since }
        visible = p
        return [.show(p)]
    }
}
