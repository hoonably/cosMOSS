// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CosMOSS",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "CosMOSS", targets: ["CosMOSS"])],
    targets: [.executableTarget(name: "CosMOSS")]
)
