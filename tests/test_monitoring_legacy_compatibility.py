"""Compatibility of legacy baselines and configuration-only readiness."""
import copy
import os
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from radioinspector.contracts import TimeInterval
from radioinspector.device_workers import DeviceWorkerManager
from radioinspector.monitoring.service import MonitoringService
from radioinspector.session_service import SessionService
from tests import test_monitoring_guard as guard_tests
from tests.test_monitoring_detector import sweep
from tests.monitoring_serial_fixture import monitoring_serial_session


class ReadinessCompatibilityTests(unittest.TestCase):
    def preflight(self, payload, *, windows=False):
        sessions=SessionService();workers=DeviceWorkerManager(sessions)
        service=MonitoringService(sessions,workers)
        self.addCleanup(sessions.close);self.addCleanup(workers.close);self.addCleanup(service.close)
        session=sessions.create_session(payload)
        project=service.create(dict(name='Config',site='Synthetic',session_id=session.id))
        service.save_preparation(project['id'],dict(antenna_notes='Fixture',receive_path_confirmed=True,location_confirmed=True))
        with patch('radioinspector.serial_instruments.create_serial_transport') as transport:
            with patch('radioinspector.monitoring.service.os',SimpleNamespace(name='nt' if windows else os.name)):
                result=service.preflight(project['id'])
            transport.assert_not_called()
        self.assertEqual(workers.active_count,0)
        self.assertEqual(sessions.list_sweeps(session.id),[])
        self.assertEqual(result['hardware_validation'],'not_run')
        return result

    def test_invalid_worker_timing_and_retries_are_rejected_before_startup(self):
        for role in (0,1):
            for field,value in (('period_s',-1),('period_s',3601),('period_s','fast'),
                                ('max_consecutive_errors',0),('max_consecutive_errors',101),
                                ('failures_remaining',-1)):
                with self.subTest(role=role,field=field,value=value):
                    payload=monitoring_serial_session();payload['streams'][role]['settings'][field]=value
                    result=self.preflight(payload)
                    self.assertFalse(result['ready'])
                    name=('analyzer' if role==0 else 'receiver')+'_worker_settings'
                    self.assertTrue(any(c['name']==name and not c['ok'] for c in result['checks']))

    def test_windows_preflight_uses_native_com_rules_without_opening_ports(self):
        for role in (0,1):
            for port in ('/dev/ttyFAKE','COM0','socket://fixture'):
                with self.subTest(role=role,port=port):
                    payload=monitoring_serial_session()
                    payload['streams'][role]['port']=port
                    self.assertFalse(self.preflight(payload,windows=True)['ready'])
        payload=monitoring_serial_session();payload['streams'][1]['port']='\\\\.\\COM4'
        self.assertTrue(self.preflight(payload,windows=True)['ready'])


class LegacyCadenceCompatibilityTests(unittest.TestCase):
    setUp=guard_tests.GuardTests.setUp

    def legacy_baseline(self):
        for i in range(20):
            item=sweep(i,session=self.session.id,stream=self.session.streams[0].id)
            started=sweep(0).interval.started_at+timedelta(seconds=4*i if i<19 else 3600)
            item.interval=TimeInterval(started,started+timedelta(seconds=1))
            self.sessions.ingest_sweep(item)
        baseline=self.service.create_baseline(self.project['id'],dict(name='Legacy',location='Fixture',confirm=True))
        baseline.pop('sweep_period_seconds')
        return self.service.store.put('baseline',baseline)

    def test_source_median_recovers_gaps_without_rewriting_confirmed_baseline(self):
        baseline=self.legacy_baseline()
        for i in range(20,23):
            levels=[-100.]*9;levels[4]=-92.
            item=sweep(i,levels,session=self.session.id,stream=self.session.streams[0].id)
            started=sweep(0).interval.started_at+timedelta(seconds=3600+16*(i-19))
            item.interval=TimeInterval(started,started+timedelta(seconds=1));self.sessions.ingest_sweep(item)
        result=self.service.analyze(self.project['id'])
        self.assertEqual(self.service.store.list('event',self.project['id']),[])
        self.assertEqual(result['quality_issues'],2)
        self.assertEqual(self.service.store.get('baseline',baseline['id']),baseline)

    def test_replacing_with_smaller_grid_preserves_old_event_and_uses_new_baseline(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Nine bins',location='Fixture',confirm=True))
        levels=[-100.]*9;levels[8]=-85.
        self.sessions.ingest_sweep(sweep(20,levels,self.session.id,self.session.streams[0].id))
        self.service.analyze(self.project['id'])
        old=self.service.store.list('event',self.project['id'])[0]
        old['analysis']={'state':'observed','observation_ids':['historical-evidence']}
        self.service.store.put('event',old)
        for i in range(100,120):
            item=sweep(i,session=self.session.id,stream=self.session.streams[0].id)
            item.frequencies_hz=(100000000,100040000,100080000);item.levels=(-100.,-100.,-100.)
            self.sessions.ingest_sweep(item)
        baseline=self.service.create_baseline(self.project['id'],dict(name='Three bins',location='Fixture',confirm=True))
        item=sweep(120,session=self.session.id,stream=self.session.streams[0].id)
        item.frequencies_hz=(100000000,100040000,100080000);item.levels=(-100.,-100.,-85.)
        self.sessions.ingest_sweep(item);self.service.analyze(self.project['id'])
        events=self.service.store.list('event',self.project['id'])
        self.assertEqual(len(events),2)
        self.assertEqual(self.service.store.get('event',old['id']),old)
        latest=next(event for event in events if event['id']!=old['id'])
        self.assertEqual(latest['baseline_id'],baseline['id'])
        self.assertEqual(latest['source_count'],1)
        self.assertEqual(len(self.sessions.list_sweeps(self.session.id)),42)

    def test_missing_sources_are_not_bridged_into_a_fabricated_period(self):
        baseline=self.legacy_baseline();baseline=copy.deepcopy(baseline)
        for i,ref in enumerate(baseline['source_refs']):
            if i not in (0,2):ref['sweep_id']='missing-'+ref['sweep_id']
        self.service.store.put('baseline',baseline)
        result=self.service.preflight(self.project['id'])
        self.assertFalse(result['ready'])
        self.assertTrue(any(c['name']=='baseline_cadence' and not c['ok'] for c in result['checks']))
        self.assertEqual(self.service.store.get('baseline',baseline['id']),baseline)
        with self.assertRaises(ValueError):self.service.arm(self.project['id'],{})
        result=self.service.start_preparation(self.project['id'],{})
        self.assertEqual(result['project']['state'],'collecting')
        self.assertNotEqual(result['project']['active_session_id'],self.session.id)
        self.service.stop_preparation(self.project['id'],{})
        self.assertEqual(self.service.store.get('baseline',baseline['id']),baseline)

    def test_unknown_research_cadence_does_not_invent_forward_gap_but_resets_backwards(self):
        from radioinspector.monitoring.detector import Detector
        baseline=self.legacy_baseline()
        baseline['sweep_period_seconds']=None
        detector=Detector(baseline,self.project['parameters'])
        first=sweep(200,session=self.session.id,stream=self.session.streams[0].id)
        detector.step(first)
        later=sweep(2000,session=self.session.id,stream=self.session.streams[0].id)
        detector.step(later)
        self.assertNotIn('quality_issue',detector.state)
        self.assertEqual(len(detector.state['window']),2)
        detector.step(sweep(201,session=self.session.id,stream=self.session.streams[0].id))
        self.assertEqual(detector.state['quality_issue']['kind'],'data_gap')
        self.assertLess(detector.state['quality_issue']['gap_seconds'],0)
        self.assertEqual(len(detector.state['window']),1)
