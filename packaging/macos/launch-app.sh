#!/bin/sh
set -eu
contents=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${ORI_REMOTE_SERVER:-}" ]; then
    unset ORI_BUNDLED_LEGACY_CORE
    ORI_LOCAL_CORE_URL="$ORI_REMOTE_SERVER" exec "$contents/Helpers/DvoynoyObzorUI"
fi
runtime=$(mktemp -d "${TMPDIR:-/tmp}/openradioinspector-launch.XXXXXX")
mkfifo -m 600 "$runtime/lifeline"
exec 3<>"$runtime/lifeline"
cleaned=0
cleanup() {
    [ "$cleaned" -eq 0 ] || return
    cleaned=1
    exec 3>&-
    if [ -n "${core_pid:-}" ]; then wait "$core_pid" || true; fi
    rm -rf -- "$runtime"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
"$contents/Helpers/openradioinspector-core/openradioinspector-core" --port 0 --ready-file "$runtime/url" --stdin-lifeline <"$runtime/lifeline" 3>&- &
core_pid=$!
count=0
until [ -s "$runtime/url" ]; do
    if ! kill -0 "$core_pid" 2>/dev/null; then echo 'Local core startup failed' >&2; exit 1; fi
    count=$((count+1))
    if [ "$count" -ge 300 ]; then echo 'Local core readiness timed out' >&2; exit 1; fi
    sleep 0.1
done
url=$(cat "$runtime/url")
case "$url" in http://127.0.0.1:*) ;; *) echo 'Invalid local readiness URL' >&2; exit 1 ;; esac
ORI_BUNDLED_LEGACY_CORE=1 ORI_LOCAL_CORE_URL="$url" "$contents/Helpers/DvoynoyObzorUI"
