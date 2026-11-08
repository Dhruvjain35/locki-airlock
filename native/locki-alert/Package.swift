// swift-tools-version:6.0
// LOCKI Alert: native macOS takeover helper for LOCKI AIRLOCK.
// LockiAlertCore is pure Swift (Foundation only): decision model, state machine, ack store.
// LockiAlert is the AppKit + SwiftUI agent app. LockiAlertSelfTest exercises the core state machine.
import PackageDescription

let package = Package(
    name: "LockiAlert",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "locki-alert", targets: ["LockiAlert"]),
        .executable(name: "locki-alert-selftest", targets: ["LockiAlertSelfTest"]),
    ],
    targets: [
        .target(name: "LockiAlertCore"),
        .executableTarget(
            name: "LockiAlert",
            dependencies: ["LockiAlertCore"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .executableTarget(name: "LockiAlertSelfTest", dependencies: ["LockiAlertCore"]),
    ]
)
