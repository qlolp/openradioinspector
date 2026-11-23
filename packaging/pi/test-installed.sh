#!/bin/bash
# Destructive package lifecycle test. The input and output are isolated to a fresh CI VM.
set -euo pipefail
if [ "$#" -ne 3 ] && [ "$#" -ne 5 ]; then
    echo "Usage: sudo $0 OLDER_DEB_DIR NEWER_DEB_DIR NEW_REPORT_DIR [NODE BROWSER_CACHE]" >&2
    exit 2
fi
[ "$(id -u)" = 0 ]
[ "$(cat /proc/1/comm)" = systemd ]
arch=$(dpkg --print-architecture)
case "$arch/$(uname -m)" in
    arm64/aarch64) package=openradioinspector-pi-rc; service_user=ori-pi-rc ;;
    amd64/x86_64)  package=openradioinspector-linux; service_user=ori-linux; [ "$#" -eq 3 ] ;;
    *) echo 'This test needs a native arm64 or amd64 Debian package host' >&2; exit 1 ;;
esac
release=$(realpath "$1")
second=$(realpath "$2")
report=$(realpath -m "$3")
checks=$(realpath "$(dirname "$0")/check_installed.py")
unit="$package.service"
state=/var/lib/$package
manifest=/usr/share/doc/$package/release.json
dropin=/run/systemd/system/$unit.d
ns=$service_user-ci
for path in "$state" "$state.ci-clean" "$state.ci-retained" "/etc/$package" "$dropin" "/run/netns/$ns" "$report"; do
    if [ -e "$path" ] || [ -L "$path" ]; then echo "CI test requires a fresh path: $path" >&2; exit 1; fi
done
if dpkg-query -s "$package" >/dev/null 2>&1; then echo 'Package already present' >&2; exit 1; fi
mkdir -m 755 "$report"
(cd "$release" && sha256sum -c SHA256SUMS)
(cd "$second" && sha256sum -c SHA256SUMS)
rc=("$release"/*.deb)
upgrade=("$second"/*.deb)
[ "${#rc[@]}" = 1 ]
[ "${#upgrade[@]}" = 1 ]
for pkg in "${rc[0]}" "${upgrade[0]}"; do
    [ "$(dpkg-deb -f "$pkg" Package)" = "$package" ]
    [ "$(dpkg-deb -f "$pkg" Architecture)" = "$arch" ]
done
dpkg --compare-versions "$(dpkg-deb -f "${rc[0]}" Version)" lt "$(dpkg-deb -f "${upgrade[0]}" Version)"

if [ "$#" -eq 5 ]; then
    node=$(realpath "$4")
    browser_cache=$(realpath "$5")
    [ -x "$node" ]
    [ -d "$browser_cache" ]
    [ -n "${SUDO_USER:-}" ]
    [ "$(id -u "$SUDO_USER")" -ne 0 ]
    capture=$(realpath "$(dirname "$checks")/../../clients/web/tests/capture-pi-installed.mjs")
    [ "$(dpkg-deb -f "${rc[0]}" Version)" = '0.4.0~rc.1-1' ]
    [ "$(dpkg-deb -f "${upgrade[0]}" Version)" = '0.4.0-1' ]
fi

if [ "$arch" = amd64 ]; then
    # CI boots the entire Debian userspace with only a private loopback device.
    . /etc/os-release
    [ "$ID" = debian ]
    case "$VERSION_ID" in 12|13) ;; *) echo 'Debian 12 or 13 is required' >&2; exit 1 ;; esac
    [ "$(ip -o link show | wc -l)" -eq 1 ]
    ip link set lo up
    network=/proc/1/ns/net
    launcher=()
else
    ip netns add "$ns"
    ip -n "$ns" link set lo up
    [ "$(ip -n "$ns" -o link show | wc -l)" -eq 1 ]
    mkdir "$dropin"
    printf '[Service]\nNetworkNamespacePath=/run/netns/%s\n' "$ns" > "$dropin/network.conf"
    network=/run/netns/$ns
    launcher=(ip netns exec "$ns")
fi
cleanup() {
    result=$?
    systemctl stop "$unit" >/dev/null 2>&1 || true
    journalctl -b -u "$unit" --no-pager > "$report/service.log" || true
    if [ "$result" -ne 0 ]; then
        dpkg-query --show ca-certificates systemd >&2 || true
        cat "$report/service.log" >&2
    fi
    if [ -d "$dropin" ]; then rm -f -- "$dropin/network.conf"; rmdir -- "$dropin"; fi
    systemctl daemon-reload
    if [ "$arch" = arm64 ]; then ip netns delete "$ns"; fi
}
trap cleanup EXIT
offline() { "${launcher[@]}" env NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost "$@"; }
verify() {
    "/usr/lib/$package/check-install" >/dev/null
    local phase_report automatic_restarts
    phase_report=$(offline python3 "$checks" "$1" --manifest "$manifest")
    # Only a healthy, crash-free scenario clears this CI unit's manual starts.
    # Preserve evidence of an automatic restart even if HTTP has recovered.
    systemctl is-active --quiet "$unit"
    automatic_restarts=$(systemctl show -p NRestarts --value "$unit")
    if [ "$automatic_restarts" != 0 ]; then
        printf 'Unexpected automatic restarts for %s: %s\n' "$unit" "$automatic_restarts" >&2
        return 1
    fi
    systemctl reset-failed "$unit"
    printf '%s\n' "$phase_report" >> "$report/lifecycle.jsonl"
}

if [ "$arch" = amd64 ]; then
    offline dpkg -i "${upgrade[0]}"
    systemctl is-enabled --quiet "$unit"
    [ -z "$(dpkg --verify "$package")" ]
    cmp "$second/release.json" "$manifest"
    verify clean-install
    python3 - "${upgrade[0]}" "$manifest" "$report/environment.json" <<'PY'
import hashlib, json, platform, subprocess, sys
from pathlib import Path
deb, manifest, output = map(Path, sys.argv[1:])
info = json.loads(manifest.read_text())
os = platform.freedesktop_os_release()
package, version, arch = subprocess.check_output([
    'dpkg-query', '-W', '-f', '${Package} ${Version} ${Architecture}', info['package']], text=True).split()
assert (package, version, arch) == (info['package'], info['debian_version'], info['debian_architecture'])
with deb.open('rb') as content:
    sha = hashlib.file_digest(content, 'sha256').hexdigest()
output.write_text(json.dumps(dict(os_id=os['ID'], os_version=os['VERSION_ID'], architecture=arch,
    package=package, source_revision=info['source_revision'], debian_version=version, deb_sha256=sha,
    hardware_validation='not_run'), indent=2) + '\n')
PY
    offline dpkg --purge "$package"
    mv -- "$state" "$state.ci-clean"
fi

offline dpkg -i "${rc[0]}"
systemctl is-active --quiet "$unit"
systemctl is-enabled --quiet "$unit"
[ -z "$(dpkg --verify "$package")" ]
cmp "$release/release.json" "/usr/share/doc/$package/release.json"
[ "$(stat -c '%U %G %a' "$state")" = "$service_user $service_user 700" ]
[ "$(stat -c '%U %a' "$state/sessions.sqlite3")" = "$service_user 600" ]
[ "$(systemctl show -p User --value "$unit")" = "$service_user" ]
status=$(runuser -u "$service_user" -- "/usr/lib/$package/ori-supervisor" status "$state")
python3 - "$status" "$service_user" "$network" <<'PY'
import grp, json, os, pwd, sys
from pathlib import Path
status = json.loads(sys.argv[1])['status']
assert status['ready'] and status['writer_owned'] and status['preflight_complete']
proc = Path('/proc') / str(status['pid'])
assert proc.stat().st_uid == pwd.getpwnam(sys.argv[2]).pw_uid != os.getuid()
groups = next(row for row in (proc / 'status').read_text().splitlines() if row.startswith('Groups:')).split()[1:]
assert str(grp.getgrnam('dialout').gr_gid) in groups
assert (proc / 'ns/net').stat().st_ino == Path(sys.argv[3]).stat().st_ino
PY
python3 - "$package" <<'CHECK'
import hashlib, json, subprocess, sys
from pathlib import Path
package = sys.argv[1]
doc = Path('/usr/share/doc') / package
manifest = json.loads((doc / 'release.json').read_text())
assert hashlib.sha256((doc / 'notices/licenses.json').read_bytes()).hexdigest() == manifest['license_manifest_sha256']
for name, expected in manifest['components'].items():
    binary = Path('/usr/lib') / package / name
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == expected['sha256']
    actual = json.loads(subprocess.check_output([binary, '--build-info']))
    assert actual['source_revision'] == manifest['source_revision']
    assert all(expected[key] == value for key, value in actual.items())
CHECK
verify install
# An operator-edited dpkg conffile must survive both upgrade and downgrade.
printf '\n' >> "/etc/$package/supervisor.json"
sha256sum "/etc/$package/supervisor.json" > "$report/config.sha256"

offline dpkg --force-confold -i "${upgrade[0]}"
cmp "$second/release.json" "/usr/share/doc/$package/release.json"
sha256sum -c "$report/config.sha256"
systemctl stop "$unit"
runuser -u "$service_user" -- "/usr/lib/$package/ori-supervisor" archive-location --data-dir "$state" --directory "$state/ci-archive-volume"
systemctl start "$unit"
verify upgrade
offline dpkg --force-confold -i "${rc[0]}"
cmp "$release/release.json" "/usr/share/doc/$package/release.json"
sha256sum -c "$report/config.sha256"
verify rollback
systemctl restart "$unit"
verify restart
offline dpkg --force-confold -i "${upgrade[0]}"
cmp "$second/release.json" "$manifest"
offline python3 "$checks" restart --manifest "$manifest" >/dev/null
systemctl stop "$unit"
runuser -u "$service_user" -- "/usr/lib/$package/ori-core" archive-materialize "$state/sessions.sqlite3" "$state/compat-v6.sqlite"
offline dpkg --purge "$package"
python3 - "$state" <<'PY'
from pathlib import Path
import sqlite3, sys
for filename, version in [('sessions.sqlite3', 7), ('compat-v6.sqlite', 6)]:
    with sqlite3.connect((Path(sys.argv[1]) / filename).as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA user_version').fetchone() == (version,)
        assert db.execute('SELECT COUNT(*) FROM sweeps').fetchone() == (1,)
        assert db.execute('SELECT COUNT(*) FROM observations').fetchone() == (2,)
PY
# Keep the entire original archive (including any sidecars) while exercising
# a separate v6 data directory. The package must preserve an existing schema.
mv -- "$state" "$state.ci-retained"
install -d -o "$service_user" -g "$service_user" -m 0700 "$state"
runuser -u "$service_user" -- cp "$state.ci-retained/compat-v6.sqlite" "$state/sessions.sqlite3"
if [ "$arch" = amd64 ]; then
    offline dpkg -i "${upgrade[0]}"
    cmp "$second/release.json" "$manifest"
else
    offline dpkg -i "${rc[0]}"
fi
verify existing-v6
if [ "$#" -eq 5 ]; then
    # The lifecycle and exact original-source checks have completed on the
    # restored fixture. Reinstall the final DEB before capturing its WebUI.
    offline dpkg --force-confold -i "${upgrade[0]}"
    cmp "$second/release.json" "/usr/share/doc/$package/release.json"
    verify existing-v6
    install -d -m 0755 -o "$SUDO_USER" "$report/installed-webui"
    offline runuser -u "$SUDO_USER" -- env PLAYWRIGHT_BROWSERS_PATH="$browser_cache" \
        "$node" "$capture" "${upgrade[0]}" "$report/installed-webui"
fi
offline dpkg --purge "$package"
python3 - "$state/sessions.sqlite3" <<'PYV6'
from pathlib import Path
import sqlite3, sys
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as db:
    assert db.execute('PRAGMA user_version').fetchone() == (6,)
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
PYV6
echo "Verified $arch install, restart, upgrade, downgrade, retained v7 data and compatible v6 restart offline."
