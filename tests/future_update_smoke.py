"""Validate the next signed update locally; never publish or install the fixture.

Uses the real release ZIP, Sparkle tools and production update controller. The
temporary next-version archive/feed are deleted and no user's app is replaced.
"""
import functools
import hashlib
import http.server
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'macos/.build/artifacts/sparkle/Sparkle/bin'


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    archive = Path(sys.argv[1]).resolve()
    key = Path(os.environ.get('SPARKLE_PRIVATE_KEY_FILE', Path.home() / 'Library/Application Support/cosMOSS Development/sparkle-private-key'))
    assert key.is_file(), 'Restore the existing update key first'
    with tempfile.TemporaryDirectory(prefix='cosmoss-next-update-') as temporary:
        root = Path(temporary)
        package = root / 'package'
        package.mkdir()
        subprocess.run(['ditto', '-x', '-k', str(archive), str(package)], check=True)
        app = package / 'cosMOSS.app'
        info_path = app / 'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        current = info['CFBundleShortVersionString']
        assert info['SUPublicEDKey'] == (ROOT / 'macos/UpdatePublicKey.txt').read_text().strip()
        major, minor, patch = map(int, current.split('.'))
        following = f'{major}.{minor}.{patch + 1}'
        info['CFBundleShortVersionString'] = following
        info['CFBundleVersion'] = str(major * 1_000_000 + minor * 1_000 + patch + 1)
        info_path.write_bytes(plistlib.dumps(info))
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True, capture_output=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        future = root / f'cosMOSS-v{following}-macos-arm64.zip'
        subprocess.run(['ditto', '-c', '-k', '--keepParent', '--norsrc', str(app), str(future)], check=True)
        shutil.rmtree(package)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/'
            feed = root / 'appcast.xml'
            subprocess.run([str(TOOLS / 'generate_appcast'), '--ed-key-file', str(key),
                '--download-url-prefix', base, '--maximum-deltas', '0', '-o', str(feed), str(root)], check=True)
            downloaded = root / 'downloaded' / future.name
            downloaded.parent.mkdir()
            urllib.request.urlretrieve(base + future.name, downloaded)
            assert hashlib.sha256(downloaded.read_bytes()).digest() == hashlib.sha256(future.read_bytes()).digest()
            subprocess.run(['swift', str(ROOT / 'scripts/verify-update.swift'), str(ROOT / 'macos/UpdatePublicKey.txt'), str(feed), str(downloaded)], check=True)
            env = dict(os.environ, COSMOSS_TEST_FEED=str(feed), COSMOSS_TEST_PREVIOUS_VERSION=current)
            subprocess.run([sys.executable, str(ROOT / 'tests/update_preferences_smoke.py')], env=env, check=True)
            print(f'PASS: {current} discovers signed {following}; downloaded archive and feed verify; tampering rejected; nothing published or installed')
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
