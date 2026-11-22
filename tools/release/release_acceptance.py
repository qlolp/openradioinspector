"""Check the public projection of private installed, load and physical bench records."""
import math
from pathlib import Path
import re
import subprocess
import tempfile

from .release_artifacts import DEBIAN_VERSIONS, LINUX_AMD64, LINUX_CI, TARGETS, keys, require, sha


NAMESPACE = 'openradioinspector-0.4.0-acceptance'
COMMON = ('windows-core.yml', 'modern-core.yml', 'desktop-packages.yml')
INSTALL_CHECKS = ('offline', 'clean_install', 'upgrade', 'rollback', 'new_measurements_preserved', 'manual_opened_offline')


def required_workflows(platform):
    if platform == LINUX_AMD64:
        return (LINUX_CI,)
    return (*COMMON, *{'macos': (), 'windows': ('modern-clients.yml',), 'pi': ('modern-clients.yml', 'pi-rc.yml')}[platform])


def integer(value, lower):
    return type(value) is int and value >= lower


def number(value, low=0, high=math.inf):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_load(load, platform, profile):
    fields = ['seconds', 'points_per_sweep', 'history_points_per_sweep', 'pcm_rate_hz', 'pcm_channels',
              'online_history_verified', 'online_export_verified', 'online_backup_verified', 'offered', 'archived', 'displays',
              'offered_sweep_ids_sha256', 'reaction_p95_ms', 'queue_p95_ms', 'commit_to_display_p95_ms', 'pan_min_fps',
              'history_first_100_ms', 'history_sweeps', 'concurrent_clients', 'backup_restore_verified', 'report_sha256']
    keys(load, fields + (['producer_report_sha256', 'history_report_sha256'] if platform == LINUX_AMD64 else []), '30-minute run')
    counts = {'sweeps': 18000, 'observations': 18000, 'pcm_samples': 0 if platform == 'macos' else 86400000}
    require(type(load['points_per_sweep']) is int and load['points_per_sweep'] == 4096
            and type(load['history_points_per_sweep']) is int and load['history_points_per_sweep'] == 290,
            'workload or million-sweep history uses the wrong point grid')
    if platform == 'macos':
        require(load['pcm_rate_hz'] is None and load['pcm_channels'] is None, 'Mac release profile excludes PCM')
    else:
        require(type(load['pcm_rate_hz']) is int and load['pcm_rate_hz'] == 48000
                and type(load['pcm_channels']) is int and load['pcm_channels'] == 1, 'reference PCM must be mono 48 kHz')
    require(all(load[k] is True for k in ('online_history_verified', 'online_export_verified', 'online_backup_verified')),
            'history, export and backup must complete during acquisition')
    for field in ('offered', 'archived'):
        keys(load[field], counts, 'source totals')
        require(all(type(load[field][k]) is int and load[field][k] == n for k, n in counts.items()),
                '30-minute source totals differ from the offered workload')
    require(number(load['seconds'], 1800) and sha(load['offered_sweep_ids_sha256']), 'incomplete installed 30-minute run')
    clients = 3 if profile == 'three-clients' else 1
    displays = load['displays']
    require(isinstance(displays, list) and len(displays) == clients
            and type(load['concurrent_clients']) is int and load['concurrent_clients'] == clients,
            'a separate display ledger for each simultaneous client is required')
    for index, display in enumerate(displays, start=1):
        keys(display, ('client', 'presented_sweeps', 'presented_sweep_ids_sha256'), 'display observer')
        require(type(display['client']) is int and display['client'] == index
                and type(display['presented_sweeps']) is int and display['presented_sweeps'] == counts['sweeps']
                and display['presented_sweep_ids_sha256'] == load['offered_sweep_ids_sha256'],
                'a client failed to present the exact offered set')
    for field, maximum in [('reaction_p95_ms', 100), ('queue_p95_ms', 200), ('commit_to_display_p95_ms', 250),
                           ('history_first_100_ms', 500)]:
        require(number(load[field], 0, maximum), f'failed latency budget: {field}')
    require(number(load['pan_min_fps'], 30) and integer(load['history_sweeps'], 1000000)
            and load['backup_restore_verified'] is True, 'history, concurrency, display or restore gate incomplete')


def validate_comparisons(scan, storage):
    keys(scan, ('10000', '30000'), 'direct scan RBW profiles')
    for row in scan.values():
        keys(row, ('start_hz', 'stop_hz', 'step_hz', 'passes', 'direct_elapsed_seconds', 'installed_elapsed_seconds',
                   'direct_profile_sha256', 'installed_profile_sha256', 'direct_reference_sha256',
                   'export_sha256', 'report_sha256'), 'direct/installed scan comparison')
        require(all(integer(row[k], 1) for k in ('start_hz', 'stop_hz', 'step_hz', 'passes'))
                and row['stop_hz'] > row['start_hz'] and row['step_hz'] == 10000
                and (row['stop_hz'] - row['start_hz']) % row['step_hz'] == 0 and row['passes'] >= 10
                and sha(row['direct_profile_sha256']) and row['direct_profile_sha256'] == row['installed_profile_sha256']
                and all(sha(row[k]) for k in ('direct_reference_sha256', 'export_sha256'))
                and row['direct_reference_sha256'] != row['export_sha256'],
                'direct and installed scans must use the same acknowledged device, grid and settings')
        direct, installed = row['direct_elapsed_seconds'], row['installed_elapsed_seconds']
        require(number(direct) and direct > 0 and number(installed) and installed > 0 and direct / installed >= .9,
                'installed scan is below 90 percent of the direct measurement')
    keys(storage, ('290', '4096'), 'native storage grid profiles')
    for row in storage.values():
        keys(row, ('sweeps', 'native', 'verified_sweeps', 'verified_events', 'v6_source_sha256', 'v7_source_sha256',
                   'v6_growth_bytes', 'v7_growth_bytes', 'v6_main_bytes', 'v7_main_bytes', 'report_sha256'), 'v6/v7 comparison')
        require(row['native'] is True and integer(row['sweeps'], 512)
                and all(type(row[k]) is int and row[k] == row['sweeps'] for k in ('verified_sweeps', 'verified_events'))
                and sha(row['v6_source_sha256']) and row['v6_source_sha256'] == row['v7_source_sha256'],
                'native archive sources and replay must match on both formats')
        for kind in ('growth_bytes', 'main_bytes'):
            old, new = row['v6_' + kind], row['v7_' + kind]
            require(integer(old, 1) and integer(new, 1) and new * 10 <= old * 7, 'v7 savings are below 30 percent')


def validate_installation(record, artifact_hash, *, linux):
    fields = [*INSTALL_CHECKS, 'prior_artifact_sha256', 'report_sha256']
    keys(record, fields + (['artifact_sha256'] if linux else []), 'offline lifecycle')
    require(all(record[k] is True for k in INSTALL_CHECKS) and sha(record['prior_artifact_sha256'])
            and record['prior_artifact_sha256'] != artifact_hash, 'offline install, upgrade or distinct rollback missing')
    if linux:
        require(record['artifact_sha256'] == artifact_hash, 'offline lifecycle used another Debian package')


def validate_receipt(row, platform, revision, artifact_hash, manifest_hash, run_id, *, native_ci=None):
    keys(row, ['schema_version', 'platform', 'target', 'source_revision', 'artifact_sha256', 'manifest_sha256',
               'build_run_id', 'software_runs', 'installation', 'loads', 'hardware', 'scan', 'storage', 'protocol_report_sha256',
               'protocol_passed'], 'platform acceptance')
    require(type(row['schema_version']) is int and row['schema_version'] == 1 and row['platform'] == platform
            and row['target'] == TARGETS[platform] and sha(revision, 40) and row['source_revision'] == revision
            and integer(row['build_run_id'], 1) and row['build_run_id'] == run_id
            and row['artifact_sha256'] == artifact_hash and sha(artifact_hash)
            and row['manifest_sha256'] == manifest_hash and sha(manifest_hash), 'receipt belongs to another build')
    keys(row['software_runs'], required_workflows(platform), 'software workflow runs')
    ids = list(row['software_runs'].values())
    require(all(integer(n, 1) for n in ids) and len(set(ids)) == len(ids), 'invalid CI run IDs')
    linux = platform == LINUX_AMD64
    if linux:
        require(native_ci and row['software_runs'][LINUX_CI] == native_ci['run_id'],
                'acceptance must cite the native CI run that installed the frozen DEB')
        keys(row['installation'], DEBIAN_VERSIONS, 'Debian offline lifecycles')
        installations = list(row['installation'].values())
    else:
        installations = [row['installation']]
    for install in installations:
        validate_installation(install, artifact_hash, linux=linux)
    loads = row['loads']
    keys(loads, ('binary-v1', 'sse', 'three-clients') if platform == 'macos' else ('three-clients',), 'installed load reports')
    for profile, load in loads.items():
        validate_load(load, platform, profile)
    validate_comparisons(row['scan'], row['storage'])
    hardware = row['hardware']
    fields = ['physical', 'devices', 'seconds', 'sweeps', 'observations', 'unexplained_losses', 'duplicates', 'report_sha256']
    keys(hardware, fields + (['os_id', 'os_version', 'architecture', 'artifact_sha256', 'coverage_sha256'] if linux else []), 'physical run')
    if linux:
        require(hardware['os_id'] == 'debian' and hardware['os_version'] == '13' and hardware['architecture'] == 'amd64'
                and hardware['artifact_sha256'] == artifact_hash, 'physical run must use the frozen DEB on Debian 13 amd64')
    require(hardware['physical'] is True and hardware['devices'] == ['tinySA Ultra', 'AR-DV1']
            and number(hardware['seconds'], 8 * 60 * 60)
            and all(integer(hardware[k], 1) for k in ('sweeps', 'observations'))
            and all(type(hardware[k]) is int and hardware[k] == 0 for k in ('unexplained_losses', 'duplicates')),
            'physical two-device acceptance incomplete')
    hashes = [*(install['report_sha256'] for install in installations), hardware['report_sha256'], row['protocol_report_sha256'],
              *(entry['report_sha256'] for section in (loads, row['scan'], row['storage']) for entry in section.values())]
    if linux:
        hashes.extend([hardware['coverage_sha256'], loads['three-clients']['producer_report_sha256'],
                       loads['three-clients']['history_report_sha256']])
    require(row['protocol_passed'] is True and all(sha(h) for h in hashes) and len(set(hashes)) == len(hashes),
            'distinct successful acceptance reports required')


def verify_signature(receipt, signature, platform, public_key):
    require(platform in TARGETS and isinstance(public_key, str), 'trusted witness key unavailable')
    # Only the protected environment supplies this public key. Neither the dispatch
    # payload nor a signer identified inside an uploaded receipt is a trust root.
    key = public_key.strip().split(' ', 2)
    require(len(key) >= 2 and key[0] == 'ssh-ed25519' and re.fullmatch('[A-Za-z0-9+/=]+', key[1]),
            'configure one authorized Ed25519 witness public key')
    principal = f'ori-{platform}'
    with tempfile.TemporaryDirectory() as tmp:
        allowed = Path(tmp) / 'allowed_signers'
        allowed.write_text(f'{principal} {key[0]} {key[1]}\n', encoding='ascii')
        with receipt.open('rb') as data:
            result = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(allowed), '-I', principal,
                                     '-n', f'{NAMESPACE}/{platform}', '-s', str(signature)], stdin=data,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        require(result.returncode == 0, 'acceptance signature did not verify for the authorized platform witness')
