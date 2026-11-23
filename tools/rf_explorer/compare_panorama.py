"""Check a complete pass API response against the independent export_survey CSV."""
import argparse
import csv
from datetime import datetime
import json
import math
from pathlib import Path


def check(condition, message):
    if not condition:
        raise ValueError(message)


def observations(row):
    frequency = int(row['frequency_hz'])
    for prefix in ('', 'overlap_'):
        if row[prefix + 'sweep_id']:
            yield dict(frequency_hz=frequency, level_dbm=float(row[prefix + 'level_dbm']),
                       sweep_id=row[prefix + 'sweep_id'], point_index=int(row[prefix + 'point_index']),
                       tile=int(row[prefix + 'tile']), started_at=row[prefix + 'started_at'],
                       ended_at=row[prefix + 'ended_at'])


def verify(csv_path, data):
    summary = data['summary']
    start, stop, step = (summary[k] for k in ('start_hz', 'stop_hz', 'step_hz'))
    count = summary['expected_points']
    columns = data['columns']
    check(step == 10_000 and stop == start + (count - 1) * step and 112 <= count <= 268_501,
          'unexpected survey lattice')
    check(0 < len(columns) <= min(count, 2048), 'unexpected display width')
    check(summary['complete'] is True and summary['unique_points'] == count, 'incomplete API pass')
    check(data['coverage'] == [dict(start_hz=start, stop_hz=stop)], 'incomplete API coverage')
    peaks = [None] * len(columns)
    first, last = None, None
    tiles = set()
    repeats = unique = 0
    with csv_path.open(encoding='utf-8', newline='') as source:
        for i, row in enumerate(csv.DictReader(source)):
            check(i < count and int(row['frequency_hz']) == start + i * step, 'CSV lattice or row count')
            check(row['pass_id'] == summary['pass_id'] and int(row['pass']) == summary['pass'], 'different pass')
            check(all(row[k] == str(summary[k]) for k in ('active_board', 'detector', 'rbw_hz', 'offset_db'))
                  and row['simulated'] == str(summary['simulated']).lower(), 'incompatible detector or readback')
            samples = list(observations(row))
            check(samples and all(math.isfinite(s['level_dbm']) for s in samples), 'invalid CSV level')
            latest = max(s['ended_at'] for s in samples)
            bucket = i * len(peaks) // count
            for sample in samples:
                tiles.add(sample['tile'])
                first = min(first or sample['started_at'], sample['started_at'])
                last = max(last or sample['ended_at'], sample['ended_at'])
                key = (-sample['level_dbm'], sample['started_at'], sample['tile'],
                       sample['point_index'], sample['sweep_id'])
                if peaks[bucket] is None or key < peaks[bucket][0]:
                    peaks[bucket] = key, dict(sample, last_measured_at=latest)
            unique += 1
            repeats += len(samples) - 1
    check(unique == count and len(tiles) == summary['received_tiles'] == summary['tiles'], 'missing CSV windows')
    check(unique + repeats == len(tiles) * 112, 'missing overlap observations')
    check(first == summary['started_at'] and last == summary['ended_at'], 'source interval mismatch')
    wall = (datetime.fromisoformat(last.replace('Z','+00:00')) -
            datetime.fromisoformat(first.replace('Z','+00:00'))).total_seconds()
    check(math.isclose(summary['duration_s'], wall, abs_tol=1e-6, rel_tol=0), 'incorrect pass duration')
    for index, (expected, actual) in enumerate(zip(peaks, columns)):
        check(expected and isinstance(actual, dict), f'missing display column {index}')
        for field, value in expected[1].items():
            check(actual.get(field) == value, f'column {index} differs on {field}')
    return dict(unique_frequencies=unique, repeat_observations=repeats, raw_windows=len(tiles),
                display_columns=len(columns), duration_s=wall, verified=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', required=True, type=Path)
    parser.add_argument('--panorama', required=True, type=Path, help='Saved API v1 envelope')
    args = parser.parse_args()
    try:
        with args.panorama.open(encoding='utf-8') as file:
            data = json.load(file)['data']
        print(json.dumps(verify(args.csv, data), sort_keys=True))
    except (KeyError, ValueError, TypeError, OSError) as error:
        parser.exit(1, f'Panorama verification failed: {error}\n')


if __name__ == '__main__':
    main()
