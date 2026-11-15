"""One bounded AR-DV1 scan through the owning core; measurements stay in its archive.

Run: python -m tools.ardv1_scan --port /dev/cu.usbserial-... --start-hz ... --stop-hz ...
"""
from __future__ import annotations

import argparse
import json
import time

from radioinspector.windows.client import PlatformClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='http://127.0.0.1:8770')
    parser.add_argument('--port', required=True)
    parser.add_argument('--start-hz', type=int, required=True)
    parser.add_argument('--stop-hz', type=int, required=True)
    parser.add_argument('--step-hz', type=int, default=25000)
    parser.add_argument('--settle-s', type=float, default=0.015)
    parser.add_argument('--rf-only', action='store_true', help='Перестраивать каждую точку через RF')
    args = parser.parse_args()
    # Validate all settings locally before creating a session or opening hardware.
    from radioinspector.contracts import MonitoringSession
    from radioinspector.serial_instruments import ARDV1SerialAdapter
    payload = dict(name='AR-DV1 fast level scan', streams=[dict(
        device='AOR AR-DV1', role='receiver', level_unit='s_meter_count',
        port=args.port, capabilities=['receiver'], settings=dict(
            adapter='ardv1-serial', receiver_profile='ardv1-monitor-v1',
            mode='FM', period_s=0, max_consecutive_errors=1,
            fast_scan=dict(start_hz=args.start_hz, stop_hz=args.stop_hz,
                           step_hz=args.step_hz, settle_s=args.settle_s,
                           strategy='rf' if args.rf_only else 'auto')))])
    # The validation transport is never used for I/O.
    validated = MonitoringSession.from_dict(payload)
    ARDV1SerialAdapter(validated.streams[0], transport=object())
    client = PlatformClient(args.server, timeout=15)
    capabilities = client._request('GET', '/capabilities')
    if capabilities.get('features', {}).get('ardv1_sampled_scope') is not True:
        parser.error('Подключённое ядро не поддерживает программный обзор AR-DV1')
    session = client.create_session(payload)
    sid, stream = session['id'], session['streams'][0]['id']
    print(json.dumps(dict(session_id=sid, stream_id=stream)), flush=True)
    began = time.monotonic()
    try:
        client.start_session(sid)
        client.start_stream(sid, stream)
        while True:
            worker = client.list_workers(sid)[0]
            current = worker.get('current', {})
            if worker.get('last_error'):
                raise RuntimeError(worker['last_error'])
            total = current.get('scan_total_points')
            if not total:
                raise RuntimeError('Ядро не поддерживает fast_scan; перезапустите обновлённое ядро')
            completed = worker['measurements']
            strategy = current.get('scan_strategy', 'RF')
            print(f'{completed}/{total}, {completed / max(time.monotonic() - began, .001):.2f} points/s [{strategy}]', flush=True)
            if not worker['alive']:
                if completed == total:
                    return 0
                raise RuntimeError('Поток остановился до завершения прохода')
            time.sleep(1)
    except KeyboardInterrupt:
        print('Остановлено; уже записанные измерения сохранены в архиве.', flush=True)
        return 130
    finally:
        if any(worker['alive'] for worker in client.list_workers(sid)):
            client._post(f'/sessions/{sid}/streams/{stream}/stop', {'mode': 'graceful'})
            deadline = time.monotonic() + 8
            while any(worker['alive'] for worker in client.list_workers(sid)):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Ядро не подтвердило освобождение порта AR-DV1')
                time.sleep(.05)
        client.stop_session(sid)


if __name__ == '__main__':
    raise SystemExit(main())
