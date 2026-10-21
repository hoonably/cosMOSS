import AppKit
import SwiftUI

@MainActor
private final class OriginalDelegate: NSObject, NSWindowDelegate {
    var moved = false
    func windowDidMove(_ notification: Notification) { moved = true }
}

@MainActor
private final class TestApplicationDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply { .terminateCancel }
}

@main
struct WindowLifecycleChecks {
    @MainActor static func main() {
        _ = NSApplication.shared
        let appDelegate = TestApplicationDelegate()
        NSApp.delegate = appDelegate
        NSApp.setActivationPolicy(.prohibited)
        Task { @MainActor in
            try await runChecks()
            exit(0)
        }
        withExtendedLifetime(appDelegate) { NSApp.run() }
    }

    @MainActor static func runChecks() async throws {
        let window = NSWindow(contentRect: NSRect(x: -12000, y: -12000, width: 200, height: 100),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        let original = OriginalDelegate()
        window.delegate = original
        var busy: Bool? = true
        var quits = 0
        var checks = 0
        let lifecycle = WindowLifecycleView.Coordinator(activeWork: {
            checks += 1
            try? await Task.sleep(nanoseconds: 30_000_000)
            return busy
        }, quit: { quits += 1 })
        lifecycle.attach(window)
        // Other SwiftUI delegate callbacks are still forwarded.
        let selector = #selector(NSWindowDelegate.windowDidMove(_:))
        precondition(lifecycle.responds(to: selector))
        precondition(lifecycle.forwardingTarget(for: selector) as AnyObject === original)

        window.orderFront(nil)
        window.performClose(nil)
        window.performClose(nil) // Repeated X while checking shares one request.
        try await Task.sleep(nanoseconds: 100_000_000)
        precondition(checks == 1 && quits == 0 && !window.isVisible)
        precondition(window.contentView != nil, "The existing view/drafts must survive")
        precondition(WindowLifecycleView.restoreWindows(in: [window]))
        precondition(window.isVisible)
        busy = nil // A stalled backend must never cause an accidental quit.
        window.performClose(nil)
        try await Task.sleep(nanoseconds: 100_000_000)
        precondition(quits == 0 && !window.isVisible)

        _ = WindowLifecycleView.restoreWindows(in: [window])
        busy = false
        window.performClose(nil)
        try await Task.sleep(nanoseconds: 100_000_000)
        precondition(quits == 1 && window.isVisible)
        window.orderOut(nil)
        precondition(!WindowLifecycleView.restoreWindows(in: []))
        let hosted = NSWindow(contentRect: NSRect(x: -12000, y: -12000, width: 200, height: 100),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        let content = NSHostingView(rootView: Text("fixture").background(
            WindowLifecycleView(activeWork: { true }, quit: { quits += 1 })))
        hosted.contentView = content
        hosted.orderFront(nil)
        try await Task.sleep(nanoseconds: 100_000_000)
        precondition(hosted.delegate is WindowLifecycleView.Coordinator, "SwiftUI must attach the close delegate")
        hosted.performClose(nil)
        try await Task.sleep(nanoseconds: 100_000_000)
        precondition(!hosted.isVisible && quits == 1)
        print("PASS: busy X hides/preserves view, Dock reopens, idle X quits, unknown state preserves work, duplicate checks coalesced, delegate forwarding")
    }
}
