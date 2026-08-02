import Foundation

/// Orchestrates startup: spawn local service, connect events, mint a token, then connect LiveKit.
@MainActor
final class BootCoordinator {
    static let shared = BootCoordinator()

    private(set) var servicePort: Int?
    private var client: LocalServiceClient?
    private var eventTask: Task<Void, Never>?
    let liveKit = LiveKitController()

    func start() {
        LocationProvider.shared.start()
        MacPrimitiveProvider.shared.requestAccessibilityPermission()
        ComputerPerceptionProvider.shared.start()
        Task { await CalendarContextProvider.shared.prepareAccess() }
        Task { await self.boot() }
    }

    private func boot() async {
        do {
            try LocalServiceProcess.shared.start()
            let port = try await LocalServiceProcess.shared.waitForPort(timeout: 30)
            servicePort = port
            let c = LocalServiceClient(port: port)
            client = c

            try await c.health()
            startEventStream(client: c)
            let token = try await c.mintToken()
            try await liveKit.connect(token: token, servicePort: port)

            LocationProvider.shared.onLocationUpdate = { [weak self] json in
                Task { @MainActor in
                    await self?.liveKit.forwardLocationUpdated(json: json)
                }
            }

        } catch {
            NSLog("[Friday] boot failed: \(error)")
            AppState.shared.set(.error, error: error.localizedDescription)
        }
    }

    private func startEventStream(client: LocalServiceClient) {
        eventTask?.cancel()
        eventTask = client.openEventStream(
            onEventJSON: { json in
                guard let data = json.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any],
                      let type = obj["type"] as? String else { return }
                switch type {
                case "wake_detected":
                    let confidence = obj["confidence"] as? Double ?? 0
                    Task { @MainActor in
                        self.liveKit.handleWakeDetected(
                            confidence: Float(confidence)
                        )
                    }
                case "profile_updated":
                    Task { @MainActor in
                        await self.liveKit.forwardProfileUpdated(json: json)
                    }
                default:
                    break
                }
            },
            onNativeToolRequest: { json in
                await MacPrimitiveProvider.shared.execute(jsonPayload: json)
            },
            onError: { err in
                NSLog("[Friday] events WS error: \(err)")
                Task { @MainActor in
                    AppState.shared.set(.error, error: "events WS dropped")
                }
            }
        )
    }

    func shutdown() async {
        eventTask?.cancel()
        ComputerPerceptionProvider.shared.stop()
        await liveKit.disconnect()
        LocalServiceProcess.shared.stop()
    }
}
