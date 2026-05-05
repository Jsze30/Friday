import AppKit
import Combine

@MainActor
final class MenuBarController {
    private let item: NSStatusItem
    private let statusMenuItem: NSMenuItem
    private var cancellables = Set<AnyCancellable>()

    init() {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusMenuItem = NSMenuItem(title: "Status: …", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        configureMenu()
        render(state: AppState.shared.state)

        AppState.shared.$state
            .receive(on: RunLoop.main)
            .sink { [weak self] s in self?.render(state: s) }
            .store(in: &cancellables)
    }

    private func configureMenu() {
        let menu = NSMenu()
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Friday", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        item.menu = menu
    }

    private func render(state: AssistantState) {
        if let button = item.button {
            button.image = icon(for: state)
            button.image?.isTemplate = true
            button.toolTip = "Friday — \(state.rawValue)"
        }
        let label = AppState.shared.lastError.map { "Error: \($0)" } ?? "Status: \(state.rawValue)"
        statusMenuItem.title = label
    }

    private func icon(for state: AssistantState) -> NSImage? {
        let name: String
        switch state {
        case .disconnected:    name = "moon.zzz"
        case .sleeping:        name = "moon"
        case .wakeDetected:    name = "ear"
        case .listening:       name = "waveform"
        case .thinking:        name = "ellipsis.circle"
        case .speaking:        name = "speaker.wave.2"
        case .followupWindow:  name = "waveform.badge.plus"
        case .error:           name = "exclamationmark.triangle"
        }
        return NSImage(systemSymbolName: name, accessibilityDescription: state.rawValue)
    }
}
