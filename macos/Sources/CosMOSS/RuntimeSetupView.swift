import AppKit
import SwiftUI

struct RuntimeSetupView: View {
    let installing: Bool
    let status: String
    let error: String?
    let logURL: URL?
    let install: () -> Void

    var body: some View {
        VStack(spacing: 18) {
            if installing {
                ProgressView().controlSize(.small)
                Text(status)
                    .font(.system(size: 14))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .accessibilityIdentifier("runtime-install-status")
            } else {
                Text(error == nil ? "처음 실행에 필요한 파일을 설치해 주세요." : "실행 환경을 설치하지 못했어요.")
                    .font(.system(size: 15, weight: .medium))
                if let error {
                    Text(error)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 420)
                }
                Button(error == nil ? "설치하고 시작하기" : "다시 시도", action: install)
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                if error != nil, let logURL {
                    Button("로그 보기") { NSWorkspace.shared.open(logURL) }
                        .buttonStyle(.link)
                        .font(.system(size: 12))
                }
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
