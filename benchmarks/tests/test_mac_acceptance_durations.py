import unittest
from benchmarks.ori_bench.mac_acceptance.checkpoints import assess
from benchmarks.tests.test_mac_acceptance_checkpoints import sample

class CheckpointDurationRegressions(unittest.TestCase):
    def rows(self,role,hold):
        state={'ui_alive':False} if role=='ui_quit' else {role.removeprefix('usb_')+'_present':False}
        return [sample(at_ns=0),sample(at_ns=1_000_000_000,analyzer_sources=11,receiver_sources=4,analyzer_source_id='s11',receiver_source_id='r4',**state),
                sample(at_ns=1_000_000_000+hold,analyzer_sources=12,receiver_sources=5,analyzer_source_id='s12',receiver_source_id='r5',**state),
                sample(at_ns=2_000_000_000+hold,analyzer_sources=13,receiver_sources=6,analyzer_source_id='s13',receiver_source_id='r6')]
    def test_ui_requires_thirty_observed_seconds(self):
        self.assertFalse(assess('ui_quit',self.rows('ui_quit',29_999_999_999))[0])
        self.assertTrue(assess('ui_quit',self.rows('ui_quit',30_000_000_000))[0])
    def test_each_usb_requires_ten_observed_seconds(self):
        for role in ('usb_analyzer','usb_receiver'):
            with self.subTest(role=role):
                self.assertFalse(assess(role,self.rows(role,9_999_999_999))[0])
                self.assertTrue(assess(role,self.rows(role,10_000_000_000))[0])
    def test_missing_or_boolean_clocks_cannot_prove_a_hold(self):
        for clock in (None,True,-1):
            rows=self.rows('ui_quit',30_000_000_000);rows[2]['at_ns']=clock
            self.assertFalse(assess('ui_quit',rows)[0])
    def test_sleep_requires_ten_real_mach_seconds(self):
        for duration,expected in ((9_999_999_999,False),(10_000_000_000,True)):
            rows=[sample(at_ns=0),sample(at_ns=duration,sleep_delta_ns=duration),sample(at_ns=duration+1,analyzer_sources=11,analyzer_source_id='s11')]
            self.assertEqual(assess('sleep',rows)[0],expected)

if __name__=='__main__':unittest.main()
