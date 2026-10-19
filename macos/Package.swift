// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CosMOSS",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "CosMOSS", targets: ["CosMOSS"])],
    dependencies: [.package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.9.6")],
    targets: [.executableTarget(name: "CosMOSS", dependencies: [.product(name: "Sparkle", package: "Sparkle")])]
)
