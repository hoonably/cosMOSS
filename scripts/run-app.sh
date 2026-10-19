#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
repair_local_runtime "$PROJECT_DIR/.venv"
cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  echo "앱 환경이 없습니다. ./scripts/setup-app.sh 를 먼저 실행하세요." >&2
  exit 1
fi

exec .venv/bin/python -m uvicorn src.main:app \
  --host "${COSMOSS_DESKTOP_HOST:-${MOSS_APP_HOST:-127.0.0.1}}" \
  --port "${COSMOSS_DESKTOP_PORT:-${MOSS_APP_PORT:-8000}}" \
  --workers 1
