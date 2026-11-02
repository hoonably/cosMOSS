import Foundation

/// Installs only after an explicit action. All downloaded state lives outside the signed app.
enum DesktopRuntime {
    enum Stage: String, Sendable {
        case preparing, tools, python, packages, verifying

        var message: String {
            switch self {
            case .preparing: return "설치를 준비하는 중…"
            case .tools: return "실행 도구를 내려받는 중…"
            case .python: return "실행 환경을 준비하는 중…"
            case .packages: return "필요한 파일을 설치하는 중…"
            case .verifying: return "설치된 파일을 확인하는 중…"
            }
        }
    }

    struct Result: Sendable {
        let path: URL?
        let logURL: URL
    }

    static func prepare(resources: URL, storage: URL, install: Bool,
                        progress: @escaping @MainActor @Sendable (Stage) -> Void = { _ in }) async -> Result {
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
                let diagnostics = Pipe()
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/bin/bash")
                process.arguments = [resources.appendingPathComponent("install-desktop-runtime.sh").path,
                                     resources.path, storage.path, install ? "--install" : "--check"]
                process.standardOutput = pipe
                process.standardError = diagnostics
                try process.run()
                defer { if process.isRunning { process.terminate() } }
                // Explicit stage codes drive the UI; version-dependent tool output stays in the log.
                var pending = Data()
                while true {
                    let chunk = diagnostics.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    try log.write(contentsOf: chunk)
                    pending.append(chunk)
                    while let newline = pending.firstIndex(of: 10) {
                        let line = String(decoding: pending[..<newline], as: UTF8.self)
                        pending.removeSubrange(...newline)
                        let prefix = "COSMOSS_RUNTIME_STAGE:"
                        if line.hasPrefix(prefix), let stage = Stage(rawValue: String(line.dropFirst(prefix.count))) {
                            await progress(stage)
                        }
                    }
                    // Some progress output uses carriage returns instead of newlines.
                    if pending.count > 65_536 { pending.removeAll(keepingCapacity: true) }
                }
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
