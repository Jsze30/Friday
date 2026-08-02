import Foundation

struct TokenResponse: Decodable {
    let url: String
    let token: String
    let roomName: String
    let participantIdentity: String
    let agentName: String
}

actor LocalServiceClient {
    private let baseURL: URL
    private let session: URLSession

    init(port: Int) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        self.session = URLSession(configuration: cfg)
    }

    func health() async throws {
        let (_, resp) = try await session.data(from: baseURL.appendingPathComponent("health"))
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw NSError(domain: "Friday", code: 2, userInfo: [NSLocalizedDescriptionKey: "health check failed"])
        }
    }

    func mintToken() async throws -> TokenResponse {
        var req = URLRequest(url: baseURL.appendingPathComponent("token"))
        req.httpMethod = "POST"
        let (data, resp) = try await session.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw NSError(domain: "Friday", code: 3, userInfo: [NSLocalizedDescriptionKey: "token mint failed"])
        }
        return try JSONDecoder().decode(TokenResponse.self, from: data)
    }

    /// Returns the raw profile JSON string (suitable for forwarding via RPC).
    func getProfileJSON() async throws -> String {
        let (data, resp) = try await session.data(from: baseURL.appendingPathComponent("profile"))
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw NSError(domain: "Friday", code: 4, userInfo: [NSLocalizedDescriptionKey: "profile fetch failed"])
        }
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    func getProfileObject() async throws -> [String: Any] {
        let raw = try await getProfileJSON()
        guard let data = raw.data(using: .utf8),
              let object = try JSONSerialization.jsonObject(with: data)
                as? [String: Any] else {
            throw NSError(
                domain: "Friday",
                code: 5,
                userInfo: [NSLocalizedDescriptionKey: "profile JSON was invalid"]
            )
        }
        return object
    }

    /// payload is a JSON object: {"tool": "...", "arguments": {...}, "requestId": "..."}
    /// Returns the raw response envelope JSON string.
    func executeTool(jsonPayload: String) async throws -> String {
        var req = URLRequest(url: baseURL.appendingPathComponent("tools/execute"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = jsonPayload.data(using: .utf8)
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw NSError(domain: "Friday", code: 6, userInfo: [NSLocalizedDescriptionKey: "tool execute failed"])
        }
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    /// Starts, polls, lists, or cancels one background capability task.
    func executeCapability(jsonPayload: String) async throws -> String {
        var req = URLRequest(
            url: baseURL.appendingPathComponent("capabilities/execute")
        )
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = jsonPayload.data(using: .utf8)
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw NSError(
                domain: "Friday",
                code: 8,
                userInfo: [
                    NSLocalizedDescriptionKey: "capability call failed"
                ]
            )
        }
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    func resolveContext(
        query: String,
        working: [String: Any],
        sessionId: String? = nil,
        maxCharacters: Int = 8_000
    ) async throws -> String {
        var req = URLRequest(url: baseURL.appendingPathComponent("context/resolve"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [
            "query": query,
            "working": working,
            "maxCharacters": maxCharacters,
        ]
        if let sessionId, !sessionId.isEmpty {
            body["sessionId"] = sessionId
        }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw NSError(
                domain: "Friday",
                code: 9,
                userInfo: [NSLocalizedDescriptionKey: "context resolution failed"]
            )
        }
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    func recordContextEvent(_ event: [String: Any]) async throws {
        var req = URLRequest(url: baseURL.appendingPathComponent("context/events"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: event)
        let (_, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw NSError(
                domain: "Friday",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey: "context event write failed"
                ]
            )
        }
    }

    func contextMemories(
        kind: String? = nil,
        query: String = ""
    ) async throws -> [[String: Any]] {
        var components = URLComponents(
            url: baseURL.appendingPathComponent("context/memories"),
            resolvingAgainstBaseURL: false
        )!
        var items: [URLQueryItem] = []
        if let kind, !kind.isEmpty, kind != "all" {
            items.append(URLQueryItem(name: "kind", value: kind))
        }
        if !query.isEmpty {
            items.append(URLQueryItem(name: "query", value: query))
        }
        components.queryItems = items.isEmpty ? nil : items
        let (data, resp) = try await session.data(from: components.url!)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200,
              let payload = try JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              let memories = payload["memories"] as? [[String: Any]] else {
            throw NSError(
                domain: "Friday",
                code: 13,
                userInfo: [NSLocalizedDescriptionKey: "memory fetch failed"]
            )
        }
        return memories
    }

    func rememberReference(
        alias: String,
        target: String,
        kind: String
    ) async throws {
        var req = URLRequest(url: baseURL.appendingPathComponent("context/references"))
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(
            withJSONObject: ["alias": alias, "target": target, "kind": kind]
        )
        let (_, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw NSError(
                domain: "Friday",
                code: 14,
                userInfo: [NSLocalizedDescriptionKey: "memory write failed"]
            )
        }
    }

    func forgetContextMemory(id: String) async throws {
        let url = baseURL
            .appendingPathComponent("context/memories")
            .appendingPathComponent(id)
        var req = URLRequest(url: url)
        req.httpMethod = "DELETE"
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200,
              let payload = try JSONSerialization.jsonObject(with: data)
                as? [String: Any],
              payload["forgotten"] as? Bool == true else {
            throw NSError(
                domain: "Friday",
                code: 15,
                userInfo: [NSLocalizedDescriptionKey: "memory deletion failed"]
            )
        }
    }

    func analyzeVisualContext(
        query: String,
        imageData: Data,
        mimeType: String,
        ocrText: String,
        metadata: [String: Any]
    ) async throws -> [String: Any] {
        var req = URLRequest(
            url: baseURL.appendingPathComponent("perception/analyze")
        )
        req.httpMethod = "POST"
        req.timeoutInterval = 25
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(
            withJSONObject: [
                "query": query,
                "imageBase64": imageData.base64EncodedString(),
                "mimeType": mimeType,
                "ocrText": ocrText,
                "metadata": metadata,
            ]
        )
        let (data, resp) = try await session.data(for: req)
        guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
            throw NSError(
                domain: "Friday",
                code: 11,
                userInfo: [
                    NSLocalizedDescriptionKey: "visual analysis failed"
                ]
            )
        }
        guard let object = try JSONSerialization.jsonObject(with: data)
                as? [String: Any] else {
            throw NSError(
                domain: "Friday",
                code: 12,
                userInfo: [
                    NSLocalizedDescriptionKey: "visual analysis response was invalid"
                ]
            )
        }
        return object
    }

    func resumeWake() async throws {
        var req = URLRequest(url: baseURL.appendingPathComponent("wake/resume"))
        req.httpMethod = "POST"
        let (_, resp) = try await session.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw NSError(
                domain: "Friday",
                code: 7,
                userInfo: [NSLocalizedDescriptionKey: "wake reset failed"]
            )
        }
    }

    /// Long-lived WebSocket; emits raw JSON event text via the callback.
    /// The returned Task should be cancelled to disconnect.
    nonisolated func openEventStream(onEventJSON: @escaping @Sendable (String) -> Void,
                                     onError: @escaping @Sendable (Error) -> Void) -> Task<Void, Never> {
        let url = URL(string: "ws://127.0.0.1:\(baseURL.port ?? 0)/events")!
        return Task.detached {
            let cfg = URLSessionConfiguration.ephemeral
            let session = URLSession(configuration: cfg)
            let task = session.webSocketTask(with: url)
            task.resume()
            do {
                while !Task.isCancelled {
                    let msg = try await task.receive()
                    let text: String?
                    switch msg {
                    case .string(let s): text = s
                    case .data(let d):   text = String(data: d, encoding: .utf8)
                    @unknown default:    text = nil
                    }
                    if let t = text {
                        onEventJSON(t)
                    }
                }
            } catch {
                onError(error)
            }
            task.cancel(with: .goingAway, reason: nil)
        }
    }
}
