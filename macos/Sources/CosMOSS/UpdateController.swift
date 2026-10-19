import AppKit
import Combine
import Sparkle

@MainActor
final class UpdateController: NSObject, ObservableObject, SPUUpdaterDelegate {
    static let shared = UpdateController()
    @Published private(set) var canCheckForUpdates = false
    private var observer: AnyCancellable?
    private var installationTask: Task<Void, Never>?
    private var started = false
    private lazy var controller = SPUStandardUpdaterController(
        startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil
    )

    func start() {
        // A development launcher must never replace a checkout-based app.
        guard !started, Bundle.main.object(forInfoDictionaryKey: "CosMOSSProjectRoot") == nil else { return }
        started = true
        observer = controller.updater.publisher(for: \.canCheckForUpdates)
            .receive(on: RunLoop.main)
            .sink { [weak self] value in self?.canCheckForUpdates = value }
        controller.startUpdater()
    }

    func checkForUpdates() {
        guard canCheckForUpdates else { return }
        controller.checkForUpdates(nil)
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
