import Foundation

enum AssistantState: String, Sendable {
    case disconnected
    case sleeping
    case wakeDetected
    case listening
    case thinking
    case acting
    case speaking
    case followupWindow
    case error
}

struct HUDReference: Identifiable, Equatable {
    let phrase: String
    let target: String
    let source: String
    let confidence: Double

    var id: String { "\(phrase)|\(target)" }
}

@MainActor
final class AppState: ObservableObject {
    static let shared = AppState()

    @Published private(set) var state: AssistantState = .disconnected
    @Published private(set) var lastError: String?
    @Published private(set) var userTranscript = ""
    @Published private(set) var assistantTranscript = ""
    @Published private(set) var activityDetail = ""
    @Published private(set) var resultDetail = ""
    @Published private(set) var references: [HUDReference] = []
    @Published private(set) var latencyText = ""
    @Published private(set) var turnID: String?
    @Published private(set) var revision = 0

    var onChange: ((AssistantState) -> Void)?

    var shouldShowHUD: Bool {
        switch state {
        case .disconnected, .sleeping:
            false
        default:
            true
        }
    }

    var preferredHUDHeight: CGFloat {
        var height: CGFloat = 82
        if !userTranscript.isEmpty { height += 34 }
        if !assistantTranscript.isEmpty { height += 44 }
        if !references.isEmpty { height += 32 }
        if !resultDetail.isEmpty { height += 28 }
        if !latencyText.isEmpty { height += 20 }
        return min(max(height, 96), 250)
    }

    func beginTurn() {
        turnID = nil
        userTranscript = ""
        assistantTranscript = ""
        activityDetail = ""
        resultDetail = ""
        references = []
        latencyText = ""
        lastError = nil
        revision += 1
    }

    func set(_ new: AssistantState, error: String? = nil) {
        if new == state && error == lastError { return }
        state = new
        lastError = error
        revision += 1
        LatencyTraceStore.shared.recordState(new, turnID: turnID)
        onChange?(new)
    }

    func applyHUDEvent(_ event: [String: Any]) {
        LatencyTraceStore.shared.recordHUD(event: event)
        let type = event["type"] as? String ?? ""
        if type == "turn_started" {
            beginTurn()
            turnID = event["turnId"] as? String
            revision += 1
            return
        }

        if let incomingTurnID = event["turnId"] as? String,
           let turnID,
           incomingTurnID != turnID {
            return
        }

        switch type {
        case "transcript":
            let text = event["text"] as? String ?? ""
            switch event["role"] as? String {
            case "user":
                userTranscript = text
            case "assistant":
                assistantTranscript = text
            default:
                break
            }
        case "context":
            references = Self.parseReferences(event["resolutions"])
            if let first = references.first {
                activityDetail = "\(first.phrase) means \(Self.displayName(first.target))"
            }
        case "action_started":
            state = .acting
            activityDetail = event["description"] as? String
                ?? Self.readableIdentifier(event["action"] as? String)
            resultDetail = ""
        case "action_completed":
            let ok = event["ok"] as? Bool ?? false
            resultDetail = Self.resultSummary(event) ?? (ok ? "Done" : "Action failed")
            if !ok {
                state = .error
                lastError = event["error"] as? String ?? "The action failed."
            }
        case "capability_started":
            state = .acting
            activityDetail = event["goal"] as? String
                ?? Self.readableIdentifier(event["capability"] as? String)
            resultDetail = ""
        case "capability_progress":
            state = .acting
            activityDetail = event["message"] as? String ?? activityDetail
        case "capability_completed":
            let ok = event["ok"] as? Bool ?? false
            resultDetail = Self.resultSummary(event) ?? (ok ? "Complete" : "Task failed")
            if !ok, event["status"] as? String != "cancelled" {
                state = .error
                lastError = event["error"] as? String ?? "The task failed."
            }
        case "latency":
            latencyText = Self.formatLatency(event["metrics"])
        case "error":
            state = .error
            lastError = event["message"] as? String ?? "Friday encountered an error."
        default:
            break
        }
        revision += 1
    }

    func runHUDPreview() {
        guard state == .sleeping || state == .disconnected else { return }
        beginTurn()
        set(.wakeDetected)
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(250))
            set(.listening)
            userTranscript = "Open that project and show me what changed"
            revision += 1
            try? await Task.sleep(for: .milliseconds(900))
            set(.thinking)
            references = [
                HUDReference(
                    phrase: "that project",
                    target: "Friday",
                    source: "saved memory",
                    confidence: 1
                )
            ]
            activityDetail = "that project means Friday"
            revision += 1
            try? await Task.sleep(for: .milliseconds(750))
            set(.acting)
            activityDetail = "Opening the Friday project"
            revision += 1
            try? await Task.sleep(for: .milliseconds(700))
            resultDetail = "Project opened"
            assistantTranscript = "I opened Friday and found three changed files."
            latencyText = "First response 812 ms"
            set(.speaking)
            try? await Task.sleep(for: .seconds(3))
            set(.sleeping)
        }
    }

    private static func parseReferences(_ raw: Any?) -> [HUDReference] {
        guard let values = raw as? [[String: Any]] else { return [] }
        return values.compactMap { value in
            guard let phrase = value["phrase"] as? String,
                  let target = value["target"] as? String else { return nil }
            return HUDReference(
                phrase: phrase,
                target: target,
                source: value["source"] as? String ?? "context",
                confidence: value["confidence"] as? Double ?? 0
            )
        }
    }

    private static func resultSummary(_ event: [String: Any]) -> String? {
        if let message = event["message"] as? String, !message.isEmpty {
            return message
        }
        if let error = event["error"] as? String, !error.isEmpty {
            return error
        }
        if let result = event["result"] as? [String: Any] {
            for key in ["summary", "message", "title", "name"] {
                if let value = result[key] as? String, !value.isEmpty {
                    return value
                }
            }
        }
        return nil
    }

    private static func formatLatency(_ raw: Any?) -> String {
        guard let metrics = raw as? [String: Any] else { return "" }
        let directValue = metrics["e2e_latency"] as? Double
        let integerValue = (metrics["e2e_latency"] as? Int).map(Double.init)
        let value = directValue ?? integerValue
        guard let value else { return "" }
        return "First response \(Int(value.rounded())) ms"
    }

    private static func displayName(_ target: String) -> String {
        URL(fileURLWithPath: target).lastPathComponent.isEmpty
            ? target
            : URL(fileURLWithPath: target).lastPathComponent
    }

    private static func readableIdentifier(_ value: String?) -> String {
        guard let value, !value.isEmpty else { return "Working" }
        return value
            .split(separator: ".")
            .last?
            .replacingOccurrences(of: "_", with: " ")
            .capitalized ?? value
    }
}
