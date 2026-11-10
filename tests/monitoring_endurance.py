"""Accelerated synthetic acceptance, never a substitute for an RF bench run.

Default: seven logical days, one 61-bin sweep/minute, explicit missing slots.
The generator, processing batches and verification pages have fixed bounds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta, timezone

from radioinspector.automatic_detection import detect_regions
from radioinspector.contracts import Sweep, TimeInterval, parse_time, to_primitive
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.monitoring.detector import Detector, build_baseline
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_device_workers import simulated_session_payload

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
GRID = tuple(100_000_000 + 100_000*i for i in range(61))
PARAMETERS = dict(margin_db=6., impulse_db=12., hysteresis_db=3., confirmations=3, window_sweeps=5)
ALLOWED = [dict(id='authorized-carrier', start_hz=GRID[45], end_hz=GRID[45], watch_changes=False)]
MAX_SLOTS = 7*24*60


def _process_peak_rss():
    if sys.platform != 'win32':
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == 'darwin' else value*1024
    import ctypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = ctypes.c_void_p
    measure = ctypes.windll.psapi.GetProcessMemoryInfo
    measure.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not measure(current(), ctypes.byref(counters), counters.cb):
        raise OSError('Cannot measure process peak working set')
    return counters.PeakWorkingSetSize


def fixture(index, seed, session='synthetic-session', stream='synthetic-analyzer', *, incompatible=False):
    """Recreate any source by ID without retaining previous sources or RNG state."""
    minute = index % 1440
    if 120 <= minute < 125:
        return None
    rng = random.Random(seed*1_000_003 + index)
    levels = [-108. + rng.uniform(-.25, .25) for _ in GRID]
    for bin_index in range(3, 10):
        levels[bin_index] = -95. + .15*(bin_index-3) + math.sin(index/1440*2*math.pi) + rng.uniform(-.25, .25)
    levels[45] = -62. + rng.uniform(-.2, .2)
    if minute in (40, 42, 44):
        levels[20] = -99.5 + rng.uniform(-.25, .25)
    if minute == 80:
        levels[30] = -88.
    if 100 <= minute <= 102:
        levels = [value + 18. for value in levels]
    started = START + timedelta(minutes=index)
    return Sweep(id=f'synthetic-{index:05d}', session_id=session, stream_id=stream,
        interval=TimeInterval(started, started+timedelta(seconds=1)), frequencies_hz=GRID,
        levels=tuple(levels), level_unit='dBm', settings=dict(adapter='synthetic-acceptance',
            rbw_hz=30000 if incompatible else 10000, simulated=True, sequence=index,
            generator_output=False))


def _truth(index):
    minute, day = index % 1440, index//1440
    if 40 <= minute <= 44:
        return 'weak', day
    if minute == 80:
        return 'impulse', day
    if 100 <= minute <= 102:
        return 'broad_scene', day
    return None


def _legacy_regions(sweep, threshold):
    """Use actual legacy decisions; recover their bin span only for scoring/catalog."""
    result = []
    for region in detect_regions(sweep, threshold_dbm=threshold, maximum=61):
        peak = GRID.index(region['center_frequency_hz'])
        first = last = peak
        while first and sweep.levels[first-1] >= threshold:
            first -= 1
        while last+1 < len(GRID) and sweep.levels[last+1] >= threshold:
            last += 1
        if any(rule['start_hz'] <= GRID[first] and rule['end_hz'] >= GRID[last] for rule in ALLOWED):
            continue
        result.append(dict(first_bin=first, last_bin=last))
    return result


class _Score:
    def __init__(self):
        self.expected = {kind: set() for kind in ('weak', 'impulse', 'broad_scene')}
        self.hits = {kind: set() for kind in self.expected}
        self.false_frames = self.false_episodes = self.quiet = 0
        self.false_active = False

    def step(self, regions, index):
        truth = _truth(index)
        if truth:
            self.expected[truth[0]].add(truth[1])
        false = False
        for region in regions:
            lo, hi = region['first_bin'], region['last_bin']
            matches = truth and (
                truth[0] == 'weak' and lo <= 20 <= hi or
                truth[0] == 'impulse' and lo <= 30 <= hi or
                truth[0] == 'broad_scene' and hi-lo+1 >= 31)
            if matches:
                self.hits[truth[0]].add(truth[1])
            else:
                false = True
        if false:
            self.false_frames += 1
            if not self.false_active:
                self.false_episodes += 1
            self.false_active, self.quiet = True, 0
        else:
            self.quiet += 1
            if self.quiet >= 5:
                self.false_active = False

    def report(self):
        result = dict(false_positive_frames=self.false_frames, false_alarm_episodes=self.false_episodes)
        for kind in self.expected:
            expected, hits = len(self.expected[kind]), len(self.hits[kind])
            result[kind] = dict(expected=expected, detected=hits, recall=hits/expected if expected else None)
        return result


def _calibrated_threshold(seed):
    # Separate seed; the highest half-dB threshold recovering every weak present
    # frame, a stricter calibration requirement than event-level recall.
    samples = [fixture(index, seed+1) for index in (40, 42, 44)]
    thresholds = [-110. + .5*i for i in range(41)]
    suitable = [value for value in thresholds if all(
        any(region['first_bin'] <= 20 <= region['last_bin'] for region in _legacy_regions(sweep, value))
        for sweep in samples)]
    return max(suitable)


def _last_source_index(slots):
    index = slots-1
    while 120 <= index % 1440 < 125:
        index -= 1
    return index


def compare_detectors(*, seed=17, slots=MAX_SLOTS):
    baseline = build_baseline([fixture(i, seed) for i in range(20)])
    detector = Detector(baseline, PARAMETERS)
    threshold = _calibrated_threshold(seed)
    old, new = _Score(), _Score()
    for index in range(20, _last_source_index(slots)):
        sweep = fixture(index, seed)
        if sweep is None:
            continue
        old.step(_legacy_regions(sweep, threshold), index)
        new.step(detector.step(sweep, ALLOWED)[0], index)
    old_report, new_report = old.report(), new.report()
    return dict(legacy_fixed_threshold=old_report, robust_city=new_report,
        legacy_threshold_dbm=threshold, comparable_weak_recall=old_report['weak']['recall']==new_report['weak']['recall'],
        legacy_function='radioinspector.automatic_detection.detect_regions',
        current_function='radioinspector.monitoring.detector.Detector.step',
        catalog=ALLOWED, parameters=PARAMETERS, hardware_validation='not_run', synthetic=True,
        limits=['Seeded synthetic city background; no field false-alarm claim.',
            'Identical sources/catalog; legacy threshold frozen using separate calibration seed.',
            'Recall counts target episodes; false-positive frames count sweeps with unmatched regions.',
            'False alarm episodes share a five-quiet-sweep closure rule; these are comparison metrics, not delivered notifications.',
            'Sweep-only observations do not establish modulation, identity, IQ or calibrated RF sensitivity.'])


def _page_records(service, kind, project):
    after = ''
    while True:
        items = service.store.list(kind, project, limit=64, after=after)
        if not items:
            return
        previous = after
        for item in items:
            assert item['id'] > previous, 'metadata pagination repeated or reordered an ID'
            previous = item['id']
            yield item
        after = items[-1]['id']


def _event_signature(service, project):
    digest, count = hashlib.sha256(), 0
    for item in _page_records(service, 'event', project):
        digest.update(item['id'].encode())
        count += 1
    return count, digest.hexdigest()


def run_acceptance(path, *, seed=17, slots=MAX_SLOTS):
    if type(slots) is not int or not 180 <= slots <= MAX_SLOTS:
        raise ValueError('slots must be 180..10080 (bounded smoke run through seven logical days)')
    path = Path(path)
    last_source_index = _last_source_index(slots)
    if path.exists():
        raise ValueError('Acceptance archive must be a new path')
    started = time.monotonic()
    tracemalloc.start()
    python_diagnostic_peak = 0
    def finish_python_diagnostic():
        nonlocal python_diagnostic_peak
        if tracemalloc.is_tracing():
            python_diagnostic_peak = max(python_diagnostic_peak, tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
    sessions = workers = service = None
    maxima = dict(sessions=0, candidates=0, investigations=0, investigation_owners=0, events=0)
    raw_count = gaps = metadata_jobs = processed = 0
    restarted = False
    sid = stream = receiver = project_id = None
    def open_core():
        sessions = SessionService(store=SQLiteSessionStore(str(path)), history_limit=128)
        workers = DeviceWorkerManager(sessions)
        return sessions, workers, MonitoringService(sessions, workers)
    def close_core():
        if service is not None:
            service.close()
        if workers is not None:
            workers.close()
        if sessions is not None:
            sessions.close()
    def sample_registry():
        current = dict(sessions=len(sessions._sessions), candidates=len(sessions._candidates),
            investigations=len(sessions._investigations), investigation_owners=len(sessions._investigation_sessions),
            events=len(sessions._events))
        for key, value in current.items():
            maxima[key] = max(maxima[key], value)
    def drain():
        total = 0
        while True:
            count = service.analyze(project_id)['processed_sweeps']
            total += count
            if not count:
                return total
    try:
        sessions, workers, service = open_core()
        payload = simulated_session_payload()
        payload['id'] = 'synthetic-acceptance-session'
        for item, identifier in zip(payload['streams'], ('synthetic-analyzer', 'synthetic-receiver')):
            item['id'] = identifier
        session = sessions.create_session(payload)
        sid = session.id
        stream, receiver = [item.id for item in session.streams]
        project = service.create(dict(name='Synthetic city acceptance', site='Seeded fixture', session_id=sid))
        project_id = project['id']
        service.add_allowed(project_id, dict(name='Authorized fixture carrier', start_hz=GRID[45], end_hz=GRID[45]))
        for index in range(slots):
            if index == 128:
                finish_python_diagnostic()
            sweep = fixture(index, seed, sid, stream, incompatible=index==last_source_index)
            if sweep is None:
                gaps += 1
                continue
            sessions.ingest_sweep(sweep)
            raw_count += 1
            if index == 19:
                service.create_baseline(project_id, dict(name='Synthetic baseline', location='Seeded fixture', confirm=True))
            elif 20 <= index < last_source_index:
                if index % 20 == 0:
                    candidate = sessions.create_candidate(sid, dict(id=f'cache-candidate-{index}', source_stream_id=stream,
                        interval=to_primitive(sweep.interval), center_frequency_hz=GRID[20], bandwidth_hz=100000,
                        peak_level=sweep.levels[20], level_unit='dBm', score=1.))
                    job = sessions.enqueue_investigation(sid, candidate.id,
                        dict(id=f'cache-job-{index}', target_stream_id=receiver))
                    sessions.cancel_investigation(sid, job.id)
                    metadata_jobs += 1
                if index % 32 == 0 or index >= slots//2 and not restarted or index == last_source_index-1:
                    processed += drain()
                if index >= slots//2 and not restarted:
                    before = _event_signature(service, project_id)
                    close_core()
                    sessions, workers, service = open_core()
                    assert drain() == 0, 'restart replayed already analyzed sweeps'
                    assert _event_signature(service, project_id) == before, 'restart duplicated event IDs'
                    assert workers.active_count == 0, 'synthetic runner unexpectedly started an instrument worker'
                    restarted = True
            sample_registry()
        try:
            service.analyze(project_id)
        except ValueError as exc:
            assert 'несовместим' in str(exc), str(exc)
        else:
            raise AssertionError('incompatible final sweep was not explicitly rejected')
        verified, pages, after = 0, 0, None
        next_index = slots-1
        digest = hashlib.sha256()
        finish_python_diagnostic()
        tracemalloc.start()
        while True:
            query = {'limit': ['64']}
            if after:
                query['after'] = [after]
            page = service.history(project_id, query)
            pages += 1
            for display in page['items']:
                while fixture(next_index, seed) is None:
                    next_index -= 1
                expected = to_primitive(fixture(next_index, seed, sid, stream, incompatible=next_index==last_source_index))
                original = service.history_original(project_id, display['id'])
                assert original == expected, f'raw source changed: {display["id"]}'
                assert display['levels'] == expected['levels'], '61-bin history lost raw levels'
                digest.update(json.dumps(original, sort_keys=True, separators=(',', ':')).encode())
                next_index -= 1
                verified += 1
            after = page['next_cursor']
            finish_python_diagnostic()
            if not after:
                break
        assert verified == raw_count and next_index == -1, 'history pagination skipped or duplicated a raw source'
        sources = 0
        event_count, event_digest = _event_signature(service, project_id)
        for event in _page_records(service, 'event', project_id):
            after = None
            observed = 0
            previous_source = ''
            while True:
                query = {'limit': ['64']}
                if after:
                    query['after'] = [after]
                page = service.event_sources(project_id, event['id'], query)
                for source in page['items']:
                    assert source['id'] > previous_source, 'event source pagination repeated or reordered an ID'
                    previous_source = source['id']
                    original = service.history_original(project_id, source['sweep_id'])
                    assert original['id'] == source['sweep_id']
                    observed += 1
                after = page['next_cursor']
                if not after:
                    break
            assert observed == event['source_count'], 'event source pages disagree with durable source count'
            sources += observed
        quality_records = 0
        for quality in _page_records(service, 'quality', project_id):
            assert quality['kind'] == 'data_gap' and quality['gap_seconds'] == 360., quality
            original = service.history_original(project_id, quality['sweep_id'])
            assert (parse_time(original['interval']['started_at'])-parse_time(quality['previous_started_at'])).total_seconds() == 360.
            quality_records += 1
        assert quality_records == len(range(125, last_source_index, 1440)), 'missing or duplicate gap quality record'
        sample_registry()
        disk = dict(database_bytes=path.stat().st_size,
            wal_bytes=Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0,
            logical_database_bytes=sessions.archive_capacity()['database_bytes'])
        report = dict(synthetic=True, hardware_validation='not_run', seed=seed, logical_slots=slots,
            cadence_seconds=60, logical_elapsed_seconds=slots*60, raw_sweeps=raw_count, raw_verified=verified,
            gap_slots=gaps, quality_records=quality_records, gap_policy='No measurement invented for five missing minute slots per day.',
            incompatible_sources_rejected=1, incompatible_policy='Final source archived, explicitly rejected; no silent baseline adaptation or continuation claim.',
            baseline_sweeps=20, processed_compatible_sweeps=processed,
            metadata_jobs_cancelled=metadata_jobs, events=event_count, event_sources=sources,
            history_pages=pages, raw_archive_sha256=digest.hexdigest(), event_id_sha256=event_digest,
            restart_no_duplicate_events=restarted, active_workers=workers.active_count,
            registry_maxima=maxima, bounded_working_registry=all(value <= 128 for value in maxima.values()),
            core_peak_memory=dict(python_sampled_tracemalloc_peak_bytes=python_diagnostic_peak,
                process_rss_peak_bytes=_process_peak_rss(),
                scope='RSS is the actual process-lifetime peak, including imports/native SQLite/numpy. Python diagnostic measures startup/first 128 slots and first verification page only; it is not a full-run Python peak.'),
            disk=disk, comparison=compare_detectors(seed=seed, slots=slots),
            wall_seconds=time.monotonic()-started)
        assert report['bounded_working_registry'], maxima
        return report
    finally:
        close_core()
        finish_python_diagnostic()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--slots', type=int, default=MAX_SLOTS)
    args = parser.parse_args(argv)
    if args.archive and args.report.resolve() == args.archive.resolve():
        parser.error('--report and --archive must be different paths')
    if args.archive:
        report = run_acceptance(args.archive, seed=args.seed, slots=args.slots)
    else:
        with tempfile.TemporaryDirectory(prefix='ori-synthetic-acceptance-') as directory:
            report = run_acceptance(Path(directory)/'archive.sqlite', seed=args.seed, slots=args.slots)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(report=str(args.report), raw_sweeps=report['raw_sweeps'], wall_seconds=report['wall_seconds'], hardware_validation='not_run')))


if __name__ == '__main__':
    main()
