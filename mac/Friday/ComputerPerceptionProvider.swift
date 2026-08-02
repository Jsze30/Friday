import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ScreenCaptureKit
import Vision

private func perceptionAXObserverCallback(
    _ observer: AXObserver,
    _ element: AXUIElement,
    _ notification: CFString,
    _ refcon: UnsafeMutableRawPointer?
) {
    guard let refcon else { return }
    let provider = Unmanaged<ComputerPerceptionProvider>
        .fromOpaque(refcon)
        .takeUnretainedValue()
    let reason = notification as String
    Task { @MainActor in
        provider.handleAccessibilityEvent(reason)
    }
}

@MainActor
final class ComputerPerceptionProvider {
    static let shared = ComputerPerceptionProvider()

    private struct OCRRegion: Sendable {
        let text: String
        let boundingBox: CGRect
        let confidence: Float
    }

    private struct OCRSnapshot: Sendable {
        let text: String
        let regions: [OCRRegion]
    }

    private static let maximumImageWidth: CGFloat = 1_200
    private static let imageMaxAge: TimeInterval = 30
    private static let minimumBackgroundCaptureInterval: TimeInterval = 2
    private static let sensitiveBundleIDs: Set<String> = [
        "com.1password.1password",
        "com.agilebits.onepassword7",
        "com.apple.keychainaccess",
        "com.bitwarden.desktop",
        "com.lastpass.LastPass",
    ]
    private static let visualTerms = [
        "look at",
        "looking at",
        "what do you see",
        "what am i seeing",
        "on my screen",
        "on the screen",
        "this screen",
        "this image",
        "this picture",
        "this design",
        "this layout",
        "this chart",
        "this graph",
        "this icon",
        "this button",
        "screenshot",
        "visually",
        "visible here",
        "what is wrong here",
        "what's wrong here",
        "does this look",
        "which button",
        "where on the screen",
    ]

    private var workspaceObserver: NSObjectProtocol?
    private var accessibilityObserver: AXObserver?
    private var observedApplication: AXUIElement?
    private var observedPID: pid_t?
    private var refreshTask: Task<Void, Never>?
    private var pendingCapture = false
    private var latestContext: [String: Any] = [
        "status": "starting",
        "capturedAt": ISO8601DateFormatter().string(from: Date()),
    ]
    private var latestImage: Data?
    private var latestImageCapturedAt: Date?
    private var latestImageProcessID: pid_t?
    private var latestImageFingerprint: Int?
    private var latestPerceptualFingerprint: String?
    private var latestWindowFrame: CGRect?
    private var latestOCRText = ""
    private var latestOCRRegions: [OCRRegion] = []
    private var actionSnapshots: [String: Data] = [:]
    private var latestRefreshAt = Date.distantPast
    private var isStarted = false

    private init() {}

    func start() {
        guard !isStarted else { return }
        isStarted = true
        workspaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.attachAccessibilityObserver()
                self?.scheduleRefresh(reason: "application_activated", capture: true)
            }
        }
        attachAccessibilityObserver()
        requestScreenCapturePermissionIfNeeded()
        scheduleRefresh(reason: "startup", capture: true)
    }

    func stop() {
        refreshTask?.cancel()
        refreshTask = nil
        if let workspaceObserver {
            NSWorkspace.shared.notificationCenter.removeObserver(workspaceObserver)
        }
        workspaceObserver = nil
        detachAccessibilityObserver()
        latestImage = nil
        latestImageCapturedAt = nil
        latestImageProcessID = nil
        latestImageFingerprint = nil
        latestPerceptualFingerprint = nil
        latestWindowFrame = nil
        latestOCRText = ""
        latestOCRRegions = []
        actionSnapshots.removeAll()
        isStarted = false
    }

    func cachedContext() -> [String: Any] {
        var context = latestContext
        if let capturedAt = latestImageCapturedAt {
            context["imageAgeMs"] = max(0, Date().timeIntervalSince(capturedAt) * 1_000)
        }
        return context
    }

    func screenState() async -> [String: Any] {
        await refresh(reason: "computer_control", capture: true)
        let context = cachedContext()
        guard let app = NSWorkspace.shared.frontmostApplication,
              latestImageProcessID == app.processIdentifier,
              let fingerprint = latestPerceptualFingerprint else {
            return [
                "available": false,
                "error": "no current active-window image is available",
            ]
        }
        var state: [String: Any] = [
            "available": true,
            "app": app.localizedName ?? app.bundleIdentifier ?? "Unknown",
            "bundleId": app.bundleIdentifier ?? "",
            "processId": Int(app.processIdentifier),
            "visualFingerprint": fingerprint,
            "ocrText": String(latestOCRText.prefix(2_500)),
            "capturedAt": ISO8601DateFormatter().string(
                from: latestImageCapturedAt ?? Date()
            ),
        ]
        if let windowID = context["windowId"] {
            state["windowId"] = windowID
        }
        if let currentWindow = context["currentWindow"] {
            state["windowTitle"] = currentWindow
        }
        if let latestWindowFrame {
            state["windowFrame"] = [
                "x": latestWindowFrame.minX,
                "y": latestWindowFrame.minY,
                "width": latestWindowFrame.width,
                "height": latestWindowFrame.height,
            ]
        }
        return state
    }

    func contextForTurn(
        query: String,
        client: LocalServiceClient
    ) async -> [String: Any] {
        let needsPixels = Self.requiresVisualAnalysis(query)
        let currentProcessID = NSWorkspace.shared.frontmostApplication?
            .processIdentifier
        let imageMatchesCurrentApplication = currentProcessID != nil
            && currentProcessID == latestImageProcessID
        let imageIsStale = (latestImageCapturedAt.map {
            Date().timeIntervalSince($0) > Self.imageMaxAge
        } ?? true) || !imageMatchesCurrentApplication
        let contextIsStale = Date().timeIntervalSince(latestRefreshAt) > 1.5
        if needsPixels || contextIsStale {
            await refresh(
                reason: "voice_turn",
                capture: needsPixels && imageIsStale
            )
        }

        var context = cachedContext()
        context["visualAnalysisRequested"] = needsPixels
        guard needsPixels else { return context }

        guard let bundleID = (
            context["application"] as? [String: Any]
        )?["bundleId"] as? String,
              !Self.sensitiveBundleIDs.contains(bundleID) else {
            context["visualAnalysis"] = [
                "ok": false,
                "available": false,
                "error": "visual analysis is disabled for security-sensitive applications",
            ]
            return context
        }
        guard let latestImage,
              latestImageProcessID == currentProcessID else {
            context["visualAnalysis"] = [
                "ok": false,
                "available": false,
                "error": "no active-window image is available",
            ]
            return context
        }

        do {
            let result = try await client.analyzeVisualContext(
                query: query,
                imageData: latestImage,
                mimeType: "image/jpeg",
                ocrText: latestOCRText,
                metadata: Self.visualMetadata(from: context)
            )
            context["visualAnalysis"] = result
        } catch {
            context["visualAnalysis"] = [
                "ok": false,
                "available": false,
                "error": "visual analysis could not be completed",
            ]
        }
        return context
    }

    func locateControl(
        target: String,
        client: LocalServiceClient
    ) async -> [String: Any] {
        let cleanedTarget = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanedTarget.isEmpty else {
            return ["found": false, "error": "target is required"]
        }
        await refresh(reason: "voice_turn", capture: true)
        let context = cachedContext()
        guard let bundleID = (
            context["application"] as? [String: Any]
        )?["bundleId"] as? String,
              !Self.sensitiveBundleIDs.contains(bundleID) else {
            return [
                "found": false,
                "error": "visual location is disabled for security-sensitive applications",
            ]
        }
        guard let latestImage,
              let latestWindowFrame,
              latestImageProcessID == NSWorkspace.shared.frontmostApplication?
                .processIdentifier else {
            return [
                "found": false,
                "error": "no current active-window image is available",
            ]
        }
        let visualToken = UUID().uuidString.lowercased()
        actionSnapshots[visualToken] = latestImage
        while actionSnapshots.count > 4, let oldest = actionSnapshots.keys.first {
            actionSnapshots.removeValue(forKey: oldest)
        }
        if let local = localOCRLocation(for: cleanedTarget) {
            let normalizedX = local.region.boundingBox.midX
            let normalizedY = 1 - local.region.boundingBox.midY
            return locatedResult(
                base: [
                    "found": true,
                    "confidence": local.score,
                    "description": local.region.text,
                    "method": "local_ocr",
                ],
                normalizedX: normalizedX,
                normalizedY: normalizedY,
                frame: latestWindowFrame,
                context: context,
                visualToken: visualToken
            )
        }
        do {
            let result = try await client.locateVisualControl(
                target: cleanedTarget,
                imageData: latestImage,
                mimeType: "image/jpeg",
                ocrText: latestOCRText,
                metadata: Self.visualMetadata(from: context)
            )
            guard result["found"] as? Bool == true,
                  let normalizedX = Self.number(result["x"]),
                  let normalizedY = Self.number(result["y"]),
                  (0...1).contains(normalizedX),
                  (0...1).contains(normalizedY) else {
                return result
            }
            return locatedResult(
                base: result.merging(["method": "visual_model"]) { current, _ in current },
                normalizedX: normalizedX,
                normalizedY: normalizedY,
                frame: latestWindowFrame,
                context: context,
                visualToken: visualToken
            )
        } catch {
            return [
                "found": false,
                "error": "visual location could not be completed",
            ]
        }
    }

    func verifyAction(
        target: String,
        visualToken: String,
        client: LocalServiceClient
    ) async -> [String: Any] {
        guard let beforeImage = actionSnapshots.removeValue(forKey: visualToken) else {
            return [
                "succeeded": false,
                "error": "visual action snapshot expired",
            ]
        }
        await refresh(reason: "voice_turn", capture: true)
        guard let afterImage = latestImage,
              latestImageProcessID == NSWorkspace.shared.frontmostApplication?
                .processIdentifier else {
            return [
                "succeeded": false,
                "error": "no post-action window image is available",
            ]
        }
        do {
            return try await client.verifyVisualAction(
                target: target,
                beforeImageData: beforeImage,
                afterImageData: afterImage,
                mimeType: "image/jpeg"
            )
        } catch {
            return [
                "succeeded": false,
                "error": "visual action verification could not be completed",
            ]
        }
    }

    func handleAccessibilityEvent(_ notification: String) {
        let capturesWindow = notification == kAXFocusedWindowChangedNotification
        scheduleRefresh(
            reason: notification.replacingOccurrences(of: "AX", with: ""),
            capture: capturesWindow
        )
    }

    private func requestScreenCapturePermissionIfNeeded() {
        guard !CGPreflightScreenCaptureAccess() else { return }
        _ = CGRequestScreenCaptureAccess()
    }

    private func scheduleRefresh(reason: String, capture: Bool) {
        pendingCapture = pendingCapture || capture
        refreshTask?.cancel()
        refreshTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(for: .milliseconds(220))
            } catch {
                return
            }
            guard let self else { return }
            let shouldCapture = self.pendingCapture
            self.pendingCapture = false
            await self.refresh(reason: reason, capture: shouldCapture)
        }
    }

    private func refresh(reason: String, capture: Bool) async {
        defer {
            latestRefreshAt = Date()
            LatencyTraceStore.shared.recordPerception(latestContext)
        }
        guard let app = NSWorkspace.shared.frontmostApplication else {
            latestContext = [
                "status": "unavailable",
                "reason": "no_frontmost_application",
                "capturedAt": ISO8601DateFormatter().string(from: Date()),
            ]
            return
        }

        let appName = app.localizedName ?? app.bundleIdentifier ?? "Unknown"
        let bundleID = app.bundleIdentifier ?? ""
        let basic = WorkingContextProvider.shared.basicSnapshot(
            includePerception: false
        )
        let semanticStarted = Date()
        let semantic = MacPrimitiveProvider.shared.semanticContext(maxElements: 80)
        let semanticLatency = Date().timeIntervalSince(semanticStarted) * 1_000
        var context: [String: Any] = [
            "status": "available",
            "capturedAt": ISO8601DateFormatter().string(from: Date()),
            "trigger": reason,
            "application": [
                "name": appName,
                "bundleId": bundleID,
                "processId": Int(app.processIdentifier),
            ],
            "semantic": semantic,
            "semanticLatencyMs": semanticLatency,
            "screenCapturePermission": CGPreflightScreenCaptureAccess(),
            "lowPowerMode": ProcessInfo.processInfo.isLowPowerModeEnabled,
            "thermalState": Self.thermalStateName(
                ProcessInfo.processInfo.thermalState
            ),
        ]
        for key in ["currentWindow", "currentDocument", "currentURL"] {
            if let value = basic[key] {
                context[key] = value
            }
        }

        let isSensitive = Self.sensitiveBundleIDs.contains(bundleID)
        if isSensitive {
            latestImage = nil
            latestImageCapturedAt = nil
            latestImageProcessID = nil
            latestImageFingerprint = nil
            latestPerceptualFingerprint = nil
            latestWindowFrame = nil
            latestOCRText = ""
            latestOCRRegions = []
            context["captureStatus"] = "blocked_sensitive_application"
            context["ocrText"] = ""
            latestContext = context
            return
        }

        let resourceConstrained = ProcessInfo.processInfo.isLowPowerModeEnabled
            || ProcessInfo.processInfo.thermalState == .serious
            || ProcessInfo.processInfo.thermalState == .critical
        let capturedCurrentApplicationRecently = latestImageProcessID
            == app.processIdentifier
            && latestImageCapturedAt.map {
                Date().timeIntervalSince($0)
                    < Self.minimumBackgroundCaptureInterval
            } == true
        let shouldCapture = capture
            && (!resourceConstrained || reason == "voice_turn")
            && (
                reason == "voice_turn"
                    || reason == "computer_control"
                    || !capturedCurrentApplicationRecently
            )
        guard shouldCapture, CGPreflightScreenCaptureAccess() else {
            if capture, !CGPreflightScreenCaptureAccess() {
                context["captureStatus"] = "permission_required"
            } else if capture, resourceConstrained, reason != "voice_turn" {
                context["captureStatus"] = "deferred_for_resource_pressure"
            } else if capture, capturedCurrentApplicationRecently {
                context["captureStatus"] = "coalesced_recent_capture"
            } else {
                context["captureStatus"] = "cached_or_not_requested"
            }
            context["ocrText"] = String(latestOCRText.prefix(2_500))
            context["imageAvailable"] = latestImage != nil
                && latestImageProcessID == app.processIdentifier
            latestContext = context
            return
        }

        let captureStarted = Date()
        do {
            let captured = try await captureActiveWindow(
                processID: app.processIdentifier,
                preferredTitle: basic["currentWindow"] as? String
            )
            let jpeg = try Self.jpegData(from: captured.image)
            let fingerprint = jpeg.hashValue
            let perceptualFingerprint = Self.perceptualFingerprint(from: captured.image)
            let changed = fingerprint != latestImageFingerprint
            var ocrLatency = 0.0
            if changed {
                let ocrStarted = Date()
                let recognized = try await Self.recognizeText(in: captured.image)
                latestOCRText = recognized.text
                latestOCRRegions = recognized.regions
                ocrLatency = Date().timeIntervalSince(ocrStarted) * 1_000
                latestImage = jpeg
                latestImageFingerprint = fingerprint
            }
            latestImageCapturedAt = Date()
            latestImageProcessID = app.processIdentifier
            latestWindowFrame = captured.frame
            latestPerceptualFingerprint = perceptualFingerprint
            context["captureStatus"] = "captured"
            context["imageAvailable"] = true
            context["imageChanged"] = changed
            context["windowId"] = captured.windowID
            context["imageWidth"] = captured.image.width
            context["imageHeight"] = captured.image.height
            context["captureLatencyMs"] = Date().timeIntervalSince(captureStarted) * 1_000
            context["ocrLatencyMs"] = ocrLatency
            context["ocrText"] = String(latestOCRText.prefix(2_500))
            let totalCaptureMs = Date().timeIntervalSince(captureStarted) * 1_000
            NSLog(
                "[Friday/perception] captured app=\(appName) "
                    + "changed=\(changed) captureMs=\(Int(totalCaptureMs)) "
                    + "ocrMs=\(Int(ocrLatency)) bytes=\(jpeg.count)"
            )
        } catch {
            context["captureStatus"] = "failed"
            context["captureError"] = error.localizedDescription
            if latestImageProcessID != app.processIdentifier {
                latestImage = nil
                latestImageCapturedAt = nil
                latestImageProcessID = nil
                latestImageFingerprint = nil
                latestPerceptualFingerprint = nil
                latestWindowFrame = nil
                latestOCRText = ""
                latestOCRRegions = []
            }
            context["imageAvailable"] = latestImage != nil
            context["ocrText"] = String(latestOCRText.prefix(2_500))
            NSLog("[Friday/perception] active-window capture failed: \(error)")
        }
        latestContext = context
    }

    private func attachAccessibilityObserver() {
        detachAccessibilityObserver()
        guard AXIsProcessTrusted(),
              let app = NSWorkspace.shared.frontmostApplication else { return }
        var observer: AXObserver?
        guard AXObserverCreate(
            app.processIdentifier,
            perceptionAXObserverCallback,
            &observer
        ) == .success,
              let observer else { return }
        let application = AXUIElementCreateApplication(app.processIdentifier)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        for notification in [
            kAXFocusedWindowChangedNotification,
            kAXFocusedUIElementChangedNotification,
        ] {
            _ = AXObserverAddNotification(
                observer,
                application,
                notification as CFString,
                refcon
            )
        }
        CFRunLoopAddSource(
            CFRunLoopGetMain(),
            AXObserverGetRunLoopSource(observer),
            .defaultMode
        )
        accessibilityObserver = observer
        observedApplication = application
        observedPID = app.processIdentifier
    }

    private func detachAccessibilityObserver() {
        if let accessibilityObserver {
            CFRunLoopRemoveSource(
                CFRunLoopGetMain(),
                AXObserverGetRunLoopSource(accessibilityObserver),
                .defaultMode
            )
        }
        accessibilityObserver = nil
        observedApplication = nil
        observedPID = nil
    }

    private struct CapturedWindow {
        let image: CGImage
        let windowID: UInt32
        let frame: CGRect
    }

    private func captureActiveWindow(
        processID: pid_t,
        preferredTitle: String?
    ) async throws -> CapturedWindow {
        let content = try await SCShareableContent.excludingDesktopWindows(
            true,
            onScreenWindowsOnly: true
        )
        let windows = content.windows.filter { window in
            window.owningApplication?.processID == processID
                && window.frame.width >= 80
                && window.frame.height >= 60
        }
        guard !windows.isEmpty else {
            throw NSError(
                domain: "Friday.Perception",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "No capturable active window was found"]
            )
        }
        let preferred = preferredTitle?.trimmingCharacters(
            in: .whitespacesAndNewlines
        )
        let window = windows.first(where: {
            guard let preferred, !preferred.isEmpty else { return false }
            return $0.title?.localizedCaseInsensitiveContains(preferred) == true
                || preferred.localizedCaseInsensitiveContains($0.title ?? "")
        }) ?? windows.max(by: {
            ($0.frame.width * $0.frame.height) < ($1.frame.width * $1.frame.height)
        })!

        let scale = min(1, Self.maximumImageWidth / max(1, window.frame.width))
        let configuration = SCStreamConfiguration()
        configuration.width = max(1, Int(window.frame.width * scale))
        configuration.height = max(1, Int(window.frame.height * scale))
        configuration.showsCursor = false
        configuration.capturesAudio = false
        configuration.captureResolution = .best
        configuration.ignoreShadowsSingleWindow = true
        configuration.shouldBeOpaque = true
        let filter = SCContentFilter(desktopIndependentWindow: window)
        let image = try await SCScreenshotManager.captureImage(
            contentFilter: filter,
            configuration: configuration
        )
        return CapturedWindow(
            image: image,
            windowID: window.windowID,
            frame: window.frame
        )
    }

    private nonisolated static func jpegData(from image: CGImage) throws -> Data {
        let representation = NSBitmapImageRep(cgImage: image)
        guard var data = representation.representation(
            using: .jpeg,
            properties: [.compressionFactor: 0.58]
        ) else {
            throw NSError(
                domain: "Friday.Perception",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Could not encode the active window"]
            )
        }
        if data.count > 2_200_000,
           let smaller = representation.representation(
               using: .jpeg,
               properties: [.compressionFactor: 0.35]
           ) {
            data = smaller
        }
        return data
    }

    private nonisolated static func perceptualFingerprint(
        from image: CGImage
    ) -> String {
        let width = 9
        let height = 8
        var pixels = [UInt8](repeating: 0, count: width * height)
        let drewImage = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width,
                space: CGColorSpaceCreateDeviceGray(),
                bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else { return false }
            context.interpolationQuality = .medium
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drewImage else {
            return ""
        }
        var hash: UInt64 = 0
        var bit: UInt64 = 1
        for y in 0..<height {
            for x in 0..<(width - 1) {
                if pixels[y * width + x] > pixels[y * width + x + 1] {
                    hash |= bit
                }
                bit <<= 1
            }
        }
        return String(format: "%016llx", hash)
    }

    private nonisolated static func recognizeText(
        in image: CGImage
    ) async throws -> OCRSnapshot {
        try await Task.detached(priority: .utility) {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .fast
            request.usesLanguageCorrection = false
            request.automaticallyDetectsLanguage = false
            request.recognitionLanguages = ["en-US"]
            request.minimumTextHeight = 0.012
            let handler = VNImageRequestHandler(cgImage: image, options: [:])
            try handler.perform([request])
            let observations = (request.results ?? []).sorted { left, right in
                let verticalDistance = abs(left.boundingBox.midY - right.boundingBox.midY)
                if verticalDistance > 0.025 {
                    return left.boundingBox.midY > right.boundingBox.midY
                }
                return left.boundingBox.minX < right.boundingBox.minX
            }
            var lines: [String] = []
            var regions: [OCRRegion] = []
            var characterCount = 0
            for observation in observations {
                guard let candidate = observation.topCandidates(1).first,
                      !candidate.string.isEmpty else { continue }
                let text = candidate.string
                if characterCount + text.count + 1 > 10_000 { break }
                lines.append(text)
                regions.append(
                    OCRRegion(
                        text: text,
                        boundingBox: observation.boundingBox,
                        confidence: candidate.confidence
                    )
                )
                characterCount += text.count + 1
            }
            return OCRSnapshot(
                text: lines.joined(separator: "\n"),
                regions: regions
            )
        }.value
    }

    private func locatedResult(
        base: [String: Any],
        normalizedX: CGFloat,
        normalizedY: CGFloat,
        frame: CGRect,
        context: [String: Any],
        visualToken: String
    ) -> [String: Any] {
        var located = base
        located["x"] = frame.minX + normalizedX * frame.width
        located["y"] = frame.minY + normalizedY * frame.height
        located["normalizedX"] = normalizedX
        located["normalizedY"] = normalizedY
        located["visualFingerprint"] = latestPerceptualFingerprint ?? ""
        located["processId"] = Int(latestImageProcessID ?? 0)
        located["windowId"] = context["windowId"]
        located["ocrText"] = String(latestOCRText.prefix(2_500))
        located["app"] = NSWorkspace.shared.frontmostApplication?
            .localizedName ?? ""
        located["visualToken"] = visualToken
        located["windowFrame"] = [
            "x": frame.minX,
            "y": frame.minY,
            "width": frame.width,
            "height": frame.height,
        ]
        return located
    }

    private func localOCRLocation(
        for target: String
    ) -> (region: OCRRegion, score: Double)? {
        let stopWords: Set<String> = [
            "button", "click", "choose", "control", "hit", "open", "press", "select",
            "the", "to",
        ]
        let targetTerms = Self.normalizedTerms(target).filter { !stopWords.contains($0) }
        guard let firstTarget = targetTerms.first else { return nil }
        let targetText = targetTerms.joined(separator: " ")
        let targetSet = Set(targetTerms)
        let candidates = latestOCRRegions.compactMap { region -> (OCRRegion, Double)? in
            let terms = Self.normalizedTerms(region.text)
            guard !terms.isEmpty else { return nil }
            let text = terms.joined(separator: " ")
            let score: Double
            if text == targetText {
                score = 0.99
            } else if text.contains(targetText) {
                score = 0.94
            } else {
                let overlap = Double(Set(terms).intersection(targetSet).count)
                guard overlap > 0 else { return nil }
                let coverage = overlap / Double(max(targetSet.count, terms.count))
                score = min(0.93, coverage + (terms.first == firstTarget ? 0.35 : 0))
            }
            guard score >= 0.72 else { return nil }
            return (region, score * max(0.75, Double(region.confidence)))
        }.sorted { $0.1 > $1.1 }
        guard let best = candidates.first else { return nil }
        if candidates.count > 1, abs(best.1 - candidates[1].1) < 0.04 {
            return nil
        }
        return best
    }

    private nonisolated static func normalizedTerms(_ value: String) -> [String] {
        value.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init)
    }

    private static func requiresVisualAnalysis(_ query: String) -> Bool {
        let normalized = query
            .lowercased()
            .replacingOccurrences(of: "’", with: "'")
        if visualTerms.contains(where: normalized.contains) {
            return true
        }
        return [
            "what is this",
            "what's this",
            "explain this error",
            "read this error",
            "what is happening here",
        ].contains(normalized.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private static func visualMetadata(
        from context: [String: Any]
    ) -> [String: Any] {
        var metadata: [String: Any] = [:]
        for key in [
            "application",
            "currentWindow",
            "currentDocument",
            "currentURL",
            "semantic",
            "capturedAt",
        ] {
            if let value = context[key] {
                metadata[key] = value
            }
        }
        return metadata
    }

    private static func number(_ value: Any?) -> CGFloat? {
        if let value = value as? Double { return CGFloat(value) }
        if let value = value as? Int { return CGFloat(value) }
        if let value = value as? NSNumber { return CGFloat(value.doubleValue) }
        return nil
    }

    private static func thermalStateName(
        _ state: ProcessInfo.ThermalState
    ) -> String {
        switch state {
        case .nominal: return "nominal"
        case .fair: return "fair"
        case .serious: return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }
}
