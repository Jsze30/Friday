import Foundation

/// Orchestrates startup: spawn local_service → wait for port → health → token → LiveKit connect → events WS.
@MainActor
final class BootCoordinator {
    static let shared = BootCoordinator()

    private(set) var servicePort: Int?
    private var client: LocalServiceClient?
    private var eventTask: Task<Void, Never>?
    let liveKit = LiveKitController()

    func start() {
        Task { await self.boot() }
    }

    private func boot() async {
        do {
            try LocalServiceProcess.shared.start()
            let port = try await LocalServiceProcess.shared.waitForPort()
            servicePort = port
            let c = LocalServiceClient(port: port)
            client = c

            try await c.health()
            let token = try await c.mintToken()
            try await liveKit.connect(token: token)

            // Subscribe to local_service events.
            eventTask = c.openEventStream(
                onEventJSON: { json in
                    guard let data = json.data(using: .utf8),
                          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                          let type = obj["type"] as? String else { return }
                    switch type {
                    case "wake_detected":
                        Task { @MainActor in
                            try? await c.pauseWake()
                            self.liveKit.handleWakeDetected()
                        }
                    case "profile_updated":
                        Task { @MainActor in
                            await self.liveKit.forwardProfileUpdated(json: json)
                        }
                    default:
                        break
                    }
                },
                onError: { err in
                    NSLog("[Friday] events WS error: \(err)")
                    Task { @MainActor in AppState.shared.set(.error, error: "events WS dropped") }
                }
            )
        } catch {
            NSLog("[Friday] boot failed: \(error)")
            AppState.shared.set(.error, error: error.localizedDescription)
        }
    }

    func shutdown() async {
        eventTask?.cancel()
        await liveKit.disconnect()
        LocalServiceProcess.shared.stop()
    }
}
