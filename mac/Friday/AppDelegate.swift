import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var menuBar: MenuBarController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Menu-bar-only; LSUIElement=true in Info.plist also enforces this.
        NSApp.setActivationPolicy(.accessory)

        menuBar = MenuBarController()
        BootCoordinator.shared.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Synchronously kill the child Python service. Must not bounce through
        // the @MainActor here — main is blocked, so any `Task { ... }` would
        // deadlock and the helper would survive (orange mic indicator).
        LocalServiceProcess.shared.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
