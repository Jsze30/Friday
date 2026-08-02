import AppKit
import Combine
import QuartzCore
import SwiftUI

@MainActor
final class HUDPanelController {
    private let panel: HUDPanel
    private var cancellables = Set<AnyCancellable>()
    private var hideTask: Task<Void, Never>?

    init() {
        panel = HUDPanel(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 110),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.contentView = NSHostingView(rootView: HUDView(model: AppState.shared))
        panel.isReleasedWhenClosed = false
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isMovable = false
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [
            .canJoinAllSpaces,
            .fullScreenAuxiliary,
            .transient,
            .ignoresCycle,
        ]
        panel.alphaValue = 0

        AppState.shared.$revision
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.render()
            }
            .store(in: &cancellables)
    }

    private func render() {
        let model = AppState.shared
        if model.shouldShowHUD {
            hideTask?.cancel()
            resizeAndPosition(height: model.preferredHUDHeight)
            if !panel.isVisible {
                panel.orderFrontRegardless()
            }
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.18
                context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                panel.animator().alphaValue = 1
            }
            return
        }

        hideTask?.cancel()
        hideTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .milliseconds(120))
            guard let self, !Task.isCancelled else { return }
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.3
                context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                self.panel.animator().alphaValue = 0
            }, completionHandler: { [weak panel] in
                Task { @MainActor in panel?.orderOut(nil) }
            })
        }
    }

    private func resizeAndPosition(height: CGFloat) {
        let oldFrame = panel.frame
        let screen = activeScreen()
        let width: CGFloat = 440
        let x = screen.visibleFrame.midX - width / 2
        let y = screen.visibleFrame.maxY - height - 16
        let frame = NSRect(x: x, y: y, width: width, height: height)
        if oldFrame.equalTo(frame) { return }
        panel.setFrame(frame, display: true, animate: panel.isVisible)
    }

    private func activeScreen() -> NSScreen {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) })
            ?? NSScreen.main
            ?? NSScreen.screens[0]
    }
}

private final class HUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}
