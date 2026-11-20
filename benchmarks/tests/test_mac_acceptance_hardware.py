import json
from pathlib import Path
import tempfile
import unittest
import threading
import time
from unittest.mock import patch
from benchmarks.ori_bench.mac_acceptance.hardware import Sampler,Collector,require_load
from benchmarks.ori_bench.config import BenchmarkError
from benchmarks.ori_bench.mac_acceptance.load import NativeWindow
from benchmarks.ori_bench.mac_acceptance.evidence import private_json


class HardwareControllerTests(unittest.TestCase):
    def test_backup_failure_keeps_closed_hardware_and_native_diagnostics(self):
        from benchmarks.ori_bench.mac_acceptance.hardware import finalize_hardware_evidence
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);native=root/'native-0';native.mkdir()
            sha='a'*64;run='partial-hardware'
            rows=[dict(run_id=run,component_sha256=sha,at_ns=i*1_000_000_000,stage='hardware.sample',id='s'+str(i)) for i in (0,1)]
            (root/'hardware.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            (native/'native-1.jsonl').write_text(json.dumps(dict(stage='native.error',run_id=run,at_ns=1,message='fixture observer failed'))+'\n')
            report=dict(identity={'core_sha256':sha},run_id=run,elapsed_seconds=1,
                        errors=[dict(code='backup_failed')],successful=False,point4_passed=False)
            finalize_hardware_evidence(report,root)
            self.assertEqual(report['hardware']['samples'],2)
            self.assertIn('fixture observer failed',report['native']['errors'])
            self.assertFalse(report['point4_passed'])

    def test_unsupported_host_refuses_before_starting_an_owner_process(self):
        from benchmarks.ori_bench.mac_acceptance.node import PackageNode
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);binary=root/'core';binary.write_bytes(b'fixture identity, never executed')
            out=root/'node';out.mkdir();node=PackageNode(binary,out,'test')
            with patch('sys.platform','win32'),patch('benchmarks.ori_bench.endurance_node.OwnedCandidate.start',side_effect=AssertionError('must not spawn')):
                with self.assertRaises(BenchmarkError):node.start()
            self.assertEqual(node.records,[])

    def test_expected_usb_gap_exempts_only_that_receiver_and_has_a_finite_end(self):
        from benchmarks.ori_bench.mac_acceptance.freshness import SourceProgress
        progress=SourceProgress()
        first=dict(at_ns=0,analyzer_sources=1,receiver_sources=1,analyzer_source_id='a1',receiver_source_id='r1',
                   analyzer_max_silence_seconds=2,receiver_max_silence_seconds=2)
        self.assertEqual(progress.observe(first),[])
        gap=[(1_000_000_000,4_000_000_000,'usb_receiver')]
        at3=dict(first,at_ns=3_000_000_000)
        self.assertEqual(progress.observe(at3,planned=gap),['analyzer_source_stalled'])
        at4=dict(at3,at_ns=4_000_000_000,analyzer_sources=2,analyzer_source_id='a2')
        self.assertEqual(progress.observe(at4,planned=gap),[])
        at7=dict(at4,at_ns=7_000_000_000,analyzer_sources=3,analyzer_source_id='a3')
        self.assertEqual(progress.observe(at7,planned=gap),['receiver_source_stalled'])

    def test_service_cleanup_requires_registration_removal_for_each_new_run(self):
        for removed in (False,True):
            with self.subTest(registration_removed=removed),tempfile.TemporaryDirectory() as d:
                root=Path(d);window=NativeWindow('/unused.app',root,root/'profile')
                def receipt():
                    deadline=time.monotonic()+2
                    while not (root/'service-stop.request').exists() and time.monotonic()<deadline:time.sleep(.005)
                    if not (root/'service-stop.request').exists():return
                    private_json(root/'native-service-stopped.json',dict(run_id='run',successful=removed,
                        resources_released=True,registration_removed=removed))
                thread=threading.Thread(target=receipt);thread.start()
                try:
                    if removed:self.assertTrue(window.finish_service('run',seconds=2)['registration_removed'])
                    else:
                        with self.assertRaises(BenchmarkError):window.finish_service('run',seconds=2)
                finally:thread.join(3);window.stop('run',required=False)

    def test_checkpoint_waits_for_first_completed_poll_before_prompting(self):
        entered=threading.Event();release=threading.Event();prompted=threading.Event();errors=[]
        class Clock:
            now=staticmethod(time.monotonic_ns);awake=staticmethod(time.monotonic_ns)
        class Delayed:
            identity=None
            def poll(self):
                entered.set();release.wait(3)
                return dict(baseline_confirmed=True,baseline_sources=20,baseline_span=60,baseline_id='real',bands=[])
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);collector=Collector(Delayed(),root/'trace','run','a'*64,Clock(),root)
            def checkpoint():
                try:collector.checkpoint('baseline','baseline')
                except BaseException as error:errors.append(error)
            with patch('builtins.input',side_effect=lambda *args:prompted.set()):
                collector.start();self.assertTrue(entered.wait(1));thread=threading.Thread(target=checkpoint);thread.start()
                try:
                    thread.join(.05)
                    self.assertTrue(thread.is_alive(),'Checkpoint must await the first actual poll')
                    self.assertFalse(prompted.is_set())
                finally:release.set();thread.join(3);collector.close()
            self.assertEqual(errors,[]);self.assertTrue(prompted.is_set())

    def test_simulator_or_wrong_revision_load_cannot_unlock_hardware(self):
        identity=dict(source_revision='a'*40,core_sha256='b'*64,native_sha256='c'*64,supervisor_sha256='d'*64)
        with tempfile.TemporaryDirectory() as d:
            from benchmarks.tests.test_mac_acceptance_load_gate import accepted_load
            p=Path(d)/'load.json';value=accepted_load('load')
            p.write_text(json.dumps(value));self.assertEqual(require_load(p,identity),'load')
            for field,change in [('smoke_only',True),('point2_passed',False),('identity',dict(identity,core_sha256='f'*64))]:
                p.write_text(json.dumps(dict(value,**{field:change})))
                with self.assertRaises(BenchmarkError):require_load(p,identity)

    def test_sampler_uses_actual_archive_and_tune_evidence_not_requested_mode(self):
        class Client:
            def json(self,path):
                if path.endswith('/status'):return dict(active=True,project={'id':'p','active_session_id':'s','session_ids':['s'],
                    'coverage':'wide','baseline_id':'b','profile':{'analyzer':{'port':'/dev/analyzer','settings':{'adapter':'tinysa-serial'}},
                    'receiver':{'port':'/dev/receiver','settings':{'adapter':'ardv1-serial'}}},
                    'preparation':{'receive_path_confirmed':True,'location_confirmed':True,'antenna_notes':'fixture'}},
                    workers=[{'alive':True,'current':{'receiver_firmware':'2504A'}}],
                    receiver_checks={'items':[{'id':'real-check','state':'sampled','receiver':{'observation_id':'o'}}]})
                if path.endswith('/analytics/summary'):return {'sweeps':{'count':22,'latest':{'id':'a','settings':{
                    'device_identity':{'info':'tinySA ULTRA ZS405','firmware':'1.4-235'}}}},'observations':{'count':8,'latest':{'id':'o'}}}
                if '/baselines/' in path:return {'id':'b','confirmed':True,'source_refs':[{}]*20,
                    'started_at':'2026-11-16T12:00:00.000000Z','ended_at':'2026-11-16T12:01:00.000000Z'}
                if path.endswith('/events?limit=1'):return {'items':[{'id':'e','source_refs':[{'sweep_id':'a'}]}]}
                if path.endswith('/health'):return {'runtime_instance_id':'actual-owner'}
                raise AssertionError(path)
        with patch('benchmarks.ori_bench.mac_acceptance.hardware.present',return_value=True):
            sampler=Sampler(Client(),'p',lambda:True);row=sampler.poll()
        self.assertEqual((row['analyzer_sources'],row['receiver_sources']),(22,8))
        self.assertEqual(row['receiver_evidence_id'],'real-check')
        self.assertEqual(row['baseline_span'],60)
        self.assertEqual(row['event_source_ids'],['a'])
        self.assertEqual(sampler.identity['analyzer_model'],'tinySA Ultra')
