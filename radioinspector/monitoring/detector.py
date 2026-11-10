"""Deterministic sweep-only change detector. Not an IQ/protocol classifier."""
import copy
import hashlib
import json
import math
import statistics
from collections import deque
from ..contracts import parse_time, format_time

VERSION = 'robust-city-v1'
VOLATILE = {'sequence', 'connection_id', 'segment_id', 'revision', 'tune_sequence'}


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list): return [stable(x) for x in value]
    return value


def fingerprint(sweep):
    if sweep.level_unit != 'dBm' or not sweep.settings:
        raise ValueError('Эталон требует исходные dBm и известные настройки')
    if any(b <= a for a, b in zip(sweep.frequencies_hz, sweep.frequencies_hz[1:])):
        raise ValueError('Для обнаружения нужна строго возрастающая сетка')
    if sweep.settings.get('overload') is True or sweep.settings.get('incomplete') is True:
        raise ValueError('Неполное/перегруженное измерение не используется для обнаружения')
    document = dict(grid=sweep.frequencies_hz, settings=stable(sweep.settings))
    return hashlib.sha256(json.dumps(document, sort_keys=True, allow_nan=False).encode()).hexdigest()


def build_baseline(sweeps):
    if len(sweeps) < 20:
        raise ValueError('Для эталона нужно не менее 20 полных свипов')
    start = min(s.interval.started_at for s in sweeps)
    end = max(s.interval.ended_at for s in sweeps)
    if (end-start).total_seconds() < 60:
        raise ValueError('Для эталона нужен интервал не менее 60 секунд')
    expected = fingerprint(sweeps[0])
    if any(fingerprint(s) != expected for s in sweeps):
        raise ValueError('Сетка или настройки источников эталона несовместимы')
    p50, p95, mad = [], [], []
    for column in zip(*(s.levels for s in sweeps)):
        ordered = sorted(column); median = statistics.median(ordered)
        p50.append(median); p95.append(ordered[math.ceil(.95*len(ordered))-1])
        mad.append(statistics.median(abs(x-median) for x in ordered))
    return dict(fingerprint=expected, frequencies_hz=list(sweeps[0].frequencies_hz),
                p50_dbm=p50, p95_dbm=p95, mad_db=mad,
                source_refs=[dict(session_id=s.session_id, stream_id=s.stream_id, sweep_id=s.id) for s in sweeps],
                started_at=start.isoformat(), ended_at=end.isoformat(), detector_version=VERSION,
                sweep_period_seconds=statistics.median([(b.interval.started_at-a.interval.started_at).total_seconds()
                    for a,b in zip(sweeps,sweeps[1:])]) if len(sweeps)>1 else 1.)


class Detector:
    def __init__(self, baseline, parameters, state=None):
        self.baseline, self.parameters = baseline, parameters
        size = len(baseline['frequencies_hz'])
        self.state = copy.deepcopy(state) if state else dict(window=[], offsets=[0.]*size, active=[])

    def step(self, sweep, allowed=()):
        if fingerprint(sweep) != self.baseline['fingerprint']:
            raise ValueError('Эталон несовместим с текущей сеткой/настройками')
        b, p, state = self.baseline, self.parameters, self.state
        state.pop('quality_issue',None)
        previous=state.get('last_started_at')
        if previous:
            gap=(sweep.interval.started_at-parse_time(previous)).total_seconds()
            period = b.get('sweep_period_seconds')
            # Unknown legacy cadence cannot establish a forward data gap.
            if gap < 0 or (period and gap > max(1., 3 * period)):
                state['window']=[];state['recent_sources']=[]
                state['quality_issue']=dict(kind='data_gap',gap_seconds=gap,previous_started_at=previous)
        state['last_started_at']=format_time(sweep.interval.started_at)
        excess = [level-(reference+offset) for level, reference, offset in
                  zip(sweep.levels, b['p95_dbm'], state['offsets'])]
        thresholds = [max(p['margin_db'], 3*1.4826*x) for x in b['mad_db']]
        above = [x >= threshold for x, threshold in zip(excess, thresholds)]
        impulses = [x >= p['impulse_db'] for x in excess]
        window = (state['window'] + [above])[-p['window_sweeps']:]
        state['window'] = window
        state['recent_sources'] = (state.get('recent_sources', []) + [dict(
            session_id=sweep.session_id, stream_id=sweep.stream_id, sweep_id=sweep.id,
            started_at=sweep.interval.started_at.isoformat())])[-p['window_sweeps']:]
        active = [(above[i] and sum(row[i] for row in window) >= p['confirmations']) or impulses[i]
                  for i in range(len(above))]
        # A broad scene change is one event, never hundreds of isolated peaks.
        broad = sum(above) >= max(2, math.ceil(.5*len(above)))
        regions = []
        start = None
        for i, flag in enumerate(active + [False]):
            if flag and start is None: start = i
            if not flag and start is not None:
                end = i-1
                peak = max(range(start, i), key=lambda j: excess[j])
                regions.append(dict(first_bin=start, last_bin=end,
                    start_frequency_hz=b['frequencies_hz'][start],
                    end_frequency_hz=b['frequencies_hz'][end],
                    center_frequency_hz=b['frequencies_hz'][peak],
                    peak_level=sweep.levels[peak], peak_excess_db=excess[peak],
                    kind='impulse' if any(impulses[start:i]) else 'persistent'))
                start = None
        if broad:
            peak = max(range(len(excess)), key=excess.__getitem__)
            regions = [dict(first_bin=0, last_bin=len(excess)-1,
                start_frequency_hz=b['frequencies_hz'][0], end_frequency_hz=b['frequencies_hz'][-1],
                center_frequency_hz=b['frequencies_hz'][peak], peak_level=sweep.levels[peak],
                peak_excess_db=excess[peak], kind='broadband_change')]
        result = []
        for region in regions:
            rule = next((a for a in allowed if a['start_hz'] <= region['start_frequency_hz']
                         and a['end_hz'] >= region['end_frequency_hz']), None)
            if rule and not rule.get('watch_changes', False): continue
            if rule: region.update(kind='authorized_changed', allowed_id=rule['id'])
            result.append(region)
        if not broad and len(result) > 32:
            # Suppressed components must not determine count, span or peak.
            first, last = result[0]['first_bin'], result[-1]['last_bin']
            peak = max(result, key=lambda r: r['peak_excess_db'])
            result = [dict(first_bin=first, last_bin=last,
                start_frequency_hz=b['frequencies_hz'][first],
                end_frequency_hz=b['frequencies_hz'][last],
                center_frequency_hz=peak['center_frequency_hz'], peak_level=peak['peak_level'],
                peak_excess_db=peak['peak_excess_db'], kind='dense_change',
                component_regions_count=len(result))]
        # Detect first, learn second. Changed/impulsive bins and broad changes
        # never enter the quiet model. A bounded offset cannot absorb a carrier.
        if not broad:
            duration = max(.01, (sweep.interval.ended_at-sweep.interval.started_at).total_seconds())
            alpha = -math.expm1(-min(duration, 30)/30)
            for i, changed in enumerate(above):
                known = any(a['start_hz'] <= b['frequencies_hz'][i] <= a['end_hz'] for a in allowed)
                if not changed and not impulses[i] and not known:
                    target = max(-3., min(3., sweep.levels[i]-b['p50_dbm'][i]))
                    state['offsets'][i] += alpha*(target-state['offsets'][i])
        return result, excess, thresholds
