import AppKit
import SwiftUI

/// Keep the existing web view (and its drafts) when a job continues in the background.
struct WindowLifecycleView: NSViewRepresentable {
    static let windowID = NSUserInterfaceItemIdentifier("cosmoss.workspace")
    let activeWork: @MainActor () async -> Bool?
    var quit: @MainActor () -> Void = { NSApp.terminate(nil) }

    func makeCoordinator() -> Coordinator { Coordinator(activeWork: activeWork, quit: quit) }

    func makeNSView(context: Context) -> WindowCaptureView {
        let view = WindowCaptureView()
        view.capture = { [weak coordinator = context.coordinator] window in coordinator?.attach(window) }
        return view
    }

    func updateNSView(_ nsView: WindowCaptureView, context: Context) {}

    @MainActor
    static func restoreWindows(in windows: [NSWindow]) -> Bool {
        let workspaces = windows.filter { $0.identifier == windowID }
        for window in workspaces { window.makeKeyAndOrderFront(nil) }
        if !workspaces.isEmpty { NSApp.activate(ignoringOtherApps: true) }
        return !workspaces.isEmpty
    }

    final class WindowCaptureView: NSView {
        var capture: ((NSWindow) -> Void)?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let window { capture?(window) }
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSWindowDelegate {
        let activeWork: @MainActor () async -> Bool?
        let quit: @MainActor () -> Void
        private weak var previousDelegate: NSWindowDelegate?
        private var checking = false

        init(activeWork: @escaping @MainActor () async -> Bool?, quit: @escaping @MainActor () -> Void) {
            self.activeWork = activeWork
            self.quit = quit
        }

        func attach(_ window: NSWindow) {
            guard window.delegate !== self else { return }
            previousDelegate = window.delegate
            window.delegate = self
            window.identifier = WindowLifecycleView.windowID
        }

        // Preserve SwiftUI's other window delegate callbacks.
        override func responds(to selector: Selector!) -> Bool {
            super.responds(to: selector) || (previousDelegate?.responds(to: selector) ?? false)
        }

        override func forwardingTarget(for selector: Selector!) -> Any? {
            if previousDelegate?.responds(to: selector) == true { return previousDelegate }
            return super.forwardingTarget(for: selector)
        }

        func windowShouldClose(_ sender: NSWindow) -> Bool {
            guard !checking else { return false }
            checking = true
            Task { [weak self, weak sender] in
                guard let self else { return }
                let busy = await activeWork()
                checking = false
                guard let sender else { return }
                if busy == false {
                    quit()
                } else {
                    // A status timeout must not interrupt a potentially active job.
                    sender.orderOut(nil)
                }
            }
            return false
        }
    }
}
