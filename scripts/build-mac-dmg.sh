#!/usr/bin/env bash
set -euo pipefail

# Package an already-built standalone app; development builds never call this.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$(uname -s)" != Darwin ]]; then
  echo "DMG 생성은 Mac에서 실행하세요." >&2
  exit 1
fi
UV_BOOTSTRAP="${UV_BIN:-uv}"
exec "$UV_BOOTSTRAP" run --no-project --python "$PROJECT_DIR/.venv/bin/python" \
  --with 'dmgbuild==1.6.7' --with 'ds-store==1.3.3' --with 'mac-alias==2.2.3' \
  python "$PROJECT_DIR/scripts/build-dmg.py" "${1:-$PROJECT_DIR/dist/cosMOSS.app}"
