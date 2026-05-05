import Foundation

enum AssistantState: String, Sendable {
    case disconnected
    case sleeping
    case wakeDetected
    case listening
    case thinking
    case speaking
    case followupWindow
    case error
}

@MainActor
final class AppState: ObservableObject {
    static let shared = AppState()

    @Published private(set) var state: AssistantState = .disconnected
    @Published private(set) var lastError: String?

    var onChange: ((AssistantState) -> Void)?

    func set(_ new: AssistantState, error: String? = nil) {
        if new == state && error == lastError { return }
        state = new
        lastError = error
        onChange?(new)
    }
}
