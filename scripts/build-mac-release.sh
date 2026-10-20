#!/usr/bin/env bash
set -euo pipefail

# A relocatable Apple Silicon bundle. No checkout, recordings, credentials,
# model weights, or local virtual environments are copied into the release.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
  echo "배포용 빌드는 Apple Silicon Mac이 필요합니다." >&2
  exit 1
fi
UV_BOOTSTRAP="${UV_BIN:-uv}"
BUILD_DIR="$PROJECT_DIR/dist/release"
APP_DIR="$BUILD_DIR/cosMOSS.app"
RESOURCES="$APP_DIR/Contents/Resources"
mkdir -p "$BUILD_DIR"
"$UV_BOOTSTRAP" pip install --no-config --target "$PROJECT_DIR/dist/.release-tools" 'uv==0.9.25'
UV_RELEASE="$PROJECT_DIR/dist/.release-tools/bin/uv"
"$UV_RELEASE" python install --no-config --install-dir "$PROJECT_DIR/dist/.release-python" 3.12.12
PYTHON_NOTICES="$PROJECT_DIR/dist/.release-python/python-full-20260113.tar.zst"
if [[ ! -f "$PYTHON_NOTICES" ]]; then
  curl --fail --location --silent --show-error \
    'https://github.com/astral-sh/python-build-standalone/releases/download/20260113/cpython-3.12.12%2B20260113-aarch64-apple-darwin-pgo%2Blto-full.tar.zst' \
    --output "$PYTHON_NOTICES"
fi
printf '%s  %s\n' 'bd90cbe76b59aeba9e96396fb15ecaae3bc19bd5e1e486dc8a937c1eba627e52' "$PYTHON_NOTICES" | shasum -a 256 --check
# Local development identities are not distribution certificates. Public builds
# retain the existing ad-hoc policy unless an explicit release identity is set.
RELEASE_IDENTITY="${COSMOSS_RELEASE_CODESIGN_IDENTITY:--}"
COSMOSS_APP_DIR="$APP_DIR" COSMOSS_CODESIGN_IDENTITY="$RELEASE_IDENTITY" "$PROJECT_DIR/scripts/build-mac-app.sh"
"$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" "$APP_DIR" <<'PY'
import plistlib
import shutil
import sys
from pathlib import Path

root, app = map(Path, sys.argv[1:])
resources = app / 'Contents/Resources'
if resources.exists():
    shutil.rmtree(resources)
resources.mkdir()
shutil.copy2(root / 'macos/Assets/AppIcon.icns', resources / 'AppIcon.icns')
for name in ('src', 'static'):
    shutil.copytree(root / name, resources / 'app' / name,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
    shutil.copy2(root / name, resources / name)
shutil.copy2(root / 'macos/.build/checkouts/Sparkle/LICENSE', resources / 'Sparkle-LICENSE')
python = root / 'dist/.release-python/cpython-3.12.12-macos-aarch64-none'
assert (python / 'BUILD').read_text().strip() == '20260113', 'Review licenses for the new Python build before packaging'
shutil.copytree(python, resources / 'python', symlinks=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
shutil.copytree(root / 'dist/.release-tools', resources / 'tools', symlinks=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
info = app / 'Contents/Info.plist'
metadata = plistlib.loads(info.read_bytes())
metadata.pop('CosMOSSProjectRoot', None)
metadata['LSArchitecturePriority'] = ['arm64']
info.write_bytes(plistlib.dumps(metadata))
PY
tar -xf "$PYTHON_NOTICES" -C "$RESOURCES/python" --strip-components 1 python/licenses python/PYTHON.json
"$UV_RELEASE" export --frozen --no-dev --no-emit-project --no-header \
  --output-file "$RESOURCES/requirements.txt" > /dev/null
"$UV_RELEASE" pip install --no-config --python "$RESOURCES/python/bin/python3.12" \
  --require-hashes --target "$RESOURCES/python/lib/python3.12/site-packages" \
  --requirements "$RESOURCES/requirements.txt"

# Sign embedded code first, then seal the bundle. Notarization is separate.
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/sign-mac-app.py" "$APP_DIR" \
  --identity "$RELEASE_IDENTITY" --embedded
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_DIR/Contents/Info.plist")"
ZIP="$PROJECT_DIR/dist/cosMOSS-v$VERSION-macos-arm64.zip"
COPYFILE_DISABLE=1 /usr/bin/ditto -c -k --keepParent --norsrc "$APP_DIR" "$ZIP"
echo "배포용 앱: $APP_DIR"
echo "릴리스 ZIP: $ZIP"
"$PROJECT_DIR/scripts/build-mac-dmg.sh" "$APP_DIR"
