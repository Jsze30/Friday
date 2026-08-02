import AppKit
import SwiftUI

@MainActor
final class MemoryManagerController: NSObject, NSWindowDelegate {
    static let shared = MemoryManagerController()

    private var window: NSWindow?

    func show() {
        NSApp.activate(ignoringOtherApps: true)
        if let window {
            window.makeKeyAndOrderFront(nil)
            window.orderFrontRegardless()
            return
        }
        let created = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 720, height: 520),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        created.title = "Friday's Memory"
        created.minSize = NSSize(width: 680, height: 480)
        created.contentView = NSHostingView(rootView: MemoryManagerView())
        created.center()
        created.delegate = self
        created.isReleasedWhenClosed = false
        window = created
        created.makeKeyAndOrderFront(nil)
        created.orderFrontRegardless()
    }

    func windowWillClose(_ notification: Notification) {
        window = nil
    }
}
