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
APP_DIR="${COSMOSS_APP_DIR:-$PROJECT_DIR/dist/cosMOSS.app}"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$BIN_DIR/CosMOSS" "$APP_DIR/Contents/MacOS/CosMOSS"
mkdir -p "$APP_DIR/Contents/Resources"
cp "$PROJECT_DIR/macos/Assets/AppIcon.icns" "$APP_DIR/Contents/Resources/AppIcon.icns"
mkdir -p "$APP_DIR/Contents/Frameworks"
SPARKLE_FRAMEWORK="$PROJECT_DIR/macos/.build/artifacts/sparkle/Sparkle/Sparkle.xcframework/macos-arm64_x86_64/Sparkle.framework"
ditto "$SPARKLE_FRAMEWORK" "$APP_DIR/Contents/Frameworks/Sparkle.framework"
if ! otool -l "$APP_DIR/Contents/MacOS/CosMOSS" | grep -q '@executable_path/../Frameworks'; then
  install_name_tool -add_rpath '@executable_path/../Frameworks' "$APP_DIR/Contents/MacOS/CosMOSS"
fi
"$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" "$APP_DIR" <<'PY'
import plistlib
import sys
import tomllib
from pathlib import Path

root, app = map(Path, sys.argv[1:])
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
major, minor, patch = map(int, version.split('.'))
metadata = {
    'CFBundleName': 'cosMOSS',
    'CFBundleDisplayName': 'cosMOSS',
    'CFBundleIdentifier': 'io.github.hoonably.cosmoss',
    'CFBundleExecutable': 'CosMOSS',
    'CFBundleIconFile': 'AppIcon.icns',
    'CFBundlePackageType': 'APPL',
    'CFBundleShortVersionString': version,
    'CFBundleVersion': str(major * 1_000_000 + minor * 1_000 + patch),
    'LSMinimumSystemVersion': '13.0',
    'NSHighResolutionCapable': True,
    'NSMicrophoneUsageDescription': '화자 목소리 프로필을 등록하기 위해 약 5초 동안 목소리를 녹음합니다.',
    'UTExportedTypeDeclarations': [{'UTTypeIdentifier': 'io.github.hoonably.cosmoss.voiceprofile',
        'UTTypeDescription': 'cosMOSS Voice Profile', 'UTTypeConformsTo': ['public.json'],
        'UTTypeTagSpecification': {'public.filename-extension': ['mossvoice'],
                                    'public.mime-type': 'application/vnd.cosmoss.voice+json'}}],
    'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    'CosMOSSProjectRoot': str(root),
    'SUFeedURL': 'https://raw.githubusercontent.com/hoonably/cosMOSS/main/appcast.xml',
    'SUPublicEDKey': (root / 'macos/UpdatePublicKey.txt').read_text().strip(),
    'SUEnableAutomaticChecks': True,
    'SURequireSignedFeed': True,
    'SUVerifyUpdateBeforeExtraction': True,
    'SUSignedFeedFailureExpirationInterval': 0,
}
with (app / 'Contents/Info.plist').open('wb') as file:
    plistlib.dump(metadata, file)
PY
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/sign-mac-app.py" "$APP_DIR"
echo "개발용 앱: $APP_DIR"
echo "실행: open \"$APP_DIR\""
