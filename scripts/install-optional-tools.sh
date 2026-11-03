#!/bin/bash
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
resources="$1"; tools="$2"; base="$3"
source "$resources/desktop-download.sh"
digest=79af8c9b885b507a82087e45161a4bda7f2382682867dc95f7e6d22514ac844d
binary_digest=8ca599bb63fb3f468eb04ee9f91bda946a886848ef1f36acb7aaf4f71af4117d
valid() { [[ -x "$1" ]] && [[ "$(shasum -a 256 "$1" | cut -d' ' -f1)" == "$binary_digest" ]]; }
mkdir -p "$tools/licenses" "$base/.cache/desktop-downloads"
if [[ "${4:-}" != --locked ]]; then
    exec /usr/bin/lockf -t 0 "$tools/.install-lock" /bin/bash "$0" "$resources" "$tools" "$base" --locked
fi
if valid "$tools/uv"; then printf '%s\n' "$tools/uv"; exit 0; fi
# Reuse the already-verified tool left by a failed old first-run installation.
for candidate in "$base"/desktop-runtimes/*/tools/bin/uv; do
    if valid "$candidate"; then
        cp "$candidate" "$tools/uv"
        old_tools="${candidate%/bin/uv}"
        [[ ! -d "$old_tools/licenses" ]] || cp -R "$old_tools/licenses/." "$tools/licenses/"
        printf '%s\n' "$tools/uv"; exit 0
    fi
done
temporary=$(mktemp -d "$tools/.prepare.XXXXXX"); download_pid=''; watcher_pid=''
install_pid=$$
if [[ "${COSMOSS_TOOL_PARENT_PID:-}" =~ ^[0-9]+$ && "$COSMOSS_TOOL_PARENT_PID" -gt 1 ]]; then
    (while kill -0 "$COSMOSS_TOOL_PARENT_PID" 2>/dev/null; do sleep 1; done; kill -TERM "$install_pid" 2>/dev/null) &
    watcher_pid=$!
fi
cleanup() { [[ -z "$download_pid" ]] || kill "$download_pid" 2>/dev/null || true; [[ -z "$watcher_pid" ]] || kill "$watcher_pid" 2>/dev/null || true; rm -rf "$temporary"; }
trap cleanup EXIT
trap 'exit 130' TERM INT
archive="$base/.cache/desktop-downloads/$digest.whl"
fetch_verified "$archive" "$digest" 19096089 \
    "https://cosmoss-app.pages.dev/runtime/$digest.whl" \
    'https://files.pythonhosted.org/packages/7c/0b/997f279db671fe4b1cf87ad252719c1b7c47a9546efd6c2594b5648ea983/uv-0.9.25-py3-none-macosx_11_0_arm64.whl' \
    "https://cosmoss-app.pages.dev/runtime/$digest.whl"
ditto -x -k "$archive" "$temporary/unpacked"
cp "$temporary/unpacked/uv-0.9.25.data/scripts/uv" "$tools/uv"
cp -R "$temporary/unpacked/uv-0.9.25.dist-info/licenses/." "$tools/licenses/"
chmod 755 "$tools/uv"
valid "$tools/uv"
printf '%s\n' "$tools/uv"
