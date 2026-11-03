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
BUILD_DIR="${COSMOSS_RELEASE_BUILD_DIR:-$PROJECT_DIR/dist/release}"
APP_DIR="$BUILD_DIR/cosMOSS.app"
RESOURCES="$APP_DIR/Contents/Resources"
mkdir -p "$BUILD_DIR"
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
import os
swift_build = Path(os.environ.get('COSMOSS_SWIFT_SCRATCH_PATH', str(root / 'macos/.build')))
shutil.copy2(swift_build / 'checkouts/Sparkle/LICENSE', resources / 'Sparkle-LICENSE')
for helper in ('install-optional-tools.sh', 'desktop-download.sh'):
    shutil.copy2(root / 'scripts' / helper, resources / helper)
info = app / 'Contents/Info.plist'
metadata = plistlib.loads(info.read_bytes())
metadata.pop('CosMOSSProjectRoot', None)
metadata['LSArchitecturePriority'] = ['arm64']
info.write_bytes(plistlib.dumps(metadata))
PY
"$UV_BOOTSTRAP" export --frozen --no-dev --no-emit-project --no-header \
  --output-file "$RESOURCES/requirements.txt" > /dev/null
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/bundle-desktop-runtime.py" "$APP_DIR" --uv "$UV_BOOTSTRAP"

# Sign embedded code first, then seal the bundle. Notarization is separate.
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/sign-mac-app.py" "$APP_DIR" \
  --identity "$RELEASE_IDENTITY" --embedded
echo "배포용 앱: $APP_DIR"
# Local installation needs the standalone app, without producing release archives.
if [[ "${COSMOSS_PACKAGE_ONLY:-0}" != 1 ]]; then
  "$PROJECT_DIR/scripts/build-mac-dmg.sh" "$APP_DIR"
fi
