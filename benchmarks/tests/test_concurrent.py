"""Bounded concurrent-load evidence; these unit cases are not Core acceptance."""
import copy
from pathlib import Path
import unittest

from benchmarks.ori_bench.config import BenchmarkError, Config
from benchmarks.ori_bench.concurrent_proof import ExportProof, overlap, reference_config
from benchmarks.ori_bench.source import encode, observation, sweep


class ConcurrentProofTests(unittest.TestCase):
    def setUp(self):
        self.config = Config('http://127.0.0.1:1', Path('/unused'), run_id='concurrent',
                             duration_seconds=1, points=8, history_repeats=1)

    def proof(self, sweeps=1, observations=1, **budgets):
        return ExportProof(self.config, {'X-Archive-Sweeps': str(sweeps),
            'X-Archive-Observations': str(observations), 'X-Archive-Boundary': '7'}, **budgets)

    def records(self):
        return [dict(format='openradioinspector-archive-ndjson', format_version=1,
                     session_id=self.config.session_id),
                dict(kind='sweep', measurement=sweep(self.config, 0)),
                dict(kind='observation', measurement=observation(self.config, 0))]

    def verify(self, records=None, **budgets):
        proof = self.proof(**budgets)
        for record in self.records() if records is None else records:
            proof.observe(encode(record) + b'\n')
        return proof.finish()

    def test_default_still_uses_the_full_original_load_and_three_clients(self):
        config = reference_config('http://127.0.0.1:1', Path('/unused'), 'reference')
        self.assertEqual(config.planned_counts(), dict(sweep=18000, observation=18000, audio=180))
        self.assertEqual(config.planned_audio_frames, 86400000)
        self.assertEqual((config.duration_seconds, config.points, config.speed), (1800, 4096, 1))
        self.assertEqual((config.request_timeout, config.bulk_timeout, config.drain_seconds), (10, 600, 60))
        self.assertTrue(config.observe_render)
        self.assertEqual(config.observer_clients, 3)

    def test_original_midload_records_have_a_stable_boundary_and_hash(self):
        result = self.verify()
        self.assertEqual(result['source_counts'], dict(sweep=1, observation=1))
        self.assertEqual(result['source_boundary'], 7)
        self.assertEqual(len(result['ndjson_sha256']), 64)
        self.assertEqual(result, self.verify())
        self.assertFalse(result['all_planned_inputs_claimed'])

    def test_changed_float_bits_hz_raw_unit_or_identity_never_verify(self):
        for mutation in ('zero', 'adjacent', 'hz', 'unit', 'id'):
            records = self.records()
            item = records[1]['measurement']
            if mutation == 'zero': item['levels'][1] = 0.0
            if mutation == 'adjacent': item['levels'][2] = -1.0
            if mutation == 'hz': item['frequencies_hz'][0] += 1
            if mutation == 'unit': item['level_unit'] = 's_meter_count'
            if mutation == 'id': item['id'] += '-foreign'
            with self.subTest(mutation=mutation), self.assertRaises(BenchmarkError):
                self.verify(records)

    def test_duplicate_missing_truncated_or_overbudget_export_is_not_a_pass(self):
        records = self.records()
        for changed in (records[:-1], records + [copy.deepcopy(records[1])]):
            with self.assertRaises(BenchmarkError): self.verify(changed)
        proof = self.proof()
        with self.assertRaises(BenchmarkError): proof.observe(encode(records[0]))
        with self.assertRaises(BenchmarkError): self.verify(max_bytes=32)
        with self.assertRaises(BenchmarkError): self.verify(max_frame_bytes=32)
        changed = self.records()
        changed[0]['format_version'] = True
        with self.assertRaises(BenchmarkError): self.verify(changed)
        with self.assertRaises(BenchmarkError):
            ExportProof(self.config, {'X-Archive-Sweeps': 'true',
                'X-Archive-Observations': '1', 'X-Archive-Boundary': '7'})
        with self.assertRaises(BenchmarkError):
            ExportProof(self.config, {'X-Archive-Sweeps': '1' * 65536,
                'X-Archive-Observations': '1', 'X-Archive-Boundary': '7'})

    def test_request_start_alone_cannot_prove_simultaneous_owned_work(self):
        backup = {'phase': 'running'}
        raw = {'phase': 'streaming', 'verified_records': 1}
        history = {'phase': 'running'}
        qos = dict(registered_work=3, cleanup_failed=False, acquisition_pressure=False,
                   cross_process_logical_io=True)
        self.assertTrue(overlap(backup, raw, history, qos))
        for changed in ({'phase': 'requested'}, {'phase': 'completed'}, {}):
            self.assertFalse(overlap(changed, raw, history, qos))
        self.assertFalse(overlap(backup, dict(raw, verified_records=0), history, qos))
        self.assertFalse(overlap(backup, raw, {'phase': 'snapshot'}, qos))
        for changed in (dict(qos, registered_work=2), dict(qos, registered_work=True),
                        dict(qos, cleanup_failed=True), dict(qos, acquisition_pressure=True),
                        dict(qos, cross_process_logical_io=False)):
            self.assertFalse(overlap(backup, raw, history, changed))


if __name__ == '__main__':
    unittest.main()
