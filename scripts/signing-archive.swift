#!/usr/bin/env swift
// The existing update secret encrypts a separate code-signing identity. It is
// never reused as an RSA key, changed, or written into the repository.
import CryptoKit
import Foundation

do {
    guard CommandLine.arguments.count == 5 else { throw NSError(domain: "Arguments", code: 1) }
    let mode = CommandLine.arguments[1]
    let root = URL(fileURLWithPath: CommandLine.arguments[2])
    let input = URL(fileURLWithPath: CommandLine.arguments[3])
    let output = URL(fileURLWithPath: CommandLine.arguments[4])
    let config = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("release.json"))) as! [String: Any]
    let fallback = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/\(config["key_directory"] as! String)/sparkle-private-key")
    let keyURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment[config["key_env"] as! String] ?? fallback.path)
    let text = try String(contentsOf: keyURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard let raw = Data(base64Encoded: text), raw.count == 32 else { throw NSError(domain: "Key", code: 1) }
    let signingKey = try Curve25519.Signing.PrivateKey(rawRepresentation: raw)
    let pinned = try String(contentsOf: root.appendingPathComponent(config["public_key_file"] as! String), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    guard signingKey.publicKey.rawRepresentation.base64EncodedString() == pinned else { throw NSError(domain: "Key", code: 2) }
    let key = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: raw),
        salt: Data((config["bundle_identifier"] as! String).utf8),
        info: Data("cosMOSS/code-signing-archive/AES-256-GCM/v1".utf8), outputByteCount: 32)
    let data = try Data(contentsOf: input)
    let result: Data
    if mode == "encrypt" {
        guard let sealed = try AES.GCM.seal(data, using: key).combined else { throw NSError(domain: "Seal", code: 1) }
        result = sealed
    } else if mode == "decrypt" {
        result = try AES.GCM.open(AES.GCM.SealedBox(combined: data), using: key)
    } else { throw NSError(domain: "Arguments", code: 2) }
    guard FileManager.default.createFile(atPath: output.path, contents: result, attributes: [.posixPermissions: 0o600]) else { throw NSError(domain: "Output", code: 1) }
} catch {
    fputs("Cannot authenticate the signing archive. Restore the existing pinned update key; no identity was changed.\n", stderr)
    exit(1)
}
