import AppKit
import SwiftUI

struct HUDView: View {
    @ObservedObject var model: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header

            if !model.userTranscript.isEmpty {
                Text(model.userTranscript)
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.72))
                    .lineLimit(2)
                    .contentTransition(.interpolate)
            }

            if !model.assistantTranscript.isEmpty {
                Text(model.assistantTranscript)
                    .font(.system(size: 15, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.96))
                    .lineLimit(3)
                    .contentTransition(.interpolate)
            }

            if let reference = model.references.first {
                referencePill(reference)
            }

            if !model.resultDetail.isEmpty {
                Label(model.resultDetail, systemImage: resultIcon)
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(model.state == .error ? .red.opacity(0.9) : .green.opacity(0.9))
                    .lineLimit(1)
            }

            if !model.latencyText.isEmpty {
                Text(model.latencyText)
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.36))
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 15)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay {
                    RoundedRectangle(cornerRadius: 22, style: .continuous)
                        .fill(Color.black.opacity(0.34))
                }
                .overlay {
                    RoundedRectangle(cornerRadius: 22, style: .continuous)
                        .strokeBorder(accent.opacity(0.26), lineWidth: 0.8)
                }
                .shadow(color: accent.opacity(0.18), radius: 24, y: 7)
                .shadow(color: .black.opacity(0.32), radius: 18, y: 10)
        }
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .animation(.snappy(duration: 0.28), value: model.revision)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var header: some View {
        HStack(spacing: 10) {
            HUDPulse(state: model.state, color: accent)
                .frame(width: 25, height: 20)

            VStack(alignment: .leading, spacing: 1) {
                Text(stateTitle)
                    .font(.system(size: 12, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white.opacity(0.94))
                if !model.activityDetail.isEmpty {
                    Text(model.activityDetail)
                        .font(.system(size: 10.5, weight: .medium, design: .rounded))
                        .foregroundStyle(.white.opacity(0.5))
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 8)

            Text("FRIDAY")
                .font(.system(size: 9, weight: .bold, design: .rounded))
                .tracking(1.8)
                .foregroundStyle(accent.opacity(0.7))
        }
    }

    private func referencePill(_ reference: HUDReference) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.triangle.branch")
                .font(.system(size: 9, weight: .semibold))
            Text("\(reference.phrase)  →  \(displayTarget(reference.target))")
                .lineLimit(1)
            Text(reference.source)
                .foregroundStyle(.white.opacity(0.32))
                .lineLimit(1)
        }
        .font(.system(size: 10.5, weight: .medium, design: .rounded))
        .foregroundStyle(accent.opacity(0.9))
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(accent.opacity(0.09), in: Capsule())
    }

    private var accent: Color {
        model.state == .error ? Color.red : Color(red: 0.27, green: 0.82, blue: 1)
    }

    private var stateTitle: String {
        switch model.state {
        case .disconnected: "Disconnected"
        case .sleeping: "Sleeping"
        case .wakeDetected: "Ready"
        case .listening: "Listening"
        case .thinking: "Thinking"
        case .acting: "Acting"
        case .speaking: "Speaking"
        case .followupWindow: "Listening for a follow-up"
        case .error: "Something went wrong"
        }
    }

    private var resultIcon: String {
        model.state == .error ? "exclamationmark.circle.fill" : "checkmark.circle.fill"
    }

    private var accessibilityText: String {
        [stateTitle, model.userTranscript, model.assistantTranscript, model.resultDetail]
            .filter { !$0.isEmpty }
            .joined(separator: ". ")
    }

    private func displayTarget(_ target: String) -> String {
        guard target.hasPrefix("/") || target.hasPrefix("file://") else { return target }
        let path = target.hasPrefix("file://")
            ? URL(string: target)?.path ?? target
            : target
        return URL(fileURLWithPath: path).lastPathComponent
    }
}

private struct HUDPulse: View {
    let state: AssistantState
    let color: Color

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 24.0)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            Canvas { graphics, size in
                let bars = 5
                for index in 0..<bars {
                    let phase = t * speed + Double(index) * 0.7
                    let motion = shouldMove ? (sin(phase) + 1) / 2 : 0.2
                    let height = 4 + motion * (size.height - 5)
                    let width: CGFloat = 2.2
                    let x = CGFloat(index) * 5 + 1
                    let rect = CGRect(
                        x: x,
                        y: (size.height - height) / 2,
                        width: width,
                        height: height
                    )
                    graphics.fill(
                        Path(roundedRect: rect, cornerRadius: width / 2),
                        with: .color(color.opacity(0.75 + motion * 0.25))
                    )
                }
            }
        }
    }

    private var shouldMove: Bool {
        switch state {
        case .wakeDetected, .listening, .thinking, .acting, .speaking, .followupWindow:
            true
        default:
            false
        }
    }

    private var speed: Double {
        switch state {
        case .listening, .speaking: 7
        case .acting: 4
        default: 2.5
        }
    }
}

