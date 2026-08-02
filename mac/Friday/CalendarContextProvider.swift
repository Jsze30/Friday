import EventKit
import Foundation

@MainActor
final class CalendarContextProvider {
    static let shared = CalendarContextProvider()

    private let store = EKEventStore()
    private var requestedThisLaunch = false

    private init() {}

    func prepareAccess() async {
        guard EKEventStore.authorizationStatus(for: .event) == .notDetermined,
              !requestedThisLaunch else { return }
        requestedThisLaunch = true
        do {
            _ = try await store.requestFullAccessToEvents()
        } catch {
            NSLog("[Friday] calendar access request failed: \(error)")
        }
    }

    func upcomingEvents(limit: Int = 5) -> [[String: Any]] {
        let status = EKEventStore.authorizationStatus(for: .event)
        guard status == .fullAccess else { return [] }

        let start = Date()
        let end = start.addingTimeInterval(24 * 60 * 60)
        let predicate = store.predicateForEvents(
            withStart: start,
            end: end,
            calendars: nil
        )
        return store.events(matching: predicate)
            .filter { !$0.isAllDay || $0.endDate >= start }
            .prefix(max(1, min(limit, 10)))
            .map { event in
                var value: [String: Any] = [
                    "id": event.eventIdentifier ?? "",
                    "title": event.title ?? "Untitled event",
                    "start": Self.iso8601.string(from: event.startDate),
                    "end": Self.iso8601.string(from: event.endDate),
                    "allDay": event.isAllDay,
                    "calendar": event.calendar.title,
                ]
                if let location = event.location, !location.isEmpty {
                    value["location"] = location
                }
                let eventAttendees: [EKParticipant] = event.attendees.flatMap { $0 } ?? []
                let attendees = eventAttendees.compactMap { attendee in
                    attendee.name ?? attendee.url.absoluteString
                }
                if !attendees.isEmpty {
                    value["attendees"] = attendees
                }
                return value
            }
    }

    private static let iso8601: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}
