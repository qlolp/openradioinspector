import unittest
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from radioinspector.device_workers import DeviceWorkerManager, WorkerError
from radioinspector.session_service import SessionService
from radioinspector.session_store import SQLiteSessionStore
from radioinspector.serial_instruments import ARDV1SerialAdapter
from radioinspector.monitoring.service import MonitoringService
from tests.test_device_workers import simulated_session_payload
from tests.test_serial_instruments import ScriptedTransport
from tests.test_monitoring_detector import sweep
from tests.test_device_workers import wait_until
from datetime import timedelta
from radioinspector.contracts import TimeInterval, utc_now
from radioinspector.device_workers import TinySASimulator


class GuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.sessions = SessionService(store=SQLiteSessionStore(str(Path(directory.name)/'archive.sqlite')))
        self.workers = DeviceWorkerManager(self.sessions)
        self.service = MonitoringService(self.sessions, self.workers)
        self.addCleanup(self.sessions.close); self.addCleanup(self.workers.close); self.addCleanup(self.service.close)
        self.session = self.sessions.create_session(simulated_session_payload())
        self.project = self.service.create(dict(name='Room', site='City', session_id=self.session.id))

    def test_arm_requires_confirmed_baseline_without_starting_workers(self):
        with self.assertRaises(ValueError): self.service.arm(self.project['id'], {})
        self.assertEqual(self.workers.active_count, 0)

    def test_guard_reserves_control_and_disarm_releases_it(self):
        for i in range(20): self.sessions.ingest_sweep(sweep(i, session=self.session.id, stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'], dict(name='Quiet', location='Room', confirm=True))
        with patch.object(self.service, '_launch_guard'):
            value = self.service.arm(self.project['id'], {})
        self.assertEqual(value['state'], 'armed')
        with self.assertRaises(WorkerError):
            self.workers.tune(value['active_session_id'], value['receiver_stream_id'], dict(frequency_hz=100000000))
        self.service.disarm(value['id'], {})
        self.assertEqual(self.service.project(value['id'])['state'], 'ready')

    def test_audio_configuration_requires_named_explicit_source(self):
        with self.assertRaises(ValueError): self.service.configure(self.project['id'], {'audio': {'device':0}})
        self.assertNotIn('audio', self.service.project(self.project['id']))

    def test_documented_auto_profile_reads_decoder_without_inference(self):
        stream = self.session.streams[1]
        stream.settings.update(receiver_profile='ardv1-monitor-v1', mode='AUTO')
        transport = ScriptedTransport({'RE1':['20'], 'VR':['20VR2504A'], 'RN':['20RNfixture'],
            'VFA':['20'], 'MD000':['20'], 'RF0145.50000':['20'], 'LM':['20LM0501'], 'MD':['20MD700']})
        adapter = ARDV1SerialAdapter(stream, transport=transport)
        observation = adapter.poll()
        self.assertEqual(observation.mode, 'AUTO'); self.assertEqual(observation.decoded_mode, 'DMR')
        self.assertEqual(observation.level_unit, 's_meter_count')
        self.assertEqual(transport.sent[-2:], [b'LM\r', b'MD\r'])
        self.assertEqual(observation.metadata['decoder_source'], 'receiver_MD')

    def test_unrecognized_firmware_never_sends_digital_mode(self):
        stream = self.session.streams[1]
        stream.settings.update(receiver_profile='ardv1-monitor-v1', mode='AUTO')
        transport = ScriptedTransport({'RE1':['20'], 'VR':['20VRunknown']})
        adapter = ARDV1SerialAdapter(stream, transport=transport)
        with self.assertRaises(WorkerError): adapter.poll()
        self.assertEqual(transport.sent, [b'RE1\r', b'VR\r'])

    def test_receiver_target_quantization_preserves_detected_frequency(self):
        event = {'center_frequency_hz':100000003}
        self.assertEqual(self.service.receiver_target(event),100000000)
        self.assertEqual(event['center_frequency_hz'],100000003)
        with self.assertRaises(ValueError): self.service.receiver_target({'center_frequency_hz':1500000000})

    def test_pending_selection_is_not_starved_by_historical_events(self):
        identifier=self.project['id']; now=utc_now().isoformat()
        with self.service.store.transaction():
            for i in range(1001):
                self.service.store.put('event',dict(id=f'{i:032x}',project_id=identifier,created_at=now,
                    last_seen=now,kind='impulse',disposition='false_positive',analysis={'state':'observed'},
                    source_refs=[{'session_id':'run'}]))
            fresh=dict(id='f'*32,project_id=identifier,created_at=now,last_seen=now,
                kind='impulse',disposition='unreviewed',source_refs=[{'session_id':'run'}])
            self.service.store.put('event',fresh)
        self.assertEqual(self.service.store.pending_receiver_events(identifier,'run')[0]['id'],fresh['id'])

    def test_rearm_waits_for_old_disarm_lifecycle(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Room',confirm=True))
        entered=threading.Event();release=threading.Event();result=[]
        with patch.object(self.service,'_launch_guard'):
            old=self.service.arm(self.project['id'],{})
            def stop(sid):entered.set();release.wait(3);return []
            with patch.object(self.workers,'stop_session',side_effect=stop):
                first=threading.Thread(target=lambda:self.service.disarm(old['id'],{}));first.start()
                self.assertTrue(entered.wait(2))
                second=threading.Thread(target=lambda:result.append(self.service.arm(old['id'],{})));second.start()
                second.join(.1);release.set();first.join(3);second.join(3)
            self.assertNotEqual(result[0]['active_session_id'],old['active_session_id'])
            self.assertEqual(self.service.guard_status(old['id'])['project']['state'],'armed')

    def test_later_receiver_failure_makes_guard_error(self):
        with patch('radioinspector.device_workers.ARDV1Simulator.poll',side_effect=WorkerError('Lost receiver USB')):
            # Matching synthetic baseline; avoid conflating detector quality with USB failure.
            for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
            self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Room',confirm=True))
            with patch.object(self.service,'analyze',return_value={}):
                self.service.arm(self.project['id'],{})
                wait_until(lambda:self.service.project(self.project['id'])['state']=='error','guard receiver failure')
            status=self.service.guard_status(self.project['id'])
            self.assertFalse(status['active']);self.assertIsNotNone(status['error'])

    def test_receiver_dwell_does_not_stop_analyzer_and_event_is_not_replayed(self):
        session = self.sessions.create_session(simulated_session_payload(tinysa_settings={'noise_sigma_db':0,'emitters':[]}))
        project = self.service.create(dict(name='Guard',site='Fixture',session_id=session.id))
        adapter = TinySASimulator(session.streams[0]); now = utc_now()
        for i in range(20):
            item = adapter.poll(); start = now-timedelta(seconds=80-4*i)
            item.interval = TimeInterval(start,start+timedelta(seconds=1)); self.sessions.ingest_sweep(item)
        self.service.create_baseline(project['id'],dict(name='Quiet',location='Fixture',confirm=True))
        project = self.service.project(project['id']); project['parameters']['dwell_seconds'] = .3
        self.service.store.put('project',project)
        armed = self.service.arm(project['id'],{})
        wait_until(lambda: self.workers.active_count == 2, 'both workers')
        target = TinySASimulator(self.sessions.get_session(armed['active_session_id']).streams[0])
        item = target.poll(); item.levels = tuple(-80 if i==20 else x for i,x in enumerate(item.levels))
        self.sessions.ingest_sweep(item)
        wait_until(lambda: bool(self.service.store.list('event',project['id'])), 'detected event')
        event = self.service.store.list('event',project['id'])[0]
        wait_until(lambda: self.service.store.get('event',event['id']).get('analysis',{}).get('state') == 'observed', 'receiver evidence: '+str(self.service.store.get('event',event['id'])))
        result = self.service.store.get('event',event['id'])['analysis']
        self.assertIn('не выбран',result['audio_error'])
        self.assertGreater(len(self.sessions.list_sweeps(armed['active_session_id'])),2)
        self.assertEqual(result['classifications'],[])
        self.service.disarm(project['id'],{})
        self.assertEqual(len(self.sessions.list_candidates(armed['active_session_id'])),1)
