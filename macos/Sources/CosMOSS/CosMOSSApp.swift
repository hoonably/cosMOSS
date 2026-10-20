import AppKit
import SwiftUI
import UniformTypeIdentifiers
import WebKit

@main
struct CosMOSSApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var backend = BackendController.shared
    @StateObject private var updates = UpdateController.shared
    @StateObject private var fileAccess = FullDiskAccessGate()

    var body: some Scene {
        WindowGroup("cosMOSS") {
            Group {
                if !fileAccess.isReady {
                    if fileAccess.hasChecked {
                        StartupAccessView(access: fileAccess) { Task { await startAfterPermission() } }
                    } else {
                        ProgressView("접근 권한을 확인하고 있어요")
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                } else if let url = backend.url {
                    WorkspaceView(url: url)
                } else if let error = backend.error {
                    VStack(spacing: 18) {
                        Image(systemName: "waveform.badge.exclamationmark")
                            .font(.system(size: 40))
                        Text("cosMOSS를 시작하지 못했어요").font(.title2)
                        Text(error).textSelection(.enabled).multilineTextAlignment(.center)
                        Button("다시 시도") { Task { await backend.start() } }
                        if let logURL = backend.logURL {
                            Button("로그 보기") { NSWorkspace.shared.open(logURL) }
                        }
                    }.padding(40).frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    VStack(spacing: 16) {
                        ProgressView()
                        Text("cosMOSS를 준비하고 있어요")
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(minWidth: 1100, minHeight: 720)
            .task { await startAfterPermission() }
            .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
                if !fileAccess.isReady { Task { await startAfterPermission() } }
            }
        }
        .defaultSize(width: 1480, height: 900)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .appInfo) {
                Button("업데이트 확인…") { updates.checkForUpdates() }
                    .disabled(!updates.canCheckForUpdates)
            }
        }
    }

    @MainActor
    private func startAfterPermission() async {
        guard await fileAccess.check() else { return }
        if FullDiskAccessAssistant.shared.window?.isVisible == true {
            FullDiskAccessAssistant.shared.close()
        }
        await backend.start()
        updates.start()
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        BackendController.shared.stop()
    }
}

@MainActor
final class BackendController: ObservableObject {
    static let shared = BackendController()
    @Published var url: URL?
    @Published var error: String?
    private(set) var logURL: URL?
    private var process: Process?
    private var log: FileHandle?
    private var starting = false
    weak var webView: WKWebView?

    func prepareForUpdate() async -> Bool {
        guard let url else { return !starting }
        var request = URLRequest(url: url.appendingPathComponent("api/update-status"))
        request.timeoutInterval = 3
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let status = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  status["busy"] as? Bool == false else { return false }
            if let webView {
                let ready: Any = try await withCheckedThrowingContinuation { continuation in
                    webView.callAsyncJavaScript(
                        "return typeof window.prepareForAppUpdate === 'function' && await window.prepareForAppUpdate();",
                        arguments: [:], in: nil, in: .page
                    ) { result in continuation.resume(with: result) }
                }
                return ready as? Bool == true
            }
            return true
        } catch { return false }
    }

    private enum Health { case ready, occupied, unavailable }

    private func health(at url: URL) async -> Health {
        var request = URLRequest(url: url.appendingPathComponent("healthz"))
        request.timeoutInterval = 1
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let response = response as? HTTPURLResponse, response.statusCode == 200,
                  let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  payload["app"] as? String == "cosMOSS", payload["ok"] as? Bool == true
            else { return .occupied }
            return .ready
        } catch { return .unavailable }
    }

    func start() async {
        guard !starting, url == nil else { return }
        starting = true
        error = nil
        defer { starting = false }
        stop()

        let projectURL: URL
        let workingURL: URL
        let executable: URL
        let arguments: [String]
        let bundled: Bool
        if let root = Bundle.main.object(forInfoDictionaryKey: "CosMOSSProjectRoot") as? String {
            projectURL = URL(fileURLWithPath: root, isDirectory: true)
            workingURL = projectURL
            executable = URL(fileURLWithPath: "/bin/bash")
            let script = projectURL.appendingPathComponent("scripts/run-app.sh")
            guard FileManager.default.fileExists(atPath: script.path) else {
                error = "프로젝트 폴더를 옮겼다면 ./scripts/build-mac-app.sh 로 다시 빌드해 주세요."
                return
            }
            arguments = [script.path]
            bundled = false
        } else if let resources = Bundle.main.resourceURL {
            projectURL = resources.appendingPathComponent("app", isDirectory: true)
            workingURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("cosMOSS", isDirectory: true)
            executable = resources.appendingPathComponent("python/bin/python3.12")
            arguments = ["-m", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port"]
            bundled = true
            guard FileManager.default.isExecutableFile(atPath: executable.path) else {
                error = "앱 실행 환경을 찾지 못했습니다. ZIP을 다시 풀어 앱 전체를 복사해 주세요."
                return
            }
        } else {
            error = "앱 파일을 확인해 주세요."
            return
        }

        let port = Int(ProcessInfo.processInfo.environment["COSMOSS_DESKTOP_PORT"] ?? "18760") ?? 18760
        guard (1024...65535).contains(port),
              let address = URL(string: "http://127.0.0.1:\(port)") else {
            error = "COSMOSS_DESKTOP_PORT는 1024~65535 범위의 포트여야 합니다."
            return
        }
        switch await health(at: address) {
        case .ready:
            url = address
            return
        case .occupied:
            error = "포트 \(port)를 다른 앱이 사용 중입니다. COSMOSS_DESKTOP_PORT를 바꿔 실행해 주세요."
            return
        case .unavailable: break
        }

        do {
            let logs = workingURL.appendingPathComponent("logs", isDirectory: true)
            try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
            let logURL = logs.appendingPathComponent("desktop.log")
            self.logURL = logURL
            if !FileManager.default.fileExists(atPath: logURL.path) {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            let log = try FileHandle(forWritingTo: logURL)
            try log.seekToEnd()
            self.log = log
            let process = Process()
            process.executableURL = executable
            process.arguments = bundled ? arguments + [String(port), "--workers", "1"] : arguments
            process.currentDirectoryURL = workingURL
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
                + ":/opt/homebrew/bin:/usr/local/bin"
            environment["COSMOSS_DESKTOP_HOST"] = "127.0.0.1"
            environment["COSMOSS_DESKTOP_PORT"] = String(port)
            if bundled, let resources = Bundle.main.resourceURL {
                environment.removeValue(forKey: "PYTHONHOME")
                environment["PYTHONPATH"] = projectURL.path
                environment["PYTHONNOUSERSITE"] = "1"
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["MOSS_DATA_DIR"] = workingURL.appendingPathComponent("data").path
                environment["MOSS_RUNTIME_DIR"] = workingURL.path
                environment["MLX_VENV"] = workingURL.appendingPathComponent(".venv-mlx").path
                environment["UV_BIN"] = resources.appendingPathComponent("tools/bin/uv").path
                environment["UV_CACHE_DIR"] = workingURL.appendingPathComponent(".cache/uv").path
                environment["UV_PYTHON_INSTALL_DIR"] = workingURL.appendingPathComponent(".cache/uv/python").path
            }
            process.environment = environment
            process.standardOutput = log
            process.standardError = log
            try process.run()
            self.process = process

            for _ in 0..<60 {
                guard process.isRunning else {
                    throw StartupError.failed("앱 실행 환경이 종료되었습니다. 로그 보기를 눌러 확인해 주세요.")
                }
                if await health(at: address) == .ready {
                    url = address
                    return
                }
                try await Task.sleep(nanoseconds: 500_000_000)
            }
            throw StartupError.failed("앱을 시작하지 못했습니다. 로그 보기를 눌러 확인해 주세요.")
        } catch {
            self.error = error.localizedDescription
            stop()
        }
    }

    func stop() {
        if let process, process.isRunning { process.terminate() }
        process = nil
        try? log?.close()
        log = nil
    }

    private enum StartupError: LocalizedError {
        case failed(String)
        var errorDescription: String? {
            switch self { case .failed(let message): return message }
        }
    }
}

struct WorkspaceView: NSViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator(origin: url) }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        for name in ["chooseModelFolder", "getSaveLocation", "chooseSaveLocation", "revealTextFile", "getAppInfo", "checkForUpdates", "openVoiceMemos", "requestVoiceMemosAccess", "openVoiceMemoPrivacy"] {
            configuration.userContentController.addScriptMessageHandler(context.coordinator, contentWorld: .page, name: name)
        }
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.load(URLRequest(url: url))
        BackendController.shared.webView = webView
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandlerWithReply {
        let origin: URL
        init(origin: URL) { self.origin = origin }

        private let saveDirectoryKey = "cosmoss.defaultTextSaveDirectory"
        private var textSaveDirectory: URL {
            if let path = UserDefaults.standard.string(forKey: saveDirectoryKey), !path.isEmpty {
                return URL(fileURLWithPath: path, isDirectory: true)
            }
            return FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads", isDirectory: true)
        }
        private func locationInfo(_ url: URL) -> [String: String] {
            ["path": url.path, "name": url.lastPathComponent]
        }

        private func probeVoiceMemoAccess(completion: @escaping (Bool) -> Void) {
            DispatchQueue.global(qos: .userInitiated).async {
                let home = FileManager.default.homeDirectoryForCurrentUser
                let folders = [
                    "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings",
                    "Library/Containers/com.apple.VoiceMemos/Data/Library/Application Support/Recordings",
                    "Library/Application Support/com.apple.voicememos/Recordings",
                ]
                let allowed = folders.contains { folder in
                    (try? FileManager.default.contentsOfDirectory(atPath: home.appendingPathComponent(folder).path)) != nil
                }
                DispatchQueue.main.async { completion(allowed) }
            }
        }

        func userContentController(_ userContentController: WKUserContentController,
                                   didReceive message: WKScriptMessage,
                                   replyHandler: @escaping (Any?, String?) -> Void) {
            guard ["chooseModelFolder", "getSaveLocation", "chooseSaveLocation", "revealTextFile", "getAppInfo", "checkForUpdates", "openVoiceMemos", "requestVoiceMemosAccess", "openVoiceMemoPrivacy"].contains(message.name), message.frameInfo.isMainFrame,
                  let url = message.frameInfo.request.url,
                  url.scheme == origin.scheme, url.host == origin.host, url.port == origin.port else {
                replyHandler(nil, "이 앱에서만 폴더 설정을 사용할 수 있습니다.")
                return
            }
            if message.name == "getAppInfo" {
                replyHandler(["version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
                              "updatesAvailable": Bundle.main.object(forInfoDictionaryKey: "CosMOSSProjectRoot") == nil,
                              "canCheckForUpdates": UpdateController.shared.canCheckForUpdates], nil)
                return
            }
            if message.name == "checkForUpdates" {
                guard UpdateController.shared.canCheckForUpdates else {
                    replyHandler(nil, "잠시 후 다시 확인해 주세요.")
                    return
                }
                UpdateController.shared.checkForUpdates()
                replyHandler(true, nil)
                return
            }
            if message.name == "openVoiceMemoPrivacy" {
                let opened = FullDiskAccessAssistant.shared.present(relativeTo: message.webView?.window) { [weak webView = message.webView] in
                    guard let webView else { return }
                    webView.evaluateJavaScript("window.dispatchEvent(new Event('cosmoss-voice-memo-permission-return'));")
                }
                replyHandler(opened, nil)
                return
            }
            if message.name == "requestVoiceMemosAccess" {
                probeVoiceMemoAccess { allowed in replyHandler(allowed, nil) }
                return
            }
            if message.name == "openVoiceMemos" {
                guard let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.VoiceMemos") else {
                    replyHandler(nil, "음성 메모 앱을 찾을 수 없습니다.")
                    return
                }
                NSWorkspace.shared.openApplication(at: app, configuration: NSWorkspace.OpenConfiguration())
                replyHandler(true, nil)
                return
            }
            if message.name == "getSaveLocation" {
                replyHandler(locationInfo(textSaveDirectory), nil)
                return
            }
            if message.name == "revealTextFile" {
                guard let body = message.body as? [String: Any], let path = body["path"] as? String,
                      path.hasPrefix("/"), ["txt", "srt", "vtt", "md"].contains(URL(fileURLWithPath: path).pathExtension.lowercased()) else {
                    replyHandler(nil, "파일 경로를 확인해 주세요.")
                    return
                }
                var isDirectory: ObjCBool = false
                guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), !isDirectory.boolValue else {
                    replyHandler(nil, "파일을 찾을 수 없습니다.")
                    return
                }
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                replyHandler(true, nil)
                return
            }
            let selectingSaveLocation = message.name == "chooseSaveLocation"
            let panel = NSOpenPanel()
            panel.title = selectingSaveLocation ? "기본 경로 선택" : "다운로드한 모델 폴더 선택"
            panel.message = selectingSaveLocation ? "파일을 저장하고, 불러올 기본 경로를 지정하세요." : "config.json과 모델 가중치가 있는 MOSS MLX 폴더를 선택하세요."
            panel.prompt = "이 폴더 사용"
            if selectingSaveLocation {
                panel.directoryURL = textSaveDirectory
                panel.canCreateDirectories = true
            }
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.allowsMultipleSelection = false
            panel.begin { result in
                guard result == .OK, let selected = panel.url else {
                    replyHandler(nil, nil)
                    return
                }
                if selectingSaveLocation {
                    guard FileManager.default.isWritableFile(atPath: selected.path) else {
                        replyHandler(nil, "선택한 폴더에 저장할 권한이 없습니다. 다른 폴더를 선택해 주세요.")
                        return
                    }
                    UserDefaults.standard.set(selected.path, forKey: self.saveDirectoryKey)
                    replyHandler(self.locationInfo(selected), nil)
                } else {
                    replyHandler(selected.path, nil)
                }
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let target = action.request.url else { decisionHandler(.cancel); return }
            if target.scheme == origin.scheme && target.host == origin.host && target.port == origin.port {
                decisionHandler(action.shouldPerformDownload ? .download : .allow)
            } else {
                if action.navigationType == .linkActivated, ["http", "https"].contains(target.scheme ?? "") {
                    NSWorkspace.shared.open(target)
                }
                decisionHandler(.cancel)
            }
        }

        func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                     decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
            let disposition = (response.response as? HTTPURLResponse)?
                .value(forHTTPHeaderField: "Content-Disposition") ?? ""
            decisionHandler(disposition.lowercased().contains("attachment") || !response.canShowMIMEType ? .download : .allow)
        }

        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                      suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
            let panel = NSSavePanel()
            if ["txt", "srt", "vtt", "md", "mossvoice"].contains((suggestedFilename as NSString).pathExtension.lowercased()) {
                panel.directoryURL = textSaveDirectory
            }
            panel.nameFieldStringValue = suggestedFilename
            panel.begin { result in completionHandler(result == .OK ? panel.url : nil) }
        }

        func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
            // Cancelling the save panel is a normal user action.
            if (error as NSError).code == NSURLErrorCancelled { return }
            let alert = NSAlert()
            alert.messageText = "파일을 저장하지 못했어요"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }

        func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
            guard frame.isMainFrame, let url = frame.request.url,
                  url.scheme == origin.scheme, url.host == origin.host, url.port == origin.port else {
                completionHandler(nil)
                return
            }
            let panel = NSOpenPanel()
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.allowedContentTypes = [.audio, .movie, UTType(exportedAs: "io.github.hoonably.cosmoss.voiceprofile", conformingTo: .json)]
            panel.begin { result in completionHandler(result == .OK ? panel.urls : nil) }
        }

        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor securityOrigin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            guard frame.isMainFrame, securityOrigin.protocol == origin.scheme,
                  securityOrigin.host == origin.host, securityOrigin.port == origin.port,
                  type == .microphone else {
                decisionHandler(.deny)
                return
            }
            decisionHandler(.prompt)
        }
    }
}
