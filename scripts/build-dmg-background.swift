#!/usr/bin/env swift
import AppKit

// Finder uses points; the TIFF includes both standard and Retina representations.
let output = URL(fileURLWithPath: CommandLine.arguments[1])
let size = NSSize(width: 640, height: 420)
let image = NSImage(size: size)
func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> NSColor {
    NSColor(srgbRed: red / 255, green: green / 255, blue: blue / 255, alpha: 1)
}
let ink = color(25, 35, 28)
let muted = color(99, 114, 104)
let green = color(31, 194, 123)

for scale in [1, 2] {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil,
        pixelsWide: 640 * scale, pixelsHigh: 420 * scale,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    bitmap.size = size
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    color(241, 244, 238).setFill()
    NSRect(origin: .zero, size: size).fill()
    let title = NSMutableAttributedString(string: "cosMOSS", attributes: [
        .font: NSFont.systemFont(ofSize: 34, weight: .bold), .foregroundColor: ink,
    ])
    title.addAttribute(.foregroundColor, value: green, range: NSRange(location: 3, length: 4))
    title.draw(at: NSPoint(x: 40, y: 337))
    ("앱을 응용 프로그램 폴더로 드래그하세요." as NSString).draw(
        at: NSPoint(x: 42, y: 305),
        withAttributes: [.font: NSFont.systemFont(ofSize: 15), .foregroundColor: muted])

    color(251, 252, 249).setFill()
    let panel = NSBezierPath(roundedRect: NSRect(x: 32, y: 65, width: 576, height: 215),
                             xRadius: 24, yRadius: 24)
    panel.fill()
    color(221, 227, 219).setStroke()
    panel.lineWidth = 1
    panel.stroke()

    let arrow = NSBezierPath()
    arrow.move(to: NSPoint(x: 294, y: 202))
    arrow.line(to: NSPoint(x: 346, y: 202))
    arrow.move(to: NSPoint(x: 335, y: 213))
    arrow.line(to: NSPoint(x: 346, y: 202))
    arrow.line(to: NSPoint(x: 335, y: 191))
    color(148, 174, 155).setStroke()
    arrow.lineWidth = 3
    arrow.lineCapStyle = .round
    arrow.lineJoinStyle = .round
    arrow.stroke()

    NSGraphicsContext.restoreGraphicsState()
    image.addRepresentation(bitmap)
}
guard let data = image.tiffRepresentation else { fatalError("Could not render DMG background") }
try data.write(to: output)
