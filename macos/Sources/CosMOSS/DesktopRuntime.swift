import Foundation

/// Installs only after an explicit action. All downloaded state lives outside the signed app.
enum DesktopRuntime {
    struct Result: Sendable {
        let path: URL?
        let logURL: URL
    }

    static func prepare(resources: URL, storage: URL, install: Bool) async -> Result {
        await Task.detached(priority: .userInitiated) {
            let logURL = storage.appendingPathComponent("logs/runtime-install.log")
            do {
                try FileManager.default.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
                if !FileManager.default.fileExists(atPath: logURL.path) {
                    FileManager.default.createFile(atPath: logURL.path, contents: nil)
                }
                let log = try FileHandle(forWritingTo: logURL)
                defer { try? log.close() }
                try log.seekToEnd()
                let pipe = Pipe()
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/bash")
                process.arguments = [resources.appendingPathComponent("install-desktop-runtime.sh").path,
                                     resources.path, storage.path, install ? "--install" : "--check"]
                process.standardOutput = pipe
                process.standardError = log
                try process.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard process.terminationStatus == 0,
                      let path = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                      !path.isEmpty else { return Result(path: nil, logURL: logURL) }
                return Result(path: URL(fileURLWithPath: path, isDirectory: true), logURL: logURL)
            } catch { return Result(path: nil, logURL: logURL) }
        }.value
    }
}
