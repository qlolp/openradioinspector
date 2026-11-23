# Raspberry Pi 0.4.0 installation and verification

This package targets a Raspberry Pi 5 with 8 GB RAM, an SSD and 64-bit Raspberry Pi OS based on Debian 12 or later. It installs the ARM64 serial-enabled Rust core, its supervisor and the shared React WebUI. The browser loads the application and its assets directly from the core. Installation and operation require only the system packages in the Debian control file. The runtime contains no Node, Python interpreter, npm or Cargo dependencies to install.

Офлайн-справочник оператора установлен в `/usr/share/doc/openradioinspector-pi-rc/operator-guide/index.html`. Он описывает подготовку приёмников, проверку верхней полосы и восстановление архива. [Руководство на русском](operator-guide/index.html)

## Install and open the station

Boot Raspberry Pi OS from the SSD. If the OS lives on another disk, bind-mount a dedicated directory from the SSD at `/var/lib/openradioinspector-pi-rc` and persist that mount in `/etc/fstab` before installing. Systemd assigns this directory and its contents to the service account. Use a local filesystem that supports SQLite WAL and POSIX ownership. Confirm `dpkg --print-architecture` reports `arm64`. Copy the complete candidate directory to the Pi. Compare its digest or the release manifest with the copy obtained through your trusted release channel. For a published 0.4.0 asset, download the complete release asset set from the same tag, including `pi-manifest.json`, `release.json` and `SHA256SUMS`. Compare the source revision in the manifests with the release tag and verify the downloaded files before installation. From that directory run

```sh
sha256sum --check SHA256SUMS
sudo dpkg -i ./OpenRadioInspector-0.4.0-pi-arm64.deb
systemctl is-enabled openradioinspector-pi-rc
systemctl is-active openradioinspector-pi-rc
sudo -u ori-pi-rc /usr/lib/openradioinspector-pi-rc/ori-supervisor status /var/lib/openradioinspector-pi-rc
```

For a private engineering RC directory, verify its bundled `SHA256SUMS` and use `sudo dpkg -i ./openradioinspector-pi-rc_0.4.0~rc.1-1_arm64.deb`. The installable 0.4.0 CI DEB has the Debian filename `openradioinspector-pi-rc_0.4.0-1_arm64.deb`. The published download carries the platform filename above. All use the same Debian package and systemd unit.

Open **http://127.0.0.1:8770/modern/** in the Pi browser and confirm the loopback connection. `/api/v1/health` reports the running core. Closing every browser window leaves the service running. The service starts at boot and opens the instruments only when a session explicitly requests them. The previous wheel installation uses another account, unit and archive. If it already owns port 8770, stop that service deliberately before installing the RC or assign it another port.

The service user `ori-pi-rc` has a private data directory and receives `dialout` as a supplementary group from systemd. Program files and the service configuration are owned by root. Connect the tinySA Ultra and AR-DV1 to distinct serial ports. Check `ls -l /dev/serial/by-id/` and `stat -Lc '%A %U %G %n' /dev/serial/by-id/*`. Use persistent device identities and the firmware profiles supported by the core. Raspberry Pi OS normally gives these serial devices to `dialout`. Device connection and acquisition require separate operator actions. The service does not grant serial access to unrelated desktop users.

| Location | Contents |
|---|---|
| `/etc/openradioinspector-pi-rc/supervisor.json` | Host, ports, task-book path, 64 GiB archive limit and startup/shutdown deadlines |
| `/var/lib/openradioinspector-pi-rc` | Measurement archive, tasks, runtime leases and locally provisioned security state |
| `/usr/share/doc/openradioinspector-pi-rc/release.json` | Source commit, binary and WebUI SHA-256 digests, SQLite version and required system libraries |
| `/usr/share/doc/openradioinspector-pi-rc/notices` | Cargo and npm lockfiles, component license inventory, available notices and Rust copyright |
| `journalctl -u openradioinspector-pi-rc -b` | Boot, readiness and shutdown diagnostics |

The first start creates a compact schema-7 archive if the database is absent. The core retains schema 6 when opening an existing v6 archive. The package keeps measurements, keys and operator configuration across upgrades. Choose an archive limit that fits the SSD and leaves headroom for WAL, backups and other applications.

## Offline update and rollback

Keep the previous candidate directory with its manifest and checksums. Schedule an acquisition interruption. Stop the service, keep a cold backup on a separate filesystem, then verify the new package before installing it. Replace the example backup location with a new path on a mounted disk with sufficient space.

```sh
set -eu
backup=/mnt/backup/pi-rc-before-update
sudo systemctl stop openradioinspector-pi-rc
sudo mkdir -m 0700 "$backup"
sudo tar -C / -cpf "$backup/pi-state.tar" var/lib/openradioinspector-pi-rc etc/openradioinspector-pi-rc
sudo tar -tf "$backup/pi-state.tar" >/dev/null
sudo sha256sum "$backup/pi-state.tar"
sha256sum --check SHA256SUMS
sudo dpkg --force-confold -i ./OpenRadioInspector-0.4.0-pi-arm64.deb
systemctl is-active openradioinspector-pi-rc
```

Use one candidate per directory. If the new service fails, read the journal, enter the **previous** verified candidate directory and repeat the checksum, installation and service checks using the previous DEB’s filename. Debian installs the older program files over the newer files, preserves the configured data directory and starts the service. RC packages that support the same archive format see measurements committed after the update. Recheck health and session history after restarting.

For a relocated archive, keep its registered volume mounted during rollback. The outgoing package's downgrade hook returns a verified copy to the node directory before the previous core starts. To prepare explicitly, stop the service and run `sudo -u ori-pi-rc /usr/lib/openradioinspector-pi-rc/ori-supervisor archive-prepare-rollback --data-dir /var/lib/openradioinspector-pi-rc`. Include the selected storage directory in an offline backup. For a disk mounted outside the service state directory, give the service account ownership and configure a systemd drop-in with `RequiresMountsFor=` in `[Unit]` and `ReadWritePaths=` in `[Service]` for that directory. Run `systemctl daemon-reload` before restarting.

For a backend that can only read schema 6, stop the compact-capable core and run its materializer **before** switching packages. It creates a separate schema-6 database and rehydrates compact events. The destination database must be new. Retain the source and use a separate restored data directory for the older backend.

```sh
state=/var/lib/openradioinspector-pi-rc
sudo systemctl stop openradioinspector-pi-rc
sudo -u ori-pi-rc mkdir -m 0700 "$state/compat-v6"
sudo -u ori-pi-rc /usr/lib/openradioinspector-pi-rc/ori-core archive-materialize \
  "$state/sessions.sqlite3" "$state/compat-v6/sessions.sqlite3"
```

The materialized file contains the session database. Restore project files, artifacts and configuration from the stopped service backup as required by the older program. To uninstall the RC, run `sudo dpkg --remove openradioinspector-pi-rc`. Purging also removes the package conffile. Both operations retain the private account and measurements.

## Optional paired LAN browser

The default listener accepts only loopback HTTP. For a trusted local network, provision the core's local CA and choose a DNS name that resolves to this Pi. The following example uses `radio.local` and port 8770. On the Pi run

```sh
state=/var/lib/openradioinspector-pi-rc
sudo systemctl stop openradioinspector-pi-rc
sudo -u ori-pi-rc /usr/lib/openradioinspector-pi-rc/ori-core init-lan --data-dir "$state" --name radio.local
sudo -u ori-pi-rc cat "$state/.ori-runtime/security/public-ca.pem" > pi-public-ca.pem
sudoedit /etc/openradioinspector-pi-rc/supervisor.json
```

In `backend.args`, change `--host` to the Pi's LAN address and add `"--lan", "--trusted-host", "radio.local:8770", "--control-port", "8771"`. Set `backend.control_addr` to `127.0.0.1:8771` and `backend.ports` to both the Pi's exact LAN `IP:8770` and `127.0.0.1:8771`. Keep the data path, serial option and supervisor lifeline. Install the public CA on the client with its trusted certificate settings and verify the fingerprint at the Pi. Start the service, then request a one-time code from the Pi terminal

```sh
sudo systemctl start openradioinspector-pi-rc
sudo -u ori-pi-rc /usr/lib/openradioinspector-pi-rc/ori-core pair --data-dir /var/lib/openradioinspector-pi-rc
```

Open `https://radio.local:8770/modern/` from the paired browser. Enter the code on that TLS connection. The installation CA private key stays in the service's private directory.

## Rebuild and verification

From a clean checkout with Node 26.13.0, Rust 1.99.1, Python 3.11+, GNU binutils and dpkg, run the following on ARM64 Debian 12. The package builder requires an unused output directory. Build inputs may be downloaded on this machine.

```sh
npm ci --prefix clients/web --no-audit --no-fund
python3 packaging/pi/build_rc.py --distribution --output-dir build/pi/release --rehearsal-dir build/pi/rehearsal
```

The native ARM64 CI job builds the 0.4.0 Debian package and a private preceding RC from the checked-out source. It installs both on an ARM64 Linux VM running systemd, gives the service a network namespace containing only loopback, verifies each served SPA asset and tests an RC-to-0.4.0 upgrade, downgrade, restart and restoration of the original measurements into schema 6. The default builder invocation without `--distribution` continues to build the private RC.

The proof artifact `openradioinspector-pi-rc-proof-<source SHA>` contains `installed-webui/pi-monitoring.png` and `installed-webui/pi-monitoring.json`. Chromium opens the installed 0.4.0 Core’s Russian monitoring desk, waits for the committed synthetic sweep ID on the drawn spectrum and captures the viewport. The JSON records the installed Debian version, DEB and PNG SHA-256 hashes, source commit, browser, OS, display dimensions and original sweep ID. This capture identifies the GitHub-hosted ARM64 VM as its host. The downloadable DEB has a separate artifact for the same revision.

Physical Pi 5 boot and reboot, actual USB instruments, SSD write rate, continuous acquisition under load and recovery after power interruption require a run on the target station. The package manifest records those checks as pending.
