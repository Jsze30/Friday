import AppKit
import ApplicationServices
import Foundation

@MainActor
final class WorkingContextProvider {
    static let shared = WorkingContextProvider()

    private init() {}

    func snapshot() -> [String: Any] {
        var result: [String: Any] = [
            "capturedAt": ISO8601DateFormatter().string(from: Date()),
        ]

        if let application = NSWorkspace.shared.frontmostApplication {
            result["currentApplication"] = [
                "name": application.localizedName
                    ?? application.bundleIdentifier
                    ?? "Unknown",
                "bundleId": application.bundleIdentifier ?? "",
                "processId": Int(application.processIdentifier),
            ]
            addAccessibilityContext(for: application, to: &result)
        }

        let events = CalendarContextProvider.shared.upcomingEvents()
        if !events.isEmpty {
            result["upcomingCalendarEvents"] = events
        }
        return result
    }

    private func addAccessibilityContext(
        for application: NSRunningApplication,
        to result: inout [String: Any]
    ) {
        guard AXIsProcessTrusted() else {
            result["accessibilityAvailable"] = false
            return
        }
        result["accessibilityAvailable"] = true

        let appElement = AXUIElementCreateApplication(application.processIdentifier)
        guard let window = elementAttribute(
            appElement,
            kAXFocusedWindowAttribute
        ) else { return }

        if let title = stringAttribute(window, kAXTitleAttribute), !title.isEmpty {
            result["currentWindow"] = title
        }

        if let document = stringAttribute(window, kAXDocumentAttribute),
           !document.isEmpty {
            result["currentDocument"] = document
            if document.hasPrefix("http://") || document.hasPrefix("https://") {
                result["currentURL"] = document
            }
        }

        if result["currentDocument"] == nil,
           let focused = elementAttribute(appElement, kAXFocusedUIElementAttribute),
           let document = stringAttribute(focused, kAXDocumentAttribute),
           !document.isEmpty {
            result["currentDocument"] = document
            if document.hasPrefix("http://") || document.hasPrefix("https://") {
                result["currentURL"] = document
            }
        }
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            element,
            name as CFString,
            &value
        ) == .success else { return nil }
        return value
    }

    private func stringAttribute(
        _ element: AXUIElement,
        _ name: String
    ) -> String? {
        attribute(element, name) as? String
    }

    private func elementAttribute(
        _ element: AXUIElement,
        _ name: String
    ) -> AXUIElement? {
        guard let value = attribute(element, name),
              CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return unsafeBitCast(value, to: AXUIElement.self)
    }
}
