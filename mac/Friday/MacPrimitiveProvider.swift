import AppKit
import ApplicationServices
import CoreAudio
import Foundation

@MainActor
final class MacPrimitiveProvider {
    static let shared = MacPrimitiveProvider()

    private struct AudioState {
        let volume: Int
        let muted: Bool
    }

    private struct CoreAudioFailure: LocalizedError {
        let operation: String
        let status: OSStatus?

        var errorDescription: String? {
            if let status {
                return "\(operation) failed with Core Audio status \(status)"
            }
            return "\(operation) is not supported by the default output device"
        }
    }

    private let maximumRPCBytes = 14_000
    private var elementCache: [String: AXUIElement] = [:]

    private init() {}

    let toolNames: Set<String> = [
        "list_apps",
        "open_app",
        "open_path",
        "open_url",
        "quit_app",
        "get_volume",
        "set_volume",
        "mute_audio",
        "inspect_ui",
        "interact_ui",
    ]

    var manifests: [[String: Any]] {
        [
            [
                "name": "list_apps",
                "description": """
                List installed or currently running Mac applications. Use this to \
                discover the exact application name or bundle identifier instead \
                of guessing an app-specific integration.
                """,
                "permission": "read_only",
                "parameters": [
                    [
                        "name": "running_only",
                        "type": "boolean",
                        "description": "Return only running apps. Defaults to false.",
                        "required": false,
                    ]
                ],
                "actions": [
                    [
                        "id": "system.list_apps",
                        "description": "List installed or running Mac applications.",
                        "parameters": [
                            [
                                "name": "running_only",
                                "type": "boolean",
                                "description": "Return only running apps.",
                                "required": false,
                            ]
                        ],
                        "routes": [
                            [
                                "pattern": #"list\s+(?:the\s+)?running\s+apps"#,
                                "arguments": ["running_only": true],
                            ],
                            [
                                "pattern": #"list\s+(?:the\s+)?apps"#,
                                "arguments": ["running_only": false],
                            ],
                        ],
                        "latencyMs": 100,
                        "priority": 110,
                    ]
                ],
            ],
            [
                "name": "open_app",
                "description": """
                Launch or bring a Mac application to the front using its name or \
                bundle identifier. This works for any installed application.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "app",
                        "type": "string",
                        "description": "Application name such as Spotify or bundle identifier.",
                        "required": true,
                    ]
                ],
                "actions": [
                    [
                        "id": "system.open_app",
                        "description": "Launch or focus a named Mac application.",
                        "routes": [
                            [
                                "pattern": #"(?:open|launch|focus|activate)\s+(?:the\s+)?(?P<app>[\w .'-]+)"#,
                            ]
                        ],
                        "latencyMs": 150,
                        "priority": 100,
                    ]
                ],
            ],
            [
                "name": "open_url",
                "description": """
                Open an HTTP or HTTPS URL in a specific installed browser using \
                native macOS APIs. The browser defaults to Arc. Use this instead \
                of Accessibility or AppleScript for opening web pages.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "url",
                        "type": "string",
                        "description": "Web URL or domain to open, such as https://youtube.com.",
                        "required": true,
                    ],
                    [
                        "name": "browser",
                        "type": "string",
                        "description": "Browser name or bundle identifier. Defaults to Arc.",
                        "required": false,
                    ],
                ],
                "actions": [
                    [
                        "id": "system.open_url",
                        "description": "Open a web URL in a browser.",
                        "routes": [
                            [
                                "pattern": #"(?:open|visit|go\s+to)\s+(?P<url>(?:https?://|www\.)\S+?)(?:\s+(?:in|with|using)\s+(?P<browser>[\w .'-]+))?"#,
                            ]
                        ],
                        "latencyMs": 150,
                        "priority": 140,
                    ]
                ],
            ],
            [
                "name": "open_path",
                "description": """
                Open a local file or folder with its default Mac application. \
                Use an optional application name to open it in a specific app.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "path",
                        "type": "string",
                        "description": "Absolute or home-relative file or folder path.",
                        "required": true,
                    ],
                    [
                        "name": "application",
                        "type": "string",
                        "description": "Optional application name or bundle identifier.",
                        "required": false,
                    ],
                ],
                "actions": [
                    [
                        "id": "system.open_path",
                        "description": "Open a local file, folder, or project.",
                        "routes": [
                            [
                                "pattern": #"(?:open|show)\s+(?P<path>(?:/|~/).+?)(?:\s+(?:in|with|using)\s+(?P<application>[\w .'-]+))?"#,
                            ]
                        ],
                        "latencyMs": 150,
                        "priority": 150,
                    ]
                ],
            ],
            [
                "name": "quit_app",
                "description": """
                Gracefully ask one running Mac application to quit immediately. \
                This does not force quit the application.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "app",
                        "type": "string",
                        "description": "Running application name or bundle identifier.",
                        "required": true,
                    ]
                ],
                "actions": [
                    [
                        "id": "system.quit_app",
                        "description": "Gracefully quit a named Mac application.",
                        "routes": [
                            [
                                "pattern": #"(?:quit|close)\s+(?:the\s+)?(?P<app>[\w .'-]+)"#,
                            ]
                        ],
                        "latencyMs": 150,
                        "priority": 110,
                    ]
                ],
            ],
            [
                "name": "get_volume",
                "description": """
                Read the current default output volume and mute state using \
                native Core Audio.
                """,
                "permission": "read_only",
                "parameters": [],
                "actions": [
                    [
                        "id": "system.get_volume",
                        "description": "Read the Mac output volume and mute state.",
                        "routes": [
                            [
                                "pattern": #"(?:what(?:'s|\s+is)\s+)?(?:the\s+)?(?:mac\s+)?volume(?:\s+level)?"#,
                            ]
                        ],
                        "latencyMs": 50,
                        "priority": 120,
                    ]
                ],
            ],
            [
                "name": "set_volume",
                "description": """
                Set the default Mac output volume from 0 to 100 using native \
                Core Audio. A positive volume also unmutes the output.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "volume",
                        "type": "integer",
                        "description": "Exact output volume from 0 to 100.",
                        "required": true,
                    ]
                ],
                "actions": [
                    [
                        "id": "system.set_volume",
                        "description": "Set the Mac output volume.",
                        "parameters": [
                            [
                                "name": "volume",
                                "type": "integer",
                                "description": "Exact output volume from 0 to 100.",
                                "required": true,
                                "minimum": 0,
                                "maximum": 100,
                            ]
                        ],
                        "routes": [
                            [
                                "pattern": #"(?:set|change)\s+(?:the\s+)?(?:mac\s+)?volume\s+(?:to\s+)?(?P<volume>\d{1,3})(?:\s*%)?"#,
                            ]
                        ],
                        "latencyMs": 50,
                        "priority": 120,
                    ]
                ],
            ],
            [
                "name": "mute_audio",
                "description": """
                Mute or unmute the default Mac output device using native Core \
                Audio.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "muted",
                        "type": "boolean",
                        "description": "True to mute or false to unmute.",
                        "required": true,
                    ]
                ],
                "actions": [
                    [
                        "id": "system.mute",
                        "description": "Mute or unmute Mac audio.",
                        "parameters": [
                            [
                                "name": "muted",
                                "type": "boolean",
                                "description": "True to mute or false to unmute.",
                                "required": true,
                            ]
                        ],
                        "routes": [
                            [
                                "pattern": #"mute(?:\s+(?:the\s+)?(?:mac|audio|sound|volume))?"#,
                                "arguments": ["muted": true],
                            ],
                            [
                                "pattern": #"unmute(?:\s+(?:the\s+)?(?:mac|audio|sound|volume))?"#,
                                "arguments": ["muted": false],
                            ],
                        ],
                        "latencyMs": 50,
                        "priority": 120,
                    ]
                ],
            ],
            [
                "name": "inspect_ui",
                "description": """
                Inspect the accessible controls in the frontmost app or a named \
                running app. Returns generic element IDs, roles, labels, values, \
                and supported actions. Call this before interact_ui. This is how \
                Friday discovers controls in Spotify, VS Code, Arc, Finder, and \
                other Mac apps without custom integrations.
                """,
                "permission": "read_only",
                "parameters": [
                    [
                        "name": "app",
                        "type": "string",
                        "description": "Optional app name or bundle identifier. Defaults to frontmost.",
                        "required": false,
                    ],
                    [
                        "name": "max_elements",
                        "type": "integer",
                        "description": "Maximum controls to return from 1 to 60. Defaults to 40.",
                        "required": false,
                    ],
                ],
            ],
            [
                "name": "interact_ui",
                "description": """
                Perform one generic Accessibility action on an element returned by \
                inspect_ui. Use an advertised AX action such as AXPress, or use \
                set_value, type_text, or focus. Execute immediately when requested.
                """,
                "permission": "low_risk_write",
                "parameters": [
                    [
                        "name": "element_id",
                        "type": "string",
                        "description": "Exact element ID returned by the latest inspect_ui call.",
                        "required": true,
                    ],
                    [
                        "name": "action",
                        "type": "string",
                        "description": "Advertised AX action, set_value, type_text, or focus.",
                        "required": true,
                    ],
                    [
                        "name": "value",
                        "type": "string",
                        "description": "Text used by set_value or type_text.",
                        "required": false,
                    ],
                ],
            ],
        ]
    }

    func requestAccessibilityPermission() {
        let promptKey = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([promptKey: true] as CFDictionary)
    }

    func execute(jsonPayload: String) async -> String {
        guard let payload = decodeObject(jsonPayload),
              let tool = payload["tool"] as? String else {
            return envelope(ok: false, error: "tool is required")
        }
        let arguments = payload["arguments"] as? [String: Any] ?? [:]
        NSLog("[Friday/mac-tools] tool_call name=%@", tool)

        switch tool {
        case "list_apps":
            return listApps(arguments: arguments)
        case "open_app":
            return openApp(arguments: arguments)
        case "open_path":
            return await openPath(arguments: arguments)
        case "open_url":
            return await openURL(arguments: arguments)
        case "quit_app":
            return await quitApp(arguments: arguments)
        case "get_volume":
            return getVolume()
        case "set_volume":
            return setVolume(arguments: arguments)
        case "mute_audio":
            return muteAudio(arguments: arguments)
        case "inspect_ui":
            return inspectUI(arguments: arguments)
        case "interact_ui":
            return interactUI(arguments: arguments)
        default:
            return envelope(ok: false, error: "unknown Mac primitive: \(tool)")
        }
    }

    private func listApps(arguments: [String: Any]) -> String {
        let runningOnly = arguments["running_only"] as? Bool ?? false
        let running = NSWorkspace.shared.runningApplications.filter {
            $0.activationPolicy == .regular
        }
        var runningByBundleID: [String: NSRunningApplication] = [:]
        for app in running {
            if let bundleID = app.bundleIdentifier {
                runningByBundleID[bundleID] = app
            }
        }

        var apps: [[String: Any]] = []
        var seenBundleIDs: Set<String> = []

        for app in running {
            let name = app.localizedName ?? app.bundleIdentifier ?? "Unknown"
            let bundleID = app.bundleIdentifier ?? ""
            apps.append([
                "name": name,
                "bundleId": bundleID,
                "running": true,
                "frontmost": app.isActive,
            ])
            if !bundleID.isEmpty {
                seenBundleIDs.insert(bundleID)
            }
        }

        if !runningOnly {
            for url in installedApplicationURLs() {
                guard let bundle = Bundle(url: url) else { continue }
                let bundleID = bundle.bundleIdentifier ?? ""
                if !bundleID.isEmpty, seenBundleIDs.contains(bundleID) {
                    continue
                }
                let displayName =
                    bundle.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
                    ?? bundle.object(forInfoDictionaryKey: "CFBundleName") as? String
                    ?? url.deletingPathExtension().lastPathComponent
                apps.append([
                    "name": displayName,
                    "bundleId": bundleID,
                    "running": runningByBundleID[bundleID] != nil,
                    "frontmost": false,
                ])
                if !bundleID.isEmpty {
                    seenBundleIDs.insert(bundleID)
                }
                if apps.count >= 60 {
                    break
                }
            }
        }

        apps.sort {
            (($0["name"] as? String) ?? "").localizedCaseInsensitiveCompare(
                ($1["name"] as? String) ?? ""
            ) == .orderedAscending
        }
        let originalCount = apps.count
        var response = listAppsEnvelope(apps, truncated: originalCount >= 60)
        while response.utf8.count > maximumRPCBytes, apps.count > 1 {
            apps.removeLast()
            response = listAppsEnvelope(apps, truncated: true)
        }
        return response
    }

    private func openApp(arguments: [String: Any]) -> String {
        guard let requested = arguments["app"] as? String,
              !requested.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return envelope(ok: false, error: "app is required")
        }

        if let running = findRunningApplication(requested) {
            let activated = running.activate()
            return envelope(
                ok: activated,
                spoken: activated ? "Brought \(running.localizedName ?? requested) to the front." : nil,
                data: [
                    "name": running.localizedName ?? requested,
                    "bundleId": running.bundleIdentifier ?? "",
                    "running": true,
                ],
                error: activated ? nil : "could not activate \(requested)"
            )
        }

        guard let url = findInstalledApplication(requested) else {
            return envelope(
                spoken: "I could not find \(requested).",
                data: ["error": "app_not_found", "app": requested]
            )
        }
        let opened = NSWorkspace.shared.open(url)
        return envelope(
            ok: opened,
            spoken: opened ? "Opened \(url.deletingPathExtension().lastPathComponent)." : nil,
            data: [
                "path": url.path,
                "bundleId": Bundle(url: url)?.bundleIdentifier ?? "",
            ],
            error: opened ? nil : "could not open \(requested)"
        )
    }

    private func openPath(arguments: [String: Any]) async -> String {
        guard let rawPath = arguments["path"] as? String,
              !rawPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return envelope(ok: false, error: "path is required")
        }

        let expandedPath = (rawPath as NSString).expandingTildeInPath
        let fileURL = URL(fileURLWithPath: expandedPath).standardizedFileURL
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(
            atPath: fileURL.path,
            isDirectory: &isDirectory
        ) else {
            return envelope(
                spoken: "I could not find that file or folder.",
                data: ["error": "path_not_found", "path": fileURL.path]
            )
        }

        let requestedApplication =
            (arguments["application"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
        if let requestedApplication, !requestedApplication.isEmpty {
            guard let applicationURL = findInstalledApplication(requestedApplication) else {
                return envelope(
                    spoken: "I could not find \(requestedApplication).",
                    data: [
                        "error": "app_not_found",
                        "application": requestedApplication,
                        "path": fileURL.path,
                    ]
                )
            }

            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            do {
                let application = try await NSWorkspace.shared.open(
                    [fileURL],
                    withApplicationAt: applicationURL,
                    configuration: configuration
                )
                let applicationName =
                    application.localizedName
                    ?? applicationURL.deletingPathExtension().lastPathComponent
                return envelope(
                    spoken: "Opened \(fileURL.lastPathComponent) in \(applicationName).",
                    data: [
                        "path": fileURL.path,
                        "isDirectory": isDirectory.boolValue,
                        "application": applicationName,
                        "bundleId": application.bundleIdentifier ?? "",
                    ]
                )
            } catch {
                return envelope(
                    ok: false,
                    error: "could not open \(fileURL.path) in \(requestedApplication): \(error.localizedDescription)"
                )
            }
        }

        let opened = NSWorkspace.shared.open(fileURL)
        return envelope(
            ok: opened,
            spoken: opened ? "Opened \(fileURL.lastPathComponent)." : nil,
            data: [
                "path": fileURL.path,
                "isDirectory": isDirectory.boolValue,
            ],
            error: opened ? nil : "could not open \(fileURL.path)"
        )
    }

    private func openURL(arguments: [String: Any]) async -> String {
        guard let rawURL = arguments["url"] as? String,
              !rawURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return envelope(ok: false, error: "url is required")
        }
        guard let webURL = normalizedWebURL(rawURL) else {
            return envelope(
                spoken: "I can only open valid HTTP or HTTPS web addresses.",
                data: ["error": "invalid_url", "url": rawURL]
            )
        }

        let requestedBrowser =
            (arguments["browser"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            ?? "Arc"
        let browser = requestedBrowser.isEmpty ? "Arc" : requestedBrowser
        guard let browserURL = findInstalledApplication(browser) else {
            return envelope(
                spoken: "I could not find \(browser).",
                data: ["error": "browser_not_found", "browser": browser]
            )
        }

        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        do {
            let application = try await NSWorkspace.shared.open(
                [webURL],
                withApplicationAt: browserURL,
                configuration: configuration
            )
            let browserName =
                application.localizedName
                ?? browserURL.deletingPathExtension().lastPathComponent
            return envelope(
                spoken: "Opened \(webURL.host() ?? webURL.absoluteString) in \(browserName).",
                data: [
                    "url": webURL.absoluteString,
                    "browser": browserName,
                    "bundleId": application.bundleIdentifier ?? "",
                ]
            )
        } catch {
            return envelope(
                ok: false,
                error: "could not open \(webURL.absoluteString) in \(browser): \(error.localizedDescription)"
            )
        }
    }

    private func quitApp(arguments: [String: Any]) async -> String {
        guard let requested = arguments["app"] as? String,
              !requested.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return envelope(ok: false, error: "app is required")
        }
        guard let running = findRunningApplication(requested) else {
            return envelope(
                spoken: "\(requested) is not running.",
                data: ["error": "app_not_running", "app": requested]
            )
        }
        let appName = running.localizedName ?? requested
        guard running.terminate() else {
            return envelope(
                ok: false,
                error: "\(appName) rejected the quit request"
            )
        }
        for _ in 0..<10 where !running.isTerminated {
            try? await Task.sleep(for: .milliseconds(100))
        }
        return envelope(
            spoken: running.isTerminated
                ? "Quit \(appName)."
                : "Asked \(appName) to quit.",
            data: [
                "name": appName,
                "bundleId": running.bundleIdentifier ?? "",
                "terminated": running.isTerminated,
                "forceQuit": false,
            ]
        )
    }

    private func getVolume() -> String {
        do {
            let state = try readAudioState()
            return audioEnvelope(
                spoken: state.muted
                    ? "The Mac is muted at \(state.volume) percent."
                    : "The Mac volume is \(state.volume) percent.",
                state: state
            )
        } catch {
            return audioErrorEnvelope(error)
        }
    }

    private func setVolume(arguments: [String: Any]) -> String {
        guard let number = arguments["volume"] as? NSNumber else {
            return envelope(ok: false, error: "volume is required")
        }
        let requested = number.intValue
        guard (0...100).contains(requested) else {
            return envelope(ok: false, error: "volume must be from 0 to 100")
        }
        do {
            let device = try defaultOutputDevice()
            try setAudioFloat(
                Float32(requested) / 100,
                selector: kAudioDevicePropertyVolumeScalar,
                device: device
            )
            if requested > 0 {
                try? setAudioUInt32(
                    0,
                    selector: kAudioDevicePropertyMute,
                    device: device
                )
            }
            let state = try readAudioState(device: device)
            return audioEnvelope(
                spoken: "Set the Mac volume to \(state.volume) percent.",
                state: state
            )
        } catch {
            return audioErrorEnvelope(error)
        }
    }

    private func muteAudio(arguments: [String: Any]) -> String {
        guard let muted = arguments["muted"] as? Bool else {
            return envelope(ok: false, error: "muted is required")
        }
        do {
            let device = try defaultOutputDevice()
            try setAudioUInt32(
                muted ? 1 : 0,
                selector: kAudioDevicePropertyMute,
                device: device
            )
            let state = try readAudioState(device: device)
            return audioEnvelope(
                spoken: state.muted ? "Muted the Mac." : "Unmuted the Mac.",
                state: state
            )
        } catch {
            return audioErrorEnvelope(error)
        }
    }

    private func defaultOutputDevice() throws -> AudioDeviceID {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var device = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size,
            &device
        )
        guard status == noErr, device != kAudioObjectUnknown else {
            throw CoreAudioFailure(
                operation: "Reading the default output device",
                status: status
            )
        }
        return device
    }

    private func readAudioState() throws -> AudioState {
        try readAudioState(device: defaultOutputDevice())
    }

    private func readAudioState(device: AudioDeviceID) throws -> AudioState {
        let scalar = try readAudioFloat(
            selector: kAudioDevicePropertyVolumeScalar,
            device: device
        )
        let muted = (
            try? readAudioUInt32(
                selector: kAudioDevicePropertyMute,
                device: device
            )
        ) == 1
        return AudioState(
            volume: Int((max(0, min(1, scalar)) * 100).rounded()),
            muted: muted
        )
    }

    private func readAudioFloat(
        selector: AudioObjectPropertySelector,
        device: AudioDeviceID
    ) throws -> Float32 {
        var address = audioAddress(selector: selector)
        guard AudioObjectHasProperty(device, &address) else {
            throw CoreAudioFailure(
                operation: "Reading the output volume",
                status: nil
            )
        }
        var value = Float32(0)
        var size = UInt32(MemoryLayout<Float32>.size)
        let status = AudioObjectGetPropertyData(
            device,
            &address,
            0,
            nil,
            &size,
            &value
        )
        guard status == noErr else {
            throw CoreAudioFailure(
                operation: "Reading the output volume",
                status: status
            )
        }
        return value
    }

    private func readAudioUInt32(
        selector: AudioObjectPropertySelector,
        device: AudioDeviceID
    ) throws -> UInt32 {
        var address = audioAddress(selector: selector)
        guard AudioObjectHasProperty(device, &address) else {
            throw CoreAudioFailure(
                operation: "Reading the output mute state",
                status: nil
            )
        }
        var value = UInt32(0)
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(
            device,
            &address,
            0,
            nil,
            &size,
            &value
        )
        guard status == noErr else {
            throw CoreAudioFailure(
                operation: "Reading the output mute state",
                status: status
            )
        }
        return value
    }

    private func setAudioFloat(
        _ value: Float32,
        selector: AudioObjectPropertySelector,
        device: AudioDeviceID
    ) throws {
        var address = try settableAudioAddress(
            selector: selector,
            device: device,
            operation: "Setting the output volume"
        )
        var mutableValue = value
        let status = AudioObjectSetPropertyData(
            device,
            &address,
            0,
            nil,
            UInt32(MemoryLayout<Float32>.size),
            &mutableValue
        )
        guard status == noErr else {
            throw CoreAudioFailure(
                operation: "Setting the output volume",
                status: status
            )
        }
    }

    private func setAudioUInt32(
        _ value: UInt32,
        selector: AudioObjectPropertySelector,
        device: AudioDeviceID
    ) throws {
        var address = try settableAudioAddress(
            selector: selector,
            device: device,
            operation: "Setting the output mute state"
        )
        var mutableValue = value
        let status = AudioObjectSetPropertyData(
            device,
            &address,
            0,
            nil,
            UInt32(MemoryLayout<UInt32>.size),
            &mutableValue
        )
        guard status == noErr else {
            throw CoreAudioFailure(
                operation: "Setting the output mute state",
                status: status
            )
        }
    }

    private func settableAudioAddress(
        selector: AudioObjectPropertySelector,
        device: AudioDeviceID,
        operation: String
    ) throws -> AudioObjectPropertyAddress {
        var address = audioAddress(selector: selector)
        var settable = DarwinBoolean(false)
        let settableStatus = AudioObjectIsPropertySettable(
            device,
            &address,
            &settable
        )
        guard settableStatus == noErr, settable.boolValue else {
            throw CoreAudioFailure(
                operation: operation,
                status: settableStatus == noErr ? nil : settableStatus
            )
        }
        return address
    }

    private func audioAddress(
        selector: AudioObjectPropertySelector
    ) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeOutput,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    private func audioEnvelope(
        spoken: String,
        state: AudioState
    ) -> String {
        envelope(
            spoken: spoken,
            data: [
                "volume": state.volume,
                "muted": state.muted,
                "provider": "Core Audio",
            ]
        )
    }

    private func audioErrorEnvelope(_ error: Error) -> String {
        envelope(
            ok: false,
            spoken: "I could not control the Mac audio output.",
            data: [
                "error": "audio_control_failed",
                "detail": error.localizedDescription,
            ],
            error: error.localizedDescription
        )
    }

    private func inspectUI(arguments: [String: Any]) -> String {
        guard AXIsProcessTrusted() else {
            requestAccessibilityPermission()
            return envelope(
                spoken: """
                Friday needs Accessibility permission. Enable Friday in System \
                Settings, Privacy and Security, Accessibility, then try again.
                """,
                data: ["error": "accessibility_permission_required"]
            )
        }

        let requested = arguments["app"] as? String
        guard let app = requested.flatMap(findRunningApplication)
            ?? NSWorkspace.shared.frontmostApplication else {
            return envelope(
                spoken: "I could not find a running application to inspect.",
                data: ["error": "app_not_running"]
            )
        }
        let requestedLimit = arguments["max_elements"] as? Int ?? 40
        let limit = max(1, min(requestedLimit, 60))
        let root = AXUIElementCreateApplication(app.processIdentifier)
        elementCache.removeAll(keepingCapacity: true)

        var output: [[String: Any]] = []
        var stack: [(AXUIElement, Int)] = [(root, 0)]
        while let (element, depth) = stack.popLast(), output.count < limit {
            let elementID = "ui-\(output.count + 1)"
            elementCache[elementID] = element

            var item: [String: Any] = [
                "id": elementID,
                "depth": depth,
                "role": stringAttribute(element, kAXRoleAttribute) ?? "unknown",
            ]
            addAttribute("subrole", from: element, attribute: kAXSubroleAttribute, to: &item)
            addAttribute("title", from: element, attribute: kAXTitleAttribute, to: &item)
            addAttribute(
                "description",
                from: element,
                attribute: kAXDescriptionAttribute,
                to: &item
            )
            addAttribute("value", from: element, attribute: kAXValueAttribute, to: &item)
            addAttribute(
                "identifier",
                from: element,
                attribute: kAXIdentifierAttribute,
                to: &item
            )
            if let enabled = boolAttribute(element, kAXEnabledAttribute) {
                item["enabled"] = enabled
            }
            if let focused = boolAttribute(element, kAXFocusedAttribute) {
                item["focused"] = focused
            }
            let actions = actionNames(element)
            if !actions.isEmpty {
                item["actions"] = actions
            }
            output.append(item)

            if depth < 10 {
                let children = childrenOf(element)
                for child in children.reversed() {
                    stack.append((child, depth + 1))
                }
            }
        }

        let appName = app.localizedName ?? "the app"
        var wasTruncated = !stack.isEmpty
        var response = inspectUIEnvelope(
            appName: appName,
            bundleID: app.bundleIdentifier ?? "",
            elements: output,
            truncated: wasTruncated
        )
        while response.utf8.count > maximumRPCBytes, output.count > 1 {
            output.removeLast()
            wasTruncated = true
            response = inspectUIEnvelope(
                appName: appName,
                bundleID: app.bundleIdentifier ?? "",
                elements: output,
                truncated: wasTruncated
            )
        }
        return response
    }

    private func listAppsEnvelope(
        _ applications: [[String: Any]],
        truncated: Bool
    ) -> String {
        envelope(
            spoken: "Found \(applications.count) Mac applications.",
            data: [
                "applications": applications,
                "frontmost": NSWorkspace.shared.frontmostApplication?.localizedName ?? "",
                "truncated": truncated,
            ]
        )
    }

    private func inspectUIEnvelope(
        appName: String,
        bundleID: String,
        elements: [[String: Any]],
        truncated: Bool
    ) -> String {
        envelope(
            spoken: "Inspected \(elements.count) controls in \(appName).",
            data: [
                "app": appName,
                "bundleId": bundleID,
                "elements": elements,
                "truncated": truncated,
                "next": "Use an exact element id and advertised action with interact_ui.",
            ]
        )
    }

    private func interactUI(arguments: [String: Any]) -> String {
        guard AXIsProcessTrusted() else {
            return envelope(
                spoken: "Friday does not have Accessibility permission.",
                data: ["error": "accessibility_permission_required"]
            )
        }
        guard let elementID = arguments["element_id"] as? String,
              let element = elementCache[elementID] else {
            return envelope(
                spoken: "That UI element is no longer available. Inspect the app again.",
                data: ["error": "unknown_element"]
            )
        }
        guard let rawAction = arguments["action"] as? String else {
            return envelope(ok: false, error: "action is required")
        }
        let action = rawAction.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalized = action.lowercased()
        let result: AXError

        switch normalized {
        case "set_value", "type_text":
            guard let value = arguments["value"] as? String else {
                return envelope(ok: false, error: "value is required for \(action)")
            }
            result = AXUIElementSetAttributeValue(
                element,
                kAXValueAttribute as CFString,
                value as CFTypeRef
            )
        case "focus":
            result = AXUIElementSetAttributeValue(
                element,
                kAXFocusedAttribute as CFString,
                kCFBooleanTrue
            )
        default:
            result = AXUIElementPerformAction(
                element,
                accessibilityAction(action) as CFString
            )
        }

        guard result == .success else {
            return envelope(
                spoken: "The app rejected that UI action.",
                data: [
                    "error": "accessibility_action_failed",
                    "elementId": elementID,
                    "action": action,
                    "axError": result.rawValue,
                ]
            )
        }
        return envelope(
            spoken: "Performed \(action) on \(elementID).",
            data: [
                "elementId": elementID,
                "action": action,
                "success": true,
            ]
        )
    }

    private func installedApplicationURLs() -> [URL] {
        let roots = [
            URL(fileURLWithPath: "/Applications", isDirectory: true),
            URL(fileURLWithPath: "/System/Applications", isDirectory: true),
            FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Applications", isDirectory: true),
        ]
        var urls: [URL] = []
        for root in roots {
            guard let enumerator = FileManager.default.enumerator(
                at: root,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles, .skipsPackageDescendants]
            ) else { continue }
            for case let url as URL in enumerator where url.pathExtension == "app" {
                urls.append(url)
                if urls.count >= 300 {
                    return urls
                }
            }
        }
        return urls
    }

    private func findRunningApplication(_ requested: String) -> NSRunningApplication? {
        let needle = requested.trimmingCharacters(in: .whitespacesAndNewlines)
        return NSWorkspace.shared.runningApplications.first { app in
            app.bundleIdentifier?.caseInsensitiveCompare(needle) == .orderedSame
                || app.localizedName?.caseInsensitiveCompare(needle) == .orderedSame
                || app.localizedName?
                    .replacingOccurrences(of: " ", with: "")
                    .caseInsensitiveCompare(
                        needle.replacingOccurrences(of: " ", with: "")
                    ) == .orderedSame
        }
    }

    private func findInstalledApplication(_ requested: String) -> URL? {
        if let bundleURL = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: requested
        ) {
            return bundleURL
        }
        let needle = requested.trimmingCharacters(in: .whitespacesAndNewlines)
        return installedApplicationURLs().first { url in
            let bundle = Bundle(url: url)
            let displayName =
                bundle?.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
                ?? bundle?.object(forInfoDictionaryKey: "CFBundleName") as? String
                ?? url.deletingPathExtension().lastPathComponent
            return displayName.caseInsensitiveCompare(needle) == .orderedSame
                || url.deletingPathExtension().lastPathComponent
                    .caseInsensitiveCompare(needle) == .orderedSame
        }
    }

    private func normalizedWebURL(_ rawValue: String) -> URL? {
        let trimmed = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let candidate = trimmed.contains("://") ? trimmed : "https://\(trimmed)"
        guard let components = URLComponents(string: candidate),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host,
              !host.isEmpty else {
            return nil
        }
        return components.url
    }

    private func attribute(
        _ element: AXUIElement,
        _ name: String
    ) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(
            element,
            name as CFString,
            &value
        ) == .success else {
            return nil
        }
        return value
    }

    private func stringAttribute(
        _ element: AXUIElement,
        _ name: String
    ) -> String? {
        guard let value = attribute(element, name) else { return nil }
        let text: String?
        if let value = value as? String {
            text = value
        } else if let value = value as? NSNumber {
            text = value.stringValue
        } else {
            text = nil
        }
        guard let text else { return nil }
        let flattened = text.replacingOccurrences(of: "\n", with: " ")
        if flattened.count <= 160 {
            return flattened
        }
        return String(flattened.prefix(160)) + "..."
    }

    private func boolAttribute(
        _ element: AXUIElement,
        _ name: String
    ) -> Bool? {
        attribute(element, name) as? Bool
    }

    private func childrenOf(_ element: AXUIElement) -> [AXUIElement] {
        attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
    }

    private func actionNames(_ element: AXUIElement) -> [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(element, &names) == .success else {
            return []
        }
        return names as? [String] ?? []
    }

    private func addAttribute(
        _ key: String,
        from element: AXUIElement,
        attribute: String,
        to output: inout [String: Any]
    ) {
        if let value = stringAttribute(element, attribute), !value.isEmpty {
            output[key] = value
        }
    }

    private func accessibilityAction(_ action: String) -> String {
        if action.hasPrefix("AX") {
            return action
        }
        switch action.lowercased() {
        case "press": return kAXPressAction
        case "show_menu": return kAXShowMenuAction
        case "increment": return kAXIncrementAction
        case "decrement": return kAXDecrementAction
        case "confirm": return kAXConfirmAction
        case "cancel": return kAXCancelAction
        default: return action
        }
    }

    private func decodeObject(_ value: String) -> [String: Any]? {
        guard let data = value.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else {
            return nil
        }
        return object as? [String: Any]
    }

    private func envelope(
        ok: Bool = true,
        spoken: String? = nil,
        data: [String: Any]? = nil,
        error: String? = nil
    ) -> String {
        let object: [String: Any] = [
            "ok": ok,
            "spoken": spoken ?? NSNull(),
            "data": data ?? NSNull(),
            "error": error ?? NSNull(),
        ]
        guard JSONSerialization.isValidJSONObject(object),
              let encoded = try? JSONSerialization.data(
                withJSONObject: object,
                options: [.sortedKeys]
              ) else {
            return #"{"ok":false,"error":"could not encode Mac primitive result"}"#
        }
        return String(data: encoded, encoding: .utf8) ?? #"{"ok":false}"#
    }

}
