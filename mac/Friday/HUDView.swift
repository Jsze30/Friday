import AppKit
import SwiftUI

struct HUDView: View {
    @ObservedObject var model: AppState
    @State private var isPresented = false
    @State private var presentationBeganAt = Date()

    var body: some View {
        HStack(spacing: 12) {
            stateLabel

            MagicRingsOrb(
                state: model.state,
                presentationBeganAt: presentationBeganAt,
                isActive: model.shouldShowHUD
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
            presentationBeganAt = Date()
            setPresentation(model.shouldShowHUD)
        }
        .onChange(of: model.state) { oldState, _ in
            let shouldShow = model.shouldShowHUD
            if shouldShow, !Self.isHUDVisible(for: oldState) {
                presentationBeganAt = Date()
            }
            setPresentation(shouldShow)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Friday. \(stateTitle)")
    }

    private var stateLabel: some View {
        Text(stateTitle.uppercased())
            .font(.system(size: 10, weight: .bold, design: .rounded))
            .tracking(2)
            .foregroundStyle(stateAccent.opacity(0.92))
            .shadow(color: .black.opacity(0.95), radius: 5, y: 1)
            .shadow(color: stateAccent.opacity(0.24), radius: 10)
            .contentTransition(.opacity)
            .frame(width: 110, alignment: .trailing)
            .animation(.easeInOut(duration: 0.32), value: model.state)
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

    private var stateAccent: Color {
        model.state == .error
            ? Color(red: 1, green: 0.25, blue: 0.38)
            : Color(red: 0.25, green: 0.92, blue: 1)
    }

    private func setPresentation(_ presented: Bool) {
        if presented {
            withAnimation(.spring(response: 0.52, dampingFraction: 0.72)) {
                isPresented = true
            }
        } else {
            withAnimation(.easeInOut(duration: 0.48).delay(0.06)) {
                isPresented = false
            }
        }
    }

    private static func isHUDVisible(for state: AssistantState) -> Bool {
        switch state {
        case .disconnected, .sleeping:
            false
        default:
            true
        }
    }
}

private struct MagicRingsOrb: View {
    let state: AssistantState
    let presentationBeganAt: Date
    let isActive: Bool
    @State private var motion: RingMotion

    init(state: AssistantState, presentationBeganAt: Date, isActive: Bool) {
        self.state = state
        self.presentationBeganAt = presentationBeganAt
        self.isActive = isActive
        _motion = State(initialValue: RingMotion(state: state))
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60.0, paused: !isActive)) { context in
            Canvas(opaque: false, colorMode: .extendedLinear, rendersAsynchronously: true) { graphics, size in
                let sample = motion.sample(at: context.date.timeIntervalSinceReferenceDate)
                drawRings(
                    in: &graphics,
                    size: size,
                    time: context.date.timeIntervalSinceReferenceDate,
                    presentationAge: max(0, context.date.timeIntervalSince(presentationBeganAt)),
                    sample: sample
                )
            }
        }
        .onChange(of: state) { _, newState in
            motion.transition(
                to: newState,
                at: Date().timeIntervalSinceReferenceDate
            )
        }
    }

    private func drawRings(
        in graphics: inout GraphicsContext,
        size: CGSize,
        time: Double,
        presentationAge: Double,
        sample: RingMotion.Sample
    ) {
        let configuration = sample.configuration
        let unit = min(size.width, size.height)
        let entrance = springEntrance(presentationAge)
        let breathe = 1 + sin(sample.breathePhase) * configuration.breatheAmount
        let jitter = sin(time * 47) * configuration.jitterAmount
        let center = CGPoint(
            x: size.width / 2 + jitter,
            y: size.height / 2 + sin(time * 0.72) * 0.7
        )
        let rotation = sample.rotation
        let palette = configuration.palette

        graphics.blendMode = .plusLighter

        drawAmbientBloom(
            in: &graphics,
            center: center,
            radius: unit * 0.39 * entrance,
            palette: palette,
            intensity: configuration.glow * entrance
        )

        for index in 0..<RingConfiguration.maximumRingCount {
            let fi = Double(index)
            let ringVisibility = min(1, max(0, configuration.ringCount - fi))
            guard ringVisibility > 0.001 else { continue }
            let stagger = min(1, max(0, presentationAge * 4.2 - fi * 0.12))
            let ringEntrance = easeOutBack(stagger)
            let pulse = ringPulse(
                index: index,
                pulsePhase: sample.pulsePhase,
                configuration: configuration
            )
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
            let opacity = max(0.16, 1 - fi * 0.105) * entrance * ringVisibility

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
                    with: .color(palette.primary.color.opacity(opacity * configuration.glow)),
                    style: StrokeStyle(lineWidth: lineWidth * 3.4, lineCap: .round)
                )
            }

            graphics.stroke(
                arc,
                with: .linearGradient(
                    Gradient(colors: [
                        palette.primary.color.opacity(opacity * 0.35),
                        palette.primary.color.opacity(opacity),
                        .white.opacity(opacity * 0.92),
                        palette.secondary.color.opacity(opacity),
                        palette.primary.color.opacity(opacity * 0.2),
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
            corePhase: sample.corePhase,
            entrance: entrance,
            configuration: configuration
        )

        if presentationAge < 1.0 || configuration.particleIntensity > 0.001 {
            drawParticles(
                in: &graphics,
                center: center,
                time: time,
                presentationAge: presentationAge,
                entrance: entrance,
                unit: unit,
                palette: palette,
                actionIntensity: configuration.particleIntensity
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
                    palette.primary.color.opacity(0.13 * intensity),
                    palette.secondary.color.opacity(0.05 * intensity),
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
        corePhase: Double,
        entrance: Double,
        configuration: RingConfiguration
    ) {
        let pulse = 1 + sin(corePhase) * 0.13
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
                with: .color(configuration.palette.primary.color.opacity(0.65 * entrance))
            )
        }
        graphics.fill(
            Path(ellipseIn: rect),
            with: .radialGradient(
                Gradient(colors: [.white, configuration.palette.primary.color, .clear]),
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
        presentationAge: Double,
        entrance: Double,
        unit: CGFloat,
        palette: RingPalette,
        actionIntensity: Double
    ) {
        let entranceProgress = min(1, max(0, presentationAge / 0.9))
        let entranceFade = presentationAge < 1 ? sin(entranceProgress * .pi) : 0
        let actionAge = time.truncatingRemainder(dividingBy: 1.35)
        let actionProgress = min(1, max(0, actionAge / 0.9))
        let actionFade = sin(actionProgress * .pi) * actionIntensity
        let fade = max(entranceFade, actionFade) * entrance
        let burstProgress = max(entranceProgress, actionProgress * actionIntensity)
        guard fade > 0.01 else { return }

        for index in 0..<12 {
            let seed = Double(index) * 2.399963
            let distance = unit * CGFloat(
                0.12 + burstProgress * (0.28 + Double(index % 4) * 0.025)
            )
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
                with: .color(
                    (index.isMultiple(of: 2) ? palette.primary.color : palette.secondary.color)
                        .opacity(fade * 0.75)
                )
            )
        }
    }

    private func ringPulse(
        index: Int,
        pulsePhase: Double,
        configuration: RingConfiguration
    ) -> Double {
        let phase = pulsePhase - Double(index) * 0.68
        let wave = sin(phase) * configuration.pulseSineAmount
        let lift = max(0, sin(phase)) * configuration.pulseLiftAmount
        let impact = pow(max(0, sin(phase)), 5) * configuration.pulseImpactAmount
        let harmonic = sin(phase * 1.83) * configuration.pulseHarmonicAmount
        let alert = sin(phase * 3) * configuration.pulseAlertAmount
        return 1 + wave + lift + impact + harmonic + alert
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

private struct RingMotion {
    struct Sample {
        let configuration: RingConfiguration
        let rotation: Double
        let pulsePhase: Double
        let breathePhase: Double
        let corePhase: Double
    }

    private static let transitionDuration = 0.72

    private var from: RingConfiguration
    private var to: RingConfiguration
    private var startedAt: Double
    private var rotationBase = 0.0
    private var pulseBase = 0.0
    private var breatheBase = 0.0
    private var coreBase = 0.0

    init(state: AssistantState) {
        let configuration = RingConfiguration.forState(state)
        from = configuration
        to = configuration
        startedAt = Date().timeIntervalSinceReferenceDate
    }

    mutating func transition(to state: AssistantState, at time: Double) {
        let current = sample(at: time)
        from = current.configuration
        to = RingConfiguration.forState(state)
        startedAt = time
        rotationBase = current.rotation
        pulseBase = current.pulsePhase
        breatheBase = current.breathePhase
        coreBase = current.corePhase
    }

    func sample(at time: Double) -> Sample {
        let elapsed = max(0, time - startedAt)
        let linearProgress = min(1, elapsed / Self.transitionDuration)
        let progress = smootherStep(linearProgress)
        return Sample(
            configuration: from.interpolated(to: to, progress: progress),
            rotation: rotationBase + integratedPhase(
                elapsed: elapsed,
                fromSpeed: from.rotationSpeed,
                toSpeed: to.rotationSpeed
            ),
            pulsePhase: pulseBase + integratedPhase(
                elapsed: elapsed,
                fromSpeed: from.pulseSpeed,
                toSpeed: to.pulseSpeed
            ),
            breathePhase: breatheBase + integratedPhase(
                elapsed: elapsed,
                fromSpeed: from.breatheSpeed,
                toSpeed: to.breatheSpeed
            ),
            corePhase: coreBase + integratedPhase(
                elapsed: elapsed,
                fromSpeed: from.coreSpeed,
                toSpeed: to.coreSpeed
            )
        )
    }

    private func smootherStep(_ value: Double) -> Double {
        value * value * value * (value * (value * 6 - 15) + 10)
    }

    private func integratedPhase(
        elapsed: Double,
        fromSpeed: Double,
        toSpeed: Double
    ) -> Double {
        let duration = Self.transitionDuration
        if elapsed >= duration {
            let transitionDistance = duration * (fromSpeed + toSpeed) / 2
            return transitionDistance + (elapsed - duration) * toSpeed
        }

        let progress = max(0, elapsed / duration)
        let smoothedIntegral = pow(progress, 6)
            - 3 * pow(progress, 5)
            + 2.5 * pow(progress, 4)
        return duration * (
            fromSpeed * progress
                + (toSpeed - fromSpeed) * smoothedIntegral
        )
    }
}

private struct RingColor {
    let red: Double
    let green: Double
    let blue: Double

    var color: Color {
        Color(red: red, green: green, blue: blue)
    }

    func interpolated(to other: RingColor, progress: Double) -> RingColor {
        RingColor(
            red: mix(red, other.red, progress),
            green: mix(green, other.green, progress),
            blue: mix(blue, other.blue, progress)
        )
    }
}

private struct RingPalette {
    let primary: RingColor
    let secondary: RingColor

    func interpolated(to other: RingPalette, progress: Double) -> RingPalette {
        RingPalette(
            primary: primary.interpolated(to: other.primary, progress: progress),
            secondary: secondary.interpolated(to: other.secondary, progress: progress)
        )
    }
}

private struct RingConfiguration {
    static let maximumRingCount = 7

    let ringCount: Double
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
    let pulseSineAmount: Double
    let pulseLiftAmount: Double
    let pulseImpactAmount: Double
    let pulseHarmonicAmount: Double
    let pulseAlertAmount: Double
    let jitterAmount: Double
    let particleIntensity: Double
    let palette: RingPalette

    init(
        ringCount: Double,
        baseRadius: CGFloat,
        radiusStep: CGFloat,
        lineWidth: CGFloat,
        rotationSpeed: Double,
        pulseSpeed: Double,
        breatheSpeed: Double,
        breatheAmount: Double,
        phaseStep: Double,
        gap: Double,
        glow: Double,
        glowRadius: CGFloat,
        coreSpeed: Double,
        pulseSineAmount: Double,
        pulseLiftAmount: Double = 0,
        pulseImpactAmount: Double = 0,
        pulseHarmonicAmount: Double = 0,
        pulseAlertAmount: Double = 0,
        jitterAmount: Double = 0,
        particleIntensity: Double = 0,
        palette: RingPalette
    ) {
        self.ringCount = ringCount
        self.baseRadius = baseRadius
        self.radiusStep = radiusStep
        self.lineWidth = lineWidth
        self.rotationSpeed = rotationSpeed
        self.pulseSpeed = pulseSpeed
        self.breatheSpeed = breatheSpeed
        self.breatheAmount = breatheAmount
        self.phaseStep = phaseStep
        self.gap = gap
        self.glow = glow
        self.glowRadius = glowRadius
        self.coreSpeed = coreSpeed
        self.pulseSineAmount = pulseSineAmount
        self.pulseLiftAmount = pulseLiftAmount
        self.pulseImpactAmount = pulseImpactAmount
        self.pulseHarmonicAmount = pulseHarmonicAmount
        self.pulseAlertAmount = pulseAlertAmount
        self.jitterAmount = jitterAmount
        self.particleIntensity = particleIntensity
        self.palette = palette
    }

    func interpolated(to other: RingConfiguration, progress: Double) -> RingConfiguration {
        RingConfiguration(
            ringCount: mix(ringCount, other.ringCount, progress),
            baseRadius: mix(baseRadius, other.baseRadius, progress),
            radiusStep: mix(radiusStep, other.radiusStep, progress),
            lineWidth: mix(lineWidth, other.lineWidth, progress),
            rotationSpeed: mix(rotationSpeed, other.rotationSpeed, progress),
            pulseSpeed: mix(pulseSpeed, other.pulseSpeed, progress),
            breatheSpeed: mix(breatheSpeed, other.breatheSpeed, progress),
            breatheAmount: mix(breatheAmount, other.breatheAmount, progress),
            phaseStep: mix(phaseStep, other.phaseStep, progress),
            gap: mix(gap, other.gap, progress),
            glow: mix(glow, other.glow, progress),
            glowRadius: mix(glowRadius, other.glowRadius, progress),
            coreSpeed: mix(coreSpeed, other.coreSpeed, progress),
            pulseSineAmount: mix(pulseSineAmount, other.pulseSineAmount, progress),
            pulseLiftAmount: mix(pulseLiftAmount, other.pulseLiftAmount, progress),
            pulseImpactAmount: mix(pulseImpactAmount, other.pulseImpactAmount, progress),
            pulseHarmonicAmount: mix(pulseHarmonicAmount, other.pulseHarmonicAmount, progress),
            pulseAlertAmount: mix(pulseAlertAmount, other.pulseAlertAmount, progress),
            jitterAmount: mix(jitterAmount, other.jitterAmount, progress),
            particleIntensity: mix(particleIntensity, other.particleIntensity, progress),
            palette: palette.interpolated(to: other.palette, progress: progress)
        )
    }

    static func forState(_ state: AssistantState) -> RingConfiguration {
        let cyan = RingColor(red: 0.16, green: 0.91, blue: 1)
        let violet = RingColor(red: 0.88, green: 0.26, blue: 1)
        let blue = RingColor(red: 0.22, green: 0.54, blue: 1)

        switch state {
        case .wakeDetected:
            return .init(ringCount: 6, baseRadius: 0.085, radiusStep: 0.052, lineWidth: 1.35, rotationSpeed: 0.7, pulseSpeed: 3, breatheSpeed: 2.4, breatheAmount: 0.018, phaseStep: 0.54, gap: 1.1, glow: 0.92, glowRadius: 6, coreSpeed: 6, pulseSineAmount: 0.008, palette: .init(primary: cyan, secondary: violet))
        case .listening:
            return .init(ringCount: 6, baseRadius: 0.09, radiusStep: 0.052, lineWidth: 1.35, rotationSpeed: 0.28, pulseSpeed: 4.8, breatheSpeed: 2.2, breatheAmount: 0.025, phaseStep: 0.63, gap: 1.18, glow: 0.82, glowRadius: 6, coreSpeed: 5.2, pulseSineAmount: 0, pulseLiftAmount: 0.038, palette: .init(primary: cyan, secondary: blue))
        case .thinking:
            return .init(ringCount: 7, baseRadius: 0.075, radiusStep: 0.047, lineWidth: 1.18, rotationSpeed: 1.05, pulseSpeed: 2.1, breatheSpeed: 1.35, breatheAmount: 0.012, phaseStep: 0.92, gap: 1.5, glow: 0.88, glowRadius: 7, coreSpeed: 3.2, pulseSineAmount: 0.012, palette: .init(primary: violet, secondary: cyan))
        case .acting:
            return .init(ringCount: 6, baseRadius: 0.08, radiusStep: 0.052, lineWidth: 1.5, rotationSpeed: 1.8, pulseSpeed: 5.8, breatheSpeed: 3.8, breatheAmount: 0.016, phaseStep: 0.45, gap: 1, glow: 1, glowRadius: 7, coreSpeed: 7.5, pulseSineAmount: 0, pulseImpactAmount: 0.07, particleIntensity: 1, palette: .init(primary: cyan, secondary: violet))
        case .speaking:
            return .init(ringCount: 6, baseRadius: 0.085, radiusStep: 0.053, lineWidth: 1.42, rotationSpeed: 0.46, pulseSpeed: 7.4, breatheSpeed: 3.1, breatheAmount: 0.022, phaseStep: 0.72, gap: 1.22, glow: 0.95, glowRadius: 7, coreSpeed: 8.2, pulseSineAmount: 0.025, pulseHarmonicAmount: 0.01125, palette: .init(primary: cyan, secondary: violet))
        case .followupWindow:
            return .init(ringCount: 5, baseRadius: 0.095, radiusStep: 0.056, lineWidth: 1.15, rotationSpeed: 0.18, pulseSpeed: 2, breatheSpeed: 1.25, breatheAmount: 0.018, phaseStep: 0.7, gap: 1.35, glow: 0.58, glowRadius: 6, coreSpeed: 2.4, pulseSineAmount: 0.008, palette: .init(primary: cyan, secondary: blue))
        case .error:
            let red = RingColor(red: 1, green: 0, blue: 0)
            let pink = RingColor(red: 1, green: 0.1, blue: 0.55)
            return .init(ringCount: 6, baseRadius: 0.08, radiusStep: 0.052, lineWidth: 1.45, rotationSpeed: -0.8, pulseSpeed: 8, breatheSpeed: 5, breatheAmount: 0.015, phaseStep: 1.15, gap: 1.8, glow: 1, glowRadius: 8, coreSpeed: 9, pulseSineAmount: 0, pulseAlertAmount: 0.025, jitterAmount: 1.8, palette: .init(primary: red, secondary: pink))
        case .disconnected, .sleeping:
            return .init(ringCount: 5, baseRadius: 0.09, radiusStep: 0.054, lineWidth: 1.1, rotationSpeed: 0.12, pulseSpeed: 1.4, breatheSpeed: 1, breatheAmount: 0.008, phaseStep: 0.68, gap: 1.5, glow: 0.35, glowRadius: 5, coreSpeed: 1.8, pulseSineAmount: 0.008, palette: .init(primary: cyan, secondary: blue))
        }
    }
}

private func mix(_ from: Double, _ to: Double, _ progress: Double) -> Double {
    from + (to - from) * progress
}

private func mix(_ from: CGFloat, _ to: CGFloat, _ progress: Double) -> CGFloat {
    from + (to - from) * CGFloat(progress)
}
