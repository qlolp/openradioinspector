"""Bench format fixtures only. No radio or platform is qualified by these tests."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_signed_release import CONFIGURATIONS, HASH, REV, receipt
from tools.release import bench_receipt, hardware_runs, release_artifacts as bundles


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp(i):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i)).isoformat()


def record(name, role, i):
    freq = 100_000_000 + i % 2 * 10_000
    item = dict(id=f'{name}-{role}-{i:02}', stream_id='port-' + role, session_id=name,
                interval=dict(started_at=stamp(i * 1800), ended_at=stamp(i * 1800)))
    if role == 'ardv1':
        raw = f'RX VFA RF{freq // 1000000:04}.{freq % 1000000 // 10:05} ST010.00 MD0F0 LM1371'
        item.update(frequency_hz=freq, level=137.0, mode='FM', level_unit='s_meter_count', squelch_open=True,
                    metadata=dict(adapter='ardv1-serial', simulated=False, receiver_firmware='2504A',
                                  scope_source='sequential_receiver_readback', scan_index=i % 2, scan_total=2,
                                  frequency_readback=True, frequency_source='receiver_RX', requested_frequency_hz=freq,
                                  observed_frequency_hz=freq, requested_mode='FM', observed_mode='FM',
                                  raw_receiver_status=raw, raw_squelch_status=1))
        return dict(kind='observation', measurement=item)
    settings = dict(simulated=False)
    if role == 'rf-explorer':
        settings.update(adapter='rf-explorer-3g-serial', detector='normal', settings_readback=True, active_input='right',
                        device_identity=dict(main_model=3, expansion_model=5, firmware='01.34'))
    else:
        settings.update(adapter='tinysa-serial', device_mode='input',
                        device_identity=dict(info='tinySA Ultra ZS405', firmware='tinySA4_v1.4-235-gea71dfd'))
    item.update(frequencies_hz=[100_000_000, 100_010_000], levels=[-71.0, -64.5], level_unit='dBm', settings=settings)
    return dict(kind='sweep', measurement=item)


class PhysicalShiftEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.manifest = dict(platform='pi', source_revision=REV, artifact={'sha256': HASH}, components={'ori-core': 'c' * 64})

    def files(self, name):
        roles = CONFIGURATIONS[name]
        case = self.folder / 'hardware' / name
        case.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'hardware-coverage.jsonl' if name == 'tinysa-ardv1' else case / 'coverage.ndjson'
        items = [dict(format='openradioinspector-archive-ndjson', format_version=1, session_id=name)]
        items.extend(record(name, role, i) for role in roles for i in range(17))
        metadata = dict(configuration=name, physical=True, platform='pi', source_revision=REV,
                        artifact_sha256=HASH, core_sha256='c' * 64, session_id=name,
                        host=dict(os_id='debian', os_version='13', architecture='arm64', model='Raspberry Pi 5 Model B Rev 1.0'),
                        streams={role: dict(stream_id='port-' + role, max_silence_seconds=1800) for role in roles},
                        unexplained_losses=0, duplicates=0, planned_gaps=[])
        def save(update_hash=True):
            path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in items))
            if update_hash:
                metadata['coverage_sha256'] = sha(path.read_bytes())
            (case / 'run.json').write_text(json.dumps(metadata), encoding='utf-8')
        save()
        return metadata, items, save

    def project(self, name):
        projector = getattr(hardware_runs, 'project', None)
        self.assertIsNotNone(projector, 'the private archive must be checked, not just named by a digest')
        return projector(self.folder, name, self.manifest)

    def test_recomputes_exact_source_ids_units_and_the_common_eight_hours_for_each_configuration(self):
        for name, roles in CONFIGURATIONS.items():
            with self.subTest(name=name):
                metadata, items, _ = self.files(name)
                row = self.project(name)
                self.assertEqual(row['seconds'], 28800)
                self.assertEqual(row['coverage_sha256'], metadata['coverage_sha256'])
                self.assertEqual(row['session_sha256'], sha(name.encode()))
                self.assertEqual(row['report_sha256'], sha((self.folder / 'hardware' / name / 'run.json').read_bytes()))
                for channel in roles:
                    expected = sorted(r['measurement']['id'] for r in items[1:] if r['measurement']['stream_id'] == 'port-' + channel)
                    digest = sha(b''.join(json.dumps(value).encode() + b'\n' for value in expected))
                    self.assertEqual(row['sources'][channel]['ids_sha256'], digest)
                    self.assertEqual(row['sources'][channel]['records'], 17)

    def test_incomplete_or_mislabeled_physical_originals_rejected_even_with_matching_file_hash(self):
        changes = [
            ('wrong OS', lambda h, r: h['host'].update(architecture='amd64')),
            ('virtual Pi', lambda h, r: h['host'].update(model='Hosted ARM runner')),
            ('wrong core', lambda h, r: h.update(core_sha256='a' * 64)),
            ('wrong session', lambda h, r: r[-1]['measurement'].update(session_id='other')),
            ('unreported stream', lambda h, r: r[-1]['measurement'].update(stream_id='other')),
            ('duplicate ID', lambda h, r: r[-1]['measurement'].update(id=r[1]['measurement']['id'])),
            ('simulator', lambda h, r: r[1]['measurement']['settings'].update(simulated=True)),
            ('replay', lambda h, r: r[1]['measurement']['settings'].update(replay=True)),
            ('wrong firmware', lambda h, r: r[1]['measurement']['settings']['device_identity'].update(firmware='02.00')),
            ('unacknowledged detector', lambda h, r: r[1]['measurement']['settings'].update(detector='max_hold')),
            ('missing amplitude', lambda h, r: r[1]['measurement'].update(levels=[])),
            ('invented scale', lambda h, r: r[-1]['measurement'].update(level_unit='dBm')),
            ('short one source', lambda h, r: r.pop()),
            ('silent interval', lambda h, r: r.pop(4)),
            ('naive time', lambda h, r: r[1]['measurement']['interval'].update(ended_at='2026-01-01T00:00:00')),
        ]
        for reason, corrupt in changes:
            with self.subTest(reason=reason):
                meta, rows, save = self.files('rf-explorer-ardv1')
                corrupt(meta, rows)
                save()
                with self.assertRaises(ValueError):
                    self.project('rf-explorer-ardv1')

    def test_solo_ardv1_cannot_be_certified_with_static_tuning_or_unconfirmed_rx(self):
        for invalid in ('static', 'raw', 'frequency', 'level'):
            with self.subTest(invalid=invalid):
                meta, rows, save = self.files('ardv1')
                if invalid == 'static':
                    for row in rows[1:]:
                        del row['measurement']['metadata']['scope_source']
                else:
                    row = rows[-1]['measurement']
                    if invalid == 'raw':
                        row['metadata']['raw_receiver_status'] = '60?'
                    elif invalid == 'frequency':
                        row['metadata']['observed_frequency_hz'] += 10
                    else:
                        row['level'] = 12
                save()
                with self.assertRaises(ValueError):
                    self.project('ardv1')

    def test_a_changed_source_file_and_two_eight_hour_runs_without_overlap_both_fail(self):
        meta, rows, save = self.files('rf-explorer-ardv1')
        rows[1]['measurement']['levels'][0] = -43
        save(update_hash=False)
        with self.assertRaises(ValueError):
            self.project('rf-explorer-ardv1')
        for i, row in enumerate(rows[18:]):
            row['measurement']['interval'] = dict(started_at=stamp(28800 + 1800 * i), ended_at=stamp(28800 + 1800 * i))
        save()
        with self.assertRaises(ValueError):
            self.project('rf-explorer-ardv1')

    def test_cli_projects_actual_candidate_files_and_requires_originals_for_local_acceptance(self):
        candidate = self.folder / 'frozen'
        package = self.folder / 'distribution.deb'
        package.write_bytes(b'unit test fixture')
        bundles.freeze('pi', {'components': {'ori-core': {'sha256': 'c' * 64}, 'ori-supervisor': {'sha256': HASH}},
                              'source_revision': REV, 'manual_sha256': HASH, 'release_version': '0.4.0'},
                       {'source_revision': REV, 'signing': {'kind': 'debian-sha256'}, 'ui_sha256': HASH},
                       package, candidate)
        manifest = candidate / 'pi-manifest.json'
        self.manifest = bundles.read_json(manifest)
        form = receipt('pi', bundles.digest(package), bundles.digest(manifest))
        hardware = {}
        for name in CONFIGURATIONS:
            meta, _, save = self.files(name)
            meta['artifact_sha256'] = bundles.digest(package)
            save()
            hardware[name] = self.project(name)
        args = ['--platform', 'pi', '--candidate', str(candidate)]
        output = io.StringIO()
        with patch('sys.argv', ['bench_receipt', 'hardware-template', *args, '--configuration', 'rf-explorer-ardv1']), redirect_stdout(output):
            try:
                bench_receipt.main()
            except SystemExit as error:
                self.fail(f'expected private hardware report template, CLI exit {error.code}')
        blank = json.loads(output.getvalue())
        self.assertEqual(blank['artifact_sha256'], bundles.digest(package))
        self.assertIsNone(blank['physical'])
        self.assertIsNone(blank['streams']['rf-explorer']['stream_id'])
        output = io.StringIO()
        with patch('sys.argv', ['bench_receipt', 'hardware', *args, '--private-reports', str(self.folder)]), redirect_stdout(output):
            try:
                bench_receipt.main()
            except SystemExit as error:
                self.fail(f'expected public projection of the original archives, CLI exit {error.code}')
        self.assertEqual(json.loads(output.getvalue()), hardware)
        form['hardware_runs'] = hardware
        form['hardware'].update(sweeps=17, observations=17)
        form_path = self.folder / 'form.json'
        bundles.write_json(form_path, form)
        with patch('sys.argv', ['bench_receipt', 'check', *args, '--build-run', '42', '--receipt', str(form_path)]):
            with self.assertRaisesRegex(ValueError, 'private'):
                bench_receipt.main()

        form['hardware_runs']['ardv1']['sources']['ardv1']['records'] = 18
        with self.assertRaisesRegex(ValueError, 'ardv1'):
            bench_receipt.compare_private(form, self.manifest, self.folder)

    def test_a_planned_reconnect_on_one_source_reduces_verified_seconds_for_both(self):
        name = 'rf-explorer-ardv1'
        meta, rows, save = self.files(name)
        # Delay both complete streams by five minutes after a documented disconnect.
        for row in rows[1:]:
            item = row['measurement']
            index = int(item['id'].rsplit('-', 1)[-1])
            if index >= 7:
                item['interval'] = dict(started_at=stamp(index * 1800 + 300), ended_at=stamp(index * 1800 + 300))
        meta['planned_gaps'] = [dict(source=role, reason='usb_reconnect', started_at=stamp(7 * 1800),
                                     ended_at=stamp(7 * 1800 + 300)) for role in CONFIGURATIONS[name]]
        save()
        self.assertEqual(self.project(name)['seconds'], 28800)
        meta['planned_gaps'].pop()
        save()
        with self.assertRaises(ValueError):
            self.project(name)

    def test_foreign_target_firmware_or_malformed_original_cannot_be_qualified(self):
        for platform, os_id, os_version, arch, model in (
            ('macos','macos','27.3','arm64','MacBookAir'), ('windows','windows','11','amd64','Bench PC'),
            ('linux-amd64','debian','13','amd64','Bench PC')):
            name = 'tinysa'
            self.manifest['platform'] = platform
            meta, rows, save = self.files(name)
            meta.update(platform=platform, host=dict(os_id=os_id, os_version=os_version, architecture=arch, model=model))
            save()
            self.assertEqual(self.project(name)['seconds'], 28800)
            rows[-1]['measurement']['settings']['device_identity']['info'] = 'generic Ultra analyzer'
            save()
            with self.assertRaises(ValueError):
                self.project(name)

    def test_a_manual_hold_can_never_supply_the_solo_panorama_coverage_window(self):
        name = 'ardv1'
        meta, rows, save = self.files(name)
        for row in rows[3:]:
            del row['measurement']['metadata']['scope_source']
        save()
        with self.assertRaises(ValueError):
            self.project(name)

    def test_impossible_native_rx_count_and_inconsistent_squelch_are_rejected(self):
        name = 'ardv1'
        for count, squelch in ((1_000_000, 1), (137, True)):
            meta, rows, save = self.files(name)
            row = rows[-1]['measurement']
            row['metadata']['raw_receiver_status'] = row['metadata']['raw_receiver_status'].replace('LM1371', f'LM{count}1')
            row['level'] = count
            row['metadata']['raw_squelch_status'] = squelch
            save()
            with self.assertRaises(ValueError):
                self.project(name)
