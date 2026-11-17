import copy
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.ori_bench.config import BenchmarkError
from benchmarks.ori_bench.mac_acceptance.hardware import require_load

IDENTITY=dict(source_revision='a'*40,core_sha256='b'*64,native_sha256='c'*64,supervisor_sha256='d'*64)


def accepted_load(run='first',transport='binary-v1'):
    return dict(profile='macos-final-load',run_id=run,point2_passed=True,smoke_only=False,identity=IDENTITY,
        kit_verified_after_run=True,kit_manifest_sha256='e'*64,
        successful=True,candidate_verified_after_run=True,producer_status=0,production_elapsed_seconds=1800,
        actual_transports=[transport],queue_errors=[],errors=[],
        counts={kind:dict(offered=18000,acknowledged=18000,verified=18000,rejected=0,queue_dropped=0)
                for kind in ('sweep','observation')},
        components={key:dict(status='passed') for key in ('production','native','resources','online_backup','final_backup','restore','cleanup')},
        native=dict(status='passed',errors=[],diagnostics=dict(required=True),
            commit_to_presented=dict(status='passed',samples=18000,missing=[],p95_ms=200),
            reaction=dict(status='passed',samples=1900,missing=[],p95_ms=80),
            queue_ack=dict(status='passed',samples=100,missing=[],p95_ms=100),
            pan_zoom=dict(status='passed',windows=3,minimum_fps=50)),
        resources=dict(status='passed'),restore=dict(status='passed',source_counts={'sweep':18000,'observation':18000}),
        software_concurrency_observed=True)


class FinalLoadGateTests(unittest.TestCase):
    def check(self,row):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'report.json';p.write_text(json.dumps(row))
            return require_load(p,IDENTITY)

    def test_flag_only_or_incomplete_source_report_is_not_hardware_admission(self):
        old=dict(point2_passed=True,smoke_only=False,identity=IDENTITY,run_id='old')
        with self.assertRaises(BenchmarkError):self.check(old)
        row=accepted_load();row['counts']['sweep']['verified']=17999
        with self.assertRaises(BenchmarkError):self.check(row)

    def test_every_component_and_original_threshold_is_still_required(self):
        for fault in ('resource','duration','presentation','pan','backup'):
            with self.subTest(fault=fault):
                row=accepted_load()
                if fault=='resource':row['resources']['status']='unmeasured'
                elif fault=='duration':row['production_elapsed_seconds']=1799.999
                elif fault=='presentation':row['native']['commit_to_presented']['p95_ms']=250.001
                elif fault=='pan':row['native']['pan_zoom']['windows']=2
                else:row['components']['final_backup']['status']='failed'
                with self.assertRaises(BenchmarkError):self.check(row)
        self.assertEqual(self.check(accepted_load()),'first')

    def test_two_distinct_full_transports_are_required_on_the_same_candidate(self):
        from benchmarks.ori_bench.mac_acceptance.hardware import require_load_pair
        with tempfile.TemporaryDirectory() as directory:
            a,b=Path(directory)/'a.json',Path(directory)/'b.json'
            a.write_text(json.dumps(accepted_load()))
            b.write_text(json.dumps(accepted_load('second','sse')))
            self.assertEqual(require_load_pair(a,b,IDENTITY),['first','second'])
            b.write_text(json.dumps(accepted_load('second','binary-v1')))
            with self.assertRaises(BenchmarkError):require_load_pair(a,b,IDENTITY)
            with self.assertRaises(BenchmarkError):require_load_pair(a,a,IDENTITY)
