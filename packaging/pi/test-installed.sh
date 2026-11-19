#!/bin/bash
# Destructive package lifecycle test. The input and output are isolated to a fresh CI VM.
set -euo pipefail
if [ "$#" -ne 3 ]; then
    echo "Usage: sudo $0 RELEASE_DIR REHEARSAL_DIR NEW_REPORT_DIR" >&2
    exit 2
fi
[ "$(id -u)" = 0 ]
[ "$(uname -m)" = aarch64 ]
[ "$(dpkg --print-architecture)" = arm64 ]
[ "$(cat /proc/1/comm)" = systemd ]
release=$(realpath "$1")
second=$(realpath "$2")
report=$(realpath -m "$3")
checks=$(realpath "$(dirname "$0")/check_installed.py")
package=openradioinspector-pi-rc
unit="$package.service"
state=/var/lib/$package
dropin=/run/systemd/system/$unit.d
ns=ori-pi-rc-ci
for path in "$state" "$state.ci-retained" "/etc/$package" "$dropin" "/run/netns/$ns" "$report"; do
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
for pkg in "${rc[0]}" "${upgrade[0]}"; do [ "$(dpkg-deb -f "$pkg" Package)" = "$package" ]; done
dpkg --compare-versions "$(dpkg-deb -f "${rc[0]}" Version)" lt "$(dpkg-deb -f "${upgrade[0]}" Version)"

ip netns add "$ns"
cleanup() {
    systemctl stop "$unit" >/dev/null 2>&1 || true
    journalctl -b -u "$unit" --no-pager > "$report/service.log" || true
    if [ -d "$dropin" ]; then rm -f -- "$dropin/network.conf"; rmdir -- "$dropin"; fi
    systemctl daemon-reload
    ip netns delete "$ns"
}
trap cleanup EXIT
ip -n "$ns" link set lo up
[ "$(ip -n "$ns" -o link show | wc -l)" = 1 ]
mkdir "$dropin"
printf '[Service]\nNetworkNamespacePath=/run/netns/%s\n' "$ns" > "$dropin/network.conf"
offline() { ip netns exec "$ns" env NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost "$@"; }
verify() { offline python3 "$checks" "$1" >> "$report/lifecycle.jsonl"; }

offline dpkg -i "${rc[0]}"
systemctl is-active --quiet "$unit"
systemctl is-enabled --quiet "$unit"
[ -z "$(dpkg --verify "$package")" ]
cmp "$release/release.json" "/usr/share/doc/$package/release.json"
[ "$(stat -c '%U %G %a' "$state")" = 'ori-pi-rc ori-pi-rc 700' ]
[ "$(stat -c '%U %a' "$state/sessions.sqlite3")" = 'ori-pi-rc 600' ]
[ "$(systemctl show -p User --value "$unit")" = ori-pi-rc ]
status=$(runuser -u ori-pi-rc -- "/usr/lib/$package/ori-supervisor" status "$state")
python3 - "$status" <<'PY'
import grp, json, os, pwd, sys
from pathlib import Path
status = json.loads(sys.argv[1])['status']
assert status['ready'] and status['writer_owned'] and status['preflight_complete']
proc = Path('/proc') / str(status['pid'])
assert proc.stat().st_uid == pwd.getpwnam('ori-pi-rc').pw_uid != os.getuid()
groups = next(row for row in (proc / 'status').read_text().splitlines() if row.startswith('Groups:')).split()[1:]
assert str(grp.getgrnam('dialout').gr_gid) in groups
assert (proc / 'ns/net').stat().st_ino == Path('/run/netns/ori-pi-rc-ci').stat().st_ino
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
verify upgrade
offline dpkg --force-confold -i "${rc[0]}"
cmp "$release/release.json" "/usr/share/doc/$package/release.json"
sha256sum -c "$report/config.sha256"
verify rollback
systemctl restart "$unit"
verify restart
systemctl stop "$unit"
runuser -u ori-pi-rc -- "/usr/lib/$package/ori-core" archive-materialize "$state/sessions.sqlite3" "$state/compat-v6.sqlite"
offline dpkg --purge "$package"
python3 - "$state" <<'PY'
from pathlib import Path
import sqlite3, sys
for filename, version in [('sessions.sqlite3', 7), ('compat-v6.sqlite', 6)]:
    with sqlite3.connect((Path(sys.argv[1]) / filename).as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA user_version').fetchone() == (version,)
        assert db.execute('SELECT COUNT(*) FROM sweeps').fetchone() == (1,)
        assert db.execute('SELECT COUNT(*) FROM observations').fetchone() == (1,)
PY
# Keep the entire original archive (including any sidecars) while exercising
# a separate v6 data directory. The package must preserve an existing schema.
mv -- "$state" "$state.ci-retained"
install -d -o ori-pi-rc -g ori-pi-rc -m 0700 "$state"
runuser -u ori-pi-rc -- cp "$state.ci-retained/compat-v6.sqlite" "$state/sessions.sqlite3"
offline dpkg -i "${rc[0]}"
verify existing-v6
offline dpkg --purge "$package"
python3 - "$state/sessions.sqlite3" <<'PYV6'
from pathlib import Path
import sqlite3, sys
with sqlite3.connect(Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as db:
    assert db.execute('PRAGMA user_version').fetchone() == (6,)
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
PYV6
echo 'Verified ARM64 install, restart, upgrade, downgrade, retained v7 data and compatible v6 restart offline.'
