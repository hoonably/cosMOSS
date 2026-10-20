import AppKit
import Combine
import Sparkle
import SwiftUI

@MainActor
final class UpdateController: NSObject, ObservableObject, SPUUpdaterDelegate {
    static let shared = UpdateController()
    @Published private(set) var canCheckForUpdates = false
    @Published private(set) var automaticallyChecksForUpdates = true
    @Published private(set) var isChecking = false
    @Published private(set) var updateAvailable = false
    @Published private(set) var status = "새 버전을 확인합니다."
    private var observers: Set<AnyCancellable> = []
    private var updateWindow: NSWindow?
    private var manualCheck = false
    private var installationTask: Task<Void, Never>?
    private var started = false
    private lazy var controller = SPUStandardUpdaterController(
        startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil
    )

    func start() {
        // A development launcher must never replace a checkout-based app.
        guard !started, Bundle.main.object(forInfoDictionaryKey: "CosMOSSProjectRoot") == nil else { return }
        started = true
        controller.updater.publisher(for: \.canCheckForUpdates)
            .receive(on: RunLoop.main)
            .sink { [weak self] value in self?.canCheckForUpdates = value }
            .store(in: &observers)
        controller.updater.publisher(for: \.automaticallyChecksForUpdates)
            .receive(on: RunLoop.main)
            .sink { [weak self] value in self?.automaticallyChecksForUpdates = value }
            .store(in: &observers)
        controller.startUpdater()
    }

    func checkForUpdates() {
        if updateWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 270),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "업데이트 확인"
            window.isReleasedWhenClosed = false
            window.contentView = NSHostingView(rootView: UpdateCheckView(updates: self))
            window.center()
            updateWindow = window
        }
        updateWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refreshUpdateInformation()
    }

    func setAutomaticChecks(_ enabled: Bool) {
        // Sparkle persists this value itself. Never overwrite it on app launch.
        controller.updater.automaticallyChecksForUpdates = enabled
        automaticallyChecksForUpdates = controller.updater.automaticallyChecksForUpdates
    }

    func refreshUpdateInformation() {
        guard canCheckForUpdates, !isChecking else { return }
        manualCheck = true
        isChecking = true
        updateAvailable = false
        status = "업데이트를 확인하고 있습니다…"
        controller.updater.checkForUpdateInformation()
    }

    func showAvailableUpdate() {
        guard canCheckForUpdates else { return }
        updateWindow?.orderOut(nil)
        controller.checkForUpdates(nil)
    }

    func updater(_ updater: SPUUpdater, didFindValidUpdate item: SUAppcastItem) {
        guard manualCheck else { return }
        updateAvailable = true
        status = "v\(item.displayVersionString) 업데이트가 있습니다."
    }

    func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: Error?) {
        guard manualCheck else { return }
        manualCheck = false
        isChecking = false
        if let error = error as NSError?, !(error.domain == SUSparkleErrorDomain && error.code == Int(SUError.noUpdateError.rawValue)) {
            updateAvailable = false
            status = "업데이트를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요."
        } else if !updateAvailable {
            status = "최신 버전을 사용하고 있습니다."
        }
    }

    func updater(_ updater: SPUUpdater, shouldPostponeRelaunchForUpdate item: SUAppcastItem,
                 untilInvokingBlock installHandler: @escaping () -> Void) -> Bool {
        installationTask?.cancel()
        installationTask = Task {
            while !Task.isCancelled {
                if await BackendController.shared.prepareForUpdate() {
                    installHandler()
                    return
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        return true
    }
}

private struct UpdateCheckView: View {
    @ObservedObject var updates: UpdateController
    private let accent = Color(red: 0.07, green: 0.57, blue: 0.35)
    private var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "" }

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.system(size: 25, weight: .medium))
                    .foregroundStyle(accent)
                    .frame(width: 48, height: 48)
                    .background(accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 13))
                VStack(alignment: .leading, spacing: 6) {
                    Text("cosMOSS 업데이트").font(.system(size: 20, weight: .semibold))
                    Text("현재 버전 v\(version)").font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 9) {
                if updates.isChecking { ProgressView().controlSize(.small) }
                Text(updates.status).font(.system(size: 13)).accessibilityIdentifier("update-status")
            }
            .frame(minHeight: 20, alignment: .leading)
            Divider()
            Toggle("새 업데이트가 있으면 자동으로 알림", isOn: Binding(
                get: { updates.automaticallyChecksForUpdates },
                set: { updates.setAutomaticChecks($0) }
            ))
            .toggleStyle(.checkbox)
            .font(.system(size: 12))
            .tint(accent)
            .accessibilityIdentifier("automatic-update-notifications")
            HStack {
                Spacer()
                Button(updates.updateAvailable ? "다운로드 및 설치…" : "다시 확인") {
                    if updates.updateAvailable { updates.showAvailableUpdate() }
                    else { updates.refreshUpdateInformation() }
                }
                .disabled(!updates.canCheckForUpdates || updates.isChecking)
                .buttonStyle(.borderedProminent)
                .tint(accent)
                .controlSize(.large)
            }
        }
        .padding(28)
        .frame(width: 440)
        .fixedSize(horizontal: false, vertical: true)
    }
}
