import CoreLocation
import Foundation

@MainActor
final class LocationProvider: NSObject, @preconcurrency CLLocationManagerDelegate {
    static let shared = LocationProvider()

    private let manager = CLLocationManager()
    private let geocoder = CLGeocoder()
    private var latestLocation: CLLocation?
    private var latestPlacemark: CLPlacemark?
    private var latestError: String?

    var onLocationUpdate: ((String) -> Void)?

    private override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    }

    func start() {
        guard CLLocationManager.locationServicesEnabled() else {
            latestError = "Location Services are disabled."
            notify()
            return
        }

        switch manager.authorizationStatus {
        case .notDetermined:
            manager.requestWhenInUseAuthorization()
        case .authorizedAlways, .authorizedWhenInUse:
            manager.requestLocation()
        case .denied:
            latestError = "Location permission was denied."
            notify()
        case .restricted:
            latestError = "Location access is restricted."
            notify()
        @unknown default:
            latestError = "Location authorization is unavailable."
            notify()
        }
    }

    func currentLocationJSON() -> String {
        Self.encode(currentLocationObject())
    }

    func currentLocationObject() -> [String: Any] {
        if let latestLocation,
           Date().timeIntervalSince(latestLocation.timestamp) > 300,
           isAuthorized {
            manager.requestLocation()
        }
        return snapshot()
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            latestError = nil
            manager.requestLocation()
        case .denied:
            latestError = "Location permission was denied."
            notify()
        case .restricted:
            latestError = "Location access is restricted."
            notify()
        case .notDetermined:
            break
        @unknown default:
            latestError = "Location authorization is unavailable."
            notify()
        }
    }

    func locationManager(
        _ manager: CLLocationManager,
        didUpdateLocations locations: [CLLocation]
    ) {
        guard let location = locations.last else { return }
        latestLocation = location
        latestPlacemark = nil
        latestError = nil
        notify()
        reverseGeocode(location)
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        latestError = error.localizedDescription
        notify()
    }

    private var isAuthorized: Bool {
        switch manager.authorizationStatus {
        case .authorizedAlways, .authorizedWhenInUse:
            return true
        default:
            return false
        }
    }

    private func notify() {
        onLocationUpdate?(snapshotJSON())
    }

    private func snapshotJSON() -> String {
        Self.encode(snapshot())
    }

    private func snapshot() -> [String: Any] {
        var snapshot: [String: Any] = [
            "status": statusName,
            "timezone": TimeZone.current.identifier,
        ]

        if let location = latestLocation, isAuthorized {
            snapshot["latitude"] = location.coordinate.latitude
            snapshot["longitude"] = location.coordinate.longitude
            snapshot["horizontalAccuracyMeters"] = location.horizontalAccuracy
            snapshot["timestamp"] = ISO8601DateFormatter().string(from: location.timestamp)
        }
        if let placemark = latestPlacemark {
            if let city = placemark.locality ?? placemark.subAdministrativeArea {
                snapshot["city"] = city
            }
            if let region = placemark.administrativeArea {
                snapshot["region"] = region
            }
            if let country = placemark.country {
                snapshot["country"] = country
            }
            if let countryCode = placemark.isoCountryCode {
                snapshot["countryCode"] = countryCode
            }
            if let postalCode = placemark.postalCode {
                snapshot["postalCode"] = postalCode
            }
            let place = [
                placemark.locality,
                placemark.administrativeArea,
                placemark.country,
            ]
                .compactMap { $0 }
                .filter { !$0.isEmpty }
                .joined(separator: ", ")
            if !place.isEmpty {
                snapshot["place"] = place
            }
        }
        if let latestError {
            snapshot["error"] = latestError
        }
        return snapshot
    }

    private static func encode(_ snapshot: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(snapshot),
              let data = try? JSONSerialization.data(
                withJSONObject: snapshot,
                options: [.sortedKeys]
              ) else {
            return #"{"status":"unavailable"}"#
        }
        return String(data: data, encoding: .utf8) ?? #"{"status":"unavailable"}"#
    }

    private func reverseGeocode(_ location: CLLocation) {
        geocoder.cancelGeocode()
        geocoder.reverseGeocodeLocation(location) { [weak self] placemarks, error in
            Task { @MainActor in
                guard let self else { return }
                if let placemark = placemarks?.first {
                    self.latestPlacemark = placemark
                    self.latestError = nil
                    self.notify()
                } else if let error {
                    self.latestError = "Location found, but place lookup failed: \(error.localizedDescription)"
                    self.notify()
                }
            }
        }
    }

    private var statusName: String {
        if latestLocation != nil, isAuthorized {
            return "available"
        }
        switch manager.authorizationStatus {
        case .notDetermined:
            return "notDetermined"
        case .authorizedAlways, .authorizedWhenInUse:
            return "acquiring"
        case .denied:
            return "denied"
        case .restricted:
            return "restricted"
        @unknown default:
            return "unavailable"
        }
    }
}
