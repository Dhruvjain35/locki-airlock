import Foundation

// Wire model for GET /api/security/takeover (see native-takeover-contract.md).
// Decoding is lenient on purpose: unknown keys are ignored, numbers may arrive as int or float,
// and missing optional fields stay nil. Only `armed` (decision) and `id` (takeover) are required;
// a record without live/fresh is treated as live=false / fresh=false and therefore never shown.

public struct EvidenceItem: Codable, Equatable, Sendable {
    public var kind: String
    public var label: String
    public var detail: String?
    public var atMs: Int64?
    public var source: String?
    public var eventId: String?
    public var severity: String?

    public init(kind: String, label: String, detail: String? = nil, atMs: Int64? = nil,
                source: String? = nil, eventId: String? = nil, severity: String? = nil) {
        self.kind = kind; self.label = label; self.detail = detail; self.atMs = atMs
        self.source = source; self.eventId = eventId; self.severity = severity
    }

    enum CodingKeys: String, CodingKey {
        case kind, label, detail, source, severity
        case atMs = "at_ms", eventId = "event_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.flexString(.kind) ?? "unknown"
        label = c.flexString(.label) ?? (c.flexString(.kind) ?? "Evidence")
        detail = c.flexString(.detail)
        atMs = c.flexInt(.atMs)
        source = c.flexString(.source)
        eventId = c.flexString(.eventId)
        severity = c.flexString(.severity)
    }
}

public struct ResponseItem: Codable, Equatable, Sendable {
    public var state: String
    public var detail: String?

    public init(state: String, detail: String? = nil) { self.state = state; self.detail = detail }

    enum CodingKeys: String, CodingKey { case state, detail }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = c.flexString(.state) ?? "unknown"
        detail = c.flexString(.detail)
    }
}

public struct TakeoverRecord: Codable, Equatable, Sendable {
    public var id: String
    public var live: Bool
    public var rehearsal: Bool
    public var trigger: String?
    public var detectedAtMs: Int64?
    public var fresh: Bool
    public var localMode: String?
    public var localModeVerified: Bool?
    public var headline: String?
    public var sentence: String?
    public var sentenceSource: String?
    public var evidence: [EvidenceItem]
    /// Keys are the contract's response rows (audio, tools, lock, monitoring, nemotron, evidence).
    public var response: [String: ResponseItem]
    public var incidentId: String?
    public var acknowledgedAtMs: Int64?
    public var shownAtMs: Int64?
    public var shownLatencyMs: Int64?

    public init(id: String, live: Bool, rehearsal: Bool = false, trigger: String? = nil,
                detectedAtMs: Int64? = nil, fresh: Bool, localMode: String? = nil,
                localModeVerified: Bool? = nil, headline: String? = nil, sentence: String? = nil,
                sentenceSource: String? = nil, evidence: [EvidenceItem] = [],
                response: [String: ResponseItem] = [:], incidentId: String? = nil,
                acknowledgedAtMs: Int64? = nil, shownAtMs: Int64? = nil, shownLatencyMs: Int64? = nil) {
        self.id = id; self.live = live; self.rehearsal = rehearsal; self.trigger = trigger
        self.detectedAtMs = detectedAtMs; self.fresh = fresh; self.localMode = localMode
        self.localModeVerified = localModeVerified; self.headline = headline; self.sentence = sentence
        self.sentenceSource = sentenceSource; self.evidence = evidence; self.response = response
        self.incidentId = incidentId; self.acknowledgedAtMs = acknowledgedAtMs
        self.shownAtMs = shownAtMs; self.shownLatencyMs = shownLatencyMs
    }

    enum CodingKeys: String, CodingKey {
        case id, live, rehearsal, trigger, fresh, headline, sentence, evidence, response
        case detectedAtMs = "detected_at_ms", localMode = "local_mode"
        case localModeVerified = "local_mode_verified", sentenceSource = "sentence_source"
        case incidentId = "incident_id", acknowledgedAtMs = "acknowledged_at_ms"
        case shownAtMs = "shown_at_ms", shownLatencyMs = "shown_latency_ms"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let rid = c.flexString(.id), !rid.isEmpty else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "takeover.id missing")
        }
        id = rid
        live = c.flexBool(.live) ?? false
        rehearsal = c.flexBool(.rehearsal) ?? false
        trigger = c.flexString(.trigger)
        detectedAtMs = c.flexInt(.detectedAtMs)
        fresh = c.flexBool(.fresh) ?? false
        localMode = c.flexString(.localMode)
        localModeVerified = c.flexBool(.localModeVerified)
        headline = c.flexString(.headline)
        sentence = c.flexString(.sentence)
        sentenceSource = c.flexString(.sentenceSource)
        evidence = (try? c.decodeIfPresent([EvidenceItem].self, forKey: .evidence)) ?? []
        response = (try? c.decodeIfPresent([String: ResponseItem].self, forKey: .response)) ?? [:]
        incidentId = c.flexString(.incidentId)
        acknowledgedAtMs = c.flexInt(.acknowledgedAtMs)
        shownAtMs = c.flexInt(.shownAtMs)
        shownLatencyMs = c.flexInt(.shownLatencyMs)
    }

    /// The UI may claim CRITICAL only when the backend says ModeEngine is CRITICAL citing this evidence.
    public var criticalVerified: Bool {
        localModeVerified == true && (localMode ?? "").uppercased() == "CRITICAL"
    }
}

public struct NativeInfo: Codable, Equatable, Sendable {
    public var connected: Bool?
    public var lastPollMs: Int64?
    public var pid: Int64?
    public var version: String?
    enum CodingKeys: String, CodingKey { case connected, pid, version, lastPollMs = "last_poll_ms" }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        connected = c.flexBool(.connected); lastPollMs = c.flexInt(.lastPollMs)
        pid = c.flexInt(.pid); version = c.flexString(.version)
    }
}

public struct Handoff: Codable, Equatable, Sendable {
    public var nonce: String?
    public var view: String?
    public var takeoverId: String?
    public var incidentId: String?
    public var atMs: Int64?
    enum CodingKeys: String, CodingKey {
        case nonce, view, takeoverId = "takeover_id", incidentId = "incident_id", atMs = "at_ms"
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nonce = c.flexString(.nonce); view = c.flexString(.view); takeoverId = c.flexString(.takeoverId)
        incidentId = c.flexString(.incidentId); atMs = c.flexInt(.atMs)
    }
}

public struct TakeoverDecision: Codable, Equatable, Sendable {
    public var nowMs: Int64?
    public var armed: Bool
    public var sessionId: String?
    public var workstationId: String?
    public var active: Bool?
    public var takeover: TakeoverRecord?
    public var blocked: [String]
    public var native: NativeInfo?
    public var handoff: Handoff?

    public init(nowMs: Int64? = nil, armed: Bool, sessionId: String? = nil, workstationId: String? = nil,
                active: Bool? = nil, takeover: TakeoverRecord? = nil, blocked: [String] = []) {
        self.nowMs = nowMs; self.armed = armed; self.sessionId = sessionId
        self.workstationId = workstationId; self.active = active; self.takeover = takeover
        self.blocked = blocked; self.native = nil; self.handoff = nil
    }

    enum CodingKeys: String, CodingKey {
        case armed, active, takeover, blocked, native, handoff
        case nowMs = "now_ms", sessionId = "session_id", workstationId = "workstation_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let a = c.flexBool(.armed) else {
            throw DecodingError.dataCorruptedError(forKey: .armed, in: c, debugDescription: "armed missing")
        }
        armed = a
        nowMs = c.flexInt(.nowMs)
        sessionId = c.flexString(.sessionId)
        workstationId = c.flexString(.workstationId)
        active = c.flexBool(.active)
        // A malformed takeover object fails the whole decision (treated as a bad payload upstream)
        // rather than being silently dropped, so a real record is never hidden by a parse quirk.
        if c.contains(.takeover), !((try? c.decodeNil(forKey: .takeover)) ?? false) {
            takeover = try c.decode(TakeoverRecord.self, forKey: .takeover)
        } else {
            takeover = nil
        }
        blocked = (try? c.decodeIfPresent([String].self, forKey: .blocked)) ?? []
        native = try? c.decodeIfPresent(NativeInfo.self, forKey: .native)
        handoff = try? c.decodeIfPresent(Handoff.self, forKey: .handoff)
    }

    public static func decode(_ data: Data) throws -> TakeoverDecision {
        try JSONDecoder().decode(TakeoverDecision.self, from: data)
    }
}

extension KeyedDecodingContainer {
    func flexInt(_ key: Key) -> Int64? {
        if let v = try? decodeIfPresent(Int64.self, forKey: key) { return v }
        if let d = try? decodeIfPresent(Double.self, forKey: key), d.isFinite { return Int64(d.rounded()) }
        if let s = try? decodeIfPresent(String.self, forKey: key), let v = Int64(s) { return v }
        return nil
    }
    func flexString(_ key: Key) -> String? {
        if let s = try? decodeIfPresent(String.self, forKey: key) { return s }
        if let i = try? decodeIfPresent(Int64.self, forKey: key) { return String(i) }
        if let d = try? decodeIfPresent(Double.self, forKey: key) { return String(d) }
        if let b = try? decodeIfPresent(Bool.self, forKey: key) { return b ? "true" : "false" }
        return nil
    }
    func flexBool(_ key: Key) -> Bool? {
        if let b = try? decodeIfPresent(Bool.self, forKey: key) { return b }
        if let i = try? decodeIfPresent(Int64.self, forKey: key) { return i != 0 }
        return nil
    }
}
