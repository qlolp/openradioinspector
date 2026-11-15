"""An older Python runtime must not silently run one band of a Rust scan plan."""
from pathlib import Path
import tempfile
import unittest

from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from tests.test_device_workers import simulated_session_payload
from tests.test_monitoring_detector import sweep


class ScanPlanFallbackTests(unittest.TestCase):
    def test_python_fallback_refuses_a_saved_cyclic_plan_before_starting_devices(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        sessions = SessionService(store=SQLiteSessionStore(str(Path(directory.name) / 'archive.sqlite')))
        self.addCleanup(sessions.close)
        workers = DeviceWorkerManager(sessions)
        self.addCleanup(workers.close)
        service = MonitoringService(sessions, workers)
        self.addCleanup(service.close)
        session = sessions.create_session(simulated_session_payload())
        project = service.create(dict(name='Room', site='City', session_id=session.id))
        for index in range(20):
            sessions.ingest_sweep(sweep(index, session=session.id, stream=session.streams[0].id))
        service.create_baseline(project['id'], dict(name='Quiet', location='Room', confirm=True))
        self.assertTrue(service.preflight(project['id'])['ready'])
        project = service.project(project['id'])
        project['scan_plan'] = {'id': 'rust-plan', 'revision': 1, 'schema_version': 1,
                                'config': {'name': 'Two bands', 'bands': [{'id': 'a'}, {'id': 'b'}]}}
        service.store.put('project', project)
        result = service.preflight(project['id'])
        self.assertFalse(result['ready'])
        check = next(item for item in result['checks'] if item['name'] == 'scan_plan_runtime')
        self.assertFalse(check['ok'])
        with self.assertRaises(ValueError):
            service.arm(project['id'], {})
        self.assertEqual(workers.active_count, 0)
        self.assertEqual(service.project(project['id']), project)
        # A new project can inherit the plan in its receive template even when
        # its root record was created by the older Python implementation.
        saved_plan = project.pop('scan_plan')
        project['profile']['analyzer']['settings']['scan_plan'] = saved_plan
        service.store.put('project', project)
        self.assertFalse(service.preflight(project['id'])['ready'])
        with self.assertRaises(ValueError):
            service.arm(project['id'], {})
        self.assertEqual(workers.active_count, 0)
