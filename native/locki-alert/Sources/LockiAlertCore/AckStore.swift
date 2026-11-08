import Foundation

/// Takeover ids the owner already dismissed (or handed off) on this Mac, newest last, capped at 200.
/// Persisted as ~/.locki/run/alert-acks.json (or <state-dir>/alert-acks.json) so a helper restart or a
/// backend reconnect never replays an acknowledged alert.
public struct AckEntry: Codable, Equatable, Sendable {
    public var id: String
    public var event: String
    public var atMs: Int64
    enum CodingKeys: String, CodingKey { case id, event, atMs = "at_ms" }
}

public struct AckStore: Equatable, Sendable {
    public static let limit = 200
    public private(set) var entries: [AckEntry] = []

    public init(entries: [AckEntry] = []) {
        self.entries = Array(entries.suffix(AckStore.limit))
    }

    public func contains(_ id: String) -> Bool { entries.contains { $0.id == id } }

    public mutating func insert(_ id: String, event: String, atMs: Int64) {
        entries.removeAll { $0.id == id }
        entries.append(AckEntry(id: id, event: event, atMs: atMs))
        if entries.count > AckStore.limit { entries.removeFirst(entries.count - AckStore.limit) }
    }

    private struct FileShape: Codable { var version: Int; var acks: [AckEntry] }

    /// Missing or corrupt file => empty store (logged by the caller); never throws.
    public static func load(from url: URL) -> AckStore {
        guard let data = try? Data(contentsOf: url),
              let shape = try? JSONDecoder().decode(FileShape.self, from: data) else { return AckStore() }
        return AckStore(entries: shape.acks)
    }

    /// Atomic write (temp file + rename), mode 0600.
    public func save(to url: URL) throws {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        let data = try enc.encode(FileShape(version: 1, acks: entries))
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let tmp = dir.appendingPathComponent(".\(url.lastPathComponent).\(getpid()).tmp")
        try data.write(to: tmp, options: [])
        chmod(tmp.path, 0o600)
        if rename(tmp.path, url.path) != 0 {
            let err = String(cString: strerror(errno))
            try? FileManager.default.removeItem(at: tmp)
            throw NSError(domain: "AckStore", code: Int(errno), userInfo: [NSLocalizedDescriptionKey: err])
        }
    }
}
