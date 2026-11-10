import unittest
from unittest.mock import patch

from radioinspector.contracts import Sweep
from radioinspector.device_workers import WorkerError
from radioinspector.monitoring.detector import Detector, build_baseline
from tests.test_monitoring_detector import sweep
from tests import test_monitoring_detector as detector_tests
from tests import test_monitoring_guard as guard_tests


class LegacyCadenceTests(unittest.TestCase):
    setUp = detector_tests.DetectorTests.setUp

    def test_legacy_unknown_cadence_does_not_assume_one_second(self):
        self.baseline.pop('sweep_period_seconds')
        detector = Detector(self.baseline, self.parameters)
        levels = [-100.] * 9
        levels[4] = -92
        for i in (20, 21):
            self.assertEqual(detector.step(sweep(i, levels))[0], [])
        self.assertEqual(detector.step(sweep(22, levels))[0][0]['kind'], 'persistent')

    def test_dense_allowlist_is_applied_to_components(self):
        def large(i, levels=None):
            item = sweep(i)
            return Sweep(session_id='s', stream_id='a', interval=item.interval,
                         frequencies_hz=tuple(100000000 + j * 10000 for j in range(401)),
                         levels=tuple(levels or [-100.] * 401), level_unit='dBm', settings=item.settings)
        baseline = build_baseline([large(i) for i in range(20)])
        levels = [-100.] * 401
        allowed = []
        for j in range(0, 66, 2):
            levels[j] = -60
            allowed.append(dict(id=str(j), start_hz=100000000+j*10000,
                                end_hz=100000000+j*10000, watch_changes=False))
        self.assertEqual(Detector(baseline, self.parameters).step(large(20, levels), allowed)[0], [])
        levels[400] = -80
        result = Detector(baseline, self.parameters).step(large(21, levels), allowed)[0]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['center_frequency_hz'], 104000000)
        self.assertEqual(result[0]['kind'], 'impulse')

    def test_legacy_unknown_cadence_still_resets_backwards_time(self):
        self.baseline.pop('sweep_period_seconds')
        detector = Detector(self.baseline, self.parameters)
        detector.step(sweep(21))
        detector.step(sweep(20))
        self.assertEqual(detector.state['quality_issue']['kind'], 'data_gap')
        self.assertEqual(len(detector.state['window']), 1)


class PreparationOwnershipTests(unittest.TestCase):
    setUp = guard_tests.GuardTests.setUp

    def test_arm_cannot_stop_another_projects_preparation(self):
        with patch.object(self.workers, 'start_session', return_value=[]):
            prepared = self.service.start_preparation(self.project['id'], {})['project']
        sid = prepared['active_session_id']
        other = self.service.create(dict(name='Other', site='City', session_id=sid))
        for i in range(20):
            self.sessions.ingest_sweep(sweep(i, session=sid, stream=prepared['analyzer_stream_id']))
        self.service.create_baseline(other['id'], dict(name='Quiet', location='Room', confirm=True))
        with patch.object(self.workers, 'stop_session') as stop, patch.object(self.service, '_launch_guard'):
            with self.assertRaises(WorkerError):
                self.service.arm(other['id'], {})
            stop.assert_not_called()
        self.assertEqual(self.service.preparing[self.project['id']], sid)
        self.assertEqual(self.sessions.get_session(sid).state.value, 'running')
        self.assertEqual(self.service.project(self.project['id'])['state'], 'collecting')

    def test_legacy_cadence_recovered_from_sources_and_gap_still_detected(self):
        for i in range(20):
            self.sessions.ingest_sweep(sweep(i, session=self.session.id, stream=self.session.streams[0].id))
        baseline = self.service.create_baseline(self.project['id'], dict(name='Quiet', location='Room', confirm=True))
        baseline.pop('sweep_period_seconds')
        self.service.store.put('baseline', baseline)
        levels = [-100.] * 9
        levels[4] = -92
        for i in (20, 21, 22, 40):
            self.sessions.ingest_sweep(sweep(i, levels, self.session.id, self.session.streams[0].id))
        result = self.service.analyze(self.project['id'])
        self.assertEqual(result['quality_issues'], 1)
        events = self.service.store.list('event', self.project['id'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['state'], 'interrupted')
        self.assertEqual(self.service.store.get('baseline', baseline['id'])['sweep_period_seconds'], 4.)
