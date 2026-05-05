import Foundation

/// Spawns the Python `local_service` as a child process and tracks its port.
final class LocalServiceProcess {
    static let shared = LocalServiceProcess()

    private let workingDir = "/Users/jas/Documents/Coding/friday2/local_service"
    private let pythonPath = "/Users/jas/Documents/Coding/friday2/local_service/.venv/bin/python"
    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?

    private let portFile: URL = {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return support.appendingPathComponent("Friday/port")
    }()

    func start() throws {
        // Best effort: kill any orphaned local_service processes from prior crashed runs.
        killOrphans()

        // Best effort: clear any stale port file from a previous crashed run.
        try? FileManager.default.removeItem(at: portFile)

        guard FileManager.default.isExecutableFile(atPath: pythonPath) else {
            throw NSError(domain: "Friday", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "local_service venv missing at \(pythonPath). Run `uv sync` in local_service/."
            ])
        }

        let p = Process()
        p.currentDirectoryURL = URL(fileURLWithPath: workingDir)
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = ["-m", "src.main"]

        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env

        let out = Pipe()
        let err = Pipe()
        p.standardOutput = out
        p.standardError = err
        stdoutPipe = out
        stderrPipe = err

        attachLogging(out, label: "py.out")
        attachLogging(err, label: "py.err")

        p.terminationHandler = { proc in
            NSLog("[Friday] local_service exited code=\(proc.terminationStatus)")
        }

        try p.run()
        process = p
        NSLog("[Friday] launched local_service pid=\(p.processIdentifier)")
    }

    /// Polls the port file written by local_service on startup.
    func waitForPort(timeout: TimeInterval = 15) async throws -> Int {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let s = try? String(contentsOf: portFile, encoding: .utf8),
               let port = Int(s.trimmingCharacters(in: .whitespacesAndNewlines)) {
                return port
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw NSError(domain: "Friday", code: 1, userInfo: [NSLocalizedDescriptionKey: "local_service did not write port file in time"])
    }

    func stop() {
        guard let p = process, p.isRunning else { return }
        // Python is a direct child now, so SIGTERM reaches it and the
        // sounddevice stream's context manager runs — releasing the mic
        // (and the orange indicator) cleanly. Wait synchronously so the
        // app doesn't exit before the child is reaped.
        p.terminate()
        let deadline = Date().addingTimeInterval(2.0)
        while p.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if p.isRunning {
            // Last-resort hard kill if Python wedged during shutdown.
            kill(p.processIdentifier, SIGKILL)
            p.waitUntilExit()
        }
    }

    /// Kills any leftover `python -m src.main` from prior crashed runs.
    /// Safe to call before every start.
    private func killOrphans() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        p.arguments = ["-9", "-f", "-m src\\.main"]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            NSLog("[Friday] killOrphans pkill failed: \(error)")
        }
    }

    private func attachLogging(_ pipe: Pipe, label: String) {
        let handle = pipe.fileHandleForReading
        handle.readabilityHandler = { fh in
            let data = fh.availableData
            guard !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
            for line in s.split(separator: "\n") {
                NSLog("[Friday/%@] %@", label, String(line))
            }
        }
    }
}
