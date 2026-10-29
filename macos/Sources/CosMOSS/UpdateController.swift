import AppKit
import Combine
import Sparkle
import SwiftUI

@MainActor
final class UpdateController: NSObject, ObservableObject, SPUUpdaterDelegate {
    static let shared = UpdateController()
    @Published private(set) var canCheckForUpdates = false
    @Published private(set) var automaticallyChecksForUpdates = true
    @Published private(set) var automaticallyDownloadsUpdates = false
    @Published private(set) var isChecking = false
    @Published private(set) var updateAvailable = false
    @Published private(set) var status = "새 버전을 확인합니다."
    private var observers: Set<AnyCancellable> = []
    private var updateWindow: NSWindow?
    private var manualCheck = false
    private var installationTask: Task<Void, Never>?
    private var started = false
    private var pendingCheck = false
    var isDevelopmentBuild: Bool { Bundle.main.object(forInfoDictionaryKey: "CosMOSSProjectRoot") != nil }
    private lazy var controller = SPUStandardUpdaterController(
        startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil
    )

    func start() {
        guard !started else { return }
        started = true
        controller.updater.publisher(for: \.canCheckForUpdates)
            .receive(on: RunLoop.main)
            .sink { [weak self] value in
                guard let self else { return }
                self.canCheckForUpdates = value
                if value && self.pendingCheck { self.refreshUpdateInformation() }
            }
            .store(in: &observers)
        controller.updater.publisher(for: \.automaticallyChecksForUpdates)
            .receive(on: RunLoop.main)
            .sink { [weak self] value in self?.automaticallyChecksForUpdates = value }
            .store(in: &observers)
        controller.updater.publisher(for: \.automaticallyDownloadsUpdates)
            .receive(on: RunLoop.main)
            .sink { [weak self] value in self?.automaticallyDownloadsUpdates = value }
            .store(in: &observers)
        automaticallyChecksForUpdates = controller.updater.automaticallyChecksForUpdates
        automaticallyDownloadsUpdates = controller.updater.automaticallyDownloadsUpdates
        // Preferences and the window remain available, but a development
        // launcher must never replace the checkout-based app.
        guard !isDevelopmentBuild else {
            status = "개발용 앱입니다. 배포 버전은 릴리스 페이지에서 확인할 수 있습니다."
            return
        }
        controller.startUpdater()
    }

    func checkForUpdates() {
        start()
        if updateWindow == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 380),
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
        // Sparkle persists the user's choice; checking does not grant installation consent.
        controller.updater.automaticallyChecksForUpdates = enabled
        automaticallyChecksForUpdates = controller.updater.automaticallyChecksForUpdates
    }

    func setAutomaticDownloads(_ enabled: Bool) {
        controller.updater.automaticallyDownloadsUpdates = enabled
        automaticallyDownloadsUpdates = controller.updater.automaticallyDownloadsUpdates
    }

    func refreshUpdateInformation() {
        guard !isDevelopmentBuild, !isChecking else { return }
        guard canCheckForUpdates, !controller.updater.sessionInProgress else {
            pendingCheck = true
            status = "업데이트 확인을 준비하고 있습니다…"
            return
        }
        pendingCheck = false
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
        guard manualCheck else {
            if pendingCheck {
                DispatchQueue.main.async { [weak self] in self?.refreshUpdateInformation() }
            }
            return
        }
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
                Text(updates.status).font(.system(size: 13))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("update-status")
            }
            .frame(minHeight: 20, alignment: .leading)
            Divider()
            VStack(alignment: .leading, spacing: 7) {
                Toggle("새 버전 자동 알림", isOn: Binding(
                    get: { updates.automaticallyChecksForUpdates },
                    set: { updates.setAutomaticChecks($0) }
                ))
                .toggleStyle(.checkbox)
                .font(.system(size: 13))
                .tint(accent)
                .accessibilityIdentifier("automatic-updates")
                Text(updates.isDevelopmentBuild
                     ? "설정은 저장되며, 자동 확인은 배포용 앱에서 적용됩니다."
                     : "앱 실행 중 새 버전을 확인하고 알려줍니다.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: 7) {
                Toggle("자동 다운로드 및 설치", isOn: Binding(
                    get: { updates.automaticallyDownloadsUpdates },
                    set: { updates.setAutomaticDownloads($0) }
                ))
                .toggleStyle(.checkbox).font(.system(size: 13)).tint(accent)
                .disabled(!updates.automaticallyChecksForUpdates)
                .accessibilityIdentifier("automatic-downloads")
                Text(updates.automaticallyChecksForUpdates
                     ? "선택하면 새 버전을 내려받고 작업을 마친 뒤 앱 종료 시 설치합니다."
                     : "자동 알림을 다시 켜면 저장된 다운로드·설치 선택이 적용됩니다.")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Link("변경 내용", destination: URL(string: "https://github.com/hoonably/cosMOSS/releases/latest")!)
                    .font(.system(size: 12)).foregroundStyle(accent)
                Spacer()
                if updates.isDevelopmentBuild {
                    Button("릴리스 보기") {
                        NSWorkspace.shared.open(URL(string: "https://github.com/hoonably/cosMOSS/releases/latest")!)
                    }
                    .buttonStyle(.borderedProminent).tint(accent).controlSize(.large)
                } else {
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
        }
        .padding(28)
        .frame(width: 440)
        .fixedSize(horizontal: false, vertical: true)
    }
}
