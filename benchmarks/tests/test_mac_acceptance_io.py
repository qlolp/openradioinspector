import json
from pathlib import Path
import tempfile
import unittest
from benchmarks.ori_bench.mac_acceptance.evidence import read_json_lines, private_json, verify_host
from benchmarks.ori_bench.config import BenchmarkError


class AcceptanceEvidenceIOTests(unittest.TestCase):
    def test_duplicate_keys_and_incomplete_tail_are_not_treated_as_empty_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace'
            for raw in [b'{"stage":"presented","stage":"gpu_completed"}\n',b'{"stage":"presented"',b'[]\n']:
                p.write_bytes(raw)
                with self.assertRaises(BenchmarkError):list(read_json_lines(p))
            p.write_bytes(b'{"stage":"presented","at_ns":9007199254740993}\n')
            self.assertEqual(list(read_json_lines(p))[0]['at_ns'],9007199254740993)

    def test_symlink_and_oversized_line_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'trace';p.write_bytes(b'{"x":"'+b'a'*65536+b'"}\n')
            with self.assertRaises(BenchmarkError):list(read_json_lines(p))
            link=Path(d)/'linked';link.symlink_to(p)
            with self.assertRaises(BenchmarkError):list(read_json_lines(link))

    def test_private_request_is_atomic_and_never_overwrites_an_existing_one(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'request.json'
            private_json(p,{'run_id':'original'})
            self.assertEqual(json.loads(p.read_text()),{'run_id':'original'})
            with self.assertRaises(BenchmarkError):private_json(p,{'run_id':'replacement'})
            self.assertEqual(json.loads(p.read_text()),{'run_id':'original'})
            self.assertFalse(list(Path(d).glob('*.pending')))

    def test_linux_or_wrong_mac_cannot_close_a_mac_acceptance_gate(self):
        good=dict(system='Darwin',architecture='arm64',macos_version='27.3.1',memory_bytes=16*1024**3)
        self.assertTrue(verify_host(good))
        for key,value in [('system','Linux'),('architecture','x86_64'),('macos_version','27.2'),('memory_bytes',8*1024**3)]:
            with self.subTest(key=key):self.assertFalse(verify_host(dict(good,**{key:value})))
