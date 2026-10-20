import AppKit
import CoreGraphics
import SwiftUI

/// Window metadata uses a top-left origin; AppKit uses the primary display's bottom-left.
enum AccessGuidePlacement {
    static func appKitFrame(_ frame: CGRect, primaryDisplayTop: CGFloat) -> NSRect {
        NSRect(x: frame.minX, y: primaryDisplayTop - frame.maxY, width: frame.width, height: frame.height)
    }

    static func frame(size: NSSize, beside settings: NSRect, screens: [NSRect]) -> NSRect {
        let screen = screens.max { lhs, rhs in
            area(lhs.intersection(settings)) < area(rhs.intersection(settings))
        } ?? settings
        let available = screen.insetBy(dx: 12, dy: 12)
        let gap: CGFloat = 12
        // Prefer below the application list; use a side when there isn't enough height.
        let candidates = [
            NSRect(x: settings.maxX - size.width, y: settings.minY - gap - size.height, width: size.width, height: size.height),
            NSRect(x: settings.maxX + gap, y: settings.maxY - size.height, width: size.width, height: size.height),
            NSRect(x: settings.minX - gap - size.width, y: settings.maxY - size.height, width: size.width, height: size.height),
            NSRect(x: settings.maxX - size.width, y: settings.maxY + gap, width: size.width, height: size.height)
        ].map { candidate -> NSRect in
            // Clamp only along the edge, preserving the gap on the other axis if possible.
            var result = candidate
            result.origin.x = max(available.minX, min(result.minX, available.maxX - size.width))
            result.origin.y = max(available.minY, min(result.minY, available.maxY - size.height))
            return result
        }
        return candidates.first { !$0.intersects(settings) } ?? candidates.min {
            area($0.intersection(settings)) < area($1.intersection(settings))
        }!
    }

    private static func area(_ rect: NSRect) -> CGFloat {
        rect.isNull || rect.isEmpty ? 0 : rect.width * rect.height
    }
}

/// A file drag assistant, not a permission grant. System Settings owns consent.
@MainActor
final class FullDiskAccessAssistant: NSWindowController, NSWindowDelegate {
    static let shared = FullDiskAccessAssistant()
    static let settingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
    private var onComplete: (() -> Void)?
    private weak var parentWindow: NSWindow?
    private var placementTask: Task<Void, Never>?

    init(appURL: URL = Bundle.main.bundleURL) {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 356, height: 410),
                            styleMask: [.titled, .closable, .utilityWindow], backing: .buffered, defer: false)
        panel.title = "cosMOSS"
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.appearance = NSAppearance(named: .aqua)
        panel.backgroundColor = NSColor(calibratedRed: 0.97, green: 0.98, blue: 0.96, alpha: 1)
        super.init(window: panel)
        panel.delegate = self
        let hosting = NSHostingView(rootView: FullDiskAccessGuide(appURL: appURL,
            openSettings: { [weak self] in _ = self?.openSettingsAndPositionGuide() },
            done: { [weak self] in self?.close() }).environment(\.colorScheme, .light))
        hosting.sizingOptions = []
        panel.contentView = hosting
        panel.setContentSize(NSSize(width: 356, height: 410))
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    @discardableResult
    func present(relativeTo parent: NSWindow?, onComplete: @escaping () -> Void) -> Bool {
        self.onComplete = onComplete
        parentWindow = parent
        return openSettingsAndPositionGuide()
    }

    private func openSettingsAndPositionGuide() -> Bool {
        guard let window else { return false }
        placementTask?.cancel()
        window.orderOut(nil)
        // Open Settings first: it may restore on a different display from cosMOSS.
        let opened = NSWorkspace.shared.open(Self.settingsURL)
        placementTask = Task { @MainActor [weak self] in
            var lastOrigin: NSPoint?
            for attempt in 0..<24 {
                do { try await Task.sleep(nanoseconds: 150_000_000) }
                catch { return }
                guard let self, !Task.isCancelled, let window = self.window else { return }
                // Respect a user moving the guide during the Settings opening animation.
                if let lastOrigin, hypot(window.frame.minX - lastOrigin.x, window.frame.minY - lastOrigin.y) > 1 { return }
                if let settings = Self.settingsWindowFrame() {
                    let frame = AccessGuidePlacement.frame(size: window.frame.size, beside: settings,
                                                          screens: NSScreen.screens.map(\.visibleFrame))
                    window.setFrameOrigin(frame.origin)
                    lastOrigin = window.frame.origin
                    window.orderFrontRegardless()
                } else if attempt == 15 {
                    // Metadata can be unavailable; still offer the guide without asking for
                    // Accessibility or Screen Recording permission just to place a window.
                    let reference = self.parentWindow?.frame ?? NSScreen.main?.visibleFrame ?? window.frame
                    let frame = AccessGuidePlacement.frame(size: window.frame.size, beside: reference,
                                                          screens: NSScreen.screens.map(\.visibleFrame))
                    window.setFrameOrigin(frame.origin)
                    lastOrigin = window.frame.origin
                    window.orderFrontRegardless()
                }
            }
        }
        return opened
    }

    private static func settingsWindowFrame() -> NSRect? {
        let pids = Set(NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.systempreferences")
            .map(\.processIdentifier))
        guard !pids.isEmpty,
              let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]],
              let primaryTop = NSScreen.screens.first?.frame.maxY else { return nil }
        // Bounds and owner PID are public window metadata, not screen contents or AX data.
        for info in windows {
            guard let pid = info[kCGWindowOwnerPID as String] as? Int32, pids.contains(pid),
                  info[kCGWindowLayer as String] as? Int == 0,
                  (info[kCGWindowAlpha as String] as? Double ?? 1) > 0,
                  let bounds = info[kCGWindowBounds as String] as? NSDictionary,
                  let frame = CGRect(dictionaryRepresentation: bounds),
                  frame.width >= 400, frame.height >= 300 else { continue }
            return AccessGuidePlacement.appKitFrame(frame, primaryDisplayTop: primaryTop)
        }
        return nil
    }

    func windowWillClose(_ notification: Notification) {
        placementTask?.cancel()
        placementTask = nil
        let complete = onComplete
        onComplete = nil
        parentWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        complete?()
    }
}

private struct FullDiskAccessGuide: View {
    let appURL: URL
    let openSettings: () -> Void
    let done: () -> Void
    private let ink = Color(red: 0.15, green: 0.24, blue: 0.19)

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("전체 디스크 접근 허용").font(.system(size: 21, weight: .bold)).foregroundColor(ink)
            Text("앱을 찾지 않고, 여기서 바로 추가하세요.")
                .font(.system(size: 12)).foregroundColor(.secondary)
            AppBundleDragTile(appURL: appURL).frame(height: 140)
            VStack(alignment: .leading, spacing: 9) {
                instruction("1", "위 아이콘을 설정의 앱 목록으로 드래그")
                instruction("2", "cosMOSS 옆 스위치 켜기")
            }
            Text("이미 목록에 있으면 스위치만 켜세요.")
                .font(.system(size: 11)).foregroundColor(.secondary)
            HStack {
                Button("설정 다시 열기", action: openSettings).buttonStyle(GuideButtonStyle(prominent: false))
                Spacer()
                Button("완료", action: done).buttonStyle(GuideButtonStyle(prominent: true))
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(red: 0.97, green: 0.98, blue: 0.96))
    }

    private func instruction(_ number: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Text(number).font(.system(size: 10, weight: .semibold))
                .frame(width: 19, height: 19).background(Color.black.opacity(0.05)).clipShape(Circle())
            Text(text).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
        }.foregroundColor(ink)
    }
}

struct GuideButtonStyle: ButtonStyle {
    let prominent: Bool
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 12, weight: .semibold))
            .foregroundColor(prominent ? .white : Color(red: 0.31, green: 0.39, blue: 0.33))
            .padding(.horizontal, 16).frame(height: 35)
            .background(prominent ? Color(red: 0.10, green: 0.50, blue: 0.33) : .white)
            .clipShape(RoundedRectangle(cornerRadius: 9))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(prominent ? Color.clear : Color.black.opacity(0.10)))
            .opacity(configuration.isPressed ? 0.75 : 1)
    }
}

private struct AppBundleDragTile: NSViewRepresentable {
    let appURL: URL
    func makeNSView(context: Context) -> AppBundleDragView { AppBundleDragView(appURL: appURL) }
    func updateNSView(_ nsView: AppBundleDragView, context: Context) {}
}

/// Drag the actual running .app URL, including when it is outside /Applications.
@MainActor
final class AppBundleDragView: NSView, NSDraggingSource {
    let appURL: URL
    private let icon: NSImage
    private var mouseDownPoint: NSPoint?

    init(appURL: URL) {
        self.appURL = appURL
        icon = NSWorkspace.shared.icon(forFile: appURL.path)
        super.init(frame: .zero)
        setAccessibilityElement(true)
        setAccessibilityRole(.image)
        setAccessibilityLabel("cosMOSS 앱 아이콘. 전체 디스크 접근 권한의 앱 목록으로 드래그하세요.")
        toolTip = "이 아이콘을 시스템 설정의 앱 목록으로 드래그하세요."
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func draw(_ dirtyRect: NSRect) {
        let card = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: 14, yRadius: 14)
        NSColor(calibratedRed: 0.92, green: 0.96, blue: 0.91, alpha: 1).setFill()
        card.fill()
        NSColor(calibratedRed: 0.72, green: 0.82, blue: 0.73, alpha: 1).setStroke()
        card.lineWidth = 1
        card.setLineDash([5, 4], count: 2, phase: 0)
        card.stroke()
        icon.draw(in: NSRect(x: bounds.midX - 30, y: 65, width: 60, height: 60))
        drawCentered("cosMOSS", y: 38, font: .systemFont(ofSize: 14, weight: .semibold), color: .labelColor)
        drawCentered("설정 목록으로 드래그 ↗", y: 16, font: .systemFont(ofSize: 11), color: .secondaryLabelColor)
    }

    private func drawCentered(_ text: String, y: CGFloat, font: NSFont, color: NSColor) {
        let label = NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color])
        label.draw(at: NSPoint(x: bounds.midX - label.size().width / 2, y: y))
    }

    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    override func mouseDown(with event: NSEvent) { mouseDownPoint = convert(event.locationInWindow, from: nil) }
    override func mouseUp(with event: NSEvent) { mouseDownPoint = nil }

    func draggingItem(at point: NSPoint) -> NSDraggingItem {
        let item = NSDraggingItem(pasteboardWriter: appURL as NSURL)
        item.setDraggingFrame(NSRect(x: point.x - 30, y: point.y - 30, width: 60, height: 60), contents: icon)
        return item
    }

    override func mouseDragged(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        guard let start = mouseDownPoint, hypot(point.x - start.x, point.y - start.y) >= 4 else { return }
        mouseDownPoint = nil
        let session = beginDraggingSession(with: [draggingItem(at: point)], event: event, source: self)
        session.animatesToStartingPositionsOnCancelOrFail = true
    }

    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        context == .outsideApplication ? .copy : []
    }
}
