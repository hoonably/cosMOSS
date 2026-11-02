import Foundation

@main
struct RuntimeProgressSmoke {
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("cosmoss-progress-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let script = root.appendingPathComponent("install-desktop-runtime.sh")
        try """
        printf 'tool version 999: unexpected diagnostic format\\n' >&2
        printf 'COSMOSS_RUNTIME_STA' >&2
        sleep 0.05
        printf 'GE:tools\\n' >&2
        for ((i=0; i<2000; i++)); do printf 'long diagnostic line that must stay out of the UI\\n' >&2; done
        printf 'COSMOSS_RUNTIME_STAGE:future-unknown-stage\\n' >&2
        printf 'COSMOSS_RUNTIME_STAGE:python\\n' >&2
        printf 'COSMOSS_RUNTIME_STAGE:packages\\n' >&2
        printf 'COSMOSS_RUNTIME_STAGE:verifying\\n' >&2
        printf '%s/slot\\n' "$2"
        """.write(to: script, atomically: true, encoding: .utf8)
        var stages: [DesktopRuntime.Stage] = []
        let result = await DesktopRuntime.prepare(resources: root, storage: root, install: true) { stages.append($0) }
        precondition(stages == [.tools, .python, .packages, .verifying], "Partial chunks, noisy logs or unknown stage broke progress")
        precondition(result.path == root.appendingPathComponent("slot", isDirectory: true))
        let log = try String(contentsOf: result.logURL, encoding: .utf8)
        precondition(log.contains("tool version 999") && log.components(separatedBy: "long diagnostic line").count == 2001)
        try "printf 'changed error message\\n' >&2; exit 1\n".write(to: script, atomically: true, encoding: .utf8)
        let failure = await DesktopRuntime.prepare(resources: root, storage: root, install: true)
        precondition(failure.path == nil)
        let failureLog = try String(contentsOf: failure.logURL, encoding: .utf8)
        precondition(failureLog.contains("changed error message"))
        try "printf '%s/slot\\n' \"$2\"\n".write(to: script, atomically: true, encoding: .utf8)
        let reuse = await DesktopRuntime.prepare(resources: root, storage: root, install: false)
        precondition(reuse.path == result.path)
        print("PASS: streaming stages, fragmented lines, noisy/version-dependent logs, failure log and existing runtime reuse")
    }
}
