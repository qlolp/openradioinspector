"""HTTP replay must read committed history beyond the in-memory event tail."""
import http.client
import json
import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import Handler,create_server
from radioinspector.web.event_streams import EventStreams
from tests.test_platform import session_payload


class DurableSseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = str(Path(self.temporary.name).resolve()/'archive.sqlite')
        self.store = SQLiteSessionStore(self.path)
        self.sessions = SessionService(store=self.store,history_limit=2)
        self.session = self.sessions.create_session(session_payload())
        self.server = self.thread = None
        self.addCleanup(self.stop)

    def start(self):
        self.server = create_server(host='127.0.0.1',port=0,sessions=self.sessions)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.thread.join(3)
            self.server.server_close()
            self.server = self.thread = None
        self.sessions.close()

    def ingest(self,index):
        return self.sessions.ingest_sweep(dict(id=f'sweep-{index}',session_id=self.session.id,
            stream_id=self.session.streams[0].id,
            interval={'started_at':'2026-11-11T00:00:00.123456Z',
                      'ended_at':'2026-11-11T00:00:00.223456Z'},
            frequencies_hz=[100,200],levels=[-0.0,1.0000000000000002],
            level_unit='dBm',settings={'sequence':index}))

    def connect(self,after=0):
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=2)
        self.addCleanup(connection.close)
        connection.request('GET',f'/api/v1/events?after={after}',headers={'Accept':'text/event-stream'})
        response=connection.getresponse()
        self.assertEqual(response.status,200)
        return response

    def next_event(self,response):
        fields={}
        while True:
            raw=response.readline(2*1024*1024)
            self.assertTrue(raw,'SSE closed before the expected committed event')
            line=raw.decode('utf-8').rstrip('\r\n')
            if not line:
                if not fields:continue
                event=json.loads(fields['data'])['data']
                self.assertEqual(str(event['sequence']),fields['id'])
                self.assertEqual(event['event_type'],fields['event'])
                return event
            if not line.startswith(':'):
                key,value=line.split(':',1)
                fields[key]=value.lstrip(' ')

    def test_replay_outside_memory_tail_then_live_handoff_keeps_every_original(self):
        for index in range(5):self.ingest(index)
        self.start()
        response=self.connect()
        self.assertEqual(self.next_event(response)['sequence'],1)
        for index in range(5):
            event=self.next_event(response)
            self.assertEqual(event['sequence'],index+2)
            source=event['payload']['sweep']
            self.assertEqual(source['id'],f'sweep-{index}')
            self.assertEqual(source['frequencies_hz'],[100,200])
            self.assertEqual(math.copysign(1,source['levels'][0]),-1)
            self.assertEqual(source['levels'][1],1.0000000000000002)
        self.ingest(5)
        self.assertEqual(self.next_event(response)['payload']['sweep']['id'],'sweep-5')

    def test_restart_replays_from_durable_cursor_instead_of_recent_tail(self):
        for index in range(5):self.ingest(index)
        self.sessions.close()
        self.store=SQLiteSessionStore(self.path)
        self.sessions=SessionService(store=self.store,history_limit=1)
        self.start()
        response=self.connect(after=2)
        for sequence in range(3,7):
            self.assertEqual(self.next_event(response)['sequence'],sequence)

    def test_busy_independent_readers_delay_stream_without_blocking_measurement_commit(self):
        self.start()
        response=self.connect(after=1)
        with self.store.read_snapshot(),self.store.read_snapshot():
            self.ingest(0)
        self.assertEqual(self.next_event(response)['payload']['sweep']['id'],'sweep-0')

    def test_closed_socket_during_headers_releases_its_owned_subscription(self):
        real_watch=self.sessions.watch_commits
        observed=[]
        def watch():
            notifier=real_watch();observed.append(notifier);return notifier
        for error in [BrokenPipeError('synthetic closed socket'),
                      ConnectionAbortedError(10053,'synthetic Windows socket abort'),
                      ConnectionResetError('synthetic connection reset'),
                      OSError(10053,'synthetic Windows aborted send')]:
            with self.subTest(error=type(error).__name__):
                handler=object.__new__(Handler)
                handler.sessions=self.sessions
                handler.event_streams=EventStreams()
                handler.connection=Mock()
                handler.wfile=Mock()
                handler.send_response=Mock(side_effect=error)
                with patch.object(self.sessions,'watch_commits',side_effect=watch):
                    handler._stream_session_events()
                self.assertNotIn(observed[-1],self.sessions._stream_notifiers)
                self.assertEqual(handler.event_streams.active_count,0)
