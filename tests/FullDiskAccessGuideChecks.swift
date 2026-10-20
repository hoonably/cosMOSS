import AppKit

@main
struct FullDiskAccessGuideChecks {
    @MainActor static func main() throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let app = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
        let guide = FullDiskAccessAssistant(appURL: app)
        let window = guide.window!
        let size = window.frame.size
        let roomy = NSRect(x: 0, y: 0, width: 2200, height: 1600)
        let highSettings = NSRect(x: 600, y: 800, width: 750, height: 700)
        let below = AccessGuidePlacement.frame(size: size, beside: highSettings, screens: [roomy])
        precondition(below.maxY == highSettings.minY - 12 && below.maxX == highSettings.maxX)
        let shortScreen = NSRect(x: 0, y: 0, width: 2000, height: 900)
        let tallSettings = NSRect(x: 650, y: 100, width: 750, height: 750)
        let right = AccessGuidePlacement.frame(size: size, beside: tallSettings, screens: [shortScreen])
        precondition(right.minX == tallSettings.maxX + 12 && !right.intersects(tallSettings))
        let edgeSettings = NSRect(x: 1200, y: 100, width: 750, height: 750)
        let left = AccessGuidePlacement.frame(size: size, beside: edgeSettings, screens: [shortScreen])
        precondition(left.maxX == edgeSettings.minX - 12 && !left.intersects(edgeSettings))
        // Settings on a secondary display, including negative and vertical coordinates.
        for offset in [NSPoint(x: 2200, y: 0), NSPoint(x: -2200, y: 0), NSPoint(x: 0, y: 1600)] {
            let screen = roomy.offsetBy(dx: offset.x, dy: offset.y)
            let settings = highSettings.offsetBy(dx: offset.x, dy: offset.y)
            let placed = AccessGuidePlacement.frame(size: size, beside: settings, screens: [roomy, screen])
            precondition(screen.contains(placed) && !placed.intersects(settings))
            precondition(placed.maxY == settings.minY - 12)
        }
        let crowded = NSRect(x: 0, y: 0, width: 1024, height: 768)
        let clamped = AccessGuidePlacement.frame(size: size, beside: crowded, screens: [crowded])
        precondition(crowded.contains(clamped), "The whole guide must remain reachable on a small display")
        let converted = AccessGuidePlacement.appKitFrame(CGRect(x: -900, y: -500, width: 750, height: 700), primaryDisplayTop: 900)
        precondition(converted == NSRect(x: -900, y: 700, width: 750, height: 700))
        // Render offscreen; never open Settings, request/grant FDA or reset TCC.
        window.setFrameOrigin(NSPoint(x: -12000, y: -12000))
        window.orderFront(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        let content = window.contentView!
        content.layoutSubtreeIfNeeded()
        precondition(content.bounds.size == NSSize(width: 356, height: 410))
        func findTile(_ view: NSView) -> AppBundleDragView? {
            if let tile = view as? AppBundleDragView { return tile }
            return view.subviews.lazy.compactMap { findTile($0) }.first
        }
        let tile = findTile(content)!
        precondition(tile.appURL == app)
        let center = tile.convert(NSPoint(x: tile.bounds.midX, y: tile.bounds.midY), to: content.superview)
        precondition(content.hitTest(center) === tile, "The tile must receive the drag gesture")
        let item = tile.draggingItem(at: NSPoint(x: 80, y: 70))
        precondition(item.draggingFrame.size == NSSize(width: 60, height: 60))
        let board = NSPasteboard.withUniqueName()
        defer { board.releaseGlobally() }
        precondition(board.writeObjects([item.item as! NSPasteboardWriting]))
        let urls = board.readObjects(forClasses: [NSURL.self]) as! [URL]
        precondition(urls.count == 1 && urls[0].standardizedFileURL == app)
        precondition(urls[0].pathExtension == "app")
        precondition(!window.hidesOnDeactivate && window.level == .floating)
        precondition(tile.frame.height >= 140 && tile.frame.width >= 280)
        if CommandLine.arguments.count > 2 {
            let image = content.bitmapImageRepForCachingDisplay(in: content.bounds)!
            content.cacheDisplay(in: content.bounds, to: image)
            try image.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
        }
        window.orderOut(nil)
        print("PASS: Settings-relative placement across displays; native guide and app drag; no Settings or TCC mutation")
    }
}
