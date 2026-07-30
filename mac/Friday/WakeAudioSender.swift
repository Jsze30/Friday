import Foundation
import os

final class WakeAudioSender: @unchecked Sendable {
    private let session: URLSession
    private let url: URL
    private let continuation: AsyncStream<Data>.Continuation
    private var worker: Task<Void, Never>?

    private static let logger = Logger(
        subsystem: "com.friday.menubar",
        category: "wake"
    )

    init(port: Int) {
        let configuration = URLSessionConfiguration.ephemeral
        let session = URLSession(configuration: configuration)
        let url = URL(string: "ws://127.0.0.1:\(port)/wake/audio")!

        var streamContinuation: AsyncStream<Data>.Continuation!
        let stream = AsyncStream<Data>(bufferingPolicy: .unbounded) {
            streamContinuation = $0
        }

        self.session = session
        self.url = url
        continuation = streamContinuation
        worker = nil

        worker = Task { [weak self] in
            await self?.send(stream)
        }
    }

    /// AsyncStream preserves the capture callback's ordering while the worker
    /// performs WebSocket sends away from LiveKit's real-time audio thread.
    func enqueue(_ data: Data) {
        continuation.yield(data)
    }

    func close() async {
        continuation.finish()
        worker?.cancel()
        session.invalidateAndCancel()
        await worker?.value
        worker = nil
    }

    private func send(_ stream: AsyncStream<Data>) async {
        var socket: URLSessionWebSocketTask?
        var keepAlive: Task<Void, Never>?
        var receiver: Task<Void, Never>?

        for await data in stream {
            guard !Task.isCancelled else { break }

            while !Task.isCancelled {
                if socket == nil {
                    let newSocket = session.webSocketTask(with: url)
                    newSocket.resume()
                    socket = newSocket
                    keepAlive = Self.startKeepAlive(for: newSocket)
                    receiver = Self.startReceiver(for: newSocket)
                }

                do {
                    try await socket?.send(.data(data))
                    break
                } catch {
                    Self.logger.warning(
                        "wake audio stream reconnecting: \(String(describing: error), privacy: .public)"
                    )
                    keepAlive?.cancel()
                    keepAlive = nil
                    receiver?.cancel()
                    receiver = nil
                    socket?.cancel(with: .abnormalClosure, reason: nil)
                    socket = nil
                    try? await Task.sleep(nanoseconds: 250_000_000)
                }
            }
        }

        keepAlive?.cancel()
        receiver?.cancel()
        socket?.cancel(with: .goingAway, reason: nil)
    }

    /// Keep an outstanding receive active so Foundation continuously processes
    /// inbound control frames, including the pong responses to our keepalives.
    private static func startReceiver(
        for socket: URLSessionWebSocketTask
    ) -> Task<Void, Never> {
        Task {
            while !Task.isCancelled {
                do {
                    _ = try await socket.receive()
                } catch {
                    guard !Task.isCancelled else { return }
                    Self.logger.warning(
                        "wake audio receive failed: \(String(describing: error), privacy: .public)"
                    )
                    socket.cancel(with: .abnormalClosure, reason: nil)
                    return
                }
            }
        }
    }

    private static func startKeepAlive(
        for socket: URLSessionWebSocketTask
    ) -> Task<Void, Never> {
        Task {
            while !Task.isCancelled {
                do {
                    try await Task.sleep(nanoseconds: 15_000_000_000)
                } catch {
                    return
                }
                socket.sendPing { error in
                    if error != nil {
                        socket.cancel(with: .abnormalClosure, reason: nil)
                    }
                }
            }
        }
    }
}
