import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from radioinspector.contracts import Sweep, TimeInterval
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.monitoring.service import MonitoringService
from radioinspector.monitoring.detector import build_baseline, Detector
from tests.test_device_workers import simulated_session_payload
from tests.test_platform import BASE


def sweep(index, levels=None, session='s', stream='a'):
    start = BASE+timedelta(seconds=4*index)
    return Sweep(session_id=session, stream_id=stream, id=f'sweep-{index}',
        interval=TimeInterval(start, start+timedelta(seconds=1)),
        frequencies_hz=tuple(100_000_000+i*10000 for i in range(9)),
        levels=tuple(levels or [-100.]*9), level_unit='dBm',
        settings={'adapter':'synthetic','rbw_hz':10000,'sequence':index})


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.baseline = build_baseline([sweep(i) for i in range(20)])
        self.parameters = dict(margin_db=6., impulse_db=12., hysteresis_db=3., confirmations=3, window_sweeps=5)

    def test_new_carrier_confirms_three_of_five_and_never_learns_it(self):
        detector = Detector(self.baseline, self.parameters)
        levels = [-100.]*9; levels[4] = -92
        for i in range(2): self.assertEqual(detector.step(sweep(20+i, levels))[0], [])
        regions = detector.step(sweep(22, levels))[0]
        self.assertEqual(len(regions), 1); self.assertEqual(regions[0]['kind'], 'persistent')
        for i in range(23,100): detector.step(sweep(i, levels))
        self.assertEqual(detector.state['offsets'][4], 0)
        self.assertEqual(self.baseline['p95_dbm'][4], -100)

    def test_impulse_is_immediate_broad_change_is_one_event_and_allowed_is_suppressed(self):
        detector = Detector(self.baseline, self.parameters)
        levels = [-100.]*9; levels[2] = -85
        result = detector.step(sweep(20, levels))[0]
        self.assertEqual(result[0]['kind'], 'impulse')
        result = detector.step(sweep(21, [-85]*9))[0]
        self.assertEqual(len(result), 1); self.assertEqual(result[0]['kind'], 'broadband_change')
        self.assertEqual(detector.state['offsets'], [0.]*9)
        allowed = [{'id':'legit','start_hz':100020000,'end_hz':100020000,'watch_changes':False}]
        self.assertEqual(Detector(self.baseline,self.parameters).step(sweep(20,levels), allowed)[0], [])
        allowed[0]['watch_changes'] = True
        self.assertEqual(Detector(self.baseline,self.parameters).step(sweep(20,levels), allowed)[0][0]['kind'], 'authorized_changed')

    def test_incompatible_sources_and_short_learning_are_rejected(self):
        with self.assertRaises(ValueError): build_baseline([sweep(0)])
        sources = [sweep(i) for i in range(20)]; sources[-1].settings['rbw_hz'] = 30000
        with self.assertRaises(ValueError): build_baseline(sources)
        source = sweep(20); source.settings['rbw_hz'] = 30000
        with self.assertRaises(ValueError): Detector(self.baseline,self.parameters).step(source)

    def test_durable_cursor_replay_and_evidence_preserve_raw_archive(self):
        with tempfile.TemporaryDirectory() as root:
            store = SQLiteSessionStore(str(Path(root)/'archive.sqlite'))
            sessions = SessionService(store=store); workers = DeviceWorkerManager(sessions)
            service = MonitoringService(sessions,workers)
            self.addCleanup(store.close); self.addCleanup(sessions.close); self.addCleanup(workers.close); self.addCleanup(service.close)
            session = sessions.create_session(simulated_session_payload()); stream = session.streams[0].id
            for i in range(20): sessions.ingest_sweep(sweep(i,session=session.id,stream=stream))
            project = service.create(dict(name='Room',site='City',session_id=session.id))
            baseline = service.create_baseline(project['id'],dict(name='Quiet',location='Room',confirm=True))
            with self.assertRaises(ValueError): service.create_baseline(project['id'],dict(name='Quiet',location='Room',confirm=False))
            levels = [-100.]*9; levels[4] = -92
            for i in range(20,23): sessions.ingest_sweep(sweep(i,levels,session.id,stream))
            result = service.analyze(project['id'])
            self.assertEqual(result['processed_sweeps'],3)
            events = service.store.list('event',project['id'])
            self.assertEqual(len(events),1); self.assertEqual(events[0]['source_count'],3)
            self.assertEqual(len(service.store.list('event-source',project['id'])),3)
            self.assertEqual(service.analyze(project['id'])['processed_sweeps'],0)
            self.assertEqual(len(sessions.list_sweeps(session.id)),23)
            for i in range(23,28): sessions.ingest_sweep(sweep(i,session=session.id,stream=stream))
            service.analyze(project['id'])
            self.assertEqual(service.store.get('event',events[0]['id'])['state'],'closed')
            self.assertEqual(service.store.get('baseline',baseline['id']),baseline)
            reviewed = service.review_event(project['id'],events[0]['id'],dict(disposition='false_positive'))
            self.assertEqual(reviewed['disposition'],'false_positive')
            self.assertEqual(len(sessions.list_sweeps(session.id)),28)
