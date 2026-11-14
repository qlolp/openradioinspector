"""Owned synthetic real-core SSE fixture. No device or existing archive input."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--count', type=int, required=True)
    args = parser.parse_args()
    if not 2 <= args.count <= 4096:
        parser.error('synthetic sweep count must be 2..4096')
    root = args.source_root.resolve(strict=True)
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                       stdin=subprocess.DEVNULL, timeout=5, text=True).strip()
    source_clean = not subprocess.check_output(
        ['git','-C',str(root),'status','--porcelain','--untracked-files=all','--','radioinspector'],
        stdin=subprocess.DEVNULL,timeout=5,text=True).strip()
    sys.path.insert(0, str(root))
    from radioinspector.session_service import SessionService
    from radioinspector.session_store import SQLiteSessionStore
    from radioinspector.tasks import TaskBook
    from radioinspector.web.server import create_server
    from tests.loopback_dns import local_loopback_names

    def reply(**value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    with tempfile.TemporaryDirectory(prefix='ori-native-durable-') as temporary:
        directory = Path(temporary).resolve()
        path = directory / 'synthetic.sqlite3'
        base = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)

        def digest():
            with closing(sqlite3.connect(path)) as connection:
                return hashlib.sha256('\n'.join(connection.iterdump()).encode()).hexdigest()

        def ingest(service, index):
            service.ingest_sweep(dict(
                id=['é', 'e\u0301'][index] if index < 2 else f'sweep-{index}',
                session_id='synthetic-é', stream_id='a',
                interval=dict(started_at=(base+timedelta(microseconds=index)).isoformat(),
                              ended_at=(base+timedelta(seconds=1,microseconds=index)).isoformat()),
                frequencies_hz=[100000001,100000002,100000003],
                levels=[-0.0,math.nextafter(1.0,2.0),-100.125], level_unit='dBm',
                settings={'é':1,'e\u0301':2,'large_integer':36893488147419103233,
                          'raw_zero':-0.0,'adapter':'synthetic-only','generator_output':'off'}))

        store = SQLiteSessionStore(str(path))
        sessions = SessionService(store=store)
        sessions.create_session(dict(id='synthetic-é',name='Synthetic SSE',streams=[
            dict(id='a',device='Synthetic tinySA',role='spectrum_analyzer',level_unit='dBm'),
            dict(id='r',device='Synthetic AR-DV1',role='receiver',level_unit='s_meter_count')]))
        for index in range(args.count):
            ingest(sessions,index)
        sessions.ingest_observation(dict(id='raw-meter',session_id='synthetic-é',stream_id='r',
            interval=dict(started_at=base.isoformat(),ended_at=(base+timedelta(seconds=1)).isoformat()),
            frequency_hz=100000001,mode='FM',level=137,level_unit='s_meter_count'))
        sessions.publish('future.opaque-event',session_id='synthetic-é',payload={
            'é':1,'e\u0301':2,'large_integer':36893488147419103233,'raw_zero':-0.0})
        # Reopen before serving: replay must come from committed archive history.
        sessions.close()
        server = thread = None

        def start(port=0):
            nonlocal store,sessions,server,thread
            store=SQLiteSessionStore(str(path))
            sessions=SessionService(store=store)
            with local_loopback_names():
                server=create_server(host='127.0.0.1',port=port,sessions=sessions,
                                     task_book=TaskBook(directory/'tasks.json'))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()

        def stop():
            if server is not None:
                server.shutdown()
                thread.join(3)
                if thread.is_alive():
                    raise RuntimeError('owned HTTP fixture did not stop')
                # The fixture owns this injected service too. Wake its SSE readers.
                sessions.close()
                server.server_close()

        start()
        expected=digest()
        reply(url=f'http://127.0.0.1:{server.server_port}',source_revision=revision,source_python_clean=source_clean,
              expected_events=args.count+3,hardware_validation='not_run')
        try:
            for line in sys.stdin:
                command=json.loads(line)
                if command == {'restart':True}:
                    if digest()!=expected:
                        raise AssertionError('read-only client changed the archive')
                    port=server.server_port
                    stop();start(port)
                    expected=digest()
                    reply(restarted=True)
                elif command == {'append':True}:
                    ingest(sessions,args.count)
                    expected=digest()
                    reply(appended=True)
                else:
                    raise ValueError('unsupported synthetic owner command')
        finally:
            unchanged=digest()==expected
            stop()
        if not unchanged:
            raise AssertionError('read-only client changed the archive')
        reply(closed=True,archive_unchanged=True,hardware_validation='not_run')


if __name__ == '__main__':
    main()
