#!/bin/bash
# Sourced by the signed desktop installer; stdout is reserved for its final path.
# Every URL serves identical, SHA-256-pinned bytes. No DNS/IP overrides are used.
verified_file() {
    [[ -f "$1" && "$(/usr/bin/stat -f%z "$1")" == "$3" ]] || return 1
    [[ "$(/usr/bin/shasum -a 256 "$1" | /usr/bin/cut -d' ' -f1)" == "$2" ]]
}
fetch_verified() {
    local destination="$1" digest="$2" total="$3"; shift 3
    local part="$destination.part" url status attempt=0 received previous=-1 idle=0
    if verified_file "$destination" "$digest" "$total"; then
        printf '\nCOSMOSS_RUNTIME_PROGRESS:%s:%s:reused\n' "$total" "$total" >&2
        return 0
    fi
    # A completed partial from a terminated app can be promoted without a request.
    if verified_file "$part" "$digest" "$total"; then
        mv "$part" "$destination"; return 0
    fi
    rm -f "$destination"
    for url in "$@"; do
        attempt=$((attempt + 1))
        received=0
        [[ ! -f "$part" ]] || received=$(/usr/bin/stat -f%z "$part")
        [[ "$received" -lt "$total" ]] || rm -f "$part"
        if [[ "$attempt" -gt 1 ]]; then
            printf '\nCOSMOSS_RUNTIME_PROGRESS:%s:%s:retry\n' "$received" "$total" >&2
        fi
        # DNS failures, refused connections and a stalled route have bounded waits.
        # Slow but progressing transfers remain visible and can continue for 10 min.
        /usr/bin/curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
            --connect-timeout 10 --max-time 600 --speed-time 25 --speed-limit 1024 \
            --continue-at - "$url" -o "$part" >&2 &
        download_pid=$!
        previous=-1; idle=0
        while kill -0 "$download_pid" 2>/dev/null; do
            received=0
            [[ ! -f "$part" ]] || received=$(/usr/bin/stat -f%z "$part" 2>/dev/null || echo 0)
            if [[ "$received" == "$previous" ]]; then idle=$((idle + 1)); else idle=0; fi
            previous="$received"
            local state=downloading
            [[ "$idle" -lt 15 ]] || state=waiting
            printf '\nCOSMOSS_RUNTIME_PROGRESS:%s:%s:%s\n' "$received" "$total" "$state" >&2
            sleep 1
        done
        status=0; wait "$download_pid" || status=$?; download_pid=''
        if [[ "$status" == 0 ]] && verified_file "$part" "$digest" "$total"; then
            mv "$part" "$destination"
            printf '\nCOSMOSS_RUNTIME_PROGRESS:%s:%s:complete\n' "$total" "$total" >&2
            return 0
        fi
        if [[ "$status" == 0 ]]; then
            # Corrupt complete bytes must never be resumed, extracted or executed.
            rm -f "$part"
            printf '\nCOSMOSS_RUNTIME_ERROR:integrity\n' >&2
        elif [[ "$status" == 33 || "$status" == 36 ]]; then
            # The next source may not support Range; retry it from zero.
            rm -f "$part"
        fi
    done
    printf '\nCOSMOSS_RUNTIME_ERROR:download\n' >&2
    return 1
}
