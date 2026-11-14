"""Generate deterministic evidence from the actual Python detector; no hardware.

Run from any cwd with the repository's supported Python interpreter. The fixture
contains authored synthetic measurements, never private/captured measurements.
"""
from __future__ import annotations
import copy
import hashlib
import json
import sys
import math
import random
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from radioinspector.contracts import Sweep, TimeInterval, to_primitive
from radioinspector.monitoring.detector import Detector, VERSION, build_baseline, fingerprint, stable

BASE = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
PARAMETERS = dict(margin_db=6., impulse_db=12., hysteresis_db=3., confirmations=3, window_sweeps=5)

def sweep(index, levels=None, *, size=9, seconds=None, duration=1., settings=None, micros=0):
    start = BASE + timedelta(seconds=4*index if seconds is None else seconds, microseconds=micros)
    return Sweep(session_id='synthetic-session', stream_id='analyzer/α', id=f'sweep-{index}',
        interval=TimeInterval(start, start+timedelta(seconds=duration)),
        frequencies_hz=tuple(100_000_000+10_000*i for i in range(size)),
        levels=tuple([-100.]*size if levels is None else levels), level_unit='dBm',
        settings=copy.deepcopy(settings) if settings is not None else dict(adapter='synthetic', rbw_hz=10000, sequence=index))

def checkpoint(state):
    return json.dumps(state, sort_keys=True, allow_nan=False)

def replay(name, baseline, items, initial=None):
    detector=Detector(copy.deepcopy(baseline), copy.deepcopy(PARAMETERS), copy.deepcopy(initial))
    result=dict(name=name, baseline=copy.deepcopy(baseline), parameters=copy.deepcopy(PARAMETERS), initial_state=copy.deepcopy(initial), expected_initial_checkpoint_json=checkpoint(detector.state), steps=[])
    for item in items:
        value, allowed = item if isinstance(item,tuple) else (item, [])
        entry=dict(sweep=to_primitive(value), allowed=copy.deepcopy(allowed))
        before=copy.deepcopy(detector.state)
        try:
            regions, excess, thresholds=detector.step(value,allowed)
            entry['expected']=dict(regions=copy.deepcopy(regions),excess_db=excess,thresholds_db=thresholds,state=copy.deepcopy(detector.state),checkpoint_json=checkpoint(detector.state))
        except ValueError:
            assert detector.state==before, 'invalid fingerprint must not mutate checkpoint'
            entry['error']='incompatible'
            entry['unchanged_state']=before
        result['steps'].append(entry)
    return result

def fixture_json(value, depth=0):
    """Keep scalar vectors compact while leaving replay records reviewable."""
    if isinstance(value, dict):
        prefix=' '*(depth+2)
        return '{\n'+',\n'.join(prefix+json.dumps(key,ensure_ascii=True)+': '+fixture_json(item,depth+2) for key,item in value.items())+'\n'+' '*depth+'}' if value else '{}'
    if isinstance(value, list) and any(isinstance(item,(dict,list)) for item in value):
        return '[\n'+',\n'.join(' '*(depth+2)+fixture_json(item,depth+2) for item in value)+'\n'+' '*depth+']'
    return json.dumps(value,ensure_ascii=True,allow_nan=False)

def main():
    quiet=[sweep(i) for i in range(20)]
    dense=[sweep(i,size=401) for i in range(20)]
    variable=[sweep(i,[-100.+((i+j)%5-2)*(2. if j%2 else .5) for j in range(9)],micros=123456) for i in range(20)]
    zeros=[sweep(i,[-0.0,0.0,-0.0,0.0],size=4) for i in range(20)]
    wide=[sweep(i,[-100.+((i+j)%5-2)*5 for j in range(9)]) for i in range(20)]
    odd=[sweep(i,[-100.+((i+j)%5-2)*.5 for j in range(9)]) for i in range(21)]
    sources=[('quiet',quiet),('variable',variable),('wide_mad',wide),('odd_median',odd),('dense',dense),('negative_zero',zeros)]
    baselines={name:build_baseline(items) for name,items in sources}
    baseline_cases=[dict(name=name,sweeps=[to_primitive(x) for x in items],expected=baselines[name]) for name,items in sources]
    b=baselines['quiet']; replays=[]
    carrier=[-100.]*9;carrier[4]=-92.
    replays.append(replay('carrier_three_of_five_and_no_carrier_learning',b,[sweep(i,carrier) for i in range(20,32)]))
    replays.append(replay('nonconsecutive_confirmations_require_current_above',b,[sweep(20+i,carrier if i in (0,2,4) else None) for i in range(9)]))
    replays.append(replay('impulses_below_robust_threshold_never_learned',baselines['wide_mad'],[sweep(20,[-76.]*9),sweep(21,[-76.]*9)]))
    quiet_items=[sweep(20,[-98.]*9,duration=.000001),sweep(21,[-102.]*9,duration=0.)]
    quiet_items += [sweep(i,[-98.+.125*(j%3) for j in range(9)],duration=1.) for i in range(22,30)]
    quiet_items += [sweep(30,[-104.]*9,duration=60.)]
    replays.append(replay('quiet_offsets_duration_clamp_and_bounded_learning',b,quiet_items))
    impulse=[-100.]*9;impulse[2]=-85.
    rule=dict(id='legit/雪',start_hz=100020000,end_hz=100020000,watch_changes=False)
    replays.append(replay('impulse_allowed_and_watched',b,[(sweep(20,impulse),[rule]),(sweep(21,impulse),[dict(rule,watch_changes=True)]),sweep(22,impulse)]))
    watched=dict(id='all',start_hz=100000000,end_hz=100080000,watch_changes=True)
    replays.append(replay('broadband_before_confirmation_and_authorized_order',b,[sweep(20,[-92.]*9),(sweep(21,[-85.]*9),[watched]),(sweep(22,[-85.]*9),[dict(watched,watch_changes=False),watched])]))
    suppressed=dict(id='local',start_hz=100000000,end_hz=100040000,watch_changes=False)
    replays.append(replay('suppressed_bins_before_broad_and_known_bins_not_learned',b,[(sweep(20,[-85.]*5+[-100.]*4),[suppressed]),(sweep(21,[-85.]*5+[-100.]*3+[-85.]),[suppressed]),(sweep(22,[-98.]*9),[suppressed])]))
    large=[-100.]*401
    for i in range(0,396,12):large[i]=-85.
    large[6]=-20.
    deny=dict(id='strong-local',start_hz=100060000,end_hz=100060000,watch_changes=False)
    replays.append(replay('dense_change_first_tied_peak_excludes_suppression',baselines['dense'],[(sweep(20,large,size=401),[deny])]))
    large=[-100.]*401
    for i in range(0,200,2):large[i]=-80.
    large[-1]=-60.
    replays.append(replay('dense_tail_peak',baselines['dense'],[sweep(20,large,size=401)]))
    gap_items=[sweep(20,carrier),sweep(21,carrier),sweep(40,carrier),sweep(41,carrier),sweep(42,carrier),sweep(19,carrier),sweep(20,carrier)]
    replays.append(replay('forward_backward_gaps_reset_window',b,gap_items))
    replays.append(replay('gap_boundary_exact_three_periods',b,[sweep(20,carrier),sweep(23,carrier),sweep(26,carrier,micros=1)]))
    legacy=copy.deepcopy(b);legacy.pop('sweep_period_seconds')
    replays.append(replay('missing_legacy_period_uses_span_mean',legacy,[sweep(i,carrier) for i in (20,21,22)]))
    unknown=dict(b,sweep_period_seconds=None)
    replays.append(replay('explicit_unknown_period_only_resets_backward',unknown,[sweep(20,carrier),sweep(2000,carrier),sweep(21,carrier)]))
    replays.append(replay('zero_period_disables_forward_gap',dict(b,sweep_period_seconds=0.),[sweep(20,carrier),sweep(2000,carrier)]))
    initial=Detector(b,PARAMETERS);initial.step(sweep(20,carrier));initial.step(sweep(21,carrier))
    saved=copy.deepcopy(initial.state);saved['future']={'雪':[-0.0,18446744073709551617], '$serde_json::private::Number':'literal'}
    saved['reserved_only']={'$serde_json::private::Number':'literal'}
    replays.append(replay('restart_preserves_checkpoint_extensions_and_confirms',b,[sweep(22,carrier),sweep(23)],saved))
    changed=sweep(20);changed.settings['rbw_hz']=30000
    replays.append(replay('incompatible_preserves_checkpoint',b,[changed,sweep(20,carrier)]))
    replays.append(replay('explicit_null_checkpoint_fields',b,[sweep(20),sweep(21,carrier)],dict(window=[],offsets=[0.]*9,active=None,last_started_at=None,quality_issue=None,future={'null':None})))
    replays.append(replay('mad_thresholds_and_quiet_model',baselines['variable'],[sweep(20,[-90.]*9,micros=123456),sweep(21,[-98.]*9,micros=123456)]))
    replays.append(replay('negative_zero_levels_and_state',baselines['negative_zero'],[sweep(20,[-0.0,0.0,0.0,-0.0],size=4)]))
    numeric=[-0.0,0.0,1.0,1e-4,1e-5,1e15,1e16,1e20,1.2345678901234568e21,1.0000000000000002,2.2250738585072014e-308,5e-324,1.7976931348623157e308,9007199254740993,18446744073709551617]
    settings=dict(adapter='synthetic',nested={'sequence':333,'stable':'é雪🔌\x7f\b\f\r\n\t'},numbers=numeric,items=[{'revision':3,'value':-0.0}],sequence=1,connection_id='volatile',segment_id='volatile',revision=2,tune_sequence=4)
    fingerprint_cases=[]
    for name,setting in [('unicode_numbers_and_nested_volatile',settings),('plain',quiet[0].settings),('stable_literal_reserved_key',{'$serde_json::private::Number':'literal','rbw_hz':10000}),('lexical_positive_zero',{'value':0.0}),('lexical_negative_zero',{'value':-0.0})]:
        item=sweep(0,settings=setting)
        document=dict(grid=item.frequencies_hz,settings=stable(item.settings))
        fingerprint_cases.append(dict(name=name,sweep=to_primitive(item),expected_json=json.dumps(document,sort_keys=True,allow_nan=False),expected=fingerprint(item)))
    random_source=random.Random(0x5246494E53504543)
    floats=[]
    while len(floats)<2048:
        value=struct.unpack('>d',random_source.getrandbits(64).to_bytes(8,'big'))[0]
        if math.isfinite(value):floats.append(value)
    item=sweep(0,settings={'floats':floats,'reserved':{'$serde_json::private::Number':'literal'}})
    fingerprint_cases.append(dict(name='seeded_finite_ieee754_repr',sweep=to_primitive(item),expected_json=json.dumps(dict(grid=item.frequencies_hz,settings=stable(item.settings)),sort_keys=True,allow_nan=False),expected=fingerprint(item)))
    for token in ['-0','1e0','1.00E-005','1E+015','1e-0']:
        raw=json.dumps(to_primitive(sweep(0,settings={'value':'TOKEN'})),ensure_ascii=True).replace('"TOKEN"',token)
        item=Sweep.from_dict(json.loads(raw))
        fingerprint_cases.append(dict(name='raw_numeric_'+token,raw_sweep_json=raw,sweep=to_primitive(item),expected_json=json.dumps(dict(grid=item.frequencies_hz,settings=stable(item.settings)),sort_keys=True,allow_nan=False),expected=fingerprint(item)))
    output=dict(schema_version=1,source='radioinspector.monitoring.detector robust-city-v1 at 96696a0',evidence='authored_synthetic_replay',detector_version=VERSION,baseline_cases=baseline_cases,fingerprint_cases=fingerprint_cases,replays=replays)
    path=ROOT/'contracts/fixtures/monitoring-detector-v1.json'
    path.write_text(fixture_json(output)+'\n',encoding='utf-8')
    print(path, 'sha256',hashlib.sha256(path.read_bytes()).hexdigest(),'bytes',path.stat().st_size)

if __name__=='__main__':main()
