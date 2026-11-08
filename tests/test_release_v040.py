"""Regression gates for the expanded v0.4.0 release."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radioinspector.panorama import PanoramaStore
from radioinspector.trace import Trace
from radioinspector.threshold import ThresholdLine, power_over_threshold
from radioinspector.units import UnitError
from radioinspector.web.server import ApplicationState


class AbsolutePowerTests(unittest.TestCase):
    def test_relative_units_cannot_be_reported_as_watts(self):
        for unit in ['dBFS', 's_meter_count', 'dB', 'dBuV']:
            with self.subTest(unit=unit):
                with self.assertRaises(UnitError):
                    power_over_threshold(Trace([1,2,3],[-10]*3,unit),
                                         ThresholdLine([1,3],[-20]*2,unit=unit),(1,3))

    def test_mixed_threshold_scale_is_rejected(self):
        with self.assertRaises(UnitError):
            power_over_threshold(Trace([1,2,3],[-10]*3,'dBm'),
                                 ThresholdLine([1,3],[-20]*2,unit='dBW'),(1,3))

    def test_dbw_and_dbm_report_the_same_physical_power(self):
        for unit,offset in [('dBm',0),('dBW',-30)]:
            result=power_over_threshold(Trace([1,2,3],[-10+offset]*3,unit),
                                        ThresholdLine([1,3],[-20+offset]*2,unit=unit),(1,3))
            self.assertAlmostEqual(result,0.00027)


class LegacyDatabaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.a=self.root/'first'/'x.pan';self.b=self.root/'second'/'x.pan'
        for path in [self.a,self.b]:
            with PanoramaStore(path) as store:
                store.append(Trace([1,2,3],[-90,-20,-90]))
        self.state=ApplicationState(str(self.a))

    def tearDown(self):
        self.state.store.close();self.temp.cleanup()

    def test_failed_load_preserves_every_active_field_and_closes_candidate(self):
        (self.b.parent/'signals.json').write_text('{',encoding='utf-8')
        old=(self.state.store,self.state.database,self.state.signals,self.state.threshold,self.state.version)
        closed=[];real_close=PanoramaStore.close
        def close(store):
            closed.append(store.path);return real_close(store)
        with patch.object(PanoramaStore,'close',close):
            with self.assertRaises(ValueError):self.state.load_database(str(self.b))
        self.assertEqual((self.state.store,self.state.database,self.state.signals,
                          self.state.threshold,self.state.version),old)
        self.assertEqual(closed,[self.b])
        self.assertEqual(self.state.snapshot()['panoramas'],1)

    def test_successful_swap_closes_previous_store_and_notifies_once(self):
        old=self.state.store;version=self.state.version;event=self.state.subscribe()
        closed=[];real_close=PanoramaStore.close
        def close(store):closed.append(store);return real_close(store)
        with patch.object(PanoramaStore,'close',close):self.state.load_database(str(self.b))
        self.assertEqual(closed,[old]);self.assertEqual(self.state.version,version+1)
        self.assertTrue(event.is_set());self.assertEqual(self.state.database,str(self.b))
        self.assertEqual(self.state.store.count,1)

    def test_nonexistent_database_cannot_create_files(self):
        missing=self.root/'absent'/'x.pan'
        with self.assertRaises(ValueError):self.state.load_database(str(missing))
        self.assertFalse(missing.parent.exists())


class WorkerIsolationTests(unittest.TestCase):
    def test_open_failure_does_not_prevent_healthy_second_stream(self):
        from radioinspector.device_workers import DeviceWorkerManager, WorkerError, create_simulated_adapter
        from radioinspector.session_service import SessionService
        from tests.test_device_workers import simulated_session_payload
        service=SessionService();session=service.create_session(simulated_session_payload());service.start_session(session.id)
        manager=DeviceWorkerManager(service);calls=[]
        def factory(stream,**kwargs):
            calls.append(stream.id)
            if len(calls)==1:raise WorkerError('synthetic open failure')
            return create_simulated_adapter(stream)
        try:
            with patch('radioinspector.serial_instruments.create_device_adapter',side_effect=factory):
                results=manager.start_session(session.id)
            self.assertEqual(len(calls),2)
            self.assertEqual(results[0]['state'],'error')
            self.assertTrue(results[1]['alive'])
        finally:manager.close();service.close()

    def test_port_cannot_be_owned_by_two_sessions_and_is_released_after_stop(self):
        from radioinspector.device_workers import DeviceWorkerManager, WorkerError, TinySASimulator
        from radioinspector.session_service import SessionService
        from tests.test_device_workers import simulated_session_payload
        service=SessionService();manager=DeviceWorkerManager(service)
        payload=simulated_session_payload();payload['streams']=payload['streams'][:1]
        payload['streams'][0]['settings']['adapter']='tinysa-serial';payload['streams'][0]['port']='COM8'
        a=service.create_session(payload);b=service.create_session(payload)
        service.start_session(a.id);service.start_session(b.id)
        try:
            with patch('radioinspector.serial_instruments.create_device_adapter',side_effect=lambda stream,**kw:TinySASimulator(stream)):
                manager.start_stream(a.id,a.streams[0].id)
                with self.assertRaises(WorkerError):manager.start_stream(b.id,b.streams[0].id)
                manager.stop_stream(a.id,a.streams[0].id)
                self.assertTrue(manager.start_stream(b.id,b.streams[0].id)['alive'])
        finally:manager.close();service.close()


class PolicyPatchTests(unittest.TestCase):
    def test_partial_patch_does_not_reenable_concurrently_disabled_policy(self):
        import importlib
        import threading
        from radioinspector.automatic_handoff import AutomaticHandoffScheduler
        from radioinspector.automatic_detection import AutomaticCandidateDetector
        from radioinspector.session_service import SessionService
        from tests.test_device_workers import simulated_session_payload
        service=SessionService();session=service.create_session(simulated_session_payload());service.start_session(session.id)
        try:
            for cls,module in [(AutomaticHandoffScheduler,'automatic_handoff'),(AutomaticCandidateDetector,'automatic_detection')]:
                with self.subTest(cls=cls.__name__):
                    scheduler=cls(service,start_background=False, **({"tune":lambda *args: {}} if cls is AutomaticHandoffScheduler else {}))
                    entered=threading.Event();release=threading.Event();errors=[]
                    try:
                        scheduler.configure(session.id,{'enabled':True})
                        real=importlib.import_module('radioinspector.'+module)._number
                        def number(*args):
                            entered.set()
                            if not release.wait(3):raise AssertionError('validation not released')
                            return real(*args)
                        def configure():
                            try:scheduler.configure(session.id,{'interval_seconds':3})
                            except Exception as exc:errors.append(exc)
                        with patch('radioinspector.'+module+'._number',side_effect=number):
                            worker=threading.Thread(target=configure);worker.start()
                            self.assertTrue(entered.wait(3))
                            scheduler.configure(session.id,{'enabled':False});release.set();worker.join(3)
                        self.assertFalse(worker.is_alive());self.assertFalse(errors)
                        self.assertFalse(scheduler.status(session.id)['policy']['enabled'])
                    finally:release.set();scheduler.close()
        finally:service.close()


class PosixPortOwnershipTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name == 'posix','POSIX serial ownership')
    def test_second_open_is_rejected_before_changing_terminal_settings(self):
        import os
        import pty
        from radioinspector.serial_instruments import PosixSerialTransport
        from radioinspector.device_workers import WorkerError
        master,slave=pty.openpty();path=os.ttyname(slave);first=second=None
        try:
            first=PosixSerialTransport(path)
            with self.assertRaises(WorkerError):
                second=PosixSerialTransport(path)
            first.close();first=None
            second=PosixSerialTransport(path)
        finally:
            if first:first.close()
            if second:second.close()
            os.close(slave);os.close(master)


class OwnedGroupProbeTests(unittest.TestCase):
    def test_linux_probe_distinguishes_live_zombie_and_unreadable_members(self):
        from tests.run_release_checks import _linux_group_has_live_members
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);proc=root/'123';proc.mkdir()
            (proc/'stat').write_text('123 (child (name)) Z 1 456 0 0\n')
            self.assertFalse(_linux_group_has_live_members(456,root))
            (proc/'stat').write_text('123 (child (name)) S 1 456 0 0\n')
            self.assertTrue(_linux_group_has_live_members(456,root))
            (proc/'stat').write_text('malformed')
            self.assertIsNone(_linux_group_has_live_members(456,root))


class DetectionArchiveTests(unittest.TestCase):
    def setUp(self):
        from radioinspector.automatic_detection import AutomaticCandidateDetector
        from radioinspector.session_service import SessionService
        from tests.test_device_workers import simulated_session_payload
        self.service=SessionService(history_limit=5000)
        self.session=self.service.create_session(simulated_session_payload());self.service.start_session(self.session.id)
        self.stream=self.session.streams[0].id
        self.detector=AutomaticCandidateDetector(self.service,start_background=False,history_limit=100)

    def tearDown(self):self.detector.close();self.service.close()

    def ingest(self,name,number=1):
        from tests.test_automatic_detection import spectral_sweep
        return self.service.ingest_sweep(spectral_sweep(self.session.id,self.stream,name,number))

    def test_small_seen_cache_does_not_reprocess_pre_opt_in_archive(self):
        for i in range(110):self.ingest('old-'+str(i),i)
        self.detector.configure(self.session.id,{'enabled':True,'threshold_dbm':-60})
        self.assertEqual(self.detector.run_once(self.session.id),[])
        self.assertEqual(self.service.list_candidates(self.session.id),[])

    def test_failed_candidate_write_leaves_sweep_retryable(self):
        self.detector.configure(self.session.id,{'enabled':True,'threshold_dbm':-60})
        self.ingest('retryable')
        with patch.object(self.service,'create_candidate',side_effect=RuntimeError('transient persistence failure')):
            with self.assertRaises(RuntimeError):self.detector.run_once(self.session.id)
        self.assertEqual(len(self.detector.run_once(self.session.id)),2)

    def test_recent_broad_candidate_retains_its_cooldown_radius(self):
        from types import SimpleNamespace
        policy=self.detector.status(self.session.id)['policy']
        self.detector._remember(self.session.id,self.stream,
                                SimpleNamespace(center_frequency_hz=100000000,bandwidth_hz=500000),policy)
        region={'center_frequency_hz':100400000,'bandwidth_hz':1}
        self.assertTrue(self.detector._duplicate(self.session.id,self.stream,region,[],policy))


class WindowsScopeTests(unittest.TestCase):
    def app(self):
        from types import SimpleNamespace
        from radioinspector.windows.scheduler_app import SchedulerWindowsApp
        from radioinspector.windows.client import PlatformSnapshot
        class Var:
            def __init__(self,value):self.value=value
            def get(self):return self.value
            def set(self,value):self.value=value
        class Tree:
            def __init__(self):self.selected=['stream-a']
            def selection(self):return self.selected
            def get_children(self):return self.selected
            def delete(self,*args):self.selected=[]
        app=SchedulerWindowsApp.__new__(SchedulerWindowsApp)
        app.session_id='A';app._revision=7;app._closed=False;app._action_running=False
        app.snapshot=PlatformSnapshot({}, {}, [{'id':'A'},{'id':'B'}],session={'id':'A','streams':[{'id':'stream-a','role':'spectrum_analyzer','level_unit':'dBm','settings':{'frequency_hz':100000000,'mode':'FM'}}]})
        app.streams_tree=Tree();app.candidates_tree=Tree();app.investigations_tree=Tree()
        app.session_box=SimpleNamespace(current=lambda:1)
        app.frequency_var=Var('102500000');app.mode_var=Var('AM');app.action_var=Var('');app.next_investigation_var=Var('')
        app._last_selected_stream_id='stream-a';app._clear_spectrum=lambda *args:None
        app._render_controls=lambda:None;app._render_spectrum=lambda:None;app.refresh=lambda:None
        return app

    def test_stale_snapshot_does_not_select_other_sessions_spectrum(self):
        app=self.app();app.session_id='B'
        self.assertIsNone(app._spectrum_stream_id())
        app.session_id=None
        self.assertIsNone(app._spectrum_stream_id())

    def test_reselecting_same_stream_preserves_unsent_tune_edits(self):
        app=self.app();app._select_stream()
        self.assertEqual((app.frequency_var.get(),app.mode_var.get()),('102500000','AM'))

    def test_new_session_invalidates_pending_old_snapshot(self):
        app=self.app();app._use_created_session({'id':'C'})
        self.assertGreater(app._revision,7);self.assertIsNone(app.snapshot.session)

    def test_selecting_session_clears_old_stream_state(self):
        app=self.app();app._select_session()
        self.assertEqual(app.session_id,'B');self.assertIsNone(app.snapshot.session)
        self.assertFalse(app.streams_tree.selection());self.assertIsNone(app._spectrum_stream_id())

    def test_old_policy_form_cannot_disable_new_session(self):
        from types import SimpleNamespace
        app=self.app();calls=[]
        client=SimpleNamespace(configure_automatic_handoff=lambda sid,body:calls.append(sid))
        app.client=client;app.session_id='B';app._require_session=lambda:True
        app._submit_action=lambda title,operation:operation()
        app._disable_policy('handoff',session_id='A',client=client)
        self.assertEqual(calls,[])


class CandidateOriginTests(unittest.TestCase):
    def test_candidate_links_must_belong_to_same_session_and_source_stream(self):
        from radioinspector.session_service import SessionService
        from radioinspector.session_store import SQLiteSessionStore
        from tests.test_device_workers import simulated_session_payload
        from tests.test_persistence import sweep_payload
        from tests.test_investigations import candidate_payload
        for persistent in [False,True]:
            with self.subTest(persistent=persistent), tempfile.TemporaryDirectory() as root:
                service=SessionService(store=SQLiteSessionStore(str(Path(root)/'sessions.db'))) if persistent else SessionService()
                try:
                    a=service.create_session(simulated_session_payload());b=service.create_session(simulated_session_payload())
                    source=service.ingest_sweep(sweep_payload(a.id,a.streams[0].id))
                    wrong=candidate_payload(b.streams[0].id,'wrong')
                    wrong['observation_ids']=[source.id]
                    before=len(service.events())
                    with self.assertRaises(ValueError):service.create_candidate(b.id,wrong)
                    self.assertEqual(len(service.events()),before)
                    self.assertEqual(service.list_candidates(b.id),[])
                    valid=candidate_payload(a.streams[0].id,'valid');valid['observation_ids']=[source.id]
                    self.assertEqual(service.create_candidate(a.id,valid).observation_ids,(source.id,))
                    valid['id']='unknown';valid['observation_ids']=['nonexistent-measurement']
                    with self.assertRaises(ValueError):service.create_candidate(a.id,valid)
                finally:service.close()


class InvestigationTransactionTests(unittest.TestCase):
    def test_failed_cancellation_rolls_back_session_and_all_events(self):
        from radioinspector.session_service import SessionService
        from radioinspector.session_store import SQLiteSessionStore
        from tests.test_device_workers import simulated_session_payload
        from tests.test_investigations import candidate_payload
        with tempfile.TemporaryDirectory() as root:
            store=SQLiteSessionStore(str(Path(root)/'archive.db'));service=SessionService(store=store)
            try:
                session=service.create_session(simulated_session_payload());service.start_session(session.id)
                candidate=service.create_candidate(session.id,candidate_payload(session.streams[0].id))
                inv=service.enqueue_investigation(session.id,candidate.id,{'target_stream_id':session.streams[1].id})
                before=len(service.events())
                store._connection.execute("CREATE TRIGGER reject_cancel BEFORE UPDATE ON investigations WHEN NEW.state='cancelled' BEGIN SELECT RAISE(ABORT,'synthetic cancel failure'); END")
                store._connection.commit()
                with self.assertRaises(__import__("sqlite3").IntegrityError):service.stop_session(session.id)
                self.assertEqual(service.get_session(session.id).state.value,'running')
                self.assertEqual(store.load_sessions()[0].state.value,'running')
                self.assertEqual(service.get_investigation(session.id,inv.id).state.value,'queued')
                self.assertEqual(len(service.events()),before)
            finally:service.close()

    def test_stopped_session_rejects_new_queue_entries(self):
        from radioinspector.session_service import SessionService
        from tests.test_device_workers import simulated_session_payload
        from tests.test_investigations import candidate_payload
        service=SessionService()
        try:
            session=service.create_session(simulated_session_payload());service.start_session(session.id)
            candidate=service.create_candidate(session.id,candidate_payload(session.streams[0].id))
            service.stop_session(session.id);before=len(service.events())
            with self.assertRaises(ValueError):service.enqueue_investigation(session.id,candidate.id,{'target_stream_id':session.streams[1].id})
            self.assertEqual(len(service.events()),before)
        finally:service.close()


class TaskBookPermissionTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name == 'posix','POSIX permissions')
    def test_saving_never_chmods_a_replaced_temporary_path(self):
        import os
        import stat
        from radioinspector.tasks import TaskBook
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);book=TaskBook(root/'tasks.json');target=root/'target'
            target.write_text('private');target.chmod(0o600)
            book.path.write_text('{}');book.path.chmod(0o644)
            chmod=os.chmod
            def hostile(path,mode,*args,**kwargs):
                path=Path(path)
                if path.name.startswith('.tasks.json.'):
                    path.unlink();path.symlink_to(target)
                return chmod(path,mode,*args,**kwargs)
            with patch('radioinspector.tasks.os.chmod',side_effect=hostile):book.save()
            self.assertFalse(book.path.is_symlink())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)

    @unittest.skipUnless(__import__('os').name == 'posix','POSIX permissions')
    def test_world_writable_parent_is_rejected_before_saving(self):
        from radioinspector.tasks import TaskBook
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);root.chmod(0o777)
            try:
                with self.assertRaises(ValueError):TaskBook(root/'tasks.json').save()
                self.assertFalse((root/'tasks.json').exists())
            finally:root.chmod(0o700)

class SweepResponseBudgetTests(unittest.TestCase):
    def test_default_waterfall_history_fits_bounded_response_budget(self):
        import io
        import json
        from unittest.mock import Mock
        from radioinspector.contracts import api_payload
        from radioinspector.windows.client import PlatformClient, PlatformClientError
        rows = [{'id':str(n), 'frequencies_hz':list(range(100000000,100004096)),
                 'levels':[-112.345678]*4096} for n in range(40)]
        raw = json.dumps(api_payload(rows)).encode()
        self.assertGreater(len(raw), 2_000_000)
        def opener():
            reply = io.BytesIO(raw)
            reply.status = 200
            return Mock(open=Mock(return_value=reply))
        self.assertEqual(len(PlatformClient('http://localhost', opener=opener()).list_sweeps('s','d')),40)
        with self.assertRaises(PlatformClientError) as error:
            PlatformClient('http://localhost', opener=opener(), max_response_bytes=1024).list_sweeps('s','d')
        self.assertEqual(error.exception.code,'response_too_large')

class SerialReadCancellationTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name == 'posix', 'POSIX serial ownership')
    def test_cancel_interrupts_owned_read_without_reusing_port(self):
        import os
        import pty
        import threading
        import time
        from radioinspector.serial_instruments import PosixSerialTransport
        from radioinspector.device_workers import WorkerError
        master, slave = pty.openpty()
        transport = PosixSerialTransport(os.ttyname(slave))
        errors = []
        def read():
            try: transport.read_line(1.0,'cancel test')
            except WorkerError as error: errors.append(error)
        started = time.monotonic()
        thread = threading.Thread(target=read)
        try:
            thread.start()
            cancel = getattr(transport,'cancel',None)
            if cancel is not None: cancel()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic()-started, .8)
            self.assertTrue(errors)
            with self.assertRaises(WorkerError): transport.read_line(.01,'reuse')
        finally:
            transport.close()
            thread.join(2)
            os.close(master)
            os.close(slave)
