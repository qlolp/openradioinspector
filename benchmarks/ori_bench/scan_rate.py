"""Compare a predeclared direct USB run with committed gridded sweeps in a raw export."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(text):
    require(isinstance(text, str), 'missing measurement timestamp')
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    require(value.utcoffset() is not None, 'measurement timestamp lacks offset')
    return value


def compare(reference, export, stream):
    band = reference['band']
    passes, size = reference['passes'], band['points']
    require(reference['format'] == 'ori-direct-scan-v1' and type(passes) is int and 1 <= passes <= 20,
            'invalid direct reference')
    require(type(size) is int and 2 <= size <= 129_991 and band['step_hz'] == 10000 and
            100000 <= band['start_hz'] < band['stop_hz'] <= 1300000000 and
            band['stop_hz'] - band['start_hz'] == (size - 1) * 10000 and
            band['rbw_hz'] in (10000, 30000), 'invalid reference grid')
    windows = (size + 289) // 290
    target = windows * passes
    direct_time = reference['elapsed_seconds']
    require(reference['windows'] == target and reference['points'] == size * passes and
            math.isfinite(direct_time) and direct_time > 0 and reference['utc_elapsed_us'] > 0 and
            isinstance(reference['firmware'], str) and reference['firmware'].strip(), 'incomplete direct pass')
    require(abs(reference['utc_elapsed_us'] / 1e6 - direct_time) <= max(.25, direct_time * .01),
            'reference clocks disagree')
    start = anchor = previous_end = plan = sequence = session = None
    count = in_window = 0
    seen = set()
    # The N+1 opening window accounts for idle time and commit of the last
    # completed sweep; the prospective sample always starts at the first row.
    with export.open('rb') as records:
        for index, raw in enumerate(iter(lambda: records.readline(16 * 1024 * 1024 + 1), b'')):
            require(len(raw) <= 16 * 1024 * 1024, 'oversize export record')
            record = json.loads(raw)
            if index == 0:
                require(record.get('format') == 'openradioinspector-archive-ndjson' and
                        record.get('format_version') == 1, 'expected canonical raw export')
                session = record['session_id']
                continue
            if record['kind'] == 'observation':
                continue
            require(record['kind'] == 'sweep', 'unknown export record')
            sweep = record['measurement']
            if sweep['stream_id'] != stream:
                continue
            if count > target:
                count += 1
                continue
            data = sweep['settings']
            require(data['adapter'] == 'tinysa-serial' and data['simulated'] is False and
                    data['device_identity']['firmware'] == reference['firmware'] and
                    data['rbw_hz'] == band['rbw_hz'] and data['scan_serial_baud'] == reference['baud'] and
                    data['scan_band_id'] == band['id'] and data['scan_step_hz'] == 10000 and
                    data['scan_window_count'] == windows, 'different instrument, settings, band or source')
            key = data['scan_plan_id'], data['scan_plan_revision']
            require(all(key) and (plan is None or plan == key), 'plan revision changed')
            plan = key
            position = count % windows
            require(data['scan_window_index'] == position and sweep['session_id'] == session and
                    sweep['id'] not in seen and (sequence is None or data['sequence'] == sequence + 1),
                    'missing, reordered or repeated sweep')
            seen.add(sweep['id'])
            sequence = data['sequence']
            lo = position * 290 - int(size % 290 == 1 and position == windows - 1)
            num = min(290, size - lo) - int(size % 290 == 1 and position == windows - 2)
            fs = sweep['frequencies_hz']
            require(len(fs) == num == len(sweep['levels']) and sweep['level_unit'] == 'dBm' and
                    all(type(f) is int and f == band['start_hz'] + (lo + i) * 10000 for i, f in enumerate(fs)),
                    'incomplete or incompatible frequency grid')
            began, ended = (timestamp(sweep['interval'][name]) for name in ('started_at', 'ended_at'))
            require(ended >= began and (previous_end is None or began >= previous_end), 'invalid instrument intervals')
            previous_end = ended
            if count == 0:
                start = began
            if count == target:
                anchor = began
            else:
                in_window += num
            count += 1
    require(anchor is not None and in_window == reference['points'], 'missing complete passes or boundary sweep')
    duration = (anchor - start).total_seconds()
    require(duration > 0, 'invalid managed elapsed time')
    ratio = direct_time / duration
    return dict(passes=passes, points=in_window, windows=target, extra_sweeps_after_boundary=count - target - 1,
                direct_points_per_second=in_window / direct_time, committed_points_per_second=in_window / duration,
                ratio=ratio, minimum_ratio=.90, passed=ratio >= .90)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--stream', required=True)
    args = parser.parse_args(argv)
    try:
        require(args.reference.stat().st_size <= 16384, 'oversize reference')
        result = compare(json.loads(args.reference.read_bytes()), args.export, args.stream)
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as error:
        print(json.dumps(dict(passed=False, error=str(error))))
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
