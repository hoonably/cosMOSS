#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
swift "$PROJECT_DIR/scripts/setup-update-key.swift" "$PROJECT_DIR"
.venv/bin/python -m unittest discover -s tests
node --check static/app.js
./scripts/build-mac-release.sh
./scripts/generate-appcast.sh
