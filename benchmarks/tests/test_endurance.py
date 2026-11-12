"""Lifecycle proof regressions; no fake Core HTTP server or device is used."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.ori_bench.config import BenchmarkError
from benchmarks.ori_bench.endurance import candidate_identity,expected_recovery,export_outcome,idle_workers,lifecycle_config,main,original_json,run_endurance
from benchmarks.ori_bench.client import HTTPFailure
from benchmarks.ori_bench.endurance_proof import EventProof
from benchmarks.ori_bench.endurance_node import OwnedCandidate,publish_stop


def event(sequence=1):
    return dict(sequence=sequence,event_type='sweep.received',session_id='source-é',
                occurred_at='2026-11-11T00:00:00.123456Z',
                payload={'id':'source-é','metadata':{'é':-0.0,'é':-1.0000000000000002},
                         'frequency_hz':9007199254740993})


class EventProofTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)/'events.sqlite'
        self.proof = EventProof(self.path)
        self.addCleanup(self.proof.close)

    def freeze(self):
        self.proof.begin(record=True)
        self.proof.observe(event())
        return self.proof.finish(1)

    def test_exact_original_events_and_ids_are_repeatable(self):
        before = self.freeze()
        self.proof.begin()
        self.proof.observe(event())
        self.assertEqual(self.proof.finish(1),before)

    def test_duplicate_noncontiguous_and_boolean_sequences_fail(self):
        for sequence in (True,0,2):
            with self.subTest(sequence=sequence),self.assertRaises(BenchmarkError):
                self.proof.begin(record=True)
                self.proof.observe(event(sequence))
        self.proof.begin(record=True)
        self.proof.observe(event())
        with self.assertRaises(BenchmarkError):
            self.proof.observe(event())

    def test_changed_raw_float_bits_integer_hz_unicode_or_ids_are_never_normalized(self):
        self.freeze()
        for change in ('zero','adjacent','hz','unicode','id'):
            changed = copy.deepcopy(event())
            if change=='zero':changed['payload']['metadata']['é']=0.0
            if change=='adjacent':changed['payload']['metadata']['é']=-1.0
            if change=='hz':changed['payload']['frequency_hz']=9007199254740992
            if change=='unicode':changed['payload']['metadata'].pop('é')
            if change=='id':changed['payload']['id']='source-é'
            with self.subTest(change=change),self.assertRaises(BenchmarkError):
                self.proof.begin()
                self.proof.observe(changed)

    def test_missing_tail_and_forged_watermark_cannot_complete(self):
        self.freeze()
        self.proof.begin()
        with self.assertRaises(BenchmarkError):self.proof.finish(1)
        self.proof.begin()
        self.proof.observe(event())
        with self.assertRaises(BenchmarkError):self.proof.finish(2)


class EnduranceCommandTests(unittest.TestCase):
    def test_public_identity_must_be_healthy_replay_rust_before_ingestion(self):
        capabilities = dict(storage_mode='sqlite',candidate=dict(mode='replay'))
        health = dict(status='ok',backend='rust-candidate')
        candidate_identity(capabilities,health)
        for changed_cap,changed_health in [
            (capabilities,dict(health,backend='python')),
            (capabilities,dict(health,status='error')),
            (dict(capabilities,candidate=dict(mode='shadow')),health),
            (dict(capabilities,storage_mode='shadow'),health),
        ]:
            with self.assertRaises(BenchmarkError):candidate_identity(changed_cap,changed_health)
    def test_missing_acknowledged_source_is_an_integrity_failure_not_a_missing_binary(self):
        class MissingOriginal:
            def json(self,_path):raise HTTPFailure(404)
        with self.assertRaises(BenchmarkError) as caught:
            original_json(MissingOriginal(),'/api/v1/sessions/owned/sweeps/original')
        self.assertEqual(caught.exception.code,'archive_invalid')
        self.assertEqual(caught.exception.operation,'GET /api/v1/sessions/owned/sweeps/original')
    def test_missing_export_after_restart_never_turns_into_an_overall_pass(self):
        passed = dict(status='passed',ndjson_sha256='a'*64)
        self.assertEqual(export_outcome([passed,passed,passed])['status'],'passed')
        for epochs in ([passed,dict(status='unavailable'),passed],
                       [dict(status='unavailable')]*3):
            self.assertEqual(export_outcome(epochs)['status'],'unavailable')
        with self.assertRaises(BenchmarkError):export_outcome([passed,passed])
        with self.assertRaises(BenchmarkError):
            export_outcome([passed,dict(status='passed',ndjson_sha256='b'*64),passed])
    def test_incompatible_real_executable_is_unavailable_and_owned_tree_is_joined(self):
        with tempfile.TemporaryDirectory() as temporary:
            report,status = run_endurance(Path(temporary)/'new',candidate=Path(sys.executable).resolve(),samples=1)
            self.assertEqual((status,report['run_status']),(2,'unavailable'))
            self.assertFalse(report['successful'])
            self.assertFalse(report['seven_day_sampled_timeline_verified'])
            self.assertTrue(all(node['joined'] for node in report['owned_nodes']))
            self.assertFalse((Path(temporary)/'new/producer/offers.sqlite').exists())

    def test_changed_binary_refuses_launch_before_creating_an_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'output';output.mkdir()
            binary = Path(temporary)/'binary';binary.write_bytes(b'first immutable bytes')
            owner = OwnedCandidate(binary,output)
            binary.write_bytes(b'changed bytes')
            with self.assertRaises(BenchmarkError):owner.start()
            binary.write_bytes(b'first immutable bytes')
            with self.assertRaises(BenchmarkError):owner.start()
            self.assertEqual(owner.records,[])
            self.assertFalse((output/'data').exists())

    def test_explicit_candidate_environment_is_used_without_a_default_or_fake_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'new'
            absent = str(Path(temporary)/'missing-candidate')
            with patch.dict(os.environ,{'ORI_CORE_ENDURANCE_BINARY':absent}),contextlib.redirect_stdout(io.StringIO()):
                status = main(['--output-dir',str(output),'--samples','1'])
            report = json.loads((output/'report.json').read_bytes())
            self.assertEqual((status,report['run_status']),(2,'unavailable'))
            self.assertIn('Requested immutable candidate',report['errors'][0]['message'])
            self.assertFalse((output/'data').exists())

    def test_configured_idle_streams_are_distinct_from_active_device_workers(self):
        config = lifecycle_config('http://127.0.0.1:1',Path('/unused'),'idle',3)
        rows = [dict(session_id=config.session_id,stream_id=stream,alive=False,measurements=0,
                     commands_completed=0,started_at=None,last_measurement_id=None)
                for stream in (config.spectrum_id,config.receiver_id)]
        self.assertEqual(idle_workers(rows,config),rows)
        for key,value in [('alive',True),('measurements',1),('commands_completed',1),
                          ('measurements',False),('started_at','2026-11-11T00:00:00Z')]:
            changed = copy.deepcopy(rows);changed[0][key] = value
            with self.subTest(key=key),self.assertRaises(BenchmarkError):idle_workers(changed,config)

    def test_expected_recovery_changes_only_explicit_runtime_stream_state(self):
        before = dict(state='running',metadata={'é':-0.0,'é':9007199254740993},
                      streams=[dict(id='é',state=None,settings={'level':-1.0000000000000002})])
        original = copy.deepcopy(before)
        after = expected_recovery(before)
        self.assertEqual(before,original)
        self.assertEqual(after['streams'][0]['state'],'disconnected')
        after['streams'][0]['state'] = None
        self.assertEqual(after,before)
    def test_private_stop_publication_is_complete_and_never_replaces_an_unknown_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'stop'
            publish_stop(path,'a'*32)
            self.assertEqual(path.read_bytes(),b'a'*32)
            with self.assertRaises(FileExistsError):publish_stop(path,'b'*32)
            self.assertEqual(path.read_bytes(),b'a'*32)
            self.assertEqual(list(path.parent.iterdir()),[path])
    def test_original_lifecycle_shape_and_pcm_denominator_are_reused(self):
        config = lifecycle_config('http://127.0.0.1:1',Path('/unused'),'sampled-week',168)
        self.assertEqual(config.scenario,'lifecycle')
        self.assertEqual(config.duration_seconds,604800)
        self.assertEqual(config.lifecycle_step_seconds,3600)
        self.assertEqual(config.planned_counts(),dict(sweep=168,observation=168,audio=168))
        self.assertEqual(config.planned_audio_frames,1680*48000)
        self.assertEqual(config.speed,86400)

    def test_missing_candidate_is_explicit_unavailable_without_canonical_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'new'
            report,status = run_endurance(output,candidate=None,samples=3,run_id='missing')
            self.assertEqual(status,2)
            self.assertEqual(report['run_status'],'unavailable')
            self.assertFalse(report['successful'])
            self.assertFalse(report['hardware_tested'])
            self.assertFalse(report['seven_day_sampled_timeline_verified'])
            self.assertFalse((output/'data').exists())
            self.assertEqual(json.loads((output/'report.json').read_bytes())['run_status'],'unavailable')

    def test_previous_reports_and_unknown_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'existing'
            output.mkdir()
            marker = output/'previous-report.json'
            marker.write_bytes(b'original evidence')
            with self.assertRaises(BenchmarkError):run_endurance(output,candidate=None)
            self.assertEqual(marker.read_bytes(),b'original evidence')
            self.assertEqual(list(output.iterdir()),[marker])

    def test_sample_bounds_cannot_inflate_a_seven_day_result(self):
        for count in (0,169,True,1.5):
            with self.subTest(count=count),self.assertRaises(BenchmarkError):
                lifecycle_config('http://127.0.0.1:1',Path('/unused'),'bad',count)


if __name__=='__main__':unittest.main()
