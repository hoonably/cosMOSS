#!/usr/bin/env bash
set -euo pipefail

# Build a development .app that uses this checkout's Python runtime and sources.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
  echo "Mac 앱 빌드는 macOS와 Xcode Command Line Tools가 필요합니다." >&2
  exit 1
fi
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "먼저 ./scripts/setup-app.sh 를 실행하세요." >&2
  exit 1
fi
swift build --package-path "$PROJECT_DIR/macos" -c release
BIN_DIR="$(swift build --package-path "$PROJECT_DIR/macos" -c release --show-bin-path)"
APP_DIR="$PROJECT_DIR/dist/cosMOSS.app"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$BIN_DIR/CosMOSS" "$APP_DIR/Contents/MacOS/CosMOSS"
"$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" "$APP_DIR" <<'PY'
import plistlib
import sys
from pathlib import Path

root, app = map(Path, sys.argv[1:])
metadata = {
    'CFBundleName': 'cosMOSS',
    'CFBundleDisplayName': 'cosMOSS',
    'CFBundleIdentifier': 'io.github.hoonably.cosmoss',
    'CFBundleExecutable': 'CosMOSS',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': '0.1.0',
    'CFBundleVersion': '1',
    'LSMinimumSystemVersion': '13.0',
    'NSHighResolutionCapable': True,
    'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    'CosMOSSProjectRoot': str(root),
}
with (app / 'Contents/Info.plist').open('wb') as file:
    plistlib.dump(metadata, file)
PY
codesign --force --deep --sign - "$APP_DIR"
echo "개발용 앱: $APP_DIR"
echo "실행: open \"$APP_DIR\""
