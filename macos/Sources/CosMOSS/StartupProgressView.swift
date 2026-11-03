import SwiftUI

struct StartupProgressView: View {
    let status: String

    var body: some View {
        VStack(spacing: 18) {
            ProgressView().controlSize(.small)
            Text(status)
                .font(.system(size: 14))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .accessibilityIdentifier("startup-status")
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
