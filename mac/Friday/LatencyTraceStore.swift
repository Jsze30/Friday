import Foundation

final class LatencyTraceStore: @unchecked Sendable {
    static let shared = LatencyTraceStore()

    private let queue = DispatchQueue(label: "com.friday.latency-trace")
    private let path: URL

    private init() {
        let logs = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Friday", isDirectory: true)
        path = logs.appendingPathComponent("latency.jsonl")
    }

    func recordHUD(event: [String: Any]) {
        var trace: [String: Any] = [
            "recordedAt": ISO8601DateFormatter().string(from: Date()),
            "type": event["type"] as? String ?? "hud_event",
        ]
        for key in ["turnId", "elapsedMs", "contextLatencyMs", "metrics"] {
            if let value = event[key] {
                trace[key] = value
            }
        }
        append(trace)
    }

    func recordState(_ state: AssistantState, turnID: String?) {
        var trace: [String: Any] = [
            "recordedAt": ISO8601DateFormatter().string(from: Date()),
            "type": "state",
            "state": state.rawValue,
        ]
        if let turnID {
            trace["turnId"] = turnID
        }
        append(trace)
    }

    func recordPerception(_ context: [String: Any]) {
        var trace: [String: Any] = [
            "recordedAt": ISO8601DateFormatter().string(from: Date()),
            "type": "computer_perception",
        ]
        for key in [
            "trigger",
            "captureStatus",
            "captureError",
            "captureLatencyMs",
            "ocrLatencyMs",
            "semanticLatencyMs",
            "imageChanged",
            "imageWidth",
            "imageHeight",
            "lowPowerMode",
            "thermalState",
        ] {
            if let value = context[key] {
                trace[key] = value
            }
        }
        append(trace)
    }

    private func append(_ trace: [String: Any]) {
        queue.async { [path] in
            guard JSONSerialization.isValidJSONObject(trace),
                  var data = try? JSONSerialization.data(
                    withJSONObject: trace,
                    options: [.sortedKeys]
                  ) else { return }
            data.append(0x0A)
            do {
                try FileManager.default.createDirectory(
                    at: path.deletingLastPathComponent(),
                    withIntermediateDirectories: true
                )
                if !FileManager.default.fileExists(atPath: path.path) {
                    try Data().write(to: path, options: .atomic)
                }
                let handle = try FileHandle(forWritingTo: path)
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
                try handle.close()
            } catch {
                NSLog("[Friday] could not write latency trace: \(error)")
            }
        }
    }
}
