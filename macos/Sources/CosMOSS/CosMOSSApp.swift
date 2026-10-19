import AppKit
import SwiftUI
import WebKit

@main
struct CosMOSSApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var backend = BackendController.shared

    var body: some Scene {
        WindowGroup("cosMOSS") {
            Group {
                if let url = backend.url {
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
            .frame(minWidth: 980, minHeight: 680)
            .task { await backend.start() }
        }
        .defaultSize(width: 1280, height: 840)
        .commands { CommandGroup(replacing: .newItem) {} }
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

        guard let root = Bundle.main.object(forInfoDictionaryKey: "CosMOSSProjectRoot") as? String else {
            error = "체크아웃에서 ./scripts/build-mac-app.sh 로 개발용 앱을 빌드해 주세요."
            return
        }
        let projectURL = URL(fileURLWithPath: root, isDirectory: true)
        let script = projectURL.appendingPathComponent("scripts/run-app.sh")
        guard FileManager.default.fileExists(atPath: script.path) else {
            error = "프로젝트 폴더를 옮겼다면 ./scripts/build-mac-app.sh 로 다시 빌드해 주세요."
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
            let logs = projectURL.appendingPathComponent("logs", isDirectory: true)
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
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = [script.path]
            process.currentDirectoryURL = projectURL
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
                + ":/opt/homebrew/bin:/usr/local/bin"
            environment["COSMOSS_DESKTOP_HOST"] = "127.0.0.1"
            environment["COSMOSS_DESKTOP_PORT"] = String(port)
            process.environment = environment
            process.standardOutput = log
            process.standardError = log
            try process.run()
            self.process = process

            for _ in 0..<60 {
                guard process.isRunning else {
                    throw StartupError.failed("앱 서버가 종료되었습니다. ./scripts/setup-app.sh 실행 여부와 logs/desktop.log를 확인해 주세요.")
                }
                if await health(at: address) == .ready {
                    url = address
                    return
                }
                try await Task.sleep(nanoseconds: 500_000_000)
            }
            throw StartupError.failed("앱 서버가 준비되지 않았습니다. logs/desktop.log를 확인해 주세요.")
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
        let webView = WKWebView()
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
        let origin: URL
        init(origin: URL) { self.origin = origin }

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
            let panel = NSOpenPanel()
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.begin { result in completionHandler(result == .OK ? panel.urls : nil) }
        }
    }
}
