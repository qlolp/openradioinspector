#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then echo "Usage: sudo sh $0 /absolute/path/openradioinspector.whl" >&2; exit 2; fi
wheel=$(realpath "$1")
case "$wheel" in /*.whl) ;; *) echo 'A local wheel is required' >&2; exit 2 ;; esac
[ -f "$wheel" ] || { echo 'Wheel is missing' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo 'Installer needs root; the service itself is unprivileged' >&2; exit 2; }
getent group dialout >/dev/null || { echo 'Configure the serial-access group first' >&2; exit 2; }
id openradioinspector >/dev/null 2>&1 || useradd --system --home-dir /var/lib/openradioinspector --shell /usr/sbin/nologin openradioinspector
usermod -a -G dialout openradioinspector
install -d -o openradioinspector -g openradioinspector -m 700 /opt/openradioinspector /var/lib/openradioinspector
# Stop the owned service before changing its environment. Data files are retained.
if [ "$(systemctl show -p LoadState --value openradioinspector.service)" != not-found ]; then
    systemctl stop openradioinspector.service
fi
runuser -u openradioinspector -- python3 -m venv /opt/openradioinspector/venv
# Copy into the private application directory so the service user can read it.
install -o openradioinspector -g openradioinspector -m 600 "$wheel" /opt/openradioinspector/$(basename "$wheel")
runuser -u openradioinspector -- /opt/openradioinspector/venv/bin/python -m pip install "/opt/openradioinspector/$(basename "$wheel")[serial]"
install -m 644 "$(dirname "$0")/openradioinspector.service" /etc/systemd/system/openradioinspector.service
if [ ! -e /etc/default/openradioinspector ]; then
    printf '%s\n' 'ORI_BIND=127.0.0.1' 'ORI_PORT=8770' 'ORI_ARCHIVE_MAX_BYTES=1073741824' > /etc/default/openradioinspector
    chmod 600 /etc/default/openradioinspector
fi
systemctl daemon-reload
systemctl enable --now openradioinspector.service
systemctl is-active --quiet openradioinspector.service
printf '%s\n' 'Service started. Verify /api/v1/health; remote binding requires a trusted network.'
