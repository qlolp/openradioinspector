"""Real HTTP regression for bounded history and immutable PCM evidence."""
import base64
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sqlite3
from urllib.parse import quote, urlencode
import wave
import struct
from contextlib import closing

from radioinspector.web.server import create_server
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.session_service import SessionService
from tests.test_platform import session_payload


class ModernHistoryAudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = create_server(host='127.0.0.1', port=0,
                                    session_database=str(Path(self.temporary.name).resolve()/'archive.sqlite'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _, response = self.request('POST', '/api/v1/sessions', session_payload())
        self.session = response['data']
        self.base = '/api/v1/sessions/' + quote(self.session['id'], safe='')

    def request(self, method, path, payload=None, *, binary=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload),
                               {} if payload is None else {'Content-Type':'application/json'})
            response = connection.getresponse()
            raw = response.read()
            return response.status, raw if binary else json.loads(raw)
        finally:
            connection.close()

    def sweep(self, identifier, started='2026-11-11T12:00:00Z'):
        return dict(id=identifier, stream_id=self.session['streams'][0]['id'],
                    interval=dict(started_at=started, ended_at=started),
                    frequencies_hz=[100,200,300], levels=[-100.,-50.,-50.],
                    level_unit='dBm', settings={'rbw_hz':1000})

    def test_summary_pages_freeze_ingestion_and_keep_exact_identity_and_first_peak(self):
        for identifier in ('a','é','e\u0301'):
            status, _ = self.request('POST', self.base+'/sweeps', self.sweep(identifier))
            self.assertEqual(status,201)
        status, response = self.request('GET',self.base+'/sweep-summaries?limit=2')
        self.assertEqual(status,200)
        items = response['data']['items']
        self.assertEqual([item['id'] for item in items], ['é','e\u0301'])
        self.assertEqual(items[0]['peak_frequency_hz'],200)
        self.assertEqual(items[0]['peak_level'],-50.)
        self.assertEqual(items[0]['point_count'],3)
        self.assertNotIn('levels',items[0])
        cursor=response['data']['next_cursor']
        self.request('POST', self.base+'/sweeps', self.sweep('late','2026-11-11T11:00:00Z'))
        _, second = self.request('GET',self.base+'/sweep-summaries?'+urlencode({'limit':2,'after':cursor}))
        self.assertEqual([item['id'] for item in second['data']['items']],['a'])
        self.assertIsNone(second['data']['next_cursor'])
        status,_=self.request('GET',self.base+'/sweep-summaries?'+urlencode({'after':cursor,'min_hz':150}))
        self.assertEqual(status,400)

    def test_summary_frequency_selects_intersecting_whole_source_and_normalizes_offsets(self):
        self.request('POST',self.base+'/sweeps',self.sweep('older','2026-11-11T12:00:00.000001Z'))
        self.request('POST',self.base+'/sweeps',self.sweep('newer','2026-11-11T15:00:00.000002+03:00'))
        status,response=self.request('GET',self.base+'/sweep-summaries?min_hz=250&max_hz=260&limit=1')
        self.assertEqual(status,200)
        self.assertEqual(response['data']['items'][0]['id'],'newer')
        self.assertEqual(response['data']['items'][0]['peak_frequency_hz'],200)

    def audio(self):
        output=io.BytesIO()
        with wave.open(output,'wb') as target:
            target.setnchannels(1);target.setsampwidth(2);target.setframerate(48000)
            target.writeframes(b'\x00\x00\x01\x00'*240)
        raw=output.getvalue()
        return raw,dict(id='audio-é',stream_id=self.session['streams'][1]['id'],frequency_hz=100000000,
                        mode='FM',interval={'started_at':'2026-11-11T12:00:00Z','ended_at':'2026-11-11T12:00:00.010000Z'},
                        data_base64=base64.b64encode(raw).decode('ascii'),sha256=hashlib.sha256(raw).hexdigest())

    def test_complete_audio_bytes_and_stable_id_are_committed_once_with_summary_event(self):
        raw, payload=self.audio()
        before=self.server.session_service.events()
        status,response=self.request('POST',self.base+'/audio-segments',payload)
        self.assertEqual(status,201,response)
        item=response['data']
        self.assertEqual(item['id'],payload['id'])
        self.assertEqual(item['metadata']['frame_count'],480)
        self.assertEqual(item['metadata']['sample_rate'],48000)
        route=self.base+'/streams/'+quote(payload['stream_id'],safe='')+'/instrument/artifacts/'+quote(item['id'],safe='')+'/content'
        self.assertEqual(self.request('GET',route,binary=True),(200,raw))
        after=self.server.session_service.events()
        self.assertEqual(len(after),len(before)+1)
        self.assertEqual(after[-1].event_type,'audio_segment.received')
        self.assertNotIn('data_base64',json.dumps(after[-1].payload))
        self.assertEqual(self.request('POST',self.base+'/audio-segments',payload)[0],409)
        self.assertEqual(self.server.session_service.events(),after)

    def test_bad_audio_cannot_change_archive_or_event_watermark(self):
        _,payload=self.audio()
        before=self.server.session_service.events()
        for changes in ({'sha256':'0'*64},{'data_base64':'!!'}, {'frequency_hz':True},
                        {'mode':''},{'interval':{'started_at':'2026-11-11T12:00:00Z','ended_at':'2026-11-11T12:01:00Z'}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.request('POST',self.base+'/audio-segments',dict(payload,**changes))[0],400)
                self.assertEqual(self.server.session_service.events(),before)
        self.assertEqual(self.server.session_service.list_instrument_artifacts(self.session['id'],payload['stream_id']),[])

    def test_incomplete_trailing_wav_chunks_and_extra_data_are_rejected(self):
        raw,payload=self.audio()
        before=self.server.session_service.events()
        for suffix in (b'JUNK'+struct.pack('<I',1000)+b'x', b'data'+struct.pack('<I',2)+b'\0\0', b'x'):
            broken=raw+suffix
            broken=broken[:4]+struct.pack('<I',len(broken)-8)+broken[8:]
            request=dict(payload,data_base64=base64.b64encode(broken).decode('ascii'),sha256=hashlib.sha256(broken).hexdigest())
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request('POST',self.base+'/audio-segments',request)[0],400)
                self.assertEqual(self.server.session_service.events(),before)

    def test_wav_declared_sample_format_must_match_mono_pcm16(self):
        raw,payload=self.audio()
        before=self.server.session_service.events()
        for offset,encoded in ((34,struct.pack('<H',9)), (32,struct.pack('<H',4)), (28,struct.pack('<I',123))):
            changed=raw[:offset]+encoded+raw[offset+len(encoded):]
            request=dict(payload,data_base64=base64.b64encode(changed).decode('ascii'),sha256=hashlib.sha256(changed).hexdigest())
            with self.subTest(offset=offset):
                self.assertEqual(self.request('POST',self.base+'/audio-segments',request)[0],400)
                self.assertEqual(self.server.session_service.events(),before)

    def test_audio_event_failure_rolls_back_artifact_before_publication(self):
        service=self.server.session_service
        db=service._store._connection
        _,payload=self.audio()
        before=service.events()
        with db:
            db.execute("""CREATE TRIGGER reject_audio_event BEFORE INSERT ON events
                WHEN NEW.event_type='audio_segment.received'
                BEGIN SELECT RAISE(ABORT,'synthetic event write failure'); END""")
        with self.assertRaises(ValueError):service.ingest_audio_segment(self.session['id'],payload)
        self.assertEqual(service.events(),before)
        self.assertEqual(service.list_instrument_artifacts(self.session['id'],payload['stream_id']),[])

    def test_bounded_read_pool_does_not_stop_ingestion(self):
        store=self.server.session_service._store
        with store.read_snapshot(),store.read_snapshot():
            self.assertEqual(self.request('GET',self.base+'/sweep-summaries')[0],503)
            self.assertEqual(self.request('POST',self.base+'/sweeps',self.sweep('readers-busy'))[0],201)
        self.assertEqual(self.request('GET',self.base+'/sweep-summaries')[1]['data']['items'][0]['id'],'readers-busy')


class SummaryStorageParityTests(unittest.TestCase):
    def test_memory_utf8_and_legacy_utf16_archives_use_identical_keyset_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            for encoding in ('memory','UTF-8','UTF-16le'):
                with self.subTest(encoding=encoding):
                    if encoding == 'memory':store=None
                    else:
                        path=str(Path(temporary)/(encoding+'.sqlite'))
                        with closing(sqlite3.connect(path)) as db:db.execute("PRAGMA encoding='"+encoding+"'")
                        store=SQLiteSessionStore(path)
                    service=SessionService(store=store)
                    try:
                        session=service.create_session(session_payload())
                        for identifier in ('é','e\u0301','α'):
                            service.ingest_sweep(dict(id=identifier,session_id=session.id,
                                stream_id=session.streams[0].id,
                                interval=dict(started_at='2026-11-11T15:00:00.000001+03:00',ended_at='2026-11-11T15:00:00.000002+03:00'),
                                frequencies_hz=[1,2,3],levels=[-90.,-50.,-70.],level_unit='dBm'))
                        first=service.sweep_summaries(session.id,limit=1)
                        second=service.sweep_summaries(session.id,limit=2,after=first['next_cursor'])
                        self.assertEqual([item['id'] for item in first['items']+second['items']],['α','é','e\u0301'])
                        self.assertIsNone(second['next_cursor'])
                    finally:service.close()
