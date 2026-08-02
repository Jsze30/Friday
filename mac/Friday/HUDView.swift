import AppKit
import SwiftUI

struct HUDView: View {
    @ObservedObject var model: AppState
    @State private var isPresented = false
    @State private var stateBeganAt = Date()
    @State private var contentVisible = false

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            messageStack
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.top, 23)

            MagicRingsOrb(
                state: model.state,
                stateBeganAt: stateBeganAt,
                revision: model.revision
            )
            .frame(width: 126, height: 126)
            .accessibilityHidden(true)
        }
        .padding(.leading, 12)
        .padding(.trailing, 2)
        .padding(.top, 2)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
        .opacity(isPresented ? 1 : 0)
        .scaleEffect(isPresented ? 1 : 0.68, anchor: .topTrailing)
        .offset(x: isPresented ? 0 : 15, y: isPresented ? 0 : -7)
        .blur(radius: isPresented ? 0 : 7)
        .animation(.spring(response: 0.52, dampingFraction: 0.72), value: isPresented)
        .onAppear {
            stateBeganAt = Date()
            setPresentation(model.shouldShowHUD)
        }
        .onChange(of: model.state) { _, newState in
            stateBeganAt = Date()
            setPresentation(model.shouldShowHUD)
            if newState == .wakeDetected {
                contentVisible = false
            } else if model.shouldShowHUD {
                withAnimation(.easeOut(duration: 0.32).delay(0.08)) {
                    contentVisible = true
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    private var messageStack: some View {
        VStack(alignment: .trailing, spacing: 7) {
            HStack(spacing: 6) {
                if model.state == .acting {
                    Image(systemName: "bolt.fill")
                        .font(.system(size: 8, weight: .bold))
                }
                Text(stateTitle.uppercased())
                    .font(.system(size: 9, weight: .bold, design: .rounded))
                    .tracking(1.8)
            }
            .foregroundStyle(accent.opacity(0.9))

            if !primaryText.isEmpty {
                Text(primaryText)
                    .font(.system(size: 14, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.94))
                    .multilineTextAlignment(.trailing)
                    .lineLimit(3)
                    .contentTransition(.opacity)
                    .shadow(color: .black.opacity(0.95), radius: 5, y: 1)
                    .shadow(color: accent.opacity(0.2), radius: 10)
            }

            if !secondaryText.isEmpty {
                Text(secondaryText)
                    .font(.system(size: 10.5, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.52))
                    .multilineTextAlignment(.trailing)
                    .lineLimit(2)
                    .contentTransition(.opacity)
                    .shadow(color: .black.opacity(0.9), radius: 4, y: 1)
            }

            if !model.resultDetail.isEmpty {
                Label(model.resultDetail, systemImage: resultIcon)
                    .font(.system(size: 10.5, weight: .semibold, design: .rounded))
                    .foregroundStyle(model.state == .error ? Color.red.opacity(0.95) : Color.green.opacity(0.92))
                    .lineLimit(1)
                    .shadow(color: .black.opacity(0.9), radius: 4, y: 1)
            }
        }
        .opacity(contentVisible ? 1 : 0)
        .offset(x: contentVisible ? 0 : 12)
        .blur(radius: contentVisible ? 0 : 4)
        .animation(.easeOut(duration: 0.3), value: model.revision)
    }

    private var primaryText: String {
        if model.state == .error {
            return model.lastError ?? "Something went wrong"
        }
        if !model.assistantTranscript.isEmpty {
            return model.assistantTranscript
        }
        if !model.userTranscript.isEmpty {
            return model.userTranscript
        }
        return model.activityDetail
    }

    private var secondaryText: String {
        if !model.assistantTranscript.isEmpty, !model.activityDetail.isEmpty {
            return model.activityDetail
        }
        if !model.userTranscript.isEmpty, !model.activityDetail.isEmpty,
           model.userTranscript != model.activityDetail {
            return model.activityDetail
        }
        return model.latencyText
    }

    private var accent: Color {
        model.state == .error
            ? Color(red: 1, green: 0.25, blue: 0.38)
            : Color(red: 0.25, green: 0.92, blue: 1)
    }

    private var stateTitle: String {
        switch model.state {
        case .disconnected: "Offline"
        case .sleeping: "Friday"
        case .wakeDetected: "Friday"
        case .listening: "Listening"
        case .thinking: "Thinking"
        case .acting: "Working"
        case .speaking: "Speaking"
        case .followupWindow: "Still here"
        case .error: "Attention"
        }
    }

    private var resultIcon: String {
        model.state == .error ? "exclamationmark.circle.fill" : "checkmark.circle.fill"
    }

    private var accessibilityText: String {
        [stateTitle, primaryText, secondaryText, model.resultDetail]
            .filter { !$0.isEmpty }
            .joined(separator: ". ")
    }

    private func setPresentation(_ presented: Bool) {
        if presented {
            withAnimation(.spring(response: 0.52, dampingFraction: 0.72)) {
                isPresented = true
            }
            withAnimation(.easeOut(duration: 0.28).delay(0.22)) {
                contentVisible = model.state != .wakeDetected
            }
        } else {
            withAnimation(.easeIn(duration: 0.18)) {
                contentVisible = false
            }
            withAnimation(.easeInOut(duration: 0.48).delay(0.06)) {
                isPresented = false
            }
        }
    }
}

private struct MagicRingsOrb: View {
    let state: AssistantState
    let stateBeganAt: Date
    let revision: Int

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60.0, paused: false)) { context in
            Canvas(opaque: false, colorMode: .extendedLinear, rendersAsynchronously: true) { graphics, size in
                drawRings(
                    in: &graphics,
                    size: size,
                    time: context.date.timeIntervalSinceReferenceDate,
                    stateAge: max(0, context.date.timeIntervalSince(stateBeganAt))
                )
            }
        }
    }

    private func drawRings(
        in graphics: inout GraphicsContext,
        size: CGSize,
        time: Double,
        stateAge: Double
    ) {
        let configuration = RingConfiguration.forState(state)
        let unit = min(size.width, size.height)
        let entrance = springEntrance(stateAge)
        let breathe = 1 + sin(time * configuration.breatheSpeed) * configuration.breatheAmount
        let jitter = state == .error ? sin(time * 47) * 1.8 : 0
        let center = CGPoint(
            x: size.width / 2 + jitter,
            y: size.height / 2 + sin(time * 0.72) * 0.7
        )
        let rotation = time * configuration.rotationSpeed
        let palette = configuration.palette

        graphics.blendMode = .plusLighter

        drawAmbientBloom(
            in: &graphics,
            center: center,
            radius: unit * 0.39 * entrance,
            palette: palette,
            intensity: configuration.glow * entrance
        )

        for index in 0..<configuration.ringCount {
            let fi = Double(index)
            let stagger = min(1, max(0, stateAge * 4.2 - fi * 0.12))
            let ringEntrance = easeOutBack(stagger)
            let pulse = ringPulse(index: index, time: time, configuration: configuration)
            let radius = unit
                * (configuration.baseRadius + CGFloat(index) * configuration.radiusStep)
                * CGFloat(breathe * pulse)
                * entrance
                * ringEntrance
            guard radius > 1 else { continue }

            let direction = index.isMultiple(of: 2) ? 1.0 : -0.72
            let start = rotation * direction + fi * configuration.phaseStep
            let gap = configuration.gap + sin(time * 0.8 + fi) * 0.08
            let sweep = max(0.7, Double.pi * 2 - gap - fi * 0.045)
            let lineWidth = configuration.lineWidth * (index == 0 ? 1.18 : 1)
            let opacity = max(0.16, 1 - fi * 0.105) * entrance

            var arc = Path()
            arc.addArc(
                center: center,
                radius: radius,
                startAngle: .radians(start),
                endAngle: .radians(start + sweep),
                clockwise: false
            )

            graphics.drawLayer { glow in
                glow.addFilter(.blur(radius: configuration.glowRadius))
                glow.stroke(
                    arc,
                    with: .color(palette.primary.opacity(opacity * configuration.glow)),
                    style: StrokeStyle(lineWidth: lineWidth * 3.4, lineCap: .round)
                )
            }

            graphics.stroke(
                arc,
                with: .linearGradient(
                    Gradient(colors: [
                        palette.primary.opacity(opacity * 0.35),
                        palette.primary.opacity(opacity),
                        .white.opacity(opacity * 0.92),
                        palette.secondary.opacity(opacity),
                        palette.primary.opacity(opacity * 0.2),
                    ]),
                    startPoint: CGPoint(x: center.x - radius, y: center.y - radius),
                    endPoint: CGPoint(x: center.x + radius, y: center.y + radius)
                ),
                style: StrokeStyle(lineWidth: lineWidth, lineCap: .round)
            )

            let sparkAngle = start + sweep
            let spark = CGPoint(
                x: center.x + cos(sparkAngle) * radius,
                y: center.y + sin(sparkAngle) * radius
            )
            let sparkRadius = max(0.8, lineWidth * 0.8)
            graphics.fill(
                Path(ellipseIn: CGRect(
                    x: spark.x - sparkRadius,
                    y: spark.y - sparkRadius,
                    width: sparkRadius * 2,
                    height: sparkRadius * 2
                )),
                with: .color(.white.opacity(opacity))
            )
        }

        drawCore(
            in: &graphics,
            center: center,
            time: time,
            entrance: entrance,
            configuration: configuration
        )

        if stateAge < 1.0 || state == .acting {
            drawParticles(
                in: &graphics,
                center: center,
                time: time,
                stateAge: stateAge,
                entrance: entrance,
                unit: unit,
                palette: palette
            )
        }
    }

    private func drawAmbientBloom(
        in graphics: inout GraphicsContext,
        center: CGPoint,
        radius: CGFloat,
        palette: RingPalette,
        intensity: Double
    ) {
        guard radius > 0 else { return }
        let rect = CGRect(
            x: center.x - radius,
            y: center.y - radius,
            width: radius * 2,
            height: radius * 2
        )
        graphics.fill(
            Path(ellipseIn: rect),
            with: .radialGradient(
                Gradient(colors: [
                    palette.primary.opacity(0.13 * intensity),
                    palette.secondary.opacity(0.05 * intensity),
                    .clear,
                ]),
                center: center,
                startRadius: 0,
                endRadius: radius
            )
        )
    }

    private func drawCore(
        in graphics: inout GraphicsContext,
        center: CGPoint,
        time: Double,
        entrance: Double,
        configuration: RingConfiguration
    ) {
        let pulse = 1 + sin(time * configuration.coreSpeed) * 0.13
        let radius = CGFloat(3.2 * pulse * entrance)
        let rect = CGRect(
            x: center.x - radius,
            y: center.y - radius,
            width: radius * 2,
            height: radius * 2
        )
        graphics.drawLayer { glow in
            glow.addFilter(.blur(radius: 6))
            glow.fill(
                Path(ellipseIn: rect.insetBy(dx: -4, dy: -4)),
                with: .color(configuration.palette.primary.opacity(0.65 * entrance))
            )
        }
        graphics.fill(
            Path(ellipseIn: rect),
            with: .radialGradient(
                Gradient(colors: [.white, configuration.palette.primary, .clear]),
                center: center,
                startRadius: 0,
                endRadius: radius
            )
        )
    }

    private func drawParticles(
        in graphics: inout GraphicsContext,
        center: CGPoint,
        time: Double,
        stateAge: Double,
        entrance: Double,
        unit: CGFloat,
        palette: RingPalette
    ) {
        let burstAge = state == .acting ? time.truncatingRemainder(dividingBy: 1.35) : stateAge
        let progress = min(1, max(0, burstAge / 0.9))
        let fade = sin(progress * .pi) * entrance
        guard fade > 0.01 else { return }

        for index in 0..<12 {
            let seed = Double(index) * 2.399963
            let distance = unit * CGFloat(0.12 + progress * (0.28 + Double(index % 4) * 0.025))
            let angle = seed + time * 0.12
            let point = CGPoint(
                x: center.x + cos(angle) * distance,
                y: center.y + sin(angle) * distance
            )
            let radius = CGFloat(0.65 + Double(index % 3) * 0.25)
            graphics.fill(
                Path(ellipseIn: CGRect(
                    x: point.x - radius,
                    y: point.y - radius,
                    width: radius * 2,
                    height: radius * 2
                )),
                with: .color((index.isMultiple(of: 2) ? palette.primary : palette.secondary).opacity(fade * 0.75))
            )
        }
    }

    private func ringPulse(index: Int, time: Double, configuration: RingConfiguration) -> Double {
        let phase = time * configuration.pulseSpeed - Double(index) * 0.68
        switch state {
        case .listening:
            return 1 + max(0, sin(phase)) * 0.038
        case .thinking:
            return 1 + sin(phase) * 0.012
        case .acting:
            return 1 + pow(max(0, sin(phase)), 5) * 0.07
        case .speaking:
            return 1 + (sin(phase) + sin(phase * 1.83) * 0.45) * 0.025
        case .error:
            return 1 + sin(phase * 3) * 0.025
        default:
            return 1 + sin(phase) * 0.008
        }
    }

    private func springEntrance(_ age: Double) -> Double {
        let progress = min(1, max(0, age / 0.68))
        return easeOutBack(progress)
    }

    private func easeOutBack(_ value: Double) -> Double {
        let c1 = 1.70158
        let c3 = c1 + 1
        let shifted = value - 1
        return 1 + c3 * pow(shifted, 3) + c1 * pow(shifted, 2)
    }
}

private struct RingPalette {
    let primary: Color
    let secondary: Color
}

private struct RingConfiguration {
    let ringCount: Int
    let baseRadius: CGFloat
    let radiusStep: CGFloat
    let lineWidth: CGFloat
    let rotationSpeed: Double
    let pulseSpeed: Double
    let breatheSpeed: Double
    let breatheAmount: Double
    let phaseStep: Double
    let gap: Double
    let glow: Double
    let glowRadius: CGFloat
    let coreSpeed: Double
    let palette: RingPalette

    static func forState(_ state: AssistantState) -> RingConfiguration {
        let cyan = Color(red: 0.16, green: 0.91, blue: 1)
        let violet = Color(red: 0.88, green: 0.26, blue: 1)
        let blue = Color(red: 0.22, green: 0.54, blue: 1)

        switch state {
        case .wakeDetected:
            return .init(ringCount: 6, baseRadius: 0.085, radiusStep: 0.052, lineWidth: 1.35, rotationSpeed: 0.7, pulseSpeed: 3, breatheSpeed: 2.4, breatheAmount: 0.018, phaseStep: 0.54, gap: 1.1, glow: 0.92, glowRadius: 6, coreSpeed: 6, palette: .init(primary: cyan, secondary: violet))
        case .listening:
            return .init(ringCount: 6, baseRadius: 0.09, radiusStep: 0.052, lineWidth: 1.35, rotationSpeed: 0.28, pulseSpeed: 4.8, breatheSpeed: 2.2, breatheAmount: 0.025, phaseStep: 0.63, gap: 1.18, glow: 0.82, glowRadius: 6, coreSpeed: 5.2, palette: .init(primary: cyan, secondary: blue))
        case .thinking:
            return .init(ringCount: 7, baseRadius: 0.075, radiusStep: 0.047, lineWidth: 1.18, rotationSpeed: 1.05, pulseSpeed: 2.1, breatheSpeed: 1.35, breatheAmount: 0.012, phaseStep: 0.92, gap: 1.5, glow: 0.88, glowRadius: 7, coreSpeed: 3.2, palette: .init(primary: violet, secondary: cyan))
        case .acting:
            return .init(ringCount: 6, baseRadius: 0.08, radiusStep: 0.052, lineWidth: 1.5, rotationSpeed: 1.8, pulseSpeed: 5.8, breatheSpeed: 3.8, breatheAmount: 0.016, phaseStep: 0.45, gap: 1.0, glow: 1, glowRadius: 7, coreSpeed: 7.5, palette: .init(primary: cyan, secondary: violet))
        case .speaking:
            return .init(ringCount: 6, baseRadius: 0.085, radiusStep: 0.053, lineWidth: 1.42, rotationSpeed: 0.46, pulseSpeed: 7.4, breatheSpeed: 3.1, breatheAmount: 0.022, phaseStep: 0.72, gap: 1.22, glow: 0.95, glowRadius: 7, coreSpeed: 8.2, palette: .init(primary: cyan, secondary: violet))
        case .followupWindow:
            return .init(ringCount: 5, baseRadius: 0.095, radiusStep: 0.056, lineWidth: 1.15, rotationSpeed: 0.18, pulseSpeed: 2, breatheSpeed: 1.25, breatheAmount: 0.018, phaseStep: 0.7, gap: 1.35, glow: 0.58, glowRadius: 6, coreSpeed: 2.4, palette: .init(primary: cyan, secondary: blue))
        case .error:
            return .init(ringCount: 6, baseRadius: 0.08, radiusStep: 0.052, lineWidth: 1.45, rotationSpeed: -0.8, pulseSpeed: 8, breatheSpeed: 5, breatheAmount: 0.015, phaseStep: 1.15, gap: 1.8, glow: 1, glowRadius: 8, coreSpeed: 9, palette: .init(primary: Color.red, secondary: Color(red: 1, green: 0.1, blue: 0.55)))
        case .disconnected, .sleeping:
            return .init(ringCount: 5, baseRadius: 0.09, radiusStep: 0.054, lineWidth: 1.1, rotationSpeed: 0.12, pulseSpeed: 1.4, breatheSpeed: 1, breatheAmount: 0.008, phaseStep: 0.68, gap: 1.5, glow: 0.35, glowRadius: 5, coreSpeed: 1.8, palette: .init(primary: cyan, secondary: blue))
        }
    }
}
