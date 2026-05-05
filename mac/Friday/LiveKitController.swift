import Foundation
import LiveKit

@MainActor
final class LiveKitController: NSObject, RoomDelegate {
    private let room = Room()
    private var agentName: String = "friday-agent"

    /// Connects to LiveKit with mic *disabled*. Call after local_service is up.
    func connect(token: TokenResponse) async throws {
        agentName = token.agentName
        room.add(delegate: self)

        let connectOptions = ConnectOptions(autoSubscribe: true)
        let roomOptions = RoomOptions(
            defaultCameraCaptureOptions: CameraCaptureOptions(),
            defaultAudioCaptureOptions: AudioCaptureOptions(),
            adaptiveStream: false,
            dynacast: false
        )

        try await room.connect(url: token.url, token: token.token,
                               connectOptions: connectOptions,
                               roomOptions: roomOptions)

        // Important: do NOT publish mic on connect. Stays muted/preconnected.
        _ = try? await room.localParticipant.setMicrophone(enabled: false)

        await registerSwiftRPCs()
        AppState.shared.set(.sleeping)
    }

    func disconnect() async {
        await room.disconnect()
    }

    // MARK: - Wake handling

    /// Called when local_service emits `wake_detected`.
    func handleWakeDetected() {
        Task {
            AppState.shared.set(.wakeDetected)
            do {
                try await room.localParticipant.setMicrophone(enabled: true)
                try await activateTurnWithRetry()
                AppState.shared.set(.listening)
            } catch {
                NSLog("[Friday] activate_turn failed: \(error)")
                AppState.shared.set(.error, error: error.localizedDescription)
            }
        }
    }

    /// Cold-start tolerant: if the agent worker isn't in the room yet, keep
    /// waiting (one retry) instead of erroring. The user stays in
    /// `wakeDetected` while the worker spins up.
    private func activateTurnWithRetry() async throws {
        do {
            try await callAgent("activate_turn")
        } catch {
            NSLog("[Friday] activate_turn cold-start retry: \(error)")
            try await callAgent("activate_turn")
        }
    }

    // MARK: - RPCs

    private func registerSwiftRPCs() async {
        try? await room.registerRpcMethod("return_to_sleep") { [weak self] _ in
            await self?.returnToSleep()
            return "ok"
        }
        try? await room.registerRpcMethod("set_assistant_state") { data in
            let raw = data.payload.trimmingCharacters(in: .whitespacesAndNewlines)
            if let s = AssistantState(rawValue: raw) {
                await MainActor.run { AppState.shared.set(s) }
            }
            return "ok"
        }
        try? await room.registerRpcMethod("get_profile") { _ in
            guard let port = await BootCoordinator.shared.servicePort else { return "{}" }
            return (try? await LocalServiceClient(port: port).getProfileJSON()) ?? "{}"
        }
        try? await room.registerRpcMethod("tool_call") { data in
            guard let port = await BootCoordinator.shared.servicePort else { return "{}" }
            return (try? await LocalServiceClient(port: port).executeTool(jsonPayload: data.payload)) ?? "{}"
        }
    }

    /// Forward a profile_updated event from local_service to the agent.
    func forwardProfileUpdated(json: String) async {
        do {
            let agent = try await waitForAgentParticipant(timeout: 2.0)
            _ = try await room.localParticipant.performRpc(
                destinationIdentity: agent.identity!,
                method: "profile_updated",
                payload: json
            )
        } catch {
            NSLog("[Friday] forwardProfileUpdated failed: \(error)")
        }
    }

    /// Called by agent RPC; we go to sleep state, mute mic, resume wake.
    private func returnToSleep() async {
        _ = try? await room.localParticipant.setMicrophone(enabled: false)
        if let port = BootCoordinator.shared.servicePort {
            try? await LocalServiceClient(port: port).resumeWake()
        }
        AppState.shared.set(.sleeping)
    }

    private func callAgent(_ method: String, payload: String = "") async throws {
        let agent = try await waitForAgentParticipant(timeout: 15.0)
        _ = try await room.localParticipant.performRpc(
            destinationIdentity: agent.identity!,
            method: method,
            payload: payload
        )
    }

    private func waitForAgentParticipant(timeout: TimeInterval) async throws -> RemoteParticipant {
        let deadline = Date().addingTimeInterval(timeout)
        var lastSnapshot = ""
        while Date() < deadline {
            if let p = findAgentParticipant() { return p }
            let snapshot = room.remoteParticipants.values
                .map { "\($0.identity?.stringValue ?? "?")(kind=\($0.kind))" }
                .joined(separator: ", ")
            if snapshot != lastSnapshot {
                NSLog("[Friday] waiting for agent — remoteParticipants=[\(snapshot)]")
                lastSnapshot = snapshot
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw NSError(domain: "Friday", code: 10,
                      userInfo: [NSLocalizedDescriptionKey: "agent not in room after \(timeout)s; saw [\(lastSnapshot)]"])
    }

    private func findAgentParticipant() -> RemoteParticipant? {
        // Prefer kind == .agent. Fall back to identity match against agentName,
        // then to any single remote participant if the room only has one.
        let participants = Array(room.remoteParticipants.values)
        if let p = participants.first(where: { $0.kind == .agent }) { return p }
        if let p = participants.first(where: { $0.identity?.stringValue == agentName }) { return p }
        if participants.count == 1 { return participants.first }
        return nil
    }

    // MARK: - RoomDelegate

    nonisolated func room(_ room: Room, didUpdateConnectionState state: ConnectionState, from oldState: ConnectionState) {
        Task { @MainActor in
            switch state {
            case .connected:
                if AppState.shared.state == .disconnected {
                    AppState.shared.set(.sleeping)
                }
            case .disconnected:
                AppState.shared.set(.disconnected)
            default: break
            }
        }
    }
}
