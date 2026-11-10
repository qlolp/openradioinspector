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

    def test_manual_analyzer_ack_before_guard_thread_starts_keeps_wide_worker_stopped(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Room',confirm=True))
        entered=threading.Event();release=threading.Event();guard_polled=threading.Event()
        original=self.service._guard_loop
        list_workers=self.workers.list_workers
        def delayed(identifier,runtime):
            entered.set();release.wait(3);original(identifier,runtime)
        def observed_workers(sid):
            result=list_workers(sid)
            if threading.current_thread().name.startswith('monitor-'):guard_polled.set()
            return result
        with patch.object(self.service,'_guard_loop',side_effect=delayed),patch.object(self.service,'analyze',return_value={}),\
                patch.object(self.workers,'list_workers',side_effect=observed_workers):
            project=self.service.arm(self.project['id'],{})
            self.assertTrue(entered.wait(2))
            try:
                self.service.manual_acquire(project['id'],{'role':'analyzer'})
                release.set()
                self.assertTrue(guard_polled.wait(2),'guard must finish initial startup before inspecting ownership')
                analyzer=[x for x in self.workers.list_workers(project['active_session_id'])
                    if x['stream_id']==project['analyzer_stream_id']]
                self.assertFalse(any(x['alive'] for x in analyzer),'manual acknowledgement must exclude later wide startup')
                self.assertEqual(self.service.project(project['id'])['coverage'],'paused')
            finally:release.set();self.service.disarm(project['id'],{})

    def test_arm_rejects_another_projects_preparation_without_stopping_it(self):
        preparation=self.service.start_preparation(self.project['id'],{})['project']
        adapter=TinySASimulator(self.sessions.get_session(preparation['active_session_id']).streams[0]);now=utc_now()
        for i in range(20):
            item=adapter.poll();started=now-timedelta(seconds=80-4*i)
            item.interval=TimeInterval(started,started+timedelta(seconds=1));self.sessions.ingest_sweep(item)
        other=self.service.create(dict(name='Other',site='City',session_id=preparation['active_session_id']))
        self.service.create_baseline(other['id'],dict(name='Quiet',location='Room',confirm=True))
        try:
            with self.assertRaises(WorkerError):self.service.arm(other['id'],{})
            self.assertEqual(self.service.preparing[self.project['id']],preparation['active_session_id'])
            self.assertEqual(self.sessions.get_session(preparation['active_session_id']).state.value,'running')
            self.assertEqual(sum(x['alive'] for x in self.workers.list_workers(preparation['active_session_id'])),2)
            self.assertEqual(self.service.project(self.project['id'])['state'],'collecting')
        finally:self.service.disarm(other['id'],{});self.service.stop_preparation(self.project['id'],{})

    def test_inspected_serial_baseline_survives_fresh_guard_acquisition_context(self):
        from tests.test_serial_instruments import serial_session,TinySATransport
        from radioinspector.serial_instruments import TinySASerialAdapter
        from radioinspector.device_workers import ARDV1Simulator
        session=self.sessions.create_session(serial_session())
        project=self.service.create(dict(name='Inspected',site='Fixture',session_id=session.id))
        self.service.save_preparation(project['id'],dict(antenna_notes='Fixture',receive_path_confirmed=True,location_confirmed=True))
        def transport():
            value=TinySATransport.valid();value.responses['help']=['info version deviceid output mode lna attenuate rbw calc scan help\r\nch>']
            value.responses['scan 100M 101M 3 3']*=100
            return value
        adapter=TinySASerialAdapter(session.streams[0],transport=transport())
        adapter.execute('instrument',{'operation':'inspect'})
        now=utc_now()
        for i in range(20):
            item=adapter.poll();started=now-timedelta(seconds=80-4*i)
            item.interval=TimeInterval(started,started+timedelta(seconds=1));self.sessions.ingest_sweep(item)
        adapter.close()
        self.service.create_baseline(project['id'],dict(name='Inspected quiet',location='Fixture',confirm=True))
        def factory(stream,**kwargs):
            return TinySASerialAdapter(stream,transport=transport()) if stream.role.value=='spectrum_analyzer' else ARDV1Simulator(stream)
        with patch('radioinspector.serial_instruments.create_device_adapter',side_effect=factory):
            armed=self.service.arm(project['id'],{})
            try:
                wait_until(lambda:self.service.project(project['id']).get('detector_cursor') is not None or
                    self.service.project(project['id'])['state']=='error','fresh serial sweep analysis')
                self.assertEqual(self.service.project(project['id'])['state'],'armed')
                self.assertTrue(self.service.guard_status(project['id'])['active'])
                self.assertEqual(sum(x['alive'] for x in self.workers.list_workers(armed['active_session_id'])),2)
            finally:self.service.disarm(project['id'],{})

    def test_modified_serial_baseline_context_fails_preflight_before_stopping_workers(self):
        from tests.test_serial_instruments import serial_session,TinySATransport
        from radioinspector.serial_instruments import TinySASerialAdapter
        session=self.sessions.create_session(serial_session())
        project=self.service.create(dict(name='Modified RX',site='Fixture',session_id=session.id))
        self.service.save_preparation(project['id'],dict(antenna_notes='Fixture',receive_path_confirmed=True,location_confirmed=True))
        transport=TinySATransport.valid();transport.responses['help']=['info version deviceid output mode lna attenuate rbw calc scan help\r\nch>']
        transport.responses['scan 100M 101M 3 3']*=20
        adapter=TinySASerialAdapter(session.streams[0],transport=transport)
        adapter.execute('instrument',{'operation':'inspect'})
        # A different, command-acknowledged RX path cannot be recreated by
        # the conservative startup, which acknowledges LNA off.
        transport.responses['lna on']=['ch>']
        adapter.execute('instrument',{'operation':'setting','parameters':{'name':'lna','value':True}})
        now=utc_now()
        for i in range(20):
            item=adapter.poll();started=now-timedelta(seconds=80-4*i)
            item.interval=TimeInterval(started,started+timedelta(seconds=1));self.sessions.ingest_sweep(item)
        adapter.close()
        self.service.create_baseline(project['id'],dict(name='Changed RX',location='Fixture',confirm=True))
        self.assertFalse(self.service.preflight(project['id'])['ready'])
        before=self.sessions.get_session(session.id).state
        with self.assertRaises(ValueError):self.service.arm(project['id'],{})
        self.assertEqual(self.sessions.get_session(session.id).state,before)
        self.assertEqual(self.workers.active_count,0)

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

    def test_later_receiver_failure_is_visible_and_analyzer_keeps_recording(self):
        # Exercise the real detector: the generic nine-bin sweep fixture does not
        # match the running analyzer and used to stop guard after analyze's mock
        # was removed, racing the assertions below on slower CI runners.
        adapter = TinySASimulator(self.session.streams[0])
        now = utc_now()
        for i in range(20):
            item = adapter.poll()
            started = now - timedelta(seconds=80 - 4*i)
            item.interval = TimeInterval(started, started + timedelta(seconds=1))
            self.sessions.ingest_sweep(item)
        self.service.create_baseline(self.project['id'], dict(name='Quiet', location='Room', confirm=True))
        with patch('radioinspector.device_workers.ARDV1Simulator.poll', side_effect=WorkerError('Lost receiver USB')):
            armed = self.service.arm(self.project['id'], {})
            try:
                wait_until(lambda: self.service.project(self.project['id'])['state'] == 'degraded',
                           'guard receiver failure')
                sid, stream = armed['active_session_id'], armed['analyzer_stream_id']
                before = len(self.sessions.list_sweeps(sid, stream_id=stream))
                wait_until(lambda: len(self.sessions.list_sweeps(sid, stream_id=stream)) >= before + 3,
                           'analyzer must keep archiving after receiver failure')
                wait_until(lambda: bool(self.service.project(self.project['id']).get('detector_cursor')),
                           'real detector must process the compatible analyzer sweeps')
                status = self.service.guard_status(self.project['id'])
                self.assertTrue(status['active'], status['error'])
                self.assertIsNotNone(status['error'])
                self.assertEqual(status['project']['state'], 'degraded')
                self.assertEqual(status['project']['coverage'], 'wide')
                self.assertTrue(next(x for x in status['workers'] if x['stream_id'] == stream)['alive'])
                receiver = next(x for x in status['workers'] if x['stream_id'] == armed['receiver_stream_id'])
                self.assertFalse(receiver['alive'])
                self.assertIn('Lost receiver USB', receiver['last_error'])
            finally:
                # Join guard before restoring fault injection, including failures.
                self.service.disarm(self.project['id'], {})

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

    def test_new_run_cannot_reuse_prior_receiver_result_or_audio(self):
        for i in range(20):self.sessions.ingest_sweep(sweep(i,session=self.session.id,stream=self.session.streams[0].id))
        self.service.create_baseline(self.project['id'],dict(name='Quiet',location='Room',confirm=True))
        levels=[-100.]*9;levels[4]=-92
        for i in range(20,23):self.sessions.ingest_sweep(sweep(i,levels,self.session.id,self.session.streams[0].id))
        self.service.analyze(self.project['id'])
        old=self.service.store.list('event',self.project['id'])[0]
        old['analysis']={'state':'observed','audio':{'id':'prior-audio','session_id':self.session.id}}
        self.service.store.put('event',old)
        with patch.object(self.service,'_launch_guard'):new=self.service.arm(self.project['id'],{})
        for i in range(40,43):self.sessions.ingest_sweep(sweep(i,levels,new['active_session_id'],new['analyzer_stream_id']))
        self.service.analyze(new['id'])
        events=self.service.store.list('event',new['id'])
        self.assertEqual(len(events),2)
        pending=self.service.store.pending_receiver_events(new['id'],new['active_session_id'])
        self.assertEqual(len(pending),1);self.assertNotEqual(pending[0]['id'],old['id'])
        self.assertNotIn('analysis',pending[0]);self.assertEqual(self.service.store.get('event',old['id']),old)
        self.service.disarm(new['id'],{})
