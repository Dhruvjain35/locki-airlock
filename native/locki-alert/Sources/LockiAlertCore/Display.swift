import Foundation

/// Pure display helpers shared by the SwiftUI view and the self-test. They only reword enum values the
/// backend sent; they never add a claim. In particular the lock row says "Locked" only for `confirmed`.
public enum Tone: String, Sendable { case good, neutral, caution }

public enum Display {
    public static let responseOrder = ["audio", "tools", "lock", "monitoring", "nemotron", "evidence"]

    public static func responseTitle(_ key: String) -> String {
        switch key {
        case "audio": return "Audio warning"
        case "tools": return "Tools"
        case "lock": return "Screen lock"
        case "monitoring": return "Monitoring"
        case "nemotron": return "Nemotron"
        case "evidence": return "Evidence"
        default: return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// Ordered response rows: the six contract keys first, then any extra keys the backend added.
    public static func orderedResponseKeys(_ response: [String: ResponseItem]) -> [String] {
        let known = responseOrder.filter { response[$0] != nil }
        let extra = response.keys.filter { !responseOrder.contains($0) }.sorted()
        return known + extra
    }

    public static func stateLabel(_ key: String, _ state: String) -> String {
        switch (key, state) {
        case ("lock", "confirmed"): return "Locked (confirmed)"
        case ("lock", "not_locked"): return "Not locked"
        case ("lock", "dry_run"): return "Dry run"
        case ("lock", "requested"): return "Lock requested"
        case ("lock", "executed"): return "Lock sent, unconfirmed"
        case ("tools", "not_frozen"): return "Not frozen"
        case ("tools", "not_configured"): return "Not configured"
        case ("evidence", "local_only"): return "Local only"
        default: return state.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    public static func tone(_ key: String, _ state: String) -> Tone {
        switch state {
        case "played", "frozen", "confirmed", "running", "answered", "recorded", "available": return .good
        case "failed", "refused", "degraded", "stopped", "unavailable", "unknown": return .caution
        default: return .neutral
        }
    }

    public static let criticalKicker = "CRITICAL SECURITY EVENT"
    public static let unverifiedKicker = "Server-confirmed incident · local CRITICAL not verified"
    public static let rehearsalBanner = "REHEARSAL — sample, not a security event"
    public static let footnote = "Dismissing hides this view only. Protection stays armed and every record is kept."

    public static func kicker(for record: TakeoverRecord) -> String {
        record.criticalVerified ? criticalKicker : unverifiedKicker
    }

    public static func clock(_ ms: Int64?, tenths: Bool = true) -> String {
        guard let ms else { return "—" }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = tenths ? "HH:mm:ss.S" : "HH:mm:ss"
        return f.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
    }

    public static func connectionLostLine(_ ms: Int64) -> String {
        "Connection to LOCKI lost at \(clock(ms, tenths: false)); showing the last record received"
    }
}

/// Sample content for `--rehearsal` and the menu's "Rehearsal alert". live=false, rehearsal=true, and every
/// string says it is a sample, so it can never be mistaken for a security event.
public enum RehearsalSample {
    public static func record(nowMs: Int64) -> TakeoverRecord {
        TakeoverRecord(
            id: "rehearsal:sample-\(nowMs)",
            live: false, rehearsal: true, trigger: "unknown_usb",
            detectedAtMs: nowMs, fresh: true, localMode: "CRITICAL", localModeVerified: true,
            headline: "Unauthorized device activity detected",
            sentence: "Sample: an unknown USB device connected 4.0 s after the Jetson reported a person at this desk.",
            sentenceSource: "local_timing",
            evidence: [
                EvidenceItem(kind: "person", label: "Person at desk", detail: "Sample · Jetson jetson-01 · 91% confidence",
                             atMs: nowMs - 4000, source: "remote_camera", eventId: "sample-person"),
                EvidenceItem(kind: "usb", label: "Unknown USB device connected", detail: "Sample · Unknown USB mass storage · 0781:5583",
                             atMs: nowMs, source: "mac_usb_watcher", eventId: "sample-usb"),
                EvidenceItem(kind: "scope", label: "Workstation and session", detail: "Sample workstation · sample session",
                             atMs: nil, source: "local"),
                EvidenceItem(kind: "sensors", label: "Sensor health", detail: "Sample · no sensors were read for this rehearsal",
                             atMs: nil, source: "backend"),
            ],
            response: [
                "audio": ResponseItem(state: "skipped", detail: "Rehearsal: no audio warning is played"),
                "tools": ResponseItem(state: "not_frozen", detail: "Rehearsal: no tools were frozen"),
                "lock": ResponseItem(state: "not_locked", detail: "Rehearsal: the screen was not locked"),
                "monitoring": ResponseItem(state: "running", detail: "Rehearsal: shown by the alert helper only"),
                "nemotron": ResponseItem(state: "unknown", detail: "Rehearsal: no investigation was started"),
                "evidence": ResponseItem(state: "unavailable", detail: "Rehearsal: nothing was recorded"),
            ],
            incidentId: nil)
    }
}
