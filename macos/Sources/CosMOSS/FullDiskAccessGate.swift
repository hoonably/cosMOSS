import AppKit
import Darwin
import SwiftUI

/// Check before creating the WebView or backend: either can touch Downloads.
@MainActor
final class FullDiskAccessGate: ObservableObject {
    @Published private(set) var isReady = false
    @Published private(set) var isChecking = false
    @Published private(set) var hasChecked = false
    @Published private(set) var isRestarting = false
    @Published private(set) var message: String?
    private var pending: Task<Bool, Never>?
    private let probe: @Sendable () -> Bool

    init(probe: @escaping @Sendable () -> Bool = { FullDiskAccessGate.probeAuthorization() }) {
        self.probe = probe
    }

    /// A readable Downloads/Voice Memos directory alone does not prove FDA.
    /// Open an FDA-protected sentinel, then close it without reading its bytes.
    /// Never inspect or change privacy database contents, or use private TCC APIs.
    nonisolated static func probeAuthorization() -> Bool {
        let candidates = [URL(fileURLWithPath: "/Library/Application Support/com.apple.TCC/TCC.db"),
                          FileManager.default.homeDirectoryForCurrentUser
                            .appendingPathComponent("Library/Application Support/com.apple.TCC/TCC.db")]
        return candidates.contains { canReadRegularFile($0) }
    }

    nonisolated static func canReadRegularFile(_ file: URL) -> Bool {
        let descriptor = Darwin.open(file.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else { return false }
        defer { Darwin.close(descriptor) }
        var info = stat()
        return fstat(descriptor, &info) == 0 && (info.st_mode & S_IFMT) == S_IFREG
    }

    @discardableResult
    func check() async -> Bool {
        // Startup permission is checked each process launch. Do not interrupt
        // active transcription if a user changes Settings while a job runs.
        guard !isRestarting else { return false }
        if isReady { return true }
        if let pending { return await pending.value }
        isChecking = true
        let probe = self.probe
        let task = Task.detached(priority: .userInitiated) { probe() }
        pending = task
        let ready = await task.value
        isReady = ready
        hasChecked = true
        isChecking = false
        pending = nil
        return ready
    }

    func restart() {
        guard !isReady, !isChecking, !isRestarting else { return }
        isRestarting = true
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.createsNewApplicationInstance = true
        NSWorkspace.shared.openApplication(at: Bundle.main.bundleURL, configuration: configuration) { [weak self] application, error in
            Task { @MainActor in
                guard error == nil, let application,
                      application.processIdentifier != ProcessInfo.processInfo.processIdentifier else {
                    self?.isRestarting = false
                    self?.message = "앱을 종료한 뒤 다시 실행해 주세요."
                    return
                }
                // This screen precedes backend startup, so no app job is lost.
                NSApp.terminate(nil)
            }
        }
    }
}

struct StartupAccessView: View {
    @ObservedObject var access: FullDiskAccessGate
    let recheck: () -> Void
    var appURL = Bundle.main.bundleURL
    private let ink = Color(red: 0.15, green: 0.24, blue: 0.19)

    var body: some View {
        VStack(spacing: 22) {
            Image(nsImage: NSWorkspace.shared.icon(forFile: appURL.path))
                .resizable().frame(width: 72, height: 72)
            Text("cosMOSS").font(.system(size: 30, weight: .bold)).foregroundColor(ink)
            Text("전체 디스크 접근 권한을 허용해 주세요")
                .font(.system(size: 19, weight: .semibold)).foregroundColor(ink)
            Text("음성 메모와 기본 경로의 파일을 불러오고 저장하는 데 사용합니다.\n허용하면 앱을 시작합니다.")
                .font(.system(size: 13)).foregroundColor(.secondary).multilineTextAlignment(.center).lineSpacing(5)
            HStack(spacing: 12) {
                Button("권한 설정하기") {
                    _ = FullDiskAccessAssistant.shared.present(relativeTo: NSApp.keyWindow, onComplete: recheck)
                }.buttonStyle(GuideButtonStyle(prominent: true)).disabled(access.isRestarting)
                Button(access.isChecking ? "확인 중…" : "다시 확인", action: recheck)
                    .buttonStyle(GuideButtonStyle(prominent: false)).disabled(access.isChecking || access.isRestarting)
            }
            VStack(spacing: 8) {
                Text(access.message ?? "설정 후에도 이 화면이 보이면 앱을 다시 시작하세요.")
                    .font(.system(size: 11)).foregroundColor(.secondary)
                Button("앱 다시 시작") { access.restart() }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundColor(ink)
                    .disabled(access.isChecking || access.isRestarting)
            }
        }
        .padding(36).frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(red: 0.95, green: 0.97, blue: 0.94))
        .environment(\.colorScheme, .light)
    }
}
