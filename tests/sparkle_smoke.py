"""Use the actual Sparkle framework to discover/reject signed update feeds.

The isolated probe never installs an update or opens the user's application.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import threading
import tomllib
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    major, minor, patch = map(int, version.split('.'))
    build = major * 1_000_000 + minor * 1_000 + patch
    archive = root / f'dist/cosMOSS-v{version}-macos-arm64.zip'
    feed = root / 'appcast.xml'
    public = root / 'macos/UpdatePublicKey.txt'
    verifier = root / 'scripts/verify-update.swift'
    framework = root / 'dist/release/cosMOSS.app/Contents/Frameworks/Sparkle.framework'

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

    with tempfile.TemporaryDirectory(prefix='cosmoss-updater-') as temporary:
        folder = Path(temporary)
        subprocess.run(['swift', str(verifier), str(public), str(feed), str(archive)], check=True)
        tampered_archive = folder / archive.name
        shutil.copy2(archive, tampered_archive)
        with tampered_archive.open('ab') as file:
            file.write(b'tampered')
        result = subprocess.run(['swift', str(verifier), str(public), str(feed), str(tampered_archive)], capture_output=True, text=True)
        assert result.returncode != 0 and 'Invalid update archive signature' in result.stderr
        (folder / 'appcast.xml').write_bytes(feed.read_bytes())
        altered = feed.read_text().replace(f'<title>{version}</title>', '<title>tampered</title>')
        assert altered != feed.read_text()
        (folder / 'tampered.xml').write_text(altered)
        result = subprocess.run(['swift', str(verifier), str(public), str(folder / 'tampered.xml'), str(archive)], capture_output=True, text=True)
        assert result.returncode != 0 and 'Invalid update feed signature' in result.stderr
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(folder)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        app = folder / 'UpdateProbe.app'
        binary = app / 'Contents/MacOS/UpdateProbe'
        binary.parent.mkdir(parents=True)
        destination = app / 'Contents/Frameworks/Sparkle.framework'
        shutil.copytree(framework, destination, symlinks=True)
        subprocess.run(['clang', '-fobjc-arc', '-mmacosx-version-min=13.0', '-F', str(framework.parent),
                        '-framework', 'AppKit', '-framework', 'Sparkle',
                        '-Wl,-rpath,@executable_path/../Frameworks', str(root / 'tests/UpdateProbe.m'), '-o', str(binary)], check=True)
        try:
            for filename, reject in [('appcast.xml', False), ('tampered.xml', True)]:
                metadata = {
                    'CFBundleIdentifier': 'io.github.hoonably.cosmoss.probe.' + uuid.uuid4().hex,
                    'CFBundleName': 'cosMOSS Update Probe', 'CFBundleExecutable': 'UpdateProbe',
                    'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1', 'CFBundleShortVersionString': '0.0.1',
                    'LSMinimumSystemVersion': '13.0',
                    'SUFeedURL': f'http://127.0.0.1:{server.server_port}/{filename}',
                    'SUPublicEDKey': public.read_text().strip(), 'SUEnableAutomaticChecks': False,
                    'SURequireSignedFeed': True, 'SUVerifyUpdateBeforeExtraction': True,
                    'SUSignedFeedFailureExpirationInterval': 0,
                    'NSAppTransportSecurity': {'NSAllowsArbitraryLoads': True},
                }
                (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(metadata))
                subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True, capture_output=True)
                env = dict(os.environ)
                env.pop('EXPECT_REJECTION', None)
                if reject:
                    env['EXPECT_REJECTION'] = '1'
                result = subprocess.run([str(binary)], env=env, capture_output=True, text=True, timeout=25)
                assert result.returncode == 0, result.stdout + result.stderr
                assert ('REJECTED:' if reject else f'FOUND:{version}:{build}') in result.stdout, result.stdout + result.stderr
                print(result.stdout.strip())
        finally:
            server.shutdown()
            server.server_close()
    print('PASS: actual Sparkle discovery, signed-feed enforcement, archive/feed tampering rejected')


if __name__ == '__main__':
    main()
