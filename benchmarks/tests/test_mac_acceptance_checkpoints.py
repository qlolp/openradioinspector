import unittest
from benchmarks.ori_bench.mac_acceptance.checkpoints import assess


def sample(**kw):
    value=dict(active=True,coverage='wide',analyzer_sources=10,receiver_sources=3,
               analyzer_source_id='s10',receiver_source_id='r3',runtime_instance_id='same',
               ui_alive=True,ui_exit_code=0,analyzer_present=True,receiver_present=True,preparation_valid=True)
    value.update(kw);return value


class HardwareCheckpointTests(unittest.TestCase):
    def test_ui_quit_requires_actual_closed_ui_and_new_sources_from_same_core(self):
        before=sample(at_ns=0)
        self.assertFalse(assess('ui_quit',[before,sample(analyzer_sources=12,analyzer_source_id='s12')])[0])
        rows=[before,sample(at_ns=1_000_000_000,ui_alive=False,analyzer_sources=12,analyzer_source_id='s12'),
              sample(at_ns=31_000_000_000,ui_alive=False,analyzer_sources=13,analyzer_source_id='s13'),
              sample(at_ns=32_000_000_000,analyzer_sources=14,analyzer_source_id='s14')]
        self.assertTrue(assess('ui_quit',rows)[0])
        rows[1]['ui_exit_code']=1
        self.assertFalse(assess('ui_quit',rows)[0])
        rows[1]['ui_exit_code']=0
        rows[1]['runtime_instance_id']='replacement'
        self.assertFalse(assess('ui_quit',rows)[0])

    def test_usb_checkbox_cannot_replace_absent_device_and_recovery_evidence(self):
        self.assertFalse(assess('usb_analyzer',[sample(),sample(analyzer_sources=11,analyzer_source_id='s11')])[0])
        rows=[sample(at_ns=0),sample(at_ns=1_000_000_000,analyzer_present=False,receiver_sources=4,receiver_source_id='r4'),
              sample(at_ns=11_000_000_000,analyzer_present=False,receiver_sources=5,receiver_source_id='r5'),
              sample(at_ns=12_000_000_000,analyzer_sources=12,analyzer_source_id='s12')]
        self.assertTrue(assess('usb_analyzer',rows)[0])
        rows[-1]['preparation_valid']=False
        self.assertFalse(assess('usb_analyzer',rows)[0])

    def test_baseline_requires_twenty_sources_sixty_seconds_and_confirmation(self):
        row=sample(baseline_confirmed=True,baseline_sources=20,baseline_span=60,baseline_id='b')
        self.assertTrue(assess('baseline',[row])[0])
        for key,value in [('baseline_sources',19),('baseline_span',59.9),('baseline_confirmed',False)]:
            self.assertFalse(assess('baseline',[dict(row,**{key:value})])[0])

    def test_source_search_requires_narrow_then_returned_wide_sources(self):
        self.assertFalse(assess('source_search',[sample(),sample(coverage='narrow')])[0])
        self.assertTrue(assess('source_search',[sample(),sample(coverage='narrow'),
                                              sample(analyzer_sources=12,analyzer_source_id='s12')])[0])

    def test_receiver_does_not_pass_while_analyzer_has_stopped(self):
        rows=[sample(),sample(receiver_sources=4,receiver_source_id='r4',receiver_evidence_id='check')]
        self.assertFalse(assess('receiver',rows)[0])
        rows[-1].update(analyzer_sources=12,analyzer_source_id='s12')
        self.assertTrue(assess('receiver',rows)[0])
