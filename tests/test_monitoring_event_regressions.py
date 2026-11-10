"""Synthetic regression coverage for baseline-specific event evidence."""
import unittest
from dataclasses import replace
from radioinspector.monitoring.detector import Detector, build_baseline
from radioinspector.monitoring.models import record
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.device_workers import DeviceWorkerManager
from tests.test_device_workers import simulated_session_payload
from tests.test_monitoring_detector import sweep


class EventRegressions(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionService()
        self.workers = DeviceWorkerManager(self.sessions)
        self.service = MonitoringService(self.sessions, self.workers)
        self.addCleanup(self.sessions.close)
        self.addCleanup(self.workers.close)
        self.addCleanup(self.service.close)
        session = self.sessions.create_session(simulated_session_payload())
        self.sid, self.stream = session.id, session.streams[0].id
        self.project = self.service.create(dict(name='Room', site='City', session_id=self.sid))
        self.pid = self.project['id']
        self.index = 0
        self.install_baseline(9)

    def source(self, levels):
        value = replace(sweep(self.index, session=self.sid, stream=self.stream),
                        frequencies_hz=tuple(100_000_000 + i*10000 for i in range(len(levels))),
                        levels=tuple(levels))
        self.index += 1
        return value

    def install_baseline(self, size):
        sources = [self.source([-100.] * size) for _ in range(20)]
        baseline = record(self.pid, **build_baseline(sources))
        self.service.store.put('baseline', baseline)
        project = self.service.project(self.pid)
        project.update(baseline_id=baseline['id'], detector_state=None)
        self.service.store.put('project', project)
        self.baseline = baseline

    def analyze(self, levels):
        self.sessions.ingest_sweep(self.source(levels))
        return self.service.analyze(self.pid)

    def test_smaller_replacement_grid_retires_old_event_without_indexing_it(self):
        self.analyze([-100.] * 8 + [-85.])
        old = self.service.store.list('event', self.pid)[0]
        self.install_baseline(3)
        self.analyze([-100.] * 3)
        updated = self.service.store.get('event', old['id'])
        self.assertEqual(updated['state'], 'superseded')
        self.assertEqual(updated['source_refs'], old['source_refs'])
        self.assertEqual(updated['disposition'], old['disposition'])

    def test_same_grid_replacement_cannot_merge_evidence_into_old_baseline(self):
        levels = [-100.] * 8 + [-85.]
        self.analyze(levels)
        old = self.service.store.list('event', self.pid)[0]
        self.install_baseline(9)
        self.analyze(levels)
        events = self.service.store.list('event', self.pid)
        self.assertEqual(len(events), 2)
        self.assertEqual(self.service.store.get('event', old['id'])['source_count'], 1)
        self.assertEqual(sum(e['baseline_id'] == self.baseline['id'] for e in events), 1)

    def test_split_regions_count_current_sweep_once(self):
        self.analyze([-100., -100., -85., -85., -85.] + [-100.] * 4)
        self.analyze([-100., -100., -85., -100., -85.] + [-100.] * 4)
        events = self.service.store.list('event', self.pid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['source_count'], 2)
        self.assertEqual(len(events[0]['source_refs']), 2)
        self.assertEqual(len(self.service.store.list('event-source', self.pid)), 2)
        self.assertEqual(self.service.analyze(self.pid)['processed_sweeps'], 0)

    def test_all_thirty_three_regions_are_persisted_and_updated(self):
        self.install_baseline(133)
        levels = [-85. if i % 4 == 0 and i < 132 else -100. for i in range(133)]
        for _ in range(2):
            self.assertEqual(self.analyze(levels)['events_updated'], 33)
        events = self.service.store.list('event', self.pid)
        self.assertEqual(len(events), 33)
        self.assertTrue(all(e['source_count'] == 2 for e in events))

    def test_allowed_broad_changes_do_not_alarm_or_enter_adaptation(self):
        parameters = self.project['parameters']
        detector = Detector(self.baseline, parameters)
        allowed = [dict(id='allowed', start_hz=100000000, end_hz=100040000, watch_changes=False)]
        result = detector.step(self.source([-85.] * 5 + [-100.] * 4), allowed)[0]
        self.assertEqual(result, [])
        self.assertEqual(detector.state['offsets'], [0.] * 9)
        allowed[0]['watch_changes'] = True
        watched = Detector(self.baseline, parameters).step(
            self.source([-85.] * 5 + [-100.] * 4), allowed)[0]
        self.assertEqual(watched[0]['kind'], 'broadband_change')
        allowed[0]['watch_changes'] = False
        result = detector.step(self.source([-85.] * 6 + [-100.] * 3), allowed)[0]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['first_bin'], 5)
        self.assertEqual(result[0]['last_bin'], 5)
