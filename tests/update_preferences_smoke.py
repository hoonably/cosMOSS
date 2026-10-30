"""Exercise the production Swift updater with isolated preferences and signed feeds."""
import functools
import http.server
import os
import pathlib
import plistlib
import shutil
import subprocess
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRAMEWORK = ROOT / "macos/.build/artifacts/sparkle/Sparkle/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework"


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    identity = "io.github.hoonably.cosmoss.update-preferences-checks." + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="cosmoss-update-preferences-") as directory:
        root = pathlib.Path(directory)
        feed = pathlib.Path(os.environ.get('COSMOSS_TEST_FEED', ROOT / "appcast.xml")).read_text()
        namespace = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'
        item = max(ET.fromstring(feed).findall('channel/item'), key=lambda value: int(value.findtext(namespace + 'version')))
        build = int(item.findtext(namespace + 'version'))
        version = item.findtext(namespace + 'shortVersionString')
        (root / "appcast.xml").write_text(feed)
        (root / "tampered.xml").write_text(feed.replace("<title>cosMOSS</title>", "<title>tampered</title>", 1))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=directory))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            bundle = root / "UpdatePreferencesChecks.app"
            contents = bundle / "Contents"
            executable = contents / "MacOS/UpdatePreferencesChecks"
            executable.parent.mkdir(parents=True)
            shutil.copytree(FRAMEWORK, contents / "Frameworks/Sparkle.framework", symlinks=True)
            subprocess.run([
                "swiftc", "-parse-as-library", "-target", "arm64-apple-macosx13.0",
                "-F", str(FRAMEWORK.parent), "-framework", "Sparkle",
                "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks",
                str(ROOT / "macos/Sources/CosMOSS/UpdateController.swift"),
                str(ROOT / "tests/UpdatePreferencesChecks.swift"), "-o", str(executable),
            ], check=True)
            for mode in ("default", "legacy-on", "legacy-on-restored", "latest", "available", "error", "development"):
                info = {
                    "CFBundleIdentifier": identity,
                    "CFBundleName": "cosMOSS Update Preferences Checks",
                    "CFBundleExecutable": "UpdatePreferencesChecks",
                    "CFBundlePackageType": "APPL",
                    "CFBundleVersion": str(build - 1 if mode == "available" else build),
                    "CFBundleShortVersionString": os.environ.get('COSMOSS_TEST_PREVIOUS_VERSION', version) if mode == "available" else version,
                    "LSMinimumSystemVersion": "13.0",
                    "SUFeedURL": f"http://127.0.0.1:{server.server_port}/" + ("tampered.xml" if mode == "error" else "appcast.xml"),
                    "SUPublicEDKey": (ROOT / "macos/UpdatePublicKey.txt").read_text().strip(),
                    "SUEnableAutomaticChecks": True,
                    "SUAutomaticallyUpdate": False,
                    "SURequireSignedFeed": True,
                    "SUVerifyUpdateBeforeExtraction": True,
                    "SUSignedFeedFailureExpirationInterval": 0,
                    "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
                }
                if mode == "development":
                    info["CosMOSSProjectRoot"] = str(ROOT)
                with (contents / "Info.plist").open("wb") as stream:
                    plistlib.dump(info, stream)
                subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True, capture_output=True)
                result = subprocess.run([str(executable), mode], capture_output=True, text=True, timeout=25)
                if result.returncode:
                    raise AssertionError(f"{mode}: {result.stdout}\n{result.stderr}")
                print(result.stdout.strip(), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            subprocess.run(["defaults", "delete", identity], capture_output=True)


if __name__ == "__main__":
    main()
