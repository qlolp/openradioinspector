import copy
import json
from pathlib import Path
import tempfile
import unittest
from benchmarks.ori_bench.mac_acceptance.sources import SourceProof
from benchmarks.ori_bench.config import BenchmarkError


class OriginalSourceProofTests(unittest.TestCase):
    def test_hardware_sources_must_be_bound_to_original_commit_receipts(self):
        fixture=json.loads((Path(__file__).resolve().parents[2]/'contracts/fixtures/measurement-v1.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            proof=SourceProof(Path(d)/'proof.sqlite')
            for kind in ('sweep','observation'):
                row={'kind':kind,'measurement':fixture[kind]};proof.original(row);proof.restored(row)
            self.assertEqual(proof.finish(require_commits=True)['status'],'failed')
            for kind in ('sweep','observation'):
                proof.committed({'kind':kind,'session_id':fixture[kind]['session_id'],'source_id':fixture[kind]['id']})
            self.assertTrue(proof.finish(require_commits=True)['commits_verified'])
            with self.assertRaises(BenchmarkError):proof.committed({'kind':'sweep','session_id':'foreign','source_id':'missing'})
            proof.close()

    def test_actual_values_not_just_counts_are_required_after_restore(self):
        fixture=json.loads((Path(__file__).resolve().parents[2]/'contracts/fixtures/measurement-v1.json').read_text())
        rows=[{'kind':'sweep','measurement':fixture['sweep']},
              {'kind':'observation','measurement':fixture['observation']}]
        with tempfile.TemporaryDirectory() as d:
            proof=SourceProof(Path(d)/'proof.sqlite')
            for row in rows:proof.original(row)
            changed=copy.deepcopy(rows[0]);changed['measurement']['levels'][0]+=1
            with self.assertRaises(BenchmarkError):proof.restored(changed)
            for row in reversed(rows):proof.restored(row)
            result=proof.finish()
            self.assertEqual(result['status'],'passed')
            self.assertEqual(result['original_sha256'],result['restored_sha256'])
            self.assertEqual((result['sweeps'],result['observations']),(1,1))
            proof.close()

    def test_duplicate_or_missing_original_is_not_hidden_by_equal_counts(self):
        fixture=json.loads((Path(__file__).resolve().parents[2]/'contracts/fixtures/measurement-v1.json').read_text())
        row={'kind':'sweep','measurement':fixture['sweep']}
        with tempfile.TemporaryDirectory() as d:
            proof=SourceProof(Path(d)/'proof.sqlite');proof.original(row)
            with self.assertRaises(BenchmarkError):proof.original(row)
            self.assertEqual(proof.finish()['status'],'failed')
            proof.restored(row)
            with self.assertRaises(BenchmarkError):proof.restored(row)
            proof.close()
