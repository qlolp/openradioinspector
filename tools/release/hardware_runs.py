"""Project installed physical shifts into a privacy-preserving platform release receipt."""
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile

from benchmarks.ori_bench.client import LINE_LIMIT
from benchmarks.ori_bench.scan_rate import FIRMWARE, MODEL, timestamp
from tools.modernization.api_parity import load_json
from .release_artifacts import keys, require


CONFIGURATIONS = {
    'tinysa': ('tinysa',),
    'rf-explorer': ('rf-explorer',),
    'ardv1': ('ardv1',),
    'tinysa-ardv1': ('tinysa', 'ardv1'),
    'rf-explorer-ardv1': ('rf-explorer', 'ardv1'),
}
SOURCE_FORMAT = {'tinysa': ('sweep', 'dBm'), 'rf-explorer': ('sweep', 'dBm'),
                 'ardv1': ('observation', 's_meter_count')}
SHIFT_SECONDS = 8 * 60 * 60
RUN_FIELDS = ('physical', 'seconds', 'unexplained_losses', 'duplicates', 'session_sha256',
              'coverage_sha256', 'report_sha256', 'sources')


def template():
    shifts = {}
    for name, roles in CONFIGURATIONS.items():
        run = dict.fromkeys(RUN_FIELDS)
        run['sources'] = {role: dict(kind=SOURCE_FORMAT[role][0], unit=SOURCE_FORMAT[role][1],
                                     records=None, ids_sha256=None) for role in roles}
        shifts[name] = run
    return shifts


def validate(shifts, paired):
    """Return the digests that must be distinct from every other qualification report."""
    keys(shifts, CONFIGURATIONS, 'five hardware configurations')
    hashes = []
    for name, roles in CONFIGURATIONS.items():
        run = shifts[name]
        keys(run, RUN_FIELDS, name + ' shift')
        require(run['physical'] is True and type(run['seconds']) is int and run['seconds'] >= SHIFT_SECONDS
                and all(type(run[k]) is int and run[k] == 0 for k in ('duplicates', 'unexplained_losses')),
                name + ' requires eight verified physical hours without unexplained gaps or duplicate IDs')
        keys(run['sources'], roles, name + ' physical sources')
        hashes.extend(run[k] for k in ('session_sha256', 'coverage_sha256', 'report_sha256'))
        for role, source in run['sources'].items():
            keys(source, ('kind', 'unit', 'records', 'ids_sha256'), role + ' source')
            require((source['kind'], source['unit']) == SOURCE_FORMAT[role]
                    and type(source['records']) is int and source['records'] >= 2,
                    'wrong native source, unit or count for ' + role)
            hashes.append(source['ids_sha256'])
    pair = shifts['tinysa-ardv1']
    require(pair['sources']['tinysa']['records'] == paired['sweeps']
            and pair['sources']['ardv1']['records'] == paired['observations'],
            'paired counters differ from the tinySA and AR-DV1 shift')
    if 'coverage_sha256' in paired:
        require(paired['coverage_sha256'] == pair['coverage_sha256'],
                'the Debian paired shift must use the same original archive export')
    return hashes


def private_template(name, manifest):
    return dict(configuration=name, platform=manifest['platform'], source_revision=manifest['source_revision'],
                artifact_sha256=manifest['artifact']['sha256'], core_sha256=manifest['components']['ori-core'],
                host=dict.fromkeys(('os_id', 'os_version', 'architecture', 'model')),
                physical=None, session_id=None, coverage_sha256=None, unexplained_losses=None, duplicates=None,
                streams={role: dict(stream_id=None, max_silence_seconds=None) for role in CONFIGURATIONS[name]},
                planned_gaps=[])


def valid_id(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 256 and bool(value.strip())


def utc(value):
    result = timestamp(value)
    require(result.utcoffset() == timedelta(0), 'physical archive times must be UTC')
    return result


def check_host(host, platform):
    system, version, arch = host['os_id'], host['os_version'], host['architecture']
    require(valid_id(host['model']) and isinstance(version, str), 'missing physical host identity')
    if platform == 'pi':
        ok = (system in ('raspbian', 'debian') and arch == 'arm64' and re.fullmatch(r'[1-9][0-9]{0,2}', version)
              and int(version) >= 12 and re.fullmatch(r'Raspberry Pi 5(?: Model B)?(?: Rev [0-9.]+)?', host['model']))
    elif platform == 'linux-amd64':
        ok = (system, version, arch) == ('debian', '13', 'amd64')
    elif platform == 'macos':
        ok = system == 'macos' and arch == 'arm64' and re.fullmatch(r'27\.3(?:\.[0-9]+)*', version)
    elif platform == 'windows':
        ok = system == 'windows' and version == '11' and arch in ('x86_64', 'amd64')
    else:
        ok = False
    require(ok, 'physical host does not match the target platform')


def analyzer_identity(item, role):
    settings = item['settings']
    frequencies, levels = item['frequencies_hz'], item['levels']
    require(isinstance(frequencies, list) and isinstance(levels, list) and len(frequencies) == len(levels) >= 2
            and all(type(hz) is int and hz > 0 for hz in frequencies)
            and all(right > left for left, right in zip(frequencies, frequencies[1:]))
            and all(type(v) in (float, int) and math.isfinite(v) for v in levels), 'invalid original analyzer sweep')
    device = settings['device_identity']
    if role == 'tinysa':
        require(settings['adapter'] == 'tinysa-serial' and settings['device_mode'] == 'input'
                and any(s.strip().casefold() == MODEL.casefold() for s in device['info'].splitlines())
                and device['firmware'].strip().splitlines() in ([FIRMWARE], [FIRMWARE, 'HW Version:V0.4.5.1']),
                'unverified tinySA Ultra receive profile')
        return device
    require(settings['adapter'] == 'rf-explorer-3g-serial' and settings['settings_readback'] is True
            and settings['detector'] == 'normal' and settings['active_input'] in ('left', 'right')
            and device == {'main_model': 3, 'expansion_model': 5, 'firmware': '01.34'},
            'unverified RF Explorer profile, input or detector')
    return device, settings['active_input']


RX = re.compile(r'RX VFA RF([0-9]{4})\.([0-9]{5}) ST[0-9]{3}\.[0-9]{2} MD[0-7]([0-7F][0-6]) LM([0-9]{1,6})([0-3])')
MODES = {'00': 'AUTO', '10': 'DSTAR', '20': 'YSF', '30': 'ALINCO', '40': 'NXDN',
         '50': 'P25', '60': 'DPMR', '70': 'DMR', 'F0': 'FM', 'F1': 'AM'}


def receiver_identity(item, primary):
    metadata = item['metadata']
    hz, level = item['frequency_hz'], item['level']
    require(metadata['adapter'] == 'ardv1-serial' and valid_id(metadata['receiver_firmware'])
            and type(hz) is int and 100_000 <= hz <= 1_300_000_000 and hz % 10 == 0
            and type(level) in (int, float) and 0 <= level <= 999999 and int(level) == level,
            'invalid original AR-DV1 observation')
    if primary or metadata['frequency_readback'] is True:
        raw = RX.fullmatch(metadata['raw_receiver_status'])
        require(raw and int(raw[1] + raw[2]) * 10 == hz and int(raw[4]) == level
                and metadata['frequency_readback'] is True and metadata['frequency_source'] == 'receiver_RX'
                and type(metadata['observed_frequency_hz']) is int and metadata['observed_frequency_hz'] == hz
                and MODES.get(raw[3]) == item['mode'] == metadata['observed_mode']
                and type(metadata['raw_squelch_status']) is int and int(raw[5]) == metadata['raw_squelch_status'] and item['squelch_open'] is (int(raw[5]) != 0),
                'AR-DV1 RX differs from original frequency, mode, squelch or raw S-meter count')
    else:
        require(metadata['frequency_readback'] is False and metadata['frequency_source'] == 'command_acknowledged'
                and metadata['requested_frequency_hz'] == hz, 'missing paired receiver frequency provenance')
    return metadata['receiver_firmware']


def planned_gaps(report, roles):
    gaps = {role: [] for role in roles}
    for gap in report['planned_gaps']:
        role = gap['source']
        begin, end = utc(gap['started_at']), utc(gap['ended_at'])
        require(role in gaps and gap['reason'] == 'usb_reconnect' and 0 < (end - begin).total_seconds() <= 300,
                'invalid per-source USB interruption')
        if gaps[role]:
            require(gaps[role][-1][1] <= begin, 'overlapping or unordered USB interruptions')
        gaps[role].append((begin, end))
    return gaps


def covered_seconds(start, stop, gaps):
    # Subtract the union so a shared outage never counts as physical coverage.
    cursor = start
    total = timedelta()
    for begin, end in sorted((a, b) for channel in gaps.values() for a, b in channel):
        require(start <= begin < end <= stop, 'USB interruption outside the common measurement interval')
        total += max(begin - cursor, timedelta())
        cursor = max(cursor, end)
    return (total + (stop - cursor)) // timedelta(seconds=1)


def inspect_archive(path, report, name, ids):
    from .linux_bench import allowed_pause

    roles = CONFIGURATIONS[name]
    streams = report['streams']
    keys(streams, roles, 'observed streams')
    require(isinstance(report['planned_gaps'], list), 'expected a list of measured USB interruptions')
    gaps = planned_gaps(report, roles)
    by_stream = {}
    for role, stream in streams.items():
        identity, limit = stream['stream_id'], stream['max_silence_seconds']
        require(valid_id(identity) and identity not in by_stream and type(limit) in (int, float) and 0 < limit <= 3600,
                'missing distinct stream or measured per-source cadence')
        by_stream[identity] = role
    first, last, fingerprints = {}, {}, {}
    cyclic_hz, varied_scan = None, False
    archive_hash = hashlib.sha256()
    with path.open('rb') as source:
        for index, line in enumerate(iter(lambda: source.readline(LINE_LIMIT + 1), b'')):
            require(len(line) <= LINE_LIMIT and line.endswith(b'\n'), 'invalid or incomplete archive record')
            archive_hash.update(line)
            entry = load_json(line, LINE_LIMIT)
            if index == 0:
                require(entry == dict(format='openradioinspector-archive-ndjson', format_version=1,
                                      session_id=report['session_id']) and type(entry['format_version']) is int,
                        'wrong physical archive session or format')
                continue
            item = entry['measurement']
            role = by_stream.get(item['stream_id'])
            require(role is not None and item['session_id'] == report['session_id']
                    and (entry['kind'], item['level_unit']) == SOURCE_FORMAT[role], 'foreign source, kind or units')
            provenance = item['metadata'] if role == 'ardv1' else item['settings']
            require(provenance['simulated'] is False and provenance.get('replay', False) is False,
                    'simulated or replayed source')
            if role == 'ardv1':
                fingerprint = receiver_identity(item, name == 'ardv1')
                if name == 'ardv1' and provenance.get('scope_source') == 'sequential_receiver_readback':
                    require(item['mode'] == 'FM' and type(provenance['scan_index']) is int and
                            type(provenance['scan_total']) is int and 0 <= provenance['scan_index'] < provenance['scan_total'],
                            'invalid primary AR-DV1 scan point')
                    varied_scan |= cyclic_hz is not None and cyclic_hz != item['frequency_hz']
                    cyclic_hz = item['frequency_hz']
            else:
                fingerprint = analyzer_identity(item, role)
            require(fingerprints.setdefault(role, fingerprint) == fingerprint, 'physical source identity changed')
            identifier = item['id']
            require(valid_id(identifier), 'missing original measurement ID')
            ids.execute('INSERT INTO sources VALUES (?,?)', (identifier, role))
            begin, end = utc(item['interval']['started_at']), utc(item['interval']['ended_at'])
            limit = streams[role]['max_silence_seconds']
            require(begin <= end and (end - begin).total_seconds() <= limit, 'invalid physical measurement interval')
            # Manual holds still contribute their original IDs. Only scan readbacks
            # contribute to the solo panorama's eight-hour coverage and cadence.
            if name == 'ardv1' and provenance.get('scope_source') != 'sequential_receiver_readback':
                continue
            if role in last:
                require(begin >= last[role][0] and end >= last[role][1] and allowed_pause(last[role][1], end, limit, gaps[role]),
                        'regressing time or unexplained measurement gap')
            first.setdefault(role, end)
            last[role] = (begin, end)
    require(archive_hash.hexdigest() == report['coverage_sha256'], 'physical archive digest differs')
    require(first.keys() == set(roles) and (name != 'ardv1' or varied_scan), 'missing source or solo AR-DV1 scan')
    seconds = covered_seconds(max(first.values()), min(end for _, end in last.values()), gaps)
    require(seconds >= SHIFT_SECONDS, 'less than eight hours of common physical coverage')
    return seconds


def project(folder, name, manifest):
    """Read fixed private filenames and return only aggregates and hashes for one configuration."""
    require(name in CONFIGURATIONS, 'unknown hardware configuration')
    parent = folder / 'hardware'
    case = parent / name
    metadata = case / 'run.json'
    path = folder / 'hardware-coverage.jsonl' if name == 'tinysa-ardv1' else case / 'coverage.ndjson'
    require(not parent.is_symlink() and not case.is_symlink() and all(p.is_file() and not p.is_symlink() for p in (path, metadata)),
            'missing or linked private shift originals for ' + name)
    try:
        with metadata.open('rb') as stream:
            raw = stream.read(65537)
        report = load_json(raw, 65536)
        require(report['physical'] is True and report['configuration'] == name
                and report['platform'] == manifest['platform'] and report['source_revision'] == manifest['source_revision']
                and report['artifact_sha256'] == manifest['artifact']['sha256'] and report['core_sha256'] == manifest['components']['ori-core']
                and valid_id(report['session_id']) and all(type(report[k]) is int and report[k] == 0 for k in ('unexplained_losses', 'duplicates')),
                'wrong physical shift, source revision, installed bytes or measured loss')
        check_host(report['host'], manifest['platform'])
        sources = {}
        # Only IDs are indexed on disk. Point arrays from the shift are read one record at a time.
        with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(Path(tmp) / 'ids.sqlite')) as ids:
            ids.execute('CREATE TABLE sources(id TEXT PRIMARY KEY, role TEXT NOT NULL) WITHOUT ROWID')
            seconds = inspect_archive(path, report, name, ids)
            for role in CONFIGURATIONS[name]:
                digest = hashlib.sha256()
                count = 0
                for (identifier,) in ids.execute('SELECT id FROM sources WHERE role=? ORDER BY id', (role,)):
                    digest.update(json.dumps(identifier, ensure_ascii=False).encode('utf-8') + b'\n')
                    count += 1
                require(count >= 2, 'insufficient distinct original IDs')
                kind, unit = SOURCE_FORMAT[role]
                sources[role] = dict(kind=kind, unit=unit, records=count, ids_sha256=digest.hexdigest())
        return dict(physical=True, seconds=seconds, unexplained_losses=0, duplicates=0, sources=sources,
                    session_sha256=hashlib.sha256(report['session_id'].encode('utf-8')).hexdigest(),
                    report_sha256=hashlib.sha256(raw).hexdigest(), coverage_sha256=report['coverage_sha256'])
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, sqlite3.Error) as error:
        raise ValueError('incomplete or inconsistent private physical evidence: ' + name) from error
