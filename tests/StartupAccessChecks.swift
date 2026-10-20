import AppKit
import SwiftUI

private final class ProbeState: @unchecked Sendable {
    private let lock = NSLock()
    private var allowed = false
    private var count = 0
    func sample() -> Bool {
        Thread.sleep(forTimeInterval: 0.05)
        lock.lock(); defer { lock.unlock() }
        count += 1
        return allowed
    }
    func allow(_ value: Bool) { lock.lock(); allowed = value; lock.unlock() }
    var calls: Int { lock.lock(); defer { lock.unlock() }; return count }
}

@main
struct StartupAccessChecks {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let state = ProbeState()
        let gate = FullDiskAccessGate(probe: { state.sample() })
        async let first = gate.check()
        async let second = gate.check()
        let results = await (first, second)
        precondition(!results.0 && !results.1)
        precondition(gate.hasChecked && !gate.isChecking && !gate.isReady)
        precondition(state.calls == 1, "Concurrent checks must share one probe")
        state.allow(true)
        let allowed = await gate.check()
        precondition(allowed)
        precondition(gate.isReady && state.calls == 2)
        // Do not interrupt an active app job if Settings changes later.
        state.allow(false)
        let stillRunning = await gate.check()
        precondition(stillRunning && state.calls == 2)
        // A fresh process/model must check actual access, never a saved flag.
        let fresh = FullDiskAccessGate(probe: { state.sample() })
        let freshAllowed = await fresh.check()
        precondition(!freshAllowed && state.calls == 3)

        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("sentinel")
        let original = Data("A descriptor check must leave this intact".utf8)
        try original.write(to: file)
        precondition(FullDiskAccessGate.canReadRegularFile(file))
        precondition(!FullDiskAccessGate.canReadRegularFile(root))
        precondition(!FullDiskAccessGate.canReadRegularFile(root.appendingPathComponent("missing")))
        let link = root.appendingPathComponent("symlink")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
        precondition(!FullDiskAccessGate.canReadRegularFile(link))
        let after = try Data(contentsOf: file)
        precondition(after == original)

        if CommandLine.arguments.count > 2 {
            let panel = NSWindow(contentRect: NSRect(x: -12000, y: -12000, width: 1100, height: 720),
                                 styleMask: [.titled], backing: .buffered, defer: false)
            panel.appearance = NSAppearance(named: .aqua)
            let hosting = NSHostingView(rootView: StartupAccessView(access: fresh, recheck: {},
                appURL: URL(fileURLWithPath: CommandLine.arguments[1])))
            hosting.sizingOptions = []
            panel.contentView = hosting
            panel.setContentSize(NSSize(width: 1100, height: 720))
            panel.orderFront(nil)
            try await Task.sleep(nanoseconds: 500_000_000)
            hosting.layoutSubtreeIfNeeded()
            let image = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds)!
            hosting.cacheDisplay(in: hosting.bounds, to: image)
            try image.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
            panel.orderOut(nil)
        }
        print("PASS: startup denied/granted/retry, serialized checks, fresh-launch revalidation, no-follow descriptor probe, unchanged files; no privacy grants or backend launch")
    }

}
