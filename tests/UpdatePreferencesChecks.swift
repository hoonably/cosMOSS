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
            let mode = CommandLine.arguments[1]
            if mode == "legacy-on" {
                UserDefaults.standard.set(true, forKey: "SUEnableAutomaticChecks")
                UserDefaults.standard.set(true, forKey: "SUAutomaticallyUpdate")
                UserDefaults.standard.synchronize()
            }
            let updates = UpdateController.shared
            updates.start()
            if mode == "development" {
                guard updates.isDevelopmentBuild && !updates.canCheckForUpdates else { fatalError("Development updater must stay stopped") }
                updates.checkForUpdates()
                NSApp.windows.filter { $0.title == "업데이트 확인" }.forEach { $0.setFrameOrigin(NSPoint(x: -12000, y: -12000)) }
                guard NSApp.windows.contains(where: { $0.title == "업데이트 확인" && $0.isVisible }) else { fatalError("Development update window must open") }
                guard updates.status.contains("개발용 앱") && !updates.isChecking else { fatalError(updates.status) }
                updates.setAutomaticChecks(true)
                updates.setAutomaticDownloads(false)
                guard updates.automaticallyChecksForUpdates && !updates.automaticallyDownloadsUpdates else { fatalError("Notifications must not enable downloads") }
                updates.setAutomaticChecks(false)
                guard !updates.automaticallyChecksForUpdates && !updates.automaticallyDownloadsUpdates else { fatalError("Both automatic settings must be off") }
                print("PASS development: window opens, settings work, updater stays stopped")
                NSApp.terminate(nil)
                return
            }
            if mode == "latest" {
                updates.checkForUpdates()
                NSApp.windows.filter { $0.title == "업데이트 확인" }.forEach { $0.setFrameOrigin(NSPoint(x: -12000, y: -12000)) }
            }
            for _ in 0..<100 {
                if updates.canCheckForUpdates { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            guard updates.canCheckForUpdates else { fatalError("Updater did not start") }
            if mode == "default" {
                guard updates.automaticallyChecksForUpdates && !updates.automaticallyDownloadsUpdates else { fatalError("First-install default must notify without installing") }
                updates.setAutomaticDownloads(true)
                updates.setAutomaticChecks(false)
                guard updates.automaticallyDownloadsUpdates else { fatalError("Check setting must preserve download preference") }
                updates.setAutomaticDownloads(false)
                updates.setAutomaticChecks(false)
                guard !updates.automaticallyChecksForUpdates && !updates.automaticallyDownloadsUpdates else { fatalError("Could not disable automatic updates") }
                UserDefaults.standard.synchronize()
                print("PASS default checked; user's off preference saved")
            } else if mode == "legacy-on" || mode == "legacy-on-restored" {
                guard updates.automaticallyChecksForUpdates && updates.automaticallyDownloadsUpdates else {
                    fatalError("Existing user's on preferences must survive new installation defaults and restarts")
                }
                if mode == "legacy-on-restored" {
                    updates.setAutomaticChecks(false)
                    guard updates.automaticallyDownloadsUpdates else { fatalError("Disabling checks must preserve install choice") }
                    updates.setAutomaticDownloads(false)
                    UserDefaults.standard.synchronize()
                }
                print("PASS saved on preferences preserved: \(mode)")
            } else {
                guard !updates.automaticallyChecksForUpdates && !updates.automaticallyDownloadsUpdates else { fatalError("User's off preference was reset on launch") }
                if mode != "latest" { updates.refreshUpdateInformation() }
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
