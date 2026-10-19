#!/usr/bin/env swift
import CryptoKit
import Foundation

let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let defaultKey = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/cosMOSS Development/sparkle-private-key").path
let keyURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SPARKLE_PRIVATE_KEY_FILE"] ?? defaultKey)
let publicURL = root.appendingPathComponent("macos/UpdatePublicKey.txt")
let key: Curve25519.Signing.PrivateKey
if FileManager.default.fileExists(atPath: keyURL.path) {
    let encoded = try String(contentsOf: keyURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard let data = Data(base64Encoded: encoded) else { fatalError("Invalid update signing key") }
    key = try Curve25519.Signing.PrivateKey(rawRepresentation: data)
} else {
    guard !FileManager.default.fileExists(atPath: publicURL.path) else {
        fatalError("Restore the existing cosMOSS private key; do not replace its public key")
    }
    key = Curve25519.Signing.PrivateKey()
    try FileManager.default.createDirectory(at: keyURL.deletingLastPathComponent(), withIntermediateDirectories: true,
                                           attributes: [.posixPermissions: 0o700])
    guard FileManager.default.createFile(atPath: keyURL.path,
                                        contents: Data(key.rawRepresentation.base64EncodedString().utf8),
                                        attributes: [.posixPermissions: 0o600]) else { fatalError("Could not save signing key") }
}
let publicKey = key.publicKey.rawRepresentation.base64EncodedString() + "\n"
if FileManager.default.fileExists(atPath: publicURL.path) {
    guard try String(contentsOf: publicURL, encoding: .utf8) == publicKey else { fatalError("Update signing keys do not match") }
} else {
    try publicKey.write(to: publicURL, atomically: true, encoding: .utf8)
}
print("cosMOSS update key ready; only the public key is stored in the repository")
