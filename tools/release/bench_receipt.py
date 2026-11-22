"""Prepare a blank acceptance form or compare it with retained private bench records."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from . import release_acceptance as gate
from .macos_rc_package import digest, read_json
from .release_artifacts import ARTIFACTS, DEBIAN_VERSIONS, LINUX_AMD64, LINUX_CI, TARGETS, check_manifest, require, verify_sums


def blank(fields):
    return dict.fromkeys(fields.split())


def template(platform, revision, artifact_hash, manifest_hash, run_id, *, native_ci=None):
    profiles = ('binary-v1', 'sse', 'three-clients') if platform == 'macos' else ('three-clients',)
    loads = {}
    for name in profiles:
        displays = 3 if name == 'three-clients' else 1
        loads[name] = dict(**blank('seconds points_per_sweep history_points_per_sweep pcm_rate_hz pcm_channels online_history_verified '
                                   'online_export_verified online_backup_verified offered_sweep_ids_sha256 reaction_p95_ms queue_p95_ms commit_to_display_p95_ms '
                                   'pan_min_fps history_first_100_ms history_sweeps backup_restore_verified report_sha256'),
                           offered=blank('sweeps observations pcm_samples'), archived=blank('sweeps observations pcm_samples'),
                           concurrent_clients=None, displays=[dict(client=n + 1, presented_sweeps=None, presented_sweep_ids_sha256=None)
                                                               for n in range(displays)])
    install = dict.fromkeys((*gate.INSTALL_CHECKS, 'prior_artifact_sha256', 'report_sha256'))
    hardware = blank('physical devices seconds sweeps observations unexplained_losses duplicates report_sha256')
    workflows = dict.fromkeys(gate.required_workflows(platform))
    if platform == LINUX_AMD64:
        require(native_ci is not None, 'a frozen Debian CI manifest is required')
        workflows[LINUX_CI] = native_ci['run_id']
        install = {version: dict(install, artifact_sha256=None) for version in DEBIAN_VERSIONS}
        hardware.update(blank('os_id os_version architecture artifact_sha256'))
    return dict(schema_version=1, platform=platform, target=TARGETS[platform], source_revision=revision,
                artifact_sha256=artifact_hash, manifest_sha256=manifest_hash, build_run_id=run_id,
                software_runs=workflows,
                protocol_passed=None, protocol_report_sha256=None,
                installation=install, loads=loads, hardware=hardware,
                scan={key: blank('start_hz stop_hz step_hz passes direct_elapsed_seconds installed_elapsed_seconds '
                                 'direct_profile_sha256 installed_profile_sha256 direct_reference_sha256 export_sha256 report_sha256')
                      for key in ('10000', '30000')},
                storage={key: blank('sweeps native verified_sweeps verified_events v6_source_sha256 v7_source_sha256 '
                                    'v6_growth_bytes v7_growth_bytes v6_main_bytes v7_main_bytes report_sha256')
                         for key in ('290', '4096')})


def display_ledger(path):
    require(path.is_file() and not path.is_symlink() and not Path(str(path) + '-wal').exists(),
            'use the completed, checkpointed three-client benchmark ledger')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        require(db.execute('PRAGMA application_id').fetchone() == (0x4F524942,)
                and db.execute('PRAGMA user_version').fetchone() == (1,), 'unexpected benchmark ledger format')
        sources = db.execute("SELECT id, status, verified FROM offers WHERE kind='sweep' ORDER BY id LIMIT 18001").fetchall()
        require(len(sources) == 18000 and all(row[1:] == ('accepted', 1) for row in sources),
                'all 18000 offered sweeps must be accepted and verified')
        offered = [row[0] for row in sources]
        require(len(set(offered)) == len(offered) and all(isinstance(s, str) and 0 < len(s) <= 128 for s in offered),
                'invalid offered sweep identities')
        clients = db.execute('SELECT DISTINCT client FROM rendered ORDER BY client LIMIT 4').fetchall()
        require(clients == [('0',), ('1',), ('2',)], 'three independent observed clients are required')
        offered_hash = sha256(json.dumps(offered, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        displays = []
        for index, (client,) in enumerate(clients, start=1):
            seen = [r[0] for r in db.execute('SELECT id FROM rendered WHERE client=? ORDER BY id LIMIT 18001', (client,))]
            require(seen == offered, f'client {index} did not display the complete offered source set')
            displays.append(dict(client=index, presented_sweeps=len(seen), presented_sweep_ids_sha256=offered_hash))
    return dict(concurrent_clients=len(clients), offered_sweep_ids_sha256=offered_hash, displays=displays)


def checked_file(path, expected):
    require(path.is_file() and not path.is_symlink() and digest(path) == expected, 'private evidence file differs: ' + path.name)
    return path


def compare_linux_bench(row, folder, hardware):
    for version, accepted in row['installation'].items():
        original = read_json(checked_file(folder / f'installation-debian-{version}.json', accepted['report_sha256']))
        expected = dict(os_id='debian', os_version=version, architecture='amd64', source_revision=row['source_revision'])
        expected.update({k: v for k, v in accepted.items() if k != 'report_sha256'})
        require(all(original.get(k) == v and type(original.get(k)) is type(v) for k, v in expected.items()),
                'private offline lifecycle differs from the signed Debian host, package or observations')
    expected = {k: v for k, v in row['hardware'].items() if k != 'report_sha256'}
    expected['source_revision'] = row['source_revision']
    require(all(hardware.get(k) == v and type(hardware.get(k)) is type(v) for k, v in expected.items()),
            'private hardware run differs from the signed Debian host, package or observations')


def compare_private(row, manifest, folder):
    from benchmarks.ori_bench import panorama_storage, scan_rate
    checked_file(folder / 'protocol.json', row['protocol_report_sha256'])
    hw = read_json(checked_file(folder / 'hardware.json', row['hardware']['report_sha256']), 32 * 1024 * 1024)
    if row['platform'] == LINUX_AMD64:
        compare_linux_bench(row, folder, hw)
    else:
        installation = read_json(checked_file(folder / 'installation.json', row['installation']['report_sha256']))
    if row['platform'] == 'windows':
        expected = dict(result='passed', phase='final_uninstall', package_rollback='passed',
                        network_check='disconnected_at_all_phase_checks', revision=row['source_revision'],
                        installer_sha256=row['artifact_sha256'], prior_installer_sha256=row['installation']['prior_artifact_sha256'])
        require(all(installation.get(k) == v for k, v in expected.items()),
                'installed Windows rehearsal differs from the offline accepted package and rollback')
    actual_display = display_ledger(folder / 'three-client-ledger.sqlite')
    require(all(row['loads']['three-clients'][k] == v for k, v in actual_display.items()),
            'signed client counts or presented sweep digests differ from the original display ledger')
    loads = {p: checked_file(folder / f'load-{p}.json', v['report_sha256']) for p, v in row['loads'].items()}
    if row['platform'] == 'macos':
        from benchmarks.ori_bench.mac_acceptance.hardware import require_load_pair
        identity = dict(source_revision=row['source_revision'], native_sha256=manifest['components']['ui'],
                        core_sha256=manifest['components']['ori-core'], supervisor_sha256=manifest['components']['ori-supervisor'])
        runs = require_load_pair(loads['binary-v1'], loads['sse'], identity, expected_kit_sha256=hw.get('kit_manifest_sha256'))
        require(hw.get('successful') is True and hw.get('hardware_tested') is True and hw.get('point4_passed') is True
                and hw.get('errors') == [] and hw.get('kit_verified_after_run') is True and hw.get('candidate_verified_after_run') is True
                and hw.get('hardware', {}).get('status') == 'passed' and hw.get('elapsed_seconds') == row['hardware']['seconds']
                and set(hw.get('load_run_ids', [])) == set(runs) and all(hw.get('identity', {}).get(k) == v for k, v in identity.items()),
                'installed Mac hardware evidence differs from the two accepted native loads')
    for bins, record in row['storage'].items():
        result = read_json(checked_file(folder / f'storage-{bins}.json', record['report_sha256']))
        require(all(panorama_storage.rc_checks(result).values()) and result['workload']['points_per_sweep'] == int(bins)
                and result['workload']['sweeps'] == record['sweeps'], 'original native storage comparison failed')
        for version, key in [('v6', 'full'), ('v7', 'compact')]:
            side = result[key]
            require(side['source_sha256'] == record[version + '_source_sha256']
                    and side['logical_growth_bytes'] == record[version + '_growth_bytes']
                    and side['after_checkpoint']['main_bytes'] == record[version + '_main_bytes'],
                    'signed storage figures differ from the private native comparison')
    for rbw, record in row['scan'].items():
        reference = read_json(checked_file(folder / f'direct-{rbw}.json', record['direct_reference_sha256']))
        export = checked_file(folder / f'export-{rbw}.ndjson', record['export_sha256'])
        raw = read_json(checked_file(folder / f'scan-{rbw}.json', record['report_sha256']))
        stream = (folder / f'stream-{rbw}.txt').read_text(encoding='utf-8').strip()
        calculated = scan_rate.compare(reference, export, stream)
        require(raw == calculated and raw['passed'] is True and reference['band']['rbw_hz'] == int(rbw)
                and all(reference['band'][k] == record[k] for k in ('start_hz', 'stop_hz', 'step_hz'))
                and raw['passes'] == record['passes'] and raw['direct_elapsed_seconds'] == record['direct_elapsed_seconds']
                and raw['managed_elapsed_seconds'] == record['installed_elapsed_seconds'],
                'installed scan and direct reference do not reproduce the signed figures')
        require(scan_profile_digest(reference) == record['direct_profile_sha256'],
                'scan comparison profile differs from the signed hardware configuration')


def scan_profile_digest(reference):
    keys = ('model', 'firmware', 'baud', 'band', 'receive_paths')
    return sha256(json.dumps({k: reference[k] for k in keys}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    for name in ('template', 'check'):
        form = operations.add_parser(name)
        form.add_argument('--platform', choices=ARTIFACTS, required=True)
        form.add_argument('--candidate', type=Path, required=True)
        form.add_argument('--build-run', type=int, required=True)
        form.add_argument('--receipt', type=Path, required=True)
        if name == 'check':
            form.add_argument('--private-reports', type=Path)
    operations.add_parser('displays').add_argument('--ledger', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'displays':
        print(json.dumps(display_ledger(args.ledger), indent=2))
        return
    require(args.build_run > 0, 'positive native workflow run ID required')
    manifest = args.candidate / f'{args.platform}-manifest.json'
    row = read_json(manifest)
    name = ARTIFACTS[args.platform]
    verify_sums(args.candidate, {manifest.name, name})
    check_manifest(row, args.platform, row['source_revision'], args.candidate / name)
    identity = (args.platform, row['source_revision'], row['artifact']['sha256'], digest(manifest), args.build_run)
    if args.operation == 'template':
        with args.receipt.open('x', encoding='utf-8', newline='\n') as out:
            json.dump(template(*identity, native_ci=row.get('native_ci')), out, indent=2)
            out.write('\n')
        return
    form = read_json(args.receipt)
    gate.validate_receipt(form, *identity, native_ci=row.get('native_ci'))
    if args.private_reports:
        compare_private(form, row, args.private_reports)
    print('Validated receipt SHA-256', digest(args.receipt))


if __name__ == '__main__':
    main()
