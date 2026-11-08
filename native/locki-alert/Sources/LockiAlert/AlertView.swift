import SwiftUI
import LockiAlertCore

// Visual system = frontend/src/airlock/console.css tokens. Red is for critical accents only, NVIDIA green
// for the brand mark only, amber for rehearsal / degraded / not-verified states.
enum Palette {
    static let bg = Color(hex: 0x000000)
    static let card = Color(hex: 0x121212)
    static let raise = Color(hex: 0x1a1a1a)
    static let raise2 = Color(hex: 0x222222)
    static let text = Color(hex: 0xf5f5f5)
    static let text2 = Color(hex: 0xa3a3a3)
    static let text3 = Color(hex: 0x8a8a8a)
    static let text4 = Color(hex: 0x5c5c5c)
    static let line = Color.white.opacity(0.07)
    static let line2 = Color.white.opacity(0.12)
    static let red = Color(hex: 0xf2564f)
    static let redSoft = Color(hex: 0xf2564f).opacity(0.12)
    static let green = Color(hex: 0x76b900)
    static let amber = Color(hex: 0xf2a93b)
    static let amberSoft = Color(hex: 0xf2a93b).opacity(0.12)
    static let amberLine = Color(hex: 0xf2a93b).opacity(0.42)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xff) / 255, green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255, opacity: 1)
    }
}

final class AlertModel: ObservableObject {
    @Published var presentation: Presentation?
    @Published var appeared = false
    @Published var notice: String?
    @Published var testBanner: String?
    var scale: CGFloat = 1
    var onExplain: () -> Void = {}
    var onEvidence: () -> Void = {}
    var onDismiss: () -> Void = {}
    var onStop: () -> Void = {}
}

struct AlertRootView: View {
    @ObservedObject var model: AlertModel
    var body: some View {
        ZStack {
            Palette.bg
            if let p = model.presentation {
                AlertContent(p: p, model: model)
                    .opacity(model.appeared ? 1 : 0.4)
                    .scaleEffect(model.appeared ? 1 : 0.985)
            }
        }
        .ignoresSafeArea()
    }
}

private struct AlertContent: View {
    let p: Presentation
    @ObservedObject var model: AlertModel
    var s: CGFloat { model.scale }

    func font(_ size: CGFloat, _ weight: Font.Weight) -> Font {
        if FontBook.archivo, let nf = FontBook.archivoFont(size * s, wght: Self.wght(weight)) { return Font(nf as CTFont) }
        return .system(size: size * s, weight: weight)
    }
    static func wght(_ w: Font.Weight) -> CGFloat {
        switch w {
        case .ultraLight: return 200
        case .thin: return 100
        case .light: return 300
        case .medium: return 500
        case .semibold: return 600
        case .bold: return 700
        case .heavy: return 800
        case .black: return 900
        default: return 400
        }
    }
    func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size * s, weight: weight, design: .monospaced)
    }

    var body: some View {
        VStack(spacing: 0) {
            if p.rehearsalBanner { banner(Display.rehearsalBanner) }
            if let tb = model.testBanner { banner(tb) }
            VStack(alignment: .leading, spacing: 16 * s) {
                topBar
                if let lost = p.connectionLostAtMs {
                    Text(Display.connectionLostLine(lost))
                        .font(font(18, .semibold)).foregroundColor(Palette.amber)
                        .padding(.horizontal, 16 * s).padding(.vertical, 8 * s)
                        .background(Capsule().fill(Palette.amberSoft))
                        .overlay(Capsule().stroke(Palette.amberLine, lineWidth: 1))
                }
                kicker
                Text(p.record.headline ?? (p.record.trigger.map { "Takeover: \($0)" } ?? "Takeover record without a headline"))
                    .font(font(66, .bold)).foregroundColor(Palette.text)
                    .lineLimit(2).minimumScaleFactor(0.6).fixedSize(horizontal: false, vertical: true)
                if let sentence = p.record.sentence {
                    Text(sentence).font(font(26, .regular)).foregroundColor(Palette.text2)
                        .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                }
                HStack(alignment: .top, spacing: 20 * s) {
                    evidenceCard
                    responseCard
                }
                .frame(maxHeight: .infinity, alignment: .top)
                buttons
                if let n = model.notice {
                    Text(n).font(font(20, .semibold)).foregroundColor(Palette.amber)
                }
                VStack(alignment: .leading, spacing: 6 * s) {
                    Text(Display.footnote).font(font(16, .regular)).foregroundColor(Palette.text3)
                    Text(metaLine).font(mono(12)).foregroundColor(Palette.text4).lineLimit(2)
                }
            }
            .padding(.horizontal, 56 * s)
            .padding(.top, 26 * s)
            .padding(.bottom, 28 * s)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    func banner(_ text: String) -> some View {
        Text(text)
            .font(font(20, .heavy)).tracking(1.2).foregroundColor(.black)
            .frame(maxWidth: .infinity).padding(.vertical, 10 * s)
            .background(Palette.amber)
    }

    var topBar: some View {
        HStack(alignment: .center, spacing: 12 * s) {
            RoundedRectangle(cornerRadius: 3 * s).fill(Palette.green).frame(width: 14 * s, height: 14 * s)
            Text("LOCKI AIRLOCK").font(font(17, .heavy)).tracking(2.4).foregroundColor(Palette.text)
            Text("Security alert").font(font(15, .medium)).foregroundColor(Palette.text3)
            Spacer()
            Text(scopeLine).font(mono(14)).foregroundColor(Palette.text3).lineLimit(1)
        }
    }

    var scopeLine: String {
        var parts: [String] = []
        if let w = p.workstationId { parts.append(w) }
        if let sid = p.sessionId { parts.append(sid.count > 18 ? String(sid.prefix(18)) + "…" : sid) }
        parts.append("detected \(Display.clock(p.record.detectedAtMs))")
        return parts.joined(separator: " · ")
    }

    var kicker: some View {
        let verified = p.record.criticalVerified
        return HStack(spacing: 12 * s) {
            PulseDot(color: verified ? Palette.red : Palette.amber, size: 14 * s)
            Text(Display.kicker(for: p.record))
                .font(font(19, .heavy)).tracking(verified ? 3 : 0.6)
                .foregroundColor(verified ? Palette.red : Palette.amber)
        }
    }

    func cardShell<C: View>(_ title: String, count: Int?, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 14 * s) {
            HStack(spacing: 10 * s) {
                Text(title).font(font(21, .bold)).foregroundColor(Palette.text)
                if let count {
                    Text("\(count)").font(mono(13, .semibold)).foregroundColor(Palette.text2)
                        .padding(.horizontal, 8 * s).padding(.vertical, 2 * s)
                        .background(Capsule().fill(Palette.raise2))
                }
            }
            content()
        }
        .padding(22 * s)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 20 * s).fill(Palette.card))
        .overlay(RoundedRectangle(cornerRadius: 20 * s).stroke(Palette.line, lineWidth: 1))
    }

    var evidenceCard: some View {
        cardShell("Evidence detected", count: p.record.evidence.count) {
            if p.record.evidence.isEmpty {
                Text("The record lists no evidence items.").font(font(17, .regular)).foregroundColor(Palette.text3)
            }
            ForEach(Array(p.record.evidence.enumerated()), id: \.offset) { _, e in
                HStack(alignment: .top, spacing: 12 * s) {
                    Circle().fill(["usb", "incident"].contains(e.kind) ? Palette.red : Palette.text4)
                        .frame(width: 9 * s, height: 9 * s).padding(.top, 8 * s)
                    VStack(alignment: .leading, spacing: 3 * s) {
                        HStack(spacing: 8 * s) {
                            Text(e.label).font(font(20, .semibold)).foregroundColor(Palette.text).lineLimit(1)
                            if let sev = e.severity {
                                Text(sev.uppercased()).font(mono(11, .bold))
                                    .foregroundColor(["high", "critical"].contains(sev.lowercased()) ? Palette.red : Palette.text2)
                                    .padding(.horizontal, 6 * s).padding(.vertical, 1 * s)
                                    .background(RoundedRectangle(cornerRadius: 6 * s).fill(Palette.redSoft))
                            }
                        }
                        if let d = e.detail {
                            Text(d).font(font(16, .regular)).foregroundColor(Palette.text2).lineLimit(2)
                        }
                    }
                    Spacer(minLength: 8 * s)
                    VStack(alignment: .trailing, spacing: 4 * s) {
                        if e.atMs != nil { Text(Display.clock(e.atMs)).font(mono(15)).foregroundColor(Palette.text2) }
                        if let src = e.source {
                            Text(src.replacingOccurrences(of: "_", with: " ").uppercased())
                                .font(mono(10, .semibold)).foregroundColor(Palette.text3)
                        }
                    }
                }
            }
        }
    }

    var responseCard: some View {
        cardShell("Security response", count: nil) {
            let keys = Display.orderedResponseKeys(p.record.response)
            if keys.isEmpty {
                Text("The record lists no response states.").font(font(17, .regular)).foregroundColor(Palette.text3)
            }
            ForEach(keys, id: \.self) { key in
                if let item = p.record.response[key] {
                    HStack(alignment: .firstTextBaseline, spacing: 12 * s) {
                        Text(Display.responseTitle(key).uppercased()).font(font(13, .bold)).tracking(1)
                            .foregroundColor(Palette.text3).frame(width: 136 * s, alignment: .leading)
                        StatePill(label: Display.stateLabel(key, item.state), tone: Display.tone(key, item.state),
                                  font: font(15, .semibold), s: s)
                        if let d = item.detail {
                            Text(d).font(font(16, .regular)).foregroundColor(Palette.text2).lineLimit(2)
                        }
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }

    var buttons: some View {
        HStack(spacing: 14 * s) {
            Button(action: model.onExplain) {
                Text("EXPLAIN INCIDENT").font(font(21, .heavy)).tracking(1.2).foregroundColor(.white)
                    .padding(.horizontal, 34 * s).padding(.vertical, 17 * s)
                    .background(Capsule().fill(Palette.red))
                    .contentShape(Capsule())
            }.buttonStyle(.plain)
            pill("View evidence", model.onEvidence)
            pill("Dismiss alert view", model.onDismiss)
            pill("Stop protection", model.onStop)
            Spacer()
        }
    }

    func pill(_ title: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(font(19, .semibold)).foregroundColor(Palette.text)
                .padding(.horizontal, 24 * s).padding(.vertical, 16 * s)
                .background(Capsule().fill(Palette.raise))
                .overlay(Capsule().stroke(Palette.line2, lineWidth: 1))
                .contentShape(Capsule())
        }.buttonStyle(.plain)
    }

    var metaLine: String {
        let r = p.record
        var parts = ["takeover \(r.id)", "incident \(r.incidentId ?? "none")"]
        if let t = r.trigger { parts.append("trigger \(t)") }
        if let src = r.sentenceSource { parts.append("sentence \(src)") }
        parts.append("local mode \(r.localMode ?? "unknown") (\(r.criticalVerified ? "verified" : "not verified"))")
        if let a = p.active { parts.append("active now \(a ? "yes" : "no")") }
        parts.append(r.live ? "live" : "live=false")
        if p.reopened { parts.append("re-opened by owner") }
        return parts.joined(separator: " · ")
    }
}

private struct StatePill: View {
    let label: String
    let tone: Tone
    let font: Font
    let s: CGFloat
    var body: some View {
        let fg: Color = tone == .caution ? Palette.amber : (tone == .good ? Palette.text : Palette.text2)
        let bg: Color = tone == .caution ? Palette.amberSoft : (tone == .good ? Palette.raise2 : Palette.raise)
        return Text(label).font(font).foregroundColor(fg).lineLimit(1).fixedSize()
            .padding(.horizontal, 12 * s).padding(.vertical, 4 * s)
            .background(Capsule().fill(bg))
            .overlay(Capsule().stroke(tone == .caution ? Palette.amberLine : Palette.line2, lineWidth: 1))
    }
}

private struct PulseDot: View {
    let color: Color
    let size: CGFloat
    @State private var on = false
    var body: some View {
        ZStack {
            Circle().fill(color.opacity(0.28)).frame(width: size * 2.1, height: size * 2.1)
                .scaleEffect(on ? 1 : 0.5).opacity(on ? 0 : 0.9)
            Circle().fill(color).frame(width: size, height: size)
        }
        .frame(width: size * 2.1, height: size * 2.1)
        .onAppear {
            withAnimation(.easeOut(duration: 1.6).repeatForever(autoreverses: false)) { on = true }
        }
    }
}
