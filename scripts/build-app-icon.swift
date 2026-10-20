#!/usr/bin/env swift
import AppKit

// Redraw the existing five-bar brand mark as vectors at every macOS icon size.
let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
let assets = root.appendingPathComponent("macos/Assets", isDirectory: true)
try FileManager.default.createDirectory(at: assets, withIntermediateDirectories: true)
let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("cosmoss-icon-\(UUID().uuidString)")
let iconset = temporary.appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: temporary) }

let heights: [CGFloat] = [163.84, 348.16, 512, 348.16, 163.84]
let centers: [CGFloat] = [307.2, 409.6, 512, 614.4, 716.8]
let green = NSColor(srgbRed: 31/255, green: 194/255, blue: 123/255, alpha: 1)
let paper = NSColor(srgbRed: 251/255, green: 252/255, blue: 249/255, alpha: 1)

func png(size: Int) throws -> Data {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                  bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                  isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    defer { NSGraphicsContext.restoreGraphicsState() }
    let transform = NSAffineTransform()
    transform.scale(by: CGFloat(size) / 1024)
    transform.concat()
    paper.setFill()
    NSBezierPath(roundedRect: NSRect(x: 64, y: 64, width: 896, height: 896), xRadius: 200, yRadius: 200).fill()
    for index in 0..<5 {
        green.withAlphaComponent(index == 0 || index == 4 ? 0.55 : 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: centers[index] - 30.72, y: 512 - heights[index] / 2,
                                        width: 61.44, height: heights[index]), xRadius: 30.72, yRadius: 30.72).fill()
    }
    return bitmap.representation(using: .png, properties: [:])!
}

for size in [16, 32, 128, 256, 512] {
    try png(size: size).write(to: iconset.appendingPathComponent("icon_\(size)x\(size).png"))
    try png(size: size * 2).write(to: iconset.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
try png(size: 1024).write(to: assets.appendingPathComponent("AppIcon.png"))
let bars = (0..<5).map { index in
    "<rect x=\"\(centers[index] - 30.72)\" y=\"\(512 - heights[index] / 2)\" width=\"61.44\" height=\"\(heights[index])\" rx=\"30.72\" opacity=\"\(index == 0 || index == 4 ? 0.55 : 1)\"/>"
}.joined(separator: "\n  ")
let svg = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect x="64" y="64" width="896" height="896" rx="200" fill="#fbfcf9"/>
  <g fill="#1fc27b">
  \(bars)
  </g>
</svg>

"""
try svg.write(to: assets.appendingPathComponent("AppIcon.svg"), atomically: true, encoding: .utf8)
let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconset.path, "-o", assets.appendingPathComponent("AppIcon.icns").path]
try iconutil.run()
iconutil.waitUntilExit()
guard iconutil.terminationStatus == 0 else { fatalError("iconutil failed") }
print("Generated macos/Assets/AppIcon.icns, .png and .svg")
