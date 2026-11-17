import unittest
from benchmarks.ori_bench.mac_acceptance.resources import parse_darwin_processes,assess_resources

class MacResourceRegressions(unittest.TestCase):
    def test_ps_preserves_identity_rss_and_paths_with_spaces(self):
        table=' 42 1 Mon Nov 16 22:00:00 2026 1024 /Applications/OpenRadioInspector RC.app/Contents/Helpers/ori-core\n 43 42 Mon Nov 16 22:00:01 2026 512 /usr/bin/helper\n'
        rows=parse_darwin_processes(table)
        self.assertEqual(rows[42]['parent'],1);self.assertEqual(rows[42]['rss'],1048576)
        self.assertEqual(rows[42]['start'],'Mon Nov 16 22:00:00 2026')
        self.assertTrue(rows[42]['executable'].endswith('RC.app/Contents/Helpers/ori-core'))
    def test_malformed_or_duplicate_process_identity_is_not_a_valid_snapshot(self):
        for data in ('42 1 bad','42 1 Mon Nov 16 22:00:00 2026 -1 /usr/bin/no',
                     '42 1 Mon Nov 16 22:00:00 2026 1 /a\n42 1 Mon Nov 16 22:00:00 2026 1 /a'):
            with self.assertRaises(ValueError):parse_darwin_processes(data)
    def facts(self):
        return dict(status='measured',process_trees={name:dict(samples=100,missing_samples=0,peak_root_rss_bytes=limit,peak_tree_rss_bytes=limit)
                    for name,limit in [('core',256*1024**2),('native_ui',1024**3),('background',1536*1024**2)]})
    def test_exact_memory_boundaries_pass_one_byte_over_and_missing_measurement_fail(self):
        self.assertEqual(assess_resources(self.facts())['status'],'passed')
        for name in ('core','native_ui','background'):
            facts=self.facts();facts['process_trees'][name]['peak_tree_rss_bytes']+=1
            facts['process_trees'][name]['peak_root_rss_bytes']+=1
            self.assertEqual(assess_resources(facts)['status'],'failed')
        facts=self.facts();facts['process_trees']['native_ui']['samples']=0
        self.assertEqual(assess_resources(facts)['status'],'failed')

    def test_two_endpoints_cannot_replace_continuous_memory_samples(self):
        facts=self.facts();facts['interval_seconds']=.5
        for row in facts['process_trees'].values():
            row.update(samples=2,first_sample_ns=1,last_sample_ns=1_800_000_000_001,max_sample_gap_ns=1_800_000_000_000)
        self.assertEqual(assess_resources(facts,minimum_seconds=1800)['status'],'failed')
        for row in facts['process_trees'].values():row.update(samples=3500,max_sample_gap_ns=550_000_000)
        self.assertEqual(assess_resources(facts,minimum_seconds=1800)['status'],'passed')
        facts=self.facts();facts['process_trees']['core']['missing_samples']=1
        self.assertEqual(assess_resources(facts)['status'],'failed')

    def test_sampling_gap_boundary_is_preserved_with_sufficient_sample_count(self):
        facts=self.facts();facts['interval_seconds']=.5
        for row in facts['process_trees'].values():
            row.update(samples=3500,first_sample_ns=1,last_sample_ns=1_800_000_000_001,max_sample_gap_ns=2_000_000_000)
        self.assertEqual(assess_resources(facts,minimum_seconds=1800)['status'],'passed')
        facts['process_trees']['core']['max_sample_gap_ns']+=1
        self.assertEqual(assess_resources(facts,minimum_seconds=1800)['status'],'failed')

if __name__=='__main__':unittest.main()
