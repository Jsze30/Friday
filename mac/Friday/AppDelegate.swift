import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var menuBar: MenuBarController?
    private var hud: HUDPanelController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Friday remains available in both the Dock and menu bar.
        NSApp.setActivationPolicy(.regular)

        menuBar = MenuBarController()
        hud = HUDPanelController()
        let isHUDPreview = CommandLine.arguments.contains("--preview-hud")
        if isHUDPreview {
            AppState.shared.set(.sleeping)
        } else {
            BootCoordinator.shared.start()
        }

        if isHUDPreview {
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                AppState.shared.runHUDPreview()
            }
        }
        if CommandLine.arguments.contains("--preview-memories") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                MemoryManagerController.shared.show()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Synchronously kill the child Python service. Must not bounce through
        // the @MainActor here - main is blocked, so any `Task { ... }` would
        // deadlock and the helper would survive (orange mic indicator).
        LocalServiceProcess.shared.stop()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}
