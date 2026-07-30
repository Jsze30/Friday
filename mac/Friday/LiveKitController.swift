import Foundation
import LiveKit

@MainActor
final class LiveKitController: NSObject, RoomDelegate {
    private let room = Room()
    private var agentName: String = "friday-agent"
    private var wakeDetector: WakeDetector?
    private var wakeAudioSender: WakeAudioSender?

    /// Topic for the pre-roll PCM byte stream; the agent registers a handler
    /// for the same topic and prepends the audio to the turn's STT input.
    private static let preRollTopic = "friday.wake-preroll"

    /// Connects to LiveKit and publishes the mic once. The capture processor
    /// sends silence while sleeping, so AEC and local wake scoring stay warm
    /// without transmitting room audio before activation.
    func connect(token: TokenResponse, servicePort: Int) async throws {
        agentName = token.agentName
        room.add(delegate: self)

        try AudioManager.shared.set(microphoneMuteMode: .inputMixer)

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

        // The agent fetches its tool manifest and profile as soon as it joins.
        // Register these handlers before publishing the microphone, which can
        // give the dispatched agent enough time to enter the room and call us.
        await registerSwiftRPCs()

        startWakeDetection(servicePort: servicePort)

        // Publish the mic and keep capture active. WakeDetector replaces the
        // outgoing buffer with silence while sleeping, which keeps local wake
        // inference alive without sending room audio before activation.
        try await room.localParticipant.setMicrophone(enabled: true)

        // Signal only after RPC handlers and the publisher connection are
        // ready. The agent waits for this before its one-time manifest fetch.
        try await room.localParticipant.set(attributes: ["friday.rpcReady": "true"])

        AppState.shared.set(.sleeping)
    }

    func disconnect() async {
        if let detector = wakeDetector {
            if AudioManager.shared.capturePostProcessingDelegate === detector {
                AudioManager.shared.capturePostProcessingDelegate = nil
            }
            wakeDetector = nil
        }
        await wakeAudioSender?.close()
        wakeAudioSender = nil
        await room.disconnect()
    }

    // MARK: - Wake detection

    private func startWakeDetection(servicePort: Int) {
        guard wakeDetector == nil else { return }
        let sender = WakeAudioSender(port: servicePort)
        let detector = WakeDetector { [weak sender] chunk in
            sender?.enqueue(chunk)
        }
        wakeAudioSender = sender
        AudioManager.shared.capturePostProcessingDelegate = detector
        wakeDetector = detector
    }

    /// openWakeWord fired on the local service. Forward new microphone audio,
    /// ship the local pre-roll, then activate the agent turn.
    func handleWakeDetected(confidence: Float) {
        guard let wakeDetector else { return }
        let preRoll = wakeDetector.pauseAndTakePreRoll()
        Task {
            AppState.shared.set(.wakeDetected)
            do {
                await sendPreRoll(preRoll)
                try await activateTurnWithRetry()
                AppState.shared.set(.listening)
            } catch {
                NSLog("[Friday] activate_turn failed: \(error)")
                await resumeWakeDetection()
                AppState.shared.set(.error, error: error.localizedDescription)
            }
        }
    }

    /// Send the pre-roll PCM (16kHz mono Int16) to the agent as a byte
    /// stream. Failure is non-fatal: the turn still works, minus the words
    /// spoken before the wake word finished.
    private func sendPreRoll(_ pcm: Data) async {
        do {
            let agent = try await waitForAgentParticipant(timeout: 15.0)
            guard let identity = agent.identity else { return }
            let options = StreamByteOptions(
                topic: Self.preRollTopic,
                attributes: [
                    "sampleRate": "\(Int(WakeDetector.sampleRate))",
                    "channels": "1",
                ],
                destinationIdentities: [identity]
            )
            let writer = try await room.localParticipant.streamBytes(options: options)
            try await writer.write(pcm)
            try await writer.close()
        } catch {
            NSLog("[Friday] pre-roll send failed (turn continues without it): \(error)")
        }
    }

    /// Cold-start tolerant: participant presence can precede RPC method
    /// registration while the cloud agent loads its tools and context. Retry
    /// across that initialization window instead of failing immediately.
    private func activateTurnWithRetry() async throws {
        let deadline = Date().addingTimeInterval(20)
        var attempt = 0

        while true {
            attempt += 1
            do {
                try await callAgent("activate_turn", responseTimeout: 10)
                return
            } catch {
                guard Date() < deadline else { throw error }
                NSLog("[Friday] activate_turn retry \(attempt): \(error)")
                try await Task.sleep(nanoseconds: 500_000_000)
            }
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
        try? await room.registerRpcMethod("get_location") { _ in
            await MainActor.run {
                LocationProvider.shared.currentLocationJSON()
            }
        }
        try? await room.registerRpcMethod("get_context") { _ in
            guard let port = await BootCoordinator.shared.servicePort else { return "{}" }
            let profile = (try? await LocalServiceClient(port: port).getProfileObject())
                ?? [:]
            let location = await MainActor.run {
                LocationProvider.shared.currentLocationObject()
            }
            return Self.encodeJSON([
                "profile": profile,
                "location": location,
            ])
        }
        try? await room.registerRpcMethod("tool_call") { data in
            guard let port = await BootCoordinator.shared.servicePort else { return "{}" }
            return await self.executeBridgedTool(
                jsonPayload: data.payload,
                servicePort: port
            )
        }
        try? await room.registerRpcMethod("capability_call") { data in
            guard let port = await BootCoordinator.shared.servicePort else {
                return Self.errorEnvelope("local service is not ready")
            }
            do {
                return try await LocalServiceClient(port: port)
                    .executeCapability(jsonPayload: data.payload)
            } catch {
                return Self.errorEnvelope("local capability call failed")
            }
        }
    }

    private func executeBridgedTool(
        jsonPayload: String,
        servicePort: Int
    ) async -> String {
        guard let payload = Self.decodeJSON(jsonPayload),
              let tool = payload["tool"] as? String else {
            return Self.errorEnvelope("tool is required")
        }

        if tool == "__list__" {
            do {
                let localJSON = try await LocalServiceClient(port: servicePort)
                    .executeTool(jsonPayload: jsonPayload)
                return Self.mergeToolManifests(localJSON: localJSON)
            } catch {
                return Self.errorEnvelope("could not load local primitives")
            }
        }

        if MacPrimitiveProvider.shared.toolNames.contains(tool) {
            return await MacPrimitiveProvider.shared.execute(jsonPayload: jsonPayload)
        }

        if tool == "confirm_action",
           let arguments = payload["arguments"] as? [String: Any],
           let confirmationID = arguments["confirmation_id"] as? String,
           confirmationID.hasPrefix("mac:") {
            return await MacPrimitiveProvider.shared.confirm(jsonPayload: jsonPayload)
        }

        do {
            return try await LocalServiceClient(port: servicePort)
                .executeTool(jsonPayload: jsonPayload)
        } catch {
            return Self.errorEnvelope("local primitive call failed")
        }
    }

    private static func mergeToolManifests(localJSON: String) -> String {
        guard var envelope = decodeJSON(localJSON),
              var data = envelope["data"] as? [String: Any],
              var tools = data["tools"] as? [[String: Any]] else {
            return errorEnvelope("local primitive manifest was invalid")
        }
        tools.append(contentsOf: MacPrimitiveProvider.shared.manifests)
        data["tools"] = tools
        envelope["data"] = data
        return encodeJSON(envelope)
    }

    private nonisolated static func decodeJSON(_ value: String) -> [String: Any]? {
        guard let data = value.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else {
            return nil
        }
        return object as? [String: Any]
    }

    private nonisolated static func encodeJSON(_ object: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(object),
              let data = try? JSONSerialization.data(
                withJSONObject: object,
                options: [.sortedKeys]
              ) else {
            return #"{"ok":false,"error":"could not encode JSON"}"#
        }
        return String(data: data, encoding: .utf8)
            ?? #"{"ok":false,"error":"could not encode JSON"}"#
    }

    private nonisolated static func errorEnvelope(_ message: String) -> String {
        encodeJSON([
            "ok": false,
            "spoken": NSNull(),
            "data": NSNull(),
            "needsConfirmation": false,
            "confirmationId": NSNull(),
            "error": message,
        ])
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

    /// Forward a fresh Core Location snapshot to the agent.
    func forwardLocationUpdated(json: String) async {
        do {
            let agent = try await waitForAgentParticipant(timeout: 2.0)
            _ = try await room.localParticipant.performRpc(
                destinationIdentity: agent.identity!,
                method: "location_updated",
                payload: json
            )
        } catch {
            NSLog("[Friday] forwardLocationUpdated failed: \(error)")
        }
    }

    /// Called by agent RPC to re-arm local wake detection.
    private func returnToSleep() async {
        await resumeWakeDetection()
        AppState.shared.set(.sleeping)
    }

    private func resumeWakeDetection() async {
        guard let port = BootCoordinator.shared.servicePort else {
            wakeDetector?.resume()
            return
        }

        do {
            try await LocalServiceClient(port: port).resumeWake()
        } catch {
            NSLog("[Friday] wake reset failed: \(error)")
        }

        wakeDetector?.resume()
    }

    private func callAgent(
        _ method: String,
        payload: String = "",
        responseTimeout: TimeInterval = 15
    ) async throws {
        let agent = try await waitForAgentParticipant(timeout: 15.0)
        _ = try await room.localParticipant.performRpc(
            destinationIdentity: agent.identity!,
            method: method,
            payload: payload,
            responseTimeout: responseTimeout
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
                NSLog("[Friday] waiting for agent - remoteParticipants=[\(snapshot)]")
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

    nonisolated func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        Task { @MainActor in
            let isAgent = participant.kind == .agent
                || participant.identity?.stringValue == agentName
            guard isAgent, AppState.shared.state != .sleeping else { return }

            NSLog("[Friday] agent disconnected during an active turn; resuming wake detection")
            await resumeWakeDetection()
            AppState.shared.set(.sleeping)
        }
    }
}
