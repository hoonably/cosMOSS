#!/usr/bin/env bash
set -euo pipefail

# The application itself does not depend on MLX, CUDA, or a model download.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
UV_BIN="${UV_BIN:-uv}"
if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "uv를 설치한 뒤 다시 실행하세요." >&2
  exit 1
fi
cd "$PROJECT_DIR"
repair_local_runtime "$PROJECT_DIR/.venv"
UV_PROJECT_ENVIRONMENT="$PROJECT_DIR/.venv" "$UV_BIN" sync --locked --python 3.12
echo "앱 설치 완료. ./scripts/run-app.sh 또는 ./scripts/build-mac-app.sh 로 실행하세요."
