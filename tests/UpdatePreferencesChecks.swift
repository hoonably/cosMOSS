import AppKit
import Sparkle

@MainActor
final class BackendController {
    static let shared = BackendController()
    func prepareForUpdate() async -> Bool { true }
}

@main
struct UpdatePreferencesChecks {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in
            let updates = UpdateController.shared
            updates.start()
            for _ in 0..<100 {
                if updates.canCheckForUpdates { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            guard updates.canCheckForUpdates else { fatalError("Updater did not start") }
            let mode = CommandLine.arguments[1]
            if mode == "default" {
                guard updates.automaticallyChecksForUpdates else { fatalError("First-install default must be enabled") }
                updates.setAutomaticChecks(false)
                guard !updates.automaticallyChecksForUpdates else { fatalError("Could not disable automatic notifications") }
                UserDefaults.standard.synchronize()
                print("PASS default checked; user's off preference saved")
            } else {
                guard !updates.automaticallyChecksForUpdates else { fatalError("User's off preference was reset on launch") }
                updates.refreshUpdateInformation()
                for _ in 0..<150 {
                    if !updates.isChecking { break }
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
                guard !updates.isChecking else { fatalError("Manual update check did not finish") }
                switch mode {
                case "latest":
                    guard !updates.updateAvailable && updates.status == "최신 버전을 사용하고 있습니다." else { fatalError(updates.status) }
                case "available":
                    guard updates.updateAvailable && updates.status.contains("업데이트가 있습니다") else { fatalError(updates.status) }
                case "error":
                    guard !updates.updateAvailable && updates.status.contains("확인하지 못했습니다") else { fatalError(updates.status) }
                default: fatalError("Unknown mode")
                }
                print("PASS restored off preference; manual check: \(mode)")
            }
            NSApp.terminate(nil)
        }
        app.run()
    }
}
