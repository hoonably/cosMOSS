#!/bin/bash
# Private, versioned desktop backend; no system Python, Homebrew, sudo or shell edits.
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
# Ignore caller uv configuration; retain normal OS proxy/certificate settings.
for name in ${!UV_@}; do unset "$name"; done
resources="$1"
base="$2"
mode="${3:---check}"
case "$mode" in --check|--install|--install-locked) ;; *) exit 2 ;; esac
requirements="$resources/requirements.txt"
# Include the installer itself so interpreter/tool changes cannot reuse an old slot.
key=$(/bin/cat "$requirements" "$0" | /usr/bin/shasum -a 256 | /usr/bin/cut -c1-24)
slot="$base/desktop-runtimes/$key"
ready() {
    [[ -f "$slot/ready" && -x "$slot/tools/bin/uv" && -x "$slot/venv/bin/python" ]] || return 1
    [[ "$("$slot/tools/bin/uv" --version)" == "uv 0.9.25 ("* ]] || return 1
    env -u PYTHONHOME -u PYTHONPATH "$slot/venv/bin/python" -I -B -c \
        'from fastapi import FastAPI; from uvicorn import Server; from httpx import Client; from multipart import MultipartParser; from markdown_it import MarkdownIt; import sys; assert sys.version_info[:3] == (3, 12, 12)' >&2
}
if ready; then printf '%s\n' "$slot"; exit 0; fi
[[ "$mode" != --check ]] || exit 1
mkdir -p "$base/desktop-runtimes"
# An OS advisory lock is released even after a crash. Keep its inode in place.
if [[ "$mode" == --install ]]; then
    exec /usr/bin/lockf -t 0 "$base/desktop-runtimes/.install-lock" \
        /bin/bash "$0" "$resources" "$base" --install-locked
fi
trap '[[ -z "${temporary:-}" ]] || rm -rf "$temporary"' EXIT
# No other slot or user data is removed; interrupted downloads are retried.
mkdir -p "$slot/tools"
rm -f "$slot/ready"
temporary=$(mktemp -d "$base/desktop-runtimes/.download.XXXXXX")
case "$(uname -m)" in
arm64)
    url='https://files.pythonhosted.org/packages/7c/0b/997f279db671fe4b1cf87ad252719c1b7c47a9546efd6c2594b5648ea983/uv-0.9.25-py3-none-macosx_11_0_arm64.whl'
    digest='79af8c9b885b507a82087e45161a4bda7f2382682867dc95f7e6d22514ac844d' ;;
x86_64)
    url='https://files.pythonhosted.org/packages/6d/5c/79dc42e1abf0afc021823c688ff04e4283f9e72d20ca4af0027aa7ed29df/uv-0.9.25-py3-none-macosx_10_12_x86_64.whl'
    digest='e47a9da2ddd33b5e7efb8068a24de24e24fd0d88a99e0c4a7e2328424783eab8' ;;
*) echo '이 Mac의 프로세서에서는 실행 환경을 설치할 수 없습니다.' >&2; exit 1 ;;
esac
{
    printf '\nCOSMOSS_RUNTIME_STAGE:tools\n' >&2
    echo '실행 도구를 내려받고 있어요…' >&2
    /usr/bin/curl --fail --location --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 20 --max-time 300 \
        "$url" -o "$temporary/uv.whl" >&2
    printf '%s  %s\n' "$digest" "$temporary/uv.whl" | /usr/bin/shasum -a 256 --check >&2
    /usr/bin/ditto -x -k "$temporary/uv.whl" "$temporary/unpacked"
    mkdir -p "$slot/tools/bin" "$slot/tools/licenses"
    cp "$temporary/unpacked/uv-0.9.25.data/scripts/uv" "$slot/tools/bin/uv"
    cp -R "$temporary/unpacked/uv-0.9.25.dist-info/licenses/." "$slot/tools/licenses/"
    chmod 755 "$slot/tools/bin/uv"
}
uv="$slot/tools/bin/uv"
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV UV_OFFLINE UV_PYTHON UV_INDEX UV_INDEX_URL UV_EXTRA_INDEX_URL
export UV_PYTHON_INSTALL_DIR="$base/.cache/uv/python"
export UV_CACHE_DIR="$base/.cache/uv"
export UV_PYTHON_PREFERENCE=only-managed
export UV_NO_PROGRESS=1
export UV_LINK_MODE=copy
# Keep managed Python in a shared stable location, also reused by local model setup.
printf '\nCOSMOSS_RUNTIME_STAGE:python\n' >&2
echo 'Python 실행 환경을 준비하고 있어요…' >&2
"$uv" python install --no-config --no-bin 3.12.12 >&2
python=$("$uv" python find --no-config 3.12.12)
"$uv" venv --no-config --clear --python "$python" "$slot/venv" >&2
printf '\nCOSMOSS_RUNTIME_STAGE:packages\n' >&2
echo '앱 실행에 필요한 파일을 설치하고 있어요…' >&2
"$uv" pip sync --no-config --python "$slot/venv/bin/python" --require-hashes \
    --index-url https://pypi.org/simple --only-binary :all: "$requirements" >&2
# Probe before committing readiness. A failed or interrupted setup stays retryable.
printf '\nCOSMOSS_RUNTIME_STAGE:verifying\n' >&2
touch "$slot/ready"
if ! ready; then rm -f "$slot/ready"; exit 1; fi
printf '%s\n' "$slot"
