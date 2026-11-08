"""Synthetic-only sweep-context HTTP fixture; never opens a user archive.

Run: python -m tests.sweep_context_smoke_server --port 56039
Stop with Ctrl-C. A complete logical SQLite dump must remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=56039)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='openradio-sweep-context-') as workspace:
        path = Path(workspace) / 'synthetic.sqlite'
        service = SessionService(store=SQLiteSessionStore(str(path)))
        base = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
        for suffix in ('one', 'two'):
            session = service.create_session({
                'id': f'context-{suffix}', 'name': f'Synthetic context {suffix}',
                'streams': [
                    {'id': f'spectrum-{suffix}', 'device': 'Synthetic tinySA',
                     'role': 'spectrum_analyzer', 'level_unit': 'dBm',
                     'capabilities': ['sweep', 'waterfall']},
                    {'id': f'receiver-{suffix}', 'device': 'Synthetic AR-DV1',
                     'role': 'receiver', 'level_unit': 's_meter_count'},
                ],
            })
            service.start_session(session.id, at=base)
            for number in (7, 2, 5, 1, 6, 3, 4):
                levels = [-100 + i / 10 + number for i in range(101)]
                service.ingest_sweep({
                    'id': f'{suffix}-sweep-{number}', 'session_id': session.id,
                    'stream_id': f'spectrum-{suffix}',
                    'interval': {
                        'started_at': (base + timedelta(seconds=number * 10, microseconds=1)).isoformat(),
                        'ended_at': (base + timedelta(seconds=number * 10 + 1, microseconds=2)).isoformat(),
                    },
                    'frequencies_hz': [100_000_000 + 100_000 * i for i in range(101)],
                    'levels': levels, 'level_unit': 'dBm',
                    'settings': {'adapter': 'synthetic-fixture', 'rbw_hz': 10_000,
                                 'sequence': number, 'simulated': True, 'generator_output': 'off'},
                })
            service.ingest_observation({
                'id': f'{suffix}-observation', 'session_id': session.id,
                'stream_id': f'receiver-{suffix}',
                'interval': {'started_at': base.isoformat(),
                             'ended_at': (base + timedelta(seconds=1)).isoformat()},
                'frequency_hz': 145_500_000, 'mode': 'FM', 'level': 37,
                'level_unit': 's_meter_count', 'squelch_open': True,
            })
        service.create_sweep_reference('context-one', {
            'name': 'Synthetic retained reference', 'sweep_id': 'one-sweep-1',
        })
        service.close()

        def digest() -> str:
            with sqlite3.connect(path) as db:
                return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()

        before = digest()
        service = SessionService(store=SQLiteSessionStore(str(path)))
        server = create_server(host='127.0.0.1', port=args.port, sessions=service)
        print(json.dumps({
            'url': f'http://127.0.0.1:{args.port}/platform',
            'session': 'context-one', 'stream': 'spectrum-one', 'anchor': 'one-sweep-4',
            'before': 2, 'after': 2, 'points': 101,
            'expected_ids': [f'one-sweep-{n}' for n in range(2, 7)],
            'expected_offsets': [-2, -1, 0, 1, 2],
            'anchor_first_level_dbm': -96,
            'dump_sha256_before': before,
        }), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            server.control_scheduler.close()
            server.automatic_completion.close()
            server.automatic_detection.close()
            server.automatic_handoff.close()
            server.worker_manager.close()
            service.close()
            after = digest()
            print(json.dumps({'dump_sha256_after': after, 'unchanged': after == before}), flush=True)
            assert before == after, 'Synthetic archive mutated during read-only smoke'


if __name__ == '__main__':
    main()
