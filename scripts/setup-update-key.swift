#!/usr/bin/env swift
import CryptoKit
import Foundation
import Security

let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let defaultKey = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/cosMOSS Development/sparkle-private-key").path
let keyURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["SPARKLE_PRIVATE_KEY_FILE"] ?? defaultKey)
let publicURL = root.appendingPathComponent("macos/UpdatePublicKey.txt")
let previousPublicKey = try? String(contentsOf: publicURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
let replaceLostKey = CommandLine.arguments.dropFirst(2).contains("--replace-lost-key")
let service = "io.github.hoonably.cosmoss.sparkle-update-key"

func keychainQuery(_ publicKey: String) -> [String: Any] {
    [kSecClass as String: kSecClassGenericPassword,
     kSecAttrService as String: service, kSecAttrAccount as String: publicKey]
}

func restoredKey(_ publicKey: String) throws -> Curve25519.Signing.PrivateKey? {
    var query = keychainQuery(publicKey)
    query[kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return nil }
    guard status == errSecSuccess, let data = result as? Data else {
        fatalError("Could not read update-key backup from login Keychain (status \(status)); unlock Keychain and retry")
    }
    let key = try Curve25519.Signing.PrivateKey(rawRepresentation: data)
    guard key.publicKey.rawRepresentation.base64EncodedString() == publicKey else { fatalError("Keychain backup does not match public key") }
    return key
}

func writePrivateKey(_ key: Curve25519.Signing.PrivateKey) throws {
    try FileManager.default.createDirectory(at: keyURL.deletingLastPathComponent(), withIntermediateDirectories: true,
                                           attributes: [.posixPermissions: 0o700])
    guard FileManager.default.createFile(atPath: keyURL.path,
                                        contents: Data(key.rawRepresentation.base64EncodedString().utf8),
                                        attributes: [.posixPermissions: 0o600]) else { fatalError("Could not save signing key") }
}

let key: Curve25519.Signing.PrivateKey
var replacedLostKey = false
if FileManager.default.fileExists(atPath: keyURL.path) {
    let encoded = try String(contentsOf: keyURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard let data = Data(base64Encoded: encoded) else { fatalError("Invalid update signing key") }
    key = try Curve25519.Signing.PrivateKey(rawRepresentation: data)
} else if let previousPublicKey, let restored = try restoredKey(previousPublicKey) {
    key = restored
    try writePrivateKey(key)
    print("Restored the existing cosMOSS update key from login Keychain")
} else {
    guard previousPublicKey == nil || replaceLostKey else {
        fatalError("Restore the existing cosMOSS private key; never rotate it without an explicit manual-reinstall migration")
    }
    key = Curve25519.Signing.PrivateKey()
    try writePrivateKey(key)
    replacedLostKey = previousPublicKey != nil
}
let publicKey = key.publicKey.rawRepresentation.base64EncodedString() + "\n"
if FileManager.default.fileExists(atPath: publicURL.path) && !replacedLostKey {
    guard try String(contentsOf: publicURL, encoding: .utf8) == publicKey else { fatalError("Update signing keys do not match") }
} else {
    try publicKey.write(to: publicURL, atomically: true, encoding: .utf8)
}
try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: keyURL.path)
var backup = keychainQuery(publicKey.trimmingCharacters(in: .whitespacesAndNewlines))
backup[kSecValueData as String] = key.rawRepresentation
backup[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
let status = SecItemAdd(backup as CFDictionary, nil)
if status == errSecDuplicateItem {
    guard let existing = try restoredKey(publicKey.trimmingCharacters(in: .whitespacesAndNewlines)),
          existing.rawRepresentation == key.rawRepresentation else { fatalError("Existing Keychain backup differs") }
} else if status != errSecSuccess {
    fatalError("Could not back up update key in login Keychain (status \(status)); private key file is intact")
}
print("cosMOSS update key ready and backed up in login Keychain; only the public key is stored in Git")
