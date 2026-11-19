"""Compare a predeclared direct USB run with committed gridded sweeps in a raw export."""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re


MODEL = 'tinySA Ultra ZS405'
FIRMWARE = 'tinySA4_v1.4-235-gea71dfd'
WIDE_FIRMWARE = [FIRMWARE, 'HW Version:V0.4.5.1']
MINIMUM_RATIO = .90


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(text):
    require(isinstance(text, str), 'missing measurement timestamp')
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    require(value.utcoffset() is not None, 'measurement timestamp lacks offset')
    return value


def receive_path(evidence, ultra, rbw):
    require(isinstance(evidence, dict), 'missing acknowledged receive path')
    digest = evidence.get('device_id_sha256')
    gain = evidence.get('attenuation_db')
    require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) and
            evidence.get('source') == 'command_acknowledged' and
            evidence.get('device_mode') == 'input' and evidence.get('calculation') == 'off' and
            type(evidence.get('lna')) is bool and
            (gain == 'auto' or (type(gain) is int and 0 <= gain <= 31)) and
            evidence.get('ultra') is ultra and type(evidence.get('rbw_hz')) is int and
            evidence['rbw_hz'] == rbw, 'invalid hardware identity or acknowledged receive path')
    return digest


def grid_windows(band):
    size = band['points']
    windows = (size + 289) // 290
    for position in range(windows):
        # Move one point to a singleton tail: the text scan accepts at least two.
        first = position * 290 - int(size % 290 == 1 and position == windows - 1)
        count = min(290, size - first) - int(size % 290 == 1 and position == windows - 2)
        start = band['start_hz'] + first * 10000
        ultra = start + (count - 1) * 10000 > 800_000_000
        yield start, count, ultra


def reference_layout(reference):
    require(isinstance(reference, dict) and reference.get('format') == 'ori-direct-scan-v2',
            'a v2 direct reference with same-unit receive evidence is required')
    band = reference['band']
    passes, size = reference['passes'], band['points']
    require(type(passes) is int and 1 <= passes <= 20 and type(reference['baud']) is int and
            9600 <= reference['baud'] <= 115200, 'invalid direct settings')
    require(isinstance(band.get('id'), str) and re.fullmatch('[A-Za-z0-9_-]{1,64}', band['id']) and
            all(type(band[key]) is int for key in ('start_hz', 'stop_hz', 'step_hz', 'rbw_hz', 'points')) and
            2 <= size <= 129_991 and band['step_hz'] == 10000 and
            100000 <= band['start_hz'] < band['stop_hz'] <= 1300000000 and
            band['stop_hz'] - band['start_hz'] == (size - 1) * 10000 and
            band['rbw_hz'] in (10000, 30000), 'invalid reference grid')
    layout = list(grid_windows(band))
    windows = len(layout)
    target = windows * passes
    firmware = reference['firmware']
    require(isinstance(reference['model'], str) and reference['model'].casefold() == MODEL.casefold() and
            isinstance(firmware, str) and (firmware.splitlines() == WIDE_FIRMWARE or
            (band['stop_hz'] <= 800_000_000 and firmware.splitlines() == [FIRMWARE])),
            'unsupported direct instrument profile')
    paths = reference['receive_paths']
    used = {('ultra' if ultra else 'normal') for _, _, ultra in layout}
    require(isinstance(paths, dict) and set(paths) == used, 'missing or unexpected direct receive path')
    units = {receive_path(evidence, name == 'ultra', band['rbw_hz']) for name, evidence in paths.items()}
    require(len(units) == 1, 'different hardware units in direct reference')
    direct_time = reference['elapsed_seconds']
    require(type(reference['windows']) is int and reference['windows'] == target and
            type(reference['points']) is int and reference['points'] == size * passes and
            type(direct_time) in (int, float) and math.isfinite(direct_time) and direct_time > 0 and
            type(reference['utc_elapsed_us']) is int and reference['utc_elapsed_us'] > 0, 'incomplete direct pass')
    require(abs(reference['utc_elapsed_us'] / 1e6 - direct_time) <= max(.001, direct_time * .01),
            'reference clocks disagree')
    return layout


def validate_window(sweep, reference, layout, position):
    first, count, ultra = layout[position]
    band = reference['band']
    data = sweep['settings']
    require(isinstance(data, dict), 'missing serial settings')
    evidence = data.get('scan_rx_evidence')
    receive_path(evidence, ultra, band['rbw_hz'])
    info = data['device_identity']['info']
    require(isinstance(info, str) and
            any(line.strip().casefold() == MODEL.casefold() for line in info.splitlines()) and
            data['device_identity']['firmware'] == reference['firmware'] and
            evidence == reference['receive_paths']['ultra' if ultra else 'normal'] and
            data['scan_ultra_input'] is ultra and data['scan_ultra_source'] == 'command_acknowledged',
            'different hardware unit or acknowledged receive path')
    require(all(type(data[key]) is int for key in
                ('rbw_hz', 'scan_serial_baud', 'scan_step_hz', 'scan_window_count', 'scan_window_index')) and
            data['adapter'] == 'tinysa-serial' and data['simulated'] is False and
            data.get('replay') is not True and data['rbw_hz'] == band['rbw_hz'] and
            data['scan_serial_baud'] == reference['baud'] and data['scan_band_id'] == band['id'] and
            data['scan_step_hz'] == 10000 and data['scan_window_count'] == len(layout) and
            data['scan_window_index'] == position,
            'different acquisition settings, band or window')
    frequencies, levels = sweep['frequencies_hz'], sweep['levels']
    require(isinstance(frequencies, list) and len(frequencies) == count and
            all(type(f) is int and f == first + i * 10000 for i, f in enumerate(frequencies)),
            'incomplete or incompatible frequency grid')
    require(isinstance(levels, list) and len(levels) == count and sweep['level_unit'] == 'dBm' and
            all(type(v) in (int, float) and math.isfinite(v) for v in levels),
            'missing or invalid raw dBm level')
    return len(levels)


def compare(reference, export, stream):
    layout = reference_layout(reference)
    target = reference['windows']
    start = anchor = previous_end = plan = session = None
    count = in_window = boundary_points = extra_points = 0
    seen = set()
    # The N+1 opening window accounts for idle time and commit of the last
    # completed sweep; the prospective sample always starts at the first row.
    with export.open('rb') as records:
        for index, raw in enumerate(iter(lambda: records.readline(16 * 1024 * 1024 + 1), b'')):
            require(len(raw) <= 16 * 1024 * 1024, 'oversize export record')
            record = json.loads(raw)
            require(isinstance(record, dict), 'invalid export record')
            if index == 0:
                require(record.get('format') == 'openradioinspector-archive-ndjson' and
                        type(record.get('format_version')) is int and record['format_version'] == 1 and
                        isinstance(record.get('session_id'), str) and
                        record['session_id'], 'expected canonical raw export')
                session = record['session_id']
                continue
            if record['kind'] == 'observation':
                continue
            require(record['kind'] == 'sweep', 'unknown export record')
            sweep = record['measurement']
            if sweep['stream_id'] != stream:
                continue
            num = validate_window(sweep, reference, layout, count % len(layout))
            data = sweep['settings']
            key = data['scan_plan_id'], data['scan_plan_revision']
            require(isinstance(key[0], str) and key[0] and type(key[1]) is int and key[1] > 0 and
                    (plan is None or plan == key), 'missing or changed plan revision')
            plan = key
            require(sweep['session_id'] == session and isinstance(sweep['id'], str) and sweep['id'] and
                    sweep['id'] not in seen and type(data['sequence']) is int and data['sequence'] == count + 1,
                    'missing, reordered or repeated sweep')
            # Only the fixed sample and its boundary participate in timing. Bound the
            # ID set even if an operator stopped exporting long after the boundary.
            if count <= target:
                seen.add(sweep['id'])
            began, ended = (timestamp(sweep['interval'][name]) for name in ('started_at', 'ended_at'))
            require(ended >= began and (previous_end is None or began >= previous_end), 'invalid instrument intervals')
            previous_end = ended
            if count == 0:
                start = began
            if count == target:
                anchor = began
                boundary_points = num
            elif count < target:
                in_window += num
            else:
                extra_points += num
            count += 1
    require(anchor is not None and in_window == reference['points'], 'missing complete passes or boundary sweep')
    duration = (anchor - start).total_seconds()
    require(duration > 0, 'invalid managed elapsed time')
    direct_time = reference['elapsed_seconds']
    ratio = direct_time / duration
    return dict(passes=reference['passes'], points=in_window, windows=target, boundary_points=boundary_points,
                extra_sweeps_after_boundary=count - target - 1, extra_points_after_boundary=extra_points,
                exported_stream_points=in_window + boundary_points + extra_points,
                direct_elapsed_seconds=direct_time, managed_elapsed_seconds=duration,
                direct_points_per_second=in_window / direct_time, committed_points_per_second=in_window / duration,
                ratio=ratio, minimum_ratio=MINIMUM_RATIO, passed=ratio >= MINIMUM_RATIO)


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
