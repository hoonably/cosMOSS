#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_FILE="${SPARKLE_PRIVATE_KEY_FILE:-$HOME/Library/Application Support/cosMOSS Development/sparkle-private-key}"
FEED="${APPCAST_OUTPUT:-$PROJECT_DIR/appcast.xml}"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
if [[ ! -f "$KEY_FILE" ]]; then
  echo "cosMOSS 업데이트 서명 키를 복원한 뒤 다시 실행하세요." >&2
  exit 1
fi
swift "$PROJECT_DIR/scripts/setup-update-key.swift" "$PROJECT_DIR"
VERSION="$("$PROJECT_DIR/.venv/bin/python" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$PROJECT_DIR/pyproject.toml")"
ARCHIVE="${1:-$PROJECT_DIR/dist/cosMOSS-v$VERSION-macos-arm64.zip}"
TOOLS="$PROJECT_DIR/macos/.build/artifacts/sparkle/Sparkle/bin"
cp "$ARCHIVE" "$STAGING/$(basename "$ARCHIVE")"
if [[ -f "$FEED" ]]; then cp "$FEED" "$STAGING/appcast.xml"; fi
"$TOOLS/generate_appcast" --ed-key-file "$KEY_FILE" \
  --download-url-prefix "${UPDATE_DOWNLOAD_PREFIX:-https://github.com/hoonably/cosMOSS/releases/download/v$VERSION/}" \
  --full-release-notes-url "https://github.com/hoonably/cosMOSS/releases/tag/v$VERSION" \
  --link 'https://github.com/hoonably/cosMOSS/releases' \
  --maximum-versions 5 --maximum-deltas 0 -o "$STAGING/appcast.xml" "$STAGING"
"$TOOLS/sign_update" --verify --ed-key-file "$KEY_FILE" "$STAGING/appcast.xml"
"$PROJECT_DIR/.venv/bin/python" - "$STAGING/appcast.xml" "$ARCHIVE" "$PROJECT_DIR" <<'PY'
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

feed, archive, root = map(Path, sys.argv[1:])
with tempfile.TemporaryDirectory() as folder:
    subprocess.run(['ditto', '-x', '-k', str(archive), folder], check=True)
    info = plistlib.loads((Path(folder) / 'cosMOSS.app/Contents/Info.plist').read_bytes())
ns = '{http://www.andymatuschak.org/xml-namespaces/sparkle}'
items = ET.parse(feed).getroot().findall('channel/item')
item = next(i for i in items if i.findtext(ns + 'shortVersionString') == info['CFBundleShortVersionString'])
assert item.findtext(ns + 'version') == info['CFBundleVersion']
enclosure = item.find('enclosure')
assert int(enclosure.attrib['length']) == archive.stat().st_size
assert enclosure.attrib[ns + 'edSignature']
assert info['SUPublicEDKey'] == (root / 'macos/UpdatePublicKey.txt').read_text().strip()
assert info['SURequireSignedFeed'] and info['SUVerifyUpdateBeforeExtraction']
print('Verified signed update feed and archive metadata')
PY
swift "$PROJECT_DIR/scripts/verify-update.swift" "$PROJECT_DIR/macos/UpdatePublicKey.txt" "$STAGING/appcast.xml" "$ARCHIVE"
cp "$STAGING/appcast.xml" "$FEED"
echo "업데이트 피드: $FEED"
