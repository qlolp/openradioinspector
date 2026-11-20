"""Archived actual-bin history and metadata-only bookmark versions."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.web.server import create_server
from tests.test_platform import session_payload
from tests.test_sweep_comparison import measurement


class FrequencyHistoryTests(unittest.TestCase):
    def service(self, persistent=False, unit='dBm'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = str(Path(temporary.name) / 'history.sqlite')
        service = SessionService(store=SQLiteSessionStore(path) if persistent else None)
        self.addCleanup(service.close)
        payload = session_payload()
        payload['streams'][0]['level_unit'] = unit
        session = service.create_session(payload)
        return service, session, path

    def seed(self, service, session, number, **changes):
        value = measurement(session.id, session.streams[0].id, number,
            id=f'sweep-{number}', interval={'started_at':f'2026-11-15T00:00:{number:02d}Z',
                'ended_at':f'2026-11-15T00:00:{number:02d}.500Z'})
        value.update(changes)
        service.ingest_sweep(value)
        return value

    def query(self, session, **changes):
        return dict(stream_id=session.streams[0].id, frequencies_hz=[150, 450],
            start='2026-11-15T00:00:00Z', end='2026-11-15T00:01:00Z', **changes)

    def test_quality_metadata_preserves_history_segments_without_defining_an_acquisition(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                service, session, _ = self.service(persistent)
                stamp = {'status': 'synthetic', 'reason': None, 'verified_through_hz': None}
                sources = [
                    {'rbw_hz': 1000, 'extension': {'value': True}},
                    {'rbw_hz': 1000, 'extension': {'value': True}, 'measurement_qualification': stamp},
                    {'rbw_hz': 1000, 'extension': {'value': 1}, 'measurement_qualification': stamp},
                    {'measurement_qualification': stamp}, {'measurement_qualification': stamp},
                ]
                for index, settings in enumerate(sources):
                    self.seed(service, session, index, settings=settings)
                items = service.frequency_history(session.id, self.query(session))['items']
                segments = {row['sweep_id']: row['segment_id'] for row in items}
                self.assertEqual(segments['sweep-0'], segments['sweep-1'])
                self.assertNotEqual(segments['sweep-1'], segments['sweep-2'])
                self.assertNotEqual(segments['sweep-3'], segments['sweep-4'])
                self.assertEqual([s.settings for s in service.list_sweeps(session.id)], sources)

    def test_invalid_json_in_an_excluded_annotation_is_still_rejected(self):
        from radioinspector.contracts import Sweep
        from radioinspector.frequency_history import history_item
        for invalid in ({'count': float('nan')}, {1: 'invalid JSON key'}):
            raw = measurement('session', 'stream', 0)
            raw['settings']['measurement_qualification'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                history_item(Sweep.from_dict(raw), [100])

    def test_actual_bins_units_intervals_and_configuration_segments(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                service, session, _ = self.service(persistent)
                for n in range(3): self.seed(service, session, n)
                self.seed(service, session, 3, settings={'rbw_hz':30000})
                self.assertTrue(hasattr(service, 'frequency_history'))
                result = service.frequency_history(session.id, self.query(session))
                self.assertEqual([r['sweep_id'] for r in result['items']], ['sweep-3','sweep-2','sweep-1','sweep-0'])
                first, second, third, _ = result['items']
                self.assertEqual(first['samples'], [
                    {'requested_hz':150, 'frequency_hz':100, 'level':-80.0},
                    {'requested_hz':450, 'frequency_hz':None, 'level':None}])
                self.assertEqual(first['level_unit'], 'dBm')
                self.assertEqual(first['interval']['ended_at'], '2026-11-15T00:00:03.500000Z')
                self.assertNotEqual(first['segment_id'], second['segment_id'])
                self.assertEqual(second['segment_id'], third['segment_id'])
                self.assertEqual(first['sweep_id'], next(item.id for item in service.list_sweeps(session.id) if item.id=='sweep-3'))

    def test_pagination_is_snapshot_stable_and_bound_to_frequency_query(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                service, session, _ = self.service(persistent)
                for n in range(4): self.seed(service, session, n)
                self.assertTrue(hasattr(service, 'frequency_history'))
                query = self.query(session, limit=2)
                first = service.frequency_history(session.id, query)
                self.seed(service, session, 4)
                # Late insertion inside the time window is excluded as well.
                self.seed(service, session, 0, id='late-zero')
                second = service.frequency_history(session.id, {**query,'after':first['next_cursor']})
                self.assertEqual([r['sweep_id'] for r in second['items']], ['sweep-1','sweep-0'])
                self.assertIsNone(second['next_cursor'])
                for changes in ({'frequencies_hz':[151]}, {'stream_id':session.streams[1].id}, {'start':'2026-11-15T00:00:01Z'}):
                    with self.assertRaises(ValueError):
                        service.frequency_history(session.id, {**query,'after':first['next_cursor'],**changes})

    def test_missing_range_and_changed_grid_are_not_interpolated_or_skipped(self):
        service, session, _ = self.service()
        self.seed(service, session, 0)
        self.seed(service, session, 1, frequencies_hz=[1000,2000,3000,4000])
        self.seed(service, session, 2, frequencies_hz=[100,210,300,400])
        self.assertTrue(hasattr(service, 'frequency_history'))
        rows = service.frequency_history(session.id,self.query(session))['items']
        self.assertEqual(len(rows),3)
        self.assertIsNone(rows[1]['samples'][0]['level'])
        self.assertNotEqual(rows[0]['segment_id'],rows[2]['segment_id'])
        self.assertEqual(rows[0]['samples'][0]['frequency_hz'],100)

    def test_invalid_and_unbounded_requests_do_not_change_archive(self):
        service, session, _ = self.service()
        self.seed(service, session, 0)
        self.assertTrue(hasattr(service, 'frequency_history'))
        before = copy.deepcopy(service.events())
        for changes in ({'frequencies_hz':[]}, {'frequencies_hz':[1]*5}, {'frequencies_hz':[1,1]},
                        {'frequencies_hz':[True]}, {'frequencies_hz':[1.5]}, {'frequencies_hz':[-1]},
                        {'limit':101}, {'limit':True}, {'after':'x'*17000}, {'other':1}, {'end':'2025-01-01T00:00:00Z'}):
            with self.subTest(changes=changes), self.assertRaises((ValueError,TypeError)):
                service.frequency_history(session.id,{**self.query(session),**changes})
        self.assertEqual(service.events(),before)

    def test_bookmark_versions_persist_and_do_not_create_candidates_or_measurements(self):
        service, session, path = self.service(True)
        self.assertTrue(hasattr(service, 'save_frequency_bookmarks'))
        one = {'name':'Пост','entries':[{'label':'Канал','frequency_hz':100000000}]}
        a = service.save_frequency_bookmarks(session.id,one)
        b = service.save_frequency_bookmarks(session.id,{**one,'entries':[{'label':'Канал 2','frequency_hz':101000000}]})
        self.assertEqual((a['revision'],b['revision']),(1,2))
        self.assertNotEqual(a['id'],b['id'])
        service.close()
        reopened = SessionService(store=SQLiteSessionStore(path))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.list_frequency_bookmarks(session.id)['items'],[b,a])
        self.assertEqual(reopened.list_sweeps(session.id),[])
        self.assertEqual(reopened.list_candidates(session.id),[])
        self.assertEqual(reopened.save_frequency_bookmarks(session.id,{**one,'entries':b['entries']}),b)

    def test_bookmarks_reject_actions_and_invalid_entries(self):
        service, session, _ = self.service()
        self.assertTrue(hasattr(service,'save_frequency_bookmarks'))
        for value in ({'name':'','entries':[]}, {'name':'A','entries':[{'label':'A','frequency_hz':1,'action':'tune'}]},
                      {'name':'A','entries':[{'label':'A','frequency_hz':True}]},
                      {'name':'A','entries':[{'label':'A','frequency_hz':1}]*5}):
            with self.assertRaises(ValueError): service.save_frequency_bookmarks(session.id,value)

    def test_units_scope_and_version_limit(self):
        for persistent in (False, True):
            service, session, _ = self.service(persistent, unit='dBµV')
            self.seed(service, session, 0, level_unit='dBµV')
            result = service.frequency_history(session.id, self.query(session))
            self.assertEqual(result['items'][0]['level_unit'], 'dBµV')
            self.assertEqual(result['items'][0]['samples'][0]['level'], -80)
            other = service.create_session(session_payload())
            with self.assertRaises((KeyError, ValueError)):
                service.frequency_history(other.id, self.query(session))
            for n in range(256):
                service.save_frequency_bookmarks(session.id, {'name':'A','entries':[{'label':'A','frequency_hz':n}]})
            with self.assertRaises(ValueError):
                service.save_frequency_bookmarks(session.id, {'name':'A','entries':[{'label':'A','frequency_hz':256}]})
            self.assertEqual(service.list_frequency_bookmarks(other.id)['items'], [])

    def test_responses_match_public_contract(self):
        from jsonschema import Draft202012Validator
        from radioinspector.web.openapi import document
        service, session, _ = self.service()
        self.seed(service,session,0)
        schemas = document()['components']['schemas']
        for name, value in (
            ('FrequencyHistoryPage', service.frequency_history(session.id,self.query(session))),
            ('FrequencyBookmarkSet', service.save_frequency_bookmarks(session.id,{'name':'A','entries':[{'label':'A','frequency_hz':150}]})),
            ('FrequencyBookmarkPage', service.list_frequency_bookmarks(session.id))):
            Draft202012Validator({'components':{'schemas':schemas}, **schemas[name]}).validate(value)


class FrequencyHistoryHTTPTests(unittest.TestCase):
    service = FrequencyHistoryTests.service
    seed = FrequencyHistoryTests.seed
    query = FrequencyHistoryTests.query
    # Exercise request parsing, envelopes and read-only behavior on a real socket.
    def test_http_history_and_bookmarks(self):
        service, session, _ = self.service(True)
        self.seed(service,session,0)
        server = create_server(host='127.0.0.1',port=0,sessions=service)
        thread = threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(3),server.server_close()))
        def request(method, path, payload=None):
            c = http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=3)
            c.request(method,f'/api/v1/sessions/{session.id}/'+path,
                body=None if payload is None else json.dumps(payload),headers={'Content-Type':'application/json'})
            r=c.getresponse(); data=json.loads(r.read()); status=r.status;c.close();return status,data
        status, data = request('POST','frequency-history',self.query(session))
        self.assertEqual(status,200,data)
        self.assertEqual(data['data']['items'][0]['sweep_id'],'sweep-0')
        status, data = request('POST','frequency-bookmarks',{'name':'A','entries':[{'label':'A','frequency_hz':150}]})
        self.assertEqual(status,201,data)
        self.assertEqual(request('GET','frequency-bookmarks')[1]['data']['items'][0]['name'],'A')
        self.assertEqual(request('POST','frequency-history?unexpected=1',self.query(session))[0],400)
        self.assertEqual(request('POST','frequency-history',{**self.query(session),'frequencies_hz':[]})[0],400)
